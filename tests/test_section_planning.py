"""
Tests for core.services.section_planning and core.section_plan_views.

Covers:
  - compute_section_plan capacity rules (local 4cr, local other, external)
  - custom limits override
  - compute_plan_summary
  - page view access control
  - generate API endpoint
  - export XLSX endpoint
"""

from __future__ import annotations

import json
from collections import Counter

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import (
    Course,
    ElectiveCourse,
    ElectiveTermMapping,
    ProgrammeRequirement,
    Student,
    StudentCourse,
)
from core.section_plan_views import (
    _apply_programme_course_names,
    _format_export_course_name,
    _merge_section_plan_rows_by_course_identity,
)
from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_GENERAL_ADVISOR,
    ROLE_SUPER_ADMIN,
    ensure_role_groups,
    ensure_scope_schema,
    set_user_scope,
)
from core.services.reporting import (
    build_course_identity_aggregate_counts,
    resolve_elective_recommendations,
)
from core.services.section_planning import (
    _load_programme_capacities,
    compute_plan_summary,
    compute_section_plan,
    get_all_courses_with_defaults,
)

pytestmark = pytest.mark.django_db


# ── Helpers ──────────────────────────────────────────────────────


def _login_as(client: Client, username: str, role: str) -> User:
    ensure_role_groups()
    ensure_scope_schema()
    user, _ = User.objects.get_or_create(username=username)
    user.groups.clear()
    user.groups.add(Group.objects.get(name=role))
    set_user_scope(user.id, advisor_id="", departments="")
    client.force_login(user)
    return user


def _create_course(code: str, credit_hours: int, is_external: bool = False) -> Course:
    return Course.objects.create(
        course_code=code,
        credit_hours=credit_hours,
        is_external=is_external,
        department=code[:2] if not is_external else "GS",
    )


# ── Unit tests for compute_section_plan ──────────────────────────


def test_compute_section_plan_local_4cr() -> None:
    """Local 4-credit course → 25 per section (default)."""
    _create_course("CS400", credit_hours=4, is_external=False)
    aggregate: Counter[str] = Counter({"CS400": 100})
    plan = compute_section_plan(aggregate)

    assert len(plan) == 1
    row = plan[0]
    assert row["course_code"] == "CS400"
    assert row["max_per_section"] == 25
    assert row["num_sections"] == 4  # ceil(100/25)
    assert row["total_students"] == 100
    assert row["department"] == "CS"


def test_compute_section_plan_local_3cr() -> None:
    """Local 3-credit course → 40 per section (default)."""
    _create_course("AI201", credit_hours=3, is_external=False)
    aggregate: Counter[str] = Counter({"AI201": 100})
    plan = compute_section_plan(aggregate)

    assert len(plan) == 1
    row = plan[0]
    assert row["max_per_section"] == 40
    assert row["num_sections"] == 3  # ceil(100/40)


def test_compute_section_plan_external() -> None:
    """External course → 50 per section (default)."""
    _create_course("MATH101", credit_hours=3, is_external=True)
    aggregate: Counter[str] = Counter({"MATH101": 100})
    plan = compute_section_plan(aggregate)

    assert len(plan) == 1
    row = plan[0]
    assert row["max_per_section"] == 50
    assert row["num_sections"] == 2  # ceil(100/50)
    assert row["is_external"] is True


def test_compute_section_plan_custom_limits() -> None:
    """Custom capacity limits are respected."""
    _create_course("DS300", credit_hours=4, is_external=False)
    aggregate: Counter[str] = Counter({"DS300": 60})
    plan = compute_section_plan(aggregate, max_local_4cr=20)

    assert len(plan) == 1
    row = plan[0]
    assert row["max_per_section"] == 20
    assert row["num_sections"] == 3  # ceil(60/20)


def test_compute_section_plan_underfilled() -> None:
    """Course with very few students gets 'underfilled' status."""
    _create_course("CYB100", credit_hours=3, is_external=False)
    aggregate: Counter[str] = Counter({"CYB100": 5})
    plan = compute_section_plan(aggregate)

    assert len(plan) == 1
    assert plan[0]["status"] == "underfilled"
    assert plan[0]["num_sections"] == 1


def test_compute_section_plan_full_status() -> None:
    """Course that exactly fills sections gets 'full' status."""
    _create_course("IS250", credit_hours=3, is_external=False)
    aggregate: Counter[str] = Counter({"IS250": 40})
    plan = compute_section_plan(aggregate)

    assert len(plan) == 1
    assert plan[0]["status"] == "full"
    assert plan[0]["num_sections"] == 1
    assert plan[0]["avg_per_section"] == 40


def test_section_planning_resolves_mapped_elective_placeholder() -> None:
    """Mapped elective placeholders are counted as real deliverable courses."""
    student = Student.objects.create(student_id=440000001, program="DS", section="M")
    ai201 = _create_course("AI201", credit_hours=3)
    ds332 = _create_course("DS332", credit_hours=4)
    StudentCourse.objects.create(student=student, course=ai201, status="passed")
    StudentCourse.objects.create(student=student, course=ds332, status="passed")

    elective = ElectiveCourse.objects.create(
        programme="DS",
        course_code="DS485",
        course_name="Decision Support Systems",
        credit_hours=3,
        prerequisites_csv="AI201,DS332",
    )
    ElectiveTermMapping.objects.create(
        academic_year="1448",
        term=1,
        programme="DS",
        placeholder_code="DS2",
        elective=elective,
    )

    resolved = resolve_elective_recommendations(
        {student.student_id: ["DS2", "DS321"]},
        year=1448,
        semester=1,
        program="DS",
    )

    assert resolved[student.student_id] == ["DS485", "DS321"]

    aggregate: Counter[str] = Counter()
    for recs in resolved.values():
        aggregate.update(recs)

    plan = compute_section_plan(aggregate)
    elective_row = next(row for row in plan if row["course_code"] == "DS485")
    assert elective_row["course_name"] == "Decision Support Systems"
    assert elective_row["credit_hours"] == 3


def test_compute_section_plan_prefers_course_metadata_over_elective_catalogue() -> None:
    """Official Course rows remain authoritative when an elective has the same code."""
    Course.objects.create(
        course_code="AI411",
        description="Official Expert Systems",
        credit_hours=4,
        is_external=False,
        department="AI",
    )
    ElectiveCourse.objects.create(
        programme="AI",
        course_code="AI411",
        course_name="Elective Catalogue Expert Systems",
        credit_hours=3,
    )

    plan = compute_section_plan(Counter({"AI411": 25}))

    assert len(plan) == 1
    assert plan[0]["course_name"] == "Official Expert Systems"
    assert plan[0]["credit_hours"] == 4
    assert plan[0]["max_per_section"] == 25


def test_compute_section_plan_fallback_to_programme_requirement() -> None:
    """Course not in Course table falls back to ProgrammeRequirement for credits."""
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="AI999",
        credit_hours=4,
        programme_term=1,
    )
    aggregate: Counter[str] = Counter({"AI999": 50})
    plan = compute_section_plan(aggregate)

    assert len(plan) == 1
    assert plan[0]["credit_hours"] == 4
    assert plan[0]["max_per_section"] == 25  # local 4cr rule


def test_compute_plan_summary() -> None:
    """Summary aggregates correctly across departments."""
    _create_course("CS100", credit_hours=3, is_external=False)
    _create_course("AI100", credit_hours=3, is_external=False)
    aggregate: Counter[str] = Counter({"CS100": 80, "AI100": 40})
    plan = compute_section_plan(aggregate)
    summary = compute_plan_summary(plan)

    assert summary["total_courses"] == 2
    assert summary["total_students"] == 120
    assert summary["total_sections"] == 3  # CS100: ceil(80/40)=2, AI100: ceil(40/40)=1
    assert len(summary["departments"]) == 2


def test_compute_section_plan_empty_aggregate() -> None:
    """Empty aggregate returns empty plan."""
    plan = compute_section_plan(Counter())
    assert plan == []


def test_apply_programme_course_names_prefers_programme_requirement() -> None:
    """Section Planning display uses the plan row name when one exists."""
    Course.objects.create(
        course_code="CS111",
        department="CS",
        description="GLOBAL COURSE NAME",
        credit_hours=3,
    )
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="CS111",
        course_name="PROGRAMMING I",
        credit_hours=3,
        programme_term=1,
    )
    plan = [
        {
            "department": "CS",
            "course_code": "CS111",
            "course_name": "GLOBAL COURSE NAME",
            "credit_hours": 3,
            "is_external": False,
            "total_students": 4,
            "num_sections": 1,
            "max_per_section": 40,
            "avg_per_section": 4,
            "fill_percent": 10,
            "status": "underfilled",
        }
    ]

    updated = _apply_programme_course_names(plan, "AI")

    assert updated[0]["course_name"] == "PROGRAMMING I"


def test_merge_section_plan_rows_keeps_same_code_different_names_separate() -> None:
    """Same code is not merged when the plan-specific course name differs."""
    ai_row = {
        "department": "CS",
        "course_code": "CS111",
        "course_name": "PROGRAMMING I",
        "credit_hours": 3,
        "is_external": False,
        "total_students": 4,
        "num_sections": 1,
        "max_per_section": 40,
        "avg_per_section": 4,
        "fill_percent": 10,
        "status": "underfilled",
    }
    ai2_row = {
        **ai_row,
        "course_name": "FUNDAMENTALS OF PROGRAMMING",
        "total_students": 4,
    }

    merged = _merge_section_plan_rows_by_course_identity([("AI", [ai_row]), ("AI2", [ai2_row])])

    assert len(merged) == 2
    assert {row["course_name"] for row in merged} == {
        "PROGRAMMING I",
        "FUNDAMENTALS OF PROGRAMMING",
    }
    assert {row["total_students"] for row in merged} == {4}
    assert {tuple(row["programs"]) for row in merged} == {("AI",), ("AI2",)}


def test_merge_section_plan_rows_merges_same_code_and_name() -> None:
    """Same code and same plan-specific name still combine safely."""
    row_a = {
        "department": "AI",
        "course_code": "AI201",
        "course_name": "MACHINE LEARNING",
        "credit_hours": 3,
        "is_external": False,
        "total_students": 25,
        "num_sections": 1,
        "max_per_section": 20,
        "avg_per_section": 25,
        "fill_percent": 125,
        "status": "full",
    }
    row_b = {
        **row_a,
        "total_students": 10,
        "max_per_section": 30,
    }

    merged = _merge_section_plan_rows_by_course_identity([("AI", [row_a]), ("AI2", [row_b])])

    assert len(merged) == 1
    assert merged[0]["total_students"] == 35
    assert merged[0]["max_per_section"] == 20
    assert merged[0]["num_sections"] == 2
    assert merged[0]["programs"] == ["AI", "AI2"]


def test_build_course_identity_aggregate_counts_splits_same_code_by_plan_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Course.objects.create(course_code="CS111", description="PROGRAMMING I", credit_hours=4)
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="CS111",
        course_name="PROGRAMMING I",
        credit_hours=4,
    )
    ProgrammeRequirement.objects.create(
        program="AI2",
        course_code="CS111",
        course_name="FUNDAMENTALS OF PROGRAMMING",
        credit_hours=4,
    )
    Student.objects.create(student_id=991101, registration_no="991101", program="AI")
    Student.objects.create(student_id=992101, registration_no="992101", program="AI2")

    monkeypatch.setattr(
        "core.services.reporting.batch_recommend_multi_program",
        lambda student_ids, _year, _term: {sid: ["CS111"] for sid in student_ids},
    )

    student_count, aggregate, metadata = build_course_identity_aggregate_counts(1448, 1)

    assert student_count == 2
    assert aggregate == Counter(
        {
            "CS111::PROGRAMMING_I": 1,
            "CS111::FUNDAMENTALS_OF_PROGRAMMING": 1,
        }
    )
    assert metadata["CS111::PROGRAMMING_I"]["course_name"] == "PROGRAMMING I"
    assert metadata["CS111::FUNDAMENTALS_OF_PROGRAMMING"]["course_name"] == (
        "FUNDAMENTALS OF PROGRAMMING"
    )


def test_format_export_course_name_uses_row_name_and_programs() -> None:
    """XLSX combined rows show both the program tag and plan-specific name."""
    row = {
        "course_code": "CS111",
        "course_name": "FUNDAMENTALS OF PROGRAMMING",
        "programs": ["AI2"],
    }

    result = _format_export_course_name(row, {"CS111": "PROGRAMMING I"})

    assert result == "AI2 - FUNDAMENTALS OF PROGRAMMING"


def test_format_export_course_name_falls_back_to_catalog_name() -> None:
    """Rows without plan-specific names keep the existing global fallback."""
    row = {
        "course_code": "CS111",
        "course_name": "",
    }

    result = _format_export_course_name(row, {"CS111": "PROGRAMMING I"})

    assert result == "PROGRAMMING I"


# ── Integration tests for views ──────────────────────────────────


def test_section_plan_page_requires_login(client: Client) -> None:
    """Unauthenticated → redirect to login."""
    r = client.get("/section-planning/")
    assert r.status_code == 302
    assert "/login/" in r["Location"]


def test_section_plan_page_requires_role(client: Client) -> None:
    """ADVISOR role → 403."""
    _login_as(client, "sp-advisor", ROLE_ADVISOR)
    r = client.get("/section-planning/")
    assert r.status_code == 403


def test_section_plan_page_accessible_by_general_advisor(client: Client) -> None:
    """GENERAL_ADVISOR → 200."""
    _login_as(client, "sp-general", ROLE_GENERAL_ADVISOR)
    r = client.get("/section-planning/")
    assert r.status_code == 200
    assert b"Section Planning" in r.content or "تخطيط الشعب".encode() in r.content


def test_section_plan_page_accessible_by_super_admin(client: Client) -> None:
    """SUPER_ADMIN → 200."""
    _login_as(client, "sp-super", ROLE_SUPER_ADMIN)
    r = client.get("/section-planning/")
    assert r.status_code == 200


def test_section_plan_generate_requires_auth(client: Client) -> None:
    """Unauthenticated POST → 401."""
    r = client.post(
        "/ops/section-planning/generate/",
        json.dumps({"year": 1447, "semester": 1}),
        content_type="application/json",
    )
    assert r.status_code == 401


def test_section_plan_generate_requires_role(client: Client) -> None:
    """ADVISOR → 403."""
    _login_as(client, "sp-gen-adv", ROLE_ADVISOR)
    r = client.post(
        "/ops/section-planning/generate/",
        json.dumps({"year": 1447, "semester": 1}),
        content_type="application/json",
    )
    assert r.status_code == 403


def test_section_plan_generate_returns_json(client: Client) -> None:
    """GENERAL_ADVISOR + valid input → 200 + JSON with ok: true."""
    _login_as(client, "sp-gen-ok", ROLE_GENERAL_ADVISOR)
    r = client.post(
        "/ops/section-planning/generate/",
        json.dumps({"year": 1447, "semester": 1}),
        content_type="application/json",
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "plan" in data
    assert "summary" in data
    assert data["student_count"] >= 0


def test_section_plan_generate_validates_year(client: Client) -> None:
    """Invalid year → 400."""
    _login_as(client, "sp-gen-yr", ROLE_GENERAL_ADVISOR)
    r = client.post(
        "/ops/section-planning/generate/",
        json.dumps({"year": 999, "semester": 1}),
        content_type="application/json",
    )
    assert r.status_code == 400


def test_section_plan_export_returns_xlsx(client: Client) -> None:
    """GENERAL_ADVISOR → XLSX file response."""
    _login_as(client, "sp-export", ROLE_GENERAL_ADVISOR)
    r = client.post(
        "/ops/section-planning/export/",
        json.dumps({"year": 1447, "semester": 1}),
        content_type="application/json",
    )
    assert r.status_code == 200
    assert "spreadsheet" in r["Content-Type"] or "xlsx" in r.get("Content-Disposition", "")


# ── Per-course overrides unit tests ──────────────────────────────


def test_compute_section_plan_course_override() -> None:
    """Per-course override trumps global capacity rule."""
    _create_course("CS500", credit_hours=4, is_external=False)
    aggregate: Counter[str] = Counter({"CS500": 60})
    # Global rule would give 25 (local 4cr), but override says 15
    plan = compute_section_plan(aggregate, course_overrides={"CS500": 15})

    assert len(plan) == 1
    row = plan[0]
    assert row["max_per_section"] == 15
    assert row["num_sections"] == 4  # ceil(60/15)


def test_compute_section_plan_override_only_affects_target() -> None:
    """Override for one course doesn't affect another."""
    _create_course("CS600", credit_hours=3, is_external=False)
    _create_course("CS700", credit_hours=3, is_external=False)
    aggregate: Counter[str] = Counter({"CS600": 80, "CS700": 80})
    plan = compute_section_plan(aggregate, course_overrides={"CS600": 20})

    by_code = {r["course_code"]: r for r in plan}
    assert by_code["CS600"]["max_per_section"] == 20
    assert by_code["CS600"]["num_sections"] == 4  # ceil(80/20)
    assert by_code["CS700"]["max_per_section"] == 40  # default local other
    assert by_code["CS700"]["num_sections"] == 2  # ceil(80/40)


# ── Courses list endpoint ────────────────────────────────────────


def test_section_plan_courses_requires_role(client: Client) -> None:
    """ADVISOR → 403 for courses endpoint."""
    _login_as(client, "sp-courses-adv", ROLE_ADVISOR)
    r = client.get("/ops/section-planning/courses/")
    assert r.status_code == 403


def test_section_plan_courses_returns_list(client: Client) -> None:
    """GENERAL_ADVISOR → 200 with courses list."""
    _login_as(client, "sp-courses-ok", ROLE_GENERAL_ADVISOR)
    # Create a ProgrammeRequirement so there's at least one course
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="AI100",
        credit_hours=3,
        programme_term=1,
    )
    r = client.get("/ops/section-planning/courses/")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert isinstance(data["courses"], list)
    assert len(data["courses"]) >= 1
    # Check structure
    course = data["courses"][0]
    assert "course_code" in course
    assert "default_max" in course


# ── Multi-program support ────────────────────────────────────────


def test_section_plan_generate_multi_program(client: Client) -> None:
    """Comma-separated programs are accepted."""
    _login_as(client, "sp-multi-prog", ROLE_GENERAL_ADVISOR)
    r = client.post(
        "/ops/section-planning/generate/",
        json.dumps({"year": 1447, "semester": 1, "program": "AI,DS"}),
        content_type="application/json",
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True


def test_section_plan_generate_with_overrides(client: Client) -> None:
    """Generate endpoint accepts course_overrides."""
    from core.authz import _rate_buckets

    _rate_buckets.clear()  # avoid throttle collisions from earlier tests

    _login_as(client, "sp-gen-ov", ROLE_GENERAL_ADVISOR)
    r = client.post(
        "/ops/section-planning/generate/",
        json.dumps(
            {
                "year": 1447,
                "semester": 1,
                "course_overrides": {"CS101": 15, "AI201": 30},
            }
        ),
        content_type="application/json",
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True


def test_section_plan_generate_combined_splits_same_code_different_plan_names(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.authz import _rate_buckets

    _rate_buckets.clear()
    _login_as(client, "sp-combined-identity", ROLE_GENERAL_ADVISOR)

    Course.objects.create(course_code="CS111", description="PROGRAMMING I", credit_hours=4)
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="CS111",
        course_name="PROGRAMMING I",
        credit_hours=4,
    )
    ProgrammeRequirement.objects.create(
        program="AI2",
        course_code="CS111",
        course_name="FUNDAMENTALS OF PROGRAMMING",
        credit_hours=4,
    )
    Student.objects.create(student_id=991101, registration_no="991101", program="AI")
    Student.objects.create(student_id=992101, registration_no="992101", program="AI2")

    monkeypatch.setattr(
        "core.services.reporting.batch_recommend_multi_program",
        lambda student_ids, _year, _term: {sid: ["CS111"] for sid in student_ids},
    )

    response = client.post(
        "/ops/section-planning/generate/",
        json.dumps({"year": 1448, "semester": 1}),
        content_type="application/json",
    )

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["mode"] == "combined"
    rows = data["plan"]
    assert len(rows) == 2
    assert {row["course_key"] for row in rows} == {
        "CS111::PROGRAMMING_I",
        "CS111::FUNDAMENTALS_OF_PROGRAMMING",
    }
    assert {row["course_name"] for row in rows} == {
        "PROGRAMMING I",
        "FUNDAMENTALS OF PROGRAMMING",
    }


# ── Programme capacities (3-tier resolution) ──────────────────


def test_load_programme_capacities_returns_non_null() -> None:
    """_load_programme_capacities returns only entries with max_capacity >= 1."""
    ProgrammeRequirement.objects.create(
        program="CS",
        course_code="CS110",
        credit_hours=3,
        max_capacity=30,
    )
    ProgrammeRequirement.objects.create(
        program="CS",
        course_code="CS120",
        credit_hours=3,
        max_capacity=None,
    )
    ProgrammeRequirement.objects.create(
        program="CS",
        course_code="CS130",
        credit_hours=3,
        max_capacity=0,
    )
    caps = _load_programme_capacities("CS", ["CS110", "CS120", "CS130"])
    assert caps == {"CS110": 30}


def test_load_programme_capacities_filters_by_program() -> None:
    """Capacities from a different program are not returned."""
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="AI310",
        credit_hours=3,
        max_capacity=20,
    )
    ProgrammeRequirement.objects.create(
        program="DS",
        course_code="AI310",
        credit_hours=3,
        max_capacity=35,
    )
    caps = _load_programme_capacities("AI", ["AI310"])
    assert caps == {"AI310": 20}


def test_compute_section_plan_programme_capacities_middle_tier() -> None:
    """programme_capacities is used when no per-course override exists."""
    _create_course("CS810", credit_hours=3, is_external=False)
    aggregate: Counter[str] = Counter({"CS810": 60})
    # Global rule would give 40 (local other), programme says 20
    plan = compute_section_plan(
        aggregate,
        programme_capacities={"CS810": 20},
    )
    assert len(plan) == 1
    row = plan[0]
    assert row["max_per_section"] == 20
    assert row["num_sections"] == 3  # ceil(60/20)


def test_compute_section_plan_override_beats_programme() -> None:
    """Per-course override takes priority over programme_capacities."""
    _create_course("CS820", credit_hours=3, is_external=False)
    aggregate: Counter[str] = Counter({"CS820": 60})
    plan = compute_section_plan(
        aggregate,
        course_overrides={"CS820": 10},
        programme_capacities={"CS820": 20},
    )
    assert len(plan) == 1
    row = plan[0]
    assert row["max_per_section"] == 10
    assert row["num_sections"] == 6  # ceil(60/10)


def test_compute_section_plan_programme_caps_fallback_to_global() -> None:
    """Courses not in programme_capacities still use global rules."""
    _create_course("CS830", credit_hours=4, is_external=False)
    _create_course("CS840", credit_hours=3, is_external=False)
    aggregate: Counter[str] = Counter({"CS830": 50, "CS840": 80})
    # Only CS830 has a programme cap; CS840 should use global rule (40)
    plan = compute_section_plan(
        aggregate,
        programme_capacities={"CS830": 15},
    )
    by_code = {r["course_code"]: r for r in plan}
    assert by_code["CS830"]["max_per_section"] == 15
    assert by_code["CS830"]["num_sections"] == 4  # ceil(50/15)
    assert by_code["CS840"]["max_per_section"] == 40  # global local-other
    assert by_code["CS840"]["num_sections"] == 2  # ceil(80/40)


def test_get_all_courses_with_defaults_programme_max_overlay() -> None:
    """get_all_courses_with_defaults overlays programme_max when program is given."""
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="AI510",
        credit_hours=3,
        max_capacity=18,
    )
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="AI520",
        credit_hours=4,
        max_capacity=None,
    )
    result = get_all_courses_with_defaults(program="AI")
    by_code = {r["course_code"]: r for r in result}
    assert by_code["AI510"]["programme_max"] == 18
    assert by_code["AI520"]["programme_max"] is None


def test_get_all_courses_with_defaults_no_program() -> None:
    """Without program, programme_max is always None."""
    ProgrammeRequirement.objects.create(
        program="DS",
        course_code="DS610",
        credit_hours=3,
        max_capacity=22,
    )
    result = get_all_courses_with_defaults()
    by_code = {r["course_code"]: r for r in result}
    if "DS610" in by_code:
        assert by_code["DS610"]["programme_max"] is None

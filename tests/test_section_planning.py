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

from core.models import Course, ProgrammeRequirement
from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_GENERAL_ADVISOR,
    ROLE_SUPER_ADMIN,
    ensure_role_groups,
    ensure_scope_schema,
    set_user_scope,
)
from core.services.section_planning import compute_plan_summary, compute_section_plan

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


# ── Integration tests for views ──────────────────────────────────


def test_section_plan_page_requires_login(client: Client) -> None:
    """Unauthenticated → redirect to login."""
    r = client.get("/section-planning/")
    assert r.status_code == 302
    assert "/login/" in r.url


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

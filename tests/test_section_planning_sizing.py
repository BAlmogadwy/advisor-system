"""Section Planning sizes courses the way the timetable builder will.

- Declared limits hold in every view: the all-programmes view (the default)
  sizes a course by the lowest limit any programme in scope declares, through
  the same function the builder uses for a pooled scenario.
- COE is one of our departments: its courses take the local 25/40 rules, and
  the page reads that list from the server.
"""

from __future__ import annotations

import io
import json
from collections import Counter
from pathlib import Path

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.authz import _rate_buckets
from core.models import Course, ProgrammeRequirement, Student
from core.services.rbac import (
    ROLE_GENERAL_ADVISOR,
    ensure_role_groups,
    ensure_scope_schema,
    set_user_scope,
)
from core.services.reporting import clear_aggregate_cache
from core.services.section_planning import (
    LOCAL_DEPARTMENTS,
    compute_section_plan,
    lowest_declared_capacities,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_state():
    _rate_buckets.clear()
    clear_aggregate_cache()
    yield
    _rate_buckets.clear()
    clear_aggregate_cache()


@pytest.fixture
def planner(client: Client) -> Client:
    ensure_role_groups()
    ensure_scope_schema()
    user, _ = User.objects.get_or_create(username="sp-sizing")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_GENERAL_ADVISOR))
    set_user_scope(user.id, advisor_id="", departments="")
    client.force_login(user)
    return client


def _requirement(
    program: str, code: str, cap: int | None, *, credits: int = 3, name: str = ""
) -> None:
    ProgrammeRequirement.objects.create(
        program=program,
        course_code=code,
        course_name=name or f"{code} NAME",
        credit_hours=credits,
        type="Mandatory",
        programme_term=5,
        max_capacity=cap,
    )


def _students(program: str, first_id: int, count: int, *, section: str = "M") -> list[int]:
    ids = list(range(first_id, first_id + count))
    Student.objects.bulk_create(
        [
            Student(student_id=sid, registration_no=str(sid), program=program, section=section)
            for sid in ids
        ]
    )
    return ids


def _recommend(monkeypatch: pytest.MonkeyPatch, by_program: dict[str, list[str]]) -> None:
    """Every student of a programme is recommended that programme's courses."""

    def single(student_ids, program, _year, _term, **_kw):
        return {sid: list(by_program.get(program, [])) for sid in student_ids}

    def multi(student_ids, _year, _term, **_kw):
        programs = dict(
            Student.objects.filter(student_id__in=student_ids).values_list("student_id", "program")
        )
        return {sid: list(by_program.get(programs[sid], [])) for sid in student_ids}

    monkeypatch.setattr("core.services.reporting.batch_recommend", single)
    monkeypatch.setattr("core.services.reporting.batch_recommend_multi_program", multi)


def _generate(client: Client, **body) -> dict:
    response = client.post(
        "/ops/section-planning/generate/",
        json.dumps({"year": 1448, "semester": 1, **body}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response.json()


def _row(plan: list[dict], code: str) -> dict:
    rows = [row for row in plan if row["course_code"] == code]
    assert len(rows) == 1, rows
    return rows[0]


def _export_rows(client: Client, **body) -> list[dict]:
    from openpyxl import load_workbook

    response = client.post(
        "/ops/section-planning/export/",
        json.dumps({"year": 1448, "semester": 1, **body}),
        content_type="application/json",
    )
    assert response.status_code == 200
    sheet = load_workbook(
        io.BytesIO(b"".join(response.streaming_content)), data_only=False
    ).worksheets[0]
    headers = [cell.value for cell in sheet[2]]
    return [
        dict(zip(headers, [cell.value for cell in row], strict=False))
        for row in sheet.iter_rows(min_row=3)
        if row[0].value is not None
    ]


# ── declared limits in every view ─────────────────────────────────


def test_lowest_declared_capacities_takes_the_most_restrictive_programme() -> None:
    _requirement("AI", "CS211", 30)
    _requirement("DS", "CS211", 25)
    _requirement("CS", "CS211", 20)  # not in scope: must not count
    _requirement("AI", "AI491", 5)
    _requirement("DS", "AI491", None)
    _requirement("AI", "MATH203", 0)  # a zero is not a declaration
    metadata = {
        "CS211::DATA_STRUCTURES": {"course_code": "CS211"},
        "AI491": {"course_code": "AI491"},
        "MATH203": {"course_code": "MATH203"},
        "PHYS103": {},
    }

    caps = lowest_declared_capacities(["AI", "DS"], metadata)

    assert caps == {"CS211::DATA_STRUCTURES": 25, "AI491": 5}
    assert lowest_declared_capacities([], metadata) == {}


@pytest.fixture
def graduation_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """31 AI students need AI491, which AI limits to 5 per section; DS limits nothing."""
    Course.objects.create(
        course_code="AI491", department="AI", credit_hours=2, description="GRADUATION PROJECT I"
    )
    Course.objects.create(
        course_code="DS321", department="DS", credit_hours=3, description="DS321 NAME"
    )
    _requirement("AI", "AI491", 5, credits=2, name="GRADUATION PROJECT I")
    _requirement("AI2", "AI491", None, credits=2, name="GRADUATION PROJECT I")
    _requirement("DS", "DS321", None)
    _students("AI", 441000001, 16, section="M")
    _students("AI", 442000001, 15, section="F")
    _students("DS", 443000001, 4, section="M")
    _recommend(monkeypatch, {"AI": ["AI491"], "DS": ["DS321"]})


def test_ai491_has_the_same_max_in_its_programme_and_in_all_programmes(
    planner: Client, graduation_project
) -> None:
    programme_view = _row(_generate(planner, program="AI")["plan"], "AI491")
    default_view = _row(_generate(planner)["plan"], "AI491")

    assert programme_view["max_per_section"] == 5
    assert default_view["max_per_section"] == programme_view["max_per_section"]
    assert default_view["num_sections"] == programme_view["num_sections"]


def test_the_all_programmes_export_carries_the_declared_limit(
    planner: Client, graduation_project
) -> None:
    row = next(r for r in _export_rows(planner) if r["Course"] == "AI491")
    assert row["Max/Section"] == 5


def test_the_builder_and_section_planning_size_a_shared_course_alike(
    planner: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.services.timetable_generate import generate_workspace_scenario

    Course.objects.create(
        course_code="CS211", department="CS", credit_hours=3, description="DATA STRUCTURES"
    )
    _requirement("AI", "CS211", 30, name="DATA STRUCTURES")
    _requirement("DS", "CS211", 35, name="DATA STRUCTURES")
    ids = _students("AI", 451000001, 40) + _students("DS", 452000001, 40)
    _recommend(monkeypatch, {"AI": ["CS211"], "DS": ["CS211"]})
    monkeypatch.setattr("core.services.timetable_generate.get_student_ids", lambda **_kw: ids)
    monkeypatch.setattr(
        "core.services.recommender_batch.batch_recommend_multi_program",
        lambda student_ids, _year, _term: {sid: ["CS211"] for sid in student_ids},
    )

    built = generate_workspace_scenario(1448, 1, ["AI", "DS"], section="M", run_autoplace=False)
    planned = _generate(planner, program="AI,DS")

    builder_row = _row(built["section_plan"], "CS211")
    planning_row = _row(planned["combined_plan"], "CS211")
    assert builder_row["max_per_section"] == 30
    assert planning_row["max_per_section"] == builder_row["max_per_section"]
    assert planning_row["num_sections"] == builder_row["num_sections"]


# ── COE is one of our departments ────────────────────────────────


def test_coe_courses_take_the_local_rules() -> None:
    Course.objects.create(course_code="COE211", department="COE", credit_hours=4)
    Course.objects.create(course_code="COE321", department="", credit_hours=3)
    Course.objects.create(course_code="MATH203", department="MATH", credit_hours=4)

    plan = {
        row["course_code"]: row
        for row in compute_section_plan(Counter({"COE211": 60, "COE321": 60, "MATH203": 60}))
    }

    assert "COE" in LOCAL_DEPARTMENTS
    assert plan["COE211"]["max_per_section"] == 25  # local, 4 credits
    assert plan["COE211"]["num_sections"] == 3
    assert (
        plan["COE321"]["max_per_section"] == 40
    )  # local, other credits (department from the code)
    assert plan["COE321"]["is_external"] is False
    assert plan["MATH203"]["max_per_section"] == 50  # a service department stays external


def test_a_coe_course_flagged_external_keeps_the_external_rule() -> None:
    """COE joining our departments does not reinterpret the per-course flag."""
    Course.objects.create(course_code="COE466", department="COE", credit_hours=3, is_external=True)

    (row,) = compute_section_plan(Counter({"COE466": 60}))

    assert row["max_per_section"] == 50


def test_the_page_is_given_the_servers_department_list(planner: Client) -> None:
    import re

    html = planner.get("/section-planning/").content.decode("utf-8")
    embedded = re.search(
        r'<script id="spLocalDepartments" type="application/json">(.*?)</script>', html
    )

    assert embedded, "the page must be handed the server's list"
    assert json.loads(embedded.group(1)) == sorted(LOCAL_DEPARTMENTS)
    js = (Path(__file__).resolve().parents[1] / "static/js/page-section-planning.js").read_text(
        encoding="utf-8"
    )
    assert "'CYB'" not in js and '"CYB"' not in js, "the page must not keep a list of its own"

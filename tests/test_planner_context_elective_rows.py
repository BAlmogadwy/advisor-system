"""The planner says which open elective row a registered course outside the plan counts for.

The owner's case (2026-11-28): a CS2 student registered in GS311 and GS333, two 2-credit
general-studies courses in no plan, was recommended FE1 "Free Elective" and GSE1 "University
Elective". The registrar posts such a course under the row's code once the grade lands, so the
rows are being met already. The forecast has known this since #285 (``elective_row_filling``);
the planner context now asks the same policy, with the arguments the graduation report builds
(``registered_elective_rows_filled``), and tags each recommended row it fills. The page shows the
tag in keep mode (tests/test_planner_board_fetch_browser.py).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import (
    Course,
    ProgrammeRequirement,
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
)
from core.services.elective_row_filling import BASIS_HOURS
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups
from core.services.student_graduation import (
    REGISTERED_TIMETABLE,
    build_graduation_report,
    registered_elective_rows_filled,
)

pytestmark = pytest.mark.django_db

SID = 4612690
PROGRAM = "PLNFILL"
YEAR, TERM = "1448", "1"


def _staff_client() -> Client:
    ensure_role_groups()
    user = User.objects.create_user(username="planner-fill-staff")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client(SERVER_NAME="localhost")
    client.force_login(user)
    return client


def _plan(code: str, name: str, hours: int, term: int, kind: str) -> None:
    ProgrammeRequirement.objects.create(
        program=PROGRAM,
        course_code=code,
        course_name=name,
        programme_term=term,
        credit_hours=hours,
        type=kind,
    )
    Course.objects.create(course_code=code, description=name, credit_hours=hours)


def _register(code: str, name: str, hours: int, day: str) -> None:
    Course.objects.get_or_create(
        course_code=code, defaults={"description": name, "credit_hours": hours}
    )
    section = TermSection.objects.create(
        course_key=code, course_code=code, course_number="", course_name=name, section="M1"
    )
    TermSectionMeeting.objects.create(
        term_section=section, day=day, start_time="09:00", end_time="10:15"
    )
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year=YEAR,
        term=TERM,
        term_section=section,
        source="scraper_timetable",
    )


@pytest.fixture()
def filling_student() -> Student:
    """Registered in one plan course and in GS311 and GS333, which no plan lists."""
    student = Student.objects.create(
        student_id=SID,
        registration_no=str(SID),
        name="Planner Fill Student",
        program=PROGRAM,
        section="M",
        status="ACTIVE",
        total_earned_credits=64,
        current_registered_credits=7,
    )
    _plan("PF201", "Data Structures", 3, 5, "Mandatory")
    # A Mandatory row of the very hours of the two courses: never one they fill.
    _plan("PF210", "Discrete Structures", 2, 5, "Mandatory")
    _plan("FE1", "Free Elective", 2, 5, "Free Elective")
    _plan("GSE1", "University Elective", 2, 6, "University Elective")
    _register("PF201", "Data Structures", 3, "SUN")
    _register("GS311", "مهارات التواصل", 2, "MON")
    _register("GS333", "التفكير الناقد", 2, "TUE")
    return student


def _context(client: Client, monkeypatch: pytest.MonkeyPatch, recs: list[str]) -> Any:
    monkeypatch.setattr(
        "core.planner_views.recommend_next_courses", lambda *args, **kwargs: list(recs)
    )
    response = client.post(
        "/ops/planner/context/",
        data=json.dumps({"student_id": str(SID), "academic_year": YEAR, "term": TERM}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response.json()


def test_the_context_tags_the_elective_rows_the_registered_courses_fill(
    filling_student: Student, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _context(_staff_client(), monkeypatch, ["FE1", "GSE1", "PF210"])

    tags = {rec["course_code"]: rec.get("filled_by") for rec in payload["recommendations"]}
    assert tags == {
        "FE1": {"course_code": "GS311", "basis": BASIS_HOURS},
        "GSE1": {"course_code": "GS333", "basis": BASIS_HOURS},
        # A Mandatory row is never one a course outside the plan counts for.
        "PF210": None,
    }
    # The same answer by row code, for the plan's own rows too.
    assert payload["elective_rows_filled"] == {
        "FE1": {"course_code": "GS311", "basis": BASIS_HOURS},
        "GSE1": {"course_code": "GS333", "basis": BASIS_HOURS},
    }
    # The rows stay recommendations like any other: the tag is a hint, not a removal.
    assert [rec["course_code"] for rec in payload["recommendations"]] == ["FE1", "GSE1", "PF210"]


def test_the_planner_and_the_forecast_name_the_same_rows(filling_student: Student) -> None:
    report = build_graduation_report(
        SID, int(YEAR), int(TERM), planning_baseline_kind=REGISTERED_TIMETABLE
    )
    filling = registered_elective_rows_filled(SID, YEAR, TERM)

    assert [row.as_dict() for row in filling.filled] == report["baseline_elective_rows_filled"]
    assert [(row.course_code, row.row_code) for row in filling.filled] == [
        ("GS311", "FE1"),
        ("GS333", "GSE1"),
    ]


def test_without_registered_courses_outside_the_plan_nothing_is_tagged(
    filling_student: Student, monkeypatch: pytest.MonkeyPatch
) -> None:
    StudentTermSection.objects.filter(term_section__course_key__in=["GS311", "GS333"]).delete()

    payload = _context(_staff_client(), monkeypatch, ["FE1", "GSE1"])

    assert payload["elective_rows_filled"] == {}
    assert all("filled_by" not in rec for rec in payload["recommendations"])


def test_a_failing_hint_never_costs_the_student(
    filling_student: Student, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("no plan today")

    monkeypatch.setattr("core.planner_views.registered_elective_rows_filled", broken)

    payload = _context(_staff_client(), monkeypatch, ["FE1"])

    assert payload["elective_rows_filled"] == {}
    assert [rec["course_code"] for rec in payload["recommendations"]] == ["FE1"]

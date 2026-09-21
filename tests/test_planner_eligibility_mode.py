"""Strict-by-default eligibility regressions for the staff timetable builder."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import Course, Prerequisite, ProgrammeRequirement, Student, StudentCourse
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups

pytestmark = pytest.mark.django_db

SID = 4498101
PROGRAM = "PLNMODE"
YEAR = "1446"
TERM = "1"


def _staff_client() -> Client:
    ensure_role_groups()
    user = User.objects.create_user(username="planner-mode-staff")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client(SERVER_NAME="localhost")
    client.force_login(user)
    return client


@pytest.fixture()
def mode_student() -> Student:
    student = Student.objects.create(
        student_id=SID,
        name="Planner Mode Student",
        program=PROGRAM,
        section="M",
        status="ACTIVE",
        total_earned_credits=80,
        current_registered_credits=15,
    )
    prerequisite = Course.objects.create(course_code="PRE100", credit_hours=3)
    Course.objects.create(course_code="NEXT200", credit_hours=3)
    StudentCourse.objects.create(student_id=SID, course=prerequisite, status="studying")
    ProgrammeRequirement.objects.create(
        program=PROGRAM,
        course_code="NEXT200",
        programme_term=5,
        credit_hours=3,
    )
    Prerequisite.objects.create(
        program=PROGRAM,
        course_code="NEXT200",
        prerequisite_course_code="PRE100,90(HOURS)",
    )
    return student


def _post_context(client: Client, *, eligibility_mode: str | None = None) -> Any:
    body = {"student_id": str(SID), "academic_year": YEAR, "term": TERM}
    if eligibility_mode is not None:
        body["eligibility_mode"] = eligibility_mode
    return client.post(
        "/ops/planner/context/",
        data=json.dumps(body),
        content_type="application/json",
    )


def test_context_defaults_to_strict_passed_and_earned_only(
    mode_student: Student, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []

    def fake_recommend(
        *args: object, strict_passed_only: bool = False, **kwargs: object
    ) -> list[str]:
        calls.append(strict_passed_only)
        return ["NEXT200"]

    monkeypatch.setattr("core.planner_views.recommend_next_courses", fake_recommend)

    response = _post_context(_staff_client())

    assert response.status_code == 200
    payload = response.json()
    assert calls == [True]
    assert payload["eligibility_mode"] == "strict"
    assert payload["strict_passed_only"] is True
    [recommendation] = payload["recommendations"]
    assert recommendation["status"] == "Blocked"
    assert set(recommendation["missing_prerequisites"]) == {"PRE100", "90(HOURS)"}


def test_relaxed_mode_explicitly_counts_studying_and_registered_credits(
    mode_student: Student, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []

    def fake_recommend(
        *args: object, strict_passed_only: bool = False, **kwargs: object
    ) -> list[str]:
        calls.append(strict_passed_only)
        return ["NEXT200"]

    monkeypatch.setattr("core.planner_views.recommend_next_courses", fake_recommend)

    response = _post_context(_staff_client(), eligibility_mode="relaxed")

    assert response.status_code == 200
    payload = response.json()
    assert calls == [False]
    assert payload["eligibility_mode"] == "relaxed"
    assert payload["strict_passed_only"] is False
    [recommendation] = payload["recommendations"]
    assert recommendation["status"] == "Eligible"
    assert recommendation["missing_prerequisites"] == []


def test_context_rejects_an_unknown_eligibility_mode(mode_student: Student) -> None:
    response = _post_context(_staff_client(), eligibility_mode="sometimes")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ELIGIBILITY_MODE"


def test_student_plan_palette_payload_uses_the_same_strict_default_and_relaxed_opt_in(
    mode_student: Student,
) -> None:
    client = _staff_client()

    strict_response = client.get(f"/report/student-plan/?student_id={SID}")
    relaxed_response = client.get(
        f"/report/student-plan/?student_id={SID}&eligibility_mode=relaxed"
    )

    assert strict_response.status_code == 200
    assert relaxed_response.status_code == 200
    strict_payload = strict_response.json()
    relaxed_payload = relaxed_response.json()
    assert strict_payload["strict_passed_only"] is True
    assert relaxed_payload["strict_passed_only"] is False
    strict_course = next(
        course
        for term in strict_payload["terms"]
        for course in term["courses"]
        if course["course_code"] == "NEXT200"
    )
    relaxed_course = next(
        course
        for term in relaxed_payload["terms"]
        for course in term["courses"]
        if course["course_code"] == "NEXT200"
    )

    assert strict_payload["eligibility_mode"] == "strict"
    assert strict_course["can_register"] is False
    assert set(strict_course["missing_prereqs"]) == {"PRE100", "90(HOURS)"}
    assert relaxed_payload["eligibility_mode"] == "relaxed"
    assert relaxed_course["can_register"] is True
    assert relaxed_course["missing_prereqs"] == []


def test_student_plan_palette_rejects_unknown_mode(mode_student: Student) -> None:
    response = _staff_client().get(
        f"/report/student-plan/?student_id={SID}&eligibility_mode=sometimes"
    )

    assert response.status_code == 400
    assert response.json()["error"] == "mode must be strict or relaxed"


def test_builder_propagates_eligibility_mode_without_overloading_registration_mode(
    mode_student: Student, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "core.planner_views.build_plans",
        lambda *args, **kwargs: {
            "summary": {},
            "options": [],
            "unscheduled": [],
            "swap_suggestions": [],
        },
    )
    client = _staff_client()
    body = {
        "student_id": str(SID),
        "academic_year": YEAR,
        "term": TERM,
        "mode": "ignore",
        "program_sections_only": False,
        "shortlist": [],
    }

    strict_response = client.post(
        "/ops/planner/build/", data=json.dumps(body), content_type="application/json"
    )
    relaxed_response = client.post(
        "/ops/planner/build/",
        data=json.dumps({**body, "eligibility_mode": "relaxed"}),
        content_type="application/json",
    )

    assert strict_response.status_code == 200
    assert strict_response.json()["mode"] == "ignore"
    assert strict_response.json()["eligibility_mode"] == "strict"
    assert relaxed_response.status_code == 200
    assert relaxed_response.json()["eligibility_mode"] == "relaxed"


def test_planner_ui_and_both_student_plan_consumers_carry_the_selected_mode() -> None:
    root = Path(__file__).resolve().parents[1]
    template = (root / "core/templates/core/planner.html").read_text(encoding="utf-8")
    script = (root / "static/js/page-planner.js").read_text(encoding="utf-8")

    assert 'id="eligibilityMode"' in template
    assert '<option value="strict" selected>' in template
    assert "Strict eligibility is the default" in template
    assert "&eligibility_mode=${encodeURIComponent(eligibilityMode)}" in script
    assert script.count("fetch(studentPlanUrl(") == 2
    assert "_planCache.eligibilityMode===eligibilityMode" in script
    assert "eligibility_mode:selectedEligibilityMode()" in script
    assert "eligibility_mode:currentCtx.eligibility_mode || selectedEligibilityMode()" in script

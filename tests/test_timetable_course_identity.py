from __future__ import annotations

import pytest

from core.models import (
    Course,
    ProgrammeRequirement,
    ScenarioSectionBudget,
    ScenarioStudentMap,
    Student,
    TimetableScenario,
)
from core.services.timetable_generate import generate_workspace_scenario
from core.services.timetable_optimizer_v2 import build_student_profiles_for_scenario

pytestmark = pytest.mark.django_db


def test_planner_splits_same_code_different_programme_course_names(monkeypatch) -> None:
    Course.objects.create(course_code="CS111", description="PROGRAMMING I", credit_hours=4)
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="CS111",
        course_name="PROGRAMMING I",
        programme_term=3,
        credit_hours=4,
        type="Mandatory",
    )
    ProgrammeRequirement.objects.create(
        program="AI2",
        course_code="CS111",
        course_name="FUNDAMENTALS OF PROGRAMMING",
        programme_term=1,
        credit_hours=4,
        type="Mandatory",
    )

    students = []
    for sid, program in [(991101, "AI"), (991102, "AI"), (992101, "AI2"), (992102, "AI2")]:
        students.append(sid)
        Student.objects.create(
            student_id=sid,
            registration_no=str(sid),
            name=f"Student {sid}",
            program=program,
            section="M",
            status="active",
        )

    monkeypatch.setattr("core.services.timetable_generate.get_student_ids", lambda **_kw: students)
    monkeypatch.setattr(
        "core.services.recommender_batch.batch_recommend_multi_program",
        lambda student_ids, _year, _term: {sid: ["CS111"] for sid in student_ids},
    )

    result = generate_workspace_scenario(
        1448,
        1,
        ["AI", "AI2"],
        scenario_name="Same code identity test",
        strategy="compact",
    )

    budgets = list(
        ScenarioSectionBudget.objects.filter(
            scenario_id=result["scenario"]["id"],
            course_code="CS111",
        ).order_by("course_key")
    )
    assert len(budgets) == 2
    assert {b.course_key for b in budgets} == {
        "CS111::FUNDAMENTALS_OF_PROGRAMMING",
        "CS111::PROGRAMMING_I",
    }
    assert {b.course_key: b.programme_term for b in budgets} == {
        "CS111::FUNDAMENTALS_OF_PROGRAMMING": 1,
        "CS111::PROGRAMMING_I": 3,
    }
    assert {b.course_key: b.total_demand for b in budgets} == {
        "CS111::FUNDAMENTALS_OF_PROGRAMMING": 2,
        "CS111::PROGRAMMING_I": 2,
    }

    student_keys = {
        row.student_id: row.recommended_course_keys
        for row in ScenarioStudentMap.objects.filter(scenario_id=result["scenario"]["id"])
    }
    assert student_keys[991101] == ["CS111::PROGRAMMING_I"]
    assert student_keys[992101] == ["CS111::FUNDAMENTALS_OF_PROGRAMMING"]


def test_optimizer_profiles_use_planner_course_keys_when_available() -> None:
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="Optimiser key identity test",
    )
    Student.objects.create(
        student_id=993101,
        registration_no="993101",
        name="Student 993101",
        program="AI",
        section="M",
        status="active",
    )
    ScenarioStudentMap.objects.create(
        scenario=scenario,
        student_id=993101,
        primary_term=3,
        recommended_courses=["CS111"],
        recommended_course_keys=["CS111::PROGRAMMING_I"],
    )

    profiles = build_student_profiles_for_scenario(scenario.id)

    assert profiles["993101"].recommended_courses == ["CS111::PROGRAMMING_I"]

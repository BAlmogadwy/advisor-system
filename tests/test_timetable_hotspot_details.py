"""The optimise result's hotspot courses: how many students each leaves
without a section, and the code and name the page shows (PR: hotspot
courses display). The engine's own ``hotspot_courses`` list is unchanged."""

from __future__ import annotations

import pytest

from core.models import ScenarioSectionBudget, TimetableScenario
from core.services.timetable_assignment_models import (
    StudentAssignmentState,
    TimetableEvaluationResult,
    UnresolvedReason,
)
from core.services.timetable_candidate_eval import (
    extract_hotspot_courses,
    hotspot_course_counts,
    hotspot_result_fields,
)
from core.services.timetable_optimizer_v2 import _attach_hotspot_details

DB = "CS372::DATABASE_SYSTEMS"
FE2 = "FE2::FREE_ELECTIVE_COURSE_II"
ENG = "ENG102::ENGLISH_LANGUAGE_SKILLS_II"


def _state(student: str, **reasons: str) -> StudentAssignmentState:
    keys = {"db": DB, "fe2": FE2, "eng": ENG}
    return StudentAssignmentState(
        student_id=student,
        unresolved_courses={
            keys[name]: UnresolvedReason(course_code=keys[name], reason=reason)
            for name, reason in reasons.items()
        },
    )


STATES = {
    "s1": _state("s1", fe2="all_clash", db="mixed_blockers"),
    "s2": _state("s2", fe2="all_clash", db="full"),
    "s3": _state("s3", fe2="mixed_blockers", eng="all_clash"),
    "s4": _state("s4", db="all_clash", eng="reserve_only"),
}


def _evaluation(states=STATES) -> TimetableEvaluationResult:
    return TimetableEvaluationResult(
        candidate_id="c",
        lexicographic_score=(0,),
        assignment_states=states,
        unresolved_student_ids=[],
        hotspot_courses=extract_hotspot_courses(states),
        capacity_pressure_courses=[],
        reserve_heavy_sections=[],
    )


def test_counts_only_clash_failures_most_students_first_then_by_course():
    # A full or reserve-only failure is capacity, not a clash: not counted.
    assert hotspot_course_counts(STATES) == [(FE2, 3), (DB, 2), (ENG, 1)]
    assert extract_hotspot_courses(STATES) == [FE2, DB, ENG]
    # A tie is broken by the course.
    tied = {"a": _state("a", eng="all_clash"), "b": _state("b", db="all_clash")}
    assert hotspot_course_counts(tied) == [(DB, 1), (ENG, 1)]


def test_result_fields_keep_the_engines_list_and_add_each_courses_students():
    fields = hotspot_result_fields(_evaluation())
    assert fields == {
        "hotspot_courses": [FE2, DB, ENG],
        "hotspot_students": {FE2: 3, DB: 2, ENG: 1},
    }
    assert hotspot_result_fields(_evaluation(), limit=2)["hotspot_students"] == {FE2: 3, DB: 2}


@pytest.mark.django_db
def test_details_take_the_code_and_name_from_the_budget_else_the_keys_code(
    django_assert_num_queries,
):
    sc = TimetableScenario.objects.create(
        academic_year="1448", term="2", name="s", gender="M", programs="AI"
    )
    other = TimetableScenario.objects.create(
        academic_year="1448", term="2", name="o", gender="M", programs="AI"
    )
    common = dict(
        department="AI", credit_hours=3, planned_sections=1, max_per_section=40, total_demand=40
    )
    ScenarioSectionBudget.objects.create(
        scenario=sc, course_key=DB, course_code="CS372", course_name="Database Systems", **common
    )
    # A row matched by its code alone (no key stored).
    ScenarioSectionBudget.objects.create(
        scenario=sc,
        course_key="",
        course_code=ENG,
        course_name="English Language Skills II",
        **common,
    )
    # Another scenario's row never names this one's course.
    ScenarioSectionBudget.objects.create(
        scenario=other, course_key=FE2, course_code="FE2", course_name="Elsewhere", **common
    )
    result = {"hotspot_courses": [FE2, DB, ENG], "hotspot_students": {FE2: 3, DB: 2, ENG: 1}}
    with django_assert_num_queries(1):
        _attach_hotspot_details(result, sc.id)
    assert result["hotspot_details"] == [
        {"course_key": FE2, "course_code": "FE2", "course_name": "", "students": 3},
        {
            "course_key": DB,
            "course_code": "CS372",
            "course_name": "Database Systems",
            "students": 2,
        },
        {
            "course_key": ENG,
            "course_code": "ENG102",
            "course_name": "English Language Skills II",
            "students": 1,
        },
    ]


@pytest.mark.django_db
def test_no_hotspots_reads_nothing_and_adds_nothing(django_assert_num_queries):
    result = {"hotspot_courses": []}
    with django_assert_num_queries(0):
        assert _attach_hotspot_details(result, 1) is result
    assert "hotspot_details" not in result


@pytest.mark.django_db
@pytest.mark.parametrize(
    "public, inner",
    [
        ("optimise_current_timetable", "_optimise_current_timetable"),
        ("optimise_scenario_timetable_v2", "_optimise_scenario_timetable_v2"),
    ],
)
def test_both_optimise_entry_points_attach_the_details(monkeypatch, public, inner):
    from core.services import timetable_optimizer_v2 as v2

    sc = TimetableScenario.objects.create(
        academic_year="1448", term="2", name="s", gender="M", programs="AI"
    )
    monkeypatch.setattr(
        v2, inner, lambda *a, **k: {"hotspot_courses": [DB], "hotspot_students": {DB: 4}}
    )
    result = getattr(v2, public)(sc.id)
    assert result["hotspot_details"] == [
        {"course_key": DB, "course_code": "CS372", "course_name": "", "students": 4}
    ]

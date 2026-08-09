"""The student planner's keep mode returns a complete, credit-safe week.

The scheduling engine treats current registrations as occupied time only.  These
tests cover the adapter boundary where that occupancy becomes a student-visible
alternative without asking the engine to schedule a held course against itself.
"""

from __future__ import annotations

import pytest

from core.models import ProgrammeRequirement, Student
from core.services.student_planner import PlannerRequest, build_student_options

pytestmark = pytest.mark.django_db

STUDENT_ID = 4981001
YEAR = 1448
TERM = 1


@pytest.fixture(autouse=True)
def planner_student(monkeypatch):
    Student.objects.create(student_id=STUDENT_ID, program="KEEP", section="M")
    for code, credits in (("AI113", 4), ("CS113", 3), ("DS341", 3)):
        ProgrammeRequirement.objects.create(
            program="KEEP",
            course_code=code,
            course_name=code,
            credit_hours=credits,
            type="Mandatory",
        )
    monkeypatch.setattr("core.services.student_planner.student_gender_strict", lambda _sid: "M")


def _row(
    code: str,
    section: str,
    section_id: int,
    day: str,
    start: str,
    end: str,
) -> dict:
    return {
        "course_code": code,
        "course_key": code,
        "section": section,
        "term_section_id": section_id,
        "day": day,
        "start_time": start,
        "end_time": end,
    }


def _baseline() -> list[dict]:
    # AI113 deliberately has two rows: the real reader emits one per meeting.
    return [
        _row("AI113", "M1", 101, "SUN", "09:00", "10:15"),
        _row("AI113", "M1", 101, "TUE", "09:00", "10:15"),
        _row("CS113", "M4", 102, "MON", "11:00", "12:15"),
    ]


def _mapping(code: str = "DS341", section: str = "M3", section_id: int = 201) -> dict:
    return {
        "course_code": code,
        "course_key": code,
        "section": section,
        "term_section_id": section_id,
        "meetings": [
            {"day": "WED", "start_time": "13:00", "end_time": "14:15"},
        ],
    }


def _request(*codes: str, keep: bool = True, cap: int = 18) -> PlannerRequest:
    return PlannerRequest(
        student_id=STUDENT_ID,
        year=YEAR,
        term=TERM,
        must_include=tuple(codes),
        keep_current_sections=keep,
        max_credits=cap,
        include_recommendations=False,
    )


def test_keep_mode_solves_only_additions_and_returns_the_complete_week(monkeypatch):
    baseline = _baseline()
    monkeypatch.setattr(
        "core.services.student_planner.get_student_term_baseline", lambda *_args: baseline
    )
    calls = []

    def solver(**kwargs):
        calls.append(kwargs)
        return {
            "options": [
                {
                    "name": "A1",
                    "scheduled": 1,
                    "target": 1,
                    "mappings": [_mapping()],
                    "unscheduled": [],
                }
            ]
        }

    monkeypatch.setattr("core.services.student_planner.run_solver", solver)
    result = build_student_options(_request("AI113", "DS341"))

    assert [row["course_code"] for row in calls[0]["shortlist"]] == ["DS341"]
    assert calls[0]["baseline"] is baseline
    assert calls[0]["keep_current_sections"] is True
    # 18-hour total cap minus distinct current credits (AI113=4, CS113=3).
    assert calls[0]["max_credits"] == 11

    option = result["alternatives"][0]
    assert [(c["course_code"], c["section"], c["source"]) for c in option["courses"]] == [
        ("AI113", "M1", "current"),
        ("CS113", "M4", "current"),
        ("DS341", "M3", "proposed"),
    ]
    assert [c["term_section_id"] for c in option["courses"]] == [101, 102, 201]
    assert [(m["course_code"], m["day"], m["source"]) for m in option["meetings"]] == [
        ("AI113", "SUN", "current"),
        ("CS113", "MON", "current"),
        ("AI113", "TUE", "current"),
        ("DS341", "WED", "proposed"),
    ]
    assert option["credit_hours"] == 10, "AI113 credits were counted once, not per meeting"
    assert option["course_count"] == 3
    assert option["scheduled_courses"] == 3
    assert option["target_courses"] == 3
    assert option["planner_options"] == ["A1"]


def test_no_additions_returns_one_meaningful_baseline_without_running_solver(monkeypatch):
    monkeypatch.setattr(
        "core.services.student_planner.get_student_term_baseline", lambda *_args: _baseline()
    )

    def forbidden(**_kwargs):
        raise AssertionError("there are no additions to solve")

    monkeypatch.setattr("core.services.student_planner.run_solver", forbidden)
    result = build_student_options(_request("AI113"))

    assert result["generated"] == 0
    assert result["reason"] == ""
    assert result["unplaced"] == []
    assert len(result["alternatives"]) == 1
    option = result["alternatives"][0]
    assert option["planner_options"] == []
    assert option["course_count"] == 2
    assert option["credit_hours"] == 7
    assert option["scheduled_courses"] == option["target_courses"] == 2
    assert {course["source"] for course in option["courses"]} == {"current"}


def test_a_consumed_total_cap_keeps_baseline_and_marks_addition_unplaced(monkeypatch):
    monkeypatch.setattr(
        "core.services.student_planner.get_student_term_baseline", lambda *_args: _baseline()
    )

    def forbidden(**_kwargs):
        raise AssertionError("zero remaining credits means 0, not the solver's unbounded sentinel")

    monkeypatch.setattr("core.services.student_planner.run_solver", forbidden)
    result = build_student_options(_request("DS341", cap=7))

    option = result["alternatives"][0]
    assert option["credit_hours"] == 7
    assert option["scheduled_courses"] == 2
    assert option["target_courses"] == 3
    assert option["unplaced"][0]["reason_code"] == "DID_NOT_FIT"
    assert result["unplaced"] == option["unplaced"]


def test_keep_variants_preserve_planner_names_and_option_specific_unplaced(monkeypatch):
    monkeypatch.setattr(
        "core.services.student_planner.get_student_term_baseline", lambda *_args: _baseline()
    )
    proposed = _mapping()
    monkeypatch.setattr(
        "core.services.student_planner.run_solver",
        lambda **_kwargs: {
            "options": [
                {
                    "name": "A1",
                    "scheduled": 1,
                    "target": 1,
                    "mappings": [proposed],
                    "unscheduled": [],
                },
                {
                    "name": "B1",
                    "scheduled": 1,
                    "target": 1,
                    "mappings": [proposed],
                    "unscheduled": [],
                },
                {
                    "name": "A2",
                    "scheduled": 0,
                    "target": 1,
                    "mappings": [],
                    "unscheduled": [{"course_code": "DS341", "reason": "No sections available"}],
                },
            ]
        },
    )

    result = build_student_options(_request("DS341"))

    assert result["generated"] == 3
    assert len(result["alternatives"]) == 2
    assert result["alternatives"][0]["planner_options"] == ["A1", "B1"]
    assert result["alternatives"][1]["planner_options"] == ["A2"]
    assert result["alternatives"][1]["unplaced"][0]["reason_code"] == ("OMITTED_IN_THIS_VARIANT")
    assert result["unplaced"] == []


def test_rebuild_filters_zero_mapping_options_but_keeps_global_reasons(monkeypatch):
    monkeypatch.setattr(
        "core.services.student_planner.get_student_term_baseline", lambda *_args: _baseline()
    )
    proposed = _mapping()
    calls = []

    def solver(**kwargs):
        calls.append(kwargs)
        return {
            "options": [
                {
                    "name": "A1",
                    "scheduled": 0,
                    "target": 2,
                    "mappings": [],
                    "unscheduled": [{"course_code": "AI113", "reason": "No sections available"}],
                },
                {
                    "name": "B1",
                    "scheduled": 1,
                    "target": 2,
                    "mappings": [proposed],
                    "unscheduled": [{"course_code": "AI113", "reason": "No sections available"}],
                },
                {
                    "name": "C1",
                    "scheduled": 1,
                    "target": 2,
                    "mappings": [proposed],
                    "unscheduled": [{"course_code": "AI113", "reason": "No sections available"}],
                },
            ]
        }

    monkeypatch.setattr("core.services.student_planner.run_solver", solver)
    result = build_student_options(_request("AI113", "DS341", keep=False))

    assert calls[0]["shortlist"][0]["course_code"] == "AI113"
    assert calls[0]["max_credits"] == 18, "rebuild has no retained credits to subtract"
    assert result["generated"] == 3
    assert len(result["alternatives"]) == 1
    option = result["alternatives"][0]
    assert option["planner_options"] == ["B1", "C1"]
    assert [(c["course_code"], c["source"]) for c in option["courses"]] == [("DS341", "proposed")]
    assert {meeting["source"] for meeting in option["meetings"]} == {"proposed"}
    assert option["unplaced"][0]["reason_code"] == "NOT_ON_FILE"
    assert result["unplaced"][0]["reason_code"] == "NOT_ON_FILE"

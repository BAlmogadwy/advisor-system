from __future__ import annotations

import json

from core.services.advisor_presentations import (
    normalise_presentation,
    replacement_timetable_presentation_from_tool_results,
)


def _certified_swap(remove: str, add: str, *, section: str = "M2") -> dict:
    return {
        "remove_course": {
            "course_code": remove,
            "course_name": "Removed course",
            "credits": 3,
            "database_id": 700,
        },
        "add_course": {
            "course_code": add,
            "course_name": "Added course",
            "credits": 4,
            "database_id": 701,
        },
        "outside_plan_addition": False,
        "academic_improvement": {
            "proven_improvement": True,
            "terms_saved": 1,
            "reason_code": "INTERNAL_ACADEMIC_PROOF",
        },
        "timetable": {
            "status": "COMPLETE_CLASH_FREE",
            "certified_options": [
                {
                    "planner_options": ["A1", "B1"],
                    "scheduled_courses": 2,
                    "target_courses": 2,
                    "credit_hours": 7,
                    "days_on_campus": 2,
                    "days": ["SUN", "TUE"],
                    "earliest_start": "09:00",
                    "latest_end": "11:45",
                    "complete_sections": [
                        {
                            "course_code": "AI221",
                            "course_name": "Artificial Intelligence Programming",
                            "section": "M1",
                            "credits": 3,
                            "term_section_id": 800,
                            "available_capacity": 12,
                        },
                        {
                            "course_code": add,
                            "course_name": "Added course",
                            "section": section,
                            "credits": 4,
                            "term_section_id": 801,
                        },
                    ],
                    "meetings": [
                        {
                            "course_code": "AI221",
                            "section": "M1",
                            "day": "SUN",
                            "start": "09:00",
                            "end": "10:15",
                            "term_section_id": 800,
                        },
                        {
                            "course_code": add,
                            "section": section,
                            "day": "TUE",
                            "start": "10:30",
                            "end": "11:45",
                            "term_section_id": 801,
                        },
                    ],
                }
            ],
        },
    }


def test_replacement_presentation_projects_top_swap_as_complete_timetable():
    safe = replacement_timetable_presentation_from_tool_results(
        [
            {
                "tool": "feasible_course_replacements",
                "ok": True,
                "student_id": 4901234,
                "academic_year": 1448,
                "term": 1,
                "baseline_kind": "EXPECTED_PLAN",
                "certified_replacements": [
                    _certified_swap("DS341", "CS285"),
                    _certified_swap("AI221", "DS321", section="M3"),
                ],
            }
        ]
    )

    assert safe["kind"] == "timetable_proposals"
    assert safe["mode"] == "certified_replacement"
    assert safe["planning_term"] == "1448/1"
    assert safe["baseline_kind"] == "EXPECTED_PLAN"
    assert safe["baseline_sections"] == []
    assert safe["expected_plan_sections"] == []
    assert safe["baseline_credit_hours"] == 6
    assert safe["expected_plan_credit_hours"] == 6
    assert safe["must_take_courses"] == ["CS285"]
    assert safe["constraints_satisfied"] is True
    assert safe["can_save"] is False
    assert safe["can_register"] is False

    assert safe["replacement"] == {
        "remove_course": {
            "course_code": "DS341",
            "course_name": "Removed course",
            "credits": 3,
        },
        "add_course": {
            "course_code": "CS285",
            "course_name": "Added course",
            "credits": 4,
        },
        "outside_plan_addition": False,
        "academic_improvement": {
            "proven_improvement": True,
            "terms_saved": 1,
        },
    }
    alternative = safe["alternatives"][0]
    assert alternative["planner_options"] == ["A1", "B1"]
    assert alternative["scheduled_courses"] == 2
    assert alternative["target_courses"] == 2
    assert alternative["total_credit_hours"] == 7
    assert [row["course_code"] for row in alternative["courses"]] == ["AI221", "CS285"]
    assert alternative["meetings"][1] == {
        "course_code": "CS285",
        "course_name": "Added course",
        "section": "M2",
        "day": "TUE",
        "start": "10:30",
        "end": "11:45",
    }
    assert "DS321" not in json.dumps(safe)

    encoded = json.dumps(safe)
    assert "student_id" not in encoded
    assert "term_section_id" not in encoded
    assert "available_capacity" not in encoded
    assert "reason_code" not in encoded
    assert normalise_presentation(safe) == safe


def test_latest_successful_replacement_without_certification_does_not_replay_stale_card():
    old = {
        "tool": "feasible_course_replacements",
        "ok": True,
        "academic_year": 1448,
        "term": 1,
        "baseline_kind": "REGISTERED",
        "certified_replacements": [_certified_swap("DS341", "CS285")],
    }
    latest = {
        "tool": "feasible_course_replacements",
        "ok": True,
        "academic_year": 1448,
        "term": 1,
        "baseline_kind": "REGISTERED",
        "certified_replacements": [],
    }

    assert replacement_timetable_presentation_from_tool_results([old, latest]) == {}


def test_replacement_presentation_rejects_incomplete_or_non_certified_options():
    malformed = _certified_swap("DS341", "CS285")
    malformed["timetable"]["certified_options"][0]["meetings"] = [
        malformed["timetable"]["certified_options"][0]["meetings"][0]
    ]
    not_certified = _certified_swap("DS341", "CS285")
    not_certified["timetable"]["status"] = "NOT_DETERMINABLE"

    for swap in (malformed, not_certified):
        assert (
            replacement_timetable_presentation_from_tool_results(
                [
                    {
                        "tool": "feasible_course_replacements",
                        "ok": True,
                        "academic_year": 1448,
                        "term": 1,
                        "baseline_kind": "REGISTERED",
                        "certified_replacements": [swap],
                    }
                ]
            )
            == {}
        )

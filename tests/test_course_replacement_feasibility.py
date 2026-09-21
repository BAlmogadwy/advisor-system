from __future__ import annotations

from copy import deepcopy

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from core.models import Course, ProgrammeRequirement, Student
from core.services.student_planner import PlannerRequest, build_student_options

pytestmark = pytest.mark.django_db

SID = 4988112


def _baseline_row(code: str, section_id: int, *, day: str = "SUN") -> dict:
    return {
        "course_code": code,
        "course_key": code,
        "course_name": f"Name {code}",
        "section": "M1",
        "term_section_id": section_id,
        "credits": 3,
        "day": day,
        "start_time": "09:00",
        "end_time": "10:00",
        "source": "registration_plan_import",
    }


def _academic_pair(remove: str, add: str) -> dict:
    return {
        "remove_course": {"code": remove, "name": f"Name {remove}", "credits": 3},
        "add_course": {"code": add, "name": f"Name {add}", "credits": 3},
        "outside_plan_addition": False,
        "comparison": {
            "proven_improvement": True,
            "timing_effect": "EARLIER",
            "term_difference": -1,
            "terms_saved": 1,
            "exact_timing_comparison_available": True,
            "improvement_basis": "COMPLETE_FORECAST",
            "blockers_resolved": [],
            "blockers_improved": [],
            "blockers_introduced": [],
        },
        "scenario": {
            "simulation_completed": True,
            "estimated_additional_terms": 4,
            "lower_bound_additional_terms": 4,
        },
    }


def _search_payload(pairs: list[dict], baseline_codes: list[str]) -> dict:
    return {
        "what_if": {
            "valid": True,
            "mode": "replacement_search",
            "pairs_evaluated": len(pairs),
            "search_truncated": False,
            "candidate_courses_considered": [row["add_course"]["code"] for row in pairs],
            "improving_replacements_found": len(pairs),
            "replacement_results_truncated": False,
            "improving_replacements": pairs,
            "baseline": {
                "planning_baseline_courses_assumed_passed": [
                    {"code": code, "name": f"Name {code}", "credits": 3} for code in baseline_codes
                ]
            },
        }
    }


def _planner_option(*codes: str) -> dict:
    courses = []
    meetings = []
    for index, code in enumerate(codes, start=1):
        section = "M1"
        source = "current" if index < len(codes) else "proposed"
        courses.append(
            {
                "course_code": code,
                "section": section,
                "credits": 3,
                "source": source,
                "term_section_id": 1000 + index,
            }
        )
        meetings.append(
            {
                "course_code": code,
                "section": section,
                "day": ["SUN", "MON", "TUE", "WED"][index - 1],
                "start": "09:00",
                "end": "10:00",
                "source": source,
            }
        )
    return {
        "planner_options": ["A1"],
        "courses": courses,
        "meetings": meetings,
        "scheduled_courses": len(codes),
        "target_courses": len(codes),
        "unplaced": [],
        "credit_hours": len(codes) * 3,
        "days_on_campus": len(codes),
        "days": [row["day"] for row in meetings],
        "earliest_start": "09:00",
        "latest_end": "10:00",
    }


def test_lower_ranked_academic_swap_is_certified_after_first_five_fail(monkeypatch):
    from core.services import course_replacement_feasibility as service

    pairs = [_academic_pair("OLD", f"ADD{index}") for index in range(1, 7)]
    search = _search_payload(pairs, ["OLD"])
    calls = []
    monkeypatch.setattr(
        service, "get_student_term_baseline", lambda *_a, **_k: [_baseline_row("OLD", 1)]
    )

    def fake_graduation(*args, **kwargs):
        calls.append(deepcopy(kwargs))
        return deepcopy(search)

    monkeypatch.setattr(service, "build_graduation_what_if", fake_graduation)

    def fake_planner(request):
        code = request.required_courses[0]
        if code != "ADD6":
            return {
                "alternatives": [],
                "unplaced": [
                    {
                        "course_code": code,
                        "reason_code": "ALL_SECTIONS_CLASH",
                        "reason": "Every section on file clashes.",
                    }
                ],
            }
        return {"alternatives": [_planner_option("ADD6")], "unplaced": []}

    monkeypatch.setattr(service, "build_student_options", fake_planner)

    result = service.find_feasible_course_replacements(SID, 1448, 1)

    assert result["status"] == "CERTIFIED_SWAPS_FOUND"
    assert result["certified_replacements"][0]["add_course"]["course_code"] == "ADD6"
    assert result["academic_search"]["academic_results_checked_for_timetable"] == 6
    assert all(
        call.get("max_replacement_results") == service.MAX_ACADEMIC_RESULTS_TO_CERTIFY
        for call in calls
    )


@pytest.mark.parametrize(
    "result_credit_predicate, expected_metadata",
    [
        (
            {"exact_result_credits": 3},
            {"exact_credit_hours": 3, "maximum_credit_hours": None},
        ),
        (
            {"max_result_credits": 3},
            {"exact_credit_hours": None, "maximum_credit_hours": 3},
        ),
    ],
    ids=("exact-result-credits", "maximum-result-credits"),
)
def test_result_credit_predicate_is_applied_before_certified_result_limit(
    monkeypatch, result_credit_predicate, expected_metadata
):
    from core.services import course_replacement_feasibility as service

    pairs = [_academic_pair("OLD", f"ADD{index}") for index in range(1, 7)]
    for pair in pairs[:5]:
        pair["add_course"]["credits"] = 4
    search = _search_payload(pairs, ["OLD"])
    search["what_if"]["result_credit_predicate_filtered_count"] = 20
    graduation_calls = []
    monkeypatch.setattr(
        service, "get_student_term_baseline", lambda *_a, **_k: [_baseline_row("OLD", 1)]
    )

    def fake_graduation(*_args, **kwargs):
        graduation_calls.append(dict(kwargs))
        return deepcopy(search)

    monkeypatch.setattr(service, "build_graduation_what_if", fake_graduation)

    def fake_planner(request):
        code = request.required_courses[0]
        credits = 3 if code == "ADD6" else 4
        option = _planner_option(code)
        option["courses"][0]["credits"] = credits
        option["credit_hours"] = credits
        return {"alternatives": [option], "unplaced": []}

    monkeypatch.setattr(service, "build_student_options", fake_planner)

    result = service.find_feasible_course_replacements(
        SID,
        1448,
        1,
        **result_credit_predicate,
    )

    assert [row["add_course"]["course_code"] for row in result["certified_replacements"]] == [
        "ADD6"
    ]
    assert {
        key: graduation_calls[0].get(key) for key in result_credit_predicate
    } == result_credit_predicate
    assert result["certification_search"] == {
        "academic_candidates_received": 6,
        "timetable_candidates_checked": 6,
        "certified_result_limit": service.MAX_CERTIFIED_REPLACEMENTS,
        "search_truncated": False,
        "result_credit_predicate": expected_metadata,
        "result_credit_predicate_filtered_count": 25,
    }


@pytest.mark.parametrize(
    "result_credit_predicate",
    [
        {"exact_result_credits": -1},
        {"max_result_credits": -1},
        {"exact_result_credits": 4, "max_result_credits": 3},
    ],
)
def test_invalid_result_credit_predicates_are_rejected(result_credit_predicate):
    from core.services import course_replacement_feasibility as service

    with pytest.raises(ValueError):
        service.find_feasible_course_replacements(SID, 1448, 1, **result_credit_predicate)


def test_exact_pair_keeps_every_other_section_and_strips_database_ids(monkeypatch):
    from core.services import course_replacement_feasibility as service

    baseline = [_baseline_row("DROP", 10, day="SUN"), _baseline_row("KEEP", 11, day="MON")]
    pair = _academic_pair("DROP", "ADD")
    search = _search_payload([pair], ["DROP", "KEEP"])
    monkeypatch.setattr(service, "get_student_term_baseline", lambda *_a, **_k: deepcopy(baseline))

    def fake_graduation(*args, **kwargs):
        if kwargs.get("search_better_replacements"):
            return deepcopy(search)
        return {
            "what_if": {
                "valid": True,
                "validation_errors": [],
                "removed_current_courses": [pair["remove_course"]],
                "added_current_courses": [pair["add_course"]],
                "outside_plan_additions": [],
                "comparison": pair["comparison"],
                "scenario": pair["scenario"],
                "baseline": search["what_if"]["baseline"],
            }
        }

    monkeypatch.setattr(service, "build_graduation_what_if", fake_graduation)
    seen = {}

    def fake_planner(request):
        seen["request"] = request
        return {"alternatives": [_planner_option("KEEP", "ADD")], "unplaced": []}

    monkeypatch.setattr(service, "build_student_options", fake_planner)

    result = service.find_feasible_course_replacements(
        SID, 1448, 1, remove_course="drop", add_course="add"
    )

    assert result["requested_remove_course"] == "DROP"
    assert result["requested_add_course"] == "ADD"
    override = seen["request"].baseline_override
    assert {row["course_code"] for row in override} == {"KEEP"}
    option = result["certified_replacements"][0]["timetable"]["certified_options"][0]
    assert {row["course_code"] for row in option["complete_sections"]} == {"KEEP", "ADD"}
    assert all("term_section_id" not in row for row in option["complete_sections"])
    assert set(option) >= {
        "planner_options",
        "complete_sections",
        "meetings",
        "scheduled_courses",
        "target_courses",
        "credit_hours",
        "days_on_campus",
        "days",
        "earliest_start",
        "latest_end",
    }


def test_incomplete_academic_baseline_cannot_be_called_clash_free(monkeypatch):
    from core.services import course_replacement_feasibility as service

    pair = _academic_pair("DROP", "ADD")
    search = _search_payload([pair], ["DROP", "UNMAPPED"])
    monkeypatch.setattr(
        service, "get_student_term_baseline", lambda *_a, **_k: [_baseline_row("DROP", 1)]
    )
    monkeypatch.setattr(service, "build_graduation_what_if", lambda *a, **kw: deepcopy(search))
    monkeypatch.setattr(
        service,
        "build_student_options",
        lambda *_a, **_k: pytest.fail("planner must not run with an incomplete retained baseline"),
    )

    result = service.find_feasible_course_replacements(SID, 1448, 1)

    assert result["certified_replacements"] == []
    rejected = result["rejected_replacements"][0]
    assert rejected["academic"]["proven_improvement"] is True
    assert rejected["timetable"]["status"] == "NOT_DETERMINABLE"
    assert rejected["timetable"]["reason_code"] == "ACADEMIC_TIMETABLE_BASELINE_MISMATCH"


def test_unmapped_removed_course_still_blocks_complete_timetable_certification(monkeypatch):
    from core.services import course_replacement_feasibility as service

    pair = _academic_pair("DROP", "ADD")
    search = _search_payload([pair], ["DROP", "KEEP"])
    monkeypatch.setattr(
        service,
        "get_student_term_baseline",
        lambda *_a, **_k: [_baseline_row("KEEP", 2, day="MON")],
    )

    def fake_graduation(*args, **kwargs):
        return {
            "what_if": {
                "valid": True,
                "validation_errors": [],
                "removed_current_courses": [pair["remove_course"]],
                "added_current_courses": [pair["add_course"]],
                "outside_plan_additions": [],
                "comparison": pair["comparison"],
                "scenario": pair["scenario"],
                "baseline": search["what_if"]["baseline"],
            }
        }

    monkeypatch.setattr(service, "build_graduation_what_if", fake_graduation)
    monkeypatch.setattr(
        service,
        "build_student_options",
        lambda *_a, **_k: pytest.fail("an unmapped removal target must fail before Planner"),
    )

    result = service.find_feasible_course_replacements(
        SID, 1448, 1, remove_course="DROP", add_course="ADD"
    )

    assert result["certified_replacements"] == []
    rejected = result["rejected_replacements"][0]
    assert rejected["timetable"]["status"] == "NOT_DETERMINABLE"
    assert rejected["timetable"]["reason_code"] == "ACADEMIC_TIMETABLE_BASELINE_MISMATCH"
    assert rejected["timetable"]["details"] == [
        {"reason_code": "BASELINE_SECTION_MAPPING_INCOMPLETE", "course_code": "DROP"}
    ]


def test_malformed_retained_meeting_cannot_be_certified(monkeypatch):
    from core.services import course_replacement_feasibility as service

    pair = _academic_pair("DROP", "ADD")
    search = _search_payload([pair], ["DROP", "KEEP"])
    malformed = _baseline_row("KEEP", 2)
    malformed["start_time"] = "not-a-time"
    monkeypatch.setattr(
        service,
        "get_student_term_baseline",
        lambda *_a, **_k: [_baseline_row("DROP", 1), malformed],
    )
    monkeypatch.setattr(service, "build_graduation_what_if", lambda *a, **kw: deepcopy(search))
    monkeypatch.setattr(
        service,
        "build_student_options",
        lambda *_a, **_k: pytest.fail("planner must not run with malformed retained meeting data"),
    )

    result = service.find_feasible_course_replacements(SID, 1448, 1)

    assert result["certified_replacements"] == []
    rejected = result["rejected_replacements"][0]
    assert rejected["timetable"]["status"] == "NOT_DETERMINABLE"
    assert rejected["timetable"]["reason_code"] == "ACADEMIC_TIMETABLE_BASELINE_MISMATCH"


def test_solver_output_is_independently_rechecked_for_clashes(monkeypatch):
    from core.services import course_replacement_feasibility as service

    pair = _academic_pair("DROP", "ADD")
    search = _search_payload([pair], ["DROP", "KEEP"])
    monkeypatch.setattr(
        service,
        "get_student_term_baseline",
        lambda *_a, **_k: [
            _baseline_row("DROP", 1, day="SUN"),
            _baseline_row("KEEP", 2, day="MON"),
        ],
    )
    monkeypatch.setattr(service, "build_graduation_what_if", lambda *a, **kw: deepcopy(search))
    overlapping = _planner_option("KEEP", "ADD")
    overlapping["meetings"][1]["day"] = overlapping["meetings"][0]["day"]
    monkeypatch.setattr(
        service,
        "build_student_options",
        lambda *_a, **_k: {"alternatives": [overlapping], "unplaced": []},
    )

    result = service.find_feasible_course_replacements(SID, 1448, 1)

    assert result["certified_replacements"] == []
    assert result["rejected_replacements"][0]["timetable"] == {
        "status": "NOT_DETERMINABLE",
        "reason_code": "MISSING_MEETING_DATA",
        "reason": "A selected section has no complete meeting data, so clashes cannot be certified.",
    }
    assert result["status"] == "NOT_DETERMINABLE"
    assert result["certification_search"]["search_truncated"] is False


def test_partial_catalogue_meeting_evidence_cannot_be_certified(monkeypatch):
    from core.services import course_replacement_feasibility as service

    pair = _academic_pair("DROP", "ADD")
    search = _search_payload([pair], ["DROP", "KEEP"])
    monkeypatch.setattr(
        service,
        "get_student_term_baseline",
        lambda *_a, **_k: [
            _baseline_row("DROP", 1, day="SUN"),
            _baseline_row("KEEP", 2, day="MON"),
        ],
    )
    monkeypatch.setattr(service, "build_graduation_what_if", lambda *a, **kw: deepcopy(search))
    option = _planner_option("KEEP", "ADD")
    option["courses"][1]["meeting_issue_codes"] = ["MISSING_MEETING_DATA"]
    monkeypatch.setattr(
        service,
        "build_student_options",
        lambda *_a, **_k: {"alternatives": [option], "unplaced": []},
    )

    result = service.find_feasible_course_replacements(SID, 1448, 1)

    assert result["certified_replacements"] == []
    assert result["rejected_replacements"][0]["timetable"]["reason_code"] == (
        "MISSING_MEETING_DATA"
    )


@pytest.mark.parametrize(
    ("day", "start", "end"),
    [
        ("MONSTER", "09:00", "10:00"),
        ("Monday typo", "09:00", "10:00"),
        ("MON", "09:99", "11:00"),
        ("MON", "24:00", "25:00"),
    ],
)
def test_malformed_proposed_meeting_cannot_be_certified(
    monkeypatch, day: str, start: str, end: str
):
    from core.services import course_replacement_feasibility as service

    pair = _academic_pair("DROP", "ADD")
    search = _search_payload([pair], ["DROP", "KEEP"])
    monkeypatch.setattr(
        service,
        "get_student_term_baseline",
        lambda *_a, **_k: [
            _baseline_row("DROP", 1, day="SUN"),
            _baseline_row("KEEP", 2, day="TUE"),
        ],
    )
    monkeypatch.setattr(service, "build_graduation_what_if", lambda *a, **kw: deepcopy(search))
    option = _planner_option("KEEP", "ADD")
    option["meetings"][1].update(day=day, start=start, end=end)
    monkeypatch.setattr(
        service,
        "build_student_options",
        lambda *_a, **_k: {"alternatives": [option], "unplaced": []},
    )

    result = service.find_feasible_course_replacements(SID, 1448, 1)

    assert result["certified_replacements"] == []
    assert result["rejected_replacements"][0]["timetable"]["reason_code"] == (
        "MISSING_MEETING_DATA"
    )


def test_mixed_registration_and_expected_plan_snapshot_fails_closed(monkeypatch):
    from core.services import course_replacement_feasibility as service

    registered = _baseline_row("A", 1)
    # Registrar evidence, not `mapped`: a staff mapping is a forecast like the
    # plan is, so pairing the two is not the contradiction this test is about.
    registered["source"] = "scraper_timetable"
    expected = _baseline_row("B", 2)
    monkeypatch.setattr(
        service, "get_student_term_baseline", lambda *_a, **_k: [registered, expected]
    )

    result = service.find_feasible_course_replacements(SID, 1448, 1)

    assert result["status"] == "NOT_DETERMINABLE"
    assert result["baseline_kind"] == "MIXED_REVIEW_REQUIRED"
    assert result["rejected_replacements"][0]["timetable"]["reason_code"] == "MIXED_BASELINE"


def test_remove_only_filter_is_sent_into_academic_search_not_post_filtered(monkeypatch):
    from core.services import course_replacement_feasibility as service

    pair = _academic_pair("DROP", "ADD")
    unfiltered = _search_payload([_academic_pair("OTHER", "OTHERADD")], ["DROP", "OTHER"])
    filtered = _search_payload([pair], ["DROP", "OTHER"])
    calls = []
    monkeypatch.setattr(
        service,
        "get_student_term_baseline",
        lambda *_a, **_k: [_baseline_row("DROP", 1), _baseline_row("OTHER", 2, day="MON")],
    )

    def fake_graduation(*args, **kwargs):
        calls.append(deepcopy(kwargs))
        return deepcopy(filtered if kwargs.get("replacement_remove_course") else unfiltered)

    monkeypatch.setattr(service, "build_graduation_what_if", fake_graduation)
    monkeypatch.setattr(
        service,
        "build_student_options",
        lambda *_a, **_k: {"alternatives": [_planner_option("OTHER", "ADD")], "unplaced": []},
    )

    result = service.find_feasible_course_replacements(SID, 1448, 1, remove_course="drop")

    assert result["certified_replacements"][0]["remove_course"]["course_code"] == "DROP"
    filtered_calls = [call for call in calls if call.get("replacement_remove_course")]
    assert filtered_calls
    assert filtered_calls[0]["replacement_remove_course"] == "DROP"


def test_service_performs_no_database_writes(monkeypatch):
    from core.services import course_replacement_feasibility as service

    pair = _academic_pair("OLD", "ADD")
    search = _search_payload([pair], ["OLD"])
    monkeypatch.setattr(
        service, "get_student_term_baseline", lambda *_a, **_k: [_baseline_row("OLD", 1)]
    )
    monkeypatch.setattr(service, "build_graduation_what_if", lambda *a, **kw: deepcopy(search))
    monkeypatch.setattr(
        service,
        "build_student_options",
        lambda *_a, **_k: {"alternatives": [_planner_option("ADD")], "unplaced": []},
    )

    with CaptureQueriesContext(connection) as captured:
        result = service.find_feasible_course_replacements(SID, 1448, 1)

    assert result["status"] == "CERTIFIED_SWAPS_FOUND"
    sql = "\n".join(query["sql"].lstrip().upper() for query in captured.captured_queries)
    assert "INSERT " not in sql
    assert "UPDATE " not in sql
    assert "DELETE " not in sql


def test_planner_server_baseline_override_uses_adapter_without_loading_snapshot(monkeypatch):
    Student.objects.create(
        student_id=SID,
        name="Replacement Test",
        program="RPL",
        section="M",
        total_earned_credits=20,
        current_registered_credits=3,
    )
    for code in ("KEEP", "ADD"):
        Course.objects.create(course_code=code, description=f"Name {code}", credit_hours=3)
        ProgrammeRequirement.objects.create(
            program="RPL",
            course_code=code,
            course_name=f"Name {code}",
            credit_hours=3,
            programme_term=1,
            type="Mandatory",
        )
    monkeypatch.setattr(
        "core.services.student_planner.get_student_term_baseline",
        lambda *_a, **_k: pytest.fail("stored snapshot must not replace the server override"),
    )
    monkeypatch.setattr(
        "core.services.student_planner.student_gender_strict", lambda *_a, **_k: "M"
    )
    seen = {}

    def fake_solver(**kwargs):
        seen.update(kwargs)
        return {
            "options": [
                {
                    "name": "A1",
                    "scheduled": 1,
                    "target": 1,
                    "mappings": [
                        {
                            "course_code": "ADD",
                            "course_key": "ADD",
                            "section": "M2",
                            "term_section_id": 22,
                            "meetings": [
                                {"day": "MON", "start_time": "11:00", "end_time": "12:00"}
                            ],
                        }
                    ],
                    "unscheduled": [],
                }
            ],
            "unscheduled": [],
            "summary": {"hard_constraint_failures": []},
        }

    monkeypatch.setattr("core.services.student_planner.run_solver", fake_solver)
    override = (_baseline_row("KEEP", 11),)

    result = build_student_options(
        PlannerRequest(
            student_id=SID,
            year=1448,
            term=1,
            must_include=("ADD",),
            required_courses=("ADD",),
            keep_current_sections=True,
            include_recommendations=False,
            max_credits=18,
            baseline_override=override,
        )
    )

    assert seen["baseline"] == list(override)
    assert result["alternatives"]
    assert {row["course_code"] for row in result["alternatives"][0]["courses"]} == {
        "KEEP",
        "ADD",
    }

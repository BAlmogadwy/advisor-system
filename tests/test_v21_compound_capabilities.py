"""V2.1 compound academic decisions stay typed, scoped, and read-only."""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.models import Student
from core.services import virtual_advisor_capabilities as caps
from core.services.llm_remote_privacy import (
    RemoteIdentityMap,
    project_tool_result_for_remote,
)
from core.services.rbac import ROLE_STUDENT

pytestmark = pytest.mark.django_db

SID = 4982101
SCOPE = {"role": ROLE_STUDENT, "student_id": SID}
CTX = {"academic_year": 1448, "term": 1}


def _graduation_delta(
    *,
    timing_effect: str = "SAME",
    term_difference: int = 0,
    simulation_completed: bool = True,
) -> dict[str, Any]:
    return {
        "what_if": {
            "valid": True,
            "validation_errors": [],
            "scenario": {
                "simulation_completed": simulation_completed,
                "estimated_additional_terms": 4 + term_difference,
                "lower_bound_additional_terms": 3,
            },
            "comparison": {
                "timing_effect": timing_effect,
                "term_difference": term_difference,
                "terms_saved": max(0, -term_difference),
                "exact_timing_comparison_available": True,
                "plan_changed": True,
                "blockers_resolved": [],
                "blockers_improved": [],
                "blockers_introduced": [],
                "deferred_courses": [],
                "term_plan_changes": [],
            },
        }
    }


def _proposal(code: str, credits: int) -> dict[str, Any]:
    return {
        "ok": True,
        "tool": "build_timetable_proposal",
        "baseline_kind": "REGISTERED",
        "baseline_credit_hours": 12,
        "credit_ceiling": 18,
        "constraints_satisfied": True,
        "constraint_failures": [],
        "unplaced_courses": [],
        "alternatives": [
            {
                "courses": [
                    {
                        "course_code": code,
                        "course_name": code,
                        "section": "M1",
                        "credits": credits,
                    }
                ],
                "meetings": [
                    {
                        "course_code": code,
                        "section": "M1",
                        "day": "SUN",
                        "start": "09:00",
                        "end": "10:00",
                    }
                ],
            }
        ],
    }


def test_single_addition_enforces_exact_additional_credit_hours(monkeypatch):
    monkeypatch.setattr(caps, "_resolve_scoped_student_id", lambda args, scope: (SID, None))
    monkeypatch.setattr(caps, "_ctx_year_term", lambda args, ctx: (1448, 1, None))
    monkeypatch.setattr(caps, "_compound_credit_cap", lambda args, term: (18, None, None))
    monkeypatch.setattr(
        caps,
        "_exec_my_progress",
        lambda *args: {
            "unlock_impact_ranking": [
                {
                    "code": "CS301",
                    "sole_remaining_prerequisite_count": 2,
                    "on_prerequisite_chain_of_count": 3,
                },
                {
                    "code": "CS401",
                    "sole_remaining_prerequisite_count": 9,
                    "on_prerequisite_chain_of_count": 10,
                },
            ],
            "prerequisites_satisfied": [
                {"code": "CS301", "course_name": "Three", "credits": 3},
                {"code": "CS401", "course_name": "Four", "credits": 4},
            ],
        },
    )
    monkeypatch.setattr(caps, "_exec_recommend_courses", lambda *args: {"recommendations": []})
    monkeypatch.setattr(
        caps,
        "_exec_why_course_locked",
        lambda args, *_: {
            "status": "PREREQUISITES_SATISFIED",
            "requirement_course_code": args["course_code"],
            "course_name": args["course_code"],
        },
    )
    monkeypatch.setattr(
        caps,
        "_exec_build_timetable_proposal",
        lambda args, *_: _proposal(
            args["must_take_courses"][0],
            3 if args["must_take_courses"][0] == "CS301" else 4,
        ),
    )
    monkeypatch.setattr(
        "core.services.student_graduation.build_graduation_what_if",
        lambda *args, **kwargs: _graduation_delta(),
    )

    result = caps._exec_recommend_feasible_course_addition(
        {
            "candidate_courses": ["CS301", "CS401"],
            "additional_credit_hours": 3,
            "objective": "unlock_impact",
        },
        SCOPE,
        CTX,
    )

    assert result["status"] == "RECOMMENDATION_FOUND"
    assert result["constraints"]["additional_credit_hours"] == 3
    assert result["recommended_addition"]["course_code"] == "CS301"
    assert [row["course_code"] for row in result["ranked_feasible_additions"]] == ["CS301"]
    assert result["excluded_candidates"][0]["reason_code"] == ("ADDITIONAL_CREDIT_HOURS_MISMATCH")
    assert result["can_register"] is False
    assert result["can_save"] is False


def test_single_addition_retains_an_exact_recorded_section_pin(monkeypatch):
    pin = {"course_code": "DS341", "section_label": "M2"}
    proposal_arguments: list[dict[str, Any]] = []
    monkeypatch.setattr(caps, "_resolve_scoped_student_id", lambda args, scope: (SID, None))
    monkeypatch.setattr(caps, "_ctx_year_term", lambda args, ctx: (1448, 1, None))
    monkeypatch.setattr(caps, "_compound_credit_cap", lambda args, term: (18, None, None))
    monkeypatch.setattr(caps, "_section_snapshot_matches_requested_term", lambda *args: True)
    monkeypatch.setattr(caps, "_compound_incomplete_registered_codes", lambda *args: [])
    monkeypatch.setattr(caps, "_timetable_baseline_kind", lambda rows: "REGISTERED")
    monkeypatch.setattr(
        "core.services.student_sections.get_student_term_baseline",
        lambda *args, **kwargs: [pin],
    )
    monkeypatch.setattr(
        "core.services.timetable_provenance.baseline_sections",
        lambda rows: [
            {
                "course_code": "DS341",
                "section": "M2",
                "day": "SUN",
                "start": "09:00",
                "end": "10:00",
            }
        ],
    )
    monkeypatch.setattr(
        caps,
        "_exec_my_progress",
        lambda *args: {
            "unlock_impact_ranking": [{"code": "CS301"}],
            "prerequisites_satisfied": [
                {"code": "CS301", "course_name": "Candidate", "credits": 3}
            ],
        },
    )
    monkeypatch.setattr(caps, "_exec_recommend_courses", lambda *args: {"recommendations": []})
    monkeypatch.setattr(
        caps,
        "_exec_why_course_locked",
        lambda *args: {
            "status": "PREREQUISITES_SATISFIED",
            "requirement_course_code": "CS301",
        },
    )

    def proposal(args, *_):
        proposal_arguments.append(dict(args))
        return _proposal("CS301", 3)

    monkeypatch.setattr(caps, "_exec_build_timetable_proposal", proposal)
    monkeypatch.setattr(
        "core.services.student_graduation.build_graduation_what_if",
        lambda *args, **kwargs: _graduation_delta(),
    )

    result = caps._exec_recommend_feasible_course_addition(
        {"objective": "balanced", "pinned_sections": [pin]},
        SCOPE,
        CTX,
    )

    assert result["status"] == "RECOMMENDATION_FOUND"
    assert result["constraints"]["pinned_sections"] == [pin]
    assert proposal_arguments == [
        {
            "mode": "around_current",
            "course_codes": ["CS301"],
            "must_take_courses": ["CS301"],
            "pinned_sections": [pin],
            "max_credits": 18,
        }
    ]


def test_single_addition_rejects_a_pin_outside_the_retained_baseline(monkeypatch):
    pin = {"course_code": "DS341", "section_label": "M2"}
    monkeypatch.setattr(caps, "_resolve_scoped_student_id", lambda args, scope: (SID, None))
    monkeypatch.setattr(caps, "_ctx_year_term", lambda args, ctx: (1448, 1, None))
    monkeypatch.setattr(caps, "_compound_credit_cap", lambda args, term: (18, None, None))
    monkeypatch.setattr(caps, "_section_snapshot_matches_requested_term", lambda *args: True)
    monkeypatch.setattr(caps, "_compound_incomplete_registered_codes", lambda *args: [])
    monkeypatch.setattr(caps, "_timetable_baseline_kind", lambda rows: "REGISTERED")
    monkeypatch.setattr(
        "core.services.student_sections.get_student_term_baseline",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr("core.services.timetable_provenance.baseline_sections", lambda rows: [])

    result = caps._exec_recommend_feasible_course_addition(
        {"objective": "balanced", "pinned_sections": [pin]},
        SCOPE,
        CTX,
    )

    assert result["status"] == "NOT_DETERMINABLE"
    assert result["reason_code"] == "PIN_NOT_IN_RETAINED_BASELINE"
    assert result["recommended_addition"] is None


def test_addition_credit_filter_runs_before_the_candidate_limit(monkeypatch):
    codes = [f"CS{index:03d}" for index in range(101, 122)]
    target = codes[-1]
    progress_rows = [
        {
            "code": code,
            "course_name": code,
            "credits": 3 if code == target else 2,
        }
        for code in codes
    ]
    evaluated: list[str] = []
    monkeypatch.setattr(caps, "_resolve_scoped_student_id", lambda args, scope: (SID, None))
    monkeypatch.setattr(caps, "_ctx_year_term", lambda args, ctx: (1448, 1, None))
    monkeypatch.setattr(caps, "_compound_credit_cap", lambda args, term: (18, None, None))
    monkeypatch.setattr(caps, "_section_snapshot_matches_requested_term", lambda *args: True)
    monkeypatch.setattr(caps, "_compound_incomplete_registered_codes", lambda *args: [])
    monkeypatch.setattr(
        caps,
        "_exec_my_progress",
        lambda *args: {
            "unlock_impact_ranking": [{"code": row["code"]} for row in progress_rows],
            "prerequisites_satisfied": progress_rows,
        },
    )
    monkeypatch.setattr(caps, "_exec_recommend_courses", lambda *args: {"recommendations": []})
    monkeypatch.setattr(
        caps,
        "_exec_why_course_locked",
        lambda args, *_: {
            "status": "PREREQUISITES_SATISFIED",
            "requirement_course_code": args["course_code"],
        },
    )

    def proposal(args, *_):
        code = args["must_take_courses"][0]
        evaluated.append(code)
        return _proposal(code, 3)

    monkeypatch.setattr(caps, "_exec_build_timetable_proposal", proposal)
    monkeypatch.setattr(
        "core.services.student_graduation.build_graduation_what_if",
        lambda *args, **kwargs: _graduation_delta(),
    )

    result = caps._exec_recommend_feasible_course_addition(
        {"objective": "balanced", "additional_credit_hours": 3}, SCOPE, CTX
    )

    assert result["status"] == "RECOMMENDATION_FOUND"
    assert result["recommended_addition"]["course_code"] == target
    assert evaluated == [target]
    assert result["search"]["candidates_discovered"] == 21
    assert result["search"]["credit_prefiltered_count"] == 20
    assert result["search"]["search_truncated"] is False


def test_faster_graduation_requires_a_verified_earlier_timing_effect(monkeypatch):
    monkeypatch.setattr(caps, "_resolve_scoped_student_id", lambda args, scope: (SID, None))
    monkeypatch.setattr(caps, "_ctx_year_term", lambda args, ctx: (1448, 1, None))
    monkeypatch.setattr(caps, "_compound_credit_cap", lambda args, term: (18, None, None))
    monkeypatch.setattr(caps, "_section_snapshot_matches_requested_term", lambda *args: True)
    monkeypatch.setattr(caps, "_compound_incomplete_registered_codes", lambda *args: [])
    monkeypatch.setattr(
        caps,
        "_exec_my_progress",
        lambda *args: {
            "unlock_impact_ranking": [{"code": "CS301"}],
            "prerequisites_satisfied": [
                {"code": "CS301", "course_name": "Candidate", "credits": 3}
            ],
        },
    )
    monkeypatch.setattr(caps, "_exec_recommend_courses", lambda *args: {"recommendations": []})
    monkeypatch.setattr(
        caps,
        "_exec_why_course_locked",
        lambda *args: {
            "status": "PREREQUISITES_SATISFIED",
            "requirement_course_code": "CS301",
        },
    )
    monkeypatch.setattr(
        caps,
        "_exec_build_timetable_proposal",
        lambda *args: _proposal("CS301", 3),
    )
    monkeypatch.setattr(
        "core.services.student_graduation.build_graduation_what_if",
        lambda *args, **kwargs: _graduation_delta(timing_effect="SAME"),
    )

    unchanged = caps._exec_recommend_feasible_course_addition(
        {"candidate_courses": ["CS301"], "objective": "faster_graduation"},
        SCOPE,
        CTX,
    )

    assert unchanged["status"] == "NO_VERIFIED_FASTER_GRADUATION_IN_BOUNDED_SEARCH"
    assert unchanged["recommended_addition"] is None
    assert unchanged["search"]["feasible_candidates_found"] == 1
    assert unchanged["search"]["objective_matches_found"] == 0

    monkeypatch.setattr(
        "core.services.student_graduation.build_graduation_what_if",
        lambda *args, **kwargs: _graduation_delta(timing_effect="EARLIER", term_difference=-1),
    )
    earlier = caps._exec_recommend_feasible_course_addition(
        {"candidate_courses": ["CS301"], "objective": "faster_graduation"},
        SCOPE,
        CTX,
    )
    assert earlier["status"] == "RECOMMENDATION_FOUND"
    assert earlier["recommended_addition"]["course_code"] == "CS301"


def test_addition_fails_closed_when_a_registered_course_has_no_section_mapping(
    monkeypatch,
):
    monkeypatch.setattr(caps, "_resolve_scoped_student_id", lambda args, scope: (SID, None))
    monkeypatch.setattr(caps, "_ctx_year_term", lambda args, ctx: (1448, 1, None))
    monkeypatch.setattr(caps, "_compound_credit_cap", lambda args, term: (18, None, None))
    monkeypatch.setattr(caps, "_section_snapshot_matches_requested_term", lambda *args: True)
    monkeypatch.setattr(
        caps,
        "_compound_incomplete_registered_codes",
        lambda *args: ["MATH471"],
    )
    monkeypatch.setattr(
        caps,
        "_exec_build_timetable_proposal",
        lambda *args: pytest.fail("incomplete registered baseline reached timetable search"),
    )

    result = caps._exec_recommend_feasible_course_addition({"objective": "balanced"}, SCOPE, CTX)

    assert result["status"] == "NOT_DETERMINABLE"
    assert result["reason_code"] == "REGISTERED_SECTION_MAPPING_INCOMPLETE"
    assert result["excluded_candidates"][0]["course_code"] == "MATH471"


def test_addition_reports_a_hard_cap_below_the_retained_baseline(monkeypatch):
    monkeypatch.setattr(caps, "_resolve_scoped_student_id", lambda args, scope: (SID, None))
    monkeypatch.setattr(caps, "_ctx_year_term", lambda args, ctx: (1448, 1, None))
    monkeypatch.setattr(caps, "_compound_credit_cap", lambda args, term: (12, 12, None))
    monkeypatch.setattr(caps, "_section_snapshot_matches_requested_term", lambda *args: True)
    monkeypatch.setattr(caps, "_compound_incomplete_registered_codes", lambda *args: [])
    monkeypatch.setattr(
        caps,
        "_exec_my_progress",
        lambda *args: {
            "unlock_impact_ranking": [{"code": "CS301"}],
            "prerequisites_satisfied": [{"code": "CS301", "credits": 3}],
        },
    )
    monkeypatch.setattr(caps, "_exec_recommend_courses", lambda *args: {"recommendations": []})
    monkeypatch.setattr(
        caps,
        "_exec_why_course_locked",
        lambda *args: {
            "status": "PREREQUISITES_SATISFIED",
            "requirement_course_code": "CS301",
        },
    )
    monkeypatch.setattr(
        caps,
        "_exec_build_timetable_proposal",
        lambda *args: {
            "ok": True,
            "baseline_kind": "REGISTERED",
            "baseline_credit_hours": 16,
            "constraints_satisfied": False,
            "constraint_failures": [{"reason_code": "BASELINE_EXCEEDS_EFFECTIVE_MAX_CREDITS"}],
            "alternatives": [],
        },
    )

    result = caps._exec_recommend_feasible_course_addition(
        {"candidate_courses": ["CS301"], "objective": "balanced", "max_credits": 12},
        SCOPE,
        CTX,
    )

    assert result["status"] == "CONSTRAINTS_UNSATISFIED"
    assert result["reason_code"] == "BASELINE_EXCEEDS_EFFECTIVE_MAX_CREDITS"
    assert result["baseline_credit_hours"] == 16
    assert result["constraints"]["effective_max_credits"] == 12


def test_drop_lowest_academic_priority_uses_verified_dependency_evidence(monkeypatch):
    Student.objects.create(student_id=SID, name="Scoped", program="CS", section="M")
    baseline = [
        {
            "course_code": "CS301",
            "course_key": "CS301",
            "course_name": "Leaf",
            "section": "M1",
            "credits": 3,
            "source": "scraper_timetable",
        },
        {
            "course_code": "CS302",
            "course_key": "CS302",
            "course_name": "Gateway",
            "section": "M2",
            "credits": 3,
            "source": "scraper_timetable",
        },
    ]
    monkeypatch.setattr(caps, "_resolve_scoped_student_id", lambda args, scope: (SID, None))
    monkeypatch.setattr(caps, "_ctx_year_term", lambda args, ctx: (1448, 1, None))
    monkeypatch.setattr(caps, "_compound_credit_cap", lambda args, term: (18, None, None))
    monkeypatch.setattr(caps, "_section_snapshot_matches_requested_term", lambda *args: True)
    monkeypatch.setattr(
        "core.services.student_sections.get_student_term_baseline",
        lambda *args, **kwargs: baseline,
    )
    monkeypatch.setattr(
        "core.services.course_priority.program_downstream_importance_scores",
        lambda program: {"CS301": 0.0, "CS302": 5.0},
    )
    monkeypatch.setattr(
        caps,
        "_exec_why_course_locked",
        lambda args, *_: {
            "requirement_course_code": args["course_code"],
            "sole_remaining_prerequisite_count": 0 if args["course_code"] == "CS301" else 3,
            "on_prerequisite_chain_of_count": 0 if args["course_code"] == "CS301" else 6,
        },
    )
    monkeypatch.setattr(
        "core.services.student_graduation.build_graduation_what_if",
        lambda *args, **kwargs: _graduation_delta(),
    )

    result = caps._exec_rank_current_course_drop_impact(
        {"objective": "lowest_academic_priority"}, SCOPE, CTX
    )

    assert result["status"] == "RANKING_AVAILABLE"
    assert result["top_ranked_drop_candidate"]["course_code"] == "CS301"
    assert result["ranked_drop_impacts"][0]["academic_priority"] == {
        "requirement_course_code": "CS301",
        "sole_remaining_prerequisite_count": 0,
        "on_prerequisite_chain_of_count": 0,
        "weighted_downstream_score": 0.0,
        "weighted_score_method": "SUM_INVERSE_DISTANCE",
    }
    assert result["can_drop"] is False


def test_drop_does_not_relabel_an_incomplete_scenario_as_no_delay(monkeypatch):
    Student.objects.create(student_id=SID, name="Scoped", program="CS", section="M")
    baseline = [
        {
            "course_code": "CS301",
            "course_key": "CS301",
            "course_name": "Current",
            "section": "M1",
            "credits": 3,
            "source": "scraper_timetable",
        }
    ]
    monkeypatch.setattr(caps, "_resolve_scoped_student_id", lambda args, scope: (SID, None))
    monkeypatch.setattr(caps, "_ctx_year_term", lambda args, ctx: (1448, 1, None))
    monkeypatch.setattr(caps, "_compound_credit_cap", lambda args, term: (18, None, None))
    monkeypatch.setattr(caps, "_section_snapshot_matches_requested_term", lambda *args: True)
    monkeypatch.setattr(
        "core.services.student_sections.get_student_term_baseline",
        lambda *args, **kwargs: baseline,
    )
    monkeypatch.setattr(
        "core.services.course_priority.program_downstream_importance_scores",
        lambda program: {},
    )
    monkeypatch.setattr(caps, "_exec_why_course_locked", lambda *args: {})
    monkeypatch.setattr(
        "core.services.student_graduation.build_graduation_what_if",
        lambda *args, **kwargs: _graduation_delta(
            timing_effect="UNRESOLVED_IMPROVEMENT",
            simulation_completed=False,
        ),
    )

    result = caps._exec_rank_current_course_drop_impact({}, SCOPE, CTX)

    assert result["status"] == "NOT_DETERMINABLE"
    assert result["ranked_drop_impacts"] == []
    assert result["excluded_courses"][0]["reason_code"] == "GRADUATION_SCENARIO_INCOMPLETE"


def _certified_replacement(remove: str, remove_credits: int, add: str, add_credits: int) -> dict:
    return {
        "remove_course": {
            "course_code": remove,
            "course_name": remove,
            "credits": remove_credits,
        },
        "add_course": {
            "course_code": add,
            "course_name": add,
            "credits": add_credits,
        },
        "academic_improvement": {
            "proven_improvement": True,
            "timing_effect": "EARLIER",
            "term_difference": -1,
            "terms_saved": 1,
            "improvement_basis": "COMPLETE_FORECAST",
            "blockers_resolved": [],
            "blockers_improved": [],
            "blockers_introduced": [],
        },
        "graduation_scenario": {
            "simulation_completed": True,
            "estimated_additional_terms": 3,
            "lower_bound_additional_terms": 3,
        },
        "timetable": {"status": "COMPLETE_CLASH_FREE", "certified_options": []},
    }


def _improvement_baseline() -> list[dict[str, Any]]:
    return [
        {
            "course_code": "CS301",
            "course_key": "CS301",
            "course_name": "Current",
            "section": "M1",
            "credits": 3,
            "day": "SUN",
            "start_time": "09:00",
            "end_time": "10:00",
            "source": "scraper_timetable",
        }
    ]


def test_improvement_not_increase_is_distinct_from_preserve(monkeypatch):
    # Load the consumer before patching the source module.  Otherwise the lazy
    # replacement-search import can initialise student_planner while
    # student_sections.get_student_term_baseline is patched, permanently binding
    # this test's fake into the consumer alias after monkeypatch restores the source.
    from core.services import student_planner, student_sections

    Student.objects.create(student_id=SID, name="Scoped", program="CS", section="M")
    baseline = [
        {
            "course_code": "CS301",
            "course_key": "CS301",
            "course_name": "Current",
            "section": "M1",
            "credits": 3,
            "day": "SUN",
            "start_time": "09:00",
            "end_time": "10:00",
            "source": "scraper_timetable",
        }
    ]
    monkeypatch.setattr(caps, "_resolve_scoped_student_id", lambda args, scope: (SID, None))
    monkeypatch.setattr(caps, "_ctx_year_term", lambda args, ctx: (1448, 1, None))
    monkeypatch.setattr(caps, "_compound_credit_cap", lambda args, term: (18, None, None))
    monkeypatch.setattr(caps, "_section_snapshot_matches_requested_term", lambda *args: True)

    def fake_baseline(*_args, **_kwargs):
        return baseline

    monkeypatch.setattr(student_sections, "get_student_term_baseline", fake_baseline)
    monkeypatch.setattr(student_planner, "get_student_term_baseline", fake_baseline)
    monkeypatch.setattr(
        "core.services.course_priority.program_downstream_importance_scores",
        lambda program: {"CS301": 0.0, "CS210": 2.0, "CS410": 4.0},
    )
    monkeypatch.setattr(
        caps,
        "_exec_why_course_locked",
        lambda args, *_: {
            "requirement_course_code": args["course_code"],
            "sole_remaining_prerequisite_count": 1,
            "on_prerequisite_chain_of_count": 2,
        },
    )
    replacement_calls = []

    def fake_replacement_search(*args, **kwargs):
        replacement_calls.append(dict(kwargs))
        return {
            "status": "CERTIFIED_SWAPS_FOUND",
            "academic_search": {"pairs_evaluated": 2, "search_truncated": False},
            "certification_search": {
                "academic_candidates_received": 2,
                "timetable_candidates_checked": 2,
                "search_truncated": False,
            },
            "certified_replacements": [
                _certified_replacement("CS301", 3, "CS410", 4),
                _certified_replacement("CS301", 3, "CS210", 2),
            ],
        }

    monkeypatch.setattr(
        "core.services.course_replacement_feasibility.find_feasible_course_replacements",
        fake_replacement_search,
    )

    result = caps._exec_improve_current_timetable(
        {
            "objective": "academic_priority",
            "credit_load_policy": "not_increase",
        },
        SCOPE,
        CTX,
    )

    assert result["status"] == "IMPROVEMENTS_FOUND"
    assert result["constraints"]["credit_load_policy"] == "not_increase"
    assert result["constraints"]["preserve_credit_hours"] is False
    assert result["recommended_change"]["replacement"]["add_course"]["course_code"] == "CS210"
    assert [row["add_course"]["course_code"] for row in result["graduation_improvements"]] == [
        "CS210"
    ]
    assert result["search"]["graduation_replacements"]["credit_policy_rejections_count"] == 1
    assert replacement_calls[0]["max_result_credits"] == 3
    assert "exact_result_credits" not in replacement_calls[0]
    assert result["can_apply"] is False
    assert result["can_save"] is False

    preserve = caps._exec_improve_current_timetable(
        {
            "objective": "academic_priority",
            "credit_load_policy": "preserve",
        },
        SCOPE,
        CTX,
    )
    assert preserve["status"] == "NO_VERIFIED_IMPROVEMENT_IN_BOUNDED_SEARCH"
    assert preserve["graduation_improvements"] == []
    assert preserve["search"]["graduation_replacements"]["credit_policy_rejections_count"] == 2
    assert replacement_calls[1]["exact_result_credits"] == 3
    assert "max_result_credits" not in replacement_calls[1]


def test_faster_improvement_requires_an_earlier_completed_forecast(monkeypatch):
    baseline = _improvement_baseline()
    replacement = _certified_replacement("CS301", 3, "CS210", 3)
    replacement["academic_improvement"]["timing_effect"] = "FORECAST_COMPLETED"
    monkeypatch.setattr(caps, "_resolve_scoped_student_id", lambda args, scope: (SID, None))
    monkeypatch.setattr(caps, "_ctx_year_term", lambda args, ctx: (1448, 1, None))
    monkeypatch.setattr(caps, "_compound_credit_cap", lambda args, term: (18, None, None))
    monkeypatch.setattr(caps, "_section_snapshot_matches_requested_term", lambda *args: True)
    monkeypatch.setattr(caps, "_compound_incomplete_registered_codes", lambda *args: [])
    monkeypatch.setattr(
        "core.services.student_sections.get_student_term_baseline",
        lambda *args, **kwargs: baseline,
    )
    monkeypatch.setattr(
        "core.services.course_replacement_feasibility.find_feasible_course_replacements",
        lambda *args, **kwargs: {
            "status": "CERTIFIED_SWAPS_FOUND",
            "certified_replacements": [replacement],
            "academic_search": {},
            "certification_search": {},
        },
    )

    result = caps._exec_improve_current_timetable(
        {
            "objective": "faster_graduation",
            "credit_load_policy": "preserve",
            "allow_course_replacements": True,
        },
        SCOPE,
        CTX,
    )

    assert result["status"] == "NO_VERIFIED_IMPROVEMENT_IN_BOUNDED_SEARCH"
    assert result["recommended_change"] is None
    assert result["graduation_improvements"] == []
    assert result["search"]["graduation_replacements"]["credit_policy_rejections_count"] == 0
    assert result["search"]["graduation_replacements"]["objective_rejections_count"] == 1


@pytest.mark.parametrize(
    "arguments",
    [
        {
            "objective": "faster_graduation",
            "credit_load_policy": "preserve",
            "allow_course_replacements": False,
        },
        {
            "objective": "schedule_quality",
            "credit_load_policy": "preserve",
            "allow_course_replacements": True,
        },
    ],
)
def test_improvement_rejects_incompatible_branch_controls(monkeypatch, arguments):
    monkeypatch.setattr(caps, "_resolve_scoped_student_id", lambda args, scope: (SID, None))
    monkeypatch.setattr(caps, "_ctx_year_term", lambda args, ctx: (1448, 1, None))
    monkeypatch.setattr(caps, "_compound_credit_cap", lambda args, term: (18, None, None))

    result = caps._exec_improve_current_timetable(arguments, SCOPE, CTX)

    assert result["ok"] is False
    assert "allow_course_replacements" in result["error"]


def test_improvement_execution_failure_is_not_a_completed_negative(monkeypatch):
    baseline = _improvement_baseline()
    monkeypatch.setattr(caps, "_resolve_scoped_student_id", lambda args, scope: (SID, None))
    monkeypatch.setattr(caps, "_ctx_year_term", lambda args, ctx: (1448, 1, None))
    monkeypatch.setattr(caps, "_compound_credit_cap", lambda args, term: (18, None, None))
    monkeypatch.setattr(caps, "_section_snapshot_matches_requested_term", lambda *args: True)
    monkeypatch.setattr(caps, "_compound_incomplete_registered_codes", lambda *args: [])
    monkeypatch.setattr(
        "core.services.student_sections.get_student_term_baseline",
        lambda *args, **kwargs: baseline,
    )
    monkeypatch.setattr(
        "core.services.course_replacement_feasibility.find_feasible_course_replacements",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = caps._exec_improve_current_timetable(
        {
            "objective": "faster_graduation",
            "credit_load_policy": "preserve",
            "allow_course_replacements": True,
        },
        SCOPE,
        CTX,
    )

    assert result["status"] == "NOT_DETERMINABLE"
    assert result["recommended_change"] is None
    assert result["search"]["graduation_replacements"]["execution_failed"] is True


def test_section_only_improvement_reports_a_hard_credit_constraint(monkeypatch):
    baseline = _improvement_baseline()
    monkeypatch.setattr(caps, "_resolve_scoped_student_id", lambda args, scope: (SID, None))
    monkeypatch.setattr(caps, "_ctx_year_term", lambda args, ctx: (1448, 1, None))
    monkeypatch.setattr(caps, "_compound_credit_cap", lambda args, term: (2, 2, None))
    monkeypatch.setattr(caps, "_section_snapshot_matches_requested_term", lambda *args: True)
    monkeypatch.setattr(caps, "_compound_incomplete_registered_codes", lambda *args: [])
    monkeypatch.setattr(
        "core.services.student_sections.get_student_term_baseline",
        lambda *args, **kwargs: baseline,
    )

    result = caps._exec_improve_current_timetable(
        {
            "objective": "schedule_quality",
            "credit_load_policy": "within_policy",
            "allow_course_replacements": False,
            "max_credits": 2,
        },
        SCOPE,
        CTX,
    )

    assert result["status"] == "CONSTRAINTS_UNSATISFIED"
    assert result["recommended_change"] is None
    assert result["search"]["schedule_quality"]["constraints_satisfied"] is False


def test_improvement_refuses_an_incomplete_registered_section_baseline(monkeypatch):
    baseline = [
        {
            "course_code": "CS301",
            "course_key": "CS301",
            "section": "M1",
            "credits": 3,
            "day": "SUN",
            "start_time": "09:00",
            "end_time": "10:00",
            "source": "scraper_timetable",
        }
    ]
    monkeypatch.setattr(caps, "_resolve_scoped_student_id", lambda args, scope: (SID, None))
    monkeypatch.setattr(caps, "_ctx_year_term", lambda args, ctx: (1448, 1, None))
    monkeypatch.setattr(caps, "_compound_credit_cap", lambda args, term: (18, None, None))
    monkeypatch.setattr(caps, "_section_snapshot_matches_requested_term", lambda *args: True)
    monkeypatch.setattr(
        "core.services.student_sections.get_student_term_baseline",
        lambda *args, **kwargs: baseline,
    )
    monkeypatch.setattr(
        caps,
        "_compound_incomplete_registered_codes",
        lambda *args: ["MATH471"],
    )

    result = caps._exec_improve_current_timetable(
        {
            "objective": "schedule_quality",
            "credit_load_policy": "preserve",
            "allow_course_replacements": False,
        },
        SCOPE,
        CTX,
    )

    assert result["status"] == "BASELINE_REVIEW_REQUIRED"
    assert result["reason_code"] == "REGISTERED_SECTION_MAPPING_INCOMPLETE"
    assert result["baseline_mapping_issues"] == [
        {
            "course_code": "MATH471",
            "reason_code": "REGISTERED_SECTION_MAPPING_INCOMPLETE",
        }
    ]


@pytest.mark.parametrize(
    ("executor", "empty_field"),
    (
        (caps._exec_rank_current_course_drop_impact, "ranked_drop_impacts"),
        (caps._exec_improve_current_timetable, "graduation_improvements"),
    ),
)
def test_current_timetable_compounds_refuse_a_noncurrent_planning_clock(
    monkeypatch,
    executor,
    empty_field: str,
):
    monkeypatch.setattr(caps, "_resolve_scoped_student_id", lambda args, scope: (SID, None))
    monkeypatch.setattr(caps, "_ctx_year_term", lambda args, ctx: (1448, 2, None))
    monkeypatch.setattr(caps, "_compound_credit_cap", lambda args, term: (18, None, None))

    result = executor(
        {},
        SCOPE,
        {
            "academic_year": 1448,
            "term": 2,
            "graduation_academic_year": 1448,
            "graduation_term": 1,
        },
    )

    assert result["status"] == "NOT_DETERMINABLE"
    assert result["reason_code"] == "PLANNING_TERM_IS_NOT_CURRENT_GRADUATION_TERM"
    assert result[empty_field] == []


def test_expected_plan_is_a_typed_gap_not_a_current_timetable(monkeypatch):
    expected = [
        {
            "course_code": "CS301",
            "course_key": "CS301",
            "course_name": "Forecast only",
            "section": "M1",
            "credits": 3,
            "source": "registration_plan_1448_t1",
        }
    ]
    monkeypatch.setattr(caps, "_resolve_scoped_student_id", lambda args, scope: (SID, None))
    monkeypatch.setattr(caps, "_ctx_year_term", lambda args, ctx: (1448, 1, None))
    monkeypatch.setattr(caps, "_compound_credit_cap", lambda args, term: (18, None, None))
    monkeypatch.setattr(caps, "_section_snapshot_matches_requested_term", lambda *args: True)
    monkeypatch.setattr(
        "core.services.student_sections.get_student_term_baseline",
        lambda *args, **kwargs: expected,
    )

    drop = caps._exec_rank_current_course_drop_impact({}, SCOPE, CTX)
    improve = caps._exec_improve_current_timetable({}, SCOPE, CTX)

    assert drop["status"] == "NO_REGISTERED_CURRENT_COURSES"
    assert drop["reason_code"] == "EXPECTED_PLAN_IS_NOT_REGISTRATION"
    assert drop["ranked_drop_impacts"] == []
    assert improve["status"] == "NO_REGISTERED_CURRENT_TIMETABLE"
    assert improve["reason_code"] == "EXPECTED_PLAN_IS_NOT_REGISTRATION"
    assert improve["recommended_change"] is None


def test_compound_registry_and_remote_projection_are_explicit_and_safe():
    registry = caps.build_default_registry()
    tool_names = {
        "recommend_feasible_course_addition",
        "rank_current_course_drop_impact",
        "improve_current_timetable",
    }
    for name in tool_names:
        capability = registry.capabilities[name]
        assert capability.read_only is True
        assert ROLE_STUDENT in capability.allowed_roles
        assert capability.parameters["additionalProperties"] is False

    addition_properties = registry.capabilities["recommend_feasible_course_addition"].parameters[
        "properties"
    ]
    assert addition_properties["additional_credit_hours"]["type"] == "integer"
    assert addition_properties["additional_credit_hours"]["minimum"] == 1
    assert addition_properties["additional_credit_hours"]["maximum"] == 12
    assert addition_properties["pinned_sections"]["maxItems"] == 10
    assert (
        "lowest_academic_priority"
        in registry.capabilities["rank_current_course_drop_impact"].parameters["properties"][
            "objective"
        ]["enum"]
    )
    improve_properties = registry.capabilities["improve_current_timetable"].parameters["properties"]
    assert improve_properties["credit_load_policy"]["enum"] == [
        "preserve",
        "not_increase",
        "within_policy",
    ]
    assert "academic_priority" in improve_properties["objective"]["enum"]

    poisoned = {
        "tool": "recommend_feasible_course_addition",
        "ok": True,
        "student_id": SID,
        "advisor_email": "private@example.test",
        "status": "RECOMMENDATION_FOUND",
        "outcome": "FEASIBLE_SINGLE_COURSE_ADDITION",
        "objective": "balanced",
        "constraints": {
            "additional_credit_hours": 3,
            "pinned_sections": [{"course_code": "DS341", "section_label": "M2", "internal_id": 7}],
            "student_id": SID,
        },
        "recommended_addition": {
            "course_code": "CS301",
            "course_name": "Safe public title",
            "credit_hours": 3,
            "student_id": SID,
            "eligibility": {"status": "PREREQUISITES_SATISFIED"},
            "timetable": {"status": "FEASIBLE", "clash_free_sections": []},
            "graduation": {"status": "EVALUATED", "timing_effect": "SAME"},
        },
    }
    projected = project_tool_result_for_remote(
        "recommend_feasible_course_addition", poisoned, RemoteIdentityMap(nonce="TESTNONCE0001")
    )
    encoded = json.dumps(projected, ensure_ascii=False)
    assert str(SID) not in encoded
    assert "private@example.test" not in encoded
    assert projected["recommended_addition"]["course_code"] == "CS301"
    assert projected["constraints"]["additional_credit_hours"] == 3
    assert projected["constraints"]["pinned_sections"] == [
        {"course_code": "DS341", "section_label": "M2"}
    ]

from core.services.answer_consistency import EvidenceValidationScope, check_answer
from core.services.student_advisor_v21_render import (
    render_course_prerequisites,
    render_improve_current_timetable,
    render_lookup_course,
    render_plan_by_term,
    render_rank_current_course_drop_impact,
    render_recommend_feasible_course_addition,
)


def _assert_compound_answer_is_grounded(
    tool: str,
    row: dict,
    renderer,
) -> None:
    for language in ("Arabic", "English"):
        answer = renderer(language, row)
        assert (
            check_answer(
                answer,
                tool_results=[row],
                question="اختبار موثوق",
                required_tools={tool},
                known_course_codes=frozenset(
                    {
                        "AI331",
                        "AI424",
                        "CS323",
                        "DS321",
                        "DS332",
                        "DS341",
                        "DS432",
                        "GS104",
                        "MATH471",
                        "STAT301",
                    }
                ),
                evidence_scopes=(
                    EvidenceValidationScope(
                        answer=answer,
                        tool_results=(row,),
                        required_tools=frozenset({tool}),
                    ),
                ),
            )
            == []
        )


def test_lookup_renderer_uses_the_verified_name_and_credits() -> None:
    answer = render_lookup_course(
        "English",
        {
            "ok": True,
            "courses": [
                {
                    "course_code": "AI331",
                    "course_name": "Machine Learning",
                    "credit_hours": 3,
                    "programs": ["AI"],
                }
            ],
        },
    )

    assert "AI331 — Machine Learning" in answer
    assert "3 credits" in answer


def test_prerequisite_renderer_does_not_turn_an_empty_list_into_permission() -> None:
    answer = render_course_prerequisites(
        "English",
        {
            "ok": True,
            "course_code": "AI331",
            "per_program": [
                {
                    "program": "AI",
                    "course_name": "Machine Learning",
                    "prerequisites": [],
                    "credit_hours": 3,
                    "programme_term": 6,
                }
            ],
        },
    )

    assert "No prerequisite is recorded" in answer
    assert "registration permission" in answer
    assert "eligible" not in answer.lower()


def test_plan_renderer_preserves_each_verified_status() -> None:
    answer = render_plan_by_term(
        "English",
        {
            "ok": True,
            "summary": {"passed": 1, "failed": 1},
            "terms": [
                {
                    "term": 8,
                    "courses": [
                        {
                            "course_code": "AI331",
                            "status": "passed",
                            "credit_hours": 3,
                            "prerequisites_satisfied": True,
                        },
                        {
                            "course_code": "CS424",
                            "status": "failed",
                            "credit_hours": 3,
                            "prerequisites_satisfied": False,
                            "missing_prereqs": ["CS323"],
                        },
                    ],
                }
            ],
        },
    )

    assert "AI331 — passed" in answer
    assert "CS424 — failed" in answer
    assert "missing prerequisites: CS323" in answer


def test_addition_renderer_names_every_ranked_course_and_typed_effect() -> None:
    candidate = {
        "course_code": "AI331",
        "course_name": "Machine Learning",
        "credit_hours": 3,
        "rank": 1,
        "eligibility": {"status": "PREREQUISITES_SATISFIED"},
        "official_recommendation": {"included": True, "rank": 2},
        "unlock_impact": {
            "sole_remaining_prerequisite_count": 2,
            "on_prerequisite_chain_of_count": 5,
        },
        "timetable": {
            "status": "FEASIBLE",
            "clash_free_section_count": 2,
            "clash_free_sections": [{"section": "M1"}, {"section": "M2"}],
        },
        "graduation": {
            "status": "EVALUATED",
            "timing_effect": "EARLIER",
            "term_difference": -1,
            "terms_saved": 1,
        },
    }
    second = {
        **candidate,
        "course_code": "DS341",
        "course_name": "Data Mining",
        "credit_hours": 4,
        "rank": 2,
    }
    answer = render_recommend_feasible_course_addition(
        "English",
        {
            "ok": True,
            "status": "RECOMMENDATION_FOUND",
            "planning_term": "1448/1",
            "baseline_kind": "REGISTERED",
            "recommended_addition": candidate,
            "ranked_feasible_additions": [candidate, second],
            "search": {
                "candidates_evaluated": 4,
                "feasible_candidates_found": 2,
                "candidate_limit": 12,
                "search_truncated": False,
            },
            "limitations": ["SECRET FREEFORM TEXT"],
        },
    )

    assert "AI331 — Machine Learning" in answer
    assert "DS341 — Data Mining" in answer
    assert "3 credits" in answer
    assert "4 credits" in answer
    assert "M1, M2" in answer
    assert "graduation effect: earlier" in answer
    assert "recorded term difference: -1" in answer
    assert "read-only" in answer
    assert "SECRET FREEFORM TEXT" not in answer


def test_every_addition_negative_has_stable_bounded_phrase_in_both_languages() -> None:
    statuses = (
        "NO_ELIGIBLE_CANDIDATES",
        "NO_FEASIBLE_ADDITION_IN_RECORDED_SNAPSHOT",
        "NO_VERIFIED_FASTER_GRADUATION_IN_BOUNDED_SEARCH",
        "CONSTRAINTS_UNSATISFIED",
        "NOT_DETERMINABLE",
    )
    for status in statuses:
        english = render_recommend_feasible_course_addition(
            "English", {"ok": True, "status": status, "search": {}}
        )
        arabic = render_recommend_feasible_course_addition(
            "Arabic", {"ok": True, "status": status, "search": {}}
        )
        assert "The bounded check did not produce a verified positive result." in english
        assert "لم ينتج الفحص المحدود نتيجة إيجابية موثقة." in arabic
        assert "read-only" in english
        assert "للقراءة فقط" in arabic

    faster = render_recommend_feasible_course_addition(
        "English",
        {
            "ok": True,
            "status": "NO_VERIFIED_FASTER_GRADUATION_IN_BOUNDED_SEARCH",
            "search": {"feasible_candidates_found": 2, "objective_matches_found": 0},
        },
    )
    assert "did not verify an earlier graduation forecast" in faster

    hard_cap = render_recommend_feasible_course_addition(
        "English",
        {
            "ok": True,
            "status": "CONSTRAINTS_UNSATISFIED",
            "baseline_credit_hours": 16,
            "constraints": {"effective_max_credits": 12},
            "search": {},
        },
    )
    assert "baseline: 16; limit: 12" in hard_cap
    assert "no addition can satisfy this constraint" in hard_cap


def test_drop_renderer_names_ranked_codes_and_never_calls_a_drop_harmless() -> None:
    least = {
        "course_code": "AI331",
        "course_name": "Machine Learning",
        "credit_hours": 3,
        "sections": ["M1"],
        "rank": 1,
        "impact_status": "NO_DETECTED_TERM_DELAY",
        "graduation": {
            "timing_effect": "SAME",
            "term_difference": 0,
            "affected_future_course_codes": ["AI424"],
        },
    }
    second = {
        "course_code": "DS341",
        "course_name": "Data Mining",
        "credit_hours": 4,
        "sections": ["M2"],
        "rank": 2,
        "impact_status": "DELAYED",
        "graduation": {"timing_effect": "LATER", "term_difference": 1},
    }
    answer = render_rank_current_course_drop_impact(
        "English",
        {
            "ok": True,
            "status": "RANKING_AVAILABLE",
            "objective": "balanced",
            "baseline_kind": "REGISTERED",
            "top_ranked_drop_candidate": least,
            "ranked_drop_impacts": [least, second],
            "search": {"drop_scenarios_evaluated": 2, "determinable_scenarios": 2},
        },
    )

    assert "Top-ranked candidate for balanced least-harmful ranking: AI331" in answer
    assert "AI331 — Machine Learning" in answer
    assert "DS341 — Data Mining" in answer
    assert "registered sections: M1" in answer
    assert "graduation effect: no change in forecast term count" in answer
    assert "not proof that a drop has no academic consequence" in answer
    assert "dropped" in answer


def test_drop_renderer_names_requested_courses_excluded_from_the_ranking() -> None:
    answer = render_rank_current_course_drop_impact(
        "English",
        {
            "ok": True,
            "status": "RANKING_AVAILABLE",
            "objective": "least_graduation_delay",
            "top_ranked_drop_candidate": {"course_code": "DS341"},
            "ranked_drop_impacts": [
                {
                    "course_code": "DS341",
                    "impact_status": "NO_DETECTED_TERM_DELAY",
                    "graduation": {
                        "simulation_completed": True,
                        "timing_effect": "SAME",
                    },
                }
            ],
            "excluded_courses": [
                {
                    "course_code": "GS104",
                    "outcome": "NOT_IN_REGISTERED_CURRENT_TIMETABLE",
                    "reason_code": "NOT_IN_CURRENT_TIMETABLE",
                }
            ],
            "search": {"drop_scenarios_evaluated": 1, "determinable_scenarios": 1},
        },
    )

    assert "GS104" in answer
    assert "was not found in the actually registered timetable baseline" in answer
    assert "was not evaluated as a drop scenario" in answer


def test_every_drop_negative_has_stable_bounded_phrase() -> None:
    for status in (
        "NO_REGISTERED_CURRENT_COURSES",
        "BASELINE_REVIEW_REQUIRED",
        "NOT_DETERMINABLE",
    ):
        answer = render_rank_current_course_drop_impact("English", {"ok": True, "status": status})
        arabic = render_rank_current_course_drop_impact("Arabic", {"ok": True, "status": status})
        assert "The bounded check did not produce a verified positive result." in answer
        assert "لم ينتج الفحص المحدود نتيجة إيجابية موثقة." in arabic
        assert "read-only" in answer
        assert "للقراءة فقط" in arabic


def test_timetable_improvement_renderer_preserves_replacement_courses_and_term_effect() -> None:
    replacement = {
        "remove_course": {
            "course_code": "DS341",
            "course_name": "Data Mining",
            "credit_hours": 3,
        },
        "add_course": {
            "course_code": "AI331",
            "course_name": "Machine Learning",
            "credit_hours": 4,
        },
        "academic_improvement": {
            "timing_effect": "EARLIER",
            "term_difference": -1,
            "terms_saved": 1,
            "blockers_resolved": ["AI424"],
        },
        "timetable": {
            "status": "CERTIFIED",
            "certified_options": [
                {
                    "credit_hours": 15,
                    "complete_sections": [
                        {"course_code": "AI331", "section": "M2"},
                        {"course_code": "CS323", "section": "M1"},
                    ],
                }
            ],
        },
    }
    answer = render_improve_current_timetable(
        "English",
        {
            "ok": True,
            "status": "IMPROVEMENTS_FOUND",
            "recommended_change": {
                "kind": "COURSE_REPLACEMENT",
                "replacement": replacement,
            },
            "graduation_improvements": [],
            "schedule_quality_improvements": [],
            "search": {"bounded": True},
        },
    )

    assert "DS341 — Data Mining → AI331 — Machine Learning" in answer
    assert "credits: 3 → 4" in answer
    assert "graduation effect: earlier" in answer
    assert "blockers resolved: AI424" in answer
    assert "AI331-M2" in answer
    assert "CS323-M1" in answer
    assert "read-only" in answer


def test_timetable_improvement_renderer_preserves_section_changes_in_arabic() -> None:
    schedule = {
        "rank": 1,
        "credit_hours": 15,
        "changed_sections": [
            {"course_code": "AI331", "from_sections": ["M1"], "to_sections": ["M2"]}
        ],
        "before": {"days_on_campus": 5, "total_daily_span_minutes": 1800},
        "after": {"days_on_campus": 4, "total_daily_span_minutes": 1500},
        "improvement": {"campus_days_saved": 1, "daily_span_minutes_saved": 300},
    }
    answer = render_improve_current_timetable(
        "Arabic",
        {
            "ok": True,
            "status": "IMPROVEMENTS_FOUND",
            "recommended_change": {
                "kind": "SECTION_REARRANGEMENT",
                "schedule": schedule,
            },
            "schedule_quality_improvements": [],
            "search": {"bounded": True},
        },
    )

    assert "AI331: M1 → M2" in answer
    assert "15 ساعات معتمدة" in answer
    assert "قبل التغيير أيام الحضور: 5" in answer
    assert "بعد التغيير أيام الحضور: 4" in answer
    assert "أيام حضور موفرة: 1" in answer
    assert "لم يُضف أو يُحذف" in answer


def test_timetable_improvement_requires_changed_not_unchanged_course_codes() -> None:
    schedule = {
        "rank": 1,
        "credit_hours": 17,
        "course_codes": ["DS321", "DS332", "DS341", "MATH471", "STAT301"],
        "changed_sections": [
            {"course_code": "DS321", "from_sections": ["M4"], "to_sections": ["M3"]},
            {"course_code": "DS332", "from_sections": ["M4"], "to_sections": ["M3"]},
        ],
        "before": {"days_on_campus": 5, "total_daily_span_minutes": 1370},
        "after": {"days_on_campus": 5, "total_daily_span_minutes": 1270},
        "improvement": {"campus_days_saved": 0, "daily_span_minutes_saved": 100},
        "meetings": [
            {
                "course_code": "DS321",
                "section": "M4",
                "day": "SUN",
                "start": "10:30",
                "end": "11:45",
            },
            {
                "course_code": "DS321",
                "section": "M3",
                "day": "MON",
                "start": "14:20",
                "end": "15:35",
            },
            {
                "course_code": "DS332",
                "section": "M3",
                "day": "MON",
                "start": "12:50",
                "end": "14:05",
            },
            {
                "course_code": "DS332",
                "section": "M4",
                "day": "TUE",
                "start": "12:50",
                "end": "14:05",
            },
        ],
    }
    row = {
        "tool": "improve_current_timetable",
        "ok": True,
        "status": "IMPROVEMENTS_FOUND",
        "recommended_change": {
            "kind": "SECTION_REARRANGEMENT",
            "schedule": schedule,
        },
        "graduation_improvements": [],
        "schedule_quality_improvements": [schedule],
        "search": {"bounded": True},
    }

    _assert_compound_answer_is_grounded(
        "improve_current_timetable",
        row,
        render_improve_current_timetable,
    )
    english = render_improve_current_timetable("English", row)
    assert "DS321" in english and "DS332" in english
    assert "DS341" not in english and "MATH471" not in english and "STAT301" not in english


def test_every_timetable_improvement_negative_has_stable_bounded_phrase() -> None:
    for status in (
        "NO_VERIFIED_IMPROVEMENT_IN_BOUNDED_SEARCH",
        "NO_REGISTERED_CURRENT_TIMETABLE",
        "BASELINE_REVIEW_REQUIRED",
        "CONSTRAINTS_UNSATISFIED",
        "NOT_DETERMINABLE",
        "NO_SEARCH_BRANCH_ENABLED",
    ):
        answer = render_improve_current_timetable(
            "English",
            {
                "ok": True,
                "status": status,
                "constraints": {"effective_max_credits": 18},
                "baseline": {"credit_hours": 20},
                "search": {"bounded": True},
            },
        )
        arabic = render_improve_current_timetable(
            "Arabic",
            {
                "ok": True,
                "status": status,
                "constraints": {"effective_max_credits": 18},
                "baseline": {"credit_hours": 20},
                "search": {"bounded": True},
            },
        )
        assert "The bounded check did not produce a verified positive result." in answer
        assert "لم ينتج الفحص المحدود نتيجة إيجابية موثقة." in arabic
        assert "read-only" in answer
        assert "للقراءة فقط" in arabic


def test_improvement_renderer_explains_incomplete_registered_meeting_facts() -> None:
    row = {
        "ok": True,
        "status": "BASELINE_REVIEW_REQUIRED",
        "reason_code": "REGISTERED_SECTION_MAPPING_INCOMPLETE",
        "baseline_mapping_issues": [
            {
                "course_code": "DS332",
                "reason_code": "REGISTERED_SECTION_MAPPING_INCOMPLETE",
            }
        ],
        "search": {"bounded": True},
    }

    english = render_improve_current_timetable("English", row)
    arabic = render_improve_current_timetable("Arabic", row)

    assert "exact section or meeting facts are incomplete" in english
    assert "DS332" in english
    assert "بيانات الشعبة أو أوقات اللقاء الدقيقة غير مكتملة" in arabic
    assert "DS332" in arabic
    assert "combines registration and planning sources" not in english


def test_compound_positive_renderers_pass_the_exact_fact_boundary() -> None:
    addition = {
        "tool": "recommend_feasible_course_addition",
        "ok": True,
        "status": "RECOMMENDATION_FOUND",
        "planning_term": "1448/1",
        "baseline_kind": "REGISTERED",
        "recommended_addition": {"course_code": "AI331"},
        "ranked_feasible_additions": [
            {
                "course_code": "AI331",
                "course_name": "Machine Learning",
                "credit_hours": 3,
                "rank": 1,
                "unlock_impact": {
                    "sole_remaining_prerequisite_count": 1,
                    "on_prerequisite_chain_of_count": 2,
                },
                "timetable": {
                    "status": "FEASIBLE",
                    "clash_free_section_count": 1,
                    "clash_free_sections": [{"section": "M1"}],
                },
                "graduation": {
                    "status": "EVALUATED",
                    "timing_effect": "EARLIER",
                    "term_difference": -1,
                    "estimated_additional_terms": 3,
                },
            }
        ],
        "search": {
            "candidates_evaluated": 1,
            "feasible_candidates_found": 1,
            "candidate_limit": 20,
        },
    }
    drop = {
        "tool": "rank_current_course_drop_impact",
        "ok": True,
        "status": "RANKING_AVAILABLE",
        "baseline_kind": "REGISTERED",
        "top_ranked_drop_candidate": {"course_code": "DS341"},
        "ranked_drop_impacts": [
            {
                "course_code": "DS341",
                "course_name": "Data Mining",
                "credit_hours": 3,
                "sections": ["M2"],
                "rank": 1,
                "impact_status": "DELAYED",
                "graduation": {
                    "timing_effect": "LATER",
                    "term_difference": 1,
                    "estimated_additional_terms": 4,
                    "lower_bound_additional_terms": 3,
                    "affected_future_course_codes": ["AI424"],
                },
            }
        ],
        "excluded_courses": [
            {
                "course_code": "GS104",
                "outcome": "NOT_IN_REGISTERED_CURRENT_TIMETABLE",
                "reason_code": "NOT_IN_CURRENT_TIMETABLE",
            }
        ],
        "search": {"drop_scenarios_evaluated": 1, "determinable_scenarios": 1},
    }
    improvement = {
        "tool": "improve_current_timetable",
        "ok": True,
        "status": "IMPROVEMENTS_FOUND",
        "recommended_change": {
            "kind": "COURSE_REPLACEMENT",
            "replacement": {
                "remove_course": {"course_code": "DS341", "credit_hours": 3},
                "add_course": {"course_code": "AI331", "credit_hours": 3},
                "academic_improvement": {
                    "timing_effect": "EARLIER",
                    "term_difference": -1,
                },
                "timetable": {
                    "status": "CERTIFIED",
                    "certified_options": [
                        {
                            "credit_hours": 15,
                            "complete_sections": [
                                {"course_code": "AI331", "section": "M2"},
                                {"course_code": "CS323", "section": "M1"},
                            ],
                        }
                    ],
                },
            },
        },
        "graduation_improvements": [],
        "schedule_quality_improvements": [],
        "search": {"bounded": True},
    }

    _assert_compound_answer_is_grounded(
        "recommend_feasible_course_addition",
        addition,
        render_recommend_feasible_course_addition,
    )
    _assert_compound_answer_is_grounded(
        "rank_current_course_drop_impact",
        drop,
        render_rank_current_course_drop_impact,
    )
    _assert_compound_answer_is_grounded(
        "improve_current_timetable",
        improvement,
        render_improve_current_timetable,
    )

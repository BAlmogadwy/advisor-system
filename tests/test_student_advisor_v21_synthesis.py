from __future__ import annotations

from copy import deepcopy

import pytest

from core.services.student_advisor_v21_synthesis import (
    CURRENT_TIMETABLE_LOAD_POLICY_SCOPE,
    CURRENT_TIMETABLE_PRIORITY_SCOPE,
    TIMETABLE_BUILD_PRIORITY_SCOPE,
    joined_answer_blocks,
    joined_scope_tools,
    localize_timetable_day,
    localize_timetable_unplaced_reason,
    render_current_timetable_load_policy_assessment,
    render_current_timetable_priority_assessment,
    render_timetable_build_priority_assessment,
)


def _timetable() -> dict:
    return {
        "tool": "my_timetable",
        "ok": True,
        "schedule_kind": "REGISTERED",
        "registered_course_count": 2,
        "registered_credit_hours": 8,
        "registrations": [
            {"course_code": "DS321", "section": "M4"},
            {"course_code": "DS332", "section": "M4"},
        ],
    }


def _progress() -> dict:
    return {
        "tool": "my_progress",
        "ok": True,
        "registered_requirement_course_codes": ["DS321", "DS332"],
        "unlock_impact_ranking": [
            {"code": "IS362", "sole_remaining_prerequisite_count": 1},
            {"code": "AI201", "sole_remaining_prerequisite_count": 1},
            {"code": "DS352", "sole_remaining_prerequisite_count": 0},
        ],
        "prerequisites_satisfied": [
            {"code": "IS362"},
            {"code": "AI201"},
            {"code": "DS352"},
        ],
    }


def _main_term_load_policy() -> dict:
    return {
        "tool": "policy_lookup",
        "ok": True,
        "direct_policy_evidence": [
            {
                "policy_id": "TU.LOAD.SEMESTER_RANGE",
                # The synthesis must ignore prose figures and use rule.max_value.
                "statement_ar": "نص تجريبي يذكر 88، بينما الحقل المنظم هو الحاكم.",
                "rule": {
                    "rule_type": "range",
                    "min_value": 12,
                    "max_value": 19,
                    "unit": "credit_units",
                    "applies_to": {
                        "study_system": "TWO_SEMESTER",
                        "term_type": "MAIN",
                    },
                },
                "citation": {
                    "policy_id": "TU.LOAD.SEMESTER_RANGE",
                    "document_title": "الدليل الإرشادي للطالب والطالبة",
                    "page": 23,
                },
            }
        ],
    }


def _review009_timetable() -> dict:
    return {
        **_timetable(),
        "academic_year": 1448,
        "term": 1,
        "registered_credit_hours": 17,
    }


def _proposal() -> dict:
    return {
        "tool": "build_timetable_proposal",
        "ok": True,
        "alternatives": [
            {
                "option": "A1",
                "courses": [
                    {"course_code": "DS341", "section": "M2"},
                    {"course_code": "IS362", "section": "M1"},
                ],
                "unplaced_courses": [],
            },
            {
                "option": "A2",
                "courses": [{"course_code": "DS341", "section": "M2"}],
                "unplaced_courses": [],
            },
        ],
    }


@pytest.mark.parametrize("language", ["Arabic", "English"])
def test_current_timetable_priority_assessment_is_direct_but_bounded(language: str) -> None:
    text = render_current_timetable_priority_assessment(
        language,
        _timetable(),
        _progress(),
    )

    assert "2" in text
    assert "DS321" in text
    assert "DS332" in text
    assert "IS362" in text
    assert "AI201" in text
    if language == "Arabic":
        assert "تتسق كل رموز مقررات جدولك" in text
        assert "لا يثبت أن الجدول هو الاختيار الأكاديمي الأفضل" in text
        assert "لا يعني عدم التداخل أن جدولك خاطئ" in text
        assert "لا تثبت طرح شعبة أو وجود مقعد أو السماح بالتسجيل" in text
    else:
        assert "every registered timetable code aligns" in text
        assert "not that the timetable is academically optimal" in text
        assert "does not mean the timetable is wrong" in text
        assert "do not prove offering, seats, registration permission" in text


@pytest.mark.parametrize("language", ["Arabic", "English"])
def test_timetable_build_priority_assessment_never_upgrades_solver_objective(
    language: str,
) -> None:
    text = render_timetable_build_priority_assessment(
        language,
        _proposal(),
        _progress(),
    )

    assert "IS362" in text
    assert "AI201" in text
    assert "DS352" in text
    if language == "Arabic":
        assert "لا يثبت أن إنشاء هذه الخيارات حسّن موعد التخرج" in text
        assert "لا توصف البدائل بأنها مُحسّنة للأولوية" in text
        assert "محلّل الجدول" not in text
    else:
        assert "do not establish that the timetable search optimised graduation timing" in text
        assert "must not be described as priority-optimised" in text
        assert "solver" not in text


def test_joined_answer_blocks_require_typed_outcome_and_tool_pairs() -> None:
    latest = {
        "my_timetable": _timetable(),
        "my_progress": _progress(),
        "build_timetable_proposal": _proposal(),
    }

    review = joined_answer_blocks(
        "Arabic",
        latest,
        planned_tools=("my_timetable", "my_progress"),
        requested_outcomes=("current_timetable", "course_priority"),
    )
    build = joined_answer_blocks(
        "English",
        latest,
        planned_tools=("build_timetable_proposal", "my_progress"),
        requested_outcomes=("timetable_build", "course_priority"),
    )
    unrelated = joined_answer_blocks(
        "Arabic",
        latest,
        planned_tools=("my_timetable", "my_progress"),
        requested_outcomes=("current_timetable",),
    )

    assert [scope for scope, _block in review] == [CURRENT_TIMETABLE_PRIORITY_SCOPE]
    assert [scope for scope, _block in build] == [TIMETABLE_BUILD_PRIORITY_SCOPE]
    assert unrelated == ()


@pytest.mark.parametrize("language", ["Arabic", "English"])
def test_current_timetable_load_policy_assessment_uses_structured_values(
    language: str,
) -> None:
    timetable = _review009_timetable()

    text = render_current_timetable_load_policy_assessment(
        language,
        timetable,
        _main_term_load_policy(),
    )

    assert text is not None
    assert "17" in text
    assert "19" in text
    assert "2" in text
    assert "88" not in text
    assert "TU.LOAD.SEMESTER_RANGE" in text
    if language == "Arabic":
        assert "يقل عبؤك عن الرقم التنظيمي بـ 2 ساعة معتمدة" in text
        assert "العبء الدراسي وحده لا يثبت جودة اختيار مقررات الجدول" in text
        assert "ولا يثبت أن الجدول يسرّع التخرج" in text
    else:
        assert "2 credit hours below that maximum" in text
        assert "Credit load alone does not establish" in text
        assert "faster for graduation" in text


@pytest.mark.parametrize("language", ["Arabic", "English"])
def test_load_policy_join_requires_the_exact_outcome_and_tool_pair(language: str) -> None:
    timetable = _review009_timetable()
    policy = _main_term_load_policy()
    latest = {"my_timetable": timetable, "policy_lookup": policy}

    exact = joined_answer_blocks(
        language,
        latest,
        planned_tools=("policy_lookup", "my_timetable"),
        requested_outcomes=("policy_rule", "current_timetable"),
    )
    extra_outcome = joined_answer_blocks(
        language,
        latest,
        planned_tools=("my_timetable", "policy_lookup"),
        requested_outcomes=("current_timetable", "policy_rule", "course_priority"),
    )
    extra_tool = joined_answer_blocks(
        language,
        {**latest, "my_progress": _progress()},
        planned_tools=("my_timetable", "policy_lookup", "my_progress"),
        requested_outcomes=("current_timetable", "policy_rule"),
    )

    assert [scope for scope, _block in exact] == [CURRENT_TIMETABLE_LOAD_POLICY_SCOPE]
    assert extra_outcome == ()
    assert extra_tool == ()
    assert joined_scope_tools(CURRENT_TIMETABLE_LOAD_POLICY_SCOPE) == (
        "my_timetable",
        "policy_lookup",
    )


def _unsafe_load_policy_cases() -> list[tuple[dict, dict]]:
    timetable = _review009_timetable()
    policy = _main_term_load_policy()
    summer = {**timetable, "term": 3}
    unknown_term = {**timetable, "term": None}
    string_term = {**timetable, "term": "1"}
    expected = {
        **timetable,
        "schedule_kind": "EXPECTED_PLAN",
        "is_expected_plan": True,
    }
    prose_load = {**timetable, "registered_credit_hours": "17"}

    prose_max = deepcopy(policy)
    prose_max["direct_policy_evidence"][0]["rule"]["max_value"] = "19"
    background_only = {**policy, "direct_policy_evidence": []}
    wrong_scope = deepcopy(policy)
    wrong_scope["direct_policy_evidence"][0]["rule"]["applies_to"]["term_type"] = "SUMMER"
    unresolved_range = deepcopy(policy)
    unresolved_range["direct_policy_evidence"][0]["source_leaves_unresolved"] = True
    expected_graduate = deepcopy(policy)
    expected_graduate["direct_policy_evidence"].append(
        {
            "policy_id": "TU.LOAD.EXPECTED_GRADUATE_REQUEST",
            "source_leaves_unresolved": True,
            "rule": {"max_value": 16},
        }
    )
    return [
        (summer, policy),
        (unknown_term, policy),
        (string_term, policy),
        (expected, policy),
        (prose_load, policy),
        (timetable, prose_max),
        (timetable, background_only),
        (timetable, wrong_scope),
        (timetable, unresolved_range),
        (timetable, expected_graduate),
    ]


@pytest.mark.parametrize(("timetable", "policy"), _unsafe_load_policy_cases())
def test_load_policy_comparison_fails_closed_outside_verified_main_term_scope(
    timetable: dict,
    policy: dict,
) -> None:
    assert render_current_timetable_load_policy_assessment("Arabic", timetable, policy) is None
    assert (
        joined_answer_blocks(
            "Arabic",
            {"my_timetable": timetable, "policy_lookup": policy},
            planned_tools=("my_timetable", "policy_lookup"),
            requested_outcomes=("current_timetable", "policy_rule"),
        )
        == ()
    )


@pytest.mark.parametrize("language", ["Arabic", "English"])
def test_load_policy_joined_block_passes_consistency_checker(language: str) -> None:
    from core.services.answer_consistency import EvidenceValidationScope, check_answer

    timetable = _review009_timetable()
    policy = _main_term_load_policy()
    ((scope_name, answer),) = joined_answer_blocks(
        language,
        {"my_timetable": timetable, "policy_lookup": policy},
        planned_tools=("my_timetable", "policy_lookup"),
        requested_outcomes=("current_timetable", "policy_rule"),
    )

    assert scope_name == CURRENT_TIMETABLE_LOAD_POLICY_SCOPE
    assert (
        check_answer(
            answer,
            tool_results=[timetable, policy],
            question=(
                "هل جدولي الحالي يستفيد من الحد الأعلى للساعات بشكل جيد؟"
                if language == "Arabic"
                else "Does my current timetable make good use of the maximum credit load?"
            ),
            # The two capability blocks own their individual completeness. This
            # supplemental scope validates only the cross-row relation.
            required_tools=set(),
            evidence_scopes=(
                EvidenceValidationScope(
                    answer=answer,
                    tool_results=(timetable, policy),
                    required_tools=frozenset(),
                ),
            ),
        )
        == []
    )


@pytest.mark.parametrize(
    ("language", "planned_tools", "outcomes", "question", "rows"),
    [
        pytest.param(
            "Arabic",
            ("my_timetable", "my_progress"),
            ("current_timetable", "course_priority"),
            "هل سجلت المواد الصح لهذا الترم؟",
            (_timetable(), _progress()),
            id="registered-course-review",
        ),
        pytest.param(
            "English",
            ("build_timetable_proposal", "my_progress"),
            ("timetable_build", "course_priority"),
            "Build a timetable and prioritise courses that prevent delay.",
            (_proposal(), _progress()),
            id="priority-timetable-build",
        ),
    ],
)
def test_joined_answer_block_passes_consistency_check_with_both_evidence_owners(
    language: str,
    planned_tools: tuple[str, ...],
    outcomes: tuple[str, ...],
    question: str,
    rows: tuple[dict, ...],
) -> None:
    from core.services.answer_consistency import EvidenceValidationScope, check_answer

    latest = {row["tool"]: row for row in rows}
    ((scope_name, answer),) = joined_answer_blocks(
        language,
        latest,
        planned_tools=planned_tools,
        requested_outcomes=outcomes,
    )

    assert (
        check_answer(
            answer,
            tool_results=list(rows),
            question=question,
            required_tools=set(planned_tools),
            known_course_codes=frozenset({"DS321", "DS332", "DS341", "IS362", "AI201", "DS352"}),
            evidence_scopes=(
                EvidenceValidationScope(
                    answer=answer,
                    tool_results=rows,
                    # The individual capability blocks own completeness. This joined
                    # block receives both rows only to validate cross-row relations.
                    required_tools=frozenset(),
                ),
            ),
        )
        == []
    ), scope_name


def test_arabic_variant_reason_uses_stable_code_not_english_fallback() -> None:
    item = {
        "course_code": "MATH471",
        "reason_code": "OMITTED_IN_THIS_VARIANT",
        "reason": (
            "This Planner variant did not place the course; another generated variant did. "
            "Compare the other options."
        ),
    }

    arabic = localize_timetable_unplaced_reason("Arabic", item)
    english = localize_timetable_unplaced_reason("English", item)
    unknown_arabic = localize_timetable_unplaced_reason(
        "Arabic",
        {"reason_code": "NEW_INTERNAL_REASON", "reason": "New internal fallback."},
    )

    assert "وضعه خيار آخر" in arabic
    assert "Planner" not in arabic
    assert english == item["reason"]
    assert unknown_arabic == "لم يُدرج المقرر في هذا الخيار ضمن القيود المحددة."
    assert "internal" not in unknown_arabic


@pytest.mark.parametrize(
    ("token", "arabic"),
    [
        ("SUN", "الأحد"),
        ("MON", "الاثنين"),
        ("TUE", "الثلاثاء"),
        ("WED", "الأربعاء"),
        ("THU", "الخميس"),
    ],
)
def test_timetable_day_localization_is_display_only(token: str, arabic: str) -> None:
    assert localize_timetable_day("Arabic", token) == arabic
    assert localize_timetable_day("English", token) == token


@pytest.mark.parametrize(
    ("language", "ceiling_text", "monday", "thursday", "raw_day"),
    [
        (
            "Arabic",
            "الحد الأعلى لساعات الجدول المقترح: 18 ساعة معتمدة.",
            "الاثنين",
            "الخميس",
            "MON",
        ),
        (
            "English",
            "Proposal credit ceiling: 18 credit hours.",
            "MON",
            "THU",
            "الاثنين",
        ),
    ],
)
def test_composite_proposal_renderer_shows_exact_cap_pin_and_localized_days(
    language: str,
    ceiling_text: str,
    monday: str,
    thursday: str,
    raw_day: str,
) -> None:
    from core.services.answer_consistency import check_answer
    from core.services.student_advisor_v2 import _safe_timetable_proposal_fact_fragment

    row = {
        "tool": "build_timetable_proposal",
        "ok": True,
        "mode": "from_scratch",
        "credit_ceiling": 18,
        "pinned_sections": [{"course_code": "DS341", "section_label": "M2"}],
        "constraints_satisfied": True,
        "alternatives": [
            {
                "option": "A1",
                "planner_options": ["A1"],
                "scheduled_courses": 1,
                "target_courses": 1,
                "courses": [{"course_code": "DS341", "section": "M2", "credits": 3}],
                "meetings": [
                    {
                        "course_code": "DS341",
                        "section": "M2",
                        "day": "MON",
                        "start": "10:30",
                        "end": "11:45",
                    },
                    {
                        "course_code": "DS341",
                        "section": "M2",
                        "day": "THU",
                        "start": "10:30",
                        "end": "11:45",
                    },
                ],
                "unplaced_courses": [],
            }
        ],
        "unplaced_courses": [],
        "constraint_failures": [],
    }

    answer = _safe_timetable_proposal_fact_fragment(language, row)
    lines = answer.splitlines()
    cap_line = next(line for line in lines if ceiling_text in line)
    meeting_line = next(line for line in lines if "DS341" in line and "10:30-11:45" in line)

    assert "10:30" not in cap_line
    assert "18" not in meeting_line
    assert monday in meeting_line
    assert thursday in meeting_line
    assert raw_day not in answer
    assert ("الشعبة M2" if language == "Arabic" else "section M2") in meeting_line
    assert (
        check_answer(
            answer,
            tool_results=[row],
            question=(
                "ابنِ لي جدولًا بحد أقصى 18 ساعة وثبت DS341-M2."
                if language == "Arabic"
                else "Build a timetable capped at 18 credit hours and pin DS341-M2."
            ),
            required_tools={"build_timetable_proposal"},
            known_course_codes=frozenset({"DS341"}),
        )
        == []
    )

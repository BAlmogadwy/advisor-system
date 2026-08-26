"""Production-shaped V2.1 timetable constraint rendering regressions."""

from __future__ import annotations

import pytest

from core.services.answer_consistency import (
    UNSUPPORTED_ACADEMIC_FACT,
    EvidenceValidationScope,
    check_answer,
)
from core.services.llm_remote_privacy import (
    RemoteIdentityMap,
    project_tool_result_for_remote,
)
from core.services.student_advisor_v2 import _safe_v21_planned_answer

_TOOL = "build_timetable_proposal"
_PINS = [
    {"course_code": "DS341", "section_label": "M2"},
    {"course_code": "DS432", "section_label": "M3"},
]
_KNOWN_CODES = frozenset({"DS341", "DS432"})


def _project_and_compose(language: str, local_row: dict) -> tuple[dict, str, str]:
    row = project_tool_result_for_remote(_TOOL, local_row, RemoteIdentityMap())
    answer, complete, scopes = _safe_v21_planned_answer(
        language,
        [row],
        "",
        planned_tools=(_TOOL,),
    )
    assert complete is True
    assert len(scopes) == 1
    return row, answer, scopes[0][1]


def _violations(*, answer: str, block: str, row: dict, question: str) -> list[str]:
    return check_answer(
        answer,
        tool_results=[row],
        question=question,
        required_tools={_TOOL},
        known_course_codes=_KNOWN_CODES,
        evidence_scopes=(
            EvidenceValidationScope(
                answer=block,
                tool_results=(row,),
                required_tools=frozenset({_TOOL}),
            ),
        ),
    )


def _two_pin_negative() -> dict:
    baseline = [
        {
            "course_code": "DS341",
            "course_name": "Data Governance",
            "credits": 3,
            "section": "M2",
            "meetings": ["SUN 14:30-15:45", "THU 14:30-15:45"],
        }
    ]
    return {
        "tool": _TOOL,
        "ok": True,
        "status": "CONSTRAINTS_UNSATISFIED",
        "mode": "around_current",
        "baseline_kind": "REGISTERED",
        "baseline_sections": baseline,
        "current_sections": baseline,
        "baseline_credit_hours": 17,
        "current_credit_hours": 17,
        "credit_ceiling": 19,
        "must_take_courses": ["DS341", "DS432"],
        "pinned_sections": _PINS,
        "constraints_satisfied": False,
        "constraint_failures": [
            {
                "course_code": "DS432",
                "section_label": "M3",
                "reason": (
                    "Must-take course could not be scheduled: "
                    "Model infeasible under current hard constraints"
                ),
            },
            {
                "course_code": "DS341",
                "section_label": "M2",
                "reason": (
                    "No valid timetable satisfies this required course under "
                    "the current constraints."
                ),
            },
        ],
        "alternatives": [],
        "unplaced_courses": [
            {
                "course_code": "DS432",
                "reason_code": "DID_NOT_FIT",
                "reason": "No combination satisfied all the limits given.",
            }
        ],
        "no_additional_courses": False,
    }


def _two_pin_positive() -> dict:
    return {
        "tool": _TOOL,
        "ok": True,
        "status": "PROPOSALS_GENERATED",
        "mode": "from_scratch",
        "baseline_kind": "NONE",
        "baseline_sections": [],
        "baseline_credit_hours": 0,
        "credit_ceiling": 19,
        "must_take_courses": ["DS341", "DS432"],
        "pinned_sections": _PINS,
        "constraints_satisfied": True,
        "constraint_failures": [],
        "alternatives": [
            {
                "option": "A1",
                "planner_options": ["A1"],
                "courses": [
                    {"course_code": "DS341", "section": "M2", "credits": 3},
                    {"course_code": "DS432", "section": "M3", "credits": 3},
                ],
                "meetings": [
                    {
                        "course_code": "DS341",
                        "section": "M2",
                        "day": "SUN",
                        "start": "14:30",
                        "end": "15:45",
                    },
                    {
                        "course_code": "DS432",
                        "section": "M3",
                        "day": "MON",
                        "start": "09:00",
                        "end": "10:15",
                    },
                ],
                "scheduled_courses": 2,
                "target_courses": 2,
                "course_count": 2,
                "proposed_credit_hours": 6,
                "total_credit_hours": 6,
                "unplaced_courses": [],
            }
        ],
        "unplaced_courses": [],
        "no_additional_courses": False,
    }


@pytest.mark.parametrize(
    ("language", "question", "neutral_ceiling", "proposal_ceiling"),
    [
        (
            "Arabic",
            "ثبت مقررين DS341-M2 وDS432-M3 وابنِ الباقي حول جدولي الحالي.",
            "الحد الأعلى للساعات المطبّق في فحص القيود: 19 ساعة معتمدة.",
            "الحد الأعلى لساعات الجدول المقترح: 19 ساعة معتمدة.",
        ),
        (
            "English",
            "Pin DS341-M2 and DS432-M3 and build the rest around my current timetable.",
            "Credit-hour ceiling applied to the constraint check: 19 credit hours.",
            "Proposal credit ceiling: 19 credit hours.",
        ),
    ],
)
def test_two_pin_negative_uses_neutral_relation_scope_without_weakening_checker(
    language: str,
    question: str,
    neutral_ceiling: str,
    proposal_ceiling: str,
) -> None:
    local_row = _two_pin_negative()
    row, answer, block = _project_and_compose(language, local_row)

    assert neutral_ceiling in answer
    assert proposal_ceiling not in answer
    assert _violations(answer=answer, block=block, row=row, question=question) == []

    if language == "Arabic":
        for raw_reason in [
            *(item["reason"] for item in local_row["constraint_failures"]),
            *(item["reason"] for item in local_row["unplaced_courses"]),
        ]:
            assert raw_reason not in answer
        assert "تعذر تحقيق هذا القيد ضمن نطاق فحص الجدولة." in answer
        assert "تعذر وضع المقرر ضمن القيود المحددة في نطاق فحص الجدولة." in answer

    # Control: the former relation-owning label still fails against a zero-option
    # negative.  The renderer fix must not relax schedule-relation validation.
    relation_leaking_answer = answer.replace(neutral_ceiling, proposal_ceiling)
    relation_leaking_block = block.replace(neutral_ceiling, proposal_ceiling)
    assert UNSUPPORTED_ACADEMIC_FACT in _violations(
        answer=relation_leaking_answer,
        block=relation_leaking_block,
        row=row,
        question=question,
    )


@pytest.mark.parametrize(
    ("language", "question", "proposal_ceiling", "neutral_ceiling"),
    [
        (
            "Arabic",
            "أنشئ جدولاً من الصفر وثبت DS341-M2 وDS432-M3.",
            "الحد الأعلى لساعات الجدول المقترح: 19 ساعة معتمدة.",
            "الحد الأعلى للساعات المطبّق في فحص القيود",
        ),
        (
            "English",
            "Build from scratch and pin DS341-M2 and DS432-M3.",
            "Proposal credit ceiling: 19 credit hours.",
            "Credit-hour ceiling applied to the constraint check",
        ),
    ],
)
def test_two_pin_positive_retains_proposal_relation_label(
    language: str,
    question: str,
    proposal_ceiling: str,
    neutral_ceiling: str,
) -> None:
    row, answer, block = _project_and_compose(language, _two_pin_positive())

    assert proposal_ceiling in answer
    assert neutral_ceiling not in answer
    assert "DS341" in answer and "M2" in answer
    assert "DS432" in answer and "M3" in answer
    assert _violations(answer=answer, block=block, row=row, question=question) == []

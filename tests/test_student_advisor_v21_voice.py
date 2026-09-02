from __future__ import annotations

import pytest

from core.services.answer_consistency import EvidenceValidationScope, check_answer
from core.services.student_advisor_v2 import _safe_v21_planned_answer
from core.services.student_advisor_v21_render import (
    render_recommend_feasible_course_addition,
)


@pytest.mark.parametrize(
    ("language", "question", "lead", "next_step", "boundary"),
    [
        (
            "English",
            "Which courses can I take this term?",
            "Based on your degree progress",
            "Next step:",
            "Before registering",
        ),
        (
            "Arabic",
            "وش المقررات اللي أقدر آخذها هذا الفصل؟",
            "بحسب تقدمك في الخطة",
            "الخطوة التالية:",
            "قبل التسجيل",
        ),
    ],
)
def test_plain_course_readiness_uses_human_adviser_contract_and_stays_grounded(
    language: str,
    question: str,
    lead: str,
    next_step: str,
    boundary: str,
) -> None:
    progress = {
        "tool": "my_progress",
        "ok": True,
        "counts": {"open": 2, "locked": 1},
        "prerequisites_satisfied": [
            {"code": "CS102", "course_name": "Programming II"},
            {"code": "CS101", "course_name": "Programming I"},
        ],
        "prerequisite_blocked": [{"code": "CS399"}],
        "unlock_impact_ranking": [{"code": "CS101"}, {"code": "CS102"}],
    }

    answer, complete, scopes = _safe_v21_planned_answer(
        language,
        [progress],
        "",
        planned_tools=("my_progress",),
        requested_outcomes=("available_courses",),
    )

    assert complete is True
    assert answer.startswith(lead)
    assert not answer.startswith("###")
    assert "CS102" in answer and "CS101" in answer
    assert "CS399" not in answer
    assert answer.count(next_step) == 1
    assert answer.count(boundary) == 1
    assert "unlock impact" not in answer.lower()
    assert "bounded" not in answer.lower()
    assert "candidate" not in answer.lower()
    assert "as an ai" not in answer.lower()
    assert "how else can i help" not in answer.lower()

    assert (
        check_answer(
            answer,
            tool_results=[progress],
            question=question,
            required_tools={"my_progress"},
            known_course_codes=frozenset({"CS101", "CS102", "CS399"}),
            evidence_scopes=(
                EvidenceValidationScope(
                    answer=scopes[0][1],
                    tool_results=(progress,),
                    required_tools=frozenset({"my_progress"}),
                ),
            ),
        )
        == []
    )


def test_course_addition_leads_with_recommendation_and_has_one_next_step() -> None:
    recommended = {
        "course_code": "AI331",
        "course_name": "Machine Learning",
        "credit_hours": 3,
        "rank": 1,
        "eligibility": {"status": "PREREQUISITES_SATISFIED"},
        "timetable": {"clash_free_section_count": 1, "clash_free_sections": [{"section": "M1"}]},
    }
    answer = render_recommend_feasible_course_addition(
        "English",
        {
            "ok": True,
            "status": "RECOMMENDATION_FOUND",
            "planning_term": "1448/1",
            "baseline_kind": "REGISTERED",
            "recommended_addition": recommended,
            "ranked_feasible_additions": [recommended],
            "search": {
                "candidates_evaluated": 3,
                "feasible_candidates_found": 1,
                "candidate_limit": 10,
                "search_truncated": False,
            },
        },
    )

    assert answer.startswith("My first recommendation is to consider adding AI331")
    assert "Why it leads this check:" in answer
    assert answer.count("Next step:") == 1
    assert "The bounded check" not in answer
    assert "candidate(s)" not in answer
    assert "the student must" not in answer

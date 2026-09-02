"""Typed cardinality contract for V2.1 course-priority answers."""

from __future__ import annotations

import json

import pytest

from core.services.answer_consistency import (
    REQUESTED_EVIDENCE_OMITTED,
    check_answer,
)
from core.services.llm_remote_privacy import (
    RemoteIdentityMap,
    project_tool_result_for_remote,
)
from core.services.student_advisor_v2 import (
    _safe_progress_fact_fragment,
    _v21_argument_provenance_contract,
    _v21_missing_explicit_constraint_paths,
    _v21_priority_limits,
    student_v2_tool_schemas,
    student_v21_tool_schemas,
)
from core.services.student_advisor_v21_plan import (
    PlannedCapabilityCall,
    StudentRequestOutcome,
    StudentTurnPlan,
    TurnPlanDecision,
    TurnPlanProvenanceError,
    validate_capability_argument_provenance,
)


def _ranking(count: int = 7) -> list[dict[str, object]]:
    return [
        {
            "code": f"CS{300 + index}",
            "course_name": f"Course {index}",
            "sole_remaining_prerequisite_count": count - index,
            "on_prerequisite_chain_of_count": (count - index) * 2,
        }
        for index in range(1, count + 1)
    ]


def _progress_row(limit: int = 5) -> dict[str, object]:
    ranking = _ranking()
    return {
        "tool": "my_progress",
        "ok": True,
        "counts": {"open": len(ranking), "locked": 1},
        "unlock_impact_ranking": ranking,
        "unlock_impact_ranking_basis": ("SOLE_REMAINING_UNLOCK_COUNT_THEN_DOWNSTREAM_COUNT"),
        "unlock_impact_ranking_note": "Typed ranking note.",
        "requested_priority_limit": limit,
        "requested_unlock_impact_ranking": ranking[:limit],
        "requested_priority_limit_fulfilled": len(ranking) >= limit,
        "prerequisites_satisfied": ranking,
        "prerequisite_blocked": [{"code": "CS399"}],
    }


def _provenance(question: str, arguments: dict[str, object]) -> dict[str, object]:
    contract = _v21_argument_provenance_contract(
        question,
        history=[],
        prior_presentation={},
        prior_course_names={},
    )
    return validate_capability_argument_provenance(
        "my_progress",
        arguments,
        contract=contract,
    )


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("عطيني أفضل 5 مقررات أسجلها حسب تأثيرها على التخرج.", [5]),
        ("عطني أهم خمسة مواد حسب أثر المتطلبات", [5]),
        ("Show my top 8 courses by impact", [8]),
        ("Which are my 3 highest-priority courses?", [3]),
        ("أعطني خمسة من أهم المقررات", [5]),
        ("أعطني 5 مقررات مرتبة بالأولوية", [5]),
        ("Do not show top 5 courses; show top 3 courses", [3]),
        ("لا تعطيني 5 مقررات مرتبة بالأولوية", []),
        ("ابنِ جدولاً بحد أقصى 15 ساعة", []),
    ],
)
def test_priority_limit_parser_binds_only_a_top_n_course_role(
    question: str,
    expected: list[int],
) -> None:
    assert _v21_priority_limits(question) == expected


def test_priority_limit_provenance_accepts_the_exact_sa_priority_numeral() -> None:
    question = "عطيني أفضل 5 مقررات أسجلها حسب تأثيرها على التخرج."

    assert _provenance(question, {"priority_limit": 5}) == {"priority_limit": 5}
    with pytest.raises(TurnPlanProvenanceError, match="trusted turn sources"):
        _provenance(question, {"priority_limit": 4})


def test_priority_limit_is_required_when_the_plan_selects_my_progress() -> None:
    question = "عطيني أفضل 5 مقررات أسجلها حسب تأثيرها على التخرج."

    def plan(arguments: dict[str, object]) -> StudentTurnPlan:
        return StudentTurnPlan(
            decision=TurnPlanDecision.EXECUTE,
            evidence_requests=(
                PlannedCapabilityCall(
                    capability="my_progress",
                    arguments=arguments,
                ),
            ),
            requested_outcomes=(StudentRequestOutcome.COURSE_PRIORITY,),
        )

    assert _v21_missing_explicit_constraint_paths(plan({}), question) == (
        "my_progress.priority_limit",
    )
    # Constraint coverage is value-sensitive: an omitted or wrong final value
    # fails closed on the same owned field path.
    assert _v21_missing_explicit_constraint_paths(plan({"priority_limit": 4}), question) == (
        "my_progress.priority_limit",
    )
    assert _v21_missing_explicit_constraint_paths(plan({"priority_limit": 5}), question) == ()


def test_priority_limit_cannot_be_sourced_from_history_or_an_unrelated_number() -> None:
    contract = _v21_argument_provenance_contract(
        "وش أهم المقررات المتبقية؟",
        history=[{"role": "user", "content": "Show my top 7 courses"}],
        prior_presentation={},
        prior_course_names={},
    )

    with pytest.raises(TurnPlanProvenanceError, match="no approved provenance rule"):
        validate_capability_argument_provenance(
            "my_progress",
            {"priority_limit": 7},
            contract=contract,
        )


def test_only_v21_advertises_the_closed_priority_limit_schema() -> None:
    v21 = {row["function"]["name"]: row["function"] for row in student_v21_tool_schemas()}
    v2 = {row["function"]["name"]: row["function"] for row in student_v2_tool_schemas()}

    typed = v21["my_progress"]["parameters"]["properties"]["priority_limit"]
    assert typed["type"] == "integer"
    assert typed["minimum"] == 1
    assert typed["maximum"] == 20
    assert "explicitly requested top-N" in typed["description"]
    assert "priority_limit" in v21["my_progress"]["description"]
    assert "priority_limit" not in v2["my_progress"]["parameters"]["properties"]


def test_remote_projection_keeps_the_typed_slice_and_drops_unknown_row_fields() -> None:
    row = _progress_row()
    row["student_id"] = 4901291
    row["requested_unlock_impact_ranking"][0]["private_adviser_note"] = "SECRET"

    projected = project_tool_result_for_remote(
        "my_progress",
        row,
        RemoteIdentityMap(),
    )

    assert projected["requested_priority_limit"] == 5
    assert projected["requested_priority_limit_fulfilled"] is True
    assert [item["code"] for item in projected["requested_unlock_impact_ranking"]] == [
        item["code"] for item in row["unlock_impact_ranking"][:5]
    ]
    assert projected["unlock_impact_ranking_basis"] == (
        "SOLE_REMAINING_UNLOCK_COUNT_THEN_DOWNSTREAM_COUNT"
    )
    encoded = json.dumps(projected, ensure_ascii=False)
    assert "SECRET" not in encoded
    assert "4901291" not in encoded


@pytest.mark.parametrize("language", ["Arabic", "English"])
def test_typed_renderer_lists_exactly_the_requested_prefix_and_labels_the_basis(
    language: str,
) -> None:
    row = _progress_row()

    answer = _safe_progress_fact_fragment(language, row)

    expected = [item["code"] for item in row["requested_unlock_impact_ranking"]]
    assert all(code in answer for code in expected)
    assert all(item["code"] not in answer for item in row["unlock_impact_ranking"][5:])
    assert "CS399" not in answer
    assert ("لماذا هذا الترتيب" if language == "Arabic" else "Why this order") in answer
    assert (
        check_answer(
            answer,
            tool_results=[row],
            question="عطيني أفضل 5 مقررات أسجلها حسب تأثيرها على التخرج.",
            required_tools={"my_progress"},
        )
        == []
    )


def test_priority_evidence_checker_rejects_omitted_extra_and_wrongly_ordered_codes() -> None:
    row = _progress_row()
    good = _safe_progress_fact_fragment("English", row)
    first, second, sixth = "CS301", "CS302", "CS306"
    variants = (
        good.replace(f"1. {first}", "1. OMITTED"),
        good.replace("Why this order:", f"6. {sixth}\nWhy this order:"),
        good.replace(first, "SWAP", 1).replace(second, first, 1).replace("SWAP", second, 1),
    )

    for answer in variants:
        assert REQUESTED_EVIDENCE_OMITTED in check_answer(
            answer,
            tool_results=[row],
            question="Show my top 5 courses by impact.",
            required_tools={"my_progress"},
        )


def test_renderer_fails_closed_when_the_projected_slice_is_not_the_canonical_prefix() -> None:
    row = _progress_row()
    row["requested_unlock_impact_ranking"] = list(reversed(row["requested_unlock_impact_ranking"]))

    assert _safe_progress_fact_fragment("English", row) == ""


def test_renderer_discloses_when_fewer_than_n_courses_exist_without_padding() -> None:
    row = _progress_row()
    row["unlock_impact_ranking"] = row["unlock_impact_ranking"][:3]
    row["requested_unlock_impact_ranking"] = row["unlock_impact_ranking"]
    row["requested_priority_limit_fulfilled"] = False
    row["prerequisites_satisfied"] = row["unlock_impact_ranking"]
    row["counts"]["open"] = 3

    answer = _safe_progress_fact_fragment("English", row)

    assert "asked me to prioritize 5 courses" in answer
    assert "only 3 with recorded prerequisite readiness" in answer
    assert all(code in answer for code in ("CS301", "CS302", "CS303"))
    assert "CS304" not in answer
    assert (
        check_answer(
            answer,
            tool_results=[row],
            question="Show my top 5 courses by impact.",
            required_tools={"my_progress"},
        )
        == []
    )

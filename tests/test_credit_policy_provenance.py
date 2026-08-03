"""The credit-load figures must be citable, or they must not claim the guide said them.

A 24-case live batch found the adviser telling students «الحد الأدنى **حسب الدليل
الإرشادي** هو 12 ساعة» with no citation attached. The figure is correct and the
attribution is correct — the guide does say it, on page 23 — but it reached the
student through ``recommendation_policy`` rather than through ``policy_lookup``, so
the citation contract never saw it. A second regulatory channel, outside every check
built to govern the first.

Two ways to close that, and only two: bind the figures to the record that states
them, or stop attributing them to the guide. The record genuinely supports 12 and
19, so this binds them.
"""

from __future__ import annotations

import pytest

from core.services.credit_policy import (
    BACKING_POLICY_IDS,
    RECOMMENDED_MAX_CREDITS,
    REGULATORY_MAX_CREDITS,
    REGULATORY_MIN_CREDITS,
    backing_citations,
    credit_policy_evidence,
    verify_against_store,
)
from core.services.policy_store import get_policy_store
from core.services.virtual_advisor import (
    _credit_policy_evidence_citations,
    _find_credit_block,
    _retrieved_citations,
)

pytestmark = pytest.mark.django_db


def _main_term_evidence(status: str | None = None):
    return credit_policy_evidence(12, [], term=1, student_status=status)


# ── the constants and the record cannot drift apart ──────────────


def test_the_constants_match_the_record_they_claim_to_come_from():
    """Two copies of the same numbers. A silent drift would cite page 23 for a figure
    page 23 does not contain, and every mechanical check would still pass."""
    assert verify_against_store() == []


def test_the_backing_record_actually_states_the_range():
    record = get_policy_store().by_id[BACKING_POLICY_IDS["semester_range"]]
    assert record["min_value"] == REGULATORY_MIN_CREDITS
    assert record["max_value"] == REGULATORY_MAX_CREDITS
    assert "12" in record["source_text_ar"] and "19" in record["source_text_ar"]


def test_drift_is_reported_rather_than_cited(monkeypatch):
    monkeypatch.setattr("core.services.credit_policy.REGULATORY_MAX_CREDITS", 21)
    problems = verify_against_store()
    assert problems and any("max" in p for p in problems)


def test_a_drifted_constant_withholds_the_citation_rather_than_attaching_a_wrong_one(
    monkeypatch,
):
    monkeypatch.setattr("core.services.credit_policy.REGULATORY_MAX_CREDITS", 21)
    found = _credit_policy_evidence_citations(
        {"context": {"recommendation_policy": _main_term_evidence()}}
    )
    assert found is None


# ── which figures get a citation, and which must never ───────────


def test_the_regulatory_range_is_backed_by_a_real_record():
    assert backing_citations(_main_term_evidence()) == [BACKING_POLICY_IDS["semester_range"]]


def test_the_expected_graduate_clause_adds_its_own_record():
    ids = backing_citations(_main_term_evidence("GRADUATION EXPECTED"))
    assert BACKING_POLICY_IDS["expected_graduate"] in ids


def test_the_advisory_cap_is_never_given_a_citation():
    """18 is this system's own number. No page of the guide says it.

    Attaching a citation would be the same defect pointed the other way: lending
    institutional authority to a figure we chose ourselves.
    """
    evidence = _main_term_evidence()
    assert evidence["max_recommended_credit_hours"] == RECOMMENDED_MAX_CREDITS
    for policy_id in backing_citations(evidence):
        record = get_policy_store().by_id[policy_id]
        assert RECOMMENDED_MAX_CREDITS not in {record.get("min_value"), record.get("max_value")}


def test_a_term_with_no_regulatory_range_cites_nothing():
    """The summer block omits the range entirely, so there is nothing to back."""
    assert backing_citations(credit_policy_evidence(9, [], term=3)) == []
    assert (
        _credit_policy_evidence_citations(
            {"context": {"recommendation_policy": credit_policy_evidence(9, [], term=3)}}
        )
        is None
    )


# ── the block must be found wherever the request carries it ──────


def test_the_block_is_found_in_the_fallback_context():
    assert _find_credit_block({"recommendation_policy": _main_term_evidence()}) is not None


def test_the_block_is_found_nested_inside_a_tool_result_list():
    """Tool results arrive as a LIST.

    The first version of this recursed through dict values only, so it found the
    block on the fallback path and never on the agent path — which is the path that
    produced the defect. The fix looked correct and did nothing.
    """
    payload = {
        "context": {"mode": "student"},
        "tools": [
            {"tool": "my_timetable", "ok": True},
            {
                "tool": "get_student_context",
                "ok": True,
                "student_context": {"recommendation_policy": _main_term_evidence()},
            },
        ],
    }
    assert _find_credit_block(payload) is not None


def test_a_request_carrying_no_credit_block_yields_no_citations():
    assert _find_credit_block({"context": {"mode": "student"}, "tools": []}) is None
    assert _credit_policy_evidence_citations({"context": {}, "tools": []}) is None


def test_recursion_is_bounded():
    deep: dict = {}
    node = deep
    for _ in range(40):
        node["next"] = {}
        node = node["next"]
    node["max_recommended_credit_hours"] = 18
    assert _find_credit_block(deep) is None


# ── and it reaches the citation contract ─────────────────────────


def test_the_backing_records_become_citable_for_the_request():
    payload = {
        "context": {"mode": "student"},
        "tools": [
            {
                "tool": "get_student_context",
                "ok": True,
                "student_context": {"recommendation_policy": _main_term_evidence()},
            }
        ],
    }
    result = _credit_policy_evidence_citations(payload)
    assert result["tool"] == "policy_lookup", "must look like any other retrieved policy"
    cited = [c["policy_id"] for c in _retrieved_citations([result])]
    assert BACKING_POLICY_IDS["semester_range"] in cited


def test_the_citation_carries_the_page_a_student_can_turn_to():
    result = _credit_policy_evidence_citations(
        {"context": {"recommendation_policy": _main_term_evidence()}}
    )
    citation = next(
        c for c in result["citable"] if c["policy_id"] == BACKING_POLICY_IDS["semester_range"]
    )
    assert citation["page"] == 23


def test_only_approved_records_can_back_a_figure():
    """The store filters on approval; this pins that the credit path inherits it."""
    result = _credit_policy_evidence_citations(
        {"context": {"recommendation_policy": _main_term_evidence()}}
    )
    for policy in result["policies"]:
        assert policy["authority"]["approval_status"] == "AUTHORITY_APPROVED"


# ── end to end: the request's citable set actually gains them ────


def test_a_real_request_can_cite_the_credit_range():
    """The helper returning the right thing is not the same as the loop using it.

    A mutation that computes the citations and then drops them on the floor left
    every unit test above green — the gap was between "derived correctly" and
    "reached the answer contract".
    """
    from core.services.rbac import ROLE_STUDENT
    from core.services.virtual_advisor import answer_virtual_advisor
    from tests.test_policy_grounding_paths import _NoToolsClient

    result = answer_virtual_advisor(
        question="كم الحد الأدنى للساعات؟",
        scope={"role": ROLE_STUDENT, "student_id": 6001001},
        client=_NoToolsClient(),
    )
    cited = {c["policy_id"] for c in result["citations"]}
    assert BACKING_POLICY_IDS["semester_range"] in cited


def test_the_credit_range_is_citable_without_a_fabrication_flag():
    """And an answer that cites it correctly must pass the citation check."""
    from core.services.rbac import ROLE_STUDENT
    from core.services.virtual_advisor import _bad_citations, answer_virtual_advisor
    from tests.test_policy_grounding_paths import _NoToolsClient

    answer = "الحد الأدنى «الدليل الإرشادي للطالب، ص 23 [TU.LOAD.SEMESTER_RANGE]» هو 12 ساعة."
    result = answer_virtual_advisor(
        question="كم الحد الأدنى للساعات؟",
        scope={"role": ROLE_STUDENT, "student_id": 6001001},
        client=_NoToolsClient(answer=answer),
    )
    assert _bad_citations(answer, result["citations"]) == []
    assert result["agent"].get("citation_refused") is not True

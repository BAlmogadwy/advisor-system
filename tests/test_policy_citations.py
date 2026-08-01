"""Runtime enforcement of the citation contract in the agent loop.

The prompt tells the model to cite only what it retrieved. These tests cover the
part that does not depend on the model complying.
"""

from __future__ import annotations

from core.services.virtual_advisor import (
    _fabricated_policy_ids,
    _policy_ids_in_text,
    _retrieved_citations,
)


def _lookup_result(*policy_ids, tool="policy_lookup"):
    return {
        "ok": True,
        "tool": tool,
        "citable": [
            {
                "policy_id": pid,
                "document_id": "TU_STUDENT_GUIDE_V3_1447",
                "document_title": "الدليل الإرشادي للطالب والطالبة",
                "edition": "1447",
                "page": 24,
                "effective_from": None,
                "effective_to": None,
            }
            for pid in policy_ids
        ],
    }


# ── extracting what the answer cited ─────────────────────────────


def test_policy_ids_are_found_in_arabic_prose():
    answer = "حسب اللائحة (TU.WITHDRAWAL.MAXIMUM، ص 24) لا يزيد عدد مرات الانسحاب عن أربع."
    assert _policy_ids_in_text(answer) == {"TU.WITHDRAWAL.MAXIMUM"}


def test_ordinary_prose_and_course_codes_are_not_mistaken_for_policy_ids():
    """The detector must not fire on innocent text, or every answer needs a retry."""
    for text in (
        "سجل CS111 و MATH101 هذا الفصل.",
        "Your GPA is 3.2 and you have 12 credits left.",
        "راجع عمادة القبول والتسجيل.",
        "The file is at core/services/policy_store.py",
    ):
        assert _policy_ids_in_text(text) == set(), text


# ── collecting what the request was entitled to cite ─────────────


def test_citations_collected_from_policy_lookup_results():
    results = [_lookup_result("TU.WITHDRAWAL.MAXIMUM", "TU.WITHDRAWAL.PROCEDURE")]
    citations = _retrieved_citations(results)
    assert [c["policy_id"] for c in citations] == [
        "TU.WITHDRAWAL.MAXIMUM",
        "TU.WITHDRAWAL.PROCEDURE",
    ]
    assert citations[0]["page"] == 24


def test_results_from_other_tools_contribute_no_citations():
    results = [
        {"ok": True, "tool": "my_progress", "citable": [{"policy_id": "TU.FAKE.FROM_DATA_TOOL"}]},
        _lookup_result("TU.WITHDRAWAL.MAXIMUM"),
    ]
    assert [c["policy_id"] for c in _retrieved_citations(results)] == ["TU.WITHDRAWAL.MAXIMUM"]


def test_repeated_lookups_do_not_duplicate_citations():
    results = [_lookup_result("TU.WITHDRAWAL.MAXIMUM"), _lookup_result("TU.WITHDRAWAL.MAXIMUM")]
    assert len(_retrieved_citations(results)) == 1


def test_failed_policy_lookup_yields_no_citations():
    denied = {"tool": "policy_lookup", "ok": False, "error": "not allowed for your role"}
    assert _retrieved_citations([denied]) == []


# ── rejecting what it was not ────────────────────────────────────


def test_citing_a_policy_that_was_never_retrieved_is_fabrication():
    citations = _retrieved_citations([_lookup_result("TU.WITHDRAWAL.MAXIMUM")])
    answer = "القاعدة هي TU.DEFERRAL.MAXIMUM وتسمح بفصلين."
    assert _fabricated_policy_ids(answer, citations) == ["TU.DEFERRAL.MAXIMUM"]


def test_citing_only_retrieved_policies_passes():
    citations = _retrieved_citations([_lookup_result("TU.WITHDRAWAL.MAXIMUM")])
    answer = "حسب TU.WITHDRAWAL.MAXIMUM لا يزيد العدد عن أربع مرات."
    assert _fabricated_policy_ids(answer, citations) == []


def test_any_citation_is_fabrication_when_no_lookup_was_made():
    """The failure this exists to catch: answering a rule question without asking."""
    answer = "حسب TU.WITHDRAWAL.MAXIMUM يمكنك الانسحاب أربع مرات."
    assert _fabricated_policy_ids(answer, []) == ["TU.WITHDRAWAL.MAXIMUM"]


def test_an_answer_with_no_citations_is_not_flagged():
    citations = _retrieved_citations([_lookup_result("TU.WITHDRAWAL.MAXIMUM")])
    assert _fabricated_policy_ids("لا يوجد نظام مكتوب لدينا حول هذا.", citations) == []

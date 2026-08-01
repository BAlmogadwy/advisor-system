"""Runtime enforcement of the citation contract in the agent loop.

The prompt tells the model to cite only what it retrieved. These tests cover the
part that does not depend on the model complying.
"""

from __future__ import annotations

import pytest

from core.services.policy_store import get_policy_store
from core.services.virtual_advisor import (
    _bad_citations,
    _claimed_citations,
    _fabricated_policy_ids,
    _policy_ids_in_text,
    _retrieved_citations,
    _uncheckable_pages,
)

pytestmark = pytest.mark.django_db


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


# ── the enforcement the review found was a no-op ─────────────────
#
# The prompt told the model to write «الدليل الإرشادي للطالب، الإصدار الثالث، ص NN»
# — document, edition, page, and no policy id. The only check scanned for dotted
# upper-case ids. The two never intersected: an answer that FOLLOWED the instruction
# contained no id, so nothing was ever checked, the retry never fired, and
# cited_policy_ids was always empty. The page — the one thing a student can verify
# against the printed guide — was validated nowhere.


@pytest.fixture
def allowed():
    store = get_policy_store()
    citable = store.lookup(policy_ids=["TU.WITHDRAWAL.MAXIMUM"])["citable"]
    assert citable and citable[0]["page"] == 24, "fixture assumption about the record"
    return _retrieved_citations([{"tool": "policy_lookup", "ok": True, "citable": citable}])


def _reasons(answer, allowed):
    return sorted(b["reason"] for b in _bad_citations(answer, allowed))


def test_the_mandated_citation_form_is_parsed(allowed):
    claims = _claimed_citations("«الدليل الإرشادي للطالب، ص 24 [TU.WITHDRAWAL.MAXIMUM]»")
    assert claims == [{"policy_id": "TU.WITHDRAWAL.MAXIMUM", "page": 24}]


def test_arabic_indic_page_digits_are_understood(allowed):
    assert _reasons("«الدليل الإرشادي للطالب، ص ٢٤ [TU.WITHDRAWAL.MAXIMUM]»", allowed) == []


def test_a_correct_citation_passes(allowed):
    answer = "لا يزيد عن خمس مرات. «الدليل الإرشادي للطالب، ص 24 [TU.WITHDRAWAL.MAXIMUM]»"
    assert _reasons(answer, allowed) == []


def test_the_original_blocker_a_bare_invented_page_is_now_caught(allowed):
    """The exact answer shape that used to sail through untouched."""
    answer = "لا يزيد عن ثماني مرات. «الدليل الإرشادي للطالب، الإصدار الثالث، ص 97»"
    assert _reasons(answer, allowed) == ["PAGE_NOT_IN_ANY_RETRIEVED_POLICY"]


def test_a_page_belonging_to_a_different_policy_is_caught(allowed):
    assert _reasons("«الدليل الإرشادي للطالب، ص 3 [TU.WITHDRAWAL.MAXIMUM]»", allowed) == [
        "PAGE_NOT_IN_RECORD"
    ]


def test_an_invented_policy_id_is_caught(allowed):
    assert _reasons("«ص 24 [TU.WITHDRAWAL.UNLIMITED]»", allowed) == ["UNKNOWN_POLICY"]


def test_a_real_policy_that_was_never_retrieved_is_caught(allowed):
    assert _reasons("«ص 25 [TU.DISMISSAL.THREE_WARNINGS]»", allowed) == [
        "NOT_RETRIEVED_THIS_REQUEST"
    ]


def test_an_answer_that_cites_nothing_is_not_flagged(allowed):
    assert _reasons("لا يوجد لدينا نظام مكتوب حول هذا الأمر.", allowed) == []


def test_a_page_regex_that_backtracks_would_flag_every_compliant_answer(allowed):
    """«ص 24 [ID]» must not read as page 2 followed by '4'.

    Without forcing a maximal digit match the negative lookahead is satisfied by
    backtracking, so the bare-page rule fires on exactly the answers that complied.
    """
    assert _uncheckable_pages("ص 24 [TU.WITHDRAWAL.MAXIMUM]", allowed) == []
    assert _uncheckable_pages("ص 97", allowed) == [97]


# ── the retry and refusal paths, end to end ──────────────────────
#
# These run the real answer_virtual_advisor against a scripted client, because the
# defects here were not in a helper — they were in what the loop DOES with the
# helper's verdict: it re-asked with the evidence stripped out, overwrote the draft
# unconditionally, and shipped answers it had already determined were fabricated.

from core.services.rbac import ROLE_STUDENT  # noqa: E402
from core.services.virtual_advisor import answer_virtual_advisor  # noqa: E402
from tests.test_virtual_advisor_agent_loop import (  # noqa: E402
    FakeToolClient,
    _tool_call,
    _tool_turn,
)

_LOOKUP = _tool_call("policy_lookup", {"query": "كم مرة أقدر أنسحب من مقرر؟"})
_GOOD = "«الدليل الإرشادي للطالب، ص 24 [TU.WITHDRAWAL.MAXIMUM]»"


def _run(client, question="كم مرة أقدر أنسحب من مقرر؟"):
    return answer_virtual_advisor(question=question, scope={"role": ROLE_STUDENT}, client=client)


def test_a_clean_citation_survives_untouched():
    fake = FakeToolClient(
        turns=[_tool_turn(tool_calls=(_LOOKUP,)), _tool_turn(content=f"خمس مرات. {_GOOD}")]
    )
    result = _run(fake)
    assert result["answer"] == f"خمس مرات. {_GOOD}"
    assert result["agent"].get("citation_retry") is not True
    assert result["agent"].get("citation_refused") is not True
    assert result["cited_policy_ids"] == ["TU.WITHDRAWAL.MAXIMUM"]


def test_the_correction_carries_the_policy_text_not_just_the_permitted_ids():
    """Rewriting a rule answer from a list of ids means rewriting it from memory."""
    fake = FakeToolClient(
        turns=[_tool_turn(tool_calls=(_LOOKUP,)), _tool_turn(content="ثماني مرات. ص 97")],
        plain_answers=[f"خمس مرات. {_GOOD}"],
    )
    _run(fake)
    correction = fake.chat_calls[0][-1]["content"]
    assert "TU.WITHDRAWAL.MAXIMUM" in correction
    # The record's own words must be in front of the model, not just its id.
    assert "الانسحاب" in correction
    assert "ص 24" in correction


def test_an_unfixable_answer_is_refused_rather_than_shipped():
    """The system knew the sourcing was wrong and returned it anyway.

    An invented rule wearing a real-looking citation is the most dangerous output
    this system can produce, and ok:true made it indistinguishable from a good one.
    """
    fake = FakeToolClient(
        turns=[_tool_turn(tool_calls=(_LOOKUP,)), _tool_turn(content="ثماني مرات. ص 97")],
        plain_answers=["ما زالت ثماني مرات. ص 98"],
    )
    result = _run(fake)
    assert result["agent"]["citation_refused"] is True
    assert "97" not in result["answer"] and "98" not in result["answer"]
    assert "عمادة القبول والتسجيل" in result["answer"]


def test_a_retry_that_invents_more_does_not_replace_the_better_draft():
    """The overwrite was unconditional, so a worse retry displaced a better draft."""
    fake = FakeToolClient(
        turns=[
            _tool_turn(tool_calls=(_LOOKUP,)),
            _tool_turn(content=f"خمس مرات. {_GOOD} وأيضاً ص 97"),
        ],
        plain_answers=["ص 97 و ص 98 و ص 99"],
    )
    result = _run(fake)
    agent = result["agent"]
    assert len(agent["bad_citations_after_retry"]) > len(agent["bad_citations"])
    assert agent["citation_retry_kept"] == "draft"


def test_a_retry_that_fixes_the_citation_is_taken():
    fake = FakeToolClient(
        turns=[_tool_turn(tool_calls=(_LOOKUP,)), _tool_turn(content="ثماني مرات. ص 97")],
        plain_answers=[f"خمس مرات. {_GOOD}"],
    )
    result = _run(fake)
    assert result["agent"]["citation_retry_kept"] == "retry"
    assert result["agent"].get("citation_refused") is not True
    assert result["answer"] == f"خمس مرات. {_GOOD}"


def test_citations_are_reported_even_when_the_answer_cites_none():
    fake = FakeToolClient(
        turns=[_tool_turn(tool_calls=(_LOOKUP,)), _tool_turn(content="لا يوجد نظام مكتوب.")]
    )
    result = _run(fake)
    assert result["citations"], "what the answer was ENTITLED to cite"
    assert result["cited_policy_ids"] == [], "what it actually cited"

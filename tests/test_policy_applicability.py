"""Which retrieved policy may ground a claim, and which merely exists.

Every case here is a real question. The positive/contrast pairs exist because a
discriminator that only ever fires proves nothing: «داخل الجامعة» must pull the
internal-transfer records IN and «من جامعة ثانية» must keep them OUT, or the alias
is just a second way of matching everything.
"""

from __future__ import annotations

import pytest

from core.services.policy_applicability import (
    BACKGROUND_ONLY,
    CONFLICTING,
    DIRECT_SUPPORT,
    NORMATIVE_CLAIM_TYPES,
    classify,
    get_applicability_index,
    validate_claims,
)
from core.services.policy_store import (
    alias_matches,
    expand_tokens_ordered,
    get_policy_store,
    raw_words_ordered,
)

pytestmark = pytest.mark.django_db


def _roles(question: str, limit: int = 8):
    store = get_policy_store()
    result = store.lookup(query=question, limit=limit)
    return classify(
        result["policies"], question=question, topics=result["matched_topics"], store=store
    )


def _direct(question: str) -> set[str]:
    return {p["policy_id"] for p in _roles(question)["direct_policy_evidence"]}


# ── ordered alias matching ───────────────────────────────────────


def test_alias_words_must_appear_in_order():
    """A word bag cannot tell a transfer TO another university from a transfer
    BETWEEN colleges inside one.

    «أحول لجامعة ثانية» matched «أحول لكلية ثانية داخل الجامعة» because every alias
    word was present — but جامعة came from a different phrase, and after ثانية
    rather than before it. The question meant the opposite of what it matched.
    """
    alias = expand_tokens_ordered("أحول لجامعة ثانية")
    internal = expand_tokens_ordered("أقدر أحول لكلية ثانية داخل الجامعة؟")
    external = expand_tokens_ordered("أقدر أحول لجامعة ثانية؟")
    assert alias_matches(alias, external)
    assert not alias_matches(alias, internal)


def test_an_alias_need_not_be_contiguous():
    """Arabic inserts particles freely; order is the constraint, not adjacency."""
    alias = expand_tokens_ordered("الانسحاب من مقرر")
    question = expand_tokens_ordered("هل أقدر أطلب الانسحاب من هذا المقرر قبل الاختبار؟")
    assert alias_matches(alias, question)


def test_an_empty_alias_matches_nothing():
    assert not alias_matches([], expand_tokens_ordered("أي سؤال"))


# ── q77: transfer direction ──────────────────────────────────────


def test_internal_transfer_question_reaches_the_internal_records():
    direct = _direct("أقدر أحول لكلية ثانية داخل الجامعة؟")
    assert "TU.TRANSFER.INTERNAL_COLLEGE" in direct
    assert "TU.TRANSFER.EXTERNAL" not in direct


def test_external_transfer_question_does_not_reach_the_internal_records():
    """The contrast. Without it the internal alias could match everything."""
    direct = _direct("أقدر أحول من جامعة ثانية إلى جامعة طيبة؟")
    assert "TU.TRANSFER.EXTERNAL" in direct
    assert "TU.TRANSFER.INTERNAL_COLLEGE" not in direct


# ── q240: a question that spans two concepts ─────────────────────


def test_a_duration_dismissal_question_resolves_to_both_concepts():
    """«يفصلوني على المدة» is a dismissal question AND a duration question.

    Forcing one concept would deny direct status to whichever record lost. Both
    resolve, and each is direct for the part it entails.
    """
    roles = _roles("خطتي ثمانية فصول. كم فصل أقدر أقعد قبل ما يفصلوني على المدة؟")
    assert set(roles["question_concepts"]) == {"programme_maximum_duration", "university_dismissal"}
    direct = {p["policy_id"] for p in roles["direct_policy_evidence"]}
    assert "TU.DISMISSAL.DURATION_EXCEEDED" in direct


def test_the_repetition_question_still_gets_no_duration_record():
    """The contrast that matters most: the concepts must NOT be broadly compatible.

    Making programme_maximum_duration generally usable for dismissal questions
    would reopen q165, where a maximum-duration rule supplied a repetition
    percentage. Two concepts both resolving for ONE question is not the same as
    the concepts being interchangeable.
    """
    direct = _direct("كم مرة مسموح أعيد نفس المادة؟")
    assert "TU.DISMISSAL.DURATION_EXCEEDED" not in direct
    assert direct == set(), "the store governs no course-repetition limit"


# ── q271: documents vs the rest of graduation ────────────────────


def test_a_document_distribution_question_reaches_the_documents_record():
    assert "TU.GRADUATION.DOCUMENTS" in _direct("متى توزيع وثائق التخرج؟")


def test_a_ceremony_question_does_not_resolve_to_document_distribution():
    """The contrast: not every graduation question is about documents."""
    roles = _roles("متى حفل التخرج؟")
    assert "academic_documents" not in roles["question_concepts"]
    assert "TU.GRADUATION.DOCUMENTS" not in {
        p["policy_id"] for p in roles["direct_policy_evidence"]
    }


# ── the roles themselves ─────────────────────────────────────────


def test_a_superseded_record_is_never_direct():
    roles = _roles("متى يقفل الحذف والإضافة؟")
    conflicting = {p["policy_id"] for p in roles["conflicting_policy_evidence"]}
    assert "TU.REG.CHANGES_CLOSE_ONE_WEEK_BEFORE" in conflicting
    assert "TU.REG.CHANGES_CLOSE_ONE_WEEK_BEFORE" not in {
        p["policy_id"] for p in roles["direct_policy_evidence"]
    }


def test_the_default_role_is_background_not_direct():
    """Failing to classify a record is not a reason to trust it."""
    store = get_policy_store()
    roles = classify(
        [{"policy_id": "TU.NOT.IN.SCOPE", "topic": "x"}],
        question="سؤال",
        topics=[],
        store=store,
    )
    assert roles["direct_policy_evidence"] == []
    assert roles["background_policy_evidence"][0]["role"] == BACKGROUND_ONLY


def test_every_returned_policy_carries_a_role_and_a_reason():
    for policy in sum(
        (
            _roles("كم مرة أقدر أنسحب من مقرر؟")[k]
            for k in (
                "direct_policy_evidence",
                "background_policy_evidence",
                "conflicting_policy_evidence",
                "irrelevant_policy_evidence",
            )
        ),
        [],
    ):
        assert policy["role"] in {DIRECT_SUPPORT, BACKGROUND_ONLY, CONFLICTING, "IRRELEVANT"}
        assert policy["role_reason"]


# ── the normative boundary ───────────────────────────────────────


def test_the_normative_set_covers_more_than_numbers():
    """My first draft listed three types and would have let q196 through: that
    failure defined الحرمان and named an appeal route, neither of them a number."""
    assert {"REGULATORY_DEFINITION", "APPEAL_ROUTE", "RESPONSIBLE_AUTHORITY", "PROHIBITION"} <= (
        NORMATIVE_CLAIM_TYPES
    )


def test_a_normative_claim_on_background_evidence_is_rejected():
    roles = _roles("كم مرة مسموح أعيد نفس المادة؟")
    background = roles["background_policy_evidence"]
    assert background, "fixture assumption: this question retrieves related records"
    verdict = validate_claims(
        [
            {
                "claim": "يحق للطالب إعادة ما لا يزيد عن 10% من الساعات",
                "claim_type": "NUMERICAL_LIMIT",
                "supporting_policy_ids": [background[0]["policy_id"]],
            }
        ],
        roles,
    )
    assert not verdict["ok"]
    assert verdict["rejected"][0]["rejection"] == "NORMATIVE_CLAIM_WITHOUT_DIRECT_SUPPORT"


def test_a_normative_claim_with_no_support_at_all_is_rejected():
    roles = _roles("كم مرة مسموح أعيد نفس المادة؟")
    verdict = validate_claims(
        [{"claim": "خمس مرات", "claim_type": "NUMERICAL_LIMIT", "supporting_policy_ids": []}],
        roles,
    )
    assert verdict["rejected"][0]["rejection"] == "NORMATIVE_CLAIM_WITH_NO_SUPPORT"


def test_a_normative_claim_on_direct_evidence_is_accepted():
    roles = _roles("كم مرة أقدر أنسحب من مقرر؟")
    direct = roles["direct_policy_evidence"]
    assert direct
    verdict = validate_claims(
        [
            {
                "claim": "خمسة انسحابات",
                "claim_type": "NUMERICAL_LIMIT",
                "supporting_policy_ids": [direct[0]["policy_id"]],
            }
        ],
        roles,
    )
    assert verdict["ok"]
    assert verdict["accepted"][0]["support_roles"] == [DIRECT_SUPPORT]


def test_a_contextual_note_may_rest_on_background():
    """Background supports exactly one thing: that related material exists."""
    roles = _roles("كم مرة مسموح أعيد نفس المادة؟")
    verdict = validate_claims(
        [
            {
                "claim": "توجد سياسات مرتبطة لكنها لا تجيب عن السؤال",
                "claim_type": "CONTEXTUAL_NOTE",
                "supporting_policy_ids": [roles["background_policy_evidence"][0]["policy_id"]],
            }
        ],
        roles,
    )
    assert verdict["ok"]


# ── citations follow the roles ───────────────────────────────────


def test_only_direct_evidence_is_offered_as_citable():
    """A background record shown beside the answer reads as authority for the
    question whatever the prose says."""
    from core.services.virtual_advisor_capabilities import ROLE_STUDENT, build_default_registry

    result = build_default_registry().execute(
        "policy_lookup", {"query": "كم مرة مسموح أعيد نفس المادة؟"}, scope={"role": ROLE_STUDENT}
    )
    assert result["policies"], "records are retrieved"
    assert result["direct_policy_evidence"] == []
    assert result["citable"] == [], "nothing governing, so nothing citable"


def test_a_governed_question_still_offers_citations():
    from core.services.virtual_advisor_capabilities import ROLE_STUDENT, build_default_registry

    result = build_default_registry().execute(
        "policy_lookup", {"query": "كم مرة أقدر أنسحب من مقرر؟"}, scope={"role": ROLE_STUDENT}
    )
    citable = {c["policy_id"] for c in result["citable"]}
    direct = {p["policy_id"] for p in result["direct_policy_evidence"]}
    assert citable == direct and citable


def test_the_index_reloads_when_asked():
    assert get_applicability_index(refresh=True).scope


# ── Arabic surface forms the matcher must survive ────────────────


def _topics(question: str) -> set[str]:
    return {t for t, _ in get_policy_store().resolve_topics(question)}


@pytest.mark.parametrize(
    "question",
    [
        "أقدر أحول لكلية ثانية داخل الجامعة؟",
        "أقدر أحول لكلية ثانية بالجامعة؟",  # prefix attached to the noun
        "أقدر أُحوِّل لكلية ثانية داخل الجامعة؟",  # diacritics
        "أحول لكلية ثانية جديدة داخل نفس الجامعة؟",  # inserted modifiers
    ],
)
def test_the_internal_transfer_phrase_survives_arabic_surface_variation(question):
    assert "internal_transfer" in _topics(question)


def test_the_same_tokens_in_reverse_order_do_not_match():
    assert "internal_transfer" not in _topics("الجامعة داخل ثانية لكلية أحول")


# ── negation ─────────────────────────────────────────────────────
#
# Ordered matching proves a phrase is PRESENT. It cannot prove the student
# asserted it, and «أبي أحول، بس مو داخل الجامعة» contains the internal-transfer
# phrase while meaning its opposite.


@pytest.mark.parametrize(
    "question",
    [
        "أبي أحول، بس مو داخل الجامعة",
        "ما أبغى تحويل داخلي، أبغى خارجي",
        "التحويل مش داخل الجامعة",
    ],
)
def test_a_negated_phrase_does_not_resolve(question):
    assert "internal_transfer" not in _topics(question)


@pytest.mark.parametrize(
    "question",
    [
        "ما هو التحويل الداخلي؟",
        "ما هي شروط التحويل الداخلي؟",
        "ما الفرق بين التحويل الداخلي والخارجي؟",
    ],
)
def test_interrogative_ma_is_not_read_as_negation(question):
    """«ما» negates a verb and interrogates a noun phrase.

    Treating every «ما» as a denial would suppress every "what is X" question in
    the set — a far larger class than the negations it would catch.
    """
    assert "internal_transfer" in _topics(question)


def test_negation_scope_does_not_reach_across_a_clause():
    """Three tokens: enough for «بس مو داخل الجامعة», not enough to let an
    unrelated earlier negation suppress a later positive phrase."""
    question = "ما عندي مشكلة أبداً بالخطة، وأبغى أحول لكلية ثانية داخل الجامعة"
    assert "internal_transfer" in _topics(question)


def test_raw_words_keep_the_negators_the_matcher_drops():
    """ما and لا are STOPWORDS, so the matching stream had already discarded
    exactly the words that carry the negation before anything could look at them.

    That is why the check needs its own raw stream rather than reusing the tokens
    the matcher already has.
    """
    from core.services.arabic_text import STOPWORDS

    assert "ما" in STOPWORDS and "لا" in STOPWORDS
    phrase = "ما أبغى تحويل داخلي"
    assert "ما" in raw_words_ordered(phrase)
    assert not any("ما" in variants for variants in expand_tokens_ordered(phrase))


def test_alias_matches_without_raw_words_skips_the_negation_check():
    """Callers that cannot supply the raw stream still get ordered matching."""
    alias = expand_tokens_ordered("داخل الجامعة")
    negated = "مو داخل الجامعة"
    assert alias_matches(alias, expand_tokens_ordered(negated))
    assert not alias_matches(alias, expand_tokens_ordered(negated), raw_words_ordered(negated))

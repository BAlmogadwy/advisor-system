"""The router is a table, so it is tested as one.

`classify_intent` reads a string and returns a family. No database, no policy
store lookup, no provider — which is the whole point: the 50-question batch that
motivated it had to be run live against Alibaba to learn what it routed, and a
routing decision that can only be measured that way cannot be regression-tested.

TWO KINDS OF ASSERTION, AND THE SECOND IS THE IMPORTANT ONE

Positive routings prove the families work. The negatives prove the thing the
owner actually asked for: that a wrong confident route is worse than no route.
`GENERAL_AGENT` costs one agent loop — today's behaviour — while a wrong family
sends a question to a surface that structurally cannot answer it. So «هل الشعب
فيها مقاعد متاحة؟» must fall through, «متى تفتح البوابة؟» must not become a
prerequisite question, and «لما أحفظ جدولًا كمفضل، هل يتغير تسجيلي؟» must not
become a request to save a preference.

The 50 batch questions are read from `evals/advisor/planner_priority_batch.yaml`
rather than copied here, so that editing the batch and forgetting this table is a
test failure rather than a silent divergence.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.services.advisor_intent import (
    IntentFamily,
    classify_intent,
    explicit_normative_claim_present,
)
from core.services.policy_contract import policy_intent

BATCH = Path(__file__).resolve().parents[1] / "evals" / "advisor" / "planner_priority_batch.yaml"


def _batch_questions() -> dict[str, str]:
    data = yaml.safe_load(BATCH.read_text(encoding="utf-8"))
    return {row["id"]: row["ar"] for row in data["questions"]}


QUESTIONS = _batch_questions()


# --------------------------------------------------------------------------
# The routings the owner specified, verbatim.
# --------------------------------------------------------------------------

OWNER_ROUTINGS: tuple[tuple[str, IntentFamily], ...] = (
    ("ابنِ لي جدول", IntentFamily.PLANNER_BUILD),
    ("سو لي أكثر من خيار", IntentFamily.PLANNER_VIEW_ALTERNATIVES),
    ("أكثر من خيار للجدول", IntentFamily.PLANNER_VIEW_ALTERNATIVES),
    ("تجاهل جدولي الحالي", IntentFamily.PLANNER_REBUILD),
    ("من الصفر", IntentFamily.PLANNER_REBUILD),
    ("احفظ الخيار الثاني", IntentFamily.PLANNER_SELECT_PREFERRED),
    ("كجدولي المفضل", IntentFamily.PLANNER_SELECT_PREFERRED),
    ("وش جدولي؟", IntentFamily.CURRENT_TIMETABLE),
    ("اعرض لي جدولي المسجل", IntentFamily.CURRENT_TIMETABLE),
    ("وش يفتح AI331؟", IntentFamily.COURSE_UNLOCKS),
    ("كم مقرر ينتظر AI331", IntentFamily.COURSE_UNLOCKS),
    ("ليش AI491 مقفل؟", IntentFamily.COURSE_LOCK_REASON),
    ("وش أهم مقرر أسجله؟", IntentFamily.COURSE_PRIORITY),
    ("هل يسمح لي بتسجيل مقرر من مستوى أعلى؟", IntentFamily.POLICY),
    ("كم الحد الأعلى للساعات", IntentFamily.POLICY),
)


@pytest.mark.parametrize(("question", "expected"), OWNER_ROUTINGS)
def test_the_owner_specified_routings(question: str, expected: IntentFamily) -> None:
    assert classify_intent(question) is expected


# --------------------------------------------------------------------------
# The same families in English. Arabic attaches the article and the possessive
# to the word and English does not, so neither language proves the other.
# --------------------------------------------------------------------------

ENGLISH_ROUTINGS: tuple[tuple[str, IntentFamily], ...] = (
    ("Build me a timetable for next term", IntentFamily.PLANNER_BUILD),
    ("Give me more than one option for my schedule", IntentFamily.PLANNER_VIEW_ALTERNATIVES),
    ("Ignore my current sections and start from scratch", IntentFamily.PLANNER_REBUILD),
    ("Save the second option as my preferred schedule", IntentFamily.PLANNER_SELECT_PREFERRED),
    ("I edited the course list, rebuild the alternatives", IntentFamily.PLANNER_EDIT_DRAFT),
    ("Show my timetable", IntentFamily.CURRENT_TIMETABLE),
    ("What is my current schedule?", IntentFamily.CURRENT_TIMETABLE),
    ("Which sections of AI352 conflict with my schedule?", IntentFamily.TIMETABLE_CLASH),
    ("How many courses does AI331 unlock?", IntentFamily.COURSE_UNLOCKS),
    ("What does AI331 unlock?", IntentFamily.COURSE_UNLOCKS),
    ("Why is AI491 locked?", IntentFamily.COURSE_LOCK_REASON),
    ("Which course is my highest priority?", IntentFamily.COURSE_PRIORITY),
    ("Am I allowed to register 21 credit hours?", IntentFamily.POLICY),
    ("What is the maximum credit limit?", IntentFamily.POLICY),
    ("Build me a timetable, and am I allowed to take 21 hours?", IntentFamily.MIXED),
)


@pytest.mark.parametrize(("question", "expected"), ENGLISH_ROUTINGS)
def test_every_family_routes_in_english_too(question: str, expected: IntentFamily) -> None:
    assert classify_intent(question) is expected


def test_capitalisation_is_not_a_different_question() -> None:
    """`arabic_text.normalise` folds hamza and ta-marbuta; it does not lowercase.

    Every English marker in the module is written lower-case, so without the fold
    in `_fold` the entire English half of the router answers only to lower-case
    input — and a student typing a sentence normally starts it with a capital.
    """
    assert classify_intent("BUILD ME A TIMETABLE") is IntentFamily.PLANNER_BUILD
    assert classify_intent("Why Is AI491 Locked?") is IntentFamily.COURSE_LOCK_REASON


# --------------------------------------------------------------------------
# The negatives. Every one of these is a question a family could plausibly
# have claimed, and every one is answered better by the agent loop.
# --------------------------------------------------------------------------

MUST_FALL_THROUGH: tuple[tuple[str, str], ...] = (
    ("", "an empty string is not a question"),
    ("     ", "whitespace is not a question"),
    ("شكرًا لك يا مرشدي", "gratitude is not a route"),
    ("وين مبنى كلية الحاسبات؟", "a campus question names no product surface"),
    (
        "متى تفتح البوابة؟",
        "«تفتح» is the unlock verb and «البوابة» is the portal — without a course "
        "noun or a course code this is a calendar question, not a prerequisite one",
    ),
    ("كم عدد الطلاب في القسم؟", "«كم» alone is not a formal-limit question"),
    ("مو أبغى جدول جديد", "an explicit negator in front of the marker suppresses the route"),
    (
        "اعتمد الخيار الأول وسجلني في الشعب الموجودة فيه",
        "adopting an option AND asking to be registered — the answer that matters "
        "is that the adviser registers nothing, so the planner must not claim it",
    ),
    (
        "لما أحفظ جدولًا كمفضل، هل يتغير تسجيلي الحالي في البوابة؟",
        "a question ABOUT the save-preference feature, not a request to use it",
    ),
    (
        "هل الشعب الموجودة في الجدول المقترح فيها مقاعد متاحة فعلًا؟",
        "the system holds no seat counts; every family would answer the wrong half",
    ),
    (
        "Does the lab room unlock at 448?",
        "'at 448' is not a course code — a code is closed up in this catalogue, "
        "and letting a space in makes 'at 448', 'in 141' and 'term 447' codes",
    ),
    (
        "وش المقرر المعتمد في خطتي الدراسية؟",
        "«المعتمد» is the APPROVED course, not a course waiting on a prerequisite",
    ),
)


#: TT10 USED to sit in MUST_FALL_THROUGH, on the reading that the imperative
#: «غيّر» makes it a build constraint rather than an edit. That reading was wrong,
#: and the reversal is recorded here rather than deleted: the sentence also contains
#: «الشعب التي اخترتها» — PAST tense — which asserts that a selection already
#: happened, so a draft exists. Left as an abstention the question reached
#: GENERAL_AGENT, which advertises `build_my_timetable`; that tool cannot see a draft
#: and would have answered with alternatives from the system's own course list,
#: presented as "based on the sections you chose". The deterministic EDIT_DRAFT route
#: already existed and simply never fired.
REVERSED_FROM_FALL_THROUGH: tuple[tuple[str, IntentFamily], ...] = (
    ("لا تغيّر الشعب التي اخترتها يدويًا، لكن غيّر باقي المقررات", IntentFamily.PLANNER_EDIT_DRAFT),
)


@pytest.mark.parametrize(("question", "expected"), REVERSED_FROM_FALL_THROUGH)
def test_a_past_tense_selection_is_an_edit_not_a_build(
    question: str, expected: IntentFamily
) -> None:
    assert classify_intent(question) is expected


@pytest.mark.parametrize(
    "question",
    [
        # The tense is the whole signal, so the tense is what gets tested. Each of
        # these puts a section noun in front of a choose-verb — the exact word order
        # the EDIT_DRAFT marker matches — and every one asks the adviser to help
        # choose, which is a build or a clash question, not an edit of a draft that
        # does not exist.
        "Which sections should I choose for AI352?",
        "Which sections do you recommend I choose?",
        "ما الشعب التي تنصحني أن أختارها؟",
        "اختر لي الشعب المناسبة وابنِ الجدول",
    ],
)
def test_asking_which_sections_to_choose_is_not_an_edit_of_a_draft(question: str) -> None:
    assert classify_intent(question) is not IntentFamily.PLANNER_EDIT_DRAFT


@pytest.mark.parametrize(("question", "why"), MUST_FALL_THROUGH)
def test_general_agent_is_the_default(question: str, why: str) -> None:
    assert classify_intent(question) is IntentFamily.GENERAL_AGENT, why


def test_none_is_a_question_shaped_hole_and_not_a_crash() -> None:
    """The router runs before validation on at least one call path."""
    assert classify_intent(None) is IntentFamily.GENERAL_AGENT  # type: ignore[arg-type]
    assert explicit_normative_claim_present(None) is False  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Precedence. Each of these questions matches TWO families in one domain, and
# picking the wrong one drops the half of the request that carries the risk.
# --------------------------------------------------------------------------


def test_rebuild_outranks_build_because_only_one_of_them_needs_confirming() -> None:
    """«ابنِ لي جدولًا جديدًا من الصفر» is a build AND a discard of the registration.

    Routed to PLANNER_BUILD it becomes an ordinary build and the confirmation the
    planner owes the student is never reached — which is the live failure
    `advisor_actions` was written for.
    """
    both = "ابنِ لي جدولًا جديدًا من الصفر وتجاهل كل الشعب المسجلة عندي."
    assert classify_intent(both) is IntentFamily.PLANNER_REBUILD
    assert classify_intent("ابنِ لي أفضل جدول للفصل القادم") is IntentFamily.PLANNER_BUILD


def test_keeping_the_current_sections_is_not_a_rebuild() -> None:
    """The inverse of the marker, in the same sentence shape.

    «خَلِّ الشعب المسجلة عندي مثل ما هي» names the current registration exactly as
    a rebuild request does, and asks for the opposite.
    """
    keep = "ابنِ لي أفضل جدول للفصل القادم وخَلِّ الشعب المسجلة عندي مثل ما هي."
    assert classify_intent(keep) is IntentFamily.PLANNER_BUILD


def test_an_edit_outranks_the_alternatives_it_asks_to_regenerate() -> None:
    both = "عدّلت قائمة المقررات؛ أعد بناء البدائل بناءً على التعديل الجديد."
    assert classify_intent(both) is IntentFamily.PLANNER_EDIT_DRAFT


def test_reading_the_registered_timetable_survives_a_subordinate_build_clause() -> None:
    """TT11 — «اعرض لي جدولي المسجل حاليًا قبل ما تبني أي بدائل».

    The sentence names a build verb and the word «بدائل», and it is a read. Both
    a show-verb-plus-option pattern and a bare build verb claimed it during
    development.
    """
    assert classify_intent(QUESTIONS["TT11"]) is IntentFamily.CURRENT_TIMETABLE


def test_priority_outranks_the_unlock_verb_in_its_subordinate_clause() -> None:
    """CP12 — the ranking is the question; «ما يفتحه» is the premise."""
    assert classify_intent(QUESTIONS["CP12"]) is IntentFamily.COURSE_PRIORITY


# --------------------------------------------------------------------------
# MIXED — only across domains.
# --------------------------------------------------------------------------


def test_a_build_carrying_a_permission_question_is_mixed() -> None:
    """Two demands, and collapsing to either one drops half the answer."""
    assert classify_intent("ابن لي جدول وهل يسمح لي بتسجيل 21 ساعة؟") is IntentFamily.MIXED


def test_two_planner_families_are_precedence_and_never_mixed() -> None:
    """A rebuild is a build; it is not two demands.

    MIXED is reserved for questions the pipeline has to serve twice. Letting
    same-domain overlaps produce it would make MIXED the commonest answer and
    the precedence ladder decorative.
    """
    assert classify_intent("ابنِ لي جدولًا من الصفر") is IntentFamily.PLANNER_REBUILD


def test_the_batch_question_that_needs_both_a_build_and_the_credit_rule() -> None:
    """TT08 is the only batch question whose curated label requires policy_lookup."""
    assert classify_intent(QUESTIONS["TT08"]) is IntentFamily.MIXED


# --------------------------------------------------------------------------
# Arabic surface forms the shared stemmer cannot reach on its own.
# --------------------------------------------------------------------------


def test_the_accusative_tanween_is_still_a_timetable() -> None:
    """«جدولًا» folds to "جدولا" and no suffix rule strips a bare alef.

    Six of the 50 batch questions are written this way. Without the alias-side
    widening they match nothing at all, and the failure is invisible because
    "no marker" and "no route" look identical downstream.
    """
    assert classify_intent("لا تبني لي جدولًا يتجاوز 15 ساعة.") is IntentFamily.PLANNER_BUILD
    assert classify_intent("ابنِ لي جدولًا") is IntentFamily.PLANNER_BUILD


def test_the_attached_lam_is_still_a_timetable() -> None:
    """«للجدول» strips ONE ل to «لجدول» and stops, so it never meets «جدول»."""
    assert classify_intent("أضف AI352 للجدول") is IntentFamily.PLANNER_BUILD


def test_the_attached_kaf_is_still_my_timetable() -> None:
    """ك is not in the stemmer's prefix set — only the three-letter «كال» is."""
    assert classify_intent("كجدولي المفضل") is IntentFamily.PLANNER_SELECT_PREFERRED


# --------------------------------------------------------------------------
# Negation. The window has to sit over the right words.
# --------------------------------------------------------------------------


def test_a_negator_in_front_of_the_marker_suppresses_the_route() -> None:
    """«مو أبغى جدول» wants no timetable built, and must not reach the planner."""
    assert classify_intent("أبغى جدول") is IntentFamily.PLANNER_BUILD
    assert classify_intent("مو أبغى جدول") is IntentFamily.GENERAL_AGENT


def test_a_negator_three_clauses_away_does_not_suppress_the_route() -> None:
    """TT05 — «ما عندي مقررات محددة، ابنِ الجدول من توصيات خطتي الدراسية».

    This is the case that proves the negation window is indexed correctly.
    `alias_matches` takes the match position from the stopword-FILTERED stream and
    indexes it into the UNFILTERED one, so handing it `raw_words_ordered` directly
    puts the window over «ما عندي مقررات» and reports this build request as
    negated. The build imperative is four words from the «ما», not one.
    """
    assert classify_intent(QUESTIONS["TT05"]) is IntentFamily.PLANNER_BUILD


def test_asking_which_sections_do_not_clash_is_still_a_clash_question() -> None:
    """TT14 — «التي ما تتعارض مع جدولي الحالي».

    «ما» negates a verb and interrogates a noun phrase, and here it does neither
    to the topic: the student wants the clash calculation either way.
    """
    assert classify_intent(QUESTIONS["TT14"]) is IntentFamily.TIMETABLE_CLASH


# --------------------------------------------------------------------------
# The 50 live-batch questions, as a routing table.
# --------------------------------------------------------------------------


#: Measured, not aspirational. 31 of 50 classify and 19 fall through; each
#: `GENERAL_AGENT` below is a question no family can serve without answering
#: something the student did not ask.
#: The routing expectations, READ from the canonical contract rather than restated.
#: There used to be three copies of this table — here, `expected_family` in the
#: executable batch, and `routing.expected_family` in the v1.0 file — and a test
#: whose whole job was to notice when they disagreed. A contract nobody can
#: contradict needs no such test.
#:
#: Only `exact` rows are a family equality. `one_of` is checked against its allowed
#: set, and `clarify` / `contextual` / `none` name no family at all: asserting one
#: for them would re-introduce the over-specification the audit was written to
#: remove.
def _contract_rows() -> list[dict]:
    path = (
        Path(__file__).resolve().parents[1] / "evals" / "advisor" / "planner_priority_eval_v1.yaml"
    )
    return yaml.safe_load(path.read_text(encoding="utf-8"))["cases"]


CONTRACT = {row["id"]: row for row in _contract_rows()}

BATCH_ROUTES: dict[str, IntentFamily] = {
    cid: IntentFamily(row["routing"]["expected_family"])
    for cid, row in CONTRACT.items()
    if row["routing"]["mode"] == "exact" and row["routing"]["expected_family"]
}


def test_the_contract_covers_the_batch_exactly() -> None:
    """A question added without a routing block is a failure, not a skip."""
    assert set(CONTRACT) == set(QUESTIONS)
    assert len(QUESTIONS) == 50
    for cid, row in CONTRACT.items():
        routing = row.get("routing")
        assert routing, f"{cid} carries no routing block"
        assert routing["mode"] in {"exact", "one_of", "clarify", "contextual", "none"}
        assert routing["domain"] in {
            "PLANNER_DATA",
            "TIMETABLE_DATA",
            "COURSE_DATA",
            "POLICY",
            "GENERAL",
        }, cid


@pytest.mark.parametrize(("qid", "expected"), sorted(BATCH_ROUTES.items()))
def test_the_fifty_live_batch_questions(qid: str, expected: IntentFamily) -> None:
    assert classify_intent(QUESTIONS[qid]) is expected


def test_no_batch_question_is_routed_outside_its_domain() -> None:
    """The measurement that justifies shipping this: 0 cross-domain errors.

    Every batch question is a planner/timetable or a prerequisite/priority
    question. A planner question routed to a course family (or the reverse) would
    call a tool that holds none of the requested data, which is the failure mode
    the abstention default exists to prevent.
    """
    planner = {
        IntentFamily.PLANNER_BUILD,
        IntentFamily.PLANNER_REBUILD,
        IntentFamily.PLANNER_VIEW_ALTERNATIVES,
        IntentFamily.PLANNER_SELECT_PREFERRED,
        IntentFamily.PLANNER_EDIT_DRAFT,
        IntentFamily.CURRENT_TIMETABLE,
        IntentFamily.TIMETABLE_CLASH,
    }
    course = {
        IntentFamily.COURSE_PRIORITY,
        IntentFamily.COURSE_UNLOCKS,
        IntentFamily.COURSE_LOCK_REASON,
    }
    neutral = {IntentFamily.GENERAL_AGENT, IntentFamily.MIXED}
    for qid, family in BATCH_ROUTES.items():
        if family in neutral:
            continue
        expected = planner if qid.startswith("TT") else course
        assert family in expected, f"{qid} routed to {family}"


def test_the_router_abstains_on_fourteen_of_the_fifty() -> None:
    """Pinned deliberately. Loosening a pattern to raise coverage moves this
    number, and the review that follows should be about which question stopped
    falling through and whether the family can actually answer it.

    It has moved twice, and both reviews are recorded here rather than in commit
    messages nobody re-reads at the next change.

    19 -> 18, commit 6A. CP11 «وش المقررات المقفلة عندي وما يفصلني عنها إلا مقرر
    واحد؟». COURSE_PRIORITY answers it outright — `my_progress` returns
    `counts.one_step`, which is that number exactly. Closed because abstention is not
    free downstream: the policy gate keys on the family, so GENERAL_AGENT kept a
    citation obligation the question could not discharge.

    18 -> 14, commit 6A.2. Four more, each classified a router defect by the audit in
    `docs/ADVISOR-ROUTING-AUDIT.md` before anything was changed:

        TT10  «الشعب التي اخترتها يدويًا»  a draft already exists; EDIT_DRAFT
        CP14  «ترتيب الأولوية»              names the ranking and no course
        CP16  «هل يظل مهمًا أكاديميًا؟»      where it sits among everything else
        CP19  «أكبر عدد من المقررات»        shares CP02's superlative-over-count

    CP19 was not on the fix list; it moved with CP02 because they share one marker,
    and it is kept because the audit had already allowed COURSE_PRIORITY for it. Its
    own risk is scope — an adviser-voice question in a student session — which is a
    different control from routing.

    Two cases that CHANGED family without changing this count are the point of the
    whole commit: CP02 and CP20 moved COURSE_UNLOCKS -> COURSE_PRIORITY. Both ask
    which course wins across the plan, and `why_course_locked` analyses one named
    course and cannot rank.
    """
    # Measured over the ROUTER against all 50, not over the expectation table:
    # `BATCH_ROUTES` now holds only the `exact` rows, and counting abstentions in it
    # would answer a question about the contract's shape rather than the router's
    # behaviour.
    fell_through = [
        qid
        for qid, text in QUESTIONS.items()
        if classify_intent(text) is IntentFamily.GENERAL_AGENT
    ]
    assert len(fell_through) == 14, sorted(fell_through)
    for closed in ("CP11", "TT10", "CP14", "CP16", "CP19"):
        assert closed not in fell_through


def test_every_family_is_reachable() -> None:
    """A family nothing routes to is a promise the pipeline cannot keep."""
    routed = {family for _, family in OWNER_ROUTINGS}
    routed |= {family for _, family in ENGLISH_ROUTINGS}
    routed |= set(BATCH_ROUTES.values())
    assert routed == set(IntentFamily)


def test_classification_is_deterministic() -> None:
    """No store, no clock, no ordering over a set."""
    for question in QUESTIONS.values():
        assert classify_intent(question) is classify_intent(question)


# --------------------------------------------------------------------------
# explicit_normative_claim_present — the narrow check Commit 6 consumes.
# --------------------------------------------------------------------------

NORMATIVE: tuple[str, ...] = (
    "هل يسمح لي بتسجيل مقرر من مستوى أعلى؟",
    "كم الحد الأعلى للساعات المسموح بها؟",
    "هل يجوز الاعتذار عن مقرر بعد الأسبوع العاشر؟",
    "وش العقوبة لو تجاوزت نسبة الغياب؟",
    "ماذا تقول اللائحة عن التسجيل المتأخر؟",
    "Am I allowed to register more than the maximum?",
    "What is the deadline for the final withdrawal?",
)

#: Every one of these is claimed by `policy_contract.policy_intent`, and none of
#: them asks what the regulation permits or requires. That function decides what
#: an ANSWER owes after retrieval has already run, so a false positive there
#: costs a citation; this one is read by a caller that changes what the student
#: is TOLD, so a false positive here replaces an answer with a referral.
NOT_NORMATIVE_BUT_POLICY_INTENT_SAYS_OTHERWISE: tuple[tuple[str, str], ...] = (
    ("TT03", "«بشكل ضروري» is emphasis on a course choice, read as OBLIGATION"),
    ("CP06", "«وش الفرق» between direct and chained unlocks, read as a definition"),
    ("CP11", "«المقررات المقفلة عندي» is the prerequisite graph, read as ENTITLEMENT"),
    ("CP13", "«هل عنده شرط ساعات مجتازة؟» asks what one course requires, not the rule"),
    ("CP14", "«النظام يقول» is the software, not the regulation"),
    ("CP15", "«لازم نحدد المقرر الفعلي» asks how an elective placeholder resolves"),
    ("CP16", "«متطلباته مكتملة» is a data question about one course"),
    ("CP17", "«وش الفرق» between priority and recommendation, read as a definition"),
    ("CP20", "a demand for an audit trail, read as registration advice"),
)


@pytest.mark.parametrize("question", NORMATIVE)
def test_explicit_normative_language_is_recognised(question: str) -> None:
    assert explicit_normative_claim_present(question) is True


@pytest.mark.parametrize(("qid", "why"), NOT_NORMATIVE_BUT_POLICY_INTENT_SAYS_OTHERWISE)
def test_the_narrow_check_drops_what_policy_intent_keeps(qid: str, why: str) -> None:
    question = QUESTIONS[qid]
    assert policy_intent(question), f"{qid} no longer exercises the difference"
    assert explicit_normative_claim_present(question) is False, why


def test_the_student_stating_their_own_ceiling_is_not_a_rule_question() -> None:
    """TT07 — «ابنِ لي أخف جدول ممكن، بحد أقصى 12 ساعة».

    «بحد أقصى» is the student's own constraint on the build, not a question about
    the regulation's ceiling.
    """
    assert explicit_normative_claim_present(QUESTIONS["TT07"]) is False
    assert classify_intent(QUESTIONS["TT07"]) is IntentFamily.PLANNER_BUILD


def test_a_limit_word_without_a_question_around_it_is_not_a_rule_question() -> None:
    """The interrogative gate, tested where the limit word IS the definite «الحد».

    TT07 alone cannot prove the gate works: «بحد» and «الحد» are different terms
    to the stemmer — it will not strip a prefix that leaves fewer than three
    letters — so TT07 never reaches the limit vocabulary in the first place. This
    sentence does reach it, in an imperative, and must still be an ordinary build.
    """
    stated = "ابنِ لي جدولًا لا يتجاوز الحد الذي وضعته لنفسي"
    assert explicit_normative_claim_present(stated) is False
    assert classify_intent(stated) is IntentFamily.PLANNER_BUILD
    asked = "كم الحد الذي أستطيع تسجيله؟"
    assert explicit_normative_claim_present(asked) is True


def test_the_one_batch_question_that_does_carry_a_normative_claim() -> None:
    """TT08 — the 19 is the credit ceiling, and the ceiling is in the لائحة.

    It is also the only question in the batch whose curated contract requires
    `policy_lookup`, so agreement here is a check against the labels and not
    only against itself.
    """
    assert explicit_normative_claim_present(QUESTIONS["TT08"]) is True


def test_the_narrow_check_is_strictly_narrower_over_the_whole_batch() -> None:
    """13/50 against 1/50 — and the 1 is a subset of the 13, not a different set.

    An overlap that was not a subset would mean the two definitions of
    "regulatory" have drifted apart rather than nested, which is the failure the
    shared marker vocabulary exists to prevent.
    """
    broad = {qid for qid, text in QUESTIONS.items() if policy_intent(text)}
    narrow = {qid for qid, text in QUESTIONS.items() if explicit_normative_claim_present(text)}
    assert narrow < broad
    assert len(narrow) == 1
    assert len(broad) == 13


@pytest.mark.parametrize(
    "question",
    [
        # «يفصل» is "separates" in any sense at all. These are the sentences that
        # make the one-step marker a THREE-word ordered sequence rather than a
        # keyword: each contains the verb and none is a priority question.
        "كم يوم يفصلني عن بداية الفصل الدراسي؟",
        "وش يفصل بين الفصل الأول والفصل الثاني؟",
        "المبنى اللي يفصل بين القاعتين وين؟",
        "how many weeks separate me from the exam period?",
    ],
)
def test_the_separator_verb_alone_does_not_claim_a_priority_question(question: str) -> None:
    """A marker written for one question must not become a pattern.

    Dropping the course noun and the count from `_m(_SEPARATES, _COURSE_NOUN,
    _ONE_WORD)` leaves every one of these classified COURSE_PRIORITY and routed at
    `my_progress`, which would answer a calendar question with a prerequisite
    ranking — the exact "wrong confident route" this module's docstring argues is
    worse than no route at all.
    """
    assert classify_intent(question) is IntentFamily.GENERAL_AGENT


@pytest.mark.parametrize(
    "cid", sorted(c for c, r in CONTRACT.items() if r["routing"]["mode"] == "one_of")
)
def test_a_one_of_case_lands_inside_its_allowed_families(cid: str) -> None:
    """Several questions are genuinely answerable under more than one family.

    TT09 «ثبّت لي شعبة M2 في مقرر AI331، وابنِ بقية الجدول حولها» is the clearest:
    it establishes no existing draft, so reading it as a build is defensible — and
    reading it as an edit is what preserves the section label. Demanding one exact
    family for it would score a defensible route as a defect.
    """
    allowed = CONTRACT[cid]["routing"]["allowed_families"]
    assert allowed, f"{cid} is one_of and names no allowed families"
    assert str(classify_intent(CONTRACT[cid]["question_ar"])) in allowed


@pytest.mark.parametrize(
    "cid",
    sorted(c for c, r in CONTRACT.items() if r["routing"]["mode"] in {"clarify", "none"}),
)
def test_a_clarify_or_none_case_names_no_family(cid: str) -> None:
    """The over-specification the audit exists to remove.

    With no course code and no antecedent, neither COURSE_LOCK_REASON nor
    COURSE_PRIORITY should execute a data tool — so the contract must not name
    either. `expected_family: null` is the assertion that the right answer is a
    question, not a lookup.
    """
    routing = CONTRACT[cid]["routing"]
    assert routing["expected_family"] is None, cid
    if routing["mode"] == "clarify":
        assert routing["clarification_reason"], f"{cid} clarifies for no stated reason"

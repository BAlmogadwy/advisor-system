"""Does this question demand a sourced rule, and did the answer supply one?

WHY A DETECTOR EXISTS AT ALL, GIVEN THE COMMENT THAT ARGUES AGAINST ONE

`virtual_advisor` records the decision to retrieve policy unconditionally on the
single-shot path, with the reason: "a classifier that says no is exactly how the
bypass comes back". That reasoning stands, and nothing here weakens it —
**retrieval is still unconditional**. What this module decides is not whether to
LOOK but what the answer OWES once the looking is done:

    required = False   evidence is available; the answer may use it
    required = True    a governing record must be cited, or the system abstains

A false negative therefore costs an obligation, never a retrieval. The bypass the
comment names — a classifier deciding a regulation question does not need the
store — is structurally impossible here.

WHY IT IS NOT `policy_count > 0`

Measured over the 284-question corpus: the store returns at least one record for
273/284 = 96.1% of questions, including 8 records for "What is my timetable this
term?". Retrieval answers "is there anything vaguely related", which is nearly a
constant. `MIN_LEXICAL_WEIGHT`'s own docstring settles the point for this repo:
tightening retrieval until off-topic questions returned nothing cost 95 of 252
genuine answers.

WHY IT IS NOT `direct_policy_evidence` EITHER

That fires on 201/284 = 70.8%, including 23 of the 32 questions whose curated
expectation names no policy at all — and it classifies RECORDS after retrieval,
so it cannot say what the question asked for. Live examples: «وش جدولي هذا
الفصل؟» resolves direct evidence to `TU.CONDUCT.CHEATING`, and "What is my
timetable this term?" to `TU.EXCUSE.*`. Cheating penalties are not what makes a
timetable question regulatory.

THE ASYMMETRY THAT SETS THE CALIBRATION

The owner's rule is "favour recall: a false positive causes a cautious refusal".
That is true for most of the corpus and NOT true for one class. For a pure
timetable or record question — «GS112 وين قاعتها؟», «وش عندي بكرة الأحد؟» — a
false positive does not produce a cautious answer; it replaces a completely
answerable answer with a referral, because the abstention takes the whole turn.
So the detector is built to be generous about normative language and strict about
that one class, and its specificity is measured against exactly those questions.

CALIBRATION, against evals/advisor (284 questions, curated per-question labels)

    required per the labels     272    detector fires on 217   recall      79.8%
    answerable without policy    11    detector fires on   1   specificity 90.9%

    joint outcome, all 284:
      required & retrieved       166   must cite a governing record
      required & none_governing   46   deterministic abstention
      required & none_matched      7   deterministic abstention
      not required & anything     65   unchanged

    53/284 = 18.7% would abstain where today an answer is produced. NONE of the
    53 is a question the labels call answerable without policy: every one is a
    rule question for which the store holds nothing governing — which is the
    case that today gets answered from the model's memory.

    The one false positive is «كم ساعة باقي لي على التخرج؟» (graduation topic).
    It retrieves governing evidence, so it costs a citation, not an answer.

The 55 false negatives are mostly `retrieved`: the model has the evidence and is
simply not compelled to cite it, so the citation validator still governs anything
it does claim. That is the direction to be wrong in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.services.policy_store import (
    alias_matches,
    expand_tokens_ordered,
    get_policy_store,
    raw_words_ordered,
)

#: Phrases that make a question a demand for a rule, grouped by the claim type
#: they signal. The groups are `policy_applicability.NORMATIVE_CLAIM_TYPES`
#: turned around: that vocabulary says what makes an ANSWER normative, and these
#: are the question-side markers of the same thing, so the two ends of the
#: contract cannot drift into different definitions of "regulatory".
#:
#: Matched through `alias_matches`, so a phrase's words must appear IN ORDER and
#: outside the scope of a negator — «أبي أحول، بس مو داخل الجامعة» does not
#: match an alias about internal transfer. Written in normalised form (no hamza,
#: ta-marbuta folded to ه) because that is what the matcher sees.
NORMATIVE_MARKERS: dict[str, tuple[str, ...]] = {
    "PERMISSION": (
        "مسموح",
        "يجوز",
        "يحق",
        "ممنوع",
        "اقدر",
        "ينفع",
        "لي الحق",
        "احق",
        "هل ممكن",
        "مسموح لي",
        "عندي حق",
        "allowed",
        "may i",
        "can i",
        "am i permitted",
        "is it permitted",
    ),
    "OBLIGATION": (
        "لازم",
        "يلزم",
        "الزامي",
        "يجب",
        "ضروري",
        "مطلوب مني",
        "ملزم",
        "احتاج موافقه",
        "موافقه المرشد",
        "بدون موافقه",
        "must",
        "required to",
        "do i have to",
        "obliged",
        "approval",
    ),
    "NUMERICAL_LIMIT": (
        "الحد",
        "حد اقصي",
        "حد ادني",
        "السقف",
        "كم مره مسموح",
        "اكثر من الحد",
        "الحد المسموح",
        "الحد النظامي",
        "نصاب",
        "المعدل المطلوب",
        "كم ساعه اسجل",
        "كم ساعه تنصحني",
        "عدد الساعات",
        "maximum",
        "minimum",
        "limit",
        "how many times",
        "cap",
    ),
    "DEADLINE": (
        "اخر موعد",
        "الموعد النهائي",
        "اخر يوم",
        "متي يفتح",
        "متي يغلق",
        "الفتره النظاميه",
        "متي الفاينل",
        "متي الاختبار",
        "متي اقدم",
        "الاجازات الرسميه",
        "التقويم",
        "متي تظهر الدرجات",
        "متي يبدا الترم",
        "deadline",
        "last date",
        "when does it open",
        "when does it close",
    ),
    "ELIGIBILITY": (
        "شروط",
        "يشترط",
        "مؤهل",
        "استحق",
        "معايير",
        "المتطلبات",
        "يسمح لي",
        "eligible",
        "conditions",
        "criteria",
        "qualify",
    ),
    "CONSEQUENCE": (
        "يترتب",
        "ينحسب",
        "يحسب علي",
        "يتاثر",
        "احرم",
        "الحرمان",
        "انذار",
        "عقوبه",
        "يفصل",
        "يتم فصلي",
        "تنحذف",
        "يتغير معدلي",
        "وش يصير لو",
        "يوثر",
        "يوثر علي",
        "يحسن معدلي",
        "يرفع معدلي",
        "يوخر تخرجي",
        "يتاخر تخرجي",
        "يظهر في السجل",
        "تنحسب لي",
        "consequence",
        "penalty",
        "will i be dismissed",
        "affect my gpa",
    ),
    "PROCEDURE": (
        "كيف اقدم",
        "كيف اطلب",
        "الاجراء",
        "اليه",
        "الخطوات",
        "كيف اسوي طلب",
        "من وين اقدم",
        "اراجع مين",
        "الجهه المسووله",
        "وش الاجراء",
        "كيف احذف",
        "كيف اضيف",
        "كيف انسحب",
        "كيف ارجع",
        "كيف احسب",
        "وش اسوي",
        "وش الحل",
        "مين اتواصل",
        "وين اقدم",
        "كيف اعرف",
        "مين اراجع",
        "وش معناها",
        "وين اراجع",
        "procedure",
        "how do i apply",
        "how do i request",
        "who do i contact",
    ),
    #: «مشكلات النظام الأكاديمي» — every question in that block of the corpus
    #: carries a policy label, because "why will the portal not let me do this"
    #: is a question about what the regulation entitles the student to, not a
    #: bug report.
    "ENTITLEMENT": (
        "ما تظهر",
        "ما ظهرت",
        "ما يطلع لي",
        "النظام يقول",
        "النظام يعتبرني",
        "موقوف",
        "صلاحيه",
        "رساله تجاوز",
        "مقفله",
        "مو صحيحه",
        "مو صحيح",
    ),
    "REGULATORY_DEFINITION": (
        "وش الفرق",
        "ايش الفرق",
        "تعريف",
        "يعرف",
        "وش المقصود",
        "وش معني",
        "difference between",
        "what does it mean",
    ),
    #: Course-selection and load advice reads as opinion and is not: the credit
    #: range governs it, which is why every question of this shape in the corpus
    #: carries a policy label.
    "REGULATED_ADVICE": (
        "تنصحني اسجل",
        "تنصحني اخذ",
        "وش اسجل",
        "المواد المناسبه",
        "جدولي ثقيل",
        "مواد اكثر",
        "افضل اعيدها",
        "اي مواد اسجل",
    ),
}


def _compile() -> dict[str, tuple[tuple[list[set[str]], str], ...]]:
    return {
        claim: tuple(
            (expand_tokens_ordered(phrase), phrase)
            for phrase in phrases
            if expand_tokens_ordered(phrase)
        )
        for claim, phrases in NORMATIVE_MARKERS.items()
    }


_COMPILED = _compile()


def _policy_domain(intent: Any) -> str:
    """The coarse domain, from a family or its name. GENERAL when nothing was routed.

    Imported lazily and tolerant of a string, because `policy_contract` is read by
    offline scoring tools that have no router; an unknown value means "not a data
    intent", which keeps the broad gate rather than relaxing it.
    """
    if intent is None:
        return "GENERAL"
    from core.services.advisor_intent import IntentFamily, policy_domain_of

    try:
        return policy_domain_of(IntentFamily(str(intent)))
    except ValueError:
        return "GENERAL"


#: Imported by value rather than referenced, so the two modules cannot drift.
_DATA_DOMAINS = frozenset({"PLANNER_DATA", "TIMETABLE_DATA", "COURSE_DATA"})


def policy_intent(question: str) -> tuple[str, ...]:
    """Which claim types this question asks for, and which regulated subjects.

    Two signals, unioned:

      * NORMATIVE MARKERS — "may I", "how many times", "what is the deadline".
        The question's own grammar asks for a rule.
      * CURATED TOPICS — `resolve_topics`, the store's 27 hand-written subject
        aliases. These catch the questions whose rule is implicit in the SUBJECT:
        «هل حذف المادة يأثر على معدلي؟» contains no modal at all, and dropping a
        course is a regulated act.

    Markers alone reached 52% recall; topics alone reach 38.7% and are Arabic-only.
    Together, 79.8%. Returned rather than reduced to a boolean so telemetry can
    record WHY a question was held to the contract.
    """
    text = str(question or "")
    if not text.strip():
        return ()
    words = expand_tokens_ordered(text)
    raw = raw_words_ordered(text)
    found: list[str] = []
    for claim, aliases in _COMPILED.items():
        for alias_words, _phrase in aliases:
            if alias_matches(alias_words, words, raw):
                found.append(claim)
                break
    try:
        topics = get_policy_store().resolve_topics(text)
    except Exception:  # pragma: no cover - a store outage must not decide intent
        topics = []
    found.extend(f"TOPIC:{topic}" for topic, _score in topics[:2])
    return tuple(found)


def requires_policy_contract(question: str) -> bool:
    """Must this answer cite a governing rule, or abstain?

    Provider-neutral by construction: it reads the question and the local store
    and nothing else. The same obligation applies whether the answer came from a
    model on localhost or from an external processor — a rule stated without a
    source is the same defect either way.
    """
    return bool(policy_intent(question))


#: The four states `_seed_policy_evidence` and the agent path can produce.
GROUNDING_RETRIEVED = "retrieved"
GROUNDING_NONE_GOVERNING = "none_governing"
GROUNDING_NONE_MATCHED = "none_matched"
GROUNDING_UNAVAILABLE = "unavailable"
#: Retrieval never ran. After server-side prefetch this is an internal contract
#: failure, not an outcome — see `PolicyContractState.retrieval_missing`.
GROUNDING_NOT_CONSULTED = "not_consulted"

_NO_GOVERNING = frozenset({GROUNDING_NONE_GOVERNING, GROUNDING_NONE_MATCHED, GROUNDING_UNAVAILABLE})


@dataclass(frozen=True)
class PolicyContractState:
    """What this turn owes, and what it was given — as data, not behaviour.

    Frozen and provider-neutral, and deliberately holding only ids rather than
    policy text: it travels into `result["agent"]`, which is persisted, shipped
    to the browser and read by `derive_outcome`. Policy prose and internal fields
    belong in the evidence, not in telemetry.
    """

    required: bool
    grounding_state: str
    direct_policy_ids: frozenset[str] = field(default_factory=frozenset)
    citable_policy_ids: frozenset[str] = field(default_factory=frozenset)
    intent: tuple[str, ...] = ()
    #: Which coarse domain decided the obligation. Recorded so a stored turn says
    #: WHY it owed a citation, not only that it did.
    policy_domain: str = "GENERAL"

    @property
    def retrieval_missing(self) -> bool:
        """Retrieval never happened on a question that needed it.

        After server-side prefetch this cannot occur through any normal path, so
        it is treated as a programming failure rather than as an answer: the one
        outcome that must never be "reply anyway".
        """
        return self.required and self.grounding_state == GROUNDING_NOT_CONSULTED

    @property
    def has_governing_evidence(self) -> bool:
        return bool(self.direct_policy_ids) and self.grounding_state == GROUNDING_RETRIEVED

    @property
    def must_abstain(self) -> bool:
        """A rule was demanded and nothing governing came back.

        `background_policy_evidence` deliberately does not rescue this. Background
        records are the ones the applicability layer decided do NOT govern the
        question, and letting them satisfy the contract would make the
        direct/background split — the thing that layer exists for — decorative.
        """
        return self.required and self.grounding_state in _NO_GOVERNING

    def missing_governing_citation(self, cited_policy_ids: Any) -> bool:
        """The answer cited nothing that actually governs.

        Citing a BACKGROUND record is not compliance. It is the most plausible
        way to fail this contract — the id is real, it was retrieved this
        request, and it passes every check the citation validator makes — which
        is exactly why the check is against `direct_policy_ids` and not against
        `citable_policy_ids`.
        """
        if not (self.required and self.has_governing_evidence):
            return False
        return not (set(cited_policy_ids or ()) & set(self.direct_policy_ids))

    def as_telemetry(self) -> dict[str, Any]:
        return {
            "policy_required": self.required,
            "policy_intent": list(self.intent),
            "policy_grounding": self.grounding_state,
            "policy_domain": self.policy_domain,
            "direct_policy_count": len(self.direct_policy_ids),
            "citable_policy_count": len(self.citable_policy_ids),
        }


def _ids(rows: Any) -> frozenset[str]:
    if not isinstance(rows, list):
        return frozenset()
    return frozenset(
        str(row.get("policy_id")) for row in rows if isinstance(row, dict) and row.get("policy_id")
    )


def build_policy_contract_state(
    question: str,
    policy_results: list[dict[str, Any]] | None,
    *,
    grounding_state: str,
    intent: Any = None,
    explicit_normative_claim: bool | None = None,
) -> PolicyContractState:
    """Assemble the contract from the question and every policy result this turn.

    Takes a LIST because a turn can hold more than one: the prefetch, and any
    credit-policy backing evidence injected when a tool result introduces a credit
    block. Both are stamped `tool: "policy_lookup"`, and a contract computed from
    only the first would understate what the answer is entitled to cite.
    """
    direct: set[str] = set()
    citable: set[str] = set()
    for result in policy_results or []:
        if not isinstance(result, dict) or result.get("tool") != "policy_lookup":
            continue
        direct |= _ids(result.get("direct_policy_evidence"))
        citable |= _ids(result.get("citable"))
        citable |= _ids(result.get("policies"))
    # ONCE. `requires_policy_contract` called `policy_intent` and this function
    # called it again, so every answer resolved the store's 27 topic aliases twice
    # for one decision. Cheap, and the shape mattered more than the cost: two calls
    # is two places for the gate to be changed in one of them.
    claims = policy_intent(question)

    # ── the domain decides what the answer OWES ──────────────────────────────
    #
    # Measured on the 50-question batch, the word-level gate demanded a citation on
    # 13 questions and could discharge it on 1. CP11 «وش المقررات المقفلة عندي وما
    # يفصلني عنها إلا مقرر واحد؟» and CP15 were REFUSED their own prerequisite data
    # over a rule that does not exist, and 7 more were "grounded" by citing a
    # glossary entry on a prerequisite question — which is worse than a refusal,
    # because it looks like evidence.
    #
    # The narrow check is the same POLICY markers the router uses, so a question a
    # data family owns can still owe a citation: TT08 «أريد تسجيل 19 ساعة» classifies
    # MIXED and keeps the obligation, which is the defect this whole branch exists
    # for. Retrieval is UNCHANGED and still unconditional — this decides what the
    # answer owes, never whether the store is consulted.
    domain = _policy_domain(intent)
    if domain in _DATA_DOMAINS:
        if explicit_normative_claim is None:
            from core.services.advisor_intent import explicit_normative_claim_present

            explicit_normative_claim = explicit_normative_claim_present(question)
        required = bool(explicit_normative_claim)
    else:
        required = bool(claims)

    return PolicyContractState(
        required=required,
        grounding_state=grounding_state,
        direct_policy_ids=frozenset(direct),
        citable_policy_ids=frozenset(citable),
        intent=claims,
        policy_domain=domain,
    )


__all__ = [
    "GROUNDING_NONE_GOVERNING",
    "GROUNDING_NONE_MATCHED",
    "GROUNDING_NOT_CONSULTED",
    "GROUNDING_RETRIEVED",
    "GROUNDING_UNAVAILABLE",
    "NORMATIVE_MARKERS",
    "PolicyContractState",
    "build_policy_contract_state",
    "policy_intent",
    "requires_policy_contract",
]

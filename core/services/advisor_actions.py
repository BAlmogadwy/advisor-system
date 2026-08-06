"""When a capability returns a ROUTE, the server answers — not the model.

A live turn produced this. A student asked for a timetable that ignores their
current registration; `build_my_timetable` refused correctly and returned

    reason: REBUILD_REQUIRES_PLANNER_CONFIRMATION
    action: OPEN_STUDENT_PLANNER

which means "this is available, through the planner, after a confirmation bound
to the student, the draft and its version". The loop handed that to the model as
one more tool result to write prose about, and the model turned a CONFIRMATION
REQUIREMENT into a CAPABILITY DENIAL: «لا يمكن للنظام بناء جدول يتجاهل تسجيلك
الحالي». It then told the student to go to the university's registration portal
and delete their courses by hand.

Three failures in one sentence, and only the first is about wording:

  * it denied a feature that exists, so the student will not look for it again;
  * it sent them out of the workflow built for exactly this request;
  * it advised deleting REAL registrations to obtain a PLANNING DRAFT. The
    rebuild never touches the registration record — the model conflated the
    two, and a student who follows that advice loses their seats.

WHY THIS IS NOT A PROMPT FIX

The obvious repair is a sentence in the system prompt telling the model to
honour `action`. That is the same class of control this branch has removed three
times already: the confirmation requirement for a destructive rebuild once lived
in a tool's JSON description — a sentence addressed to the model — and a single
Arabic word walked through it. Retrieval used to be the model's decision.
Identity used to be a parameter it could supply.

A route is a decision the server has already made. The model's job is to explain
things it was given; it is not the router. So a result carrying a known action
short-circuits generation entirely: deterministic text, a structured action for
the interface to render, and no provider call at all.

FOUR INTENTS, ONE ACTION, AND WHY CHOOSING AN ALTERNATIVE IS NOT A TOOL

The capability registry refuses to register anything that is not read-only
(`if not capability.read_only: raise`). That boundary is why chat cannot rebuild
a timetable, and it is the same boundary that keeps «احفظ الخيار الثاني كجدولي
المفضل» out of the tool list: `planner_drafts.select_alternative` writes
`selected_alternative` on a draft row, version-guarded, checked against the
alternatives that draft actually offered, and bound to the student who owns it.
Advertising that as a model tool would put a mutation behind an argument the
model fills in — the exact arrangement the rebuild gate was built to remove.

So all four planner intents are ROUTES, not operations. The model requests
nothing; the server names the surface; the authenticated planner endpoint —
student-only, via `AdvisorPrincipal.for_student` — performs the write once the
student has seen what they are agreeing to.

    VIEW_ALTERNATIVES                 «سوِّ لي أكثر من خيار للجدول»        TT02
    REBUILD_WITHOUT_CURRENT_SECTIONS  «ابنِ من الصفر وتجاهل الشعب»          TT27-29
    SELECT_PREFERRED_ALTERNATIVE      «احفظ الخيار الثاني كجدولي المفضل»    TT24
    EDIT_DRAFT                        «عدّلت قائمة المقررات؛ أعد البناء»    TT26

REBUILD arrives from a TOOL RESULT; the other three arrive from the QUESTION,
through `advisor_intent`. The asymmetry is deliberate. A rebuild is refused
inside `build_my_timetable`, at the point where the arguments are known and the
refusal is auditable against them; deciding it a second time from the question
alone would put one rule in two places with nothing keeping them equal. The other
three name no tool at all — there is no read-only capability whose refusal could
carry them — so the question is the only place the request exists.

WHY EDIT_DRAFT IS A ROUTE RATHER THAN A `build_my_timetable` CALL

TT26 is «عدّلت قائمة المقررات؛ أعد بناء البدائل بناءً على التعديل الجديد» — the
edit already happened, in the planner, on a draft. `_exec_build_my_timetable`
builds from `must_include` plus `recommend_next_courses`; it has no access to a
draft and no way to learn which courses the student added or dropped. Answering
it with a build produces alternatives from the SYSTEM's list and presents them as
"based on your edit", which is a fabrication with a tool call behind it — the
same shape as the failure this module exists for, one surface along.

WHY `alternative_ref` IS A HINT AND NEVER AN IDENTIFIER

An alternative's real key is `sha256("-".join(section ids))[:16]`
(`student_planner.build_student_options`) — opaque, regenerated whenever the
draft's version changes, and unknowable from a sentence. «الخيار الثاني» names a
POSITION in a list the chat has never seen. The ref therefore travels as an
ordinal for the planner to resolve against what it is currently displaying, and
`select_alternative` still refuses anything that is not among the offered keys.

The prose never mentions it. If the extraction were wrong, a sentence saying
"option 2" would be a false statement about the student's own request, and the
payload — the part a UI can re-resolve — would be the only part that could be
corrected. The ordinal is omitted rather than guessed when none sits beside a
word naming an option.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from core.services.advisor_intent import IntentFamily, classify_intent
from core.services.arabic_text import normalise

#: The one action any handoff emits today. Adding another means adding an
#: `ActionHandoff` below — deliberately, with its own wording reviewed — rather
#: than widening a rule that lets unknown routes through as free prose.
OPEN_STUDENT_PLANNER = "OPEN_STUDENT_PLANNER"

#: Fail closed. An action string nothing here recognises is a route the interface
#: has no screen for, so a handoff carrying one is refused at construction rather
#: than shipped as a button that goes nowhere.
KNOWN_ACTIONS = frozenset({OPEN_STUDENT_PLANNER})

INTENT_REBUILD_WITHOUT_CURRENT_SECTIONS = "REBUILD_WITHOUT_CURRENT_SECTIONS"
INTENT_VIEW_ALTERNATIVES = "VIEW_ALTERNATIVES"
INTENT_SELECT_PREFERRED_ALTERNATIVE = "SELECT_PREFERRED_ALTERNATIVE"
INTENT_EDIT_DRAFT = "EDIT_DRAFT"

KNOWN_INTENTS = frozenset(
    {
        INTENT_REBUILD_WITHOUT_CURRENT_SECTIONS,
        INTENT_VIEW_ALTERNATIVES,
        INTENT_SELECT_PREFERRED_ALTERNATIVE,
        INTENT_EDIT_DRAFT,
    }
)


@dataclass(frozen=True)
class ActionHandoff:
    """A server-authored answer, and the route the interface should offer."""

    action: str
    intent: str
    answer_ar: str
    answer_en: str
    #: Whether the student's official registration was touched. Always False so
    #: far, and stated rather than implied: the live failure this module exists
    #: for was a model telling a student to delete real registrations, so
    #: "nothing was changed" is the sentence that has to survive translation,
    #: rendering, and every future edit of the prose above it.
    registration_modified: bool = False
    #: Whether the route ends at a step the STUDENT has to take. True for all
    #: four, including viewing: a route is an offer, and nothing at the other end
    #: happens because chat said so. It is not the rebuild's confirmation TOKEN —
    #: that is `planner_drafts.issue_rebuild_token`, hashed and bound to a draft
    #: version, and no field in this payload could stand in for it.
    requires_confirmation: bool = True
    #: Whether an ordinal like "ALT_2" means anything for this intent. A flag
    #: rather than a check in the caller, because "which routes carry a ref" is a
    #: property of the route: two call sites that each decide it separately are
    #: how a ref ends up on a rebuild, where there is no list to be second in.
    accepts_alternative_ref: bool = False
    #: Set per request by `handoff_for_question`, never at definition — it comes
    #: from the student's sentence, not from the route.
    alternative_ref: str = ""

    def __post_init__(self) -> None:
        if self.action not in KNOWN_ACTIONS:
            raise ValueError(f"unknown action {self.action!r}")
        if self.intent not in KNOWN_INTENTS:
            raise ValueError(f"unknown intent {self.intent!r}")
        if self.alternative_ref and not self.accepts_alternative_ref:
            raise ValueError(f"{self.intent} carries no alternative reference")

    def answer(self, language: str) -> str:
        return self.answer_ar if language == "Arabic" else self.answer_en

    def with_alternative_ref(self, ref: str) -> ActionHandoff:
        """A copy naming which offered alternative the student meant.

        Returns `self` unchanged for an empty ref, so a caller can apply the
        extraction unconditionally and an absent ordinal stays an ABSENT key
        rather than becoming `"alternative_ref": ""` — a value a client would
        have to test for separately from the key being missing.
        """
        if not ref:
            return self
        return replace(self, alternative_ref=ref)

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.action,
            "intent": self.intent,
            "requires_confirmation": self.requires_confirmation,
            "registration_modified": self.registration_modified,
        }
        if self.alternative_ref:
            payload["alternative_ref"] = self.alternative_ref
        return payload


# --------------------------------------------------------------------------
# The prose. Every handoff says three things, in this order: the feature EXISTS,
# it lives in the planner, and the official registration is untouched. The third
# is the one the live failure got wrong, and it is written out in full in each
# language rather than shared through a formatting helper — a template that
# assembles the sentence is a template someone can change once and silently
# change four answers, and this is the sentence that costs a student their seats.
# --------------------------------------------------------------------------

_REBUILD_AR = (
    "أستطيع إنشاء مسودة جدول بديل تتجاهل الشعب المسجّلة حاليًا، لكن إعادة البناء "
    "تحتاج إلى تأكيد من خلال المخطط الدراسي.\n\n"
    "هذا الإجراء لن يحذف أو يغيّر تسجيلك الرسمي؛ فهو ينشئ بدائل تخطيطية جديدة "
    "لتختار منها، ويبقى تسجيلك الفعلي كما هو إلى أن تعدّله بنفسك عبر البوابة "
    "الرسمية.\n\n"
    "افتح المخطط الدراسي لإكمال تأكيد إعادة البناء."
)
_REBUILD_EN = (
    "I can build a draft timetable that ignores your currently registered "
    "sections, but a rebuild has to be confirmed in the study planner.\n\n"
    "This does not delete or change your official registration. It creates new "
    "planning alternatives for you to choose from, and your actual registration "
    "stays exactly as it is until you change it yourself through the official "
    "portal.\n\n"
    "Open the study planner to confirm the rebuild."
)

_VIEW_ALTERNATIVES_AR = (
    "أستطيع عرض أكثر من جدول مقترح لك؛ والمقارنة بينها تتم في المخطط الدراسي، "
    "حيث تظهر الجداول جنبًا إلى جنب بمواعيدها وشعبها.\n\n"
    "استعراض البدائل لن يحذف أو يغيّر تسجيلك الرسمي؛ فكل جدول معروض هو مسودة "
    "تخطيطية، ويبقى تسجيلك الفعلي كما هو إلى أن تعدّله بنفسك عبر البوابة "
    "الرسمية.\n\n"
    "افتح المخطط الدراسي لاستعراض البدائل والمقارنة بينها."
)
_VIEW_ALTERNATIVES_EN = (
    "I can offer you more than one proposed timetable. They are compared in the "
    "study planner, where the alternatives appear side by side with their times "
    "and sections.\n\n"
    "Looking at them does not delete or change your official registration. Every "
    "timetable shown is a planning draft, and your actual registration stays "
    "exactly as it is until you change it yourself through the official "
    "portal.\n\n"
    "Open the study planner to see the alternatives and compare them."
)

_SELECT_PREFERRED_AR = (
    "تحديد جدول مفضّل من بين البدائل المعروضة متاح، ويتم داخل المخطط الدراسي "
    "لأن الاختيار يُحفظ على المسودة التي عُرضت عليك.\n\n"
    "تفضيل جدول لن يسجّلك في أي شعبة، ولن يحذف أو يغيّر تسجيلك الرسمي؛ فهو "
    "تفضيل على مسودة تخطيطية، ويبقى تسجيلك الفعلي كما هو إلى أن تعدّله بنفسك "
    "عبر البوابة الرسمية.\n\n"
    "افتح المخطط الدراسي لتحديد الجدول الذي تفضّله."
)
_SELECT_PREFERRED_EN = (
    "Marking one of the offered timetables as your preferred one is available, "
    "and it is done in the study planner, because the choice is saved against "
    "the draft the alternatives were generated from.\n\n"
    "Preferring a timetable does not register you in any section, and does not "
    "delete or change your official registration. It is a preference on a "
    "planning draft, and your actual registration stays exactly as it is until "
    "you change it yourself through the official portal.\n\n"
    "Open the study planner to mark the timetable you prefer."
)

_EDIT_DRAFT_AR = (
    "تعديل قائمة المقررات وإعادة توليد البدائل منها متاح، ويتم داخل المخطط "
    "الدراسي لأن قائمتك المعدّلة محفوظة هناك على المسودة ولا تصل إلى "
    "المحادثة.\n\n"
    "التعديل لن يحذف أو يغيّر تسجيلك الرسمي؛ فهو يغيّر مسودة تخطيطية، ويبقى "
    "تسجيلك الفعلي كما هو إلى أن تعدّله بنفسك عبر البوابة الرسمية.\n\n"
    "افتح المخطط الدراسي لتعديل قائمة المقررات وإعادة توليد البدائل."
)
_EDIT_DRAFT_EN = (
    "Editing the course list and regenerating the alternatives from it is "
    "available, and it is done in the study planner, because your edited list is "
    "held there on the draft and does not reach this conversation.\n\n"
    "Editing does not delete or change your official registration. It changes a "
    "planning draft, and your actual registration stays exactly as it is until "
    "you change it yourself through the official portal.\n\n"
    "Open the study planner to edit the course list and regenerate the "
    "alternatives."
)


_REBUILD_HANDOFF = ActionHandoff(
    action=OPEN_STUDENT_PLANNER,
    intent=INTENT_REBUILD_WITHOUT_CURRENT_SECTIONS,
    answer_ar=_REBUILD_AR,
    answer_en=_REBUILD_EN,
)
_VIEW_ALTERNATIVES_HANDOFF = ActionHandoff(
    action=OPEN_STUDENT_PLANNER,
    intent=INTENT_VIEW_ALTERNATIVES,
    answer_ar=_VIEW_ALTERNATIVES_AR,
    answer_en=_VIEW_ALTERNATIVES_EN,
)
_SELECT_PREFERRED_HANDOFF = ActionHandoff(
    action=OPEN_STUDENT_PLANNER,
    intent=INTENT_SELECT_PREFERRED_ALTERNATIVE,
    answer_ar=_SELECT_PREFERRED_AR,
    answer_en=_SELECT_PREFERRED_EN,
    accepts_alternative_ref=True,
)
_EDIT_DRAFT_HANDOFF = ActionHandoff(
    action=OPEN_STUDENT_PLANNER,
    intent=INTENT_EDIT_DRAFT,
    answer_ar=_EDIT_DRAFT_AR,
    answer_en=_EDIT_DRAFT_EN,
)

#: Keyed on the capability's `reason`, not on `action`, because the reason is
#: what the executor decided and the action is only how it is offered. Two
#: reasons could share one action and need different sentences.
HANDOFFS: dict[str, ActionHandoff] = {
    "REBUILD_REQUIRES_PLANNER_CONFIRMATION": _REBUILD_HANDOFF,
}

#: Keyed on the router's family. `PLANNER_REBUILD` is deliberately ABSENT: it is
#: routed by `HANDOFFS` above, from the executor's refusal, and adding it here
#: would answer a rebuild request without the executor ever seeing it — two
#: copies of one rule, and the audited copy is the one that would stop running.
ROUTED_INTENTS: dict[IntentFamily, ActionHandoff] = {
    IntentFamily.PLANNER_VIEW_ALTERNATIVES: _VIEW_ALTERNATIVES_HANDOFF,
    IntentFamily.PLANNER_SELECT_PREFERRED: _SELECT_PREFERRED_HANDOFF,
    IntentFamily.PLANNER_EDIT_DRAFT: _EDIT_DRAFT_HANDOFF,
}


# --------------------------------------------------------------------------
# Which alternative the student meant.
# --------------------------------------------------------------------------

#: `planner_builder.build_plans` assembles three methods × `_top_k_method(k=3)`,
#: then `build_student_options` drops duplicate signatures — so nine is the
#: ceiling on what a student can ever have been shown, and "الخيار الحادي عشر"
#: names nothing the planner could resolve. Refusing to emit a ref above this is
#: not a guess about the UI: it is the generator's own arithmetic.
MAX_ALTERNATIVES = 9

#: Written the way the words are spelled and folded HERE, at definition, the way
#: `arabic_text.STOPWORDS` is. Stored raw, «الأول» would be compared against a
#: question already folded to «الاول» and would never match — which is precisely
#: the bug that module's comment records for four of the commonest words in
#: Arabic. Feminine forms are listed because «البدائل» and «الخيارات» take
#: agreement: «البديل الثاني» but «النسخة الثانية».
_ORDINAL_SPELLINGS: dict[int, tuple[str, ...]] = {
    1: ("الأول", "الأولى", "أول", "أولى", "first"),
    2: ("الثاني", "الثانية", "ثاني", "ثانية", "second"),
    3: ("الثالث", "الثالثة", "ثالث", "ثالثة", "third"),
    4: ("الرابع", "الرابعة", "رابع", "رابعة", "fourth"),
    5: ("الخامس", "الخامسة", "خامس", "خامسة", "fifth"),
    6: ("السادس", "السادسة", "سادس", "سادسة", "sixth"),
    7: ("السابع", "السابعة", "سابع", "سابعة", "seventh"),
    8: ("الثامن", "الثامنة", "ثامن", "ثامنة", "eighth"),
    9: ("التاسع", "التاسعة", "تاسع", "تاسعة", "ninth"),
}
_ORDINALS: dict[str, int] = {
    normalise(spelling).lower(): value
    for value, spellings in _ORDINAL_SPELLINGS.items()
    for spelling in spellings
}

#: The word the ordinal has to be attached to. «الجدول» is absent although
#: «الجدول الثاني» reads like "the second alternative": a timetable is also the
#: thing a student asks to SEE, and «جدولي المسجل» is a read of the registered
#: week — a family away from anything with a second item. An ordinal that names
#: no option is dropped, which is the documented default.
_OPTION_SPELLINGS: tuple[str, ...] = (
    "خيار",
    "الخيار",
    "خيارات",
    "الخيارات",
    "بديل",
    "البديل",
    "بدائل",
    "البدائل",
    "option",
    "options",
    "alternative",
    "alternatives",
)
_OPTION_WORDS = frozenset(normalise(word).lower() for word in _OPTION_SPELLINGS)

#: Words that carry no meaning between the option and its number. «البديل رقم 2»
#: and "option number 3" are the same request as «البديل الثاني»; without this
#: the ordinal is one token too far away and the ref is dropped. Exactly one is
#: skipped — a wider window would let «الخيار الذي يبدأ الساعة 2» name ALT_2.
_ORDINAL_CONNECTORS = frozenset({normalise("رقم").lower(), "number"})


def _ordinal_value(token: str) -> int | None:
    """The alternative this token names, or None.

    Digits are read as themselves: `normalise` has already folded «٢» to "2", so
    a question written in Arabic-Indic numerals reaches the same branch.
    """
    named = _ORDINALS.get(token)
    if named is not None:
        return named
    if token.isdigit():
        value = int(token)
        return value if 1 <= value <= MAX_ALTERNATIVES else None
    return None


def alternative_ref_in(question: str) -> str:
    """Which offered alternative the sentence names: «الخيار الثاني» -> "ALT_2".

    Returns the empty string when no ordinal sits beside a word naming an option,
    so an absent ordinal stays absent instead of defaulting to the first.

    Adjacency is the whole rule. Arabic puts the ordinal after the noun («الخيار
    الثاني») and English puts it before ("the second option"), so both sides of
    the option word are read — but only the immediate neighbours, because an
    ordinal anywhere in the sentence would let «ابنِ الخيار الذي يناسب الفصل
    الثاني» ask for the second alternative.
    """
    tokens = normalise(question).lower().split()
    for index, token in enumerate(tokens):
        if token not in _OPTION_WORDS:
            continue
        after = index + 1
        if after < len(tokens) and tokens[after] in _ORDINAL_CONNECTORS:
            after += 1
        for neighbour in (after, index - 1):
            if not 0 <= neighbour < len(tokens):
                continue
            value = _ordinal_value(tokens[neighbour])
            if value is not None:
                return f"ALT_{value}"
    return ""


def handoff_for(result: Any) -> ActionHandoff | None:
    """The route this tool result demands, if any.

    Returns None for everything else, so the loop is unchanged for the tools
    that simply return data — which is all of them but one.
    """
    if not isinstance(result, dict):
        return None
    return HANDOFFS.get(str(result.get("reason") or ""))


def handoff_for_question(question: str) -> ActionHandoff | None:
    """The route this QUESTION demands, before any tool runs or any token is generated.

    Returns None for every family the router does not route — including
    `PLANNER_REBUILD`, which keeps its executor-driven path, and including
    `GENERAL_AGENT`, which is what the router returns whenever it is not certain.
    Fail-closed in both directions: an unrouted question costs one agent loop,
    which is what happens today.
    """
    handoff = ROUTED_INTENTS.get(classify_intent(question))
    if handoff is None:
        return None
    if not handoff.accepts_alternative_ref:
        return handoff
    return handoff.with_alternative_ref(alternative_ref_in(question))


__all__ = [
    "HANDOFFS",
    "INTENT_EDIT_DRAFT",
    "INTENT_REBUILD_WITHOUT_CURRENT_SECTIONS",
    "INTENT_SELECT_PREFERRED_ALTERNATIVE",
    "INTENT_VIEW_ALTERNATIVES",
    "KNOWN_ACTIONS",
    "KNOWN_INTENTS",
    "MAX_ALTERNATIVES",
    "OPEN_STUDENT_PLANNER",
    "ROUTED_INTENTS",
    "ActionHandoff",
    "alternative_ref_in",
    "handoff_for",
    "handoff_for_question",
]

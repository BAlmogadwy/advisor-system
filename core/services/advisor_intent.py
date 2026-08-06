"""Which product surface can answer this question — decided here, not by the model.

WHAT THIS IS FOR

The 50-question live batch (`runtime/evals/planner_priority_alibaba_20260806-081850.json`)
did not fail on comprehension. It failed because the workflow, the tool schemas,
the returned payloads and the policy gate each described a slightly different
system, and the model was left to reconcile them turn by turn. A router cannot
fix a payload, but it can stop the reconciliation from happening in prose: once
the server knows «تجاهل جدولي الحالي» is a REBUILD and «وش جدولي؟» is a read of
the registered timetable, the rest of the pipeline can hold those two to
different contracts instead of hoping one paragraph of system prompt separates
them.

The same argument `advisor_actions` makes about routes applies to intents, for
the same reason: the confirmation requirement for a destructive rebuild once
lived in a tool's JSON description — a sentence addressed to the model — and a
single Arabic word walked through it.

WHY GENERAL_AGENT IS THE DEFAULT AND WHY THAT IS NOT TIMIDITY

A wrong confident route is strictly worse than no route. `GENERAL_AGENT` costs
one agent loop, which is what happens today; a wrong family sends a question to
a surface that structurally cannot answer it — «هل الشعب فيها مقاعد؟» routed to
the planner returns a timetable and no seat count, and the answer that comes back
is a fabrication with a tool call behind it. So every family here fires on an
explicit, enumerated marker, and anything else falls through. Measured on the 50
batch questions this classifies 32 and abstains on 18; every one of the 32 lands
in the domain its curated label names. The 18 abstentions are the point, not a
gap to close by loosening a pattern — «هل الشعب فيها مقاعد؟» (TT22) and «بصفتي
المرشد، ما المقرر الذي يحرر أكبر عدد من المقررات؟» (CP19) are both better served
by the agent loop than by a family that would answer the wrong half.

AN ABSTENTION IS NOT ALWAYS FREE, WHICH IS WHY ONE WAS CLOSED

CP11 «وش المقررات المقفلة عندي وما يفصلني عنها إلا مقرر واحد؟» read as
GENERAL_AGENT — defensible on its own terms, and a live defect anyway, because the
POLICY gate keys on the family: an unrouted question kept the broad obligation and
the student was refused their own prerequisite data over a rule that does not
exist. So the cost of abstaining is paid downstream, and a family whose data the
system holds outright is worth an enumerated marker. The whole table is now pinned
row by row in `tests/test_advisor_action_handoff.py`, GENERAL_AGENT rows included,
so the next marker cannot move one silently.

WHY IT REUSES `policy_store`'S MATCHER RATHER THAN A REGEX SET

Arabic attaches the article, the conjunction and the preposition to the word, so
«المقررات» and «مقرر» are one term to a reader and two strings to a comparison.
`expand_tokens_ordered` already solves that, `alias_matches` already requires the
words IN ORDER (a bag cannot tell «أكثر من خيار» from «خيار واحد أكثر وضوحًا»),
and `arabic_text.normalise` already folds the hamza and ta-marbuta variants. This
repo has been bitten twice by a second copy of that logic — once when a
duplicated diacritics range normalised every string to the empty string and
reported 64 of 81 rules as unevidenced. There is no tokeniser in this module.

THREE THINGS THE PRIMITIVES DO NOT COVER, HANDLED HERE AND NOWHERE ELSE

  1. Case. `normalise` does not lowercase, so "Build my schedule" and "build my
     schedule" are different tokens. Every string on both sides goes through
     `_fold` first.
  2. Accusative tanween. «جدولًا» folds to "جدولا", and the light stemmer's suffix
     list has no bare alef, so it never meets «جدول». 15 of the 50 batch questions
     carry the form somewhere; it lands on a word this module actually matches in
     six of them — «جدولًا» in TT06, TT16, TT25 and TT27, «مقررًا» in TT21 and
     CP15. The ALIAS side is widened with the alef form; the question side is
     untouched, so this cannot make two unrelated question words collide.
  3. The attached ل. «للجدول» strips one ل to "لجدول" and stops, so it never meets
     «جدول» either. Those surfaces are listed explicitly in the word classes.

WHY THE NEGATION STREAM IS REBUILT INSTEAD OF PASSED STRAIGHT THROUGH

`alias_matches(alias, question_words, raw_words)` indexes its negation window
into `raw_words` using a position taken from `question_words` — and those two
streams have different lengths, because `expand_tokens_ordered` drops stopwords
and single characters while `raw_words_ordered` keeps them. On
«ما عندي مقررات محددة، ابنِ الجدول من توصيات خطتي» the build imperative sits at
raw index 4 and question index 3, so the window lands on «ما عندي مقررات» and
reports a build request as negated. Feeding it a stream filtered by the SAME
predicate — one `expand_tokens_ordered` call per raw word, so the predicate is
never restated — puts the window back over the right words. The only negator
lost that way is «ما», which is the one whose meaning depends on what follows it
(«ما تتعارض مع جدولي» is a clash question, not a denial of one) and which caused
false suppressions in both directions. This is a defect in `policy_store`, not a
local preference; it is reported separately and deliberately not changed here,
because `policy_contract`'s calibration was measured with the current behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from core.services.arabic_text import normalise
from core.services.policy_store import (
    alias_matches,
    expand_tokens_ordered,
    raw_words_ordered,
)


class IntentFamily(StrEnum):
    """The domain that owns the answer, not the tool that produces it.

    Deliberately coarser than the capability registry: a family names a product
    surface with one contract (a planner build has a confirmation story, a
    registered-timetable read does not), and several tools can serve one family.
    Values equal their names so telemetry and stored turns stay readable.
    """

    PLANNER_BUILD = "PLANNER_BUILD"
    PLANNER_VIEW_ALTERNATIVES = "PLANNER_VIEW_ALTERNATIVES"
    PLANNER_REBUILD = "PLANNER_REBUILD"
    PLANNER_SELECT_PREFERRED = "PLANNER_SELECT_PREFERRED"
    PLANNER_EDIT_DRAFT = "PLANNER_EDIT_DRAFT"
    CURRENT_TIMETABLE = "CURRENT_TIMETABLE"
    TIMETABLE_CLASH = "TIMETABLE_CLASH"
    COURSE_PRIORITY = "COURSE_PRIORITY"
    COURSE_UNLOCKS = "COURSE_UNLOCKS"
    COURSE_LOCK_REASON = "COURSE_LOCK_REASON"
    POLICY = "POLICY"
    MIXED = "MIXED"
    GENERAL_AGENT = "GENERAL_AGENT"


def _fold(text: str) -> str:
    """The one pre-fold both sides get. `normalise` does not lowercase."""
    return normalise(text).lower()


def _words(*spellings: str) -> frozenset[str]:
    """One matcher set for every surface spelling of a single concept.

    `alias_matches` compares variant SET against variant SET, so widening the
    alias side is the supported way to add a spelling the light stemmer cannot
    reach. Each spelling must be one word: the set is order-free by construction,
    and a phrase written here would silently lose its order.

    The trailing alef is the accusative tanween — «جدولًا» folds to "جدولا" and no
    suffix rule removes a bare alef, so without this a question written in MSA
    accusative matches nothing at all. Widening the ALIAS side only is what keeps
    that safe: two question words are never brought together by it.
    """
    out: set[str] = set()
    for spelling in spellings:
        expanded = expand_tokens_ordered(_fold(spelling))
        if len(expanded) != 1:  # pragma: no cover - guards a typo, not a branch
            raise ValueError(f"_words() takes single content words, got {spelling!r}")
        out |= expanded[0]
        out |= {variant + "ا" for variant in expanded[0]}
    return frozenset(out)


# --------------------------------------------------------------------------
# Word classes. Every family below is an ORDERED sequence of these.
# --------------------------------------------------------------------------

#: An indefinite timetable — the thing a build produces. «للجدول» is listed
#: because the stemmer strips one ل and stops, so it never reaches «جدول»; the
#: accusative «جدولًا» is covered by `_words` instead (see module docstring).
_SCHEDULE = _words(
    "جدول", "الجدول", "للجدول", "بالجدول", "جداول", "schedule", "timetable", "timetables"
)

#: The student's OWN timetable. Kept apart from `_SCHEDULE` so that "build me a
#: timetable" and "show me my timetable" cannot collapse into one family; the
#: possessive ي is the whole distinction and no stemming rule touches it.
#: «كجدولي» is listed separately because ك is not in the stemmer's prefix set —
#: only the three-letter «كال» is — so «كجدولي المفضل» reaches nothing otherwise.
_MY_SCHEDULE = _words("جدولي", "لجدولي", "كجدولي", "myschedule")

_BUILD_VERB = _words(
    "ابن",
    "ابني",
    "سو",
    "سوي",
    "اعمل",
    "انشئ",
    "جهز",
    "رتب",
    "تبني",
    "بناء",
    "نظم",
    "build",
    "create",
    "generate",
    "construct",
    "make",
)

_WANT_VERB = _words("ابغي", "اريد", "ابي", "ودي", "احتاج", "want", "need")

#: «أضف AI352 للجدول وكمل باقي الساعات» is a build with a must-include, not an
#: edit of an existing draft. «إضافة» is deliberately absent: its ه-suffix strips
#: to «اضاف», which would match «الجدول أضاف مقررًا» — a provenance question.
_ADD_VERB = _words("اضف", "ضيف", "add", "include")

_SHOW_VERB = _words("اعرض", "ورني", "اعطني", "وريني", "show", "display", "view", "list")

_ASK_WORD = _words("وش", "ايش", "شنو", "كم", "هل", "what", "how", "which")

#: Arabic attaches the possessive; English does not, so «جدولي» is one token and
#: "my timetable" is two. Without this class every CURRENT_TIMETABLE marker is
#: Arabic-only, and "show my timetable" falls through to the agent loop.
_POSSESSIVE = _words("my", "mine")

_REGISTERED = _words(
    "المسجل", "المسجله", "الحالي", "الحاليه", "حاليا", "المعتمد", "registered", "current"
)

#: Every surface form is listed. The stemmer strips only the leading ال/و/ب/ل/ف
#: family and a short suffix list, and nothing in either turns «تتعارض» into
#: «تعارض» — the ت is part of the verb, not an affix it knows about.
_CLASH = _words(
    "تعارض",
    "تتعارض",
    "يتعارض",
    "التعارض",
    "متعارض",
    "متعارضه",
    "تعارضات",
    "clash",
    "clashes",
    "conflict",
    "conflicts",
    "overlap",
    "overlaps",
)

_IGNORE_VERB = _words("تجاهل", "تجاهلي", "الغ", "الغي", "احذف", "ignore", "discard")

_WITHOUT = _words("بدون", "دون", "without", "excluding")

#: What a rebuild throws away. «جدولي» belongs here as well as in `_MY_SCHEDULE`:
#: «تجاهل جدولي الحالي» names the current timetable as the thing being discarded.
_CURRENT_REGISTRATION = _words(
    "الشعب",
    "شعبي",
    "المسجله",
    "المسجل",
    "الحالي",
    "الحاليه",
    "جدولي",
    "تسجيلي",
    "sections",
    "registration",
    "registered",
    "current",
)

_FROM_SCRATCH = _words("الصفر", "scratch")

#: «ثبّت» is absent although it reads like "fix in place". In this product it is
#: the PIN verb: TT09 is «ثبّت لي شعبة M2 في مقرر AI331، وابنِ بقية الجدول حولها»,
#: a build with one section held constant, and nothing about it is a preference.
_SAVE_VERB = _words("احفظ", "خزن", "save", "keep", "store")

_OPTION = _words(
    "خيار",
    "خيارات",
    "الخيارات",
    "بديل",
    "بدائل",
    "البدائل",
    "option",
    "options",
    "alternative",
    "alternatives",
)

#: «اعتمد الخيار الأول» is NOT here. TT23 is «اعتمد الخيار الأول وسجلني في الشعب»
#: — adopting an option and asking to be registered — and the answer that matters
#: is that the adviser registers nothing. Routing it to a save-preference surface
#: would answer the half of the question that is harmless.
#:
#: «كمفضل» is absent, and that single omission is what separates the command from
#: a question about the command. TT25 is «لما أحفظ جدولًا كمفضل، هل يتغير تسجيلي
#: الحالي في البوابة؟» — it contains the save verb AND the preference word, and
#: it is not a request to save anything; the answer it needs is that preferring a
#: draft changes no registration. Routing it to the planner would answer a
#: question the student did not ask and leave the one they did ask unanswered.
_PREFERRED = _words("المفضل", "المفضله", "مفضل", "preferred", "favourite", "favorite")

_MORE_THAN_ONE = _words("اكثر", "عده", "متعدده", "more", "several", "multiple", "many")

#: Past tense and the noun only. An imperative «غيّر باقي المقررات» is a build
#: constraint, not an edit of a saved draft — TT10 asks for exactly that and must
#: not land here.
_EDIT_WORD = _words(
    "عدلت",
    "عدلته",
    "التعديل",
    "تعديلي",
    "غيرت",
    "حذفت",
    "اضفت",
    "edited",
    "modified",
    "changed",
)

_COURSE_NOUN = _words(
    "مقرر",
    "المقرر",
    "مقررات",
    "المقررات",
    "للمقررات",
    "ماده",
    "المواد",
    "مواد",
    "course",
    "courses",
    "subject",
    "subjects",
)

#: «افتح»/"open" are absent on purpose: «افتح لي مخطط الجدول» opens the planner.
_UNLOCK_VERB = _words("يفتح", "تفتح", "ينفتح", "تنفتح", "بينفتح", "unlock", "unlocks", "unlocked")

#: «معتمد» and "blocked" are absent although both read like "is waiting on":
#: «المقرر المعتمد» is the APPROVED course, and "blocked" already belongs to
#: `_LOCKED_WORD`, where a lock question needs a «ليش» in front of it.
_WAIT_VERB = _words("ينتظر", "تنتظر", "waiting", "awaiting")

#: The same question with the dependency named from the other end: «كم مقرر يعتمد
#: على AI331؟» / "how many courses depend on AI331?". Both classified GENERAL_AGENT
#: until now, which left the phrasing the task brief names verbatim with no owning
#: capability at all.
#:
#: «يشترط» and "requires" are deliberately ABSENT. They read as dependency words but
#: point the OTHER way — «AI352 يشترط AI331» is a statement about AI352's own
#: prerequisites, which is `course_prerequisites`' question, and folding it in here
#: would recreate the direction confusion this family exists to resolve. «يشترط»
#: also already belongs to `_FORMAL_REQUIREMENT`, where it means a regulation.
_DEPEND_VERB = _words("يعتمد", "تعتمد", "يعتمدون", "depend", "depends", "dependent")

#: «مهم» is deliberately absent. «هل يظل مهمًا أكاديميًا؟» (CP16) is a question
#: about a course with no section on file, and «لا تقل لي فقط إن المقرر مهم» (CP20)
#: is a demand for an audit trail; neither is answered by the priority ranking.
_PRIORITY_WORD = _words("اهم", "الاهم", "اولويه", "الاولويه", "priority", "priorities")

#: The one-step question, named by DISTANCE rather than by the course:
#: «وش المقررات المقفلة عندي وما يفصلني عنها إلا مقرر واحد؟» (CP11). It classified
#: GENERAL_AGENT, which mattered beyond routing — the broad policy gate keys on the
#: family, so an unrouted question kept a citation obligation it could not discharge
#: and a student was refused their own prerequisite data. `my_progress` answers it
#: outright: `counts.one_step` is exactly this number.
#:
#: Three words IN ORDER, not one. «يفصل» alone is "separates" in any sense and
#: «واحد» alone is the numeral; it is the sequence separator -> course -> one that
#: names the relation. Measured against the 284-question corpus this fires on zero
#: of them, which is the check that a marker written for one question has not
#: quietly become a pattern.
_SEPARATES = _words("يفصلني", "يفصل", "يفصلها", "يفصلنا", "away", "separates", "separate")

_ONE_WORD = _words("واحد", "واحده", "one", "single")

_WHY_WORD = _words("ليش", "لماذا", "ليه", "السبب", "why")

_LOCKED_WORD = _words("مقفل", "مقفله", "المقفل", "المقفله", "مغلق", "مغلقه", "locked", "blocked")

#: Course code as written in this catalogue: 2-4 Latin letters closed up against
#: 3 digits, optionally carrying the section label («AI352-M1»). No space between
#: the halves — every code in the plan data and in all 50 batch questions is
#: written closed up, and permitting a space turns "at 448", "in 141" and
#: "term 447" into course codes, which is enough to make "does the lab room
#: unlock at 448?" a prerequisite question. Read off the RAW text: folding
#: lowercases, and a code is the one token whose case is data.
_COURSE_CODE = re.compile(r"\b[A-Za-z]{2,4}-?\d{3}\b")


# --------------------------------------------------------------------------
# Normative language. Much stricter than `policy_contract.policy_intent`.
# --------------------------------------------------------------------------

_PERMISSION = _words(
    "مسموح",
    "يجوز",
    "يحق",
    "ممنوع",
    "يسمح",
    "تسمح",
    "محظور",
    "allowed",
    "permitted",
    "prohibited",
    "forbidden",
)

#: «بحد» is deliberately absent, and it is a genuinely separate term rather than
#: an oversight: the stemmer refuses a prefix strip that would leave fewer than
#: three letters, so «الحد», «بحد» and «حد» never meet. TT07 is «ابنِ لي أخف جدول
#: ممكن، بحد أقصى 12 ساعة» — the student's own ceiling on a build, not a question
#: about the regulation's. The interrogative gate on the marker below is the
#: second, independent reason that sentence is not a rule question.
_LIMIT_WORD = _words("الحد", "حد", "السقف", "limit", "maximum", "minimum", "cap")

_LOAD_WORD = _words("الساعات", "ساعات", "للساعات", "ساعه", "الوحدات", "credits", "hours", "load")

#: "Deadline" is normative on its own; «موعد» is not — it is also the word for an
#: appointment, and «متى موعد المحاضرة؟» is a timetable question. So the English
#: noun stands alone and the Arabic one needs its qualifier. Splitting them also
#: fixes the word order: Arabic puts the qualifier first («آخر موعد») and English
#: puts it after the noun ("the deadline for the final withdrawal"), and one
#: ordered pattern cannot hold both.
_DEADLINE_WORD = _words("deadline", "deadlines")

_APPOINTMENT = _words("الموعد", "موعد", "المهله", "date")

_DEADLINE_QUALIFIER = _words("النهائي", "اخر", "الاخير", "final", "last")

_SANCTION = _words(
    "انذار", "عقوبه", "الحرمان", "يترتب", "penalty", "sanction", "dismissal", "probation"
)

#: «شرط»/«شروط» are absent. «هل عنده شرط ساعات مجتازة؟» (CP13) asks what the
#: prerequisite graph holds for one course, and «لازم» is absent for the same
#: reason: «أو لازم نحدد المقرر الفعلي أولًا؟» (CP15) is a question about how the
#: elective placeholder resolves. Both are data questions wearing modal grammar,
#: and `policy_intent` classifies both as regulatory — which is correct for its
#: job (deciding what an answer OWES) and wrong for this one.
_FORMAL_REQUIREMENT = _words("يشترط", "تشترط", "الزامي", "يلزم", "obligatory", "mandatory")

#: Naming the regulation is itself the normative frame. «النظام» is NOT here:
#: «النظام يقول إن أحد المتطلبات غير معروف» and «بيانات النظام» both mean the
#: software, and seven of the 50 batch questions use it that way.
_REGULATION = _words(
    "اللائحه", "اللوائح", "لائحه", "التعليمات", "regulation", "regulations", "bylaw", "bylaws"
)


@dataclass(frozen=True)
class _Marker:
    """One ordered word sequence, optionally gated on a course code.

    The gate exists because «وش يفتح AI331؟» names no noun at all — the object of
    the verb is the code — while «أي مقرر يفتح أكبر عدد من المقررات؟» names the
    noun and no code. Without the gate the unlock verb would have to fire alone,
    and «متى تفتح البوابة؟» would become a prerequisite question.
    """

    words: tuple[frozenset[str], ...]
    requires_course_code: bool = False


def _m(*words: frozenset[str]) -> _Marker:
    return _Marker(words=words)


def _m_code(*words: frozenset[str]) -> _Marker:
    return _Marker(words=words, requires_course_code=True)


#: Ordered sequences per family. A family fires when ANY of its markers matches.
_MARKERS: dict[IntentFamily, tuple[_Marker, ...]] = {
    IntentFamily.PLANNER_REBUILD: (
        _m(_IGNORE_VERB, _CURRENT_REGISTRATION),
        _m(_WITHOUT, _CURRENT_REGISTRATION),
        _m(_FROM_SCRATCH),
    ),
    IntentFamily.PLANNER_SELECT_PREFERRED: (
        _m(_SAVE_VERB, _OPTION),
        _m(_SAVE_VERB, _PREFERRED),
        _m(_OPTION, _PREFERRED),
        _m(_PREFERRED, _OPTION),
        _m(_MY_SCHEDULE, _PREFERRED),
    ),
    IntentFamily.PLANNER_EDIT_DRAFT: (
        _m(_EDIT_WORD, _COURSE_NOUN),
        _m(_COURSE_NOUN, _EDIT_WORD),
        _m(_EDIT_WORD, _OPTION),
        _m(_OPTION, _EDIT_WORD),
    ),
    #: "Show me the alternatives" is deliberately NOT a marker. TT11 is «اعرض لي
    #: جدولي المسجل حاليًا قبل ما تبني أي بدائل» — a read of the registered
    #: timetable whose subordinate clause happens to name alternatives, and a
    #: show-verb-plus-option pattern claimed it. The count is what makes this
    #: family distinct from every other planner request, so the count is required.
    IntentFamily.PLANNER_VIEW_ALTERNATIVES: (
        _m(_MORE_THAN_ONE, _OPTION),
        _m(_OPTION, _MORE_THAN_ONE),
    ),
    IntentFamily.TIMETABLE_CLASH: (_m(_CLASH),),
    IntentFamily.PLANNER_BUILD: (
        _m(_BUILD_VERB, _SCHEDULE),
        _m(_WANT_VERB, _SCHEDULE),
        _m(_ADD_VERB, _SCHEDULE),
    ),
    IntentFamily.CURRENT_TIMETABLE: (
        _m(_SHOW_VERB, _MY_SCHEDULE),
        _m(_ASK_WORD, _MY_SCHEDULE),
        _m(_MY_SCHEDULE, _REGISTERED),
        _m(_SHOW_VERB, _POSSESSIVE, _SCHEDULE),
        _m(_ASK_WORD, _POSSESSIVE, _SCHEDULE),
    ),
    IntentFamily.COURSE_PRIORITY: (
        _m(_PRIORITY_WORD, _COURSE_NOUN),
        _m(_COURSE_NOUN, _PRIORITY_WORD),
        _m(_SEPARATES, _COURSE_NOUN, _ONE_WORD),
    ),
    IntentFamily.COURSE_UNLOCKS: (
        _m(_UNLOCK_VERB, _COURSE_NOUN),
        _m(_COURSE_NOUN, _UNLOCK_VERB),
        _m(_COURSE_NOUN, _WAIT_VERB),
        _m(_WAIT_VERB, _COURSE_NOUN),
        _m(_COURSE_NOUN, _DEPEND_VERB),
        _m(_DEPEND_VERB, _COURSE_NOUN),
        _m_code(_UNLOCK_VERB),
        _m_code(_WAIT_VERB),
        _m_code(_DEPEND_VERB),
    ),
    IntentFamily.COURSE_LOCK_REASON: (
        _m(_WHY_WORD, _LOCKED_WORD),
        _m(_LOCKED_WORD, _WHY_WORD),
    ),
    IntentFamily.POLICY: (
        _m(_PERMISSION),
        _m(_ASK_WORD, _LIMIT_WORD),
        _m(_LIMIT_WORD, _LOAD_WORD, _PERMISSION),
        _m(_DEADLINE_WORD),
        _m(_DEADLINE_QUALIFIER, _APPOINTMENT),
        _m(_SANCTION),
        _m(_FORMAL_REQUIREMENT),
        _m(_REGULATION),
    ),
}

#: A question can only be MIXED across these. Two planner families both firing is
#: a precedence question — «ابنِ جدولًا من الصفر» is a rebuild, full stop — but a
#: build request carrying a permission question is genuinely two demands, and
#: collapsing it to either one drops half the answer.
_DOMAIN: dict[IntentFamily, str] = {
    IntentFamily.PLANNER_REBUILD: "planner",
    IntentFamily.PLANNER_SELECT_PREFERRED: "planner",
    IntentFamily.PLANNER_EDIT_DRAFT: "planner",
    IntentFamily.PLANNER_VIEW_ALTERNATIVES: "planner",
    IntentFamily.TIMETABLE_CLASH: "planner",
    IntentFamily.PLANNER_BUILD: "planner",
    IntentFamily.CURRENT_TIMETABLE: "planner",
    IntentFamily.COURSE_PRIORITY: "course",
    IntentFamily.COURSE_UNLOCKS: "course",
    IntentFamily.COURSE_LOCK_REASON: "course",
    IntentFamily.POLICY: "policy",
}

#: Most specific first. The order is load-bearing in three places:
#:   * REBUILD above BUILD — «ابنِ لي جدولًا جديدًا من الصفر» is both, and the one
#:     that needs a confirmation has to win, or the destructive reading of the
#:     request is the one that gets skipped.
#:   * EDIT_DRAFT above VIEW_ALTERNATIVES — «أعد بناء البدائل بناءً على التعديل»
#:     names both, and the edit is what changed.
#:   * PRIORITY above UNLOCKS — «هل يعتبر عالي الأولوية رغم أن اجتياز مقرر واحد ما
#:     يفتحه؟» contains an unlock verb inside a subordinate clause about ranking.
_PRECEDENCE: tuple[IntentFamily, ...] = (
    IntentFamily.PLANNER_REBUILD,
    IntentFamily.PLANNER_SELECT_PREFERRED,
    IntentFamily.PLANNER_EDIT_DRAFT,
    IntentFamily.PLANNER_VIEW_ALTERNATIVES,
    IntentFamily.TIMETABLE_CLASH,
    IntentFamily.PLANNER_BUILD,
    IntentFamily.CURRENT_TIMETABLE,
    IntentFamily.COURSE_PRIORITY,
    IntentFamily.COURSE_UNLOCKS,
    IntentFamily.COURSE_LOCK_REASON,
    IntentFamily.POLICY,
)


#: The capability that OWNS each family's answer. Declared here so "which tool
#: answers this question" is a fact the server states and a test can read, instead
#: of a hope about how a model reads a description.
#:
#: The three course families were the ones getting it wrong live: a forward-unlock
#: question was answered with `course_prerequisites`, which returns what a course
#: REQUIRES — the reverse relation — and structurally cannot express "what is
#: waiting on it". `why_course_locked` owns both directions for a named course.
#:
#: NOT USED TO WITHHOLD THE OTHER TOOL, and that is a measured decision rather than
#: caution. `course_prerequisites` is REQUIRED by the owner's own batch on CP03
#: («ليش النظام يعتبر AI331 أهم مقرر لي؟», which classifies COURSE_PRIORITY), CP05
#: («هل كل مقرر يذكر AI331 كمتطلب…», COURSE_UNLOCKS) and CP06 (COURSE_UNLOCKS) —
#: the same two families that get it wrong on CP04. A family is too coarse to
#: separate CP04, which FORBIDS the tool, from CP05, which REQUIRES it, so
#: withholding by family would break three questions to fix one. Routing here is
#: therefore advisory and recorded; the forcing mechanism is the description.
#:
#: Families with no entry are absent on purpose: a planner family is answered by a
#: hand-off (`advisor_actions.ROUTED_INTENTS`), not by a capability, and MIXED
#: spans two domains by definition.
CAPABILITY_FOR_FAMILY: dict[IntentFamily, str] = {
    IntentFamily.COURSE_UNLOCKS: "why_course_locked",
    IntentFamily.COURSE_LOCK_REASON: "why_course_locked",
    IntentFamily.COURSE_PRIORITY: "my_progress",
    IntentFamily.CURRENT_TIMETABLE: "my_timetable",
    IntentFamily.TIMETABLE_CLASH: "my_clash_free_sections",
    IntentFamily.PLANNER_BUILD: "build_my_timetable",
    IntentFamily.POLICY: "policy_lookup",
}


def owning_capability(question: str) -> str | None:
    """The capability that should answer this question, or None if no family owns it.

    Side-effect free and offline, like `classify_intent`: the route is a property of
    the string, so it is testable as a table rather than only observable in a live
    batch against a provider.
    """
    return CAPABILITY_FOR_FAMILY.get(classify_intent(question))


def _streams(question: str) -> tuple[list[set[str]], list[str]]:
    """The matching stream and an INDEX-ALIGNED raw stream for negation.

    Both come from one `expand_tokens_ordered` call per raw word, so the "is this
    token kept" predicate is asked once and never restated here. See the module
    docstring for the misalignment this exists to avoid.
    """
    folded = _fold(question)
    matching: list[set[str]] = []
    aligned: list[str] = []
    for word in raw_words_ordered(folded):
        expanded = expand_tokens_ordered(word)
        if not expanded:
            continue
        matching.append(expanded[0])
        aligned.append(word)
    return matching, aligned


def _families(question: str) -> list[IntentFamily]:
    """Every family whose markers fire, in precedence order."""
    matching, aligned = _streams(question)
    if not matching:
        return []
    has_code = bool(_COURSE_CODE.search(str(question or "")))
    hits: list[IntentFamily] = []
    for family in _PRECEDENCE:
        for marker in _MARKERS[family]:
            if marker.requires_course_code and not has_code:
                continue
            if alias_matches(list(marker.words), matching, aligned):
                hits.append(family)
                break
    return hits


def classify_intent(question: str) -> IntentFamily:
    """The one family that owns this question, or GENERAL_AGENT.

    Deterministic and side-effect free: no database, no store, no provider. The
    same string always returns the same family, which is what makes the routing
    testable as a table rather than as a live batch.
    """
    hits = _families(question)
    if not hits:
        return IntentFamily.GENERAL_AGENT
    domains = {_DOMAIN[family] for family in hits}
    if len(domains) > 1:
        return IntentFamily.MIXED
    return hits[0]


def explicit_normative_claim_present(question: str) -> bool:
    """Does the question ask, in so many words, what the RULES permit or require?

    Narrow by construction, and narrower than `policy_contract.policy_intent` by
    design rather than by accident. That function decides what an ANSWER owes once
    retrieval has already run unconditionally, so a false positive there costs a
    citation; its own docstring records it firing on 217 of 284 corpus questions.
    This one is read by a caller that changes what the student is TOLD, so a false
    positive there replaces an answerable answer with a referral.

    The gap is measured on the 50-question batch, where every question is a
    planner or prerequisite question:

        policy_intent                      13/50
        explicit_normative_claim_present    1/50

    The one is TT08, «أريد تسجيل 19 ساعة، هل تستطيع بناء جدول كامل بهذا الحد؟» —
    and TT08 is the only question in the batch whose curated label requires
    `policy_lookup`, because the 19 is the credit ceiling and the ceiling is in
    the لائحة. The twelve it drops are drops on purpose: «بشكل ضروري» read as
    OBLIGATION (TT03), «وش الفرق بين المقررات التي يفتحها AI331 مباشرة» read as
    REGULATORY_DEFINITION (CP06, CP17), «النظام يقول إن أحد المتطلبات غير معروف»
    read as ENTITLEMENT (CP14). Each is correct for `policy_intent`'s job and
    wrong for this one; the markers responsible are named at their definitions.
    """
    matching, aligned = _streams(question)
    if not matching:
        return False
    for marker in _MARKERS[IntentFamily.POLICY]:
        if alias_matches(list(marker.words), matching, aligned):
            return True
    return False


__all__ = [
    "CAPABILITY_FOR_FAMILY",
    "IntentFamily",
    "classify_intent",
    "explicit_normative_claim_present",
    "owning_capability",
]

"""What a finished answer may not say, checked against the facts it was given.

WHY THIS IS NOT A CONTENT FILTER

Every check here runs over free Arabic and English prose, and a false positive does
not soften an answer — it REPLACES it with a referral. So the rule throughout is that
a violation must be provable from STRUCTURED evidence: a course code the tool results
do not contain, a section the payload never mentioned, a claim of a mutation the
server knows it did not perform. Where a check could only be made by reading the
sentence's meaning, it is either anchored on an identifier the prose must also carry,
or it is not implemented and says so.

The identifiers do most of the work. A timetable answer names course codes and
section labels, and those are exactly the things the server already holds as data —
so "did this answer invent a section" is a set comparison rather than a judgement.

WHAT IS DELIBERATELY WEAK, AND WHY IT IS STILL HERE

`SEAT_CLAIM` is the least sound of the legacy checks. The system holds no seat counts at all,
so any affirmative statement about availability is false — but a CORRECT answer
explains that limitation, and it uses the same word to do it. The check therefore
fires only on an affirmative seat phrase with no negator in front of it, and it is
the one most likely to need loosening after live evidence. It is included because the
alternative — no check at all on the one claim the data can never support — is worse,
and because a violation is logged with the phrase that triggered it, so a false
positive is diagnosable rather than mysterious.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from core.services.arabic_text import normalise

#: One code per violation, so a trace can be read without parsing prose.
RETAINED_ADD_CONTRADICTION = "retained_and_added_contradiction"
INVENTED_OPTION_CONTENTS = "invented_option_contents"
UNSUPPORTED_STUDENT_REQUEST = "unsupported_student_request_provenance"
UNSUPPORTED_RECOMMENDATION = "unsupported_recommendation_provenance"
PREREQ_TO_REGISTRATION_LEAP = "prerequisites_read_as_registration_eligibility"
NOT_ON_FILE_TO_NOT_OFFERED = "not_on_file_read_as_not_offered"
SEAT_CLAIM = "seat_availability_claimed"
CREDIT_CAP_CONTRADICTION = "inconsistent_credit_cap"
CLAIMED_PLANNER_MUTATION = "claimed_planner_mutation"
CLAIMED_REGISTRATION_MUTATION = "claimed_registration_mutation"
REQUIRED_EVIDENCE_MISSING = "required_academic_evidence_missing"
REQUESTED_EVIDENCE_OMITTED = "requested_academic_evidence_omitted"
UNSUPPORTED_ACADEMIC_FACT = "unsupported_academic_fact"
EXACT_ACADEMIC_FIGURE_MISMATCH = "exact_academic_figure_mismatch"
POLICY_FIGURE_MISMATCH = "policy_figure_mismatch"

ALL_CHECKS = (
    RETAINED_ADD_CONTRADICTION,
    INVENTED_OPTION_CONTENTS,
    UNSUPPORTED_STUDENT_REQUEST,
    UNSUPPORTED_RECOMMENDATION,
    PREREQ_TO_REGISTRATION_LEAP,
    NOT_ON_FILE_TO_NOT_OFFERED,
    SEAT_CLAIM,
    CREDIT_CAP_CONTRADICTION,
    CLAIMED_PLANNER_MUTATION,
    CLAIMED_REGISTRATION_MUTATION,
    REQUIRED_EVIDENCE_MISSING,
    REQUESTED_EVIDENCE_OMITTED,
    UNSUPPORTED_ACADEMIC_FACT,
    EXACT_ACADEMIC_FIGURE_MISMATCH,
    POLICY_FIGURE_MISMATCH,
)

#: The first V2 evidence postcondition deliberately covers only exact student facts
#: whose truth is already represented as typed capability output.  Widening this set
#: is an explicit product decision: free-form policy explanations and general advice
#: are not made "safe" by guessing their meaning with more regular expressions.
EXACT_FACT_TOOLS = frozenset(
    {
        "my_timetable",
        "recommend_courses",
        "my_progress",
        "graduation_progress",
        "build_timetable_proposal",
        "present_prior_artifact",
        # Both return typed student facts, which is the stated admission rule.
        # my_clash_free_sections is the ONLY tool that lists the sections of a
        # named course, so while it sat outside this set an answer built on it
        # received no checks at all - an invented code, section, time and room
        # in one sentence returned zero violations. That is the open defect
        # where the adviser invents sections and attributes them to "the
        # system". lookup_course likewise returns typed code/name/credits.
        # Free-form tools such as policy_lookup stay out, per the rule above.
        "my_clash_free_sections",
        "lookup_course",
        # The remaining typed student tools.  Each returns exact facts about
        # named courses, and while any of them sat outside this set an answer
        # built on it received no postconditions at all - the same hole the
        # my_clash_free_sections admission closed for section questions.
        "my_plan_by_term",
        "course_prerequisites",
        "course_choice_comparison",
        "feasible_course_replacements",
        "course_eligibility",
        "build_my_timetable",
    }
)

#: Tools deliberately OUTSIDE the exact-fact postconditions, each for a stated
#: reason - never by omission.  A parity test asserts that every student-reachable
#: capability is in exactly one of these two sets, so the next capability added
#: cannot silently land unchecked.
FREE_FORM_TOOLS = frozenset(
    {
        # Identity/context surface; its prose fields are not exact-fact claims.
        "get_student_context",
        # Narrative prerequisite explanation; its typed core is covered by
        # course_prerequisites and my_progress.
        "why_course_locked",
        # Policy text is verified by the citation contract, not by token relations.
        "policy_lookup",
        # A yes/no adviser-assignment fact with no academic tokens.
        "my_advisor",
    }
)

#: Same shape as `advisor_intent._COURSE_CODE`, and deliberately a separate constant:
#: importing the router into a postcondition module would make a text check depend on
#: a routing decision, and these two must be able to disagree.
#:
#: NOT `\b`. Arabic letters are word characters, so there is no word boundary between
#: the conjunction and the code in «AI352 وAI371» — the second code was invisible to
#: every check in this module, which is a provenance check a model could walk past by
#: writing a conjunction. The boundary that matters here is a Latin letter or digit.
_COURSE_CODE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{2,4}-?\d{3}(?![0-9])")

#: «شعبة M2» / "section M2" — a label only counts when a section word introduces it,
#: because "M2" alone is two characters and appears inside ordinary words.
#: A standalone negation word opening or inside the paragraph, word-anchored:
#: the first version used the substring negator list, and folding «الأحد»
#: yields «الاحد», whose substring «لا» made every Sunday sentence read as
#: negated and the whole check inert.  (An earlier comment blamed «إلى»; the
#: real culprit was the day name.)
_NEGATED_SCHEDULE_CLAUSE = re.compile(
    # Deliberately NOT the preposition pair "دون/بدون" - «بدون تعارض» is
    # ordinary timetable prose and one such word laundered a whole fabricated
    # schedule.  Proclitic forms («ولا أستطيع»), «غير», the apology and the
    # referral openers are all honest-refusal shapes.  «لا تنس» ("don't
    # forget") is grammatical negation wrapping an AFFIRMATIVE reminder, so it
    # is carved back out.  The English branch uses real word boundaries; two
    # earlier revisions wrote them as literal backspace bytes (a shell heredoc
    # and then a non-raw string) and every English refusal near a code and a
    # time was refused as a fabrication.
    r"(?:^|\s)(?!لا\s*تنس)(?:[وف]?لا|لن|لم|ليس|ليست|تعذر|يتعذر|غير|أعتذر|اعتذر)\s"
    r"|(?:^|\s)(?:يرجى\s+مراجعة|راجع)\s"
    r"|\b(?:cannot|can't|not|no|unable)\b",
    re.IGNORECASE,
)

#: Administrative-time spans - office hours, registration windows, deadlines.
#: A clock token inside one is not a meeting claim, and «حتى الساعة 23:59»
#: beside a course code is ordinary true policy prose.  The span removes the
#: introducing phrase plus everything to the end of its clause segment.
_ADMIN_TIME_SPAN = re.compile(
    r"(?:ساعات\s+(?:ال)?مكتب\w*|فترة\s+التسجيل|فترة\s+الحذف|آخر\s+موعد"
    r"|حتى\s+الساعة|قبل\s+الساعة|موعد\s+أقصاه"
    r"|office\s+hours|registration\s+(?:window|period)|deadline)"
    r"[^.!?؟;؛\n]{0,60}",
    re.IGNORECASE,
)

#: Every tool whose payload can carry a day, clock time, room, or section
#: meeting.  A clause asserting a meeting needs a row from one of these.
_SCHEDULE_TOOLS = frozenset(
    {
        "my_timetable",
        "build_timetable_proposal",
        "my_clash_free_sections",
        "build_my_timetable",
        # Its alternatives embed proposal-shaped courses/meetings rows.
        "feasible_course_replacements",
    }
)

_SECTION_NEAR = re.compile(
    r"(?:شعب[ةه]?|الشعب[ةه]?|section)\s*[:\-]?\s*([A-Za-z]{1,2}\d{1,3}[A-Za-z]?)\b",
    re.IGNORECASE,
)


def _fold(text: str) -> str:
    return normalise(str(text or "")).lower()


def _phrases(text: str, phrases: tuple[str, ...]) -> str:
    """The first phrase present, folded on both sides. Empty when none is."""
    folded = _fold(text)
    for phrase in phrases:
        if _fold(phrase) in folded:
            return phrase
    return ""


#: A negator immediately before a claim turns it into the correct disclaimer. Checked
#: over a WINDOW rather than the whole answer: «لا» somewhere in a long answer must
#: not license a seat claim three sentences later.
_NEGATORS = ("لا ", "لن ", "ليس", "غير ", "بدون", "no ", "not ", "cannot", "never", "without")
_NEGATION_WINDOW = 60


def _affirmative(text: str, phrase: str) -> bool:
    """Is this phrase used as a claim rather than as a denial?"""
    folded = _fold(text)
    index = folded.find(_fold(phrase))
    if index < 0:
        return False
    window = folded[max(0, index - _NEGATION_WINDOW) : index]
    return not any(_fold(n) in window for n in _NEGATORS)


_SEAT_PHRASES = (
    "مقعد متاح",
    "مقاعد متاحة",
    "فيه مقاعد",
    "يوجد مقاعد",
    "seat is available",
    "seats are available",
    "has room",
    "there is room",
)
_REGISTERED_PHRASES = (
    "سجلت لك",
    "تم تسجيلك",
    "قمت بتسجيل",
    "i registered",
    "you are now registered",
    "i have registered",
    "registration has been updated",
)
_PLANNER_MUTATION_PHRASES = (
    "حفظت الخيار",
    "تم حفظ الخيار",
    "عدّلت المسودة",
    # The production ledger's wording for "the change was applied", none of
    # which the original list matched.  A mutation claim is unconditionally
    # false - the adviser mutates nothing - so breadth here has no
    # false-positive cost on true answers.
    "تم تطبيق التعديل",
    "تم التعديل على جدول",
    "تم تحديث الجدول",
    "تم تحديث جدولك",
    "أضفت المقرر",
    "اضفت المقرر",
    "نفذت طلبك",
    "نفّذت طلبك",
    "i saved the option",
    "i have saved",
    "the draft has been updated",
    "i updated the draft",
    "has been applied",
    "change was applied",
)
_REQUEST_PROVENANCE = ("طلبت", "الذي طلبته", "التي طلبتها", "you requested", "you asked for")
_RECOMMEND_PROVENANCE = (
    "أوصى النظام",
    "اقترح النظام",
    "توصية النظام",
    "the system recommended",
    "the system suggested",
)
_ELIGIBLE_PHRASES = (
    "يمكنك التسجيل",
    "تستطيع التسجيل",
    "مسموح لك بالتسجيل",
    "you can register",
    "you are eligible to register",
    "you may register",
)
_NOT_OFFERED_PHRASES = (
    "لا تطرحه الجامعة",
    "غير مطروح",
    "الجامعة لا تقدم",
    "the university does not offer",
    "is not offered",
)


def _facts_for(tool_results: list[dict[str, Any]] | None, tool: str) -> dict[str, Any]:
    for row in tool_results or []:
        if isinstance(row, dict) and row.get("tool") == tool:
            return row
    return {}


def _codes(rows: Any, key: str = "course_code") -> set[str]:
    if not isinstance(rows, list):
        return set()
    return {str(r.get(key) or "").upper() for r in rows if isinstance(r, dict) and r.get(key)}


#: Each kind of cap-or-load claim, and the words that make a sentence one. A number
#: is only checked when the sentence CLAIMS something about a limit or a total —
#: «AI352 مقرر بثلاث ساعات» states a course's credits and asserts no cap at all.
_CAP_CLAIMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "regulatory",
        (
            "الحد الاعلي",
            "الحد الاقصي",
            "الحد النظامي",
            "اللائحه تسمح",
            "regulatory maximum",
            "maximum permitted",
        ),
    ),
    (
        "advisory",
        (
            "الموصي به",
            "الحد الموصي",
            "ينصح",
            "advisory limit",
            "recommended limit",
            "recommended maximum",
        ),
    ),
    ("requested_cap", ("طلبت", "الحد المطلوب", "بحد اقصي", "requested", "you asked for")),
    (
        "retained",
        ("مسجل حاليا", "الحاليه", "لديك حاليا", "المحتفظ بها", "currently registered", "retained"),
    ),
    ("new", ("المضافه", "الجديده", "newly added", "new courses total")),
    # Bare stems, so «المجموع» and «مجموع الخطة» are both the same claim.
    ("total", ("مجموع", "اجمالي", "proposed total", "total load")),
)

#: A number standing on its own — NOT the digits inside a course code. The offline
#: gate caught this the moment rejected drafts became visible: «والمحتفظ بها: AI1،
#: AI331، CS323» made the check read a retained load of "1", because the first digit
#: after the phrase was the 1 in AI1. Eleven of fifty answers were refused over it.
_STANDALONE_NUMBER = re.compile(r"(?<![A-Za-z0-9])(\d{1,2})(?![0-9])")

#: Where one assertion ends and the next begins: a line break, a list bullet, a
#: sentence terminator, a semicolon. The answer's own structure says this — which is
#: why a character window was the wrong instrument. A window of any size is a guess
#: about how far a claim reaches; a clause boundary is the author saying so.
#:
#: SPLIT ON THE RAW TEXT. `normalise` collapses every newline to a single space, so
#: folding first destroys exactly the structure this depends on — «لديك حاليًا:» and
#: the bulleted course list beneath it fold into one flat line.
_CLAUSE_BREAK = re.compile(r"[\n\r]+|(?<=[.!?؟])\s+|[;؛]|\s+[-*•–—]\s+")

#: A list item, in the bullet forms the models actually emit.
_LIST_ITEM = re.compile(r"^\s*(?:[-*•–—]|\d{1,2}[.)])\s+")

#: A bullet that never got its own line. Zero-width, so the marker survives the split
#: and `_LIST_ITEM` can still recognise the piece as an item.
_INLINE_BULLET = re.compile(r"(?=\s[-*•–—]\s)")

#: Anything shaped like a course reference, INCLUDING the one- and two-digit forms
#: `_COURSE_CODE` deliberately excludes (`AI1`, `GSE1`). Used only to decide whether a
#: number has already been spoken for — for that question `AI1` counts, because
#: «AI1 3 ساعات» is a statement about AI1 no matter how the catalogue spells it.
#: Two spellings: the glued form in any case, and a spaced/hyphenated form
#: restricted to UPPERCASE letters - «CS 202» is a spelling models emit
#: constantly and it escaped the existence floor, but an unrestricted space
#: made ordinary English prose («at 08:00», «no 3») parse as course tokens.
#: Arabic-Indic digits are accepted and folded by _normalise_course_token.
_COURSE_TOKEN = re.compile(
    # A trailing colon disqualifies the token: without that, the spaced form
    # parsed the day-and-hour of every meeting string («SUN 14:30») as a
    # course code SUN14 and the server's own timetable fallback refused
    # itself.
    r"(?<![A-Za-z0-9])"
    r"(?:[A-Za-z]{2,4}-?|[A-Z]{2,4}[ ])"
    r"[0-9٠-٩۰-۹]{1,3}(?![A-Za-z0-9:])"
)

#: Numbers that are already something else, removed before any figure is read.
#: `_fold` erases the punctuation that makes them recognisable — «09:00» becomes
#: «09 00» and «1448/1» becomes «1448 1» — so this runs on the raw clause.
#:
#: Each entry was a live refusal, not a precaution: a clock time made a load phrase
#: claim 9 hours, a citation's «ص 23» became a regulatory cap, and the TERM the
#: answer was planning for — «في الفصل 1448/1» — became a retained load of 1. They
#: are the same defect three times: a course adviser's sentences are full of numbers,
#: and almost none of them are credit hours.
_NOT_A_FIGURE = re.compile(
    r"\d{1,2}\s*:\s*\d{2}"  # clock time
    r"|\d{3,4}\s*/\s*\d{1,2}"  # academic term, 1448/1
    r"|(?:ص|صفحة|p\.|page)\s*\d{1,3}",  # cited page
    re.IGNORECASE,
)

#: A number stated AS CREDIT HOURS. Preferred over any bare number in the clause,
#: because a correct citation puts other numbers in the way: «الحد الأعلى … الدليل
#: الإرشادي … ص 23 … يتراوح بين 12 و19 ساعة» made the check read a regulatory cap of
#: 23 — the page it was citing. The unit is what distinguishes a load figure from a
#: page, an edition, a year or a section number, and an adviser that cites its source
#: will always have those nearby.
_UNIT_FIGURE = re.compile(r"(?<![A-Za-z0-9])(\d{1,2})(?![0-9])\s*(?:ساع|hour|credit)")

#: Where one provenance assertion ends and the next begins. Finer than a clause,
#: because «المقررات التي طلبتها AI352، واقترح النظام CS323» is two attributions in one
#: sentence and a rule that gave each of them both codes would refuse it twice.
_PROVENANCE_BREAK = re.compile(r"[\n\r]+|(?<=[.!?؟])\s+|[;؛,،]|\s+[-*•–—]\s+")


def _clauses(text: str) -> list[str]:
    """The answer's assertions, one per clause, in order."""
    return [c for c in _CLAUSE_BREAK.split(str(text or "")) if c and c.strip()]


def _lines(text: str) -> list[str]:
    """The answer's lines, with run-together bullets separated back out."""
    out: list[str] = []
    for raw in str(text or "").splitlines():
        out.extend(part for part in _INLINE_BULLET.split(raw) if part.strip())
    return out


def _attributed(text: str, phrases: tuple[str, ...]) -> set[str]:
    """The course codes an answer attributes to ONE provenance, and only those.

    The old rule was global: if «طلبت» appeared anywhere, every course code anywhere
    in the answer had to be a student request. A timetable answer legitimately names
    both kinds in the same breath — the courses the student asked for, and the
    sections carried over from the current registration — so a correct TT03/TT07
    answer was refused for naming the second kind.

    An assertion owns what it names. Concretely:

      * the rest of its own line, when it names courses there;
      * otherwise, if it is a heading (ends in a colon and names nothing itself), the
        contiguous list beneath it, stopping at the first line that is not an item.

    The second clause is what makes «المقررات التي طلبتها:» own its bullets without
    reaching the «والشعب التي احتفظت بها» list further down. The first is what stops
    a sentence that already names its courses from adopting an unrelated list below.
    """
    lines = _lines(text)
    claimed: set[str] = set()
    for index, line in enumerate(lines):
        # Within the line, each attribution owns only up to the next one. A single
        # sentence can carry two — «المقررات التي طلبتها AI352، واقترح النظام CS323» —
        # and giving both codes to both provenances refuses a correct answer twice.
        asserted_here = False
        for segment in _PROVENANCE_BREAK.split(line):
            if not segment or not segment.strip():
                continue
            folded = _fold(segment)
            if not any(_fold(phrase) in folded for phrase in phrases):
                continue
            asserted_here = True
            claimed |= {c.upper() for c in _COURSE_CODE.findall(segment)}
        if not asserted_here:
            continue
        # A heading names nothing itself and ends in a colon. Only then does the
        # contiguous list beneath belong to it; a sentence that already named its
        # courses owns those and does not adopt an unrelated list below.
        if _COURSE_CODE.search(line) or not line.rstrip().endswith((":", "：")):
            continue
        for following in lines[index + 1 :]:
            if not _LIST_ITEM.match(following):
                break
            claimed |= {c.upper() for c in _COURSE_CODE.findall(following)}
    return claimed


def _cap_claims(text: str) -> list[tuple[str, int]]:
    """Every (kind, number) this answer asserts about a cap or a load.

    A claim binds to a number in its OWN clause. «لديك حاليًا:» followed by a list of
    courses that each carry their own credit hours asserts no current load at all —
    reading past the line break turned the first course's 3 into a claimed load of 3,
    and refused a correct TT16 answer for contradicting the true 15.
    """
    clauses = [_fold(_NOT_A_FIGURE.sub(" ", c)) for c in _clauses(text)]
    out: list[tuple[str, tuple[int, ...]]] = []
    for kind, phrases in _CAP_CLAIMS:
        figures: tuple[int, ...] = ()
        # EVERY clause, not the first match. An answer commonly names the courses it
        # retained before it names how many hours they are — stopping at the first
        # mention reads the list, not the claim.
        for phrase in phrases:
            folded_phrase = _fold(phrase)
            for clause in clauses:
                hit = clause.find(folded_phrase)
                if hit < 0:
                    continue
                figures = _figures_in(clause[hit + len(folded_phrase) :])
                if figures:
                    break
            if figures:
                break
        if figures:
            out.append((kind, figures))
    return out


def _figures_in(tail: str) -> tuple[int, ...]:
    """The credit figures a claim's clause states, most trustworthy first.

    Numbers stated AS HOURS win, and all of them are returned: «يتراوح بين 12 و19
    ساعة» states a range and both ends are true. Only when the clause names no figure
    in hours at all does the first bare number stand in — that fallback is what keeps
    a terse «the regulatory maximum is 18» checkable.
    """

    def unclaimed(match: re.Match[str]) -> bool:
        # A NUMBER ALREADY SPOKEN FOR. If a course is named between the claim and the
        # figure, the figure is that course's — «لديك حاليًا: AI1 (3 ساعات)، AI331 (4
        # ساعات)» states no current load at all. Enumerating separators could not
        # settle this: the same list arrives bulleted, on separate lines, comma-joined
        # or in parentheses, and each new mark would be one more refusal found in
        # production.
        return not _COURSE_TOKEN.search(tail[: match.start()])

    in_hours = tuple(int(m.group(1)) for m in _UNIT_FIGURE.finditer(tail) if unclaimed(m))
    if in_hours:
        return in_hours
    bare = _STANDALONE_NUMBER.search(tail)
    return (int(bare.group(1)),) if bare and unclaimed(bare) else ()


def _credit_sources(
    timetable: dict[str, Any],
    context: dict[str, Any] | None,
    tool_results: list[dict[str, Any]] | None,
) -> dict[str, set[int]]:
    """The numbers each authority is entitled to state. Empty means "not established".

    An empty set means the check stays silent for that kind: an answer may not be
    refused for citing a limit the turn never retrieved evidence about.
    """
    summary = timetable.get("credit_summary") or {}
    policy = (context or {}).get("recommendation_policy") or {}

    regulatory: set[int] = set()
    for row in tool_results or []:
        if not isinstance(row, dict) or row.get("tool") != "policy_lookup":
            continue
        for bucket in ("direct_policy_evidence", "citable", "policies"):
            for record in row.get(bucket) or []:
                if isinstance(record, dict):
                    blob = " ".join(str(record.get(k) or "") for k in ("text", "statement", "rule"))
                    regulatory |= {int(m.group(1)) for m in re.finditer(r"\b(\d{1,2})\b", blob)}

    def one(value: Any) -> set[int]:
        return {int(value)} if isinstance(value, int) else set()

    return {
        "regulatory": regulatory,
        "advisory": one(policy.get("max_recommended_credit_hours"))
        | one(policy.get("min_recommended_credit_hours")),
        "requested_cap": one(summary.get("new_courses_credit_cap")),
        "retained": one(summary.get("retained_credit_hours")),
        "new": one(summary.get("new_credit_hours")),
        "total": one(summary.get("total_plan_credit_hours")),
    }


def _normalise_course_token(value: Any) -> str:
    """One comparison spelling for an academic course identifier.

    Arabic-Indic digits fold to ASCII FIRST: stripping them instead turned
    «MATH٢٤٣» into "MATH", which is in no catalogue, so every Arabic-Indic
    spelling of a REAL code was refused as an invention.
    """
    folded = str(value or "").translate(_ARABIC_INDIC_DIGITS)
    return re.sub(r"[^A-Za-z0-9]", "", folded).upper()


_ARABIC_INDIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def _successful_exact_fact_rows(
    tool_results: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    return [
        row
        for row in (tool_results or [])
        if isinstance(row, dict)
        and str(row.get("tool") or "") in EXACT_FACT_TOOLS
        and bool(row.get("ok"))
    ]


def _course_codes_in_evidence(value: Any, *, parent_key: str = "") -> set[str]:
    """Course identifiers explicitly carried by an exact-fact payload.

    The traversal is key-aware rather than a regex over serialized JSON.  A year,
    section, reason code, or policy id therefore cannot accidentally become course
    evidence just because its printed representation resembles a catalogue code.
    """
    codes: set[str] = set()
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key or "")
            if key in {"course_code", "code", "candidate_code"} and isinstance(child, str):
                token = _normalise_course_token(child)
                if token and _COURSE_TOKEN.fullmatch(child.strip()):
                    codes.add(token)
                continue
            if key == "course_codes" and isinstance(child, list):
                for item in child:
                    token = _normalise_course_token(item)
                    if token and _COURSE_TOKEN.fullmatch(str(item).strip()):
                        codes.add(token)
                continue
            if key in {
                "missing_course_prerequisites",
                "missing_prerequisites_outside_plan",
                "courses_without_a_time",
            } and isinstance(child, list):
                for item in child:
                    token = _normalise_course_token(item)
                    if token and _COURSE_TOKEN.fullmatch(str(item).strip()):
                        codes.add(token)
                continue
            codes |= _course_codes_in_evidence(child, parent_key=key)
    elif isinstance(value, list):
        if parent_key in _ECHO_KEYS:
            # A suggestion or request-echo list is never course evidence.
            return codes
        for child in value:
            if isinstance(child, str):
                # Payloads such as course_prerequisites carry bare code lists
                # ("prerequisites": ["CS111"]).  A true answer quoting them was
                # invisible to this miner and got refused as an invention.
                # Suggestion keys (a future resolver's did_you_mean) must NOT be
                # routed through here - they are not evidence of anything.
                token = _normalise_course_token(child)
                if token and _COURSE_TOKEN.fullmatch(child.strip()):
                    codes.add(token)
                continue
            codes |= _course_codes_in_evidence(child, parent_key=parent_key)
    return codes


def _course_codes_in_text(text: str) -> set[str]:
    return {_normalise_course_token(match.group(0)) for match in _COURSE_TOKEN.finditer(text or "")}


def _section_labels_in_timetable(rows: list[dict[str, Any]]) -> set[str]:
    labels: set[str] = set()
    for row in rows:
        # Every row given here is already exact-fact evidence; a section label
        # carried by ANY of them (a replacement option, a comparison row) is a
        # recorded fact, and quoting it must not read as an invention.

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"section", "section_label", "from_section", "to_section"}:
                        if child:
                            labels.add(str(child).strip().upper())
                    elif key in {
                        "currently_registered_sections",
                        "expected_plan_sections",
                        "baseline_sections",
                    } and isinstance(child, list):
                        # my_timetable carries bare label strings here, which
                        # the key-based walk below would never reach.
                        # build_timetable_proposal carries full ROWS under the
                        # same key - stringifying those minted garbage labels
                        # AND skipped the walk that would have found their
                        # real "section" keys, so quoting the student's own
                        # baseline read as an invention.
                        for item in child:
                            if isinstance(item, dict | list):
                                visit(item)
                            elif item:
                                labels.add(str(item).strip().upper())
                    else:
                        visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(row)
    return labels


def _normalise_section_label(value: Any) -> str:
    """One spelling for a section label: trimmed, upper, internal spaces removed.

    Issue #54's three classifiers disagree on " M1" precisely because each one
    normalises privately.  This mirrors student_sections.normalize_section_label
    and a parity test holds the two together.  Leading zeros are deliberately
    NOT folded: the audited production F01-F04 were inventions inside a fully
    fabricated table (98 real labels, none zero-padded), so F01 must stay
    distinct from F1 and be flagged, not whitelisted.
    """
    return "".join(str(value or "").split()).upper()


_COHORT_SECTION_TOKEN = re.compile(
    # Three digits: F116-F119 are real labels in the live catalogue.
    r"(?<![A-Za-z0-9])((?:Y[MF]|[MF])\d{1,3})(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# Every other figure reader in this module accepts Arabic-Indic digits; this one
# did not, so «١١:٣٠» sailed past all meeting-time verification while the same
# fabricated time in ASCII was caught.
_CLOCK_TOKEN = re.compile(
    r"(?<![0-9٠-٩])"
    r"([0-2٠-٢]?[0-9٠-٩]:[0-5٠-٥][0-9٠-٩])"
    r"(?![0-9٠-٩])"
)


def _ascii_clock(value: str) -> str:
    """One spelling for a clock time, so an Arabic-Indic one compares equal."""
    return str(value).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))


_ROOM_NEAR = re.compile(
    r"(?:القاع[ةه]|قاع[ةه]|room)\s*[:\-]?\s*([A-Za-z0-9_-]{1,20})",
    re.IGNORECASE,
)
_ACADEMIC_TERM_TOKEN = re.compile(r"(?<![0-9])([0-9]{3,4}\s*/\s*[0-9]{1,2})(?![0-9])")
_DISPLAY_TERM_INDEX = re.compile(
    r"(?:الفصل|الترم|term|semester)\s*(?:رقم\s*)?([0-9]{1,2})(?![0-9])",
    re.IGNORECASE,
)
_INSTRUCTOR_MARKER = re.compile(
    # «المحاضر» is the lecturer; «المحاضرة» is, in this domain, almost always
    # THE LECTURE - «المحاضرة في القاعة M7» named no person, and the optional
    # feminine made every lecture sentence an instructor claim that then
    # failed for lack of a name.  A female lecturer is still caught through
    # «الدكتورة» / «المدرسة» / the explicit English words.
    r"(?:الدكتور(?:ة)?|د\.|المحاضر(?!ة)|المدرس(?:ة)?|instructor|teacher|taught\s+by)",
    re.IGNORECASE,
)


def _values_for_key(value: Any, key_name: str) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) == key_name and child not in (None, ""):
                found.append(str(child).strip())
            else:
                found.extend(_values_for_key(child, key_name))
    elif isinstance(value, list):
        for child in value:
            found.extend(_values_for_key(child, key_name))
    return found


def _unsupported_timetable_values(answer: str, rows: list[dict[str, Any]]) -> bool:
    timetable_rows = [row for row in rows if row.get("tool") in _SCHEDULE_TOOLS]
    if not timetable_rows:
        return False

    sections = _section_labels_in_timetable(timetable_rows)
    room_tokens = {match.group(1).upper() for match in _ROOM_NEAR.finditer(answer or "")}
    for clause in _clauses(answer):
        claimed_sections = {
            match.group(1).upper() for match in _COHORT_SECTION_TOKEN.finditer(clause)
        } - room_tokens
        if claimed_sections - sections and not any(
            _fold(negator) in _fold(clause) for negator in _CLAIM_NEGATORS
        ):
            return True

    evidence_blob = " ".join(
        str(value)
        for row in timetable_rows
        for value in (
            _values_for_key(row, "start")
            + _values_for_key(row, "end")
            + _values_for_key(row, "start_time")
            + _values_for_key(row, "end_time")
            + [item for item in _values_for_key(row, "meetings")]
        )
    )
    # Both sides folded to one spelling: the evidence is always ASCII, the
    # answer may be Arabic-Indic, and comparing the two raw would make every
    # TRUE «٠٩:٠٠» an unsupported value.
    evidence_times = {_ascii_clock(m.group(1)) for m in _CLOCK_TOKEN.finditer(evidence_blob)}
    for clause in _clauses(answer):
        if not (_course_codes_in_text(clause) or _has_words(clause, _TIMETABLE_WORDS)):
            continue
        claimed_times = {_ascii_clock(m.group(1)) for m in _CLOCK_TOKEN.finditer(clause)}
        if claimed_times - evidence_times:
            return True

    evidence_rooms = {
        room.upper() for row in timetable_rows for room in _values_for_key(row, "room") if room
    }
    claimed_rooms = {match.group(1).upper() for match in _ROOM_NEAR.finditer(answer or "")}
    # An evidence set that RECORDS no rooms cannot refute a room claim - the
    # clash-free payload carries meetings without rooms, and refusing every
    # true room mention beside it would be the checker inventing an authority
    # the payload never had.
    if evidence_rooms and claimed_rooms - evidence_rooms:
        return True

    instructor_relations: set[tuple[str, str, str]] = set()

    def collect_instructors(value: Any) -> None:
        if isinstance(value, dict):
            code = _normalise_course_token(value.get("course_code"))
            section = str(value.get("section") or "").strip().upper()
            instructor = str(value.get("instructor") or value.get("instructor_name") or "").strip()
            if code and instructor:
                instructor_relations.add((code, section, instructor.casefold()))
            for child in value.values():
                collect_instructors(child)
        elif isinstance(value, list):
            for child in value:
                collect_instructors(child)

    collect_instructors(timetable_rows)
    instructors = {name for _code, _section, name in instructor_relations}
    for clause in _clauses(answer):
        if not _INSTRUCTOR_MARKER.search(clause):
            continue
        codes = _course_codes_in_text(clause)
        sections = {match.group(1).upper() for match in _SECTION_NEAR.finditer(clause)}
        negated = any(_fold(negator) in _fold(clause) for negator in _CLAIM_NEGATORS)
        if negated:
            if codes and any(
                relation_code in codes and (not sections or relation_section in sections)
                for relation_code, relation_section, _name in instructor_relations
            ):
                return True
            continue
        named = {name for name in instructors if name in clause.casefold()}
        if not named:
            return True
        if codes and not any(
            relation_code in codes
            and relation_name in named
            and (not sections or relation_section in sections)
            for relation_code, relation_section, relation_name in instructor_relations
        ):
            return True
    return False


_CLAIM_NEGATORS = (
    "لا ",
    "ليس",
    "ليست",
    "غير ",
    "ما هو",
    "ماهي",
    "not ",
    "isn't",
    "is not",
    "was not",
    "no ",
)
_ACADEMIC_STATE_WORDS = (
    "مسجل",
    "الجدول",
    "توصي",
    "توصية",
    "موصى",
    "مقترح",
    "مفتوح",
    "متاح",
    "مستوف",
    "مجتاز",
    "متبقي",
    "registered",
    "timetable",
    "schedule",
    "recommend",
    "suggested",
    "open",
    "eligible",
    "passed",
    "remaining",
)


def _unsupported_answer_codes(
    answer: str,
    question: str,
    rows: list[dict[str, Any]],
) -> set[str]:
    """Exact course tokens the draft asserts without current-turn support.

    A code supplied by the student may be repeated while asking for clarification or
    saying it is absent.  It does not become an affirmative record fact merely by
    appearing in the question, so a positive status clause still needs tool evidence.
    """
    evidence_codes = set().union(*(_course_codes_in_evidence(row) for row in rows))
    question_codes = _course_codes_in_text(question)
    unsupported: set[str] = set()
    for clause in _clauses(answer):
        clause_codes = _course_codes_in_text(clause)
        for code in clause_codes - evidence_codes:
            if code not in question_codes:
                unsupported.add(code)
                continue
            folded = _fold(clause)
            affirmative_state = any(_fold(word) in folded for word in _ACADEMIC_STATE_WORDS)
            negated = any(_fold(word) in folded for word in _CLAIM_NEGATORS)
            if affirmative_state and not negated:
                unsupported.add(code)
    return unsupported


_CREDIT_FIGURE = re.compile(
    r"(?<![A-Za-z0-9])([0-9\u0660-\u0669]{1,3}(?:\.[0-9]{1,2})?)"
    r"(?![0-9])\s*(?:ساع\w*|وحد\w*|credit(?:s)?|hour(?:s)?)",
    re.IGNORECASE,
)
_COURSE_COUNT_FIGURE = re.compile(
    r"(?<![A-Za-z0-9])([0-9\u0660-\u0669]{1,3})(?![0-9])\s*"
    r"(?:مقرر\w*|مواد|مادة|course(?:s)?)",
    re.IGNORECASE,
)
_COURSE_COUNT_PAIR = re.compile(
    r"(?<![A-Za-z0-9])([0-9\u0660-\u0669]{1,3})\s*(?:/|من|of)\s*"
    r"([0-9\u0660-\u0669]{1,3})(?![0-9])\s*(?:مقرر\w*|مواد|مادة|course(?:s)?)",
    re.IGNORECASE,
)
_TERM_FIGURE = re.compile(
    r"(?<![A-Za-z0-9])([0-9\u0660-\u0669]{1,2})(?![0-9])\s*"
    # «فصول» is a broken plural - ف-ص-و-ل - so it does not start with
    # «فصل» and `فصل\w*` never matched it. Every fabricated term count
    # written with the plural was therefore invisible to this check.
    r"(?:فصول|فصل\w*|ترم\w*|term(?:s)?|semester(?:s)?)",
    re.IGNORECASE,
)
_QUALIFIED_TERM_FIGURE = re.compile(
    r"(?<![A-Za-z0-9])([0-9\u0660-\u0669]{1,2})(?![0-9])\s*"
    r"(?:additional|extra|more|including|total)\s+"
    r"(?:term(?:s)?|semester(?:s)?)",
    re.IGNORECASE,
)
_PERCENT_FIGURE = re.compile(
    r"(?<![A-Za-z0-9])([0-9\u0660-\u0669]{1,3}(?:\.[0-9]{1,2})?)"
    r"(?![0-9])\s*(?:%|٪|بالمئ\w*|percent)",
    re.IGNORECASE,
)

_TIMETABLE_WORDS = (
    "جدول",
    "مسجل",
    "متوقع",
    "timetable",
    "schedule",
    "registered",
    "expected",
)
_RECOMMENDATION_WORDS = ("توص", "موصى", "اقترح", "recommend", "suggest")
_GRADUATION_WORDS = (
    "تخرج",
    "الخطة",
    "إضاف",
    "اضاف",
    "شامل",
    "graduat",
    "degree plan",
    "completion",
    "additional",
    "including",
)
_PROGRESS_WORDS = ("تقدم", "مفتوح", "محجوب", "مستوف", "progress", "open", "locked")
_REMAINING_WORDS = ("متبقي", "باقي", "remaining", "left")
_EARNED_WORDS = ("مكتسب", "مجتاز", "earned", "passed")
_PERSONAL_GRADUATION_CLAIM_WORDS = (
    "تخرج",
    "متبقي",
    "باقي",
    "مكتسب",
    "إضاف",
    "اضاف",
    "graduat",
    "remaining",
    "earned",
    "additional",
)
_POST_BASELINE_WORDS = (
    "بعد اجتياز مقررات البداية",
    "بعد اجتياز مقررات الفصل المرجعي",
    "بعد اجتياز المقررات المرجعية",
    "بعد نجاح مقررات البداية",
    "after passing the planning baseline",
    "after the planning-baseline courses pass",
    "after the baseline courses pass",
    "once the planning-baseline courses pass",
)


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
    except (TypeError, ValueError):
        return None


def _figures(pattern: re.Pattern[str], clause: str) -> set[float]:
    figures: set[float] = set()
    for match in pattern.finditer(_NOT_A_FIGURE.sub(" ", clause or "")):
        number = _as_number(match.group(1))
        if number is not None:
            figures.add(number)
    return figures


_TAIL_NUMBER = re.compile(r"(?<![A-Za-z0-9])([0-9\u0660-\u0669]{1,3}(?:\.[0-9]{1,2})?)(?![0-9])")


def _figures_after_words(clause: str, words: tuple[str, ...]) -> set[float]:
    cleaned = _COURSE_TOKEN.sub(" ", _NOT_A_FIGURE.sub(" ", clause or ""))
    figures: set[float] = set()
    for word in words:
        for marker in re.finditer(re.escape(word), cleaned, re.IGNORECASE):
            match = _TAIL_NUMBER.search(cleaned[marker.end() :])
            if match:
                figures |= _number_set(match.group(1))
    return figures


def _course_count_figures(clause: str) -> set[float]:
    figures = _figures(_COURSE_COUNT_FIGURE, clause)
    for match in _COURSE_COUNT_PAIR.finditer(_NOT_A_FIGURE.sub(" ", clause or "")):
        figures |= _number_set(match.group(1), match.group(2))
    if not figures and not _figures(_CREDIT_FIGURE, clause):
        figures |= _figures_after_words(clause, ("مقرر", "مواد", "مادة", "course"))
    return figures


def _credit_figures(clause: str) -> set[float]:
    figures = _figures(_CREDIT_FIGURE, clause)
    return figures or _figures_after_words(clause, ("ساع", "وحد", "credit", "hour"))


def _term_figures(clause: str) -> set[float]:
    figures = _figures(_TERM_FIGURE, clause) | _figures(_QUALIFIED_TERM_FIGURE, clause)
    if figures:
        return figures
    # In "each term at 18 credits", 18 belongs to the credit unit, not to the
    # preceding word "term".  Reverse-order extraction is only needed for headings
    # such as "additional terms: 5", so do not reach across another typed unit.
    if _figures(_CREDIT_FIGURE, clause) or _figures(_COURSE_COUNT_FIGURE, clause):
        return set()
    return _figures_after_words(
        clause, ("فصل", "فصول", "ترم", "term", "terms", "semester", "semesters")
    )


def _numeric_values_for_keys(value: Any, keys: set[str]) -> set[float]:
    found: set[float] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) in keys:
                found |= _number_set(child)
            else:
                found |= _numeric_values_for_keys(child, keys)
    elif isinstance(value, list):
        for child in value:
            found |= _numeric_values_for_keys(child, keys)
    return found


def _number_set(*values: Any) -> set[float]:
    out: set[float] = set()
    for value in values:
        number = _as_number(value)
        if number is not None:
            out.add(number)
    return out


def _nested(row: dict[str, Any], *path: str) -> Any:
    value: Any = row
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _sum_known_credits(rows: Any) -> float | None:
    if not isinstance(rows, list) or not rows:
        return 0.0 if isinstance(rows, list) else None
    credits: list[float] = []
    for row in rows:
        value = _as_number((row or {}).get("credit_hours")) if isinstance(row, dict) else None
        if value is None:
            return None
        credits.append(value)
    return sum(credits)


def _has_words(clause: str, words: tuple[str, ...]) -> bool:
    folded = _fold(clause)
    return any(_fold(word) in folded for word in words)


def _course_count_after_term_disambiguation(
    clause: str, count_figures: set[float], term_figures: set[float]
) -> set[float]:
    """Drop a "course count" that is really a TERM count written label-first.

    «فصل المقررات المرجعية ...: 6» labels six TERMS, but the generic
    reverse-order course counter sees «مقررات» before the number and would
    relabel the same six as a plan-course count.

    Both figure checks read the same clause, so this disambiguation has to be
    identical in both or one will reject the very answer the other accepts -
    which is how the server's own deterministic graduation answer came to fail
    its own validator and abstain.
    """
    if term_figures and not (
        _COURSE_COUNT_FIGURE.search(_NOT_A_FIGURE.sub(" ", clause or ""))
        or _COURSE_COUNT_PAIR.search(_NOT_A_FIGURE.sub(" ", clause or ""))
    ):
        return set()
    return count_figures


def _exact_figure_mismatch(answer: str, rows: list[dict[str, Any]]) -> bool:
    """Whether an explicitly typed count/credit/term figure contradicts evidence.

    Figures bind to their unit and subject in the same clause.  This avoids treating
    a course code, room, clock time, Hijri term, or cited page as an academic total.
    """
    timetables = [row for row in rows if row.get("tool") == "my_timetable"]
    recommendations = [row for row in rows if row.get("tool") == "recommend_courses"]
    progress_rows = [row for row in rows if row.get("tool") == "my_progress"]
    graduations = [row for row in rows if row.get("tool") == "graduation_progress"]

    for clause in _clauses(answer):
        credit_figures = _credit_figures(clause)
        term_figures = _term_figures(clause)
        count_figures = _course_count_after_term_disambiguation(
            clause, _course_count_figures(clause), term_figures
        )
        percent_figures = _figures(_PERCENT_FIGURE, clause)

        if credit_figures and _has_words(clause, _TIMETABLE_WORDS) and timetables:
            expected: set[float] = set()
            for row in timetables:
                expected |= _number_set(
                    row.get("registered_credit_hours"), row.get("expected_credit_hours")
                )
            if expected and not credit_figures <= expected:
                return True

        if count_figures and _has_words(clause, _TIMETABLE_WORDS) and timetables:
            expected = set()
            for row in timetables:
                expected |= _number_set(
                    row.get("registered_course_count"), row.get("expected_course_count")
                )
            if expected and not count_figures <= expected:
                return True

        if credit_figures and _has_words(clause, _RECOMMENDATION_WORDS) and recommendations:
            expected = set()
            for row in recommendations:
                recommended_total = _sum_known_credits(row.get("recommendations"))
                expected |= _number_set(recommended_total)
                expected |= _number_set(
                    *(
                        item.get("credit_hours")
                        for item in (row.get("recommendations") or [])
                        if isinstance(item, dict)
                    )
                )
                expected |= _number_set(
                    _nested(row, "credit_policy", "recommended_credit_hours"),
                    _nested(row, "recommendation_policy", "recommended_credit_hours"),
                    _nested(row, "credit_policy", "max_recommended_credit_hours"),
                    _nested(row, "recommendation_policy", "max_recommended_credit_hours"),
                )
            if expected and not credit_figures <= expected:
                return True

        if count_figures and _has_words(clause, _RECOMMENDATION_WORDS) and recommendations:
            expected = set().union(
                *(
                    _number_set(
                        row.get("recommendation_count"), len(row.get("recommendations") or [])
                    )
                    for row in recommendations
                )
            )
            if expected and not count_figures <= expected:
                return True

        if count_figures and _has_words(clause, _PROGRESS_WORDS) and progress_rows:
            expected = set()
            for row in progress_rows:
                counts = row.get("counts") if isinstance(row.get("counts"), dict) else {}
                expected |= _number_set(*counts.values())
            if expected and not count_figures <= expected:
                return True

        graduation_context = (
            _has_words(clause, _GRADUATION_WORDS)
            or bool(credit_figures and _has_words(clause, _REMAINING_WORDS + _EARNED_WORDS))
            or bool(term_figures)
        )
        if (
            not graduations
            and (credit_figures or count_figures or term_figures or percent_figures)
            and _has_words(clause, _PERSONAL_GRADUATION_CLAIM_WORDS)
        ):
            # Comparison and replacement payloads carry their own server-
            # computed graduation-shaped numbers (terms_saved, unlock counts,
            # estimated additional terms per option).  Quoting one of those IS
            # a supported claim even with no graduation_progress row in the
            # turn - refusing them made the server's own safe summaries fail
            # this validator the moment their tools joined EXACT_FACT_TOOLS.
            # The exemption is figures-present-in-evidence, for THIS branch
            # only; label-to-value relations remain _metric_claim_mismatch's
            # job whenever real graduation evidence exists.
            # Graduation-SHAPED keys only, not every numeric leaf: a payload
            # also carries the academic year, the term, ranks and search
            # counters, and "every leaf" meant any small integer in a personal
            # graduation claim was laundered - a 1448/1 row alone supported
            # «يتبقى لتخرجك فصل واحد».
            payload_figures = _numeric_values_for_keys(
                [
                    row
                    for row in rows
                    if row.get("tool")
                    in {"feasible_course_replacements", "course_choice_comparison"}
                ],
                {
                    "terms_saved",
                    "term_difference",
                    "estimated_additional_terms",
                    "estimated_terms_including_planning_baseline",
                    "lower_bound_additional_terms",
                    "lower_bound_terms_including_planning_baseline",
                    "direct_unlock_count",
                    "on_prerequisite_chain_of_count",
                    "sole_remaining_prerequisite_count",
                    "chain_course_count",
                    "clash_free_count",
                    "clashing_count",
                    "sections_on_file",
                    "credit_hours",
                },
            )
            figures = credit_figures | count_figures | term_figures | percent_figures
            if not (payload_figures and figures <= payload_figures):
                return True
        if graduations and graduation_context:
            if count_figures:
                expected = set()
                for row in graduations:
                    expected |= _number_set(
                        row.get("plan_courses_passed"),
                        row.get("plan_courses_total"),
                        row.get("courses_remaining"),
                    )
                if expected and not count_figures <= expected:
                    return True
            if percent_figures:
                expected = set().union(
                    *(_number_set(row.get("percent_complete")) for row in graduations)
                )
                if expected and not percent_figures <= expected:
                    return True
            if credit_figures:
                expected = set()
                gate_clause = _has_words(clause, ("الشرط", "شرط", "استيفاء", "gate", "requirement"))
                for row in graduations:
                    if _has_words(clause, _REMAINING_WORDS):
                        expected |= _number_set(row.get("credits_remaining_in_plan"))
                        # A gate sentence states the gate's OWN shortfall, not
                        # the plan's. _metric_claim_mismatch already carves this
                        # out; without the same carve-out here the server's
                        # blocker line rejected itself.
                        if gate_clause:
                            expected |= _numeric_values_for_keys(
                                row.get("credit_hour_gates")
                                or row.get("unresolved_requirements")
                                or [],
                                {"remaining"},
                            )
                    elif _has_words(clause, _EARNED_WORDS):
                        expected |= _number_set(
                            row.get("credits_earned_registrar"), row.get("passed_credits_in_plan")
                        )
                    else:
                        expected |= _number_set(
                            row.get("planning_baseline_credits"),
                            row.get("max_credits_per_term"),
                        )
                        expected |= _numeric_values_for_keys(
                            row.get("credit_hour_gates")
                            or row.get("unresolved_requirements")
                            or [],
                            {"required", "effective", "effective_in_scenario", "remaining"},
                        )
                if expected and not credit_figures <= expected:
                    return True
            if term_figures:
                expected = set()
                term_keys = (
                    "estimated_additional_terms",
                    "estimated_terms_including_planning_baseline",
                    "lower_bound_additional_terms",
                    "lower_bound_terms_including_planning_baseline",
                )
                for row in graduations:
                    expected |= _number_set(
                        *(row.get(key) for key in term_keys),
                        row.get("terms_estimate"),
                    )
                    # A what-if payload is the SCENARIO report at top level;
                    # the baseline it was compared against lives only under
                    # what_if.baseline.  A comparison answer necessarily
                    # quotes BOTH sides - «1 قبل التغيير، مقابل 2 بعده» -
                    # plus the server-computed difference between them, and
                    # with only the top level mined the deterministic safe
                    # composer's own truthful comparison failed this check
                    # and the turn abstained.
                    what_if = row.get("what_if") if isinstance(row.get("what_if"), dict) else {}
                    sides = [
                        side
                        for side in (what_if.get("baseline"), what_if.get("scenario"))
                        if isinstance(side, dict)
                    ]
                    for side in sides:
                        expected |= _number_set(*(side.get(key) for key in term_keys))
                    if len(sides) == 2:
                        for key in term_keys:
                            before, after = sides[0].get(key), sides[1].get(key)
                            if (
                                isinstance(before, int | float)
                                and isinstance(after, int | float)
                                and not isinstance(before, bool)
                                and not isinstance(after, bool)
                            ):
                                expected.add(float(abs(after - before)))
                if expected and not term_figures <= expected:
                    return True
    return False


def _post_baseline_phase_mismatch(answer: str, rows: list[dict[str, Any]]) -> bool:
    """Reject pre-baseline remaining totals relabelled as post-baseline facts.

    ``courses_remaining`` and ``credits_remaining_in_plan`` currently describe the
    record before the planning-baseline courses are assumed passed.  A matching
    number therefore does not support the sentence "after those courses pass".
    The phase is part of the fact, not decoration around it.
    """
    graduations = [row for row in rows if row.get("tool") == "graduation_progress"]
    if not graduations:
        return False
    clauses = _clauses(answer)
    for index, clause in enumerate(clauses):
        if not _has_words(clause, _POST_BASELINE_WORDS):
            continue
        # A phase heading commonly owns the two bullets immediately beneath it.
        phase_text = " ".join(clauses[index : index + 3])
        course_figures = _course_count_figures(phase_text)
        credit_figures = _credit_figures(phase_text)
        for row in graduations:
            if course_figures:
                explicit = _number_set(row.get("courses_remaining_after_planning_baseline"))
                if not explicit or not course_figures <= explicit:
                    return True
            if credit_figures and _has_words(phase_text, _REMAINING_WORDS):
                explicit = _number_set(row.get("credits_remaining_after_planning_baseline"))
                if not explicit or not credit_figures <= explicit:
                    return True
    return False


_EMPTY_EVIDENCE_WORDS = (
    "لا توجد",
    "لا يوجد",
    "لا تتوفر",
    "لا يظهر",
    "no new",
    "none",
    "empty",
    "not on file",
)


def _tool_signature_present(answer: str, row: dict[str, Any]) -> bool:
    """Has the draft actually surfaced at least one typed fact from this result?"""
    answer_codes = _course_codes_in_text(answer)
    evidence_codes = _course_codes_in_evidence(row)
    if answer_codes & evidence_codes:
        return True

    tool = str(row.get("tool") or "")
    if tool == "my_timetable":
        sections = _section_labels_in_timetable([row])
        named = {match.group(1).upper() for match in _SECTION_NEAR.finditer(answer or "")}
        if sections & named:
            return True
        figures = set().union(
            *(
                _number_set(row.get(key))
                for key in (
                    "registered_course_count",
                    "expected_course_count",
                    "registered_credit_hours",
                    "expected_credit_hours",
                )
            )
        )
        spoken = _course_count_figures(answer) | _credit_figures(answer)
        return bool(figures & spoken)

    if tool == "recommend_courses":
        count = int(row.get("recommendation_count") or 0)
        if count == 0 and any(_fold(word) in _fold(answer) for word in _EMPTY_EVIDENCE_WORDS):
            return True
        figures = _number_set(count, _sum_known_credits(row.get("recommendations")))
        spoken = _course_count_figures(answer) | _credit_figures(answer)
        return bool(figures & spoken and _has_words(answer, _RECOMMENDATION_WORDS))

    if tool == "my_progress":
        counts = row.get("counts") if isinstance(row.get("counts"), dict) else {}
        if not counts and not evidence_codes:
            # Some injected/test clients expose only an opaque summary.  There is
            # no typed fact here on which a deterministic completeness decision can
            # be based, so stay silent instead of pretending the prose was parsed.
            return True
        figures = _number_set(*counts.values())
        spoken = _course_count_figures(answer)
        return bool(figures & spoken and _has_words(answer, _PROGRESS_WORDS))

    if tool == "graduation_progress":
        what_if = row.get("what_if") if isinstance(row.get("what_if"), dict) else None
        if what_if:
            folded_answer = _fold(answer)
            if bool(what_if.get("no_proven_improvement")) and any(
                _fold(phrase) in folded_answer
                for phrase in (
                    "no one-for-one replacement proven",
                    "no replacement proven",
                    "لم يثبت",
                    "لا يوجد استبدال مثبت",
                    "لم تجد المحاكاة استبدالا مثبتا",
                )
            ):
                return True
            rejected_count = _as_number(what_if.get("unproven_blocker_progress_pairs"))
            if rejected_count is not None and _has_words(
                answer, ("استبدال", "عائق", "replacement", "blocker")
            ):
                spoken_numbers = {
                    number
                    for match in _TAIL_NUMBER.finditer(_NOT_A_FIGURE.sub(" ", answer or ""))
                    if (number := _as_number(match.group(1))) is not None
                }
                if rejected_count in spoken_numbers:
                    return True
        figures = _number_set(
            row.get("percent_complete"),
            row.get("credits_remaining_in_plan"),
            row.get("estimated_additional_terms"),
            row.get("estimated_terms_including_planning_baseline"),
            row.get("lower_bound_additional_terms"),
            row.get("lower_bound_terms_including_planning_baseline"),
        )
        spoken = _figures(_PERCENT_FIGURE, answer) | _credit_figures(answer) | _term_figures(answer)
        return bool(figures & spoken and _has_words(answer, _GRADUATION_WORDS))
    if tool == "build_timetable_proposal":
        planner_names: set[str] = set()
        for alternative in row.get("alternatives") or []:
            if not isinstance(alternative, dict):
                continue
            option = str(alternative.get("option") or "").strip()
            if option:
                planner_names.add(option.upper())
            planner_names |= {
                str(name).strip().upper()
                for name in (alternative.get("planner_options") or [])
                if str(name).strip()
            }
        if planner_names and any(name in (answer or "").upper() for name in planner_names):
            return True
        return bool(answer_codes & evidence_codes)
    return False


_SCHEDULE_KIND_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("EXPECTED_PLAN", ("الجدول المتوقع", "خطة متوقعة", "expected timetable", "expected plan")),
    (
        "REGISTERED",
        (
            "الجدول المسجل",
            "الجدول المسجّل",
            "مسجل فعليا",
            "مسجّل فعليًا",
            "registered timetable",
            "actual registration",
        ),
    ),
    ("PROPOSAL", ("الجدول المقترح", "الخيار", "مقترح", "proposal", "option")),
)

_DAY_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("SUN", ("sun", "sunday", "الأحد", "الاحد")),
    ("MON", ("mon", "monday", "الاثنين", "الإثنين")),
    ("TUE", ("tue", "tues", "tuesday", "الثلاثاء")),
    ("WED", ("wed", "wednesday", "الأربعاء", "الاربعاء")),
    ("THU", ("thu", "thur", "thursday", "الخميس")),
    ("FRI", ("fri", "friday", "الجمعة")),
    ("SAT", ("sat", "saturday", "السبت")),
)


def _schedule_kind_from_text(text: str) -> str:
    folded = _fold(text)
    for kind, words in _SCHEDULE_KIND_WORDS:
        if any(_fold(word) in folded for word in words):
            return kind
    return ""


def _canonical_day(value: Any) -> str:
    folded = _fold(str(value or ""))
    for day, aliases in _DAY_ALIASES:
        if any(folded == _fold(alias) or _fold(alias) in folded for alias in aliases):
            return day
    return str(value or "").strip().upper()


def _days_in_text(text: str) -> set[str]:
    folded = _fold(text)
    return {
        day
        for day, aliases in _DAY_ALIASES
        if any(
            re.search(rf"(?<![A-Za-z]){re.escape(_fold(alias))}(?![A-Za-z])", folded)
            for alias in aliases
        )
    }


def _row_schedule_kind(row: dict[str, Any]) -> str:
    kind = str(row.get("schedule_kind") or "").strip().upper()
    if kind:
        return kind
    return "EXPECTED_PLAN" if row.get("is_expected_plan") else "REGISTERED"


def _schedule_evidence(
    rows: list[dict[str, Any]],
) -> tuple[set[tuple[str, str, str]], set[tuple[str, str, str, str, str, str, str]]]:
    """Typed registration and meeting relations, preserving their schedule source."""
    registrations: set[tuple[str, str, str]] = set()
    meetings: set[tuple[str, str, str, str, str, str, str]] = set()
    for row in rows:
        tool = str(row.get("tool") or "")
        if tool == "my_timetable":
            kind = _row_schedule_kind(row)
            registration_rows = [
                item for item in row.get("registrations") or [] if isinstance(item, dict)
            ]
            if not registration_rows:
                registration_rows = [
                    item for item in row.get("meetings") or [] if isinstance(item, dict)
                ]
            for item in registration_rows:
                code = _normalise_course_token(item.get("course_code"))
                section = str(item.get("section") or "").strip().upper()
                if code:
                    registrations.add((kind, code, section))
            for item in row.get("meetings") or []:
                if not isinstance(item, dict):
                    continue
                code = _normalise_course_token(item.get("course_code"))
                section = str(item.get("section") or "").strip().upper()
                if not code:
                    continue
                meetings.add(
                    (
                        kind,
                        code,
                        section,
                        _canonical_day(item.get("day")),
                        str(item.get("start") or item.get("start_time") or "").strip(),
                        str(item.get("end") or item.get("end_time") or "").strip(),
                        str(item.get("room") or "").strip().upper(),
                    )
                )
        elif tool == "build_timetable_proposal":
            # The payload's OWN baseline: «ابني لي جدول جديد» around a full
            # current registration returns zero alternatives and the whole
            # answer speaks about baseline_sections - which this miner never
            # read, so every truthful clock claim about the student's existing
            # week was refused as unsupported and the turn always abstained.
            baseline_kind = str(row.get("baseline_kind") or "").strip().upper() or "REGISTERED"
            for item in row.get("baseline_sections") or []:
                if not isinstance(item, dict):
                    continue
                code = _normalise_course_token(item.get("course_code"))
                section = str(item.get("section") or "").strip().upper()
                if not code:
                    continue
                registrations.add((baseline_kind, code, section))
                for meeting in item.get("meetings") or []:
                    # The provenance helper flattens meetings to the
                    # "SUN 09:00-10:15" spelling; dicts stay handled for the
                    # projector's shape, symmetric with my_clash_free_sections.
                    if isinstance(meeting, dict):
                        day = str(meeting.get("day") or "")
                        start = str(meeting.get("start") or meeting.get("start_time") or "")
                        end = str(meeting.get("end") or meeting.get("end_time") or "")
                    else:
                        parsed = _MEETING_STRING.match(str(meeting or ""))
                        if not parsed:
                            continue
                        day, start, end = parsed.groups()
                    if not (day and start and end):
                        continue
                    meetings.add(
                        (
                            baseline_kind,
                            code,
                            section,
                            _canonical_day(day),
                            start.strip(),
                            end.strip(),
                            str(item.get("room") or "").strip().upper(),
                        )
                    )
            for index, alternative in enumerate(row.get("alternatives") or [], start=1):
                if not isinstance(alternative, dict):
                    continue
                option = str(alternative.get("option") or "").strip().upper()
                if not option:
                    option = (
                        "+".join(
                            str(value).strip().upper()
                            for value in alternative.get("planner_options") or []
                            if str(value).strip()
                        )
                        or f"A{index}"
                    )
                kind = f"PROPOSAL:{option}"
                for item in alternative.get("courses") or []:
                    if not isinstance(item, dict):
                        continue
                    code = _normalise_course_token(item.get("course_code"))
                    section = str(item.get("section") or "").strip().upper()
                    if code:
                        registrations.add((kind, code, section))
                for item in alternative.get("meetings") or []:
                    if not isinstance(item, dict):
                        continue
                    code = _normalise_course_token(item.get("course_code"))
                    section = str(item.get("section") or "").strip().upper()
                    if not code:
                        continue
                    meetings.add(
                        (
                            kind,
                            code,
                            section,
                            _canonical_day(item.get("day")),
                            str(item.get("start") or "").strip(),
                            str(item.get("end") or "").strip(),
                            str(item.get("room") or "").strip().upper(),
                        )
                    )
        elif tool == "my_clash_free_sections":
            # Recorded sections are facts about the CATALOGUE, not about the
            # student's registration, so they carry their own kind.  A row the
            # payload itself marks as the student's current section addition-
            # ally supports a REGISTERED claim.
            #
            # The payload key is "courses" - the executor and the remote
            # projector both spell it that way.  A first version read
            # "results", which exists in no real payload, so the whole arm was
            # dead code and its only test was written to the same wrong shape.
            # "indeterminate" rows are recorded sections too (their meeting
            # data is merely incomplete); quoting one must not read as an
            # invention.
            for result in row.get("courses") or []:
                if not isinstance(result, dict):
                    continue
                code = _normalise_course_token(result.get("course_code"))
                if not code:
                    continue
                for bucket in ("clash_free", "clashing", "indeterminate"):
                    for entry in result.get(bucket) or []:
                        if not isinstance(entry, dict):
                            continue
                        section = _normalise_section_label(entry.get("section"))
                        if not section:
                            continue
                        registrations.add(("SECTIONS", code, section))
                        if entry.get("is_current_section"):
                            registrations.add(("REGISTERED", code, section))
                        for meeting in entry.get("meetings") or []:
                            # Two spellings cross this seam: the executor's
                            # flattened "SUN 09:00-10:15" strings and the
                            # legacy projector's {day, start, end} dicts.
                            if isinstance(meeting, dict):
                                day = str(meeting.get("day") or "")
                                start = str(meeting.get("start") or "")
                                end = str(meeting.get("end") or "")
                            else:
                                parsed = _MEETING_STRING.match(str(meeting or ""))
                                if not parsed:
                                    continue
                                day, start, end = parsed.groups()
                            if not (day and start and end):
                                continue
                            for kind in (
                                ("SECTIONS", "REGISTERED")
                                if entry.get("is_current_section")
                                else ("SECTIONS",)
                            ):
                                meetings.add(
                                    (
                                        kind,
                                        code,
                                        section,
                                        _canonical_day(day),
                                        start.strip(),
                                        end.strip(),
                                        str(entry.get("room") or "").strip().upper(),
                                    )
                                )
    return registrations, meetings


#: "SUN 09:00-10:15" - the flattened meeting spelling my_clash_free_sections uses.
_MEETING_STRING = re.compile(
    r"\s*([A-Za-z]{3,9})\s+([0-2]?[0-9]:[0-5][0-9])\s*-\s*([0-2]?[0-9]:[0-5][0-9])\s*\Z"
)


def _kind_matches(claimed: str, actual: str) -> bool:
    if not claimed:
        return True
    if claimed == "PROPOSAL":
        return actual.startswith("PROPOSAL:")
    return claimed == actual


#: Verb forms as well as nouns. _RECOMMENDATION_WORDS targets «توصية»/«موصى»,
#: which the adviser's own commonest phrasing «يوصي النظام» does not contain -
#: ي-و-ص-ي shares no substring with «توص». A scope reset that cannot see the
#: sentence announcing a recommendation is no reset at all.
_RECOMMENDATION_SCOPE_WORDS = _RECOMMENDATION_WORDS + ("يوص", "نوص", "ننصح", "ينصح")


def _schedule_relation_mismatch(answer: str, rows: list[dict[str, Any]]) -> bool:
    registrations, meetings = _schedule_evidence(rows)
    if not registrations and not meetings:
        return False

    active_kind = ""
    for line in _lines(answer):
        line_kind = _schedule_kind_from_text(line)
        if line_kind:
            active_kind = line_kind
        elif _has_words(line, _RECOMMENDATION_SCOPE_WORDS):
            # A recommendation is a different relation from a registration.
            # A heading such as «في الجدول المسجل لديك» scopes the lines that
            # continue it, but not one that switches to what the system
            # suggests. Inheriting REGISTERED across that switch made the
            # commonest adviser answer shape - registered courses on one line,
            # a recommendation on the next - an unsupported academic fact,
            # even with both tools' evidence present and both lines true.
            active_kind = ""
        for clause in _clauses(line):
            # Attribution windows.  Arabic writes the section BEFORE the course
            # («الشعبة M1 لمقرر AI331»), so a window opening AT the code loses
            # the leading section and a real time attributed to the wrong
            # section reads as clean.  But blindly extending every window
            # backwards leaks the PREVIOUS item's trailing attributes into the
            # next code.  The list comma is the real item boundary: split into
            # comma chunks first; a chunk with one code owns the whole chunk,
            # and only a multi-code comma-free chunk falls back to trailing
            # windows.
            segments: list[tuple[str, str]] = []
            for chunk in re.split(r"[،,]", clause):
                chunk_codes = list(_COURSE_TOKEN.finditer(chunk))
                if not chunk_codes:
                    # "AI1, section M6, Monday 13:00" - English apposition puts
                    # the attributes in codeless chunks that belong to the
                    # code before them.
                    if segments:
                        code, text = segments[-1]
                        segments[-1] = (code, text + " " + chunk)
                    continue
                if len(chunk_codes) == 1:
                    segments.append((_normalise_course_token(chunk_codes[0].group(0)), chunk))
                    continue
                for index, code_match in enumerate(chunk_codes):
                    end = (
                        chunk_codes[index + 1].start()
                        if index + 1 < len(chunk_codes)
                        else len(chunk)
                    )
                    segments.append(
                        (
                            _normalise_course_token(code_match.group(0)),
                            chunk[code_match.start() : end],
                        )
                    )
            for code, segment in segments:
                claimed_kind = _schedule_kind_from_text(segment) or active_kind
                sections = {match.group(1).upper() for match in _SECTION_NEAR.finditer(segment)}
                if not sections:
                    sections = {
                        match.group(1).upper() for match in _COHORT_SECTION_TOKEN.finditer(segment)
                    } - {match.group(1).upper() for match in _ROOM_NEAR.finditer(segment)}
                times = [_ascii_clock(m.group(1)) for m in _CLOCK_TOKEN.finditer(segment)]
                rooms = {match.group(1).upper() for match in _ROOM_NEAR.finditer(segment)}
                days = _days_in_text(segment)

                if sections or claimed_kind:
                    if not any(
                        relation_code == code
                        and (not sections or relation_section in sections)
                        and _kind_matches(claimed_kind, relation_kind)
                        for relation_kind, relation_code, relation_section in registrations
                    ):
                        return True

                if times or rooms or days:
                    matching = []
                    for relation in meetings:
                        kind, relation_code, section, day, start, finish, room = relation
                        if relation_code != code or not _kind_matches(claimed_kind, kind):
                            continue
                        if sections and section not in sections:
                            continue
                        if days and day not in days:
                            continue
                        # room == "" means the payload did not record one -
                        # a blank is no evidence AGAINST any claimed room.
                        if rooms and room and room not in rooms:
                            continue
                        if len(times) >= 2 and (start, finish) != (times[0], times[1]):
                            continue
                        if len(times) == 1 and times[0] not in {start, finish}:
                            continue
                        matching.append(relation)
                    if not matching:
                        return True
    return False


_RECOMMENDATION_BUCKET_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "REGISTERED",
        (
            # Both agreements. «مقرر» is masculine singular and takes «موجود»,
            # while the non-human plural «مقررات» takes «موجودة». The adviser
            # writes both, and with only the feminine listed the whole bucket
            # check was silently inert for every singular masculine sentence.
            "موجودة أصلا في الجدول المسجل",
            "موجوده اصلا في الجدول المسجل",
            "موجود أصلا في الجدول المسجل",
            "موجود اصلا في الجدول المسجل",
            "already in the registered timetable",
            "already registered",
        ),
    ),
    (
        "EXPECTED_PLAN",
        (
            "موجودة أصلا في الجدول المتوقع",
            "موجوده اصلا في الجدول المتوقع",
            "موجود أصلا في الجدول المتوقع",
            "موجود اصلا في الجدول المتوقع",
            "already in the expected",
        ),
    ),
    ("RECOMMENDED", _RECOMMENDATION_WORDS),
)


def _recommendation_bucket(text: str) -> str:
    folded = _fold(text)
    for bucket, words in _RECOMMENDATION_BUCKET_WORDS:
        if any(_fold(word) in folded for word in words):
            return bucket
    return ""


def _recommendation_evidence(
    rows: list[dict[str, Any]],
) -> set[tuple[str, str, float | None]]:
    facts: set[tuple[str, str, float | None]] = set()
    for row in rows:
        if row.get("tool") != "recommend_courses":
            continue
        for bucket, field in (
            ("RECOMMENDED", "recommendations"),
            ("REGISTERED", "already_in_current_timetable"),
            ("EXPECTED_PLAN", "already_in_expected_plan"),
        ):
            for item in row.get(field) or []:
                if not isinstance(item, dict):
                    continue
                code = _normalise_course_token(item.get("course_code") or item.get("code"))
                if code:
                    facts.add(
                        (bucket, code, _as_number(item.get("credit_hours") or item.get("credits")))
                    )
    return facts


def _recommendation_relation_mismatch(answer: str, rows: list[dict[str, Any]]) -> bool:
    facts = _recommendation_evidence(rows)
    if not facts:
        return False
    active_bucket = ""
    for line in _lines(answer):
        active_bucket = _recommendation_bucket(line) or active_bucket
        for clause in _clauses(line):
            bucket = _recommendation_bucket(clause) or active_bucket
            codes = _course_codes_in_text(clause)
            credits = _credit_figures(clause)
            for code in codes:
                matching = [
                    fact for fact in facts if fact[1] == code and (not bucket or fact[0] == bucket)
                ]
                if bucket and not matching:
                    return True
                if len(codes) == 1 and credits and matching:
                    known = {credit for _bucket, _code, credit in matching if credit is not None}
                    if known and not credits <= known:
                        return True
    return False


_PROGRESS_BUCKET_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("OPEN", ("مستوف", "مفتوح", "جاهز", "prerequisite-ready", "open")),
    ("LOCKED", ("محجوب", "مقفل", "blocked", "locked")),
)


def _progress_bucket(text: str) -> str:
    folded = _fold(text)
    for bucket, words in _PROGRESS_BUCKET_WORDS:
        if any(_fold(word) in folded for word in words):
            return bucket
    return ""


def _progress_relation_mismatch(answer: str, rows: list[dict[str, Any]]) -> bool:
    facts: set[tuple[str, str]] = set()
    for row in rows:
        if row.get("tool") != "my_progress":
            continue
        for bucket, field in (
            ("OPEN", "prerequisites_satisfied"),
            ("LOCKED", "prerequisite_blocked"),
        ):
            for item in row.get(field) or []:
                if isinstance(item, dict):
                    code = _normalise_course_token(item.get("code") or item.get("course_code"))
                    if code:
                        facts.add((bucket, code))
    if not facts:
        return False
    active_bucket = ""
    for line in _lines(answer):
        active_bucket = _progress_bucket(line) or active_bucket
        for clause in _clauses(line):
            bucket = _progress_bucket(clause) or active_bucket
            if not bucket:
                continue
            for code in _course_codes_in_text(clause):
                if (bucket, code) not in facts:
                    return True
    return False


def _prior_artifact_relation_mismatch(answer: str, rows: list[dict[str, Any]]) -> bool:
    """Bind a displayed course name to the term/option returned for that view.

    The prior artifact is not free-form conversation history.  It is a normalized
    card projected into one bounded view.  If the draft repeats one of that view's
    exact labels next to a course name, the pair must exist in the same typed row.
    """
    for row in rows:
        if row.get("tool") != "present_prior_artifact" or row.get("view") == "source_artifact":
            continue
        containers = [*(row.get("terms") or []), *(row.get("options") or [])]
        facts: list[tuple[str, str, int | None]] = []
        labels: set[str] = set()
        for container in containers:
            if not isinstance(container, dict):
                continue
            label = str(container.get("term_label") or container.get("option") or "").strip()
            if not label:
                continue
            labels.add(label)
            term_index = _as_number(container.get("term_index"))
            for course in container.get("courses") or []:
                if not isinstance(course, dict):
                    continue
                name = str(course.get("course_name") or "").strip()
                if name:
                    facts.append((name, label, int(term_index) if term_index is not None else None))
        for line in _lines(answer):
            folded_line = _fold(line)
            named = [fact for fact in facts if _fold(fact[0]) in folded_line]
            stated_labels = {label for label in labels if _fold(label) in folded_line}
            if (
                named
                and stated_labels
                and any(label not in stated_labels for _name, label, _index in named)
            ):
                return True
            stated_terms = {
                re.sub(r"\s+", "", match.group(1)) for match in _ACADEMIC_TERM_TOKEN.finditer(line)
            }
            if named and stated_terms:
                expected_terms = {
                    re.sub(r"\s+", "", match.group(1))
                    for _name, label, _index in named
                    for match in _ACADEMIC_TERM_TOKEN.finditer(label)
                }
                if not expected_terms or not stated_terms <= expected_terms:
                    return True
            stated_indices = {int(match.group(1)) for match in _DISPLAY_TERM_INDEX.finditer(line)}
            if named and stated_indices:
                expected_indices = {index for _name, _label, index in named if index is not None}
                if not expected_indices or not stated_indices <= expected_indices:
                    return True
    return False


def _metric_values(rows: list[dict[str, Any]], tool: str, field: str) -> set[float]:
    return set().union(
        *(_number_set(row.get(field)) for row in rows if row.get("tool") == tool and row.get("ok")),
        set(),
    )


def _graduation_metric_values(rows: list[dict[str, Any]], field: str) -> set[float]:
    """A graduation metric's value at top level AND on both what-if sides.

    The what-if payload is the SCENARIO report at top level; the baseline it
    was compared against keeps the same field names under what_if.baseline.
    A comparison answer states the metric once per side - «1 قبل التغيير،
    مقابل 2 بعده» - and may state the server-computed difference between
    them; binding such a clause to the top level alone made the
    deterministic safe composer's own truthful comparison fail and the turn
    abstain.  Fields absent from the side summaries contribute nothing, so
    non-scenario metrics keep their exact single-value binding.
    """
    values = _metric_values(rows, "graduation_progress", field)
    for row in rows:
        if row.get("tool") != "graduation_progress" or not row.get("ok"):
            continue
        what_if = row.get("what_if") if isinstance(row.get("what_if"), dict) else {}
        sides = [
            side
            for side in (what_if.get("baseline"), what_if.get("scenario"))
            if isinstance(side, dict)
        ]
        for side in sides:
            values |= _number_set(side.get(field))
        if len(sides) == 2:
            before, after = sides[0].get(field), sides[1].get(field)
            if (
                isinstance(before, int | float)
                and isinstance(after, int | float)
                and not isinstance(before, bool)
                and not isinstance(after, bool)
            ):
                values.add(float(abs(after - before)))
    return values


def _metric_claim_mismatch(answer: str, rows: list[dict[str, Any]]) -> bool:
    """Bind each number to its named metric instead of accepting a value set."""
    graduation_rows = [row for row in rows if row.get("tool") == "graduation_progress"]
    progress_rows = [row for row in rows if row.get("tool") == "my_progress"]
    for clause in _clauses(answer):
        count_figures = _course_count_figures(clause)
        credit_figures = _credit_figures(clause)
        term_figures = _term_figures(clause)
        percent_figures = _figures(_PERCENT_FIGURE, clause)
        count_figures = _course_count_after_term_disambiguation(clause, count_figures, term_figures)

        if progress_rows and count_figures:
            bucket = _progress_bucket(clause)
            if bucket:
                key = "open" if bucket == "OPEN" else "locked"
                expected = {
                    value
                    for row in progress_rows
                    for value in _number_set(
                        (row.get("counts") or {}).get(key)
                        if isinstance(row.get("counts"), dict)
                        else None
                    )
                }
                if expected and not count_figures <= expected:
                    return True

        if not graduation_rows:
            continue
        pair_matches = list(_COURSE_COUNT_PAIR.finditer(_NOT_A_FIGURE.sub(" ", clause)))
        if pair_matches and _has_words(clause, _GRADUATION_WORDS + _EARNED_WORDS):
            for match in pair_matches:
                passed = _number_set(match.group(1))
                total = _number_set(match.group(2))
                if not passed <= _graduation_metric_values(graduation_rows, "plan_courses_passed"):
                    return True
                if not total <= _graduation_metric_values(graduation_rows, "plan_courses_total"):
                    return True

        if count_figures and not pair_matches:
            field = ""
            if _has_words(clause, _POST_BASELINE_WORDS):
                field = "courses_remaining_after_planning_baseline"
            elif _has_words(clause, _REMAINING_WORDS):
                field = "courses_remaining"
            elif _has_words(clause, _EARNED_WORDS):
                field = "plan_courses_passed"
            elif _has_words(clause, ("إجمالي", "اجمالي", "total", "الخطة")):
                field = "plan_courses_total"
            if field:
                expected = _graduation_metric_values(graduation_rows, field)
                if not expected or not count_figures <= expected:
                    return True

        if credit_figures:
            field = ""
            if _has_words(clause, _REMAINING_WORDS) and _has_words(
                clause, ("الشرط", "شرط", "استيفاء", "gate", "requirement")
            ):
                expected = set().union(
                    *(
                        _numeric_values_for_keys(
                            row.get("credit_hour_gates")
                            or row.get("unresolved_requirements")
                            or [],
                            {"remaining"},
                        )
                        for row in graduation_rows
                    ),
                    set(),
                )
                if expected and not credit_figures <= expected:
                    return True
                continue
            # _POST_BASELINE_WORDS alone, exactly as the count branch above
            # does it.  Adding _REMAINING_WORDS here made the first condition a
            # superset of the second, so credits_remaining_in_plan became
            # unreachable and every true «الساعات المتبقية» sentence was
            # measured against a field graduation_progress never emits - an
            # empty expected set, which fails closed on a correct answer.
            if _has_words(clause, _POST_BASELINE_WORDS):
                field = "credits_remaining_after_planning_baseline"
            elif _has_words(clause, _REMAINING_WORDS):
                field = "credits_remaining_in_plan"
            elif _has_words(clause, ("الحد", "سقف", "cap", "maximum")):
                field = "max_credits_per_term"
            elif _has_words(clause, ("البداية", "المرجع", "baseline")):
                field = "planning_baseline_credits"
            elif _has_words(clause, _EARNED_WORDS):
                field = "credits_earned_registrar"
            if field:
                expected = _graduation_metric_values(graduation_rows, field)
                if not expected or not credit_figures <= expected:
                    return True

        if term_figures:
            complete = any(row.get("simulation_completed") for row in graduation_rows)
            including = _has_words(
                clause,
                (
                    "شامل",
                    "بما فيها",
                    "باحتساب",
                    "الإجمالي",
                    "الاجمالي",
                    "مع فصل البداية",
                    "including",
                    "in total",
                    "total",
                ),
            )
            typed_clause = _NOT_A_FIGURE.sub(" ", clause or "")
            explicit_term_matches = sorted(
                [
                    *_TERM_FIGURE.finditer(typed_clause),
                    *_QUALIFIED_TERM_FIGURE.finditer(typed_clause),
                ],
                key=lambda match: match.start(),
            )
            has_additional_label = _has_words(
                clause,
                ("إضاف", "اضاف", "بعد فصل", "بعد الفصل", "additional", "after"),
            )
            if len(explicit_term_matches) >= 2 and including and has_additional_label:
                additional_field = (
                    "estimated_additional_terms" if complete else "lower_bound_additional_terms"
                )
                including_field = (
                    "estimated_terms_including_planning_baseline"
                    if complete
                    else "lower_bound_terms_including_planning_baseline"
                )
                first = _number_set(explicit_term_matches[0].group(1))
                last = _number_set(explicit_term_matches[-1].group(1))
                if not first <= _graduation_metric_values(graduation_rows, additional_field):
                    return True
                if not last <= _graduation_metric_values(graduation_rows, including_field):
                    return True
                continue
            # The clause's OWN metric words outrank the completeness
            # heuristic: «الحد الأدنى المقدّر لعدد الفصول الإضافية: 1» names
            # the lower bound explicitly, and binding it to the estimate
            # because the simulation completed measured the safe composer's
            # truthful lower-bound line against the wrong field.
            lower_bound_clause = _has_words(
                clause,
                ("الحد الأدنى", "على الأقل", "lower bound", "at least", "minimum"),
            )
            if lower_bound_clause:
                field = (
                    "lower_bound_terms_including_planning_baseline"
                    if including
                    else "lower_bound_additional_terms"
                )
            elif complete:
                field = (
                    "estimated_terms_including_planning_baseline"
                    if including
                    else "estimated_additional_terms"
                )
            else:
                field = (
                    "lower_bound_terms_including_planning_baseline"
                    if including
                    else "lower_bound_additional_terms"
                )
            expected = _graduation_metric_values(graduation_rows, field)
            if not expected or not term_figures <= expected:
                return True

        if percent_figures:
            expected = _graduation_metric_values(graduation_rows, "percent_complete")
            if not expected or not percent_figures <= expected:
                return True
    return False


def _presentation_fulfils_tool(
    presentation: dict[str, Any] | None,
    row: dict[str, Any],
) -> bool:
    if not isinstance(presentation, dict) or not presentation:
        return False
    tool = str(row.get("tool") or "")
    if tool == "graduation_progress" and presentation.get("kind") == "graduation_scenario":
        graph = presentation.get("graph") if isinstance(presentation.get("graph"), dict) else {}
        term_of = {
            _normalise_course_token(code): int(term)
            for code, term in (graph.get("termOf") or {}).items()
            if _normalise_course_token(code) and _as_number(term) is not None
        }
        planned_terms = {
            (_normalise_course_token(code), int(sequence) + 1)
            for term in row.get("term_plan") or []
            if isinstance(term, dict)
            for sequence in [_as_number(term.get("sequence"))]
            if sequence is not None
            for code in term.get("course_codes") or []
            if code
        }
        if planned_terms and not all(term_of.get(code) == band for code, band in planned_terms):
            return False
        what_if = row.get("what_if") if isinstance(row.get("what_if"), dict) else {}
        for evidence_field, presentation_field in (
            ("removed_current_courses", "removed_current_courses"),
            ("added_current_courses", "added_current_courses"),
        ):
            expected = {
                _normalise_course_token(code)
                for code in what_if.get(evidence_field) or []
                if _normalise_course_token(code)
            }
            shown = {
                _normalise_course_token(code)
                for code in presentation.get(presentation_field) or []
                if _normalise_course_token(code)
            }
            if expected != shown:
                return False
        expected_terms = _number_set(row.get("estimated_terms_including_planning_baseline"))
        shown_terms = _number_set(presentation.get("estimated_terms_including_planning_baseline"))
        return not expected_terms or bool(expected_terms & shown_terms)

    if tool == "build_timetable_proposal" and presentation.get("kind") == "timetable_proposals":
        evidence_sections = {
            (
                _normalise_course_token(course.get("course_code")),
                str(course.get("section") or "").strip().upper(),
            )
            for alternative in row.get("alternatives") or []
            if isinstance(alternative, dict)
            for course in alternative.get("courses") or []
            if isinstance(course, dict) and course.get("course_code")
        }
        shown_sections = {
            (
                _normalise_course_token(course.get("course_code")),
                str(course.get("section") or "").strip().upper(),
            )
            for alternative in presentation.get("alternatives") or []
            if isinstance(alternative, dict)
            for course in alternative.get("courses") or []
            if isinstance(course, dict) and course.get("course_code")
        }
        evidence_unplaced = {
            _normalise_course_token(course.get("course_code"))
            for alternative in row.get("alternatives") or []
            if isinstance(alternative, dict)
            for course in alternative.get("unplaced_courses") or []
            if isinstance(course, dict) and course.get("course_code")
        }
        shown_unplaced = {
            _normalise_course_token(course.get("course_code"))
            for alternative in presentation.get("alternatives") or []
            if isinstance(alternative, dict)
            for course in alternative.get("unplaced_courses") or []
            if isinstance(course, dict) and course.get("course_code")
        }
        return bool(evidence_sections or evidence_unplaced) and (
            evidence_sections == shown_sections and evidence_unplaced == shown_unplaced
        )

    if tool == "my_timetable" and presentation.get("kind") == "timetable_proposals":
        evidence_sections = {
            (code, section) for _kind, code, section in _schedule_evidence([row])[0]
        }
        shown_sections = {
            (
                _normalise_course_token(course.get("course_code")),
                str(course.get("section") or "").strip().upper(),
            )
            for course in presentation.get("baseline_sections") or []
            if isinstance(course, dict) and course.get("course_code")
        }
        return bool(evidence_sections) and evidence_sections == shown_sections
    return False


def _tool_contract_complete(
    answer: str,
    row: dict[str, Any],
    *,
    presentation: dict[str, Any] | None,
) -> bool:
    if _presentation_fulfils_tool(presentation, row):
        return True
    tool = str(row.get("tool") or "")
    answer_codes = _course_codes_in_text(answer)

    if tool == "my_timetable":
        registrations, _meetings = _schedule_evidence([row])
        if not registrations:
            return any(_fold(word) in _fold(answer) for word in _EMPTY_EVIDENCE_WORDS)
        return {code for _kind, code, _section in registrations} <= answer_codes

    if tool == "recommend_courses":
        recommended = {
            code
            for bucket, code, _credits in _recommendation_evidence([row])
            if bucket == "RECOMMENDED"
        }
        if not recommended:
            return any(_fold(word) in _fold(answer) for word in _EMPTY_EVIDENCE_WORDS)
        return recommended <= answer_codes

    if tool == "build_timetable_proposal":
        # One alternative, presented WHOLE - its placed courses and the ones
        # it could not place - is a complete, honest answer.  The first
        # contract demanded the union of every code across EVERY alternative,
        # which no natural answer satisfies, and returned False outright for
        # a zero-alternative build - the everyday «ابني لي جدول جديد» around a
        # full registration - making abstention the only possible outcome of
        # a correct build.  Verified against three live production turns on
        # 2026-08-22, all abstained on this branch.
        alternative_codes = [
            {
                _normalise_course_token(course.get("course_code"))
                for course in [
                    *(alternative.get("courses") or []),
                    *(alternative.get("unplaced_courses") or []),
                ]
                if isinstance(course, dict) and course.get("course_code")
            }
            for alternative in row.get("alternatives") or []
            if isinstance(alternative, dict)
        ]
        if any(codes for codes in alternative_codes):
            return any(codes and codes <= answer_codes for codes in alternative_codes)
        # Nothing could be built.  The truthful answer names what blocked it;
        # with nothing blocking either, it says plainly there is nothing to
        # add - the same escape my_timetable and recommend_courses have.
        blocking = {
            _normalise_course_token(entry.get("course_code"))
            # "unplaced_courses" is the key the executor actually writes at
            # top level; a first spelling read "unplaced", which exists only
            # on the planner's INTERNAL result - so the branch inverted, the
            # dishonest silence passed and the truthful disclosure was
            # refused.  The executor-payload key parity test pins this.
            for key in ("constraint_failures", "unplaced_courses")
            for entry in row.get(key) or []
            if isinstance(entry, dict) and entry.get("course_code")
        }
        blocking.discard("")
        if blocking:
            return blocking <= answer_codes
        return any(_fold(word) in _fold(answer) for word in _EMPTY_EVIDENCE_WORDS)

    if tool == "present_prior_artifact":
        if row.get("view") == "source_artifact":
            return False
        names = [
            str(course.get("course_name") or course.get("course_code") or "").strip()
            for container in [*(row.get("terms") or []), *(row.get("options") or [])]
            if isinstance(container, dict)
            for course in container.get("courses") or []
            if isinstance(course, dict)
            and str(course.get("course_name") or course.get("course_code") or "").strip()
        ]
        folded_answer = _fold(answer)
        return bool(names) and all(_fold(name) in folded_answer for name in names)

    return _tool_signature_present(answer, row)


#: Payload keys that ECHO a request argument or carry a suggestion, never a
#: resolved record.  A code under one of these is not proof the code exists -
#: three executors echo the model's own unvalidated argument inside an ok:True
#: envelope, and "look the invented code up, get not-found, assert it anyway"
#: was the audited CS202/CS212 model behaviour.
_ECHO_KEYS = frozenset(
    {
        "query",
        "unknown_query",
        "candidate_courses_considered",
        "requested_remove_course",
        "requested_add_course",
        # NOT did_you_mean: the resolver emits only letter-repairs of codes
        # that EXIST in the catalogue, so a candidate is a legitimate ground
        # for naming the real course in a correction - «لعلك تقصد MATH243» -
        # while the unknown token itself stays inadmissible.  A resolver that
        # ever emits non-catalogue candidates would re-open the laundering
        # hole.  That cannot skew: the resolver's index and the floor are two
        # views of ONE cached load (candidates are keys of the very mapping
        # the floor is built from), and tests/test_course_catalogue.py pins
        # the subset through the resolver's real output.
    }
)

_UNRESOLVED_STATUSES = frozenset({"NOT_ON_FILE", "NOT_MATCHING_STUDENT_PROFILE"})


def _floor_evidence_codes(rows: list[dict[str, Any]]) -> set[str]:
    """Codes an exact-fact payload RESOLVED, never merely repeated.

    course_prerequisites answers a not-found lookup with the model's own
    argument echoed beside "Course not found in any programme plan", and
    my_clash_free_sections keeps a NOT_ON_FILE row for the code it could not
    find.  Mining those as existence evidence let the model launder any
    invention by looking it up first.  Free-form tools contribute nothing
    here: policy_lookup's applies_to must not prove a course exists.
    """
    codes: set[str] = set()
    for row in rows:
        tool = str(row.get("tool") or "")
        pruned: dict[str, Any] = {}
        for key, value in row.items():
            if key in _ECHO_KEYS:
                continue
            if key == "courses" and tool == "my_clash_free_sections":
                value = [
                    entry
                    for entry in (value or [])
                    if isinstance(entry, dict)
                    and str(entry.get("status") or "") not in _UNRESOLVED_STATUSES
                ]
            pruned[key] = value
        if (
            tool == "course_prerequisites"
            and not pruned.get("per_program")
            and not pruned.get("options")
        ):
            # The whole row is a not-found echo of the model's argument.
            continue
        codes |= _course_codes_in_evidence(pruned)
    return codes


def _policy_texts_by_id(tool_results: list[dict[str, Any]] | None) -> dict[str, str]:
    texts: dict[str, str] = {}
    for row in tool_results or []:
        if not isinstance(row, dict) or row.get("tool") != "policy_lookup" or not row.get("ok"):
            continue
        for value in row.values():
            if not isinstance(value, list):
                continue
            for record in value:
                if not isinstance(record, dict):
                    continue
                policy_id = str(record.get("policy_id") or "").strip()
                if not policy_id:
                    continue
                blob = " ".join(
                    str(record.get(field) or "")
                    for field in ("rule", "statement_ar", "caveat", "exceptions")
                )
                if blob.strip():
                    texts[policy_id] = (texts.get(policy_id, "") + " " + blob).strip()
    return texts


def _policy_figure_mismatch(answer: str, tool_results: list[dict[str, Any]] | None) -> bool:
    """A figure asserted BESIDE a policy citation must come from that policy.

    The citation contract proves the cited id EXISTS; nothing proved the
    number beside it was what that policy says - the audited wrong
    summer-hours answer carried a perfectly real citation.  Scoped to clauses
    that name a policy id whose TEXT is in this turn's evidence: the id is the
    claim of provenance, so the named source is the only admissible authority
    for the figures in its clause.

    Bound PER FIGURE KIND, and a kind is bound only when the cited policy's
    own text carries a figure of that kind.  The first spelling bound every
    kind unconditionally: 51 of the 81 policies in the store contain no digit
    at all, so ANY figure beside them flagged, and a credit-cap citation
    flagged the course count and progress percentage sharing its sentence -
    figures the policy never speaks about.  A policy with credit figures
    still binds every credit figure in its clause, so one true figure cannot
    vouch for a second invented one of the same kind.

    Accepted limits, both owned by the claim layer (PR-2b), not by widening
    this clause window: a figure separated from its citation by a clause
    boundary is not bound, and when one clause cites several policies their
    numbers pool - per-id attribution inside one clause is claim extraction,
    not lexical scope.
    """
    texts = _policy_texts_by_id(tool_results)
    if not texts:
        return False
    for clause in _clauses(answer):
        cited = [policy_id for policy_id in texts if policy_id and policy_id in clause]
        if not cited:
            continue
        policy_blob = " ".join(texts[policy_id] for policy_id in cited).translate(
            _ARABIC_INDIC_DIGITS
        )
        # Membership stays against the policy's RAW numbers: a policy that
        # writes its figure without a unit word still vouches for it once the
        # kind itself is bound by a typed occurrence.
        allowed = {float(match.group(1)) for match in _TAIL_NUMBER.finditer(policy_blob)}
        for policy_kind_figures, claimed in (
            (_credit_figures(policy_blob), _credit_figures(clause)),
            (_course_count_figures(policy_blob), _course_count_figures(clause)),
            (_term_figures(policy_blob), _term_figures(clause)),
            (_figures(_PERCENT_FIGURE, policy_blob), _figures(_PERCENT_FIGURE, clause)),
        ):
            if claimed and policy_kind_figures and not claimed <= allowed:
                return True
    return False


def _evidence_postcondition_violations(
    answer: str,
    *,
    question: str,
    tool_results: list[dict[str, Any]] | None,
    required_tools: Iterable[str] | None,
    presentation: dict[str, Any] | None,
    known_course_codes: frozenset[str] | None = None,
) -> list[str]:
    rows = _successful_exact_fact_rows(tool_results)
    required = {str(tool) for tool in (required_tools or ()) if str(tool) in EXACT_FACT_TOOLS}
    present = {str(row.get("tool") or "") for row in rows}
    violations: list[str] = []

    if required - present:
        violations.append(REQUIRED_EVIDENCE_MISSING)

    successful_rows = [
        row for row in (tool_results or []) if isinstance(row, dict) and row.get("ok")
    ]

    # The two checks below deliberately run OUTSIDE the exact-fact rows gate.
    # The audited no-tool fabrications - an invented numbering guide, MATE243,
    # CS202/CS212 - shipped on turns where no exact-fact tool ran, so every
    # gated check was silent and the turn recorded not_applicable.
    if known_course_codes:
        # Existence floor: a course code must exist SOMEWHERE - the catalogue,
        # a code an exact-fact payload RESOLVED this turn, or the student's own
        # question (repeating a code to say it does not exist is how a correct
        # answer refutes it).  Support for a CLAIM about the code remains the
        # relational checks' job.  An empty catalogue disables the floor by
        # design: a freshly-provisioned environment with zero imported courses
        # would otherwise refuse every code, and the DB-verified fact is that
        # the three-table union covers every live code (0 outside it).
        #
        # Room codes are subtracted: 29 real rooms (LIB826, GF3, ...) are
        # course-code shaped and in no catalogue, and «القاعة LIB826 مغلقة»
        # is an ordinary true sentence.
        floor_evidence = _floor_evidence_codes(rows)
        question_codes = _course_codes_in_text(question)
        room_codes = {
            _normalise_course_token(match.group(1)) for match in _ROOM_NEAR.finditer(answer or "")
        }
        flagged = False
        for clause in _clauses(answer):
            for code in _course_codes_in_text(clause):
                if code in known_course_codes or code in floor_evidence or code in room_codes:
                    continue
                if code in question_codes:
                    # The student's own token may be repeated to refute or
                    # question it - but an AFFIRMATIVE claim about it is not
                    # laundered by the student having typed it first.  The
                    # audited MATE243 answer was exactly that: the student
                    # asked about the code and the adviser recommended it.
                    folded = _fold(clause)
                    affirmative = any(_fold(word) in folded for word in _ACADEMIC_STATE_WORDS)
                    negated = any(_fold(word) in folded for word in _CLAIM_NEGATORS)
                    if not affirmative or negated:
                        continue
                violations.append(UNSUPPORTED_ACADEMIC_FACT)
                flagged = True
                break
            if flagged:
                break

    if not any(str(row.get("tool") or "") in _SCHEDULE_TOOLS for row in successful_rows):
        # A meeting-time OR section claim with no schedule-bearing evidence in
        # the whole turn.  This is the structural defence for the fabricated-
        # timetable class built from REAL codes in FALSE relations: with no
        # tool row the relational checks have nothing to compare and stay
        # silent by design, so the obligation attaches to the CLAIM.
        #
        # The scope is the PARAGRAPH, not the clause: a model's own markdown
        # («**الأحد** / AI1 08:00» on separate lines) splits the code and the
        # time into different clauses, and clause scope made the list format a
        # bypass while the same content as one sentence was caught.  Sections
        # carry the same obligation - an invented «W2» needs no time beside it.
        # An explicitly negated paragraph is exempt: refusing while repeating
        # the asked time is honest.  Section tokens the student typed are
        # exempt like question codes on the floor.  Administrative times -
        # office hours, registration windows, deadlines («حتى الساعة 23:59») -
        # are not meeting claims: those spans are removed before the clock
        # search, because a deadline beside a course code is ordinary true
        # policy prose.
        question_sections = {
            _normalise_section_label(match.group(1))
            for match in _COHORT_SECTION_TOKEN.finditer(question or "")
        }
        for paragraph in re.split(r"\n\s*\n", answer or ""):
            if not _course_codes_in_text(paragraph):
                continue
            claimed_sections = (
                {
                    _normalise_section_label(match.group(1))
                    for match in _COHORT_SECTION_TOKEN.finditer(paragraph)
                }
                - {
                    _normalise_section_label(match.group(1))
                    for match in _ROOM_NEAR.finditer(paragraph)
                }
                - question_sections
            )
            without_admin_times = _ADMIN_TIME_SPAN.sub(" ", paragraph)
            if not (_CLOCK_TOKEN.search(without_admin_times) or claimed_sections):
                continue
            if _NEGATED_SCHEDULE_CLAUSE.search(_fold(paragraph)):
                continue
            violations.append(UNSUPPORTED_ACADEMIC_FACT)
            break

    if _policy_figure_mismatch(answer, tool_results):
        violations.append(POLICY_FIGURE_MISMATCH)

    if rows:
        if _unsupported_answer_codes(answer, question, rows):
            violations.append(UNSUPPORTED_ACADEMIC_FACT)

        if _unsupported_timetable_values(answer, rows):
            violations.append(UNSUPPORTED_ACADEMIC_FACT)

        if _schedule_relation_mismatch(answer, rows):
            violations.append(UNSUPPORTED_ACADEMIC_FACT)

        if _recommendation_relation_mismatch(answer, rows):
            violations.append(UNSUPPORTED_ACADEMIC_FACT)

        if _progress_relation_mismatch(answer, rows):
            violations.append(UNSUPPORTED_ACADEMIC_FACT)

        if _prior_artifact_relation_mismatch(answer, rows):
            violations.append(UNSUPPORTED_ACADEMIC_FACT)

        timetable_sections = {
            _normalise_section_label(label) for label in _section_labels_in_timetable(rows)
        }

        def sections_in(text: str) -> set[str]:
            # Both spellings: labels introduced by a section word AND bare
            # cohort tokens.  F01-F04 shipped as bare table cells, which the
            # section-word pattern never saw.  Room tokens are subtracted so
            # "القاعة M7" stays a room.
            named = {
                _normalise_section_label(match.group(1))
                for match in _SECTION_NEAR.finditer(text or "")
            }
            named |= {
                _normalise_section_label(match.group(1))
                for match in _COHORT_SECTION_TOKEN.finditer(text or "")
            } - {
                _normalise_section_label(match.group(1))
                for match in _ROOM_NEAR.finditer(text or "")
            }
            return named

        if sections_in(answer) - timetable_sections - sections_in(question):
            violations.append(UNSUPPORTED_ACADEMIC_FACT)

        if _exact_figure_mismatch(answer, rows):
            violations.append(EXACT_ACADEMIC_FIGURE_MISMATCH)

        if _post_baseline_phase_mismatch(answer, rows):
            violations.append(EXACT_ACADEMIC_FIGURE_MISMATCH)

        if _metric_claim_mismatch(answer, rows):
            violations.append(EXACT_ACADEMIC_FIGURE_MISMATCH)

        # A selected exact-fact tool must contribute something to the final answer;
        # otherwise "the tool ran" launders an evidence-free response.  When a
        # high-confidence request names one owning tool, require that tool's own
        # signature rather than accepting an unrelated capability's number.
        required_rows: list[dict[str, Any]] = []
        for tool in required:
            matching = [row for row in rows if row.get("tool") == tool]
            if matching:
                required_rows.append(matching[-1])
        if required_rows and not all(
            _tool_contract_complete(answer, row, presentation=presentation) for row in required_rows
        ):
            violations.append(REQUESTED_EVIDENCE_OMITTED)

    return list(dict.fromkeys(violations))


def check_answer(
    answer: str,
    *,
    tool_results: list[dict[str, Any]] | None = None,
    action: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    question: str | None = None,
    required_tools: Iterable[str] | None = None,
    presentation: dict[str, Any] | None = None,
    known_course_codes: frozenset[str] | None = None,
) -> list[str]:
    """Every consistency violation in this answer, as codes. Empty means shippable.

    `tool_results` is the evidence the answer was built from. With no timetable in it
    most checks have nothing to compare against and stay silent — which is correct:
    an answer that made no timetable claim cannot contradict one.
    """
    text = str(answer or "")
    if not text.strip():
        return []
    found: list[str] = []

    timetable = _facts_for(tool_results, "build_my_timetable")
    retained = _codes(timetable.get("retained_sections"))
    new = _codes(timetable.get("new_sections"))
    requested = _codes(timetable.get("student_requested_courses"))
    recommended = _codes(timetable.get("system_recommended_courses"))
    unplaced = _codes(timetable.get("unplaced_courses"))
    known_codes = retained | new | requested | recommended | unplaced

    # 1. A course cannot be kept and added at once unless the payload said how — a
    #    replacement names both ends, and that is the only shape that may.
    if timetable:
        replaced = _codes(timetable.get("section_replacements"))
        both = (retained & new) - replaced
        if both:
            found.append(RETAINED_ADD_CONTRADICTION)

    # 2. Sections the payload never held. The comparison is on LABELS introduced by a
    #    section word, so an ordinary two-character run cannot be mistaken for one.
    if timetable:
        payload_sections = {
            str(r.get("section") or "").upper()
            for key in ("retained_sections", "new_sections", "fixed_sections")
            for r in (timetable.get(key) or [])
            if isinstance(r, dict) and r.get("section")
        }
        named = {m.group(1).upper() for m in _SECTION_NEAR.finditer(text)}
        if named and payload_sections and not named <= payload_sections:
            found.append(INVENTED_OPTION_CONTENTS)

    # 3/4. Provenance, bound to the assertion that makes it — not to the answer.
    #      Anchored on BOTH a provenance phrase and a course code, so neither alone
    #      can trigger it.
    answer_codes = {c.upper() for c in _COURSE_CODE.findall(text)}
    if timetable and answer_codes:
        if (_attributed(text, _REQUEST_PROVENANCE) & known_codes) - requested:
            found.append(UNSUPPORTED_STUDENT_REQUEST)
        if (_attributed(text, _RECOMMEND_PROVENANCE) & known_codes) - recommended:
            found.append(UNSUPPORTED_RECOMMENDATION)

    # 5. Prerequisite readiness is not permission, and the payloads say so themselves.
    progress = _facts_for(tool_results, "my_progress") or _facts_for(
        tool_results, "why_course_locked"
    )
    if progress:
        phrase = _phrases(text, _ELIGIBLE_PHRASES)
        if phrase and _affirmative(text, phrase):
            found.append(PREREQ_TO_REGISTRATION_LEAP)

    # 6. NOT_ON_FILE means this system holds no section, never that none exists.
    if any(
        str(r.get("reason_code") or "") == "NOT_ON_FILE"
        for r in (timetable.get("unplaced_courses") or [])
        if isinstance(r, dict)
    ):
        phrase = _phrases(text, _NOT_OFFERED_PHRASES)
        if phrase and _affirmative(text, phrase):
            found.append(NOT_ON_FILE_TO_NOT_OFFERED)

    # 7. There are no seat counts anywhere in the data. See the module docstring for
    #    why this is the weakest of the legacy checks.
    phrase = _phrases(text, _SEAT_PHRASES)
    if phrase and _affirmative(text, phrase):
        found.append(SEAT_CLAIM)

    # 8. CAP AND LOAD claims only, each against the source that owns it.
    #
    #    Two live refusals of TT08 taught this. The first version compared every
    #    number beside a credit word against `credit_summary`; source-awareness fixed
    #    the authorities but not the SCOPE, so «AI352 مقرر بثلاث ساعات» — an ordinary
    #    true sentence about a course — was still a "cap contradiction", because 3 is
    #    not 15, 18 or 19.
    #
    #    A course's own credit hours are not a claim about a limit or a load, and this
    #    check has no business reading them. It now looks only for sentences that
    #    ASSERT a cap or a total, and compares each to the one source entitled to say
    #    it. Adding every course's credits to the allowed set would have passed TT08
    #    and made the check meaningless — «الحد الأعلى 3 ساعات» would sail through.
    for claim, figures in _cap_claims(text):
        expected = _credit_sources(timetable, context, tool_results).get(claim)
        # ANY of the clause's figures may satisfy the claim. A range is two true
        # numbers about one limit, and requiring the first to match would refuse the
        # more informative answer.
        if expected and not (set(figures) & expected):
            found.append(CREDIT_CAP_CONTRADICTION)
            break

    # Legacy checks 9/10. The adviser mutates nothing. A hand-off is an OFFER, so an answer that
    #       carries one may not also report the thing as done.
    phrase = _phrases(text, _PLANNER_MUTATION_PHRASES)
    if phrase and _affirmative(text, phrase):
        found.append(CLAIMED_PLANNER_MUTATION)
    phrase = _phrases(text, _REGISTERED_PHRASES)
    if phrase and _affirmative(text, phrase):
        found.append(CLAIMED_REGISTRATION_MUTATION)

    # A caller opts into the evidence-to-answer postcondition by supplying the
    # current question.  Both adviser paths now do - the legacy agent passes
    # question= and the catalogue too, so a preview or rollback environment
    # running the legacy path keeps the same floor.
    if question is not None:
        found.extend(
            _evidence_postcondition_violations(
                text,
                question=question,
                tool_results=tool_results,
                required_tools=required_tools,
                presentation=presentation,
                known_course_codes=known_course_codes,
            )
        )

    return list(dict.fromkeys(found))


__all__ = ["ALL_CHECKS", "EXACT_FACT_TOOLS", "check_answer", *ALL_CHECKS]

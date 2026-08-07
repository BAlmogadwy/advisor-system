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

`SEAT_CLAIM` is the least sound of the ten. The system holds no seat counts at all,
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
)

#: Same shape as `advisor_intent._COURSE_CODE`, and deliberately a separate constant:
#: importing the router into a postcondition module would make a text check depend on
#: a routing decision, and these two must be able to disagree.
_COURSE_CODE = re.compile(r"\b[A-Za-z]{2,4}-?\d{3}\b")

#: «شعبة M2» / "section M2" — a label only counts when a section word introduces it,
#: because "M2" alone is two characters and appears inside ordinary words.
_SECTION_NEAR = re.compile(
    r"(?:شعب[ةه]?|الشعب[ةه]?|section)\s*[:\-]?\s*([A-Za-z]{1,2}\d{1,2}[A-Za-z]?)\b",
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
    "i saved the option",
    "i have saved",
    "the draft has been updated",
    "i updated the draft",
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

#: How far after the phrase a number still belongs to it.
_CLAIM_WINDOW = 60

#: A number standing on its own — NOT the digits inside a course code. The offline
#: gate caught this the moment rejected drafts became visible: «والمحتفظ بها: AI1،
#: AI331، CS323» made the check read a retained load of "1", because the first digit
#: after the phrase was the 1 in AI1. Eleven of fifty answers were refused over it.
_STANDALONE_NUMBER = re.compile(r"(?<![A-Za-z0-9])(\d{1,2})(?![0-9])")


def _cap_claims(text: str) -> list[tuple[str, int]]:
    """Every (kind, number) this answer asserts about a cap or a load."""
    folded = _fold(text)
    out: list[tuple[str, int]] = []
    for kind, phrases in _CAP_CLAIMS:
        figure = None
        # EVERY occurrence, not the first. An answer commonly names the courses it
        # retained before it names how many hours they are — stopping at the first
        # mention reads the list, not the claim.
        for phrase in phrases:
            for hit in re.finditer(re.escape(_fold(phrase)), folded):
                window = folded[hit.end() : hit.end() + _CLAIM_WINDOW]
                match = _STANDALONE_NUMBER.search(window)
                if match:
                    figure = int(match.group(1))
                    break
            if figure is not None:
                break
        if figure is not None:
            out.append((kind, figure))
    return out


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


def check_answer(
    answer: str,
    *,
    tool_results: list[dict[str, Any]] | None = None,
    action: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
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

    # 3/4. Provenance. Anchored on BOTH a provenance phrase and a course code the
    #      answer names, so neither alone can trigger it.
    answer_codes = {c.upper() for c in _COURSE_CODE.findall(text)}
    if timetable and answer_codes:
        if _phrases(text, _REQUEST_PROVENANCE) and (answer_codes & known_codes) - requested:
            found.append(UNSUPPORTED_STUDENT_REQUEST)
        if _phrases(text, _RECOMMEND_PROVENANCE) and (answer_codes & known_codes) - recommended:
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
    #    why this is the weakest of the ten.
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
    for claim, figure in _cap_claims(text):
        expected = _credit_sources(timetable, context, tool_results).get(claim)
        if expected and figure not in expected:
            found.append(CREDIT_CAP_CONTRADICTION)
            break

    # 9/10. The adviser mutates nothing. A hand-off is an OFFER, so an answer that
    #       carries one may not also report the thing as done.
    phrase = _phrases(text, _PLANNER_MUTATION_PHRASES)
    if phrase and _affirmative(text, phrase):
        found.append(CLAIMED_PLANNER_MUTATION)
    phrase = _phrases(text, _REGISTERED_PHRASES)
    if phrase and _affirmative(text, phrase):
        found.append(CLAIMED_REGISTRATION_MUTATION)

    return found


__all__ = ["ALL_CHECKS", "check_answer", *ALL_CHECKS]

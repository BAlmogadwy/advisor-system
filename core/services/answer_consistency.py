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


#: «الحد الأعلى» / «الحد الأقصى» — a claim about what the REGULATION permits, as
#: opposed to what the student asked for or what the recommender advises. All three
#: are different numbers and one answer may state them all; what it may not do is
#: attribute one to the wrong authority.
_REGULATORY_MAX = (
    "الحد الاعلي",
    "الحد الاقصي",
    "الحد الأعلى",
    "الحد الأقصى",
    "الحد النظامي",
    "regulatory maximum",
    "maximum permitted",
)


def _regulatory_claim(text: str) -> int | None:
    """The number this sentence presents as the REGULATION's ceiling, if any."""
    folded = _fold(text)
    for phrase in _REGULATORY_MAX:
        index = folded.find(_fold(phrase))
        if index < 0:
            continue
        match = re.search(r"(\d{1,2})", folded[index : index + 80])
        if match:
            return int(match.group(1))
    return None


def _credit_figures(
    timetable: dict[str, Any],
    context: dict[str, Any] | None,
    tool_results: list[dict[str, Any]] | None,
) -> dict[str, set[int]]:
    """Every credit number the answer may state, grouped by the source that owns it."""
    summary = timetable.get("credit_summary") or {}
    planning = {
        int(v)
        for v in (
            summary.get("new_courses_credit_cap"),
            summary.get("new_credit_hours"),
            summary.get("retained_credit_hours"),
            summary.get("total_plan_credit_hours"),
        )
        if isinstance(v, int)
    }

    policy = (context or {}).get("recommendation_policy") or {}
    advisory = {
        int(v)
        for v in (
            policy.get("max_recommended_credit_hours"),
            policy.get("min_recommended_credit_hours"),
        )
        if isinstance(v, int)
    }

    # Regulated figures come from the records actually retrieved this turn: a number
    # is only "the regulation's" if a governing record says so.
    regulatory: set[int] = set()
    for row in tool_results or []:
        if not isinstance(row, dict) or row.get("tool") != "policy_lookup":
            continue
        for bucket in ("direct_policy_evidence", "citable", "policies"):
            for record in row.get(bucket) or []:
                if isinstance(record, dict):
                    blob = " ".join(str(record.get(k) or "") for k in ("text", "statement", "rule"))
                    regulatory |= {int(m.group(1)) for m in re.finditer(r"\b(\d{1,2})\b", blob)}

    return {
        "planning": planning,
        "advisory": advisory,
        "regulatory": regulatory,
        "any": planning | advisory | regulatory,
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

    # 8. Credit figures, checked against the source that OWNS each kind.
    #
    #    The first version compared every number beside a credit word against the
    #    timetable payload alone, and it refused TT08 on the live canary — the
    #    question this whole branch exists for. A correct answer to «أريد تسجيل 19
    #    ساعة» states three true numbers from three places: 19 requested, 18 advised
    #    by `recommendation_policy`, 19 permitted by the لائحة, beside 15 already
    #    held. Only one of those is in `credit_summary`, so the check called the rest
    #    contradictions.
    #
    #    Exempting "numbers with a citation" would be the easy repair and the wrong
    #    one: it lets «الحد الأعلى 18 ساعة» through, which is false, and misattributing
    #    a figure to the regulation is exactly what the citation contract exists to
    #    stop. So a figure must come from SOME source, and a figure presented as the
    #    REGULATORY ceiling must come from the regulatory one.
    figures = _credit_figures(timetable, context, tool_results)
    stated = {int(m.group(1)) for m in re.finditer(r"(\d{1,2})\s*(?:ساع|hour|credit)", _fold(text))}
    #    THE TIMETABLE GUARD IS LOAD-BEARING, and dropping it while making the check
    #    source-aware was a second false positive, caught by the suite rather than
    #    live: «مقرر AI221 بثلاث ساعات، والحد 19 ساعة معتمدة، صفحة 28» is a correct
    #    answer to «وش عندي بكرة الأحد؟», and that turn built no timetable for its
    #    figures to contradict. A credit number in ordinary prose is not a claim
    #    about a plan the turn never produced.
    if timetable and stated and figures["any"] and not stated <= figures["any"]:
        found.append(CREDIT_CAP_CONTRADICTION)
    else:
        claimed = _regulatory_claim(text)
        if claimed is not None and figures["regulatory"] and claimed not in figures["regulatory"]:
            found.append(CREDIT_CAP_CONTRADICTION)

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

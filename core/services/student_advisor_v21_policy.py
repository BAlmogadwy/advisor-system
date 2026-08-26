"""Closed semantic-policy checks shared by V2.1 runtime and offline evals.

This module deliberately has no Django or adviser-runtime dependency.  It does
not route general natural language and it never rewrites a provider plan.  The
four detectors below recognize only narrow, high-confidence request families
whose typed plan shape is part of the V2.1 product contract.  A mismatch is
reported with a closed identifier so the caller can request one bounded repair
or fail closed before executing evidence capabilities.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any


class SemanticPolicyId(str, Enum):
    """Closed V2.1 request-family policies that are safe to expose in repair."""

    STANDALONE_COREQUISITE_UNSUPPORTED = "standalone_corequisite_unsupported"
    SINGLE_COURSE_CHOICE_BALANCED = "single_course_choice_balanced"
    PLAIN_AVAILABLE_COURSES_ONLY = "plain_available_courses_only"
    PINNED_COURSE_ADDITION_BALANCED = "pinned_course_addition_balanced"


SEMANTIC_POLICY_IDS: frozenset[str] = frozenset(item.value for item in SemanticPolicyId)

_ARABIC_FOLD = str.maketrans(
    {
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ٱ": "ا",
        "ى": "ي",
        "ة": "ه",
        "ؤ": "و",
        "ئ": "ي",
        "ـ": "",
    }
)
_INLINE_CODE = re.compile(r"`+")
_NON_WORD = re.compile(r"[^\w\-]+", re.UNICODE)
_SPACE = re.compile(r"\s+")
_COURSE_CODE = re.compile(r"\b[A-Z]{2,8}[0-9]{2,4}[A-Z]?\b", re.IGNORECASE)
_HISTORICAL = re.compile(
    r"\b(?:last|previous|prior)\s+(?:term|semester)\b|"
    r"(?:الترم|الفصل)\s+(?:الماضي|السابق)|سابقا"
)
_NOT_ASKING = re.compile(
    r"\b(?:i\s+am\s+not|i'm\s+not|not)\s+(?:asking|looking)\b|"
    r"(?:مو|لست|ماني)\s+(?:قاعد\s+)?(?:اسال|ابي)|لا\s+اسال"
)
_COREQUISITE_DEFINITION = re.compile(
    r"\b(?:what\s+does|what\s+is|define|definition|meaning\s+of)\b.{0,30}"
    r"\b(?:corequisite|co\s*-?\s*requisite)\b|"
    r"(?:وش|ايش|ما)\s+(?:يعني|معنى|معني)\s+متطلب\s+متزامن|تعريف\s+متطلب\s+متزامن"
)
_QUOTED_COREQUISITE = re.compile(
    r"[\"'“”‘’«»]\s*(?:corequisite|co\s*-?\s*requisite|متطلب\s+متزامن)"
    r"\s*[\"'“”‘’«»]",
    re.IGNORECASE,
)
_EXPLICIT_ADDITION_CRITERION = re.compile(
    r"\b(?:graduat\w*|unlock\w*|priorit\w*|important|highest\s+impact|"
    r"clash\w*|conflict\w*|timetable\s+fit|schedule\s+fit|"
    r"exactly\s+\d+\s+credits?|\d+\s*-?\s*credit)\b|"
    r"(?:تخرج|يفتح|اهم|اولوي|تعارض|بدون\s+تعارض|ملاءم|"
    r"\d+\s*(?:ساعه|ساعات))"
)
_PRIORITY_CUE = re.compile(
    r"\b(?:best|important|priority|prioritize|rank|top)\b|"
    r"(?:افضل|مهم|اهم|اولوي|رتب|ترتيب)"
)
_ADDITION_SELECTION_CUE = re.compile(
    r"\b(?:add|adding|choose|pick|recommend|which\s+(?:one|course\b))\b|"
    r"(?:اضيف|نضيف|اختار|تنصح|ارشح|ماده\s+وحده|مقرر\s+واحد)"
)
_SINGLE_COURSE_DISQUALIFIER = re.compile(
    r"\b(?:drop|remove|withdraw|fail|failed|failing|retake|repeat|section|"
    r"timetable|schedule|compare|comparison)\b|"
    r"\b(?:do\s+not|don't|never)\s+(?:add|choose|pick|recommend|take)\b|"
    r"(?:احذف|حذف|انسحب|سحب|ارسب|رسوب|فشل|اعيد|اعاده|شعبه|جدول|قارن|"
    r"لا\s+(?:اضيف|اختار|اخذ)|ما\s+(?:ابي|ابغى)\s+(?:اضيف|اختار))"
)
_TIMETABLE_BUILD_CUE = re.compile(
    r"\b(?:build|create|rebuild|timetable|schedule|alternatives?|"
    r"timetable\s+options?)\b|(?:ابن|انشئ|جدول|خيارات\s+جدول)"
)


def _fold(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = _INLINE_CODE.sub(" ", value).translate(_ARABIC_FOLD).casefold()
    return _SPACE.sub(" ", _NON_WORD.sub(" ", value)).strip()


def _inactive_context(text: str) -> bool:
    """Conservatively decline a closed policy in historical/negated framing."""

    return bool(_HISTORICAL.search(text) or _NOT_ASKING.search(text))


def _standalone_corequisite(text: str) -> bool:
    code = r"[a-z]{2,8}[0-9]{2,4}[a-z]?"
    return bool(
        re.fullmatch(
            rf"(?:does\s+{code}\s+have\s+(?:a\s+)?(?:corequisite|co\s+requisite)|"
            rf"is\s+there\s+(?:a\s+)?(?:corequisite|co\s+requisite)\s+"
            rf"(?:for|with)\s+{code}|"
            rf"هل\s+(?:فيه|يوجد)\s+متطلب\s+متزامن\s+(?:مع\s+)?{code}|"
            r"هل\s+(?:فيه|يوجد)\s+متطلب\s+متزامن\s+لهذا\s+المقرر)",
            text,
        )
    )


def _generic_single_course_choice(text: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:(?:i\s+have|there\s+is)\s+(?:room|space)\s+for\s+"
            r"(?:only\s+)?one\s+course\s+(?:what|which)\s+"
            r"(?:course\s+)?(?:should\s+i\s+)?(?:choose|pick|take)|"
            r"عندي\s+(?:مجال|مكان)\s+ل?(?:ماده\s+وحده|مقرر\s+واحد)\s+"
            r"(?:بس\s+)?وش\s+(?:اختار|تنصحني))",
            text,
        )
    )


def _plain_available_not_current(text: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:which\s+(?:courses|classes)\s+am\s+i\s+eligible\s+for\s+but\s+"
            r"(?:are\s+not|aren\s+t|not)\s+in\s+my\s+(?:timetable|schedule)"
            r"(?:\s+(?:without\s+ranking|do\s+not\s+rank\s+them|"
            r"don\s+t\s+rank\s+them))?|"
            r"وش\s+(?:المواد|المقررات)\s+اللي\s+انا\s+موهل\s+لها\s+بس\s+"
            r"مو\s+موجوده\s+في\s+جدولي"
            r"(?:\s+(?:ما\s+ابي\s+ترتيب|بدون\s+ترتيب))?)",
            text,
        )
    )


def _pinned_best_course_addition(
    text: str,
    pins: Sequence[Mapping[str, str]],
) -> bool:
    if not pins:
        return False
    token = r"[a-z]{2,8}[0-9]{2,4}[a-z]?\s*-\s*[a-z][0-9]{1,3}"
    return bool(
        re.fullmatch(
            rf"(?:if\s+we\s+pin\s+{token}\s+which\s+are\s+the\s+best\s+"
            rf"(?:courses|classes)\s+to\s+add\s+with\s+it|"
            rf"اذا\s+ثبتنا\s+{token}\s+وش\s+افضل\s+(?:المواد|المقررات)\s+"
            r"اللي\s+نضيفها\s+معه)",
            text,
        )
    )


def active_semantic_policy_ids(
    question: str,
    *,
    explicit_pins: Sequence[Mapping[str, str]] | None = None,
) -> tuple[SemanticPolicyId, ...]:
    """Return the closed high-confidence policies active for one current turn."""

    text = _fold(question)
    pins = tuple(explicit_pins or ())
    if _QUOTED_COREQUISITE.search(str(question or "")):
        return ()
    if _standalone_corequisite(text):
        return (SemanticPolicyId.STANDALONE_COREQUISITE_UNSUPPORTED,)
    if _pinned_best_course_addition(text, pins):
        return (SemanticPolicyId.PINNED_COURSE_ADDITION_BALANCED,)
    if _generic_single_course_choice(text):
        return (SemanticPolicyId.SINGLE_COURSE_CHOICE_BALANCED,)
    if _plain_available_not_current(text):
        return (SemanticPolicyId.PLAIN_AVAILABLE_COURSES_ONLY,)
    return ()


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip()


def _plan_view(plan: Any) -> tuple[str, frozenset[str], tuple[tuple[str, dict[str, Any]], ...]]:
    if isinstance(plan, Mapping):
        decision = plan.get("decision", plan.get("mode", ""))
        outcomes = plan.get("requested_outcomes", plan.get("outcomes", ()))
        requests = plan.get("evidence_requests", plan.get("tool_calls", ()))
    else:
        decision = getattr(plan, "decision", "")
        outcomes = getattr(plan, "requested_outcomes", ())
        requests = getattr(plan, "evidence_requests", ())
    normalized_outcomes = frozenset(_enum_value(item) for item in outcomes or ())
    calls: list[tuple[str, dict[str, Any]]] = []
    for request in requests or ():
        if isinstance(request, Mapping):
            source = request.get("function", request)
            name = source.get("capability", source.get("name", ""))
            arguments = source.get("arguments", {})
        else:
            name = getattr(request, "capability", getattr(request, "name", ""))
            arguments = getattr(request, "arguments", {})
        calls.append(
            (
                str(name or "").strip(),
                dict(arguments) if isinstance(arguments, Mapping) else {},
            )
        )
    return _enum_value(decision), normalized_outcomes, tuple(calls)


def _normalized_pins(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    pins: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            return ()
        course = str(item.get("course_code") or "").strip().upper()
        section = str(item.get("section_label") or "").strip().upper()
        if not course or not section:
            return ()
        pins.append((course, section))
    return tuple(sorted(pins))


def semantic_policy_violations(
    question: str,
    plan: Any,
    *,
    explicit_pins: Sequence[Mapping[str, str]] | None = None,
) -> tuple[SemanticPolicyId, ...]:
    """Compare a typed plan with every active closed policy; never mutate it."""

    active = active_semantic_policy_ids(question, explicit_pins=explicit_pins)
    if not active:
        return ()
    decision, outcomes, calls = _plan_view(plan)
    violations: list[SemanticPolicyId] = []
    for policy in active:
        valid = False
        if policy is SemanticPolicyId.STANDALONE_COREQUISITE_UNSUPPORTED:
            valid = decision == "unsupported" and outcomes == {"unsupported_request"} and not calls
        elif policy is SemanticPolicyId.SINGLE_COURSE_CHOICE_BALANCED:
            valid = (
                decision == "execute"
                and outcomes == {"course_addition"}
                and len(calls) == 1
                and calls[0][0] == "recommend_feasible_course_addition"
                and calls[0][1].get("objective") == "balanced"
                and not calls[0][1].get("pinned_sections")
            )
        elif policy is SemanticPolicyId.PLAIN_AVAILABLE_COURSES_ONLY:
            valid = (
                decision == "execute"
                and outcomes == {"available_courses"}
                and len(calls) == 1
                and calls[0][0] == "my_progress"
                and "priority_limit" not in calls[0][1]
            )
        elif policy is SemanticPolicyId.PINNED_COURSE_ADDITION_BALANCED:
            expected_pins = _normalized_pins(tuple(explicit_pins or ()))
            actual_pins = _normalized_pins(
                calls[0][1].get("pinned_sections") if len(calls) == 1 else ()
            )
            valid = (
                bool(expected_pins)
                and decision == "execute"
                and outcomes == {"course_addition"}
                and len(calls) == 1
                and calls[0][0] == "recommend_feasible_course_addition"
                and calls[0][1].get("objective") == "balanced"
                and actual_pins == expected_pins
            )
        if not valid:
            violations.append(policy)
    return tuple(violations)


__all__ = [
    "SEMANTIC_POLICY_IDS",
    "SemanticPolicyId",
    "active_semantic_policy_ids",
    "semantic_policy_violations",
]

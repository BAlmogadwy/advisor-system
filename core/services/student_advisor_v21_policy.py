"""Closed semantic-policy checks shared by V2.1 runtime and offline evals.

This module deliberately has no Django or adviser-runtime dependency.  It does
not route general natural language and it never rewrites a provider plan.  The
detectors below recognize only narrow, high-confidence request families
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
    PERSONALIZED_PREREQUISITE_ANALYSIS = "personalized_prerequisite_analysis"
    PRIORITY_COURSE_ADDITION_UNLOCK = "priority_course_addition_unlock"
    FASTEST_GRADUATION_TIMETABLE_REVIEW = "fastest_graduation_timetable_review"
    ONE_COURSE_GRADUATION_IMPACT = "one_course_graduation_impact"
    BEST_TIMETABLE_PREFERENCE_CLARIFICATION = "best_timetable_preference_clarification"
    MOST_DELAYING_COURSE_PRIORITY = "most_delaying_course_priority"
    REGISTRATION_SHORTFALL_COURSE_PRIORITY = "registration_shortfall_course_priority"
    SINGLE_DROP_GRADUATION_DELAY = "single_drop_graduation_delay"
    BALANCED_NAMED_DROP_IMPACT = "balanced_named_drop_impact"
    SINGLE_DROP_PREREQUISITE_CONTINUITY = "single_drop_prerequisite_continuity"
    LEAST_DELAY_NAMED_DROP_SELECTION = "least_delay_named_drop_selection"
    PINNED_SECTION_EVERY_OPTION_BUILD = "pinned_section_every_option_build"
    FRESH_PINNED_GRADUATION_PRIORITY_BUILD = "fresh_pinned_graduation_priority_build"
    TIMETABLE_SPACE_COURSE_ADDITION = "timetable_space_course_addition"
    GRADUATION_IMPROVING_COURSE_SWAP = "graduation_improving_course_swap"


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
_COURSE_CODE = re.compile(r"\b[A-Z]{2,6}(?:[0-9]{3,4}|[0-9]{1,2})\b", re.IGNORECASE)
_HISTORICAL = re.compile(
    r"\b(?:last|previous|prior)\s+(?:term|semester)\b|" r"(?:الترم|الفصل)\s+(?:الماضي|السابق)|سابقا"
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
    r"[\"'“”‘’«»]\s*(?:corequisite|co\s*-?\s*requisite|متطلب\s+متزامن)" r"\s*[\"'“”‘’«»]",
    re.IGNORECASE,
)
_FULL_CODE_WRAPPER = re.compile(r"(?P<fence>`{1,3})[^`\r\n]+(?P=fence)")
_FULL_FENCED_BLOCK_WRAPPER = re.compile(
    r"(?P<fence>`{3,8}|~{3,8})[ \t]*(?:[A-Za-z0-9_+.-]{1,24})?[ \t]*\r?\n"
    r"[\s\S]{1,4096}?\r?\n[ \t]*(?P=fence)",
)
_FULL_EMPTY_DESTINATION_MARKDOWN = re.compile(r"!?\[[^\]\r\n]{1,4096}\]\(\s*\)")
_FULL_ESCAPED_ASCII_QUOTE = re.compile(r"\\(?P<quote>[\"'])[^\r\n]{1,4096}\\(?P=quote)")
_QUOTE_IGNORABLE_CONTROLS = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b\u200e\u200f" r"\u202a-\u202e\u2060\u2066-\u2069\ufeff]"
)
_OUTER_MARKDOWN_HEADING = re.compile(r"^#{1,6}[ \t]*")
_OUTER_TRAILING_PUNCTUATION = re.compile(r"[؟?!.،؛:]+\s*$")
_OUTER_BLOCKQUOTE = re.compile(r"^(?:>\s*)+")
_OUTER_LIST_MARKER = re.compile(r"^(?:[*+-])\s+")
_EXPLICIT_ADDITION_CRITERION = re.compile(
    r"\b(?:graduat\w*|unlock\w*|priorit\w*|important|highest\s+impact|"
    r"clash\w*|conflict\w*|timetable\s+fit|schedule\s+fit|"
    r"exactly\s+\d+\s+credits?|\d+\s*-?\s*credit)\b|"
    r"(?:تخرج|يفتح|اهم|اولوي|تعارض|بدون\s+تعارض|ملاءم|"
    r"\d+\s*(?:ساعه|ساعات))"
)
_PRIORITY_CUE = re.compile(
    r"\b(?:best|important|priority|prioritize|rank|top)\b|" r"(?:افضل|مهم|اهم|اولوي|رتب|ترتيب)"
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


def _whole_utterance_is_quoted(value: str) -> bool:
    """Reject quoted examples without disabling inline course-code markup."""

    raw_text = unicodedata.normalize("NFKC", str(value or ""))
    control_count = len(_QUOTE_IGNORABLE_CONTROLS.findall(raw_text))
    if control_count > 64:
        return True
    raw_text = _QUOTE_IGNORABLE_CONTROLS.sub("", raw_text)
    nonempty_lines = [line for line in raw_text.splitlines() if line.strip()]
    if nonempty_lines and all(re.match(r"^(?: {4}|\t)", line) for line in nonempty_lines):
        return True
    text = raw_text.strip()
    nonempty_lines = [line for line in text.splitlines() if line.strip()]
    if nonempty_lines and all(re.match(r"^\s*(?:>\s*)+", line) for line in nonempty_lines):
        return True
    if _FULL_ESCAPED_ASCII_QUOTE.fullmatch(text):
        return True
    escaped_quote_pairs = (("\\“", "\\”"), ("\\‘", "\\’"), ("\\«", "\\»"))
    if any(
        text.startswith(prefix) and text.endswith(suffix) and len(text) > len(prefix) + len(suffix)
        for prefix, suffix in escaped_quote_pairs
    ):
        return True
    if _FULL_EMPTY_DESTINATION_MARKDOWN.fullmatch(text):
        return True
    if text.startswith("~~") and text.endswith("~~") and 4 < len(text) <= 4100:
        return True
    if _FULL_FENCED_BLOCK_WRAPPER.fullmatch(text):
        return True
    wrapper_pairs = {("(", ")"), ("[", "]"), ("{", "}")}
    markup_pairs = (
        ("***", "***"),
        ("___", "___"),
        ("**", "**"),
        ("__", "__"),
        ("*", "*"),
        ("_", "_"),
    )
    for _ in range(8):
        prior = text
        text = _OUTER_BLOCKQUOTE.sub("", text).strip()
        text = _OUTER_LIST_MARKER.sub("", text).strip()
        text = _OUTER_MARKDOWN_HEADING.sub("", text).strip()
        text = _OUTER_TRAILING_PUNCTUATION.sub("", text).strip()
        if _FULL_FENCED_BLOCK_WRAPPER.fullmatch(text):
            return True
        if len(text) >= 2 and (text[0], text[-1]) in wrapper_pairs:
            text = text[1:-1].strip()
        else:
            for prefix, suffix in markup_pairs:
                if (
                    text.startswith(prefix)
                    and text.endswith(suffix)
                    and len(text) > 2 * len(prefix)
                ):
                    text = text[len(prefix) : -len(suffix)].strip()
                    break
        if text == prior:
            break
    quoted_pairs = (
        ('"', '"'),
        ("'", "'"),
        ("“", "”"),
        ("‘", "’"),
        ("«", "»"),
        ("„", "“"),
        ("‚", "‘"),
        ("‹", "›"),
        ("「", "」"),
        ("『", "』"),
        ("《", "》"),
        ("〈", "〉"),
    )
    return bool(
        _FULL_CODE_WRAPPER.fullmatch(text)
        or _FULL_FENCED_BLOCK_WRAPPER.fullmatch(text)
        or any(
            text.startswith(prefix)
            and text.endswith(suffix)
            and len(text) > len(prefix) + len(suffix)
            for prefix, suffix in quoted_pairs
        )
    )


def _standalone_corequisite(text: str) -> bool:
    code = r"[a-z]{2,6}(?:[0-9]{3,4}|[0-9]{1,2})"
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
            r"(?:\s+(?:ما\s+ابي\s+ترتيب|بدون\s+ترتيب))?|"
            r"(?:which|what)\s+(?:courses|classes)\s+(?:can\s+i|am\s+i\s+eligible\s+to)\s+"
            r"take\s+(?:this\s+term|this\s+semester)|"
            r"(?:وش|ايش)\s+(?:المواد|المقررات)\s+اللي\s+(?:اقدر\s+"
            r"(?:انزلها|اسجلها|اخذها)|انا\s+موهل\s+لها)\s+"
            r"(?:هذا\s+الترم|هالترم|هذا\s+الفصل|هالفصل))",
            text,
        )
    )


def _pinned_best_course_addition(
    text: str,
    pins: Sequence[Mapping[str, str]],
) -> bool:
    if not pins:
        return False
    token = r"[a-z]{2,6}(?:[0-9]{3,4}|[0-9]{1,2})\s*-\s*[a-z][0-9]{1,3}"
    return bool(
        re.fullmatch(
            rf"(?:if\s+we\s+pin\s+{token}\s+which\s+are\s+the\s+best\s+"
            rf"(?:courses|classes)\s+to\s+add\s+with\s+it|"
            rf"اذا\s+ثبتنا\s+{token}\s+وش\s+افضل\s+(?:المواد|المقررات)\s+"
            r"اللي\s+نضيفها\s+معه)",
            text,
        )
    )


def _personalized_prerequisite_analysis(text: str) -> bool:
    code = r"[a-z]{2,6}(?:[0-9]{3,4}|[0-9]{1,2})"
    return bool(
        re.fullmatch(
            rf"(?:what\s+do\s+i\s+still\s+need\s+to\s+pass\s+before\s+{code}|"
            rf"show\s+me\s+the\s+prerequisite\s+chain\s+i\s+still\s+have\s+before\s+{code}|"
            rf"وش\s+لازم\s+انجح\s+فيه\s+قبل\s+{code}|"
            rf"عطيني\s+سلسله\s+المتطلبات\s+المرتبطه\s+ب\s*{code})",
            text,
        )
    )


def _priority_course_addition(text: str) -> bool:
    if _COURSE_CODE.search(text):
        return False
    return bool(
        re.fullmatch(
            r"(?:i\s+want\s+one\s+extra\s+course\s+but\s+i\s+do\s+not\s+want\s+"
            r"(?:anything|one)\s+with\s+no\s+academic\s+priority|"
            r"is\s+there\s+an\s+important\s+course\s+i\s+can\s+take\s+that\s+is\s+not\s+"
            r"in\s+my\s+current\s+timetable|"
            r"ابي\s+ماده\s+اضافيه\s+بس\s+ما\s+ابي\s+شيء\s+ما\s+له\s+اولويه|"
            r"فيه\s+ماده\s+مهمه\s+اقدر\s+انزلها\s+وما\s+هي\s+موجوده\s+بجدولي)",
            text,
        )
    )


def _fastest_graduation_timetable_review(text: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:if\s+our\s+goal\s+is\s+the\s+fastest\s+possible\s+graduation\s+what\s+is\s+"
            r"the\s+best\s+timetable\s+for\s+me|"
            r"لو\s+هدفنا\s+اسرع\s+تخرج\s+ممكن\s+وش\s+الجدول\s+الافضل\s+لي)",
            text,
        )
    )


def _one_course_graduation_impact(text: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:would\s+adding\s+one\s+course\s+this\s+term\s+actually\s+change\s+my\s+"
            r"graduation\s+date|"
            r"هل\s+زياده\s+مقرر\s+واحد\s+هذا\s+الترم\s+فعل(?:ا|\s+ا)\s+تفرق\s+في\s+"
            r"موعد\s+تخرجي)",
            text,
        )
    )


def _best_timetable_preference(text: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:build\s+me\s+the\s+best\s+possible\s+timetable\s+for\s+this\s+term|"
            r"build\s+several\s+timetable\s+options\s+and\s+give\s+me\s+the\s+best\s+one|"
            r"ابن\s+لي\s+افضل\s+جدول\s+ممكن\s+لهذا\s+الترم|"
            r"سو\s+لي\s+اكثر\s+من\s+خيار\s+جدول\s+واعطني\s+الافضل)",
            text,
        )
    )


def _v20_family(text: str) -> SemanticPolicyId | None:
    code = r"[a-z]{2,6}(?:[0-9]{3,4}|[0-9]{1,2})"
    section = r"[a-z][0-9]{1,3}"
    families = (
        (
            SemanticPolicyId.MOST_DELAYING_COURSE_PRIORITY,
            r"(?:which course is delaying me the most in my degree plan|وش اكثر مقرر ماخرني في الخطه)",
        ),
        (
            SemanticPolicyId.REGISTRATION_SHORTFALL_COURSE_PRIORITY,
            r"(?:if i cannot take all the courses what is the most important thing to register|اذا ما قدرت اخذ كل المواد وش اهم شيء اسجله)",
        ),
        (
            SemanticPolicyId.SINGLE_DROP_GRADUATION_DELAY,
            rf"(?:if i drop {code} will my graduation be delayed|لو حذفت {code} هل يتاخر تخرجي)",
        ),
        (
            SemanticPolicyId.BALANCED_NAMED_DROP_IMPACT,
            rf"(?:which is better to drop {code} or {code}|what happens if i withdraw from {code}|ايهم افضل احذف {code} او {code}|وش بيصير لو انسحبت من {code})",
        ),
        (
            SemanticPolicyId.SINGLE_DROP_PREREQUISITE_CONTINUITY,
            rf"(?:will dropping {code} block courses for me next term|هل حذف {code} يقفل علي مواد في الترم الجاي)",
        ),
        (
            SemanticPolicyId.LEAST_DELAY_NAMED_DROP_SELECTION,
            rf"(?:my current timetable has {code} and {code} and {code} if i must drop one course choose the course with the least impact on my graduation date and explain why|جدولي الحالي فيه {code} و {code} و {code} اذا اضطررت احذف مقرر واحد اختر المقرر الاقل تاثيرا علي موعد تخرجي ووضح لي ليه)",
        ),
        (
            SemanticPolicyId.PINNED_SECTION_EVERY_OPTION_BUILD,
            rf"(?:i want {code} section {section} included in every option|ابي {code} شعبه {section} تكون موجوده في كل الخيارات)",
        ),
        (
            SemanticPolicyId.FRESH_PINNED_GRADUATION_PRIORITY_BUILD,
            rf"(?:build me a new timetable from scratch with a maximum of \d+ credits pin {code}\s*-\s*{section} and prioritize courses that prevent graduation delay|ابن لي جدول جديد من الصفر بحد اقصي \d+ ساعه ثبت فيه {code}\s*-\s*{section} واعط الاولويه للمقررات اللي تمنع تاخر التخرج)",
        ),
        (
            SemanticPolicyId.TIMETABLE_SPACE_COURSE_ADDITION,
            r"(?:i have (?:room|space) in (?:my|the) (?:timetable|schedule) (?:what|which) (?:courses|classes) can i add|عندي (?:مكان|مجال) في (?:الجدول|جدولي) وش (?:المواد|المقررات) اللي اقدر اضيفها)",
        ),
        (
            SemanticPolicyId.GRADUATION_IMPROVING_COURSE_SWAP,
            r"(?:is there a swap between two courses (?:that )?(?:would )?(?:let|make|help) me graduate faster|هل (?:فيه|يوجد) تبديل بين مقررين يخلي تخرجي اسرع)",
        ),
    )
    for policy, pattern in families:
        if re.fullmatch(pattern, text):
            return policy
    return None


def _single_question_course_code(question: str) -> str:
    codes = tuple(dict.fromkeys(code.upper() for code in _COURSE_CODE.findall(question)))
    return codes[0] if len(codes) == 1 else ""


def _question_course_codes(question: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(code.upper() for code in _COURSE_CODE.findall(question)))


def _question_credit_limit(question: str) -> int | None:
    folded = _fold(question)
    matches = re.findall(r"(?:maximum of|at most|حد اقصي)\s+(\d+)\s+(?:credits?|ساعه)", folded)
    value = int(matches[0]) if len(matches) == 1 else None
    return value if value is not None and 1 <= value <= 99 else None


def active_semantic_policy_ids(
    question: str,
    *,
    explicit_pins: Sequence[Mapping[str, str]] | None = None,
) -> tuple[SemanticPolicyId, ...]:
    """Return the closed high-confidence policies active for one current turn."""

    if _whole_utterance_is_quoted(str(question or "")):
        return ()
    text = _fold(question)
    pins = tuple(explicit_pins or ())
    if _QUOTED_COREQUISITE.search(str(question or "")):
        return ()
    v20_policy = _v20_family(text)
    if v20_policy is not None:
        distinct_codes = _question_course_codes(question)
        if (
            v20_policy
            in {
                SemanticPolicyId.SINGLE_DROP_GRADUATION_DELAY,
                SemanticPolicyId.SINGLE_DROP_PREREQUISITE_CONTINUITY,
            }
            and len(distinct_codes) != 1
        ):
            return ()
        if v20_policy is SemanticPolicyId.BALANCED_NAMED_DROP_IMPACT:
            is_comparison = bool(
                text.startswith("which is better to drop") or text.startswith("ايهم افضل احذف")
            )
            if len(distinct_codes) != (2 if is_comparison else 1):
                return ()
        if (
            v20_policy is SemanticPolicyId.LEAST_DELAY_NAMED_DROP_SELECTION
            and len(distinct_codes) != 3
        ):
            return ()
        if (
            v20_policy
            in {
                SemanticPolicyId.PINNED_SECTION_EVERY_OPTION_BUILD,
                SemanticPolicyId.FRESH_PINNED_GRADUATION_PRIORITY_BUILD,
            }
            and len(_normalized_pins(pins)) != 1
        ):
            return ()
        if (
            v20_policy is SemanticPolicyId.FRESH_PINNED_GRADUATION_PRIORITY_BUILD
            and _question_credit_limit(question) is None
        ):
            return ()
        return (v20_policy,)
    if _standalone_corequisite(text):
        return (SemanticPolicyId.STANDALONE_COREQUISITE_UNSUPPORTED,)
    if _pinned_best_course_addition(text, pins):
        return (SemanticPolicyId.PINNED_COURSE_ADDITION_BALANCED,)
    if _generic_single_course_choice(text):
        return (SemanticPolicyId.SINGLE_COURSE_CHOICE_BALANCED,)
    if _plain_available_not_current(text):
        return (SemanticPolicyId.PLAIN_AVAILABLE_COURSES_ONLY,)
    if _personalized_prerequisite_analysis(text):
        return (SemanticPolicyId.PERSONALIZED_PREREQUISITE_ANALYSIS,)
    if _priority_course_addition(text):
        return (SemanticPolicyId.PRIORITY_COURSE_ADDITION_UNLOCK,)
    if _fastest_graduation_timetable_review(text):
        return (SemanticPolicyId.FASTEST_GRADUATION_TIMETABLE_REVIEW,)
    if _one_course_graduation_impact(text):
        return (SemanticPolicyId.ONE_COURSE_GRADUATION_IMPACT,)
    if _best_timetable_preference(text):
        return (SemanticPolicyId.BEST_TIMETABLE_PREFERENCE_CLARIFICATION,)
    return ()


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip()


def _plan_view(
    plan: Any,
) -> tuple[str, str, frozenset[str], tuple[tuple[str, dict[str, Any]], ...]]:
    if isinstance(plan, Mapping):
        decision = plan.get("decision", plan.get("mode", ""))
        clarification_kind = plan.get("clarification_kind", "")
        outcomes = plan.get("requested_outcomes", plan.get("outcomes", ()))
        requests = plan.get("evidence_requests", plan.get("tool_calls", ()))
    else:
        decision = getattr(plan, "decision", "")
        clarification_kind = getattr(plan, "clarification_kind", "")
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
    return _enum_value(decision), _enum_value(clarification_kind), normalized_outcomes, tuple(calls)


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
    active_policy_ids: Sequence[SemanticPolicyId | str] | None = None,
) -> tuple[SemanticPolicyId, ...]:
    """Compare a typed plan with every active closed policy; never mutate it."""

    active = (
        active_semantic_policy_ids(question, explicit_pins=explicit_pins)
        if active_policy_ids is None
        else tuple(SemanticPolicyId(_enum_value(policy)) for policy in active_policy_ids)
    )
    if not active:
        return ()
    decision, clarification_kind, outcomes, calls = _plan_view(plan)
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
                and calls[0][1] == {}
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
        elif policy is SemanticPolicyId.PERSONALIZED_PREREQUISITE_ANALYSIS:
            expected_code = _single_question_course_code(question)
            valid = (
                bool(expected_code)
                and decision == "execute"
                and outcomes == {"prerequisite_information"}
                and len(calls) == 1
                and calls[0][0] == "why_course_locked"
                and calls[0][1] == {"course_code": expected_code}
            )
        elif policy is SemanticPolicyId.PRIORITY_COURSE_ADDITION_UNLOCK:
            valid = (
                decision == "execute"
                and outcomes == {"course_addition"}
                and len(calls) == 1
                and calls[0][0] == "recommend_feasible_course_addition"
                and calls[0][1] == {"objective": "unlock_impact"}
            )
        elif policy is SemanticPolicyId.FASTEST_GRADUATION_TIMETABLE_REVIEW:
            valid = (
                decision == "execute"
                and outcomes == {"timetable_review"}
                and len(calls) == 1
                and calls[0][0] == "improve_current_timetable"
                and calls[0][1]
                == {
                    "objective": "faster_graduation",
                    "credit_load_policy": "preserve",
                    "allow_course_replacements": True,
                }
            )
        elif policy is SemanticPolicyId.ONE_COURSE_GRADUATION_IMPACT:
            valid = (
                decision == "execute"
                and outcomes == {"graduation_impact"}
                and len(calls) == 1
                and calls[0][0] == "recommend_feasible_course_addition"
                and calls[0][1] == {"objective": "faster_graduation"}
            )
        elif policy is SemanticPolicyId.BEST_TIMETABLE_PREFERENCE_CLARIFICATION:
            valid = (
                decision == "clarify"
                and clarification_kind == "timetable_preference"
                and outcomes == {"timetable_build"}
                and not calls
            )
        elif policy in {
            SemanticPolicyId.MOST_DELAYING_COURSE_PRIORITY,
            SemanticPolicyId.REGISTRATION_SHORTFALL_COURSE_PRIORITY,
        }:
            valid = (
                decision == "execute"
                and outcomes == {"course_priority"}
                and calls == (("my_progress", {}),)
            )
        elif policy is SemanticPolicyId.SINGLE_DROP_GRADUATION_DELAY:
            codes = _question_course_codes(question)
            valid = (
                len(codes) == 1
                and decision == "execute"
                and outcomes == {"graduation_impact"}
                and calls
                == (
                    (
                        "rank_current_course_drop_impact",
                        {"objective": "least_graduation_delay", "course_codes": list(codes)},
                    ),
                )
            )
        elif policy in {
            SemanticPolicyId.BALANCED_NAMED_DROP_IMPACT,
            SemanticPolicyId.SINGLE_DROP_PREREQUISITE_CONTINUITY,
            SemanticPolicyId.LEAST_DELAY_NAMED_DROP_SELECTION,
        }:
            codes = _question_course_codes(question)
            objective = {
                SemanticPolicyId.BALANCED_NAMED_DROP_IMPACT: "balanced",
                SemanticPolicyId.SINGLE_DROP_PREREQUISITE_CONTINUITY: "prerequisite_continuity",
                SemanticPolicyId.LEAST_DELAY_NAMED_DROP_SELECTION: "least_graduation_delay",
            }[policy]
            valid = (
                bool(codes)
                and decision == "execute"
                and outcomes == {"course_drop_impact"}
                and calls
                == (
                    (
                        "rank_current_course_drop_impact",
                        {"objective": objective, "course_codes": list(codes)},
                    ),
                )
            )
        elif policy is SemanticPolicyId.PINNED_SECTION_EVERY_OPTION_BUILD:
            pins = _normalized_pins(tuple(explicit_pins or ()))
            args = (
                {
                    "mode": "from_scratch",
                    "must_take_courses": [pins[0][0]],
                    "pinned_sections": [{"course_code": pins[0][0], "section_label": pins[0][1]}],
                }
                if len(pins) == 1
                else {}
            )
            valid = (
                decision == "execute"
                and outcomes == {"timetable_build"}
                and calls == (("build_timetable_proposal", args),)
            )
        elif policy is SemanticPolicyId.FRESH_PINNED_GRADUATION_PRIORITY_BUILD:
            pins = _normalized_pins(tuple(explicit_pins or ()))
            limit = _question_credit_limit(question)
            args = (
                {
                    "mode": "from_scratch",
                    "max_credits": limit,
                    "must_take_courses": [pins[0][0]],
                    "pinned_sections": [{"course_code": pins[0][0], "section_label": pins[0][1]}],
                }
                if len(pins) == 1 and limit is not None
                else {}
            )
            valid = (
                decision == "execute"
                and outcomes == {"timetable_build", "course_priority"}
                and calls == (("build_timetable_proposal", args), ("my_progress", {}))
            )
        elif policy is SemanticPolicyId.TIMETABLE_SPACE_COURSE_ADDITION:
            valid = (
                decision == "execute"
                and outcomes == {"course_addition"}
                and calls
                == (("recommend_feasible_course_addition", {"objective": "timetable_fit"}),)
            )
        elif policy is SemanticPolicyId.GRADUATION_IMPROVING_COURSE_SWAP:
            valid = (
                decision == "execute"
                and outcomes == {"course_replacement"}
                and calls
                == (
                    (
                        "graduation_progress",
                        {
                            "planning_baseline_kind": "recommended_current_term",
                            "search_better_replacements": True,
                        },
                    ),
                )
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

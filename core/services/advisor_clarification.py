"""When the question names a course it never identifies, ask — do not guess.

THE FAILURE THIS REMOVES

«ليش المقرر مقفل مع أني مجتاز كل المقررات السابقة؟» names no course code and no
course. There is nothing to look up, so every path that runs anyway has to invent the
subject: the tool is called with whatever code happened to be nearby, or the model
picks one from the plan and explains why THAT course is locked. Both produce a
confident answer about a course the student did not ask about, with a real tool call
behind it.

Measured on the 50-case contract, three questions are in this shape — TT19, CP12 and
CP13 — and all three executed a data tool.

WHY IT IS DECIDED BEFORE THE PROVIDER

Same reason the planner hand-offs are: a rule addressed to the model is a rule a
model can decline. "Ask which course they mean" in a system prompt competes with
twelve other instructions and with a tool that looks answerable. Here it costs no
inference at all.

THE ANTECEDENT IS CHECKED FIRST, and that is the whole difficulty. «هذا المقرر»
after a turn about AI331 is not ambiguous — it is a pronoun with a referent — and
asking again would be worse than guessing, because the student already told us. So
history is read for a course code before anything is asked, and only a reference with
NO antecedent produces a question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: Why the turn stopped. Recorded on the answer so a trace can tell a clarification
#: from a refusal — they look identical in prose and mean opposite things.
MISSING_COURSE = "MISSING_COURSE"
MISSING_COURSE_OR_PLANNER_CONTEXT = "MISSING_COURSE_OR_PLANNER_CONTEXT"

_COURSE_CODE = re.compile(r"\b[A-Za-z]{2,4}-?\d{3}\b")

#: SINGULAR and definite. «المقررات» is the plural and means the whole plan — CP11
#: «وش المقررات المقفلة عندي» is a complete question about every locked course, and
#: asking which one they meant would be refusing an answerable question.
_DEFINITE_COURSE = frozenset(
    {"المقرر", "للمقرر", "بالمقرر", "المادة", "الماده", "للمادة", "للماده"}
)
_DEMONSTRATIVE = frozenset({"هذا", "هذه", "ذلك", "تلك", "this", "that"})
_ENGLISH_DEFINITE = ("the course", "this course", "that course", "the subject")

#: A prior planner result could name the course instead. TT19 «ما دخل في أفضل جدول»
#: is about something a previous build did, so the question it deserves says so.
_PLANNER_WORDS = frozenset({"جدول", "الجدول", "جدولا", "بدائل", "البدائل", "schedule", "timetable"})

#: A claim about ONE course's state, which is meaningless until the course is named.
#: Deliberately not «مهم» or «أولوية»: those are the questions CP19 and CP20 ask, and
#: both are answerable across the whole plan without naming anything.
_STATE_WORDS = frozenset(
    {"مقفل", "مقفله", "مغلق", "مغلقه", "موجود", "موجوده", "دخل", "يدخل", "اضيف", "يضاف"}
)

#: Two words either side. CP15 holds «مقفل» and «المقرر» nine apart, in different
#: clauses — co-occurrence would call that a reference and refuse an answerable
#: question.
_STATE_WINDOW = 2


@dataclass(frozen=True)
class AdvisorClarification:
    """One question back, decided by the server, costing no inference."""

    reason: str
    answer_ar: str
    answer_en: str

    def answer(self, language: str) -> str:
        return self.answer_ar if language == "Arabic" else self.answer_en


_ASK_COURSE = AdvisorClarification(
    reason=MISSING_COURSE,
    answer_ar="أي مقرر تقصد؟ اكتب رمز المقرر مثل AI331 حتى أتحقق من حالته في خطتك.",
    answer_en=(
        "Which course do you mean? Send the course code, for example AI331, and I will "
        "check its status in your plan."
    ),
)

_ASK_COURSE_OR_PLAN = AdvisorClarification(
    reason=MISSING_COURSE_OR_PLANNER_CONTEXT,
    answer_ar="أي مقرر تقصد أنه لم يُضف إلى الجدول؟ اكتب رمز المقرر مثل AI331.",
    answer_en=(
        "Which course do you mean was not added to the timetable? Send the course code, "
        "for example AI331."
    ),
)


def _words(text: str) -> list[str]:
    return re.findall(r"[^\s،؛.,؟?!]+", str(text or ""))


def _names_a_course(text: str) -> bool:
    """Does this sentence make a CLAIM about one course without identifying it?

    The definite article alone is far too wide, and the three questions it wrongly
    caught say why:

        CP19  «ما المقرر الذي يحرر أكبر عدد من المقررات؟»  asks US to identify it
        CP15  «لازم نحدد المقرر الفعلي أولًا؟»              asks about the PROCESS
        CP20  «لا تقل لي فقط إن المقرر مهم»                asks for an audit trail

    None of those is a reference the student expects us to already hold. What TT19,
    CP12 and CP13 share is a definite course sitting NEXT TO a claim about its state —
    «المقرر مقفل», «المقرر موجود» — which only means something if we know which course.
    Adjacency is what separates them: CP15 contains both «مقفل» and «المقرر» and they
    are nine words apart, in different clauses.
    """
    lowered = str(text or "").lower()
    if any(phrase in lowered for phrase in _ENGLISH_DEFINITE):
        return True
    words = _words(text)
    for index, word in enumerate(words):
        if word not in _DEFINITE_COURSE:
            continue
        window = words[max(0, index - _STATE_WINDOW) : index + _STATE_WINDOW + 1]
        if any(w in _STATE_WORDS for w in window):
            return True
    return False


def clarification_for(
    question: str, *, history: list[dict[str, Any]] | None = None
) -> AdvisorClarification | None:
    """The question to ask back, or None when the turn can proceed.

    Returns None the moment a course code appears anywhere in the question or in the
    recent history: a reference with a referent is not ambiguous, and asking a student
    to repeat what they just said is worse than guessing.
    """
    text = str(question or "")
    if _COURSE_CODE.search(text):
        return None
    for turn in reversed(list(history or [])):
        for key in ("content", "question", "answer"):
            if _COURSE_CODE.search(str(turn.get(key) or "")):
                return None
    if not _names_a_course(text):
        return None
    words = set(_words(text))
    if words & _PLANNER_WORDS:
        return _ASK_COURSE_OR_PLAN
    return _ASK_COURSE


__all__ = [
    "MISSING_COURSE",
    "MISSING_COURSE_OR_PLANNER_CONTEXT",
    "AdvisorClarification",
    "clarification_for",
]

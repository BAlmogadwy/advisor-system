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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The one action a capability emits today. Adding another means adding a
#: `_Handoff` below — deliberately, with its own wording reviewed — rather than
#: widening a rule that lets unknown routes through as free prose.
OPEN_STUDENT_PLANNER = "OPEN_STUDENT_PLANNER"


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
    requires_confirmation: bool = True

    def answer(self, language: str) -> str:
        return self.answer_ar if language == "Arabic" else self.answer_en

    def as_payload(self) -> dict[str, Any]:
        return {
            "type": self.action,
            "intent": self.intent,
            "requires_confirmation": self.requires_confirmation,
            "registration_modified": self.registration_modified,
        }


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

#: Keyed on the capability's `reason`, not on `action`, because the reason is
#: what the executor decided and the action is only how it is offered. Two
#: reasons could share one action and need different sentences.
HANDOFFS: dict[str, ActionHandoff] = {
    "REBUILD_REQUIRES_PLANNER_CONFIRMATION": ActionHandoff(
        action=OPEN_STUDENT_PLANNER,
        intent="REBUILD_WITHOUT_CURRENT_SECTIONS",
        answer_ar=_REBUILD_AR,
        answer_en=_REBUILD_EN,
    ),
}


def handoff_for(result: Any) -> ActionHandoff | None:
    """The route this tool result demands, if any.

    Returns None for everything else, so the loop is unchanged for the tools
    that simply return data — which is all of them but one.
    """
    if not isinstance(result, dict):
        return None
    return HANDOFFS.get(str(result.get("reason") or ""))


__all__ = ["HANDOFFS", "OPEN_STUDENT_PLANNER", "ActionHandoff", "handoff_for"]

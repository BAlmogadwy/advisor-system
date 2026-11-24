"""Arabic for the messages the Virtual Advisor's endpoints send to its page.

The endpoints keep their English ``error`` exactly as before: other callers and
the tests read it. In Arabic mode a view ADDS ``error_display`` (the sentence the
page shows) and, when there is one, ``error_detail``: a technical name such as an
exception class, which stays Latin and which the page isolates left-to-right.

ARABIC STYLE. The portal's own: formal Arabic, wording that does not depend on
whether the reader is male or female, requests as «يُرجى» with a verbal noun,
statements in the passive or as a noun phrase. A sentence that is followed by a
detail carries no final full stop: the page closes it after the detail.

Nothing here translates a model's answer, a student's data or any other data.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

#: Local-model failures by error class name (`core.services.llm_backend`). The
#: lookup walks the class's MRO, so a subclass without its own entry falls back to
#: its parent's sentence.
LLM_ERRORS: Mapping[str, str] = {
    "LLMAuthenticationError": "رفض خادم النموذج بيانات الاعتماد",
    "LLMConfigError": "إعدادات خادم النموذج غير مكتملة أو غير صحيحة",
    "LLMRateLimited": "تجاوزت الطلبات الحد المسموح به لدى خادم النموذج",
    "LLMTimeout": "انتهت مهلة انتظار رد خادم النموذج",
    "LLMInvalidResponse": "أعاد خادم النموذج ردًّا غير صالح",
    "LLMBadRequest": "رفض خادم النموذج الطلب",
    "LLMUnavailable": "تعذّر الوصول إلى خادم النموذج المحلي",
    "LLMPrivacyError": "أُوقف الطلب حمايةً لخصوصية البيانات",
    "LLMError": "تعذّر استخدام خادم النموذج المحلي",
}

#: Request-validation messages the views write themselves, by their exact English.
VIEW_ERRORS: Mapping[str, str] = {
    "Invalid JSON body": "تعذّرت قراءة الطلب.",
    "JSON body must be an object": "تعذّرت قراءة الطلب.",
    "message is required": "يُرجى كتابة السؤال.",
    "message is too long": "السؤال أطول من الحد المسموح به.",
    "student_id must be an integer": "يُرجى إدخال الرقم الجامعي أرقامًا فقط.",
    "academic_year must be an integer": "يُرجى إدخال السنة أرقامًا فقط.",
    "term must be an integer": "يُرجى إدخال الفصل الدراسي رقمًا.",
    "model is too long": "اسم النموذج أطول من الحد المسموح به.",
}

#: Student-scope refusals (`core.services.policy.require_student_scope`), by reason code.
SCOPE_ERRORS: Mapping[str, str] = {
    "EXAM_COMMITTEE_EXAM_ONLY": "صلاحية لجنة الاختبارات مقصورة على جداول الاختبارات.",
    "STUDENT_SCOPE_NOT_FOUND": "لم يُعثر على الطالب.",
    "STUDENT_SCOPE_SELF_ONLY": "لا يُتاح للطالب إلا سجله الخاص.",
    "STUDENT_SCOPE_ADVISOR_MISMATCH": "الطالب خارج نطاق الإرشاد الخاص بك.",
    "STUDENT_SCOPE_DEPARTMENT_MISMATCH": "الطالب خارج نطاق القسم.",
}

#: `answer_virtual_advisor` raises ValueError("Student not found: <id>").
STUDENT_NOT_FOUND = "لم يُعثر على الطالب."
#: `AdvisorPrincipal` refuses an identity it cannot use (IdentityError).
IDENTITY_REFUSED = "تعذّر التحقق من هوية الحساب لهذا الطلب."

# "local is not reachable (ConnectionRefusedError)." -> "ConnectionRefusedError"
_REASON_RE = re.compile(r"\(([A-Za-z_][A-Za-z0-9_.]*)\)\.?\s*$")


def language_of(code: object) -> str:
    """'ar' for any Arabic language code, 'en' for everything else."""
    return "ar" if str(code or "").lower().startswith("ar") else "en"


def llm_error_fields(class_names: Iterable[str], message: str) -> dict[str, str]:
    """``error_display`` and ``error_detail`` in Arabic for a local-model failure.

    ``class_names`` is the error's class name, then its parents' (most specific
    first). The detail is the technical reason the English message names in
    parentheses (an exception class such as ``ConnectionRefusedError``), else
    the error's own class name.
    """
    names = [str(name) for name in class_names if str(name or "").strip()]
    arabic = next((LLM_ERRORS[name] for name in names if name in LLM_ERRORS), None)
    if arabic is None:
        arabic = LLM_ERRORS["LLMError"]
    match = _REASON_RE.search(str(message or ""))
    detail = match.group(1) if match else (names[0] if names else "")
    if detail:
        return {"error_display": arabic, "error_detail": detail}
    return {"error_display": f"{arabic}."}


def view_error_display(message: str) -> str | None:
    """Arabic for a message the views write themselves, or None when unknown."""
    return VIEW_ERRORS.get(str(message or ""))


def scope_error_display(reason_code: str) -> str | None:
    """Arabic for a student-scope refusal, or None for an unknown reason code."""
    return SCOPE_ERRORS.get(str(reason_code or ""))

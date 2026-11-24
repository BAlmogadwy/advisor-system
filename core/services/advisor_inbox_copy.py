"""Every code the adviser inbox prints, in English and Arabic.

ONE CLOSED VOCABULARY. The inbox speaks in codes: a case status, the reason a
case was sent to a person, the kind of an entry in its history, and what the
virtual adviser's answer did. This module turns each code into words. The
English of every Status, Reason and event Kind is the model's own label, word
for word, so the English pages read exactly as they did; the Arabic lives here
because the model labels are a migration's business and must not change. A code
the models add later shows up as a missing entry in a test, not as English in
an Arabic page.

ARABIC STYLE. The portal's own (see `timetable_feedback_copy`): formal Arabic,
wording that does not depend on whether the reader is male or female, requests
as «يُرجى» with a verbal noun, statements in the passive or as a noun phrase.
The student-facing escalation labels in `advisor_conversation_views` and the
staff timetable-feedback copy already name some of these states; their Arabic
is reused here so one case reads the same on every page.
"""

from __future__ import annotations

from collections.abc import Mapping

from core.services.timetable_feedback_copy import language_of

__all__ = [
    "DISPOSITION",
    "EVENT",
    "MESSAGES",
    "REASON",
    "STATUS",
    "disposition_label",
    "event_label",
    "language_of",
    "message",
    "reason_label",
    "status_label",
]

#: A case's status (AdvisorEscalation.Status).
STATUS: Mapping[str, tuple[str, str]] = {
    "OPEN": ("Open", "مفتوحة"),
    "ASSIGNED": ("Assigned", "مُسندة"),
    "NEEDS_INFORMATION": ("Needs information", "مطلوب معلومات إضافية"),
    "RESOLVED": ("Resolved", "تمت المعالجة"),
    "CLOSED": ("Closed", "مغلقة"),
}

#: Why a case was sent to a person (AdvisorEscalation.Reason).
REASON: Mapping[str, tuple[str, str]] = {
    "PROHIBITED_FOR_DECISION": (
        "The regulation reserves this decision to a person",
        "تقصر اللائحة هذا القرار على شخص مختص",
    ),
    "POLICY_NOT_FOUND": (
        "No approved policy governs the question",
        "لا توجد سياسة معتمدة تنظّم هذا السؤال",
    ),
    "POLICY_UNAVAILABLE": (
        "The policy store could not be consulted",
        "تعذّر الرجوع إلى مستودع السياسات",
    ),
    "STUDENT_DATA_MISSING": (
        "Required student facts were unavailable",
        "بيانات الطالب اللازمة غير متاحة",
    ),
    "PROCEDURE_NOT_DOCUMENTED": (
        "The procedure is not written down",
        "الإجراء غير موثّق",
    ),
    "CONFLICTING_AUTHORITIES": (
        "Sources disagree and a person must choose",
        "المصادر متعارضة، ويلزم أن يحسم شخص مختص بينها",
    ),
    "JUDGE_REJECTED": ("The answer did not survive review", "لم تجتز الإجابة المراجعة"),
    "MODEL_UNAVAILABLE": ("No answer could be produced", "تعذّر إعداد إجابة"),
    "STUDENT_REQUESTED": (
        "The student asked for a human adviser",
        "طلب الطالب التواصل مع مرشد أكاديمي",
    ),
    "OUTPUT_NOT_GROUNDED": (
        "The answer named identifiers the evidence does not support",
        "ذكرت الإجابة معرّفات لا تدعمها الأدلة",
    ),
}

#: Each entry of a case's history (AdvisorEscalationEvent.Kind).
EVENT: Mapping[str, tuple[str, str]] = {
    "OPENED": ("Opened by the student", "فتحها الطالب"),
    "VIEWED": ("Opened by an adviser", "فتحها مرشد"),
    "ASSIGNED": ("Assigned", "إسناد الحالة"),
    "STATUS_CHANGED": ("Status changed", "تغيير الحالة"),
    "NOTE_ADDED": ("Internal note added", "إضافة ملاحظة داخلية"),
    "RESPONSE_RECORDED": ("Reply to the student recorded", "تسجيل الرد على الطالب"),
}

#: What the virtual adviser's answer did (FinalDisposition). The English page
#: prints the bare code here, as it always has; these are for the Arabic one.
DISPOSITION: Mapping[str, tuple[str, str]] = {
    "PASS": ("Answered", "تمت الإجابة"),
    "ABSTAIN": ("Declined to answer", "الامتناع عن الإجابة"),
    "ESCALATE": ("Sent to a human adviser", "الإحالة إلى مرشد أكاديمي"),
    "FAILED": ("Could not be produced", "تعذّر إعدادها"),
}

#: What the case-action endpoint says when it refuses. Placeholders are {name}.
MESSAGES: Mapping[str, tuple[str, str]] = {
    "invalid_json": ("Invalid JSON body.", "محتوى الطلب ليس JSON صالحًا."),
    "not_an_object": ("Body must be a JSON object.", "يجب أن يكون محتوى الطلب كائن JSON."),
    "no_adviser_id": (
        "No adviser id is linked to your account.",
        "لا يوجد معرّف مرشد مرتبط بهذا الحساب.",
    ),
    "note_empty": ("The note is empty.", "الملاحظة فارغة."),
    "reply_empty": ("The reply is empty.", "الرد فارغ."),
    "unknown_action": ("Unknown action: {action}", "إجراء غير معروف: {action}"),
    "not_a_status": ("{status} is not a case status.", "القيمة {status} ليست حالة معروفة."),
    "bad_transition": (
        "A {before} case cannot become {after}.",
        "لا يمكن تغيير الحالة من «{before}» إلى «{after}».",
    ),
    "reply_before_resolving": (
        "Record a reply to the student before resolving the case.",
        "يُرجى تسجيل الرد على الطالب قبل إتمام معالجة الحالة.",
    ),
}


def _pick(pair: tuple[str, str], language: str) -> str:
    return pair[1] if language == "ar" else pair[0]


def _lookup(table: Mapping[str, tuple[str, str]], code: object, language: str) -> str:
    key = str(code or "")
    pair = table.get(key)
    return _pick(pair, language) if pair else key


def status_label(code: object, language: str) -> str:
    return _lookup(STATUS, code, language)


def reason_label(code: object, language: str) -> str:
    return _lookup(REASON, code, language)


def event_label(code: object, language: str) -> str:
    return _lookup(EVENT, code, language)


def disposition_label(code: object, language: str) -> str:
    return _lookup(DISPOSITION, code, language)


def message(key: str, language: str, **params: object) -> str:
    """A refusal as plain text (it travels in JSON and is set with textContent)."""
    return _pick(MESSAGES[key], language).format(**{k: str(v) for k, v in params.items()})

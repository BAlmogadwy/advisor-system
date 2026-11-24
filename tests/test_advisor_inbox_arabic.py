"""The adviser inbox in Arabic, and the same inbox unchanged in English.

The labels come from one closed vocabulary (`advisor_inbox_copy`): every code
the models can store has an English entry equal to the model's own label, so
the English pages read as they always did, and an Arabic entry, so no model
label leaks into an Arabic page. A code added to the models without a copy
entry fails here.
"""

from __future__ import annotations

import json

import pytest
from django.conf import settings
from django.contrib.auth.models import Group, User
from django.urls import reverse

from core.models import (
    AdvisorConversation,
    AdvisorEscalation,
    AdvisorEscalationEvent,
    AdvisorMessage,
    FinalDisposition,
    Student,
)
from core.services import advisor_inbox_copy as copy
from core.services.advisor_outcome import ReasonCode
from core.services.rbac import ROLE_ADVISOR, ensure_role_groups, set_user_scope

pytestmark = pytest.mark.django_db

STUDENT = 9502001

#: Page words that must not survive into the Arabic pages.
INBOX_ENGLISH = (
    "Escalated cases",
    "Questions the virtual adviser could not safely resolve",
    "Assigned to me",
    "No cases match these filters.",
    ">Filter<",
    ">Any<",
)
CASE_ENGLISH = (
    "Student question",
    "Their note",
    "submitted ",
    "Virtual adviser response",
    "Sources shown with that answer",
    "Why it was escalated",
    "Sent to a person because",
    "The answer itself",
    "What limited it",
    "Recorded as missing",
    "Generated summary",
    "Adviser action",
    "Assign to me",
    "Mark ",
    "Internal note",
    "the student never sees this",
    "Add note",
    "Reply to the student",
    "shown in their conversation",
    "Save reply",
    "History",
    "edition ",
)


def _adviser() -> User:
    ensure_role_groups()
    user = User.objects.create_user("adv-ar", password="x")
    user.groups.add(Group.objects.get(name=ROLE_ADVISOR))
    set_user_scope(user.id, advisor_id="A1", departments="")
    return user


def _case(*, status: str = AdvisorEscalation.Status.OPEN) -> AdvisorEscalation:
    Student.objects.update_or_create(
        student_id=STUDENT,
        defaults={"name": "S-AR", "program": "CS", "advisor_id": "A1"},
    )
    conversation = AdvisorConversation.objects.create(student_id=STUDENT)
    asked = AdvisorMessage.objects.create(
        conversation=conversation, role=AdvisorMessage.ROLE_STUDENT, content="سؤال"
    )
    answered = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        in_reply_to=asked,
        content="جواب",
        final_disposition=FinalDisposition.ABSTAIN,
        reason_codes=[ReasonCode.PROHIBITED_FOR_DECISION],
    )
    case = AdvisorEscalation.objects.create(
        conversation=conversation,
        source_message=answered,
        student_id=STUDENT,
        reason_code=ReasonCode.PROHIBITED_FOR_DECISION,
        status=status,
        student_note="ملاحظة",
        generated_summary="ملخص",
        evidence_snapshot={
            "question": "سؤال",
            "assistant_answer": "جواب",
            "final_disposition": FinalDisposition.ABSTAIN,
            "reason_codes": [ReasonCode.PROHIBITED_FOR_DECISION],
            "citations": [
                {
                    "document_title": "الدليل",
                    "edition": "1447",
                    "page": "24",
                    "policy_id": "TU.WITHDRAWAL.MAXIMUM",
                }
            ],
            "missing_information": [{"code": "GPA", "label_ar": "المعدل"}],
        },
    )
    AdvisorEscalationEvent.objects.create(
        escalation=case,
        actor_label="A1",
        kind=AdvisorEscalationEvent.Kind.STATUS_CHANGED,
        from_status=AdvisorEscalation.Status.OPEN,
        to_status=AdvisorEscalation.Status.ASSIGNED,
    )
    return case


def _client(client, language: str):
    client.force_login(_adviser())
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    return client


# ── the vocabulary ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("table", "choices"),
    [
        (copy.STATUS, AdvisorEscalation.Status.choices),
        (copy.REASON, AdvisorEscalation.Reason.choices),
        (copy.EVENT, AdvisorEscalationEvent.Kind.choices),
        (copy.DISPOSITION, FinalDisposition.choices),
    ],
    ids=["status", "reason", "event", "disposition"],
)
def test_every_code_has_both_languages_and_english_is_the_model_label(table, choices):
    assert set(table) == {value for value, _label in choices}
    for value, label in choices:
        english, arabic = table[value]
        assert english == label, value
        assert arabic.strip(), value
        assert not any("a" <= ch.lower() <= "z" for ch in arabic), (value, arabic)


def test_every_message_has_both_languages_with_the_same_placeholders():
    import string

    fields = string.Formatter()
    for key, (english, arabic) in copy.MESSAGES.items():
        assert english.strip() and arabic.strip(), key
        names_en = {f for _t, f, _s, _c in fields.parse(english) if f}
        names_ar = {f for _t, f, _s, _c in fields.parse(arabic) if f}
        assert names_en == names_ar, key


def test_an_unknown_code_falls_back_to_itself():
    assert copy.status_label("NOT_A_CODE", "ar") == "NOT_A_CODE"
    assert copy.reason_label("", "en") == ""


# ── the pages in Arabic ──────────────────────────────────────────


def test_the_arabic_inbox_shows_arabic_labels(client):
    _case()
    page = _client(client, "ar").get(reverse("advisor_inbox")).content.decode()

    assert "الحالات المحالة" in page
    for value, _label in AdvisorEscalation.Status.choices:
        assert copy.status_label(value, "ar") in page
    for value, _label in AdvisorEscalation.Reason.choices:
        assert copy.reason_label(value, "ar") in page
    for _value, label in AdvisorEscalation.Reason.choices:
        assert label not in page
    for english in INBOX_ENGLISH:
        assert english not in page, english


def test_the_arabic_empty_inbox_says_so_in_arabic(client):
    page = _client(client, "ar").get(reverse("advisor_inbox")).content.decode()
    assert "لا توجد حالات تطابق عوامل التصفية هذه." in page
    assert "No cases match these filters." not in page


def test_the_arabic_case_page_shows_arabic_labels(client):
    case = _case()
    page = _client(client, "ar").get(reverse("advisor_inbox_case", args=[case.reference]))
    body = page.content.decode()

    assert page.status_code == 200
    assert copy.status_label("OPEN", "ar") in body
    assert copy.reason_label("PROHIBITED_FOR_DECISION", "ar") in body
    assert copy.disposition_label("ABSTAIN", "ar") in body
    assert copy.event_label("STATUS_CHANGED", "ar") in body
    assert copy.event_label("VIEWED", "ar") in body
    assert "مفتوحة ← مُسندة" in body
    # Every move the case may make, as a button carrying its code and Arabic words.
    assert 'data-ai-status="ASSIGNED"' in body
    assert "تغيير الحالة إلى: مُسندة" in body
    assert "لا تظهر للطالب أبدًا" in body
    assert "يظهر في محادثة الطالب" in body
    for english in CASE_ENGLISH:
        assert english not in body, english
    for _value, label in AdvisorEscalationEvent.Kind.choices:
        assert f">{label}<" not in body, label


def test_an_arabic_refusal_is_in_arabic(client):
    case = _case(status=AdvisorEscalation.Status.ASSIGNED)
    response = _client(client, "ar").post(
        reverse("advisor_inbox_case_action", args=[case.reference]),
        data=json.dumps({"action": "set_status", "status": "RESOLVED"}),
        content_type="application/json",
    )
    assert response.status_code == 409
    assert response.json()["error"] == copy.message("reply_before_resolving", "ar")


def test_an_arabic_illegal_move_names_both_states_in_arabic(client):
    case = _case(status=AdvisorEscalation.Status.CLOSED)
    response = _client(client, "ar").post(
        reverse("advisor_inbox_case_action", args=[case.reference]),
        data=json.dumps({"action": "set_status", "status": "OPEN"}),
        content_type="application/json",
    )
    assert response.status_code == 409
    assert response.json()["error"] == "لا يمكن تغيير الحالة من «مغلقة» إلى «مفتوحة»."


# ── the pages in English, unchanged ──────────────────────────────


def test_the_english_inbox_is_unchanged(client):
    _case()
    english_client = _client(client, "en")
    page = english_client.get(reverse("advisor_inbox")).content.decode()
    empty = english_client.get(reverse("advisor_inbox"), {"status": "CLOSED"}).content.decode()

    assert "No cases match these filters." in empty
    for english in INBOX_ENGLISH:
        if english != "No cases match these filters.":
            assert english in page, english
    assert "Escalated adviser cases" in page
    for _value, label in AdvisorEscalation.Status.choices:
        assert f">{label}</option>" in page, label
    for _value, label in AdvisorEscalation.Reason.choices:
        assert label in page, label
    assert "الحالات المحالة" not in page
    assert "مفتوحة" not in page


def test_the_english_case_page_is_unchanged(client):
    case = _case()
    body = (
        _client(client, "en")
        .get(reverse("advisor_inbox_case", args=[case.reference]))
        .content.decode()
    )

    for english in CASE_ENGLISH:
        assert english in body, english
    assert "Mark assigned" in body
    assert "edition 1447, p. 24" in body
    assert "<dd>ABSTAIN</dd>" in body
    assert "<dd>PROHIBITED_FOR_DECISION</dd>" in body
    assert "OPEN → ASSIGNED" in body
    assert "Status changed" in body
    assert "Opened by an adviser" in body
    assert "تغيير الحالة" not in body
    assert "سبب الإحالة" not in body


def test_an_english_refusal_is_the_services_own_sentence(client):
    case = _case(status=AdvisorEscalation.Status.CLOSED)
    response = _client(client, "en").post(
        reverse("advisor_inbox_case_action", args=[case.reference]),
        data=json.dumps({"action": "set_status", "status": "OPEN"}),
        content_type="application/json",
    )
    assert response.json()["error"] == "A CLOSED case cannot become OPEN."

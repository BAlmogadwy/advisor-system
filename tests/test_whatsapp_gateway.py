import hashlib
import hmac
import json

import pytest
from django.test import Client, override_settings

from core.models import AcademicAdvisor, Student
from core.services.rbac import ROLE_STUDENT
from core.services.sendgrid_email import SendGridDeliveryError
from core.services.student_otp import provision_student_user
from core.services.virtual_advisor import find_students_tool
from whatsapp_gateway.models import WhatsAppMessageLog, WhatsAppOtpChallenge, WhatsAppUserLink
from whatsapp_gateway.services import (
    OtpChallengeError,
    process_inbound_text,
    scope_for_link,
    start_link_challenge,
    verify_link_otp,
    verify_meta_signature,
)

pytestmark = pytest.mark.django_db


def test_advisor_otp_linking_creates_active_whatsapp_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    AcademicAdvisor.objects.create(
        advisor_id="75",
        full_name="Dr. Advisor",
        email="advisor75@uni.edu",
        department="AI,DS",
    )
    sent: dict[str, object] = {}

    monkeypatch.setattr("whatsapp_gateway.services._generate_otp", lambda: "123456")

    def fake_sendgrid(to_email, subject, message):
        sent["subject"] = subject
        sent["message"] = message
        sent["recipient"] = to_email
        return "safe-message-id"

    monkeypatch.setattr("whatsapp_gateway.services.send_transactional_email", fake_sendgrid)

    challenge = start_link_challenge(
        wa_id="966500000001",
        phone_number="966500000001",
        university_id="75",
    )

    assert challenge.email_masked == "a***5@uni.edu"
    assert challenge.otp_hash != "123456"
    assert sent["recipient"] == "advisor75@uni.edu"

    link = verify_link_otp(wa_id="966500000001", otp="123456")
    assert link.status == WhatsAppUserLink.STATUS_ACTIVE
    assert link.role == "ADVISOR"
    assert link.advisor_id == "75"
    assert scope_for_link(link)["advisor_id"] == "75"


def test_student_linking_uses_canonical_university_email_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Student.objects.create(student_id=4450001, name="Student One", program="AI")
    sent: dict[str, object] = {}

    def fake_sendgrid(to_email, subject, message):
        sent["recipient"] = to_email
        return "safe-message-id"

    monkeypatch.setattr("whatsapp_gateway.services.send_transactional_email", fake_sendgrid)

    challenge = start_link_challenge(
        wa_id="966500000002",
        phone_number="966500000002",
        university_id="4450001",
    )

    assert challenge.email_masked == "4***1@taibahu.edu.sa"
    assert sent["recipient"] == "4450001@taibahu.edu.sa"


def test_provisioned_student_with_blank_user_email_uses_canonical_mailbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    student = Student.objects.create(student_id=4550004, name="Student Two", program="AI")
    user = provision_student_user(student.student_id)
    assert user.email == ""
    sent: dict[str, object] = {}

    def fake_sendgrid(to_email, subject, message):
        sent["recipient"] = to_email
        return "safe-message-id"

    monkeypatch.setattr("whatsapp_gateway.services.send_transactional_email", fake_sendgrid)

    challenge = start_link_challenge(
        wa_id="966500000004",
        phone_number="966500000004",
        university_id=str(student.student_id),
    )

    assert challenge.resolved_role == ROLE_STUDENT
    assert challenge.resolved_user_id == user.id
    assert challenge.resolved_student_id == student.student_id
    assert sent["recipient"] == f"tu{student.student_id}@taibahu.edu.sa"


def test_sendgrid_failure_leaves_no_usable_whatsapp_challenge_or_pii(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    advisor_email = "private-advisor@uni.edu"
    AcademicAdvisor.objects.create(
        advisor_id="76",
        full_name="Private Advisor",
        email=advisor_email,
        department="AI",
    )
    monkeypatch.setattr("whatsapp_gateway.services._generate_otp", lambda: "654321")

    def fail_sendgrid(to_email, subject, message):
        raise SendGridDeliveryError("provider_rejected")

    monkeypatch.setattr("whatsapp_gateway.services.send_transactional_email", fail_sendgrid)

    with pytest.raises(OtpChallengeError) as captured:
        start_link_challenge(
            wa_id="966500000002",
            phone_number="966500000002",
            university_id="76",
        )

    public_error = str(captured.value)
    assert public_error == "Unable to send a verification code. Try again later."
    assert advisor_email not in public_error
    assert "654321" not in public_error
    assert "provider_rejected" not in public_error
    assert WhatsAppOtpChallenge.objects.count() == 0


@override_settings(STUDENT_EMAIL_DOMAIN="students.uni.edu")
def test_student_scope_limits_generic_find_students_query(monkeypatch: pytest.MonkeyPatch) -> None:
    linked = Student.objects.create(
        student_id=4550002,
        name="Linked Student",
        program="AI",
        section="F",
        total_earned_credits=90,
    )
    Student.objects.create(
        student_id=4550003,
        name="Other Student",
        program="AI",
        section="F",
        total_earned_credits=130,
    )

    monkeypatch.setattr("whatsapp_gateway.services._generate_otp", lambda: "123456")
    sent: dict[str, object] = {}

    def fake_sendgrid(to_email, subject, message):
        sent["recipient"] = to_email
        return "safe-message-id"

    monkeypatch.setattr("whatsapp_gateway.services.send_transactional_email", fake_sendgrid)

    start_link_challenge(
        wa_id="966500000003",
        phone_number="966500000003",
        university_id=str(linked.student_id),
    )
    assert sent["recipient"] == f"tu{linked.student_id}@students.uni.edu"
    link = verify_link_otp(wa_id="966500000003", otp="123456")

    result = find_students_tool({"min_earned_credits": 0}, scope=scope_for_link(link))

    assert result["count"] == 1
    assert result["students"][0]["student_id"] == linked.student_id


@override_settings(WHATSAPP_APP_SECRET="secret", WHATSAPP_REQUIRE_SIGNATURE=True)
def test_meta_signature_validation() -> None:
    body = b'{"object":"whatsapp_business_account"}'
    digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()

    assert verify_meta_signature(body=body, signature_header=f"sha256={digest}") is True
    assert verify_meta_signature(body=body, signature_header="sha256=bad") is False


@override_settings(WHATSAPP_VERIFY_TOKEN="verify-me", WHATSAPP_REQUIRE_SIGNATURE=False)
def test_webhook_verification_endpoint() -> None:
    client = Client()
    response = client.get(
        "/whatsapp/webhook/",
        {
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-me",
            "hub.challenge": "challenge-123",
        },
    )

    assert response.status_code == 200
    assert response.content.decode("utf-8") == "challenge-123"


@override_settings(WHATSAPP_REQUIRE_SIGNATURE=False)
def test_webhook_post_processes_unknown_text_without_outbound_credentials() -> None:
    client = Client()
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": "966500000004"}],
                            "messages": [
                                {
                                    "from": "966500000004",
                                    "id": "wamid.test",
                                    "type": "text",
                                    "text": {"body": "hello"},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }

    response = client.post(
        "/whatsapp/webhook/",
        data=json.dumps(payload),
        content_type="application/json",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["processed"][0]["action"] == "request_university_id"
    assert body["processed"][0]["outbound"]["reason"] == "whatsapp_not_configured"


def test_process_inbound_unlink_is_safe_without_existing_link() -> None:
    result = process_inbound_text(wa_id="966500000005", text="unlink")

    assert result["ok"] is True
    assert result["action"] == "unlink"
    assert WhatsAppOtpChallenge.objects.count() == 0


def test_authenticated_whatsapp_message_uses_virtual_advisor_not_canned_role_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    student = Student.objects.create(
        student_id=4450006,
        name="Linked Student",
        program="AI",
        total_earned_credits=91,
    )
    WhatsAppUserLink.objects.create(
        wa_id="966500000006",
        phone_number="966500000006",
        role="STUDENT",
        student=student,
    )
    captured: dict[str, object] = {}

    def fake_answer_virtual_advisor(**kwargs):
        captured.update(kwargs)
        return {"answer": "A natural, evidence-based advisor answer.", "ok": True}

    monkeypatch.setattr(
        "whatsapp_gateway.services.answer_virtual_advisor", fake_answer_virtual_advisor
    )

    result = process_inbound_text(
        wa_id="966500000006",
        text="Can I take AI431 next term?",
    )

    assert result["action"] == "answered"
    assert result["reply"] == "A natural, evidence-based advisor answer."
    # One identity object, not an id beside a scope that could disagree with it.
    # This test stubs `answer_virtual_advisor`, so it is deliberately explicit about
    # the CALL SHAPE: stubbing hid a signature change that made every real inbound
    # message raise TypeError out of the webhook while this stayed green.
    assert "student_id" not in captured and "scope" not in captured
    principal = captured["principal"]
    assert principal.student_id == student.student_id
    assert principal.as_scope() == {"role": "STUDENT", "student_id": student.student_id}


def test_authenticated_whatsapp_message_passes_recent_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    student = Student.objects.create(student_id=4450007, name="Linked Student", program="AI")
    WhatsAppUserLink.objects.create(
        wa_id="966500000007",
        phone_number="966500000007",
        role="STUDENT",
        student=student,
    )
    WhatsAppMessageLog.objects.create(
        wa_id="966500000007",
        direction=WhatsAppMessageLog.DIRECTION_INBOUND,
        message_type="text",
        text_preview="What is my GPA?",
        status="received",
    )
    WhatsAppMessageLog.objects.create(
        wa_id="966500000007",
        direction=WhatsAppMessageLog.DIRECTION_OUTBOUND,
        message_type="text",
        text_preview="Your verified GPA is 4.2.",
        status="sent",
    )
    captured: dict[str, object] = {}

    def fake_answer_virtual_advisor(**kwargs):
        captured.update(kwargs)
        return {"answer": "It refers to the GPA we just discussed.", "ok": True}

    monkeypatch.setattr(
        "whatsapp_gateway.services.answer_virtual_advisor", fake_answer_virtual_advisor
    )

    process_inbound_text(wa_id="966500000007", text="What does that mean?")

    assert captured["history"] == [
        {"role": "user", "content": "What is my GPA?"},
        {"role": "assistant", "content": "Your verified GPA is 4.2."},
    ]


# ── the call shape, checked against the REAL function ────────────


def test_the_gateway_call_matches_the_real_advisor_signature():
    """Every gateway test stubs `answer_virtual_advisor`, so none of them can see a
    signature change. Bind the arguments against the real function instead."""
    import inspect
    from unittest import mock

    from core.services.virtual_advisor import answer_virtual_advisor
    from whatsapp_gateway.models import WhatsAppUserLink
    from whatsapp_gateway.services import answer_for_link

    link = WhatsAppUserLink(
        wa_id="966500000001", role=ROLE_STUDENT, student_id=6001001, user_id=None
    )
    seen: dict[str, object] = {}

    def capture(**kwargs):
        seen.update(kwargs)
        # Raises TypeError if the gateway is calling a shape the real function
        # does not accept.
        inspect.signature(answer_virtual_advisor).bind(**kwargs)
        return {"answer": "ok", "model": "t"}

    with (
        mock.patch("whatsapp_gateway.services.answer_virtual_advisor", capture),
        mock.patch("whatsapp_gateway.services.recent_history_for_wa_id", return_value=[]),
    ):
        answer_for_link(link=link, message="كم ساعة باقي؟")

    assert seen["principal"].student_id == 6001001


def test_an_unrecognised_sender_cannot_be_answered_as_a_student():
    """The gateway's "no student here" case must not become a principal at all."""
    from core.services.advisor_principal import IdentityError
    from whatsapp_gateway.models import WhatsAppUserLink
    from whatsapp_gateway.services import answer_for_link

    link = WhatsAppUserLink(wa_id="966500000002", role=ROLE_STUDENT, student_id=None, user_id=None)
    with pytest.raises(IdentityError):
        answer_for_link(link=link, message="hi")

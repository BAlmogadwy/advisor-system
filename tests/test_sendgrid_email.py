from http.client import HTTPMessage
from io import StringIO
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from python_http_client.exceptions import HTTPError

from config import settings as project_settings
from core.models import AcademicAdvisor, RateLimitBucket, Student
from core.services import sendgrid_email, student_otp
from core.services.rate_limit import SENDGRID_SUBMISSION
from core.services.sendgrid_email import SendGridDeliveryError
from whatsapp_gateway.services import OtpChallengeError, start_link_challenge

pytestmark = pytest.mark.django_db

PROVIDER_SETTINGS = {
    "STUDENT_OTP_SENDGRID_ENABLED": True,
    "SENDGRID_API_KEY": "SG.ci-only",
    "SENDGRID_FROM_EMAIL": "verified-sender@example.invalid",
    "SENDGRID_FROM_NAME": "بوابة الطالب",
    "SENDGRID_MAX_SUBMISSIONS": 4700,
    "SENDGRID_SUBMISSION_WINDOW_SECONDS": 86_400,
}


def _response_headers(message_id: str = "message-id-123") -> HTTPMessage:
    headers = HTTPMessage()
    headers["X-Message-Id"] = message_id
    return headers


@pytest.mark.parametrize(
    ("configured_timeout", "expected_timeout"),
    [(0, 1), (15, 15), (999, 60)],
)
def test_real_sdk_builds_plain_text_mail_and_propagates_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch,
    configured_timeout: int,
    expected_timeout: int,
) -> None:
    captured: dict[str, object] = {}

    def fake_send(self, message):
        captured["root_timeout"] = self.client.timeout
        captured["nested_timeout"] = self.client.mail.send.timeout
        captured["payload"] = message.get()
        return SimpleNamespace(status_code=202, headers=_response_headers())

    monkeypatch.setattr(sendgrid_email.SendGridAPIClient, "send", fake_send)
    with override_settings(**PROVIDER_SETTINGS, SENDGRID_TIMEOUT_SECONDS=configured_timeout):
        message_id = sendgrid_email.send_transactional_email(
            "student@example.invalid",
            "رمز التحقق",
            "رمزك هو 123456",
        )

    assert message_id == "message-id-123"
    assert captured["root_timeout"] == expected_timeout
    assert captured["nested_timeout"] == expected_timeout
    assert captured["payload"] == {
        "from": {
            "name": "بوابة الطالب",
            "email": "verified-sender@example.invalid",
        },
        "subject": "رمز التحقق",
        "personalizations": [{"to": [{"email": "student@example.invalid"}]}],
        "content": [{"type": "text/plain", "value": "رمزك هو 123456"}],
    }


def test_message_id_supports_real_python_http_client_header_type() -> None:
    headers = _response_headers("safe-ID:123 / discarded")

    assert sendgrid_email._message_id(headers) == "safe-ID:123discarded"


def test_malformed_optional_response_headers_never_fail_an_accepted_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MalformedHeaders:
        def items(self):
            raise RuntimeError("private malformed header detail")

    monkeypatch.setattr(
        sendgrid_email.SendGridAPIClient,
        "send",
        lambda self, message: SimpleNamespace(status_code=202, headers=MalformedHeaders()),
    )

    with override_settings(**PROVIDER_SETTINGS, SENDGRID_TIMEOUT_SECONDS=15):
        message_id = sendgrid_email.send_transactional_email(
            "student@example.invalid",
            "subject",
            "body",
        )

    assert message_id == ""


@pytest.mark.parametrize(
    ("status_code", "expected_reason"),
    [
        (400, "provider_rejected"),
        (401, "provider_rejected"),
        (403, "provider_rejected"),
        (408, "provider_unavailable"),
        (429, "provider_unavailable"),
        (500, "provider_unavailable"),
    ],
)
def test_sdk_http_errors_are_classified_from_status_only(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected_reason: str,
) -> None:
    private_body = b'{"errors":[{"message":"private provider detail"}]}'
    private_headers = {"Authorization": "Bearer SG.must-not-escape"}

    def fail_send(self, message):
        raise HTTPError(status_code, "private reason", private_body, private_headers)

    monkeypatch.setattr(sendgrid_email.SendGridAPIClient, "send", fail_send)
    with (
        override_settings(**PROVIDER_SETTINGS, SENDGRID_TIMEOUT_SECONDS=15),
        pytest.raises(SendGridDeliveryError) as captured,
    ):
        sendgrid_email.send_transactional_email(
            "private-recipient@example.invalid",
            "private subject",
            "private body",
        )

    assert str(captured.value) == expected_reason
    rendered = repr(captured.value)
    assert "private" not in rendered
    assert "SG.must-not-escape" not in rendered
    assert RateLimitBucket.objects.get(key=f"{SENDGRID_SUBMISSION}:global").count == 1


@pytest.mark.parametrize(
    ("settings_override", "expected_reason"),
    [
        ({"STUDENT_OTP_SENDGRID_ENABLED": False}, "provider_disabled"),
        ({"SENDGRID_API_KEY": ""}, "configuration_missing"),
        ({"SENDGRID_FROM_EMAIL": ""}, "configuration_missing"),
        ({"SENDGRID_TIMEOUT_SECONDS": "invalid"}, "configuration_invalid"),
        ({"SENDGRID_MAX_SUBMISSIONS": 0}, "configuration_invalid"),
        ({"SENDGRID_SUBMISSION_WINDOW_SECONDS": 0}, "configuration_invalid"),
    ],
)
def test_provider_configuration_fails_before_opening_a_connection(
    monkeypatch: pytest.MonkeyPatch,
    settings_override: dict[str, object],
    expected_reason: str,
) -> None:
    def unexpected_client(*args, **kwargs):
        raise AssertionError("invalid configuration opened a provider connection")

    monkeypatch.setattr(sendgrid_email, "SendGridAPIClient", unexpected_client)
    configured = {**PROVIDER_SETTINGS, "SENDGRID_TIMEOUT_SECONDS": 15, **settings_override}
    with (
        override_settings(**configured),
        pytest.raises(SendGridDeliveryError, match=f"^{expected_reason}$"),
    ):
        sendgrid_email.send_transactional_email(
            "student@example.invalid",
            "subject",
            "body",
        )
    assert not RateLimitBucket.objects.filter(key=f"{SENDGRID_SUBMISSION}:global").exists()


def test_student_whatsapp_and_manual_test_share_one_global_provider_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Student.objects.create(student_id=4901234, name="Student", program="AI")
    AcademicAdvisor.objects.create(
        advisor_id="sg-advisor",
        full_name="Advisor",
        email="advisor@example.invalid",
        department="AI",
    )
    provider_calls = 0

    def accept_send(self, message):
        nonlocal provider_calls
        provider_calls += 1
        return SimpleNamespace(status_code=202, headers=_response_headers())

    monkeypatch.setattr(sendgrid_email.SendGridAPIClient, "send", accept_send)
    settings_override = {
        **PROVIDER_SETTINGS,
        "SENDGRID_TIMEOUT_SECONDS": 15,
        "SENDGRID_MAX_SUBMISSIONS": 2,
    }
    with override_settings(**settings_override):
        student_otp._send_code_sendgrid(4901234, "123456")
        start_link_challenge(
            wa_id="966500000099",
            phone_number="966500000099",
            university_id="sg-advisor",
        )
        with pytest.raises(CommandError, match="did not accept"):
            call_command(
                "send_test_email",
                to="operator@example.invalid",
                stdout=StringIO(),
            )

    assert provider_calls == 2
    bucket = RateLimitBucket.objects.get(key=f"{SENDGRID_SUBMISSION}:global")
    assert bucket.count == 2


def test_failed_whatsapp_submission_keeps_the_global_attempt_charge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    AcademicAdvisor.objects.create(
        advisor_id="sg-failure-advisor",
        full_name="Advisor",
        email="advisor@example.invalid",
        department="AI",
    )

    def fail_send(self, message):
        raise HTTPError(503, "private reason", b"private body", {})

    monkeypatch.setattr(sendgrid_email.SendGridAPIClient, "send", fail_send)
    with (
        override_settings(**PROVIDER_SETTINGS, SENDGRID_TIMEOUT_SECONDS=15),
        pytest.raises(OtpChallengeError),
    ):
        start_link_challenge(
            wa_id="966500000098",
            phone_number="966500000098",
            university_id="sg-failure-advisor",
        )

    assert RateLimitBucket.objects.get(key=f"{SENDGRID_SUBMISSION}:global").count == 1


def test_vendor_email_loggers_cannot_propagate_debug_payloads_or_headers() -> None:
    logging_config = project_settings.LOGGING

    assert logging_config["handlers"]["discard_vendor_email"]["class"] == ("logging.NullHandler")
    for logger_name in ("python_http_client.client", "sendgrid"):
        logger_config = logging_config["loggers"][logger_name]
        assert logger_config == {
            "handlers": ["discard_vendor_email"],
            "level": "WARNING",
            "propagate": False,
        }

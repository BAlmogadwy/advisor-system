import json
from datetime import timedelta
from typing import Any
from unittest.mock import Mock

import boto3  # type: ignore[import-untyped]
import pytest
from django.utils import timezone

from core.models import RateLimitBucket
from core.services import rate_limit, transactional_email
from core.services.transactional_email import EmailDeliveryError, send_transactional_email

RECIPIENT = "student@example.invalid"
SENDER = "advisor@example.invalid"
SUBJECT = "Student login"
BODY = "Your login code is 203040."

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def provider_settings(settings: Any) -> None:
    settings.STUDENT_OTP_EMAIL_PROVIDER = "brevo_ses"
    settings.STUDENT_OTP_FROM_EMAIL = SENDER
    settings.STUDENT_OTP_FROM_NAME = "بوابة الطالب"
    settings.STUDENT_OTP_EMAIL_TIMEOUT_SECONDS = 5
    settings.BREVO_API_KEY = "local-test-key"
    settings.BREVO_FROM_EMAIL = ""
    settings.BREVO_MAX_SUBMISSIONS = 300
    settings.AWS_ACCESS_KEY_ID = "local-aws-access-key"
    settings.AWS_SECRET_ACCESS_KEY = "local-aws-secret"
    settings.AWS_SESSION_TOKEN = ""
    settings.AWS_REGION = "ap-southeast-1"
    settings.SENDGRID_MAX_SUBMISSIONS = 4700
    settings.SENDGRID_SUBMISSION_WINDOW_SECONDS = 86_400


@pytest.fixture
def providers(monkeypatch: pytest.MonkeyPatch) -> tuple[Mock, Mock, Mock, Mock]:
    response = Mock(status=201)
    response.read.return_value = b'{"messageId":"<brevo.receipt@example.invalid>"}'
    brevo = Mock()
    brevo.return_value.getresponse.return_value = response
    monkeypatch.setattr(transactional_email.http.client, "HTTPSConnection", brevo)

    ses = Mock()
    ses.send_email.return_value = {
        "ResponseMetadata": {"HTTPStatusCode": 200},
        "MessageId": "ses-id",
    }
    ses_connection = Mock()
    ses_connection.__enter__ = Mock(return_value=ses)
    ses_connection.__exit__ = Mock(return_value=False)
    ses_factory = Mock(return_value=ses_connection)
    monkeypatch.setattr(boto3, "client", ses_factory)
    return brevo, response, ses_factory, ses


def _shared_attempts() -> int:
    return RateLimitBucket.objects.get(key=f"{rate_limit.SENDGRID_SUBMISSION}:global").count


def test_primary_accepts_one_message(providers: tuple[Mock, Mock, Mock, Mock]) -> None:
    brevo, _, ses_factory, _ = providers

    assert send_transactional_email(RECIPIENT, SUBJECT, BODY) == "brevo.receipt@example.invalid"

    brevo.assert_called_once_with("api.brevo.com", timeout=5)
    request = brevo.return_value.request
    request.assert_called_once()
    assert request.call_args.args == ("POST", "/v3/smtp/email")
    assert request.call_args.kwargs["headers"]["api-key"] == "local-test-key"
    assert json.loads(request.call_args.kwargs["body"]) == {
        "sender": {"email": SENDER, "name": "بوابة الطالب"},
        "to": [{"email": RECIPIENT}],
        "subject": SUBJECT,
        "textContent": BODY,
    }
    ses_factory.assert_not_called()
    assert _shared_attempts() == 1


@pytest.mark.parametrize("status", [400, 401, 403, 429])
def test_explicit_primary_rejection_falls_back_once_with_the_same_code(
    providers: tuple[Mock, Mock, Mock, Mock], status: int
) -> None:
    brevo, response, ses_factory, ses = providers
    response.status = status

    assert send_transactional_email(RECIPIENT, SUBJECT, BODY) == "ses-id"

    brevo.return_value.request.assert_called_once()
    ses_factory.assert_called_once()
    assert ses_factory.call_args.args == ("sesv2",)
    config = ses_factory.call_args.kwargs
    assert config["region_name"] == "ap-southeast-1"
    assert config["endpoint_url"] == "https://email.ap-southeast-1.amazonaws.com"
    assert config["config"].retries["total_max_attempts"] == 1
    assert config["config"].read_timeout == config["config"].connect_timeout == 5
    ses.send_email.assert_called_once()
    message = ses.send_email.call_args.kwargs
    assert message["FromEmailAddress"].isascii()
    assert message["FromEmailAddress"].endswith(f" <{SENDER}>")
    assert message["Destination"] == {"ToAddresses": [RECIPIENT]}
    assert message["Content"] == {
        "Simple": {
            "Subject": {"Data": SUBJECT, "Charset": "UTF-8"},
            "Body": {"Text": {"Data": BODY, "Charset": "UTF-8"}},
        }
    }
    assert _shared_attempts() == 2


def test_separate_authenticated_sender_for_each_provider(
    providers: tuple[Mock, Mock, Mock, Mock], settings: Any
) -> None:
    brevo, response, _, ses = providers
    settings.BREVO_FROM_EMAIL = "advisor@login.example.invalid"
    response.status = 429

    assert send_transactional_email(RECIPIENT, SUBJECT, BODY) == "ses-id"

    brevo_body = json.loads(brevo.return_value.request.call_args.kwargs["body"])
    assert brevo_body["sender"]["email"] == settings.BREVO_FROM_EMAIL
    assert ses.send_email.call_args.kwargs["FromEmailAddress"].endswith(f" <{SENDER}>")


@pytest.mark.parametrize("status", [301, 408, 409, 500, 502, 503, 504])
def test_uncertain_primary_response_never_sends_a_replacement(
    providers: tuple[Mock, Mock, Mock, Mock], status: int
) -> None:
    brevo, response, ses_factory, _ = providers
    response.status = status

    with pytest.raises(EmailDeliveryError, match="^provider_unavailable$"):
        send_transactional_email(RECIPIENT, SUBJECT, BODY)

    brevo.return_value.request.assert_called_once()
    ses_factory.assert_not_called()
    assert _shared_attempts() == 1


def test_timeout_before_acceptance_counts_without_fallback(
    providers: tuple[Mock, Mock, Mock, Mock], settings: Any
) -> None:
    brevo, _, ses_factory, _ = providers
    settings.BREVO_MAX_SUBMISSIONS = 1
    brevo.return_value.getresponse.side_effect = TimeoutError("sensitive request details")

    with pytest.raises(EmailDeliveryError, match="^provider_unavailable$"):
        send_transactional_email(RECIPIENT, SUBJECT, BODY)
    ses_factory.assert_not_called()

    assert send_transactional_email(RECIPIENT, SUBJECT, BODY) == "ses-id"
    brevo.return_value.request.assert_called_once()
    assert _shared_attempts() == 2


def test_timeout_reading_optional_receipt_preserves_acceptance(
    providers: tuple[Mock, Mock, Mock, Mock],
) -> None:
    brevo, response, ses_factory, _ = providers
    response.read.side_effect = TimeoutError("sensitive receipt details")

    assert send_transactional_email(RECIPIENT, SUBJECT, BODY) == ""

    brevo.return_value.request.assert_called_once()
    ses_factory.assert_not_called()
    assert _shared_attempts() == 1


def test_brevo_allowance_uses_a_strict_rolling_day(
    providers: tuple[Mock, Mock, Mock, Mock], settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    brevo, _, ses_factory, _ = providers
    settings.BREVO_MAX_SUBMISSIONS = 2
    start = timezone.now()
    current = start
    monkeypatch.setattr(transactional_email.timezone, "now", lambda: current)

    assert send_transactional_email(RECIPIENT, SUBJECT, BODY) != "ses-id"
    current = start + timedelta(hours=23, minutes=59)
    assert send_transactional_email(RECIPIENT, SUBJECT, BODY) != "ses-id"
    assert send_transactional_email(RECIPIENT, SUBJECT, BODY) == "ses-id"
    current = start + timedelta(hours=24, seconds=1)
    assert send_transactional_email(RECIPIENT, SUBJECT, BODY) != "ses-id"
    assert send_transactional_email(RECIPIENT, SUBJECT, BODY) == "ses-id"

    assert brevo.call_count == 3
    assert ses_factory.call_count == 2


def test_fallback_cannot_bypass_shared_submission_limit(
    providers: tuple[Mock, Mock, Mock, Mock], settings: Any
) -> None:
    brevo, response, ses_factory, _ = providers
    response.status = 429
    settings.SENDGRID_MAX_SUBMISSIONS = 1

    with pytest.raises(EmailDeliveryError, match="^provider_budget_exhausted$"):
        send_transactional_email(RECIPIENT, SUBJECT, BODY)

    brevo.return_value.request.assert_called_once()
    ses_factory.assert_not_called()
    assert _shared_attempts() == 1


def test_brevo_only_mode_ignores_absent_ses_credentials(
    providers: tuple[Mock, Mock, Mock, Mock], settings: Any
) -> None:
    brevo, response, ses_factory, _ = providers
    settings.STUDENT_OTP_EMAIL_PROVIDER = "brevo"
    settings.AWS_ACCESS_KEY_ID = ""
    settings.AWS_SECRET_ACCESS_KEY = ""
    settings.BREVO_MAX_SUBMISSIONS = 2

    assert send_transactional_email(RECIPIENT, SUBJECT, BODY) != "ses-id"
    response.status = 429
    with pytest.raises(EmailDeliveryError, match="^provider_rejected$"):
        send_transactional_email(RECIPIENT, SUBJECT, BODY)
    with pytest.raises(EmailDeliveryError, match="^provider_budget_exhausted$"):
        send_transactional_email(RECIPIENT, SUBJECT, BODY)

    assert brevo.call_count == 2
    ses_factory.assert_not_called()
    assert _shared_attempts() == 2

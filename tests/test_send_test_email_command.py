from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from core.services.sendgrid_email import SendGridDeliveryError


@override_settings(
    STUDENT_OTP_SENDGRID_ENABLED=True,
    SENDGRID_API_KEY="SG.private-ci-key",
    SENDGRID_FROM_EMAIL="private-sender@example.invalid",
    SENDGRID_TIMEOUT_SECONDS=15,
)
def test_send_test_email_uses_sendgrid_without_printing_addresses_or_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def fake_send(to_email: str, subject: str, body: str) -> str:
        captured.update(to_email=to_email, subject=subject, body=body)
        return "safe-message-id"

    monkeypatch.setattr(
        "core.management.commands.send_test_email.send_transactional_email", fake_send
    )
    stdout = StringIO()

    call_command("send_test_email", to="private-recipient@example.invalid", stdout=stdout)

    output = stdout.getvalue()
    assert captured["to_email"] == "private-recipient@example.invalid"
    assert captured["subject"] == "Advisor system — SendGrid delivery test"
    assert "student data" in captured["body"]
    assert "safe-message-id" in output
    assert "private-recipient@example.invalid" not in output
    assert "private-sender@example.invalid" not in output
    assert "SG.private-ci-key" not in output


@override_settings(
    STUDENT_OTP_SENDGRID_ENABLED=True,
    SENDGRID_API_KEY="SG.private-ci-key",
    SENDGRID_FROM_EMAIL="private-sender@example.invalid",
    SENDGRID_TIMEOUT_SECONDS=15,
)
def test_send_test_email_hides_provider_failure_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_send(to_email: str, subject: str, body: str) -> str:
        raise SendGridDeliveryError("private-recipient@example.invalid")

    monkeypatch.setattr(
        "core.management.commands.send_test_email.send_transactional_email", fail_send
    )

    with pytest.raises(CommandError) as captured:
        call_command("send_test_email", to="private-recipient@example.invalid")

    assert str(captured.value) == "SendGrid did not accept the test message."


@override_settings(
    STUDENT_OTP_SENDGRID_ENABLED=False,
    SENDGRID_API_KEY="",
    SENDGRID_FROM_EMAIL="",
    SENDGRID_TIMEOUT_SECONDS=15,
)
def test_send_test_email_refuses_to_send_while_provider_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_send(to_email: str, subject: str, body: str) -> str:
        raise AssertionError("disabled command attempted delivery")

    monkeypatch.setattr(
        "core.management.commands.send_test_email.send_transactional_email", unexpected_send
    )

    with pytest.raises(CommandError, match="disabled"):
        call_command("send_test_email", to="private-recipient@example.invalid")

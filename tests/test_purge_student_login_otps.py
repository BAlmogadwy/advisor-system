from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from core.models import StudentLoginOTP

pytestmark = pytest.mark.django_db


def _otp(*, student_id: int, expires_at) -> StudentLoginOTP:
    return StudentLoginOTP.objects.create(
        student_id=student_id,
        code_hash="a" * 64,
        expires_at=expires_at,
        consumed=True,
        request_ip="b" * 64,
        provider_message_id="private-provider-receipt",
    )


def test_purge_student_login_otps_is_dry_run_by_default_and_prints_only_counts() -> None:
    old = _otp(student_id=4900001, expires_at=timezone.now() - timedelta(hours=25))
    stdout = StringIO()

    call_command("purge_student_login_otps", stdout=stdout)

    assert StudentLoginOTP.objects.filter(pk=old.pk).exists()
    output = stdout.getvalue()
    assert "Would delete 1" in output
    assert "4900001" not in output
    assert "private-provider-receipt" not in output
    assert "b" * 64 not in output


def test_purge_student_login_otps_keeps_the_24_hour_diagnostic_window() -> None:
    now = timezone.now()
    old = _otp(student_id=4900001, expires_at=now - timedelta(hours=25))
    recent = _otp(student_id=4900002, expires_at=now - timedelta(hours=23))
    active = _otp(student_id=4900003, expires_at=now + timedelta(hours=1))
    stdout = StringIO()

    call_command("purge_student_login_otps", apply=True, stdout=stdout)

    assert not StudentLoginOTP.objects.filter(pk=old.pk).exists()
    assert StudentLoginOTP.objects.filter(pk=recent.pk).exists()
    assert StudentLoginOTP.objects.filter(pk=active.pk).exists()
    assert "Deleted 1" in stdout.getvalue()

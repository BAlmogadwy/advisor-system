"""Student login OTP: issue a one-time code to {student_id}@<domain>, verify it,
and lazily provision the Django user. The code is never stored in plaintext —
only a SECRET_KEY-salted SHA-256 hash, compared in constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from core.models import StudentLoginOTP
from core.services.rbac import ROLE_NAMES, ROLE_STUDENT, ensure_role_groups, set_user_scope

logger = logging.getLogger(__name__)


class OTPError(Exception):
    """Raised for rate-limit, send-failure, or account-conflict conditions."""


def student_email(student_id: int) -> str:
    return f"{student_id}@{settings.STUDENT_EMAIL_DOMAIN}"


def _hash(code: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), code.encode(), hashlib.sha256).hexdigest()


def _send_code(student_id: int, code: str) -> None:
    minutes = max(1, settings.STUDENT_OTP_TTL_SECONDS // 60)
    # Testing escape hatch: when STUDENT_OTP_REDIRECT_EMAIL is set, every code goes
    # there instead of the student's real mailbox (so testing never emails a student).
    redirect_to = getattr(settings, "STUDENT_OTP_REDIRECT_EMAIL", "")
    recipient = redirect_to or student_email(student_id)
    if redirect_to:
        logger.warning(
            "student OTP for %s redirected to %s (testing mode)", student_id, redirect_to
        )
    send_mail(
        subject="رمز الدخول / Advisor login code",
        message=(
            f"رمز الدخول الخاص بك: {code}\n"
            f"ينتهي خلال {minutes} دقيقة. لا تشارك هذا الرمز مع أحد.\n\n"
            f"Your login code is: {code}\n"
            f"It expires in {minutes} minutes. Do not share it with anyone."
            + (f"\n\n[testing] intended for {student_email(student_id)}" if redirect_to else "")
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[recipient],
        fail_silently=False,
    )


def issue_otp(student_id: int, ip: str = "") -> None:
    """Generate + email a fresh code, invalidating prior unconsumed ones.
    Email is dispatched asynchronously by default so the request returns in
    constant time (no valid-ID timing oracle) and an SMTP outage never 500s.
    Raises OTPError('too_many_requests') when the send window is exhausted."""
    now = timezone.now()
    window_start = now - timedelta(seconds=settings.STUDENT_OTP_SEND_WINDOW_SECONDS)
    recent = StudentLoginOTP.objects.filter(
        student_id=student_id, created_at__gte=window_start
    ).count()
    if recent >= settings.STUDENT_OTP_MAX_SENDS:
        raise OTPError("too_many_requests")

    code = f"{secrets.randbelow(1_000_000):06d}"
    StudentLoginOTP.objects.filter(student_id=student_id, consumed=False).update(consumed=True)
    StudentLoginOTP.objects.create(
        student_id=student_id,
        code_hash=_hash(code),
        expires_at=now + timedelta(seconds=settings.STUDENT_OTP_TTL_SECONDS),
        request_ip=(ip or "")[:64],
    )

    def _dispatch() -> None:
        try:
            _send_code(student_id, code)
        except Exception:  # noqa: BLE001 — never surface SMTP errors to the caller / 500
            logger.exception("student OTP email failed for %s", student_id)

    if getattr(settings, "STUDENT_OTP_ASYNC_EMAIL", True):
        threading.Thread(target=_dispatch, daemon=True).start()
    else:
        _dispatch()


def verify_otp(student_id: int, code: str) -> bool:
    """Consume-on-success. Atomic (select_for_update) so concurrent guesses cannot
    exceed the attempt cap. True only for a matching, unexpired, unconsumed code."""
    now = timezone.now()
    with transaction.atomic():
        otp = (
            StudentLoginOTP.objects.select_for_update()
            .filter(student_id=student_id, consumed=False, expires_at__gt=now)
            .order_by("-created_at")
            .first()
        )
        if otp is None:
            return False
        if otp.attempts >= settings.STUDENT_OTP_MAX_ATTEMPTS:
            otp.consumed = True
            otp.save(update_fields=["consumed"])
            return False
        otp.attempts += 1
        ok = hmac.compare_digest(otp.code_hash, _hash(str(code).strip()))
        otp.consumed = ok
        otp.save(update_fields=["attempts", "consumed"])
        return ok


def provision_student_user(student_id: int) -> User:
    """Get-or-create the student's Django user (username == Uni ID), never staff,
    always in the STUDENT group, with the immutable student_id persisted on scope.
    Refuses to attach to any pre-existing NON-student account (staff, superuser,
    any staff-role group, or a usable password)."""
    ensure_role_groups()
    student_group = Group.objects.get(name=ROLE_STUDENT)
    with transaction.atomic():
        user, created = User.objects.get_or_create(
            username=str(student_id), defaults={"is_staff": False, "is_superuser": False}
        )
        if created:
            user.set_unusable_password()
            user.save(update_fields=["password"])
        else:
            privileged = (
                user.is_staff
                or user.is_superuser
                or user.has_usable_password()
                or user.groups.filter(name__in=ROLE_NAMES).exists()
            )
            if privileged:
                raise OTPError("account_conflict")
        if not user.groups.filter(name=ROLE_STUDENT).exists():
            user.groups.add(student_group)
        # Inside the transaction: a user must never exist without its identity
        # binding, which is what every student-scope guard reads.
        set_user_scope(user.id, student_id=student_id)
    return user

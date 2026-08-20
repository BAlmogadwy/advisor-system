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
from django.http import HttpRequest
from django.utils import timezone

from core.models import StudentLoginOTP
from core.services.rbac import ROLE_NAMES, ROLE_STUDENT, ensure_role_groups, set_user_scope

logger = logging.getLogger(__name__)

STUDENT_AUTHENTICATED_AT_SESSION_KEY = "student_authenticated_at"
DEFAULT_RECENT_AUTH_SECONDS = 10 * 60


def _minutes_ar(value: int) -> str:
    minutes = int(value)
    if minutes == 1:
        return "دقيقة واحدة"
    if minutes == 2:
        return "دقيقتين"
    if 3 <= minutes <= 10:
        return f"{minutes} دقائق"
    return f"{minutes} دقيقة"


class OTPError(Exception):
    """Raised for rate-limit, send-failure, or account-conflict conditions."""


def mark_student_authentication(request: HttpRequest) -> None:
    """Record a successful student authentication in this browser session."""

    request.session[STUDENT_AUTHENTICATED_AT_SESSION_KEY] = int(timezone.now().timestamp())


def has_recent_student_authentication(
    request: HttpRequest, *, max_age_seconds: int | None = None
) -> bool:
    """Whether this exact session completed student authentication recently.

    ``User.last_login`` is account-wide: a login on the student's phone would make
    a stale shared-lab session look fresh. The session marker is deliberately
    browser-specific, and sessions created before this control fail closed.
    """

    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return False
    raw = request.session.get(STUDENT_AUTHENTICATED_AT_SESSION_KEY)
    try:
        authenticated_at = int(raw)
    except (TypeError, ValueError):
        return False
    configured_age = (
        getattr(
            settings,
            "TELEGRAM_LINK_AUTH_MAX_AGE_SECONDS",
            DEFAULT_RECENT_AUTH_SECONDS,
        )
        if max_age_seconds is None
        else max_age_seconds
    )
    age = int(timezone.now().timestamp()) - authenticated_at
    return 0 <= age <= max(1, int(configured_age))


def student_email(student_id: int) -> str:
    return f"{student_id}@{settings.STUDENT_EMAIL_DOMAIN}"


def _hash(code: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), code.encode(), hashlib.sha256).hexdigest()


def _send_code(student_id: int, code: str, *, recipient_override: str = "") -> None:
    minutes = max(1, settings.STUDENT_OTP_TTL_SECONDS // 60)
    # Testing escape hatch: when STUDENT_OTP_REDIRECT_EMAIL is set, every code goes
    # there instead of the student's real mailbox (so testing never emails a student).
    # It deliberately retains precedence over a caller-specific override, preserving
    # its existing global semantics for test environments that already use it.
    global_redirect = str(getattr(settings, "STUDENT_OTP_REDIRECT_EMAIL", "") or "").strip()
    explicit_redirect = str(recipient_override or "").strip()
    intended_recipient = student_email(student_id)
    recipient = global_redirect or explicit_redirect or intended_recipient
    if recipient != intended_recipient:
        # A redirect is operationally important, but neither side of that mapping
        # belongs in logs: both the student id and the test receiver are personal
        # identifiers. The email body carries the intended mailbox to the operator.
        logger.warning("student OTP redirected to a testing inbox")
    send_mail(
        subject="رمز التحقق لتسجيل الدخول / Student portal verification code",
        message=(
            f"رمز التحقق لتسجيل الدخول إلى بوابة الطالب: {code}\n"
            f"تنتهي صلاحية الرمز خلال {_minutes_ar(minutes)}. لا تشاركه مع أي شخص.\n\n"
            f"Your login code is: {code}\n"
            f"It expires in {minutes} minutes. Do not share it with anyone."
            + (
                f"\n\n[testing] intended for {intended_recipient}"
                if recipient != intended_recipient
                else ""
            )
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[recipient],
        fail_silently=False,
    )


def issue_otp(student_id: int, ip: str = "", *, recipient_override: str = "") -> None:
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
            _send_code(student_id, code, recipient_override=recipient_override)
        except Exception:  # noqa: BLE001 — never surface SMTP errors to the caller / 500
            # SMTP exceptions can echo envelope recipients. Keep both the log
            # message and its metadata identifier-free; delivery is deliberately
            # best-effort and the caller receives the enumeration-safe response.
            logger.error("student OTP email delivery failed")

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

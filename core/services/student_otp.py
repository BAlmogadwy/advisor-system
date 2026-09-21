"""Student login OTP delivery, verification, and lazy user provisioning.

The code is never stored in plaintext — only a SECRET_KEY-salted SHA-256 hash,
compared in constant time.  Delivery always uses the student's canonical
university mailbox; there is intentionally no recipient-redirect escape hatch.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import secrets
import threading
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core.mail import send_mail
from django.db import close_old_connections, transaction
from django.http import HttpRequest
from django.utils import timezone

from core.models import Student, StudentLoginOTP
from core.services.rbac import ROLE_NAMES, ROLE_STUDENT, ensure_role_groups, set_user_scope
from core.services.sendgrid_email import send_transactional_email
from core.services.student_identity import normalize_student_id, student_email

logger = logging.getLogger(__name__)

STUDENT_AUTHENTICATED_AT_SESSION_KEY = "student_authenticated_at"
DEFAULT_RECENT_AUTH_SECONDS = 10 * 60
CHANNEL_SMTP = "smtp"
CHANNEL_SENDGRID = "sendgrid"

_DELIVERY_PENDING = StudentLoginOTP.DeliveryStatus.PENDING
_DELIVERY_ACCEPTED = StudentLoginOTP.DeliveryStatus.ACCEPTED
_DELIVERY_FAILED = StudentLoginOTP.DeliveryStatus.FAILED
_DELIVERY_CANCELLED = StudentLoginOTP.DeliveryStatus.CANCELLED
_SUPPORTED_CHANNELS = {CHANNEL_SMTP, CHANNEL_SENDGRID}
_OTP_SUBJECT = "رمز التحقق لتسجيل الدخول / Student portal verification code"


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


def _hash(code: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), code.encode(), hashlib.sha256).hexdigest()


def _request_ip_fingerprint(ip: str) -> str:
    """Return a domain-separated HMAC without retaining the network address."""

    raw = str(ip or "").strip()
    if not raw:
        return ""
    try:
        normalized = ipaddress.ip_address(raw).compressed.lower()
    except ValueError:
        # Direct service callers may supply a proxy-specific token rather than an
        # address. It still must never be persisted in plaintext.
        normalized = raw.lower()
    payload = f"student-login-otp-request-ip\0{normalized}".encode()
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _otp_body(code: str) -> str:
    minutes = max(1, settings.STUDENT_OTP_TTL_SECONDS // 60)
    return (
        f"رمز التحقق لتسجيل الدخول إلى بوابة الطالب: {code}\n"
        f"تنتهي صلاحية الرمز خلال {_minutes_ar(minutes)}. لا تشاركه مع أي شخص.\n\n"
        f"Your login code is: {code}\n"
        f"It expires in {minutes} minutes. Do not share it with anyone."
    )


def _send_code(student_id: int, code: str) -> None:
    """Legacy SMTP adapter retained only for local/test compatibility."""

    send_mail(
        subject=_OTP_SUBJECT,
        message=_otp_body(code),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[student_email(student_id)],
        fail_silently=False,
    )


def _send_code_sendgrid(student_id: int, code: str) -> str:
    return send_transactional_email(
        student_email(student_id),
        _OTP_SUBJECT,
        _otp_body(code),
    )


def _lock_student(student_id: int) -> bool:
    """Lock the stable identity row used to serialize this student's OTP state."""

    try:
        Student.objects.select_for_update().only("student_id").get(student_id=student_id)
    except Student.DoesNotExist:
        return False
    return True


def _mark_delivery_failed(student_id: int, otp_id: int) -> None:
    now = timezone.now()
    with transaction.atomic():
        _lock_student(student_id)
        StudentLoginOTP.objects.filter(
            pk=otp_id,
            student_id=student_id,
            delivery_status=_DELIVERY_PENDING,
        ).update(
            consumed=True,
            delivery_status=_DELIVERY_FAILED,
            delivery_finished_at=now,
            provider_message_id="",
        )


def _finish_smtp_delivery(student_id: int, otp_id: int) -> None:
    now = timezone.now()
    with transaction.atomic():
        _lock_student(student_id)
        StudentLoginOTP.objects.filter(
            pk=otp_id,
            student_id=student_id,
            delivery_status=_DELIVERY_PENDING,
        ).update(
            delivery_status=_DELIVERY_ACCEPTED,
            delivery_finished_at=now,
        )


def _accept_sendgrid_delivery(student_id: int, otp_id: int, message_id: str) -> None:
    """Atomically make an accepted provider-backed code the sole active code."""

    now = timezone.now()
    with transaction.atomic():
        if not _lock_student(student_id):
            StudentLoginOTP.objects.filter(
                pk=otp_id,
                delivery_status=_DELIVERY_PENDING,
            ).update(
                consumed=True,
                delivery_status=_DELIVERY_CANCELLED,
                delivery_finished_at=now,
            )
            return

        candidate = (
            StudentLoginOTP.objects.select_for_update()
            .filter(pk=otp_id, student_id=student_id)
            .first()
        )
        # A successful verification may have cancelled this pending replacement
        # while its HTTP request was in flight.  Never resurrect it afterwards.
        if candidate is None or candidate.delivery_status != _DELIVERY_PENDING:
            return

        StudentLoginOTP.objects.filter(
            student_id=student_id,
            delivery_status=_DELIVERY_PENDING,
        ).exclude(pk=otp_id).update(
            consumed=True,
            delivery_status=_DELIVERY_CANCELLED,
            delivery_finished_at=now,
        )

        if candidate.expires_at <= now:
            # The provider did accept the mail, but an unusually slow response
            # must not activate a code whose validity window has already ended.
            candidate.delivery_status = _DELIVERY_ACCEPTED
            candidate.delivery_finished_at = now
            candidate.provider_message_id = message_id
            candidate.consumed = True
            candidate.save(
                update_fields=[
                    "delivery_status",
                    "delivery_finished_at",
                    "provider_message_id",
                    "consumed",
                ]
            )
            return

        # Preserve the old usable code until this exact request has a 202.  Once
        # it does, replacement and activation happen under the same student lock.
        StudentLoginOTP.objects.filter(student_id=student_id, consumed=False).exclude(
            pk=otp_id
        ).update(consumed=True)
        candidate.delivery_status = _DELIVERY_ACCEPTED
        candidate.delivery_finished_at = now
        candidate.provider_message_id = message_id
        candidate.consumed = False
        candidate.save(
            update_fields=[
                "delivery_status",
                "delivery_finished_at",
                "provider_message_id",
                "consumed",
            ]
        )


def _dispatch_code(*, student_id: int, otp_id: int, code: str, channel: str) -> None:
    """Send and persist the receipt without exposing delivery details to callers."""

    close_old_connections()
    try:
        # A newer request or a successful verification can cancel a queued worker
        # before it opens an external connection.
        if not StudentLoginOTP.objects.filter(
            pk=otp_id,
            student_id=student_id,
            delivery_status=_DELIVERY_PENDING,
        ).exists():
            return
        if channel == CHANNEL_SENDGRID:
            message_id = _send_code_sendgrid(student_id, code)
            _accept_sendgrid_delivery(student_id, otp_id, message_id)
        else:
            _send_code(student_id, code)
            _finish_smtp_delivery(student_id, otp_id)
    except Exception:  # noqa: BLE001 - provider errors must never reach the request/log
        try:
            _mark_delivery_failed(student_id, otp_id)
        except Exception:  # noqa: BLE001 - still do not log DB/provider exception details
            logger.error("student OTP delivery state update failed")
        logger.error("student OTP email delivery failed")
    finally:
        close_old_connections()


def issue_otp(
    student_id: int | str,
    ip: str = "",
    *,
    channel: str = CHANNEL_SMTP,
    min_interval_seconds: int = 0,
) -> None:
    """Create and dispatch a code under a per-student serialization lock.

    SMTP remains an explicit local/test compatibility channel.  Production views
    select SendGrid.  SendGrid candidates start inactive and replace an existing
    active code only after Mail Send returns HTTP 202.
    """

    try:
        student_id = normalize_student_id(student_id)
    except ValueError as exc:
        raise OTPError("invalid_student_id") from exc
    channel = str(channel or "").strip().lower()
    if channel not in _SUPPORTED_CHANNELS:
        raise OTPError("unsupported_channel")
    try:
        min_interval_seconds = max(0, int(min_interval_seconds))
    except (TypeError, ValueError) as exc:
        raise OTPError("invalid_interval") from exc

    now = timezone.now()
    window_start = now - timedelta(seconds=settings.STUDENT_OTP_SEND_WINDOW_SECONDS)
    code = f"{secrets.randbelow(1_000_000):06d}"
    with transaction.atomic():
        if not _lock_student(student_id):
            raise OTPError("invalid_student_id")

        recent_otps = StudentLoginOTP.objects.filter(
            student_id=student_id,
            created_at__gte=window_start,
        )
        if recent_otps.count() >= settings.STUDENT_OTP_MAX_SENDS:
            raise OTPError("too_many_requests")
        if min_interval_seconds:
            latest_created_at = (
                StudentLoginOTP.objects.filter(student_id=student_id)
                .order_by("-created_at")
                .values_list("created_at", flat=True)
                .first()
            )
            if latest_created_at is not None:
                age = (now - latest_created_at).total_seconds()
                if age < min_interval_seconds:
                    raise OTPError("too_soon")

        # Only the newest not-yet-finished request is allowed to activate.  This
        # does not touch a previously accepted active code.
        StudentLoginOTP.objects.filter(
            student_id=student_id,
            delivery_status=_DELIVERY_PENDING,
        ).update(
            consumed=True,
            delivery_status=_DELIVERY_CANCELLED,
            delivery_finished_at=now,
        )

        activate_before_delivery = channel == CHANNEL_SMTP
        if activate_before_delivery:
            StudentLoginOTP.objects.filter(student_id=student_id, consumed=False).update(
                consumed=True
            )
        candidate = StudentLoginOTP.objects.create(
            student_id=student_id,
            code_hash=_hash(code),
            expires_at=now + timedelta(seconds=settings.STUDENT_OTP_TTL_SECONDS),
            request_ip=_request_ip_fingerprint(ip),
            consumed=not activate_before_delivery,
            delivery_channel=channel,
            delivery_status=_DELIVERY_PENDING,
        )

    def _dispatch() -> None:
        _dispatch_code(
            student_id=student_id,
            otp_id=candidate.pk,
            code=code,
            channel=channel,
        )

    if getattr(settings, "STUDENT_OTP_ASYNC_EMAIL", True):
        # If a caller wrapped issuance in a wider transaction, do not let a worker
        # race a row that is not committed yet.
        transaction.on_commit(lambda: threading.Thread(target=_dispatch, daemon=True).start())
    else:
        _dispatch()


def verify_otp(student_id: int | str, code: str) -> bool:
    """Consume-on-success. Atomic (select_for_update) so concurrent guesses cannot
    exceed the attempt cap. True only for a matching, unexpired, unconsumed code."""
    try:
        student_id = normalize_student_id(student_id)
    except ValueError:
        return False

    now = timezone.now()
    with transaction.atomic():
        if not _lock_student(student_id):
            return False
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
        if ok:
            # A replacement request may already be waiting on SendGrid while the
            # student submits the previous valid code.  Cancel it under the same
            # stable student lock so its late 202 cannot change the login code.
            StudentLoginOTP.objects.filter(
                student_id=student_id,
                delivery_channel=CHANNEL_SENDGRID,
                delivery_status=_DELIVERY_PENDING,
            ).update(
                consumed=True,
                delivery_status=_DELIVERY_CANCELLED,
                delivery_finished_at=now,
            )
        return ok


def provision_student_user(student_id: int | str) -> User:
    """Get-or-create the student's Django user (username == Uni ID), never staff,
    always in the STUDENT group, with the immutable student_id persisted on scope.
    Refuses to attach to any pre-existing NON-student account (staff, superuser,
    any staff-role group, or a usable password)."""
    try:
        student_id = normalize_student_id(student_id)
    except ValueError as exc:
        raise OTPError("invalid_student_id") from exc

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

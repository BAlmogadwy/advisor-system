import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from core.models import AcademicAdvisor, Student
from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_GENERAL_ADVISOR,
    ROLE_STUDENT,
    ROLE_SUPER_ADMIN,
    get_user_role,
    get_user_scope,
)
from core.services.virtual_advisor import answer_virtual_advisor
from whatsapp_gateway.models import (
    WhatsAppConversation,
    WhatsAppMessageLog,
    WhatsAppOtpChallenge,
    WhatsAppUserLink,
)


class WhatsAppGatewayError(RuntimeError):
    """Base error for the WhatsApp gateway."""


class IdentityResolutionError(WhatsAppGatewayError):
    """Raised when a university identity cannot be safely resolved."""


class OtpChallengeError(WhatsAppGatewayError):
    """Raised when an OTP challenge cannot be started or verified."""


@dataclass(frozen=True)
class ResolvedIdentity:
    university_id: str
    role: str
    email: str
    user_id: int | None = None
    student_id: int | None = None
    advisor_id: str = ""
    departments: str = ""


def normalize_wa_id(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not local or not domain:
        return "***"
    if len(local) <= 2:
        masked_local = local[0] + "*"
    else:
        masked_local = f"{local[0]}***{local[-1]}"
    return f"{masked_local}@{domain}"


def _hash_otp(*, wa_id: str, otp: str) -> str:
    payload = f"{normalize_wa_id(wa_id)}|{otp.strip()}".encode()
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _student_email(student: Student) -> str:
    email = str(getattr(student, "email", "") or "").strip()
    if email:
        return email
    domain = str(getattr(settings, "WHATSAPP_STUDENT_EMAIL_DOMAIN", "") or "").strip()
    if domain:
        return f"{student.student_id}@{domain.lstrip('@')}"
    return ""


def resolve_university_identity(university_id: str) -> ResolvedIdentity:
    uid = str(university_id or "").strip()
    if not uid:
        raise IdentityResolutionError("University ID is required.")

    advisor = AcademicAdvisor.objects.filter(advisor_id=uid).first()
    if advisor:
        email = str(advisor.email or "").strip()
        if not email:
            raise IdentityResolutionError("This advisor does not have a registered email address.")
        return ResolvedIdentity(
            university_id=uid,
            role=ROLE_ADVISOR,
            email=email,
            advisor_id=str(advisor.advisor_id).strip(),
            departments=str(advisor.department or "").strip(),
        )

    User = get_user_model()
    user = User.objects.filter(username__iexact=uid).first()
    if user:
        role = get_user_role(user)
        if role == ROLE_SUPER_ADMIN and not bool(
            getattr(settings, "WHATSAPP_ALLOW_SUPER_ADMIN", False)
        ):
            raise IdentityResolutionError("Super admin WhatsApp access is disabled.")
        email = str(getattr(user, "email", "") or "").strip()
        if not email:
            raise IdentityResolutionError("This user does not have a registered email address.")
        scope = get_user_scope(user)
        departments = ",".join(str(x).strip().upper() for x in scope.get("departments", []))
        return ResolvedIdentity(
            university_id=uid,
            role=role,
            email=email,
            user_id=int(user.id),
            advisor_id=str(scope.get("advisor_id") or "").strip(),
            departments=departments,
        )

    student: Student | None = None
    try:
        student = Student.objects.filter(student_id=int(uid)).first()
    except ValueError:
        student = None
    if student:
        email = _student_email(student)
        if not email:
            raise IdentityResolutionError(
                "Student email is not configured yet. Add a verified student email source before "
                "enabling WhatsApp student linking."
            )
        return ResolvedIdentity(
            university_id=uid,
            role=ROLE_STUDENT,
            email=email,
            student_id=int(student.student_id),
        )

    raise IdentityResolutionError("No matching university identity was found.")


def start_link_challenge(
    *,
    wa_id: str,
    phone_number: str = "",
    university_id: str,
) -> WhatsAppOtpChallenge:
    wa_id = normalize_wa_id(wa_id)
    if not wa_id:
        raise OtpChallengeError("WhatsApp sender ID is required.")

    identity = resolve_university_identity(university_id)
    otp = _generate_otp()
    now = timezone.now()
    expires_at = now + timedelta(seconds=int(getattr(settings, "WHATSAPP_OTP_TTL_SECONDS", 300)))

    with transaction.atomic():
        WhatsAppOtpChallenge.objects.filter(
            wa_id=wa_id,
            status=WhatsAppOtpChallenge.STATUS_PENDING,
        ).update(status=WhatsAppOtpChallenge.STATUS_EXPIRED)
        challenge = WhatsAppOtpChallenge.objects.create(
            wa_id=wa_id,
            phone_number=phone_number,
            university_id=identity.university_id,
            resolved_role=identity.role,
            resolved_user_id=identity.user_id,
            resolved_student_id=identity.student_id,
            resolved_advisor_id=identity.advisor_id,
            resolved_departments=identity.departments,
            email_masked=mask_email(identity.email),
            otp_hash=_hash_otp(wa_id=wa_id, otp=otp),
            expires_at=expires_at,
        )

    send_mail(
        "Your WhatsApp advisor verification code",
        (
            "Use this code to link WhatsApp to your university advisor account:\n\n"
            f"{otp}\n\n"
            "This code expires in 5 minutes. If you did not request it, ignore this email."
        ),
        settings.DEFAULT_FROM_EMAIL,
        [identity.email],
        fail_silently=False,
    )
    return challenge


def verify_link_otp(*, wa_id: str, otp: str) -> WhatsAppUserLink:
    wa_id = normalize_wa_id(wa_id)
    code = str(otp or "").strip()
    if not wa_id or not code:
        raise OtpChallengeError("WhatsApp sender ID and OTP are required.")

    max_attempts = int(getattr(settings, "WHATSAPP_OTP_MAX_ATTEMPTS", 5))
    now = timezone.now()
    with transaction.atomic():
        challenge = (
            WhatsAppOtpChallenge.objects.select_for_update()
            .filter(wa_id=wa_id, status=WhatsAppOtpChallenge.STATUS_PENDING)
            .order_by("-created_at", "-id")
            .first()
        )
        if not challenge:
            raise OtpChallengeError("No pending OTP challenge was found.")
        if challenge.expires_at <= now:
            challenge.status = WhatsAppOtpChallenge.STATUS_EXPIRED
            challenge.save(update_fields=["status"])
            raise OtpChallengeError("OTP expired. Request a new code.")
        if challenge.attempts >= max_attempts:
            challenge.status = WhatsAppOtpChallenge.STATUS_LOCKED
            challenge.save(update_fields=["status"])
            raise OtpChallengeError("Too many OTP attempts. Request a new code.")

        expected = challenge.otp_hash
        actual = _hash_otp(wa_id=wa_id, otp=code)
        if not hmac.compare_digest(expected, actual):
            challenge.attempts += 1
            update_fields = ["attempts"]
            if challenge.attempts >= max_attempts:
                challenge.status = WhatsAppOtpChallenge.STATUS_LOCKED
                update_fields.append("status")
            challenge.save(update_fields=update_fields)
            raise OtpChallengeError("Invalid OTP.")

        challenge.status = WhatsAppOtpChallenge.STATUS_VERIFIED
        challenge.verified_at = now
        challenge.save(update_fields=["status", "verified_at"])

        link, _ = WhatsAppUserLink.objects.update_or_create(
            wa_id=wa_id,
            defaults={
                "phone_number": challenge.phone_number,
                "role": challenge.resolved_role,
                "status": WhatsAppUserLink.STATUS_ACTIVE,
                "user_id": challenge.resolved_user_id,
                "student_id": challenge.resolved_student_id,
                "advisor_id": challenge.resolved_advisor_id,
                "departments": challenge.resolved_departments,
                "verified_at": now,
                "last_seen_at": now,
                "revoked_at": None,
                "updated_at": now,
            },
        )
        WhatsAppConversation.objects.update_or_create(
            wa_id=wa_id,
            defaults={
                "state": "linked",
                "last_auth_at": now,
                "last_message_at": now,
                "step_up_required": False,
                "updated_at": now,
            },
        )
        return link


def active_link_for_wa_id(wa_id: str) -> WhatsAppUserLink | None:
    wa_id = normalize_wa_id(wa_id)
    if not wa_id:
        return None
    return (
        WhatsAppUserLink.objects.filter(wa_id=wa_id, status=WhatsAppUserLink.STATUS_ACTIVE)
        .select_related("user", "student")
        .first()
    )


def revoke_link(*, wa_id: str) -> bool:
    wa_id = normalize_wa_id(wa_id)
    now = timezone.now()
    updated = WhatsAppUserLink.objects.filter(
        wa_id=wa_id,
        status=WhatsAppUserLink.STATUS_ACTIVE,
    ).update(
        status=WhatsAppUserLink.STATUS_REVOKED,
        revoked_at=now,
        updated_at=now,
    )
    return updated > 0


def scope_for_link(link: WhatsAppUserLink) -> dict[str, Any]:
    if link.role == ROLE_STUDENT:
        return {"role": ROLE_STUDENT, "student_id": link.student_id}
    if link.user_id:
        return get_user_scope(link.user)
    if link.role == ROLE_ADVISOR:
        return {"role": ROLE_ADVISOR, "advisor_id": link.advisor_id, "departments": []}
    if link.role == ROLE_GENERAL_ADVISOR:
        departments = [x.strip().upper() for x in link.departments.split(",") if x.strip()]
        return {"role": ROLE_GENERAL_ADVISOR, "advisor_id": "", "departments": departments}
    if link.role == ROLE_SUPER_ADMIN and bool(
        getattr(settings, "WHATSAPP_ALLOW_SUPER_ADMIN", False)
    ):
        return {"role": ROLE_SUPER_ADMIN, "advisor_id": "", "departments": []}
    return {"role": ROLE_STUDENT, "student_id": -1}


def recent_history_for_wa_id(
    *, wa_id: str, latest_message: str = "", limit: int = 8
) -> list[dict[str, str]]:
    wa_id = normalize_wa_id(wa_id)
    if not wa_id:
        return []
    rows = list(
        WhatsAppMessageLog.objects.filter(wa_id=wa_id, message_type="text")
        .order_by("-created_at", "-id")
        .values("direction", "text_preview")[: max(limit + 2, 4)]
    )
    rows = list(reversed(rows))
    history: list[dict[str, str]] = []
    latest = latest_message[:500].strip()
    for idx, row in enumerate(rows):
        text = str(row.get("text_preview") or "").strip()
        if not text:
            continue
        direction = str(row.get("direction") or "")
        if direction == WhatsAppMessageLog.DIRECTION_INBOUND:
            if latest and text == latest and idx == len(rows) - 1:
                continue
            history.append({"role": "user", "content": text})
        elif direction == WhatsAppMessageLog.DIRECTION_OUTBOUND:
            history.append({"role": "assistant", "content": text})
    return history[-limit:]


def answer_for_link(
    *, link: WhatsAppUserLink, message: str, model: str | None = None
) -> dict[str, Any]:
    scope = scope_for_link(link)
    history = recent_history_for_wa_id(wa_id=link.wa_id, latest_message=message)
    if link.role == ROLE_STUDENT:
        return answer_virtual_advisor(
            question=message,
            student_id=link.student_id,
            scope=scope,
            model=model,
            history=history,
        )
    return answer_virtual_advisor(question=message, scope=scope, model=model, history=history)


def verify_meta_signature(*, body: bytes, signature_header: str) -> bool:
    secret = str(getattr(settings, "WHATSAPP_APP_SECRET", "") or "").strip()
    require_signature = bool(getattr(settings, "WHATSAPP_REQUIRE_SIGNATURE", False))
    if not secret:
        return not require_signature
    if not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def extract_text_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    extracted: list[dict[str, str]] = []
    for entry in payload.get("entry", []) if isinstance(payload.get("entry"), list) else []:
        for change in entry.get("changes", []) if isinstance(entry.get("changes"), list) else []:
            value = change.get("value") if isinstance(change, dict) else {}
            if not isinstance(value, dict):
                continue
            contacts = value.get("contacts") if isinstance(value.get("contacts"), list) else []
            phone_by_wa = {
                normalize_wa_id(item.get("wa_id")): str(item.get("wa_id") or "")
                for item in contacts
                if isinstance(item, dict)
            }
            for message in (
                value.get("messages", []) if isinstance(value.get("messages"), list) else []
            ):
                if not isinstance(message, dict):
                    continue
                wa_id = normalize_wa_id(message.get("from"))
                msg_type = str(message.get("type") or "")
                text = ""
                if msg_type == "text":
                    text_obj = message.get("text") if isinstance(message.get("text"), dict) else {}
                    text = str(text_obj.get("body") or "").strip()
                if wa_id and text:
                    extracted.append(
                        {
                            "wa_id": wa_id,
                            "phone_number": phone_by_wa.get(wa_id, wa_id),
                            "message_id": str(message.get("id") or ""),
                            "message_type": msg_type,
                            "text": text,
                        }
                    )
    return extracted


def process_inbound_text(
    *,
    wa_id: str,
    text: str,
    phone_number: str = "",
    model: str | None = None,
) -> dict[str, Any]:
    wa_id = normalize_wa_id(wa_id)
    text = str(text or "").strip()
    now = timezone.now()
    WhatsAppMessageLog.objects.create(
        wa_id=wa_id,
        direction=WhatsAppMessageLog.DIRECTION_INBOUND,
        message_type="text",
        text_preview=text[:500],
        status="received",
    )
    WhatsAppConversation.objects.update_or_create(
        wa_id=wa_id,
        defaults={"last_message_at": now, "updated_at": now},
    )

    if text.lower() in {"unlink", "logout", "remove whatsapp"}:
        revoked = revoke_link(wa_id=wa_id)
        reply = (
            "Your WhatsApp link has been removed."
            if revoked
            else "No active WhatsApp link was found."
        )
        return {"ok": True, "action": "unlink", "reply": reply}

    link = active_link_for_wa_id(wa_id)
    if link:
        link.last_seen_at = now
        link.updated_at = now
        link.save(update_fields=["last_seen_at", "updated_at"])
        result = answer_for_link(link=link, message=text, model=model)
        return {
            "ok": True,
            "action": "answered",
            "reply": str(result.get("answer") or ""),
            "result": result,
        }

    pending = WhatsAppOtpChallenge.objects.filter(
        wa_id=wa_id,
        status=WhatsAppOtpChallenge.STATUS_PENDING,
    ).exists()
    if pending and re.fullmatch(r"\d{4,8}", text):
        link = verify_link_otp(wa_id=wa_id, otp=text)
        return {
            "ok": True,
            "action": "linked",
            "reply": ("WhatsApp linked successfully. You can now ask academic advisor questions."),
            "link_id": link.id,
        }

    if not re.fullmatch(r"[A-Za-z0-9_.@-]{2,40}", text) or text.lower() in {
        "hi",
        "hello",
        "salam",
        "start",
    }:
        return {
            "ok": True,
            "action": "request_university_id",
            "reply": "Please send your university ID to link WhatsApp to your advisor account.",
        }

    try:
        challenge = start_link_challenge(wa_id=wa_id, phone_number=phone_number, university_id=text)
    except IdentityResolutionError as exc:
        return {"ok": False, "action": "identity_not_resolved", "reply": str(exc)}
    except OtpChallengeError as exc:
        return {"ok": False, "action": "otp_error", "reply": str(exc)}

    return {
        "ok": True,
        "action": "otp_sent",
        "reply": f"Verification code sent to {challenge.email_masked}. Reply with the OTP to continue.",
        "challenge_id": challenge.id,
    }

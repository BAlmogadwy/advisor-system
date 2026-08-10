"""Two doors, and they must never be confused with each other.

**The webhook** is bot-to-server. It carries no session, no cookie and no CSRF
token, and it must not: its only authority is Telegram's
`X-Telegram-Bot-Api-Secret-Token` header. It is `csrf_exempt` because there is no
browser on the other end — and that exemption is exactly why it must never read
`request.user` or act on any cookie it is sent.

**The linking pages** are browser-to-server. They are session-authenticated,
CSRF-protected, and are the only place a student identity is ever established.

Keeping them apart is the whole authentication design. The webhook can prove which
*bot* is calling and nothing about which *person*; the browser can prove which
person and nothing about which chat. A link exists only where a token issued to a
chat is confirmed inside a session belonging to a student — neither door can mint
one alone.

The secret check fails CLOSED. The WhatsApp gateway next door returns
`not require_signature` when no secret is configured, which is open-by-default in
development and closed in production only because a `DEBUG`-conditional default
happens to be set. That conditional is not copied here: an unconfigured secret is
a refusal, always.
"""

from __future__ import annotations

import hmac
import json
import logging
from datetime import datetime

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import bot, linking, messages
from .models import TelegramUpdateReceipt
from .transport import INLINE_TIMEOUT_SECONDS, send_text

logger = logging.getLogger(__name__)

_SECRET_HEADER = "HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN"


def _secret_ok(request: HttpRequest) -> bool:
    """Constant-time comparison against the configured webhook secret.

    `hmac.compare_digest` rather than `==`: the naive comparison returns as soon
    as two bytes differ, and the time it takes is a measurement of how much of the
    secret was right.

    An unset secret returns False. There is no `DEBUG` escape hatch — a
    development deployment that wants the webhook sets the same variable a
    production one does, and one that does not simply cannot be called.
    """
    expected = str(getattr(settings, "TELEGRAM_WEBHOOK_SECRET", "") or "")
    if not expected:
        logger.error("telegram: webhook refused — TELEGRAM_WEBHOOK_SECRET is not configured")
        return False
    presented = str(request.META.get(_SECRET_HEADER, "") or "")
    # Compared as BYTES. `hmac.compare_digest` accepts `str` only when both sides
    # are ASCII and raises `TypeError` otherwise — so a header carrying one
    # non-ASCII byte turned an authentication check into an unhandled 500 from an
    # unauthenticated caller. `surrogateescape` round-trips whatever WSGI decoded
    # without raising on its own.
    return hmac.compare_digest(
        presented.encode("utf-8", "surrogateescape"), expected.encode("utf-8")
    )


@csrf_exempt
@require_POST
def telegram_webhook_view(request: HttpRequest) -> JsonResponse:
    """Receive one Telegram update.

    Returns `200` for everything it accepts *and* for everything it deliberately
    ignores, because Telegram redelivers anything else — and a redelivery of an
    update we already answered is a second model call. The only non-200s are the
    ones that must stop a caller: a bad secret, and a disabled channel.

    `@require_POST` answers `405` to GET, PUT, DELETE and the rest. Telegram only
    ever POSTs; there is no verification GET in the Bot API the way there is for
    Meta's webhooks.
    """
    if not bot.is_enabled():
        # Fail closed on the flag, before the secret is even considered. A disabled
        # channel should be inert, not merely unauthenticated.
        return JsonResponse({"ok": False, "error": "disabled"}, status=404)

    if not _secret_ok(request):
        # No detail. Distinguishing "no header" from "wrong header" tells a caller
        # which half to work on.
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)

    try:
        payload = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        # Malformed JSON is not going to become well-formed on redelivery.
        return JsonResponse({"ok": True, "ignored": "unparsable"}, status=200)

    inbound = bot.parse_update(payload)
    if inbound is None:
        # Group, supergroup, channel, edited message, callback query, a bot sender,
        # a mismatched chat id. Acknowledged and dropped, with nothing sent back —
        # a reply into a group would itself be the disclosure.
        return JsonResponse({"ok": True, "ignored": "unsupported"}, status=200)

    if not bot.claim_update(inbound.update_id):
        # Already handled. Telegram is retrying a delivery whose response it never
        # saw; the work is done and must not be done again.
        return JsonResponse({"ok": True, "duplicate": True}, status=200)

    try:
        # The port THIS process is listening on, carried through so the card
        # renderer can fetch over localhost. Hard-coding it produced a silent
        # failure on every server not running on 8000.
        return _dispatch_inbound(
            inbound, server_port=str(request.META.get("SERVER_PORT", "") or "")
        )
    except Exception:
        # The receipt is claimed BEFORE the work so a concurrent redelivery cannot
        # race it. That makes it a promise the work happened — so if the work
        # raises, the promise has to be withdrawn, or this question can never be
        # asked again under this update id. Safe to release: the partial unique
        # index on (conversation, idempotency_key) is the second line of defence
        # against a double answer.
        TelegramUpdateReceipt.objects.filter(update_id=inbound.update_id).delete()
        raise


def _dispatch_inbound(inbound: bot.InboundMessage, *, server_port: str = "") -> JsonResponse:
    """Everything after the update has been authenticated and claimed."""
    if not inbound.text.strip():
        # A photo, voice note, contact, location or sticker. Nothing was fetched.
        send_text(
            chat_id=inbound.chat_id,
            text=messages.UNSUPPORTED_CONTENT,
            timeout=INLINE_TIMEOUT_SECONDS,
        )
        return JsonResponse({"ok": True}, status=200)

    link = linking.active_link_for_chat(inbound.telegram_user_id)

    if inbound.command or link is None:
        # Commands and every unlinked message resolve without the model, so they
        # are answered inline. An unlinked sender never reaches the branch below.
        for text in bot.handle_command(inbound, link):
            send_text(chat_id=inbound.chat_id, text=text, timeout=INLINE_TIMEOUT_SECONDS)
        return JsonResponse({"ok": True}, status=200)

    link.last_seen_at = _now()
    link.save(update_fields=["last_seen_at"])

    # Acknowledged, not answered. The wording matters: a progress message that
    # reads as a result is a claim the system has not earned yet.
    send_text(chat_id=inbound.chat_id, text=messages.WORKING, timeout=INLINE_TIMEOUT_SECONDS)

    from .runner import dispatch

    dispatch(
        bot.answer_question,
        link_id=link.pk,
        update_id=inbound.update_id,
        question=inbound.text,
        server_port=server_port,
    )
    return JsonResponse({"ok": True}, status=200)


def _now() -> datetime:
    from django.utils import timezone

    return timezone.now()


# ── linking: the browser half ────────────────────────────────────


@require_GET
def link_start_view(request: HttpRequest, token: str) -> HttpResponse:
    """The page the student opens from the bot.

    Not `login_required`-wrapped, because the redirect has to carry `next` and be
    rendered as a page a person can read — an anonymous visitor holding an expired
    token should be told the invitation expired, not bounced to a login screen and
    then to a confirmation for something that no longer exists.
    """
    if not bot.is_enabled():
        return render(
            request, "telegram_gateway/link_result.html", {"state": "disabled"}, status=404
        )

    invitation = linking.peek_token(token)
    if invitation is None:
        # Expired, already used, or never real — one answer for all three.
        return render(
            request, "telegram_gateway/link_result.html", {"state": "invalid"}, status=404
        )

    if not request.user.is_authenticated:
        # Into the EXISTING student login flow, with a `next` that returns here.
        # No new identity provider, and no second password prompt.
        from django.urls import reverse

        destination = reverse("telegram_link_start", args=[token])
        return redirect(f"{reverse('student_login')}?next={destination}")

    return render(
        request,
        "telegram_gateway/link_confirm.html",
        {"token": token, "privacy_text": messages.PRIVACY},
    )


@require_POST
def link_confirm_view(request: HttpRequest, token: str) -> HttpResponse:
    """Approve the invitation, and hand back the code that completes it.

    This view deliberately does NOT create the link. A token is a bearer
    credential — anybody who is sent the URL reaches this page — so approving here
    and binding here would let a forwarded link attach an attacker's chat to
    whichever student signs in and presses the button. The session proves which
    student; nothing here yet proves which chat.

    So it records the approval and returns a code that must be sent back from the
    chat the token was minted in. CSRF-protected by the project default; the
    student id is read inside `approve_link` from
    `AdvisorPrincipal.for_student(request)` and from nothing else — there is no
    student field on this form to forge.
    """
    if not bot.is_enabled():
        return render(
            request, "telegram_gateway/link_result.html", {"state": "disabled"}, status=404
        )

    if not request.user.is_authenticated:
        return render(request, "telegram_gateway/link_result.html", {"state": "signin"}, status=403)

    try:
        code = linking.approve_link(request=request, raw_token=token)
    except linking.LinkError as exc:
        status = 403 if exc.code == linking.NOT_A_STUDENT else 409
        if exc.code == linking.TOKEN_INVALID:
            status = 404
        return render(
            request,
            "telegram_gateway/link_result.html",
            {"state": exc.code},
            status=status,
        )

    # The code is rendered ONLY here, to the browser that just authenticated, and
    # is deliberately NOT sent to the chat: the whole point is that it travels the
    # opposite way to the token, so completing the link needs both halves.
    return render(
        request,
        "telegram_gateway/link_result.html",
        {"state": "approved", "confirm_code": code},
    )


@require_GET
def card_view(request: HttpRequest, token: str) -> HttpResponse:
    """Render one timetable card, for the screenshotter and nothing else.

    Signature-authenticated, **not** session-authenticated. The headless browser
    has no session and must not be handed one — minting a login so a screenshot
    can be taken is how a convenience becomes an authentication hole. The token is
    server-minted, names one stored message, and expires in three minutes; no view
    mints one for a user, and the only caller runs on this machine.

    The presentation is re-normalised on the way out even though it was
    normalised on the way in. It is being handed to a renderer, and a stored row
    written under older rules is exactly the case the whitelist exists for.
    """
    from core.models import AdvisorMessage
    from core.services.advisor_presentations import normalise_presentation

    from .cards import unsign_card
    from .rendering import images_enabled

    # Gated on the IMAGE flag, not just the channel flag. The endpoint exists only
    # to be screenshotted; with images off nothing mints a token for it, so leaving
    # it routed is surface for no purpose.
    if not bot.is_enabled() or not images_enabled():
        return HttpResponse(status=404)

    payload = unsign_card(token)
    if payload is None:
        # Expired, tampered, or never real — one answer for all three.
        return HttpResponse(status=404)

    message = AdvisorMessage.objects.filter(
        pk=payload["m"], role=AdvisorMessage.ROLE_ASSISTANT
    ).first()
    if message is None:
        return HttpResponse(status=404)

    presentation = normalise_presentation(message.presentation)
    if not presentation:
        return HttpResponse(status=404)

    from core.advisor_conversation_views import _language_of

    question = message.in_reply_to
    language = _language_of(question.content) if question else _language_of(message.content)

    response = render(
        request,
        "telegram_gateway/card.html",
        {
            "presentation": presentation,
            "language": language,
            # -1 renders the card as the screen draws it (first option open).
            "option_index": int(payload.get("i", -1)),
        },
    )
    # Never cached and never indexed: it is a student's timetable behind a
    # short-lived signature, not a page.
    response["Cache-Control"] = "no-store, private"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


@require_http_methods(["GET", "POST"])
def link_manage_view(request: HttpRequest) -> HttpResponse:
    """Let a signed-in student see and revoke their own link from the web.

    The bot's `/unlink` needs the chat to still be reachable. A student who lost
    the handset has to be able to revoke from somewhere else, and making them ask
    an administrator for that is a support ticket standing between a person and
    their own data.
    """
    from core.services.advisor_principal import AdvisorPrincipal, IdentityError
    from core.services.advisor_turn import student_id_of

    try:
        principal = AdvisorPrincipal.for_student(request)
        student_id = student_id_of(principal)
    except IdentityError:
        return render(request, "telegram_gateway/link_result.html", {"state": "signin"}, status=403)

    if request.method == "POST":
        revoked = linking.revoke_links_for_student(student_id)
        return render(
            request,
            "telegram_gateway/link_result.html",
            {"state": "unlinked" if revoked else "not_linked"},
        )

    from .models import TelegramLink

    has_link = TelegramLink.objects.filter(
        student_id=student_id, status=TelegramLink.STATUS_ACTIVE
    ).exists()
    return render(
        request,
        "telegram_gateway/link_manage.html",
        {"has_link": has_link, "privacy_text": messages.PRIVACY},
    )


__all__ = [
    "link_confirm_view",
    "link_manage_view",
    "link_start_view",
    "telegram_webhook_view",
]

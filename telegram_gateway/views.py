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
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth import logout
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from core.services.advisor_principal import AdvisorPrincipal, IdentityError
from core.services.rate_limit import TELEGRAM_COMMAND, TELEGRAM_LINK
from core.services.student_otp import has_recent_student_authentication

from . import bot, jobs, linking, messages
from .models import TelegramUpdateReceipt
from .transport import INLINE_TIMEOUT_SECONDS, send_text

logger = logging.getLogger(__name__)

_SECRET_HEADER = "HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN"
_ORDERED_COMMANDS = frozenset({"/new", "/advisor"})
# The worker is held behind this timestamp until the webhook has attempted the
# progress acknowledgement. A process crash still releases the durable job after
# a bounded delay instead of leaving it stranded.
_ACK_ACTIVATION_FALLBACK_SECONDS = max(10.0, INLINE_TIMEOUT_SECONDS + 2.0)


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

    # Durable requests claim their update id while they are enqueued below. Inline
    # requests keep the small terminal receipt used by the original gateway.
    return _dispatch_inbound(inbound)


def _dispatch_inbound(inbound: bot.InboundMessage) -> JsonResponse:
    """Claim inline updates or durably enqueue ordered linked work."""
    if not inbound.text.strip():
        # A photo, voice note, contact, location or sticker. Nothing was fetched.
        refused = _admit_inline(inbound, budget_name=TELEGRAM_COMMAND)
        if refused is not None:
            return refused
        send_text(
            chat_id=inbound.chat_id,
            text=messages.UNSUPPORTED_CONTENT,
            timeout=INLINE_TIMEOUT_SECONDS,
        )
        return JsonResponse({"ok": True}, status=200)

    link = linking.active_link_for_chat(inbound.telegram_user_id)

    # Questions, /new and /advisor all touch ordered conversation state. They are
    # persisted before acknowledgement, and the worker admits only the lowest
    # active update id for each link.
    if link is not None and (not inbound.command or inbound.command in _ORDERED_COMMANDS):
        kind = (
            TelegramUpdateReceipt.KIND_COMMAND
            if inbound.command
            else TelegramUpdateReceipt.KIND_QUESTION
        )
        try:
            available_at = (
                _now() + timedelta(seconds=_ACK_ACTIVATION_FALLBACK_SECONDS)
                if kind == TelegramUpdateReceipt.KIND_QUESTION
                else None
            )
            job, created = jobs.enqueue_question_or_command(
                update_id=inbound.update_id,
                link=link,
                kind=kind,
                payload_text=inbound.text,
                available_at=available_at,
            )
        except (jobs.AdmissionLimited, jobs.LinkUnavailable, jobs.QueueFull) as exc:
            reply = (
                messages.NEEDS_LINK
                if isinstance(exc, jobs.LinkUnavailable)
                else messages.rate_limited(getattr(exc, "retry_after", 60))
            )
            return _refuse_once(inbound, text=reply)
        if not created:
            return JsonResponse({"ok": True, "duplicate": True}, status=200)

        link.last_seen_at = _now()
        link.save(update_fields=["last_seen_at"])

        if kind == TelegramUpdateReceipt.KIND_QUESTION:
            # A progress acknowledgement is sent only after the job is durable.
            # While it is in flight, ``available_at`` keeps an external worker
            # from overtaking it with the final answer. Always release after the
            # attempt: send_text reports failure instead of raising, and the final
            # answer is still useful when Telegram lost this courtesy message.
            try:
                send_text(
                    chat_id=inbound.chat_id,
                    text=messages.WORKING,
                    timeout=INLINE_TIMEOUT_SECONDS,
                )
            finally:
                jobs.make_job_available(job.update_id)

        from .runner import dispatch_sync

        if dispatch_sync():
            jobs.run_job(job.update_id, worker_id="telegram-inline-test")
        return JsonResponse({"ok": True, "queued": True}, status=200)

    # Public/unknown commands, /unlink and every unlinked message are cheap and
    # finish inline. An exception withdraws only this terminal receipt; durable
    # jobs above are never erased after acknowledgement.
    # A linked `/unlink` is the emergency revocation path and bypasses admission.
    # Once it succeeds the chat is unlinked, so later `/unlink` spam is ordinary
    # unlinked command traffic and is bounded like everything else.
    if inbound.command == "/unlink" and link is not None:
        if not bot.claim_update(inbound.update_id):
            return JsonResponse({"ok": True, "duplicate": True}, status=200)
    else:
        budget = TELEGRAM_LINK if inbound.command in {"/link", "/confirm"} else TELEGRAM_COMMAND
        refused = _admit_inline(inbound, budget_name=budget)
        if refused is not None:
            return refused
    try:
        for text in bot.handle_command(inbound, link):
            send_text(chat_id=inbound.chat_id, text=text, timeout=INLINE_TIMEOUT_SECONDS)
        if inbound.command == "/unlink":
            jobs.cancel_jobs_for_revoked_links()
        return JsonResponse({"ok": True}, status=200)
    except Exception:
        TelegramUpdateReceipt.objects.filter(
            update_id=inbound.update_id,
            kind=TelegramUpdateReceipt.KIND_INLINE,
        ).delete()
        raise


def _admit_inline(inbound: bot.InboundMessage, *, budget_name: str) -> JsonResponse | None:
    """Claim one cheap update only after its persistent admission succeeds."""

    if TelegramUpdateReceipt.objects.filter(update_id=inbound.update_id).exists():
        return JsonResponse({"ok": True, "duplicate": True}, status=200)

    from core.services.rate_limit import consume as spend_budget
    from core.services.rate_limit import release as refund_budget

    decision = spend_budget(budget_name, int(inbound.telegram_user_id))
    if not decision.allowed:
        return _refuse_once(
            inbound,
            text=messages.rate_limited(decision.retry_after),
        )
    if bot.claim_update(inbound.update_id):
        return None

    # A concurrent delivery won the receipt after our optimistic existence check.
    # It owns the work, so this request must not consume another allowance unit.
    refund_budget(budget_name, int(inbound.telegram_user_id))
    return JsonResponse({"ok": True, "duplicate": True}, status=200)


def _refuse_once(inbound: bot.InboundMessage, *, text: str) -> JsonResponse:
    """Explain overload once per chat/window, then acknowledge it silently."""

    from core.services.rate_limit import TELEGRAM_REFUSAL_NOTICE
    from core.services.rate_limit import consume as spend_budget
    from core.services.rate_limit import release as refund_budget

    notice = spend_budget(TELEGRAM_REFUSAL_NOTICE, int(inbound.telegram_user_id))
    notified = False
    if notice.allowed:
        if not bot.claim_update(inbound.update_id):
            refund_budget(TELEGRAM_REFUSAL_NOTICE, int(inbound.telegram_user_id))
            return JsonResponse({"ok": True, "duplicate": True}, status=200)
        send_text(chat_id=inbound.chat_id, text=text, timeout=INLINE_TIMEOUT_SECONDS)
        notified = True
    return JsonResponse(
        {"ok": True, "admitted": False, "notified": notified},
        status=200,
    )


def _now() -> datetime:
    from django.utils import timezone

    return timezone.now()


# ── linking: the browser half ────────────────────────────────────


@never_cache
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

    try:
        principal = AdvisorPrincipal.for_student(request)
    except IdentityError:
        return render(
            request,
            "telegram_gateway/link_result.html",
            {"state": "not_a_student"},
            status=403,
        )
    if not has_recent_student_authentication(request):
        return render(
            request,
            "telegram_gateway/link_result.html",
            {"state": "reauth", "token": token},
            status=403,
        )

    student_suffix = str(principal.student_id)[-4:]
    return render(
        request,
        "telegram_gateway/link_confirm.html",
        {
            "token": token,
            "privacy_text": messages.PRIVACY,
            "student_suffix": student_suffix,
        },
    )


@never_cache
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

    if not has_recent_student_authentication(request):
        return render(
            request,
            "telegram_gateway/link_result.html",
            {"state": "reauth", "token": token},
            status=403,
        )

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


@never_cache
@require_POST
def link_reauthenticate_view(request: HttpRequest, token: str) -> HttpResponse:
    """End a stale browser session before the account-linking decision.

    Linking is an authentication event, not merely a page reached while some old
    session cookie happens to exist. The POST and CSRF token prevent an external
    page from silently signing a student out; the ordinary OTP login then records
    a fresh session-local authentication timestamp and returns to this token.
    """

    if not bot.is_enabled():
        return render(
            request, "telegram_gateway/link_result.html", {"state": "disabled"}, status=404
        )
    if linking.peek_token(token) is None:
        return render(
            request, "telegram_gateway/link_result.html", {"state": "invalid"}, status=404
        )

    logout(request)
    from django.urls import reverse

    destination = reverse("telegram_link_start", args=[token])
    return redirect(f"{reverse('student_login')}?next={destination}")


@require_GET
def card_view(request: HttpRequest, token: str) -> HttpResponse:
    """Render one adviser presentation card, for the screenshotter and nothing else.

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

    from .cards import unsign_card, verify_renderer_request
    from .rendering import images_enabled, presentation_images_enabled

    # Gated on the IMAGE flag, not just the channel flag. The endpoint exists only
    # to be screenshotted; with images off nothing mints a token for it, so leaving
    # it routed is surface for no purpose.
    if not bot.is_enabled() or not images_enabled():
        return HttpResponse(status=404)

    # A signed URL is necessary but not sufficient. The browser also carries a
    # separate short-lived proof in a header that access logs do not record, so a
    # URL copied from any delayed/error log is useless on the public web service.
    if not verify_renderer_request(request.headers.get("X-Telegram-Card-Renderer", "")):
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
    if not presentation or not presentation_images_enabled(presentation):
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
    response["Referrer-Policy"] = "no-referrer"
    # The shared stylesheet imports a web font on ordinary screens. A private
    # render must remain entirely on the worker-local origin: no third-party
    # request should learn that a card was rendered, even as an origin-only
    # referrer. The screenshot uses the platform fallback font when that import
    # is blocked.
    response["Content-Security-Policy"] = (
        "default-src 'none'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "connect-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    return response


@require_GET
def card_asset_view(request: HttpRequest, asset_path: str) -> HttpResponse:
    """Serve only the source assets used by the authenticated card renderer.

    This avoids coupling worker screenshots to a possibly stale collected-static
    manifest. It deliberately is not a general static route: both the separate
    renderer proof and an exact filename allowlist are required.
    """

    from .card_assets import CARD_ASSET_CONTENT_TYPES, resolve_card_asset
    from .cards import verify_renderer_request
    from .rendering import images_enabled

    if not bot.is_enabled() or not images_enabled():
        return HttpResponse(status=404)
    if not verify_renderer_request(request.headers.get("X-Telegram-Card-Renderer", "")):
        return HttpResponse(status=404)

    normalised = str(asset_path or "").replace("\\", "/").lstrip("/")
    resolved = resolve_card_asset(normalised)
    if resolved is None:
        return HttpResponse(status=404)

    try:
        body = resolved.read_bytes()
    except OSError:
        logger.warning("telegram: an allowlisted card asset could not be read")
        return HttpResponse(status=404)

    response = HttpResponse(body, content_type=CARD_ASSET_CONTENT_TYPES[normalised])
    response["Cache-Control"] = "no-store, private"
    response["X-Content-Type-Options"] = "nosniff"
    response["Referrer-Policy"] = "no-referrer"
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
    "card_asset_view",
    "card_view",
    "link_confirm_view",
    "link_manage_view",
    "link_reauthenticate_view",
    "link_start_view",
    "telegram_webhook_view",
]

"""The Telegram channel, tested as a transport rather than as a chatbot.

The order is deliberate. Webhook authenticity and chat-type filtering come first,
because everything after them assumes the request is genuine and private. Then
linking, because it is the only place an identity is created. Then idempotency,
because a duplicate is the failure mode a webhook has that a browser does not.
Only then the delivery details.

Every test that touches the adviser stubs `answer_student_advisor`, and every test
that would send a message installs a `RecordingTransport` — so a failure here is
never a bill, a real message, or a request to Alibaba.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.http import HttpResponse
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import AdvisorConversation, AdvisorMessage, Course, Student, StudentCourse
from core.services.rbac import ensure_role_groups
from telegram_gateway import bot, linking, messages
from telegram_gateway.models import TelegramLink, TelegramLinkToken, TelegramUpdateReceipt
from telegram_gateway.rendering import RecordingRenderer
from telegram_gateway.transport import RecordingTransport, set_transport

pytestmark = pytest.mark.django_db

SECRET = "a-long-webhook-secret-value"
MINE = 7101001
THEIRS = 7101002
CHAT = 55500111
OTHER_CHAT = 55500222

#: Every flag this suite depends on is PINNED here, including the ones it wants
#: off. Twice now a test read a flag's ambient value and started failing the
#: moment a developer set it in their own `.env` to try the bot — first
#: TELEGRAM_ADVISOR_ENABLED, then TELEGRAM_SEND_TIMETABLE_IMAGES. A suite whose
#: result depends on whose machine it runs on is not testing the code.
CHANNEL_ON = override_settings(
    TELEGRAM_ADVISOR_ENABLED=True,
    TELEGRAM_WEBHOOK_SECRET=SECRET,
    TELEGRAM_PUBLIC_BASE_URL="https://advisor.example.edu",
    TELEGRAM_BOT_TOKEN="",
    TELEGRAM_SEND_TIMETABLE_IMAGES=False,
    TELEGRAM_SEND_GRADUATION_IMAGES=False,
    TELEGRAM_INTERNAL_BASE_URL="",
    TELEGRAM_DISPATCH_SYNC=True,
)


@pytest.fixture
def outbox():
    """Capture every outbound Telegram message; never open a socket."""
    transport = RecordingTransport()
    set_transport(transport)
    yield transport
    set_transport(None)


def _student_row(student_id: int) -> Student:
    student, _ = Student.objects.get_or_create(
        student_id=student_id,
        defaults={"name": f"S{student_id}", "program": "CS", "section": "M"},
    )
    return student


def _student(client, student_id: int) -> User:
    """A signed-in student, provisioned by the production helper."""
    from core.services import student_otp

    ensure_role_groups()
    _student_row(student_id)
    user = student_otp.provision_student_user(student_id)
    client.force_login(user)
    session = client.session
    session[student_otp.STUDENT_AUTHENTICATED_AT_SESSION_KEY] = int(timezone.now().timestamp())
    session.save()
    return user


def _link(student_id: int = MINE, telegram_user_id: int = CHAT) -> TelegramLink:
    from core.services import student_otp

    _student_row(student_id)
    user = student_otp.provision_student_user(student_id)
    return TelegramLink.objects.create(
        telegram_user_id=telegram_user_id,
        student_id=student_id,
        university_user=user,
    )


def _complete_ceremony(client, student_id: int = MINE, telegram_user_id: int = CHAT):
    """Run the whole two-sided link: /link, approve in the browser, /confirm.

    Both halves, because either alone is meant to be insufficient — a helper that
    shortcut one of them would hide the property the ceremony exists for.
    """
    _student(client, student_id)
    issued = linking.issue_link_token(telegram_user_id=telegram_user_id)
    response = client.post(reverse("telegram_link_confirm", args=[issued.raw_token]))
    code = response.context["confirm_code"]
    return linking.confirm_link(telegram_user_id=telegram_user_id, code=code)


def _update(
    *,
    update_id: int = 1,
    text: str = "مرحبا",
    chat_type: str = "private",
    chat_id: int = CHAT,
    user_id: int | None = None,
    key: str = "message",
    is_bot: bool = False,
    omit_text: bool = False,
) -> dict:
    message: dict = {
        "message_id": 10,
        "from": {"id": CHAT if user_id is None else user_id, "is_bot": is_bot},
        "chat": {"id": chat_id, "type": chat_type},
    }
    if not omit_text:
        message["text"] = text
    return {"update_id": update_id, key: message}


def _post(client, payload: dict, *, secret: str | None = SECRET):
    headers = {}
    if secret is not None:
        headers["HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN"] = secret
    return client.post(
        reverse("telegram_webhook"),
        data=json.dumps(payload),
        content_type="application/json",
        **headers,
    )


def _fake_answer(answer="باقي لك ٣ مواد.", citations=None, agent=None, presentation=None):
    return {
        "ok": True,
        "answer": answer,
        "model": "fake-model",
        "citations": citations or [],
        "cited_policy_ids": [],
        "presentation": presentation or {},
        "agent": {"loop_used": True, "policy_grounding": "not_consulted", **(agent or {})},
    }


@pytest.mark.parametrize(
    ("answer", "forbidden"),
    [
        (
            "The scenario has 5 terms. I cannot generate or send images; the plan is below.\n\nDetails.",
            "cannot generate or send images",
        ),
        (
            "الخطة فيها 5 فصول. لا يمكنني إنشاء أو إرسال صور؛ والتفاصيل بالأسفل.\n\nالتفاصيل.",
            "لا يمكنني إنشاء أو إرسال صور",
        ),
    ],
)
def test_presentation_delivery_removes_false_model_image_incapability(answer, forbidden):
    from types import SimpleNamespace

    assistant = SimpleNamespace(
        content=answer,
        presentation=_graduation_presentation(),
        citations=SimpleNamespace(all=lambda: []),
        conversation_id="conversation-id",
    )
    result = SimpleNamespace(
        outcome="CREATED",
        assistant_message=assistant,
        student_message=SimpleNamespace(content="Show the plan image."),
    )

    rendered = bot._render_outcome(result, question="Show the plan image.")

    assert forbidden not in "\n".join(rendered)
    assert "Details." in "\n".join(rendered) or "التفاصيل." in "\n".join(rendered)


def test_presentation_delivery_preserves_facts_after_same_line_media_claim():
    from types import SimpleNamespace

    assistant = SimpleNamespace(
        content=(
            "I cannot send an image, but the verified plan estimates 6 terms "
            "including the planning baseline."
        ),
        presentation=_graduation_presentation(),
        citations=SimpleNamespace(all=lambda: []),
        conversation_id="conversation-id",
    )
    result = SimpleNamespace(
        outcome="CREATED",
        assistant_message=assistant,
        student_message=SimpleNamespace(content="Show the plan image."),
    )

    rendered = bot._render_outcome(result, question="Show the plan image.")

    assert "verified plan estimates 6 terms" in "\n".join(rendered)


def _adviser(*, answer="باقي لك ٣ مواد.", side_effect=None, **kw):
    """Patch the adviser at the ONE seam every channel goes through."""
    if side_effect is not None:
        return mock.patch(
            "core.services.student_advisor_v2.answer_student_advisor",
            side_effect=side_effect,
        )
    return mock.patch(
        "core.services.student_advisor_v2.answer_student_advisor",
        return_value=_fake_answer(answer, **kw),
    )


# ── 1-4. webhook authenticity, method and chat type ──────────────


@CHANNEL_ON
def test_a_valid_secret_is_accepted(client, outbox):
    response = _post(client, _update(text="/help"))
    assert response.status_code == 200
    assert outbox.texts, "a valid update produced no reply at all"


@CHANNEL_ON
def test_a_missing_secret_is_rejected(client, outbox):
    response = _post(client, _update(text="/help"), secret=None)
    assert response.status_code == 403
    assert outbox.sent == [], "a rejected update still sent a message"
    assert TelegramUpdateReceipt.objects.count() == 0


@CHANNEL_ON
def test_a_wrong_secret_is_rejected(client, outbox):
    response = _post(client, _update(text="/help"), secret="not-the-secret")
    assert response.status_code == 403
    assert outbox.sent == []


@override_settings(
    TELEGRAM_ADVISOR_ENABLED=True,
    TELEGRAM_WEBHOOK_SECRET="",
    TELEGRAM_PUBLIC_BASE_URL="https://advisor.example.edu",
)
def test_an_unconfigured_secret_refuses_everything(client, outbox):
    """Fail closed, and with no DEBUG escape.

    The WhatsApp gateway returns `not require_signature` when unconfigured, which
    is open-by-default and closed in production only because a DEBUG-conditional
    default happens to be set. A deployment that forgets the variable here gets a
    dead webhook, not an open one.
    """
    assert _post(client, _update(text="/help"), secret="").status_code == 403
    assert _post(client, _update(text="/help"), secret=None).status_code == 403


@CHANNEL_ON
def test_the_secret_is_compared_in_constant_time(client):
    """A byte-by-byte `==` leaks how much of the secret was right, in timing.

    Asserted structurally rather than by measurement: a timing test is flaky, and
    what actually matters is that the comparison never becomes `==` again.
    """
    import inspect

    from telegram_gateway import views

    source = inspect.getsource(views._secret_ok)
    assert "compare_digest" in source
    assert "== expected" not in source and "expected ==" not in source


@CHANNEL_ON
@pytest.mark.parametrize("method", ["get", "put", "delete", "patch"])
def test_unsupported_http_methods_are_rejected(client, method):
    response = getattr(client, method)(reverse("telegram_webhook"))
    assert response.status_code == 405


@CHANNEL_ON
def test_a_private_chat_is_accepted(client, outbox):
    assert _post(client, _update(text="/help")).status_code == 200
    assert outbox.texts


@CHANNEL_ON
@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
def test_group_and_channel_chats_are_refused_silently(client, outbox, chat_type):
    """A bot added to a class group must not answer, and must not explain itself.

    Replying at all — even to refuse — puts the university adviser's voice into a
    room full of other students, and a refusal that names the feature is an
    invitation to work out how to reach it.
    """
    _link()
    response = _post(client, _update(text="ما هي موادي؟", chat_type=chat_type))
    assert response.status_code == 200
    assert outbox.sent == [], "the gateway replied into a group chat"


@CHANNEL_ON
@pytest.mark.parametrize(
    "key", ["edited_message", "channel_post", "callback_query", "inline_query"]
)
def test_unsubscribed_update_types_are_ignored(client, outbox, key):
    _link()
    response = _post(client, _update(text="ما هي موادي؟", key=key))
    assert response.status_code == 200
    assert outbox.sent == []


@CHANNEL_ON
def test_a_message_from_a_bot_is_ignored(client, outbox):
    _link()
    assert _post(client, _update(text="ما هي موادي؟", is_bot=True)).status_code == 200
    assert outbox.sent == []


@CHANNEL_ON
def test_a_private_update_whose_chat_and_sender_disagree_is_refused(client, outbox):
    """Telegram makes them equal in a private chat, so a payload that separates
    them is forged or malformed — and treating `chat.id` as an identity there
    would let a crafted update address somebody else's link."""
    _link()
    response = _post(client, _update(text="ما هي موادي؟", chat_id=CHAT, user_id=999999))
    assert response.status_code == 200
    assert outbox.sent == []


@CHANNEL_ON
def test_media_is_refused_without_being_fetched(client, outbox):
    """No file id is resolved, no URL is opened, nothing is stored."""
    _link()
    response = _post(client, _update(omit_text=True))
    assert response.status_code == 200
    assert outbox.texts == [messages.UNSUPPORTED_CONTENT]
    assert AdvisorMessage.objects.count() == 0


@override_settings(TELEGRAM_ADVISOR_ENABLED=False, TELEGRAM_WEBHOOK_SECRET=SECRET)
def test_the_channel_is_off_by_default(client, outbox):
    response = _post(client, _update(text="/help"))
    assert response.status_code == 404
    assert outbox.sent == []


def test_the_channel_fails_closed_when_nothing_configures_it(settings):
    """Absent configuration must mean OFF, not "unspecified".

    Asserted by REMOVING the setting rather than by reading its current value. The
    first version of this test read the live value — so the moment a developer put
    `TELEGRAM_ADVISOR_ENABLED=true` in their own `.env` to try the bot, it failed
    on their machine and passed on everyone else's. What actually matters is the
    fallback inside `is_enabled`.
    """
    del settings.TELEGRAM_ADVISOR_ENABLED
    assert bot.is_enabled() is False


def test_the_settings_default_is_off_and_uses_the_strict_boolean_idiom():
    """The other half: the settings module's own default.

    The strict `== "true"` form matters. The looser `in ("1","true","yes","on")`
    idiom used by the timetable flags elsewhere in that file would read
    `TELEGRAM_ADVISOR_ENABLED=1` as False — a deployment that believed it had
    enabled the channel and had not.
    """
    from pathlib import Path

    line = next(
        ln
        for ln in Path("config/settings.py").read_text(encoding="utf-8").splitlines()
        if ln.startswith("TELEGRAM_ADVISOR_ENABLED")
    )
    assert 'os.getenv("TELEGRAM_ADVISOR_ENABLED", "false").lower() == "true"' in line


# ── 5. an unlinked sender gets instructions and nothing else ─────


@CHANNEL_ON
def test_an_unlinked_sender_is_told_to_link_and_learns_nothing(client, outbox):
    _student_row(MINE)
    with _adviser() as adviser:
        response = _post(client, _update(text="كم معدلي التراكمي؟"))
    assert response.status_code == 200
    assert outbox.texts == [messages.NEEDS_LINK]
    adviser.assert_not_called(), "the adviser ran for an unauthenticated sender"
    body = " ".join(outbox.texts)
    assert str(MINE) not in body
    assert AdvisorMessage.objects.count() == 0
    assert AdvisorConversation.objects.count() == 0


@CHANNEL_ON
@pytest.mark.parametrize("command", ["/start", "/help", "/privacy"])
def test_the_unauthenticated_command_surface_is_exactly_start_link_help_privacy(
    client, outbox, command
):
    response = _post(client, _update(text=command))
    assert response.status_code == 200
    assert outbox.texts and str(MINE) not in outbox.texts[0]


@CHANNEL_ON
@pytest.mark.parametrize("command", ["/new", "/unlink", "/advisor", "/whoami"])
def test_authenticated_commands_are_unavailable_before_linking(client, outbox, command):
    response = _post(client, _update(text=command))
    assert response.status_code == 200
    assert outbox.texts == [messages.NEEDS_LINK]


@CHANNEL_ON
def test_the_privacy_notice_does_not_claim_end_to_end_encryption(client, outbox):
    _post(client, _update(text="/privacy"))
    notice = outbox.texts[0]
    assert "ليست مشفّرة طرفًا إلى طرف" in notice
    assert "not end-to-end encrypted" in notice
    assert "/unlink" in notice
    assert "خريطة" in notice and "graduation-plan map" in notice
    assert "prerequisite links" in notice
    # The retention promise has to be accurate: unlinking revokes the mapping and
    # does NOT delete the conversation.
    assert "سجل المحادثات" in notice or "retention" in notice


# ── 6-8. the link token, and where identity comes from ───────────


@CHANNEL_ON
def test_the_link_token_is_opaque_hashed_expiring_and_single_use(client, outbox):
    _post(client, _update(text="/link"))
    token_row = TelegramLinkToken.objects.get()

    sent = outbox.texts[0]
    raw = sent.split("/telegram/link/")[1].split("/")[0]

    # Opaque: no student id, no telegram id, nothing decodable.
    assert len(raw) >= 32
    assert str(MINE) not in raw and str(CHAT) not in raw

    # Hashed at rest: the raw value appears nowhere in the row.
    assert token_row.token_hash == linking.hash_token(raw)
    assert raw not in token_row.token_hash
    assert not any(raw in str(v) for v in token_row.__dict__.values())

    # Expiring.
    assert token_row.expires_at > timezone.now()
    assert token_row.expires_at <= timezone.now() + timedelta(seconds=901)

    # Single use.
    token_row.consumed_at = timezone.now()
    token_row.save(update_fields=["consumed_at"])
    assert linking.peek_token(raw) is None


@CHANNEL_ON
def test_an_expired_token_cannot_be_approved(client):
    _student(client, MINE)
    issued = linking.issue_link_token(telegram_user_id=CHAT)
    TelegramLinkToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

    response = client.post(reverse("telegram_link_confirm", args=[issued.raw_token]))
    assert response.status_code == 404
    assert TelegramLink.objects.count() == 0


@CHANNEL_ON
def test_an_expired_approval_cannot_be_confirmed(client):
    """Expiry binds the whole ceremony, not just its first half."""
    _student(client, MINE)
    issued = linking.issue_link_token(telegram_user_id=CHAT)
    code = client.post(reverse("telegram_link_confirm", args=[issued.raw_token])).context[
        "confirm_code"
    ]

    TelegramLinkToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(linking.LinkError):
        linking.confirm_link(telegram_user_id=CHAT, code=code)
    assert TelegramLink.objects.count() == 0


@CHANNEL_ON
def test_a_confirmation_code_can_only_be_spent_once(client, outbox):
    _student(client, MINE)
    issued = linking.issue_link_token(telegram_user_id=CHAT)
    code = client.post(reverse("telegram_link_confirm", args=[issued.raw_token])).context[
        "confirm_code"
    ]

    linking.confirm_link(telegram_user_id=CHAT, code=code)
    assert TelegramLink.objects.filter(status=TelegramLink.STATUS_ACTIVE).count() == 1

    with pytest.raises(linking.LinkError):
        linking.confirm_link(telegram_user_id=CHAT, code=code)
    assert TelegramLink.objects.count() == 1


@CHANNEL_ON
def test_issuing_a_new_token_burns_the_previous_one(client, outbox):
    """Otherwise `/link` three times leaves two live invitations behind."""
    first = linking.issue_link_token(telegram_user_id=CHAT)
    second = linking.issue_link_token(telegram_user_id=CHAT)
    assert linking.peek_token(first.raw_token) is None
    assert linking.peek_token(second.raw_token) is not None


@CHANNEL_ON
def test_link_completion_takes_the_student_from_the_session(client):
    """The token says WHICH CHAT. The session says WHICH STUDENT. Never crossed."""
    link = _complete_ceremony(client)
    assert link.student_id == MINE
    assert link.telegram_user_id == CHAT


@CHANNEL_ON
def test_a_forged_student_id_in_the_form_or_url_is_ignored(client):
    """There is no student field to honour, and posting one changes nothing."""
    _student(client, MINE)
    _student_row(THEIRS)
    issued = linking.issue_link_token(telegram_user_id=CHAT)

    code = client.post(
        reverse("telegram_link_confirm", args=[issued.raw_token]),
        data={"student_id": THEIRS, "student": THEIRS, "user_id": THEIRS},
    ).context["confirm_code"]
    linking.confirm_link(telegram_user_id=CHAT, code=code)

    link = TelegramLink.objects.get()
    assert link.student_id == MINE, "a posted student id was believed"


@CHANNEL_ON
def test_an_anonymous_visitor_cannot_approve_a_link(client):
    issued = linking.issue_link_token(telegram_user_id=CHAT)
    response = client.post(reverse("telegram_link_confirm", args=[issued.raw_token]))
    assert response.status_code == 403
    assert TelegramLink.objects.count() == 0
    assert TelegramLinkToken.objects.get().approved_student_id is None


@CHANNEL_ON
def test_a_stale_student_session_must_reauthenticate_before_linking(client):
    from core.services import student_otp

    _student(client, MINE)
    session = client.session
    session[student_otp.STUDENT_AUTHENTICATED_AT_SESSION_KEY] = int(
        (timezone.now() - timedelta(minutes=11)).timestamp()
    )
    session.save()
    issued = linking.issue_link_token(telegram_user_id=CHAT)

    page = client.get(reverse("telegram_link_start", args=[issued.raw_token]))
    approval = client.post(reverse("telegram_link_confirm", args=[issued.raw_token]))

    assert page.status_code == 403
    assert page.context["state"] == "reauth"
    assert approval.status_code == 403
    assert approval.context["state"] == "reauth"
    assert TelegramLinkToken.objects.get().approved_student_id is None


@CHANNEL_ON
def test_reauthentication_is_a_csrf_protected_post_back_to_student_login(client):
    _student(client, MINE)
    issued = linking.issue_link_token(telegram_user_id=CHAT)

    response = client.post(reverse("telegram_link_reauthenticate", args=[issued.raw_token]))

    assert response.status_code == 302
    assert response["Location"].startswith(reverse("student_login"))
    assert "next=" in response["Location"]
    assert "_auth_user_id" not in client.session


@CHANNEL_ON
def test_confirmation_page_identifies_only_the_masked_account_suffix(client):
    _student(client, MINE)
    issued = linking.issue_link_token(telegram_user_id=CHAT)

    response = client.get(reverse("telegram_link_start", args=[issued.raw_token]))

    body = response.content.decode()
    assert response.status_code == 200
    assert str(MINE)[-4:] in body
    assert str(MINE) not in body


@CHANNEL_ON
def test_a_non_student_account_cannot_approve_a_link(client):
    """`get_user_role` falls back to ADVISOR for an authenticated account with no
    group. A channel that accepted that fallback would hand adviser-tier identity
    to a chat bot, so linking asserts STUDENT explicitly."""
    ensure_role_groups()
    staff = User.objects.create_user("some-staff", password="x")  # noqa: S106
    client.force_login(staff)
    issued = linking.issue_link_token(telegram_user_id=CHAT)

    response = client.post(reverse("telegram_link_confirm", args=[issued.raw_token]))
    assert response.status_code == 403
    assert TelegramLink.objects.count() == 0


# ── the forwarded-link attack, and why the ceremony has two halves ──


@CHANNEL_ON
def test_approving_a_forwarded_link_does_not_link_anything(client):
    """THE attack this ceremony exists to stop.

    An attacker types /link in their OWN chat, forwards the URL to a student, and
    the student — on the university's real domain, through the real login — signs
    in and presses confirm. If approving were linking, the attacker's chat would
    now be bound to the student's record and could read their GPA, remaining
    courses and timetable. The confirmation page cannot warn them: it stores no
    Telegram profile data, so it has no chat to name.

    Approving must therefore bind nothing.
    """
    attacker_chat = 99900001
    issued = linking.issue_link_token(telegram_user_id=attacker_chat)

    # The victim, authenticated for real, presses the button on a forwarded URL.
    _student(client, MINE)
    response = client.post(reverse("telegram_link_confirm", args=[issued.raw_token]))

    assert response.status_code == 200
    assert TelegramLink.objects.count() == 0, "a forwarded link bound an account"
    assert linking.active_link_for_chat(attacker_chat) is None


@CHANNEL_ON
def test_the_confirmation_code_is_shown_only_to_the_browser(client, outbox):
    """It travels browser -> chat, the opposite way to the token. Sending it to
    the chat as well would hand both halves to whoever holds the forwarded URL."""
    attacker_chat = 99900001
    issued = linking.issue_link_token(telegram_user_id=attacker_chat)
    _student(client, MINE)

    outbox.sent.clear()
    response = client.post(reverse("telegram_link_confirm", args=[issued.raw_token]))
    code = response.context["confirm_code"]

    assert code and code not in " ".join(outbox.texts)
    assert outbox.sent == [], "the code was delivered to the chat as well"
    assert code.encode() in response.content, "the browser was not shown the code"


@CHANNEL_ON
def test_a_victim_who_follows_a_forwarded_link_cannot_complete_it_from_their_chat(client):
    """The safe failure that makes this direction the right one.

    A student who was sent a link and then does the natural thing — open the bot
    and try to confirm — finds no approval for THEIR chat, because the token was
    minted in the attacker's. Nothing links, and the student is not quietly bound
    to somebody else's ceremony.
    """
    attacker_chat = 99900001
    issued = linking.issue_link_token(telegram_user_id=attacker_chat)
    _student(client, MINE)
    code = client.post(reverse("telegram_link_confirm", args=[issued.raw_token])).context[
        "confirm_code"
    ]

    victim_chat = 77700002
    with pytest.raises(linking.LinkError):
        linking.confirm_link(telegram_user_id=victim_chat, code=code)
    assert TelegramLink.objects.count() == 0


@CHANNEL_ON
def test_a_confirmation_code_is_useless_in_a_chat_it_was_not_minted_for(client):
    """Scoping the lookup to the calling chat is what stops an approved code from
    being spendable anywhere it is typed."""
    _student(client, MINE)
    issued = linking.issue_link_token(telegram_user_id=CHAT)
    code = client.post(reverse("telegram_link_confirm", args=[issued.raw_token])).context[
        "confirm_code"
    ]

    with pytest.raises(linking.LinkError):
        linking.confirm_link(telegram_user_id=OTHER_CHAT, code=code)
    assert TelegramLink.objects.count() == 0
    # And the real chat can still finish.
    assert linking.confirm_link(telegram_user_id=CHAT, code=code).student_id == MINE


@CHANNEL_ON
def test_a_wrong_code_is_refused_and_bounded(client, outbox):
    """Guessing is capped, and the cap burns the approval rather than leaving it
    standing for the next attempt."""
    _student(client, MINE)
    issued = linking.issue_link_token(telegram_user_id=CHAT)
    real = client.post(reverse("telegram_link_confirm", args=[issued.raw_token])).context[
        "confirm_code"
    ]

    for _ in range(linking.MAX_CONFIRM_ATTEMPTS):
        with pytest.raises(linking.LinkError):
            linking.confirm_link(telegram_user_id=CHAT, code="ZZZZZZ")

    # The cap is reached: even the CORRECT code no longer works.
    with pytest.raises(linking.LinkError):
        linking.confirm_link(telegram_user_id=CHAT, code=real)
    assert TelegramLink.objects.count() == 0


@CHANNEL_ON
def test_confirm_is_reachable_from_the_chat_end_to_end(client, outbox):
    """The whole ceremony, driven the way a student drives it."""
    _post(client, _update(update_id=1, text="/link"))
    invitation = outbox.texts[-1]
    raw = invitation.split("/telegram/link/")[1].split("/")[0]
    assert "/confirm" in invitation, "the invitation never mentions the second step"

    _student(client, MINE)
    code = client.post(reverse("telegram_link_confirm", args=[raw])).context["confirm_code"]

    outbox.sent.clear()
    _post(client, _update(update_id=2, text=f"/confirm {code}"))

    assert outbox.texts == [messages.LINK_CONFIRMED]
    assert linking.active_link_for_chat(CHAT).student_id == MINE


@CHANNEL_ON
def test_confirmation_code_response_is_never_browser_cacheable(client):
    _student(client, MINE)
    issued = linking.issue_link_token(telegram_user_id=CHAT)

    response = client.post(reverse("telegram_link_confirm", args=[issued.raw_token]))

    assert response.status_code == 200
    assert "/confirm" in response.content.decode("utf-8")
    cache_control = response.headers.get("Cache-Control", "")
    assert "no-store" in cache_control
    assert "max-age=0" in cache_control


@CHANNEL_ON
def test_confirm_with_no_code_explains_itself(client, outbox):
    _post(client, _update(text="/confirm"))
    assert outbox.texts == [messages.CONFIRM_USAGE]


@CHANNEL_ON
def test_confirm_with_nothing_pending_reveals_nothing(client, outbox):
    """One answer for a wrong code, an expired approval, and a chat with no
    approval at all — telling them apart says whether an approval exists."""
    _post(client, _update(text="/confirm ABC123"))
    assert outbox.texts == [messages.CONFIRM_INVALID]


@CHANNEL_ON
def test_link_command_has_a_persistent_per_chat_admission_budget(client, outbox):
    from core.services.rate_limit import LIMITS, TELEGRAM_LINK

    max_calls, _window = LIMITS[TELEGRAM_LINK]
    for index in range(max_calls + 1):
        _post(client, _update(update_id=8000 + index, text="/link"))

    assert TelegramLinkToken.objects.count() == max_calls
    assert "طلبات كثيرة" in outbox.texts[-1]


@CHANNEL_ON
def test_unlink_is_never_blocked_by_the_command_budget(client, outbox):
    from core.services.rate_limit import LIMITS, TELEGRAM_COMMAND
    from core.services.rate_limit import consume as spend_budget

    link = _link()
    max_calls, _window = LIMITS[TELEGRAM_COMMAND]
    for _ in range(max_calls):
        assert spend_budget(TELEGRAM_COMMAND, CHAT).allowed

    _post(client, _update(update_id=8100, text="/unlink"))

    link.refresh_from_db()
    assert link.status == TelegramLink.STATUS_REVOKED
    assert outbox.texts[-1] == messages.UNLINKED


@CHANNEL_ON
def test_a_confirmation_code_is_stored_only_as_a_hash(client):
    _student(client, MINE)
    issued = linking.issue_link_token(telegram_user_id=CHAT)
    code = client.post(reverse("telegram_link_confirm", args=[issued.raw_token])).context[
        "confirm_code"
    ]

    token = TelegramLinkToken.objects.get()
    assert token.confirm_code_hash == linking.hash_token(code)
    assert not any(code in str(v) for v in token.__dict__.values() if v is not None)


@CHANNEL_ON
def test_the_link_page_sends_an_anonymous_visitor_into_the_existing_login_flow(client):
    issued = linking.issue_link_token(telegram_user_id=CHAT)
    response = client.get(reverse("telegram_link_start", args=[issued.raw_token]))
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("student_login"))
    assert "next=" in response["Location"]


@CHANNEL_ON
def test_the_linking_url_carries_no_student_identifier(client, outbox):
    _post(client, _update(text="/link"))
    url = outbox.texts[0]
    assert str(MINE) not in url
    assert "student" not in url.split("/telegram/link/")[1].split("/")[0].lower()


# ── redirect-after-login, added so the link can survive sign-in ──


@CHANNEL_ON
def test_signing_in_returns_the_student_to_the_confirmation_page(client):
    """`login_required` has always emitted `?next=`; the student login views
    discarded it, so there was no way back from sign-in to a linking page."""
    from core.services import student_otp

    ensure_role_groups()
    _student_row(MINE)
    issued = linking.issue_link_token(telegram_user_id=CHAT)
    destination = reverse("telegram_link_start", args=[issued.raw_token])

    client.get(f"{reverse('student_login')}?next={destination}")
    user = student_otp.provision_student_user(MINE)
    client.force_login(user)

    response = client.get(f"{reverse('student_login')}?next={destination}")
    assert response.status_code == 302
    assert response["Location"] == destination


@pytest.mark.parametrize(
    "hostile",
    [
        # Caught by the explicit shape check.
        "https://evil.example/steal",
        "//evil.example/steal",
        "http://evil.example",
        r"\\evil.example",
        "javascript:alert(1)",
        # Caught ONLY by `url_has_allowed_host_and_scheme`: each starts with a
        # single slash, so the shape check waves it through, and each is a
        # protocol-relative URL once the browser or Django folds the separator.
        # Without these the validator could be deleted and this test stay green.
        r"/\evil.example",
        r"/\/evil.example",
        r"/\\evil.example",
        "/\t/evil.example",
    ],
)
def test_an_off_site_next_is_never_followed(client, hostile):
    """The parameter is attacker-controlled: it arrives in a URL a student can be
    sent. An unvalidated one turns the university's own login into an open
    redirect, which is the standard way a phishing page borrows a real domain.

    Completed through the OTP step rather than by revisiting the login page: a
    bare GET clears the stored destination outright, so asserting through that
    path would stay green with every validation line deleted.
    """
    ensure_role_groups()
    _student_row(MINE)

    client.get(f"{reverse('student_login')}?next={hostile}")
    session = client.session
    session["otp_student_id"] = MINE
    session.save()

    with mock.patch("core.student_auth_views.verify_otp", return_value=True):
        response = client.post(reverse("student_otp_verify"), data={"code": "123456"})

    assert response.status_code == 302
    assert "evil.example" not in response["Location"]
    assert response["Location"] == reverse("student_home")


def test_a_staff_session_never_follows_a_students_next(client):
    """The destination was chosen for a student session and may not be theirs."""
    ensure_role_groups()
    staff = User.objects.create_user("staffer", password="x")  # noqa: S106

    client.get(f"{reverse('student_login')}?next=/student/advisor/")
    client.force_login(staff)
    response = client.get(reverse("student_login"))

    assert response.status_code == 302
    assert response["Location"] == reverse("dashboard")


def test_login_still_lands_on_student_home_when_nothing_asked_otherwise(client):
    """The pre-existing behaviour, unchanged for every ordinary sign-in."""
    from core.services import student_otp

    ensure_role_groups()
    _student_row(MINE)
    client.force_login(student_otp.provision_student_user(MINE))
    response = client.get(reverse("student_login"))
    assert response["Location"] == reverse("student_home")


# ── 9-11. one chat, one student, and revocation ──────────────────


@CHANNEL_ON
def test_one_telegram_account_cannot_reach_another_student(client, outbox):
    """The link is the only identity, so a second chat gets its own student."""
    _link(student_id=MINE, telegram_user_id=CHAT)
    _link(student_id=THEIRS, telegram_user_id=OTHER_CHAT)

    captured = []

    def _capture(**kwargs):
        captured.append(kwargs["principal"].student_id)
        return _fake_answer()

    with mock.patch(
        "core.services.student_advisor_v2.answer_student_advisor", side_effect=_capture
    ):
        _post(client, _update(update_id=1, text="موادي؟", chat_id=CHAT, user_id=CHAT))
        _post(
            client,
            _update(update_id=2, text="موادي؟", chat_id=OTHER_CHAT, user_id=OTHER_CHAT),
        )

    assert captured == [MINE, THEIRS]


@CHANNEL_ON
def test_a_student_cannot_hold_two_active_links(client):
    """Enforced by a partial unique index, not only by a check in Python."""
    from django.db import IntegrityError, transaction

    _link(student_id=MINE, telegram_user_id=CHAT)
    with pytest.raises(IntegrityError), transaction.atomic():
        TelegramLink.objects.create(telegram_user_id=OTHER_CHAT, student_id=MINE)


@CHANNEL_ON
def test_a_chat_cannot_hold_two_active_links(client):
    from django.db import IntegrityError, transaction

    _link(student_id=MINE, telegram_user_id=CHAT)
    with pytest.raises(IntegrityError), transaction.atomic():
        TelegramLink.objects.create(telegram_user_id=CHAT, student_id=THEIRS)


@CHANNEL_ON
def test_linking_a_chat_that_belongs_to_another_student_is_refused(client):
    _link(student_id=THEIRS, telegram_user_id=CHAT)
    _student(client, MINE)
    issued = linking.issue_link_token(telegram_user_id=CHAT)

    response = client.post(reverse("telegram_link_confirm", args=[issued.raw_token]))
    assert response.status_code == 409
    assert TelegramLink.objects.filter(status=TelegramLink.STATUS_ACTIVE).count() == 1
    assert TelegramLink.objects.get(status=TelegramLink.STATUS_ACTIVE).student_id == THEIRS


@CHANNEL_ON
def test_unlink_revokes_access_immediately(client, outbox):
    _link()
    assert _post(client, _update(update_id=1, text="/unlink")).status_code == 200
    assert outbox.texts[-1] == messages.UNLINKED

    outbox.sent.clear()
    with _adviser() as adviser:
        _post(client, _update(update_id=2, text="كم معدلي؟"))
    adviser.assert_not_called()
    assert outbox.texts == [messages.NEEDS_LINK]


@CHANNEL_ON
def test_a_revoked_link_frees_the_student_to_link_again(client):
    link = _link()
    link.revoke()
    _complete_ceremony(client, student_id=MINE, telegram_user_id=OTHER_CHAT)
    assert TelegramLink.objects.filter(status=TelegramLink.STATUS_ACTIVE).count() == 1


@CHANNEL_ON
def test_an_administrator_can_revoke_by_student(client):
    """The safe direction: staff hold a student number, never a Telegram id."""
    _link()
    assert linking.revoke_links_for_student(MINE) == 1
    assert linking.active_link_for_chat(CHAT) is None


def test_the_admin_cannot_create_or_edit_a_link():
    """A link minted by staff never passed through the student's own session."""
    from django.contrib import admin as django_admin

    from telegram_gateway.admin import TelegramLinkAdmin

    site = django_admin.site
    model_admin = TelegramLinkAdmin(TelegramLink, site)
    assert model_admin.has_add_permission(None) is False
    assert model_admin.has_change_permission(None) is False
    assert model_admin.has_delete_permission(None) is False


# ── 12. idempotency ──────────────────────────────────────────────


@CHANNEL_ON
def test_a_duplicate_update_produces_one_turn_and_one_model_call(client, outbox):
    """Telegram redelivers anything it does not get a prompt 200 for — including
    a delivery that timed out AFTER the answer was generated."""
    _link()
    payload = _update(update_id=4242, text="كم مادة باقية؟")

    with _adviser() as adviser:
        first = _post(client, payload)
        second = _post(client, payload)

    assert first.status_code == 200 and second.status_code == 200
    assert second.json().get("duplicate") is True
    assert adviser.call_count == 1
    assert AdvisorMessage.objects.filter(role=AdvisorMessage.ROLE_STUDENT).count() == 1
    assert AdvisorMessage.objects.filter(role=AdvisorMessage.ROLE_ASSISTANT).count() == 1
    assert TelegramUpdateReceipt.objects.count() == 1


@CHANNEL_ON
def test_the_turn_layer_also_holds_when_the_receipt_is_lost(client, outbox):
    """Belt and braces: if the receipt table were wiped between retries, the
    partial unique index on (conversation, idempotency_key) still refuses a
    second stored turn and replays the stored answer."""
    _link()
    payload = _update(update_id=99, text="كم مادة باقية؟")

    with _adviser() as adviser:
        _post(client, payload)
        TelegramUpdateReceipt.objects.all().delete()
        _post(client, payload)

    assert adviser.call_count == 1
    assert AdvisorMessage.objects.filter(role=AdvisorMessage.ROLE_ASSISTANT).count() == 1


@CHANNEL_ON
def test_question_new_and_followup_are_durably_processed_in_link_order(client, outbox):
    from telegram_gateway import jobs

    link = _link()
    with mock.patch("telegram_gateway.runner.dispatch_sync", return_value=False):
        _post(client, _update(update_id=7001, text="سؤال أول"))
        _post(client, _update(update_id=7002, text="/new"))
        _post(client, _update(update_id=7003, text="سؤال ثاني"))

    assert (
        list(TelegramUpdateReceipt.objects.order_by("update_id").values_list("status", flat=True))
        == [TelegramUpdateReceipt.STATUS_QUEUED] * 3
    )

    with _adviser() as adviser:
        for _ in range(3):
            jobs.run_next_job(worker_id="ordered-test")

    assert adviser.call_count == 2
    questions = list(
        AdvisorMessage.objects.filter(role=AdvisorMessage.ROLE_STUDENT).order_by("created_at")
    )
    assert len(questions) == 2
    assert questions[0].conversation_id != questions[1].conversation_id
    link.refresh_from_db()
    assert link.current_conversation_id == questions[1].conversation_id
    assert not TelegramUpdateReceipt.objects.exclude(
        status=TelegramUpdateReceipt.STATUS_SUCCEEDED
    ).exists()


@CHANNEL_ON
def test_external_worker_cannot_overtake_the_progress_acknowledgement(client, outbox):
    """The durable row exists first, but is not claimable until WORKING returns."""

    _link()
    observed: dict[str, object] = {}

    def inspect_during_send(*, chat_id, text, timeout):
        assert chat_id == CHAT
        assert text == messages.WORKING
        assert timeout == 3.0
        queued = TelegramUpdateReceipt.objects.get(update_id=7099)
        observed["status"] = queued.status
        observed["available_at"] = queued.available_at
        observed["now"] = timezone.now()
        return {"ok": True}

    with (
        mock.patch("telegram_gateway.runner.dispatch_sync", return_value=False),
        mock.patch("telegram_gateway.views.send_text", side_effect=inspect_during_send),
    ):
        response = _post(client, _update(update_id=7099, text="سؤال"))

    assert response.status_code == 200
    assert observed["status"] == TelegramUpdateReceipt.STATUS_QUEUED
    assert observed["available_at"] > observed["now"]
    queued = TelegramUpdateReceipt.objects.get(update_id=7099)
    assert queued.available_at <= timezone.now()


@CHANNEL_ON
def test_unlink_immediately_cancels_queued_questions(client, outbox):
    from telegram_gateway import jobs

    _link()
    with mock.patch("telegram_gateway.runner.dispatch_sync", return_value=False):
        _post(client, _update(update_id=7101, text="سؤال خاص"))

    _post(client, _update(update_id=7102, text="/unlink"))
    queued = TelegramUpdateReceipt.objects.get(update_id=7101)
    assert queued.status == TelegramUpdateReceipt.STATUS_CANCELLED
    assert queued.payload_text == ""

    with _adviser() as adviser:
        assert jobs.run_next_job(worker_id="after-unlink") is None
    adviser.assert_not_called()


@CHANNEL_ON
@override_settings(TELEGRAM_MAX_PENDING_PER_LINK=1)
def test_webhook_refuses_more_work_when_the_links_durable_queue_is_full(client, outbox):
    _link()
    with mock.patch("telegram_gateway.runner.dispatch_sync", return_value=False):
        first = _post(client, _update(update_id=7151, text="السؤال الأول"))
        second = _post(client, _update(update_id=7152, text="السؤال الثاني"))

    assert first.json()["queued"] is True
    assert second.json()["admitted"] is False
    assert TelegramUpdateReceipt.objects.get(update_id=7151).status == (
        TelegramUpdateReceipt.STATUS_QUEUED
    )
    assert TelegramUpdateReceipt.objects.get(update_id=7152).status == (
        TelegramUpdateReceipt.STATUS_SUCCEEDED
    )
    assert "طلبات كثيرة" in outbox.texts[-1]


@CHANNEL_ON
def test_durable_ingress_is_bounded_even_when_the_worker_drains_each_job(
    client, outbox, monkeypatch
):
    from core.services import rate_limit

    _link()
    monkeypatch.setitem(rate_limit.LIMITS, rate_limit.TELEGRAM_INGRESS, (2, 600))

    with _adviser() as adviser:
        first = _post(client, _update(update_id=7161, text="السؤال الأول"))
        second = _post(client, _update(update_id=7162, text="السؤال الثاني"))
        refused = _post(client, _update(update_id=7163, text="السؤال الثالث"))

    assert first.json()["queued"] is True
    assert second.json()["queued"] is True
    assert refused.json()["admitted"] is False
    assert adviser.call_count == 2
    refused_receipt = TelegramUpdateReceipt.objects.get(update_id=7163)
    assert refused_receipt.kind == TelegramUpdateReceipt.KIND_INLINE
    assert refused_receipt.status == TelegramUpdateReceipt.STATUS_SUCCEEDED
    assert "طلبات كثيرة" in outbox.texts[-1]

    sent_after_notice = len(outbox.sent)
    for offset in range(10):
        repeated = _post(
            client,
            _update(update_id=7170 + offset, text=f"ضغط إضافي {offset}"),
        )
        assert repeated.json()["admitted"] is False
        assert repeated.json()["notified"] is False

    assert len(outbox.sent) == sent_after_notice
    assert TelegramUpdateReceipt.objects.count() == 3


@CHANNEL_ON
def test_unlinked_unlink_spam_is_bounded_after_the_security_override_is_irrelevant(
    client, outbox, monkeypatch
):
    from core.services import rate_limit

    monkeypatch.setitem(rate_limit.LIMITS, rate_limit.TELEGRAM_COMMAND, (2, 600))
    for offset in range(10):
        _post(client, _update(update_id=7180 + offset, text="/unlink"))

    # Two admitted explanations plus one rate-limit notice; everything after it
    # is acknowledged silently and creates no idempotency row.
    assert len(outbox.sent) == 3
    assert TelegramUpdateReceipt.objects.count() == 3


@CHANNEL_ON
def test_new_command_side_effect_is_idempotent_before_delivery_is_materialised(client):
    from telegram_gateway import jobs

    link = _link()
    job, created = jobs.enqueue_question_or_command(
        update_id=7201,
        link=link,
        kind=TelegramUpdateReceipt.KIND_COMMAND,
        payload_text="/new",
    )
    assert created

    first = bot.execute_durable_job(job)
    second = bot.execute_durable_job(job)

    assert first["conversation_id"] == second["conversation_id"]
    assert AdvisorConversation.objects.filter(student_id=MINE).count() == 1


@CHANNEL_ON
def test_an_update_without_an_update_id_is_refused(client, outbox):
    """No update id means no idempotency key, and no idempotency key means a
    retry becomes a second answer."""
    _link()
    payload = _update(text="سؤال")
    payload.pop("update_id")
    with _adviser() as adviser:
        assert _post(client, payload).status_code == 200
    adviser.assert_not_called()


# ── 13-14. conversations ─────────────────────────────────────────


@CHANNEL_ON
def test_new_starts_a_fresh_conversation_owned_by_the_student(client, outbox):
    link = _link()
    with _adviser():
        _post(client, _update(update_id=1, text="سؤال أول"))
    link.refresh_from_db()
    first = link.current_conversation
    assert first is not None and first.student_id == MINE

    _post(client, _update(update_id=2, text="/new"))
    link.refresh_from_db()
    assert link.current_conversation is not None
    assert link.current_conversation.pk != first.pk
    assert link.current_conversation.student_id == MINE
    assert outbox.texts[-1] == messages.NEW_CONVERSATION


@CHANNEL_ON
def test_a_follow_up_sees_only_this_students_own_history(client, outbox):
    """The history handed to the model is scoped to one conversation object the
    student is already proved to own — so cross-student bleed is impossible here
    rather than merely unlikely."""
    _link(student_id=MINE, telegram_user_id=CHAT)

    other = AdvisorConversation.objects.create(student_id=THEIRS)
    AdvisorMessage.objects.create(
        conversation=other,
        role=AdvisorMessage.ROLE_STUDENT,
        content="سؤال الطالب الآخر السري",
        status=AdvisorMessage.STATUS_COMPLETED,
    )

    seen = []

    def _capture(**kwargs):
        seen.append(kwargs.get("history") or [])
        return _fake_answer("جواب")

    with mock.patch(
        "core.services.student_advisor_v2.answer_student_advisor", side_effect=_capture
    ):
        _post(client, _update(update_id=1, text="سؤالي الأول"))
        _post(client, _update(update_id=2, text="والثاني؟"))

    assert seen[0] == []
    contents = [t["content"] for t in seen[1]]
    assert "سؤالي الأول" in contents
    assert not any("السري" in c for c in contents)


@CHANNEL_ON
def test_new_does_not_carry_the_previous_thread_forward(client, outbox):
    _link()
    seen = []

    def _capture(**kwargs):
        seen.append(kwargs.get("history") or [])
        return _fake_answer("جواب")

    with mock.patch(
        "core.services.student_advisor_v2.answer_student_advisor", side_effect=_capture
    ):
        _post(client, _update(update_id=1, text="سؤال أول"))
        _post(client, _update(update_id=2, text="/new"))
        _post(client, _update(update_id=3, text="سؤال ثانٍ"))

    assert seen[-1] == [], "a fresh conversation inherited the old thread"


# ── 15-16. the capability surface does not change ────────────────


@CHANNEL_ON
def test_the_channel_calls_the_same_seam_the_web_uses(client, outbox):
    """Not `answer_virtual_advisor` directly. The WhatsApp gateway imports V1 by
    name and pins itself to it forever; going through the flagged seam means
    Telegram gets whatever the web gets."""
    import inspect

    from telegram_gateway import bot as bot_module

    source = inspect.getsource(bot_module)
    assert "answer_virtual_advisor" not in source
    assert "advisor_turn" in source


@CHANNEL_ON
def test_the_adviser_is_called_with_a_self_only_student_principal(client, outbox):
    """And the call BINDS against the real signature, so a signature change that
    would `TypeError` on every live message fails here instead."""
    import inspect

    from core.services.rbac import ROLE_STUDENT
    from core.services.student_advisor_v2 import answer_student_advisor_v2
    from core.services.virtual_advisor import answer_virtual_advisor

    _link()
    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        # Both sides of the feature flag must accept this call.
        inspect.signature(answer_student_advisor_v2).bind(**kwargs)
        inspect.signature(answer_virtual_advisor).bind(**kwargs)
        return _fake_answer()

    with mock.patch(
        "core.services.student_advisor_v2.answer_student_advisor", side_effect=_capture
    ):
        _post(client, _update(text="موادي؟"))

    principal = captured["principal"]
    assert principal.role == ROLE_STUDENT
    assert principal.student_id == MINE
    assert principal.advisor_id == ""
    assert principal.departments == ()
    assert principal.as_scope() == {"role": ROLE_STUDENT, "student_id": MINE}
    # Identity travels as ONE object. A second channel is how the two disagree.
    assert "student_id" not in captured and "scope" not in captured
    assert "llm_client" not in captured and "client" not in captured


@CHANNEL_ON
def test_no_write_capability_is_reachable_from_telegram(client, outbox):
    """The channel exposes no tool surface of its own, and the student principal
    it builds is the same read-only one the web chat uses."""
    import telegram_gateway.bot as bot_module
    from core.services.student_advisor_v2 import FORBIDDEN_STUDENT_V2_TOOLS, STUDENT_V2_TOOL_NAMES

    source = __import__("inspect").getsource(bot_module)
    for forbidden in FORBIDDEN_STUDENT_V2_TOOLS:
        assert forbidden not in source
    # The gateway names no tool at all — it is a transport.
    assert not [name for name in STUDENT_V2_TOOL_NAMES if name in source]


@CHANNEL_ON
def test_an_unsupported_write_request_is_still_refused(client, outbox):
    """The refusal comes from the adviser, unchanged. The channel neither adds a
    capability nor softens a refusal."""
    _link()
    refusal = "لا أستطيع تسجيل المواد نيابةً عنك. التسجيل يتم عبر البوابة."
    with _adviser(answer=refusal):
        _post(client, _update(text="سجّل لي مادة AI351"))
    assert refusal in " ".join(outbox.texts)


# ── 17-18. formatting, splitting and injection ───────────────────


def test_a_long_answer_splits_without_dropping_the_sources():
    from telegram_gateway.formatting import SAFE_CHUNK_CHARS, render_answer

    class _Citation:
        document_title = "دليل الطالب"
        edition = "1445"
        page = "24"

    body = "\n\n".join(["فقرة طويلة جدًا " * 60 for _ in range(6)])
    chunks = render_answer(answer=body, citations=[_Citation()])

    assert len(chunks) > 1
    assert all(len(c) <= SAFE_CHUNK_CHARS for c in chunks)
    # The sources survive, whole, and at the end.
    assert "المصادر:" in chunks[-1]
    assert "دليل الطالب" in chunks[-1] and "ص 24" in chunks[-1]
    assert sum(c.count("المصادر:") for c in chunks) == 1
    # Nothing is lost: every word of the body is still somewhere.
    assert "فقرة" in " ".join(chunks)


def test_english_answer_localises_its_citation_and_platform_footer():
    from telegram_gateway.formatting import render_answer

    class _Citation:
        document_title = "Student Guide"
        edition = "1447"
        page = "24"

    rendered = "\n".join(
        render_answer(
            answer="You may take the course.",
            citations=[_Citation()],
            web_url="https://advisor.example.edu/student/advisor/?c=example",
            has_presentation=True,
            language="English",
        )
    )

    assert "Sources:" in rendered
    assert "Student Guide, 1447, p. 24" in rendered
    assert "View the full plan and details on the platform:" in rendered
    assert "المصادر:" not in rendered
    assert "على المنصة" not in rendered


def test_splitting_never_cuts_a_word_in_half():
    from telegram_gateway.formatting import split_message

    text = " ".join(f"كلمة{i}" for i in range(2000))
    chunks = split_message(text, limit=500)
    rejoined = " ".join(chunks)
    for i in (0, 999, 1999):
        assert f"كلمة{i}" in rejoined


def test_a_short_answer_is_one_message():
    from telegram_gateway.formatting import render_answer

    assert render_answer(answer="باقي لك ٣ مواد.") == ["باقي لك ٣ مواد."]


def test_internal_policy_ids_are_hidden_but_readable_sources_remain():
    from telegram_gateway.formatting import render_answer

    class _Citation:
        document_title = "الدليل الإرشادي للطالب والطالبة"
        edition = "1447"
        page = "23"

    rendered = "\n".join(
        render_answer(
            answer=(
                "الحد الأعلى هو 19 وحدة، ص 23 [TU.LOAD.SEMESTER_RANGE]. "
                "وهذا [NOT.A.REAL.POLICY] لا ينبغي أن يظهر أيضًا."
            ),
            citations=[_Citation()],
        )
    )

    assert "19 وحدة، ص 23." in rendered
    assert "[TU." not in rendered and "[NOT." not in rendered
    assert "المصادر:" in rendered
    assert "الدليل الإرشادي للطالب والطالبة، 1447، ص 23" in rendered


def test_model_strong_markers_are_removed_from_arabic_and_english_answers():
    from telegram_gateway.formatting import render_answer

    answer = (
        "**الخيار 1:** جدول بلا تعارض\n"
        "- **1448/2:** DS321 وDS332\n"
        "هل أسجل **DS491**؟ نعم،**إذا تحققت المتطلبات**.\n"
        "Visit **https://example.edu/path**.\n"
        "See **[student portal](https://example.edu/path)**.\n"
        "**Assumptions:** **read-only scenario**"
    )

    assert render_answer(answer=answer) == [
        "الخيار 1: جدول بلا تعارض\n"
        "- 1448/2: DS321 وDS332\n"
        "هل أسجل DS491؟ نعم،إذا تحققت المتطلبات.\n"
        "Visit https://example.edu/path.\n"
        "See [student portal](https://example.edu/path).\n"
        "Assumptions: read-only scenario"
    ]


def test_strong_marker_cleanup_preserves_code_links_and_ambiguous_markers():
    from telegram_gateway.formatting import render_answer

    answer = (
        r"Keep *single* _single_ [label](https://example.edu/a_b) `**code**` "
        r"\**escaped** **unfinished"
        "\n**first line\nsecond line**"
        "\n```text\n**fenced code**\n```"
        "\n__init__ CS__LAB__1 https://example.edu/__private__/file"
        "\nCS**LAB**1 src/**/foo/**/bar https://example.edu/**private**/file"
        "\n**/foo/** [portal](https://example.edu/find?q=**DS491**)"
        "\nx.**y**.z 2+**3**+4"
        "\nhttps://example.edu/(**DS491**) **DS*.csv**"
    )

    assert render_answer(answer=answer) == [answer]


def test_unmatched_ticks_and_markers_do_not_hide_later_valid_strong_text():
    from telegram_gateway.formatting import render_answer

    answer = "before \\` **again**\n**unfinished then **valid**\nbefore ` **later**"

    assert render_answer(answer=answer) == [
        "before \\` again\n**unfinished then valid\nbefore ` later"
    ]


def test_strong_markers_can_wrap_protected_inline_code():
    from telegram_gateway.formatting import render_answer

    answer = "**Use `DS321` now** and **before `code` after** and **call `func(**kwargs)` now**."

    assert render_answer(answer=answer) == [
        "Use `DS321` now and before `code` after and call `func(**kwargs)` now."
    ]


def test_backslash_before_inline_code_close_does_not_unprotect_its_contents():
    from telegram_gateway.formatting import render_answer

    answer = "`x **literal**.\\` **outside**"

    assert render_answer(answer=answer) == ["`x **literal**.\\` outside"]


def test_longer_closing_fence_keeps_inner_markers_literal():
    from telegram_gateway.formatting import render_answer

    answer = "```text\n**fenced code**\n````\n**outside**"

    assert render_answer(answer=answer) == ["```text\n**fenced code**\n````\noutside"]


def test_backticks_inside_fence_do_not_close_it_early():
    from telegram_gateway.formatting import render_answer

    answer = (
        "```text\n"
        "some ``` literal\n"
        "some ```` longer literal\n"
        "**still fenced code**\n"
        "```\n"
        "**outside**"
    )

    assert render_answer(answer=answer) == [
        "```text\nsome ``` literal\nsome ```` longer literal\n**still fenced code**\n```\noutside"
    ]


def test_line_start_triple_inline_code_does_not_shield_later_prose():
    from telegram_gateway.formatting import render_answer

    answer = "```code``` **outside**"

    assert render_answer(answer=answer) == ["```code``` outside"]


def test_nested_strong_cleanup_is_idempotent_without_a_depth_cap():
    from telegram_gateway.formatting import render_answer

    answer = "answer"
    for _ in range(20):
        answer = f"**level {answer} end**"

    rendered = render_answer(answer=answer)

    assert rendered == ["level " * 20 + "answer" + " end" * 20]
    assert render_answer(answer=rendered[0]) == rendered


def test_malformed_marker_sequence_is_preserved_instead_of_cross_paired():
    from telegram_gateway.formatting import render_answer

    answer = "**unfinished:**valid**"

    assert render_answer(answer=answer) == [answer]


def test_strong_markers_are_removed_before_message_chunking():
    from telegram_gateway.formatting import render_answer

    answer = "\n\n".join(f"**Section {index}:** " + ("word " * 20) for index in range(5))
    chunks = render_answer(answer=answer, limit=80)

    assert len(chunks) > 1
    assert all(len(chunk) <= 80 for chunk in chunks)
    assert all("**" not in chunk for chunk in chunks)
    assert all(f"Section {index}:" in " ".join(chunks) for index in range(5))


def test_atx_heading_prefixes_are_removed_from_arabic_and_english_answers():
    from telegram_gateway.formatting import render_answer

    answer = (
        "### الفصل الدراسي 1448/2\r\n"
        "المقررات: IS251 وDS321\r\n"
        "# Graduation summary\r\n"
        "##\tEligibility\r\n"
        "   ###### Assumptions"
    )

    rendered = render_answer(answer=answer)
    assert rendered == [
        "الفصل الدراسي 1448/2\r\nالمقررات: IS251 وDS321\r\nGraduation summary\r\n"
        "Eligibility\r\nAssumptions"
    ]
    assert render_answer(answer=rendered[0]) == rendered


def test_atx_heading_cleanup_preserves_code_urls_and_identifier_like_hashes():
    from telegram_gateway.formatting import render_answer

    answer = (
        "    ### leading indented code\n"
        "\t### tab-indented code\n"
        "`### inline code`\n"
        "```text\n### fenced code\n```\n"
        "~~~text\n**tilde strong**\n### tilde-fenced code\n~~~\n"
        "https://example.edu/plan#semester and [plan](https://example.edu/#term)\n"
        "#DS491 ###AI351 ##/plan C:\\\\plans\\###\\term\n"
        "# 4501789\n"
        "# ٤٥٠١٧٨٩\n"
        "\\### escaped heading\n"
        "####### seven hashes\n"
        "> ### quoted text\n"
        "- ### list text\n"
        "###\n"
        "###   \n"
        "~~~text\n### unclosed tilde fence"
    )

    assert render_answer(answer=answer) == [answer]


@pytest.mark.parametrize("answer", ["\t### tab-indented code", "    ### four-space code"])
def test_indented_code_is_preserved_across_repeated_rendering(answer):
    from telegram_gateway.formatting import render_answer

    first_render = render_answer(answer=answer)

    assert first_render == [answer]
    assert render_answer(answer=first_render[0]) == first_render


@pytest.mark.parametrize(
    ("answer", "expected_first"),
    [
        (("x" * 45) + "\n    ### code", "x" * 45),
        (("x" * 44) + " \n    ### code", "x" * 44),
    ],
)
def test_chunking_does_not_expose_an_indented_code_line_as_a_heading(answer, expected_first):
    from telegram_gateway.formatting import render_answer

    chunks = render_answer(answer=answer, limit=45)

    assert chunks == [expected_first, "    ### code"]
    assert [render_answer(answer=chunk, limit=45)[0] for chunk in chunks] == chunks


def test_atx_heading_cleanup_happens_before_chunking_across_a_fence():
    from telegram_gateway.formatting import render_answer

    answer = "~~~text\n" + ("code word " * 12) + "\n### literal code\n~~~\n### Outside"
    chunks = render_answer(answer=answer, limit=45)
    rendered = "\n".join(chunks)

    assert len(chunks) > 1
    assert all(len(chunk) <= 45 for chunk in chunks)
    assert "### literal code" in rendered
    assert "### Outside" not in rendered
    assert "Outside" in rendered


def test_foreign_fence_openers_stay_inside_the_active_fence():
    from telegram_gateway.formatting import render_answer

    answers = (
        "~~~text\n``` literal opener\n**literal strong**\n### literal heading\n~~~\n"
        "### Outside\n**outside strong**",
        "```text\n~~~ literal opener\n**literal strong**\n### literal heading\n```\n"
        "### Outside\n**outside strong**",
    )

    for answer in answers:
        rendered = render_answer(answer=answer)[0]
        assert "**literal strong**\n### literal heading" in rendered
        assert rendered.endswith("Outside\noutside strong")
        assert "### Outside" not in rendered


@CHANNEL_ON
def test_atx_heading_cleanup_is_telegram_only(client, outbox):
    raw_answer = "### الخطة المقترحة\n**Term 1:** DS321"
    _link()

    with _adviser(answer=raw_answer):
        _post(client, _update(text="اعرض خطتي"))

    stored = AdvisorMessage.objects.get(role=AdvisorMessage.ROLE_ASSISTANT)
    assert stored.content == raw_answer
    assert outbox.texts[-1] == "الخطة المقترحة\nTerm 1: DS321"
    assert all("parse_mode" not in message for message in outbox.sent)


def test_an_empty_answer_sends_nothing():
    """Telegram rejects empty text, and a rejected send looks like an outage."""
    from telegram_gateway.formatting import render_answer, split_message

    assert render_answer(answer="   ") == []
    assert split_message("") == []


@CHANNEL_ON
def test_telegram_formatting_is_never_interpreted(client, outbox):
    """Only strong hints are unwrapped; no model markup is interpreted."""
    _link()
    hostile = "**النتيجة:** المعدل *1.0* _تحذير_ [رابط](http://evil.example) `**code**` ~~شطب~~"
    with _adviser(answer=hostile):
        _post(client, _update(text="سؤال"))

    rendered = " ".join(outbox.texts)
    assert "النتيجة: المعدل" in rendered
    assert "**النتيجة:**" not in rendered
    assert "*1.0* _تحذير_ [رابط](http://evil.example) `**code**` ~~شطب~~" in rendered
    assert all("parse_mode" not in m for m in outbox.sent)


def test_the_http_transport_never_sets_a_parse_mode():
    import inspect

    from telegram_gateway.transport import HttpTelegramTransport

    source = inspect.getsource(HttpTelegramTransport)
    assert '"parse_mode"' not in source and "'parse_mode'" not in source


def test_the_markdown_escaper_escapes_every_reserved_character():
    """Unused by the delivery path, but present — so a future markup mode has one
    correct implementation rather than an ad-hoc one per call site."""
    from telegram_gateway.formatting import escape_markdown_v2

    for char in r"_*[]()~`>#+-=|{}.!":
        assert escape_markdown_v2(char) == "\\" + char
    assert escape_markdown_v2("a\\b") == "a\\\\b"


@CHANNEL_ON
def test_a_structured_card_becomes_a_link_to_the_web_screen(client, outbox):
    """The adviser keeps its prose short because the web chat draws a timetable
    card. On a text channel the answer alone is incomplete, so the student is
    pointed at the screen that already draws it rather than having it rebuilt in
    chat messages."""
    from core.services.advisor_presentations import KIND_TIMETABLE, normalise_presentation

    _link()
    # A REAL payload, run through the server whitelist first — a fabricated shape
    # is normalised to `{}` and the test would pass for the wrong reason (or, as
    # it did, fail while the code was right).
    presentation = {
        "kind": KIND_TIMETABLE,
        "baseline_kind": "REGISTERED",
        "baseline_sections": [
            {
                "course_code": "AI351",
                "course_name": "Machine Learning",
                "section": "M1",
                "credits": 3,
                "meetings": ["SUN 09:00-10:15"],
            }
        ],
    }
    assert normalise_presentation(presentation), "the fixture is not a renderable card"

    with _adviser(answer="هذا جدولك.", presentation=presentation):
        _post(client, _update(text="ابنِ لي جدولًا"))

    body = " ".join(outbox.texts)
    assert "https://advisor.example.edu/student/advisor/" in body
    # The card itself is NOT rebuilt in chat messages.
    assert "Machine Learning" not in body

    # The `?c=` is what makes the link WORK. `page-student-advisor.js` bootstraps
    # its thread from that one parameter and calls `openConversation` only when it
    # is present, so a link without it renders the sidebar and no messages — an
    # empty page where the student's answer should be. Found in live testing.
    conversation = AdvisorConversation.objects.get(student_id=MINE)
    assert f"/student/advisor/?c={conversation.id}" in body


@CHANNEL_ON
def test_the_web_link_survives_signing_in(client, outbox):
    """A student following the link on a phone is usually signed out.

    `login_required` puts the FULL path — query string included — into `?next=`,
    and the redirect-after-login added for this feature has to carry it through,
    or the student lands on an empty adviser screen after logging in.
    """
    from django.urls import reverse as _reverse

    _student_row(MINE)
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    destination = f"/student/advisor/?c={conversation.id}"

    ensure_role_groups()
    client.get(f"{_reverse('student_login')}?next={destination}")
    session = client.session
    session["otp_student_id"] = MINE
    session.save()

    with mock.patch("core.student_auth_views.verify_otp", return_value=True):
        response = client.post(_reverse("student_otp_verify"), data={"code": "123456"})

    assert response.status_code == 302
    assert response["Location"] == destination, "the conversation id was lost at login"


# ── 19. safe failure ─────────────────────────────────────────────


@CHANNEL_ON
def test_a_model_failure_produces_a_safe_arabic_answer(client, outbox):
    _link()
    with _adviser(side_effect=RuntimeError("model down")):
        response = _post(client, _update(text="سؤال"))

    assert response.status_code == 200, "a model failure made Telegram redeliver"
    body = " ".join(outbox.texts)
    assert messages.GENERATION_FAILED in body
    # No exception class, no subsystem name: varying the input and reading back
    # which error came out is a free map of what just broke.
    assert "RuntimeError" not in body and "model down" not in body
    assert "Traceback" not in body


@CHANNEL_ON
def test_a_missing_student_record_produces_a_safe_arabic_answer(client, outbox):
    _link()
    with _adviser(side_effect=ValueError("No student record exists")):
        _post(client, _update(text="سؤال"))
    assert messages.NO_STUDENT_RECORD in " ".join(outbox.texts)


@CHANNEL_ON
def test_a_telegram_delivery_failure_does_not_re_run_the_model(client):
    """A send that fails is a notification lost, not a turn to redo. Raising
    would make the webhook non-200, and a non-200 makes Telegram redeliver."""
    _link()
    failing = RecordingTransport(fail_with=OSError("telegram unreachable"))
    set_transport(failing)
    try:
        with _adviser() as adviser:
            response = _post(client, _update(text="سؤال"))
        assert response.status_code == 200
        assert adviser.call_count == 1
    finally:
        set_transport(None)

    # The answer is stored even though it could not be delivered.
    assert AdvisorMessage.objects.filter(role=AdvisorMessage.ROLE_ASSISTANT).count() == 1


@CHANNEL_ON
def test_the_rate_limit_is_the_students_shared_generation_budget(client, outbox):
    """Not a Telegram-specific allowance. Every door onto generation draws on the
    same budget, so no door becomes a way around the others."""
    from core.services.rate_limit import GENERATION, LIMITS

    _link()
    max_calls, _window = LIMITS[GENERATION]

    with _adviser():
        for i in range(max_calls + 1):
            _post(client, _update(update_id=100 + i, text=f"سؤال {i}"))

    assert any("طلبات كثيرة" in t for t in outbox.texts)


@CHANNEL_ON
def test_an_unconfigured_bot_token_never_opens_a_socket():
    """The repo has been bitten once by a client reaching the network from a test;
    `forbid_llm_network` only covers `core.services.llm_backend`."""
    from telegram_gateway.transport import HttpTelegramTransport

    with override_settings(TELEGRAM_BOT_TOKEN=""):
        result = HttpTelegramTransport().send_message(chat_id=1, text="x")
    assert result == {"ok": False, "skipped": True, "reason": "telegram_not_configured"}


# ── 20. logs carry no secrets and no identifiers ─────────────────


@CHANNEL_ON
def test_no_secret_or_identifier_reaches_the_logs(client, outbox, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    _link()
    with _adviser(answer="جوابك السري"):
        _post(client, _update(text="سؤالي الخاص جدًا"))
    _post(client, _update(update_id=2, text="/link"))

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET not in logged
    assert "سؤالي الخاص جدًا" not in logged, "message content was logged"
    assert "جوابك السري" not in logged, "answer content was logged"
    assert str(MINE) not in logged, "a student id was logged"
    assert str(CHAT) not in logged, "a full chat id was logged"
    for text in outbox.texts:
        if "/telegram/link/" in text:
            raw = text.split("/telegram/link/")[1].split("/")[0]
            assert raw not in logged, "a link token was logged"


def test_no_raw_update_payload_is_stored():
    """Only normalised work text is temporary; the raw Telegram body has no field."""
    field_names = {f.name for f in TelegramUpdateReceipt._meta.get_fields()}
    assert not ({"raw_update", "raw_payload", "telegram_payload", "profile"} & field_names)

    from telegram_gateway import jobs

    link = _link()
    job, _ = jobs.enqueue_question_or_command(
        update_id=991,
        link=link,
        kind=TelegramUpdateReceipt.KIND_QUESTION,
        payload_text="temporary private question",
    )
    jobs.run_job(
        job.update_id,
        executor=lambda _job: {"messages": [], "result_code": "tested"},
    )
    job.refresh_from_db()
    assert job.payload_text == ""
    assert job.delivery_payload == {}


def test_no_telegram_profile_information_is_stored():
    """No username, no display name, no phone number, no photo — none of it is
    needed to deliver an answer, and a column that exists gets filled."""
    field_names = {f.name for f in TelegramLink._meta.get_fields()}
    for forbidden in ("username", "first_name", "last_name", "phone", "phone_number", "photo"):
        assert not any(forbidden in name for name in field_names), forbidden


@pytest.mark.parametrize(
    "question",
    [
        "كم معدلي التراكمي؟",
        "وش جبت في CS113؟",
        "هل نجحت في DS341؟",
        "ايش المواد اللي رسبت فيها؟",
        "What did I get in CS113?",
        "What grade did I get in CS113?",
        "What is my CGPA?",
        "Show me the grades on my transcript",
        "List the courses I failed",
        "How many failed classes are on my record?",
        "What courses have I not passed?",
        "What about mine?",
        "Did I fail DS341?",
        "Show my academic standing",
        "اعطني نتيجة مقرر CS113",
        "اعرض سجلي الأكاديمي",
        "طيب وش عني؟",
    ],
)
def test_exact_personal_results_require_the_authenticated_web_surface(question):
    assert bot.requires_secure_record_surface(question)


@pytest.mark.parametrize(
    "question",
    [
        "كيف ينحسب المعدل التراكمي؟",
        "هل الرسوب يؤثر على المعدل؟",
        "How is GPA calculated?",
        "What is the passing grade policy?",
    ],
)
def test_general_grade_policy_questions_remain_available_in_telegram(question):
    assert not bot.requires_secure_record_surface(question)


@CHANNEL_ON
def test_a_personal_grade_request_never_calls_the_model_or_enters_chat_history(client, outbox):
    _link()
    with _adviser() as adviser:
        response = _post(client, _update(text="وش جبت في CS113؟"))

    assert response.status_code == 200
    adviser.assert_not_called()
    assert AdvisorMessage.objects.count() == 0
    assert "authenticated student portal" in " ".join(outbox.texts)
    receipt = TelegramUpdateReceipt.objects.get()
    assert receipt.status == TelegramUpdateReceipt.STATUS_SUCCEEDED
    assert receipt.result_code == "secure_surface_required"
    assert receipt.payload_text == ""


@CHANNEL_ON
@pytest.mark.parametrize(
    "answer, forbidden",
    [
        ("Your CGPA is 2.86 and your grade in CS113 is B.", "2.86"),
        ("معدلك التراكمي 2.86 ودرجتك في CS113 هي ب.", "2.86"),
    ],
)
def test_a_model_that_volunteers_a_personal_result_is_blocked_before_telegram_delivery(
    client, outbox, answer, forbidden
):
    _link()
    with _adviser(answer=answer):
        response = _post(client, _update(text="Which courses should I take next?"))

    assert response.status_code == 200
    delivered = " ".join(outbox.texts)
    assert forbidden not in delivered
    assert "authenticated student portal" in delivered
    assert AdvisorMessage.objects.filter(
        role=AdvisorMessage.ROLE_ASSISTANT,
        content=answer,
    ).exists(), "the complete answer should remain available on the authenticated web surface"
    receipt = TelegramUpdateReceipt.objects.get()
    assert receipt.result_code == "secure_output_withheld"
    student_turn = AdvisorMessage.objects.get(role=AdvisorMessage.ROLE_STUDENT)
    assert student_turn.generation_profile == "telegram_withheld"


@CHANNEL_ON
def test_a_withheld_answer_never_reenters_later_telegram_history(client, outbox):
    _link()
    with _adviser(answer="Your CGPA is 2.86."):
        _post(client, _update(update_id=8200, text="Which courses should I take?"))

    seen: list[list[dict[str, str]]] = []

    def capture(**kwargs):
        seen.append(kwargs.get("history") or [])
        return _fake_answer("A safe answer.")

    with mock.patch("core.services.student_advisor_v2.answer_student_advisor", side_effect=capture):
        _post(client, _update(update_id=8201, text="Tell me more."))

    assert seen == [[]]


@CHANNEL_ON
def test_a_withheld_replay_stays_withheld_after_the_student_record_changes():
    """The durable decision, not a fresh comparison with mutable rows, wins."""

    from telegram_gateway import jobs

    student = _student_row(MINE)
    student.gpa = 2.86
    student.save(update_fields=["gpa"])
    link = _link()
    job, created = jobs.enqueue_question_or_command(
        update_id=8202,
        link=link,
        kind=TelegramUpdateReceipt.KIND_QUESTION,
        payload_text="Which courses should I take next?",
    )
    assert created

    with _adviser(answer="Your CGPA is 2.86.") as adviser:
        first = bot.execute_durable_job(job)
        student.gpa = 4.0
        student.save(update_fields=["gpa"])
        second = bot.execute_durable_job(job)

    assert adviser.call_count == 1
    assert first["result_code"] == second["result_code"] == "secure_output_withheld"
    assert "2.86" not in " ".join(first["messages"] + second["messages"])
    assert "authenticated student portal" in " ".join(second["messages"])


@CHANNEL_ON
def test_a_crash_before_output_validation_cannot_release_or_remember_the_answer():
    """Unvalidated provenance closes the commit-before-DLP crash window."""

    from core.services import advisor_turn
    from core.services.advisor_channel_privacy import (
        TELEGRAM_SAFE_IDEMPOTENCY_PREFIX,
        TELEGRAM_SAFE_PROFILE,
        TELEGRAM_UNVALIDATED_PROFILE,
        TELEGRAM_WITHHELD_PROFILE,
    )
    from core.services.advisor_history import load_profiled_history
    from telegram_gateway import jobs

    student = _student_row(MINE)
    student.gpa = 2.86
    student.save(update_fields=["gpa"])
    link = _link()
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    link.current_conversation = conversation
    link.save(update_fields=["current_conversation"])
    update_id = 8203
    question = "Which courses should I take next?"

    # Simulate the worker dying immediately after the shared turn service commits
    # the model response, before bot.execute_durable_job reaches its output check.
    with _adviser(answer="GPA: 2.86"):
        result = advisor_turn.run_advisor_turn(
            principal=bot._principal_for(link),
            conversation=conversation,
            question=question,
            idempotency_key=f"{TELEGRAM_SAFE_IDEMPOTENCY_PREFIX}{update_id}",
            channel_profile=TELEGRAM_SAFE_PROFILE,
        )

    assert result.outcome == advisor_turn.CREATED
    assert result.student_message.generation_profile == TELEGRAM_UNVALIDATED_PROFILE
    assert (
        load_profiled_history(
            conversation,
            channel_profile=TELEGRAM_SAFE_PROFILE,
        )
        == []
    )

    student.gpa = 4.0
    student.save(update_fields=["gpa"])
    job, created = jobs.enqueue_question_or_command(
        update_id=update_id,
        link=link,
        kind=TelegramUpdateReceipt.KIND_QUESTION,
        payload_text=question,
    )
    assert created
    with _adviser(side_effect=AssertionError("replay called the model")) as adviser:
        replay = bot.execute_durable_job(job)

    adviser.assert_not_called()
    assert replay["result_code"] == "secure_output_withheld"
    assert "2.86" not in " ".join(replay["messages"])
    result.student_message.refresh_from_db()
    assert result.student_message.generation_profile == TELEGRAM_WITHHELD_PROFILE
    assert (
        load_profiled_history(
            conversation,
            channel_profile=TELEGRAM_SAFE_PROFILE,
        )
        == []
    )


@CHANNEL_ON
def test_general_gpa_policy_output_is_not_mistaken_for_a_personal_result(client, outbox):
    _link()
    answer = "GPA is calculated by weighting each course grade by its credit hours."
    with _adviser(answer=answer):
        _post(client, _update(text="How is GPA calculated?"))

    assert answer in " ".join(outbox.texts)
    assert (
        AdvisorMessage.objects.get(role=AdvisorMessage.ROLE_STUDENT).generation_profile
        == "telegram_safe"
    )


@CHANNEL_ON
def test_record_defence_does_not_hide_ordinary_course_and_policy_language(client, outbox):
    student = _student_row(MINE)
    student.gpa = 2.86
    student.save(update_fields=["gpa"])
    passed = Course.objects.create(course_code="CS113")
    failed = Course.objects.create(course_code="DS341")
    StudentCourse.objects.create(
        student=student,
        course=passed,
        status=StudentCourse.Status.PASSED,
        grade="A",
    )
    StudentCourse.objects.create(
        student=student,
        course=failed,
        status=StudentCourse.Status.FAILED,
    )
    _link()
    answer = (
        "A GPA of 2.86 can be a general policy threshold. "
        "CS113 is a prerequisite, and DS341 is another course code."
    )

    with _adviser(answer=answer):
        _post(client, _update(text="Explain these programme terms generally."))

    assert answer in " ".join(outbox.texts)


@CHANNEL_ON
@pytest.mark.parametrize(
    "answer, setup_kind",
    [
        ("GPA: 2.86", "gpa"),
        ("You currently have a 2.86 GPA.", "gpa"),
        ("The cumulative GPA on file is 2.86.", "gpa"),
        ("CS113 - B", "grade"),
        ("You failed DS341.", "failed"),
    ],
)
def test_structured_record_values_cannot_bypass_the_output_boundary(
    client, outbox, answer, setup_kind
):
    student = _student_row(MINE)
    student.gpa = 2.86
    student.save(update_fields=["gpa"])
    if setup_kind in {"grade", "failed"}:
        code = "CS113" if setup_kind == "grade" else "DS341"
        course = Course.objects.create(course_code=code)
        StudentCourse.objects.create(
            student=student,
            course=course,
            status=(
                StudentCourse.Status.PASSED
                if setup_kind == "grade"
                else StudentCourse.Status.FAILED
            ),
            grade="B" if setup_kind == "grade" else "",
        )
    _link()

    with _adviser(answer=answer):
        _post(client, _update(text="Which courses should I take next?"))

    delivered = " ".join(outbox.texts)
    assert answer not in delivered
    assert "authenticated student portal" in delivered


def test_credentials_live_only_in_the_environment():
    """No bot token and no webhook secret may be committed."""
    import re
    from pathlib import Path

    settings_source = Path("config/settings.py").read_text(encoding="utf-8")
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET"):
        line = next(ln for ln in settings_source.splitlines() if ln.startswith(f"{name} ="))
        assert re.fullmatch(rf'{name} = os\.getenv\("{name}", ""\)', line.strip()), line
    # A real bot token looks like 123456789:AA... — none may appear anywhere here.
    for path in Path("telegram_gateway").rglob("*.py"):
        assert not re.search(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}", path.read_text(encoding="utf-8"))


# ── 21. escalation reuses the existing function ──────────────────


@CHANNEL_ON
def test_advisor_hands_the_last_turn_to_a_human_using_the_shared_function(client, outbox):
    _link()
    with _adviser(answer="لست متأكدًا من هذه الحالة."):
        _post(client, _update(update_id=1, text="حالتي خاصة"))

    with mock.patch("core.services.advisor_turn.may_escalate", return_value=True) as gate:
        _post(client, _update(update_id=2, text="/advisor"))

    assert gate.called, "the channel did not use the shared escalation policy"
    from core.models import AdvisorEscalation

    case = AdvisorEscalation.objects.get()
    assert case.student_id == MINE
    assert case.reference in " ".join(outbox.texts)


@CHANNEL_ON
def test_advisor_case_and_reply_survive_a_worker_crash_without_a_second_case(client, outbox):
    """Closing the first case before retry must not make `/advisor` run twice."""

    from core.models import AdvisorEscalation
    from telegram_gateway import jobs

    link = _link()
    with _adviser(answer="لست متأكدًا من هذه الحالة."):
        _post(client, _update(update_id=8100, text="حالتي خاصة"))

    job, created = jobs.enqueue_question_or_command(
        update_id=8101,
        link=link,
        kind=TelegramUpdateReceipt.KIND_COMMAND,
        payload_text="/advisor",
    )
    assert created
    now = timezone.now()
    TelegramUpdateReceipt.objects.filter(pk=job.pk).update(
        status=TelegramUpdateReceipt.STATUS_RUNNING,
        attempt_count=1,
        locked_by="crashing-worker",
        locked_at=now,
        lease_expires_at=now + timedelta(hours=1),
    )
    job.refresh_from_db()

    with mock.patch("core.services.advisor_turn.may_escalate", return_value=True):
        assert bot.execute_durable_job(job) == {}

    job.refresh_from_db()
    case = AdvisorEscalation.objects.get()
    assert case.reference in " ".join(
        item["text"] for item in job.delivery_payload["items"] if item["kind"] == "text"
    )

    # The process dies here: the durable payload exists but no delivery cursor
    # moved. A human then closes the case before lease recovery/retry.
    case.status = AdvisorEscalation.Status.CLOSED
    case.save(update_fields=["status"])
    TelegramUpdateReceipt.objects.filter(pk=job.pk).update(
        status=TelegramUpdateReceipt.STATUS_QUEUED,
        available_at=timezone.now(),
        locked_by="",
        locked_at=None,
        lease_expires_at=None,
    )
    delivered: list[str] = []

    def executor_must_not_repeat(_job):
        raise AssertionError("the escalation side effect was repeated")

    retried = jobs.run_job(
        job.update_id,
        worker_id="replacement-worker",
        executor=executor_must_not_repeat,
        deliver=lambda _job, text: delivered.append(text) or {"ok": True},
    )

    assert retried is not None
    assert retried.status == TelegramUpdateReceipt.STATUS_SUCCEEDED
    assert AdvisorEscalation.objects.count() == 1
    assert case.reference in " ".join(delivered)


@CHANNEL_ON
def test_advisor_with_nothing_to_escalate_says_so(client, outbox):
    _link()
    _post(client, _update(text="/advisor"))
    assert outbox.texts == [messages.ESCALATION_NOTHING_TO_ESCALATE]


# ── 22. the parser, directly ─────────────────────────────────────


def test_parse_update_refuses_everything_that_is_not_a_private_text_message():
    assert bot.parse_update(None) is None
    assert bot.parse_update("nope") is None
    assert bot.parse_update({}) is None
    assert bot.parse_update({"update_id": "1", "message": {}}) is None
    assert bot.parse_update({"update_id": True, "message": {}}) is None
    assert bot.parse_update(_update(chat_type="group")) is None
    assert bot.parse_update(_update(is_bot=True)) is None
    assert bot.parse_update(_update(chat_id=1, user_id=2)) is None
    assert bot.parse_update(_update(key="edited_message")) is None


def test_a_command_with_a_bot_suffix_is_still_the_command():
    inbound = bot.parse_update(_update(text="/help@MyAdviserBot"))
    assert inbound is not None
    assert inbound.command == "/help"


def test_free_text_is_not_a_command():
    inbound = bot.parse_update(_update(text="كم مادة باقية لي؟"))
    assert inbound is not None and inbound.command == ""


def test_the_migration_is_reversible():
    """`migrate telegram_gateway zero` must work — a channel that cannot be
    unshipped is one nobody will switch off in a hurry."""
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(None, ignore_no_migrations=True)
    migration = loader.get_migration("telegram_gateway", "0001_initial")
    for operation in migration.operations:
        assert operation.reversible, operation


# ── holes a mutation review found, and the fixes that answer them ──


@CHANNEL_ON
def test_a_non_ascii_secret_header_is_refused_not_crashed(client, outbox):
    """`hmac.compare_digest` accepts `str` only when both sides are ASCII.

    One non-ASCII byte in the header therefore turned an authentication check
    into an unhandled TypeError — a 500 available to any unauthenticated caller,
    on the one view whose entire job is to refuse them.
    """
    for hostile in ["é", "\xc3\xa9", "secret\u00ff", "\u0000"]:
        response = _post(client, _update(text="/help"), secret=hostile)
        assert response.status_code == 403, f"{hostile!r} did not produce a clean 403"
    assert outbox.sent == []


@CHANNEL_ON
def test_revoking_mid_generation_stops_the_answer_reaching_the_chat(client, outbox):
    """A turn takes up to ~90 s. `/unlink` on a stolen handset has to take effect
    NOW — not after the student's GPA has already arrived in the thief's chat."""
    link = _link()

    def _revoke_then_answer(**kwargs):
        # Stands in for the student revoking while the model is still working.
        linking.unlink_chat(CHAT)
        return _fake_answer("معدلك التراكمي ٤٫٥ وباقي لك ٣ مواد.")

    with mock.patch(
        "core.services.student_advisor_v2.answer_student_advisor",
        side_effect=_revoke_then_answer,
    ):
        response = _post(client, _update(text="وش المقررات المناسبة لي؟"))

    assert response.status_code == 200
    delivered = " ".join(outbox.texts)
    assert "معدلك" not in delivered, "the answer was delivered to a revoked chat"
    # The answer is still stored, so the student loses nothing on the web.
    assert AdvisorMessage.objects.filter(role=AdvisorMessage.ROLE_ASSISTANT).count() == 1
    assert link.pk is not None


@CHANNEL_ON
def test_delivery_goes_to_the_link_not_to_the_payload(client, outbox):
    """The payload is attacker-controlled input; the link row is the verified fact.

    Asserted on the SIGNATURE, not by grepping the body: `answer_question` no
    longer accepts a chat id at all, so the payload's value cannot reach delivery
    even by accident. A body grep would also have to keep pace with every local
    variable named `chat_id`, which is how a structural test starts failing for
    reasons unrelated to the property it protects.
    """
    import inspect

    from telegram_gateway import bot as bot_module

    params = inspect.signature(bot_module.answer_question).parameters
    assert "chat_id" not in params, "the payload's chat id is reachable again"
    assert set(params) == {"link_id", "update_id", "question", "server_port"}
    assert "live.telegram_user_id" in inspect.getsource(bot_module.answer_question)


@CHANNEL_ON
def test_an_in_request_send_uses_the_short_deadline(client, outbox):
    """Render runs two sync gunicorn workers for the whole platform. A 30-second
    Telegram stall on the request path is not a slow reply — it is the site having
    no worker left."""
    from telegram_gateway.transport import INLINE_TIMEOUT_SECONDS

    _post(client, _update(text="/help"))
    assert outbox.sent
    assert all(m["timeout"] == INLINE_TIMEOUT_SECONDS for m in outbox.sent)
    assert INLINE_TIMEOUT_SECONDS <= 5


@CHANNEL_ON
def test_a_crash_after_claiming_does_not_burn_the_update_id(client, outbox):
    """The receipt is claimed before the work so a redelivery cannot race it —
    which makes it a promise the work happened. If the work raises, the promise
    must be withdrawn or the question can never be asked again."""
    with (
        mock.patch(
            "telegram_gateway.views.linking.active_link_for_chat",
            side_effect=RuntimeError("database gone"),
        ),
        pytest.raises(RuntimeError),
    ):
        _post(client, _update(update_id=515, text="سؤال"))

    assert not TelegramUpdateReceipt.objects.filter(update_id=515).exists()

    # And the same update now works.
    _link()
    with _adviser():
        assert _post(client, _update(update_id=515, text="سؤال")).status_code == 200
    assert AdvisorMessage.objects.filter(role=AdvisorMessage.ROLE_ASSISTANT).count() == 1


# ── link management from the web (the lost-handset path) ──────────


@CHANNEL_ON
def test_link_manage_refuses_anonymous_visitors(client):
    response = client.get(reverse("telegram_link_manage"))
    assert response.status_code == 403


@CHANNEL_ON
def test_link_manage_revokes_only_the_signed_in_students_own_link(client):
    """A destructive browser-reachable endpoint. Replacing its filter with a mass
    revoke would otherwise keep the suite green."""
    _link(student_id=MINE, telegram_user_id=CHAT)
    _link(student_id=THEIRS, telegram_user_id=OTHER_CHAT)
    _student(client, MINE)

    response = client.post(reverse("telegram_link_manage"))

    assert response.status_code == 200
    assert linking.active_link_for_chat(CHAT) is None
    assert linking.active_link_for_chat(OTHER_CHAT) is not None, "another student was unlinked"
    assert TelegramLink.objects.get(telegram_user_id=OTHER_CHAT).student_id == THEIRS


@CHANNEL_ON
def test_link_manage_with_no_link_says_so(client):
    _student(client, MINE)
    response = client.post(reverse("telegram_link_manage"))
    assert response.status_code == 200
    assert response.context["state"] == "not_linked"


@CHANNEL_ON
def test_link_manage_never_shows_a_telegram_identifier(client):
    _link()
    _student(client, MINE)
    response = client.get(reverse("telegram_link_manage"))
    assert response.status_code == 200
    assert str(CHAT).encode() not in response.content
    assert str(MINE).encode() not in response.content


@CHANNEL_ON
def test_link_pages_keep_the_shared_skip_link_target_and_wide_wrapper(client):
    """Custom base bodies still need the landmark targeted by the global skip link."""

    _student(client, MINE)
    issued = linking.issue_link_token(telegram_user_id=CHAT)
    pages = [
        client.get(reverse("telegram_link_start", args=[issued.raw_token])),
        client.get(reverse("telegram_link_manage")),
        client.get(reverse("telegram_link_start", args=["invalid-token"])),
    ]

    for response in pages:
        html = response.content.decode("utf-8")
        assert 'href="#main-content"' in html
        assert html.count('id="main-content"') == 1
        assert 'class="login-wrap telegram-link-wrap"' in html

    css = Path("static/css/global.css").read_text(encoding="utf-8")
    assert ".telegram-link-wrap" in css and "max-width: 720px" in css


@CHANNEL_ON
def test_approving_a_link_requires_a_csrf_token(client):
    """The webhook is csrf_exempt; the linking pages must not be."""
    from django.test import Client

    _student(client, MINE)
    issued = linking.issue_link_token(telegram_user_id=CHAT)

    strict = Client(enforce_csrf_checks=True)
    strict.cookies = client.cookies
    response = strict.post(reverse("telegram_link_confirm", args=[issued.raw_token]))
    assert response.status_code == 403
    assert TelegramLinkToken.objects.get().approved_student_id is None


# ── escalation: ownership, and the real policy gate ───────────────


@CHANNEL_ON
def test_advisor_cannot_escalate_another_students_turn(client, outbox):
    """`/advisor` picks the most recent answered turn. It must pick it from THIS
    student's thread, and the service must re-prove that rather than trust it."""
    from core.models import AdvisorEscalation

    _link(student_id=MINE, telegram_user_id=CHAT)

    theirs = AdvisorConversation.objects.create(student_id=THEIRS)
    question = AdvisorMessage.objects.create(
        conversation=theirs,
        role=AdvisorMessage.ROLE_STUDENT,
        content="سؤالهم",
        status=AdvisorMessage.STATUS_COMPLETED,
    )
    AdvisorMessage.objects.create(
        conversation=theirs,
        in_reply_to=question,
        role=AdvisorMessage.ROLE_ASSISTANT,
        content="جوابهم",
        status=AdvisorMessage.STATUS_ABSTAINED,
    )

    with _adviser(answer="لست متأكدًا."):
        _post(client, _update(update_id=1, text="سؤالي"))
    _post(client, _update(update_id=2, text="/advisor"))

    for case in AdvisorEscalation.objects.all():
        assert case.student_id == MINE
        assert case.source_message.conversation.student_id == MINE


@CHANNEL_ON
def test_escalate_turn_refuses_a_message_the_principal_does_not_own(client):
    """The service proves ownership itself. Both callers filter today, but
    "every caller remembers" is the shape of the defect PR #61 fixed."""
    from core.services import advisor_turn
    from core.services.advisor_principal import AdvisorPrincipal
    from core.services.rbac import ROLE_STUDENT

    _student_row(MINE)
    _student_row(THEIRS)
    theirs = AdvisorConversation.objects.create(student_id=THEIRS)
    question = AdvisorMessage.objects.create(
        conversation=theirs,
        role=AdvisorMessage.ROLE_STUDENT,
        content="سؤالهم",
        status=AdvisorMessage.STATUS_COMPLETED,
    )
    answer = AdvisorMessage.objects.create(
        conversation=theirs,
        in_reply_to=question,
        role=AdvisorMessage.ROLE_ASSISTANT,
        content="جوابهم",
        status=AdvisorMessage.STATUS_ABSTAINED,
    )

    with pytest.raises(advisor_turn.ConversationNotFound):
        advisor_turn.escalate_turn(
            principal=AdvisorPrincipal(role=ROLE_STUDENT, student_id=MINE),
            message=answer,
            student_requested=True,
        )


@CHANNEL_ON
def test_advisor_uses_the_real_escalation_policy_and_says_it_was_requested(client, outbox):
    """Without the `student_requested` flag the command is dead for every answer
    the policy does not already consider escalation-worthy."""
    from core.models import AdvisorEscalation

    _link()
    with _adviser(answer="لست متأكدًا من هذه الحالة."):
        _post(client, _update(update_id=1, text="حالتي خاصة"))

    with mock.patch(
        "core.services.advisor_turn.may_escalate", wraps=None, return_value=True
    ) as gate:
        _post(client, _update(update_id=2, text="/advisor"))

    assert gate.call_args.kwargs["student_requested"] is True
    assert AdvisorEscalation.objects.count() == 1


# ── the transport payload, not its source text ────────────────────


def test_the_outbound_payload_is_plain_text_and_within_the_api_limit():
    """Asserted on what is actually sent, not by grepping the module. A source
    grep passes for a module that never runs."""
    import json as _json
    from unittest.mock import MagicMock

    from telegram_gateway.transport import TELEGRAM_MAX_MESSAGE_CHARS, HttpTelegramTransport

    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def _fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = _json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _Response()

    with (
        override_settings(TELEGRAM_BOT_TOKEN="123:abc"),
        mock.patch("telegram_gateway.transport.urlopen", _fake_urlopen),
    ):
        HttpTelegramTransport().send_message(chat_id=7, text="ب" * 9000, timeout=2.5)

    assert captured["url"].startswith("https://api.telegram.org/bot")
    assert "parse_mode" not in captured["body"], "model output was sent as markup"
    assert captured["body"]["disable_web_page_preview"] is True
    assert captured["body"]["chat_id"] == 7
    assert len(captured["body"]["text"]) <= TELEGRAM_MAX_MESSAGE_CHARS
    assert captured["timeout"] == 2.5
    assert MagicMock  # keep the import meaningful for linters


# ── logging: the token path that previously never executed ────────


@CHANNEL_ON
def test_a_link_token_is_never_written_to_the_logs(client, outbox, caplog):
    """Issued from an UNLINKED chat — the earlier version of this assertion sat
    behind an already-linked chat, so `/link` returned ALREADY_LINKED and the
    token branch never ran."""
    import logging

    caplog.set_level(logging.DEBUG)
    assert linking.active_link_for_chat(CHAT) is None

    _post(client, _update(text="/link"))

    invitation = outbox.texts[-1]
    raw = invitation.split("/telegram/link/")[1].split("/")[0]
    assert len(raw) >= 32, "the invitation did not contain a token"

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert raw not in logged
    assert linking.hash_token(raw) not in logged


@CHANNEL_ON
def test_a_confirmation_code_is_never_written_to_the_logs(client, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    _student(client, MINE)
    issued = linking.issue_link_token(telegram_user_id=CHAT)
    code = client.post(reverse("telegram_link_confirm", args=[issued.raw_token])).context[
        "confirm_code"
    ]

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert code not in logged


# ── exact delivery, so anything APPENDED is visible ───────────────


@CHANNEL_ON
def test_a_linked_turn_sends_exactly_the_acknowledgement_and_the_stored_answer(client, outbox):
    """Equality, not substring. Every other assertion on this path is `in`, so a
    change that APPENDS the grounding state, the disposition or the student's own
    id to each answer would be invisible."""
    _link()
    with _adviser(answer="باقي لك ٣ مواد."):
        _post(client, _update(text="كم مادة باقية؟"))

    assert outbox.texts == [messages.WORKING, "باقي لك ٣ مواد."]


@CHANNEL_ON
def test_internal_turn_metadata_never_reaches_the_chat(client, outbox):
    """The stored row carries `grounding_state`, `final_disposition`, `route` and
    `model_name`. None of it is the student's business, and the web serialiser
    deliberately omits it too."""
    _link()
    with _adviser(answer="جواب.", agent={"policy_grounding": "not_consulted"}):
        _post(client, _update(text="سؤال"))

    body = " ".join(outbox.texts)
    for internal in ("not_consulted", "SEEDED_FALLBACK", "AGENT", "fake-model", "PASS"):
        assert internal not in body, internal


# ── the budget ordering the whole extraction is built around ──────


@CHANNEL_ON
def test_a_replayed_turn_does_not_cost_a_question(client, outbox):
    """Documented in `advisor_turn` as a security property and, before this,
    tested nowhere in the repo — on either channel. Charging before the replay
    branch means a retry of a stored answer costs the student a real question."""
    from core.models import RateLimitBucket
    from core.services.rate_limit import GENERATION

    _link()
    payload = _update(update_id=4321, text="كم مادة باقية؟")

    with _adviser():
        _post(client, payload)
        spent_once = RateLimitBucket.objects.get(key=f"{GENERATION}:{MINE}").count
        # A redelivery Telegram makes after the answer already existed.
        TelegramUpdateReceipt.objects.all().delete()
        _post(client, payload)

    spent_twice = RateLimitBucket.objects.get(key=f"{GENERATION}:{MINE}").count
    assert spent_twice == spent_once, "a replay was charged as a new question"


# ── an already-linked student gets a sentence they can act on ─────


@CHANNEL_ON
def test_a_student_linking_a_second_chat_is_told_to_unlink_the_first(client, outbox):
    """Not "this chat belongs to someone else" — a sentence that reads as an
    account compromise when the real cause is an old link they forgot."""
    _link(student_id=MINE, telegram_user_id=OTHER_CHAT)
    _student(client, MINE)
    issued = linking.issue_link_token(telegram_user_id=CHAT)

    response = client.post(reverse("telegram_link_confirm", args=[issued.raw_token]))
    assert response.status_code == 409
    assert response.context["state"] == linking.STUDENT_ALREADY_LINKED


# ── the parser's type checks, isolated from its shape checks ──────


@pytest.mark.parametrize("bad_update_id", ["1", True, None, 1.5, [1]])
def test_a_non_integer_update_id_is_refused_by_the_type_check(bad_update_id):
    """Built from a VALID update with only `update_id` replaced, so a `None`
    return is caused by the type check and not by an empty message."""
    payload = _update(text="سؤال")
    payload["update_id"] = bad_update_id
    assert bot.parse_update(payload) is None
    # And the same payload with a real int parses, proving the rest is valid.
    payload["update_id"] = 9
    assert bot.parse_update(payload) is not None


# ── the stale-next hazard on a shared machine ─────────────────────


def test_an_abandoned_next_is_not_inherited_by_the_next_student(client):
    """A lab machine: one student starts a redirect-carrying login and walks away;
    the destination must not survive into somebody else's sign-in."""
    from core.services import student_otp

    ensure_role_groups()
    _student_row(MINE)
    _student_row(THEIRS)

    client.get(f"{reverse('student_login')}?next=/telegram/link/abc/")
    # Somebody else arrives at the plain login page and signs in.
    client.get(reverse("student_login"))
    client.force_login(student_otp.provision_student_user(THEIRS))
    response = client.get(reverse("student_login"))

    assert response["Location"] == reverse("student_home")


def test_a_next_older_than_the_window_is_ignored(client):
    """Driven through the OTP step, which is the only place a stale entry can
    actually reach the redirect.

    A bare GET of the login page clears the entry outright, so asserting through
    that path would pass with the age check deleted — the clear would be doing all
    the work and the test would prove nothing about staleness.
    """
    ensure_role_groups()
    _student_row(MINE)

    client.get(f"{reverse('student_login')}?next=/telegram/link/abc/")
    session = client.session
    stored = session["post_login_next"]
    stored["at"] = (timezone.now() - timedelta(hours=2)).isoformat()
    session["post_login_next"] = stored
    session["otp_student_id"] = MINE
    session.save()

    with mock.patch("core.student_auth_views.verify_otp", return_value=True):
        response = client.post(reverse("student_otp_verify"), data={"code": "123456"})

    assert response.status_code == 302
    assert response["Location"] == reverse("student_home")


def test_a_fresh_next_still_survives_the_otp_step(client):
    """The control for the test above: same path, an in-window timestamp."""
    ensure_role_groups()
    _student_row(MINE)
    destination = "/telegram/link/abc/"

    client.get(f"{reverse('student_login')}?next={destination}")
    session = client.session
    session["otp_student_id"] = MINE
    session.save()

    with mock.patch("core.student_auth_views.verify_otp", return_value=True):
        response = client.post(reverse("student_otp_verify"), data={"code": "123456"})

    assert response["Location"] == destination


def test_the_next_destination_survives_the_login_post(client):
    """The destination is recorded on the GET and used by a later POST.

    Clearing it on *any* request without `next` would drop it halfway through the
    flow it was recorded for, because neither login POST carries the first step's
    query string. Driven through the real login rather than by reading the
    session, so it proves the destination is actually followed.
    """
    ensure_role_groups()
    _student_row(MINE)
    destination = "/telegram/link/sometoken/"

    client.get(f"{reverse('student_login')}?next={destination}")
    response = client.post(reverse("student_login"), data={"student_id": str(MINE)})

    if response.status_code == 302:
        # The DEBUG-only no-OTP bypass signs in on this POST.
        assert response["Location"] == destination
    else:
        # The ordinary two-step flow: the OTP step is still to come, and the
        # destination has to be waiting for it.
        assert client.session["post_login_next"]["url"] == destination


def test_the_next_destination_survives_the_otp_step(client):
    """The second POST is a different view and carries no query string at all."""
    from core.services import student_otp

    ensure_role_groups()
    _student_row(MINE)
    destination = "/telegram/link/sometoken/"

    client.get(f"{reverse('student_login')}?next={destination}")
    session = client.session
    session["otp_student_id"] = MINE
    session.save()

    with mock.patch("core.student_auth_views.verify_otp", return_value=True):
        response = client.post(reverse("student_otp_verify"), data={"code": "123456"})

    assert response.status_code == 302
    assert response["Location"] == destination
    assert student_otp is not None


# ── timetable images: one renderer, and a picture that never costs the answer ──


def _renderer(fail: bool = False):
    """Install a recording card renderer; never starts a browser."""
    from telegram_gateway.rendering import RecordingRenderer, set_renderer

    r = RecordingRenderer(fail=fail)
    set_renderer(r)
    return r


def _card_request(client: Client, token: str) -> HttpResponse:
    from telegram_gateway.cards import sign_renderer_request

    return client.get(  # type: ignore[return-value]
        f"/telegram/card/{token}/",
        HTTP_X_TELEGRAM_CARD_RENDERER=sign_renderer_request(),
    )


def _card_asset_request(client: Client, asset_path: str) -> HttpResponse:
    from telegram_gateway.cards import sign_renderer_request

    return client.get(  # type: ignore[return-value]
        f"/telegram/card-assets/{asset_path}",
        HTTP_X_TELEGRAM_CARD_RENDERER=sign_renderer_request(),
    )


@pytest.fixture
def cards():
    from telegram_gateway.rendering import set_renderer

    r = _renderer()
    yield r
    set_renderer(None)


#: Same flag set as CHANNEL_ON with images ON. Built by copying it rather than
#: retyping, because the two drifting is how `TELEGRAM_INTERNAL_BASE_URL` came to
#: be pinned in one and ambient in the other — the THIRD recurrence of a bug this
#: file already documents twice.
IMAGES_ON = override_settings(**{**CHANNEL_ON.options, "TELEGRAM_SEND_TIMETABLE_IMAGES": True})
GRADUATION_IMAGES_ON = override_settings(
    **{**CHANNEL_ON.options, "TELEGRAM_SEND_GRADUATION_IMAGES": True}
)


def _timetable_presentation():
    from core.services.advisor_presentations import KIND_TIMETABLE, normalise_presentation

    p = {
        "kind": KIND_TIMETABLE,
        "baseline_kind": "REGISTERED",
        "baseline_sections": [
            {
                "course_code": "AI331",
                "course_name": "Machine Learning",
                "section": "M2",
                "credits": 4,
                "meetings": ["SUN 09:00-10:15"],
            }
        ],
    }
    assert normalise_presentation(p), "fixture is not a renderable card"
    return p


def _graduation_presentation():
    from core.services.advisor_presentations import KIND_GRADUATION, normalise_presentation

    presentation = {
        "kind": KIND_GRADUATION,
        "program": "DS2",
        "planning_term": "1448/1",
        "simulation_completed": True,
        "lower_bound_terms_including_planning_baseline": 2,
        "max_credits_per_term": 18,
        "band_labels": {
            "0": "Completed before the scenario",
            "1": "Planning baseline 1448/1",
            "2": "Projected 1448/2",
        },
        "graph": {
            "items": [
                {
                    "course_code": "DS341",
                    "prerequisite_course_code": "DS225",
                }
            ],
            "termOf": {"DS225": 1, "DS341": 2},
            "nameOf": {"DS225": "Data Mining", "DS341": "Data Governance"},
            "statusOf": {"DS225": "studying", "DS341": "open"},
            "extraNodes": ["DS225", "DS341"],
        },
        "unresolved_requirements": [],
        "read_only": True,
    }
    assert normalise_presentation(presentation), "fixture is not a renderable graduation card"
    return presentation


def test_images_fail_closed_when_nothing_configures_them(settings):
    """A picture of a week grid says where a student is and when, Telegram keeps
    it under a durable file_id, and it forwards more easily than prose. It gets
    its own switch, and absent configuration means off.

    Asserted by REMOVING the setting, not by reading its current value — the
    previous version of this test failed the moment the flag was switched on in a
    developer's own `.env`.
    """
    from telegram_gateway.rendering import images_enabled

    del settings.TELEGRAM_SEND_TIMETABLE_IMAGES
    del settings.TELEGRAM_SEND_GRADUATION_IMAGES
    assert images_enabled() is False


def test_the_image_setting_defaults_to_off_in_settings():
    """And the settings module's own default is `false`, with the strict idiom."""
    from pathlib import Path

    source = Path("config/settings.py").read_text(encoding="utf-8")
    assert 'os.getenv("TELEGRAM_SEND_TIMETABLE_IMAGES", "false").lower() == "true"' in source
    assert 'os.getenv("TELEGRAM_SEND_GRADUATION_IMAGES", "false").lower() == "true"' in source


@CHANNEL_ON
def test_no_image_is_sent_while_the_flag_is_off(client, outbox, cards):
    _link()
    with _adviser(answer="هذا جدولك.", presentation=_timetable_presentation()):
        _post(client, _update(text="ابنِ لي جدولًا"))

    assert cards.requested == [], "the renderer ran with the flag off"
    assert outbox.photos == []
    assert outbox.texts, "the text answer was withheld too"


@IMAGES_ON
def test_durable_worker_delivers_the_timetable_image_and_validated_text(
    client: Client,
    outbox: RecordingTransport,
    cards: RecordingRenderer,
) -> None:
    """The picture is a cursor-tracked queue item, never an immediate side send."""
    _link()
    with _adviser(answer="هذا جدولك.", presentation=_timetable_presentation()):
        _post(client, _update(text="ابنِ لي جدولًا"))

    assert len(cards.requested) == 1
    assert len(outbox.photos) == 1
    assert "هذا جدولك." in " ".join(outbox.texts)


@IMAGES_ON
def test_durable_render_failure_retries_then_preserves_the_text_answer(
    client: Client,
    outbox: RecordingTransport,
) -> None:
    from telegram_gateway import jobs
    from telegram_gateway.rendering import set_renderer

    failing = _renderer(fail=True)
    try:
        _link()
        with _adviser(
            answer="هذا جدولك.",
            presentation=_timetable_presentation(),
        ) as adviser:
            _post(client, _update(update_id=8801, text="ابنِ لي جدولًا"))
            TelegramUpdateReceipt.objects.filter(update_id=8801).update(available_at=timezone.now())
            jobs.run_job(8801, worker_id="image-retry-2")
            TelegramUpdateReceipt.objects.filter(update_id=8801).update(available_at=timezone.now())
            final = jobs.run_job(8801, worker_id="image-retry-3")
    finally:
        set_renderer(None)

    assert adviser.call_count == 1
    assert len(failing.requested) == 3
    assert outbox.photos == []
    assert "هذا جدولك." in " ".join(outbox.texts)
    assert final is not None
    assert final.status == TelegramUpdateReceipt.STATUS_SUCCEEDED
    assert final.error_code == "image_delivery_degraded"


@IMAGES_ON
def test_withheld_output_never_materialises_or_sends_a_timetable_image(
    client: Client,
    outbox: RecordingTransport,
    cards: RecordingRenderer,
) -> None:
    student = _student_row(MINE)
    student.gpa = 2.86
    student.save(update_fields=["gpa"])
    _link()

    with _adviser(
        answer="Your CGPA is 2.86.",
        presentation=_timetable_presentation(),
    ):
        _post(client, _update(update_id=8802, text="Which courses should I take?"))

    job = TelegramUpdateReceipt.objects.get(update_id=8802)
    assert job.result_code == "secure_output_withheld"
    assert cards.requested == []
    assert outbox.photos == []
    assert "2.86" not in " ".join(outbox.texts)


@IMAGES_ON
@pytest.mark.parametrize(
    ("visible_result", "course_code"),
    [
        ("GPA: 2.86", "AI331"),
        ("CS113 - 84", "CS113"),
        ("You failed DS341.", "DS341"),
        ("Failed previously with mark 40.", "DS341"),
    ],
)
def test_personal_record_text_inside_a_presentation_suppresses_only_the_photo_recipe(
    client: Client,
    outbox: RecordingTransport,
    cards: RecordingRenderer,
    visible_result: str,
    course_code: str,
) -> None:
    """Safe prose still travels; visible personal data must never enter a screenshot."""

    from telegram_gateway import jobs

    student = _student_row(MINE)
    student.gpa = 2.86
    student.save(update_fields=["gpa"])
    passed = Course.objects.create(course_code="CS113")
    failed = Course.objects.create(course_code="DS341")
    StudentCourse.objects.create(
        student=student,
        course=passed,
        status=StudentCourse.Status.PASSED,
        grade="B",
        mark=84,
    )
    StudentCourse.objects.create(
        student=student,
        course=failed,
        status=StudentCourse.Status.FAILED,
        grade="F",
        mark=40,
    )
    _link()

    presentation = _timetable_presentation()
    presentation["constraint_failures"] = [
        {
            "course_code": course_code,
            "section_label": "M1",
            "reason": visible_result,
        }
    ]
    safe_answer = "Here is the timetable summary you requested."
    from core.services.advisor_presentations import normalise_presentation

    normalised = normalise_presentation(presentation)
    assert bot.contains_personal_record_output(
        f"{course_code} | {visible_result}",
        student_id=MINE,
        question="Build me a timetable.",
    )
    assert bot._presentation_contains_personal_record(
        normalised,
        student_id=MINE,
        question="Build me a timetable.",
    )
    captured_items: list[dict] = []
    original = bot._durable_delivery_items

    def capture_items(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        items = original(*args, **kwargs)
        captured_items.extend(items)
        return items

    with (
        mock.patch("telegram_gateway.bot._durable_delivery_items", side_effect=capture_items),
        _adviser(answer=safe_answer, presentation=presentation),
    ):
        response = _post(
            client,
            _update(update_id=8803, text="Build me a timetable."),
        )

    assert response.status_code == 200
    assert captured_items, "the durable answer was not materialised"
    assert not any(
        item.get("kind") == jobs.DELIVERY_KIND_TIMETABLE_PHOTO for item in captured_items
    )
    assert any(
        item.get("kind") == jobs.DELIVERY_KIND_TEXT and safe_answer in item.get("text", "")
        for item in captured_items
    )
    assert cards.requested == []
    assert outbox.photos == []
    assert safe_answer in " ".join(outbox.texts)


@IMAGES_ON
def test_a_failed_render_still_delivers_the_answer(client, outbox):
    """The answer is generated, validated and stored before anything is drawn.
    A missing Chromium — the state on Render today — must cost the picture and
    never the answer."""
    from telegram_gateway.rendering import set_renderer

    failing = _renderer(fail=True)
    try:
        link = _link()
        with _adviser(answer="هذا جدولك.", presentation=_timetable_presentation()):
            bot.answer_question(
                link_id=link.pk,
                update_id=1,
                question="ابنِ لي جدولًا",
                server_port="8000",
            )
    finally:
        set_renderer(None)

    assert failing.requested, "the renderer was never asked"
    assert outbox.photos == []
    body = " ".join(outbox.texts)
    assert "هذا جدولك." in body
    assert "/student/advisor/?c=" in body, "the link fallback was lost too"


@IMAGES_ON
def test_no_image_when_there_is_no_card(client, outbox, cards):
    _link()
    with _adviser(answer="باقي لك ٣ مواد."):
        _post(client, _update(text="كم مادة باقية؟"))
    assert cards.requested == []
    assert outbox.photos == []


@IMAGES_ON
def test_the_render_url_is_signed_and_local(client, outbox, cards):
    """The headless browser has no session and must not be given one. And it
    reaches this process locally — routing a signed card URL out through the
    public hostname and back would be exposure for nothing."""
    link = _link()
    with _adviser(answer="هذا جدولك.", presentation=_timetable_presentation()):
        bot.answer_question(
            link_id=link.pk,
            update_id=1,
            question="ابنِ لي جدولًا",
            server_port="8000",
        )

    assert len(cards.requested) == 1
    url = cards.requested[0]
    assert url.startswith("http://127.0.0.1"), url
    assert "advisor.example.edu" not in url
    assert "/telegram/card/" in url
    token = url.split("/telegram/card/")[1].rstrip("/")
    assert len(token) > 20 and str(MINE) not in token


# ── the card page itself ─────────────────────────────────────────


@CHANNEL_ON
def test_the_card_page_refuses_an_unsigned_or_forged_token(client):
    for bad in ["nonsense", "a.b.c", ""]:
        response = _card_request(client, bad or "x")
        assert response.status_code == 404


@CHANNEL_ON
def test_the_card_page_refuses_an_expired_token(client):
    from telegram_gateway.cards import sign_card, unsign_card

    _link()
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    m = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        content="x",
        presentation=_timetable_presentation(),
        status=AdvisorMessage.STATUS_COMPLETED,
    )
    token = sign_card(message_id=m.pk)
    assert unsign_card(token) is not None

    with mock.patch("telegram_gateway.cards.CARD_TOKEN_MAX_AGE_SECONDS", -1):
        assert unsign_card(token) is None


@IMAGES_ON
def test_the_card_page_renders_only_the_whitelisted_presentation(client):
    """Re-normalised on the way OUT as well as in: it is being handed to a
    renderer, and a row stored under older rules is exactly what the whitelist
    exists for."""
    from telegram_gateway.cards import sign_card

    conversation = AdvisorConversation.objects.create(student_id=MINE)
    question = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_STUDENT,
        content="ابنِ لي جدولًا",
        status=AdvisorMessage.STATUS_COMPLETED,
    )
    m = AdvisorMessage.objects.create(
        conversation=conversation,
        in_reply_to=question,
        role=AdvisorMessage.ROLE_ASSISTANT,
        content="هذا جدولك.",
        presentation={**_timetable_presentation(), "secret_operator_note": "LEAK-ME"},
        status=AdvisorMessage.STATUS_COMPLETED,
    )

    response = _card_request(client, sign_card(message_id=m.pk))

    assert response.status_code == 200
    body = response.content.decode("utf-8")
    assert "LEAK-ME" not in body, "an unwhitelisted key reached the card page"
    assert "AI331" in body
    assert "cardOnly" in body, "the bootstrap guard is missing; the page would call the API"
    assert response["Cache-Control"] == "no-store, private"
    assert response["Referrer-Policy"] == "no-referrer"
    policy = response["Content-Security-Policy"]
    assert "default-src 'none'" in policy
    assert "connect-src 'none'" in policy
    assert "fonts.googleapis.com" not in policy
    # No student identifier on a page reachable with only a signature.
    assert str(MINE) not in body


@IMAGES_ON
def test_card_assets_are_exactly_allowlisted_renderer_only_and_no_store(
    client: Client,
    tmp_path: Path,
) -> None:
    """Screenshots use source assets, not a possibly stale collected manifest."""

    with override_settings(STATIC_ROOT=tmp_path / "stale-static-root"):
        response = _card_asset_request(client, "js/shared-timetable.js")
        graph_response = _card_asset_request(client, "js/prereq-graph.js")

    assert response.status_code == 200
    assert b"WeekGrid" in response.content
    assert response["Content-Type"].startswith("text/javascript")
    assert response["Cache-Control"] == "no-store, private"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert graph_response.status_code == 200
    assert b"PrereqGraph" in graph_response.content

    assert client.get("/telegram/card-assets/js/shared-timetable.js").status_code == 404
    assert (
        client.get(
            "/telegram/card-assets/js/shared-timetable.js",
            HTTP_X_TELEGRAM_CARD_RENDERER="forged",
        ).status_code
        == 404
    )
    assert _card_asset_request(client, "js/not-allowlisted.js").status_code == 404


@IMAGES_ON
def test_card_html_loads_only_the_private_source_asset_route(client: Client) -> None:
    from telegram_gateway.cards import sign_card

    conversation = AdvisorConversation.objects.create(student_id=MINE)
    message = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        content="x",
        presentation=_timetable_presentation(),
        status=AdvisorMessage.STATUS_COMPLETED,
    )

    body = _card_request(client, sign_card(message_id=message.pk)).content.decode("utf-8")
    assert "/telegram/card-assets/css/global.css" in body
    assert "/telegram/card-assets/js/page-student-advisor.js" in body
    assert "/telegram/card-assets/js/prereq-graph.js" in body
    assert 'href="/static/' not in body
    assert 'src="/static/' not in body


@CHANNEL_ON
def test_the_card_page_refuses_a_message_with_no_card(client):
    from telegram_gateway.cards import sign_card

    conversation = AdvisorConversation.objects.create(student_id=MINE)
    m = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        content="no card here",
        status=AdvisorMessage.STATUS_COMPLETED,
    )
    assert _card_request(client, sign_card(message_id=m.pk)).status_code == 404


@override_settings(TELEGRAM_ADVISOR_ENABLED=False)
def test_the_card_page_is_dead_while_the_channel_is_off(client):
    from telegram_gateway.cards import sign_card

    conversation = AdvisorConversation.objects.create(student_id=MINE)
    m = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        content="x",
        presentation=_timetable_presentation(),
        status=AdvisorMessage.STATUS_COMPLETED,
    )
    assert _card_request(client, sign_card(message_id=m.pk)).status_code == 404


def test_there_is_exactly_one_timetable_renderer():
    """The image must be of the SAME card the link points at. A server-side
    drawing routine would be a second answer to "what does a timetable look
    like", and this codebase has twice paid for that kind of duplication."""
    from pathlib import Path

    js = Path("static/js/page-student-advisor.js").read_text(encoding="utf-8")
    assert "window.__SA_RENDER_TIMETABLE_CARD__ = renderTimetablePresentation;" in js
    assert "if (cfg.cardOnly) return;" in js, "the card page would run the bootstrap"

    # No second renderer crept into the gateway. Checked by parsing the IMPORTS
    # rather than grepping the text: the first version of this assertion matched
    # the docstring in cards.py that explains why not to use matplotlib, which is
    # a test failing on its own rationale.
    import ast

    banned = {"PIL", "matplotlib", "cairosvg", "reportlab"}
    for path in Path("telegram_gateway").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            assert not (names & banned), f"{path} imports a second timetable renderer"


def test_a_photo_caption_is_capped_below_the_telegram_limit():
    from telegram_gateway.transport import TELEGRAM_MAX_CAPTION_CHARS

    assert TELEGRAM_MAX_CAPTION_CHARS == 1024


def test_the_outbound_photo_is_multipart_and_carries_no_parse_mode():
    import json as _json

    from telegram_gateway.transport import HttpTelegramTransport

    captured = {}

    class _R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def _fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["ctype"] = request.headers.get("Content-type", "")
        captured["body"] = request.data
        return _R()

    with (
        override_settings(TELEGRAM_BOT_TOKEN="123:abc"),
        mock.patch("telegram_gateway.transport.urlopen", _fake_urlopen),
    ):
        HttpTelegramTransport().send_photo(chat_id=7, png=b"\x89PNG-bytes", caption="x")

    assert captured["url"].endswith("/sendPhoto")
    assert captured["ctype"].startswith("multipart/form-data; boundary=")
    assert b"LEAK" not in captured["body"]
    assert b"parse_mode" not in captured["body"]
    assert b'name="photo"' in captured["body"]
    assert _json is not None


def test_an_unconfigured_token_sends_no_photo_and_opens_no_socket():
    from telegram_gateway.transport import HttpTelegramTransport

    with override_settings(TELEGRAM_BOT_TOKEN=""):
        out = HttpTelegramTransport().send_photo(chat_id=1, png=b"x")
    assert out == {"ok": False, "skipped": True, "reason": "telegram_not_configured"}


@IMAGES_ON
def test_the_legacy_card_helper_honours_its_explicit_server_port(client, outbox, cards):
    """The dormant renderer helper never guesses or hard-codes its local port."""
    link = _link()
    with _adviser(answer="هذا جدولك.", presentation=_timetable_presentation()):
        bot.answer_question(
            link_id=link.pk,
            update_id=1,
            question="ابنِ لي جدولًا",
            server_port="8002",
        )

    assert cards.requested, "no card was requested"
    assert cards.requested[0].startswith("http://127.0.0.1:8002/"), cards.requested[0]


@IMAGES_ON
def test_the_legacy_delivery_path_sends_all_text_before_the_photo(client):
    """The direct fallback preserves the durable worker's delivery invariant."""
    link = _link()
    events = []

    with (
        mock.patch.object(
            bot,
            "send_text",
            side_effect=lambda **_kwargs: events.append("text") or {"ok": True},
        ),
        mock.patch.object(
            bot,
            "_send_card_image",
            side_effect=lambda *_args, **_kwargs: events.append("photo"),
        ),
        _adviser(answer="Here is your timetable.", presentation=_timetable_presentation()),
    ):
        bot.answer_question(
            link_id=link.pk,
            update_id=1,
            question="Build my timetable.",
            server_port="8002",
        )

    assert events.count("photo") == 1
    text_indexes = [index for index, event in enumerate(events) if event == "text"]
    assert text_indexes
    assert events.index("photo") > max(text_indexes)


@IMAGES_ON
def test_the_legacy_delivery_path_sends_no_photo_when_text_delivery_fails(client):
    link = _link()

    with (
        mock.patch.object(bot, "send_text", return_value={"ok": False, "error": "timeout"}),
        mock.patch.object(bot, "_send_card_image") as send_card,
        _adviser(answer="Here is your timetable.", presentation=_timetable_presentation()),
    ):
        bot.answer_question(
            link_id=link.pk,
            update_id=1,
            question="Build my timetable.",
            server_port="8002",
        )

    send_card.assert_not_called()


@IMAGES_ON
def test_the_legacy_delivery_path_rechecks_link_after_card_render(client, outbox):
    link = _link()

    def render_then_revoke(*_args, **_kwargs):
        link.revoke()
        return [b"png"]

    with (
        mock.patch("telegram_gateway.rendering.render_cards", side_effect=render_then_revoke),
        _adviser(answer="Here is your timetable.", presentation=_timetable_presentation()),
    ):
        bot.answer_question(
            link_id=link.pk,
            update_id=1,
            question="Build my timetable.",
            server_port="8002",
        )

    assert outbox.texts
    assert outbox.photos == []


@IMAGES_ON
def test_no_render_is_attempted_when_the_base_url_is_unknown(client, cards):
    """Better to say so than to fetch a guessed port and call the failure a
    fallback."""
    from telegram_gateway.rendering import local_base_url, render_card

    with override_settings(TELEGRAM_INTERNAL_BASE_URL=""):
        assert local_base_url("") == ""
        assert render_card(message_id="x", base_url="") is None
    assert cards.requested == [], "a card was fetched with no base URL"


def test_the_internal_base_url_defaults_to_the_request_port_and_stays_local():
    """And a NON-local override is refused.

    The natural workaround for a broken loopback fetch is to point this at the
    public origin — which would send a signed card token, a bearer credential for
    one student's timetable, across the internet and back through the edge on
    every render. Refused rather than trusted.
    """
    from telegram_gateway.rendering import local_base_url

    with override_settings(TELEGRAM_INTERNAL_BASE_URL=""):
        assert local_base_url(8002) == "http://127.0.0.1:8002"
        assert local_base_url("") == ""

    for local in ("http://127.0.0.1:9000/", "http://localhost:9000"):
        with override_settings(TELEGRAM_INTERNAL_BASE_URL=local):
            assert local_base_url(8002) == local.rstrip("/")

    for remote in ("https://advisor.example.edu", "http://app.internal:9000"):
        with override_settings(TELEGRAM_INTERNAL_BASE_URL=remote):
            assert local_base_url(8002) == "", f"{remote} was accepted"


@IMAGES_ON
def test_option_zero_is_not_swallowed_by_a_falsy_default(client):
    """`{{ option_index|default:"-1" }}` is wrong for option 0, and wrong quietly.

    Django's `default` filter fires on any FALSY value, and the first option's
    index IS 0 — so it fell back to -1, the page showed every alternative with
    the first merely open, and the grid swap never ran. Options 1 and 2 rendered
    correctly, which is exactly why it looked like it worked.
    """
    from telegram_gateway.cards import sign_card

    conversation = AdvisorConversation.objects.create(student_id=MINE)
    m = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        content="هذا جدولك.",
        presentation=_timetable_presentation(),
        status=AdvisorMessage.STATUS_COMPLETED,
    )

    body = _card_request(
        client,
        sign_card(message_id=m.pk, option_index=0),
    ).content.decode("utf-8")
    assert "var wanted = 0;" in body, "option 0 was turned into a fallback"

    body_one = _card_request(
        client,
        sign_card(message_id=m.pk, option_index=1),
    ).content.decode("utf-8")
    assert "var wanted = 1;" in body_one

    # And no index at all still means "render as the screen draws it".
    body_none = _card_request(client, sign_card(message_id=m.pk)).content.decode("utf-8")
    assert "var wanted = -1;" in body_none


@IMAGES_ON
@pytest.mark.parametrize(
    ("baseline_kind", "section_field"),
    [
        ("REGISTERED", "baseline_sections"),
        ("REGISTERED", "current_sections"),
        ("EXPECTED_PLAN", "expected_plan_sections"),
    ],
)
def test_a_baseline_only_card_expands_and_gridifies_every_supported_baseline_shape(
    client: Client,
    baseline_kind: str,
    section_field: str,
) -> None:
    """A screenshot has no click target, so a closed baseline is an empty answer."""

    from core.services.advisor_presentations import KIND_TIMETABLE, normalise_presentation
    from telegram_gateway.cards import sign_card

    section = {
        "course_code": "AI331",
        "course_name": "Machine Learning",
        "section": "M2",
        "credits": 4,
        "meetings": ["SUN 09:00-10:15", "WED 10:30\u201311:45"],
    }
    presentation = {
        "kind": KIND_TIMETABLE,
        "baseline_kind": baseline_kind,
        section_field: [section],
        "alternatives": [],
    }
    normalised = normalise_presentation(presentation)
    assert normalised["baseline_sections"] == [section]

    conversation = AdvisorConversation.objects.create(student_id=MINE)
    assistant = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        content="Here is your timetable.",
        presentation=presentation,
        status=AdvisorMessage.STATUS_COMPLETED,
    )

    response = _card_request(client, sign_card(message_id=assistant.pk))
    body = response.content.decode("utf-8")

    assert response.status_code == 200
    assert "var wanted = -1;" in body
    assert "AI331" in body and "SUN 09:00-10:15" in body
    assert "var retained = root.querySelector('details.sa-tt-current');" in body
    assert "retained.open = true;" in body
    assert "gridifyBaseline(retained, data.baseline_sections || []);" in body
    assert "function gridifyBaseline(details, sections)" in body
    assert "course_code: course.course_code" in body
    assert "course_name: course.course_name" in body
    assert "section: course.section" in body
    assert "window.WeekGrid.renderWeekGrid" in body


@IMAGES_ON
def test_the_card_page_leaks_no_template_comments(client):
    """Django `{# #}` comments are SINGLE-LINE. A multi-line one is not stripped:
    it is served verbatim, and inside a <script> it produced
    `SyntaxError: Invalid or unexpected token` and a screenshot timeout. This
    screen has grown that defect before, so it is pinned rather than remembered."""
    from telegram_gateway.cards import sign_card

    conversation = AdvisorConversation.objects.create(student_id=MINE)
    m = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        content="x",
        presentation=_timetable_presentation(),
        status=AdvisorMessage.STATUS_COMPLETED,
    )
    response = _card_request(client, sign_card(message_id=m.pk))
    body = response.content.decode("utf-8")

    # Positive control FIRST: an empty 404 body would satisfy every assertion
    # below without the page ever having rendered.
    assert response.status_code == 200
    assert "sa-card-root" in body and "AI331" in body

    assert "{#" not in body and "#}" not in body
    # And the internal commentary itself never reaches the wire.
    for phrase in ("Card-only page", "refuses to initialise", "shared week-grid"):
        assert phrase not in body, f"template commentary leaked: {phrase!r}"


def test_the_card_template_has_no_multiline_django_comments():
    """The source-level guard for the same trap, across every gateway template."""
    import re
    from pathlib import Path

    pattern = re.compile(r"\{#(?:(?!#\})[\s\S])*\n(?:(?!#\})[\s\S])*#\}")
    for path in Path("telegram_gateway/templates").rglob("*.html"):
        found = pattern.findall(path.read_text(encoding="utf-8"))
        assert not found, f"{path} has a multi-line {{# #}} comment; use {{% comment %}}"


@IMAGES_ON
def test_the_image_uses_the_shared_week_grid_not_a_new_one(client):
    """The picture reuses `WeekGrid.renderWeekGrid` — the same primitive the
    planner and the student timetable use — rather than becoming a fifth bespoke
    rendering of a week. The chat thread keeps its meetings list, which is the
    right shape for a narrow bubble."""
    from pathlib import Path

    from telegram_gateway.cards import sign_card

    conversation = AdvisorConversation.objects.create(student_id=MINE)
    m = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        content="x",
        presentation=_timetable_presentation(),
        status=AdvisorMessage.STATUS_COMPLETED,
    )
    body = _card_request(client, sign_card(message_id=m.pk)).content.decode("utf-8")

    assert "shared-timetable.js" in body, "the card page does not load WeekGrid"
    assert "WeekGrid.renderWeekGrid" in body

    # The web thread is untouched: no grid call was added to the adviser module.
    js = Path("static/js/page-student-advisor.js").read_text(encoding="utf-8")
    assert "renderWeekGrid" not in js, "option 1 was 'image only'; the screen changed too"


# ── housekeeping: the tables nobody reads once they have done their job ──


def test_purge_removes_only_rows_past_the_window(settings):
    """Receipts grow at one row per message the bot ever receives, and tokens at
    one per /link. Neither is read again once spent."""
    from telegram_gateway.linking import purge_expired
    from telegram_gateway.models import TelegramLinkToken, TelegramUpdateReceipt

    old = timezone.now() - timedelta(days=30)
    fresh = linking.issue_link_token(telegram_user_id=CHAT)
    TelegramUpdateReceipt.objects.create(update_id=1)
    TelegramUpdateReceipt.objects.create(update_id=2)
    TelegramUpdateReceipt.objects.create(
        update_id=3,
        kind=TelegramUpdateReceipt.KIND_QUESTION,
        status=TelegramUpdateReceipt.STATUS_QUEUED,
        payload_text="still waiting",
    )

    # Backdate one of each; `auto_now_add` has to be written around.
    TelegramUpdateReceipt.objects.filter(update_id=1).update(received_at=old)
    TelegramUpdateReceipt.objects.filter(update_id=3).update(received_at=old)
    stale = TelegramLinkToken.objects.create(
        token_hash="deadbeef" * 8,
        telegram_user_id=OTHER_CHAT,
        expires_at=timezone.now() - timedelta(days=29),
    )
    TelegramLinkToken.objects.filter(pk=stale.pk).update(created_at=old)

    tokens, receipts = purge_expired(timedelta(days=7))

    assert tokens == 1 and receipts == 1
    assert TelegramUpdateReceipt.objects.filter(update_id=2).exists(), "a fresh receipt was purged"
    assert TelegramUpdateReceipt.objects.filter(update_id=3).exists(), "live work was purged"
    assert linking.peek_token(fresh.raw_token) is not None, "a live token was purged"


def test_the_purge_command_is_dry_by_default():
    """A command that deletes on a bare invocation deletes when somebody runs it
    to see what it does."""
    from io import StringIO

    from django.core.management import call_command

    from telegram_gateway.models import TelegramUpdateReceipt

    TelegramUpdateReceipt.objects.create(update_id=99)
    TelegramUpdateReceipt.objects.filter(update_id=99).update(
        received_at=timezone.now() - timedelta(days=30)
    )

    out = StringIO()
    call_command("purge_telegram_tokens", stdout=out)
    assert "DRY RUN" in out.getvalue()
    assert TelegramUpdateReceipt.objects.filter(update_id=99).exists(), "dry run deleted"

    out = StringIO()
    call_command("purge_telegram_tokens", "--apply", stdout=out)
    assert not TelegramUpdateReceipt.objects.filter(update_id=99).exists()


# ── the card token as an authentication boundary ─────────────────


@IMAGES_ON
def test_a_forged_signature_is_refused(client):
    """The signature IS the authentication on this endpoint — there is no session
    to fall back on. A structurally valid token whose signature is wrong must be
    indistinguishable from one that was never real."""
    from telegram_gateway.cards import sign_card, unsign_card

    conversation = AdvisorConversation.objects.create(student_id=MINE)
    m = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        content="x",
        presentation=_timetable_presentation(),
        status=AdvisorMessage.STATUS_COMPLETED,
    )
    good = sign_card(message_id=m.pk)
    assert _card_request(client, good).status_code == 200  # positive control

    # One character of the signature flipped — everything else identical.
    head, _, sig = good.rpartition(":")
    flipped = "B" if sig[0] != "B" else "C"
    forged = f"{head}:{flipped}{sig[1:]}"
    assert forged != good and len(forged) == len(good)

    assert unsign_card(forged) is None
    assert _card_request(client, forged).status_code == 404

    # And an empty token, which the earlier `bad or 'x'` loop never actually sent.
    assert unsign_card("") is None


@IMAGES_ON
def test_a_signed_card_url_alone_is_not_enough_without_the_renderer_header(
    client: Client,
) -> None:
    from telegram_gateway.cards import sign_card

    conversation = AdvisorConversation.objects.create(student_id=MINE)
    message = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        content="x",
        presentation=_timetable_presentation(),
        status=AdvisorMessage.STATUS_COMPLETED,
    )
    token = sign_card(message_id=message.pk)

    assert client.get(f"/telegram/card/{token}/").status_code == 404
    assert (
        client.get(
            f"/telegram/card/{token}/",
            HTTP_X_TELEGRAM_CARD_RENDERER="forged",
        ).status_code
        == 404
    )
    assert _card_request(client, token).status_code == 200


def test_a_card_token_carries_no_student_identifier():
    """Decoded, not grepped. `signing.dumps` base64-encodes its payload, so
    asserting a decimal student id is absent from the token string proves
    nothing — a token containing one would pass that check."""
    from telegram_gateway.cards import sign_card, unsign_card

    payload = unsign_card(sign_card(message_id="abc-123", option_index=2))
    assert payload is not None
    assert set(payload) <= {"m", "i"}, f"the token carries extra fields: {payload}"


def test_the_card_token_window_is_short():
    """Pinned, not patched. It is a bearer URL to one student's timetable; the
    earlier test mocked this constant away rather than asserting it."""
    from telegram_gateway.cards import CARD_TOKEN_MAX_AGE_SECONDS

    assert 0 < CARD_TOKEN_MAX_AGE_SECONDS <= 300


def test_renderer_proof_keeps_the_established_authentication_purpose():
    """A rollout must remain compatible with proofs minted by the prior worker."""
    from django.core import signing

    from telegram_gateway.cards import verify_renderer_request

    established_proof = signing.dumps(
        "render_timetable_card",
        salt="telegram_gateway.card_renderer",
    )

    assert verify_renderer_request(established_proof)


@IMAGES_ON
def test_the_card_page_refuses_a_token_naming_a_student_message(client):
    """The token names a message id; the view must still insist it is an
    ASSISTANT turn. Without that filter a signed token plus a guessed id renders
    whatever presentation a student-role row happens to carry."""
    from telegram_gateway.cards import sign_card

    conversation = AdvisorConversation.objects.create(student_id=MINE)
    student_turn = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_STUDENT,
        content="ابنِ لي جدولًا",
        presentation=_timetable_presentation(),
        status=AdvisorMessage.STATUS_COMPLETED,
    )
    assert _card_request(client, sign_card(message_id=student_turn.pk)).status_code == 404


@CHANNEL_ON
def test_the_card_endpoint_retires_with_the_image_flag(client):
    """With images off nothing mints a token, so leaving the endpoint live is
    surface for no purpose."""
    from telegram_gateway.cards import sign_card

    conversation = AdvisorConversation.objects.create(student_id=MINE)
    m = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        content="x",
        presentation=_timetable_presentation(),
        status=AdvisorMessage.STATUS_COMPLETED,
    )
    assert _card_request(client, sign_card(message_id=m.pk)).status_code == 404


# ── the image phase must never cost the answer ───────────────────


@IMAGES_ON
def test_a_raising_image_phase_still_delivers_the_text(client, outbox, cards):
    """Proved by the review: without a guard the student got the acknowledgement
    and then silence for ever — answer stored, webhook already 200'd so Telegram
    never redelivers, exception swallowed by the background runner."""
    link = _link()
    with (
        mock.patch("telegram_gateway.rendering.local_base_url", side_effect=RuntimeError("boom")),
        _adviser(answer="هذا جدولك.", presentation=_timetable_presentation()),
    ):
        bot.answer_question(
            link_id=link.pk,
            update_id=1,
            question="ابنِ لي جدولًا",
            server_port="8000",
        )

    assert outbox.photos == []
    body = " ".join(outbox.texts)
    assert "هذا جدولك." in body, "the answer was lost with the picture"


@IMAGES_ON
def test_one_image_per_option_capped(client, outbox, cards):
    """Six alternatives must not become six browser renders and six messages."""
    from telegram_gateway.bot import MAX_CARD_IMAGES

    link = _link()
    presentation = _timetable_presentation()
    presentation["alternatives"] = [
        {
            "planner_options": [f"A{i}"],
            "meetings": [
                {
                    "day": "SUN",
                    "start": "09:00",
                    "end": "10:15",
                    "course_code": "AI331",
                    "section": "M1",
                }
            ],
            "scheduled_courses": 1,
            "target_courses": 1,
            "total_credit_hours": 4,
        }
        for i in range(6)
    ]
    with _adviser(answer="هذا جدولك.", presentation=presentation):
        bot.answer_question(
            link_id=link.pk,
            update_id=1,
            question="ابنِ لي جدولًا",
            server_port="8000",
        )

    assert len(cards.requested) == MAX_CARD_IMAGES == 4
    assert len(outbox.photos) == MAX_CARD_IMAGES

    from telegram_gateway.cards import unsign_card

    indexes = [unsign_card(u.split("/telegram/card/")[1].rstrip("/"))["i"] for u in cards.requested]
    assert indexes == [0, 1, 2, 3]


# ── the production shape, which is where this feature actually broke ──


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class _FakePage:
    def __init__(self, status: int = 200, error: str | None = None) -> None:
        self._status = status
        self._error = error
        self.goto_kwargs: dict = {}

    def goto(self, url, **kwargs):
        self.goto_kwargs = kwargs
        return _FakeResponse(self._status)

    def wait_for_selector(self, selector, **kwargs):
        return object()

    def query_selector(self, selector):
        page = self

        class _Root:
            def get_attribute(self, name):
                return page._error if name == "data-card-error" else None

            def screenshot(self, **kwargs):
                return b"PNG-BYTES"

        return _Root()


class _FakeBrowser:
    def __init__(self, page: _FakePage) -> None:
        self._page = page
        self.context_kwargs: dict = {}
        self.closed = False

    def new_context(self, **kwargs):
        self.context_kwargs = kwargs
        browser = self

        class _Ctx:
            def new_page(self):
                return browser._page

        return _Ctx()

    def close(self):
        self.closed = True


def _fake_playwright(page: _FakePage):
    """Drive PlaywrightCardRenderer without a browser, capturing what it asked for."""
    holder: dict = {}

    class _Chromium:
        def launch(self, **kwargs):
            holder["launch_kwargs"] = kwargs
            holder["browser"] = _FakeBrowser(page)
            return holder["browser"]

    class _P:
        chromium = _Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return (lambda: _P()), holder


def test_the_renderer_asserts_the_proxy_header_so_production_does_not_301():
    """`SECURE_SSL_REDIRECT` is on whenever DEBUG is off, and this fetch is plain
    HTTP over loopback. Without the header the card page AND every {% static %}
    asset 301 to https://127.0.0.1:PORT, where nothing speaks TLS — the renderer
    script never loads and the page reports `renderer-missing`. Exempting only the
    card path would fix the page and leave the assets broken."""
    from telegram_gateway import render_child
    from telegram_gateway.cards import sign_renderer_request

    page = _FakePage()
    fake, holder = _fake_playwright(page)
    with mock.patch("playwright.sync_api.sync_playwright", fake):
        out = render_child.render_urls(
            ["http://127.0.0.1:8002/telegram/card/t/"], sign_renderer_request()
        )

    assert out == [b"PNG-BYTES"]
    headers = holder["browser"].context_kwargs.get("extra_http_headers") or {}
    assert headers.get("X-Forwarded-Proto") == "https", headers
    from telegram_gateway.cards import verify_renderer_request

    assert verify_renderer_request(headers.get("X-Telegram-Card-Renderer", ""))
    assert holder["launch_kwargs"].get("timeout"), "launch has no deadline"
    assert page.goto_kwargs.get("wait_until") == "domcontentloaded"
    assert holder["browser"].closed, "the browser was not closed"


def test_a_non_200_card_page_is_named_in_the_log_not_swallowed(caplog):
    """400 (DisallowedHost) and 301 (SSL redirect) otherwise both surface as an
    indistinguishable TimeoutError, and an operator holding only
    `card render failed` has nothing to go on."""
    import logging

    from telegram_gateway import render_child
    from telegram_gateway.cards import sign_renderer_request

    caplog.set_level(logging.WARNING)
    for status in (400, 301, 500):
        caplog.clear()
        fake, _ = _fake_playwright(_FakePage(status=status))
        with mock.patch("playwright.sync_api.sync_playwright", fake):
            out = render_child.render_urls(
                ["http://127.0.0.1:8002/telegram/card/t/"], sign_renderer_request()
            )
        assert out == [None]
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert str(status) in logged, f"HTTP {status} was not named: {logged!r}"


def test_the_loopback_host_is_allowed_when_images_are_on():
    """The renderer fetches over loopback, so the loopback Host must be allowed —
    otherwise CommonMiddleware answers 400 and the screenshot waits 15s for an
    attribute that will never appear. Left to DJANGO_ALLOWED_HOSTS this never
    happens: the operator sets that to the public hostname.

    Re-imports the settings module with the flag on, because the flag is off in
    this process and the append is module-level.
    """
    import importlib
    import os

    import config.settings as live

    saved = {
        k: os.environ.get(k)
        for k in (
            "TELEGRAM_SEND_TIMETABLE_IMAGES",
            "TELEGRAM_SEND_GRADUATION_IMAGES",
            "DJANGO_ALLOWED_HOSTS",
        )
    }
    # FORCED, not defaulted: a developer's own .env carries
    # `DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost`, which would satisfy the
    # assertion whether or not the append exists — and did.
    os.environ["DJANGO_ALLOWED_HOSTS"] = "advisor.example.edu"
    try:
        os.environ["TELEGRAM_SEND_TIMETABLE_IMAGES"] = "false"
        os.environ["TELEGRAM_SEND_GRADUATION_IMAGES"] = "false"
        without = importlib.reload(live)
        assert "127.0.0.1" not in without.ALLOWED_HOSTS, (
            "the control failed: loopback is present with images OFF, so this "
            "test cannot prove the append does anything"
        )

        os.environ["TELEGRAM_SEND_TIMETABLE_IMAGES"] = "true"
        fresh = importlib.reload(live)
        assert fresh.TELEGRAM_SEND_TIMETABLE_IMAGES is True
        assert "127.0.0.1" in fresh.ALLOWED_HOSTS, fresh.ALLOWED_HOSTS
        assert "localhost" in fresh.ALLOWED_HOSTS
        assert "advisor.example.edu" in fresh.ALLOWED_HOSTS, "the public host was lost"
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(live)


@GRADUATION_IMAGES_ON
def test_durable_worker_materialises_validated_text_before_one_graduation_photo(
    client: Client,
    outbox: RecordingTransport,
    cards: RecordingRenderer,
) -> None:
    from telegram_gateway import jobs

    _link()
    answer = "Your graduation scenario is ready."
    manifests: list[list[dict[str, Any]]] = []
    original = bot._durable_delivery_items

    def capture_items(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        items = original(*args, **kwargs)
        manifests.append(items)
        return items

    with (
        mock.patch("telegram_gateway.bot._durable_delivery_items", side_effect=capture_items),
        _adviser(answer=answer, presentation=_graduation_presentation()),
    ):
        _post(client, _update(update_id=8804, text="When will I graduate?"))

    job = TelegramUpdateReceipt.objects.get(update_id=8804)
    assert job.status == TelegramUpdateReceipt.STATUS_SUCCEEDED
    assert len(manifests) == 1
    items = manifests[0]
    assert len(items) == 2
    assert items[0]["kind"] == jobs.DELIVERY_KIND_TEXT
    assert answer in items[0]["text"]
    assert items[1] == {
        "kind": jobs.DELIVERY_KIND_TIMETABLE_PHOTO,
        "option_index": None,
    }
    assert len(cards.requested) == 1
    assert len(outbox.photos) == 1
    delivered_text = " ".join(outbox.texts)
    assert answer in delivered_text
    assert "View the full plan and details on the platform:" in delivered_text
    assert "على المنصة" not in delivered_text


@IMAGES_ON
def test_durable_timetable_alternatives_still_materialise_one_photo_per_option(
    client: Client,
    outbox: RecordingTransport,
    cards: RecordingRenderer,
) -> None:
    from telegram_gateway import jobs
    from telegram_gateway.cards import unsign_card

    presentation = _timetable_presentation()
    presentation["alternatives"] = [
        {
            "planner_options": [f"A{index + 1}"],
            "meetings": [
                {
                    "day": "SUN",
                    "start": f"0{9 + index}:00",
                    "end": f"{10 + index}:15",
                    "course_code": "AI331",
                    "section": f"M{index + 1}",
                }
            ],
            "scheduled_courses": 1,
            "target_courses": 1,
            "total_credit_hours": 4,
        }
        for index in range(2)
    ]
    _link()
    manifests: list[list[dict[str, Any]]] = []
    original = bot._durable_delivery_items

    def capture_items(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        items = original(*args, **kwargs)
        manifests.append(items)
        return items

    with (
        mock.patch("telegram_gateway.bot._durable_delivery_items", side_effect=capture_items),
        _adviser(answer="Here are two timetable options.", presentation=presentation),
    ):
        _post(client, _update(update_id=8805, text="Build two timetable alternatives."))

    assert len(manifests) == 1
    items = manifests[0]
    assert items[0]["kind"] == jobs.DELIVERY_KIND_TEXT
    assert items[1:] == [
        {"kind": jobs.DELIVERY_KIND_TIMETABLE_PHOTO, "option_index": 0},
        {"kind": jobs.DELIVERY_KIND_TIMETABLE_PHOTO, "option_index": 1},
    ]
    assert len(items) == 3
    indexes = [
        unsign_card(url.split("/telegram/card/")[1].rstrip("/"))["i"] for url in cards.requested
    ]
    assert indexes == [0, 1]
    assert len(outbox.photos) == 2


@GRADUATION_IMAGES_ON
def test_personal_record_inside_graduation_card_suppresses_only_the_photo_recipe(
    client: Client,
    outbox: RecordingTransport,
    cards: RecordingRenderer,
) -> None:
    from telegram_gateway import jobs

    student = _student_row(MINE)
    student.gpa = 2.86
    student.save(update_fields=["gpa"])
    _link()
    presentation = _graduation_presentation()
    presentation["unresolved_requirements"] = [
        {
            "code": "DS492",
            "name": "GPA: 2.86",
            "missing_prerequisites": [],
        }
    ]
    safe_answer = "Your graduation scenario is available on the platform."

    from core.services.advisor_presentations import normalise_presentation

    normalised = normalise_presentation(presentation)
    assert bot._presentation_contains_personal_record(
        normalised,
        student_id=MINE,
        question="When will I graduate?",
    )
    manifests: list[list[dict[str, Any]]] = []
    original = bot._durable_delivery_items

    def capture_items(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        items = original(*args, **kwargs)
        manifests.append(items)
        return items

    with (
        mock.patch("telegram_gateway.bot._durable_delivery_items", side_effect=capture_items),
        _adviser(answer=safe_answer, presentation=presentation),
    ):
        _post(client, _update(update_id=8806, text="When will I graduate?"))

    assert len(manifests) == 1
    items = manifests[0]
    assert not any(item.get("kind") == jobs.DELIVERY_KIND_TIMETABLE_PHOTO for item in items)
    assert any(
        item.get("kind") == jobs.DELIVERY_KIND_TEXT and safe_answer in item.get("text", "")
        for item in items
    )
    assert cards.requested == []
    assert outbox.photos == []
    assert safe_answer in " ".join(outbox.texts)


@IMAGES_ON
def test_timetable_image_flag_alone_does_not_enable_graduation_photos(client, outbox, cards):
    """The narrower timetable-media consent cannot export a graduation map."""
    from core.services.advisor_presentations import KIND_GRADUATION, normalise_presentation

    graduation = {
        "kind": KIND_GRADUATION,
        "graph": {
            "extraNodes": ["AI331", "CS323"],
            "items": [{"course_code": "AI331", "prerequisite_course_code": "CS323"}],
        },
    }
    assert normalise_presentation(graduation), "the graduation fixture is not renderable"

    _link()
    with _adviser(answer="خطة تخرجك.", presentation=graduation):
        _post(client, _update(text="متى أتخرج؟"))

    assert cards.requested == [], "a browser was started for a non-timetable card"
    assert outbox.photos == []
    assert "خطة تخرجك." in " ".join(outbox.texts)


@IMAGES_ON
def test_signed_graduation_card_is_404_when_only_timetable_images_are_enabled(client):
    from telegram_gateway.cards import sign_card

    conversation = AdvisorConversation.objects.create(student_id=MINE)
    message = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        content="x",
        presentation=_graduation_presentation(),
        status=AdvisorMessage.STATUS_COMPLETED,
    )

    assert _card_request(client, sign_card(message_id=message.pk)).status_code == 404

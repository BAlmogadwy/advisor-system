"""Turning a chat into a student, once, deliberately, and never the other way round.

The rule this module exists to enforce is one sentence: **the student id comes
from the browser session and from nowhere else.** A Telegram user id, a username,
a phone number and a link token are all things an attacker can hold; none of them
is evidence of who somebody is at the university. What the token proves is much
smaller and exactly enough — that *this chat* asked to be linked, recently, and
has not been linked with this invitation already.

So the halves of the flow are deliberately asymmetric:

* the **token** carries the Telegram side (which chat), is opaque, is stored only
  as a hash, expires, and is single-use;
* the **session** carries the university side (which student), and is read through
  `AdvisorPrincipal.for_student`, which is the same fail-closed constructor the web
  adviser uses. It refuses anything that is not a signed-in student — which matters
  more than it looks, because `get_user_role` falls back to `ADVISOR` for an
  authenticated account with no group, and a channel that accepted that fallback
  would be handing adviser-tier identity to a chat bot.

Neither half is sufficient alone, and neither is ever derived from the other.

**And holding both is still not enough**, which is the part that took a review to
see. A token is a *bearer* credential: whoever opens the URL gets the page. So the
first version of this module linked `token.telegram_user_id` to
`session.student_id` — two facts it had every right to trust individually, joined
by nothing. An attacker types `/link` in their own chat, forwards the URL, and the
student's ordinary university login plus one confirm button binds the *attacker's*
chat to the *student's* record. The confirmation page could not warn them: it has
no identifier to show, deliberately.

The ceremony is therefore two-sided, and the second secret travels the other way —
browser → chat, redeemable only from the chat the token was minted in. See
`TelegramLinkToken` for why that direction and not the reverse.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.http import HttpRequest
from django.utils import timezone

from core.models import Student, UserScope
from core.services.advisor_principal import AdvisorPrincipal, IdentityError
from core.services.rbac import ROLE_STUDENT, get_user_role
from core.services.student_otp import has_recent_student_authentication

from .models import TelegramLink, TelegramLinkToken

logger = logging.getLogger(__name__)

#: 32 bytes of `secrets.token_urlsafe` is 43 URL-safe characters. Long enough that
#: guessing is not a strategy, short enough that the whole link still fits in one
#: Telegram message without wrapping.
_TOKEN_BYTES = 32

DEFAULT_TOKEN_TTL_SECONDS = 900


def token_ttl() -> timedelta:
    """How long an invitation stays open.

    Read at call time rather than captured at import, so the flag cannot go stale
    in a long-lived worker — the convention the LLM backend documents.
    """
    raw = getattr(settings, "TELEGRAM_LINK_TOKEN_TTL_SECONDS", DEFAULT_TOKEN_TTL_SECONDS)
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        seconds = DEFAULT_TOKEN_TTL_SECONDS
    # A non-positive TTL would mint tokens that are already expired, which reads to
    # a student as "linking is broken" rather than as a misconfiguration.
    return timedelta(seconds=max(60, seconds))


def hash_token(raw_token: str) -> str:
    """The stored form of a token.

    Plain SHA-256 with no salt, deliberately: the input is 256 bits of
    `secrets`-grade randomness, so there is no dictionary to stretch against, and
    an unsalted digest is what makes a constant-time lookup by hash possible.
    """
    return hashlib.sha256(str(raw_token or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IssuedToken:
    """The one moment the raw token exists. It is not stored anywhere."""

    raw_token: str
    expires_at: object


def issue_link_token(*, telegram_user_id: int) -> IssuedToken:
    """Mint an invitation for this chat.

    Any invitation this chat already holds is burned first. Otherwise a student
    who types `/link` three times leaves two live tokens behind, and "single use"
    stops meaning "one link per request".
    """
    now = timezone.now()
    TelegramLinkToken.objects.filter(
        telegram_user_id=telegram_user_id, consumed_at__isnull=True
    ).update(consumed_at=now)

    raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
    expires_at = now + token_ttl()
    TelegramLinkToken.objects.create(
        token_hash=hash_token(raw_token),
        telegram_user_id=telegram_user_id,
        expires_at=expires_at,
    )
    # No token, no telegram id, no student id. What is useful operationally is that
    # linking is being used at all.
    logger.info("telegram: link invitation issued")
    return IssuedToken(raw_token=raw_token, expires_at=expires_at)


def peek_token(raw_token: str) -> TelegramLinkToken | None:
    """The live invitation this token names, without consuming it.

    Used to render the confirmation page. Returning `None` for expired, consumed
    and non-existent alike is deliberate: they are the same answer to the person
    holding the URL, and distinguishing them would say whether a token was ever
    real.
    """
    if not raw_token:
        return None
    token = TelegramLinkToken.objects.filter(token_hash=hash_token(raw_token)).first()
    if token is None or not token.is_live:
        return None
    return token


class LinkError(Exception):
    """Linking could not be completed. Carries a machine-readable `code`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


#: The invitation is expired, already used, or was never real.
TOKEN_INVALID = "token_invalid"
#: The browser session is not a signed-in student.
NOT_A_STUDENT = "not_a_student"
#: This student already has a different Telegram chat linked.
STUDENT_ALREADY_LINKED = "student_already_linked"
#: This Telegram chat is already linked to a different student.
CHAT_ALREADY_LINKED = "chat_already_linked"


#: The confirmation code shown in the browser. Six characters from an alphabet
#: with no 0/O/1/I/L, because a student reads this off one screen and types it
#: into another and a misread character is a support ticket. ~1.07e9 combinations,
#: and a code is only ever checked against ONE approved token for ONE chat, with a
#: hard attempt cap — so the entropy is a typo margin, not the security boundary.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 6

#: How many wrong codes a chat may try before the invitation is burned.
MAX_CONFIRM_ATTEMPTS = 5

#: The confirmation code was wrong, or there is nothing awaiting confirmation in
#: this chat.
CONFIRM_INVALID = "confirm_invalid"


def _new_confirm_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def normalise_code(raw: str) -> str:
    """Codes are read off a screen: fold case and drop spaces and dashes."""
    return "".join(str(raw or "").split()).replace("-", "").upper()


def _active_student_account(*, user_id: Any, student_id: int) -> Any | None:
    """Return the exact still-authorised account, or fail closed.

    ``Student.status`` is academic standing and has no stable active/inactive
    vocabulary, so it is deliberately not an authentication input.  The account
    lifecycle is the Django user plus its STUDENT role and authoritative
    ``UserScope`` binding; the Student row must still exist as the academic
    subject the link would expose.
    """

    if not user_id:
        return None
    user = (
        get_user_model()
        ._default_manager.filter(
            pk=user_id,
            is_active=True,
            is_staff=False,
            is_superuser=False,
        )
        .first()
    )
    if user is None:
        return None
    if get_user_role(user) != ROLE_STUDENT:
        return None
    if not UserScope.objects.filter(user_id=user.pk, student_id=student_id).exists():
        return None
    if not Student.objects.filter(student_id=student_id).exists():
        return None
    return user


def _burn_live_tokens(
    *,
    student_ids: set[int] | None = None,
    telegram_user_ids: set[int] | None = None,
    preserve_token_ids: set[Any] | None = None,
) -> int:
    """Invalidate ceremonies related to an identity boundary.

    A confirmation code is a temporary authentication credential. It must not
    outlive a successful competing link or any revocation affecting the student
    or chat it could bind.
    """

    students = {int(value) for value in (student_ids or set())}
    chats = {int(value) for value in (telegram_user_ids or set())}
    scope = Q(pk__isnull=True)
    if students:
        scope |= Q(approved_student_id__in=students)
    if chats:
        scope |= Q(telegram_user_id__in=chats)
    if not students and not chats:
        return 0
    tokens = TelegramLinkToken.objects.filter(consumed_at__isnull=True).filter(scope)
    if preserve_token_ids:
        tokens = tokens.exclude(pk__in=preserve_token_ids)
    return tokens.update(consumed_at=timezone.now())


def _consume_token(token: TelegramLinkToken) -> bool:
    """Claim one live token without letting an exception roll the claim back."""

    return bool(
        TelegramLinkToken.objects.filter(
            pk=token.pk,
            consumed_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).update(consumed_at=timezone.now())
    )


def revoke_links(queryset: Any, *, preserve_token_ids: set[Any] | None = None) -> int:
    """Revoke a selected link queryset and burn every related live ceremony."""

    with transaction.atomic():
        targets = list(
            queryset.filter(status=TelegramLink.STATUS_ACTIVE).values_list(
                "pk", "student_id", "telegram_user_id"
            )
        )
        if not targets:
            return 0
        now = timezone.now()
        revoked = TelegramLink.objects.filter(pk__in=[row[0] for row in targets]).update(
            status=TelegramLink.STATUS_REVOKED,
            revoked_at=now,
            current_conversation=None,
        )
        _burn_live_tokens(
            student_ids={int(row[1]) for row in targets},
            telegram_user_ids={int(row[2]) for row in targets},
            preserve_token_ids=preserve_token_ids,
        )
        return int(revoked)


def active_account_for_link(
    link: TelegramLink, *, preserve_token_id: Any | None = None
) -> Any | None:
    """Revalidate a link's exact university account and revoke invalid links.

    This is intentionally a live database decision on every entry point, not a
    property remembered at link time.  Revocation is durable so deleting and
    recreating a university account cannot silently revive the old chat binding.
    """

    if link.status != TelegramLink.STATUS_ACTIVE:
        return None
    account = _active_student_account(
        user_id=link.university_user_id,
        student_id=int(link.student_id),
    )
    if account is not None:
        return account

    now = timezone.now()
    revoked = revoke_links(
        TelegramLink.objects.filter(
            pk=link.pk,
            status=TelegramLink.STATUS_ACTIVE,
        ),
        preserve_token_ids={preserve_token_id} if preserve_token_id else None,
    )
    if revoked:
        link.status = TelegramLink.STATUS_REVOKED
        link.revoked_at = now
        link.current_conversation = None
    logger.info("telegram: invalid university account binding revoked")
    return None


def _invalidate_conflicting_links(
    *, student_id: int, telegram_user_id: int, preserve_token_id: Any | None = None
) -> None:
    """Revoke stale rows before live uniqueness/conflict decisions are made."""

    conflicts = TelegramLink.objects.filter(status=TelegramLink.STATUS_ACTIVE).filter(
        Q(student_id=student_id) | Q(telegram_user_id=telegram_user_id)
    )
    for candidate in conflicts:
        active_account_for_link(candidate, preserve_token_id=preserve_token_id)


def approve_link(*, request: HttpRequest, raw_token: str) -> str:
    """Record that THIS student approves the invitation, and return the code.

    Approving is not linking. Nothing is bound here, because at this point the
    server knows which student is present but has no evidence that the browser and
    the chat belong to the same person — the URL may have been forwarded. What it
    can do is issue a secret that only this browser has seen, and require it back
    from the chat the token was minted in.

    The student id is taken from the session and from nowhere else. `for_student`
    raises unless the authenticated account is in the STUDENT role AND carries a
    `UserScope` student id — the two conditions that stop the `ADVISOR` role
    fallback from becoming a Telegram principal.

    Raises `LinkError`; returns the raw confirmation code, which is not stored.
    """
    # Repeated here as the authority boundary, not only in the page view. A direct
    # POST or a future caller must not be able to approve a sensitive account link
    # merely because an eight-hour browser session still exists.
    if not has_recent_student_authentication(request):
        raise LinkError(NOT_A_STUDENT)

    try:
        principal = AdvisorPrincipal.for_student(request)
    except IdentityError as exc:
        raise LinkError(NOT_A_STUDENT) from exc

    student_id = int(principal.student_id or 0)
    account = _active_student_account(user_id=request.user.pk, student_id=student_id)
    if account is None:
        raise LinkError(NOT_A_STUDENT)

    token = TelegramLinkToken.objects.filter(token_hash=hash_token(raw_token)).first()
    if token is None or not token.is_live:
        raise LinkError(TOKEN_INVALID)

    _invalidate_conflicting_links(
        student_id=student_id,
        telegram_user_id=int(token.telegram_user_id),
        preserve_token_id=token.pk,
    )

    # The states worth naming before a code is issued, so the student is told what
    # to do rather than watching a code fail later for a reason nobody explained.
    active = TelegramLink.objects.filter(status=TelegramLink.STATUS_ACTIVE)
    student_link = active.filter(student_id=student_id).first()
    if student_link is not None and (
        student_link.telegram_user_id != token.telegram_user_id
        or student_link.university_user_id != account.pk
    ):
        raise LinkError(STUDENT_ALREADY_LINKED)
    chat_link = active.filter(telegram_user_id=token.telegram_user_id).first()
    if chat_link is not None and (
        chat_link.student_id != student_id or chat_link.university_user_id != account.pk
    ):
        raise LinkError(CHAT_ALREADY_LINKED)

    code = _new_confirm_code()
    # One conditional UPDATE: re-approving replaces the code, and a token that
    # expired or was consumed between the read and the write is not approved at all.
    approved = TelegramLinkToken.objects.filter(
        pk=token.pk, consumed_at__isnull=True, expires_at__gt=timezone.now()
    ).update(
        approved_student_id=student_id,
        approved_user_id=account.pk,
        approved_at=timezone.now(),
        confirm_code_hash=hash_token(code),
        confirm_attempts=0,
    )
    if not approved:
        raise LinkError(TOKEN_INVALID)

    logger.info("telegram: a link invitation was approved in the browser")
    return code


def confirm_link(*, telegram_user_id: int, code: str) -> TelegramLink:
    """Complete the link, from the chat, using the code shown in the browser.

    This is the half that makes the ceremony two-sided. The lookup is scoped to
    the calling chat, so an approval earned in somebody else's browser is
    unreachable from any chat but the one that opened it — and a student who
    followed a forwarded URL and then messages the bot finds no approval of their
    own, which is the safe outcome rather than a confusing one.

    **Deliberately NOT wrapped in one transaction.** The rejection paths write —
    the attempt counter, and the burn once the cap is reached — and then raise. A
    single enclosing `atomic` would roll those writes back with the exception, so
    every wrong guess would be forgotten and the cap would count to one for ever.
    The successful path gets its own `atomic` below; the race guard is the
    conditional UPDATE on `consumed_at`, not a lock.

    Raises `LinkError`; never returns a partial result.
    """
    token = (
        TelegramLinkToken.objects.filter(
            telegram_user_id=int(telegram_user_id),
            consumed_at__isnull=True,
            expires_at__gt=timezone.now(),
            approved_student_id__isnull=False,
            approved_user_id__isnull=False,
        )
        .order_by("-approved_at")
        .first()
    )
    if token is None:
        raise LinkError(CONFIRM_INVALID)

    if token.confirm_attempts >= MAX_CONFIRM_ATTEMPTS:
        # Burn it rather than leave a guessable approval standing.
        TelegramLinkToken.objects.filter(pk=token.pk).update(consumed_at=timezone.now())
        raise LinkError(CONFIRM_INVALID)

    # Constant time: a byte-by-byte comparison of the digest would leak how much of
    # the code was right, and the code is short enough for that to matter.
    if not hmac.compare_digest(hash_token(normalise_code(code)), token.confirm_code_hash):
        TelegramLinkToken.objects.filter(pk=token.pk).update(
            confirm_attempts=F("confirm_attempts") + 1
        )
        raise LinkError(CONFIRM_INVALID)

    student_id = int(token.approved_student_id or 0)
    chat_id = int(token.telegram_user_id)
    if student_id <= 0:
        raise LinkError(CONFIRM_INVALID)

    account = _active_student_account(user_id=token.approved_user_id, student_id=student_id)
    if account is None:
        # This approval cannot become valid again: it names an exact account that
        # was deactivated, deleted, de-scoped, or ceased to be a student.
        TelegramLinkToken.objects.filter(pk=token.pk).update(consumed_at=timezone.now())
        raise LinkError(CONFIRM_INVALID)

    _invalidate_conflicting_links(student_id=student_id, telegram_user_id=chat_id)

    # Re-checked here, not only at approval time: the two are minutes apart and
    # either side may have linked something else in between.
    active = TelegramLink.objects.filter(status=TelegramLink.STATUS_ACTIVE)
    student_link = active.filter(student_id=student_id).first()
    if student_link is not None and (
        student_link.telegram_user_id != chat_id or student_link.university_user_id != account.pk
    ):
        if not _consume_token(token):
            raise LinkError(CONFIRM_INVALID)
        raise LinkError(STUDENT_ALREADY_LINKED)
    chat_link = active.filter(telegram_user_id=chat_id).first()
    if chat_link is not None and (
        chat_link.student_id != student_id or chat_link.university_user_id != account.pk
    ):
        if not _consume_token(token):
            raise LinkError(CONFIRM_INVALID)
        raise LinkError(CHAT_ALREADY_LINKED)

    existing = active.filter(
        telegram_user_id=chat_id,
        student_id=student_id,
        university_user_id=account.pk,
    ).first()
    if existing is not None:
        # Already linked, and the same pair. Idempotent by effect, but the code is
        # still single-use: leaving it live lets it recreate this link after an
        # unlink without another authenticated browser ceremony.
        if not _consume_token(token):
            raise LinkError(CONFIRM_INVALID)
        _burn_live_tokens(student_ids={student_id}, telegram_user_ids={chat_id})
        return existing

    # The claim and the link are one unit: a token marked spent with no link to
    # show for it is an invitation the student can never redeem and never reissue.
    with transaction.atomic():
        # Single use, claimed in ONE statement — two `/confirm`s of the same code
        # cannot both link.
        claimed = TelegramLinkToken.objects.filter(
            pk=token.pk, consumed_at__isnull=True, expires_at__gt=timezone.now()
        ).update(consumed_at=timezone.now())
        if not claimed:
            raise LinkError(CONFIRM_INVALID)

        try:
            link = TelegramLink.objects.create(
                telegram_user_id=chat_id,
                student_id=student_id,
                university_user_id=account.pk,
                status=TelegramLink.STATUS_ACTIVE,
            )
            _burn_live_tokens(student_ids={student_id}, telegram_user_ids={chat_id})
        except IntegrityError as exc:
            # The partial unique constraints caught a concurrent link. The database
            # is the authority here, not the checks above — those are for a good
            # error message, this is for correctness.
            raise LinkError(CHAT_ALREADY_LINKED) from exc

    logger.info("telegram: chat linked to a student account")
    return link


def active_link_for_chat(telegram_user_id: int) -> TelegramLink | None:
    """The verified student behind this chat, or nothing.

    Every academic path in the gateway starts here. The status filter retrieves
    only a candidate; the exact bound university account is revalidated before
    that candidate is returned, and an invalid candidate is durably revoked.
    """
    link = TelegramLink.objects.filter(
        telegram_user_id=int(telegram_user_id),
        status=TelegramLink.STATUS_ACTIVE,
    ).first()
    if link is None or active_account_for_link(link) is None:
        return None
    return link


def active_link_by_id(link_id: Any) -> TelegramLink | None:
    """Resolve queued/background work through the same live account gate."""

    link = TelegramLink.objects.filter(pk=link_id, status=TelegramLink.STATUS_ACTIVE).first()
    if link is None or active_account_for_link(link) is None:
        return None
    return link


def unlink_chat(telegram_user_id: int) -> bool:
    """Revoke this chat's link immediately. True when there was one to revoke."""
    link = active_link_for_chat(telegram_user_id)
    if link is None:
        return False
    link.revoke()
    logger.info("telegram: chat unlinked")
    return True


def revoke_links_for_student(student_id: int) -> int:
    """Administrator revocation, by student rather than by chat.

    The safe direction for staff: an administrator acting on a report of a lost
    phone knows the student, and should never have to be told a Telegram id to do
    their job. Returns the number of links revoked.
    """
    return revoke_links(
        TelegramLink.objects.filter(student_id=int(student_id), status=TelegramLink.STATUS_ACTIVE)
    )


def purge_expired(older_than: timedelta | None = None) -> tuple[int, int]:
    """Delete only old dead tokens and old terminal receipt/jobs.

    Neither table is read once its row has served its purpose — a consumed token
    can never link again. QUEUED/RUNNING jobs remain actionable regardless of
    age. Returns `(tokens, receipts)` deleted.
    """
    now = timezone.now()
    cutoff = now - (older_than or timedelta(days=7))
    tokens, _ = (
        TelegramLinkToken.objects.filter(created_at__lt=cutoff)
        .filter(Q(consumed_at__isnull=False) | Q(expires_at__lte=now))
        .delete()
    )
    receipts, _ = purgeable_terminal_receipts(cutoff).delete()
    return tokens, receipts


def purgeable_terminal_receipts(cutoff: datetime):
    """Terminal receipts whose retention period has elapsed since completion.

    Legacy inline receipts predate ``finished_at`` and legitimately have it null;
    their receipt time is the only available terminal timestamp. Durable work
    with a completion timestamp is retained for the full window after it
    actually finishes, however long it waited in the queue first.
    """

    from .models import TelegramUpdateReceipt

    terminal = (
        TelegramUpdateReceipt.STATUS_SUCCEEDED,
        TelegramUpdateReceipt.STATUS_FAILED,
        TelegramUpdateReceipt.STATUS_CANCELLED,
    )
    terminal_age = Q(finished_at__lt=cutoff) | Q(
        finished_at__isnull=True,
        received_at__lt=cutoff,
    )
    return TelegramUpdateReceipt.objects.filter(terminal_age, status__in=terminal)


__all__ = [
    "CHAT_ALREADY_LINKED",
    "CONFIRM_INVALID",
    "MAX_CONFIRM_ATTEMPTS",
    "NOT_A_STUDENT",
    "STUDENT_ALREADY_LINKED",
    "TOKEN_INVALID",
    "IssuedToken",
    "LinkError",
    "active_account_for_link",
    "active_link_by_id",
    "active_link_for_chat",
    "approve_link",
    "confirm_link",
    "hash_token",
    "issue_link_token",
    "normalise_code",
    "peek_token",
    "purge_expired",
    "purgeable_terminal_receipts",
    "revoke_links_for_student",
    "revoke_links",
    "token_ttl",
    "unlink_chat",
]

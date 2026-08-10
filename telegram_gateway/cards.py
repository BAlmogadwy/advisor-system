"""A picture of the timetable card, drawn by the card's own renderer.

The Telegram channel sends an image of a proposed timetable. The tempting way to
build that is a server-side drawing routine — Pillow, matplotlib — and it is the
wrong way twice over.

**It would be a second renderer.** The web card is drawn by
`renderTimetablePresentation` in `static/js/page-student-advisor.js`. A second
implementation is a second answer to "what does a timetable look like", and the
two drift silently: this codebase has already paid for that with a lecture grid
duplicated in four places and three cohort classifiers disagreeing about `" M1"`.
The student would compare the image against the screen it links to and find them
different.

**And it would re-open the Arabic problem.** Pillow does no Arabic shaping, so
«الأحد» comes out as disconnected letters in reverse. Getting that right means
re-fighting the bidi battle that #66 already fought in the template layer, in a
new place where none of that work applies.

So the image is a screenshot of the real card, produced by loading the real
renderer with the stored presentation. One renderer, and Arabic shaping stays the
browser's job.

**The URL is signed, not session-authenticated.** The headless browser has no
session and must not be given one — minting a login so a screenshot can be taken
is exactly the kind of shortcut that becomes an authentication hole. Instead the
server signs `(message id, option index)` with `SECRET_KEY` for a window barely
longer than a screenshot takes. Nothing mints one of these for a user; the only
caller is the renderer running on this machine.
"""

from __future__ import annotations

from typing import Any

from django.core import signing

#: Signing namespace. A dedicated salt means a token minted here cannot be
#: replayed against any other signed value in the project.
_SALT = "telegram_gateway.card"

#: How long a signed card URL stays valid. Long enough for a cold browser start,
#: short enough that a URL captured from a process list is already dead.
CARD_TOKEN_MAX_AGE_SECONDS = 180


def sign_card(*, message_id: Any, option_index: int | None = None) -> str:
    """Mint a short-lived token naming one card to render."""
    payload: dict[str, Any] = {"m": str(message_id)}
    if option_index is not None:
        payload["i"] = int(option_index)
    return signing.dumps(payload, salt=_SALT)


def unsign_card(token: str) -> dict[str, Any] | None:
    """The card this token names, or `None` for anything expired or forged.

    One answer for expired, tampered and never-real, for the same reason the link
    tokens give one answer: distinguishing them tells the holder which half to
    work on.
    """
    try:
        payload = signing.loads(str(token or ""), salt=_SALT, max_age=CARD_TOKEN_MAX_AGE_SECONDS)
    except signing.BadSignature:
        return None
    if not isinstance(payload, dict) or not payload.get("m"):
        return None
    return payload


__all__ = ["CARD_TOKEN_MAX_AGE_SECONDS", "sign_card", "unsign_card"]

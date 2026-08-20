"""The saved portal sign-in, minted by a person and reused by the scraper.

WHY THE SCRAPER NO LONGER TYPES A PASSWORD

Driving Microsoft Entra unattended means holding the staff password in `.env`,
and it means every automated mis-step is a failed sign-in counted by Entra smart
lockout and ADFS extranet lockout. It also cannot work at all the moment the
tenant asks for MFA, consent, device compliance or a Conditional Access
interrupt — an unattended scraper must refuse those, not automate around them.

So a person signs in once, in a real browser window, doing whatever the tenant
asks including MFA. Playwright captures the resulting cookies and local storage,
and every later scrape starts from that state. The password never leaves the
person; the scraper never types one.

WHAT THE SAVED FILE IS

A live credential. Anyone holding it is signed in as that staff account until it
expires. It is written outside version control, with owner-only permissions where
the platform supports them, and nothing in this project ever logs its contents.
Treat a leaked session file exactly like a leaked password: sign out of the portal
to invalidate it, then mint a new one.

WHY EXPIRY IS CHECKED BY USING IT

Cookie expiry timestamps say when a cookie stops being *sent*, not when the
portal stops honouring the session — the portal can drop a session at any time,
and the ``SESSION_LOGGED_OUT_HTML`` path exists because it does. So validity here
means one thing: load the state, open the portal, and see whether an authenticated
page comes back. Anything else is a guess about somebody else's server.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from django.conf import settings


class PortalSessionError(RuntimeError):
    """No usable saved portal session."""

    #: Every message a person sees for this ends with the same instruction,
    #: because there is exactly one way out and it should never be guessed at.
    REMEDY = "Run:  .venv/Scripts/python.exe manage.py portal_login"

    def __init__(self, problem: str) -> None:
        super().__init__(f"{problem}\n{self.REMEDY}")


def session_path() -> Path:
    """Where the saved session lives. Overridable, never inside the repo tree."""
    configured = str(getattr(settings, "PORTAL_SESSION_STATE_PATH", "") or "").strip()
    if configured:
        return Path(configured)
    return Path(settings.BASE_DIR) / ".portal_session.json"


def load_state() -> dict[str, Any]:
    """The saved storage state, or raise with the one instruction that fixes it."""
    path = session_path()
    if not path.exists():
        raise PortalSessionError(f"No saved portal session at {path}.")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PortalSessionError(f"Could not read the saved portal session: {exc}") from None
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        # Deliberately does not echo the file. It is a credential, and a parse
        # error is exactly the moment somebody pastes the message into a chat.
        raise PortalSessionError(f"The saved portal session at {path} is not valid JSON.") from None
    if not isinstance(state, dict) or not state.get("cookies"):
        raise PortalSessionError(f"The saved portal session at {path} carries no cookies.")
    return state


def save_state(state: dict[str, Any]) -> Path:
    """Write the storage state, owner-only where the platform allows it."""
    path = session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")
    try:
        # POSIX honours this properly; on Windows it clears the read-only bit at
        # best. It is a cheap improvement where it works and harmless where it
        # does not — the real protection is that the path is outside the repo and
        # in .gitignore.
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return path


def has_session() -> bool:
    """Is there a saved session at all? Says nothing about whether it still works."""
    try:
        load_state()
    except PortalSessionError:
        return False
    return True


#: The enquiry form the scraper fills for every student. Its presence is the
#: strongest available proof that a session is usable — stronger than any
#: navigation marker, because it is literally the thing the scrape needs next.
_ENQUIRY_FORM_MARKER = 'name="StudentNumber"'


async def page_shows_live_session(page: Any) -> bool:
    """Navigate ``page`` to a page that REQUIRES a session, and report the answer.

    THE URL MATTERS AND WAS WRONG

    This used to navigate to ``PORTAL_LOGIN_URL`` — `staffLogin.do?ex=preLogin`,
    the SIGN-IN ENTRY PAGE. That page renders a sign-in form whether or not you
    hold a session, so the check reported "not live" for a session that had just
    been minted successfully, and `portal_login` threw away a real sign-in an
    operator had just completed by hand. Measured, not theorised: the attended
    login printed "Signed in." and the verification then failed.

    So liveness is decided on ``STUDENT_PLAN_URL``, the student-enquiry page the
    scraper actually uses, and the proof is the enquiry form itself. A session that
    can load that form is a session that can scrape; anything weaker is a proxy for
    the question rather than the question.

    Takes a PAGE rather than a context so a caller that already has one — the
    scrape's session anchor, for instance — does not open a second just to ask.
    """
    from core.services.portal_scraper import is_logged_out, is_logged_out_html, safe_page_content

    try:
        await page.goto(
            str(getattr(settings, "STUDENT_PLAN_URL", "")),
            wait_until="domcontentloaded",
            timeout=30000,
        )
        if await is_logged_out(page):
            return False
        html = await safe_page_content(page)
        return _ENQUIRY_FORM_MARKER in html and not is_logged_out_html(html)
    except Exception:
        return False


async def session_is_live(context: Any) -> bool:
    """Does this context hold an authenticated portal session? Opens a scratch page."""
    page = await context.new_page()
    try:
        return await page_shows_live_session(page)
    finally:
        with contextlib.suppress(Exception):
            await page.close()


__all__ = [
    "PortalSessionError",
    "has_session",
    "load_state",
    "save_state",
    "page_shows_live_session",
    "session_is_live",
    "session_path",
]

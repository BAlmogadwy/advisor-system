"""The operator-minted portal session: what it promises and what it refuses.

The saved file is a live credential, so two properties matter as much as the
happy path: every failure names the ONE command that fixes it, and no failure
ever echoes the file's contents into a message somebody will paste into a chat.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.services import portal_session
from core.services.portal_session import (
    PortalSessionError,
    has_session,
    load_state,
    page_shows_live_session,
    save_state,
    session_path,
)

LIVE_STATE = {"cookies": [{"name": "JSESSIONID", "value": "SESSION-SECRET-VALUE"}], "origins": []}


@pytest.fixture
def session_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "portal_session.json"
    monkeypatch.setattr(
        portal_session.settings, "PORTAL_SESSION_STATE_PATH", str(path), raising=False
    )
    return path


def test_the_default_path_is_outside_the_repo_tree(monkeypatch: pytest.MonkeyPatch):
    """A session file inside a tracked directory is a credential one `git add -A`
    away from being published."""
    monkeypatch.setattr(portal_session.settings, "PORTAL_SESSION_STATE_PATH", "", raising=False)
    assert session_path().name == ".portal_session.json"
    assert session_path().suffix == ".json"


def test_save_and_load_round_trip(session_file: Path):
    saved = save_state(LIVE_STATE)
    assert saved == session_file
    assert load_state() == LIVE_STATE
    assert has_session() is True


def test_a_missing_session_names_the_command_that_fixes_it(session_file: Path):
    assert has_session() is False
    with pytest.raises(PortalSessionError) as excinfo:
        load_state()
    assert "portal_login" in str(excinfo.value)


@pytest.mark.parametrize(
    ("contents", "because"),
    [
        ("not json at all {{{", "unparseable"),
        (json.dumps({"cookies": []}), "no cookies"),
        (json.dumps({"origins": []}), "no cookies key"),
        (json.dumps(["a", "list"]), "not an object"),
    ],
)
def test_an_unusable_session_is_refused_with_the_remedy(
    session_file: Path, contents: str, because: str
):
    session_file.write_text(contents, encoding="utf-8")
    assert has_session() is False
    with pytest.raises(PortalSessionError) as excinfo:
        load_state()
    assert "portal_login" in str(excinfo.value), because


def test_a_failure_message_never_echoes_the_session_file(session_file: Path):
    """A parse error is exactly the moment somebody pastes the message into a
    chat. The cookie value must not travel with it."""
    session_file.write_text(
        '{"cookies": [{"value": "SESSION-SECRET-VALUE"}] BROKEN', encoding="utf-8"
    )
    with pytest.raises(PortalSessionError) as excinfo:
        load_state()
    assert "SESSION-SECRET-VALUE" not in str(excinfo.value)


class _Page:
    def __init__(self, html: str, *, explode: bool = False) -> None:
        self._html = html
        self._explode = explode
        self.visited: list[str] = []
        # `is_logged_out` reads the URL and fails closed when it cannot, so the
        # fake has to carry one or every case would look logged out.
        self.url = "about:blank"

    async def goto(self, url: str, **kwargs) -> None:
        if self._explode:
            raise RuntimeError("network down")
        self.visited.append(url)
        self.url = url

    async def content(self) -> str:
        return self._html

    def is_closed(self) -> bool:
        return False


#: The enquiry page the scraper actually needs: it carries the form.
ENQUIRY_PAGE = (
    '<html><body><a href="signOut.do">Sign out</a>'
    '<form><input name="StudentNumber"><input name="send"></form></body></html>'
)
#: A sign-in page. Note it also carries `signOut.do` in the shared navigation —
#: which is exactly why a navigation marker cannot decide this.
SIGN_IN = (
    '<html><body><a href="signOut.do">Sign out</a>'
    '<a href="staffLogin.do?ex=preLogin">Sign in</a>'
    '<input name="loginfmt"></body></html>'
)
#: Authenticated-looking, but WITHOUT the enquiry form. A session that cannot load
#: the form cannot scrape, so this is not live for our purposes.
WELCOME_ONLY = '<html><body><a href="staffWelcomePage.do">Home</a></body></html>'


@pytest.mark.parametrize(
    ("html", "live"),
    [
        (ENQUIRY_PAGE, True),
        (SIGN_IN, False),
        (WELCOME_ONLY, False),
        ("", False),
    ],
)
def test_liveness_is_decided_by_the_enquiry_form(html: str, live: bool):
    """Not by a navigation marker. `signOut.do` appears on sign-in pages too, and
    the question that matters is whether the scrape's next page will load."""
    page = _Page(html)
    assert asyncio.run(page_shows_live_session(page)) is live


def test_liveness_is_checked_against_the_enquiry_page_not_the_sign_in_page():
    """THE bug that threw away a real operator sign-in: the check navigated to
    `PORTAL_LOGIN_URL`, which renders a sign-in form whether or not you hold a
    session, so a freshly minted session always reported "not live"."""
    from django.conf import settings

    page = _Page(ENQUIRY_PAGE)
    assert asyncio.run(page_shows_live_session(page)) is True
    assert page.visited == [settings.STUDENT_PLAN_URL]
    assert settings.PORTAL_LOGIN_URL not in page.visited


def test_a_network_failure_is_not_a_live_session():
    """`page_shows_live_session` swallows exceptions, so it must fail CLOSED —
    returning True on an error would start a scrape with no session at all."""
    assert asyncio.run(page_shows_live_session(_Page("", explode=True))) is False


def test_session_is_live_closes_the_page_it_opened():
    """The scrape holds one browser for a whole roster; a page leaked on every
    liveness check is a page leaked per recovery attempt."""
    closed: list[bool] = []

    class Page(_Page):
        async def close(self) -> None:
            closed.append(True)

    class Context:
        async def new_page(self):
            return Page(ENQUIRY_PAGE)

    assert asyncio.run(portal_session.session_is_live(Context())) is True
    assert closed == [True]

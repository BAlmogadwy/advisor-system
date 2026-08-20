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

    async def goto(self, url: str, **kwargs) -> None:
        if self._explode:
            raise RuntimeError("network down")
        self.visited.append(url)

    async def content(self) -> str:
        return self._html

    def is_closed(self) -> bool:
        return False


AUTHENTICATED = '<html><body><a href="staffWelcomePage.do">Home</a></body></html>'
SIGN_IN = '<html><body><a href="staffLogin.do?ex=preLogin">Sign in</a></body></html>'
#: Carries the success marker AND is a service page — the shape the two-sided gate
#: exists for.
SERVICE_PAGE = (
    "<html><head><title>نظام الخدمات "
    "الالكترونية</title></head>"
    '<body><a href="signOut.do">Sign out</a></body></html>'
)


@pytest.mark.parametrize(
    ("html", "live"),
    [
        (AUTHENTICATED, True),
        (SIGN_IN, False),
        (SERVICE_PAGE, False),
        ("", False),
    ],
)
def test_liveness_uses_the_two_sided_gate(html: str, live: bool):
    page = _Page(html)
    assert asyncio.run(page_shows_live_session(page)) is live


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
            return Page(AUTHENTICATED)

    assert asyncio.run(portal_session.session_is_live(Context())) is True
    assert closed == [True]

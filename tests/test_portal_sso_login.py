from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.services import portal_scraper


@pytest.mark.parametrize(
    ("checker", "url"),
    [
        (
            portal_scraper._is_microsoft_login_url,
            "http://login.microsoftonline.com/tenant/oauth2/v2.0/authorize",
        ),
        (
            portal_scraper._is_microsoft_login_url,
            "https://login.microsoftonline.com:444/tenant/oauth2/v2.0/authorize",
        ),
        (portal_scraper._is_taibah_adfs_url, "http://tufs.taibahu.edu.sa/adfs/ls/"),
        (portal_scraper._is_taibah_adfs_url, "https://tufs.taibahu.edu.sa:444/adfs/ls/"),
        (
            portal_scraper._is_portal_url,
            "http://eas.taibahu.edu.sa/TaibahReg/staffLogin.do",
        ),
        (
            portal_scraper._is_portal_url,
            "https://eas.taibahu.edu.sa:444/TaibahReg/staffLogin.do",
        ),
    ],
)
def test_authentication_hosts_require_https_on_the_default_port(
    checker: Callable[[str], bool],
    url: str,
) -> None:
    assert checker(url) is False


class _FakeLocator:
    def __init__(self, page: _FakeSsoPage, selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> _FakeLocator:
        return self

    async def count(self) -> int:
        return int(self.page.is_selector_visible(self.selector))

    async def is_visible(self) -> bool:
        return self.page.is_selector_visible(self.selector)

    async def wait_for(self, *, state: str, timeout: int) -> None:
        assert state == "visible"
        assert timeout >= 1000
        if not self.page.is_selector_visible(self.selector):
            raise TimeoutError(self.selector)

    async def click(self) -> None:
        self.page.clicks.append(self.selector)
        self.page.advance_after_click(self.selector)

    async def fill(self, value: str) -> None:
        self.page.fills.append((self.selector, value))


class _FakeSsoPage:
    def __init__(self, outcome: str = "success", *, entry_stage: str = "username") -> None:
        self.outcome = outcome
        self.entry_stage = entry_stage
        self.stage = "blank"
        self.url = "about:blank"
        self.clicks: list[str] = []
        self.fills: list[tuple[str, str]] = []

    def is_closed(self) -> bool:
        return False

    async def goto(
        self,
        url: str,
        *,
        wait_until: str,
        timeout: int,
        referer: str | None = None,
    ) -> None:
        assert wait_until == "domcontentloaded"
        assert timeout == 30000
        assert referer is None
        self.url = url
        self.stage = "portal"

    async def content(self) -> str:
        if self.stage == "success":
            return '<html><a href="signOut.do">Logout</a></html>'
        if self.stage == "portal":
            return (
                '<html><a href="staffLogin.do?ex=authLogin&amp;key=fresh-dynamic-key">'
                "الدخول الموحد</a></html>"
            )
        return "<html><body>Microsoft sign-in</body></html>"

    def locator(self, selector: str, **kwargs: Any) -> _FakeLocator:
        assert not kwargs
        return _FakeLocator(self, selector)

    def is_selector_visible(self, selector: str) -> bool:
        visible_by_stage = {
            "portal": {portal_scraper._PORTAL_SSO_LINK_SELECTOR},
            "username": {
                portal_scraper._MICROSOFT_USERNAME_SELECTOR,
                portal_scraper._MICROSOFT_SUBMIT_SELECTOR,
            },
            "password": {
                portal_scraper._MICROSOFT_PASSWORD_SELECTOR,
                portal_scraper._MICROSOFT_SUBMIT_SELECTOR,
            },
            "adfs-choice": {portal_scraper._TAIBAH_ADFS_ACTIVE_DIRECTORY_SELECTOR},
            "adfs": {
                portal_scraper._TAIBAH_ADFS_USERNAME_SELECTOR,
                portal_scraper._TAIBAH_ADFS_PASSWORD_SELECTOR,
                portal_scraper._TAIBAH_ADFS_SUBMIT_SELECTOR,
            },
            "account": {portal_scraper._MICROSOFT_OTHER_ACCOUNT_SELECTOR},
            "kmsi": {
                portal_scraper._MICROSOFT_KMSI_SELECTOR,
                portal_scraper._MICROSOFT_KMSI_NO_SELECTOR,
            },
            "invalid": {portal_scraper._MICROSOFT_CREDENTIAL_ERROR_SELECTOR},
            "adfs-invalid": {portal_scraper._TAIBAH_ADFS_CREDENTIAL_ERROR_SELECTOR},
            "mfa": {portal_scraper._MICROSOFT_INTERACTIVE_SELECTOR},
            "policy": {portal_scraper._MICROSOFT_POLICY_ERROR_SELECTOR},
        }
        return selector in visible_by_stage.get(self.stage, set())

    def advance_after_click(self, selector: str) -> None:
        if selector == portal_scraper._PORTAL_SSO_LINK_SELECTOR:
            if self.outcome == "unexpected-host":
                self.url = "https://unexpected.example/sign-in"
                self.stage = "unexpected"
                return
            self.url = "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize"
            self.stage = self.entry_stage
            return
        if selector == portal_scraper._MICROSOFT_OTHER_ACCOUNT_SELECTOR:
            self.stage = "username"
            return
        if selector == portal_scraper._MICROSOFT_SUBMIT_SELECTOR:
            if self.stage == "username":
                if self.outcome in {"microsoft-invalid", "mfa", "policy"}:
                    self.stage = self.outcome.removeprefix("microsoft-")
                    return
                self.url = "https://tufs.taibahu.edu.sa/adfs/ls/?opaque=federation-state"
                self.stage = "adfs-choice"
                return
            if self.stage == "password":
                self.stage = self.outcome
                if self.outcome == "success":
                    self.url = "https://eas.taibahu.edu.sa/TaibahReg/staffWelcomePage.do?ex=home"
                return
        if selector == portal_scraper._TAIBAH_ADFS_ACTIVE_DIRECTORY_SELECTOR:
            self.url = (
                "https://tufs.taibahu.edu.sa/adfs/ls/"
                "?opaque=federation-state&RedirectToIdentityProvider=AD+AUTHORITY"
            )
            self.stage = "adfs"
            return
        if selector == portal_scraper._TAIBAH_ADFS_SUBMIT_SELECTOR:
            self.stage = self.outcome
            if self.outcome == "success":
                self.url = "https://eas.taibahu.edu.sa/TaibahReg/staffWelcomePage.do?ex=home"
            elif self.outcome == "kmsi":
                self.url = "https://login.microsoftonline.com/common/login"
            return
        if selector == portal_scraper._MICROSOFT_KMSI_NO_SELECTOR:
            self.stage = "success"
            self.url = "https://eas.taibahu.edu.sa/TaibahReg/staffWelcomePage.do?ex=home"
            return
        raise AssertionError(f"Unexpected click in stage {self.stage}: {selector}")


def test_sso_login_follows_dynamic_link_and_completes_email_password() -> None:
    page = _FakeSsoPage()

    asyncio.run(
        portal_scraper.authenticate_portal_page(
            page,  # type: ignore[arg-type]
            "staff.member@taibahu.edu.sa",
            "test-password",  # noqa: S106
            timeout_ms=5000,
        )
    )

    assert page.clicks == [
        portal_scraper._PORTAL_SSO_LINK_SELECTOR,
        portal_scraper._MICROSOFT_SUBMIT_SELECTOR,
        portal_scraper._TAIBAH_ADFS_ACTIVE_DIRECTORY_SELECTOR,
        portal_scraper._TAIBAH_ADFS_SUBMIT_SELECTOR,
    ]
    assert page.fills == [
        (portal_scraper._MICROSOFT_USERNAME_SELECTOR, "staff.member@taibahu.edu.sa"),
        (portal_scraper._TAIBAH_ADFS_USERNAME_SELECTOR, "staff.member@taibahu.edu.sa"),
        (portal_scraper._TAIBAH_ADFS_PASSWORD_SELECTOR, "test-password"),
    ]


def test_sso_login_selects_other_account_and_declines_persistent_login() -> None:
    page = _FakeSsoPage(outcome="kmsi", entry_stage="account")

    asyncio.run(
        portal_scraper.authenticate_portal_page(
            page,  # type: ignore[arg-type]
            "staff.member@taibahu.edu.sa",
            "test-password",  # noqa: S106
            timeout_ms=5000,
        )
    )

    assert portal_scraper._MICROSOFT_OTHER_ACCOUNT_SELECTOR in page.clicks
    assert portal_scraper._TAIBAH_ADFS_ACTIVE_DIRECTORY_SELECTOR in page.clicks
    assert page.clicks[-1] == portal_scraper._MICROSOFT_KMSI_NO_SELECTOR
    assert page.stage == "success"


def test_sso_login_supports_microsoft_password_reauthentication() -> None:
    page = _FakeSsoPage(entry_stage="password")

    asyncio.run(
        portal_scraper.authenticate_portal_page(
            page,  # type: ignore[arg-type]
            "staff.member@taibahu.edu.sa",
            "test-password",  # noqa: S106
            timeout_ms=5000,
        )
    )

    assert page.fills == [
        (portal_scraper._MICROSOFT_PASSWORD_SELECTOR, "test-password"),
    ]


@pytest.mark.parametrize(
    ("outcome", "exception_type", "message"),
    [
        (
            "adfs-invalid",
            portal_scraper.PortalAuthenticationError,
            "federated sign-in rejected.*username or password",
        ),
        (
            "microsoft-invalid",
            portal_scraper.PortalAuthenticationError,
            "Microsoft SSO rejected.*username or password",
        ),
        (
            "mfa",
            portal_scraper.PortalInteractiveAuthenticationRequired,
            "requires MFA, CAPTCHA",
        ),
        ("policy", portal_scraper.PortalAuthenticationError, "tenant policy"),
        ("unexpected-host", portal_scraper.PortalAuthenticationError, "unexpected sign-in host"),
    ],
)
def test_sso_login_fails_closed_for_unsupported_states(
    outcome: str,
    exception_type: type[Exception],
    message: str,
) -> None:
    page = _FakeSsoPage(outcome=outcome)

    with pytest.raises(exception_type, match=message):
        asyncio.run(
            portal_scraper.authenticate_portal_page(
                page,  # type: ignore[arg-type]
                "staff.member@taibahu.edu.sa",
                "test-password",  # noqa: S106
                timeout_ms=5000,
            )
        )


def test_initial_sso_failure_closes_browser_and_playwright(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Page:
        pass

    class Context:
        async def new_page(self) -> Page:
            return Page()

    class Browser:
        def __init__(self) -> None:
            self.closed = False

        async def new_context(self) -> Context:
            return Context()

        async def close(self) -> None:
            self.closed = True

    class Chromium:
        def __init__(self, browser: Browser) -> None:
            self.browser = browser

        async def launch(self, *, headless: bool) -> Browser:
            assert headless is True
            return self.browser

    class Playwright:
        def __init__(self, browser: Browser) -> None:
            self.chromium = Chromium(browser)
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    class Starter:
        def __init__(self, playwright: Playwright) -> None:
            self.playwright = playwright

        async def start(self) -> Playwright:
            return self.playwright

    browser = Browser()
    playwright = Playwright(browser)
    authenticate = AsyncMock(side_effect=portal_scraper.PortalAuthenticationError("failed"))
    monkeypatch.setattr(portal_scraper, "async_playwright", lambda: Starter(playwright))
    monkeypatch.setattr(portal_scraper, "authenticate_portal_page", authenticate)

    with pytest.raises(portal_scraper.PortalAuthenticationError, match="failed"):
        asyncio.run(portal_scraper.login_to_portal("staff@taibahu.edu.sa", "irrelevant"))

    assert browser.closed is True
    assert playwright.stopped is True

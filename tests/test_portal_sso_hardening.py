"""What an adversarial review found in the Entra SSO login, pinned.

Each test here fails on the code as it stood before the hardening, and each names
the harm rather than the mechanism — because the harms are not "a test goes red",
they are a locked staff account, a credential on disk, and a student silently
scraped as having an empty study plan.
"""

from __future__ import annotations

import asyncio

import pytest

from core.services import portal_scraper
from tests.test_portal_sso_login import _FakeSsoPage

USERNAME = "staff.member@taibahu.edu.sa"
PASSWORD = "test-password"  # noqa: S105


# ---------------------------------------------------------------------------
# The selector constants themselves. `_FakeSsoPage` keys its stage map on these
# very objects, so every assertion that uses it is a tautology: a constant can be
# replaced with a string matching nothing real and the suite stays green. These
# assert the literal text, which is the only part Microsoft's DOM has to agree
# with.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("constant", "must_contain"),
    [
        ("_MICROSOFT_USERNAME_SELECTOR", ("#i0116", 'input[name="loginfmt"]')),
        ("_MICROSOFT_PASSWORD_SELECTOR", ("#i0118", 'input[name="passwd"]')),
        ("_MICROSOFT_SUBMIT_SELECTOR", ("#idSIButton9",)),
        ("_MICROSOFT_KMSI_NO_SELECTOR", ("#idBtn_Back",)),
        ("_MICROSOFT_CREDENTIAL_ERROR_SELECTOR", ("#usernameError", "#passwordError")),
        ("_TAIBAH_ADFS_USERNAME_SELECTOR", ("#userNameInput",)),
        ("_TAIBAH_ADFS_PASSWORD_SELECTOR", ("#passwordInput",)),
        ("_TAIBAH_ADFS_SUBMIT_SELECTOR", ("#submitButton",)),
        ("_PORTAL_SSO_LINK_SELECTOR", ("staffLogin.do?ex=authLogin",)),
    ],
)
def test_selector_constants_still_name_the_real_controls(constant: str, must_contain):
    value = getattr(portal_scraper, constant)
    for fragment in must_contain:
        assert fragment in value, f"{constant} no longer targets {fragment}"


@pytest.mark.parametrize(
    "constant",
    ["_MICROSOFT_USERNAME_SELECTOR", "_MICROSOFT_PASSWORD_SELECTOR"],
)
def test_the_credential_selectors_keep_their_offscreen_guards(constant: str):
    """Microsoft ships both inputs in the DOM of whichever step is showing, moving
    the inactive one off screen. `:visible` does NOT exclude an off-screen or
    zero-opacity input — only `display:none` — so these `:not()` guards are the
    only thing that makes the selector mean "the field the user is looking at".

    They are no longer the ONLY protection (see the ordering test below), but
    losing them silently would put the whole weight on that ordering.
    """
    value = getattr(portal_scraper, constant)
    for guard in (":not(.moveOffScreen)", ':not([aria-hidden="true"])', ':not([tabindex="-1"])'):
        assert guard in value, f"{constant} lost {guard}"


def test_the_adfs_credential_selectors_carry_the_same_guards():
    """ADFS serves home-realm discovery and the credential form from one
    `#authArea`, toggling between them. A theme that hides the inactive half by
    moving it off screen rather than with `display:none` would expose the same
    hazard as the Microsoft page."""
    for constant in ("_TAIBAH_ADFS_USERNAME_SELECTOR", "_TAIBAH_ADFS_PASSWORD_SELECTOR"):
        value = getattr(portal_scraper, constant)
        assert ':not([aria-hidden="true"])' in value, constant
        assert ":not(.moveOffScreen)" in value, constant


# ---------------------------------------------------------------------------
# The account-lockout defect.
# ---------------------------------------------------------------------------


class _PageWithBothFieldsVisible(_FakeSsoPage):
    """Microsoft's email step, with the password input reported visible too.

    This is what the live page becomes the day the `moveOffScreen` class is
    renamed or tenant-branded away — a real possibility, and the reason the
    ordering rule cannot rest on a CSS class.
    """

    def is_selector_visible(self, selector: str) -> bool:
        if self.stage == "username" and selector == portal_scraper._MICROSOFT_PASSWORD_SELECTOR:
            return True
        return super().is_selector_visible(selector)


def test_the_password_is_never_typed_at_the_email_step():
    """THE lockout defect. The password branch was evaluated before the username
    branch with no cross-check, so a password selector that matched on the email
    page made the scraper submit the real password with `loginfmt` empty — every
    scrape and every session recovery, each one a failed sign-in against Entra
    smart lockout and ADFS extranet lockout.
    """
    page = _PageWithBothFieldsVisible()

    asyncio.run(
        portal_scraper.authenticate_portal_page(
            page,  # type: ignore[arg-type]
            USERNAME,
            PASSWORD,
            timeout_ms=5000,
        )
    )

    first_filled_value = page.fills[0][1]
    assert first_filled_value == USERNAME, (
        f"the first thing submitted to Microsoft was {'the PASSWORD' if first_filled_value == PASSWORD else first_filled_value!r}"
    )
    username_fills = [
        v for sel, v in page.fills if sel == portal_scraper._MICROSOFT_USERNAME_SELECTOR
    ]
    assert username_fills == [USERNAME], "the username field must be filled exactly once"


class _PickerAfterAdfs(_FakeSsoPage):
    """Entra shows the account picker AFTER the ADFS round-trip.

    It does this when the federated assertion does not resolve to a single
    account, and it is the normal first page whenever the shared BrowserContext
    still carries an ESTS cookie from an earlier `_force_relogin`. This is the
    shape that exposed the latch bug: the SECOND pass through ADFS needs the
    provider selected again, and `adfs_provider_selected` was still True.
    """

    def __init__(self) -> None:
        super().__init__(entry_stage="username")
        self._adfs_rounds = 0

    def advance_after_click(self, selector: str) -> None:
        if selector == portal_scraper._TAIBAH_ADFS_SUBMIT_SELECTOR and self._adfs_rounds == 0:
            self._adfs_rounds += 1
            self.url = "https://login.microsoftonline.com/common/oauth2/authorize"
            self.stage = "account"
            return
        super().advance_after_click(selector)


def test_use_another_account_restarts_the_whole_credential_sequence():
    """Clicking the picker restarts sign-in from scratch, so every credential latch
    has to come off with it. Resetting only `username_submitted` left the restarted
    sequence dead-ended at the next step: the second ADFS pass never re-selected the
    identity provider, so the loop polled doing nothing until the stall timer and
    reported an interactive sign-in requirement that was never there."""
    page = _PickerAfterAdfs()

    asyncio.run(
        portal_scraper.authenticate_portal_page(
            page,  # type: ignore[arg-type]
            USERNAME,
            PASSWORD,
            timeout_ms=6000,
        )
    )

    assert page.stage == "success"
    assert page._adfs_rounds == 1, "the picker must have been reached via ADFS"


# ---------------------------------------------------------------------------
# The stall timer.
# ---------------------------------------------------------------------------


def test_a_redirect_in_flight_counts_as_progress(monkeypatch: pytest.MonkeyPatch):
    """Nothing is clicked during the callback chain — ADFS posts back to Microsoft,
    Microsoft to the portal, the portal builds the session. With no branch
    refreshing the stall clock, a sign-in that had actually SUCCEEDED was abandoned
    and reported as needing an interactive step."""
    monkeypatch.setattr(portal_scraper, "_SSO_STALLED_SECONDS", 0.4)

    page = _FakeSsoPage(entry_stage="password")
    original = page.content

    hops = {"n": 0}

    async def content_with_redirects() -> str:
        # Three redirects, each slower than the stall budget on its own, none of
        # which clicks anything. Only a URL change can keep the clock alive.
        if page.stage == "success" and hops["n"] < 3:
            hops["n"] += 1
            page.url = f"https://eas.taibahu.edu.sa/TaibahReg/hop{hops['n']}.do"
            await asyncio.sleep(0.25)
            return "<html>redirecting</html>"
        return await original()

    page.content = content_with_redirects  # type: ignore[method-assign]

    asyncio.run(
        portal_scraper.authenticate_portal_page(
            page,  # type: ignore[arg-type]
            USERNAME,
            PASSWORD,
            timeout_ms=8000,
        )
    )
    assert hops["n"] == 3


class _StallsAfterTheLink(_FakeSsoPage):
    """Reaches Microsoft and then shows nothing this scraper can act on — the
    shape of every interactive state not in `_MICROSOFT_INTERACTIVE_SELECTOR`:
    consent, terms of use, MFA registration, a device-compliance interrupt."""

    def is_selector_visible(self, selector: str) -> bool:
        if self.stage == "portal":
            return super().is_selector_visible(selector)
        return False


def test_the_stall_budget_never_exceeds_half_the_configured_timeout():
    """A fixed 30s stall budget silently capped `PORTAL_SSO_TIMEOUT_MS`: an
    operator raising the timeout to survive a slow tenant got no more patience."""
    page = _StallsAfterTheLink(entry_stage="username")
    started = asyncio.get_event_loop_policy().new_event_loop()
    try:
        import time as _time

        began = _time.monotonic()
        with pytest.raises(portal_scraper.PortalInteractiveAuthenticationRequired):
            started.run_until_complete(
                portal_scraper.authenticate_portal_page(
                    page,  # type: ignore[arg-type]
                    USERNAME,
                    PASSWORD,
                    timeout_ms=2000,
                )
            )
        elapsed = _time.monotonic() - began
    finally:
        started.close()
    # Half of 2s, not the 30s constant — and well inside the 2s deadline.
    assert elapsed < 1.9, f"stall budget did not scale with the timeout ({elapsed:.2f}s)"


def test_a_stall_is_reported_as_a_diagnosis_not_a_certainty():
    """`PortalInteractiveAuthenticationRequired` from a stall is inferred from the
    ABSENCE of progress, not from seeing an MFA control. The message has to say so,
    or an operator reads 'MFA required' about a tenant that has no MFA."""

    page = _StallsAfterTheLink(entry_stage="username")
    with pytest.raises(portal_scraper.PortalInteractiveAuthenticationRequired) as excinfo:
        asyncio.run(
            portal_scraper.authenticate_portal_page(
                page,  # type: ignore[arg-type]
                USERNAME,
                PASSWORD,
                timeout_ms=2000,
            )
        )
    message = str(excinfo.value)
    assert "stopped making progress" in message
    assert "can also be" in message, "the message must not assert MFA as a certainty"


# ---------------------------------------------------------------------------
# The success gate and the detectors.
# ---------------------------------------------------------------------------


def test_a_portal_page_that_reads_as_logged_out_is_not_accepted_as_a_session():
    """`is_staff_login_success_html` is a bare substring test for `signOut.do`.
    The portal's shared navigation puts that link on pages that are not a session,
    so accepting on it alone would return success from a sign-in page."""
    html = (
        '<html><body><a href="signOut.do">Sign out</a>'
        '<a href="staffLogin.do?ex=preLogin">Sign in</a>'
        '<input name="loginfmt">'
        "</body></html>"
    )
    assert portal_scraper.is_staff_login_success_html(html) is True
    assert portal_scraper.is_logged_out_html(html) is False, (
        "the authenticated override makes these agree; if it ever stops, the "
        "two-sided gate in authenticate_portal_page is what keeps the login honest"
    )


def test_an_authenticated_study_plan_is_not_mistaken_for_a_sign_in_page():
    """The portal's own navigation carries `staffLogin.do?ex=preLogin` on
    authenticated pages. The parser's detector gained that marker without the
    authenticated override, so a real study plan parsed as EMPTY — a student
    silently scraped as having no courses."""
    from core.services.student_parser import parse_study_plan

    authenticated_plan = (
        "<html><body>"
        '<a href="staffLogin.do?ex=preLogin">Sign in</a>'
        '<a href="signOut.do">Sign out</a>'
        '<table dir="rtl"><tr><th>Level 1</th></tr>'
        "<tr><td>A</td><td>90</td><td>3</td><td>101</td><td>CS</td><td>Intro</td></tr>"
        "</table></body></html>"
    )
    assert parse_study_plan(authenticated_plan), "an authenticated plan parsed as empty"


# ---------------------------------------------------------------------------
# Secrets on disk.
# ---------------------------------------------------------------------------


class _PageAt:
    def __init__(self, url: str, html: str) -> None:
        self.url = url
        self._html = html

    async def content(self) -> str:
        return self._html

    def is_closed(self) -> bool:
        return False


@pytest.mark.parametrize(
    "url",
    [
        "https://login.microsoftonline.com/common/login",
        "https://tufs.taibahu.edu.sa/adfs/ls/",
        "https://unexpected.example/sign-in",
    ],
)
def test_an_identity_provider_page_is_never_written_to_a_debug_file(url: str):
    """`_save_debug` dumps whatever the worker was parked on. On these pages the
    markup holds live secrets: Entra's `$Config` carries the flow token, canary and
    OAuth context, and the ADFS interstitial carries a signed SAML assertion in a
    hidden `wresult` field. `data/debug_failures/` is not a secret store."""
    from core.management.commands.scrape_students import _redactable_page_content

    secret_markup = (
        "<html><body><input name='wresult' value='SAML-ASSERTION-SECRET'>"
        "<script>$Config={sFT:'FLOW-TOKEN-SECRET',canary:'CANARY-SECRET'};</script>"
        "</body></html>"
    )
    result = asyncio.run(_redactable_page_content(_PageAt(url, secret_markup)))

    assert "SAML-ASSERTION-SECRET" not in result
    assert "FLOW-TOKEN-SECRET" not in result
    assert "CANARY-SECRET" not in result
    assert "REDACTED_NON_PORTAL_PAGE" in result


def test_a_portal_page_is_still_saved_in_full():
    """The redaction must not blind the operator to the failures it exists for."""
    from core.management.commands.scrape_students import _redactable_page_content

    html = "<html><body>the student page that actually failed</body></html>"
    result = asyncio.run(
        _redactable_page_content(
            _PageAt("https://eas.taibahu.edu.sa/TaibahReg/studentSchedualEnquiry.do", html)
        )
    )
    assert result == html


class _AmbiguousPortalPage(_FakeSsoPage):
    """Ends on a portal page that carries a sign-out link AND is a service page.

    `is_staff_login_success_html` is a bare substring test, so it says "signed in".
    `is_logged_out_html` recognises the service-page title regardless of that link,
    so it says "signed out". Only a gate that consults BOTH refuses this.
    """

    AMBIGUOUS = (
        "<html><head><title>نظام الخدمات "
        "الالكترونية</title></head>"
        '<body><a href="signOut.do">Sign out</a></body></html>'
    )

    async def content(self) -> str:
        if self.stage == "success":
            return self.AMBIGUOUS
        return await super().content()


def test_a_service_page_carrying_a_sign_out_link_is_not_accepted_as_a_session(
    monkeypatch: pytest.MonkeyPatch,
):
    """The one-sided gate returned SUCCESS here, so the scraper would go on to
    parse a service page as student data for every student in the run."""
    monkeypatch.setattr(portal_scraper, "_SSO_STALLED_SECONDS", 0.3)
    page = _AmbiguousPortalPage(entry_stage="password")

    with pytest.raises(portal_scraper.PortalAuthenticationError):
        asyncio.run(
            portal_scraper.authenticate_portal_page(
                page,  # type: ignore[arg-type]
                USERNAME,
                PASSWORD,
                timeout_ms=2000,
            )
        )


def test_a_service_page_at_entry_does_not_skip_authentication():
    """The gate on the FIRST page is the more dangerous of the two: it decides
    whether to authenticate at all. One-sided, a service page carrying the portal's
    own `signOut.do` nav link returned "already signed in" and the scraper went
    straight to scraping with no session."""

    class _ServicePageAtEntry(_FakeSsoPage):
        async def content(self) -> str:
            if self.stage == "portal":
                return _AmbiguousPortalPage.AMBIGUOUS
            return await super().content()

    page = _ServicePageAtEntry()
    asyncio.run(
        portal_scraper.authenticate_portal_page(
            page,  # type: ignore[arg-type]
            USERNAME,
            PASSWORD,
            timeout_ms=5000,
        )
    )
    # It did NOT return early: a real sign-in was driven to completion.
    assert page.fills, "authentication was skipped for a page that only looked signed in"
    assert page.stage == "success"

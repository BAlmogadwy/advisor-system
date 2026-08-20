"""Async Playwright utilities for scraping the university portal.

Provides login, navigation to study-plan / timetable pages, and
session-health helpers.  All public functions are async and expect a
Playwright ``Page`` or ``BrowserContext``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlsplit

from django.conf import settings

try:
    from playwright.async_api import (
        Browser,
        BrowserContext,
        Locator,
        Page,
        Playwright,
        async_playwright,
    )
    from playwright.async_api import (
        TimeoutError as PlaywrightTimeoutError,
    )

    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

logger = logging.getLogger(__name__)

_PORTAL_HOST = "eas.taibahu.edu.sa"
_MICROSOFT_LOGIN_HOST = "login.microsoftonline.com"
_TAIBAH_ADFS_HOST = "tufs.taibahu.edu.sa"
_PORTAL_SSO_LINK_SELECTOR = 'a[href*="staffLogin.do?ex=authLogin"]:visible'
_MICROSOFT_USERNAME_SELECTOR = (
    '#i0116:not([aria-hidden="true"]):not([tabindex="-1"]):not(.moveOffScreen):visible, '
    'input[name="loginfmt"]:not([aria-hidden="true"]):not([tabindex="-1"]):not('
    ".moveOffScreen):visible"
)
_MICROSOFT_PASSWORD_SELECTOR = (
    '#i0118:not([aria-hidden="true"]):not([tabindex="-1"]):not(.moveOffScreen):visible, '
    'input[name="passwd"]:not([aria-hidden="true"]):not([tabindex="-1"]):not('
    ".moveOffScreen):visible"
)
_MICROSOFT_SUBMIT_SELECTOR = "#idSIButton9:visible"
_MICROSOFT_OTHER_ACCOUNT_SELECTOR = '#otherTile:visible, [data-test-id="otherTile"]:visible'
_MICROSOFT_KMSI_SELECTOR = (
    '#KmsiDescription:visible, #KmsiCheckboxField:visible, [data-bind*="Kmsi"]:visible'
)
_MICROSOFT_KMSI_NO_SELECTOR = "#idBtn_Back:visible"
_MICROSOFT_CREDENTIAL_ERROR_SELECTOR = "#usernameError:visible, #passwordError:visible"
_MICROSOFT_POLICY_ERROR_SELECTOR = "#service_exception_message:visible, #idTD_Error:visible"
_MICROSOFT_INTERACTIVE_SELECTOR = ", ".join(
    (
        "#idDiv_SAOTCS_Proofs:visible",
        "#idTxtBx_SAOTCC_OTC:visible",
        'input[name="otc"]:visible',
        '[data-bind*="PhoneAppNotification"]:visible',
        "#idRichContext_DisplaySign:visible",
        "#wlspispSolutionElement:visible",
    )
)
_TAIBAH_ADFS_USERNAME_SELECTOR = '#userNameInput:visible, input[name="UserName"]:visible'
_TAIBAH_ADFS_PASSWORD_SELECTOR = '#passwordInput:visible, input[name="Password"]:visible'
_TAIBAH_ADFS_ACTIVE_DIRECTORY_SELECTOR = (
    '#bySelection .idp[role="button"][onclick*="AD AUTHORITY"]:visible'
)
_TAIBAH_ADFS_SUBMIT_SELECTOR = "#submitButton:visible"
_TAIBAH_ADFS_CREDENTIAL_ERROR_SELECTOR = "#error:visible, #errorText:visible"
_SSO_POLL_SECONDS = 0.25
_SSO_STALLED_SECONDS = 30.0


class PortalAuthenticationError(RuntimeError):
    """The Taibah/Microsoft sign-in did not establish a portal session."""


class PortalInteractiveAuthenticationRequired(PortalAuthenticationError):
    """Microsoft requires an interactive step that an unattended scrape cannot perform."""


# ------------------------------------------------------------------
# Safe HTML utilities
# ------------------------------------------------------------------


async def safe_page_content(page: Page, retries: int = 2) -> str:
    for _ in range(retries):
        try:
            if getattr(page, "is_closed", lambda: False)():
                return "<PAGE_CLOSED>"
            content: str = await page.content()
            return content
        except Exception:
            await asyncio.sleep(0.5)
    return "<FAILED_TO_GET_PAGE_CONTENT>"


# ------------------------------------------------------------------
# Logout detectors
# ------------------------------------------------------------------


async def is_logged_out(page: Page) -> bool:
    try:
        if getattr(page, "is_closed", lambda: False)():
            return True
        try:
            current_url = str(page.url or "")
            lowered_url = current_url.casefold()
            if current_url == "about:blank":
                return True
            if "teachers_login.jsp" in lowered_url or "student_login.jsp" in lowered_url:
                return True
            if _is_microsoft_login_url(current_url):
                return True
            if _is_taibah_adfs_url(current_url):
                return True
            if "stafflogin.do?ex=prelogin" in lowered_url:
                return True
            if "stafflogin.do?ex=authlogin" in lowered_url:
                return True
        except Exception:
            return True
        return False
    except Exception:
        return True


def is_logged_out_html(html: str) -> bool:
    if not html:
        return False
    lowered = html.casefold()
    authenticated = "staffwelcomepage.do" in lowered or "signout.do" in lowered
    if "<title>نظام الخدمات الالكترونية</title>" in html:
        return True
    if (
        "teachers_login.jsp" in html
        and "student_login.jsp" in html
        and "services4GraduatedStudent.do" in html
    ):
        return True
    if not authenticated and any(
        marker in lowered
        for marker in (
            "stafflogin.do?ex=prelogin",
            "stafflogin.do?ex=authlogin",
            'name="loginfmt"',
            'id="i0116"',
            'id="usernameinput"',
            'id="passwordinput"',
        )
    ):
        return True
    if any(
        marker in lowered
        for marker in (
            "<title>service unavailable",
            "<title>internal server error",
            "<title>http status 500",
            "<title>http status 503",
            "<title>error 500",
            "<title>error 503",
        )
    ):
        return True
    return False


def is_staff_login_success_html(html: str) -> bool:
    if not html:
        return False
    # Public/login pages contain much of the same Arabic navigation text as the
    # staff landing page. These authenticated links are the reliable boundary.
    return "staffWelcomePage.do" in html or "signOut.do" in html


def _hostname(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").casefold()
    except ValueError:
        return ""


def _is_trusted_https_url(url: str, allowed_hosts: set[str]) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        return (
            parsed.scheme.casefold() == "https"
            and host in allowed_hosts
            and parsed.port in {None, 443}
        )
    except ValueError:
        return False


def _is_microsoft_login_url(url: str) -> bool:
    return _is_trusted_https_url(url, {_MICROSOFT_LOGIN_HOST})


def _is_taibah_adfs_url(url: str) -> bool:
    return _is_trusted_https_url(url, {_TAIBAH_ADFS_HOST})


def _is_portal_url(url: str) -> bool:
    configured_host = _hostname(str(getattr(settings, "PORTAL_LOGIN_URL", "")))
    allowed_hosts = {_PORTAL_HOST}
    if configured_host:
        allowed_hosts.add(configured_host)
    return _is_trusted_https_url(url, allowed_hosts)


# ------------------------------------------------------------------
# Internal utilities
# ------------------------------------------------------------------


def _mono_ms() -> int:
    return int(time.monotonic() * 1000)


async def _safe_wait_network(page: Page, timeout_ms: int = 20000) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:
        logger.debug("domcontentloaded wait timed out", exc_info=True)
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        logger.debug("networkidle wait timed out", exc_info=True)


async def _safe_goto(
    page: Page,
    url: str,
    timeout_ms: int = 30000,
    *,
    referer: str | None = None,
) -> None:
    if referer:
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=timeout_ms,
            referer=referer,
        )
        return
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)


async def _wait_for_stable_count(
    locator: Locator,
    *,
    min_count: int = 1,
    stable_rounds: int = 3,
    poll_ms: int = 350,
    timeout_ms: int = 30000,
) -> int:
    deadline = _mono_ms() + timeout_ms
    stable = 0
    last = -1

    while _mono_ms() < deadline:
        try:
            cnt: int = await locator.count()
        except Exception:
            cnt = 0

        if cnt >= min_count and cnt == last:
            stable += 1
            if stable >= stable_rounds:
                return int(cnt)
        else:
            stable = 0
            last = cnt

        await asyncio.sleep(poll_ms / 1000.0)

    raise PlaywrightTimeoutError(
        f"Stable count not reached (min_count={min_count}) within {timeout_ms}ms"
    )


async def _wait_for_stable_rowcount(
    page: Page,
    table_locator: Locator,
    *,
    min_rows: int = 2,
    timeout_ms: int = 30000,
) -> int:
    deadline = _mono_ms() + timeout_ms
    last = -1
    stable = 0

    while _mono_ms() < deadline:
        try:
            cnt: int = await table_locator.locator("tr").count()
        except Exception:
            cnt = 0

        if cnt >= min_rows and cnt == last:
            stable += 1
            if stable >= 3:
                return int(cnt)
        else:
            stable = 0
            last = cnt

        await asyncio.sleep(0.35)

    raise PlaywrightTimeoutError(
        f"Stable rowcount not reached (min_rows={min_rows}) within {timeout_ms}ms"
    )


async def _wait_for_plan_results(page: Page, timeout_ms: int = 60000) -> None:
    tables = page.locator('table[dir="rtl"]')
    deadline = _mono_ms() + timeout_ms
    last = -1
    stable = 0

    while _mono_ms() < deadline:
        html = await safe_page_content(page, retries=1)
        if is_logged_out_html(html):
            raise RuntimeError("SESSION_LOGGED_OUT_HTML")
        try:
            count = await tables.count()
        except Exception:
            count = 0
        if count >= 1 and count == last:
            stable += 1
            if stable >= 3:
                return
        else:
            stable = 0
            last = count
        await asyncio.sleep(0.35)

    raise PlaywrightTimeoutError(f"Stable study-plan table count not reached within {timeout_ms}ms")


async def _pick_course_table_from_forumline(page: Page) -> Locator:
    # Never fall back to the first forumline table: the portal's navigation
    # menu uses that class and has dozens of stable rows, which previously made
    # an error page look like a completed timetable response.
    return (
        page.locator("table.forumline", has_text="المادة")
        .filter(has_text="شعبة")
        .filter(has_text="قاعة")
        .first
    )


async def _wait_for_timetable_results(page: Page, timeout_ms: int = 60000) -> None:
    deadline = _mono_ms() + timeout_ms
    course_table = await _pick_course_table_from_forumline(page)
    no_timetable_marker = page.get_by_text("رقم الطالب به خطأ", exact=True)

    while _mono_ms() < deadline:
        html = await safe_page_content(page, retries=1)
        if is_logged_out_html(html):
            raise RuntimeError("SESSION_LOGGED_OUT_HTML")
        try:
            if await no_timetable_marker.count() and await no_timetable_marker.first.is_visible():
                return
        except Exception:
            pass
        try:
            if await course_table.count() > 0:
                remaining = max(1000, deadline - _mono_ms())
                await _wait_for_stable_rowcount(
                    page,
                    course_table,
                    min_rows=2,
                    timeout_ms=remaining,
                )
                return
        except PlaywrightTimeoutError:
            raise
        except Exception:
            pass
        await asyncio.sleep(0.35)

    raise PlaywrightTimeoutError(
        "Neither a timetable result nor the portal's no-current-timetable marker appeared "
        f"within {timeout_ms}ms"
    )


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


async def _visible_locator(page: Page, selector: str) -> Locator | None:
    """Return the first visible match without waiting or exposing page text."""
    try:
        locator = page.locator(selector)
        if await locator.count() < 1:
            return None
        candidate = locator.first
        if await candidate.is_visible():
            return candidate
    except Exception:
        # Microsoft authorize URLs contain one-time state and nonce values. Do
        # not log a Playwright exception that could include the current URL.
        pass
    return None


async def _required_visible_locator(page: Page, selector: str) -> Locator:
    locator = await _visible_locator(page, selector)
    if locator is None:
        raise PortalAuthenticationError("Microsoft SSO did not expose the expected action.")
    return locator


async def _click_auth_locator(locator: Locator) -> None:
    try:
        await locator.click()
    except Exception:
        raise PortalAuthenticationError("Microsoft SSO browser interaction failed.") from None


async def _fill_auth_locator(locator: Locator, value: str) -> None:
    try:
        await locator.fill(value)
    except Exception:
        raise PortalAuthenticationError("Microsoft SSO browser interaction failed.") from None


async def authenticate_portal_page(
    page: Page,
    admin_username: str | None = None,
    admin_password: str | None = None,
    *,
    timeout_ms: int | None = None,
) -> None:
    """Establish a Taibah staff session through Microsoft Entra ID.

    The portal creates a short-lived, keyed SSO link on every pre-login page, so
    the link must be clicked rather than reconstructed. Microsoft controls are
    selected by stable IDs/names instead of translated button labels.
    """

    username = str(
        getattr(settings, "PORTAL_ADMIN_USERNAME", "") if admin_username is None else admin_username
    ).strip()
    password = str(
        getattr(settings, "PORTAL_ADMIN_PASSWORD", "") if admin_password is None else admin_password
    )
    if not username or not password:
        raise PortalAuthenticationError(
            "Microsoft SSO credentials are not configured. Set the portal username "
            "to the full university Microsoft account and provide its password."
        )

    resolved_timeout_ms = int(
        timeout_ms if timeout_ms is not None else getattr(settings, "PORTAL_SSO_TIMEOUT_MS", 120000)
    )
    if resolved_timeout_ms < 1000:
        raise PortalAuthenticationError("Microsoft SSO timeout must be at least 1000 ms.")

    await _safe_goto(page, str(getattr(settings, "PORTAL_LOGIN_URL", "")))
    initial_html = await safe_page_content(page)
    if is_staff_login_success_html(initial_html):
        return

    try:
        sso_link = page.locator(_PORTAL_SSO_LINK_SELECTOR).first
        await sso_link.wait_for(state="visible", timeout=min(30000, resolved_timeout_ms))
        await sso_link.click()
    except Exception:
        raise PortalAuthenticationError(
            "The Taibah portal did not provide its Microsoft unified-login link."
        ) from None

    deadline = time.monotonic() + (resolved_timeout_ms / 1000.0)
    last_progress = time.monotonic()
    username_submitted = False
    password_submitted = False
    other_account_selected = False
    kmsi_handled = False
    adfs_provider_selected = False
    adfs_submitted = False

    while time.monotonic() < deadline:
        current_url = str(getattr(page, "url", "") or "")

        if _is_portal_url(current_url):
            html = await safe_page_content(page, retries=1)
            if is_staff_login_success_html(html):
                logger.info("Microsoft SSO established the portal staff session.")
                return
        elif (
            current_url != "about:blank"
            and not _is_microsoft_login_url(current_url)
            and not _is_taibah_adfs_url(current_url)
        ):
            raise PortalAuthenticationError(
                "Microsoft SSO redirected to an unexpected sign-in host."
            )

        if _is_microsoft_login_url(current_url):
            if await _visible_locator(page, _MICROSOFT_CREDENTIAL_ERROR_SELECTOR):
                raise PortalAuthenticationError(
                    "Microsoft SSO rejected the configured username or password."
                )
            if await _visible_locator(page, _MICROSOFT_POLICY_ERROR_SELECTOR):
                raise PortalAuthenticationError(
                    "Microsoft SSO rejected the unattended sign-in because of account "
                    "or university tenant policy."
                )
            if await _visible_locator(page, _MICROSOFT_INTERACTIVE_SELECTOR):
                raise PortalInteractiveAuthenticationRequired(
                    "Microsoft SSO requires MFA, CAPTCHA, or another interactive verification "
                    "step; the unattended scraper cannot complete it."
                )

            kmsi = await _visible_locator(page, _MICROSOFT_KMSI_SELECTOR)
            if kmsi is not None and not kmsi_handled:
                # Choose "No" so the scraper does not create a persistent Microsoft
                # browser login beyond this isolated Playwright context.
                kmsi_no = await _required_visible_locator(page, _MICROSOFT_KMSI_NO_SELECTOR)
                await _click_auth_locator(kmsi_no)
                kmsi_handled = True
                last_progress = time.monotonic()
                await asyncio.sleep(_SSO_POLL_SECONDS)
                continue

            password_field = await _visible_locator(page, _MICROSOFT_PASSWORD_SELECTOR)
            if password_field is not None and not password_submitted:
                await _fill_auth_locator(password_field, password)
                submit = await _required_visible_locator(page, _MICROSOFT_SUBMIT_SELECTOR)
                await _click_auth_locator(submit)
                password_submitted = True
                last_progress = time.monotonic()
                await asyncio.sleep(_SSO_POLL_SECONDS)
                continue

            username_field = await _visible_locator(page, _MICROSOFT_USERNAME_SELECTOR)
            if username_field is not None and not username_submitted:
                await _fill_auth_locator(username_field, username)
                submit = await _required_visible_locator(page, _MICROSOFT_SUBMIT_SELECTOR)
                await _click_auth_locator(submit)
                username_submitted = True
                last_progress = time.monotonic()
                await asyncio.sleep(_SSO_POLL_SECONDS)
                continue

            other_account = await _visible_locator(page, _MICROSOFT_OTHER_ACCOUNT_SELECTOR)
            if other_account is not None and not other_account_selected:
                await _click_auth_locator(other_account)
                other_account_selected = True
                username_submitted = False
                last_progress = time.monotonic()
                await asyncio.sleep(_SSO_POLL_SECONDS)
                continue

        if _is_taibah_adfs_url(current_url):
            if await _visible_locator(page, _TAIBAH_ADFS_CREDENTIAL_ERROR_SELECTOR):
                raise PortalAuthenticationError(
                    "Taibah federated sign-in rejected the configured username or password."
                )

            active_directory = await _visible_locator(page, _TAIBAH_ADFS_ACTIVE_DIRECTORY_SELECTOR)
            if active_directory is not None and not adfs_provider_selected:
                # Taibah also exposes an explicitly non-working experimental
                # provider. Select only the reviewed on-prem Active Directory path.
                await _click_auth_locator(active_directory)
                adfs_provider_selected = True
                last_progress = time.monotonic()
                await asyncio.sleep(_SSO_POLL_SECONDS)
                continue

            adfs_username = await _visible_locator(page, _TAIBAH_ADFS_USERNAME_SELECTOR)
            adfs_password = await _visible_locator(page, _TAIBAH_ADFS_PASSWORD_SELECTOR)
            if adfs_username is not None and adfs_password is not None and not adfs_submitted:
                await _fill_auth_locator(adfs_username, username)
                await _fill_auth_locator(adfs_password, password)
                submit = await _required_visible_locator(page, _TAIBAH_ADFS_SUBMIT_SELECTOR)
                await _click_auth_locator(submit)
                adfs_submitted = True
                last_progress = time.monotonic()
                await asyncio.sleep(_SSO_POLL_SECONDS)
                continue

        if time.monotonic() - last_progress >= _SSO_STALLED_SECONDS:
            if _is_microsoft_login_url(current_url):
                raise PortalInteractiveAuthenticationRequired(
                    "Microsoft SSO is waiting for an unsupported interactive sign-in step."
                )
            if _is_taibah_adfs_url(current_url):
                raise PortalInteractiveAuthenticationRequired(
                    "Taibah federated sign-in is waiting for an unsupported interactive step."
                )
            raise PortalAuthenticationError(
                "The Taibah SSO callback did not establish an authenticated staff session."
            )

        await asyncio.sleep(_SSO_POLL_SECONDS)

    raise PortalAuthenticationError("Microsoft SSO timed out before portal login completed.")


async def login_to_portal(
    admin_username: str | None = None,
    admin_password: str | None = None,
) -> tuple[Playwright, Browser, Page]:
    playwright = await async_playwright().start()
    browser: Browser | None = None
    try:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await authenticate_portal_page(page, admin_username, admin_password)
    except Exception:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                logger.debug("Browser cleanup after SSO failure failed", exc_info=True)
        try:
            await playwright.stop()
        except Exception:
            logger.debug("Playwright cleanup after SSO failure failed", exc_info=True)
        raise

    logger.info("Admin login successful.")
    assert browser is not None
    return playwright, browser, page


async def create_fresh_page_from_context(
    context: BrowserContext,
    entry_url: str | None = None,
    *,
    referer_url: str | None = None,
) -> Page:
    """Open a worker page through the authenticated staff navigation path.

    The portal does not treat its session cookie as sufficient when a protected
    enquiry URL is opened from ``about:blank``.  Supplying the authenticated
    staff page as the HTTP referrer preserves the same navigation provenance as
    clicking the enquiry link in the portal UI.
    """
    entry_url = entry_url or settings.STUDENT_PLAN_URL
    page = await context.new_page()
    await _safe_goto(page, entry_url, referer=referer_url)
    await _safe_wait_network(page, timeout_ms=30000)
    return page


async def navigate_to_student_study_plan(
    page: Page, student_id: str | int, verbose: bool = True
) -> str:
    if await is_logged_out(page):
        raise RuntimeError("SESSION_LOGGED_OUT_HTML")

    await _safe_goto(page, settings.STUDENT_PLAN_URL)
    if is_logged_out_html(await safe_page_content(page)):
        raise RuntimeError("SESSION_LOGGED_OUT_HTML")
    await page.locator('input[name="StudentNumber"]').wait_for(state="visible", timeout=30000)
    await page.fill('input[name="StudentNumber"]', str(student_id))
    await page.click('input[name="send"]')

    await _safe_wait_network(page, timeout_ms=30000)
    await _wait_for_plan_results(page, timeout_ms=60000)

    html = await safe_page_content(page)
    if is_logged_out_html(html):
        raise RuntimeError("SESSION_LOGGED_OUT_HTML")

    return html


async def navigate_to_student_timetable(
    page: Page, student_id: str | int, verbose: bool = True
) -> str:
    if await is_logged_out(page):
        raise RuntimeError("SESSION_LOGGED_OUT_HTML")

    await _safe_goto(page, settings.STUDENT_TIMETABLE_URL)
    if is_logged_out_html(await safe_page_content(page)):
        raise RuntimeError("SESSION_LOGGED_OUT_HTML")
    await page.locator('input[name="StudentNumber"]').wait_for(state="visible", timeout=30000)
    await page.fill('input[name="StudentNumber"]', str(student_id))
    await page.click('input[name="send"]')

    await _safe_wait_network(page, timeout_ms=30000)
    await _wait_for_timetable_results(page, timeout_ms=60000)

    html = await safe_page_content(page)
    if is_logged_out_html(html):
        raise RuntimeError("SESSION_LOGGED_OUT_HTML")

    return html


async def close_browser(playwright: Playwright, browser: Browser) -> None:
    try:
        await browser.close()
    except Exception:
        logger.debug("Browser close failed", exc_info=True)
    try:
        await playwright.stop()
    except Exception:
        logger.debug("Playwright stop failed", exc_info=True)
    logger.info("Browser closed.")

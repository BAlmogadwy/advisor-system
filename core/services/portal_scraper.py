"""Async Playwright utilities for scraping the university portal.

Provides login, navigation to study-plan / timetable pages, and
session-health helpers.  All public functions are async and expect a
Playwright ``Page`` or ``BrowserContext``.
"""

from __future__ import annotations

import asyncio
import logging
import time

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
            if page.url == "about:blank":
                return True
            if "teachers_login.jsp" in page.url or "student_login.jsp" in page.url:
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
    if "<title>نظام الخدمات الالكترونية</title>" in html:
        return True
    if (
        "teachers_login.jsp" in html
        and "student_login.jsp" in html
        and "services4GraduatedStudent.do" in html
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


async def _safe_goto(page: Page, url: str, timeout_ms: int = 30000) -> None:
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


async def login_to_portal(
    admin_username: str | None = None,
    admin_password: str | None = None,
) -> tuple[Playwright, Browser, Page]:
    admin_username = admin_username or settings.PORTAL_ADMIN_USERNAME
    admin_password = admin_password or settings.PORTAL_ADMIN_PASSWORD

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context()
    page = await context.new_page()

    await _safe_goto(page, settings.PORTAL_LOGIN_URL)
    await page.fill('input[name="userName"]', admin_username)
    await page.fill('input[name="password"]', admin_password)
    await page.click('input[name="submit"]')

    await _safe_wait_network(page, timeout_ms=30000)

    html = await safe_page_content(page)
    if is_logged_out_html(html):
        raise RuntimeError("Login failed — still logged out.")
    if not is_staff_login_success_html(html):
        raise RuntimeError("Login failed — success markers not found on staff landing page.")

    logger.info("Admin login successful.")
    return playwright, browser, page


async def create_fresh_page_from_context(
    context: BrowserContext, entry_url: str | None = None
) -> Page:
    entry_url = entry_url or settings.STUDENT_PLAN_URL
    page = await context.new_page()
    await _safe_goto(page, entry_url)
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

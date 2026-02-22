from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from config.settings import (
    PORTAL_LOGIN_URL,
    STUDENT_PLAN_URL,
    STUDENT_TIMETABLE_URL,
    ADMIN_USERNAME,
    ADMIN_PASSWORD
)
import asyncio

# ============================================================
# Safe HTML utilities
# ============================================================

async def safe_page_content(page, retries=2):
    for _ in range(retries):
        try:
            if getattr(page, "is_closed", lambda: False)():
                return "<PAGE_CLOSED>"
            return await page.content()
        except Exception:
            await asyncio.sleep(0.5)
    return "<FAILED_TO_GET_PAGE_CONTENT>"


# ============================================================
# PAGE-LEVEL logout detector (PRE-NAVIGATION ONLY)
# ============================================================

async def is_logged_out(page):
    try:
        if getattr(page, "is_closed", lambda: False)():
            return True

        try:
            if page.url == "about:blank":
                return True
            # NOTE: after successful staff login, portal lands on staffLogin.do,
            # so this URL alone is NOT a logout signal.
            if "teachers_login.jsp" in page.url or "student_login.jsp" in page.url:
                return True
        except Exception:
            return True

        return False
    except Exception:
        return True


# ============================================================
# HTML-LEVEL logout detector (AUTHORITATIVE)
# ============================================================

def is_logged_out_html(html: str) -> bool:
    if not html:
        return False

    if "<title>نظام الخدمات الالكترونية</title>" in html:
        return True

    if (
        "teachers_login.jsp" in html
        and "student_login.jsp" in html
        and "services4GraduatedStudent.do" in html
    ):
        return True

    return False


def is_staff_login_success_html(html: str) -> bool:
    if not html:
        return False

    # Stable indicators from authenticated staff landing page.
    return (
        "staffWelcomePage.do" in html
        or "signOut.do" in html
        or "البيانات الشخصية" in html
        or "الخدمات الالكترونية لعمادة القبول والتسجيل" in html
    )


# ============================================================
# Internal utilities
# ============================================================

def _mono_ms() -> int:
    import time
    return int(time.monotonic() * 1000)


async def _safe_wait_network(page, timeout_ms=20000):
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass


async def _safe_goto(page, url, timeout_ms=30000):
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)


async def _wait_for_stable_count(locator, *, min_count=1, stable_rounds=3, poll_ms=350, timeout_ms=30000):
    deadline = _mono_ms() + timeout_ms
    stable = 0
    last = -1

    while _mono_ms() < deadline:
        try:
            cnt = await locator.count()
        except Exception:
            cnt = 0

        if cnt >= min_count and cnt == last:
            stable += 1
            if stable >= stable_rounds:
                return cnt
        else:
            stable = 0
            last = cnt

        await asyncio.sleep(poll_ms / 1000.0)

    raise PlaywrightTimeoutError(
        f"Stable count not reached (min_count={min_count}) within {timeout_ms}ms"
    )


async def _wait_for_stable_rowcount(page, table_locator, *, min_rows=2, timeout_ms=30000):
    deadline = _mono_ms() + timeout_ms
    last = -1
    stable = 0

    while _mono_ms() < deadline:
        try:
            cnt = await table_locator.locator("tr").count()
        except Exception:
            cnt = 0

        if cnt >= min_rows and cnt == last:
            stable += 1
            if stable >= 3:
                return cnt
        else:
            stable = 0
            last = cnt

        await asyncio.sleep(0.35)

    raise PlaywrightTimeoutError(
        f"Stable rowcount not reached (min_rows={min_rows}) within {timeout_ms}ms"
    )


async def _wait_for_plan_results(page, timeout_ms=60000):
    tables = page.locator('table[dir="rtl"]')
    await tables.first.wait_for(state="attached", timeout=timeout_ms)
    await _wait_for_stable_count(tables, min_count=1, timeout_ms=timeout_ms)


async def _pick_course_table_from_forumline(page):
    tables = page.locator("table.forumline")
    await tables.first.wait_for(state="attached", timeout=60000)

    candidate = page.locator("table.forumline", has_text="المادة").first
    if await candidate.count() > 0:
        return candidate

    return tables.first


async def _wait_for_timetable_results(page, timeout_ms=60000):
    tables = page.locator("table.forumline")
    await tables.first.wait_for(state="attached", timeout=timeout_ms)

    course_table = await _pick_course_table_from_forumline(page)
    await _wait_for_stable_rowcount(page, course_table, min_rows=2, timeout_ms=timeout_ms)


# ============================================================
# Public API
# ============================================================

async def login_to_portal(admin_username=ADMIN_USERNAME, admin_password=ADMIN_PASSWORD):
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context()
    page = await context.new_page()

    await _safe_goto(page, PORTAL_LOGIN_URL)
    await page.fill('input[name="userName"]', admin_username)
    await page.fill('input[name="password"]', admin_password)
    await page.click('input[name="submit"]')

    await _safe_wait_network(page, timeout_ms=30000)

    html = await safe_page_content(page)
    if is_logged_out_html(html):
        raise RuntimeError("Login failed — still logged out.")
    if not is_staff_login_success_html(html):
        raise RuntimeError("Login failed — success markers not found on staff landing page.")

    print("Admin login successful.")
    return playwright, browser, page


async def create_fresh_page_from_context(context, entry_url=STUDENT_PLAN_URL):
    page = await context.new_page()
    await _safe_goto(page, entry_url)
    await _safe_wait_network(page, timeout_ms=30000)
    return page


async def navigate_to_student_study_plan(page, student_id, verbose=True):
    if await is_logged_out(page):
        raise RuntimeError("Page logged out before navigation.")

    await _safe_goto(page, STUDENT_PLAN_URL)
    await page.locator('input[name="StudentNumber"]').wait_for(state="visible", timeout=30000)
    await page.fill('input[name="StudentNumber"]', str(student_id))
    await page.click('input[name="send"]')

    await _safe_wait_network(page, timeout_ms=30000)
    await _wait_for_plan_results(page, timeout_ms=60000)

    html = await safe_page_content(page)
    if is_logged_out_html(html):
        raise RuntimeError("SESSION_LOGGED_OUT_HTML")

    return html


async def navigate_to_student_timetable(page, student_id, verbose=True):
    if await is_logged_out(page):
        raise RuntimeError("Page logged out before navigation.")

    await _safe_goto(page, STUDENT_TIMETABLE_URL)
    await page.locator('input[name="StudentNumber"]').wait_for(state="visible", timeout=30000)
    await page.fill('input[name="StudentNumber"]', str(student_id))
    await page.click('input[name="send"]')

    await _safe_wait_network(page, timeout_ms=30000)
    await _wait_for_timetable_results(page, timeout_ms=60000)

    html = await safe_page_content(page)
    if is_logged_out_html(html):
        raise RuntimeError("SESSION_LOGGED_OUT_HTML")

    return html


async def close_browser(playwright, browser):
    try:
        await browser.close()
    except Exception:
        pass
    try:
        await playwright.stop()
    except Exception:
        pass
    print("Browser closed.")

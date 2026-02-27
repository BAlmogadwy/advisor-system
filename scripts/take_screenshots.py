"""
Automated screenshot capture for all pages at multiple viewports.
Uses Playwright to navigate, resize, and save PNGs to runtime/screenshots/.
"""

from pathlib import Path

from playwright.sync_api import Page, sync_playwright

BASE_URL = "http://127.0.0.1:8001"
OUT_DIR = Path(__file__).resolve().parent.parent / "runtime" / "screenshots"

# Credentials
USERNAME = "superadmin"
PASSWORD = "test123"

# Pages to screenshot (folder_name, url_path)
PAGES = [
    ("login", "/login/"),
    ("dashboard", "/"),
    ("profile", "/profile/"),
    ("exam-timetable", "/exam-timetable/"),
    ("planner", "/planner/"),
    ("advisor-portfolio", "/advisor-portfolio/"),
    ("audit-explorer", "/audit-explorer/"),
    ("db-admin", "/db-admin/"),
    ("sections-import", "/ops/sections-import/"),
    ("user-management", "/user-management/"),
]

# Viewports: (label, width, height)
VIEWPORTS = [
    ("1440x900", 1440, 900),
    ("768x1024", 768, 1024),
    ("390x844", 390, 844),
    ("360x800", 360, 800),
]


def login(page: Page) -> None:
    """Log in via the Django login form."""
    page.goto(f"{BASE_URL}/login/", wait_until="networkidle")
    page.fill('input[name="username"]', USERNAME)
    page.fill('input[name="password"]', PASSWORD)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")
    print(f"  Logged in as {USERNAME}")


def logout(page: Page) -> None:
    """Log out by submitting the logout form."""
    page.evaluate("""() => {
        const form = document.querySelector('form[action*="logout"]');
        if (form) form.submit();
    }""")
    page.wait_for_load_state("networkidle")


def take_page_screenshots(page: Page, folder: str, url_path: str) -> None:
    """Navigate to a page and screenshot at every viewport."""
    out = OUT_DIR / folder
    out.mkdir(parents=True, exist_ok=True)

    for label, w, h in VIEWPORTS:
        page.set_viewport_size({"width": w, "height": h})
        page.goto(f"{BASE_URL}{url_path}", wait_until="networkidle")
        # Small extra wait for any animations / lazy loads
        page.wait_for_timeout(500)

        filepath = out / f"{folder}_{label}.png"
        page.screenshot(path=str(filepath), full_page=True)
        print(f"    [{label}] -> {filepath.name}")


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            device_scale_factor=1,
        )
        page = context.new_page()

        # ── 1. Login page (must be captured BEFORE logging in) ──
        print("1/10 login")
        out = OUT_DIR / "login"
        out.mkdir(parents=True, exist_ok=True)
        for label, w, h in VIEWPORTS:
            page.set_viewport_size({"width": w, "height": h})
            page.goto(f"{BASE_URL}/login/", wait_until="networkidle")
            page.wait_for_timeout(400)
            filepath = out / f"login_{label}.png"
            page.screenshot(path=str(filepath), full_page=True)
            print(f"    [{label}] -> {filepath.name}")

        # ── 2. Log in ──
        page.set_viewport_size({"width": 1440, "height": 900})
        login(page)

        # ── 3. Remaining pages (all require auth) ──
        for i, (folder, url_path) in enumerate(PAGES[1:], start=2):
            print(f"{i}/10 {folder}")
            take_page_screenshots(page, folder, url_path)

        browser.close()
        print("\nDone — all screenshots saved to runtime/screenshots/")


if __name__ == "__main__":
    main()

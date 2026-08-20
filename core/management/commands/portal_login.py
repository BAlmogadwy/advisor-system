"""Sign in to the portal yourself, once, and save the session for the scraper.

Opens a real browser window at the portal's sign-in page and waits. You do the
whole sign-in — Microsoft, the university's federated page, MFA, "stay signed in",
whatever the tenant asks. When an authenticated portal page appears, the session
is captured and written to disk, and every later scrape starts from it.

    .venv/Scripts/python.exe manage.py portal_login

Nothing here reads or types a password. The scraper does not either, once a
session exists: `PORTAL_ADMIN_PASSWORD` becomes unnecessary and should be removed
from `.env`.

This command needs a screen. It is deliberately not runnable from the web
dashboard or from Render — an attended sign-in on a server with no display is a
prompt nobody can answer, and the failure would look like a hang.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.services.portal_session import save_state, session_is_live, session_path

#: How long to leave the window open. Generous on purpose: a person may have to
#: find a phone, approve a push, or read a consent screen.
DEFAULT_WAIT_SECONDS = 600

#: How often to check whether the sign-in has landed.
_POLL_SECONDS = 1.0


async def _attended_login(wait_seconds: int, *, keep_open: bool) -> tuple[dict[str, Any], str]:
    from playwright.async_api import async_playwright

    from core.services.portal_scraper import (
        is_logged_out_html,
        is_staff_login_success_html,
        safe_page_content,
    )

    login_url = str(getattr(settings, "PORTAL_LOGIN_URL", ""))
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=False)
    try:
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(login_url, wait_until="domcontentloaded", timeout=60000)

        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if page.is_closed():
                raise CommandError("The browser window was closed before sign-in completed.")
            html = await safe_page_content(page, retries=1)
            # The same two-sided gate the scraper uses. `signOut.do` alone appears
            # in the portal's shared navigation on pages that are not a session.
            if is_staff_login_success_html(html) and not is_logged_out_html(html):
                state: dict[str, Any] = await context.storage_state()
                return state, page.url
            await asyncio.sleep(_POLL_SECONDS)
        raise CommandError(
            f"No authenticated portal page appeared within {wait_seconds}s. Nothing was saved."
        )
    finally:
        if not keep_open:
            with contextlib.suppress(Exception):
                await browser.close()
        with contextlib.suppress(Exception):
            await playwright.stop()


async def _verify(state: dict[str, Any]) -> bool:
    """Prove the saved state works from a FRESH context before promising it does.

    Capturing storage state and assuming it is usable is how a command reports
    success for a session that fails on the next scrape. This replays it exactly
    as the scraper will.
    """
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    try:
        context = await browser.new_context(storage_state=state)
        return await session_is_live(context)
    finally:
        with contextlib.suppress(Exception):
            await browser.close()
        with contextlib.suppress(Exception):
            await playwright.stop()


class Command(BaseCommand):
    help = "Open a browser, let an operator sign in, and save the portal session."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--wait-seconds",
            type=int,
            default=DEFAULT_WAIT_SECONDS,
            help=f"How long to wait for sign-in to complete (default {DEFAULT_WAIT_SECONDS}).",
        )
        parser.add_argument(
            "--keep-open",
            action="store_true",
            help="Leave the browser window open after capturing, for inspection.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            from playwright.async_api import async_playwright  # noqa: F401
        except ImportError:
            raise CommandError("Playwright is not installed in this environment.") from None

        wait_seconds = int(options["wait_seconds"])
        if wait_seconds < 30:
            raise CommandError("--wait-seconds must be at least 30.")

        self.stdout.write(f"Opening {settings.PORTAL_LOGIN_URL}")
        self.stdout.write(
            "Sign in in the browser window that opens. Complete every step the "
            "university asks for, including MFA."
        )
        self.stdout.write(f"Waiting up to {wait_seconds}s. Nothing is typed for you.\n")

        state, landed_on = asyncio.run(
            _attended_login(wait_seconds, keep_open=bool(options["keep_open"]))
        )
        self.stdout.write(self.style.SUCCESS("Signed in."))

        self.stdout.write("Verifying the captured session from a fresh browser…")
        if not asyncio.run(_verify(state)):
            raise CommandError(
                "You signed in successfully, but the captured session did not load "
                "the student-enquiry page from a fresh browser, so it was NOT saved.\n"
                "\n"
                "That means the cookies Playwright captured are not enough on their "
                "own — the portal may bind the session to something outside cookie "
                "storage. Re-run with --keep-open and check whether the enquiry page\n"
                f"  {settings.STUDENT_PLAN_URL}\n"
                "loads in the window you signed in with. Nothing was written."
            )

        path = save_state(state)
        self.stdout.write(self.style.SUCCESS(f"Session saved to {path}"))
        self.stdout.write(
            "\nTreat that file as a password: anyone holding it is signed in as this\n"
            "account until it expires. It is gitignored; do not copy it to a server.\n"
            "\nPORTAL_ADMIN_PASSWORD is no longer used by the scraper and can be\n"
            "removed from .env.\n"
        )
        self.stdout.write(f"Landed on: {landed_on.split('?')[0]}")
        self.stdout.write(f"Session file: {session_path()}")

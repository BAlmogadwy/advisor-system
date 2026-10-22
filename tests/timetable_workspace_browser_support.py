"""The Timetable Workspace in a real browser: a logged-in superuser and a
draft scenario with a board of placed sections, loaded at a window size, in a
language and a theme, in the wide font CI's Linux fonts are like.

Shared by the browser tests of the workspace (its frame, its keyboard).
"""

from __future__ import annotations

import os

# Playwright's synchronous API runs through a greenlet; fixture creation is
# synchronous ORM work while Django serves the page on its own thread.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client
from playwright.sync_api import sync_playwright

from tests import one_teacher_support as fx
from tests.test_exam_rosters_browser import WIDE_FONT


class WorkspaceBrowserMixin:
    """Mixed into a StaticLiveServerTestCase. `_load` returns the page, the
    non-GET requests it sent, and its page errors."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls._pw.stop()
        super().tearDownClass()

    def setUp(self) -> None:
        user = get_user_model().objects.create_superuser(
            username="workspace-frame", email="workspace-frame@example.test", password="unused"
        )
        client = Client()
        client.force_login(user)
        self.session = client.cookies[settings.SESSION_COOKIE_NAME].value
        sc, board = fx.scenario("Workspace frame", programs=("AI",))
        # Two sections on the board, and a third still to place: the scenario
        # has a publish blocker (a required section that is on no board).
        for code, name, meetings in (
            ("AI111", "Intro AI", (("SUN", "09:00", "10:15"), ("TUE", "09:00", "10:15"))),
            ("AI222", "Search", (("MON", "13:00", "14:15"), ("WED", "13:00", "14:15"))),
        ):
            key = fx.requirement("AI", code, name)
            fx.budget(sc, key, 1 if code == "AI111" else 2)
            fx.place(board, fx.section(sc, key, "S1"), meetings, room="R101")
        self.scenario = sc
        self.path = f"/timetable-workspace/?scenario={sc.id}"

    def _load(self, language: str, theme: str, width: int, height: int, cards: int = 4):
        context = self.browser.new_context(
            locale="ar" if language == "ar" else "en-US",
            extra_http_headers={"Accept-Language": language},
            viewport={"width": width, "height": height},
            color_scheme=theme,
        )
        self.addCleanup(context.close)
        context.add_cookies(
            [
                {
                    "name": settings.SESSION_COOKIE_NAME,
                    "value": self.session,
                    "url": self.live_server_url,
                },
                {
                    "name": settings.LANGUAGE_COOKIE_NAME,
                    "value": language,
                    "url": self.live_server_url,
                },
            ]
        )
        context.add_init_script(
            f"try {{ localStorage.setItem('theme', '{theme}'); }} catch (e) {{}}"
        )
        context.add_init_script(WIDE_FONT)
        page = context.new_page()
        sent: list[str] = []
        errors: list[str] = []
        page.on(
            "request", lambda r: sent.append(f"{r.method} {r.url}") if r.method != "GET" else None
        )
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{self.live_server_url}{self.path}")
        page.add_style_tag(
            content="*, *::before, *::after { transition: none !important; animation: none !important; }"
        )
        self.assertEqual(page.evaluate("document.documentElement.lang"), language)
        # Loaded: the board's cards are drawn and the readiness answer is in.
        page.wait_for_function(
            f"document.querySelectorAll('.tw-card').length === {cards}"
            " && !document.getElementById('twOptimiseMenu').disabled"
            " && /\\d/.test(document.getElementById('twPublishLabel').textContent)"
        )
        return page, sent, errors

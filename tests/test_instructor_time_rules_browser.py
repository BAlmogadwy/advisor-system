"""The teaching-time rules editor in a real browser, in English and Arabic.

jsdom cannot see the top layer or what a modal dialog makes inert, so this is
the one end-to-end check of the editor (tests/frontend/instructor-time-rules.
test.cjs covers its behaviour in detail): open it from a roster row, add a
rule, leave with Escape, open again and find the draft restored, discard it
back to the saved rules, then save and see the row's pill change; and at a
phone's width, with the dialog open, the page never scrolls sideways.

Nothing leaves the machine (tests/browser_isolation.py).
"""

from __future__ import annotations

import os

# Playwright's synchronous API runs through a greenlet; fixture creation is
# synchronous ORM work while Django serves the page on its own thread.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client

from core.models import Instructor, InstructorTimeRule
from tests.test_exam_rosters_browser import WIDE_FONT

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect
sync_playwright = playwright_api.sync_playwright

# The page at its current width: how far it scrolls sideways, and where the
# open dialog and its body sit.
FIT = """() => {
  const root = document.documentElement;
  const dialog = document.getElementById('imRulesDialog');
  const body = dialog.querySelector('.ux-dialog-body');
  const box = dialog.getBoundingClientRect();
  return { page: root.scrollWidth - root.clientWidth, left: box.left, right: box.right,
           width: window.innerWidth, body: body.scrollWidth - body.clientWidth,
           modal: dialog.matches(':modal') };
}"""


class TimeRulesBrowserMixin:
    """Mixed into a StaticLiveServerTestCase: a signed-in superuser and two
    instructors without rules; `_page` opens Instructor Management."""

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
            username="time-rules-browser", email="time-rules@example.test", password="unused"
        )
        client = Client()
        client.force_login(user)
        self.session = client.cookies[settings.SESSION_COOKIE_NAME].value
        self.hala = Instructor.objects.create(
            full_name="Dr Hala Omar", normalised_name="dr hala omar", full_name_ar="د. هالة عمر"
        )
        self.sami = Instructor.objects.create(
            full_name="Dr Sami Noor", normalised_name="dr sami noor", full_name_ar="د. سامي نور"
        )

    def _page(self, language: str, width: int = 1440, height: int = 900, theme: str = "light"):
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
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        page.goto(f"{self.live_server_url}/instructor-management/")
        page.add_style_tag(
            content="*, *::before, *::after { transition: none !important; animation: none !important; }"
        )
        self.assertEqual(page.evaluate("document.documentElement.lang"), language)
        page.click("#imTabBtnRoster")
        return page


class InstructorTimeRulesBrowserTests(TimeRulesBrowserMixin, StaticLiveServerTestCase):
    def test_a_draft_survives_escape_discard_restores_the_saved_rules_and_a_save_moves_the_pill(
        self,
    ) -> None:
        for language, instructor in (("en", self.hala), ("ar", self.sami)):
            with self.subTest(language=language):
                page = self._page(language)
                times = page.locator(f'[data-im-rules="{instructor.pk}"]')
                pill = page.locator(f'tr:has([data-im-rules="{instructor.pk}"]) .im-times-pill')
                dialog = page.locator("#imRulesDialog")
                rules = page.locator("#imRulesList > li")
                expect(pill).to_have_attribute("data-state", "any")

                times.click()
                expect(dialog).to_be_visible()
                self.assertTrue(page.evaluate(FIT)["modal"], "the editor is a modal dialog")
                expect(rules).to_have_count(0)
                page.click("#imRulesAddNoMornings")
                expect(rules).to_have_count(1)
                expect(page.locator("#imRulesCounts bdi").first).to_have_text("20")
                expect(pill).to_have_attribute("data-state", "any")  # nothing saved yet

                # Escape closes; the keyboard is back on the row; the draft is kept.
                page.keyboard.press("Escape")
                expect(dialog).to_be_hidden()
                expect(times).to_be_focused()
                times.click()
                expect(page.locator("#imRulesRestored")).to_be_visible()
                expect(rules).to_have_count(1)

                # Discard reads the saved rules again: none.
                page.click("#imRulesDiscard")
                expect(page.locator("#imRulesRestored")).to_be_hidden()
                expect(rules).to_have_count(0)

                # Save: the row's pill changes without a reload.
                page.click("#imRulesAddNoMornings")
                page.click("#imRulesSave")
                expect(dialog).to_be_hidden()
                expect(pill).to_have_attribute("data-state", "limited")
                expect(pill.locator("bdi")).to_have_text("20/35")
                expect(times).to_be_focused()
                stored = InstructorTimeRule.objects.filter(instructor=instructor)
                self.assertEqual(
                    list(stored.values_list("kind", "day_mask", "start_minute", "end_minute")),
                    [("unavailable", 31, 0, 780)],
                )

                # A phone: the dialog fits, and the page never scrolls sideways.
                # (Below 769px the global .tbl-card rule hides a table's fifth
                # column on, the roster's actions included, so the dialog is
                # opened first and the window narrowed under it.)
                times.click()
                expect(dialog).to_be_visible()
                page.set_viewport_size({"width": 390, "height": 844})
                page.click("#imRulesAddOnlyDays")
                page.locator("#imRulesList > li").last.locator("[data-day]").nth(4).click()
                fit = page.evaluate(FIT)
                self.assertLessEqual(fit["page"], 0, fit)
                self.assertGreaterEqual(fit["left"], 0, fit)
                self.assertLessEqual(fit["right"], fit["width"], fit)
                self.assertLessEqual(fit["body"], 1, fit)
                page.keyboard.press("Escape")
                expect(dialog).to_be_hidden()
                page.context.close()

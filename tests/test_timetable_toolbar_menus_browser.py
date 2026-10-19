"""The workspace's toolbar menus open whole, beside the buttons that opened them.

Three menus hang off the timetable workspace's toolbars: Generate (in the
generate bar) and Optimise and Export (in the sticky command bar). Two defects
hid them. On wide screens the Generate menu opened beneath the sticky command
bar, because the generate bar's glass blur makes it a stacking context that
traps the menu's own z-index. On tablets a fixed alignment cut Optimise or
Export off at the edge of the content, depending on how the command bar
wrapped; on phones every menu, a fixed sheet with `top: auto`, opened over the
buttons that opened it.

This loads the real page with a draft scenario (so all three menus are live),
opens each menu with the mouse, and checks what the user sees: every item
whole and on top at its corners and centre, the menu inside the visible
content (the viewport cut by every clipping ancestor) and fully on screen,
just under its own buttons (never over them, never detached from them), and
level with them. Escape then closes it, returns focus to its button and, on a
wide screen, puts the generate bar back in its layer. The Generate menu is
also opened from the keyboard. Widths: a wide desktop, the tablet widths where
a fixed alignment failed (Export at 870px in English, Optimise at 800px in
Arabic), the phone breakpoint, a phone and a short phone; English and Arabic;
a wide font, as a CI machine without the design's fonts renders the page.
Opening a menu sends nothing that could change data, and nothing leaves the
machine (tests/browser_isolation.py).
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

from core.models import DeliveryBoard, TimetableScenario
from tests.test_exam_rosters_browser import WIDE_FONT

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

VIEWPORTS = (
    (1440, 900),
    (900, 800),
    (870, 800),
    (800, 800),
    (700, 800),
    (641, 800),
    (390, 844),
    (320, 640),
)
PHONE_MAX_WIDTH = 640

MENUS = (
    ("Generate", "twGenerateMenu", "twGenerateDropdown", "twGenerateGroup"),
    ("Optimise", "twOptimiseMenu", "twOptimiseDropdown", "twOptimiseGroup"),
    ("Export", "twExportMenu", "twExportDropdown", "twExportGroup"),
)

# What is wrong with the open menu, as a list of short findings.
SEEN = r"""([trigger, menuId, groupId]) => {
  const $ = id => document.getElementById(id);
  const menu = $(menuId), buttons = $(groupId).getBoundingClientRect();
  if (menu.style.display === 'none' || $(trigger).getAttribute('aria-expanded') !== 'true') return ['did not open'];
  const r = menu.getBoundingClientRect();
  let left = 0, right = innerWidth, top = 0, bottom = innerHeight;
  for (let a = menu.parentElement; a; a = a.parentElement) {
    const s = getComputedStyle(a), box = a.getBoundingClientRect();
    if (s.overflowX !== 'visible') { left = Math.max(left, box.left); right = Math.min(right, box.right); }
    if (s.overflowY !== 'visible') { top = Math.max(top, box.top); bottom = Math.min(bottom, box.bottom); }
  }
  const px = v => Math.round(v);
  const bad = [];
  if (r.left < left - 0.5 || r.right > right + 0.5) bad.push(`cut off sideways: ${px(r.left)}-${px(r.right)} shown ${px(left)}-${px(right)}`);
  if (r.top < top - 0.5 || r.bottom > bottom + 0.5) bad.push(`not on screen: ${px(r.top)}-${px(r.bottom)} shown ${px(top)}-${px(bottom)}`);
  if (r.top < buttons.bottom - 0.5) bad.push(`over its buttons: top ${px(r.top)}, buttons end ${px(buttons.bottom)}`);
  else if (r.top - buttons.bottom > 12) bad.push(`detached ${px(r.top - buttons.bottom)}px below its buttons`);
  if (r.right < buttons.left || r.left > buttons.right) bad.push('not level with its buttons');
  for (const item of menu.querySelectorAll('[role="menuitem"]')) {
    const box = item.getBoundingClientRect();
    for (const [x, y] of [[0.04, 0.12], [0.96, 0.12], [0.5, 0.5], [0.04, 0.88], [0.96, 0.88]]) {
      const hit = document.elementFromPoint(box.left + box.width * x, box.top + box.height * y);
      if (!hit || !item.contains(hit)) {
        bad.push(`"${item.textContent.trim().slice(0, 24)}" covered by ${hit ? hit.id || hit.className || hit.tagName : 'nothing'}`);
        break;
      }
    }
  }
  return bad;
}"""

AFTER_ESCAPE = r"""(trigger) => ({
  open: document.getElementById(trigger).getAttribute('aria-expanded') === 'true',
  focus: document.activeElement?.id || document.activeElement?.tagName,
  barLayer: getComputedStyle(document.getElementById('twGenBar')).zIndex,
})"""


class ToolbarMenusBrowserTests(StaticLiveServerTestCase):
    maxDiff = None

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
            username="toolbar-menus", email="toolbar-menus@example.test", password="unused"
        )
        client = Client()
        client.force_login(user)
        self.session = client.cookies[settings.SESSION_COOKIE_NAME].value
        scenario = TimetableScenario.objects.create(
            academic_year="1448", term="2", name="Toolbar menus", gender="M", programs=["AI"]
        )
        DeliveryBoard.objects.create(
            scenario=scenario, label="Term 2", nominal_term=2, program="AI", display_order=1
        )
        self.path = f"/timetable-workspace/?scenario={scenario.id}"

    def _load(self, language: str, width: int, height: int):
        context = self.browser.new_context(
            locale="ar" if language == "ar" else "en-US",
            extra_http_headers={"Accept-Language": language},
            viewport={"width": width, "height": height},
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
        context.add_init_script("try { localStorage.setItem('theme', 'light'); } catch (e) {}")
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
        # The scenario has loaded once its edit menus are live.
        page.wait_for_function(
            "!document.getElementById('twOptimiseMenu').disabled"
            " && !document.getElementById('twExportMenu').disabled"
        )
        return page, sent, errors

    def test_each_toolbar_menu_opens_whole_beside_its_buttons(self) -> None:
        # Every case is checked before failing, so one run names every misfit.
        problems: dict[str, list] = {}
        for language in ("en", "ar"):
            for width, height in VIEWPORTS:
                page, sent, errors = self._load(language, width, height)
                phone = width <= PHONE_MAX_WIDTH
                for name, trigger, menu, group in MENUS:
                    where = f"{name} {language} {width}x{height}"
                    page.click(f"#{trigger}")
                    found = page.evaluate(SEEN, [trigger, menu, group])
                    if name == "Generate" and not phone:
                        layer = page.evaluate(AFTER_ESCAPE, trigger)["barLayer"]
                        if layer in ("auto", "0"):
                            found.append(f"generate bar not raised while open ({layer})")
                    page.keyboard.press("Escape")
                    closed = page.evaluate(AFTER_ESCAPE, trigger)
                    if closed["open"]:
                        found.append("Escape did not close it")
                    if closed["focus"] != trigger:
                        found.append(f"Escape left focus on {closed['focus']}")
                    if not phone and closed["barLayer"] != "auto":
                        found.append(
                            f"generate bar still raised after closing ({closed['barLayer']})"
                        )
                    if found:
                        problems[where] = found
                # The keyboard reaches the Generate menu too, and opens it whole.
                page.focus("#twGenerateMenu")
                page.keyboard.press("ArrowDown")
                where = f"Generate by keyboard {language} {width}x{height}"
                found = page.evaluate(
                    SEEN, ["twGenerateMenu", "twGenerateDropdown", "twGenerateGroup"]
                )
                if page.evaluate("document.activeElement?.dataset.engine") != "classic":
                    found.append("ArrowDown did not focus the first engine")
                page.keyboard.press("Escape")
                if found:
                    problems[where] = found
                # Opening menus sent nothing that could change data.
                if sent or errors:
                    problems[f"page {language} {width}x{height}"] = [*sent, *errors]
                page.context.close()
        self.assertEqual(problems, {})

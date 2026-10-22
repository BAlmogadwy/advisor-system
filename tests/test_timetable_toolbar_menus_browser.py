"""The workspace's toolbar menus open whole, beside the buttons that opened them.

Five menus hang off the timetable workspace's toolbars, all driven by the
design system's menu controller (static/js/ux-controls.js): New and the
scenario's actions, Optimise and Export in the page header, and "Open in" in
the board bar. Earlier defects this guards against: a menu opened beneath the
next bar because its own bar's glass blur is a stacking context (the header's
menus must paint over the board bar); a fixed alignment cut a menu off at the
edge of the content, depending on how its bar wrapped; on phones a fixed sheet
opened over the buttons that opened it.

This loads the real page with a draft scenario (so every menu is live), opens
each menu with the mouse, and checks what the user sees: every item whole and
on top at its corners and centre, the menu inside the visible content (the
viewport cut by every clipping ancestor) and fully on screen, just under its
own buttons (never over them, never detached from them), and level with them;
on a phone, a sheet 12px inside each edge of its toolbar; and the page never
scrolled sideways. Escape then closes it and returns focus to its button. The
New menu is also opened from the keyboard. Widths: a wide desktop, the tablet
widths where a fixed alignment once failed, the phone breakpoint, a phone and a
short phone; English and Arabic; a wide font, as a CI machine without the
design's fonts renders the page.

A phone turned from landscape to portrait crosses the phone breakpoint, and a
wide-screen placement once outlived it (the Arabic sheet hung 146px off the
page and dragged it sideways). So each menu is also opened on the landscape
layout and reopened after the turn, and kept open across the turn and back.
Scrolled past an open menu, the page stays put while the address bar shows or
the page's status line grows: only opening a menu scrolls to it. Opening a
menu sends nothing that could change data, and nothing leaves the machine
(tests/browser_isolation.py).
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
# (Phones, at or under 640px, show each menu as a sheet across its toolbar.)
# A phone on its side and upright, either side of the phone breakpoint.
LANDSCAPE = (844, 390)
PORTRAIT = (390, 844)

MENUS = (
    ("New", "twNewMenu", "twNewDropdown", "twNewGroup"),
    ("Scenario actions", "twMoreMenu", "twMoreDropdown", "twMoreGroup"),
    ("Optimise", "twOptimiseMenu", "twOptimiseDropdown", "twOptimiseGroup"),
    ("Export", "twExportMenu", "twExportDropdown", "twExportGroup"),
    ("Open in", "twOpenMenu", "twOpenDropdown", "twOpenGroup"),
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
  if (matchMedia('(max-width: 640px)').matches) {
    const bar = menu.closest('.ux-toolbar').getBoundingClientRect();
    if (Math.abs(r.left - bar.left - 12) > 1 || Math.abs(bar.right - r.right - 12) > 1) {
      bad.push(`not a sheet across its bar: ${px(r.left)}-${px(r.right)}, bar ${px(bar.left)}-${px(bar.right)}`);
    }
  }
  // The page clips sideways overflow; a scroll into view can still shift it.
  const sideways = $('main-content').scrollLeft;
  if (sideways) bad.push(`page scrolled sideways ${sideways}px`);
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

# Resolves once the bars, their button groups and the menus have held still
# for ten frames: after a resize a bar goes on rewrapping for several frames.
SETTLED = r"""async () => {
  const boxes = () => JSON.stringify([...document.querySelectorAll(
    '.tw-header, #twCmdBar, .btn-split, .ux-menu-anchor, [role="menu"]'
  )].map(e => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; }));
  let last = boxes(), still = 0;
  for (let frame = 0; frame < 300 && still < 10; frame++) {
    await new Promise(resolve => requestAnimationFrame(resolve));
    const now = boxes();
    still = now === last ? still + 1 : 0;
    last = now;
  }
}"""

# Scrolls the page until the open New menu is above the fold; false when the
# page is too short for that.
SCROLL_PAST_NEW = r"""() => {
  const main = document.getElementById('main-content');
  const menu = document.getElementById('twNewDropdown');
  main.scrollTop += menu.getBoundingClientRect().bottom - main.getBoundingClientRect().top + 20;
  return menu.getBoundingClientRect().bottom < main.getBoundingClientRect().top;
}"""
# Whether the page still shows what the user scrolled to, past the menu.
STILL_PAST_NEW = r"""() => {
  const menu = document.getElementById('twNewDropdown').getBoundingClientRect();
  const view = document.getElementById('main-content').getBoundingClientRect();
  return menu.bottom < view.top ? [] : [`page pulled back to the menu (${Math.round(menu.top)}-${Math.round(menu.bottom)})`];
}"""

AFTER_ESCAPE = r"""(trigger) => ({
  open: document.getElementById(trigger).getAttribute('aria-expanded') === 'true',
  focus: document.activeElement?.id || document.activeElement?.tagName,
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
                for name, trigger, menu, group in MENUS:
                    where = f"{name} {language} {width}x{height}"
                    page.click(f"#{trigger}")
                    found = page.evaluate(SEEN, [trigger, menu, group])
                    page.keyboard.press("Escape")
                    closed = page.evaluate(AFTER_ESCAPE, trigger)
                    if closed["open"]:
                        found.append("Escape did not close it")
                    if closed["focus"] != trigger:
                        found.append(f"Escape left focus on {closed['focus']}")
                    if found:
                        problems[where] = found
                # The keyboard reaches the New menu too, and opens it whole.
                page.focus("#twNewMenu")
                page.keyboard.press("ArrowDown")
                where = f"New by keyboard {language} {width}x{height}"
                found = page.evaluate(SEEN, ["twNewMenu", "twNewDropdown", "twNewGroup"])
                if page.evaluate("document.activeElement?.id") != "twGenerate":
                    found.append("ArrowDown did not focus Generate timetable")
                page.keyboard.press("Escape")
                if found:
                    problems[where] = found
                # Opening menus sent nothing that could change data.
                if sent or errors:
                    problems[f"page {language} {width}x{height}"] = [*sent, *errors]
                page.context.close()
        self.assertEqual(problems, {})

    @staticmethod
    def _turn(page, width: int, height: int) -> None:
        page.set_viewport_size({"width": width, "height": height})
        page.evaluate(SETTLED)

    def test_each_toolbar_menu_follows_a_phone_turned_upright(self) -> None:
        problems: dict[str, list] = {}
        for language in ("en", "ar"):
            page, sent, errors = self._load(language, *LANDSCAPE)
            # Placed on the landscape layout, then reopened upright.
            for _name, trigger, _menu, _group in MENUS:
                page.click(f"#{trigger}")
                page.keyboard.press("Escape")
            self._turn(page, *PORTRAIT)
            for name, trigger, menu, group in MENUS:
                page.click(f"#{trigger}")
                found = page.evaluate(SEEN, [trigger, menu, group])
                page.keyboard.press("Escape")
                if found:
                    problems[f"{name} {language} reopened upright"] = found
            # Left open while the phone turns upright and back.
            self._turn(page, *LANDSCAPE)
            for name, trigger, menu, group in MENUS:
                page.click(f"#{trigger}")
                for turned, viewport in (("upright", PORTRAIT), ("back", LANDSCAPE)):
                    self._turn(page, *viewport)
                    found = page.evaluate(SEEN, [trigger, menu, group])
                    if found:
                        problems[f"{name} {language} open, turned {turned}"] = found
                page.keyboard.press("Escape")
            # Scrolled past an open menu, the page stays where the user put it
            # while the layout changes: the address bar shows as the page
            # scrolls (and shortens it), and the generate bar's status line
            # grows, which re-places the menu. Only opening a menu scrolls.
            self._turn(page, *PORTRAIT)
            page.click("#twNewMenu")
            if not page.evaluate(SCROLL_PAST_NEW):
                problems[f"page {language}"] = ["too short to scroll past the New menu"]
            self._turn(page, PORTRAIT[0], PORTRAIT[1] - 56)
            found = page.evaluate(STILL_PAST_NEW)
            if found:
                problems[f"New {language} open, address bar shown"] = found
            page.evaluate(
                "document.getElementById('twGenStatus').textContent = 'Saved. '.repeat(40)"
            )
            page.evaluate(SETTLED)
            found = page.evaluate(STILL_PAST_NEW)
            if found:
                problems[f"New {language} open, status line grown"] = found
            # Something above the buttons inside the bar moves them without
            # resizing their group: the bar is watched too, so the sheet
            # follows them, and re-placing it still leaves the page alone.
            page.evaluate("document.querySelector('.tw-header').style.paddingTop = '48px'")
            page.evaluate(SETTLED)
            found = page.evaluate(STILL_PAST_NEW)
            page.evaluate("document.getElementById('main-content').scrollTop = 0")
            page.evaluate(SETTLED)
            found += page.evaluate(SEEN, ["twNewMenu", "twNewDropdown", "twNewGroup"])
            if found:
                problems[f"New {language} open, buttons moved down"] = found
            page.keyboard.press("Escape")
            if sent or errors:
                problems[f"page {language} turned"] = [*sent, *errors]
            page.context.close()
        self.assertEqual(problems, {})

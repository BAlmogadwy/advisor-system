"""The Timetable Workspace is a full-screen tool that people can read.

docs/TIMETABLE-WORKSPACE-UI.md, "Acceptance". The page used to sit in a 1120px
column on any screen, so its ~600px grid cut off the day's last slot and the
page, its panes and the grid all scrolled; 72% of its text was 11.5px or less;
and Publish looked ready while the server would refuse it.

This loads a real draft scenario (a board with placed sections) at the window
sizes staff use — 1366×650, a 1440×900 monitor's 1440×790 window, a 125%-scaled
1080p laptop's 1536×730, and 1920×1080 — in English and Arabic, light and dark,
in a wide font, and checks what is on screen:

- the page never scrolls sideways, and in a window at least 640px tall it does
  not scroll at all (its panes do);
- at the default pane state a lecture slot is at least 96px wide;
- at 1440px and wider the chrome above the panes is at most 170px;
- no text in the page frame is under 11px;
- the stat strip's, the board bar's and the grid headers' text clears 4.5:1
  against what is behind it.

And Publish: on a scenario the server will not publish, the button says how
many blockers there are, is not the filled primary, and opening it lists them
without ever calling publish. Nothing is sent but reads, and nothing leaves the
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

from tests import one_teacher_support as fx
from tests.test_exam_rosters_browser import WIDE_FONT

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

WINDOWS = ((1366, 650), (1440, 790), (1536, 730), (1920, 1080))

# What is wrong with the frame, as a list of short findings.
FRAME = r"""(minChromeWidth) => {
  const bad = [];
  const main = document.getElementById('main-content');
  const px = v => Math.round(v);
  if (document.documentElement.scrollWidth > innerWidth + 0.5) bad.push(`page scrolls sideways ${document.documentElement.scrollWidth - innerWidth}px`);
  if (main.scrollWidth > main.clientWidth + 0.5) bad.push(`main scrolls sideways ${main.scrollWidth - main.clientWidth}px`);
  if (innerHeight >= 640 && main.scrollHeight > main.clientHeight + 1) bad.push(`page scrolls ${main.scrollHeight - main.clientHeight}px`);
  const slot = document.querySelector('.tw-grid-table:not(.tw-lab-grid):not(.tw-english-grid) .tw-grid-header:nth-child(2)');
  if (!slot) bad.push('no lecture grid');
  else if (slot.getBoundingClientRect().width < 95.5) bad.push(`lecture slot ${px(slot.getBoundingClientRect().width)}px`);
  const chrome = document.querySelector('.tw-workspace').getBoundingClientRect().top - main.getBoundingClientRect().top;
  if (innerWidth >= minChromeWidth && chrome > 170.5) bad.push(`chrome ${px(chrome)}px above the panes`);
  const small = [...document.querySelectorAll('.content-wrap *')].filter(el => {
    if (!el.getClientRects().length || getComputedStyle(el).visibility === 'hidden') return false;
    const own = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
    return own && parseFloat(getComputedStyle(el).fontSize) < 10.95;
  });
  for (const el of small.slice(0, 5)) bad.push(`text ${getComputedStyle(el).fontSize}: "${el.textContent.trim().slice(0, 24)}"`);
  return bad;
}"""

# Text that must read at 4.5:1 against what is actually behind it (each
# ancestor's background composited in), in either theme.
CONTRAST = r"""(selectors) => {
  const cv = document.createElement('canvas'); cv.width = cv.height = 1;
  const cx = cv.getContext('2d', { willReadFrequently: true });
  const rgba = v => { cx.clearRect(0, 0, 1, 1); cx.fillStyle = 'rgba(0,0,0,0)'; cx.fillStyle = v; cx.fillRect(0, 0, 1, 1);
    const d = cx.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3] / 255]; };
  const over = (top, under) => top.slice(0, 3).map((c, i) => c * top[3] + under[i] * (1 - top[3])).concat(1);
  const page = getComputedStyle(document.body).backgroundColor;
  const behind = el => { const chain = []; for (let e = el; e; e = e.parentElement) chain.push(rgba(getComputedStyle(e).backgroundColor));
    return chain.reverse().reduce((under, c) => c[3] > 0 ? over(c, under) : under, rgba(page)); };
  const lum = c => c.slice(0, 3).map(x => { x /= 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; })
    .reduce((s, x, i) => s + x * [0.2126, 0.7152, 0.0722][i], 0);
  const ratio = (fg, bg) => { const a = lum(over(fg, bg)), b = lum(bg); return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05); };
  const bad = [];
  for (const selector of selectors) {
    const optional = selector.startsWith('?');
    const css = optional ? selector.slice(1) : selector;
    const els = [...document.querySelectorAll(css)].filter(el => el.getClientRects().length && el.textContent.trim());
    if (!els.length) { if (!optional) bad.push(`nothing matches ${css}`); continue; }
    for (const el of els.slice(0, 3)) {
      const r = ratio(rgba(getComputedStyle(el).color), behind(el));
      if (r < 4.5) bad.push(`${selector} "${el.textContent.trim().slice(0, 20)}" ${r.toFixed(2)}:1`);
    }
  }
  return bad;
}"""
TEXT = [
    ".ux-stat > dt",
    ".ux-stat > dd",
    ".ux-stat-scope",
    ".ux-toolbar-label",
    ".tw-grid-header",
    ".tw-grid-slot-label",
    # Only where the required-sections list is open (it starts collapsed below 1600px).
    "?.tw-section-item .meta",
]


class WorkspaceFrameBrowserTests(StaticLiveServerTestCase):
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

    def _load(self, language: str, theme: str, width: int, height: int):
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
            "document.querySelectorAll('.tw-card').length === 4"
            " && !document.getElementById('twOptimiseMenu').disabled"
            " && /\\d/.test(document.getElementById('twPublishLabel').textContent)"
        )
        return page, sent, errors

    def test_the_frame_fits_the_window_and_reads(self) -> None:
        problems: dict[str, list] = {}
        for language in ("en", "ar"):
            for theme in ("light", "dark"):
                for width, height in WINDOWS:
                    page, sent, errors = self._load(language, theme, width, height)
                    found = page.evaluate(FRAME, 1440) + page.evaluate(CONTRAST, TEXT)
                    if sent or errors:
                        found += [*sent, *errors]
                    if found:
                        problems[f"{language} {theme} {width}x{height}"] = found
                    page.context.close()
        self.assertEqual(problems, {})

    def test_publish_names_its_blockers_and_never_publishes_a_blocked_scenario(self) -> None:
        for language in ("en", "ar"):
            page, sent, errors = self._load(language, "light", 1440, 790)
            button = page.locator("#twPublish")
            self.assertEqual(button.get_attribute("class").split().count("btn-primary"), 0)
            self.assertRegex(page.inner_text("#twPublishLabel"), r"1")
            self.assertFalse(page.is_hidden("#twPublishIcon"))
            button.click()
            page.wait_for_selector(".dlg-backdrop .tw-readiness-list")
            listed = page.inner_text(".dlg-backdrop .tw-readiness-list")
            self.assertIn("AI222", listed)
            self.assertEqual([r for r in sent if "/publish/" in r], [])
            self.assertEqual(errors, [])
            page.context.close()

    def test_the_generate_dialog_reads_in_both_themes(self) -> None:
        # A checked programme chip sits on the primary fill: its text must use
        # the fill's ink (white on the dark theme's mint was 2.10:1).
        problems: dict[str, list] = {}
        for theme in ("light", "dark"):
            page, sent, errors = self._load("en", theme, 1440, 790)
            page.click("#twNewMenu")
            page.click("#twGenerate")
            page.wait_for_selector("#twGenerateDialog[open]")
            found = page.evaluate(
                CONTRAST,
                [
                    ".chip-toggle:has(input:checked)",
                    ".ux-field-label",
                    ".ux-choice-title",
                    ".ux-choice-desc",
                    ".ux-seg-item",
                    ".ux-dialog-sub",
                ],
            )
            # Closing the dialog returns the keyboard to the menu's trigger.
            page.keyboard.press("Escape")
            if page.evaluate("document.activeElement?.id") != "twNewMenu":
                found.append(f"focus after closing: {page.evaluate('document.activeElement?.id')}")
            if sent or errors:
                found += [*sent, *errors]
            if found:
                problems[theme] = found
            page.context.close()
        self.assertEqual(problems, {})

    def test_typing_in_a_filter_never_scrolls_the_page(self) -> None:
        # Below 1025px wide the page scrolls instead of its panes. Re-rendering
        # the board buttons on each keystroke once called scrollIntoView, which
        # scrolled the page and threw the field the user was typing in off screen.
        page, sent, errors = self._load("en", "light", 1000, 700)
        page.evaluate("document.getElementById('twToggleLeft').click()")
        field = page.locator("#twSectionSearch")
        field.scroll_into_view_if_needed()
        before = page.evaluate("document.getElementById('main-content').scrollTop")
        field.click()
        field.type("AI", delay=40)
        page.wait_for_timeout(300)
        after = page.evaluate("document.getElementById('main-content').scrollTop")
        self.assertEqual(after, before)
        self.assertEqual([*sent, *errors], [])

    def test_the_chosen_board_stays_in_view_in_a_long_board_strip(self) -> None:
        # Coming back from MRI or Split view to the last of many boards: the
        # strip scrolls (never the page) to show it, and keeps it shown when
        # later answers widen the buttons before it ("N left", flags).
        from core.models import DeliveryBoard

        last = None
        for term in range(3, 13):
            last = DeliveryBoard.objects.create(
                scenario=self.scenario,
                label=f"Term {term} group with a longer name",
                nominal_term=term,
                program="AI",
                display_order=term,
            )
        self.path = f"/timetable-workspace/?scenario={self.scenario.id}&board={last.id}"
        for language in ("en", "ar"):
            context = self.browser.new_context(
                locale="ar" if language == "ar" else "en-US",
                extra_http_headers={"Accept-Language": language},
                viewport={"width": 1440, "height": 900},
            )
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
            page = context.new_page()
            page.goto(f"{self.live_server_url}{self.path}")
            page.wait_for_function(
                f"document.querySelector('#twBoardTabs [aria-current=\"true\"]')?.dataset.boardId === '{last.id}'"
                " && /\d/.test(document.getElementById('twPublishLabel').textContent)"
            )
            page.wait_for_timeout(600)
            seen = page.evaluate(
                """() => {
                  const strip = document.getElementById('twBoardTabs').getBoundingClientRect();
                  const chosen = document.querySelector('#twBoardTabs [aria-current="true"]').getBoundingClientRect();
                  return { overflow: document.getElementById('twBoardTabs').scrollWidth > strip.width + 1,
                           inside: chosen.left >= strip.left - 1 && chosen.right <= strip.right + 1,
                           pageScroll: document.getElementById('main-content').scrollTop };
                }"""
            )
            self.assertTrue(seen["overflow"], f"{language}: the strip should overflow in this test")
            self.assertTrue(seen["inside"], f"{language}: the chosen board is out of view")
            self.assertEqual(seen["pageScroll"], 0)
            context.close()

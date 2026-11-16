"""The sidebar's command palette, icon rail and section groups, in a browser.

Thirty-three links in one flat scrolling list got three affordances
(static/js/sidebar-nav.js, the SIDEBAR section of static/css/global.css, and
core/templates/core/partials/sidebar.html):

- a command palette on Ctrl/Cmd+K and on the nav's search row, which
  fuzzy-matches every destination the server drew for this person, moves with
  the arrow keys, opens with Enter, closes with Escape and gives focus back;
- an icon rail at 64px, remembered across loads and APPLIED BY THE HEAD
  SCRIPT, before the body is parsed, so a collapsed sidebar is never painted
  open first; each item keeps its name, clipped, and shows it as a tooltip;
- collapsible section groups, remembered per browser, except that arriving at
  a page never leaves the group holding it collapsed.

And the two contrast fixes: an inactive nav icon clears 3:1 (WCAG 1.4.11)
against the sidebar it sits on, in light and in dark, where it measured 1.84
and 2.21.

The palette's words come from the template in both languages, so the Arabic
run asserts against a page the SERVER rendered in Arabic (`advisor_language`,
checked by `document.documentElement.lang`), never a translated one.

Focus is asserted with `expect(...).to_be_focused()`, which retries, not with
one `document.activeElement` read. Nothing leaves the machine
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

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright
expect = playwright_api.expect

#: WCAG 1.4.11 for a non-text graphic. Asserted a little over 3 so that alpha
#: rounding and a slightly different composited backdrop cannot sit the result
#: on the threshold; it is the THRESHOLD that is pinned here, never the alpha
#: the stylesheet happens to use.
MIN_ICON = 3.05

#: Recorded the moment `<body>` is inserted — before a single byte of the body
#: is parsed, so before any script at the end of it. Whatever is on <html> at
#: that point was put there by the anti-flash block in base.html's <head>.
#: An element's painted colour against everything composited behind it, as a
#: WCAG contrast ratio. `opacity` is folded in: that is what the fix moved.
CONTRAST = """(selector) => {
  const el = document.querySelector(selector);
  if (!el) return null;
  const cv = document.createElement('canvas'); cv.width = cv.height = 1;
  const cx = cv.getContext('2d', { willReadFrequently: true });
  const rgba = (v) => { cx.clearRect(0, 0, 1, 1); cx.fillStyle = 'rgba(0,0,0,0)'; cx.fillStyle = v;
    cx.fillRect(0, 0, 1, 1); const d = cx.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3] / 255]; };
  const over = (top, under) => top.slice(0, 3).map((c, i) => c * top[3] + under[i] * (1 - top[3])).concat(1);
  const behind = (node) => { const chain = [];
    for (let e = node; e; e = e.parentElement) chain.push(rgba(getComputedStyle(e).backgroundColor));
    return chain.reverse().reduce((under, c) => c[3] > 0 ? over(c, under) : under, [255, 255, 255, 1]); };
  const lum = (c) => c.slice(0, 3).map(x => { x /= 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; })
    .reduce((s, x, i) => s + x * [0.2126, 0.7152, 0.0722][i], 0);
  const bg = behind(el.parentElement);
  const style = getComputedStyle(el);
  const ink = rgba(style.color);
  ink[3] = ink[3] * parseFloat(style.opacity);
  const a = lum(over(ink, bg)), b = lum(bg);
  return +(((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)).toFixed(2));
}"""


class SidebarNavBrowserTests(StaticLiveServerTestCase):
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
            username="sidebar-nav", email="sidebar-nav@example.test", password="unused"
        )
        client = Client()
        client.force_login(user)
        self.session = client.cookies[settings.SESSION_COOKIE_NAME].value

    # ── Harness ──────────────────────────────────────────────────────────
    def _context(self, language: str = "en", theme: str = "light", height: int = 820):
        context = self.browser.new_context(
            locale="ar" if language == "ar" else "en-US",
            extra_http_headers={"Accept-Language": language},
            viewport={"width": 1440, "height": height},
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
        return context

    def _open(self, context, path: str = "/planner/", language: str = "en"):
        page = context.new_page()
        page.goto(f"{self.live_server_url}{path}")
        # The language has to be the SERVER's, or an Arabic assertion asserts
        # nothing (and a translated page would read Arabic while declaring en).
        self.assertEqual(page.evaluate("document.documentElement.lang"), language)
        page.add_style_tag(
            content="*, *::before, *::after { transition: none !important; animation: none !important; }"
        )
        return page

    # ── The command palette ──────────────────────────────────────────────
    def test_the_shortcut_opens_the_palette_and_the_keyboard_drives_it(self) -> None:
        page = self._open(self._context())
        page.keyboard.press("Control+k")

        dialog = page.locator("#sbp .sbp-panel")
        expect(dialog).to_be_visible()
        self.assertEqual(dialog.get_attribute("aria-modal"), "true")
        # The page behind it is out of the tab order and out of the a11y tree.
        self.assertTrue(page.locator(".shell").get_attribute("inert") is not None)

        field = page.locator("#sbpInput")
        expect(field).to_be_focused()

        # A fuzzy, out-of-order query, and the characters it matched are marked.
        field.type("secpl")
        options = page.locator("#sbpList .sbp-opt")
        expect(options).to_have_count(1)
        expect(options.first.locator(".sbp-opt-t")).to_have_text("Section Planning")
        self.assertEqual(options.first.locator("mark").all_text_contents(), ["Sec", "Pl"])
        self.assertEqual(options.first.get_attribute("aria-selected"), "true")
        self.assertEqual(
            field.get_attribute("aria-activedescendant"), options.first.get_attribute("id")
        )

        # Enter opens what is chosen.
        page.keyboard.press("Enter")
        page.wait_for_url(f"{self.live_server_url}/section-planning/")
        expect(page.locator('.sidebar .nav-a.active[href="/section-planning/"]')).to_have_count(1)

    def test_the_arrow_keys_move_the_choice_and_wrap(self) -> None:
        page = self._open(self._context())
        page.keyboard.press("Control+k")
        page.locator("#sbpInput").type("exp")

        options = page.locator("#sbpList .sbp-opt")
        expect(options).to_have_count(2)
        chosen = lambda: [o.get_attribute("aria-selected") for o in options.all()]  # noqa: E731
        self.assertEqual(chosen(), ["true", "false"])
        page.keyboard.press("ArrowDown")
        self.assertEqual(chosen(), ["false", "true"])
        # Past the end it comes back to the top.
        page.keyboard.press("ArrowDown")
        self.assertEqual(chosen(), ["true", "false"])
        page.keyboard.press("ArrowUp")
        self.assertEqual(chosen(), ["false", "true"])
        page.keyboard.press("Home")
        self.assertEqual(chosen(), ["true", "false"])
        page.keyboard.press("End")
        self.assertEqual(chosen(), ["false", "true"])

    def test_escape_closes_the_palette_and_hands_focus_back(self) -> None:
        page = self._open(self._context())
        row = page.locator("#navFind")
        row.focus()
        expect(row).to_be_focused()
        page.keyboard.press("Enter")

        expect(page.locator("#sbpInput")).to_be_focused()
        page.keyboard.press("Escape")

        expect(page.locator("#sbp .sbp-panel")).not_to_be_visible()
        expect(row).to_be_focused()
        # Nothing is left inert behind a closed dialog.
        self.assertIsNone(page.locator(".shell").get_attribute("inert"))

    def test_tab_stays_inside_the_open_palette(self) -> None:
        page = self._open(self._context())
        page.keyboard.press("Control+k")
        field = page.locator("#sbpInput")
        expect(field).to_be_focused()
        page.keyboard.press("Tab")
        expect(field).to_be_focused()
        page.keyboard.press("Shift+Tab")
        expect(field).to_be_focused()

    def test_a_query_that_matches_nothing_says_so(self) -> None:
        page = self._open(self._context())
        page.keyboard.press("Control+k")
        page.locator("#sbpInput").type("zzzz")
        expect(page.locator("#sbpList .sbp-opt")).to_have_count(0)
        expect(page.locator("#sbpEmpty")).to_be_visible()
        expect(page.locator("#sbpEmpty")).to_have_text("No page matches that.")

    def test_the_palette_searches_the_arabic_labels_on_an_arabic_page(self) -> None:
        context = self._context(language="ar")
        page = self._open(context, language="ar")
        # The words are the template's Arabic, not a string this script built.
        expect(page.locator("#navFind .nav-find-t")).to_have_text("الانتقال إلى صفحة…")

        page.keyboard.press("Control+k")
        field = page.locator("#sbpInput")
        expect(field).to_be_focused()
        # "تخطيط" — the Arabic for Section Planning is "تخطيط الشعب", and
        # Graduation Planning is "تخطيط التخرج".
        field.type("تخطيط")
        labels = page.locator("#sbpList .sbp-opt .sbp-opt-t").all_text_contents()
        self.assertIn("تخطيط الشعب", labels)
        self.assertIn("تخطيط التخرج", labels)
        # An English name is not what an Arabic page offers.
        self.assertNotIn("Section Planning", labels)

    def test_the_shortcut_stands_down_where_the_dashboard_already_owns_it(self) -> None:
        # The dashboard's own Ctrl/Cmd+K jumps to a student, course or advisor
        # by id, and has no other way in. The row still opens the palette.
        page = self._open(self._context(), path="/")
        row = page.locator("#navFind")
        self.assertIsNone(row.get_attribute("aria-keyshortcuts"))
        expect(page.locator("#navFind .nav-find-kbd")).to_have_count(0)

        page.keyboard.press("Control+k")
        expect(page.locator("#cmdPalette")).not_to_have_class("d-none")
        expect(page.locator("#sbp")).to_have_count(0)

        page.keyboard.press("Escape")
        row.click()
        expect(page.locator("#sbp .sbp-panel")).to_be_visible()

    def test_the_search_row_says_the_shortcut_where_it_is_bound(self) -> None:
        page = self._open(self._context())
        row = page.locator("#navFind")
        self.assertEqual(row.get_attribute("aria-keyshortcuts"), "Control+K Meta+K")
        expect(page.locator("#navFind .nav-find-kbd")).to_have_text("Ctrl K")

    # ── The icon rail ────────────────────────────────────────────────────
    def test_the_rail_is_remembered_and_applied_before_the_body_is_parsed(self) -> None:
        context = self._context()
        page = self._open(context)
        sidebar = page.locator("#appSidebar")
        self.assertGreater(sidebar.bounding_box()["width"], 200)

        toggle = page.locator("#sbRailToggle")
        self.assertEqual(toggle.get_attribute("aria-expanded"), "true")
        toggle.click()
        self.assertEqual(toggle.get_attribute("aria-expanded"), "false")
        self.assertAlmostEqual(sidebar.bounding_box()["width"], 64, delta=2)
        self.assertEqual(page.evaluate("localStorage.getItem('sidebar-rail')"), "1")
        # Its name says what pressing it does now, in the page's language.
        expect(toggle).to_have_accessible_name("Expand the menu")

        again = self._open(context)
        self.assertAlmostEqual(again.locator("#appSidebar").bounding_box()["width"], 64, delta=2)
        expect(again.locator("#sbRailToggle")).to_have_accessible_name("Expand the menu")

        # And it goes back.
        again.locator("#sbRailToggle").click()
        self.assertGreater(again.locator("#appSidebar").bounding_box()["width"], 200)
        self.assertIsNone(again.evaluate("localStorage.getItem('sidebar-rail')"))

    def test_the_rail_keeps_every_link_named_and_shows_the_name_on_hover(self) -> None:
        context = self._context()
        page = self._open(context)
        page.locator("#sbRailToggle").click()

        link = page.locator('.sidebar .nav-a[href="/db-admin/"]')
        # The label is clipped, not removed: it is still the link's name.
        expect(link).to_have_accessible_name("DB Admin Panel")
        self.assertLess(link.locator(".nav-t").bounding_box()["width"], 4)

        tip = page.locator(".sb-tip")
        link.hover()
        expect(tip).to_be_visible()
        expect(tip).to_have_text("DB Admin Panel")

        # Dismissible with Escape (WCAG 1.4.13).
        page.keyboard.press("Escape")
        expect(tip).not_to_be_visible()

        # And it comes up on keyboard focus too, not only under a pointer.
        page.locator('.sidebar .nav-a[href="/audit-explorer/"]').focus()
        expect(tip).to_be_visible()
        expect(tip).to_have_text("Audit Explorer")

    def test_the_rail_shows_every_group_even_when_one_is_collapsed(self) -> None:
        # The group headers are gone in the rail, so a collapsed group would
        # hide its icons with nothing left to re-open it.
        context = self._context()
        page = self._open(context, path="/planner/")
        page.locator('.nav-grp[data-group="insights"] .nav-lbl').click()
        expect(page.locator("#nav-grp-insights")).not_to_be_visible()

        page.locator("#sbRailToggle").click()
        expect(page.locator("#nav-grp-insights")).to_be_visible()
        expect(page.locator('.sidebar .nav-a[href="/audit-explorer/"]')).to_be_visible()

    def test_the_rail_has_no_toggle_on_a_phone(self) -> None:
        # There the sidebar is already a drawer the hamburger opens, and there
        # is no rail to collapse to. Asserted with the DRAWER OPEN: with it
        # shut the whole sidebar is hidden, and the assertion would hold
        # whatever the rule said.
        context = self._context()
        page = self._open(context)
        page.set_viewport_size({"width": 390, "height": 760})
        page.locator(".sidebar-toggle").click()
        expect(page.locator("#navFind")).to_be_visible()
        expect(page.locator("#sbRailToggle")).not_to_be_visible()

    # ── Applied before the first paint ───────────────────────────────────
    def _without_the_module(self, stored: str):
        """A context whose pages never load static/js/sidebar-nav.js. What the
        sidebar then does is the anti-flash block in base.html's <head> and the
        stylesheet, and a stylesheet applies before the first paint — so these
        say the sidebar is never painted open and then snapped shut."""
        context = self._context()
        context.add_init_script(f"try {{ {stored} }} catch (e) {{}}")
        context.route("**/sidebar-nav.js*", lambda route: route.abort())
        return context

    def test_the_head_script_alone_draws_the_rail(self) -> None:
        context = self._without_the_module("localStorage.setItem('sidebar-rail', '1');")
        page = self._open(context, path="/planner/")
        self.assertAlmostEqual(page.locator("#appSidebar").bounding_box()["width"], 64, delta=2)

    def test_the_head_script_alone_draws_a_collapsed_group(self) -> None:
        context = self._without_the_module(
            "localStorage.setItem('sidebar-nav-collapsed', JSON.stringify(['advising']));"
        )
        page = self._open(context, path="/planner/")
        expect(page.locator("#nav-grp-advising")).not_to_be_visible()
        expect(page.locator("#nav-grp-timetabling")).to_be_visible()

    def test_a_tampered_store_cannot_put_anything_into_the_attribute(self) -> None:
        # Without the module, so this is the <head> block's own filter.
        context = self._without_the_module(
            "localStorage.setItem('sidebar-nav-collapsed',"
            " JSON.stringify(['advising', 'x\" onload=\"1', 42, 'Insights']));"
        )
        page = self._open(context, path="/planner/")
        self.assertEqual(
            page.evaluate("document.documentElement.getAttribute('data-sidebar-collapsed')"),
            "advising",
        )

    def test_a_store_that_is_not_a_list_is_simply_ignored(self) -> None:
        context = self._context()
        context.add_init_script(
            "try { localStorage.setItem('sidebar-nav-collapsed', 'not json at all'); } catch (e) {}"
        )
        page = self._open(context, path="/planner/")
        self.assertIsNone(
            page.evaluate("document.documentElement.getAttribute('data-sidebar-collapsed')")
        )
        expect(page.locator("#nav-grp-advising")).to_be_visible()

    # ── Section groups ───────────────────────────────────────────────────
    def test_a_collapsed_group_is_remembered(self) -> None:
        context = self._context()
        page = self._open(context, path="/planner/")
        header = page.locator('.nav-grp[data-group="advising"] .nav-lbl')
        self.assertEqual(header.get_attribute("aria-expanded"), "true")

        header.click()
        self.assertEqual(header.get_attribute("aria-expanded"), "false")
        expect(page.locator("#nav-grp-advising")).not_to_be_visible()
        self.assertEqual(
            page.evaluate("localStorage.getItem('sidebar-nav-collapsed')"), '["advising"]'
        )

        again = self._open(context, path="/planner/")
        expect(again.locator("#nav-grp-advising")).not_to_be_visible()
        self.assertEqual(
            again.locator('.nav-grp[data-group="advising"] .nav-lbl').get_attribute(
                "aria-expanded"
            ),
            "false",
        )

    def test_arriving_at_a_page_never_leaves_its_own_group_collapsed(self) -> None:
        context = self._context()
        page = self._open(context, path="/planner/")
        # Two groups away from this page, and one that holds it.
        page.locator('.nav-grp[data-group="advising"] .nav-lbl').click()
        page.locator('.nav-grp[data-group="timetabling"] .nav-lbl').click()
        self.assertEqual(
            page.evaluate("localStorage.getItem('sidebar-nav-collapsed')"),
            '["advising","timetabling"]',
        )

        # /planner/ is in Timetabling: that group comes back, and stops being
        # remembered as collapsed, so the two never disagree.
        again = self._open(context, path="/planner/")
        expect(again.locator("#nav-grp-timetabling")).to_be_visible()
        expect(again.locator('.sidebar .nav-a.active[href="/planner/"]')).to_be_visible()
        expect(again.locator("#nav-grp-advising")).not_to_be_visible()
        self.assertEqual(
            again.evaluate("localStorage.getItem('sidebar-nav-collapsed')"), '["advising"]'
        )

    # ── The scroll affordance ────────────────────────────────────────────
    def test_the_shadow_shows_only_at_an_edge_with_more_list_beyond_it(self) -> None:
        # Short enough that 33 links cannot all show.
        page = self._open(self._context(height=620), path="/planner/")
        wrap = page.locator("#navWrap")
        page.evaluate("document.getElementById('menu').scrollTop = 0")
        page.wait_for_function("document.getElementById('menu').scrollTop === 0")
        self.assertIsNone(wrap.get_attribute("data-more-above"))
        self.assertIsNotNone(wrap.get_attribute("data-more-below"))

        page.evaluate(
            "const n = document.getElementById('menu'); n.scrollTop = n.scrollHeight - n.clientHeight;"
        )
        page.wait_for_function(
            "const n = document.getElementById('menu'); n.scrollTop >= n.scrollHeight - n.clientHeight - 1"
        )
        self.assertIsNotNone(wrap.get_attribute("data-more-above"))
        self.assertIsNone(wrap.get_attribute("data-more-below"))

    def test_a_list_that_fits_draws_no_shadow(self) -> None:
        # Collapsing every group leaves a list of headers, which fits at any
        # height this suite runs at — no viewport guess to go stale.
        page = self._open(self._context(), path="/planner/")
        for header in page.locator(".nav-grp .nav-lbl").all():
            header.click()
        page.wait_for_function(
            "const n = document.getElementById('menu'); n.scrollHeight - n.clientHeight <= 2"
        )
        wrap = page.locator("#navWrap")
        self.assertIsNone(wrap.get_attribute("data-more-above"))
        self.assertIsNone(wrap.get_attribute("data-more-below"))

    # ── The contrast fix ─────────────────────────────────────────────────
    def test_an_inactive_nav_icon_clears_three_to_one_in_both_themes(self) -> None:
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                page = self._open(self._context(theme=theme))
                self.assertEqual(page.evaluate("document.documentElement.dataset.theme"), theme)
                ratio = page.evaluate(CONTRAST, ".sidebar .nav-a:not(.active) .i")
                self.assertIsNotNone(ratio)
                self.assertGreaterEqual(
                    ratio, MIN_ICON, f"{theme}: an inactive nav icon is {ratio}:1"
                )

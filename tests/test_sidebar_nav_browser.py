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


#: The same arithmetic as CONTRAST, for a colour that is painted ON an element
#: rather than taken from it — a scrollbar thumb has no element of its own.
CONTRAST_ON = """({ selector, colour }) => {
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
  const bg = behind(el);
  const a = lum(over(rgba(colour), bg)), b = lum(bg);
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

    def _open(self, context, path: str = "/planner/", language: str = "en", still: bool = True):
        page = context.new_page()
        page.goto(f"{self.live_server_url}{path}")
        # The language has to be the SERVER's, or an Arabic assertion asserts
        # nothing (and a translated page would read Arabic while declaring en).
        self.assertEqual(page.evaluate("document.documentElement.lang"), language)
        if still:
            # `animation: none` also resets animation-timeline, which is what
            # drives the nav's scroll fade — so the test for that one asks for
            # a page that still moves.
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
        expect(page.locator("#navFind .nav-find-t")).to_have_text("انتقل لصفحة…")

        page.keyboard.press("Control+k")
        field = page.locator("#sbpInput")
        expect(field).to_be_focused()
        # "تخطيط" — the Arabic for Section Planning is "تخطيط الشعب", and
        # Graduation Planning is "تخطيط التخرج".
        # Every destination, before a key is typed: the number the filtered
        # list has to be SMALLER than. Without this the assertions below hold
        # just as well on a palette that filtered nothing at all, because
        # `ranked('')` returns the lot.
        everything = page.locator("#sbpList .sbp-opt").count()
        self.assertGreater(everything, 10)

        field.type("تخطيط")
        options = page.locator("#sbpList .sbp-opt")
        matched = options.count()
        self.assertGreater(matched, 0)
        self.assertLess(matched, everything, "the Arabic query filtered nothing")

        labels = page.locator("#sbpList .sbp-opt .sbp-opt-t").all_text_contents()
        self.assertIn("تخطيط الشعب", labels)
        self.assertIn("تخطيط التخرج", labels)
        # Nothing got in that does not hold the needle, and the characters it
        # matched are the ones marked.
        for label in labels:
            self.assertIn("تخطيط", label, f"{label} does not contain the query")
        self.assertEqual(
            options.first.locator("mark").all_text_contents(),
            ["تخطيط"],
            "the Arabic highlight is not over the matched characters",
        )
        # An English name is not what an Arabic page offers.
        self.assertNotIn("Section Planning", labels)

    def test_the_palette_folds_a_shadda_and_maps_the_highlight_back(self) -> None:
        """Batch Recommender is «الاقتراحات المجمّعة» — with a shadda (U+0651).

        Typing the plain letters has to find it, which is `fold()` stripping
        the mark, and the highlight has to land on the characters the reader
        sees, which is `foldWithMap()` mapping folded indices back. Folding
        SHORTENS this label, so an identity map would mark the wrong run.
        """
        page = self._open(self._context(language="ar"), language="ar")
        page.keyboard.press("Control+k")
        page.locator("#sbpInput").type("المجمعة")

        option = page.locator("#sbpList .sbp-opt").first
        expect(option.locator(".sbp-opt-t")).to_have_text("الاقتراحات المجمّعة")
        # The match is a subsequence, so it marks runs. Joined, they are
        # exactly the letters typed — the shadda sits BETWEEN two runs and is
        # left unmarked, which is only possible if the folded indices were
        # mapped back onto the characters the reader sees.
        marks = option.locator("mark").all_text_contents()
        self.assertEqual("".join(marks), "المجمعة")
        self.assertNotIn("ّ", "".join(marks))

    def test_the_palette_folds_the_alef_forms(self) -> None:
        """Timetable Builder is «إنشاء الجداول», with an alef-hamza-below.

        Nobody types the hamza, so a plain alef has to reach it.

        Asserted on the MARKS, not on the label's presence: the group's own
        name is searched too, so an unfolded query can still drag this page in
        through its section and look like a match. Only a hit on the label
        itself marks characters, and only folding can produce one here.
        """
        page = self._open(self._context(language="ar"), language="ar")
        page.keyboard.press("Control+k")
        page.locator("#sbpInput").type("انشاء")

        options = page.locator("#sbpList .sbp-opt")
        labels = [t.strip() for t in options.locator(".sbp-opt-t").all_text_contents()]
        self.assertIn("إنشاء الجداول", labels)

        marks = options.nth(labels.index("إنشاء الجداول")).locator("mark").all_text_contents()
        self.assertEqual("".join(marks), "إنشاء", "the plain alef did not match the hamza form")

    def test_a_section_name_finds_the_pages_under_it(self) -> None:
        """A page is reachable by the name of the group it sits in.

        "timetabling" is no label's subsequence — after `timetabl` neither
        "Timetable Builder" nor "Timetable Workspace" can supply the `i` and
        then the `n` — so every one of these matches through the section
        fallback, and nothing else.
        """
        page = self._open(self._context())
        page.keyboard.press("Control+k")
        page.locator("#sbpInput").type("timetabling")

        options = page.locator("#sbpList .sbp-opt")
        self.assertGreater(options.count(), 0, "a section name finds nothing")
        sections = set(page.locator("#sbpList .sbp-opt .sbp-opt-sec").all_text_contents())
        self.assertEqual(sections, {"Timetabling"})

    def test_a_label_hit_outranks_a_section_hit(self) -> None:
        """ "adv" is in several labels AND in the Advising section's name.

        The pages that say it themselves come first; the rest of Advising
        follows. That ordering is the `+ 1000` in `ranked()`, and without it
        the two kinds interleave by raw score.
        """
        page = self._open(self._context())
        page.keyboard.press("Control+k")
        page.locator("#sbpInput").type("adv")

        labels = page.locator("#sbpList .sbp-opt .sbp-opt-t").all_text_contents()
        self.assertGreater(len(labels), 1)
        direct = [i for i, t in enumerate(labels) if "adv" in t.lower()]
        indirect = [i for i, t in enumerate(labels) if "adv" not in t.lower()]
        self.assertTrue(direct, "no label matched directly")
        self.assertTrue(indirect, "nothing matched through its section")
        self.assertLess(max(direct), min(indirect), "a section hit was ranked above a label hit")

    def test_the_shortcut_stands_down_where_the_dashboard_already_owns_it(self) -> None:
        # The dashboard's own Ctrl/Cmd+K jumps to a student, course or advisor
        # by id, and has no other way in. The row still opens the palette.
        page = self._open(self._context(), path="/")
        row = page.locator("#navFind")
        self.assertIsNone(row.get_attribute("aria-keyshortcuts"))
        expect(page.locator("#navFind .nav-find-kbd")).to_have_count(0)

        page.keyboard.press("Control+k")
        # Visibility, not `not_to_have_class("d-none")`: given a string that
        # matches the WHOLE class attribute, and the dashboard's is
        # "cmd-backdrop d-none", so it would have passed however the chord went.
        expect(page.locator("#cmdPalette")).to_be_visible()
        expect(page.locator("#cmdInput")).to_be_focused()
        expect(page.locator("#sbp")).to_have_count(0)

        page.keyboard.press("Escape")
        row.click()
        expect(page.locator("#sbp .sbp-panel")).to_be_visible()

    def test_the_search_row_says_the_shortcut_where_it_is_bound(self) -> None:
        page = self._open(self._context())
        row = page.locator("#navFind")
        self.assertEqual(row.get_attribute("aria-keyshortcuts"), "Control+K Meta+K")
        expect(page.locator("#navFind .nav-find-kbd")).to_have_text("Ctrl K")

    def test_the_search_rows_label_is_never_cut_short(self) -> None:
        """The row shares its width with the rail toggle. At the 210px and
        195px sidebar steps the label and the shortcut badge do not both fit,
        and the label was cut to an ellipsis ("Go t…"); in Arabic it was
        cut even at full width. The badge yields: it shows where the label
        fits beside it, and only there."""
        clipped = (
            "() => { const t = document.querySelector('#navFind .nav-find-t');"
            " return t.scrollWidth > t.clientWidth; }"
        )
        settle = (
            "() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))"
        )
        badge_shown_at_full_width = {}
        for language in ("en", "ar"):
            page = self._open(self._context(language=language), language=language)
            badge = page.locator("#navFind .nav-find-kbd")
            for width in (1440, 1100, 900):
                page.set_viewport_size({"width": width, "height": 820})
                page.evaluate(settle)
                self.assertFalse(
                    page.evaluate(clipped),
                    f"the search row's label is cut short at {width}px ({language})",
                )
                if width == 1440:
                    badge_shown_at_full_width[language] = badge.is_visible()
            # Below the first step the English label and the badge cannot share
            # the 40px the label is left with, so the badge must have gone, and
            # it comes back when there is room again.
            if language == "en":
                expect(badge).to_be_hidden()
                page.set_viewport_size({"width": 1440, "height": 820})
                page.evaluate(settle)
                expect(badge).to_be_visible()
        # A badge that simply never showed would pass everything above.
        self.assertTrue(badge_shown_at_full_width["en"], "the badge never shows")

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

    def test_the_rail_centres_its_icons_whatever_the_scrollbar_takes(self) -> None:
        """A browser that reserves a gutter for its scrollbar reserves it at one
        edge, which pushes every icon half a scrollbar off the centre of a 64px
        rail. Where the scrollbar is an overlay there is no gutter and this is
        already true, so it holds either way."""
        page = self._open(self._context(height=620), path="/planner/")
        page.locator("#sbRailToggle").click()
        page.wait_for_function(
            "const n = document.getElementById('menu'); n.scrollHeight - n.clientHeight > 2"
        )
        rail = page.locator("#appSidebar").bounding_box()
        for href in ("/planner/", "/db-admin/", "/#student"):
            icon = page.locator(f'.sidebar .nav-a[href="{href}"] .i').bounding_box()
            self.assertAlmostEqual(
                icon["x"] + icon["width"] / 2,
                rail["x"] + rail["width"] / 2,
                delta=2,
                msg=href,
            )

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

    def test_the_rails_controls_stay_big_enough_to_hit(self) -> None:
        """The search row stacks into a column on the rail.

        A column sizes its children to their content, and the button's
        `flex: 1` buys height there, not width — so the button has to be told
        to stretch or it collapses to its icon. Both controls owe the 24px of
        WCAG 2.5.8, and the button should fill the rail as the nav links do.
        """
        page = self._open(self._context())
        page.locator("#sbRailToggle").click()
        rail = page.locator("#appSidebar")
        self.assertAlmostEqual(rail.bounding_box()["width"], 64, delta=2)

        button = page.locator("#navFind").bounding_box()
        toggle = page.locator("#sbRailToggle").bounding_box()
        self.assertGreaterEqual(button["width"], 24, "the rail's search button is too small")
        self.assertGreaterEqual(button["height"], 24, "the rail's search button is too short")
        self.assertGreaterEqual(toggle["width"], 24)
        self.assertGreaterEqual(toggle["height"], 24)

        # It spans its row — the rail less that row's own 8px gutters — rather
        # than sitting as a lone icon-sized box. (The nav links below sit in
        # `.nav-sec`, which keeps a different gutter, so they are not the
        # yardstick.)
        row = page.locator(".nav-find").bounding_box()
        self.assertAlmostEqual(
            button["width"], row["width"] - 16, delta=2, msg="the button did not stretch"
        )
        # And both are still centred on the rail.
        centre = rail.bounding_box()["x"] + rail.bounding_box()["width"] / 2
        for name, box in (("button", button), ("toggle", toggle)):
            self.assertAlmostEqual(box["x"] + box["width"] / 2, centre, delta=2, msg=name)

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

    # ── The scroll affordance that was already there ───────────────
    def test_the_list_still_fades_at_whichever_edge_has_more_to_scroll(self) -> None:
        """`.nav-sec` carries a scroll-driven mask that fades the edge the list
        continues past (global.css, "G16"). It is pure CSS and it was already
        there; wrapping or re-parenting the scroller would have killed it
        silently, and a second, scripted shadow would have doubled it."""
        page = self._open(self._context(height=620), path="/planner/", still=False)
        nav = page.locator("#menu")
        # The module brings the current page into view, which can leave the
        # list part-scrolled: settle that, then drive the scroller from the top.
        page.wait_for_function(
            "const n = document.getElementById('menu'); n.scrollHeight - n.clientHeight > 2"
        )
        page.evaluate("document.getElementById('menu').scrollTop = 0")
        page.wait_for_function(
            "getComputedStyle(document.getElementById('menu'))"
            ".getPropertyValue('--nav-fade-t').trim() === '0px'"
        )
        # At the top: the bottom edge fades, the top edge does not.
        self.assertEqual(
            nav.evaluate("el => getComputedStyle(el).animationTimeline"), "scroll(self)"
        )
        self.assertEqual(
            nav.evaluate("el => getComputedStyle(el).getPropertyValue('--nav-fade-t').trim()"),
            "0px",
        )
        self.assertNotEqual(
            nav.evaluate("el => getComputedStyle(el).getPropertyValue('--nav-fade-b').trim()"),
            "0px",
        )

        page.evaluate(
            "const n = document.getElementById('menu'); n.scrollTop = n.scrollHeight - n.clientHeight;"
        )
        page.wait_for_function(
            "getComputedStyle(document.getElementById('menu'))"
            ".getPropertyValue('--nav-fade-t').trim() !== '0px'"
        )
        # At the bottom the two have swapped.
        self.assertEqual(
            nav.evaluate("el => getComputedStyle(el).getPropertyValue('--nav-fade-b').trim()"),
            "0px",
        )

    def test_the_nav_scrollbar_is_the_design_system_s_and_not_the_browser_s(self) -> None:
        """A 3px brand pill, not the platform's bar.

        `scrollbar-width: thin` cannot set a width, a radius or a track, so the
        scrollbar is drawn by the ::-webkit-scrollbar pseudo-elements — which a
        browser only honours while `scrollbar-width` and `scrollbar-color` are
        `auto` on that scroller, and `*` sets `scrollbar-width: thin` on
        everything near the top of the sheet. If that reset is ever lost, the
        pseudo-elements stop applying and the platform bar comes back, which is
        what the first two assertions are for. (Firefox has no pseudo-elements
        and keeps the standard properties, under `@supports not selector(...)`.)
        """
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                page = self._open(self._context(theme=theme), path="/planner/")
                nav = page.locator("#menu")
                # The reset that hands this scroller to the pseudo-elements.
                self.assertEqual(nav.evaluate("el => getComputedStyle(el).scrollbarWidth"), "auto")
                self.assertEqual(nav.evaluate("el => getComputedStyle(el).scrollbarColor"), "auto")

                drawn = nav.evaluate(
                    "el => {"
                    " const bar = getComputedStyle(el, '::-webkit-scrollbar');"
                    " const thumb = getComputedStyle(el, '::-webkit-scrollbar-thumb');"
                    " const track = getComputedStyle(el, '::-webkit-scrollbar-track');"
                    " return { width: bar.width, radius: thumb.borderRadius,"
                    "   pad: thumb.borderTopWidth, thumb: thumb.backgroundColor,"
                    "   track: track.backgroundColor,"
                    "   gutter: el.offsetWidth - el.clientWidth }; }"
                )
                # Thin: a 7px gutter with 2px of transparent inset each side, so
                # the pill is 3px, and round rather than square. (A browser
                # snaps a scrollbar border to whole pixels, so 2.5px in 8px
                # would give 4px, not 3.)
                self.assertEqual(drawn["width"], "7px", drawn)
                self.assertEqual(drawn["pad"], "2px", drawn)
                self.assertEqual(drawn["radius"], "99px", drawn)
                self.assertEqual(drawn["track"], "rgba(0, 0, 0, 0)", drawn)
                self.assertLessEqual(drawn["gutter"], 7, drawn)

                # At rest it is a WASH of the brand teal, deliberately quiet —
                # under 3:1, which it may be because it is not what says the
                # list runs on. That is the fade on `.nav-sec` ("G16"), which
                # test_the_list_still_fades_at_whichever_edge_has_more_to_scroll
                # keeps honest.
                resting = page.evaluate(
                    CONTRAST_ON, {"selector": "#menu", "colour": drawn["thumb"]}
                )
                self.assertLess(resting, MIN_ICON, f"{theme}: the resting thumb is {resting}:1")
                # And it is OUR wash, not whatever else paints a quiet thumb:
                # the sheet's global dark ::-webkit-scrollbar rules draw white
                # at 8%, which is just as quiet and just as wrong.
                self.assertEqual(
                    drawn["thumb"],
                    page.evaluate(
                        "(v) => { const d = document.createElement('div');"
                        " d.style.backgroundColor = v; document.body.append(d);"
                        " const c = getComputedStyle(d).backgroundColor; d.remove(); return c; }",
                        page.evaluate(
                            "getComputedStyle(document.documentElement)"
                            ".getPropertyValue('--sb-thumb').trim()"
                        ),
                    ),
                    "the resting thumb is not --sb-thumb",
                )

                # Reach for it and it is the full teal, clear of 3:1, so the
                # control itself is never faint even though its resting state
                # is. `getComputedStyle` on a pseudo-element does NOT follow
                # the host's :hover — it returns the resting value whatever the
                # pointer is doing — so the rule is read from the stylesheet
                # and its token resolved against the page.
                rule = page.evaluate(
                    "() => { for (const sheet of document.styleSheets) {"
                    "   let rules; try { rules = sheet.cssRules; } catch (e) { continue; }"
                    "   for (const r of rules || []) {"
                    "     if (r.selectorText && r.selectorText.includes("
                    "         '.nav-sec:hover::-webkit-scrollbar-thumb'))"
                    "       return r.style.getPropertyValue('background-color');"
                    "   } } return null; }"
                )
                self.assertEqual(rule, "var(--sb-thumb-strong)", "no hover rule for the thumb")

                resolve = (
                    "(name) => { const d = document.createElement('div');"
                    " d.style.color = getComputedStyle(document.documentElement)"
                    "   .getPropertyValue(name).trim();"
                    " document.body.append(d);"
                    " const v = getComputedStyle(d).color; d.remove(); return v; }"
                )
                hovered = page.evaluate(resolve, "--sb-thumb-strong")
                self.assertEqual(
                    hovered, page.evaluate(resolve, "--teal"), "the hover thumb is not the teal"
                )
                ratio = page.evaluate(CONTRAST_ON, {"selector": "#menu", "colour": hovered})
                self.assertGreaterEqual(ratio, MIN_ICON, f"{theme}: the hovered thumb is {ratio}:1")

    # ── What the review of #233 found unpinned ───────────────────────────
    def test_a_save_keeps_the_groups_this_role_does_not_draw(self) -> None:
        """One store serves every role, and a sidebar only draws its own groups.

        Saving from here must not erase the rest. "exams" is drawn for the exam
        committee alone, so a super admin's save is exactly the case that used
        to be able to lose it — and every other test in this file runs as that
        super admin, so nothing else can see this.
        """
        context = self._context()
        context.add_init_script(
            "try { localStorage.setItem('sidebar-nav-collapsed',"
            " JSON.stringify(['exams'])); } catch (e) {}"
        )
        page = self._open(context, path="/planner/")
        expect(page.locator('.nav-grp[data-group="exams"]')).to_have_count(0)

        page.locator('.nav-grp[data-group="advising"] .nav-lbl').click()
        expect(page.locator("#nav-grp-advising")).not_to_be_visible()

        stored = page.evaluate("JSON.parse(localStorage.getItem('sidebar-nav-collapsed') || '[]')")
        self.assertEqual(
            sorted(stored),
            ["advising", "exams"],
            "a save dropped a group this role never drew",
        )

    def test_moving_between_dashboard_panels_reopens_the_group_that_holds_one(self) -> None:
        """The dashboard changes page without changing document.

        Its panels are hash routes, and the sidebar marks the matching link
        active as they switch. Arriving at one has to count as arriving at a
        page, or the link you just landed on stays hidden under a collapsed
        header.
        """
        page = self._open(self._context(), path="/")
        header = page.locator('.nav-grp[data-group="advising"] .nav-lbl')
        header.click()
        self.assertEqual(header.get_attribute("aria-expanded"), "false")
        expect(page.locator("#nav-grp-advising")).not_to_be_visible()

        page.evaluate("location.hash = '#batch'")
        expect(page.locator("#nav-grp-advising")).to_be_visible()
        self.assertEqual(header.get_attribute("aria-expanded"), "true")

    def test_a_badge_repaint_does_not_reopen_a_collapsed_group(self) -> None:
        """The dashboard rewrites its sidebar badges' className after a fetch.

        That is not an arrival. Counting it as one would pull a group the user
        has just collapsed back open — and write it to the store, so it would
        not even stay collapsed on the next load.

        The active link has to be INSIDE the collapsed group for this to say
        anything: `applyGroups(true)` only ever rescues the group holding the
        page you are on, so with the active link elsewhere a repaint treated
        as an arrival would be harmless and the test would pass either way.
        """
        page = self._open(self._context(), path="/")
        page.evaluate("location.hash = '#batch'")
        expect(page.locator('.sidebar .nav-a.active[data-target="batch"]')).to_have_count(1)

        header = page.locator('.nav-grp[data-group="advising"] .nav-lbl')
        header.click()
        expect(page.locator("#nav-grp-advising")).not_to_be_visible()

        changed = page.evaluate(
            "() => { const b = document.querySelector('#menu .nav-badge');"
            " if (!b) return false; b.className = b.className + ' sb-badge-repaint';"
            " return true; }"
        )
        self.assertTrue(changed, "no sidebar badge to repaint")
        # This asserts that nothing happens, so it has to give "something"
        # room to happen first. Two animation frames outlast the observer's
        # microtask and would outlast a rAF-deferred one too — a wall-clock
        # wait would quietly start passing if the handling were ever deferred.
        page.evaluate(
            "() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))"
        )

        expect(page.locator("#nav-grp-advising")).not_to_be_visible()
        self.assertEqual(header.get_attribute("aria-expanded"), "false")
        self.assertEqual(
            page.evaluate("localStorage.getItem('sidebar-nav-collapsed')"), '["advising"]'
        )

    def test_the_drawer_moves_focus_to_something_the_layout_renders(self) -> None:
        """The drawer calls itself a modal dialog, so it owes focus to its own
        contents. The first candidate in document order is not always one the
        layout draws — the rail toggle is display:none at this width, and
        focus() on a hidden element goes nowhere at all."""
        page = self._open(self._context())
        page.set_viewport_size({"width": 390, "height": 760})
        page.locator(".sidebar-toggle").click()

        focused = page.locator("#appSidebar :focus")
        expect(focused).to_have_count(1)
        self.assertTrue(
            page.evaluate("document.activeElement.offsetParent !== null"),
            "the drawer gave focus to an element it does not draw",
        )

    def test_the_drawer_passes_over_controls_it_does_not_show(self) -> None:
        """The test above holds today whatever the drawer does, because its
        first candidate in document order happens to be drawn. Here the first
        two are not: one has no box (display:none), the other has a box it
        does not show (visibility:hidden), and neither can take focus."""
        page = self._open(self._context())
        page.set_viewport_size({"width": 390, "height": 760})
        page.evaluate(
            "() => { const s = document.getElementById('appSidebar');"
            " for (const [id, css] of [['sbGhostB', 'visibility:hidden'], ['sbGhostA', 'display:none']]) {"
            " const b = document.createElement('button'); b.type = 'button'; b.id = id;"
            " b.textContent = id; b.style.cssText = css; s.prepend(b); } }"
        )
        page.locator(".sidebar-toggle").click()

        focused = page.locator("#appSidebar :focus")
        expect(focused).to_have_count(1)
        self.assertNotIn(focused.get_attribute("id"), ("sbGhostA", "sbGhostB"))

    def test_the_palettes_status_region_lives_inside_the_dialog(self) -> None:
        """`aria-modal="true"` tells assistive technology to ignore everything
        outside the dialog, which included the live region reporting how many
        pages matched."""
        page = self._open(self._context())
        page.keyboard.press("Control+k")
        expect(page.locator("#sbp .sbp-panel #sbpCount")).to_have_count(1)

        page.locator("#sbpInput").type("secpl")
        expect(page.locator("#sbpCount")).to_have_text("1 page matches")

    def test_the_chosen_row_does_not_resize_the_list_in_forced_colours(self) -> None:
        """In forced colours the chosen row is marked with an outline, not a
        border: a border is laid out, so every arrow key grew the row by 2px
        and shunted the rows under the pointer."""
        context = self.browser.new_context(
            viewport={"width": 1440, "height": 820}, forced_colors="active"
        )
        self.addCleanup(context.close)
        context.add_cookies(
            [
                {
                    "name": settings.SESSION_COOKIE_NAME,
                    "value": self.session,
                    "url": self.live_server_url,
                }
            ]
        )
        page = context.new_page()
        page.goto(f"{self.live_server_url}/planner/")
        page.add_style_tag(
            content="*, *::before, *::after { transition: none !important; animation: none !important; }"
        )
        page.keyboard.press("Control+k")
        rows = page.locator("#sbpList .sbp-opt")
        expect(rows.first).to_be_visible()
        self.assertGreater(rows.count(), 2)

        first = rows.nth(0).bounding_box()["height"]
        page.keyboard.press("ArrowDown")
        self.assertEqual(rows.nth(1).get_attribute("aria-selected"), "true")
        after = rows.nth(0).bounding_box()["height"]
        self.assertAlmostEqual(
            first, after, delta=0.5, msg="the row changed height when it lost the selection"
        )

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

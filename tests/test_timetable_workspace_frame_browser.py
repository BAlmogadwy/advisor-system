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
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings

from tests import one_teacher_support as fx
from tests.test_exam_rosters_browser import WIDE_FONT
from tests.timetable_workspace_browser_support import WorkspaceBrowserMixin

pytest.importorskip("playwright.sync_api")

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


class WorkspaceFrameBrowserTests(WorkspaceBrowserMixin, StaticLiveServerTestCase):
    maxDiff = None

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
        # later answers widen the buttons before it ("N left", flags). And at
        # every desktop width, in the wide font, no control of the bar is cut
        # off: from 1201px the bar is one row (the strip gives way; it used to
        # wrap at 1366 and 1440), below it the bar may wrap but never clips.
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
        problems: dict[str, list] = {}
        for language in ("en", "ar"):
            for width in (1025, 1100, 1201, 1366, 1440):
                context = self.browser.new_context(
                    locale="ar" if language == "ar" else "en-US",
                    extra_http_headers={"Accept-Language": language},
                    viewport={"width": width, "height": 900},
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
                context.add_init_script(WIDE_FONT)
                page = context.new_page()
                page.goto(f"{self.live_server_url}{self.path}")
                page.wait_for_function(
                    f"document.querySelector('#twBoardTabs [aria-current=\"true\"]')?.dataset.boardId === '{last.id}'"
                    " && /\\d/.test(document.getElementById('twPublishLabel').textContent)"
                )
                # A long filter count, as when every meeting matches.
                page.evaluate(
                    "document.getElementById('twSearchCount').textContent = '1234 of 1234'"
                )
                page.wait_for_timeout(600)
                found = page.evaluate(
                    """(oneRow) => {
                      const bad = [];
                      const bar = document.getElementById('twCmdBar');
                      const main = document.getElementById('main-content').getBoundingClientRect();
                      const strip = document.getElementById('twBoardTabs');
                      const chosen = strip.querySelector('[aria-current="true"]').getBoundingClientRect();
                      const s = strip.getBoundingClientRect();
                      // As much of the chosen board as the strip can show: a board
                      // wider than a narrow strip cannot be wholly inside it, and
                      // the strip's own padding (3px each side) is never covered.
                      const seen = Math.min(chosen.right, s.right) - Math.max(chosen.left, s.left);
                      if (seen < Math.min(chosen.width, s.width - 8)) bad.push(`chosen board out of view (${Math.round(seen)}px of ${Math.round(chosen.width)}px seen; strip ${Math.round(s.width)}px)`);
                      if (bar.scrollWidth > bar.clientWidth + 1) bad.push(`bar overflows ${bar.scrollWidth - bar.clientWidth}px`);
                      const shown = [...bar.children].filter(el => el.getClientRects().length);
                      for (const el of shown) {
                        const r = el.getBoundingClientRect();
                        if (r.width && (r.left < main.left - 1 || r.right > main.right + 1)) bad.push(`${el.id || el.className} cut off`);
                      }
                      const centres = shown.filter(el => el.getBoundingClientRect().height > 20)
                        .map(el => { const r = el.getBoundingClientRect(); return r.top + r.height / 2; });
                      const spread = Math.max(...centres) - Math.min(...centres);
                      if (oneRow && spread > 2) bad.push(`not one row (centres ${Math.round(spread)}px apart)`);
                      if (document.getElementById('main-content').scrollTop) bad.push('the page scrolled');
                      return bad;
                    }""",
                    width >= 1201,
                )
                if found:
                    problems[f"{language} {width}"] = found
                context.close()
        self.assertEqual(problems, {})

    def test_every_column_of_a_wide_table_is_drawn(self) -> None:
        """Eight lab windows (five labs, three online) need 824px; at 1366 and
        1440 the board is narrower. The board scrolls sideways (never the
        page), the day column stays, and no column is cut off."""
        from core.services.timetable_autoplace import DEFAULT_LAB_SLOTS

        self.scenario.lab_slot_config = [dict(slot) for slot in DEFAULT_LAB_SLOTS]
        self.scenario.save(update_fields=["lab_slot_config"])
        self.assertEqual(len(DEFAULT_LAB_SLOTS), 8)
        problems: dict[str, list] = {}
        for language in ("en", "ar"):
            for width, height in ((1366, 650), (1440, 790)):
                page, sent, errors = self._load(language, "light", width, height)
                found = page.evaluate(
                    """() => {
                      const bad = [];
                      const lab = document.querySelector('.tw-lab-grid');
                      if (!lab) return ['no lab table'];
                      const box = lab.getBoundingClientRect();
                      const heads = [...lab.querySelectorAll('.tw-grid-header')];
                      if (heads.length !== 9) bad.push(`${heads.length} lab headers`);
                      for (const h of heads) {
                        const r = h.getBoundingClientRect();
                        if (r.left < box.left - 1 || r.right > box.right + 1) bad.push(`column cut: ${h.textContent.trim().slice(0, 14)}`);
                        if (r.width < 95.5 && h.querySelector('.tw-slot-time')) bad.push(`column ${Math.round(r.width)}px`);
                      }
                      const centre = document.getElementById('twGridWrap');  // the board's scroller
                      if (box.width > centre.clientWidth && centre.scrollWidth <= centre.clientWidth) bad.push('the board cannot scroll to it');
                      if (document.documentElement.scrollWidth > innerWidth + 0.5) bad.push('the page scrolls sideways');
                      return bad;
                    }"""
                )
                found += [*sent, *errors]
                if found:
                    problems[f"{language} {width}"] = found
        self.assertEqual(problems, {})

    def test_a_seven_character_code_with_its_marks_fits_a_slot(self) -> None:
        """The card's ~70px at a 96px slot, in the wide font. The code is never cut
        by its head, the room word is never cut, every mark stays on its card's
        meta lines — with a lock, a critical tone, and on a section another
        scenario also places, all three marks at once."""
        from core.models import DeliveryBoard, SectionPlacement, TimetableScenario

        key = fx.requirement("AI", "MATH101", "Calculus")
        fx.budget(self.scenario, key, 1)
        board = self.scenario.boards.first()
        math = fx.section(self.scenario, key, "S1")
        fx.place(
            board,
            math,
            (("SUN", "10:30", "11:45"), ("TUE", "10:30", "11:45")),
            locked=True,
            room="UNASSIGNED",
        )
        # Another scenario also places MATH101 S1: it is shared, a third mark.
        other = TimetableScenario.objects.create(academic_year="1448", term="2", name="Other")
        other_board = DeliveryBoard.objects.create(
            scenario=other, label="Other", nominal_term=2, program="AI"
        )
        SectionPlacement.objects.create(
            board=other_board, term_section=math, day="SUN", start_time="10:30", end_time="11:45"
        )
        problems: dict[str, list] = {}
        for language in ("en", "ar"):
            for theme in ("light", "dark"):
                for width, height in ((1366, 650), (1440, 790)):
                    page, sent, errors = self._load(language, theme, width, height, cards=6)
                    found = page.evaluate(
                        """() => [...document.querySelectorAll('.tw-card')].flatMap(card => {
                          const bad = [];
                          const head = card.querySelector('.cc').getBoundingClientRect();
                          const code = card.querySelector('.tw-card-code');
                          const label = code.textContent;
                          const c = code.getBoundingClientRect();
                          // The head clips: the code's own box must lie inside it.
                          if (c.left < head.left - 0.5 || c.right > head.right + 0.5) bad.push(`${label}: code cut`);
                          const room = card.querySelector('.tw-card-room');
                          if (room && room.scrollWidth > room.clientWidth + 0.5) bad.push(`${label}: room cut`);
                          const meta = card.querySelector('.ci').getBoundingClientRect();
                          for (const mark of card.querySelectorAll('.tw-card-marks > :not([hidden])')) {
                            const m = mark.getBoundingClientRect();
                            if (m.width < 11 || m.left < meta.left - 0.5 || m.right > meta.right + 0.5
                                || m.top < meta.top - 0.5 || m.bottom > meta.bottom + 0.5) bad.push(`${label}: mark cut`);
                          }
                          return bad;
                        })"""
                    )
                    marks = page.evaluate(
                        "['lock', 'shared'].map(i => document.querySelectorAll(`.tw-card [data-icon=${i}]`).length).join(':')"
                        " + ':' + document.querySelectorAll('.tw-card .tw-card-tone[data-tone=critical]:not([hidden])').length"
                    )
                    if marks != "2:2:2":
                        found.append(f"marks {marks}")
                    found += page.evaluate(
                        CONTRAST, [".tw-card-code", ".tw-card-room", ".ux-legend-item"]
                    )
                    found += [*sent, *errors]
                    if found:
                        problems[f"{language} {theme} {width}"] = found
        self.assertEqual(problems, {})

    def test_a_disclosure_chevron_points_the_way_its_group_opens(self) -> None:
        """The global disclosure: open points down; closed points along the
        reading direction, right in English and left in Arabic. Read from the
        rendered transform and the sides the chevron draws, so the check holds
        however the stylesheet draws it."""
        seen = {}
        for language in ("en", "ar"):
            page, sent, errors = self._load(language, "light", 1440, 900)
            seen[language] = page.evaluate(
                """() => {
                  const host = document.createElement('div');
                  host.innerHTML = '<button type="button" class="ux-disclosure-toggle" aria-expanded="true"'
                    + ' aria-controls="uxChevronProbe"><span class="ux-disclosure-label">Probe</span>'
                    + '<span class="ux-disclosure-chevron"></span></button><div id="uxChevronProbe"></div>';
                  document.querySelector('main, body').appendChild(host);
                  const toggle = host.querySelector('button');
                  const chevron = host.querySelector('.ux-disclosure-chevron');
                  const way = () => {
                    const style = getComputedStyle(chevron);
                    const drawn = side => parseFloat(style[`border${side}Width`]) > 0;
                    // The chevron's point: the corner where its two borders meet.
                    const x = drawn('Right') ? 1 : drawn('Left') ? -1 : 0;
                    const y = drawn('Bottom') ? 1 : drawn('Top') ? -1 : 0;
                    const m = new DOMMatrix(style.transform === 'none' ? undefined : style.transform);
                    const px = m.a * x + m.c * y, py = m.b * x + m.d * y;
                    return Math.abs(py) > Math.abs(px) ? (py > 0 ? 'down' : 'up') : (px > 0 ? 'right' : 'left');
                  };
                  const open = way();
                  toggle.click();  // the design system closes it
                  const result = [open, toggle.getAttribute('aria-expanded'), way()];
                  host.remove();
                  return result;
                }"""
            )
            self.assertEqual([*sent, *errors], [])
        self.assertEqual(seen, {"en": ["down", "false", "right"], "ar": ["down", "false", "left"]})

    def test_a_button_switched_off_while_it_has_the_keyboard_gets_it_back(self) -> None:
        """The inspector's Lock, pressed from the keyboard: the page switches its
        controls off while the write is in flight (the browser lets go of the
        focused button) and draws the panel again; the keyboard ends on the
        same control, now "Unlock time"."""
        page, sent, errors = self._load("en", "light", 1440, 900)
        page.click(".tw-card")
        page.click("#twTabSelection")
        lock = '#twRightBody [data-focus-key="lock"]'
        page.focus(lock)
        page.keyboard.press("Enter")
        page.wait_for_function(
            f"document.querySelector('{lock}')?.textContent.trim() === 'Unlock time'"
            f" && !document.querySelector('{lock}').disabled"
        )
        page.wait_for_timeout(100)
        self.assertEqual(
            page.evaluate("document.activeElement.dataset.focusKey || document.activeElement.id"),
            "lock",
        )
        self.assertEqual(errors, [])
        self.assertTrue(any("/lock/" in request for request in sent), sent)

    @override_settings(TIMETABLE_SECTION_PROGRAMME_PREFERENCE=True)
    def test_a_keyboard_mark_choice_saves_once_through_a_redraw(self) -> None:
        """Chromium blurs a focused select as a redraw removes it. A keyboard
        choice is not "left" then: it is not saved in the middle of the redraw,
        its wait ends in one save, and the select ends enabled, showing it,
        with the keyboard."""
        from core.models import TermSection

        # A marked section: its select offers "Any" and its mark (the fixture
        # seats no students, so the lens names no other programme).
        TermSection.objects.filter(scenario=self.scenario).update(preferred_programme="AI")
        page, sent, errors = self._load("en", "light", 1440, 900)
        page.click(".tw-card")
        page.click("#twTabSelection")
        page.wait_for_selector("#twSelProgramme:not([disabled])")
        page.evaluate("window.TimetableProgrammeMark.settleMs = 400")
        page.focus("#twSelProgramme")
        page.keyboard.press("ArrowUp")
        chosen = page.evaluate("document.getElementById('twSelProgramme').value")
        self.assertEqual(chosen, "", "the arrow key chose Any")
        # An answer arrives before the keys rest: the panel is drawn anew.
        page.evaluate("TW.dataVersion++; renderRightPanel('selection')")
        page.wait_for_timeout(100)
        posts = [r for r in sent if "programme-preference" in r]
        self.assertEqual(posts, [], "nothing is saved in the middle of a redraw")
        page.wait_for_function(
            "TW.placements.find(p => p.id === TW.selectedPlacementId).preferred_programme === ''"
            " && !document.getElementById('twSelProgramme').disabled"
        )
        page.wait_for_timeout(300)
        self.assertEqual(len([r for r in sent if "programme-preference" in r]), 1)
        self.assertEqual(
            page.evaluate(
                "[document.getElementById('twSelProgramme').value, document.activeElement.id]"
            ),
            [chosen, "twSelProgramme"],
        )
        self.assertEqual(errors, [])

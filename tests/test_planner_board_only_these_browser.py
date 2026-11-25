"""The new planner screen, "Only these": several allowed sections for one course, in a real browser.

The detail panel offers "Any section" or "Only these" with a tick per section (one to six), the
table says what was allowed, and while the panel is open the week draws each ticked section the week
does not already show solid as a dashed "alt" block. The same synthetic student and fakes as the
other board tests (tests/planner_board_support.py).

AI331 has nine sections here, all usable (the registered week is on Sun/Tue mornings and Mon/Wed
at 12:45): M1..M9 are term sections 21..29. Option B1 puts AI331 in M2 (22), B2 in M3 (23).
"""

from __future__ import annotations

import re

from tests.planner_board_support import (
    OVERFLOWS,
    PlannerBoardCase,
    _mapping,
    _option,
    _section,
    board_fake,
    expect,
)

STALE = "The plan changed since this build. Rebuild to apply."
SECTIONS = [
    _section(21, "AI331", "M1", ["THU"], "08:00", "09:15", 20, 40),
    _section(22, "AI331", "M2", ["THU"], "10:00", "11:15", 20, 40),
    _section(23, "AI331", "M3", ["MON", "WED"], "15:00", "16:15", 20, 40),
    _section(24, "AI331", "M4", ["THU"], "12:00", "13:15", 20, 40),
    _section(25, "AI331", "M5", ["THU"], "14:00", "15:00", 20, 40),
    _section(26, "AI331", "M6", ["THU"], "16:00", "17:15", 20, 40),
    _section(27, "AI331", "M7", ["SAT"], "09:00", "10:15", 20, 40),
    _section(28, "AI331", "M8", ["THU"], "09:30", "10:45", 20, 40),
    _section(29, "AI331", "M9", ["THU"], "10:00", "11:15", 20, 40),  # the time of M2
    _section(13, "STAT305", "M1", ["MON", "WED"], "08:00", "09:15", 30, 40),
    _section(14, "CS351", "M1", ["THU"], "09:00", "11:30", 37, 40),
    _section(15, "AI342", "M1", ["SUN"], "13:00", "14:15", None, None),
]
CATALOG = {"ok": True, "count": len(SECTIONS), "program_sections_only": True, "sections": SECTIONS}
AI342 = _mapping("AI342", "M1", 15, ["SUN"], "13:00", "14:15")
B1 = _option("B1", [_mapping("AI331", "M2", 22, ["THU"], "10:00", "11:15"), AI342])
B2 = _option("B2", [_mapping("AI331", "M3", 23, ["MON", "WED"], "15:00", "16:15"), AI342])
ONE_TO_SIX = (21, 22, 23, 24, 25, 26)

SNAPSHOT = """() => {
  const text = (n) => n.textContent.replace(/\\s+/g, ' ').trim();
  return {
    feet: [...document.querySelectorAll('#plbWeek .plb-df')].map(text),
    free: [...document.querySelectorAll('#plbWeek .plb-free')].map((n) => n.style.gridColumn),
    notes: [...document.querySelectorAll('#plbWeek .plb-note')].map(text),
    name: text(document.getElementById('plbWeekName')),
    strip: text(document.getElementById('plbStrip')),
    tabs: [...document.querySelectorAll('#plbTabs [role=tab]')].map(
      (t) => (t.dataset.opt || '(registered)') + ':' + t.getAttribute('aria-selected')),
    solid: [...document.querySelectorAll('#plbWeek .plb-b')].map(
      (b) => b.dataset.code + ' ' + b.dataset.kind + ' ' + b.style.gridColumn),
  };
}"""


class PlannerBoardOnlyTheseTests(PlannerBoardCase):
    # ── helpers ──────────────────────────────────────────────────────────────
    def _page(self, **kwargs):
        fake = board_fake([B1, B2], catalog=CATALOG)
        return self._open(fake, **kwargs), fake

    def _panel(self, page, code: str = "AI331") -> None:
        page.click(f"#plbTable tr[data-code='{code}'] button[data-act='detail']")
        expect(page.locator("#plbDetail")).to_be_visible()
        expect(page.locator("#plbDetailTitle")).to_contain_text(code)

    @staticmethod
    def _box(page, tsid: int):
        return page.locator(f"#plbDetail input[type='checkbox'][value='{tsid}']")

    def _tick(self, page, *tsids: int) -> None:
        for tsid in tsids:
            self._box(page, tsid).check()

    def _build(self, page) -> None:
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text(re.compile("Rebuild|أعد البناء"))
        expect(page.locator("#plbBuild")).to_be_enabled()
        expect(page.locator("#plbBuildLine")).to_have_text("")

    @staticmethod
    def _pins(fake, n: int, code: str = "AI331"):
        item = [c for c in fake.build_bodies[n]["shortlist"] if c["course_code"] == code][0]
        return item.get("pinned_sections", "absent")

    @staticmethod
    def _alts(page) -> list[str]:
        return page.eval_on_selector_all(
            "#plbWeek .plb-gb",
            "bs => bs.map(b => [...b.children].map(c => c.textContent.trim()).join('|'))",
        )

    def _line(self, page, code: str = "AI331") -> str:
        return self._cell(page, code)

    def _status(self, page) -> str:
        return self._text(page, "#plbStatus")

    @staticmethod
    def _checked(page, selector: str) -> bool:
        return page.locator(selector).is_checked()

    # ── the request ──────────────────────────────────────────────────────────
    def test_the_request_carries_every_tick_in_section_order_and_no_key_for_none(self) -> None:
        page, fake = self._page()
        self._panel(page)
        self._tick(page, 25, 21, 23)  # ticked out of order
        self._build(page)
        self.assertEqual(
            self._pins(fake, 0),
            [
                {"term_section_id": 21, "section": "M1"},
                {"term_section_id": 23, "section": "M3"},
                {"term_section_id": 25, "section": "M5"},
            ],
        )
        for entry in self._pins(fake, 0):
            self.assertIsInstance(entry["term_section_id"], int)
        # One tick.
        self._box(page, 21).uncheck()
        self._box(page, 23).uncheck()
        self._build(page)
        self.assertEqual(self._pins(fake, 1), [{"term_section_id": 25, "section": "M5"}])
        # Two ticks.
        self._tick(page, 22)
        self._build(page)
        self.assertEqual(
            self._pins(fake, 2),
            [{"term_section_id": 22, "section": "M2"}, {"term_section_id": 25, "section": "M5"}],
        )
        # Six ticks.
        self._tick(page, *ONE_TO_SIX)
        self._build(page)
        self.assertEqual(
            self._pins(fake, 3),
            [{"term_section_id": t, "section": f"M{t - 20}"} for t in ONE_TO_SIX],
        )
        # None: the key is not sent at all, and the other course never had one.
        for tsid in ONE_TO_SIX:
            self._box(page, tsid).uncheck()
        self._build(page)
        self.assertEqual(self._pins(fake, 4), "absent")
        self.assertEqual(self._pins(fake, 3, "AI342"), "absent")

    # ── the mode ─────────────────────────────────────────────────────────────
    def test_the_two_modes_a_tick_selects_only_these_and_any_section_clears(self) -> None:
        page, fake = self._page()
        self._panel(page)
        any_, only = (
            "#plbDetail input[type='radio'][value='any']",
            "#plbDetail input[type='radio'][value='only']",
        )
        self.assertEqual(page.locator("#plbDetail input[type='radio']").count(), 2)
        self.assertEqual(
            self._text(page, "#plbDetail label:has(input[value='only'])"), "Only these"
        )
        self.assertTrue(self._checked(page, any_))
        self.assertFalse(self._checked(page, only))
        # Every row carries a box in either mode; nothing is ticked and no sentence is shown yet.
        self.assertEqual(
            page.locator("#plbDetail input[type='checkbox'][name='plbOnly']").count(), 9
        )
        self.assertEqual(page.locator("#plbDetail .plb-dt-note").count(), 0)
        # Choosing "Only these" with nothing ticked says in words what that means; nothing changes.
        page.locator(only).check()
        self.assertTrue(self._checked(page, only))
        self.assertEqual(
            self._text(page, "#plbDetail .plb-dt-note"),
            "Tick the sections the build may use. Until one is ticked, any section may be used.",
        )
        self.assertEqual(self._line(page), "Any section")
        # A tick: one gesture, and the mode follows.
        self._tick(page, 22)
        self.assertTrue(self._checked(page, only))
        self.assertFalse(self._checked(page, any_))
        self.assertEqual(
            self._text(page, "#plbDetail .plb-dt-note"),
            "The build picks one of the ticked sections.",
        )
        self.assertEqual(page.locator("#plbDetail .plb-dt-note").count(), 1)
        # From "Any section" a tick selects "Only these" too (a reopened panel is "Only these" while ticked).
        page.click("#plbDetail [data-act='dt-close']")
        self._panel(page)
        self.assertTrue(self._checked(page, only))
        self.assertTrue(self._box(page, 22).is_checked())
        # "Any section" clears every tick.
        page.locator(any_).check()
        self.assertEqual(page.locator("#plbDetail input[name='plbOnly']:checked").count(), 0)
        self.assertTrue(self._checked(page, any_))
        self.assertEqual(page.locator("#plbDetail .plb-dt-note").count(), 0)
        self.assertEqual(self._line(page), "Any section")
        self._build(page)
        self.assertEqual(self._pins(fake, 0), "absent")
        # A panel opened on a course with no ticks starts on "Any section", whatever it did before.
        page.locator(only).check()
        page.click("#plbDetail [data-act='dt-close']")
        self._panel(page)
        self.assertTrue(self._checked(page, any_))

    def test_six_ticked_disables_the_rest_and_a_seventh_never_reaches_the_request(self) -> None:
        page, fake = self._page()
        self._panel(page)
        self._tick(page, *ONE_TO_SIX[:5])
        self.assertEqual(page.locator("#plbDetail .plb-dt-note").count(), 1)
        self.assertEqual(page.locator("#plbDetail input[name='plbOnly']:disabled").count(), 0)
        self._tick(page, 26)
        for tsid in (27, 28, 29):
            self.assertTrue(self._box(page, tsid).is_disabled(), tsid)
        for tsid in ONE_TO_SIX:
            self.assertTrue(self._box(page, tsid).is_enabled(), tsid)
        self.assertEqual(
            self._text(page, "#plbDetail .plb-dt-note:last-of-type"), "Six sections at most."
        )
        # Even a change event that gets past the disabled box changes nothing.
        page.evaluate(
            """() => {
              const box = document.querySelector("#plbDetail input[value='27']");
              box.disabled = false; box.checked = true;
              box.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        self.assertEqual(page.locator("#plbDetail input[name='plbOnly']:checked").count(), 6)
        self.assertFalse(self._box(page, 27).is_checked())
        self._build(page)
        self.assertEqual(len(self._pins(fake, 0)), 6)
        # Unticking one frees the others and drops the line.
        self._box(page, 23).uncheck()
        for tsid in (27, 28, 29):
            self.assertTrue(self._box(page, tsid).is_enabled(), tsid)
        self.assertNotIn("Six sections", self._text(page, "#plbDetail"))

    # ── sections that cannot be used ─────────────────────────────────────────
    def test_a_section_that_cannot_be_used_cannot_be_ticked_and_a_tick_that_became_so_stays(
        self,
    ) -> None:
        fake = board_fake([B1, B2], catalog=CATALOG)
        sections = [
            _section(31, "AI331", "M1", ["SUN", "TUE"], "10:00", "11:15", 20, 40),  # clashes CS322
            _section(32, "AI331", "M2", ["THU"], "10:00", "11:15", 40, 40),  # full
            _section(33, "AI331", "M3", ["THU"], "12:00", "13:15", 20, 40),
            *[s for s in SECTIONS if s["course_key"] != "AI331"],
        ]
        fake.catalog_payload = {**CATALOG, "sections": sections}
        page = self._open(fake)
        self._panel(page)
        self.assertTrue(self._box(page, 31).is_disabled())
        self.assertTrue(self._box(page, 32).is_disabled())
        self.assertFalse(self._box(page, 33).is_disabled())
        self.assertIn(
            "clashes with a registered class", self._text(page, "#plbDetail [data-tsid='31']")
        )
        self.assertIn("full sections are off", self._text(page, "#plbDetail [data-tsid='32']"))
        # A change that gets past the disabled box changes nothing.
        page.evaluate(
            """() => {
              const box = document.querySelector("#plbDetail input[value='31']");
              box.disabled = false; box.checked = true;
              box.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        self.assertEqual(self._line(page), "Any section")
        self.assertEqual(page.locator("#plbDetail input[name='plbOnly']:checked").count(), 0)
        # Allow both, tick both; then take the allowance away: the ticks stay, flagged, and can go.
        self._rules(page, full=True, mode="ignore")
        self._tick(page, 31, 32)
        self._rules(page, full=False, mode="keep")
        for tsid in (31, 32):
            self.assertTrue(self._box(page, tsid).is_checked())
            self.assertTrue(self._box(page, tsid).is_enabled())
            self.assertIn(
                "Ticked, but it can no longer be used.",
                self._text(page, f"#plbDetail [data-tsid='{tsid}']"),
            )
        self.assertEqual(self._line(page), "Only §M1 or §M2")
        self._box(page, 31).uncheck()
        self.assertFalse(self._box(page, 31).is_checked())
        self.assertTrue(self._box(page, 31).is_disabled())
        self.assertEqual(self._line(page), "Only §M2")
        self._build(page)
        self.assertEqual(self._pins(fake, 0), [{"term_section_id": 32, "section": "M2"}])

    def test_a_tick_missing_from_the_new_list_is_shown_flagged_and_can_be_unticked(self) -> None:
        page, fake = self._page()
        self._panel(page)
        self._tick(page, 22, 23)
        fake.catalog_payload = {
            **CATALOG,
            "sections": [s for s in SECTIONS if s["term_section_id"] != 22],
        }
        self._rules(page, prog=False)  # reloads the catalogue
        gone = "#plbDetail [data-tsid='22']"
        expect(page.locator(gone)).to_contain_text(
            "Ticked, but it is not in the current section list."
        )
        self.assertTrue(self._box(page, 22).is_checked())
        self.assertEqual(self._line(page), "Only §M2 or §M3")
        self._build(page)
        self.assertEqual(
            self._pins(fake, 0),
            [{"term_section_id": 22, "section": "M2"}, {"term_section_id": 23, "section": "M3"}],
        )
        self._box(page, 22).click()
        self.assertEqual(page.locator(gone).count(), 0)
        page.click("#plbTabRegistered")
        self.assertEqual(self._line(page), "Only §M3")

    # ── what the table and the status line say ───────────────────────────────
    def test_the_table_line_and_the_status_line_on_the_registered_tab(self) -> None:
        page, _ = self._page()
        self._panel(page)
        self.assertEqual(self._line(page), "Any section")
        self._tick(page, 21)
        self.assertEqual(self._line(page), "Only §M1")
        self.assertIn("AI331: only §M1 may be used.", self._status(page))
        self._tick(page, 22)
        self.assertEqual(self._line(page), "Only §M1 or §M2")
        self.assertIn("AI331: only §M1 or §M2 may be used.", self._status(page))
        self._tick(page, 23)
        self.assertEqual(self._line(page), "Only 3 sections")
        self.assertIn("AI331: only 3 sections may be used.", self._status(page))
        self._tick(page, 24)
        self.assertEqual(self._line(page), "Only 4 sections")
        for tsid in (21, 22, 23, 24):
            self._box(page, tsid).uncheck()
        self.assertEqual(self._line(page), "Any section")
        self.assertIn("AI331: any section may be used.", self._status(page))
        self.assertNotIn("Pinned", self._text(page, "#plbTable"))

    def test_the_table_line_on_an_options_tab_says_what_it_did_and_what_else_was_allowed(
        self,
    ) -> None:
        page, _ = self._page()
        self._panel(page)
        self._tick(page, 22)
        self._build(page)
        self._pick(page, "B1")
        self.assertEqual(self._line(page), "§M2")  # the only tick, which is the one it used
        self._tick(page, 21)
        self.assertEqual(self._line(page), "§M2 · or §M1")
        self.assertEqual(page.locator("#plbTable tr[data-code='AI331'] .plb-or").count(), 1)
        self._tick(page, 24)
        self.assertEqual(self._line(page), "§M2 · or 2 others")
        # B2 used M3, which is not ticked: it says what it did and adds nothing.
        self._pick(page, "B2")
        self.assertEqual(self._line(page), "§M3")
        self.assertEqual(page.locator("#plbTable tr[data-code='AI331'] .plb-or").count(), 0)
        # Ticked with others: "or" names the others.
        self._tick(page, 23)
        self.assertEqual(self._line(page), "§M3 · or 3 others")
        # The Registered tab reads the allowance again.
        page.click("#plbTabRegistered")
        self.assertEqual(self._line(page), "Only 4 sections")

    # ── the week ─────────────────────────────────────────────────────────────
    def test_dashed_blocks_for_the_open_course_only_and_only_what_is_not_solid(self) -> None:
        page, _ = self._page()
        self._panel(page)
        self.assertEqual(self._alts(page), [])
        self.assertNotIn("Alternative", self._text(page, "#plbLegend"))
        self._tick(page, 21, 23)
        # Registered tab: every ticked section, on each of its meeting days.
        self.assertEqual(
            sorted(self._alts(page)),
            ["AI331|alt §M1|08:00-09:15", "AI331|alt §M3|15:00-16:15", "AI331|alt §M3|15:00-16:15"],
        )
        self.assertIn("Alternative", self._text(page, "#plbLegend"))
        # They are pictures: not buttons, hidden from assistive technology, and not blocks.
        self.assertEqual(
            page.eval_on_selector_all(
                "#plbWeek .plb-gb",
                "bs => bs.map(b => [b.tagName, b.getAttribute('aria-hidden'), b.classList.contains('plb-b'), b.querySelectorAll('button').length].join())",
            ),
            ["DIV,true,false,0"] * 3,
        )
        self.assertEqual(self._blocks(page, "new"), [])
        # Gone with the tick, and gone with the panel.
        self._box(page, 21).uncheck()
        self.assertEqual(len(self._alts(page)), 2)
        page.click("#plbDetail [data-act='dt-close']")
        self.assertEqual(self._alts(page), [])
        self.assertNotIn("Alternative", self._text(page, "#plbLegend"))
        # Another course's panel draws that course's ticks, none here.
        self._panel(page, "AI342")
        self.assertEqual(self._alts(page), [])
        page.click("#plbDetail [data-act='dt-close']")
        self._panel(page)
        self.assertEqual(len(self._alts(page)), 2)
        # A course that is not in the plan has no allowance to draw.
        self._panel(page, "CS351")
        self.assertEqual(self._alts(page), [])
        self.assertEqual(
            page.locator("#plbDetail input[type='checkbox'][name='plbOnly']").count(), 0
        )

    def test_on_an_options_tab_the_section_it_used_stays_solid_and_the_rest_are_dashed(
        self,
    ) -> None:
        page, _ = self._page()
        self._panel(page)
        self._tick(page, 22, 21, 29)
        self._build(page)
        self._pick(page, "B1")
        self.assertEqual(self._blocks(page, "new").count("AI331 new"), 1)
        self.assertEqual(
            sorted(self._alts(page)), ["AI331|alt §M1|08:00-09:15", "AI331|alt §M9|10:00-11:15"]
        )
        # B2 used M3, which is not ticked: all three are dashed there.
        self._pick(page, "B2")
        self.assertEqual(len(self._alts(page)), 3)
        # The legend entry follows the blocks.
        self._box(page, 21).uncheck()
        self._box(page, 22).uncheck()
        self._box(page, 29).uncheck()
        self.assertEqual(self._alts(page), [])
        self.assertNotIn("Alternative", self._text(page, "#plbLegend"))

    def test_dashed_blocks_never_change_what_the_week_says(self) -> None:
        for tab in ("registered", "B1", "B2"):
            with self.subTest(tab=tab):
                page, _ = self._page()
                self._panel(page)
                self._tick(page, 21, 22, 23, 24, 25)
                self._build(page)
                if tab != "registered":
                    self._pick(page, tab)
                else:
                    page.click("#plbTabRegistered")
                page.click("#plbDetail [data-act='dt-close']")
                without = page.evaluate(SNAPSHOT)
                self.assertEqual(self._alts(page), [])
                self._panel(page)
                self.assertTrue(self._alts(page))
                with_alts = page.evaluate(SNAPSHOT)
                self.assertEqual(with_alts, without)
                page.context.close()

    def test_a_dashed_block_outside_the_weeks_hours_or_days_widens_the_week_to_show_it(
        self,
    ) -> None:
        page, _ = self._page()
        self._panel(page)
        before = page.evaluate(
            "[document.querySelectorAll('#plbWeek .plb-dh').length, document.querySelector('.plb-wk').getBoundingClientRect().height]"
        )
        self._tick(page, 26, 27)  # Thu 16:00-17:15 and a Saturday
        after = page.evaluate(
            "[document.querySelectorAll('#plbWeek .plb-dh').length, document.querySelector('.plb-wk').getBoundingClientRect().height]"
        )
        self.assertEqual(after[0], before[0] + 1)
        self.assertGreater(after[1], before[1])
        self.assertEqual(
            page.evaluate(
                """() => {
                  const grid = document.querySelector('.plb-wk').getBoundingClientRect();
                  return [...document.querySelectorAll('#plbWeek .plb-gb')].map((b) => {
                    const r = b.getBoundingClientRect();
                    return r.height > 4 && r.width > 4 && r.top >= grid.top - 1 && r.bottom <= grid.bottom + 1
                      && r.left >= grid.left - 1 && r.right <= grid.right + 1;
                  });
                }"""
            ),
            [True, True],
        )
        # The Saturday column exists for the picture only: its day-off foot says so, nothing counts it.
        self.assertIn("Sat", self._text(page, "#plbWeek"))
        self._box(page, 27).uncheck()
        self.assertEqual(
            page.evaluate("document.querySelectorAll('#plbWeek .plb-dh').length"), before[0]
        )

    def test_where_a_dashed_block_shares_a_time_with_a_solid_one_the_solid_one_is_readable(
        self,
    ) -> None:
        page, _ = self._page()
        self._panel(page)
        self._tick(page, 22, 29)  # the same time on the same day
        self._build(page)
        self._pick(page, "B1")
        self.assertEqual(self._alts(page), ["AI331|alt §M9|10:00-11:15"])
        top = page.evaluate(
            """() => {
              const solid = document.querySelector("#plbWeek .plb-b[data-kind='new'][data-code='AI331']");
              const r = solid.getBoundingClientRect();
              const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
              return [hit.closest('.plb-b') === solid, !!hit.closest('.plb-gb')];
            }"""
        )
        self.assertEqual(top, [True, False])
        self.assertEqual(
            page.evaluate(
                "getComputedStyle(document.querySelector('#plbWeek .plb-gb')).pointerEvents"
            ),
            "none",
        )
        # The solid block still opens the panel (a click goes through to it).
        page.click("#plbWeek .plb-b[data-kind='new'][data-code='AI331']")
        expect(page.locator("#plbDetailTitle")).to_be_focused()

    # ── a change after a build, and the keyboard ─────────────────────────────
    def test_a_change_after_a_build_marks_it_out_of_date(self) -> None:
        for change in ("tick", "untick", "any"):
            with self.subTest(change=change):
                page, _ = self._page()
                self._panel(page)
                if change != "tick":
                    self._tick(page, 21)
                self._build(page)
                self.assertIsNone(page.get_attribute("#plbStrip", "data-stale"))
                if change == "tick":
                    self._tick(page, 21)
                elif change == "untick":
                    self._box(page, 21).uncheck()
                else:
                    page.locator("#plbDetail input[type='radio'][value='any']").check()
                expect(page.locator("#plbBuildLine")).to_have_text(STALE)
                self.assertEqual(page.get_attribute("#plbStrip", "data-stale"), "changed")
                self.assertTrue(page.locator("#plbReview").is_disabled())
                page.context.close()
        # Choosing "Only these" with nothing ticked changes nothing the build would read.
        page, _ = self._page()
        self._panel(page)
        self._build(page)
        page.locator("#plbDetail input[type='radio'][value='only']").check()
        expect(page.locator("#plbBuildLine")).to_have_text("")
        self.assertIsNone(page.get_attribute("#plbStrip", "data-stale"))

    def test_the_keyboard_stays_on_a_box_and_a_box_that_goes_disabled_hands_it_on(self) -> None:
        page, _ = self._page()
        self._panel(page)
        self._box(page, 22).focus()
        page.keyboard.press("Space")
        expect(self._box(page, 22)).to_be_checked()
        expect(self._box(page, 22)).to_be_focused()
        self.assertIn("AI331: only §M2 may be used.", self._status(page))
        page.keyboard.press("Space")
        expect(self._box(page, 22)).not_to_be_checked()
        expect(self._box(page, 22)).to_be_focused()
        # The radios keep it too.
        only = page.locator("#plbDetail input[type='radio'][value='only']")
        only.focus()
        page.keyboard.press("Space")
        expect(page.locator("#plbDetail input[type='radio'][value='only']")).to_be_focused()
        # The box with the keyboard becomes disabled (six others were ticked): the mode radio has it.
        self._box(page, 29).focus()
        page.evaluate(
            """() => { for (const t of [21, 22, 23, 24, 25, 26])
              document.querySelector(`#plbDetail input[value='${t}']`).click(); }"""
        )
        self.assertTrue(self._box(page, 29).is_disabled())
        expect(page.locator("#plbDetail input[type='radio'][value='only']")).to_be_focused()

    # ── Arabic ───────────────────────────────────────────────────────────────
    def test_removing_the_course_ends_only_these_for_the_panel_left_open(self) -> None:
        only = "#plbDetail input[name='plbMode'][value='only']"
        for tick in (False, True):
            with self.subTest(tick=tick):
                page, _fake = self._page()
                self._panel(page)
                if tick:
                    self._tick(page, 22)
                else:
                    page.check(only)
                self.assertTrue(self._checked(page, only))
                page.click("#plbDetail [data-act='dt-remove']")
                expect(page.locator("#plbDetail [data-act='dt-add']")).to_be_visible()
                page.click("#plbDetail [data-act='dt-add']")
                expect(
                    page.locator("#plbDetail input[name='plbMode'][value='any']")
                ).to_be_checked()
                self.assertEqual(
                    page.locator("#plbDetail input[name='plbOnly']:checked").count(), 0
                )
                self.assertEqual(page.locator("#plbDetail .plb-dt-note").count(), 0)

    def test_the_only_these_radio_says_what_it_means_right_now(self) -> None:
        page, _fake = self._page()
        self._panel(page)
        only = page.locator("#plbDetail input[name='plbMode'][value='only']")
        self.assertIsNone(only.get_attribute("aria-describedby"))
        only.check()
        self.assertEqual(only.get_attribute("aria-describedby"), "plbOnlyNote")
        expect(page.locator("#plbOnlyNote")).to_have_text(
            "Tick the sections the build may use. Until one is ticked, any section may be used."
        )
        self._tick(page, *ONE_TO_SIX)
        self.assertEqual(only.get_attribute("aria-describedby"), "plbOnlyNote plbOnlyMax")
        expect(page.locator("#plbOnlyNote")).to_have_text(
            "The build picks one of the ticked sections."
        )
        expect(page.locator("#plbOnlyMax")).to_have_text("Six sections at most.")

    def test_on_a_phone_a_dashed_block_names_its_section_as_a_solid_one_does(self) -> None:
        for width, word in ((1440, True), (390, False)):
            with self.subTest(width=width):
                page, _fake = self._page(width=width)
                self._panel(page)
                self._tick(page, 23)  # M3, Mon and Wed: dashed on the Registered tab
                block = page.locator("#plbWeek .plb-gb").first
                expect(block).to_be_attached()
                self.assertEqual(block.locator(".plb-alt-w").is_visible(), word)
                self.assertIn("\u00a7M3", block.text_content())

    def test_arabic_for_every_text(self) -> None:
        page, _ = self._page(language="ar")
        self._panel(page)
        text = self._text(page, "#plbDetail")
        self.assertIn("أي شعبة", text)
        self.assertIn("هذه فقط", text)
        page.locator("#plbDetail input[type='radio'][value='only']").check()
        self.assertEqual(
            self._text(page, "#plbDetail .plb-dt-note"),
            "حدِّد الشُعب التي يجوز للبناء استخدامها. ما لم تُحدَّد شعبة، تُستخدم أي شعبة.",
        )
        self._tick(page, 21)
        self.assertEqual(
            self._text(page, "#plbDetail .plb-dt-note"), "يختار البناء إحدى الشُعب المحدَّدة."
        )
        self.assertEqual(self._line(page), "فقط §M1")
        self.assertIn("AI331: يجوز استخدام §M1 فقط.", self._status(page))
        self.assertEqual(self._alts(page), ["AI331|بديل §M1|08:00-09:15"])
        self.assertIn("بديل", self._text(page, "#plbLegend"))
        self._tick(page, 22)
        self.assertEqual(self._line(page), "فقط §M1 أو §M2")
        self.assertIn("AI331: يجوز استخدام §M1 أو §M2 فقط.", self._status(page))
        self._tick(page, 23)
        self.assertEqual(self._line(page), "فقط 3 شعب")
        self.assertIn("AI331: يجوز استخدام 3 شعب فقط.", self._status(page))
        self._tick(page, 24, 25, 26)
        self.assertEqual(
            self._text(page, "#plbDetail .plb-dt-note:last-of-type"), "ست شُعب على الأكثر."
        )
        self._build(page)
        self._pick(page, "B1")
        self.assertEqual(self._line(page), "§M2 · أو 5 شعب أخرى")
        for tsid in (24, 25, 26):
            self._box(page, tsid).uncheck()
        self.assertEqual(self._line(page), "§M2 · أو شعبتان أخريان")
        self._box(page, 23).uncheck()
        self.assertEqual(self._line(page), "§M2 · أو §M1")
        self._box(page, 21).uncheck()
        self.assertIn("AI331: يجوز استخدام §M2 فقط.", self._status(page))
        self._box(page, 22).uncheck()
        self.assertIn("AI331: يجوز استخدام أي شعبة.", self._status(page))
        self.assertEqual(self._line(page), "§M2")
        self.assertNotIn("مثبّت", self._text(page, "#plbTable") + self._text(page, "#plbDetail"))
        self.assertEqual(page.evaluate("document.documentElement.dir"), "rtl")

    # ── size ─────────────────────────────────────────────────────────────────
    def test_nothing_overflows_at_three_widths_in_both_languages(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in ("en", "ar"):
            for font in ("app", "wide"):
                for width, height in ((1440, 900), (1152, 800), (390, 844)):
                    page, _ = self._page(language=language, width=width, height=height, font=font)
                    self._panel(page)
                    found = list(page.evaluate(OVERFLOWS))
                    page.locator("#plbDetail input[type='radio'][value='only']").check()
                    found += page.evaluate(OVERFLOWS)
                    self._tick(
                        page, *ONE_TO_SIX
                    )  # six ticks: the "at most" line and the dashed blocks
                    found += page.evaluate(OVERFLOWS)
                    self.assertEqual(len(self._alts(page)), 7)
                    page.click("#plbBuild")
                    expect(page.locator("#plbBuild")).to_be_enabled()
                    self._pick(page, "B1")
                    found += page.evaluate(OVERFLOWS)
                    if found:
                        problems[f"{language} {width}px {font} fonts"] = found
                    page.context.close()
        if problems:
            self.fail(
                "\n" + "\n".join(f"{k}:\n    " + "\n    ".join(v) for k, v in problems.items())
            )

    def test_the_new_texts_wrap_in_a_wide_font_and_never_say_pinned(self) -> None:
        page, _ = self._page(font="wide", width=390, height=844)
        self._panel(page)
        page.locator("#plbDetail input[type='radio'][value='only']").check()
        note = page.locator("#plbDetail .plb-dt-note")
        box = note.bounding_box()
        panel = page.locator("#plbDetail").bounding_box()
        self.assertLessEqual(box["x"] + box["width"], panel["x"] + panel["width"] + 1)
        self.assertIsNone(re.search(r"(?i)pinned", self._text(page, "#plbDetail")))

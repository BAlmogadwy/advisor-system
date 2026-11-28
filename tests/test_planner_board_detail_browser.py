"""The new planner screen, part three, in a real browser: the course detail panel, seat warnings,
a build that goes out of date, "Why these options", where a save went, tabs that read the same.

The same synthetic student and the same fakes as parts one and two (tests/planner_board_support.py).
The catalogue below gives AI331 six sections: one that clashes with a registered class, one that is
full, and three that are nearly full (2, 5 and 6 seats left: the warning stops after 5).
"""

from __future__ import annotations

import datetime as dt
import re

from tests.planner_board_support import (
    FIVE,
    OTHER_ID,
    OVERFLOWS,
    SAVED_ROWS,
    BuildFake,
    PlannerBoardCase,
    _mapping,
    _option,
    _section,
    build_reply,
    context_for,
    expect,
)

DETAIL_CATALOG = {
    "ok": True,
    "program_sections_only": True,
    "count": 9,
    "sections": [
        _section(11, "AI331", "M1", ["SUN", "TUE"], "10:00", "11:15", 20, 40),  # clashes CS322
        _section(12, "AI331", "M2", ["MON", "WED"], "10:00", "11:15", 20, 40),
        _section(16, "AI331", "M3", ["MON", "WED"], "14:30", "15:45", 40, 40),  # full
        _section(17, "AI331", "M4", ["THU"], "08:00", "09:15", 38, 40),  # 2 left
        _section(18, "AI331", "M5", ["THU"], "10:00", "11:15", 35, 40),  # 5 left
        _section(19, "AI331", "M6", ["THU"], "12:00", "13:15", 34, 40),  # 6 left: no warning
        _section(13, "STAT305", "M1", ["MON", "WED"], "08:00", "09:15", 30, 40),
        _section(14, "CS351", "M1", ["THU"], "09:00", "11:30", 37, 40),  # 3 left
        _section(15, "AI342", "M1", ["SUN"], "13:00", "14:15", None, None),  # capacity unknown
    ],
}
STALE = "The plan changed since this build. Rebuild to apply."
OLD = "This build is over 30 minutes old. Rebuild before applying."
MINUTE = 60 * 1000


def _fake() -> BuildFake:
    fake = BuildFake()
    fake.catalog_payload = DETAIL_CATALOG
    return fake


class PlannerBoardDetailBrowserTests(PlannerBoardCase):
    # ── helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _button(code: str) -> str:
        return f"#plbPlanList button[data-act='detail'][data-code='{code}']"

    def _open_panel(self, page, code: str) -> None:
        self._plan_panel(page)
        page.click(self._button(code))
        expect(page.locator("#plbPop")).to_be_visible()
        expect(page.locator("#plbDetailTitle")).to_contain_text(code)

    def _reopen(self, page, code: str = "AI331") -> None:
        """A click outside the popover (a rule, Build, a tab) closes it: open it again."""
        if not page.locator("#plbPop").is_visible():
            self._open_panel(page, code)

    def _section_text(self, page, tsid: int) -> str:
        return re.sub(
            r"\s+", " ", page.locator(f"#plbPop [data-tsid='{tsid}']").inner_text().strip()
        )

    def _stale(self, page, text=STALE, kind="changed", rebuild="Rebuild options") -> None:
        expect(page.locator("#plbBuildLine")).to_have_text(text)
        # The tabs keep the order they were laid out in (the options ranked, C1 first).
        self.assertEqual(self._tab_names(page), ["C1", "B1", "C2", "(registered)"])
        self.assertEqual(page.get_attribute("#plbStrip", "data-stale"), kind)
        self.assertTrue(page.locator("#plbReview").is_disabled())
        self.assertEqual(page.get_attribute("#plbReview", "aria-describedby"), "plbBuildLine")
        expect(page.locator("#plbBuild")).to_have_text(rebuild)
        self.assertTrue(page.locator("#plbBuild").is_enabled())
        # Out of date, Rebuild is the outline in the brand colour; Review and apply is disabled.
        self.assertIn("btn-outline-primary", page.get_attribute("#plbBuild", "class"))
        self.assertNotIn("btn-primary", page.get_attribute("#plbBuild", "class").split())
        # Out-of-date tabs are marked, not faded: their small grey facts must stay readable.
        self.assertEqual(
            page.evaluate(
                "() => [getComputedStyle(document.getElementById('plbTab_B1')).opacity,"
                " getComputedStyle(document.getElementById('plbStrip')).borderBottomStyle]"
            ),
            ["1", "dashed"],
        )

    # ── the panel: opening, where it sits, what it lists ─────────────────────
    def test_the_panel_opens_from_a_row_lists_the_sections_and_closes_with_focus_back(self) -> None:
        page = self._open(_fake())
        button = page.locator(self._button("AI331"))
        self.assertEqual(button.get_attribute("aria-expanded"), "false")
        self.assertEqual(button.get_attribute("aria-controls"), "plbPop")
        self.assertEqual(button.get_attribute("aria-haspopup"), "dialog")
        self.assertEqual(self._text(page, self._button("AI331")), "Any section")
        self.assertTrue(page.locator("#plbPop").is_hidden())
        # A registered row has the controls of a planned one (S7: using one makes the registered
        # courses editable); a blocked row has none.
        self.assertEqual(
            page.eval_on_selector_all(
                "#plbPlanList .plb-prow--kept[data-code='CS301'] [data-act]",
                "ns => ns.map(n => n.dataset.act)",
            ),
            ["ptick", "detail", "must", "remove"],
        )
        self._rows(page, "blocked")
        self.assertEqual(page.locator("#plbBlocked [data-act], #plbBlocked input").count(), 0)
        button.click()
        expect(page.locator("#plbPop")).to_be_visible()
        self.assertEqual(page.get_attribute(self._button("AI331"), "aria-expanded"), "true")
        row = "#plbPlanList .plb-prow[data-code='{}']"
        self.assertEqual(page.get_attribute(row.format("AI331"), "data-open"), "true")
        self.assertIsNone(page.get_attribute(row.format("AI342"), "data-open"))
        self.assertEqual(page.get_attribute("#plbPop", "role"), "dialog")
        self.assertEqual(page.get_attribute("#plbPop", "aria-labelledby"), "plbDetailTitle")
        expect(page.locator("#plbDetailTitle")).to_be_focused()
        self.assertEqual(self._text(page, "#plbDetailTitle"), "AI331 Machine Learning · 3 cr")
        # Any section is the default; every catalogue section is listed with its seats and flags.
        self.assertTrue(page.locator("#plbPop input[value='any']").is_checked())
        ids = page.eval_on_selector_all("#plbPop [data-tsid]", "ns => ns.map(n => n.dataset.tsid)")
        self.assertEqual(ids, ["11", "12", "16", "17", "18", "19"])
        m1 = self._section_text(page, 11)
        self.assertIn("§M1 Sun Tue 10:00-11:15", m1)
        self.assertIn("20 of 40 taken", m1)
        # A section that cannot be used says why once, in a sentence: no chip repeats it.
        self.assertIn("Not available: it clashes with a registered class", m1)
        self.assertEqual(page.locator("#plbPop [data-tsid='11'] .plb-flag--bad").count(), 0)
        m2 = self._section_text(page, 12)
        self.assertIn("§M2 Mon Wed 10:00-11:15 For AI · 20 of 40 taken", m2)
        self.assertNotIn("clashes", m2.lower())
        self.assertIn("Not available: full sections are off", self._section_text(page, 16))
        self.assertEqual(page.locator("#plbPop [data-tsid='16'] .plb-flag--bad").count(), 0)
        # Close puts the keyboard back on the row's button.
        page.click("#plbPop [data-act='dt-close']")
        expect(page.locator("#plbPop")).to_be_hidden()
        expect(page.locator(self._button("AI331"))).to_be_focused()
        self.assertEqual(page.get_attribute(self._button("AI331"), "aria-expanded"), "false")
        # Escape does the same from inside the panel.
        page.click(self._button("AI331"))
        page.keyboard.press("Tab")
        page.keyboard.press("Escape")
        expect(page.locator("#plbPop")).to_be_hidden()
        expect(page.locator(self._button("AI331"))).to_be_focused()
        # The same button closes it, and another row's button switches the popover (the row above
        # the open one: a popover hangs over the rows under its own button).
        page.click(self._button("AI342"))
        expect(page.locator("#plbDetailTitle")).to_contain_text("AI342")
        self.assertEqual(self._section_text(page, 15).count("seats unknown"), 1)
        self.assertNotIn("seats left", self._section_text(page, 15))
        page.click(self._button("AI331"))
        expect(page.locator("#plbDetailTitle")).to_contain_text("AI331")
        page.click(self._button("AI331"))
        expect(page.locator("#plbPop")).to_be_hidden()

    def test_the_popover_hangs_under_its_button_inside_the_plan_at_every_width(self) -> None:
        for width, height in ((1440, 900), (900, 900), (390, 844)):
            with self.subTest(width=width):
                page = self._open(_fake(), width=width, height=height)
                self._open_panel(page, "AI331")
                button = page.locator(self._button("AI331")).bounding_box()
                pop = page.locator("#plbPop").bounding_box()
                plan = page.locator("#plbPlan").bounding_box()
                # under its button, or above it when there is no room below (never over Build)
                under = 0 <= pop["y"] - (button["y"] + button["height"]) <= 8
                over = 0 <= button["y"] - (pop["y"] + pop["height"]) <= 8
                self.assertTrue(under or over, (pop, button))
                self.assertGreaterEqual(pop["x"], plan["x"] - 1)
                self.assertLessEqual(pop["x"] + pop["width"], plan["x"] + plan["width"] + 1)
                self.assertEqual(page.get_attribute("#plbPop", "role"), "dialog")
                page.context.close()

    def test_a_redraw_keeps_the_panel_open_and_the_focus_inside_it(self) -> None:
        page = self._open(_fake())
        self._open_panel(page, "AI331")
        # Required, on the row, redraws the plan: the keyboard stays on it (the popover, a click
        # outside it, closes).
        required = page.locator("#plbPlanList button[data-act='must'][data-code='AI331']")
        required.focus()
        page.keyboard.press("Space")
        self.assertEqual(required.get_attribute("aria-pressed"), "true")
        expect(required).to_be_focused()
        expect(page.locator("#plbPop")).to_be_hidden()
        self._open_panel(page, "AI331")
        page.locator("#plbPop input[value='12']").focus()
        page.keyboard.press("Space")
        expect(page.locator("#plbPop input[value='12']")).to_be_checked()
        expect(page.locator("#plbPop input[value='12']")).to_be_focused()
        self.assertEqual(self._cell(page, "AI331"), "Section M2")

    def test_the_panel_closes_when_its_course_goes_or_another_student_loads(self) -> None:
        fake = _fake()
        page = self._open(fake)
        self._open_panel(page, "AI342")
        page.click("#plbPlanList button[data-act='remove'][data-code='AI342']")
        expect(page.locator("#plbPop")).to_be_hidden()
        self.assertEqual(page.eval_on_selector_all("#plbPop *", "ns => ns.length"), 0)
        page.context.close()
        page = self._open(_fake())
        self._open_panel(page, "AI331")
        self._fetch(page, OTHER_ID)
        expect(page.locator("#plbContext")).to_have_text(f"{OTHER_ID} · 1448/1")
        expect(page.locator("#plbPop")).to_be_hidden()

    # ── the panel: pinning ───────────────────────────────────────────────────
    def test_a_pin_travels_in_the_build_body_in_the_shape_the_current_page_sends(self) -> None:
        fake = _fake()
        page = self._open(fake)
        self._open_panel(page, "AI331")
        page.check("#plbPop input[value='12']")
        self.assertEqual(self._cell(page, "AI331"), "Section M2")
        self.assertIn("AI331: only §M2 may be used.", self._text(page, "#plbStatus"))
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text("Rebuild options")
        item = [c for c in fake.build_bodies[0]["shortlist"] if c["course_code"] == "AI331"][0]
        self.assertEqual(item["pinned_sections"], [{"term_section_id": 12, "section": "M2"}])
        self._reopen(page)
        other = [c for c in fake.build_bodies[0]["shortlist"] if c["course_code"] == "AI342"][0]
        self.assertNotIn("pinned_sections", other)
        # Moving the allowance to another section: tick it, then untick the first.
        page.check("#plbPop input[value='17']")
        page.uncheck("#plbPop input[value='12']")
        page.click("#plbBuild")
        expect(page.locator("#plbBuildLine")).to_have_text("")
        item = [c for c in fake.build_bodies[1]["shortlist"] if c["course_code"] == "AI331"][0]
        self.assertEqual(item["pinned_sections"], [{"term_section_id": 17, "section": "M4"}])

    def test_any_section_removes_the_pin(self) -> None:
        fake = _fake()
        page = self._open(fake)
        self._open_panel(page, "AI331")
        page.check("#plbPop input[value='12']")
        page.check("#plbPop input[value='any']")
        self.assertEqual(self._cell(page, "AI331"), "Any section")
        self.assertIn("AI331: any section may be used.", self._text(page, "#plbStatus"))
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text("Rebuild options")
        item = [c for c in fake.build_bodies[0]["shortlist"] if c["course_code"] == "AI331"][0]
        self.assertNotIn("pinned_sections", item)

    def test_a_section_the_rules_forbid_is_disabled_and_a_pin_that_became_so_is_flagged(
        self,
    ) -> None:
        page = self._open(_fake())
        self._open_panel(page, "AI331")
        self.assertTrue(page.locator("#plbPop input[value='11']").is_disabled())  # clashes
        self.assertTrue(page.locator("#plbPop input[value='16']").is_disabled())  # full
        self.assertFalse(page.locator("#plbPop input[value='17']").is_disabled())
        # Allow full sections and ignore registered courses: both become usable.
        self._rules(page, full=True, mode="ignore")
        self._reopen(page)
        for tsid in (11, 16):
            self.assertFalse(page.locator(f"#plbPop input[value='{tsid}']").is_disabled())
        self.assertNotIn("Clashes", self._section_text(page, 11))
        page.check("#plbPop input[value='16']")
        page.check("#plbPop input[value='11']")
        self._rules(page, full=False, mode="keep")
        self._reopen(page)
        for tsid in (11, 16):
            self.assertTrue(page.locator(f"#plbPop input[value='{tsid}']").is_checked())
            self.assertIn("Ticked, but it can no longer be used.", self._section_text(page, tsid))
        self.assertIn("clashes with a registered class", self._section_text(page, 11))
        # The unusable ticks are still in the plan: nothing dropped them.
        self.assertEqual(self._cell(page, "AI331"), "Sections M1, M3")
        fake_item = page.evaluate(
            "() => document.querySelector(\"#plbPop input[value='11']\").checked"
        )
        self.assertTrue(fake_item)

    def test_a_pinned_section_missing_from_the_new_list_stays_selected_and_flagged(self) -> None:
        fake = _fake()
        page = self._open(fake)
        self._open_panel(page, "AI331")
        page.check("#plbPop input[value='12']")
        fake.catalog_payload = {
            **DETAIL_CATALOG,
            "sections": [s for s in DETAIL_CATALOG["sections"] if s["term_section_id"] != 12],
        }
        self._rules(page, prog=False)  # reloads the catalogue
        self._reopen(page)
        expect(page.locator("#plbPop [data-tsid='12']")).to_contain_text("not in the current")
        self.assertTrue(page.locator("#plbPop input[value='12']").is_checked())
        self.assertIn(
            "Ticked, but it is not in the current section list.", self._section_text(page, 12)
        )
        self.assertEqual(self._cell(page, "AI331"), "Section M2")

    def test_the_panel_names_the_options_that_chose_each_section(self) -> None:
        page, _ = self._built(_fake())
        self._open_panel(page, "AI331")
        self.assertIn("in B1, C2, B3", self._section_text(page, 12))
        self.assertIn("in C1, B2", self._section_text(page, 11))
        self.assertNotIn(" in ", " " + self._section_text(page, 17))
        # A stale build still says it.
        page.click("#plbPop input[value='17']")
        self._stale(page)
        self.assertIn("in B1, C2, B3", self._section_text(page, 12))

    def test_a_course_not_in_the_plan_has_no_popover_its_sections_are_on_its_row(self) -> None:
        page = self._open(_fake())
        self.assertEqual(page.locator("#plbPlanList [data-code='CS351']").count(), 0)
        page.click("#plbAvailList [data-code='CS351'] button[data-act='open']")
        row = self._text(page, "#plbAvailList [data-code='CS351'] .plb-sec[data-tsid='14']")
        self.assertIn("Thu 09:00-11:30", row)
        self.assertIn("3 left", row)
        self.assertNotIn("null", self._text(page, "#plbAvailList [data-code='CS351']"))
        # Adding it gives it a popover of its own, with the same sections.
        page.check("#plbAvailList [data-code='CS351'] input[data-act='tick']")
        page.click("#plbXAdd")
        self.assertEqual(self._rows(page, "new")[-1], "CS351")
        self._open_panel(page, "CS351")
        self.assertIn("37 of 40 taken", self._section_text(page, 14))
        self.assertEqual(page.locator("#plbPop input[type='radio']").count(), 2)
        # Removing it closes its popover and puts it back among the courses to add.
        page.click("#plbPlanList button[data-act='remove'][data-code='CS351']")
        expect(page.locator("#plbPop")).to_be_hidden()
        self.assertIn("CS351", self._rows(page, "could"))

    # ── seat warnings ────────────────────────────────────────────────────────
    def test_few_seats_show_in_the_panel_the_row_and_the_tab_and_unknown_never_does(self) -> None:
        fake = _fake()
        fake.build_answer = lambda body: (
            200,
            build_reply(
                [
                    _option(
                        "B1",
                        [
                            _mapping("AI331", "M4", 17, ["THU"], "08:00", "09:15"),
                            _mapping("AI342", "M1", 15, ["SUN"], "13:00", "14:15"),
                        ],
                    ),
                    _option(
                        "B2",
                        [_mapping("AI331", "M5", 18, ["THU"], "10:00", "11:15")],
                        unscheduled=[{"course_code": "AI342", "reason_code": "ALL_SECTIONS_CLASH"}],
                    ),
                    _option(
                        "B3",
                        [
                            _mapping("AI331", "M6", 19, ["THU"], "12:00", "13:15"),
                            _mapping("AI342", "M1", 15, ["SUN"], "13:00", "14:15"),
                        ],
                    ),
                    _option(
                        "C1",
                        [
                            _mapping("AI331", "M3", 16, ["MON"], "14:30", "15:45"),
                            _mapping("AI342", "M1", 15, ["SUN"], "13:00", "14:15"),
                        ],
                    ),
                ]
            ),
        )
        page, _ = self._built(fake)
        self._open_panel(page, "AI331")
        # The panel: 1 to 5 seats left is a warning, 6 is not, full is "Full", not "left".
        self.assertIn("2 seats left", self._section_text(page, 17))
        self.assertIn("5 seats left", self._section_text(page, 18))
        self.assertNotIn("seats left", self._section_text(page, 19))
        self.assertNotIn("left", self._section_text(page, 16))
        # The row on an option's tab, and the tab, one flag: the smallest number.
        page.click("#plbTab_B1")
        self.assertEqual(self._cell(page, "AI331"), "§M4 · 2 seats left")
        self.assertEqual(self._cell(page, "AI342"), "§M1")  # unknown capacity: no warning
        self.assertEqual(self._tab_lines(page, "B1")[-1], "2 seats left")
        # With another flag on the tab they share one line (B2 leaves a course out: last in the order).
        self._pick(page, "B2")
        self.assertEqual(self._tab_lines(page, "B2")[-1], "1 not scheduled · 5 seats left")
        self.assertEqual(len(self._tab_lines(page, "B2")), 4)
        # Six seats, full and unknown give no flag.
        for name in ("B3", "C1"):
            self.assertNotIn("seats", " ".join(self._tab_lines(page, name)))
        page.click("#plbTab_B3")
        self.assertEqual(self._cell(page, "AI331"), "§M6")

    # ── a build goes out of date ─────────────────────────────────────────────
    def test_each_change_to_the_plan_or_a_rule_marks_the_build_out_of_date(self) -> None:
        for change in ("add", "remove", "must", "rule", "pin", "add-all", "remove-all"):
            with self.subTest(change=change):
                page, fake = self._built(_fake())
                self.assertIsNone(page.get_attribute("#plbStrip", "data-stale"))
                if change == "add":
                    self._add(page, "CS351")
                elif change == "add-all":
                    page.select_option("#plbSource", "eligible")
                    page.click("#plbXAddAll")
                elif change == "remove-all":
                    page.click("#plbXRemAll")
                elif change == "remove":
                    page.click("#plbPlanList button[data-act='remove'][data-code='AI342']")
                elif change == "must":
                    page.click("#plbPlanList button[data-act='must'][data-code='AI331']")
                elif change == "rule":
                    self._rules(page, full=True)
                else:
                    self._open_panel(page, "AI331")
                    page.check("#plbPop input[value='12']")
                self._stale(page)
                # The tabs can still be selected and read.
                self._pick(page, "B2")  # one of the "More options"
                self.assertEqual(page.get_attribute("#plbTab_B2", "aria-selected"), "true")
                self.assertEqual(self._blocks(page, "new"), ["AI331 new", "AI331 new"])
                # A rebuild clears it.
                page.click("#plbBuild")
                expect(page.locator("#plbBuildLine")).to_have_text("")
                self.assertIsNone(page.get_attribute("#plbStrip", "data-stale"))
                self.assertTrue(page.locator("#plbReview").is_enabled())
                self.assertIsNone(page.get_attribute("#plbReview", "aria-describedby"))
                self.assertEqual(len(fake.build_bodies), 2)
                page.context.close()

    def test_resetting_unchanged_rules_keeps_the_build_current(self) -> None:
        page, _ = self._built(_fake())
        page.click("#plbRulesReset")
        self.assertIsNone(page.get_attribute("#plbStrip", "data-stale"))
        self.assertTrue(page.locator("#plbReview").is_enabled())

    def test_a_row_added_after_the_build_says_it_is_not_in_the_build(self) -> None:
        page, _ = self._built(_fake())
        self._add(page, "CS351")
        self.assertEqual(self._cell(page, "CS351"), "not in this build")
        self.assertEqual(
            self._cell(page, "AI331"), "§M1"
        )  # C1, the first of the order, is selected

    def test_another_student_and_a_save_still_remove_the_options(self) -> None:
        page, _ = self._built(_fake())
        self._open_panel(page, "AI331")
        page.click("#plbPop input[value='12']")
        self._fetch(page, OTHER_ID)
        expect(page.locator("#plbBuildLine")).to_have_text(
            "The plan changed. Build again to see options."
        )
        self.assertEqual(self._tab_names(page), ["(registered)"])
        self.assertIsNone(page.get_attribute("#plbStrip", "data-stale"))
        expect(page.locator("#plbWhyBtn")).to_be_hidden()
        page.context.close()
        page, fake = self._built(_fake())
        page.click("#plbReview")
        page.click("#plbApplyGo")
        expect(page.locator("#plbBuild")).to_have_text("Build options")
        self.assertEqual(self._tab_names(page), ["(registered)"])
        self.assertEqual(len(fake.save_bodies), 1)

    def test_a_build_older_than_thirty_minutes_is_out_of_date(self) -> None:
        page, _ = self._built(_fake(), clock=True)
        page.clock.fast_forward(29 * MINUTE)
        self.assertIsNone(page.get_attribute("#plbStrip", "data-stale"))
        self.assertTrue(page.locator("#plbReview").is_enabled())
        page.clock.fast_forward(2 * MINUTE)  # the once-a-minute check
        self._stale(page, OLD, "old")

    def test_the_age_is_checked_when_review_is_pressed_and_when_the_page_shows_again(self) -> None:
        later = lambda: dt.datetime.now() + dt.timedelta(minutes=31)  # noqa: E731
        page, fake = self._built(_fake(), clock=True)
        page.clock.set_system_time(later())  # time passes, no timer fires
        self.assertTrue(page.locator("#plbReview").is_enabled())
        page.click("#plbReview")
        expect(page.locator("#plbApplyDialog")).to_be_hidden()
        self._stale(page, OLD, "old")
        self.assertEqual(fake.save_bodies, [])
        page.context.close()
        page, fake = self._built(_fake(), clock=True)
        page.clock.set_system_time(later())
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        self._stale(page, OLD, "old")

    def test_a_review_open_when_the_build_goes_out_of_date_closes_and_nothing_is_saved(
        self,
    ) -> None:
        page, fake = self._built(_fake(), clock=True)
        page.click("#plbReview")
        expect(page.locator("#plbApplyDialog")).to_be_visible()
        page.clock.fast_forward(31 * MINUTE)
        expect(page.locator("#plbApplyDialog")).to_be_hidden()
        self._stale(page, OLD, "old")
        expect(page.locator("#plbBuild")).to_be_focused()
        self.assertEqual(fake.save_bodies, [])
        page.context.close()
        # The same when the plan changes under it (here from script: the dialog is modal).
        page, fake = self._built(_fake())
        page.click("#plbReview")
        page.evaluate(
            """() => {
              document.querySelector("#plbPlanList button[data-act='must'][data-code='AI331']").click();
            }"""
        )
        expect(page.locator("#plbApplyDialog")).to_be_hidden()
        self._stale(page)
        self.assertEqual(fake.save_bodies, [])

    # ── Why these options ────────────────────────────────────────────────────
    def test_options_that_are_the_same_timetable_share_one_tab(self) -> None:
        # Different build methods often arrive at the same sections. On real data six options
        # were two timetables: one tab each, carrying the names of the options it stands for.
        def option(name: str, tsid: int, label: str, left=()) -> dict:
            return _option(
                name,
                [
                    _mapping("AI331", label, tsid, ["MON", "WED"], "10:00", "11:15"),
                    _mapping("AI342", "M1", 15, ["SUN"], "13:00", "14:15"),
                ],
                unscheduled=list(left),
            )

        fake = _fake()
        fake.build_answer = lambda body: (
            200,
            build_reply(
                [
                    option("A1", 12, "M2"),
                    option("B1", 17, "M4"),  # another section: a different timetable
                    option("C1", 12, "M2"),  # the same as A1
                    option("B2", 12, "M2"),  # the same as A1
                    # The same sections as A1, but each leaves a course out for its own reason:
                    # merging them would drop a reason, so they stay apart.
                    option("C2", 12, "M2", [{"course_code": "STAT305", "reason_code": "R_ONE"}]),
                    option("C3", 12, "M2", [{"course_code": "STAT305", "reason_code": "R_TWO"}]),
                    # Options with nothing to save are never folded into one another.
                    _option("A8", []),
                    _option("A9", []),
                ]
            ),
        )
        page = self._open(fake)
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text("Rebuild options")
        self.assertEqual(self._tab_names(page), ["A1", "B1", "C2", "(registered)"])
        self.assertEqual(
            self._text(page, "#plbTab_A1 .plb-tab-n"), "A1 | C1 | B2 Three afternoons 15 cr"
        )
        self.assertEqual(self._text(page, "#plbTab_B1 .plb-tab-n"), "B1 15 cr")
        self.assertEqual(self._text(page, "#plbMoreBtn"), "More options (3)")
        page.click("#plbMoreBtn")
        hidden = page.eval_on_selector_all(
            "#plbMoreList .plb-more-item", "ns => ns.map(n => n.dataset.more)"
        )
        self.assertEqual(hidden, ["C3", "A8", "A9"])
        page.keyboard.press("Escape")
        # Nothing the reply said is lost: every name still counts for its method.
        page.click("#plbWhyBtn")
        why = self._text(page, "#plbWhy")
        self.assertIn("Method A (3 options).", why)
        self.assertIn("Method B (2 options).", why)
        self.assertIn("Method C (3 options).", why)
        page.keyboard.press("Escape")
        page.click("#plbTab_A1")
        # And the merged option is saved as what it is: its own sections, once.
        page.click("#plbReview")
        page.click("#plbApplyGo")
        expect(page.locator("#plbApplyDialog")).to_be_hidden()
        self.assertEqual(fake.save_bodies[-1]["term_section_ids"], [12, 15])

    def test_apply_checks_the_age_itself_when_no_timer_has_fired(self) -> None:
        later = dt.datetime.now() + dt.timedelta(minutes=31)
        page, fake = self._built(_fake(), clock=True)
        page.click("#plbReview")
        expect(page.locator("#plbApplyDialog")).to_be_visible()
        page.clock.set_system_time(later)  # time passes under the open review; no timer fires
        page.click("#plbApplyGo")
        expect(page.locator("#plbApplyDialog")).to_be_hidden()
        self._stale(page, OLD, "old")
        self.assertEqual(fake.save_bodies, [])

    def test_the_age_check_does_not_take_the_keyboard_away(self) -> None:
        # The check runs on a timer and redraws the strip and the table: whoever is on a tab or
        # on a row's control at minute 30 must still be there afterwards.
        remove = "#plbPlanList [data-act='remove'][data-code='AI331']"
        for selector in ("#plbTab_B1", remove):
            with self.subTest(selector=selector):
                page, _ = self._built(_fake(), clock=True)
                page.focus(selector)
                page.clock.fast_forward(31 * MINUTE)
                self._stale(page, OLD, "old")
                expect(page.locator(selector)).to_be_focused()
                page.context.close()

    def test_escape_elsewhere_leaves_why_these_options_open(self) -> None:
        page, _ = self._built(_fake())
        page.click("#plbWhyBtn")
        expect(page.locator("#plbWhy")).to_be_visible()
        for selector in ("#plbFind", "#plbTab_B1"):
            with self.subTest(selector=selector):
                page.focus(selector)
                page.keyboard.press("Escape")
                expect(page.locator("#plbWhy")).to_be_visible()
                expect(page.locator(selector)).to_be_focused()
        # From its own button it still closes.
        page.focus("#plbWhyBtn")
        page.keyboard.press("Escape")
        expect(page.locator("#plbWhy")).to_be_hidden()

    def test_why_these_options_opens_lists_the_methods_and_closes_on_escape(self) -> None:
        page = self._open(_fake())
        expect(page.locator("#plbWhyBtn")).to_be_hidden()
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text("Rebuild options")
        button = page.locator("#plbWhyBtn")
        expect(button).to_be_visible()
        self.assertEqual(button.get_attribute("aria-expanded"), "false")
        self.assertEqual(button.get_attribute("aria-controls"), "plbWhy")
        self.assertTrue(page.locator("#plbWhy").is_hidden())
        button.click()
        self.assertEqual(button.get_attribute("aria-expanded"), "true")
        text = self._text(page, "#plbWhy")
        self.assertIn(
            "Method A (no option). Tries to schedule as many courses of the plan as it can.", text
        )
        self.assertIn("Method B (3 options). Looks for a compact week", text)
        self.assertIn(
            "Method C (2 options). Uses the fewest days first, then the fewest gaps.", text
        )
        # An optional line that is absent is absent: it never prints the word "null".
        self.assertNotIn("null", text)
        # What was left out, with the reply's own reason.
        self.assertIn("Courses left unscheduled", text)
        self.assertIn("B2: Not scheduled: AI342 — Every section clashes with the chosen plan", text)
        page.keyboard.press("Escape")
        self.assertEqual(button.get_attribute("aria-expanded"), "false")
        self.assertTrue(page.locator("#plbWhy").is_hidden())
        expect(button).to_be_focused()
        # A rebuild closes it.
        button.click()
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_be_enabled()
        self.assertTrue(page.locator("#plbWhy").is_hidden())

    def test_why_says_once_what_the_whole_build_left_out(self) -> None:
        # A course no option could place is in the reply's own list AND under every option:
        # on real data the same sentence was printed for each of them.
        nowhere = {"course_code": "AI342", "reason_code": "ALL_SECTIONS_CLASH"}
        reply = build_reply(
            [
                _option(
                    "A1",
                    [_mapping("AI331", "M2", 12, ["MON", "WED"], "10:00", "11:15")],
                    unscheduled=[nowhere],
                ),
                _option(
                    "B1",
                    [_mapping("AI331", "M4", 17, ["THU"], "08:00", "09:15")],
                    unscheduled=[nowhere],
                ),
            ]
        )
        reply["unscheduled"] = [nowhere]
        fake = _fake()
        fake.build_answer = lambda body: (200, reply)
        page = self._open(fake)
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text("Rebuild options")
        page.click("#plbWhyBtn")
        why = self._text(page, "#plbWhy")
        self.assertEqual(why.count("Not scheduled: AI342"), 1)
        self.assertNotIn("A1: Not scheduled", why)

    def test_why_says_when_every_course_was_placed(self) -> None:
        fake = _fake()
        fake.build_answer = lambda body: (
            200,
            build_reply(
                [
                    _option(
                        "A1",
                        [
                            _mapping("AI331", "M2", 12, ["MON", "WED"], "10:00", "11:15"),
                            _mapping("AI342", "M1", 15, ["SUN"], "13:00", "14:15"),
                        ],
                    )
                ]
            ),
        )
        page, _ = self._built(fake)
        page.click("#plbWhyBtn")
        text = self._text(page, "#plbWhy")
        self.assertIn("1 option", text)
        self.assertIn("Every course in the plan was scheduled in each option.", text)
        self.assertNotIn("Courses left unscheduled", text)

    # ── after Apply ──────────────────────────────────────────────────────────
    def test_the_success_line_says_where_the_plan_went(self) -> None:
        page, fake = self._built(_fake())
        page.click("#plbReview")
        page.click("#plbApplyGo")
        expect(page.locator("#plbBuildLine")).to_have_text(
            "Saved 2 sections as this student's planned timetable for 1448/1."
            " Registered courses are unchanged."
        )
        # A student with no registered timetable reads the saved sections back as the week.
        fake = _fake()
        fake.context_answer = lambda body: (
            200,
            {
                **context_for(str(body["student_id"]), eligibility_mode=body["eligibility_mode"]),
                **(
                    {"baseline": [{**row, "source": "planner"} for row in SAVED_ROWS]}
                    if fake.saved
                    else {}
                ),
            },
        )
        page, _ = self._built(fake)
        page.click("#plbReview")
        page.click("#plbApplyGo")
        expect(page.locator("#plbBuildLine")).to_contain_text(
            "Registered courses are unchanged. No registered timetable is on file for this term"
        )

    # ── tabs that read the same ──────────────────────────────────────────────
    @staticmethod
    def _same(name, label, tsid, extra=()):
        return _option(
            name,
            [
                _mapping("AI331", label, tsid, ["MON", "WED"], "10:00", "11:15"),
                _mapping("AI342", "M1", 15, ["SUN"], "13:00", "14:15"),
                *extra,
            ],
        )

    def test_tabs_that_read_the_same_say_what_sets_them_apart_and_others_do_not(self) -> None:
        fake = _fake()
        fake.build_answer = lambda body: (
            200,
            build_reply(
                [
                    self._same("B1", "M2", 12),
                    self._same("B2", "M7", 21),
                    _option(
                        "B3",
                        [
                            _mapping("AI331", "M1", 11, ["SUN", "TUE"], "12:00", "13:15"),
                            _mapping("AI342", "M1", 15, ["MON"], "09:00", "10:15"),
                        ],
                    ),
                ]
            ),
        )
        page, _ = self._built(fake)
        self.assertEqual(self._tab_lines(page, "B1")[3], "AI331 §M2")
        self.assertEqual(self._tab_lines(page, "B2")[3], "AI331 §M7")
        self.assertEqual(len(self._tab_lines(page, "B3")), 3)  # already different: no extra line

    def test_the_difference_line_names_two_sections_then_the_rest(self) -> None:
        fake = _fake()
        extra = lambda label, tsid: [  # noqa: E731
            _mapping("STAT305", label, tsid, ["MON", "WED"], "08:00", "09:15")
        ]
        fake.build_answer = lambda body: (
            200,
            build_reply(
                [
                    self._same("B1", "M2", 12, extra("M1", 13)),
                    self._same("B2", "M7", 21, extra("M7", 23)),
                ]
            ),
        )
        page, _ = self._built(fake)
        self.assertEqual(self._tab_lines(page, "B1")[3], "AI331 §M2, STAT305 §M1")
        fake.build_answer = lambda body: (
            200,
            build_reply(
                [
                    _option(
                        "B1",
                        [
                            _mapping("AI331", "M2", 12, ["MON", "WED"], "10:00", "11:15"),
                            _mapping("AI342", "M1", 15, ["SUN"], "13:00", "14:15"),
                            *extra("M1", 13),
                        ],
                    ),
                    _option(
                        "B2",
                        [
                            _mapping("AI331", "M7", 21, ["MON", "WED"], "10:00", "11:15"),
                            _mapping("AI342", "M7", 25, ["SUN"], "13:00", "14:15"),
                            *extra("M7", 23),
                        ],
                    ),
                ]
            ),
        )
        page.click("#plbBuild")
        expect(page.locator("#plbTab_B2")).to_be_visible()
        self.assertEqual(self._tab_lines(page, "B1")[3], "AI331 §M2, AI342 §M1 +1")

    def test_five_distinct_options_get_no_difference_line(self) -> None:
        page, _ = self._built(_fake())
        self.assertEqual(page.locator(".plb-tab-diff").count(), 0)
        # The synthetic five have different facts, exactly as the part-two comment says.
        self.assertEqual(len({tuple(self._tab_lines(page, n)[1:3]) for n in ("C1", "B1", "C2")}), 3)
        self.assertEqual(FIVE["options"][0]["name"], "B1")

    # ── Arabic and size ──────────────────────────────────────────────────────
    def test_arabic_labels_and_right_to_left(self) -> None:
        page, _ = self._built(_fake(), language="ar")
        self.assertEqual(page.evaluate("document.documentElement.dir"), "rtl")
        self.assertEqual(self._text(page, "#plbWhyBtn"), "لماذا هذه الخيارات")
        self._open_panel(page, "AI331")
        self.assertEqual(self._text(page, "#plbPop [data-act='dt-close']"), "إغلاق")
        text = self._text(page, "#plbPop")
        for arabic in ("أي شعبة", "تتعارض مع مقرر مسجّل", "20 من 40 مشغولة", "ممتلئة", "الشعبة"):
            self.assertIn(arabic, text)
        self.assertIn("في B1، C2، B3", self._section_text(page, 12))
        self.assertIn("مقعدان متبقيان", self._section_text(page, 17))
        self.assertIn(
            "غير متاحة: الشُعب الممتلئة غير مسموحة في القواعد.", self._section_text(page, 16)
        )
        # Mirrored: the plan is the left panel in right-to-left, and the popover hangs inside it.
        plan = page.locator("#plbPlan").bounding_box()
        pop = page.locator("#plbPop").bounding_box()
        self.assertLess(plan["x"], page.locator("#plbAvail").bounding_box()["x"])
        self.assertGreaterEqual(pop["x"], plan["x"] - 1)
        self.assertLessEqual(pop["x"] + pop["width"], plan["x"] + plan["width"] + 1)
        page.check("#plbPop input[value='12']")
        self._stale(
            page, "تغيّرت الخطة منذ هذا البناء. أعد البناء قبل التطبيق.", rebuild="أعد بناء الخيارات"
        )
        page.click("#plbWhyBtn")
        text = self._text(page, "#plbWhy")
        self.assertIn("الطريقة B", text)
        self.assertIn("3 خيارات", text)
        self.assertIn("الطريقة A (بلا خيارات).", text)
        self.assertNotIn("null", text)
        page.keyboard.press("Escape")
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_be_enabled()
        page.click("#plbReview")
        page.click("#plbApplyGo")
        expect(page.locator("#plbBuildLine")).to_contain_text("المقررات المسجّلة لم تتغيّر.")
        self.assertIn(
            "تم حفظ شعبتين كجدول مخطَّط لهذا الطالب للفصل", self._text(page, "#plbBuildLine")
        )
        page.context.close()
        page, _ = self._built(_fake(), language="ar", clock=True)
        page.clock.fast_forward(31 * MINUTE)
        self._stale(
            page,
            "مضى على هذا البناء أكثر من 30 دقيقة. أعد البناء قبل التطبيق.",
            "old",
            "أعد بناء الخيارات",
        )

    def test_nothing_overflows_with_the_panel_open_at_three_widths_in_both_languages(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in ("en", "ar"):
            for font in ("app", "wide"):
                for width, height in ((1440, 900), (1152, 800), (390, 844)):
                    page, _ = self._built(
                        _fake(), language=language, width=width, height=height, font=font
                    )
                    self._open_panel(page, "AI331")
                    found = list(page.evaluate(OVERFLOWS))
                    page.click("#plbWhyBtn")
                    self._reopen(page)
                    page.check("#plbPop input[value='12']")  # a stale build, with the panel open
                    found += page.evaluate(OVERFLOWS)
                    page.click("#plbTab_B1")
                    found += page.evaluate(OVERFLOWS)
                    if found:
                        problems[f"{language} {width}px {font} fonts"] = found
                    page.context.close()
        if problems:
            self.fail(
                "\n" + "\n".join(f"{k}:\n    " + "\n    ".join(v) for k, v in problems.items())
            )

    def test_the_panel_controls_are_big_enough_and_focus_shows(self) -> None:
        for width, floor in ((1440, 31.5), (390, 43.5)):
            page = self._open(_fake(), width=width, height=900 if width > 400 else 844)
            self._open_panel(page, "AI331")
            sizes = page.evaluate(
                "() => [...document.querySelectorAll('#plbPop .plb-dt-close, #plbPop .btn, "
                "#plbPop .plb-dt-sec, .plb-secbtn')]"
                ".map(e => [e.className, Math.round(e.getBoundingClientRect().height)])"
            )
            self.assertEqual([s for s in sizes if s[1] < floor], [], f"{width}px")
            page.focus(self._button("AI342"))
            page.keyboard.press("Shift")
            self.assertNotEqual(
                page.evaluate("getComputedStyle(document.activeElement).outlineStyle"), "none"
            )
            page.context.close()

    def test_arabic_polish_stats_on_one_row_durations_and_group_headers_stay_whole(self) -> None:
        long_name = "عبدالرحمن بن محمد بن عبدالعزيز الشمري الحربي"
        problems = []
        for language in ("ar", "en"):
            for font in ("app", "wide"):
                for width, height in ((1440, 900), (1152, 800), (390, 844)):
                    fake = _fake()
                    fake.context_answer = lambda body: (
                        200,
                        context_for(str(body["student_id"]), name=long_name),
                    )
                    page, _ = self._built(
                        fake, language=language, width=width, height=height, font=font
                    )
                    label = f"{language} {width} {font}"
                    stats = page.eval_on_selector_all(
                        ".plb-sf",
                        "ns => ns.map(n => { const r = n.getBoundingClientRect(); return [Math.round(r.top), r.left, r.right]; })",
                    )
                    if width == 1440 and (len(stats) != 4 or len({s[0] for s in stats}) != 1):
                        problems.append(f"{label}: the four facts are not on one row: {stats}")
                    for sel in (".plb-nb", "#plbPlanLoad", "#plbAvailTicked"):
                        for broken in page.eval_on_selector_all(
                            sel,
                            "ns => ns.filter(n => new Set([...n.getClientRects()]"
                            ".map(r => Math.round(r.top))).size > 1"
                            " || n.getBoundingClientRect().height > 30).map(n => n.textContent)",
                        ):
                            problems.append(f"{label}: {sel} wraps: {broken!r}")
                    page.context.close()
        self.assertEqual(problems, [])
        page, _ = self._built(_fake(), language="ar", width=1152, height=800)
        self.assertGreater(page.locator(".plb-nb").count(), 0)
        self.assertIn("1 س 45 د", self._text(page, "#plbTab_B1"))
        self.assertIn("3 ساعات متاحة", self._text(page, "#plbMeterLine"))

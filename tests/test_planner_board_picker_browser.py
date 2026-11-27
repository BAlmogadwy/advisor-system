"""The planner screen's course picker (zone 02), in a real browser.

Courses from <source> | Available | arrows | Your plan, with a section popover for each planned
course. The synthetic student and fakes are the other board tests' (tests/planner_board_support.py);
the whole-term catalogue (source 4) is answered by the same fake, here with credit hours.

The plan after loading: AI331 and AI342 (the suggestions). The recommendations also hold two
blocked courses (AI431, AI450); the student's plan offers STAT305 and CS351 besides.
"""

from __future__ import annotations

import re

from tests.planner_board_support import (
    OVERFLOWS,
    BuildFake,
    PlannerBoardCase,
    _section,
    expect,
)


def _sec(tsid, code, section, days, start, end, registered, capacity, hours=3, programs=("AI",)):
    row = _section(tsid, code, section, days, start, end, registered, capacity)
    row["credit_hours"] = hours
    row["programs"] = list(programs)
    row["course_name"] = f"{code} name"
    return row


SECTIONS = [
    _sec(11, "AI331", "M1", ["SUN", "TUE"], "10:00", "11:15", 20, 40),
    _sec(12, "AI331", "M2", ["MON", "WED"], "10:00", "11:15", 20, 40),
    _sec(13, "STAT305", "M1", ["MON", "WED"], "08:00", "09:15", 30, 40),
    _sec(14, "CS351", "M1", ["THU"], "09:00", "11:30", 37, 40),  # 3 seats left: few
    _sec(15, "AI342", "M1", ["SUN"], "13:00", "14:15", None, None),
    _sec(16, "STAT305", "M2", ["THU"], "13:00", "14:15", 40, 40),  # full
    _sec(17, "CS351", "M2", ["THU"], "12:00", "13:15", 10, 40),
    _sec(31, "DS410", "M1", ["THU"], "08:00", "09:15", 10, 40, programs=("DS",)),
    _sec(32, "XX900", "M1", ["SAT"], "08:00", "09:15", 10, 40, hours=None),
]
CATALOG = {"ok": True, "count": len(SECTIONS), "program_sections_only": True, "sections": SECTIONS}

AVAIL = "#plbAvailList .plb-arow:not(.plb-arow--off)"
ROW = "#plbAvailList .plb-arow[data-code='{}']"
PLAN = "#plbPlanList .plb-prow:not(.plb-prow--kept)"


class PlannerBoardPickerTests(PlannerBoardCase):
    def _page(self, **kwargs):
        fake = BuildFake()
        fake.catalog_payload = CATALOG
        kwargs.setdefault("source", "recommended")
        return self._open(fake, **kwargs), fake

    @staticmethod
    def _avail(page) -> list[str]:
        return page.eval_on_selector_all(AVAIL, "rows => rows.map(r => r.dataset.code)")

    @staticmethod
    def _plan(page) -> list[str]:
        return page.eval_on_selector_all(PLAN, "rows => rows.map(r => r.dataset.code)")

    @staticmethod
    def _source(page, name: str) -> None:
        page.select_option("#plbSource", name)

    @staticmethod
    def _tick(page, code: str) -> None:
        page.check(f"{ROW.format(code)} input[data-act='tick']")

    @staticmethod
    def _pins(fake, code: str):
        item = [c for c in fake.build_bodies[-1]["shortlist"] if c["course_code"] == code][0]
        return item.get("pinned_sections", "absent")

    def _build(self, page, fake) -> dict:
        before = len(fake.build_bodies)
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_be_enabled()
        self.assertEqual(len(fake.build_bodies), before + 1)
        return fake.build_bodies[-1]

    def _all_loaded(self, page) -> None:
        self._source(page, "all")
        expect(page.locator("#plbSource option[value='all']")).to_have_text(re.compile(r"\(\d+\)"))

    # ── sources ──────────────────────────────────────────────────────────────
    def test_each_source_lists_the_right_courses_with_its_count(self) -> None:
        page, fake = self._page()
        self._remove(page, "AI342")
        names = page.eval_on_selector_all("#plbSource option", "os => os.map(o => o.textContent)")
        self.assertEqual(
            names,
            [
                "Recommended for this student (3)",
                "This student's plan (4)",
                "Everything they can register (3)",
                "All programmes' courses",
            ],
        )
        self.assertEqual(page.locator("#plbSource").input_value(), "recommended")
        self.assertEqual(self._avail(page), ["AI342"])
        self.assertEqual(self._rows(page, "blocked"), ["AI431", "AI450"])
        expect(page.locator("#plbAvailCount")).to_have_text("Available · 1 course")
        self._source(page, "plan")
        self.assertEqual(self._avail(page), ["AI342", "STAT305", "CS351"])
        page.click("#plbFold")
        self.assertEqual(
            page.eval_on_selector_all("#plbBlocked .plb-arow", "r => r.map(x => x.dataset.code)"),
            ["AI431"],
        )
        self.assertIn("Prerequisites not met", self._text(page, "#plbBlocked"))
        self._source(page, "eligible")
        self.assertEqual(self._avail(page), ["AI342", "STAT305", "CS351"])
        self.assertEqual(page.locator("#plbFold").count(), 0)
        self._all_loaded(page)
        self.assertEqual(self._avail(page), ["AI342", "CS351", "DS410", "STAT305", "XX900"])
        self.assertEqual(
            page.locator("#plbSource option[value='all']").text_content(),
            "All programmes' courses (5)",
        )

    def test_the_whole_term_source_loads_once_and_marks_compare_only(self) -> None:
        page, fake = self._page()
        self.assertEqual(len(fake.catalog_bodies), 1)
        self._all_loaded(page)
        self._source(page, "recommended")
        self._source(page, "all")
        self.assertEqual(len(fake.catalog_bodies), 2)  # the plan's own, then ONE for the term
        body = fake.catalog_bodies[-1]
        self.assertNotIn("course_codes", body)
        self.assertIs(body["program_sections_only"], False)
        tag = page.locator(f"{ROW.format('DS410')} .plb-flag", has_text="Compare only")
        expect(tag).to_be_visible()
        self.assertIn("Any programme", tag.get_attribute("title"))
        self.assertEqual(
            page.locator(ROW.format("STAT305") + " .plb-flag", has_text="Compare").count(), 0
        )
        # a course of another programme can be planned
        self._tick(page, "DS410")
        page.click("#plbXAdd")
        self.assertEqual(self._plan(page)[-1], "DS410")

    def test_unknown_credits_cannot_be_added(self) -> None:
        page, fake = self._page()
        self._all_loaded(page)
        box = page.locator(f"{ROW.format('XX900')} input[data-act='tick']")
        expect(box).to_be_disabled()
        self.assertIn("not known", box.get_attribute("title"))
        self.assertIn("? cr", self._text(page, ROW.format("XX900")))
        # its sections cannot be ticked either, and there is no "Add only these" for it
        page.click(f"{ROW.format('XX900')} button[data-act='open']")
        expect(page.locator(f"{ROW.format('XX900')} input[data-act='sectick']")).to_be_disabled()
        self.assertEqual(page.locator(f"{ROW.format('XX900')} button[data-act='only']").count(), 0)
        self.assertIn(
            "not known", page.locator(f"{ROW.format('XX900')} .plb-ar-main").get_attribute("title")
        )
        # » skips it and says so, naming the course and why
        page.click("#plbXAddAll")
        self.assertNotIn("XX900", self._plan(page))
        self.assertIn("DS410", self._plan(page))
        status = self._text(page, "#plbStatus")
        self.assertIn("XX900", status)
        self.assertIn("credits are not known", status)

    def test_ticked_sections_that_leave_the_catalogue_are_dropped_with_their_button(self) -> None:
        page, fake = self._page()
        self._source(page, "eligible")
        row = ROW.format("CS351")
        page.click(f"{row} button[data-act='open']")
        page.check(f"{row} input[data-tsid='14']")
        page.check(f"{row} input[data-tsid='17']")
        expect(page.locator(f"{row} button[data-act='only']")).to_contain_text("2 sections")
        # the rule changes and the catalogue no longer lists those sections
        fake.catalog_payload = {
            **CATALOG,
            "sections": [x for x in SECTIONS if x["course_key"] != "CS351"],
        }
        self._rules(page, prog=False)
        expect(page.locator(f"{row} .plb-meta")).to_contain_text("No sections")
        self.assertEqual(page.locator(f"{row} button[data-act='only']").count(), 0)
        expect(page.locator(f"{row} input[data-act='tick']")).not_to_be_checked()
        expect(page.locator("#plbAvailTicked")).to_have_text("0 ticked · 0 cr")

    def test_the_whole_term_list_survives_a_reload_of_the_same_student(self) -> None:
        page, fake = self._page()
        self._all_loaded(page)
        whole = lambda: [b for b in fake.catalog_bodies if "course_codes" not in b]  # noqa: E731
        self.assertEqual(len(whole()), 1)
        self._rules(page, elig="relaxed")  # reloads the student, not the term
        expect(page.locator("#plbEligRelaxed")).to_be_checked()
        page.wait_for_timeout(400)  # a second request, if any, would be here by now
        self.assertIn("DS410", self._avail(page))
        self.assertEqual(len(whole()), 1)
        self.assertEqual(page.locator("#plbSource").input_value(), "all")

    def test_the_popover_never_covers_the_build_row_and_follows_its_row_in_dom_order(self) -> None:
        for width, height in ((1440, 900), (1152, 800), (390, 844)):
            with self.subTest(width=width):
                page, fake = self._page(width=width, height=height)
                self._remove(page, "AI342")  # a one-row plan: little room under the button
                self._sections_pop(page, "AI331")
                pop = page.locator("#plbPop").bounding_box()
                build = page.locator("#plbBuild").bounding_box()
                self.assertTrue(
                    pop["y"] + pop["height"] <= build["y"] + 0.5
                    or pop["y"] >= build["y"] + build["height"]
                    or pop["x"] + pop["width"] <= build["x"]
                    or pop["x"] >= build["x"] + build["width"],
                    (pop, build),
                )
                self.assertEqual(
                    page.evaluate(
                        "document.getElementById('plbPop').previousElementSibling.dataset.code"
                    ),
                    "AI331",
                )
                page.context.close()

    def test_the_ticked_count_is_not_a_live_region(self) -> None:
        page, fake = self._page()
        self.assertIsNone(page.locator("#plbAvailTicked").get_attribute("aria-live"))

    def test_search_filters_by_code_or_name_and_reads_arabic_digits(self) -> None:
        page, fake = self._page()
        self._source(page, "eligible")
        self.assertEqual(self._avail(page), ["STAT305", "CS351"])
        page.fill("#plbFind", "stat")
        self.assertEqual(self._avail(page), ["STAT305"])
        page.fill("#plbFind", "net")  # a name: Networks
        self.assertEqual(self._avail(page), ["CS351"])
        page.fill("#plbFind", "٣٠٥")
        self.assertEqual(self._avail(page), ["STAT305"])
        page.fill("#plbFind", "cs 351")
        self.assertEqual(self._avail(page), ["CS351"])
        page.fill("#plbFind", "zzz")
        expect(page.locator("#plbAvailList")).to_contain_text("No course matches")
        page.fill("#plbFind", "")
        self.assertEqual(self._avail(page), ["STAT305", "CS351"])

    # ── moving courses ───────────────────────────────────────────────────────
    def test_tick_and_the_arrow_add_a_course_for_any_section(self) -> None:
        page, fake = self._page()
        self._source(page, "eligible")
        self._tick(page, "STAT305")
        expect(page.locator("#plbAvailTicked")).to_have_text("1 ticked · 3 cr")
        page.click("#plbXAdd")
        self.assertEqual(self._plan(page), ["AI331", "AI342", "STAT305"])
        self.assertEqual(self._avail(page), ["CS351"])
        self.assertEqual(self._cell(page, "STAT305"), "Any section")
        body = self._build(page, fake)
        self.assertEqual(self._pins(fake, "STAT305"), "absent")
        self.assertEqual(
            [c["course_code"] for c in body["shortlist"]], ["AI331", "AI342", "STAT305"]
        )

    def test_ticked_sections_send_exactly_those_pins(self) -> None:
        page, fake = self._page()
        self._source(page, "eligible")
        page.click(f"{ROW.format('STAT305')} button[data-act='open']")
        expect(page.locator(f"{ROW.format('STAT305')} .plb-secs")).to_be_visible()
        full = page.locator(f"{ROW.format('STAT305')} input[data-tsid='16']")
        expect(full).to_be_disabled()  # full, and "Use full sections" is off
        self.assertIn("Full", self._text(page, f"{ROW.format('STAT305')} .plb-sec[data-tsid='16']"))
        page.check(f"{ROW.format('STAT305')} input[data-tsid='13']")
        # ticking a section ticks its course
        expect(page.locator(f"{ROW.format('STAT305')} input[data-act='tick']")).to_be_checked()
        page.click(f"{ROW.format('STAT305')} button[data-act='only']")
        self.assertEqual(self._plan(page)[-1], "STAT305")
        self.assertEqual(self._cell(page, "STAT305"), "Section M1")
        self._build(page, fake)
        self.assertEqual(self._pins(fake, "STAT305"), [{"term_section_id": 13, "section": "M1"}])

    def test_the_arrow_moves_a_course_with_its_ticked_sections_only(self) -> None:
        page, fake = self._page()
        self._source(page, "eligible")
        page.click(f"{ROW.format('CS351')} button[data-act='open']")
        page.check(f"{ROW.format('CS351')} input[data-tsid='14']")
        self._tick(page, "STAT305")
        page.click("#plbXAdd")
        self.assertEqual(self._cell(page, "CS351"), "Section M1")
        self.assertEqual(self._cell(page, "STAT305"), "Any section")
        self._build(page, fake)
        self.assertEqual(self._pins(fake, "CS351"), [{"term_section_id": 14, "section": "M1"}])
        self.assertEqual(self._pins(fake, "STAT305"), "absent")

    def test_clearing_the_sections_leaves_the_course_ticked(self) -> None:
        page, fake = self._page()
        self._source(page, "eligible")
        page.click(f"{ROW.format('CS351')} button[data-act='open']")
        page.check(f"{ROW.format('CS351')} input[data-tsid='14']")
        expect(page.locator(f"{ROW.format('CS351')} button[data-act='only']")).to_be_visible()
        page.uncheck(f"{ROW.format('CS351')} input[data-tsid='14']")
        expect(page.locator(f"{ROW.format('CS351')} input[data-act='tick']")).to_be_checked()
        self.assertEqual(page.locator(f"{ROW.format('CS351')} button[data-act='only']").count(), 0)
        page.click("#plbXAdd")
        self.assertEqual(self._cell(page, "CS351"), "Any section")

    def test_the_double_arrow_adds_every_course_shown_never_a_blocked_one(self) -> None:
        page, fake = self._page()
        self._source(page, "plan")
        page.fill("#plbFind", "stat")
        page.click("#plbXAddAll")
        self.assertEqual(self._plan(page), ["AI331", "AI342", "STAT305"])  # the search limited it
        page.fill("#plbFind", "")
        page.click("#plbXAddAll")
        self.assertEqual(self._plan(page), ["AI331", "AI342", "STAT305", "CS351"])
        self.assertNotIn("AI431", self._plan(page))  # blocked: prerequisites missing
        expect(page.locator("#plbXAddAll")).to_be_disabled()
        expect(page.locator("#plbXAdd")).to_be_disabled()

    def test_the_left_arrows_remove_ticked_rows_or_all_and_kept_rows_stay(self) -> None:
        page, fake = self._page()
        self.assertEqual(self._rows(page, "registered"), ["CS301", "CS322", "CS330"])
        expect(page.locator("#plbXRem")).to_be_disabled()
        page.check("#plbPlanList .plb-prow[data-code='AI342'] input[data-act='ptick']")
        page.click("#plbXRem")
        self.assertEqual(self._plan(page), ["AI331"])
        expect(page.locator("#plbXRem")).to_be_disabled()
        self._add(page, "STAT305")
        page.click("#plbXRemAll")
        self.assertEqual(self._plan(page), [])
        self.assertEqual(self._rows(page, "registered"), ["CS301", "CS322", "CS330"])
        expect(page.locator("#plbXRemAll")).to_be_disabled()
        expect(page.locator("#plbPlanList .plb-hint")).to_be_visible()
        # the removed courses come back to their source
        self._source(page, "recommended")
        self.assertEqual(self._avail(page), ["AI331", "AI342"])

    def test_double_click_moves_a_row(self) -> None:
        page, fake = self._page()
        self._source(page, "eligible")
        page.dblclick(f"{ROW.format('CS351')} .plb-nm")
        self.assertEqual(self._plan(page)[-1], "CS351")
        self.assertEqual(self._avail(page), ["STAT305"])

    def test_the_blocked_fold_lists_each_courses_reason(self) -> None:
        page, fake = self._page()
        fold = page.locator("#plbFold")
        expect(fold).to_have_text(re.compile(r"2 can't be registered yet · prerequisites missing"))
        self.assertEqual(fold.get_attribute("aria-expanded"), "false")
        fold.click()
        self.assertEqual(fold.get_attribute("aria-expanded"), "true")
        self.assertIn(
            "Missing: AI331", self._text(page, "#plbBlocked .plb-arow[data-code='AI431']")
        )
        self.assertEqual(page.locator("#plbBlocked input").count(), 0)  # nothing to tick

    # ── the plan ─────────────────────────────────────────────────────────────
    def test_the_plan_header_load_meter_and_hint(self) -> None:
        page, fake = self._page()
        expect(page.locator("#plbPlanCount")).to_have_text("Your plan · 2 courses")
        expect(page.locator("#plbPlanLoad")).to_have_text("15 / 18 cr")
        expect(page.locator("#plbMeterLine")).to_have_text("6 planned + 9 registered · 3 cr free")
        self.assertEqual(page.locator("#plbPlanLoad").get_attribute("data-over"), "false")
        self.assertIn("Tick courses on the left", self._text(page, "#plbPlanList .plb-hint"))
        self._rules(page, mode="ignore")
        expect(page.locator("#plbMeterLine")).to_have_text("6 planned · 12 cr free")
        self.assertEqual(self._rows(page, "registered"), [])
        self._rules(page, max=4)
        expect(page.locator("#plbMeterLine")).to_have_text("6 planned · 2 cr over the limit")
        self.assertEqual(page.locator("#plbPlanLoad").get_attribute("data-over"), "true")
        self._rules(page, max="")
        expect(page.locator("#plbPlanLoad")).to_have_text("6 cr")

    def test_required_toggles_must_take_in_the_request(self) -> None:
        page, fake = self._page()
        button = page.locator("#plbPlanList button[data-act='must'][data-code='AI342']")
        self.assertEqual(button.get_attribute("aria-pressed"), "false")
        self.assertEqual(button.get_attribute("aria-label"), "Make AI342 required")
        button.click()
        self.assertEqual(button.get_attribute("aria-pressed"), "true")
        expect(button).to_have_text("Required")
        self._build(page, fake)
        must = {c["course_code"]: c["must_take"] for c in fake.build_bodies[-1]["shortlist"]}
        self.assertEqual(must, {"AI331": False, "AI342": True})
        button.click()
        self.assertEqual(button.get_attribute("aria-pressed"), "false")

    def test_the_section_popover_sets_pins_and_closes_with_focus_back(self) -> None:
        page, fake = self._page()
        self._rules(page, mode="ignore")  # M1 clashes with a registered class while they are kept
        self._sections_pop(page, "AI331")
        pop = page.locator("#plbPop")
        box = pop.locator("input[name='plbOnly'][value='12']")
        box.check()
        self.assertEqual(self._cell(page, "AI331"), "Section M2")
        self.assertEqual(
            page.locator("#plbPlanList .plb-secbtn[data-code='AI331']").get_attribute(
                "data-limited"
            ),
            "true",
        )
        pop.locator("input[name='plbOnly'][value='11']").check()
        self.assertEqual(self._cell(page, "AI331"), "Sections M1, M2")
        page.keyboard.press("Escape")
        expect(pop).to_be_hidden()
        expect(
            page.locator("#plbPlanList button[data-act='detail'][data-code='AI331']")
        ).to_be_focused()
        # one popover at a time: AI342's, then AI331's again
        self._sections_pop(page, "AI342")
        expect(page.locator("#plbDetailTitle")).to_contain_text("AI342")
        # (the popover may hang over that button: the keyboard reaches it either way)
        page.locator("#plbPlanList button[data-act='detail'][data-code='AI331']").focus()
        page.keyboard.press("Enter")
        expect(page.locator("#plbDetailTitle")).to_contain_text("AI331")
        self.assertEqual(page.locator("#plbPop:visible").count(), 1)
        self.assertEqual(
            page.locator("#plbPlanList button[data-act='detail'][aria-expanded='true']").count(), 1
        )
        self._build(page, fake)
        self.assertEqual(
            self._pins(fake, "AI331"),
            [{"term_section_id": 11, "section": "M1"}, {"term_section_id": 12, "section": "M2"}],
        )

    def test_the_popover_closes_on_an_outside_click_and_on_its_button(self) -> None:
        page, fake = self._page()
        self._sections_pop(page, "AI331")
        page.click("#plbFind", force=True)
        expect(page.locator("#plbPop")).to_be_hidden()
        self._sections_pop(page, "AI331")
        page.click("#plbPlanList button[data-act='detail'][data-code='AI331']")
        expect(page.locator("#plbPop")).to_be_hidden()
        expect(
            page.locator("#plbPlanList button[data-act='detail'][data-code='AI331']")
        ).to_be_focused()

    def test_a_planned_block_in_the_week_opens_the_popover(self) -> None:
        page, fake = self._page()
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_be_enabled()
        block = page.locator("#plbWeek .plb-b[data-act='week-open'][data-code='AI331']").first
        block.click()
        expect(page.locator("#plbPop")).to_be_visible()
        expect(page.locator("#plbDetailTitle")).to_contain_text("AI331")
        page.keyboard.press("Escape")
        expect(page.locator("#plbPop")).to_be_hidden()
        expect(block).to_be_focused()

    def test_removing_a_course_returns_it_to_its_source(self) -> None:
        page, fake = self._page()
        self._remove(page, "AI342")
        self.assertEqual(self._plan(page), ["AI331"])
        self.assertEqual(self._avail(page), ["AI342"])
        expect(page.locator(f"{ROW.format('AI342')} input[data-act='tick']")).to_be_focused()

    # ── Arabic, mirrored arrows, phone ───────────────────────────────────────
    def test_arabic_texts_and_the_arrows_point_at_the_plan(self) -> None:
        page, fake = self._page(language="ar")
        self.assertEqual(page.evaluate("document.documentElement.dir"), "rtl")
        expect(page.locator("#plbPlanCount")).to_have_text(re.compile(r"^خطتك · "))
        expect(page.locator("#plbAvailCount")).to_have_text(re.compile(r"^المتاحة · "))
        self.assertIn("مقترحة لهذا الطالب", page.locator("#plbSource option").first.text_content())
        self.assertIn("متطلبات سابقة ناقصة", self._text(page, "#plbFold"))
        self.assertEqual(
            page.locator("#plbPlanList .plb-secbtn[data-code='AI331']").text_content().strip(),
            "أي شعبة",
        )
        self.assertEqual(
            page.locator("#plbPlanList button[data-act='must'][data-code='AI331']").get_attribute(
                "aria-label"
            ),
            "اجعل AI331 إلزاميًا",
        )
        # the available panel is on the right, the plan on the left, and the arrow points left
        avail = page.locator("#plbAvail").bounding_box()
        plan = page.locator("#plbPlan").bounding_box()
        self.assertGreater(avail["x"], plan["x"])
        pointing = page.evaluate(
            """() => {
              const dir = (id) => {
                const m = new DOMMatrix(getComputedStyle(document.querySelector('#' + id + ' svg')).transform);
                return m.a < 0 ? 'left' : 'right';
              };
              return [dir('plbXAdd'), dir('plbXAddAll'), dir('plbXRem'), dir('plbXRemAll')];
            }"""
        )
        self.assertEqual(pointing, ["left", "left", "right", "right"])
        # and in English the plan is on the right and the arrow points right
        page_en, _ = self._page()
        pointing = page_en.evaluate(
            """() => ['plbXAdd', 'plbXRem'].map((id) => {
              const m = new DOMMatrix(getComputedStyle(document.querySelector('#' + id + ' svg')).transform);
              return m.a < 0 ? 'left' : 'right';
            })"""
        )
        self.assertEqual(pointing, ["right", "left"])

    def test_on_a_phone_the_columns_stack_and_nothing_overflows(self) -> None:
        page, fake = self._page(width=390, height=844)
        self.assertEqual(page.evaluate(OVERFLOWS), [])
        avail = page.locator("#plbAvail").bounding_box()
        xfer = page.locator("#plbXAdd").bounding_box()
        plan = page.locator("#plbPlan").bounding_box()
        self.assertLessEqual(avail["y"] + avail["height"], xfer["y"] + 1)
        self.assertLessEqual(xfer["y"] + xfer["height"], plan["y"] + 1)
        self._source(page, "eligible")
        page.click(f"{ROW.format('STAT305')} button[data-act='open']")
        self._sections_pop(page, "AI331")
        self.assertEqual(page.evaluate(OVERFLOWS), [])
        sizes = page.evaluate(
            """() => [...document.querySelectorAll('#plbPick button')]
              .filter((b) => b.getClientRects().length && !b.closest('#plbPop'))
              .map((b) => [b.dataset.act || b.dataset.xfer || b.id, Math.round(b.getBoundingClientRect().height)])"""
        )
        small = [s for s in sizes if s[1] < 44]
        self.assertEqual(small, [])

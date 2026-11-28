"""The planner screen on phones (<= 768 px), in a real browser: the picker's two-tab switch, the "+"
on every available row, the section popover as a sheet, the summary line and the full-width Build,
the option tabs and the week scrolling inside their own boxes, and 44 px targets - at 390 and 360,
English and Arabic. Same synthetic student and fakes as the other board tests.
"""

from __future__ import annotations

from tests.planner_board_support import BuildFake, PlannerBoardCase, expect
from tests.test_planner_board_picker_browser import CATALOG

SIZES = (390, 360)
PAGE_OVERFLOW = "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
VISIBLE = "#plbAvail, #plbPlan"


class PlannerBoardPhoneTests(PlannerBoardCase):
    def _phone(self, width=390, language="en", built=False):
        fake = BuildFake()
        fake.catalog_payload = CATALOG
        if built:
            page, fake = self._built(fake, width=width, height=900, language=language)
        else:
            page = self._open(fake, width=width, height=900, language=language)
        return page, fake

    @staticmethod
    def _shown(page, selector) -> bool:
        return page.locator(selector).is_visible()

    # ── the two-tab switch ────────────────────────────────────────────────────────────────
    def test_the_switch_shows_one_panel_and_keeps_its_choice(self) -> None:
        page, _fake = self._phone()
        tabs = page.locator("#plbPickTabs")
        self.assertEqual(tabs.get_attribute("role"), "tablist")
        self.assertEqual(page.locator("#plbPickTabAvail").inner_text().split(" · ")[0], "Available")
        self.assertTrue(self._shown(page, "#plbAvail"))
        self.assertFalse(self._shown(page, "#plbPlan"))
        self.assertFalse(self._shown(page, ".plb-xfer"))
        page.click("#plbPickTabPlan")
        self.assertTrue(self._shown(page, "#plbPlan"))
        self.assertFalse(self._shown(page, "#plbAvail"))
        self.assertEqual(page.get_attribute("#plbPickTabPlan", "aria-selected"), "true")
        # a redraw (the search box) keeps the chosen panel
        page.fill("#plbFind", "AI")
        self.assertTrue(self._shown(page, "#plbPlan"))
        self.assertEqual(page.inner_text("#plbPickTabPlan").split(" · ")[0], "Your plan")
        # a desktop width shows both panels and no switch
        page.set_viewport_size({"width": 1440, "height": 900})
        self.assertTrue(self._shown(page, "#plbAvail") and self._shown(page, "#plbPlan"))
        self.assertFalse(self._shown(page, "#plbPickTabs"))

    # ── the "+" on every row ──────────────────────────────────────────────────────────────
    def test_plus_adds_the_whole_course_and_stays_on_available(self) -> None:
        page, _fake = self._phone()
        plus = page.locator("#plbAvailList .plb-addbtn[data-code='STAT305']")
        self.assertEqual(plus.get_attribute("aria-label"), "Add STAT305")
        box = plus.bounding_box()
        self.assertGreaterEqual(min(box["width"], box["height"]), 43.9)
        before = page.locator("#plbPlanList .plb-prow:not(.plb-prow--kept)").count()
        plus.click()
        self.assertEqual(
            page.locator("#plbPlanList .plb-prow:not(.plb-prow--kept)").count(), before + 1
        )
        self.assertEqual(page.locator("#plbZoneCourses").get_attribute("data-tab"), "avail")
        self.assertTrue(self._shown(page, "#plbAvail"))
        self.assertEqual(page.locator("#plbAvailList .plb-addbtn[data-code='STAT305']").count(), 0)
        page.click("#plbPickTabPlan")
        self.assertEqual(
            page.locator("#plbPlanList .plb-prow[data-code='STAT305'] .plb-secbtn").get_attribute(
                "data-limited"
            ),
            None,
        )

    def test_plus_adds_only_the_ticked_sections(self) -> None:
        page, _fake = self._phone()
        page.click("#plbAvailList .plb-chev[data-code='CS351']")
        page.locator("#plbAvailList .plb-sec[data-tsid='14']").click()
        plus = page.locator("#plbAvailList .plb-addbtn[data-code='CS351']")
        self.assertEqual(plus.get_attribute("aria-label"), "Add CS351, section M1")
        plus.click()
        page.click("#plbPickTabPlan")
        expect(
            page.locator("#plbPlanList .plb-prow[data-code='CS351'] .plb-secbtn")
        ).to_have_attribute("data-limited", "true")

    # ── the summary line, Build, the popover sheet ────────────────────────────────────────
    def test_the_summary_line_and_a_full_width_build(self) -> None:
        for language in ("en", "ar"):
            page, _fake = self._phone(language=language)
            sum_box = page.locator("#plbPhoneSum")
            expect(sum_box).to_be_visible()
            text = sum_box.inner_text()
            self.assertRegex(
                text, r"\d+" if language == "ar" else r"^\d+ courses? · \d+ of \d+ cr", msg=language
            )
            zone = page.locator("#plbZoneCourses").bounding_box()
            build = page.locator("#plbBuild").bounding_box()
            self.assertGreaterEqual(build["width"], zone["width"] - 2, msg=language)
            self.assertGreaterEqual(build["height"], 43.9, msg=language)
            # visible whichever panel is shown
            page.click("#plbPickTabPlan")
            expect(sum_box).to_be_visible()
        # the warning when over the limit
        page.fill("#plbMaxIn", "3")
        page.press("#plbMaxIn", "Enter")
        self.assertEqual(sum_box.get_attribute("data-over"), "true")

    def test_the_section_popover_is_a_full_width_sheet_under_its_row(self) -> None:
        for language in ("en", "ar"):
            page, _fake = self._phone(language=language)
            page.click("#plbPickTabPlan")
            row = page.locator("#plbPlanList .plb-prow[data-code='AI331']")
            row.locator(".plb-secbtn").click()
            pop = page.locator("#plbPop")
            expect(pop).to_be_visible()
            self.assertEqual(
                pop.evaluate("n => getComputedStyle(n).position"), "static", msg=language
            )
            pb, rb = pop.bounding_box(), row.bounding_box()
            lb = page.locator("#plbPlanList").bounding_box()
            self.assertGreaterEqual(pb["y"], rb["y"] + rb["height"] - 1, msg=language)
            self.assertGreaterEqual(pb["width"], lb["width"] - 2, msg=language)
            self.assertLessEqual(page.evaluate(PAGE_OVERFLOW), 0, msg=language)

    # ── scrolling inside boxes, no page overflow ──────────────────────────────────────────
    def test_the_tabs_and_the_week_scroll_inside_their_boxes(self) -> None:
        for language in ("en", "ar"):
            for width in SIZES:
                page, _fake = self._phone(width=width, language=language, built=True)
                msg = (language, width)
                self.assertLessEqual(page.evaluate(PAGE_OVERFLOW), 0, msg=msg)
                tops = page.eval_on_selector_all(
                    "#plbTabs .plb-tab",
                    "ts => ts.map(t => Math.round(t.getBoundingClientRect().top))",
                )
                self.assertGreater(len(tops), 1, msg=msg)
                self.assertEqual(len(set(tops)), 1, msg=msg)  # one row: no wrap into a stack
                box = page.locator("#plbTabs").evaluate(
                    "n => [n.scrollWidth, n.clientWidth, getComputedStyle(n).overflowX]"
                )
                self.assertGreater(box[0], box[1], msg=msg)
                self.assertEqual(box[2], "auto", msg=msg)
                wk = page.locator("#plbWeek .plb-wkwrap").first.evaluate(
                    "n => [n.scrollWidth, n.clientWidth]"
                )
                self.assertGreater(wk[0], wk[1], msg=msg)
                self.assertLessEqual(wk[1], width, msg=msg)
                page.click("#plbPickTabPlan")
                self.assertLessEqual(page.evaluate(PAGE_OVERFLOW), 0, msg=msg)

    def test_the_apply_bar_and_the_glance_stack_on_a_phone(self) -> None:
        page, _fake = self._phone(built=True)
        review = page.locator("#plbReview").bounding_box()
        notes = page.locator("#plbActs .plb-applynotes").bounding_box()
        # The button fills the bar's content box (the bar is a padded card).
        inner = page.locator("#plbActs").evaluate(
            "n => { const s = getComputedStyle(n); return n.clientWidth"
            " - parseFloat(s.paddingLeft) - parseFloat(s.paddingRight); }"
        )
        self.assertGreaterEqual(review["width"], inner - 2)
        self.assertGreaterEqual(review["y"], notes["y"] + notes["height"] - 1)
        week = page.locator("#plbWeekPanel").bounding_box()
        glance = page.locator("#plbGlance").bounding_box()
        self.assertGreaterEqual(glance["y"], week["y"] + week["height"] - 1)

    def test_the_masthead_and_the_rules_fill_the_width(self) -> None:
        page, _fake = self._phone(built=True)
        sid = page.locator("#plbLoad .plb-cap").first.bounding_box()
        term = page.locator("#plbTermBtn").bounding_box()
        fetch = page.locator("#plbFetch").bounding_box()
        self.assertGreater(term["y"], sid["y"])
        self.assertAlmostEqual(term["y"], fetch["y"], delta=1)
        self.assertGreaterEqual(min(term["height"], fetch["height"]), 43.9)
        rows = page.eval_on_selector_all(
            ".plb-rc-row", "rs => rs.map(r => Math.round(r.getBoundingClientRect().top))"
        )
        self.assertEqual(len(rows), len(set(rows)))  # each rule on its own line
        for seg in page.locator(".plb-seg").all():
            parts = seg.locator(".plb-seg-o").evaluate_all(
                "os => os.map(o => o.getBoundingClientRect().width)"
            )
            self.assertAlmostEqual(parts[0], parts[1], delta=1)

    # ── touch targets ─────────────────────────────────────────────────────────────────────
    def test_every_control_of_zones_two_and_three_is_44px_on_a_phone(self) -> None:
        small = """() => {
          const out = [];
          const sel = '#plbZoneCourses, #plbZoneOptions';
          const controls = document.querySelectorAll(
            sel.split(', ').map((z) => z + ' button, ' + z + ' input, ' + z + ' select, ' + z + ' summary, ' + z + ' [role=tab]').join(', '));
          for (const c of controls) {
            if (c.closest('.plb-b, .plb-gb, [hidden], .sr-only')) continue;      // a block of the week is a picture
            if (c.type === 'hidden') continue;
            const target = c.tagName === 'INPUT' ? (c.closest('label') || c) : c;
            const r = target.getBoundingClientRect();
            if (!r.width || !r.height) continue;
            if (Math.min(r.width, r.height) < 43.5) out.push((c.id || c.className || c.tagName) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
          }
          return out;
        }"""
        for language in ("en", "ar"):
            for width in SIZES:
                page, _fake = self._phone(width=width, language=language, built=True)
                page.click("#plbAvailList .plb-chev[data-code='CS351']")
                self.assertEqual(page.evaluate(small), [], msg=(language, width, "avail"))
                page.click("#plbPickTabPlan")
                page.click("#plbPlanList .plb-prow[data-code='AI331'] .plb-secbtn")
                self.assertEqual(page.evaluate(small), [], msg=(language, width, "plan"))

"""The planner board, part eight: the rules where advisers look for them, and a clearer first screen.

All the rules are on the board itself (zone 01); there is no rules dialog. Eligibility over an
edited course list asks with an inline confirm. Same synthetic student and fakes as the other board tests (tests/planner_board_support.py).
"""

from __future__ import annotations

import re

from tests.planner_board_support import (
    OTHER_ID,
    OVERFLOWS,
    STUDENT_ID,
    BuildFake,
    PlannerBoardCase,
    context_for,
    expect,
)

BODY_KEYS = (
    "mode",
    "eligibility_mode",
    "swap",
    "program_sections_only",
    "allow_full_sections",
    "max_credits",
)
MUST_TAKE_NAME = "Must-take"

# Where a box is, measured: the page decides, never a number of this file.
BOX = "(s) => { const r = document.querySelector(s).getBoundingClientRect(); return {l: r.left, r: r.right, t: r.top, b: r.bottom, h: r.height, w: r.width}; }"


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


class PlannerBoardRulesInlineTests(PlannerBoardCase):
    # ── helpers ──────────────────────────────────────────────────────────────
    def _built_body(self, page, fake) -> dict:
        before = len(fake.build_bodies)
        page.click("#plbBuild")
        for _ in range(200):
            if len(fake.build_bodies) > before:
                break
            page.wait_for_timeout(20)
        self.assertEqual(len(fake.build_bodies), before + 1)
        expect(page.locator("#plbBuild")).to_be_enabled()
        return fake.build_bodies[-1]

    def _same_as_dialog(self, in_place, dialog, **kwargs) -> None:
        """The build request after a change made in place equals the one after the dialog's."""
        fake_a, fake_b = BuildFake(), BuildFake()
        page_a = self._open(fake_a, **kwargs)
        in_place(page_a)
        body_a = self._built_body(page_a, fake_a)
        page_b = self._open(fake_b, **kwargs)
        self._rules(page_b, **dialog)
        body_b = self._built_body(page_b, fake_b)
        self.assertEqual(body_a, body_b)
        # ... and the rule really changed (the comparison is not between two untouched pages).
        self.assertNotEqual(
            {k: body_a[k] for k in BODY_KEYS},
            {k: BuildFake_default_body()[k] for k in BODY_KEYS},
        )

    # ── the same request body as the dialog ─────────────────────────────────
    def test_ignore_registered_in_place_sends_the_dialogs_request(self) -> None:
        self._same_as_dialog(lambda p: p.click("#plbRegIgnore"), {"mode": "ignore"})

    def test_relaxed_eligibility_in_place_sends_the_dialogs_request(self) -> None:
        self._same_as_dialog(lambda p: p.click("#plbEligRelaxed"), {"elig": "relaxed"})

    def test_max_credits_in_place_sends_the_dialogs_request(self) -> None:
        def typed(page) -> None:
            page.fill("#plbMaxIn", "15")
            page.press("#plbMaxIn", "Enter")

        self._same_as_dialog(typed, {"max": 15})

    def test_the_same_requests_in_arabic(self) -> None:
        self._same_as_dialog(lambda p: p.click("#plbRegIgnore"), {"mode": "ignore"}, language="ar")
        self._same_as_dialog(
            lambda p: p.click("#plbEligRelaxed"), {"elig": "relaxed"}, language="ar"
        )

    # ── a build on screen is marked out of date, as by the dialog ───────────
    def test_a_change_in_place_marks_a_build_out_of_date_as_the_dialog_does(self) -> None:
        def after(change) -> tuple:
            page, _ = self._built()
            expect(page.locator("#plbActs")).to_have_attribute("data-stale", "")
            change(page)
            expect(page.locator("#plbActs")).to_have_attribute("data-stale", "changed")
            return (
                _squash(page.locator("#plbBuildLine").text_content()),
                page.locator("#plbReview").is_disabled(),
                page.locator("#plbTabs [role='tab']").count() > 1,  # the options stay on screen
                _squash(page.locator("#plbRulesChanged").text_content()),
            )

        for in_place, dialog in (
            (lambda p: p.click("#plbRegIgnore"), {"mode": "ignore"}),
            (lambda p: p.click("#plbEligRelaxed"), {"elig": "relaxed"}),
            (lambda p: (p.fill("#plbMaxIn", "15"), p.press("#plbMaxIn", "Enter")), {"max": 15}),
        ):
            with self.subTest(dialog=dialog):
                self.assertEqual(after(in_place), after(lambda p, d=dialog: self._rules(p, **d)))

    def test_a_max_credits_that_is_not_a_change_marks_nothing(self) -> None:
        page, _ = self._built()
        page.fill("#plbMaxIn", "18")
        page.press("#plbMaxIn", "Enter")
        expect(page.locator("#plbActs")).to_have_attribute("data-stale", "")
        page.click("#plbRegKeep")  # already on: nothing happens
        expect(page.locator("#plbActs")).to_have_attribute("data-stale", "")

    # ── eligibility: reloads at once with an unchanged list ─────────────────
    def test_eligibility_in_place_reloads_the_suggestions_at_once_with_an_unchanged_list(
        self,
    ) -> None:
        fake = BuildFake()
        page = self._open(fake)
        self.assertEqual(len(fake.context_bodies), 1)
        page.click("#plbEligRelaxed")
        expect(page.locator("#plbEligConfirm")).to_be_hidden()
        expect(page.locator("#plbEligRelaxed")).to_be_checked()
        for _ in range(100):
            if len(fake.context_bodies) == 2:
                break
            page.wait_for_timeout(20)
        self.assertEqual(
            [b["eligibility_mode"] for b in fake.context_bodies], ["strict", "relaxed"]
        )
        expect(page.locator("#plbEligRelaxed")).to_be_checked()

    # ── eligibility: a changed list asks first ──────────────────────────────
    def _changed_list(self):
        fake = BuildFake()
        page = self._open(fake)
        self._remove(page, "AI342")
        self.assertEqual(self._rows(page, "new"), ["AI331"])
        return page, fake

    def test_eligibility_over_a_changed_list_asks_inline_and_applies_nothing(self) -> None:
        page, fake = self._changed_list()
        page.click("#plbEligRelaxed")
        expect(page.locator("#plbEligConfirm")).to_be_visible()
        self.assertEqual(page.get_attribute("#plbEligConfirm", "role"), "group")
        self.assertIn("reloads the suggested courses", self._text(page, "#plbEligConfirm"))
        self.assertEqual(self._text(page, "#plbEligGo"), "Change and reset")
        self.assertEqual(self._text(page, "#plbEligKeep"), "Keep current")
        expect(page.locator("#plbEligGo")).to_be_focused()
        # Nothing has happened yet: the switch shows the rule in force, nothing was reloaded.
        expect(page.locator("#plbEligStrict")).to_be_checked()
        self.assertEqual(len(fake.context_bodies), 1)
        self.assertEqual(self._rows(page, "new"), ["AI331"])

    def test_change_and_reset_confirms_an_eligibility_change_over_a_changed_list(self) -> None:
        page, fake = self._changed_list()
        page.click("#plbEligRelaxed")
        page.click("#plbEligGo")
        expect(page.locator("#plbEligConfirm")).to_be_hidden()
        expect(page.locator("#plbEligRelaxed")).to_be_checked()
        expect(page.locator("#plbPlanList .plb-prow[data-code='AI342']")).to_be_visible()
        self.assertEqual(self._rows(page, "new"), ["AI331", "AI342"])  # the suggestions came back
        self.assertEqual(fake.context_bodies[-1]["eligibility_mode"], "relaxed")
        expect(page.locator("#plbReset")).to_be_disabled()

    def test_keep_current_and_escape_leave_everything_as_it_was_the_switch_included(self) -> None:
        page, fake = self._changed_list()
        page.click("#plbEligRelaxed")
        page.click("#plbEligKeep")
        expect(page.locator("#plbEligConfirm")).to_be_hidden()
        expect(page.locator("#plbEligStrict")).to_be_checked()
        expect(page.locator("#plbEligStrict")).to_be_focused()
        expect(page.locator("#plbEligRelaxed")).not_to_be_checked()
        self.assertEqual(len(fake.context_bodies), 1)
        self.assertEqual(self._rows(page, "new"), ["AI331"])
        self.assertTrue(page.locator("#plbRulesChanged").is_hidden())
        # Escape is a Keep too.
        page.click("#plbEligRelaxed")
        expect(page.locator("#plbEligConfirm")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#plbEligConfirm")).to_be_hidden()
        expect(page.locator("#plbEligStrict")).to_be_checked()
        expect(page.locator("#plbEligStrict")).to_be_focused()

    def test_every_rule_changes_in_place_and_the_other_rules_ride_along(self) -> None:
        fake = BuildFake()
        page = self._open(fake)
        self._rules(page, mode="ignore", elig="relaxed", max=15, prog=False, full=True)
        expect(page.locator("#plbRegIgnore")).to_be_checked()
        expect(page.locator("#plbEligRelaxed")).to_be_checked()
        self.assertEqual(page.input_value("#plbMaxIn"), "15")
        expect(page.locator("#plbProgAny")).to_be_checked()
        expect(page.locator("#plbFullIn")).to_be_checked()

    def test_programme_and_full_section_rules_reach_the_build_request(self) -> None:
        fake = BuildFake()
        page = self._open(fake)
        self.assertTrue(self._built_body(page, fake)["program_sections_only"])
        self._rules(page, prog=False, full=True)
        body = self._built_body(page, fake)
        self.assertFalse(body["program_sections_only"])
        self.assertTrue(body["allow_full_sections"])
        self.assertFalse(body["swap"])  # Suggest swaps is not available yet
        self._rules(page, prog=True, full=False)
        body = self._built_body(page, fake)
        self.assertTrue(body["program_sections_only"])
        self.assertFalse(body["allow_full_sections"])

    # ── max credits ─────────────────────────────────────────────────────────
    def test_max_credits_shows_the_cap_and_no_hint_line(self) -> None:
        page = self._open(BuildFake())
        self.assertEqual(page.input_value("#plbMaxIn"), "18")
        self.assertEqual(page.locator("#plbMaxInHint").count(), 0)  # no hint line at all

    def test_max_credits_applies_on_enter_and_when_the_field_loses_focus(self) -> None:
        page = self._open(BuildFake())
        page.fill("#plbMaxIn", "16")
        page.press("#plbMaxIn", "Enter")
        self.assertEqual(self._rules_now(page), {"mode": "keep", "elig": "strict", "max": "16"})
        self.assertEqual(self._load(page), ("15 / 16 cr", "6 planned + 9 registered · 1 cr free"))
        page.fill("#plbMaxIn", "17")
        page.press("#plbMaxIn", "Tab")
        self.assertEqual(self._load(page), ("15 / 17 cr", "6 planned + 9 registered · 2 cr free"))
        expect(page.locator("#plbMaxInErr")).to_be_hidden()
        # Empty is "not set", as in the dialog.
        page.fill("#plbMaxIn", "")
        page.press("#plbMaxIn", "Enter")
        self.assertEqual(self._rules_now(page)["max"], "")

    def test_an_invalid_max_credits_is_refused_with_the_dialogs_message(self) -> None:
        for bad in ("0", "20", "abc", "1.5", "-3", "100"):
            with self.subTest(bad=bad):
                fake = BuildFake()
                page = self._open(fake)
                page.fill("#plbMaxIn", bad)
                page.press("#plbMaxIn", "Enter")
                expect(page.locator("#plbMaxInErr")).to_have_text(
                    "Enter a whole number from 1 to 19."
                )
                expect(page.locator("#plbMaxIn")).to_have_attribute("aria-invalid", "true")
                self.assertEqual(
                    self._load(page), ("15 / 18 cr", "6 planned + 9 registered · 3 cr free")
                )
                self.assertTrue(page.locator("#plbRulesChanged").is_hidden())
                # Leaving the field does not apply it either.
                page.press("#plbMaxIn", "Tab")
                self.assertEqual(
                    self._load(page), ("15 / 18 cr", "6 planned + 9 registered · 3 cr free")
                )
                self.assertEqual(self._built_body(page, fake)["max_credits"], 18)
                page.context.close()

    def test_a_valid_value_clears_the_refusal(self) -> None:
        page = self._open(BuildFake())
        page.fill("#plbMaxIn", "99")
        page.press("#plbMaxIn", "Enter")
        expect(page.locator("#plbMaxInErr")).to_be_visible()
        page.fill("#plbMaxIn", "12")
        page.press("#plbMaxIn", "Enter")
        expect(page.locator("#plbMaxInErr")).to_be_hidden()
        expect(page.locator("#plbMaxIn")).not_to_have_attribute("aria-invalid", "true")
        self.assertEqual(
            self._load(page), ("15 / 12 cr", "6 planned + 9 registered · 3 cr over the limit")
        )

    # ── the switches by keyboard ────────────────────────────────────────────
    def test_the_switches_are_radio_groups_and_the_arrow_keys_move_between_the_two(self) -> None:
        fake = BuildFake()
        page = self._open(fake)
        groups = page.eval_on_selector_all(
            ".plb-seg",
            "g => g.map(x => [x.getAttribute('role'), x.querySelectorAll('input[type=radio]').length])",
        )
        self.assertEqual(groups, [["radiogroup", 2]] * 3)
        for label in ("#plbRegLabel", "#plbEligLabel"):
            self.assertTrue(_squash(page.locator(label).text_content()))
        page.focus("#plbRegKeep")
        page.keyboard.press("ArrowDown")
        expect(page.locator("#plbRegIgnore")).to_be_checked()
        expect(page.locator("#plbRegIgnore")).to_be_focused()
        self.assertEqual(self._rules_now(page)["mode"], "ignore")
        page.keyboard.press("ArrowUp")
        expect(page.locator("#plbRegKeep")).to_be_checked()
        page.focus("#plbEligStrict")
        page.keyboard.press("ArrowDown")
        expect(page.locator("#plbEligRelaxed")).to_be_checked()
        self.assertEqual(self._built_body(page, fake)["eligibility_mode"], "relaxed")

    def test_in_arabic_the_controls_say_arabic_and_work(self) -> None:
        page = self._open(BuildFake(), language="ar")
        self.assertEqual(self._text(page, "#plbRegLabel"), "المقررات المسجّلة")
        self.assertEqual(self._text(page, "#plbEligLabel"), "المتطلبات السابقة")
        self.assertEqual(self._text(page, "#plbRulesReset"), "استعادة")
        self.assertEqual(
            page.eval_on_selector_all(
                ".plb-seg-o", "n => n.map(x => x.textContent.replace(/\\s+/g, ' ').trim())"
            ),
            ["إبقاء", "تجاهل", "صارمة", "مرنة", "هذا البرنامج", "أي برنامج (للمقارنة فقط)"],
        )
        page.click("#plbRegIgnore")
        expect(page.locator("#plbRegIgnore")).to_be_checked()
        self.assertEqual(self._rules_now(page)["mode"], "ignore")
        page.fill("#plbMaxIn", "99")
        page.press("#plbMaxIn", "Enter")
        expect(page.locator("#plbMaxInErr")).to_have_text("أدخل عددًا صحيحًا من 1 إلى 19.")
        self.assertEqual(page.locator("#plbMaxInHint").count(), 0)  # no hint line at all

    def test_the_other_rules_are_on_the_board_and_swaps_are_not_available(self) -> None:
        page = self._open(BuildFake())
        self.assertEqual(self._text(page, "#plbRulesReset"), "Reset")
        expect(page.locator("#plbProgOnly")).to_be_checked()
        expect(page.locator("#plbFullIn")).to_be_visible()
        expect(page.locator("#plbSwapIn")).to_be_disabled()
        self.assertIn("(not available yet)", self._text(page, "#plbZoneRules"))
        self.assertEqual(page.locator("dialog#plbRulesDialog").count(), 0)

    # ── before a student is loaded ──────────────────────────────────────────
    def test_the_controls_work_before_a_student_is_loaded(self) -> None:
        fake = BuildFake()
        page = self._open(fake, load=False)
        page.click("#plbRegIgnore")
        page.click("#plbEligRelaxed")
        expect(page.locator("#plbEligConfirm")).to_be_hidden()  # no list to lose
        page.fill("#plbMaxIn", "15")
        page.press("#plbMaxIn", "Enter")
        self.assertEqual(self._rules_now(page), {"mode": "ignore", "elig": "relaxed", "max": "15"})
        self.assertEqual(fake.context_bodies, [])  # no student: nothing to reload
        self._fetch(page, STUDENT_ID)
        self._eligible(page)
        self.assertEqual(fake.context_bodies[0]["eligibility_mode"], "relaxed")
        expect(page.locator("#plbRegIgnore")).to_be_checked()
        expect(page.locator("#plbEligRelaxed")).to_be_checked()

    # ── touch and size ──────────────────────────────────────────────────────
    def test_every_control_has_the_boards_touch_size_on_a_phone(self) -> None:
        page = self._open(BuildFake(), width=390, height=844)
        self._remove(page, "AI342")  # shows Reset to suggestions
        heights = page.evaluate(
            """() => ['#plbRulesReset', '#plbMaxIn', '.plb-seg-o', '#plbReset']
              .flatMap(s => [...document.querySelectorAll(s)].map(n => [s, n.getBoundingClientRect().height]))"""
        )
        self.assertEqual(len(heights), 1 + 1 + 6 + 1)
        self.assertEqual([h for h in heights if h[1] < 43.5], [])
        # the radios under the labels are as big as their labels
        radios = page.evaluate(
            "[...document.querySelectorAll('.plb-seg-o input')].map(n => n.getBoundingClientRect().height)"
        )
        self.assertEqual([h for h in radios if h < 43.5], [])

    def test_the_controls_are_32px_on_a_laptop(self) -> None:
        page = self._open(BuildFake())
        heights = page.evaluate(
            """() => ['#plbMaxIn', '.plb-seg-o'].flatMap(s => [...document.querySelectorAll(s)]
              .map(n => n.getBoundingClientRect().height))"""
        )
        self.assertEqual([h for h in heights if h < 31.5], [])

    def test_a_focused_switch_shows_its_ring_and_the_checked_one_is_marked(self) -> None:
        page = self._open(BuildFake())
        page.keyboard.press("Shift")
        page.focus("#plbRegKeep")
        outline = page.evaluate(
            "getComputedStyle(document.querySelector('#plbRegKeep').closest('.plb-seg-o')).outlineStyle"
        )
        self.assertNotEqual(outline, "none")
        keep = page.evaluate(
            "getComputedStyle(document.querySelector('#plbRegKeep').closest('.plb-seg-o')).backgroundColor"
        )
        ignore = page.evaluate(
            "getComputedStyle(document.querySelector('#plbRegIgnore').closest('.plb-seg-o')).backgroundColor"
        )
        self.assertNotEqual(keep, ignore)
        page.evaluate("document.documentElement.setAttribute('data-theme', 'dark')")
        dark = page.evaluate(
            "getComputedStyle(document.querySelector('#plbRegKeep').closest('.plb-seg-o')).backgroundColor"
        )
        self.assertNotEqual(dark, keep)  # the tokens turn with the theme

    def test_nothing_overflows_at_three_widths_in_both_languages_and_both_fonts(self) -> None:
        for language in ("en", "ar"):
            for font in ("app", "wide"):
                for width in (1440, 1152, 390):
                    with self.subTest(language=language, font=font, width=width):
                        page = self._open(
                            BuildFake(), language=language, font=font, width=width, height=900
                        )
                        self.assertEqual(page.evaluate(OVERFLOWS), [])
                        # the refusal line is the longest thing the box can show
                        page.fill("#plbMaxIn", "99")
                        page.press("#plbMaxIn", "Enter")
                        expect(page.locator("#plbMaxInErr")).to_be_visible()
                        self.assertEqual(page.evaluate(OVERFLOWS), [])
                        page.context.close()

    # ── the first screen ────────────────────────────────────────────────────
    def test_the_student_id_field_has_the_keyboard_when_the_empty_page_opens(self) -> None:
        page = self._open(BuildFake(), load=False)
        expect(page.locator("#plbStudentId")).to_be_focused()
        # Once: moving on is respected, nothing takes the keyboard back.
        page.focus("#plbFetch")
        page.click("#plbRegIgnore")
        page.fill("#plbMaxIn", "15")
        page.press("#plbMaxIn", "Enter")
        page.wait_for_timeout(300)
        self.assertNotEqual(page.evaluate("document.activeElement.id"), "plbStudentId")

    def test_a_loaded_student_does_not_give_the_field_the_keyboard_back(self) -> None:
        page = self._open(BuildFake())
        expect(page.locator("#plbStudentId")).not_to_be_focused()
        page.click("#plbFetch")
        page.wait_for_timeout(300)
        expect(page.locator("#plbStudentId")).not_to_be_focused()

    def test_a_deep_link_loads_the_student_and_the_field_does_not_take_the_keyboard(self) -> None:
        page = self._open(BuildFake(), load=False)
        page.goto(f"{self.live_server_url}/planner/?student={STUDENT_ID}&year=1448&term=1")
        self._eligible(page)
        page.wait_for_timeout(200)
        expect(page.locator("#plbStudentId")).not_to_be_focused()
        self.assertEqual(page.input_value("#plbStudentId"), STUDENT_ID)

    def test_build_options_looks_disabled_until_a_student_is_loaded(self) -> None:
        read = """() => {
          const s = getComputedStyle(document.getElementById('plbBuild'));
          return {bg: s.backgroundColor, color: s.color, opacity: s.opacity, cursor: s.cursor,
                  pointer: s.pointerEvents};
        }"""
        page = self._open(BuildFake(), load=False)
        expect(page.locator("#plbBuild")).to_be_disabled()
        off = page.evaluate(read)
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbBuild")).to_be_enabled()
        page.mouse.move(0, 0)
        page.wait_for_function("() => document.getAnimations().length === 0")
        on = page.evaluate(read)
        # The design system's disabled look: faded, the not-allowed cursor, no pointer events.
        self.assertLess(float(off["opacity"]), 0.9)
        self.assertEqual(off["cursor"], "not-allowed")
        self.assertEqual(off["pointer"], "none")
        # ... and the enabled one is the full-strength button.
        self.assertEqual(on["opacity"], "1")
        self.assertNotEqual(on["cursor"], "not-allowed")
        self.assertEqual(on["pointer"], "auto")
        review_off = page.evaluate(read.replace("plbBuild", "plbReview"))
        self.assertEqual(
            review_off["opacity"], off["opacity"]
        )  # as quiet as the other disabled one

    # ── the layout ──────────────────────────────────────────────────────────
    def test_reset_to_suggestions_sits_in_the_plan_header_and_only_when_the_list_differs(
        self,
    ) -> None:
        for language in ("en", "ar"):
            with self.subTest(language=language):
                page = self._open(BuildFake(), language=language)
                self.assertTrue(
                    page.evaluate("!!document.querySelector('#plbPlan .plb-ph #plbReset')")
                )
                # nothing to reset while the list is the suggested one
                expect(page.locator("#plbReset")).to_be_hidden()
                self._remove(page, "AI342")
                expect(page.locator("#plbReset")).to_be_visible()
                head = page.evaluate(BOX, "#plbPlanCount")
                reset = page.evaluate(BOX, "#plbReset")
                # on the heading's line, not under it
                self.assertLess(reset["t"], head["b"])
                self.assertGreater(reset["b"], head["t"])
                if language == "en":
                    self.assertEqual(self._text(page, "#plbPlanCount"), "Your plan · 1 course")
                page.context.close()

    def test_reset_in_its_new_place_still_resets(self) -> None:
        page = self._open(BuildFake())
        expect(page.locator("#plbReset")).to_be_hidden()
        self._remove(page, "AI342")
        expect(page.locator("#plbReset")).to_be_enabled()
        page.click("#plbReset")
        self.assertEqual(self._rows(page, "new"), ["AI331", "AI342"])
        expect(page.locator("#plbReset")).to_be_hidden()
        expect(page.locator("#plbFind")).to_be_focused()

    def test_the_source_and_the_search_share_one_line_at_1440_and_1152(self) -> None:
        for font in ("app", "wide"):
            for width in (1440, 1152):
                with self.subTest(font=font, width=width):
                    page = self._open(BuildFake(), font=font, width=width, height=900)
                    source = page.evaluate(BOX, "#plbSource")
                    find = page.evaluate(BOX, "#plbFind")
                    # the same line: the search field starts above the select's bottom
                    self.assertLess(find["t"], source["b"], (source, find))
                    page.context.close()

    def test_the_view_link_is_on_its_own_line_under_the_reason(self) -> None:
        for font in ("app", "wide"):
            for width in (1440, 1152):
                with self.subTest(font=font, width=width):
                    page, _ = self._built(font=font, width=width, height=900)
                    tabs = page.eval_on_selector_all(
                        "#plbTabs [data-opt]", "t => t.map(x => x.dataset.opt)"
                    )
                    for name in tabs:
                        page.click(f"#plbTabs [data-opt='{name}']")
                        if page.locator("#plbGuideView").count():
                            break
                    expect(page.locator("#plbGuideView")).to_be_visible()
                    reason = page.evaluate(BOX, "#plbGuide .plb-fr .plb-gv-row--own .plb-gx")
                    link = page.evaluate(BOX, "#plbGuideView")
                    self.assertGreaterEqual(
                        (link["t"] + link["b"]) / 2, reason["b"] - 1, (reason, link)
                    )
                    row = page.evaluate(BOX, "#plbGuide .plb-gv-row--own")
                    self.assertLessEqual(abs(link["l"] - row["l"]), 1)  # at the start of the line
                    # the reason takes the whole line, so nothing can sit beside it
                    self.assertAlmostEqual(reason["w"], row["w"], delta=1)
                    # (a short reason would otherwise leave room for the link beside it)
                    self.assertEqual(
                        page.evaluate(
                            "getComputedStyle(document.querySelector('#plbGuide .plb-gv-row--own .plb-gx')).flexBasis"
                        ),
                        "100%",
                    )
                    self.assertEqual(page.evaluate(OVERFLOWS), [])
                    page.context.close()

    # ── digits, and what the field says after it applies ────────────────────
    def test_arabic_indic_and_persian_digits_are_read_as_digits_by_the_field(self) -> None:
        for typed, want in (("١٥", "15"), ("۱۲", "12")):
            with self.subTest(typed=typed, entry="field"):
                page = self._open(BuildFake())
                page.fill("#plbMaxIn", typed)
                page.press("#plbMaxIn", "Enter")
                expect(page.locator("#plbMaxInErr")).to_be_hidden()
                self.assertEqual(self._rules_now(page)["max"], want)
                self.assertEqual(
                    self._load(page),
                    (
                        f"15 / {want} cr",
                        "6 planned + 9 registered · "
                        + ("0 cr free" if want == "15" else "3 cr over the limit"),
                    ),
                )
                page.context.close()

    def test_the_field_shows_what_was_applied(self) -> None:
        page = self._open(BuildFake())
        page.fill("#plbMaxIn", "016")
        page.press("#plbMaxIn", "Enter")
        self.assertEqual(self._rules_now(page)["max"], "16")
        page.fill("#plbMaxIn", "017")
        page.press("#plbMaxIn", "Tab")
        self.assertEqual(self._rules_now(page)["max"], "17")
        page.fill("#plbMaxIn", "١٨")
        page.press("#plbMaxIn", "Tab")
        self.assertEqual(self._rules_now(page)["max"], "18")
        # a refused value is left as typed, with the refusal beside it
        page.fill("#plbMaxIn", "00")
        page.press("#plbMaxIn", "Enter")
        self.assertEqual(self._rules_now(page)["max"], "00")
        expect(page.locator("#plbMaxInErr")).to_be_visible()

    def test_a_loaded_student_updates_the_field_unless_it_is_being_edited(self) -> None:
        fake = BuildFake()
        caps = {STUDENT_ID: 18, OTHER_ID: 16}

        def answer(body):
            return 200, context_for(
                str(body["student_id"]),
                eligibility_mode=body["eligibility_mode"],
                credit_cap=caps[str(body["student_id"])],
            )

        fake.context_answer = answer
        page = self._open(fake)
        self._fetch(page, OTHER_ID)
        expect(page.locator("#plbMaxIn")).to_have_value("16")
        # In the middle of typing: the next student's cap does not overwrite it.
        page.focus("#plbMaxIn")
        page.keyboard.type("9")
        typed = page.input_value("#plbMaxIn")
        page.evaluate(
            f"document.getElementById('plbStudentId').value = '{STUDENT_ID}';"
            "document.getElementById('plbLoad').requestSubmit();"
        )
        expect(page.locator("#plbContext")).to_have_text(f"{STUDENT_ID} · 1448/1")
        self.assertEqual(page.input_value("#plbMaxIn"), typed)
        expect(page.locator("#plbMaxIn")).to_be_focused()
        # Out of the field, the next load puts the student's cap there.
        page.fill("#plbMaxIn", "18")
        page.press("#plbMaxIn", "Tab")
        self._fetch(page, OTHER_ID)
        expect(page.locator("#plbContext")).to_have_text(f"{OTHER_ID} · 1448/1")
        expect(page.locator("#plbMaxIn")).to_have_value("16")
        # Focused but not typed in: a load still puts the student's cap there.
        page.focus("#plbMaxIn")
        page.evaluate(
            f"document.getElementById('plbStudentId').value = '{STUDENT_ID}';"
            "document.getElementById('plbLoad').requestSubmit();"
        )
        expect(page.locator("#plbContext")).to_have_text(f"{STUDENT_ID} · 1448/1")
        expect(page.locator("#plbMaxIn")).to_have_value("18")
        expect(page.locator("#plbMaxIn")).to_be_focused()

    # ── the keyboard on the Eligibility switch ──────────────────────────────
    def test_an_arrow_key_on_eligibility_over_a_changed_list_opens_the_confirm_and_keep_returns(
        self,
    ) -> None:
        page, fake = self._changed_list()
        page.focus("#plbEligStrict")
        page.keyboard.press("ArrowDown")
        expect(page.locator("#plbEligConfirm")).to_be_visible()
        expect(page.locator("#plbEligGo")).to_be_focused()
        self.assertEqual(len(fake.context_bodies), 1)
        page.click("#plbEligKeep")
        expect(page.locator("#plbEligConfirm")).to_be_hidden()
        expect(page.locator("#plbEligStrict")).to_be_checked()
        expect(page.locator("#plbEligStrict")).to_be_focused()
        # Escape gives it back too, and the arrow works again from there.
        page.keyboard.press("ArrowDown")
        expect(page.locator("#plbEligConfirm")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#plbEligStrict")).to_be_focused()
        self.assertEqual(self._rows(page, "new"), ["AI331"])
        # Change and reset lands on the switch as well, now showing the new rule.
        page.keyboard.press("ArrowDown")
        page.click("#plbEligGo")
        expect(page.locator("#plbEligRelaxed")).to_be_checked()
        expect(page.locator("#plbEligRelaxed")).to_be_focused()


def BuildFake_default_body() -> dict:
    """The body of a build with no rule touched (what tests/planner_board_support.py pins)."""
    from tests.planner_board_support import EXPECTED_BUILD_BODY

    return EXPECTED_BUILD_BODY

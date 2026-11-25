"""The planner board, part eight: the rules where advisers look for them, and a clearer first screen.

"Keep or ignore registered", the eligibility and the max credits are on the board itself; the
dialog ("More rules") keeps all six and works as before. A change made in place must have exactly
the effects of the same change made in the dialog, so most tests below do both and compare.
Same synthetic student and fakes as the other board tests (tests/planner_board_support.py).
"""

from __future__ import annotations

import re

from tests.planner_board_support import (
    OVERFLOWS,
    STUDENT_ID,
    BuildFake,
    PlannerBoardCase,
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
        expect(page.locator("#plbRulesDialog")).not_to_be_visible()
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
        page.click("#plbTable tr[data-code='AI342'] [data-act='remove']")
        self.assertEqual(self._rows(page, "new"), ["AI331"])
        return page, fake

    def test_eligibility_over_a_changed_list_opens_the_dialog_preset_and_applies_nothing(
        self,
    ) -> None:
        page, fake = self._changed_list()
        page.click("#plbEligRelaxed")
        expect(page.locator("#plbRulesDialog")).to_be_visible()
        self.assertEqual(page.input_value("#plbRuleElig"), "relaxed")
        self.assertEqual(page.input_value("#plbRuleMode"), "keep")
        expect(page.locator("#plbRuleEligNote")).to_be_visible()
        self.assertIn("reloads the suggested courses", self._text(page, "#plbRuleEligNote"))
        # Nothing has happened yet: the switch shows the rule in force, nothing was reloaded.
        expect(page.locator("#plbEligStrict")).to_be_checked()
        self.assertEqual(len(fake.context_bodies), 1)
        self.assertEqual(self._rows(page, "new"), ["AI331"])

    def test_apply_rules_confirms_an_eligibility_change_over_a_changed_list(self) -> None:
        page, fake = self._changed_list()
        page.click("#plbEligRelaxed")
        page.click("#plbRulesApply")
        expect(page.locator("#plbRulesDialog")).not_to_be_visible()
        expect(page.locator("#plbEligRelaxed")).to_be_checked()
        expect(
            page.locator("#plbTable tbody[data-group='new'] tr[data-code='AI342']")
        ).to_be_visible()
        self.assertEqual(self._rows(page, "new"), ["AI331", "AI342"])  # the suggestions came back
        self.assertEqual(fake.context_bodies[-1]["eligibility_mode"], "relaxed")
        expect(page.locator("#plbReset")).to_be_disabled()

    def test_cancel_leaves_everything_as_it_was_the_switch_included(self) -> None:
        page, fake = self._changed_list()
        page.click("#plbEligRelaxed")
        page.click("#plbRulesCancel")
        expect(page.locator("#plbRulesDialog")).not_to_be_visible()
        expect(page.locator("#plbEligStrict")).to_be_checked()
        expect(page.locator("#plbEligRelaxed")).not_to_be_checked()
        self.assertEqual(len(fake.context_bodies), 1)
        self.assertEqual(self._rows(page, "new"), ["AI331"])
        self.assertTrue(page.locator("#plbRulesChanged").is_hidden())
        # Escape is a cancel too, and the note is not left flagged for the next time.
        page.click("#plbEligRelaxed")
        expect(page.locator("#plbRulesDialog")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#plbEligStrict")).to_be_checked()
        page.click("#plbEditRules")
        self.assertEqual(page.input_value("#plbRuleElig"), "strict")
        expect(page.locator("#plbRuleEligNote")).not_to_have_attribute("data-flag", "true")

    def test_the_dialog_still_changes_every_rule_as_before(self) -> None:
        fake = BuildFake()
        page = self._open(fake)
        self._rules(page, mode="ignore", elig="relaxed", max=15, prog=False, full=True, swap=True)
        expect(page.locator("#plbRegIgnore")).to_be_checked()
        expect(page.locator("#plbEligRelaxed")).to_be_checked()
        self.assertEqual(page.input_value("#plbMaxIn"), "15")
        self.assertEqual(
            self._text(page, "#plbRules2"), "Any programme · Full sections allowed · Suggest swaps"
        )
        # The dialog opens on what the board says.
        page.click("#plbEditRules")
        self.assertEqual(page.input_value("#plbRuleMode"), "ignore")
        self.assertEqual(page.input_value("#plbRuleMax"), "15")

    # ── max credits ─────────────────────────────────────────────────────────
    def test_max_credits_shows_the_cap_and_its_hint(self) -> None:
        page = self._open(BuildFake())
        self.assertEqual(page.input_value("#plbMaxIn"), "18")
        self.assertEqual(self._text(page, "#plbMaxInHint"), "advisory 18 · regulatory 19")

    def test_max_credits_applies_on_enter_and_when_the_field_loses_focus(self) -> None:
        page = self._open(BuildFake())
        page.fill("#plbMaxIn", "16")
        page.press("#plbMaxIn", "Enter")
        self.assertEqual(
            self._text(page, "#plbRules1"), "Keep Registered · Strict eligibility · Max credits 16"
        )
        self.assertEqual(self._text(page, "#plbCredits"), "15 of 16 cr · 1 free")
        page.fill("#plbMaxIn", "17")
        page.press("#plbMaxIn", "Tab")
        self.assertEqual(self._text(page, "#plbCredits"), "15 of 17 cr · 2 free")
        expect(page.locator("#plbMaxInErr")).to_be_hidden()
        # Empty is "not set", as in the dialog.
        page.fill("#plbMaxIn", "")
        page.press("#plbMaxIn", "Enter")
        self.assertIn("Max credits not set", self._text(page, "#plbRules1"))

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
                self.assertEqual(self._text(page, "#plbCredits"), "15 of 18 cr · 3 free")
                self.assertTrue(page.locator("#plbRulesChanged").is_hidden())
                # Leaving the field does not apply it either.
                page.press("#plbMaxIn", "Tab")
                self.assertEqual(self._text(page, "#plbCredits"), "15 of 18 cr · 3 free")
                self.assertEqual(self._built_body(page, fake)["max_credits"], 18)
                # The dialog says the same about the same value.
                page.click("#plbEditRules")
                page.fill("#plbRuleMax", bad)
                page.click("#plbRulesApply")
                self.assertEqual(
                    self._text(page, "#plbRuleMaxErr"), "Enter a whole number from 1 to 19."
                )
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
        self.assertEqual(self._text(page, "#plbCredits"), "15 of 12 cr · 3 over")

    # ── the switches by keyboard ────────────────────────────────────────────
    def test_the_switches_are_radio_groups_and_the_arrow_keys_move_between_the_two(self) -> None:
        fake = BuildFake()
        page = self._open(fake)
        groups = page.eval_on_selector_all(
            ".plb-seg",
            "g => g.map(x => [x.getAttribute('role'), x.querySelectorAll('input[type=radio]').length])",
        )
        self.assertEqual(groups, [["radiogroup", 2], ["radiogroup", 2]])
        for label in ("#plbRegLabel", "#plbEligLabel"):
            self.assertTrue(_squash(page.locator(label).text_content()))
        page.focus("#plbRegKeep")
        page.keyboard.press("ArrowDown")
        expect(page.locator("#plbRegIgnore")).to_be_checked()
        expect(page.locator("#plbRegIgnore")).to_be_focused()
        self.assertEqual(self._text(page, "#plbRules1").split(" · ")[0], "Ignore Registered")
        page.keyboard.press("ArrowUp")
        expect(page.locator("#plbRegKeep")).to_be_checked()
        page.focus("#plbEligStrict")
        page.keyboard.press("ArrowDown")
        expect(page.locator("#plbEligRelaxed")).to_be_checked()
        self.assertEqual(self._built_body(page, fake)["eligibility_mode"], "relaxed")

    def test_in_arabic_the_controls_say_arabic_and_work(self) -> None:
        page = self._open(BuildFake(), language="ar")
        self.assertEqual(self._text(page, "#plbRegLabel"), "المقررات المسجّلة")
        self.assertEqual(self._text(page, "#plbEligLabel"), "الأهلية")
        self.assertEqual(self._text(page, "#plbEditRules"), "قواعد أخرى")
        self.assertEqual(
            page.eval_on_selector_all(
                ".plb-seg-o", "n => n.map(x => x.textContent.replace(/\\s+/g, ' ').trim())"
            ),
            ["إبقاء", "تجاهل", "صارمة", "مرنة"],
        )
        page.click("#plbRegIgnore")
        expect(page.locator("#plbRegIgnore")).to_be_checked()
        self.assertIn("تجاهل المسجّل", self._text(page, "#plbRules1"))
        page.fill("#plbMaxIn", "99")
        page.press("#plbMaxIn", "Enter")
        expect(page.locator("#plbMaxInErr")).to_have_text("أدخل عددًا صحيحًا من 1 إلى 19.")
        self.assertEqual(self._text(page, "#plbMaxInHint"), "إرشادي 18 · النظامي 19")

    def test_the_other_three_rules_stay_one_summary_line_and_more_rules_opens_the_dialog(
        self,
    ) -> None:
        page = self._open(BuildFake())
        self.assertEqual(self._text(page, "#plbEditRules"), "More rules")
        self.assertEqual(
            self._text(page, "#plbRules2"), "This programme only · No full sections · No swaps"
        )
        expect(page.locator("#plbRules1")).to_be_hidden()  # the controls say it
        page.click("#plbEditRules")
        expect(page.locator("#plbRulesDialog")).to_be_visible()
        expect(page.locator("#plbRuleProg")).to_be_visible()
        expect(page.locator("#plbRuleFull")).to_be_visible()
        expect(page.locator("#plbRuleSwap")).to_be_visible()
        # No dialog-first flag when it is opened by hand.
        expect(page.locator("#plbRuleEligNote")).not_to_have_attribute("data-flag", "true")
        expect(page.locator("#plbRuleMode")).to_be_focused()

    # ── before a student is loaded ──────────────────────────────────────────
    def test_the_controls_work_before_a_student_is_loaded(self) -> None:
        fake = BuildFake()
        page = self._open(fake, load=False)
        page.click("#plbRegIgnore")
        page.click("#plbEligRelaxed")
        expect(page.locator("#plbRulesDialog")).not_to_be_visible()  # no list to lose
        page.fill("#plbMaxIn", "15")
        page.press("#plbMaxIn", "Enter")
        self.assertEqual(
            self._text(page, "#plbRules1"),
            "Ignore Registered · Relaxed eligibility · Max credits 15",
        )
        self.assertEqual(fake.context_bodies, [])  # no student: nothing to reload
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbTable tbody[data-group='blocked']")).to_be_visible()
        self.assertEqual(fake.context_bodies[0]["eligibility_mode"], "relaxed")
        expect(page.locator("#plbRegIgnore")).to_be_checked()
        expect(page.locator("#plbEligRelaxed")).to_be_checked()

    # ── touch and size ──────────────────────────────────────────────────────
    def test_every_control_has_the_boards_touch_size_on_a_phone(self) -> None:
        page = self._open(BuildFake(), width=390, height=844)
        heights = page.evaluate(
            """() => ['#plbEditRules', '#plbMaxIn', '.plb-seg-o', '#plbReset']
              .flatMap(s => [...document.querySelectorAll(s)].map(n => [s, n.getBoundingClientRect().height]))"""
        )
        self.assertEqual(len(heights), 1 + 1 + 4 + 1)
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
        page.focus("#plbYear")
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
        page.goto(
            f"{self.live_server_url}/planner/?layout=board&student={STUDENT_ID}&year=1448&term=1"
        )
        expect(page.locator("#plbTable tbody[data-group='blocked']")).to_be_visible()
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
    def test_reset_to_suggestions_sits_in_the_new_group_header_before_must_take(self) -> None:
        for language in ("en", "ar"):
            with self.subTest(language=language):
                page = self._open(BuildFake(), language=language)
                inside = page.evaluate(
                    "!!document.querySelector(\"#plbTable tbody[data-group='new'] .plb-grp th #plbReset\")"
                )
                self.assertTrue(inside)
                self.assertFalse(page.evaluate("!!document.querySelector('.plb-ph #plbReset')"))
                reset = page.evaluate(BOX, "#plbReset")
                must = page.evaluate(BOX, "#plbTable tbody[data-group='new'] .plb-grp td")
                if language == "en":
                    self.assertLessEqual(reset["r"], must["l"] + 1)
                else:
                    self.assertGreaterEqual(reset["l"], must["r"] - 1)
                page.context.close()

    def test_reset_in_its_new_place_still_resets(self) -> None:
        page = self._open(BuildFake())
        expect(page.locator("#plbReset")).to_be_disabled()
        page.click("#plbTable tr[data-code='AI342'] [data-act='remove']")
        expect(page.locator("#plbReset")).to_be_enabled()
        page.click("#plbReset")
        self.assertEqual(self._rows(page, "new"), ["AI331", "AI342"])
        expect(page.locator("#plbReset")).to_be_disabled()
        expect(page.locator("#plbAdd")).to_be_focused()

    def test_add_a_course_stays_on_the_title_line_at_1440_and_1152(self) -> None:
        for font in ("app", "wide"):
            for width in (1440, 1152):
                with self.subTest(font=font, width=width):
                    page = self._open(BuildFake(), font=font, width=width, height=900)
                    title = page.evaluate(BOX, "#plbCoursesTitle")
                    add = page.evaluate(BOX, "#plbAdd")
                    # the same line: the field's box starts above the title's bottom
                    self.assertLess(add["t"], title["b"], (title, add))
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


def BuildFake_default_body() -> dict:
    """The body of a build with no rule touched (what tests/planner_board_support.py pins)."""
    from tests.planner_board_support import EXPECTED_BUILD_BODY

    return EXPECTED_BUILD_BODY

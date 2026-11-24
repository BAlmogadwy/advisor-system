"""The new planner screen (part one) in a real browser: shell, student, rules, courses, week.

The fixtures, the fake answers and the browser harness live in tests/planner_board_support.py.
"""

from __future__ import annotations

import re

from tests.planner_board_support import (
    BASELINE,
    CONTROL_HEIGHTS,
    OTHER_ID,
    OVERFLOWS,
    OVERLAPS,
    RECOMMENDATIONS,
    SCRIPT,
    STUDENT_ID,
    STUDENT_PLAN,
    Fake,
    PlannerBoardCase,
    _rec,
    _row,
    context_for,
    expect,
)


class PlannerBoardShellBrowserTests(PlannerBoardCase):
    # ── The student ──────────────────────────────────────────────────────────
    def test_before_a_student_is_loaded_the_page_is_calm(self) -> None:
        page = self._open(Fake(), load=False)
        self.assertIn("No student loaded", self._text(page, "#plbStudent"))
        self.assertIn("Build options to see the projection", self._text(page, ".plb-grad"))
        self.assertTrue(page.locator("#plbGuide").is_hidden())
        self.assertTrue(page.locator("#plbDetail").is_hidden())
        self.assertEqual(page.locator(".plb-tab").count(), 1)
        self.assertEqual(page.get_attribute("#plbTabRegistered", "aria-selected"), "true")
        self.assertIn("Load a student to see their courses", self._text(page, "#plbTable"))
        self.assertIn("Load a student to see the week", self._text(page, "#plbWeek"))
        self.assertTrue(page.locator("#plbAdd").is_disabled())

    def test_load_shows_the_student_figures(self) -> None:
        page = self._open(Fake())
        student = self._text(page, "#plbStudent")
        self.assertIn("Sample Student", student)
        self.assertIn(STUDENT_ID, student)
        self.assertIn("AI", student)
        self.assertIn("3.42", self._text(page, ".plb-stat:nth-child(1)"))
        self.assertIn("9", self._text(page, ".plb-stat:nth-child(2)"))
        cap = self._text(page, ".plb-stat:nth-child(3)")
        self.assertIn("18", cap)
        self.assertIn("Credit cap", cap)
        self.assertIn("advisory · max 19", cap)
        self.assertEqual(self._text(page, "#plbContext"), "4500000 · 1448/1")
        self.assertEqual(self._text(page, "#plbStatus"), "Student loaded.")

    def test_loading_not_found_and_error_are_plain_lines(self) -> None:
        fake = Fake()
        fake.hold_ids = {STUDENT_ID}
        page = self._open(fake, load=False)
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbStatus")).to_have_text("Loading student…")
        # Release the held request as "not found".
        body, route = fake.pending.pop()
        route.fulfill(
            status=404,
            json={
                "ok": False,
                "error": {"code": "STUDENT_NOT_FOUND", "message": "Student not found"},
            },
        )
        expect(page.locator("#plbStatus")).to_have_text("No student found with that ID.")
        # A server error is a different plain line, and a loaded page stays.
        fake.hold_ids = set()
        fake.context_answer = lambda body: (
            500,
            {"ok": False, "error": {"code": "X", "message": "boom"}},
        )
        self._fetch(page, OTHER_ID)
        expect(page.locator("#plbStatus")).to_have_text(
            "The student could not be loaded. Try again."
        )
        fake.context_answer = lambda body: (200, context_for(str(body["student_id"])))
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbStudent")).to_contain_text("Sample Student")
        fake.context_answer = lambda body: (404, {"error": {"code": "STUDENT_NOT_FOUND"}})
        self._fetch(page, "4999999")
        expect(page.locator("#plbStatus")).to_have_text("No student found with that ID.")
        self.assertIn("Sample Student", self._text(page, "#plbStudent"))
        # Missing inputs never reach the server.
        before = len(fake.context_bodies)
        page.fill("#plbStudentId", "")
        page.click("#plbFetch")
        expect(page.locator("#plbStatus")).to_have_text("Enter a student ID.")
        page.fill("#plbStudentId", STUDENT_ID)
        page.fill("#plbYear", "14")
        page.click("#plbFetch")
        expect(page.locator("#plbStatus")).to_have_text("Enter a four-digit year.")
        self.assertEqual(len(fake.context_bodies), before)

    def test_a_slow_earlier_reply_cannot_paint_over_a_newer_one(self) -> None:
        fake = Fake()
        fake.hold_ids = {STUDENT_ID}
        fake.context_answer = lambda body: (
            200,
            context_for(str(body["student_id"]), name="Newer Student"),
        )
        page = self._open(fake, load=False)
        self._fetch(page, STUDENT_ID)  # the slow one, held
        expect(page.locator("#plbStatus")).to_have_text("Loading student…")
        self._fetch(page, OTHER_ID)  # the newer one answers at once
        expect(page.locator("#plbStudent")).to_contain_text("Newer Student")
        # Now the slow one answers, with a different name.
        _body, route = fake.pending.pop()
        route.fulfill(json=context_for(STUDENT_ID, name="Older Student"))
        page.wait_for_timeout(400)
        page.evaluate("new Promise(r => requestAnimationFrame(() => r(true)))")
        student = self._text(page, "#plbStudent")
        self.assertIn("Newer Student", student)
        self.assertNotIn("Older Student", student)
        self.assertEqual(self._text(page, "#plbContext"), "4500001 · 1448/1")
        self.assertEqual(self._text(page, "#plbStatus"), "Student loaded.")

    def test_a_failed_fetch_leaves_the_plan_of_the_student_on_screen_alive(self) -> None:
        fake = Fake()
        fake.hold_plan = 1
        page = self._open(fake, load=False)
        self._fetch(page, STUDENT_ID)  # the student loads; their plan is still on its way
        expect(page.locator("#plbTable tbody[data-group='blocked']")).to_be_visible()
        self.assertEqual(self._rows(page, "could"), [])
        # A second fetch fails while that plan is still on its way.
        fake.context_answer = lambda body: (404, {"error": {"code": "STUDENT_NOT_FOUND"}})
        self._fetch(page, "4999999")
        expect(page.locator("#plbStatus")).to_have_text("No student found with that ID.")
        # The plan then arrives. It belongs to the student still on screen, so it is drawn:
        # the group must not be left saying "loading" for good.
        fake.pending_plan.pop().fulfill(json=STUDENT_PLAN)
        expect(
            page.locator("#plbTable tbody[data-group='could'] tr[data-code='STAT305']")
        ).to_be_visible()
        self.assertEqual(self._rows(page, "could"), ["STAT305", "CS351"])
        self.assertIn("Sample Student", self._text(page, "#plbStudent"))

    def test_a_slow_earlier_plan_cannot_paint_over_a_newer_students_plan(self) -> None:
        fake = Fake()
        fake.hold_plan = 1
        page = self._open(fake, load=False)
        self._fetch(page, STUDENT_ID)  # this student's plan is held back
        expect(page.locator("#plbTable tbody[data-group='blocked']")).to_be_visible()
        self._fetch(page, OTHER_ID)  # the newer student's plan answers at once
        expect(
            page.locator("#plbTable tbody[data-group='could'] tr[data-code='STAT305']")
        ).to_be_visible()
        self.assertEqual(self._text(page, "#plbContext"), "4500001 · 1448/1")
        # The older plan then arrives, with a course of its own.
        stale_course = {
            "course_code": "OLD999",
            "course_name": "Stale",
            "credit_hours": 3,
            "importance_score": 5,
            "status": "not_taken",
            "can_register": True,
        }
        fake.pending_plan.pop().fulfill(json={"terms": [{"courses": [stale_course]}]})
        page.wait_for_timeout(400)
        page.evaluate("new Promise(r => requestAnimationFrame(() => r(true)))")
        self.assertEqual(self._rows(page, "could"), ["STAT305", "CS351"])

    # ── The rules ────────────────────────────────────────────────────────────
    def test_the_rules_summary_and_the_cap_default(self) -> None:
        page = self._open(Fake())
        self.assertEqual(
            self._text(page, "#plbRules1"), "Keep Registered · Strict eligibility · Max credits 18"
        )
        self.assertEqual(
            self._text(page, "#plbRules2"), "This programme only · No full sections · No swaps"
        )
        self.assertTrue(page.locator("#plbRulesChanged").is_hidden())
        page.click("#plbEditRules")
        # Max credits comes from the student's cap: the old page's 17 was a bug.
        self.assertEqual(page.input_value("#plbRuleMax"), "18")
        self.assertIn("advisory 18 · regulatory 19", self._text(page, "#plbRuleMaxHint"))
        self.assertIn("reloads the suggested courses", self._text(page, "#plbRuleEligNote"))

    def test_max_credits_follows_each_students_own_cap_and_an_unknown_cap_is_not_zero(self) -> None:
        fake = Fake()
        fake.context_answer = lambda body: (
            200,
            context_for(str(body["student_id"]), credit_cap=16, regulatory_max_credits=17),
        )
        page = self._open(fake)
        self.assertIn("Max credits 16", self._text(page, "#plbRules1"))
        self.assertTrue(page.locator("#plbRulesChanged").is_hidden())
        self.assertEqual(self._text(page, "#plbCredits"), "15 of 16 cr · 1 free")
        # A student whose cap is not known: nothing is shown as zero, nothing crashes.
        fake.context_answer = lambda body: (
            200,
            context_for(str(body["student_id"]), credit_cap=None, regulatory_max_credits=None),
        )
        self._fetch(page, OTHER_ID)
        expect(page.locator("#plbRules1")).to_contain_text("Max credits not set")
        self.assertEqual(self._text(page, "#plbCredits"), "15 cr")
        self.assertIn("—", self._text(page, ".plb-stat:nth-child(3)"))
        self.assertNotIn("Credit cap 0", self._text(page, "#plbStudent"))
        # The student's cap is back when the next student has one.
        fake.context_answer = lambda body: (
            200,
            context_for(str(body["student_id"]), credit_cap=20),
        )
        self._fetch(page, "4500002")
        expect(page.locator("#plbRules1")).to_contain_text("Max credits 20")

    def test_the_rules_dialog_round_trips_each_value_and_counts_the_changes(self) -> None:
        fake = Fake()
        page = self._open(fake)
        before = len(fake.context_bodies)
        self._rules(page, mode="ignore")
        expect(page.locator("#plbRulesDialog")).to_be_hidden()
        self.assertIn("Ignore Registered", self._text(page, "#plbRules1"))
        self.assertEqual(self._text(page, "#plbRulesChanged"), "1 rule changed")
        self._rules(page, max=15)
        self.assertIn("Max credits 15", self._text(page, "#plbRules1"))
        self.assertEqual(self._text(page, "#plbRulesChanged"), "2 rules changed")
        self._rules(page, prog=False)
        self.assertIn("Any programme", self._text(page, "#plbRules2"))
        self._rules(page, full=True)
        self.assertIn("Full sections allowed", self._text(page, "#plbRules2"))
        self._rules(page, swap=True)
        self.assertEqual(
            self._text(page, "#plbRules1"),
            "Ignore Registered · Strict eligibility · Max credits 15",
        )
        self.assertEqual(
            self._text(page, "#plbRules2"), "Any programme · Full sections allowed · Suggest swaps"
        )
        self.assertEqual(self._text(page, "#plbRulesChanged"), "5 rules changed")
        # None of those five reloaded the student.
        self.assertEqual(len(fake.context_bodies), before)
        # The dialog holds what was applied.
        page.click("#plbEditRules")
        self.assertEqual(page.input_value("#plbRuleMode"), "ignore")
        self.assertEqual(page.input_value("#plbRuleElig"), "strict")
        self.assertEqual(page.input_value("#plbRuleMax"), "15")
        self.assertFalse(page.is_checked("#plbRuleProg"))
        self.assertTrue(page.is_checked("#plbRuleFull"))
        self.assertTrue(page.is_checked("#plbRuleSwap"))
        # Cancel throws edits away.
        page.select_option("#plbRuleMode", "keep")
        page.click("#plbRulesCancel")
        self.assertIn("Ignore Registered", self._text(page, "#plbRules1"))
        # Reset to defaults, then Apply: nothing is changed any more.
        page.click("#plbEditRules")
        page.click("#plbRulesReset")
        self.assertEqual(page.input_value("#plbRuleMax"), "18")
        page.click("#plbRulesApply")
        self.assertEqual(
            self._text(page, "#plbRules1"), "Keep Registered · Strict eligibility · Max credits 18"
        )
        self.assertTrue(page.locator("#plbRulesChanged").is_hidden())

    def test_the_max_credits_field_is_validated_and_the_dialog_stays_open(self) -> None:
        page = self._open(Fake())
        for bad in ("abc", "0", "25", "-3"):
            page.click("#plbEditRules") if not page.locator(
                "#plbRulesDialog"
            ).is_visible() else None
            page.fill("#plbRuleMax", bad)
            page.click("#plbRulesApply")
            expect(page.locator("#plbRulesDialog")).to_be_visible()
            expect(page.locator("#plbRuleMaxErr")).to_have_text(
                "Enter a whole number from 1 to 19."
            )
            self.assertIn("Max credits 18", self._text(page, "#plbRules1"))
        page.fill("#plbRuleMax", "")
        page.click("#plbRulesApply")
        expect(page.locator("#plbRulesDialog")).to_be_hidden()
        self.assertIn("Max credits not set", self._text(page, "#plbRules1"))

    def test_changing_eligibility_says_so_then_reloads_and_keeps_the_other_rules(self) -> None:
        fake = Fake()

        def answer(body):
            payload = context_for(str(body["student_id"]))
            payload["eligibility_mode"] = body["eligibility_mode"]
            if body["eligibility_mode"] == "relaxed":
                payload["recommendations"] = [
                    *RECOMMENDATIONS[:2],
                    _rec("AI431", "Deep Learning", "Eligible"),
                ]
            return 200, payload

        fake.context_answer = answer
        page = self._open(fake)
        self.assertEqual(self._rows(page, "new"), ["AI331", "AI342"])
        # The adviser changes the plan and one rule, then eligibility.
        page.click("#plbTable tr[data-code='AI342'] [data-act='remove']")
        self.assertEqual(self._rows(page, "new"), ["AI331"])
        self._rules(page, max=16)
        before = len(fake.context_bodies)
        page.click("#plbEditRules")
        expect(page.locator("#plbRuleEligNote")).to_be_visible()
        page.select_option("#plbRuleElig", "relaxed")
        page.click("#plbRulesApply")
        expect(page.locator("#plbRules1")).to_contain_text("Relaxed eligibility")
        expect(
            page.locator("#plbTable tbody[data-group='new'] tr[data-code='AI431']")
        ).to_be_visible()
        self.assertEqual(len(fake.context_bodies), before + 1)
        self.assertEqual(fake.context_bodies[-1]["eligibility_mode"], "relaxed")
        self.assertEqual(fake.context_bodies[-1]["student_id"], STUDENT_ID)
        # The suggestions were reloaded (the removed course is back), and the
        # max credits the adviser chose is still the max credits.
        self.assertEqual(self._rows(page, "new"), ["AI331", "AI342", "AI431"])
        self.assertIn("Max credits 16", self._text(page, "#plbRules1"))
        self.assertEqual(self._text(page, "#plbRulesChanged"), "2 rules changed")

    def test_a_failed_eligibility_reload_puts_the_old_rule_back(self) -> None:
        fake = Fake()
        page = self._open(fake)
        fake.context_answer = lambda body: (
            500,
            {"ok": False, "error": {"code": "X", "message": "boom"}},
        )
        self._rules(page, elig="relaxed", mode="ignore")
        expect(page.locator("#plbStatus")).to_have_text(
            "The student could not be loaded. Try again."
        )
        # The page still shows the strict suggestions, so the summary must say strict ...
        expect(page.locator("#plbRules1")).to_contain_text("Strict eligibility")
        self.assertEqual(self._rows(page, "new"), ["AI331", "AI342"])
        # ... while the other rule changed in the same dialog is kept and applied.
        self.assertEqual(self._text(page, "#plbRulesChanged"), "1 rule changed")
        self.assertIn("CS301", self._rows(page, "could"))

    def test_a_reload_overtaken_by_a_failed_fetch_still_puts_the_old_rule_back(self) -> None:
        fake = Fake()
        page = self._open(fake)
        # The eligibility reload is slow ...
        fake.hold_ids = {STUDENT_ID}
        self._rules(page, elig="relaxed")
        expect(page.locator("#plbStatus")).to_have_text("Loading student…")
        # ... and a newer fetch overtakes it, then fails.
        fake.context_answer = lambda body: (404, {"error": {"code": "STUDENT_NOT_FOUND"}})
        self._fetch(page, "4999999")
        expect(page.locator("#plbStatus")).to_have_text("No student found with that ID.")
        expect(page.locator("#plbRules1")).to_contain_text("Strict eligibility")
        # The overtaken reload then answers: it is too late to change anything.
        _body, route = fake.pending.pop()
        route.fulfill(json=context_for(STUDENT_ID, eligibility_mode="relaxed"))
        page.wait_for_timeout(400)
        page.evaluate("new Promise(r => requestAnimationFrame(() => r(true)))")
        self.assertIn("Strict eligibility", self._text(page, "#plbRules1"))
        self.assertEqual(self._rows(page, "new"), ["AI331", "AI342"])
        self.assertEqual(self._text(page, "#plbRulesChanged"), "")

    def test_closing_the_rules_dialog_puts_focus_back_on_edit_rules(self) -> None:
        page = self._open(Fake())
        for close in ("Escape", "#plbRulesCancel", "#plbRulesApply"):
            with self.subTest(close=close):
                page.click("#plbEditRules")
                expect(page.locator("#plbRulesDialog")).to_be_visible()
                if close == "Escape":
                    page.keyboard.press("Escape")
                else:
                    page.click(close)
                expect(page.locator("#plbRulesDialog")).to_be_hidden()
                expect(page.locator("#plbEditRules")).to_be_focused()

    def test_the_over_cap_mark_goes_when_max_credits_is_cleared(self) -> None:
        page = self._open(Fake())
        self._rules(page, max=12)
        expect(page.locator("#plbCredits")).to_have_text("15 of 12 cr · 3 over")
        self.assertEqual(page.get_attribute("#plbCredits", "data-over"), "true")
        self._rules(page, max="")
        expect(page.locator("#plbCredits")).to_have_text("15 cr")
        self.assertIsNone(page.get_attribute("#plbCredits", "data-over"))

    def test_the_masthead_year_is_not_used_by_a_rules_reload(self) -> None:
        fake = Fake()
        page = self._open(fake)
        page.fill("#plbStudentId", "9999999")  # typed, never fetched
        self._rules(page, elig="relaxed")
        expect(page.locator("#plbRules1")).to_contain_text("Relaxed eligibility")
        self.assertEqual(fake.context_bodies[-1]["student_id"], STUDENT_ID)

    # ── The courses ──────────────────────────────────────────────────────────
    def test_courses_come_in_four_groups_with_counts(self) -> None:
        page = self._open(Fake())
        groups = page.eval_on_selector_all(
            "#plbTable tbody", "bodies => bodies.map(b => b.dataset.group)"
        )
        self.assertEqual(groups, ["registered", "new", "could", "blocked"])
        self.assertEqual(self._rows(page, "registered"), ["CS301", "CS322", "CS330"])
        self.assertEqual(self._rows(page, "new"), ["AI331", "AI342"])
        self.assertEqual(self._rows(page, "could"), ["STAT305", "CS351"])
        self.assertEqual(self._rows(page, "blocked"), ["AI431", "AI450"])
        self.assertEqual(self._text(page, "#plbCredits"), "15 of 18 cr · 3 free")
        self.assertEqual(
            self._text(page, "tbody[data-group='registered'] .plb-grp th"),
            "Registered · fixed here",
        )
        self.assertEqual(self._text(page, "tbody[data-group='registered'] .plb-grp td"), "9 cr")
        self.assertEqual(
            self._text(page, "tbody[data-group='new'] .plb-grp th"), "New in this plan · 6 cr"
        )
        self.assertEqual(self._text(page, "tbody[data-group='new'] .plb-grp td"), "Must-take")
        self.assertIn("Could be added", self._text(page, "tbody[data-group='could'] .plb-grp"))
        self.assertIn("3 cr free", self._text(page, "tbody[data-group='could'] .plb-grp"))
        # Registered: the section and a lock. New: "any section" and a must-take tick.
        reg = "tbody[data-group='registered'] tr[data-code='CS301']"
        self.assertIn("§M1", self._text(page, f"{reg} .plb-sc"))
        self.assertEqual(page.locator(f"{reg} [role='img'][aria-label='Registered']").count(), 1)
        row = "tbody[data-group='new'] tr[data-code='AI331']"
        self.assertEqual(self._text(page, f"{row} .plb-sc"), "any section")
        self.assertFalse(page.is_checked(f"{row} input[type='checkbox']"))
        # Could be added: the sections, and a seat warning when one section is nearly full.
        self.assertEqual(self._text(page, "tr[data-code='STAT305'] .plb-sc"), "Mon Wed")
        self.assertEqual(self._text(page, "tr[data-code='CS351'] .plb-sc"), "Thu · 3 seats left")
        # Blocked: the reason; waiting for a course in the plan says so, for one outside it does not.
        self.assertIn("Missing: AI331 (in this plan)", self._text(page, "tr[data-code='AI431']"))
        self.assertIn("Missing: CS999", self._text(page, "tr[data-code='AI450']"))
        self.assertNotIn("(in this plan)", self._text(page, "tr[data-code='AI450']"))

    def test_add_remove_and_must_take_change_the_plan_and_the_credits_header(self) -> None:
        page = self._open(Fake())
        # A blocked course waiting for a course in the plan says so.
        page.click("#plbTable tr[data-code='AI331'] [data-act='remove']")
        self.assertEqual(self._rows(page, "new"), ["AI342"])
        self.assertEqual(self._text(page, "#plbCredits"), "12 of 18 cr · 6 free")
        self.assertIn("AI331", self._rows(page, "could"))
        self.assertEqual(self._text(page, "#plbStatus"), "Removed AI331 from the plan.")
        self.assertEqual(self._rows(page, "blocked"), ["AI431", "AI450"])
        self.assertNotIn("(in this plan)", self._text(page, "tr[data-code='AI431']"))
        # Add it back by its button; the row moves and focus follows to its remove button.
        page.click("#plbTable tr[data-code='AI331'] [data-act='add']")
        self.assertEqual(self._rows(page, "new"), ["AI342", "AI331"])
        self.assertEqual(self._text(page, "#plbCredits"), "15 of 18 cr · 3 free")
        self.assertEqual(self._text(page, "#plbStatus"), "Added AI331 to the plan.")
        expect(page.locator("tr[data-code='AI331'] [data-act='remove']")).to_be_focused()
        self.assertIn("(in this plan)", self._text(page, "tr[data-code='AI431']"))
        # Must-take survives a redraw (adding another course draws the table again).
        page.check("tr[data-code='AI342'] input[type='checkbox']")
        page.click("#plbTable tr[data-code='STAT305'] [data-act='add']")
        self.assertTrue(page.is_checked("tr[data-code='AI342'] input[type='checkbox']"))
        self.assertFalse(page.is_checked("tr[data-code='AI331'] input[type='checkbox']"))
        self.assertEqual(self._text(page, "#plbCredits"), "18 of 18 cr · 0 free")
        # Over the cap says so instead of a negative number.
        page.click("#plbTable tr[data-code='CS351'] [data-act='add']")
        self.assertEqual(self._text(page, "#plbCredits"), "21 of 18 cr · 3 over")
        self.assertEqual(page.get_attribute("#plbCredits", "data-over"), "true")
        # A new max credits rule is the new cap.
        self._rules(page, max=19)
        self.assertEqual(self._text(page, "#plbCredits"), "21 of 19 cr · 2 over")

    def test_ignoring_registered_courses_changes_what_counts(self) -> None:
        page = self._open(Fake())
        self._rules(page, mode="ignore")
        self.assertEqual(self._text(page, "#plbCredits"), "6 of 18 cr · 12 free")
        self.assertIn(
            "ignored in this build", self._text(page, "tbody[data-group='registered'] .plb-grp")
        )
        # The registered courses can be planned again.
        self.assertIn("CS301", self._rows(page, "could"))

    def test_add_a_course_searches_the_plan(self) -> None:
        page = self._open(Fake())
        page.fill("#plbAdd", "stat")
        expect(page.locator("#plbAddList [role='option']")).to_have_count(1)
        self.assertIn("STAT305", self._text(page, "#plbAddList"))
        self.assertEqual(page.get_attribute("#plbAdd", "aria-expanded"), "true")
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Enter")
        self.assertIn("STAT305", self._rows(page, "new"))
        self.assertEqual(page.input_value("#plbAdd"), "")
        self.assertTrue(page.locator("#plbAddList").is_hidden())
        # By name, with no match, and a course already in the plan is not offered.
        page.fill("#plbAdd", "network")
        expect(page.locator("#plbAddList [role='option']")).to_have_count(1)
        page.fill("#plbAdd", "zzz")
        self.assertEqual(self._text(page, "#plbAddList"), "No course matches")
        page.fill("#plbAdd", "AI331")
        self.assertEqual(self._text(page, "#plbAddList"), "No course matches")
        page.keyboard.press("Escape")
        self.assertTrue(page.locator("#plbAddList").is_hidden())

    def test_a_failed_plan_or_catalogue_is_a_plain_line_not_a_crash(self) -> None:
        fake = Fake()
        fake.plan_status = 500
        page = self._open(fake, load=False)
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbStatus")).to_have_text(
            "The student's plan could not be loaded, so no extra courses are listed."
        )
        self.assertEqual(self._rows(page, "new"), ["AI331", "AI342"])
        self.assertEqual(self._rows(page, "could"), [])
        # The catalogue failing leaves section information unknown, not "none".
        fake.plan_status = 200
        fake.catalog_status = 500
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbStatus")).to_have_text("Section data could not be loaded.")
        self.assertEqual(self._text(page, "tr[data-code='CS351'] .plb-sc"), "—")
        self.assertEqual(self._text(page, "#plbCredits"), "15 of 18 cr · 3 free")

    def test_a_slow_earlier_section_reply_cannot_paint_over_a_newer_one(self) -> None:
        fake = Fake()
        page = self._open(fake)
        self.assertEqual(self._text(page, "tr[data-code='CS351'] .plb-sc"), "Thu · 3 seats left")
        # A rule change asks for the sections again; that reply is slow ...
        fake.hold_catalog = 1
        self._rules(page, prog=False)
        expect(page.locator("#plbRules2")).to_contain_text("Any programme")
        page.wait_for_function("() => true")
        self.assertEqual(len(fake.pending_catalog), 1)
        self.assertFalse(fake.catalog_bodies[-1]["program_sections_only"])
        # ... and a second change is answered at once.
        self._rules(page, mode="ignore")
        expect(page.locator("#plbRules1")).to_contain_text("Ignore Registered")
        self.assertEqual(len(fake.catalog_bodies), 3)
        # The slow reply then arrives, saying there are no sections at all.
        fake.pending_catalog.pop().fulfill(json={"ok": True, "sections": []})
        page.wait_for_timeout(400)
        self.assertEqual(self._text(page, "tr[data-code='CS351'] .plb-sc"), "Thu · 3 seats left")

    # ── The registered week ──────────────────────────────────────────────────
    def test_the_registered_week_has_the_right_blocks_footers_and_facts(self) -> None:
        page = self._open(Fake())
        blocks = page.eval_on_selector_all(
            "#plbWeek .plb-b",
            "bs => bs.map(b => [b.dataset.code, b.style.gridColumn, b.textContent.replace(/\\s+/g, ' ').trim()])",
        )
        self.assertEqual(len(blocks), 6)
        by_col = {(code, col): text for code, col, text in blocks}
        self.assertIn("08:00-09:15", by_col[("CS301", "2")])  # Sunday
        self.assertIn("08:00-09:15", by_col[("CS301", "4")])  # Tuesday
        self.assertIn("10:00-11:15", by_col[("CS322", "2")])
        self.assertIn("12:45-14:00", by_col[("CS330", "3")])  # Monday
        self.assertIn("12:45-14:00", by_col[("CS330", "5")])  # Wednesday
        self.assertIn("§M1", by_col[("CS301", "2")])
        footers = page.eval_on_selector_all(
            "#plbWeek .plb-df", "fs => fs.map(f => f.innerText.replace(/\\s+/g, ' ').trim())"
        )
        self.assertEqual(
            footers,
            [
                "2 classes ends 11:15",
                "1 class ends 14:00",
                "2 classes ends 11:15",
                "1 class ends 14:00",
                "No classes day off",
            ],
        )
        heads = page.eval_on_selector_all("#plbWeek .plb-dh", "hs => hs.map(h => h.textContent)")
        self.assertEqual(heads, ["Sun", "Mon", "Tue", "Wed", "Thu"])
        self.assertEqual(page.locator("#plbWeek .plb-free").count(), 1)
        # The Registered tab's facts, worked out in the browser.
        self.assertEqual(self._text(page, "#plbTabName"), "Registered")
        self.assertEqual(self._text(page, "#plbTabCredits"), "9 cr")
        self.assertEqual(self._text(page, "#plbTabFacts1"), "4 days · Thu free")
        self.assertEqual(self._text(page, "#plbTabFacts2"), "ends 14:00 · gap 45 min")
        # Blocks never overlap one another on a day.
        self.assertEqual(page.evaluate(OVERLAPS), [])

    def test_a_registered_row_without_times_is_noted_and_never_drawn_as_zero(self) -> None:
        fake = Fake()
        broken = [
            *BASELINE,
            _row("CS340", "Compilers", "M9", None, None, None, None, 9),
            _row("CS341", "Graphics", "M8", 3, "SUN", "bad", "worse", 8),
        ]
        fake.context_answer = lambda body: (
            200,
            {**context_for(str(body["student_id"])), "baseline": broken},
        )
        page = self._open(fake, load=False)
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbWeek .plb-note")).to_have_text(
            "2 registered courses have no timetable slots."
        )
        self.assertEqual(page.locator("#plbWeek .plb-b").count(), 6)
        # CS340's credits are unknown: they add nothing and nothing shows "0 cr" for it.
        self.assertEqual(self._text(page, "#plbTabCredits"), "12 cr")
        self.assertEqual(self._text(page, "#plbTabFacts2"), "ends 14:00 · gap 45 min")

    def test_no_registered_classes_is_a_calm_state(self) -> None:
        fake = Fake()
        fake.context_answer = lambda body: (
            200,
            {**context_for(str(body["student_id"])), "baseline": []},
        )
        page = self._open(fake, load=False)
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbWeek")).to_contain_text("No registered classes this term.")
        self.assertEqual(self._text(page, "#plbTabFacts1"), "No timed classes")
        self.assertIn(
            "No registered courses this term.", self._text(page, "tbody[data-group='registered']")
        )

    def test_a_friday_class_gets_its_own_column(self) -> None:
        fake = Fake()
        fake.context_answer = lambda body: (
            200,
            {
                **context_for(str(body["student_id"])),
                "baseline": [
                    *BASELINE,
                    _row("CS350", "Seminar", "M7", 1, "FRIDAY", "09:00", "10:00", 7),
                ],
            },
        )
        page = self._open(fake)
        heads = page.eval_on_selector_all("#plbWeek .plb-dh", "hs => hs.map(h => h.textContent)")
        self.assertEqual(heads, ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri"])
        self.assertEqual(self._text(page, "#plbTabFacts1"), "5 days · Thu free")

    # ── Arabic ───────────────────────────────────────────────────────────────
    def test_arabic_renders_arabic_labels_and_right_to_left(self) -> None:
        page = self._open(Fake(), language="ar")
        self.assertEqual(page.evaluate("document.documentElement.dir"), "rtl")
        self.assertEqual(self._text(page, "#plbStatus"), "تم تحميل بيانات الطالب.")
        self.assertIn("المعدل التراكمي", self._text(page, "#plbStudent"))
        self.assertIn("إرشادي · الأقصى 19", self._text(page, "#plbStudent"))
        self.assertEqual(
            self._text(page, "#plbRules1"), "الإبقاء على المسجّل · أهلية صارمة · الحد الأقصى 18"
        )
        self.assertEqual(
            self._text(page, "#plbRules2"), "شُعب البرنامج فقط · بلا شُعب ممتلئة · بلا تبديلات"
        )
        self.assertEqual(
            self._text(page, "tbody[data-group='registered'] .plb-grp th"), "المسجّل · ثابت هنا"
        )
        self.assertEqual(self._text(page, "tr[data-code='AI331'] .plb-sc"), "أي شعبة")
        self.assertEqual(self._text(page, "#plbCredits"), "15 من 18 ساعة · 3 متاحة")
        self.assertIn("ناقص:", self._text(page, "tr[data-code='AI450']"))
        footers = page.eval_on_selector_all(
            "#plbWeek .plb-df", "fs => fs.map(f => f.innerText.replace(/\\s+/g, ' ').trim())"
        )
        self.assertEqual(footers[0], "محاضرتان تنتهي 11:15")
        self.assertEqual(footers[1], "محاضرة واحدة تنتهي 14:00")
        self.assertEqual(footers[4], "لا محاضرات يوم إجازة")
        self.assertEqual(self._text(page, "#plbTabFacts1"), "4 أيام · الخميس فارغ")
        # Rules: the count in Arabic.
        self._rules(page, max=15)
        self.assertEqual(self._text(page, "#plbRulesChanged"), "قاعدة واحدة تغيّرت")
        self._rules(page, prog=False)
        self.assertEqual(self._text(page, "#plbRulesChanged"), "قاعدتان تغيّرتا")
        self._rules(page, full=True)
        self.assertEqual(self._text(page, "#plbRulesChanged"), "3 قواعد تغيّرت")
        # Latin codes and times sit in <bdi>, left to right.
        self.assertGreater(page.locator("#plbTable bdi[dir='ltr']").count(), 10)
        self.assertEqual(page.locator("#plbWeek .plb-bt bdi[dir='ltr']").count(), 6)
        # None of the English labels of this page are left on screen.
        text = self._text(page, "#plbRoot")
        for english in (
            "Registered",
            "Could be added",
            "Must-take",
            "Blocked",
            "No classes",
            "Edit rules",
        ):
            self.assertNotIn(english, text)
        # The week mirrors: Sunday is at the start edge, which is the right edge.
        sun = page.locator("#plbWeek .plb-dh").first.bounding_box()
        thu = page.locator("#plbWeek .plb-dh").nth(4).bounding_box()
        self.assertGreater(sun["x"], thu["x"])

    def test_arabic_status_lines_and_the_rules_dialog(self) -> None:
        fake = Fake()
        page = self._open(fake, language="ar", load=False)
        fake.context_answer = lambda body: (404, {"error": {"code": "STUDENT_NOT_FOUND"}})
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbStatus")).to_have_text("لا يوجد طالب بهذا الرقم.")
        page.fill("#plbStudentId", "")
        page.click("#plbFetch")
        expect(page.locator("#plbStatus")).to_have_text("أدخل رقم الطالب.")
        page.click("#plbEditRules")
        self.assertIn("أهلية المقررات", self._text(page, "#plbRulesDialog"))
        self.assertIn("تعديل", self._text(page, "#plbEditRules"))
        page.fill("#plbRuleMax", "x")
        page.click("#plbRulesApply")
        expect(page.locator("#plbRuleMaxErr")).to_have_text("أدخل عددًا صحيحًا من 1 إلى 99.")

    # ── Fit ──────────────────────────────────────────────────────────────────
    def test_nothing_overflows_the_page_at_three_widths_in_both_languages_and_fonts(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in ("en", "ar"):
            for font in ("app", "wide"):
                for width, height in ((1440, 900), (1152, 800), (390, 844)):
                    page = self._open(Fake(), language, width, height, font)
                    found = page.evaluate(OVERFLOWS)
                    if found:
                        problems[f"{language} {width}px {font} fonts"] = found
                    page.context.close()
        if problems:
            self.fail(
                "\n" + "\n".join(f"{k}:\n    " + "\n    ".join(v) for k, v in problems.items())
            )

    def test_the_controls_are_big_enough(self) -> None:
        # 32 px controls on a laptop, 44 px on a phone; focus shows.
        page = self._open(Fake(), width=1440, height=900)
        small = page.evaluate(CONTROL_HEIGHTS)
        self.assertEqual([c for c in small if c[1] < 31.5], [])
        page.context.close()
        page = self._open(Fake(), width=390, height=844)
        phone = page.evaluate(CONTROL_HEIGHTS)
        self.assertEqual([c for c in phone if c[1] < 43.5], [])
        page.keyboard.press("Shift")  # a keyboard user: focus shows its ring
        page.focus("#plbEditRules")
        outline = page.evaluate(
            "getComputedStyle(document.getElementById('plbEditRules')).outlineStyle"
        )
        self.assertNotEqual(outline, "none")

    def test_dark_theme_uses_the_dark_tokens(self) -> None:
        page = self._open(Fake())
        page.evaluate("document.documentElement.setAttribute('data-theme', 'dark')")
        block = page.evaluate(
            "getComputedStyle(document.querySelector('#plbWeek .plb-b')).backgroundColor"
        )
        page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")
        light = page.evaluate(
            "getComputedStyle(document.querySelector('#plbWeek .plb-b')).backgroundColor"
        )
        self.assertNotEqual(block, light)


def test_the_new_script_writes_no_markup() -> None:
    """No innerHTML (or any markup sink) in the new script: every node is built."""
    source = SCRIPT.read_text(encoding="utf-8")
    sinks = re.findall(
        r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|createContextualFragment|DOMParser|\.html\(",
        source,
    )
    assert sinks == []
    # And it does not borrow the sink helper either.
    assert "safeHtml" not in source

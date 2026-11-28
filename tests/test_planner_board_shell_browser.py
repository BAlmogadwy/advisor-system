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
        self.assertTrue(page.locator("#plbPop").is_hidden())
        self.assertEqual(page.locator(".plb-tab").count(), 1)
        self.assertEqual(page.get_attribute("#plbTabRegistered", "aria-selected"), "true")
        self.assertIn("Load a student to see their courses", self._text(page, "#plbAvailList"))
        self.assertIn("Load a student to see the week", self._text(page, "#plbWeek"))
        self.assertTrue(page.locator("#plbFind").is_disabled())
        self.assertTrue(page.locator("#plbSource").is_disabled())

    def test_load_shows_the_student_figures(self) -> None:
        page = self._open(Fake())
        student = self._text(page, "#plbStudent")
        self.assertIn("Sample Student", student)
        self.assertIn(STUDENT_ID, student)
        self.assertIn("AI", student)
        self.assertIn("Sample Student", self._text(page, ".plb-sf:nth-child(1)"))
        self.assertIn("3.42", self._text(page, ".plb-sf:nth-child(2)"))
        # This term's registered credits, counted from the timetable: not the registrar's
        # running total (96 in the reply), which beside an 18-credit cap reads as a mistake.
        registered = self._text(page, ".plb-sf:nth-child(3)")
        self.assertIn("9", registered)
        self.assertIn("Registered now", registered)
        self.assertNotIn("96", student)
        self.assertIn("Graduates as it stands", self._text(page, ".plb-sf:nth-child(4)"))
        self.assertIn("After the first build", self._text(page, ".plb-sf:nth-child(4)"))
        self.assertEqual(self._text(page, "#plbContext"), "4500000 · 1448/1")
        self.assertEqual(self._text(page, "#plbStatus"), "")

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
        page.click("#plbTermBtn")
        page.fill("#plbYear", "14")
        page.click("#plbFetch")
        expect(page.locator("#plbStatus")).to_have_text("Enter a four-digit year.")
        expect(page.locator("#plbYear")).to_be_visible()  # the term opens for the fix
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
        self._fetch_forced(page, OTHER_ID)  # the newer one answers at once
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
        self.assertEqual(self._text(page, "#plbStatus"), "")

    def test_a_failed_fetch_leaves_the_plan_of_the_student_on_screen_alive(self) -> None:
        fake = Fake()
        fake.hold_plan = 1
        page = self._open(fake, load=False)
        self._fetch(page, STUDENT_ID)  # the student loads; their plan is still on its way
        self._eligible(page)
        self.assertEqual(self._rows(page, "could"), [])
        # A second fetch fails while that plan is still on its way.
        fake.context_answer = lambda body: (404, {"error": {"code": "STUDENT_NOT_FOUND"}})
        self._fetch(page, "4999999")
        expect(page.locator("#plbStatus")).to_have_text("No student found with that ID.")
        # The plan then arrives. It belongs to the student still on screen, so it is drawn:
        # the group must not be left saying "loading" for good.
        fake.pending_plan.pop().fulfill(json=STUDENT_PLAN)
        expect(page.locator("#plbAvailList .plb-arow[data-code='STAT305']")).to_be_visible()
        self.assertEqual(self._rows(page, "could"), ["STAT305", "CS351"])
        self.assertIn("Sample Student", self._text(page, "#plbStudent"))

    def test_a_slow_earlier_plan_cannot_paint_over_a_newer_students_plan(self) -> None:
        fake = Fake()
        fake.hold_plan = 1
        page = self._open(fake, load=False)
        self._fetch(page, STUDENT_ID)  # this student's plan is held back
        self._eligible(page)
        self._fetch(page, OTHER_ID)  # the newer student's plan answers at once
        expect(page.locator("#plbAvailList .plb-arow[data-code='STAT305']")).to_be_visible()
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
    def test_the_rules_row_and_the_cap_default(self) -> None:
        page = self._open(Fake())
        self.assertEqual(self._rules_now(page), {"mode": "keep", "elig": "strict", "max": "18"})
        expect(page.locator("#plbProgOnly")).to_be_checked()
        expect(page.locator("#plbFullIn")).not_to_be_checked()
        expect(page.locator("#plbSwapIn")).to_be_disabled()
        expect(page.locator("#plbSwapIn")).not_to_be_checked()
        self.assertTrue(page.locator("#plbRulesChanged").is_hidden())
        # Max credits comes from the student's cap: the old page's 17 was a bug.
        # The cap is the field's value; no "advisory / regulatory" hint line under it any more.
        self.assertEqual(page.locator("#plbMaxInHint").count(), 0)  # no hint line at all
        self.assertIn("not available yet", self._text(page, "#plbZoneRules"))
        self.assertEqual(page.locator("#plbRulesDialog, #plbEditRules, #plbRules2").count(), 0)

    def test_max_credits_follows_each_students_own_cap_and_an_unknown_cap_is_not_zero(self) -> None:
        fake = Fake()
        fake.context_answer = lambda body: (
            200,
            context_for(str(body["student_id"]), credit_cap=16, regulatory_max_credits=17),
        )
        page = self._open(fake)
        self.assertEqual(self._rules_now(page)["max"], "16")
        self.assertTrue(page.locator("#plbRulesChanged").is_hidden())
        self.assertEqual(self._load(page), ("15 / 16 cr", "6 planned + 9 registered · 1 cr free"))
        # A student whose cap is not known: nothing is shown as zero, nothing crashes.
        fake.context_answer = lambda body: (
            200,
            context_for(str(body["student_id"]), credit_cap=None, regulatory_max_credits=None),
        )
        self._fetch(page, OTHER_ID)
        expect(page.locator("#plbMaxIn")).to_have_value("")
        self.assertEqual(self._load(page), ("15 cr", "6 planned + 9 registered"))
        self.assertEqual(page.locator("#plbMaxInHint").count(), 0)  # no hint line at all
        self.assertNotIn("Credit cap 0", self._text(page, "#plbStudent"))
        # The student's cap is back when the next student has one.
        fake.context_answer = lambda body: (
            200,
            context_for(str(body["student_id"]), credit_cap=20),
        )
        self._fetch(page, "4500002")
        expect(page.locator("#plbMaxIn")).to_have_value("20")

    def test_the_rules_row_round_trips_each_value_and_counts_the_changes(self) -> None:
        fake = Fake()
        page = self._open(fake)
        before = len(fake.context_bodies)
        self._rules(page, mode="ignore")
        self.assertEqual(self._rules_now(page)["mode"], "ignore")
        self.assertEqual(self._text(page, "#plbRulesChanged"), "1 rule changed")
        self._rules(page, max=15)
        self.assertEqual(self._rules_now(page)["max"], "15")
        self.assertEqual(self._text(page, "#plbRulesChanged"), "2 rules changed")
        self._rules(page, prog=False)
        expect(page.locator("#plbProgAny")).to_be_checked()
        self._rules(page, full=True)
        expect(page.locator("#plbFullIn")).to_be_checked()
        self.assertEqual(self._rules_now(page), {"mode": "ignore", "elig": "strict", "max": "15"})
        self.assertEqual(self._text(page, "#plbRulesChanged"), "4 rules changed")
        # None of those four reloaded the student.
        self.assertEqual(len(fake.context_bodies), before)
        # Reset to the usual rules: nothing is changed any more.
        page.click("#plbRulesReset")
        self.assertEqual(self._rules_now(page), {"mode": "keep", "elig": "strict", "max": "18"})
        expect(page.locator("#plbProgOnly")).to_be_checked()
        expect(page.locator("#plbFullIn")).not_to_be_checked()
        self.assertTrue(page.locator("#plbRulesChanged").is_hidden())

    def test_the_max_credits_field_is_validated_in_place(self) -> None:
        page = self._open(Fake())
        for bad in ("abc", "0", "25", "-3"):
            page.fill("#plbMaxIn", bad)
            page.press("#plbMaxIn", "Enter")
            expect(page.locator("#plbMaxInErr")).to_have_text("Enter a whole number from 1 to 19.")
            expect(page.locator("#plbMaxIn")).to_have_attribute("aria-invalid", "true")
        page.fill("#plbMaxIn", "")
        page.press("#plbMaxIn", "Enter")
        expect(page.locator("#plbMaxInErr")).to_be_hidden()
        self.assertEqual(self._rules_now(page)["max"], "")

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
        page.click("#plbPlanList [data-act='remove'][data-code='AI342']")
        self.assertEqual(self._rows(page, "new"), ["AI331"])
        self._rules(page, max=16)
        before = len(fake.context_bodies)
        # The edited list asks first, and the switch keeps the rule in force until Go.
        page.click("#plbEligRelaxed")
        expect(page.locator("#plbEligConfirm")).to_be_visible()
        self.assertIn("reloads the suggested courses", self._text(page, "#plbEligConfirm"))
        expect(page.locator("#plbEligStrict")).to_be_checked()
        expect(page.locator("#plbEligGo")).to_be_focused()
        self.assertEqual(len(fake.context_bodies), before)
        page.click("#plbEligGo")
        expect(page.locator("#plbEligConfirm")).to_be_hidden()
        expect(page.locator("#plbEligRelaxed")).to_be_checked()
        expect(page.locator("#plbPlanList .plb-prow[data-code='AI431']")).to_be_visible()
        self.assertEqual(len(fake.context_bodies), before + 1)
        self.assertEqual(fake.context_bodies[-1]["eligibility_mode"], "relaxed")
        self.assertEqual(fake.context_bodies[-1]["student_id"], STUDENT_ID)
        # The suggestions were reloaded (the removed course is back), and the
        # max credits the adviser chose is still the max credits.
        self.assertEqual(self._rows(page, "new"), ["AI331", "AI342", "AI431"])
        self.assertEqual(self._rules_now(page)["max"], "16")
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
        expect(page.locator("#plbEligStrict")).to_be_checked()
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
        self._fetch_forced(page, "4999999")
        expect(page.locator("#plbStatus")).to_have_text("No student found with that ID.")
        expect(page.locator("#plbEligStrict")).to_be_checked()
        # The overtaken reload then answers: it is too late to change anything.
        _body, route = fake.pending.pop()
        route.fulfill(json=context_for(STUDENT_ID, eligibility_mode="relaxed"))
        page.wait_for_timeout(400)
        page.evaluate("new Promise(r => requestAnimationFrame(() => r(true)))")
        self.assertEqual(self._rules_now(page)["elig"], "strict")
        self.assertEqual(self._rows(page, "new"), ["AI331", "AI342"])
        self.assertEqual(self._text(page, "#plbRulesChanged"), "")

    def test_keep_and_escape_close_the_eligibility_confirm_and_return_focus_to_the_switch(
        self,
    ) -> None:
        for close in ("Escape", "#plbEligKeep"):
            with self.subTest(close=close):
                fake = Fake()
                page = self._open(fake)
                page.click("#plbPlanList [data-act='remove'][data-code='AI342']")
                before = len(fake.context_bodies)
                page.click("#plbEligRelaxed")
                expect(page.locator("#plbEligConfirm")).to_be_visible()
                if close == "Escape":
                    page.keyboard.press("Escape")
                else:
                    page.click(close)
                expect(page.locator("#plbEligConfirm")).to_be_hidden()
                expect(page.locator("#plbEligStrict")).to_be_checked()
                expect(page.locator("#plbEligStrict")).to_be_focused()
                self.assertEqual(self._rows(page, "new"), ["AI331"])  # the edit is kept
                self.assertEqual(len(fake.context_bodies), before)
                page.context.close()

    def test_another_rule_change_closes_the_eligibility_confirm(self) -> None:
        page = self._open(Fake())
        page.click("#plbPlanList [data-act='remove'][data-code='AI342']")
        page.click("#plbEligRelaxed")
        expect(page.locator("#plbEligConfirm")).to_be_visible()
        page.click("#plbRegIgnore")
        expect(page.locator("#plbEligConfirm")).to_be_hidden()
        expect(page.locator("#plbEligStrict")).to_be_checked()

    def test_the_over_cap_mark_goes_when_max_credits_is_cleared(self) -> None:
        page = self._open(Fake())
        self._rules(page, max=12)
        self.assertEqual(
            self._load(page), ("15 / 12 cr", "6 planned + 9 registered · 3 cr over the limit")
        )
        self.assertEqual(page.get_attribute("#plbPlanLoad", "data-over"), "true")
        self._rules(page, max="")
        self.assertEqual(self._load(page), ("15 cr", "6 planned + 9 registered"))
        self.assertIsNone(page.get_attribute("#plbPlanLoad", "data-over"))

    def test_the_masthead_year_is_not_used_by_a_rules_reload(self) -> None:
        fake = Fake()
        page = self._open(fake)
        page.fill("#plbStudentId", "9999999")  # typed, never fetched
        self._rules(page, elig="relaxed")
        expect(page.locator("#plbEligRelaxed")).to_be_checked()
        self.assertEqual(fake.context_bodies[-1]["student_id"], STUDENT_ID)

    # ── The courses ──────────────────────────────────────────────────────────
    def test_courses_are_the_plan_the_kept_rows_and_the_picker_with_counts(self) -> None:
        page = self._open(Fake())
        self.assertEqual(self._rows(page, "registered"), ["CS301", "CS322", "CS330"])
        self.assertEqual(self._rows(page, "new"), ["AI331", "AI342"])
        self.assertEqual(self._rows(page, "could"), ["STAT305", "CS351"])
        self.assertEqual(self._rows(page, "blocked"), ["AI431", "AI450"])
        self.assertEqual(self._load(page), ("15 / 18 cr", "6 planned + 9 registered · 3 cr free"))
        self.assertEqual(self._text(page, "#plbPlanCount"), "Your plan · 2 courses")
        # Registered: a lock, the credits and the section, no controls.
        reg = "#plbPlanList .plb-prow--kept[data-code='CS301']"
        self.assertEqual(self._text(page, f"{reg} .plb-cr"), "3 cr · section M1")
        self.assertEqual(page.locator(f"{reg} [role='img'][aria-label='Registered']").count(), 1)
        self.assertEqual(self._text(page, f"{reg} .plb-flag"), "Registered · kept")
        self.assertEqual(page.locator(f"{reg} button, {reg} input").count(), 0)
        # New: "Any section" and a Required toggle that is not pressed.
        row = "#plbPlanList .plb-prow[data-code='AI331']"
        self.assertEqual(self._text(page, f"{row} .plb-secbtn"), "Any section")
        self.assertEqual(
            page.get_attribute(f"{row} button[data-act='must']", "aria-pressed"), "false"
        )
        # Could be added: the credits and sections, and a seat warning when every open section is nearly full.
        page.select_option("#plbSource", "eligible")
        self.assertEqual(
            self._text(page, "#plbAvailList [data-code='STAT305'] .plb-meta"), "3 cr · 1 section"
        )
        self.assertEqual(
            self._text(page, "#plbAvailList [data-code='CS351'] .plb-meta"),
            "3 cr · 1 section Few seats",
        )
        page.click("#plbAvailList [data-code='CS351'] button[data-act='open']")
        line = self._text(page, "#plbAvailList [data-code='CS351'] .plb-sec:not(.plb-sec--h)")
        self.assertIn("Thu 09:00-11:30", line)
        self.assertIn("3 left", line)
        # Blocked: the reason; waiting for a course in the plan says so, for one outside it does not.
        self._rows(page, "blocked")
        self.assertIn(
            "Missing: AI331 (in this plan)", self._text(page, "#plbBlocked [data-code='AI431']")
        )
        self.assertIn("Missing: CS999", self._text(page, "#plbBlocked [data-code='AI450']"))
        self.assertNotIn("(in this plan)", self._text(page, "#plbBlocked [data-code='AI450']"))

    def test_add_remove_and_must_take_change_the_plan_and_the_credits_header(self) -> None:
        page = self._open(Fake())
        # A blocked course waiting for a course in the plan says so.
        page.click("#plbPlanList [data-act='remove'][data-code='AI331']")
        self.assertEqual(self._rows(page, "new"), ["AI342"])
        self.assertEqual(self._load(page), ("12 / 18 cr", "3 planned + 9 registered · 6 cr free"))
        self.assertIn("AI331", self._rows(page, "could"))
        self.assertEqual(self._text(page, "#plbStatus"), "Removed AI331 from the plan.")
        self.assertEqual(self._rows(page, "blocked"), ["AI431", "AI450"])
        self.assertNotIn("(in this plan)", self._text(page, "#plbBlocked [data-code='AI431']"))
        # Add it back with its tick and the arrow; the row moves.
        self._add(page, "AI331")
        self.assertEqual(self._rows(page, "new"), ["AI342", "AI331"])
        self.assertEqual(self._load(page), ("15 / 18 cr", "6 planned + 9 registered · 3 cr free"))
        self.assertEqual(self._text(page, "#plbStatus"), "Added AI331 to the plan.")
        self._rows(page, "blocked")
        self.assertIn("(in this plan)", self._text(page, "#plbBlocked [data-code='AI431']"))
        # Required survives a redraw (adding another course draws the plan again).
        must = "#plbPlanList button[data-act='must'][data-code='{}']"
        page.click(must.format("AI342"))
        self._add(page, "STAT305")
        self.assertEqual(page.get_attribute(must.format("AI342"), "aria-pressed"), "true")
        self.assertEqual(page.get_attribute(must.format("AI331"), "aria-pressed"), "false")
        self.assertEqual(self._load(page), ("18 / 18 cr", "9 planned + 9 registered · 0 cr free"))
        # Over the cap says so instead of a negative number.
        self._add(page, "CS351")
        self.assertEqual(
            self._load(page), ("21 / 18 cr", "12 planned + 9 registered · 3 cr over the limit")
        )
        self.assertEqual(page.get_attribute("#plbPlanLoad", "data-over"), "true")
        # A new max credits rule is the new cap.
        self._rules(page, max=19)
        self.assertEqual(
            self._load(page), ("21 / 19 cr", "12 planned + 9 registered · 2 cr over the limit")
        )

    def test_ignoring_registered_courses_changes_what_counts(self) -> None:
        page = self._open(Fake())
        self._rules(page, mode="ignore")
        self.assertEqual(self._load(page), ("6 / 18 cr", "6 planned · 12 cr free"))
        self.assertEqual(self._rows(page, "registered"), [])  # nothing is kept: no locked rows
        # The registered courses can be planned again.
        self.assertIn("CS301", self._rows(page, "could"))

    def test_the_search_narrows_what_the_student_could_add(self) -> None:
        page = self._open(Fake())
        page.fill("#plbFind", "stat")
        self.assertEqual(self._rows(page, "could"), ["STAT305"])
        page.check("#plbAvailList [data-code='STAT305'] input[data-act='tick']")
        page.click("#plbXAdd")
        self.assertIn("STAT305", self._rows(page, "new"))
        # By name, with no match, and a course already in the plan is not offered.
        page.fill("#plbFind", "network")
        self.assertEqual(self._rows(page, "could"), ["CS351"])
        page.fill("#plbFind", "zzz")
        self.assertIn("No course matches", self._text(page, "#plbAvailList"))
        page.fill("#plbFind", "AI331")
        self.assertIn("No course matches", self._text(page, "#plbAvailList"))

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
        # unknown, not "none": the row says no section count, and its list says why
        self.assertEqual(self._text(page, "#plbAvailList [data-code='CS351'] .plb-meta"), "3 cr")
        page.click("#plbAvailList [data-code='CS351'] button[data-act='open']")
        self.assertIn(
            "Section data is not available", self._text(page, "#plbAvailList [data-code='CS351']")
        )
        self.assertEqual(self._load(page), ("15 / 18 cr", "6 planned + 9 registered · 3 cr free"))

    def test_a_slow_earlier_section_reply_cannot_paint_over_a_newer_one(self) -> None:
        fake = Fake()
        page = self._open(fake)
        self.assertEqual(
            self._text(page, "#plbAvailList [data-code='CS351'] .plb-meta"),
            "3 cr · 1 section Few seats",
        )
        # A rule change asks for the sections again; that reply is slow ...
        fake.hold_catalog = 1
        self._rules(page, prog=False)
        expect(page.locator("#plbProgAny")).to_be_checked()
        page.wait_for_function("() => true")
        self.assertEqual(len(fake.pending_catalog), 1)
        self.assertFalse(fake.catalog_bodies[-1]["program_sections_only"])
        # ... and a second change is answered at once.
        self._rules(page, mode="ignore")
        expect(page.locator("#plbRegIgnore")).to_be_checked()
        self.assertEqual(len(fake.catalog_bodies), 3)
        # The slow reply then arrives, saying there are no sections at all.
        fake.pending_catalog.pop().fulfill(json={"ok": True, "sections": []})
        page.wait_for_timeout(400)
        self.assertEqual(
            self._text(page, "#plbAvailList [data-code='CS351'] .plb-meta"),
            "3 cr · 1 section Few seats",
        )

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

    def test_a_long_arabic_course_name_has_the_width_of_the_pane(self) -> None:
        # Real titles are long Arabic phrases. In a narrow column of their own they ran down four
        # or five lines; the name now shares one cell with its section line.
        long_name = "دراسات إسلامية: القيم والأخلاق الإسلامية"
        rows = [*BASELINE, _row("GS104", long_name, "M46", 2, "THU", "08:00", "09:40", 46)]
        fake = Fake()
        fake.context_answer = lambda body: (
            200,
            {**context_for(str(body["student_id"])), "baseline": rows},
        )
        for font in ("app", "wide"):
            with self.subTest(font=font):
                page = self._open(fake, font=font)
                kept = "#plbPlanList .plb-prow--kept[data-code='GS104']"
                self.assertEqual(self._text(page, f"{kept} .plb-nm"), long_name)
                self.assertEqual(self._text(page, f"{kept} .plb-cr"), "2 cr · section M46")
                name = page.locator(f"{kept} .plb-nm").bounding_box()
                pane = page.locator("#plbPlan").bounding_box()
                self.assertGreater(name["width"], pane["width"] * 0.4)
                self.assertLessEqual(name["height"], 18 * 3 + 2)  # at most three 18 px lines
                # The credits and section are a line of their own under the name, not beside it.
                under = page.locator(f"{kept} .plb-cr").bounding_box()
                self.assertGreaterEqual(under["y"], name["y"] + name["height"] - 1)

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
            "2 registered courses have no timetable slots (CS340, CS341)."
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
        self.assertEqual(self._rows(page, "registered"), [])
        self.assertEqual(self._load(page), ("6 / 18 cr", "6 planned + 0 registered · 12 cr free"))

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
        self.assertEqual(self._text(page, "#plbStatus"), "")
        self.assertIn("المعدل التراكمي", self._text(page, "#plbStudent"))
        self.assertIn("المسجّل الآن", self._text(page, "#plbStudent"))
        self.assertIn("بعد أول بناء", self._text(page, "#plbStudent"))
        self.assertEqual(self._rules_now(page), {"mode": "keep", "elig": "strict", "max": "18"})
        self.assertIn("الشُعب من", self._text(page, "#plbZoneRules"))
        self.assertIn("اقتراح تبديلات (غير متاح بعد)", self._text(page, "#plbZoneRules"))
        self.assertEqual(self._text(page, "#plbPlanList .plb-prow--kept .plb-flag"), "مسجّل · مُبقى")
        self.assertEqual(
            self._text(page, "#plbPlanList [data-code='AI331'] .plb-secbtn"), "أي شعبة"
        )
        self.assertEqual(self._load(page), ("15 / 18 ساعة", "6 مخطَّطة + 9 مسجّلة · 3 ساعات متاحة"))
        self._rows(page, "blocked")
        self.assertIn("ناقص:", self._text(page, "#plbBlocked [data-code='AI450']"))
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
        self.assertGreater(page.locator("#plbPick bdi[dir='ltr']").count(), 5)
        self.assertEqual(page.locator("#plbWeek .plb-bt bdi[dir='ltr']").count(), 6)
        # None of the English labels of this page are left on screen.
        text = self._text(page, "#plbRoot")
        for english in (
            "Registered",
            "Available",
            "Your plan",
            "Required",
            "Blocked",
            "No classes",
            "Edit rules",
        ):
            self.assertNotIn(english, text)
        # The week mirrors: Sunday is at the start edge, which is the right edge.
        sun = page.locator("#plbWeek .plb-dh").first.bounding_box()
        thu = page.locator("#plbWeek .plb-dh").nth(4).bounding_box()
        self.assertGreater(sun["x"], thu["x"])

    def test_arabic_status_lines_and_the_max_credits_error(self) -> None:
        fake = Fake()
        page = self._open(fake, language="ar", load=False)
        fake.context_answer = lambda body: (404, {"error": {"code": "STUDENT_NOT_FOUND"}})
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbStatus")).to_have_text("لا يوجد طالب بهذا الرقم.")
        page.fill("#plbStudentId", "")
        page.click("#plbFetch")
        expect(page.locator("#plbStatus")).to_have_text("أدخل رقم الطالب.")
        page.fill("#plbMaxIn", "x")
        page.press("#plbMaxIn", "Enter")
        expect(page.locator("#plbMaxInErr")).to_have_text("أدخل عددًا صحيحًا من 1 إلى 99.")
        self.assertIn("القواعد", self._text(page, "#plbRulesTitle"))

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
        page.focus("#plbRulesReset")
        outline = page.evaluate(
            "getComputedStyle(document.getElementById('plbRulesReset')).outlineStyle"
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


def test_no_dom_call_is_handed_a_bare_null() -> None:
    """`node.append(null)` writes the word "null" on the page.

    Only the script's `el()` skips empty children. An optional child handed straight to
    append / replaceChildren / prepend must go through `present()`: three such calls were written
    in one review cycle before this guard existed.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    offenders: list[int] = []
    for match in re.finditer(r"\.(append|replaceChildren|prepend)\(", source):
        depth, i, start, quote = 1, match.end(), match.end(), ""
        args: list[str] = []
        while i < len(source) and depth:
            ch = source[i]
            if quote:
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    quote = ""
            elif ch in "'\"`":
                quote = ch
            elif ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == "," and depth == 1:
                args.append(source[start:i])
                start = i + 1
            i += 1
        args.append(source[start : i - 1])
        for arg in args:
            # Look only at the argument's own level: what is nested inside el(...) is safe.
            flat = arg.strip()
            while True:
                smaller = re.sub(r"\([^()]*\)|\[[^\[\]]*\]|\{[^{}]*\}", "0", flat)
                if smaller == flat:
                    break
                flat = smaller
            if re.search(
                r"\?\s*(null|undefined|false)\s*:|(^|:)\s*(null|undefined|false)\s*$", flat
            ):
                offenders.append(source.count("\n", 0, match.start()) + 1)
                break
    assert offenders == [], f"a bare null reaches the DOM at line(s) {offenders}"

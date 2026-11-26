"""The new planner screen, part two, in a real browser: build, option tabs, week, apply.

The same synthetic student as part one (tests/planner_board_support.py). The build and the
save requests are answered by a fake shaped like the real replies (BuildFake in
tests/planner_board_support.py); nothing leaves the machine.

The student has three registered courses on Sun, Mon, Tue and Wed (Thu free, one 45 minute
wait, last class ends 14:00) and two courses in the plan, AI331 and AI342.
"""

from __future__ import annotations

from tests.planner_board_support import (
    C1,
    C2,
    EXPECTED_BUILD_BODY,
    EXPECTED_SAVE_BODY,
    FIVE,
    OTHER_ID,
    OVERFLOWS,
    SAVED,
    STUDENT_ID,
    BuildFake,
    PlannerBoardCase,
    _mapping,
    _option,
    build_reply,
    context_for,
    expect,
)


class PlannerBoardBuildBrowserTests(PlannerBoardCase):
    def _assert_options_gone(self, page) -> None:
        expect(page.locator("#plbBuild")).to_have_text("Build options")
        expect(page.locator("#plbBuildLine")).to_have_text(
            "The plan changed. Build again to see options."
        )
        self.assertEqual(self._tab_names(page), ["(registered)"])
        self.assertTrue(page.locator("#plbReview").is_disabled())
        self.assertEqual(self._blocks(page, "new"), [])
        self.assertTrue(page.locator("#plbLastBuild").is_hidden())

    # ── The request bodies ───────────────────────────────────────────────────
    def test_the_build_posts_exactly_the_body_the_current_page_sends(self) -> None:
        page, fake = self._built()
        self.assertEqual(fake.build_bodies, [EXPECTED_BUILD_BODY])

    def test_the_build_body_follows_the_rules_and_the_plan(self) -> None:
        fake = BuildFake()
        page = self._open(fake)
        self._rules(page, mode="ignore", elig="relaxed", max=15, prog=False, full=True, swap=True)
        page.click("#plbTable tbody[data-group='new'] button[data-act='remove'][data-code='AI342']")
        page.check("#plbTable input[data-act='must'][data-code='AI331']")
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text("Rebuild")
        body = fake.build_bodies[0]
        self.assertEqual(
            {k: body[k] for k in body if k not in ("shortlist", "baseline")},
            {
                "student_id": STUDENT_ID,
                "academic_year": "1448",
                "term": "1",
                "mode": "ignore",
                "eligibility_mode": "relaxed",
                "swap": True,
                "program_sections_only": False,
                "allow_full_sections": True,
                "max_credits": 15,
            },
        )
        self.assertEqual([c["course_code"] for c in body["shortlist"]], ["AI331"])
        self.assertTrue(body["shortlist"][0]["must_take"])

    def test_apply_posts_exactly_the_body_the_current_page_sends(self) -> None:
        page, fake = self._built()
        page.click("#plbReview")
        page.click("#plbApplyGo")
        expect(page.locator("#plbApplyDialog")).not_to_be_visible()
        self.assertEqual(fake.save_bodies, [EXPECTED_SAVE_BODY])

    # ── Build while it runs ──────────────────────────────────────────────────
    def test_while_building_the_button_is_busy_and_a_double_click_posts_once(self) -> None:
        fake = BuildFake()
        fake.hold_build = 1
        page = self._open(fake)
        page.dblclick("#plbBuild")
        expect(page.locator("#plbBuildLine")).to_have_text(
            "Building options… this can take up to 10 seconds"
        )
        self.assertTrue(page.locator("#plbBuild").is_disabled())
        self.assertEqual(page.get_attribute("#plbBuild", "aria-busy"), "true")
        page.wait_for_timeout(200)
        self.assertEqual(len(fake.build_bodies), 1)
        fake.pending_build.pop().fulfill(json=FIVE)
        expect(page.locator("#plbBuild")).to_have_text("Rebuild")
        self.assertTrue(page.locator("#plbBuild").is_enabled())
        self.assertIsNone(page.get_attribute("#plbBuild", "aria-busy"))
        self.assertEqual(self._text(page, "#plbBuildLine"), "")
        self.assertEqual(len(fake.build_bodies), 1)

    def test_a_reply_that_arrives_after_the_plan_changed_is_dropped(self) -> None:
        fake = BuildFake()
        fake.hold_build = 1
        page = self._open(fake)
        page.click("#plbBuild")
        expect(page.locator("#plbBuildLine")).to_contain_text("Building options")
        page.click("#plbTable tbody[data-group='new'] button[data-act='remove'][data-code='AI342']")
        fake.pending_build.pop().fulfill(json=FIVE)
        expect(page.locator("#plbBuildLine")).to_have_text(
            "The plan changed. Build again to see options."
        )
        self.assertEqual(self._tab_names(page), ["(registered)"])
        self.assertEqual(self._blocks(page, "new"), [])
        expect(page.locator("#plbBuild")).to_have_text("Build options")
        self.assertTrue(page.locator("#plbBuild").is_enabled())

    def test_a_reply_that_arrives_after_another_student_was_loaded_is_dropped(self) -> None:
        fake = BuildFake()
        fake.hold_build = 1
        page = self._open(fake)
        page.click("#plbBuild")
        expect(page.locator("#plbBuildLine")).to_contain_text("Building options")
        self._fetch(page, OTHER_ID)
        expect(page.locator("#plbContext")).to_have_text(f"{OTHER_ID} · 1448/1")
        fake.pending_build.pop().fulfill(json=FIVE)
        expect(page.locator("#plbBuildLine")).to_have_text(
            "The plan changed. Build again to see options."
        )
        self.assertEqual(self._tab_names(page), ["(registered)"])

    # ── Option tabs ──────────────────────────────────────────────────────────
    def test_five_options_give_three_tabs_more_options_and_registered_last(self) -> None:
        page, _ = self._built()
        self.assertEqual(self._tab_names(page), ["C1", "B1", "C2", "(registered)"])
        self.assertEqual(page.get_attribute("#plbTabs", "role"), "tablist")
        self.assertEqual(page.get_attribute("#plbTab_C1", "aria-selected"), "true")
        self.assertEqual(page.get_attribute("#plbTab_B1", "aria-selected"), "false")
        self.assertEqual(page.get_attribute("#plbTabRegistered", "aria-selected"), "false")
        self.assertEqual(self._text(page, "#plbMoreBtn"), "More options (2)")
        self.assertEqual(page.get_attribute("#plbMoreBtn", "aria-expanded"), "false")
        self.assertTrue(page.locator("#plbMoreList").is_hidden())
        # The tab list holds tabs only; "More options" is a disclosure beside it.
        order = page.eval_on_selector_all("#plbTabs > *", "ns => ns.map(n => n.id)")
        self.assertEqual(order, ["plbTab_C1", "plbTab_B1", "plbTab_C2", "plbTabRegistered"])
        roles = page.eval_on_selector_all(
            "#plbTabs > *", "ns => ns.map(n => n.getAttribute('role'))"
        )
        self.assertEqual(roles, ["tab", "tab", "tab", "tab"])
        beside = page.eval_on_selector_all("#plbStrip > *", "ns => ns.map(n => n.id)")
        self.assertEqual(beside, ["plbTabs", "plbMore"])

    def test_each_tab_says_its_three_lines_from_its_own_meetings(self) -> None:
        page, _ = self._built()
        # The first of the order carries the Suggested label on its last line.
        self.assertEqual(
            self._tab_lines(page, "C1"),
            [
                "C1 Four afternoons 15 cr",
                "4 days · Thu free",
                "ends 14:00 · gap 2 h 30 min Suggested",
            ],
        )
        self.assertEqual(
            self._tab_lines(page, "B1"),
            ["B1 Three afternoons 15 cr", "4 days · Thu free", "ends 14:15 · gap 1 h 45 min"],
        )
        self.assertEqual(
            self._tab_lines(page, "C2"),
            ["C2 Two afternoons 15 cr", "5 days · no free day", "ends 14:00 · gap 3 h 30 min"],
        )
        # The two that wait under "More options": B2 leaves a course out and, reading like B3,
        # goes without a name.
        self._pick(page, "B2")
        self.assertEqual(
            self._tab_lines(page, "B2"),
            ["B2 12 cr", "4 days · Thu free", "ends 14:00 · gap 45 min", "1 not scheduled"],
        )
        self._pick(page, "B3")
        self.assertEqual(
            self._tab_lines(page, "B3"),
            ["B3 15 cr", "5 days · no free day", "ends 16:15 · gap 1 h"],
        )
        # The Registered tab keeps what part one says.
        self.assertEqual(self._text(page, "#plbTabFacts1"), "4 days · Thu free")
        self.assertEqual(self._text(page, "#plbTabFacts2"), "ends 14:00 · gap 45 min")

    def test_more_options_opens_and_a_chosen_option_becomes_the_selected_tab(self) -> None:
        page, _ = self._built()
        page.click("#plbMoreBtn")
        self.assertEqual(page.get_attribute("#plbMoreBtn", "aria-expanded"), "true")
        expect(page.locator("#plbMoreList")).to_be_visible()
        items = page.eval_on_selector_all(
            "#plbMoreList .plb-more-item", "ns => ns.map(n => n.dataset.more)"
        )
        self.assertEqual(items, ["B3", "B2"])
        page.click("#plbMoreList [data-more='B3']")
        self.assertEqual(self._tab_names(page), ["C1", "B1", "C2", "B3", "(registered)"])
        self.assertEqual(page.get_attribute("#plbTab_B3", "aria-selected"), "true")
        self.assertEqual(self._text(page, "#plbMoreBtn"), "More options (1)")
        self.assertTrue(page.locator("#plbMoreList").is_hidden())
        expect(page.locator("#plbTab_B3")).to_be_focused()
        self.assertEqual(self._cell(page, "AI331"), "§M2")
        self.assertEqual(self._cell(page, "AI342"), "§M1")
        self.assertEqual(
            self._tab_lines(page, "B3"),
            ["B3 15 cr", "5 days · no free day", "ends 16:15 · gap 1 h"],
        )
        # Escape closes the list and gives the focus back to the button.
        page.click("#plbMoreBtn")
        expect(page.locator("#plbMoreList")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#plbMoreList")).to_be_hidden()
        expect(page.locator("#plbMoreBtn")).to_be_focused()

    def test_arrow_keys_home_and_end_move_between_the_tabs(self) -> None:
        page, _ = self._built()
        page.focus("#plbTab_C1")
        page.keyboard.press("ArrowRight")
        expect(page.locator("#plbTab_B1")).to_be_focused()
        self.assertEqual(page.get_attribute("#plbTab_B1", "aria-selected"), "true")
        self.assertEqual(page.get_attribute("#plbTab_B1", "tabindex"), "0")
        self.assertEqual(page.get_attribute("#plbTab_C1", "tabindex"), "-1")
        page.keyboard.press("End")
        expect(page.locator("#plbTabRegistered")).to_be_focused()
        self.assertEqual(page.get_attribute("#plbTabRegistered", "aria-selected"), "true")
        page.keyboard.press("ArrowRight")  # wraps to the first
        expect(page.locator("#plbTab_C1")).to_be_focused()
        page.keyboard.press("ArrowLeft")  # and back to the last
        expect(page.locator("#plbTabRegistered")).to_be_focused()
        page.keyboard.press("Home")
        expect(page.locator("#plbTab_C1")).to_be_focused()
        self.assertEqual(page.get_attribute("#plbWeekPanel", "aria-labelledby"), "plbTab_C1")

    # ── The week and the courses table follow the tab ───────────────────────
    def test_the_week_follows_the_selected_tab(self) -> None:
        page, _ = self._built()
        # C1, the first of the order, is selected: six registered blocks and three planned ones.
        self.assertEqual(len(self._blocks(page, "reg")), 6)
        self.assertEqual(sorted(self._blocks(page, "new")), ["AI331 new", "AI331 new", "AI342 new"])
        self.assertEqual(self._text(page, "#plbWeekName"), "Option C1 · Four afternoons")
        self.assertEqual(self._text(page, "#plbLegend"), "Registered Planned")
        footers = page.eval_on_selector_all(
            "#plbWeek .plb-df", "fs => fs.map(f => f.innerText.replace(/\\s+/g, ' ').trim())"
        )
        self.assertEqual(footers[0], "3 classes ends 13:15")
        self.assertEqual(footers[4], "No classes day off")
        page.click("#plbTab_B1")
        footers = page.eval_on_selector_all(
            "#plbWeek .plb-df", "fs => fs.map(f => f.innerText.replace(/\\s+/g, ' ').trim())"
        )
        self.assertEqual(footers[0], "3 classes ends 14:15")
        self._pick(page, "B3")
        self.assertEqual(len(self._blocks(page, "reg")), 6)
        self.assertEqual(sorted(self._blocks(page, "new")), ["AI331 new", "AI331 new", "AI342 new"])
        footers = page.eval_on_selector_all(
            "#plbWeek .plb-df", "fs => fs.map(f => f.innerText.replace(/\\s+/g, ' ').trim())"
        )
        self.assertEqual(footers[4], "1 class ends 10:15")
        page.click("#plbTabRegistered")
        self.assertEqual(self._blocks(page, "new"), [])
        self.assertEqual(len(self._blocks(page)), 6)
        self.assertEqual(self._text(page, "#plbWeekName"), "Registered")
        self.assertEqual(self._text(page, "#plbLegend"), "Registered")
        self.assertEqual(page.get_attribute("#plbWeekPanel", "aria-labelledby"), "plbTabRegistered")

    def test_a_planned_class_that_overlaps_a_registered_one_is_marked(self) -> None:
        fake = BuildFake()
        clash = _option("B1", [_mapping("AI331", "M1", 11, ["SUN"], "10:00", "11:15")])
        fake.build_answer = lambda body: (200, build_reply([clash]))
        page, _ = self._built(fake)
        self.assertEqual(page.locator("#plbWeek .plb-b[data-clash='true']").count(), 2)
        self.assertIn("1 planned class overlaps a registered class", self._text(page, "#plbWeek"))

    def test_the_section_cells_follow_the_tab_and_an_unplaced_course_says_so(self) -> None:
        page, _ = self._built()
        page.click("#plbTab_B1")
        self.assertEqual(self._cell(page, "AI331"), "§M2")
        self.assertEqual(self._cell(page, "AI342"), "§M1")
        self._pick(page, "B2")
        self.assertEqual(self._cell(page, "AI331"), "§M1")
        self.assertEqual(self._cell(page, "AI342"), "not scheduled in this option")
        self.assertEqual(self._tab_lines(page, "B2")[-1], "1 not scheduled")
        # Registered courses keep their cells; the Registered tab reads as in part one.
        self.assertEqual(
            self._text(page, "tbody[data-group='registered'] tr[data-code='CS301'] .plb-sc"), "§M1"
        )
        page.click("#plbTabRegistered")
        self.assertEqual(self._cell(page, "AI331"), "Any section")
        self.assertEqual(self._cell(page, "AI342"), "Any section")

    # ── Review and apply ─────────────────────────────────────────────────────
    def test_review_lists_what_will_be_saved_and_apply_saves_once_then_reloads(self) -> None:
        fake = BuildFake()
        fake.hold_save = 1
        page, _ = self._built(fake)
        self.assertTrue(page.locator("#plbReview").is_enabled())
        self.assertEqual(
            self._text(page, ".plb-acts-safe"),
            "Nothing is saved and no seat is held until you apply.",
        )
        contexts = len(fake.context_bodies)
        page.click("#plbReview")
        expect(page.locator("#plbApplyDialog")).to_be_visible()
        self.assertEqual(self._text(page, "#plbApplyTitle"), "Review and apply option C1")
        rows = page.eval_on_selector_all(
            "#plbApplyList li", "ls => ls.map(l => l.textContent.replace(/\\s+/g, ' ').trim())"
        )
        self.assertEqual(len(rows), 2)
        self.assertIn("AI331 §M1 Machine Learning", rows[0])
        self.assertIn("AI342 §M1 Computer Vision", rows[1])
        self.assertIn("Registered courses stay as they are (3)", self._text(page, "#plbApplyReg"))
        self.assertIn("CS301", self._text(page, "#plbApplyReg"))
        self.assertIn("is removed", self._text(page, "#plbApplyWarn"))
        expect(page.locator("#plbApplyCancel")).to_be_focused()
        page.dblclick("#plbApplyGo")
        self.assertTrue(page.locator("#plbApplyGo").is_disabled())
        self.assertEqual(self._text(page, "#plbApplyGo"), "Saving…")
        page.wait_for_timeout(200)
        self.assertEqual(len(fake.save_bodies), 1)
        fake.saved = True
        fake.pending_save.pop().fulfill(json=SAVED)
        expect(page.locator("#plbApplyDialog")).not_to_be_visible()
        expect(page.locator("#plbBuildLine")).to_have_text(
            "Saved 2 sections as this student's planned timetable for 1448/1."
            " Registered courses are unchanged."
        )
        self.assertEqual(self._text(page, "#plbStatus"), "Saved")
        self.assertEqual(len(fake.save_bodies), 1)
        # The student was loaded again: the registered week holds the saved sections.
        self.assertEqual(len(fake.context_bodies), contexts + 1)
        self.assertEqual(self._tab_names(page), ["(registered)"])
        expect(page.locator("#plbBuild")).to_have_text("Build options")
        self.assertTrue(page.locator("#plbReview").is_disabled())
        self.assertEqual(sorted(self._blocks(page, "reg")).count("AI331 reg"), 2)
        self.assertEqual(sorted(self._blocks(page, "reg")).count("AI342 reg"), 1)
        self.assertEqual(self._blocks(page, "new"), [])
        expect(page.locator("#plbBuild")).to_be_focused()

    def test_what_a_save_removed_or_clashed_with_is_said(self) -> None:
        fake = BuildFake()
        fake.save_answer = lambda body: (
            200,
            {
                **SAVED,
                "removed": [{"term_section_id": 9, "course_code": "AI100", "section": "M1"}],
                "removed_count": 1,
                "clashes_with_registered": [
                    {
                        "course_code": "AI331",
                        "section": "M2",
                        "slot": "MON 10:00-11:15",
                        "clashes_with": "CS330",
                    }
                ],
            },
        )
        page, _ = self._built(fake)
        page.click("#plbReview")
        page.click("#plbApplyGo")
        expect(page.locator("#plbBuildLine")).to_contain_text(
            "Registered courses are unchanged. 1 previously saved planned"
        )
        line = self._text(page, "#plbBuildLine")
        self.assertIn("AI100 M1", line)
        self.assertIn("1 class clashes with the registered timetable", line)
        self.assertIn("CS330", line)

    def test_a_refused_save_keeps_the_dialog_open_with_the_servers_reason(self) -> None:
        fake = BuildFake()
        fake.save_answer = lambda body: (
            409,
            {
                "ok": False,
                "error": {
                    "code": "SECTION_NOT_AVAILABLE_TO_STUDENT",
                    "message": "Some sections are outside the student's programme or cohort",
                },
            },
        )
        page, _ = self._built(fake)
        contexts = len(fake.context_bodies)
        page.click("#plbReview")
        page.click("#plbApplyGo")
        expect(page.locator("#plbApplyErr")).to_have_text(
            "Some sections are outside the student's programme or cohort"
        )
        expect(page.locator("#plbApplyDialog")).to_be_visible()
        self.assertTrue(page.locator("#plbApplyGo").is_enabled())
        self.assertEqual(self._text(page, "#plbApplyGo"), "Apply")
        # Nothing was cleared and the student was not reloaded.
        self.assertEqual(len(fake.context_bodies), contexts)
        page.click("#plbApplyCancel")
        expect(page.locator("#plbApplyDialog")).not_to_be_visible()
        self.assertEqual(self._tab_names(page), ["C1", "B1", "C2", "(registered)"])
        # Other refusals read the way the current page reads them.
        fake.save_answer = lambda body: (500, {"ok": False, "error": "plain text refusal"})
        page.click("#plbReview")
        page.click("#plbApplyGo")
        expect(page.locator("#plbApplyErr")).to_have_text("plain text refusal")
        fake.save_answer = lambda body: (403, {"detail": "CSRF"})
        page.click("#plbApplyGo")
        expect(page.locator("#plbApplyErr")).to_contain_text("session has expired")
        # A 403 that carries the server's own reason (role, student scope) is not a lost session.
        fake.save_answer = lambda body: (
            403,
            {"ok": False, "error": {"code": "STAFF_ROLE_REQUIRED", "message": "Staff only"}},
        )
        page.click("#plbApplyGo")
        expect(page.locator("#plbApplyErr")).to_have_text("Staff only")
        self.assertEqual(len(fake.save_bodies), 4)

    def test_options_dropped_under_an_open_review_close_it_and_nothing_is_saved(self) -> None:
        fake = BuildFake()
        page, _ = self._built(fake)
        fake.hold_ids = {OTHER_ID}
        self._fetch(page, OTHER_ID)  # another student is on the way, slowly
        page.click("#plbReview")
        expect(page.locator("#plbApplyDialog")).to_be_visible()
        # The other student arrives under the open review: the options it lists are gone.
        _body, route = fake.pending.pop()
        route.fulfill(json=context_for(OTHER_ID))
        expect(page.locator("#plbApplyDialog")).to_be_hidden()
        self._assert_options_gone(page)
        expect(page.locator("#plbBuild")).to_be_focused()
        self.assertEqual(fake.save_bodies, [])

    def test_apply_after_the_options_went_during_a_refused_save_closes_the_review(self) -> None:
        fake = BuildFake()
        fake.hold_save = 1
        page, _ = self._built(fake)
        fake.hold_ids = {OTHER_ID}
        self._fetch(page, OTHER_ID)  # another student is on the way, slowly
        page.click("#plbReview")
        page.click("#plbApplyGo")  # and the save is on its way too
        expect(page.locator("#plbApplyGo")).to_be_disabled()
        # The student arrives first: the options go, the save is left to finish and report.
        _body, route = fake.pending.pop()
        route.fulfill(json=context_for(OTHER_ID))
        expect(page.locator("#plbStatus")).to_have_text("Student loaded.")
        expect(page.locator("#plbApplyDialog")).to_be_visible()
        fake.pending_save.pop().fulfill(
            status=409, json={"ok": False, "error": {"code": "X", "message": "Refused"}}
        )
        expect(page.locator("#plbApplyErr")).to_have_text("Refused")
        # Apply again: there is nothing left to save, and the page says so instead of going quiet.
        page.click("#plbApplyGo")
        expect(page.locator("#plbApplyDialog")).to_be_hidden()
        expect(page.locator("#plbBuildLine")).to_have_text(
            "The plan changed. Build again to see options."
        )
        self.assertEqual(len(fake.save_bodies), 1)

    def test_choosing_a_tab_keeps_the_focus_on_it(self) -> None:
        page, _ = self._built()
        page.focus("#plbTab_C1")
        page.keyboard.press("ArrowRight")
        expect(page.locator("#plbTab_B1")).to_be_focused()
        # Enter on a tab redraws the strip: the keyboard must not fall to the top of the page.
        page.keyboard.press("Enter")
        expect(page.locator("#plbTab_B1")).to_be_focused()
        self.assertEqual(page.get_attribute("#plbTab_B1", "aria-selected"), "true")
        page.click("#plbTab_C2")
        expect(page.locator("#plbTab_C2")).to_be_focused()
        self.assertEqual(page.get_attribute("#plbTab_C2", "aria-selected"), "true")
        page.click("#plbTabRegistered")
        expect(page.locator("#plbTabRegistered")).to_be_focused()

    def test_apply_is_available_only_on_an_option_tab(self) -> None:
        page, _ = self._built()
        self.assertTrue(page.locator("#plbReview").is_enabled())
        page.click("#plbTabRegistered")
        self.assertTrue(page.locator("#plbReview").is_disabled())
        page.click("#plbTab_B1")
        self.assertTrue(page.locator("#plbReview").is_enabled())

    def test_comparison_only_builds_cannot_be_applied_and_say_why(self) -> None:
        fake = BuildFake()
        page = self._open(fake)
        self._rules(page, prog=False)
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text("Rebuild")
        self.assertFalse(fake.build_bodies[0]["program_sections_only"])
        self.assertTrue(page.locator("#plbReview").is_disabled())
        self.assertEqual(
            self._text(page, "#plbApplyWhy"),
            "Comparison only: other programmes' sections are included",
        )
        page.click("#plbTab_B1")
        self.assertTrue(page.locator("#plbReview").is_disabled())
        self.assertEqual(fake.save_bodies, [])

    def test_an_option_with_no_sections_to_save_cannot_be_applied(self) -> None:
        fake = BuildFake()
        empty = _option("B1", [])
        fake.build_answer = lambda body: (200, build_reply([empty]))
        page, _ = self._built(fake)
        self.assertTrue(page.locator("#plbReview").is_disabled())
        self.assertEqual(self._text(page, "#plbApplyWhy"), "This option has no sections to save")

    # ── Last build ───────────────────────────────────────────────────────────
    def test_last_build_shows_a_latin_time_in_both_languages(self) -> None:
        page, _ = self._built()
        self.assertRegex(self._text(page, "#plbLastBuild"), r"^Last build \d\d:\d\d$")
        self.assertEqual(page.locator("#plbLastBuild bdi[dir='ltr']").count(), 1)
        page.context.close()
        page, _ = self._built(language="ar")
        self.assertRegex(self._text(page, "#plbLastBuild"), r"^آخر بناء [0-9]{2}:[0-9]{2}$")
        self.assertEqual(page.locator("#plbLastBuild bdi[dir='ltr']").count(), 1)

    # ── States ───────────────────────────────────────────────────────────────
    def test_no_option_found_shows_the_reply_s_reasons_not_a_broken_box(self) -> None:
        fake = BuildFake()
        reply = build_reply([])
        reply["summary"]["hard_constraint_failures"] = [
            {"kind": "must_take", "course_code": "AI331", "reason": "AI331 cannot be placed"}
        ]
        reply["unscheduled"] = [
            {"course_code": "AI342", "reason_code": "ALL_SECTIONS_CLASH"},
            {"course_code": "CS351", "reason_code": "ALL_SECTIONS_CLASH"},
        ]
        fake.build_answer = lambda body: (200, reply)
        page, _ = self._built(fake)
        note = self._text(page, "#plbBuildNote")
        self.assertIn("No option could be built under the current constraints.", note)
        self.assertIn("AI331: AI331 cannot be placed", note)
        self.assertIn(
            "Not scheduled: AI342, CS351 — Every section clashes with the chosen plan", note
        )
        self.assertEqual(self._tab_names(page), ["(registered)"])
        self.assertTrue(page.locator("#plbReview").is_disabled())
        self.assertEqual(len(self._blocks(page, "reg")), 6)
        self.assertEqual(self._cell(page, "AI331"), "Any section")
        # A reply with no reasons at all still says something plain.
        fake.build_answer = lambda body: (200, build_reply([]))
        page.click("#plbBuild")
        expect(page.locator("#plbBuildNote")).to_contain_text(
            "No valid result satisfies every must-take"
        )

    def test_a_failed_build_a_429_and_an_expired_session_each_give_a_plain_line(self) -> None:
        fake = BuildFake()
        page = self._open(fake)
        line = page.locator("#plbBuildLine")
        fake.build_answer = lambda body: (
            500,
            {"ok": False, "error": {"code": "INTERNAL", "message": "The solver crashed"}},
        )
        page.click("#plbBuild")
        expect(line).to_have_text("The solver crashed")
        self.assertEqual(page.get_attribute("#plbBuildLine", "data-kind"), "error")
        self.assertEqual(self._tab_names(page), ["(registered)"])
        self.assertEqual(self._text(page, "#plbBuild"), "Build options")
        self.assertTrue(page.locator("#plbBuild").is_enabled())
        fake.build_answer = lambda body: (200, {"ok": False, "error": "A text error"})
        page.click("#plbBuild")
        expect(line).to_have_text("A text error")
        fake.build_raw = lambda route: route.fulfill(status=502, body="<html>bad gateway</html>")
        page.click("#plbBuild")
        expect(line).to_have_text("The build failed. Try again.")
        fake.build_raw = lambda route: route.abort()
        page.click("#plbBuild")
        expect(line).to_have_text("The build failed. Try again.")
        # A 429 has a different JSON shape: a string, not an object.
        fake.build_raw = lambda route: route.fulfill(
            status=429,
            headers={"Retry-After": "5"},
            json={"error": "Rate limit exceeded. Please try again later."},
        )
        page.click("#plbBuild")
        expect(line).to_have_text("Too many builds. Try again in a moment.")
        # The session has expired: a 403, or a redirect to the sign-in page.
        fake.build_raw = lambda route: route.fulfill(status=403, body="<html>Forbidden</html>")
        page.click("#plbBuild")
        expect(line).to_have_text("Your session has expired. Sign in again, then retry.")
        fake.build_raw = lambda route: route.fulfill(
            status=302, headers={"Location": "/definitely-not-a-page/"}
        )
        page.click("#plbBuild")
        expect(line).to_have_text("Your session has expired. Sign in again, then retry.")
        # A 403 that carries the server's own reason is a refusal, said in the server's words.
        fake.build_raw = None
        fake.build_answer = lambda body: (
            403,
            {"ok": False, "error": {"code": "SCOPE", "message": "Not one of your students."}},
        )
        page.click("#plbBuild")
        expect(line).to_have_text("Not one of your students.")
        fake.build_answer = lambda body: (403, {"error": "Insufficient role: requires staff"})
        page.click("#plbBuild")
        expect(line).to_have_text("Insufficient role: requires staff")
        # And the page recovers by itself.
        fake.build_raw = None
        fake.build_answer = lambda body: (200, FIVE)
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text("Rebuild")
        self.assertEqual(self._text(page, "#plbBuildLine"), "")

    # ── A build goes out of date ─────────────────────────────────────────────
    def test_loading_another_student_removes_the_options(self) -> None:
        # A change to the plan or a rule now keeps the options, marked out of date
        # (tests/test_planner_board_detail_browser.py); another student still removes them.
        page, _ = self._built()
        self.assertEqual(self._tab_names(page)[0], "C1")
        self._fetch(page, OTHER_ID)
        self._assert_options_gone(page)
        self.assertEqual(self._cell(page, "AI331"), "Any section")

    def test_applying_the_rules_dialog_with_nothing_changed_keeps_the_options(self) -> None:
        page, _ = self._built()
        page.click("#plbEditRules")
        page.click("#plbRulesApply")
        expect(page.locator("#plbRulesDialog")).not_to_be_visible()
        self.assertEqual(self._tab_names(page), ["C1", "B1", "C2", "(registered)"])
        self.assertTrue(page.locator("#plbReview").is_enabled())

    def test_a_rebuild_replaces_the_options_and_selects_the_first(self) -> None:
        fake = BuildFake()
        page, _ = self._built(fake)
        page.click("#plbTab_C2")
        fake.build_answer = lambda body: (200, build_reply([C1, C2]))
        page.click("#plbBuild")
        expect(page.locator("#plbTab_C1")).to_be_visible()
        self.assertEqual(self._tab_names(page), ["C1", "C2", "(registered)"])
        self.assertEqual(page.get_attribute("#plbTab_C1", "aria-selected"), "true")
        self.assertEqual(len(fake.build_bodies), 2)
        self.assertEqual(page.locator("#plbMore").count(), 0)

    # ── Arabic, mirroring, size ──────────────────────────────────────────────
    def test_arabic_labels_and_right_to_left(self) -> None:
        fake = BuildFake()
        page = self._open(fake, language="ar")
        self.assertEqual(page.evaluate("document.documentElement.dir"), "rtl")
        self.assertEqual(self._text(page, "#plbBuild"), "ابنِ الخيارات")
        self.assertEqual(self._text(page, "#plbReview"), "مراجعة وتطبيق")
        self.assertEqual(
            self._text(page, ".plb-acts-safe"), "لا يُحفظ شيء ولا يُحجز أي مقعد حتى تضغط تطبيق."
        )
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text("أعد البناء")
        self.assertEqual(self._text(page, "#plbMoreBtn"), "خيارات أخرى (2)")
        self._pick(page, "B2")
        self.assertEqual(
            self._tab_lines(page, "B2"),
            [
                "B2 12 ساعة",
                "4 أيام · الخميس فارغ",
                "تنتهي 14:00 · فجوة 45 د",
                "مقرر واحد غير مجدول",
            ],
        )
        self.assertEqual(self._text(page, "#plbWeekName"), "الخيار B2")
        self.assertEqual(self._text(page, "#plbLegend"), "المسجّل المخطَّط")
        self.assertEqual(self._cell(page, "AI342"), "غير مجدول في هذا الخيار")
        # The keys mirror: in a right-to-left page the left arrow goes to the next tab.
        page.click("#plbTab_B1")
        page.focus("#plbTab_B1")
        page.keyboard.press("ArrowLeft")
        expect(page.locator("#plbTab_C2")).to_be_focused()
        # Tabs run from the right edge, and the dialog speaks Arabic.
        first = page.locator("#plbTab_C1").bounding_box()
        last = page.locator("#plbTabRegistered").bounding_box()
        self.assertGreater(first["x"], last["x"])
        page.click("#plbReview")
        self.assertEqual(self._text(page, "#plbApplyTitle"), "مراجعة وتطبيق الخيار C2")
        self.assertEqual(self._text(page, "#plbApplyGo"), "تطبيق")
        self.assertIn("المقررات المسجّلة تبقى كما هي (3)", self._text(page, "#plbApplyReg"))
        self.assertGreater(page.locator("#plbApplyList bdi[dir='ltr']").count(), 3)
        page.click("#plbApplyCancel")
        # None of this page's English is left on screen.
        text = self._text(page, "#plbRoot")
        for english in (
            "Build",
            "Rebuild",
            "Review and apply",
            "More options",
            "Planned",
            "Last build",
        ):
            self.assertNotIn(english, text)
        fake.build_raw = lambda route: route.fulfill(status=429, json={"error": "x"})
        page.click("#plbBuild")
        expect(page.locator("#plbBuildLine")).to_have_text("طلبات بناء كثيرة. حاول بعد لحظات.")

    def test_nothing_overflows_with_options_at_three_widths_in_both_languages(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in ("en", "ar"):
            for font in ("app", "wide"):
                for width, height in ((1440, 900), (1152, 800), (390, 844)):
                    page, _ = self._built(
                        None, language=language, width=width, height=height, font=font
                    )
                    page.click("#plbMoreBtn")
                    expect(page.locator("#plbMoreList")).to_be_visible()
                    found = list(page.evaluate(OVERFLOWS))
                    page.keyboard.press("Escape")
                    page.click("#plbTab_B1")
                    found += page.evaluate(OVERFLOWS)
                    page.click("#plbReview")
                    expect(page.locator("#plbApplyDialog")).to_be_visible()
                    box = page.locator("#plbApplyDialog").bounding_box()
                    if box["x"] < -0.5 or box["x"] + box["width"] > width + 0.5:
                        found.append(f"the dialog spills: {box}")
                    if found:
                        problems[f"{language} {width}px {font} fonts"] = found
                    page.context.close()
        if problems:
            self.fail(
                "\n" + "\n".join(f"{k}:\n    " + "\n    ".join(v) for k, v in problems.items())
            )

    def test_the_new_controls_are_big_enough_and_focus_shows(self) -> None:
        page, _ = self._built(None, width=1440, height=900)
        sizes = page.evaluate(
            "() => [...document.querySelectorAll('#plbBuild, #plbReview, #plbMoreBtn, .plb-tab')]"
            ".map(e => [e.id || e.dataset.opt, e.getBoundingClientRect().height])"
        )
        self.assertEqual([s for s in sizes if s[1] < 31.5], [])
        page.context.close()
        page, _ = self._built(None, width=390, height=844)
        sizes = page.evaluate(
            "() => [...document.querySelectorAll('#plbBuild, #plbReview, #plbMoreBtn, .plb-tab')]"
            ".map(e => [e.id || e.dataset.opt, e.getBoundingClientRect().height])"
        )
        self.assertEqual([s for s in sizes if s[1] < 43.5], [])
        page.keyboard.press("Shift")
        page.focus("#plbTab_B1")
        self.assertNotEqual(
            page.evaluate("getComputedStyle(document.getElementById('plbTab_B1')).outlineStyle"),
            "none",
        )

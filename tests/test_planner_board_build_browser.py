"""The new planner screen, part two, in a real browser: build, option tabs, week, apply.

The same synthetic student as part one (tests/planner_board_support.py). The build and the
save requests are answered by a fake shaped like the real replies (see
tests/test_planner_flow_browser.py); nothing leaves the machine.

The student has three registered courses on Sun, Mon, Tue and Wed (Thu free, one 45 minute
wait, last class ends 14:00) and two courses in the plan, AI331 and AI342.
"""

from __future__ import annotations

from tests.planner_board_support import (
    BASELINE,
    OTHER_ID,
    OVERFLOWS,
    STUDENT_ID,
    Fake,
    PlannerBoardCase,
    _row,
    context_for,
    expect,
)


def _mapping(code, section, tsid, days, start, end):
    return {
        "course_code": code,
        "course_key": code,
        "course_number": "",
        "section": section,
        "term_section_id": tsid,
        "meetings": [{"day": d, "start_time": start, "end_time": end} for d in days],
        "meeting_issue_codes": [],
    }


def _option(name, mappings, unscheduled=()):
    return {
        "name": name,
        "method": name[0],
        "rank": int(name[1]),
        "scheduled": len(mappings),
        "target": 2,
        "mappings": mappings,
        "unscheduled": list(unscheduled),
    }


# Each option with what its tab must say (registered week included, three credits a course):
#   B1  15 cr  4 days, Thu free   ends 14:15  longest gap 1 h 45 min
#   B2  12 cr  4 days, Thu free   ends 14:00  longest gap 45 min, one course not scheduled
#   B3  15 cr  5 days, no free    ends 16:15  longest gap 1 h
#   C1  15 cr  4 days, Thu free   ends 14:00  longest gap 2 h 30 min
#   C2  15 cr  5 days, no free    ends 14:00  longest gap 3 h 30 min
B1 = _option(
    "B1",
    [
        _mapping("AI331", "M2", 12, ["MON", "WED"], "10:00", "11:15"),
        _mapping("AI342", "M1", 15, ["SUN"], "13:00", "14:15"),
    ],
)
B2 = _option(
    "B2",
    [_mapping("AI331", "M1", 11, ["SUN", "TUE"], "11:30", "12:45")],
    unscheduled=[{"course_code": "AI342", "reason_code": "ALL_SECTIONS_CLASH"}],
)
B3 = _option(
    "B3",
    [
        _mapping("AI331", "M2", 12, ["MON", "WED"], "15:00", "16:15"),
        _mapping("AI342", "M1", 15, ["THU"], "09:00", "10:15"),
    ],
)
C1 = _option(
    "C1",
    [
        _mapping("AI331", "M1", 11, ["SUN", "TUE"], "12:00", "13:15"),
        _mapping("AI342", "M1", 15, ["MON"], "09:00", "10:15"),
    ],
)
C2 = _option(
    "C2",
    [
        _mapping("AI331", "M2", 12, ["MON", "WED"], "08:00", "09:15"),
        _mapping("AI342", "M1", 15, ["THU"], "08:00", "09:15"),
    ],
)


def build_reply(options):
    return {
        "ok": True,
        "summary": {
            "scheduled": 2,
            "target": 2,
            "conflicts": 0,
            "swaps_required": 0,
            "best_feasible": bool(options),
            "hard_constraint_failures": [],
        },
        "options": options,
        "unscheduled": [],
        "swap_suggestions": [],
        "mode": "keep",
        "eligibility_mode": "strict",
        "constraints": {"program_sections_only": True, "allow_full_sections": False},
        "graduation_projection_token": "token",
    }


FIVE = build_reply([B1, B2, B3, C1, C2])
SAVED = {
    "ok": True,
    "inserted": 2,
    "removed": [],
    "removed_count": 0,
    "clashes_with_registered": [],
}
SAVED_ROWS = [
    _row("AI331", "Machine Learning", "M2", 3, "MON", "10:00", "11:15", 12),
    _row("AI331", "Machine Learning", "M2", 3, "WED", "10:00", "11:15", 12),
    _row("AI342", "Computer Vision", "M1", 3, "SUN", "13:00", "14:15", 15),
]

EXPECTED_BUILD_BODY = {
    "student_id": STUDENT_ID,
    "academic_year": "1448",
    "term": "1",
    "mode": "keep",
    "eligibility_mode": "strict",
    "swap": False,
    "program_sections_only": True,
    "allow_full_sections": False,
    "max_credits": 18,
    "shortlist": [
        {
            "course_code": "AI331",
            "course_name": "Machine Learning",
            "credits": 3,
            "priority": "High",
            "status": "Eligible",
            "missing_prerequisites": [],
            "must_take": False,
            "score": 90,
        },
        {
            "course_code": "AI342",
            "course_name": "Computer Vision",
            "credits": 3,
            "priority": "High",
            "status": "Eligible",
            "missing_prerequisites": [],
            "must_take": False,
            "score": 90,
        },
    ],
    "baseline": BASELINE,
}
EXPECTED_SAVE_BODY = {
    "student_id": STUDENT_ID,
    "academic_year": "1448",
    "term": "1",
    "term_section_ids": [12, 15],
    "confirm_replace": True,
}


class BuildFake(Fake):
    """Fake with the build and the save endpoints, and a student who changes once saved."""

    def __init__(self) -> None:
        super().__init__()
        self.build_bodies: list[dict] = []
        self.save_bodies: list[dict] = []
        self.build_answer = lambda body: (200, FIVE)
        self.save_answer = lambda body: (200, SAVED)
        self.hold_build = 0
        self.pending_build: list = []
        self.hold_save = 0
        self.pending_save: list = []
        self.saved = False
        self.build_raw = None  # a callable(route) that answers the next build itself
        self.context_answer = self._context

    def _context(self, body):
        payload = context_for(str(body["student_id"]), eligibility_mode=body["eligibility_mode"])
        if self.saved:
            payload["baseline"] = [*BASELINE, *SAVED_ROWS]
        return 200, payload

    def build(self, route) -> None:
        self.build_bodies.append(route.request.post_data_json)
        if self.build_raw is not None:
            self.build_raw(route)
            return
        if self.hold_build:
            self.hold_build -= 1
            self.pending_build.append(route)
            return
        status, payload = self.build_answer(route.request.post_data_json)
        route.fulfill(status=status, json=payload)

    def save(self, route) -> None:
        body = route.request.post_data_json
        self.save_bodies.append(body)
        if self.hold_save:
            self.hold_save -= 1
            self.pending_save.append(route)
            return
        status, payload = self.save_answer(body)
        if status == 200:
            self.saved = True
        route.fulfill(status=status, json=payload)


class PlannerBoardBuildBrowserTests(PlannerBoardCase):
    def _built(self, fake=None, **kwargs):
        fake = fake or BuildFake()
        page = self._open(fake, **kwargs)
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text(
            "Rebuild" if kwargs.get("language", "en") == "en" else "أعد البناء"
        )
        expect(page.locator("#plbBuild")).to_be_enabled()
        return page, fake

    @staticmethod
    def _tab_names(page) -> list[str]:
        return page.eval_on_selector_all(
            "#plbTabs [role='tab']", "tabs => tabs.map(t => t.dataset.opt || '(registered)')"
        )

    @staticmethod
    def _tab_lines(page, name: str) -> list[str]:
        return page.eval_on_selector_all(
            f"#plbTabs [data-opt='{name}'] > span",
            "spans => spans.map(s => s.textContent.replace(/\\s+/g, ' ').trim())",
        )

    @staticmethod
    def _blocks(page, kind: str | None = None) -> list[str]:
        selector = "#plbWeek .plb-b" + (f"[data-kind='{kind}']" if kind else "")
        return page.eval_on_selector_all(
            selector, "bs => bs.map(b => b.dataset.code + ' ' + b.dataset.kind)"
        )

    def _cell(self, page, code: str) -> str:
        return self._text(page, f"#plbTable tbody[data-group='new'] tr[data-code='{code}'] .plb-sc")

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
        self.assertEqual(self._tab_names(page), ["B1", "B2", "B3", "(registered)"])
        self.assertEqual(page.get_attribute("#plbTabs", "role"), "tablist")
        self.assertEqual(page.get_attribute("#plbTab_B1", "aria-selected"), "true")
        self.assertEqual(page.get_attribute("#plbTab_B2", "aria-selected"), "false")
        self.assertEqual(page.get_attribute("#plbTabRegistered", "aria-selected"), "false")
        self.assertEqual(self._text(page, "#plbMoreBtn"), "More options (2)")
        self.assertEqual(page.get_attribute("#plbMoreBtn", "aria-expanded"), "false")
        self.assertTrue(page.locator("#plbMoreList").is_hidden())
        # "More options" sits between the option tabs and the Registered tab.
        order = page.eval_on_selector_all(
            "#plbTabs > *", "ns => ns.map(n => n.id || n.dataset.opt)"
        )
        self.assertEqual(
            order, ["plbTab_B1", "plbTab_B2", "plbTab_B3", "plbMore", "plbTabRegistered"]
        )

    def test_each_tab_says_its_three_lines_from_its_own_meetings(self) -> None:
        page, _ = self._built()
        self.assertEqual(
            self._tab_lines(page, "B1"),
            ["B1 · 15 cr", "4 days · Thu free", "ends 14:15 · gap 1 h 45 min"],
        )
        self.assertEqual(
            self._tab_lines(page, "B2"),
            ["B2 · 12 cr", "4 days · Thu free", "ends 14:00 · gap 45 min", "1 not scheduled"],
        )
        self.assertEqual(
            self._tab_lines(page, "B3"),
            ["B3 · 15 cr", "5 days · no free day", "ends 16:15 · gap 1 h"],
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
        self.assertEqual(items, ["C1", "C2"])
        page.click("#plbMoreList [data-more='C2']")
        self.assertEqual(self._tab_names(page), ["B1", "B2", "B3", "C2", "(registered)"])
        self.assertEqual(page.get_attribute("#plbTab_C2", "aria-selected"), "true")
        self.assertEqual(self._text(page, "#plbMoreBtn"), "More options (1)")
        self.assertTrue(page.locator("#plbMoreList").is_hidden())
        expect(page.locator("#plbTab_C2")).to_be_focused()
        self.assertEqual(self._cell(page, "AI331"), "§M2")
        self.assertEqual(self._cell(page, "AI342"), "§M1")
        self.assertEqual(
            self._tab_lines(page, "C2"),
            ["C2 · 15 cr", "5 days · no free day", "ends 14:00 · gap 3 h 30 min"],
        )
        # Escape closes the list and gives the focus back to the button.
        page.click("#plbMoreBtn")
        expect(page.locator("#plbMoreList")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#plbMoreList")).to_be_hidden()
        expect(page.locator("#plbMoreBtn")).to_be_focused()

    def test_arrow_keys_home_and_end_move_between_the_tabs(self) -> None:
        page, _ = self._built()
        page.focus("#plbTab_B1")
        page.keyboard.press("ArrowRight")
        expect(page.locator("#plbTab_B2")).to_be_focused()
        self.assertEqual(page.get_attribute("#plbTab_B2", "aria-selected"), "true")
        self.assertEqual(page.get_attribute("#plbTab_B2", "tabindex"), "0")
        self.assertEqual(page.get_attribute("#plbTab_B1", "tabindex"), "-1")
        page.keyboard.press("End")
        expect(page.locator("#plbTabRegistered")).to_be_focused()
        self.assertEqual(page.get_attribute("#plbTabRegistered", "aria-selected"), "true")
        page.keyboard.press("ArrowRight")  # wraps to the first
        expect(page.locator("#plbTab_B1")).to_be_focused()
        page.keyboard.press("ArrowLeft")  # and back to the last
        expect(page.locator("#plbTabRegistered")).to_be_focused()
        page.keyboard.press("Home")
        expect(page.locator("#plbTab_B1")).to_be_focused()
        self.assertEqual(page.get_attribute("#plbWeekPanel", "aria-labelledby"), "plbTab_B1")

    # ── The week and the courses table follow the tab ───────────────────────
    def test_the_week_follows_the_selected_tab(self) -> None:
        page, _ = self._built()
        # B1 is selected: six registered blocks and three planned ones.
        self.assertEqual(len(self._blocks(page, "reg")), 6)
        self.assertEqual(sorted(self._blocks(page, "new")), ["AI331 new", "AI331 new", "AI342 new"])
        self.assertEqual(self._text(page, "#plbWeekName"), "Option B1")
        self.assertEqual(self._text(page, "#plbLegend"), "Registered Planned")
        footers = page.eval_on_selector_all(
            "#plbWeek .plb-df", "fs => fs.map(f => f.innerText.replace(/\\s+/g, ' ').trim())"
        )
        self.assertEqual(footers[0], "3 classes ends 14:15")
        self.assertEqual(footers[4], "No classes day off")
        page.click("#plbTab_B3")
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
        self.assertEqual(self._cell(page, "AI331"), "§M2")
        self.assertEqual(self._cell(page, "AI342"), "§M1")
        page.click("#plbTab_B2")
        self.assertEqual(self._cell(page, "AI331"), "§M1")
        self.assertEqual(self._cell(page, "AI342"), "not scheduled in this option")
        self.assertEqual(self._tab_lines(page, "B2")[-1], "1 not scheduled")
        # Registered courses keep their cells; the Registered tab reads as in part one.
        self.assertEqual(
            self._text(page, "tbody[data-group='registered'] tr[data-code='CS301'] .plb-sc"), "§M1"
        )
        page.click("#plbTabRegistered")
        self.assertEqual(self._cell(page, "AI331"), "any section")
        self.assertEqual(self._cell(page, "AI342"), "any section")

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
        self.assertEqual(self._text(page, "#plbApplyTitle"), "Review and apply option B1")
        rows = page.eval_on_selector_all(
            "#plbApplyList li", "ls => ls.map(l => l.textContent.replace(/\\s+/g, ' ').trim())"
        )
        self.assertEqual(len(rows), 2)
        self.assertIn("AI331 §M2 Machine Learning", rows[0])
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
        expect(page.locator("#plbBuildLine")).to_have_text("Saved")
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
        expect(page.locator("#plbBuildLine")).to_contain_text("Saved. 1 previously saved planned")
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
        self.assertEqual(self._tab_names(page), ["B1", "B2", "B3", "(registered)"])
        # Other refusals read the way the current page reads them.
        fake.save_answer = lambda body: (500, {"ok": False, "error": "plain text refusal"})
        page.click("#plbReview")
        page.click("#plbApplyGo")
        expect(page.locator("#plbApplyErr")).to_have_text("plain text refusal")
        fake.save_answer = lambda body: (403, {"detail": "CSRF"})
        page.click("#plbApplyGo")
        expect(page.locator("#plbApplyErr")).to_contain_text("session has expired")
        self.assertEqual(len(fake.save_bodies), 3)

    def test_apply_is_available_only_on_an_option_tab(self) -> None:
        page, _ = self._built()
        self.assertTrue(page.locator("#plbReview").is_enabled())
        page.click("#plbTabRegistered")
        self.assertTrue(page.locator("#plbReview").is_disabled())
        page.click("#plbTab_B2")
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
        page.click("#plbTab_B2")
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
        self.assertEqual(self._cell(page, "AI331"), "any section")
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
        # And the page recovers by itself.
        fake.build_raw = None
        fake.build_answer = lambda body: (200, FIVE)
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text("Rebuild")
        self.assertEqual(self._text(page, "#plbBuildLine"), "")

    # ── A build goes out of date ─────────────────────────────────────────────
    def test_changing_the_plan_or_the_rules_removes_the_options(self) -> None:
        for change in ("add", "remove", "must", "rule", "student"):
            with self.subTest(change=change):
                page, _ = self._built()
                self.assertEqual(self._tab_names(page)[0], "B1")
                if change == "add":
                    page.click("#plbTable button[data-act='add'][data-code='CS351']")
                elif change == "remove":
                    page.click("#plbTable button[data-act='remove'][data-code='AI342']")
                elif change == "must":
                    page.check("#plbTable input[data-act='must'][data-code='AI331']")
                elif change == "rule":
                    self._rules(page, full=True)
                else:
                    self._fetch(page, OTHER_ID)
                self._assert_options_gone(page)
                self.assertEqual(self._cell(page, "AI331"), "any section")
                page.context.close()

    def test_applying_the_rules_dialog_with_nothing_changed_keeps_the_options(self) -> None:
        page, _ = self._built()
        page.click("#plbEditRules")
        page.click("#plbRulesApply")
        expect(page.locator("#plbRulesDialog")).not_to_be_visible()
        self.assertEqual(self._tab_names(page), ["B1", "B2", "B3", "(registered)"])
        self.assertTrue(page.locator("#plbReview").is_enabled())

    def test_a_rebuild_replaces_the_options_and_selects_the_first(self) -> None:
        fake = BuildFake()
        page, _ = self._built(fake)
        page.click("#plbTab_B3")
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
        self.assertEqual(
            self._tab_lines(page, "B2"),
            [
                "B2 · 12 ساعة",
                "4 أيام · الخميس فارغ",
                "تنتهي 14:00 · فجوة 45 د",
                "مقرر واحد غير مجدول",
            ],
        )
        self.assertEqual(self._text(page, "#plbWeekName"), "الخيار B1")
        self.assertEqual(self._text(page, "#plbLegend"), "المسجّل المخطَّط")
        page.click("#plbTab_B2")
        self.assertEqual(self._cell(page, "AI342"), "غير مجدول في هذا الخيار")
        # The keys mirror: in a right-to-left page the left arrow goes to the next tab.
        page.focus("#plbTab_B2")
        page.keyboard.press("ArrowLeft")
        expect(page.locator("#plbTab_B3")).to_be_focused()
        # Tabs run from the right edge, and the dialog speaks Arabic.
        first = page.locator("#plbTab_B1").bounding_box()
        last = page.locator("#plbTabRegistered").bounding_box()
        self.assertGreater(first["x"], last["x"])
        page.click("#plbReview")
        self.assertEqual(self._text(page, "#plbApplyTitle"), "مراجعة وتطبيق الخيار B3")
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
                    page.click("#plbTab_B2")
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
        page.focus("#plbTab_B2")
        self.assertNotEqual(
            page.evaluate("getComputedStyle(document.getElementById('plbTab_B2')).outlineStyle"),
            "none",
        )

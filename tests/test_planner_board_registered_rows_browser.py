"""The registered courses are rows of Your plan, and the first change makes them editable (S7).

The owner (2026-11-28): "When the registered courses load they are locked (Keep). I might need to
change one course only; today I have to clear the whole list, add all his courses and sections
again, then change that one." Now each registered course is a row with a tick, its section, a
Required pin and ×; using any of them (not the tick) switches the board to Ignore with every
registered course as a Required row pinned to its section, and a course with no usable section as
a load-only row (counted, never placed). A notice bar offers Back to Keep. The server side:
tests/test_planner_build_ignore_held.py. Answered by the board fakes (tests/planner_board_support.py).
"""

from __future__ import annotations

from tests.planner_board_support import (
    BASELINE,
    OVERFLOWS,
    SECTIONS_CATALOG,
    STUDENT_ID,
    BuildFake,
    PlannerBoardCase,
    _mapping,
    _option,
    _row,
    _section,
    build_reply,
    context_for,
    expect,
)

# The registered week of the board fakes (CS301 M1, CS322 M2, CS330 M3; 9 cr), a course the
# student studies with no section on file (LO101, 4 cr: load only) and an elective slot its mapped
# elective carries at 0 credits (FE1: no row at all).
LOAD_ONLY = {
    **_row("LO101", "Professional Ethics", "", 4, "", "", "", None),
    "source": "fallback_studying",
}
SLOT = {**_row("FE1", "Free Elective", "", 0, "", "", "", None), "source": "fallback_studying"}
REG_BASELINE = [*BASELINE, LOAD_ONLY, SLOT]
ROWS = ["CS301", "CS322", "CS330", "LO101"]

# CS301 M1 (tsid 1, the student's own) is full; CS322 M5 meets when CS301 M1 does.
CATALOG = {
    "ok": True,
    "program_sections_only": True,
    "sections": [
        _section(1, "CS301", "M1", ["SUN", "TUE"], "08:00", "09:15", 40, 40),
        _section(31, "CS301", "M2", ["MON", "WED"], "08:00", "09:15", 10, 40),
        _section(2, "CS322", "M2", ["SUN", "TUE"], "10:00", "11:15", 20, 40),
        _section(32, "CS322", "M5", ["SUN", "TUE"], "08:00", "09:15", 20, 40),
        _section(3, "CS330", "M3", ["MON", "WED"], "12:45", "14:00", 20, 40),
        *SECTIONS_CATALOG["sections"],
    ],
}
CATALOG["count"] = len(CATALOG["sections"])

# An option of the edited plan: CS301 moved to M2, CS322 where it is, CS330 left out, AI331 added.
EDITED = _option(
    "A1",
    [
        _mapping("CS301", "M2", 31, ["MON", "WED"], "08:00", "09:15"),
        _mapping("CS322", "M2", 2, ["SUN", "TUE"], "10:00", "11:15"),
        _mapping("AI331", "M1", 11, ["SUN", "TUE"], "11:30", "12:45"),
    ],
    unscheduled=[{"course_code": "CS330", "reason_code": "ALL_SECTIONS_CLASH"}],
)

# The Arabic the board must say, by code point.
AR = {
    "lead": "أصبحت المقررات المسجّلة قابلة للتعديل (تجاهل).",
    "bar": "المقررات المسجّلة قابلة للتعديل الآن",
    "back": "العودة إلى الإبقاء",
    "changes": "التغييرات:",
    "dropped": "أُزيل",
    "not_placed": "لم يوضع",
    "registered": "مسجّل",
}


class RegFake(BuildFake):
    """The board fakes with a registered week that has a load-only course, and its sections."""

    def __init__(self, baseline=REG_BASELINE, options=(EDITED,)) -> None:
        super().__init__()
        self.baseline = baseline
        self.catalog_payload = CATALOG
        self.build_answer = lambda body: (200, build_reply(list(options)))

    def _context(self, body):
        payload = context_for(str(body["student_id"]), eligibility_mode=body["eligibility_mode"])
        payload["baseline"] = list(self.baseline)
        return 200, payload

    def catalog(self, route) -> None:
        """Only the courses the request names, as the server answers (a held route is answered
        later the same way)."""
        body = route.request.post_data_json
        self.catalog_bodies.append(body)
        wanted = set(body.get("course_codes") or [])
        reply = {
            **CATALOG,
            "sections": [s for s in CATALOG["sections"] if s["course_key"] in wanted],
        }
        reply["count"] = len(reply["sections"])
        if self.hold_catalog:
            self.hold_catalog -= 1
            self.pending_catalog.append((route, reply))
            return
        route.fulfill(json=reply)


class RegisteredRowsCase(PlannerBoardCase):
    def _reg(self, fake=None, plan=False, **kwargs):
        """The board with this student, Your plan holding the registered rows (nothing added)."""
        page = self._open(fake or RegFake(), plan=plan, **kwargs)
        expect(page.locator("#plbPlanList .plb-prow--kept")).to_have_count(4)
        return page

    @staticmethod
    def _mode(page) -> str:
        return page.evaluate("document.querySelector('input[name=plbRegIn]:checked').value")

    @staticmethod
    def _plan_rows(page) -> list[list[str]]:
        """Each row of Your plan: [code, what it is] (planned | keep | edit | load)."""
        return page.eval_on_selector_all(
            "#plbPlanList .plb-prow",
            "rows => rows.map(r => [r.dataset.code, r.dataset.reg || 'planned'])",
        )

    def _status(self, page) -> str:
        return self._text(page, "#plbStatus")

    def _ctl(self, page, act: str, code: str):
        self._plan_panel(page)
        return page.locator(f"#plbPlanList [data-act='{act}'][data-code='{code}']")

    @staticmethod
    def _wait(page, predicate) -> None:
        for _ in range(250):
            if predicate():
                return
            page.wait_for_timeout(20)
        raise AssertionError("timed out")

    @staticmethod
    def _built_now(page) -> None:
        """After Build: the reply is in and the button is free again."""
        expect(page.locator("#plbTabs [data-opt='A1']")).to_be_visible()
        expect(page.locator("#plbBuild")).to_be_enabled()


class TestKeepRows(RegisteredRowsCase):
    def test_keep_lists_each_registered_course_as_a_row_with_its_controls(self) -> None:
        page = self._reg()
        self.assertEqual(self._mode(page), "keep")
        self.assertEqual(self._plan_rows(page), [[c, "keep"] for c in ROWS])
        # No lock: a tick, the section "§M1" (named "§M1 · registered"), Required on, × and the
        # "Registered" tag.
        row = "#plbPlanList .plb-prow[data-code='CS301']"
        self.assertEqual(page.locator(f"{row} .plb-lock, {row} svg rect").count(), 0)
        self.assertEqual(self._text(page, f"{row} [data-act='detail']"), "§M1")
        self.assertEqual(
            page.get_attribute(f"{row} [data-act='detail']", "aria-label"),
            "CS301: §M1 · registered",
        )
        self.assertEqual(page.get_attribute(f"{row} [data-act='must']", "aria-pressed"), "true")
        expect(page.locator(f"{row} [data-act='remove']")).to_be_visible()
        expect(page.locator(f"{row} [data-act='ptick']")).to_be_visible()
        self.assertEqual(self._text(page, f"{row} .plb-reg-tag"), "Registered")
        # Load only: counted (4 cr), no section control, no Required; never "not in this build".
        load = "#plbPlanList .plb-prow[data-code='LO101']"
        self.assertEqual(
            page.locator(f"{load} [data-act='detail'], {load} [data-act='must']").count(), 0
        )
        self.assertEqual(
            self._text(page, f"{load} .plb-sl"), "4 cr Registered counted, no timetable"
        )
        # The elective slot its mapped elective carries is not a row.
        self.assertEqual(page.locator("#plbPlanList [data-code='FE1']").count(), 0)
        self.assertEqual(self._text(page, "#plbPlanCount"), "Your plan · 4 registered + 0 added")
        self.assertEqual(self._load(page), ("13 / 18 cr", "0 planned + 13 registered · 5 cr free"))

    def test_a_row_held_only_as_a_forecast_is_expected_not_registered(self) -> None:
        forecast = [{**row, "source": "registration_plan_1448_t1"} for row in BASELINE]
        page = self._open(RegFake(baseline=forecast), plan=False)
        expect(page.locator("#plbPlanList .plb-prow--kept")).to_have_count(3)
        self.assertEqual(
            self._text(page, "#plbPlanList [data-code='CS301'] .plb-reg-tag"), "Expected"
        )
        self.assertEqual(
            page.get_attribute("#plbPlanList [data-act='detail'][data-code='CS301']", "aria-label"),
            "CS301: §M1 · expected",
        )

    def test_a_tick_alone_only_selects(self) -> None:
        page = self._reg()
        page.check("#plbPlanList [data-act='ptick'][data-code='CS330']")
        self.assertEqual(self._mode(page), "keep")
        expect(page.locator("#plbXRem")).to_be_enabled()
        self.assertEqual(self._plan_rows(page), [[c, "keep"] for c in ROWS])

    def test_a_load_above_the_limit_with_nothing_planned_says_so(self) -> None:
        # S6 left "20 / 18 cr" with no mark: the registered courses alone are over the limit.
        heavy = [*BASELINE, {**LOAD_ONLY, "credits": 11}]
        for language, line in (
            (
                "en",
                "0 planned + 20 registered · registered courses are 2 cr over the limit: only a"
                " swap for the same credits fits",
            ),
            (
                "ar",
                "0 مخطَّطة + 20 مسجّلة"
                " · المسجّل يزيد"
                " على الحد: ساعتان"
                "، لا يتسع إلا"
                " لاستبدال مقرر"
                " بآخر بالساعات"
                " نفسها",
            ),
        ):
            with self.subTest(language=language):
                page = self._open(RegFake(baseline=heavy), plan=False, language=language)
                expect(page.locator("#plbPlanList .plb-prow--kept")).to_have_count(4)
                self.assertEqual(self._text(page, "#plbMeterLine"), line)
                self.assertEqual(page.get_attribute("#plbPlanLoad", "data-over"), "true")
                self.assertEqual(page.get_attribute("#plbMeterLine", "data-over"), "true")
                page.context.close()


class TestConversion(RegisteredRowsCase):
    def _converted(self, page, trigger: str) -> None:
        self.assertEqual(self._mode(page), "ignore", trigger)
        expect(page.locator("#plbConvText")).to_have_text("Registered courses are editable now")

    def test_the_section_control_opens_the_menu_of_an_editable_row(self) -> None:
        page = self._reg()
        self._ctl(page, "detail", "CS301").click()
        self._converted(page, "section")
        expect(page.locator("#plbPop")).to_be_visible()
        expect(page.locator("#plbDetailTitle")).to_be_focused()
        self.assertEqual(
            self._plan_rows(page),
            [["CS301", "edit"], ["CS322", "edit"], ["CS330", "edit"], ["LO101", "load"]],
        )
        self.assertEqual(
            self._status(page),
            "Registered courses are now editable (Ignore). Choose a section for CS301.",
        )
        # Escape closes the menu and leaves the plan editable (Back to Keep is one click away).
        page.keyboard.press("Escape")
        expect(page.locator("#plbPop")).to_be_hidden()
        self.assertEqual(self._mode(page), "ignore")
        expect(page.locator("#plbBackKeep")).to_have_text("Back to Keep")

    def test_required_off_converts_and_keeps_the_others_required_and_pinned(self) -> None:
        fake = RegFake()
        page = self._reg(fake)
        self._ctl(page, "must", "CS330").click()
        self._converted(page, "must")
        expect(self._ctl(page, "must", "CS330")).to_be_focused()
        self.assertEqual(
            self._status(page),
            "Registered courses are now editable (Ignore). CS330 is no longer required.",
        )
        pressed = {
            code: page.get_attribute(
                f"#plbPlanList [data-act='must'][data-code='{code}']", "aria-pressed"
            )
            for code in ("CS301", "CS322", "CS330")
        }
        self.assertEqual(pressed, {"CS301": "true", "CS322": "true", "CS330": "false"})
        self.assertEqual(
            self._text(page, "#plbPlanList [data-code='CS301'] .plb-reg-tag"), "Registered §M1"
        )
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text("Rebuild options")
        body = fake.build_bodies[-1]
        self.assertEqual(body["mode"], "ignore")
        self.assertEqual(body["registered_load_only"], ["LO101"])
        rows = {item["course_code"]: item for item in body["shortlist"]}
        self.assertEqual(sorted(rows), ["CS301", "CS322", "CS330"])
        for code, tsid, section, must in (
            ("CS301", 1, "M1", True),
            ("CS322", 2, "M2", True),
            ("CS330", 3, "M3", False),
        ):
            self.assertEqual(rows[code]["must_take"], must, code)
            self.assertEqual(
                rows[code]["pinned_sections"], [{"term_section_id": tsid, "section": section}], code
            )
            self.assertEqual(rows[code]["credits"], 3, code)

    def test_remove_converts_and_removes(self) -> None:
        page = self._reg()
        self._ctl(page, "remove", "CS330").click()
        self._converted(page, "remove")
        self.assertEqual(
            self._status(page),
            "Registered courses are now editable (Ignore). Removed CS330 from the plan.",
        )
        self.assertEqual([c for c, _ in self._plan_rows(page)], ["CS301", "CS322", "LO101"])
        # Removed, it can be planned again: the keyboard is on its tick in Available.
        expect(page.locator("#plbAvailList [data-act='tick'][data-code='CS330']")).to_be_focused()
        expect(page.locator("#plbBackKeep")).to_have_text("Back to Keep (drops 1 change)")

    def test_the_arrow_with_registered_rows_ticked_converts_and_removes_them(self) -> None:
        page = self._reg()
        page.check("#plbPlanList [data-act='ptick'][data-code='CS301']")
        page.check("#plbPlanList [data-act='ptick'][data-code='LO101']")
        page.click("#plbXRem")
        self._converted(page, "remove ticked")
        self.assertEqual([c for c, _ in self._plan_rows(page)], ["CS322", "CS330"])
        self.assertEqual(
            self._status(page),
            "Registered courses are now editable (Ignore). Removed 2 courses from the plan.",
        )
        expect(page.locator("#plbBackKeep")).to_have_text("Back to Keep (drops 2 changes)")

    def test_remove_all_in_keep_starts_from_scratch_and_back_to_keep_restores_the_registration(
        self,
    ) -> None:
        page = self._reg(plan=True)
        page.click("#plbXRemAll")
        self._converted(page, "remove all")
        self.assertEqual(self._plan_rows(page), [])
        self.assertEqual(
            self._status(page),
            "Registered courses are now editable (Ignore). Your plan is empty: start from scratch.",
        )
        page.click("#plbBackKeep")
        expect(page.locator("#plbConvAsk")).to_be_visible()
        expect(page.locator("#plbConvGo")).to_be_focused()
        self.assertEqual(
            self._text(page, "#plbConvAskText"),
            "Back to Keep drops 4 changes to the registered courses. The courses you added stay.",
        )
        page.click("#plbConvGo")
        self.assertEqual(self._mode(page), "keep")
        self.assertEqual(self._plan_rows(page), [[c, "keep"] for c in ROWS])
        expect(page.locator("#plbRegKeep")).to_be_focused()

    def test_ignore_for_a_student_with_no_registered_course_says_nothing_about_them(self) -> None:
        page = self._open(RegFake(baseline=[]), plan=False)
        expect(page.locator("#plbPlanList .plb-prow--kept")).to_have_count(0)
        page.click("#plbRegIgnore")
        self.assertEqual(self._mode(page), "ignore")
        self.assertNotIn("editable", self._status(page))
        self.assertNotIn("Change any of them", self._status(page))
        expect(page.locator("#plbConvText")).to_have_text("")

    def test_a_course_held_at_two_sections_becomes_one_row(self) -> None:
        # None in the data today; the build reads such a course as one course, so the plan does.
        two = [
            *REG_BASELINE,
            _row("CS301", "Algorithms", "M2", 3, "MON", "08:00", "09:15", 31),
            _row("CS301", "Algorithms", "M2", 3, "WED", "08:00", "09:15", 31),
        ]
        page = self._open(RegFake(baseline=two), plan=False)
        expect(page.locator("#plbPlanList .plb-prow--kept")).to_have_count(5)
        page.click("#plbRegIgnore")
        self.assertEqual(
            self._plan_rows(page),
            [["CS301", "edit"], ["CS322", "edit"], ["CS330", "edit"], ["LO101", "load"]],
        )
        expect(page.locator("#plbBackKeep")).to_have_text("Back to Keep")  # nothing changed

    def test_conversion_in_arabic(self) -> None:
        page = self._reg(language="ar")
        self._ctl(page, "remove", "CS330").click()
        self.assertEqual(self._mode(page), "ignore")
        self.assertTrue(self._status(page).startswith(AR["lead"]), self._status(page))
        expect(page.locator("#plbConvText")).to_have_text(AR["bar"])
        self.assertTrue(self._text(page, "#plbBackKeep").startswith(AR["back"] + " ("))
        self.assertEqual(
            self._text(page, "#plbPlanList [data-code='CS301'] .plb-reg-tag"),
            f"{AR['registered']} §M1",
        )


class TestBackToKeep(RegisteredRowsCase):
    def test_back_to_keep_with_no_change_is_one_click_and_exact(self) -> None:
        page = self._reg(plan=True)
        before = (self._plan_rows(page), self._load(page), self._text(page, "#plbPlanCount"))
        page.click("#plbRegIgnore")
        self.assertEqual(self._mode(page), "ignore")
        self.assertEqual(
            self._status(page),
            "Registered courses are now editable (Ignore). Change any of them, or add courses.",
        )
        page.click("#plbBackKeep")
        expect(page.locator("#plbConvAsk")).to_be_hidden()
        self.assertEqual(self._mode(page), "keep")
        self.assertEqual(
            (self._plan_rows(page), self._load(page), self._text(page, "#plbPlanCount")), before
        )
        expect(page.locator("#plbRegKeep")).to_be_focused()
        self.assertEqual(self._status(page), "The registered courses are kept as registered again.")

    def test_back_to_keep_asks_first_when_a_row_changed_and_keeps_the_added_courses(self) -> None:
        page = self._reg()
        self._ctl(page, "must", "CS322").click()
        self._add(page, "STAT305")
        page.click("#plbBackKeep")
        expect(page.locator("#plbConvGo")).to_be_focused()
        page.click("#plbConvKeep")  # Keep editing: nothing changes, the keyboard goes back
        expect(page.locator("#plbConvAsk")).to_be_hidden()
        expect(page.locator("#plbBackKeep")).to_be_focused()
        self.assertEqual(self._mode(page), "ignore")
        page.click("#plbBackKeep")
        page.click("#plbConvGo")
        self.assertEqual(self._mode(page), "keep")
        self.assertEqual(
            self._plan_rows(page), [["STAT305", "planned"], *[[c, "keep"] for c in ROWS]]
        )

    def test_the_keep_switch_is_back_to_keep_with_the_same_confirm(self) -> None:
        page = self._reg()
        page.click("#plbRegIgnore")
        page.click("#plbRegKeep")  # nothing changed: straight back
        self.assertEqual(self._mode(page), "keep")
        page.click("#plbRegIgnore")
        self._ctl(page, "remove", "LO101").click()
        page.click("#plbRegKeep")
        expect(page.locator("#plbEligConfirm")).to_be_visible()
        self.assertEqual(self._mode(page), "ignore")  # the switch shows the rule in force
        self.assertEqual(
            self._text(page, "#plbEligConfirmText"),
            "Back to Keep drops 1 change to the registered courses. The courses you added stay.",
        )
        self.assertEqual(
            (self._text(page, "#plbEligGo"), self._text(page, "#plbEligKeep")),
            ("Drop the changes", "Keep editing"),
        )
        page.keyboard.press("Escape")
        expect(page.locator("#plbRegIgnore")).to_be_focused()
        page.click("#plbRegKeep")
        page.click("#plbEligGo")
        self.assertEqual(self._mode(page), "keep")
        self.assertEqual(self._plan_rows(page), [[c, "keep"] for c in ROWS])
        expect(page.locator("#plbRegKeep")).to_be_focused()

    def test_one_back_to_keep_question_at_a_time(self) -> None:
        page = self._reg()
        self._ctl(page, "remove", "LO101").click()
        page.click("#plbBackKeep")
        expect(page.locator("#plbConvAsk")).to_be_visible()
        page.click("#plbRegKeep")  # the switch asks the same: the bar's question goes
        expect(page.locator("#plbEligConfirm")).to_be_visible()
        expect(page.locator("#plbConvAsk")).to_be_hidden()
        page.click("#plbBackKeep")  # and the other way round
        expect(page.locator("#plbConvAsk")).to_be_visible()
        expect(page.locator("#plbEligConfirm")).to_be_hidden()
        page.keyboard.press("Escape")  # one Escape closes the one question
        expect(page.locator("#plbConvAsk")).to_be_hidden()
        expect(page.locator("#plbEligConfirm")).to_be_hidden()
        self.assertEqual(self._mode(page), "ignore")

    def test_the_confirm_says_what_each_question_does(self) -> None:
        page = self._reg()
        # Keep with a course added: the prerequisite question as before (the template's words).
        self._add(page, "STAT305")
        page.click("#plbEligRelaxed")
        self.assertEqual(
            self._text(page, "#plbEligConfirmText"),
            "Changing the prerequisite rule reloads the recommended courses and clears the courses"
            " you added to Your plan.",
        )
        page.click("#plbEligKeep")
        self._remove(page, "STAT305")
        # The prerequisite switch over an edited plan.
        self._ctl(page, "must", "CS301").click()
        page.click("#plbEligRelaxed")
        self.assertEqual(
            self._text(page, "#plbEligConfirmText"),
            "Changing the prerequisite rule reloads the recommended courses, clears the courses you"
            " added and keeps the registered courses as registered again.",
        )
        self.assertEqual(
            (self._text(page, "#plbEligGo"), self._text(page, "#plbEligKeep")),
            ("Change and clear", "Keep current"),
        )
        page.click("#plbEligGo")  # the reload ends in Keep
        expect(page.locator("#plbEligRelaxed")).to_be_checked()
        expect(page.locator("#plbPlanList .plb-prow--kept")).to_have_count(4)
        self.assertEqual(self._mode(page), "keep")
        # Reset over an edited plan under another prerequisite rule: both questions in one.
        self._ctl(page, "must", "CS301").click()
        page.click("#plbRulesReset")
        self.assertEqual(
            self._text(page, "#plbEligConfirmText"),
            "Reset puts back the usual rules: the recommended courses reload, the courses you added are"
            " cleared and 1 change to the registered courses is dropped.",
        )
        self.assertEqual(
            (self._text(page, "#plbEligGo"), self._text(page, "#plbEligKeep")),
            ("Reset and clear", "Keep current"),
        )
        page.click("#plbEligGo")
        expect(page.locator("#plbEligStrict")).to_be_checked()
        expect(page.locator("#plbPlanList .plb-prow--kept")).to_have_count(4)
        self.assertEqual(self._mode(page), "keep")
        # Reset over an edited plan under the usual prerequisite rule: Back to Keep's question.
        self._ctl(page, "remove", "CS301").click()
        page.click("#plbRulesReset")
        self.assertEqual(
            self._text(page, "#plbEligConfirmText"),
            "Back to Keep drops 1 change to the registered courses. The courses you added stay.",
        )
        page.click("#plbEligGo")
        self.assertEqual(self._mode(page), "keep")
        self.assertEqual(self._plan_rows(page), [[c, "keep"] for c in ROWS])

    def test_the_confirm_in_arabic(self) -> None:
        page = self._reg(language="ar")
        self._ctl(page, "remove", "CS301").click()
        page.click("#plbRegKeep")
        self.assertEqual(
            self._text(page, "#plbEligConfirmText"),
            f"{AR['back']} \u062a\u0644\u063a\u064a \u062a\u063a\u064a\u064a\u0631"
            " \u0648\u0627\u062d\u062f \u0641\u064a \u0627\u0644\u0645\u0642\u0631\u0631\u0627\u062a"
            " \u0627\u0644\u0645\u0633\u062c\u0651\u0644\u0629\u060c \u0648\u062a\u0628\u0642\u0649"
            " \u0627\u0644\u0645\u0642\u0631\u0631\u0627\u062a \u0627\u0644\u062a\u064a"
            " \u0623\u0636\u0641\u062a\u0647\u0627.",
        )
        self.assertEqual(
            self._text(page, "#plbEligGo"),
            "\u0625\u0644\u063a\u0627\u0621 \u0627\u0644\u062a\u063a\u064a\u064a\u0631\u0627\u062a",
        )

    def test_fetch_and_a_reload_of_the_same_student_start_in_keep(self) -> None:
        page = self._reg()
        self._ctl(page, "remove", "CS330").click()
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbPlanList .plb-prow--kept")).to_have_count(4)
        self.assertEqual(self._mode(page), "keep")
        expect(page.locator("#plbConvText")).to_have_text("")
        # An edited plan with nothing changed reloads by the prerequisite switch, also into Keep.
        page.click("#plbRegIgnore")
        page.click("#plbEligRelaxed")
        expect(page.locator("#plbEligRelaxed")).to_be_checked()
        expect(page.locator("#plbPlanList .plb-prow--kept")).to_have_count(4)
        self.assertEqual(self._mode(page), "keep")

    def test_ignore_chosen_before_a_student_does_not_ride_along(self) -> None:
        page = self._open(RegFake(), load=False)
        page.click("#plbRegIgnore")
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbPlanList .plb-prow--kept")).to_have_count(4)
        self.assertEqual(self._mode(page), "keep")


class TestSectionMenu(RegisteredRowsCase):
    def test_the_menu_waits_for_its_sections_and_the_own_one_is_never_full(self) -> None:
        fake = RegFake()
        page = self._reg(fake)
        fake.hold_catalog = 1  # the reload that conversion makes
        self._ctl(page, "detail", "CS301").click()
        expect(page.locator("#plbPop .plb-dt-hint").first).to_have_text("Loading the sections…")
        expect(page.locator("#plbPop")).not_to_contain_text("No sections this term")
        # While the list is on its way no section is listed, and none is said to be gone.
        self.assertEqual(page.locator("#plbPop [data-tsid]").count(), 0)
        self._wait(page, lambda: fake.pending_catalog)
        body = fake.catalog_bodies[-1]
        self.assertTrue(body.get("include_registered"), body)
        self.assertIn("CS301", body["course_codes"])
        route, reply = fake.pending_catalog.pop()
        route.fulfill(json=reply)
        own = page.locator("#plbPop .plb-dt-sec[data-tsid='1']")
        expect(own).to_be_visible()
        self.assertIsNone(own.get_attribute("data-unusable"))  # full, and his: usable
        self.assertEqual(own.get_attribute("data-own"), "true")
        self.assertIn(
            "Registered section",
            self._text(page, "#plbPop .plb-dt-sec[data-tsid='1'] .plb-dt-flags"),
        )
        self.assertNotIn(
            "Full", self._text(page, "#plbPop .plb-dt-sec[data-tsid='1'] .plb-dt-flags")
        )
        expect(page.locator("#plbPop input[name='plbOnly'][value='1']")).to_be_checked()
        # Its seats are a count, never "Full", for the student who holds one of them.
        self.assertEqual(
            self._text(page, "#plbPop .plb-dt-sec[data-tsid='1'] .plb-dt-sec-seats"),
            "For AI · 40 of 40 taken",
        )
        # Another course's section that meets when a Required row is pinned says so.
        page.keyboard.press("Escape")
        self._ctl(page, "detail", "CS322").click()
        self.assertIn(
            "Clashes with CS301 §M1 (required)",
            self._text(page, "#plbPop .plb-dt-sec[data-tsid='32'] .plb-dt-flags"),
        )


class TestChanges(RegisteredRowsCase):
    def _edited_build(self, language="en", **kwargs):
        fake = RegFake()
        page = self._reg(fake, language=language, **kwargs)
        self._ctl(page, "must", "CS330").click()  # Required off: the build leaves it out
        self._ctl(page, "remove", "LO101").click()  # a load-only course dropped
        self._add(page, "AI331")
        page.click("#plbBuild")
        self._built_now(page)
        return page, fake

    def test_each_option_says_what_it_changes_against_the_registration(self) -> None:
        page, fake = self._edited_build()
        self.assertEqual(fake.build_bodies[-1]["registered_load_only"], [])
        self.assertEqual(
            self._text(page, "#plbTabs [data-opt='A1'] .plb-tab-chg"),
            "Changes: CS301 §M1 › §M2 · CS330 not placed · LO101 dropped · +AI331",
        )
        # The section the student holds is marked in the week, in words too.
        own = page.locator("#plbWeek .plb-b[data-code='CS322']").first
        self.assertEqual(own.get_attribute("data-own"), "true")
        self.assertIn("registered section", own.get_attribute("aria-label"))
        self.assertIsNone(
            page.locator("#plbWeek .plb-b[data-code='CS301']").first.get_attribute("data-own")
        )
        # Not "not placed" for a load-only course; the option's load counts what is kept.
        self.assertEqual(self._text(page, "#plbApplyStatus"), "1 not placed: CS330")

    def test_a_registered_course_added_back_is_not_new(self) -> None:
        fake = RegFake()
        page = self._reg(fake)
        self._ctl(page, "remove", "CS322").click()
        self._add(page, "CS322")  # back from Available, any section
        self._add(page, "AI331")
        page.click("#plbBuild")
        self._built_now(page)
        self.assertEqual(
            self._text(page, "#plbTabs [data-opt='A1'] .plb-tab-chg"),
            "Changes: CS301 §M1 › §M2 · CS330 not placed · +AI331",
        )

    def test_changes_in_arabic(self) -> None:
        page, _fake = self._edited_build(language="ar")
        self.assertEqual(
            self._text(page, "#plbTabs [data-opt='A1'] .plb-tab-chg"),
            f"{AR['changes']} CS301 §M1 ‹ §M2 · CS330 {AR['not_placed']} · LO101 {AR['dropped']} · +AI331",
        )

    def test_load_only_hours_count_in_every_figure_and_it_is_never_not_placed(self) -> None:
        fake = RegFake()
        page = self._reg(fake)
        self._ctl(page, "must", "CS330").click()
        self.assertEqual(self._load(page), ("13 / 18 cr", "0 planned + 13 registered · 5 cr free"))
        self._add(page, "AI331")
        self.assertEqual(self._load(page), ("16 / 18 cr", "3 planned + 13 registered · 2 cr free"))
        page.click("#plbBuild")
        self._built_now(page)
        self.assertEqual(fake.build_bodies[-1]["registered_load_only"], ["LO101"])
        # A1: CS301 + CS322 + AI331 placed (9) and LO101 counted (4).
        self.assertEqual(self._text(page, "#plbFactLoad .plb-fv"), "13 of 18 cr")
        self.assertIn("LO101", self._text(page, "#plbSecs"))
        self.assertEqual(self._text(page, "#plbApplyStatus"), "1 not placed: CS330")
        self.assertIn("13 of 18 cr", self._text(page, "#plbApplySum"))


class TestRequiredRows(RegisteredRowsCase):
    def test_a_build_whose_required_rows_cannot_all_be_placed_says_which_and_what_to_do(
        self,
    ) -> None:
        fake = RegFake()
        failed = build_reply([])
        failed["summary"]["hard_constraint_failures"] = [
            {
                "kind": "must_take",
                "course_code": "CS301",
                "reason": "Must-take course could not be scheduled: x",
            },
            {
                "kind": "must_take",
                "course_code": "CS322",
                "reason": "Must-take course could not be scheduled: x",
            },
        ]
        fake.build_answer = lambda body: (200, failed)
        page = self._reg(fake)
        page.click("#plbRegIgnore")
        page.click("#plbBuild")
        expect(page.locator("#plbBuildNote")).to_be_visible()
        self.assertIn(
            "Required courses that cannot all be placed: CS301, CS322. Turn Required off on those"
            " the build may move.",
            self._text(page, "#plbBuildNote"),
        )

    def test_a_course_named_by_code_and_name_shows_its_code_and_keeps_its_identity(self) -> None:
        named = [
            {**row, "course_code": "CS112::PROGRAMMING_I", "course_key": "CS112::PROGRAMMING_I"}
            for row in BASELINE[:2]
        ]
        fake = RegFake(baseline=named)
        page = self._open(fake, plan=False)
        row = "#plbPlanList .plb-prow--kept[data-code='CS112::PROGRAMMING_I']"
        expect(page.locator(row)).to_have_count(1)
        self.assertEqual(self._text(page, f"{row} .plb-code"), "CS112")
        self._ctl(page, "must", "CS112::PROGRAMMING_I").click()
        self.assertEqual(
            self._status(page),
            "Registered courses are now editable (Ignore). CS112 is no longer required.",
        )
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_be_enabled()
        self.assertEqual(
            [item["course_code"] for item in fake.build_bodies[-1]["shortlist"]],
            ["CS112::PROGRAMMING_I"],
        )


class TestApply(RegisteredRowsCase):
    def test_apply_saves_the_edited_plan_and_the_board_stays_as_built(self) -> None:
        fake = RegFake()
        page = self._reg(fake)
        self._ctl(page, "remove", "CS330").click()
        self._add(page, "AI331")
        page.click("#plbBuild")
        self._built_now(page)
        page.click("#plbReview")
        self.assertEqual(
            self._text(page, "#plbApplyReg"),
            "This saves the plan as built. The registrar's timetable is unchanged. Changes: CS301 §M1"
            " › §M2 · CS330 dropped · +AI331",
        )
        page.click("#plbApplyGo")
        expect(page.locator("#plbApplyDialog")).to_be_hidden()
        body = fake.save_bodies[-1]
        self.assertEqual(body["mode"], "ignore")
        self.assertEqual(body["dropped_registered"], ["CS330"])
        self.assertEqual(body["term_section_ids"], [31, 2, 11])
        expect(page.locator("#plbBuildLine")).to_contain_text("Changes: CS301 §M1 › §M2")
        self.assertEqual(self._mode(page), "ignore")
        self.assertEqual(
            self._plan_rows(page),
            [["CS301", "edit"], ["CS322", "edit"], ["AI331", "planned"], ["LO101", "load"]],
        )

    def test_apply_for_a_student_with_only_a_forecast_reads_the_saved_plan_back_in_keep(
        self,
    ) -> None:
        forecast = [{**row, "source": "registration_plan_1448_t1"} for row in BASELINE]
        fake = RegFake(baseline=forecast)
        page = self._open(fake, plan=False)
        expect(page.locator("#plbPlanList .plb-prow--kept")).to_have_count(3)
        self._ctl(page, "remove", "CS330").click()
        self._add(page, "AI331")
        page.click("#plbBuild")
        self._built_now(page)
        page.click("#plbReview")
        # The save is the student's timetable now: the next read gives the saved sections back.
        fake.baseline = [
            {**_row(code, name, section, 3, day, start, end, tsid), "source": "planner"}
            for code, name, section, days, start, end, tsid in (
                ("CS301", "Algorithms", "M2", ("MON", "WED"), "08:00", "09:15", 31),
                ("CS322", "Databases", "M2", ("SUN", "TUE"), "10:00", "11:15", 2),
                ("AI331", "Machine Learning", "M1", ("SUN", "TUE"), "11:30", "12:45", 11),
            )
            for day in days
        ]
        page.click("#plbApplyGo")
        expect(page.locator("#plbApplyDialog")).to_be_hidden()
        expect(page.locator("#plbPlanList .plb-prow--kept")).to_have_count(3)
        self.assertEqual(self._mode(page), "keep")
        self.assertEqual(
            self._plan_rows(page), [["CS301", "keep"], ["CS322", "keep"], ["AI331", "keep"]]
        )
        expect(page.locator("#plbConvText")).to_have_text("")


class TestWeekAndBar(RegisteredRowsCase):
    def test_the_notice_bar_is_a_live_region_that_is_always_there(self) -> None:
        page = self._reg()
        bar = page.locator("#plbConvText")
        self.assertEqual(
            (bar.get_attribute("role"), bar.get_attribute("aria-live")), ("status", "polite")
        )
        expect(bar).to_be_attached()
        expect(bar).to_have_text("")
        self.assertEqual(
            page.evaluate("getComputedStyle(document.getElementById('plbConvText')).display"),
            "block",
        )
        page.click("#plbRegIgnore")
        expect(bar).to_have_text("Registered courses are editable now")

    def test_the_registered_week_has_no_lock_in_ignore(self) -> None:
        page = self._reg()
        locks = "#plbWeek .plb-b[data-kind='reg'] svg"
        self.assertGreater(page.locator(locks).count(), 0)
        page.click("#plbRegIgnore")
        self.assertEqual(page.locator(locks).count(), 0)
        self.assertEqual(page.locator("#plbLegend svg").count(), 0)

    def test_the_elective_row_tag_follows_its_course(self) -> None:
        # As GS311 counts for FE1 in the owner's case: here CS330 likely counts for GSE1.
        fake = RegFake()
        base = fake._context

        def with_filled(body):
            status, payload = base(body)
            payload["recommendations"] = [
                *payload["recommendations"],
                {
                    "course_code": "GSE1",
                    "course_name": "University Elective",
                    "credits": 3,
                    "priority": "Low",
                    "score": 10,
                    "status": "Eligible",
                    "missing_prerequisites": [],
                    "reason_tags": [],
                },
            ]
            payload["elective_rows_filled"] = {
                "GSE1": {"course_code": "CS330", "basis": "same_hours"}
            }
            return status, payload

        fake.context_answer = with_filled
        page = self._reg(fake, source="recommended")
        tag = "#plbAvailList .plb-arow[data-code='GSE1'] .plb-flag--filled"
        expect(page.locator(tag)).to_have_text("CS330 (registered) likely counts for it")
        page.click("#plbRegIgnore")
        expect(page.locator(tag)).to_have_text("CS330 (registered) likely counts for it")
        self._ctl(page, "remove", "CS330").click()
        expect(page.locator(tag)).to_have_count(0)


class TestPhone(RegisteredRowsCase):
    def test_on_a_phone_the_rows_convert_and_nothing_spills(self) -> None:
        for language in ("en", "ar"):
            with self.subTest(language=language):
                page = self._reg(width=390, height=900, language=language)
                self.assertEqual(page.evaluate(OVERFLOWS), [])
                # The plan's tab counts its rows: the registered ones are rows too.
                self.assertEqual(page.inner_text("#plbPickTabPlan").split(" · ")[1], "4")
                self._ctl(page, "must", "CS322").click()
                self.assertEqual(self._mode(page), "ignore")
                expect(self._ctl(page, "must", "CS322")).to_be_focused()
                expect(page.locator("#plbConvBar")).to_be_visible()
                self.assertEqual(page.evaluate(OVERFLOWS), [])
                page.context.close()

    def test_a_long_tag_ends_in_an_ellipsis_inside_its_name_cell(self) -> None:
        long = "M1-EVENING-ONLINE-WOMEN"
        baseline = [
            {**row, "section": long} if row["course_code"] == "CS301" else row
            for row in REG_BASELINE
        ]
        for language in ("en", "ar"):
            with self.subTest(language=language):
                page = self._open(
                    RegFake(baseline=baseline), plan=False, width=390, height=900, language=language
                )
                expect(page.locator("#plbPlanList .plb-prow--kept")).to_have_count(4)
                page.click("#plbRegIgnore")
                self._plan_panel(page)
                tag = page.evaluate(
                    """() => {
                      const tag = document.querySelector(
                        "#plbPlanList .plb-prow[data-code='CS301'] .plb-sl > .plb-reg-tag");
                      const cell = tag.closest('.plb-pmain').getBoundingClientRect();
                      const r = tag.getBoundingClientRect();
                      return {
                        inside: r.left >= cell.left - 0.5 && r.right <= cell.right + 0.5,
                        cut: tag.scrollWidth > tag.clientWidth,
                      };
                    }"""
                )
                self.assertEqual(tag, {"inside": True, "cut": True})
                self.assertEqual(page.evaluate(OVERFLOWS), [])
                page.context.close()


class TestNameColumn(RegisteredRowsCase):
    EDGES = (
        "els => els.map(e => Math.round(document.documentElement.dir === 'rtl'"
        " ? e.getBoundingClientRect().right : e.getBoundingClientRect().left))"
    )

    def test_the_names_start_on_one_line_where_the_list_has_room(self) -> None:
        # Each row is its own grid: CS301 and STAT305 used to start their names apart.
        for width in (1366, 1440):
            for language in ("en", "ar"):
                with self.subTest(width=width, language=language):
                    page = self._reg(width=width, height=900, language=language)
                    self._add(page, "STAT305")
                    edges = page.eval_on_selector_all(
                        "#plbPlanList .plb-prow > .plb-pmain", self.EDGES
                    )
                    self.assertEqual(len(edges), 5)
                    self.assertEqual(len(set(edges)), 1, edges)
                    page.context.close()

    def test_a_narrow_list_keeps_each_rows_own_code_width(self) -> None:
        # At 1152 a name needs every pixel: the 5-letter codes do not take a 7-letter cell.
        for language in ("en", "ar"):
            with self.subTest(language=language):
                page = self._reg(width=1152, height=900, language=language)
                self._add(page, "STAT305")
                codes = page.eval_on_selector_all(
                    "#plbPlanList .plb-prow > .plb-code",
                    "els => Object.fromEntries(els.map(e => [e.closest('.plb-prow').dataset.code,"
                    " Math.round(e.getBoundingClientRect().width)]))",
                )
                self.assertLess(codes["CS301"], codes["STAT305"] - 10, codes)
                page.context.close()

"""The new planner screen, one screen: the student band, the current-timetable disclosure and the
three zones, in a real browser.

Page order: status, student band (four facts), "Current timetable" (closed), zone 01 Rules, zone 02
Courses (with the Build button), zone 03 Options (projection, guidance, tabs, week), the apply bar.
Answered by the same fakes as the other board tests (tests/planner_board_support.py).
"""

from __future__ import annotations

import copy

from tests.planner_board_support import (
    OTHER_ID,
    OVERFLOWS,
    READY,
    STUDENT_PLAN,
    BuildFake,
    PlannerBoardCase,
    _plan_course,
    expect,
)

ORDER = [
    "#plbStatus",
    ".plb-sband",
    "#plbCurTT",
    "#plbZoneRules",
    "#plbZoneCourses",
    "#plbZoneOptions",
    "#plbActs",
]
TOP = "(s) => document.querySelector(s).getBoundingClientRect().top"


def _plan_with_passed() -> dict:
    """The sample plan plus two passed courses: 6 of 30 credits."""
    plan = copy.deepcopy(STUDENT_PLAN)
    plan["terms"][0]["courses"] += [
        _plan_course("MATH101", "Calculus", "passed", False),
        _plan_course("PHY101", "Physics", "passed", False),
    ]
    return plan


class PlannerBoardZonesTests(PlannerBoardCase):
    # ── the order of the page ───────────────────────────────────────────────
    def test_the_page_is_status_band_disclosure_then_three_zones_then_the_apply_bar(self) -> None:
        for language, heads in (
            ("en", ["01 Rules", "02 Courses", "03 Options"]),
            ("ar", ["01 القواعد", "02 المقررات", "03 الخيارات"]),
        ):
            with self.subTest(language=language):
                page = self._open(BuildFake(), language=language)
                tops = [page.evaluate(TOP, selector) for selector in ORDER]
                self.assertEqual(tops, sorted(tops), tops)
                self.assertEqual(len(set(tops)), len(tops))
                got = page.eval_on_selector_all(
                    ".plb-zone > .plb-zh > h2",
                    "n => n.map(x => x.textContent.replace(/\\s+/g, ' ').trim())",
                )
                self.assertEqual(got, heads)
                # what lives in which zone
                self.assertEqual(page.locator("#plbZoneRules #plbMaxIn").count(), 1)
                self.assertEqual(page.locator("#plbZoneCourses #plbPick").count(), 1)
                self.assertEqual(page.locator("#plbZoneCourses #plbPop").count(), 1)
                self.assertEqual(page.locator("#plbZoneCourses #plbBuild").count(), 1)
                self.assertEqual(page.locator("#plbZoneCourses #plbBuildLine").count(), 1)
                for inside in (
                    "#plbGrad",
                    "#plbGuide",
                    "#plbStrip",
                    "#plbWhyBtn",
                    "#plbBuildNote",
                    "#plbWeekPanel",
                ):
                    self.assertEqual(page.locator(f"#plbZoneOptions {inside}").count(), 1, inside)
                self.assertEqual(page.locator("#plbActs #plbReview").count(), 1)
                self.assertEqual(page.locator("#plbActs #plbApplyWhy").count(), 1)
                self.assertEqual(page.locator("#plbActs .plb-acts-safe").count(), 1)
                page.context.close()

    # ── the current timetable ───────────────────────────────────────────────
    def test_the_current_timetable_is_closed_on_load_and_opens_to_the_registered_week(self) -> None:
        page = self._open(BuildFake(), load=False)
        expect(page.locator("#plbCurTT")).to_be_hidden()  # no student: nothing to show
        self._fetch(page, "4500000")
        expect(page.locator("#plbCurTT")).to_be_visible()
        self.assertFalse(page.evaluate("document.getElementById('plbCurTT').open"))
        expect(page.locator("#plbCurTTWeek .plb-b")).to_have_count(0)
        digest = self._text(page, "#plbCurTTDigest")
        # codes only, never meetings or times
        self.assertEqual(digest, "CS301 · CS322 · CS330")
        self.assertEqual(self._text(page, ".plb-curtt-t"), "Current timetable")
        expect(page.locator(".plb-curtt-show")).to_be_visible()
        expect(page.locator(".plb-curtt-hide")).to_be_hidden()
        page.click("#plbCurTT > summary")
        expect(page.locator("#plbCurTTWeek .plb-b")).to_have_count(6)
        self.assertEqual(page.locator("#plbCurTTWeek .plb-b[data-kind='reg']").count(), 6)
        # the same blocks as the Registered week below, drawn by the same grid
        self.assertEqual(page.locator("#plbWeek .plb-b").count(), 6)
        expect(page.locator(".plb-curtt-hide")).to_be_visible()
        expect(page.locator(".plb-curtt-show")).to_be_hidden()
        page.click("#plbCurTT > summary")
        expect(page.locator("#plbCurTTWeek .plb-b")).to_have_count(0)  # closed: nothing drawn
        page.context.close()

    def test_the_open_timetable_is_redrawn_for_the_next_student(self) -> None:
        fake = BuildFake()

        def answer(body):
            from tests.planner_board_support import _row, context_for

            payload = context_for(str(body["student_id"]))
            if str(body["student_id"]) == OTHER_ID:
                payload["baseline"] = [_row("AI499", "Thesis", "M1", 3, "THU", "09:00", "10:00", 9)]
            return 200, payload

        fake.context_answer = answer
        page = self._open(fake)
        page.click("#plbCurTT > summary")
        expect(page.locator("#plbCurTTWeek .plb-b")).to_have_count(6)
        self._fetch(page, OTHER_ID)
        expect(page.locator("#plbCurTTWeek .plb-b")).to_have_count(1)
        self.assertEqual(self._text(page, "#plbCurTTDigest"), "AI499")
        self.assertTrue(page.evaluate("document.getElementById('plbCurTT').open"))

    def test_the_current_timetable_says_so_in_arabic_and_without_timed_classes(self) -> None:
        page = self._open(BuildFake(), language="ar")
        self.assertEqual(self._text(page, ".plb-curtt-t"), "الجدول الحالي")
        self.assertEqual(self._text(page, "#plbCurTTDigest"), "CS301 · CS322 · CS330")
        self.assertEqual(page.locator("#plbCurTTDigest > bdi").count(), 3)
        expect(page.locator(".plb-curtt-show")).to_have_text("عرض")
        page.click("#plbCurTT > summary")
        expect(page.locator(".plb-curtt-hide")).to_have_text("إخفاء")
        expect(page.locator("#plbCurTTWeek .plb-b")).to_have_count(6)
        page.context.close()
        fake = BuildFake()

        def answer(body):
            from tests.planner_board_support import context_for

            payload = context_for(str(body["student_id"]))
            payload["baseline"] = []
            return 200, payload

        fake.context_answer = answer
        page = self._open(fake)
        self.assertEqual(self._text(page, "#plbCurTTDigest"), "No timed classes")
        page.click("#plbCurTT > summary")
        expect(page.locator("#plbCurTTWeek")).to_contain_text("No registered classes")

    # ── the four facts ──────────────────────────────────────────────────────
    def test_the_four_facts_of_the_band(self) -> None:
        fake = BuildFake()
        fake.plan_payload = _plan_with_passed()
        fake.projection_answer = lambda body: (200, READY)
        page = self._open(fake)
        facts = page.eval_on_selector_all(
            "#plbStudent .plb-sf", "n => n.map(x => x.innerText.replace(/\\s+/g, ' ').trim())"
        )
        self.assertEqual(len(facts), 4)
        self.assertTrue(facts[0].startswith("PROGRAMME AI"), facts[0])
        self.assertIn("Sample Student · 4500000", facts[0])
        # plan credits passed over the plan's, and the GPA as the small line under it
        expect(page.locator("#plbStudent .plb-sf:nth-child(2) .plb-big")).to_have_text("6 / 30 cr")
        self.assertIn("GPA 3.42", self._text(page, "#plbStudent .plb-sf:nth-child(2)"))
        self.assertEqual(
            page.eval_on_selector("#plbStudent .plb-bar > i", "n => n.style.inlineSize"), "20%"
        )
        # this term's credits and the number of courses; the codes are NOT repeated here
        # (the owner: they are on the Current timetable line just below)
        self.assertEqual("registered now 9 3 courses", facts[2].lower())
        self.assertIn("CS301", self._text(page, "#plbCurTTDigest"))
        # before a build: the muted line; after it: the projection's baseline completion term
        self.assertIn("After the first build", self._text(page, "#plbStudent .plb-sf:nth-child(4)"))
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text("Rebuild options")
        expect(page.locator("#plbStudent .plb-sf:nth-child(4) .plb-big")).to_have_text(
            "Term 1, 1450"
        )
        self.assertNotIn("After the first build", self._text(page, "#plbStudent"))
        # a new student has no build, so the line is muted again
        self._fetch(page, OTHER_ID)
        expect(page.locator("#plbStudent .plb-sf:nth-child(4) .plb-big--muted")).to_be_visible()

    def test_progress_shows_a_dash_while_the_plan_is_loading_and_when_it_failed(self) -> None:
        fake = BuildFake()
        fake.hold_plan = 1
        page = self._open(fake, load=False)
        self._fetch(page, "4500000")
        expect(page.locator("#plbStudent .plb-sf:nth-child(2) .plb-big")).to_have_text("—")
        self.assertEqual(page.locator("#plbStudent .plb-bar").count(), 0)
        fake.pending_plan.pop().fulfill(json=_plan_with_passed())
        expect(page.locator("#plbStudent .plb-sf:nth-child(2) .plb-big")).to_have_text("6 / 30 cr")
        page.context.close()
        failing = BuildFake()
        failing.plan_status = 500
        page = self._open(failing, load=False)
        self._fetch(page, "4500000")
        expect(page.locator("#plbStatus")).to_contain_text("plan")
        expect(page.locator("#plbStudent .plb-sf:nth-child(2) .plb-big")).to_have_text("—")

    def test_the_facts_in_arabic(self) -> None:
        fake = BuildFake()
        fake.plan_payload = _plan_with_passed()
        fake.projection_answer = lambda body: (200, READY)
        page = self._open(fake, language="ar")
        text = self._text(page, "#plbStudent")
        for word in ("البرنامج", "التقدم", "المسجّل الآن", "يتخرّج كما هو الحال", "بعد أول بناء"):
            self.assertIn(word, text)
        expect(page.locator("#plbStudent .plb-sf:nth-child(2) .plb-big")).to_contain_text("6 / 30")
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text("أعد بناء الخيارات")
        done = self._text(page, "#plbStudent .plb-sf:nth-child(4) .plb-big")
        self.assertIn("1450", done)
        self.assertNotIn("بعد أول بناء", self._text(page, "#plbStudent"))

    # ── fit ─────────────────────────────────────────────────────────────────
    def test_the_band_and_the_rules_wrap_without_sideways_scrolling(self) -> None:
        for language in ("en", "ar"):
            for width in (1440, 1152, 900, 390):
                with self.subTest(language=language, width=width):
                    page = self._open(BuildFake(), language=language, width=width, height=900)
                    self.assertEqual(page.evaluate(OVERFLOWS), [])
                    lefts = page.eval_on_selector_all(
                        "#plbStudent .plb-sf",
                        "n => n.map(x => { const r = x.getBoundingClientRect(); return [Math.round(r.top), Math.round(r.left)]; })",
                    )
                    rows = len({t for t, _ in lefts})
                    if width == 1440:
                        self.assertEqual(rows, 1, lefts)  # four facts in one row
                    if width == 390:
                        self.assertEqual(len({left for _, left in lefts}), 2, lefts)  # two columns
                        self.assertEqual(rows, 2)
                    # the six rules are all reachable at every width
                    for selector in (
                        "#plbRegKeep",
                        "#plbEligStrict",
                        "#plbMaxIn",
                        "#plbProgOnly",
                        "#plbFullIn",
                        "#plbSwapIn",
                    ):
                        self.assertEqual(page.locator(selector).count(), 1)
                    page.context.close()

    # ── reset, escape and the inert disclosure ───────────────────────────────
    def test_reset_over_an_edited_list_and_a_changed_eligibility_asks_first(self) -> None:
        fake = BuildFake()
        page = self._open(fake, plan=False)
        page.click("#plbEligRelaxed")  # nothing added yet: applied at once
        expect(page.locator("#plbEligRelaxed")).to_be_checked()
        self._add(page, "AI331")
        self.assertEqual(self._rows(page, "new"), ["AI331"])
        loads = len(fake.context_bodies)
        page.click("#plbRulesReset")
        expect(page.locator("#plbEligConfirm")).to_be_visible()
        expect(page.locator("#plbEligGo")).to_be_focused()
        # Keep: nothing changes
        page.click("#plbEligKeep")
        expect(page.locator("#plbEligConfirm")).to_be_hidden()
        expect(page.locator("#plbEligRelaxed")).to_be_checked()
        self.assertEqual(self._rows(page, "new"), ["AI331"])
        self.assertEqual(len(fake.context_bodies), loads)
        # Go: the usual rules come back, the student is reloaded and Your plan is the registered
        # courses only again (the course the adviser added is cleared)
        page.click("#plbRulesReset")
        page.click("#plbEligGo")
        expect(page.locator("#plbEligStrict")).to_be_checked()
        expect(page.locator("#plbRulesUsual")).to_be_visible()
        expect(page.locator("#plbAvailList .plb-arow[data-code='AI331']")).to_be_visible()
        self.assertEqual(self._rows(page, "new"), [])
        self.assertEqual(self._rows(page, "registered"), ["CS301", "CS322", "CS330"])
        self.assertEqual(fake.context_bodies[-1]["eligibility_mode"], "strict")

    def test_reset_without_an_edited_list_applies_at_once(self) -> None:
        page = self._open(BuildFake())
        page.click("#plbEligRelaxed")
        page.click("#plbRulesReset")
        expect(page.locator("#plbEligConfirm")).to_be_hidden()
        expect(page.locator("#plbEligStrict")).to_be_checked()

    def test_escape_closes_the_eligibility_confirm_wherever_the_keyboard_is(self) -> None:
        page = self._open(BuildFake())
        page.click("#plbPlanList [data-act='remove'][data-code='AI342']")
        page.click("#plbEligRelaxed")
        expect(page.locator("#plbEligConfirm")).to_be_visible()
        page.focus("#plbFind")
        page.keyboard.press("Escape")
        expect(page.locator("#plbEligConfirm")).to_be_hidden()
        expect(page.locator("#plbEligStrict")).to_be_focused()

    def test_the_current_timetable_blocks_are_inert(self) -> None:
        page, _ = self._built()
        page.click("#plbCurTT > summary")
        expect(page.locator("#plbCurTTWeek .plb-b")).to_have_count(6)
        self.assertEqual(
            page.locator(
                "#plbCurTTWeek button, #plbCurTTWeek [data-act], #plbCurTTWeek [tabindex]"
            ).count(),
            0,
        )

    def test_the_disclosure_is_drawn_again_only_when_the_registered_week_changes(self) -> None:
        from tests.planner_board_support import _row, context_for

        fake = BuildFake()

        def answer(body):
            payload = context_for(str(body["student_id"]))
            if str(body["student_id"]) == OTHER_ID:
                payload["baseline"] = [_row("AI499", "Thesis", "M1", 3, "THU", "09:00", "10:00", 9)]
            return 200, payload

        fake.context_answer = answer
        page, _ = self._built(fake)
        page.click("#plbCurTT > summary")
        expect(page.locator("#plbCurTTWeek .plb-b")).to_have_count(6)
        tag = "document.querySelector('#plbCurTTWeek .plb-wk').__tag = 1"
        read = "document.querySelector('#plbCurTTWeek .plb-wk').__tag === 1"
        page.evaluate(tag)
        page.click("#plbRegIgnore")  # the build goes out of date: the week below is drawn again
        expect(page.locator("#plbBuildLine")).to_contain_text("plan changed")
        self.assertTrue(page.evaluate(read))
        self._fetch(page, OTHER_ID)
        expect(page.locator("#plbCurTTWeek .plb-b")).to_have_count(1)
        self.assertNotEqual(page.evaluate(read), True)

    # ── the header and the rules wording ─────────────────────────────────────
    def test_the_title_and_the_term_button(self) -> None:
        for language, title, button in (
            ("en", "Plan next term", "Term 1, 1447"),
            ("ar", "خطة الفصل القادم", "الفصل 1، 1447"),
        ):
            with self.subTest(language=language):
                page = self._open(BuildFake(), language=language, load=False)
                self.assertIn(title, page.title())
                expect(page.locator("#plbStudentId")).to_be_visible()
                expect(page.locator("#plbFetch")).to_be_visible()
                expect(page.locator("#plbYear")).to_be_hidden()
                expect(page.locator("#plbTermBtn")).to_have_attribute("aria-expanded", "false")
                page.click("#plbTermBtn")
                expect(page.locator("#plbTermBtn")).to_have_attribute("aria-expanded", "true")
                expect(page.locator("#plbYear")).to_be_visible()
                expect(page.locator("#plbYear")).to_be_focused()
                page.select_option("#plbTerm", "1")
                page.fill("#plbYear", "1447")
                self.assertIn(button, " ".join(self._text(page, "#plbTermBtn").split()))
                page.keyboard.press("Escape")
                expect(page.locator("#plbYear")).to_be_hidden()
                expect(page.locator("#plbTermBtn")).to_be_focused()
                # the fields keep their ids and still drive the load
                self.assertEqual(page.input_value("#plbYear"), "1447")
                page.context.close()

    def test_the_rules_wording_and_the_usual_rules_line(self) -> None:
        for language, usual, reset, labels in (
            (
                "en",
                "The usual rules",
                "Reset",
                ["Prerequisites", "Most credits", "Use full sections", "Sections from"],
            ),
            (
                "ar",
                "القواعد المعتادة",
                "استعادة",
                ["المتطلبات السابقة", "أقصى عدد للساعات", "استخدام الشُعب الممتلئة", "الشُعب من"],
            ),
        ):
            with self.subTest(language=language):
                page = self._open(BuildFake(), language=language)
                zone = self._text(page, "#plbZoneRules")
                for label in labels:
                    self.assertIn(label, zone)
                self.assertEqual(self._text(page, "#plbRulesUsual"), usual)
                self.assertEqual(self._text(page, "#plbRulesReset"), reset)
                page.click("#plbRegIgnore")
                expect(page.locator("#plbRulesUsual")).to_be_hidden()
                expect(page.locator("#plbRulesChanged")).to_be_visible()
                page.click("#plbRulesReset")
                expect(page.locator("#plbRulesUsual")).to_be_visible()
                expect(page.locator("#plbRulesChanged")).to_be_hidden()
                page.context.close()

"""The planner board before it becomes the default: what the old page said and the board did not.

Section times by day, each section's programme, the way back to an empty plan (Fetch adds no
course, so "Reset to suggestions" went), the registered-data notes
(untimed codes, partial mapping), the student's advisor and Fetch while a load is held. Answered
from the same fakes as the other board tests; the real endpoints are in
tests/test_planner_board_end_to_end_browser.py.
"""

from __future__ import annotations

from tests.planner_board_support import (
    BASELINE,
    EXPECTED_BUILD_BODY,
    FIVE,
    OTHER_ID,
    OVERFLOWS,
    STUDENT_ID,
    BuildFake,
    PlannerBoardCase,
    _mapping,
    _option,
    _row,
    _section,
    context_for,
    expect,
)


def _multi(tsid, code, section, meetings, programs, registered=20, capacity=40):
    """A catalogue section from (day, start, end) meetings, in the order given."""
    out = _section(tsid, code, section, [], "00:00", "00:00", registered, capacity)
    out["meetings"] = [
        {"day": d, "start_time": s, "end_time": e, "room": "", "instructor": ""}
        for d, s, e in meetings
    ]
    out["programs"] = list(programs)
    return out


def _catalog(*sections):
    return {
        "ok": True,
        "count": len(sections),
        "program_sections_only": True,
        "sections": list(sections),
    }


ONE = _multi(
    21, "AI331", "M1", [("MON", "10:00", "11:15"), ("WED", "10:00", "11:15")], ["AI", "DS"]
)
TWO = _multi(22, "AI331", "M2", [("TUE", "10:00", "11:15"), ("SUN", "08:00", "09:15")], [])
# Three groups, given out of order, with the days of the middle group out of order too.
THREE = _multi(
    23,
    "AI331",
    "M3",
    [
        ("THU", "14:00", "15:15"),
        ("TUE", "09:30", "10:45"),
        ("SUN", "09:30", "10:45"),
        ("MON", "08:00", "08:50"),
    ],
    ["AI"],
)
NONE_ = _multi(24, "AI331", "M4", [], ["AI"])
# The one section of a course that could be added: two groups and a seat warning.
STAT = _multi(
    13, "STAT305", "M1", [("MON", "08:00", "09:15"), ("WED", "10:00", "11:15")], ["AI"], 37, 40
)
CATALOG = _catalog(ONE, TWO, THREE, NONE_, STAT)


def _fake(catalog=CATALOG) -> BuildFake:
    fake = BuildFake()
    fake.catalog_payload = catalog
    return fake


class PlannerBoardGapsTests(PlannerBoardCase):
    def _open_panel(self, page, code="AI331") -> None:
        self._sections_pop(page, code)
        expect(page.locator("#plbDetailTitle")).to_contain_text(code)

    def _main(self, page, tsid: int) -> str:
        return self._text(page, f"#plbPop [data-tsid='{tsid}'] .plb-dt-sec-main")

    def _second(self, page, tsid: int) -> str:
        return self._text(page, f"#plbPop [data-tsid='{tsid}'] .plb-dt-sec-seats")

    # ── 1. Section times by day ──────────────────────────────────────────────
    def test_section_times_name_the_days_of_each_group_in_the_panel(self) -> None:
        page = self._open(_fake())
        self._open_panel(page)
        self.assertEqual(self._main(page, 21), "§M1 Mon Wed 10:00-11:15")  # one group, as before
        self.assertEqual(self._main(page, 22), "§M2 Sun 08:00-09:15 · Tue 10:00-11:15")
        self.assertEqual(
            self._main(page, 23),
            "§M3 Mon 08:00-08:50 · Sun Tue 09:30-10:45 · Thu 14:00-15:15",
        )
        self.assertEqual(self._main(page, 24), "§M4")  # no meetings: no stray separator
        # Every time range is a left-to-right island of its own.
        ranges = page.eval_on_selector_all(
            "#plbPop [data-tsid='23'] .plb-dt-sec-main bdi[dir='ltr']",
            "ns => ns.map(n => n.textContent)",
        )
        self.assertEqual(ranges, ["§M3", "08:00-08:50", "09:30-10:45", "14:00-15:15"])

    def test_section_times_in_arabic_use_the_arabic_day_names(self) -> None:
        page = self._open(_fake(), language="ar")
        self._open_panel(page)
        self.assertEqual(self._main(page, 21), "§M1 اثن أرب 10:00-11:15")
        self.assertEqual(self._main(page, 22), "§M2 أحد 08:00-09:15 · ثلا 10:00-11:15")
        self.assertEqual(
            self._main(page, 23), "§M3 اثن 08:00-08:50 · أحد ثلا 09:30-10:45 · خم 14:00-15:15"
        )

    def test_a_course_that_could_be_added_says_when_each_section_meets(self) -> None:
        row = "#plbAvailList [data-code='STAT305']"
        page = self._open(_fake())
        page.click(f"{row} button[data-act='open']")
        line = self._text(page, f"{row} .plb-sec:not(.plb-sec--h)")
        self.assertIn("Mon 08:00-09:15 · Wed 10:00-11:15", line)
        self.assertIn("3 left", line)
        page = self._open(_fake(), language="ar")
        page.click(f"{row} button[data-act='open']")
        self.assertIn(
            "اثن 08:00-09:15 · أرب 10:00-11:15",
            self._text(page, f"{row} .plb-sec:not(.plb-sec--h)"),
        )

    def test_the_apply_dialog_names_the_days_of_each_group(self) -> None:
        two = _mapping("AI331", "M2", 12, ["MON"], "10:00", "11:15")
        two["meetings"] = [
            {"day": "TUE", "start_time": "10:00", "end_time": "11:15"},
            {"day": "SUN", "start_time": "08:00", "end_time": "09:15"},
        ]
        one = _mapping("AI342", "M1", 15, ["SUN", "TUE"], "13:00", "14:15")
        for language, expected in (
            ("en", ("Sun 08:00-09:15 · Tue 10:00-11:15", "Sun Tue 13:00-14:15")),
            ("ar", ("أحد 08:00-09:15 · ثلا 10:00-11:15", "أحد ثلا 13:00-14:15")),
        ):
            with self.subTest(language=language):
                page, _ = self._board([_option("B1", [two, one])], language=language)
                page.click("#plbReview")
                expect(page.locator("#plbApplyDialog")).to_be_visible()
                rows = page.eval_on_selector_all(
                    "#plbApplyList li",
                    "ls => ls.map(l => l.textContent.replace(/\\s+/g, ' ').trim())",
                )
                self.assertTrue(rows[0].endswith(expected[0]), rows)
                self.assertTrue(rows[1].endswith(expected[1]), rows)
                self.assertGreater(page.locator("#plbApplyList .plb-apply-when bdi").count(), 2)
                page.context.close()

    # ── 2. The programme of each section ────────────────────────────────────
    def test_each_section_names_its_programmes(self) -> None:
        page = self._open(_fake())
        self._open_panel(page)
        self.assertEqual(self._second(page, 21), "For AI / DS · 20 of 40 taken")
        self.assertEqual(self._second(page, 22), "Unassigned · 20 of 40 taken")
        self.assertEqual(self._second(page, 23), "For AI · 20 of 40 taken")
        page = self._open(_fake(), language="ar")
        self._open_panel(page)
        self.assertEqual(self._second(page, 21), "لبرامج AI / DS · 20 من 40 مشغولة")
        self.assertEqual(self._second(page, 22), "غير مرتبط ببرنامج · 20 من 40 مشغولة")
        self.assertEqual(self._second(page, 23), "لبرنامج AI · 20 من 40 مشغولة")

    def test_a_section_added_from_the_picker_names_its_programmes_too(self) -> None:
        # STAT305 is not in the plan until added: once it is, its popover says whose sections they are.
        page = self._open(_fake())
        self._add(page, "STAT305")
        self._open_panel(page, "STAT305")
        self.assertEqual(self._second(page, 13), "For AI · 37 of 40 taken")

    # ── 3. No suggestions to reset to: « is the way back ───────────────────
    # FETCH ADDS NO COURSE (owner, 2026-11-28). "Reset to suggestions" put back the list the page had
    # filled itself; with nothing filled there is nothing to put back, and « clears what was added.
    def test_remove_all_clears_every_kind_of_change_back_to_the_registered_courses(self) -> None:
        fake = _fake()
        page = self._open(fake)
        self.assertEqual(page.locator("#plbReset").count(), 0)
        # Change the list in every way a build reads: remove, add, must-take, a ticked section.
        page.click("#plbPlanList [data-act='remove'][data-code='AI342']")
        self._add(page, "STAT305")
        page.click("#plbPlanList button[data-act='must'][data-code='AI331']")
        self._open_panel(page, "AI331")
        page.locator("#plbPop [data-tsid='21'] input").check()
        self.assertEqual(self._rows(page, "new"), ["AI331", "STAT305"])
        page.click("#plbXRemAll")
        self.assertEqual(self._rows(page, "new"), [])
        self.assertEqual(self._rows(page, "registered"), ["CS301", "CS322", "CS330"])
        expect(page.locator("#plbStatus")).to_have_text("Removed 2 courses from the plan.")
        expect(page.locator("#plbFind")).to_be_focused()
        # What a build would send is the registered courses' room and no course of the adviser's.
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text("Rebuild options")
        self.assertEqual(fake.build_bodies[-1]["shortlist"], [])
        # The recommended courses moved back keep the recommender's priority and score.
        self._plan_recommended(page)
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_be_enabled()
        self.assertEqual(fake.build_bodies[-1]["shortlist"], EXPECTED_BUILD_BODY["shortlist"])

    def test_a_course_moved_from_any_source_keeps_its_recommendation(self) -> None:
        fake = _fake()
        page = self._open(fake, plan=False)
        # AI331 from the plan's own list, AI342 from "Everything they can register": both are
        # recommended, so both carry the recommender's priority and score, as Fetch used to give.
        self._add(page, "AI331", source="plan")
        self._add(page, "AI342", source="eligible")
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text("Rebuild options")
        self.assertEqual(fake.build_bodies[-1]["shortlist"], EXPECTED_BUILD_BODY["shortlist"])

    def test_remove_all_marks_a_build_out_of_date(self) -> None:
        page, fake = self._built()
        expect(page.locator("#plbActs")).to_have_attribute("data-stale", "")
        expect(page.locator("#plbReview")).to_be_enabled()
        page.click("#plbXRemAll")
        expect(page.locator("#plbActs")).to_have_attribute("data-stale", "changed")
        expect(page.locator("#plbReview")).to_be_disabled()
        expect(page.locator("#plbBuildLine")).to_have_text(
            "The plan changed since this build. Rebuild to apply."
        )
        self.assertEqual(self._rows(page, "new"), [])
        # The options stay on screen, out of date.
        self.assertGreater(page.locator("#plbTabs [role='tab']").count(), 1)

    def test_remove_all_during_a_build_makes_that_build_unwelcome(self) -> None:
        fake = _fake()
        fake.hold_build = 1
        page = self._open(fake)
        page.click("#plbBuild")
        expect(page.locator("#plbBuildLine")).to_contain_text("Building options")
        page.click("#plbXRemAll")
        fake.pending_build.pop().fulfill(json=FIVE)
        expect(page.locator("#plbBuildLine")).to_have_text(
            "The plan changed. Build again to see options."
        )
        self.assertEqual(page.locator("#plbTabs [role='tab']").count(), 1)

    def test_a_new_student_starts_with_only_their_registered_courses(self) -> None:
        fake = _fake()
        page = self._open(fake)
        self.assertEqual(self._rows(page, "new"), ["AI331", "AI342"])
        self._fetch(page, OTHER_ID)
        expect(page.locator("#plbContext")).to_have_text(f"{OTHER_ID} · 1448/1")
        expect(page.locator("#plbPlanList .plb-prow[data-code='AI331']")).to_have_count(0)
        self.assertEqual(self._rows(page, "new"), [])
        self.assertEqual(self._rows(page, "registered"), ["CS301", "CS322", "CS330"])

    # ── 4. Notes about the registered data ───────────────────────────────────
    def _broken_baseline(self, *extra):
        return [
            *BASELINE,
            _row("CS340", "Compilers", "M9", None, None, None, None, 9),
            _row("CS341", "Graphics", "M8", 3, "SUN", "bad", "worse", 8),
            *extra,
        ]

    def _note_fake(self, baseline):
        fake = _fake()
        fake.context_answer = lambda body: (
            200,
            {**context_for(str(body["student_id"])), "baseline": baseline},
        )
        return fake

    def test_the_week_names_the_registered_courses_without_class_times(self) -> None:
        page = self._open(self._note_fake(self._broken_baseline()), load=False)
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbWeek .plb-note")).to_have_text(
            "2 registered courses have no timetable slots (CS340, CS341)."
        )
        codes = page.eval_on_selector_all(
            "#plbWeek .plb-note bdi[dir='ltr']", "ns => ns.map(n => n.textContent)"
        )
        self.assertEqual(codes, ["CS340", "CS341"])
        page = self._open(self._note_fake(self._broken_baseline()), language="ar", load=False)
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbWeek .plb-note")).to_have_text(
            "مقرران مسجّلان بلا مواعيد في الجدول (CS340، CS341)."
        )

    def test_one_untimed_course_is_named_in_the_singular(self) -> None:
        baseline = [*BASELINE, _row("CS340", "Compilers", "M9", None, None, None, None, 9)]
        page = self._open(self._note_fake(baseline), load=False)
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbWeek .plb-note")).to_have_text(
            "1 registered course has no timetable slots (CS340)."
        )

    def test_a_partly_mapped_timetable_says_so_in_both_languages(self) -> None:
        # Two sections came from a fallback (one twice, on its two days); the third is a real one.
        fallback = [
            {
                **_row("CS301", "Algorithms", "M1", 3, "SUN", "08:00", "09:15", 1),
                "source": "x_fallback",
            },
            {
                **_row("CS301", "Algorithms", "M1", 3, "TUE", "08:00", "09:15", 1),
                "source": "x_fallback",
            },
            {
                **_row("CS322", "Databases", "M2", 3, "SUN", "10:00", "11:15", 2),
                "source": "FALLBACK",
            },
        ]
        baseline = [*fallback, *BASELINE[4:]]
        for language, text in (
            ("en", "Mapping quality: partial (2 fallback)"),
            ("ar", "جودة الربط: جزئي (2 احتياطي)"),
        ):
            with self.subTest(language=language):
                page = self._open(self._note_fake(baseline), language=language, load=False)
                self._fetch(page, STUDENT_ID)
                expect(page.locator("#plbWeek .plb-note")).to_have_text(text)
                page.context.close()

    def test_a_fully_mapped_timetable_has_no_note_and_ignored_registered_courses_have_none(
        self,
    ) -> None:
        page = self._open(_fake())
        self.assertEqual(page.locator("#plbWeek .plb-note").count(), 0)
        baseline = self._broken_baseline(
            {**BASELINE[0], "source": "fallback", "term_section_id": 77}
        )
        fake = self._note_fake(baseline)
        page = self._open(fake)
        self.assertEqual(page.locator("#plbWeek .plb-note").count(), 2)
        self._rules(page, mode="ignore")  # the registered week is not on this board any more
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text("Rebuild options")
        self.assertEqual(page.locator("#plbWeek .plb-note").count(), 0)

    # ── 5. The advisor ───────────────────────────────────────────────────────
    def test_the_student_card_names_the_advisor(self) -> None:
        for language, text in (
            ("en", "Sample Student · 4500000 · Advisor Dr. Sample Advisor"),
            ("ar", "Sample Student · 4500000 · المرشد Dr. Sample Advisor"),
        ):
            with self.subTest(language=language):
                fake = _fake()
                fake.context_answer = lambda body: (
                    200,
                    context_for(
                        str(body["student_id"]),
                        advisor_id="90210",
                        advisor_name="Dr. Sample Advisor",
                    ),
                )
                page = self._open(fake, language=language)
                self.assertEqual(self._text(page, "#plbStudent .plb-smeta"), text)
                self.assertEqual(
                    page.locator("#plbStudent .plb-smeta bdi[dir='ltr']").all_inner_texts(),
                    ["4500000"],
                )
                page.context.close()

    def test_no_advisor_name_no_word_and_never_the_number(self) -> None:
        for value in ("", None, "  "):
            with self.subTest(value=value):
                fake = _fake()
                fake.context_answer = lambda body, v=value: (
                    200,
                    context_for(str(body["student_id"]), advisor_id="90210", advisor_name=v),
                )
                page = self._open(fake)
                self.assertEqual(
                    self._text(page, "#plbStudent .plb-smeta"), "Sample Student · 4500000"
                )
                page.context.close()

    # ── 6. Fetch while a student loads ───────────────────────────────────────
    def test_fetch_waits_while_a_student_loads_and_is_back_afterwards(self) -> None:
        fake = _fake()
        fake.hold_ids = {STUDENT_ID}
        page = self._open(fake, load=False)
        expect(page.locator("#plbFetch")).to_be_enabled()
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbFetch")).to_be_disabled()
        # Enter in the field does not send a second request either.
        page.focus("#plbStudentId")
        page.keyboard.press("Enter")
        page.wait_for_timeout(200)
        self.assertEqual(len(fake.context_bodies), 1)
        _body, route = fake.pending.pop()
        route.fulfill(json=context_for(STUDENT_ID))
        expect(page.locator("#plbStatus")).to_have_text("")
        expect(page.locator("#plbFetch")).to_be_enabled()

    def test_fetch_is_back_after_a_failed_load(self) -> None:
        fake = _fake()
        fake.hold_ids = {STUDENT_ID}
        page = self._open(fake, load=False)
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbFetch")).to_be_disabled()
        _body, route = fake.pending.pop()
        route.fulfill(status=500, json={"ok": False, "error": {"code": "X"}})
        expect(page.locator("#plbStatus")).to_have_text(
            "The student could not be loaded. Try again."
        )
        expect(page.locator("#plbFetch")).to_be_enabled()
        # A missing field never starts a load, so it never disables the button.
        page.fill("#plbStudentId", "")
        page.click("#plbFetch")
        expect(page.locator("#plbStatus")).to_have_text("Enter a student ID.")
        expect(page.locator("#plbFetch")).to_be_enabled()

    def test_fetch_stays_off_until_the_newest_load_answers(self) -> None:
        # A rule reloads the suggestions (a load of its own) while the first is still held.
        fake = _fake()
        page = self._open(fake)
        fake.hold_ids = {STUDENT_ID}
        self._rules(page, elig="relaxed")
        expect(page.locator("#plbFetch")).to_be_disabled()
        _body, route = fake.pending.pop()
        route.fulfill(json=context_for(STUDENT_ID, eligibility_mode="relaxed"))
        expect(page.locator("#plbFetch")).to_be_enabled()

    # ── Layout ───────────────────────────────────────────────────────────────
    def test_the_keyboard_comes_back_to_fetch_after_a_load(self) -> None:
        for answer in ("loaded", "not found"):
            with self.subTest(answer=answer):
                fake = _fake()
                fake.hold_ids = {STUDENT_ID}
                page = self._open(fake, load=False)
                page.fill("#plbStudentId", STUDENT_ID)
                page.focus("#plbFetch")
                page.keyboard.press("Enter")
                expect(page.locator("#plbFetch")).to_be_disabled()
                for _ in range(250):
                    if fake.pending:
                        break
                    page.wait_for_timeout(20)
                body, route = fake.pending.pop()
                if answer == "loaded":
                    status, payload = fake.context_answer(body)
                    route.fulfill(status=status, json=payload)
                else:
                    route.fulfill(status=404, json={"error": {"code": "STUDENT_NOT_FOUND"}})
                expect(page.locator("#plbFetch")).to_be_enabled()
                expect(page.locator("#plbFetch")).to_be_focused()

    def test_a_click_during_the_load_keeps_fetch_from_taking_the_keyboard_back(self) -> None:
        fake = _fake()
        fake.hold_ids = {STUDENT_ID}
        page = self._open(fake, load=False)
        page.fill("#plbStudentId", STUDENT_ID)
        page.focus("#plbFetch")
        page.keyboard.press("Enter")
        expect(page.locator("#plbFetch")).to_be_disabled()
        for _ in range(250):
            if fake.pending:
                break
            page.wait_for_timeout(20)
        # A click on plain text during the load: the user has moved on, and the keyboard stays put.
        box = page.locator("h1").first.bounding_box()
        page.mouse.click(box["x"] + 2, box["y"] + box["height"] / 2)
        body, route = fake.pending.pop()
        status, payload = fake.context_answer(body)
        route.fulfill(status=status, json=payload)
        expect(page.locator("#plbFetch")).to_be_enabled()
        page.wait_for_timeout(100)
        expect(page.locator("#plbFetch")).not_to_be_focused()

    def test_two_untimed_courses_that_share_a_code_are_named_apart(self) -> None:
        baseline = [
            *BASELINE,
            _row("CS112", "Programming I", "M1", 3, None, None, None, 31),
            _row("CS112", "Discrete Structures", "M2", 3, None, None, None, 32),
        ]
        page = self._open(self._note_fake(baseline), load=False)
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbWeek .plb-note")).to_have_text(
            "2 registered courses have no timetable slots"
            " (CS112 Programming I, CS112 Discrete Structures)."
        )

    def test_a_course_line_that_wraps_never_covers_the_name_or_leaves_its_row(self) -> None:
        row = "#plbAvailList .plb-arow[data-code='STAT305']"
        geometry = """(sel) => {
          const row = document.querySelector(sel);
          const box = (n) => { const r = n.getBoundingClientRect(); return {top: r.top, bottom: r.bottom, left: r.left, right: r.right, height: r.height, width: r.width}; };
          return {row: box(row), name: box(row.querySelector('.plb-nm')), text: box(row.querySelector('.plb-meta'))};
        }"""
        for width in (1440, 390):
            for font in ("app", "wide"):
                with self.subTest(width=width, font=font):
                    page = self._open(_fake(), width=width, font=font)
                    # Half the width the line takes now: it has to wrap, whatever the font.
                    half = page.evaluate(geometry, row)["text"]["width"] / 2
                    page.add_style_tag(content=f"{row} .plb-meta {{ max-width: {half:.1f}px; }}")
                    boxes = page.evaluate(geometry, row)
                    name, text = boxes["name"], boxes["text"]
                    # the line is under the name or beside it, never over it
                    under = text["top"] >= name["bottom"] - 0.5
                    beside = (
                        text["left"] >= name["right"] - 0.5 or text["right"] <= name["left"] + 0.5
                    )
                    self.assertTrue(under or beside, boxes)
                    self.assertLessEqual(text["bottom"], boxes["row"]["bottom"] + 0.5, boxes)

    def test_nothing_overflows_at_three_widths_in_both_languages_and_both_fonts(self) -> None:
        crowded = _multi(
            31,
            "AI331",
            "M7",
            [
                ("SUN", "08:00", "09:15"),
                ("MON", "10:00", "11:15"),
                ("TUE", "12:00", "13:15"),
                ("WED", "14:00", "15:15"),
            ],
            ["AI", "DS", "CS", "COE", "CYP", "IS", "AI2", "DS2"],
        )
        catalog = _catalog(ONE, TWO, THREE, crowded, STAT)
        many = [
            _row(f"CS3{n}0", f"Course {n}", "M9", 3, None, None, None, 90 + n) for n in range(6)
        ]
        problems: dict[str, list[str]] = {}
        for language in ("en", "ar"):
            for font in ("app", "wide"):
                for width, height in ((1440, 900), (1152, 800), (390, 844)):
                    fake = _fake(catalog)
                    fake.context_answer = lambda body: (
                        200,
                        {
                            **context_for(str(body["student_id"]), advisor_id="1234567890123"),
                            "baseline": [
                                *BASELINE,
                                *many,
                                {**BASELINE[0], "source": "fallback", "term_section_id": 99},
                            ],
                        },
                    )
                    page = self._open(
                        fake, language=language, width=width, height=height, font=font
                    )
                    found = list(page.evaluate(OVERFLOWS))
                    self._open_panel(page)
                    found += page.evaluate(OVERFLOWS)
                    page.click("#plbBuild")
                    expect(page.locator("#plbBuild")).to_be_enabled()
                    page.click("#plbReview")
                    expect(page.locator("#plbApplyDialog")).to_be_visible()
                    page.click("#plbApplyCancel")
                    self._remove(page, "AI342")
                    self._remove(page, "AI331")
                    found += page.evaluate(OVERFLOWS)
                    if found:
                        problems[f"{language} {width}px {font} fonts"] = found
                    page.context.close()
        if problems:
            self.fail(
                "\n" + "\n".join(f"{k}:\n    " + "\n    ".join(v) for k, v in problems.items())
            )

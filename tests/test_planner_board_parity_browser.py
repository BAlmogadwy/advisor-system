"""The planner board's working-screen finish (S5): the Available list grows with its content, a
course that does not move graduation says nothing, the title carries a kicker, the build row says
whether the options still match, and every week uses one colour per course.
"""

from __future__ import annotations

import copy

from tests.planner_board_support import (
    FIVE,
    OVERFLOWS,
    STUDENT_ID,
    STUDENT_PLAN,
    BuildFake,
    PlannerBoardCase,
    _plan_course,
    expect,
)


class PlannerBoardParityBrowserTests(PlannerBoardCase):
    start_on = "C1"

    def _long_list(self, extra: int):
        plan = copy.deepcopy(STUDENT_PLAN)
        plan["terms"][0]["courses"] += [
            _plan_course(f"EL{n:03d}", f"Elective {n}", "not_taken", True)
            for n in range(1, extra + 1)
        ]
        fake = BuildFake()
        fake.plan_payload = plan
        return fake

    # ── item 3: no inner scroll; a long source shows its first rows and a button ───────────
    def test_the_available_list_has_no_inner_scrollbar(self) -> None:
        page = self._open(self._long_list(30))
        page.select_option("#plbSource", "eligible")
        metrics = page.evaluate(
            "() => { const n = document.getElementById('plbAvailList');"
            " return [getComputedStyle(n).overflowY, n.scrollHeight - n.clientHeight]; }"
        )
        self.assertEqual(metrics[0], "visible")
        self.assertLessEqual(metrics[1], 1)
        self.assertEqual(page.locator("#plbAvailList .plb-showall").count(), 0)

    def test_a_long_source_shows_a_show_all_button_with_the_count_and_it_works(self) -> None:
        for language, label in (("en", "Show all 52"), ("ar", "عرض الكل 52")):
            with self.subTest(language=language):
                page = self._open(self._long_list(50), language=language)
                page.select_option("#plbSource", "eligible")
                self.assertEqual(page.locator("#plbAvailList .plb-arow").count(), 40)
                expect(page.locator("#plbAvailList .plb-showall")).to_have_text(label)
                self.assertEqual(page.evaluate(OVERFLOWS), [])
                page.focus("#plbAvailList .plb-showall")
                page.keyboard.press("Enter")
                self.assertEqual(page.locator("#plbAvailList .plb-arow").count(), 52)
                # the keyboard lands on the first revealed row (the 41st), not in Find
                expect(
                    page.locator("#plbAvailList .plb-arow").nth(40).locator('[data-act="tick"]')
                ).to_be_focused()
                self.assertEqual(page.locator("#plbAvailList .plb-showall").count(), 0)
                page.context.close()

    # ── item 9: the kicker over the title ──────────────────────────────────────────────────
    def test_the_title_has_a_kicker_and_no_id_chip(self) -> None:
        page = self._open(BuildFake())
        self.assertEqual(self._text(page, "#plbKicker"), "Planner · Term 1, 1448")
        self.assertEqual(
            page.locator("#plbKicker").evaluate("n => getComputedStyle(n).textTransform"),
            "uppercase",
        )
        # the id/term chip is gone from the screen (it stays in the DOM for assistive technology)
        box = page.locator("#plbContext").bounding_box()
        self.assertLessEqual(box["width"], 2)
        # above the heading, not beside it
        kicker = page.locator("#plbKicker").bounding_box()
        heading = page.locator("main h1").bounding_box()
        self.assertLessEqual(kicker["y"] + kicker["height"], heading["y"] + 2)

    def test_the_kicker_follows_the_term_fields_and_speaks_arabic(self) -> None:
        page = self._open(BuildFake(), language="ar")
        self.assertIn("المخطِّط", self._text(page, "#plbKicker"))
        self.assertIn("1448", self._text(page, "#plbKicker"))

    # ── item 11: the build row's status line ───────────────────────────────────────────────
    def test_the_build_row_says_whether_the_options_still_match(self) -> None:
        page = self._open(BuildFake())
        expect(page.locator("#plbBuildState")).to_have_text("")  # before any build: nothing said
        page.click("#plbBuild")
        expect(page.locator("#plbBuildState")).to_have_text(
            "The options below match these courses and rules"
        )
        self.assertEqual(page.get_attribute("#plbBuildState", "data-kind"), "ok")
        self._rules(page, full=True)
        expect(page.locator("#plbBuildState")).to_have_text("Options are out of date — rebuild")
        self.assertEqual(page.get_attribute("#plbBuildState", "data-kind"), "warn")
        page.click("#plbBuild")
        expect(page.locator("#plbBuildState")).to_have_text(
            "The options below match these courses and rules"
        )

    def test_the_build_row_status_in_arabic(self) -> None:
        page = self._open(BuildFake(), language="ar")
        page.click("#plbBuild")
        expect(page.locator("#plbBuildState")).to_have_text(
            "الخيارات أدناه تطابق هذه المقررات والقواعد"
        )
        self._rules(page, full=True)
        expect(page.locator("#plbBuildState")).to_have_text("الخيارات قديمة، أعد البناء")

    # ── S6 item F: the status line is a live region that is always there ─────────────────
    LIVE = """() => { const n = document.getElementById('plbBuildState'); const cs = getComputedStyle(n);
      return { hidden: n.hidden, display: cs.display, visibility: cs.visibility, live: n.getAttribute('aria-live'),
        atomic: n.getAttribute('aria-atomic'), text: n.textContent, same: n.dataset.probe === '1',
        box: Math.round(n.getBoundingClientRect().height) }; }"""

    def test_the_build_state_line_stays_in_the_accessibility_tree_and_only_its_words_change(
        self,
    ) -> None:
        fake = BuildFake()
        fake.hold_build = 1
        page = self._open(fake)
        page.evaluate("document.getElementById('plbBuildState').dataset.probe = '1'")

        def held() -> None:
            for _ in range(250):
                if fake.pending_build:
                    return
                page.wait_for_timeout(20)
            raise AssertionError("the build was not asked")

        def look(text):
            expect(page.locator("#plbBuildState")).to_have_text(text)
            now = page.evaluate(self.LIVE)
            # Never hidden: present to a screen reader before its words change, so they are said.
            self.assertFalse(now["hidden"], now)
            self.assertNotEqual(now["display"], "none", now)
            self.assertEqual(now["visibility"], "visible", now)
            self.assertEqual((now["live"], now["atomic"]), ("polite", "true"), now)
            self.assertTrue(now["same"], "the region was replaced, not its words")
            return now

        before = look("")  # no build: nothing said, and no room taken
        self.assertLessEqual(before["box"], 1)
        page.click("#plbBuild")
        held()
        look("")  # building: nothing claimed yet
        fake.pending_build.pop().fulfill(json=FIVE)
        look("The options below match these courses and rules")
        self._rules(page, full=True)
        stale = look("Options are out of date — rebuild")
        self.assertGreater(stale["box"], 1)
        # A rebuild: the out-of-date claim goes while it runs, and the new words come back.
        fake.hold_build = 1
        page.click("#plbBuild")
        held()
        look("")
        fake.pending_build.pop().fulfill(json=FIVE)
        look("The options below match these courses and rules")

    # ── item F: one week style, one colour per course, registered ones locked ──────────────
    def test_every_week_draws_registered_courses_in_their_own_colour_with_a_lock(self) -> None:
        page, _fake = self._built()
        page.click("#plbTabRegistered")
        blocks = page.evaluate(
            "() => [...document.querySelectorAll('#plbWeek .plb-b[data-kind=\"reg\"]')]"
            ".map(b => [b.dataset.hue || '', !!b.querySelector('svg'), b.querySelector('.plb-bs') ? b.querySelector('.plb-bs').textContent : ''])"
        )
        self.assertTrue(blocks)
        for hue, lock, section in blocks:
            self.assertTrue(hue, "a registered block has a course colour")
            self.assertTrue(lock, "a registered block carries the lock")
            self.assertTrue(section.startswith("§"))
        # the same colour for the same course in the Current timetable week
        page.click("#plbCurTT > summary")
        expect(page.locator("#plbCurTTWeek .plb-b").first).to_be_visible()
        mine = page.evaluate(
            "() => Object.fromEntries([...document.querySelectorAll('#plbCurTTWeek .plb-b')].map(b => [b.dataset.code, b.dataset.hue]))"
        )
        theirs = page.evaluate(
            "() => Object.fromEntries([...document.querySelectorAll('#plbWeek .plb-b')].map(b => [b.dataset.code, b.dataset.hue]))"
        )
        self.assertEqual(mine, theirs)
        # the lavender registered scheme is gone: the ring is the course's own colour
        ring = page.evaluate(
            "() => getComputedStyle(document.querySelector('#plbWeek .plb-b[data-kind=\"reg\"]')).borderInlineStartColor"
        )
        planned = page.evaluate(
            "() => { const h = document.querySelector('#plbWeek .plb-b[data-hue]'); return getComputedStyle(h).borderTopStyle; }"
        )
        self.assertTrue(ring)
        self.assertEqual(planned, "solid")

    def test_a_registered_course_keeps_one_colour_across_tabs(self) -> None:
        page, _fake = self._built()
        page.click("#plbTabRegistered")
        before = page.evaluate(
            "() => document.querySelector('#plbWeek .plb-b[data-code=\"CS301\"]').dataset.hue"
        )
        self._pick(page, "C1")
        after = page.evaluate(
            "() => document.querySelector('#plbWeek .plb-b[data-code=\"CS301\"]').dataset.hue"
        )
        self.assertEqual(before, after)

    def test_the_student_id_is_still_the_context_for_screen_readers(self) -> None:
        page = self._open(BuildFake())
        self.assertEqual(self._text(page, "#plbContext"), f"{STUDENT_ID} · 1448/1")

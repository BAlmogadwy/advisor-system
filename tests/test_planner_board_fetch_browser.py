"""What Fetch puts in Your plan: the registered courses, and no course at all (owner, 2026-11-28).

"Fetch should never fill any course at all. It only adds the registered courses." The owner's case:
a student registered in 18 credits (six courses, two of them general-studies courses in no plan)
was shown "Your plan · 3 courses" (every eligible recommendation, prefilled) and "8 cr over the
limit", while the source said "Recommended for this student (0)". Now Your plan is the registered
courses only, the recommendations wait in Available, and a course the adviser adds where there is
no room is said to have none. The two elective rows its general-studies courses are likely posted
under carry a muted tag in keep mode (the server side: tests/test_planner_context_elective_rows.py).
Answered by the board fakes (tests/planner_board_support.py).
"""

from __future__ import annotations

import copy

from tests.planner_board_support import (
    STUDENT_ID,
    STUDENT_PLAN,
    BuildFake,
    PlannerBoardCase,
    _rec,
    _row,
    context_for,
    expect,
)
from tests.test_planner_board_kept_load_browser import NO_ROOM_LEAD, NO_ROOM_LEAD_AR, no_room

# Six registered courses, 18 credits: the whole default limit.
FULL_LOAD = [
    _row("CS205", "هياكل البيانات", "M1", 4, "SUN", "08:00", "09:40", 51),
    _row("CS285", "تصميم الخوارزميات", "M2", 4, "MON", "08:00", "09:40", 52),
    _row("COE124", "الدوائر المنطقية", "M3", 3, "TUE", "10:00", "11:15", 53),
    _row("ENGL214", "اللغة الإنجليزية", "M4", 3, "WED", "10:00", "11:15", 54),
    _row("GS311", "مهارات التواصل", "M5", 2, "THU", "08:00", "08:50", 55),
    _row("GS333", "التفكير الناقد", "M6", 2, "THU", "09:00", "09:50", 56),
]
REGISTERED = ["CS205", "CS285", "COE124", "ENGL214", "GS311", "GS333"]


def _owner_rec(code, name, credits):
    return {**_rec(code, name, "Eligible"), "credits": credits}


OWNER_RECS = [
    _owner_rec("ENG101", "English Language I", 4),
    _owner_rec("FE1", "Free Elective", 2),
    _owner_rec("GSE1", "University Elective", 2),
]
FILLED = {
    "FE1": {"course_code": "GS311", "basis": "same_hours"},
    "GSE1": {"course_code": "GS333", "basis": "same_hours"},
}


def owner_context(body, recs=OWNER_RECS, filled=FILLED, tag_recs=True):
    """The owner's case; `tag_recs=False` sends the map by row code only, no rec's own tag."""
    payload = context_for(str(body["student_id"]), eligibility_mode=body["eligibility_mode"])
    payload["baseline"] = FULL_LOAD
    payload["baseline_totals"] = {"courses": 6, "credits": 18}
    payload["recommendations"] = [
        {
            **rec,
            **(
                {"filled_by": filled[rec["course_code"]]}
                if tag_recs and rec["course_code"] in filled
                else {}
            ),
        }
        for rec in copy.deepcopy(recs)
    ]
    payload["elective_rows_filled"] = dict(filled)
    return 200, payload


def owner_fake(recs=OWNER_RECS, filled=FILLED, tag_recs=True):
    fake = BuildFake()
    fake.context_answer = lambda body: owner_context(body, recs, filled, tag_recs)
    plan = copy.deepcopy(STUDENT_PLAN)
    plan["terms"][0]["courses"] = [
        {
            "course_code": "FE1",
            "course_name": "Free Elective",
            "type": "Free Elective",
            "programme_term": 5,
            "credit_hours": 2,
            "status": "not_taken",
            "can_register": True,
            "prerequisites": [],
            "missing_prereqs": [],
            "importance_score": 1.0,
        }
    ]
    fake.plan_payload = plan
    return fake


class PlannerBoardFetchTests(PlannerBoardCase):
    def _owner(self, language="en", fake=None):
        page = self._open(fake or owner_fake(), language=language, load=False)
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbSource")).to_be_enabled()
        expect(page.locator("#plbPlanList .plb-prow--kept")).to_have_count(6)
        page.select_option("#plbSource", "recommended")
        return page

    # ── A. Fetch adds no course ───────────────────────────────────────────────────────────────
    def test_fetch_puts_only_the_registered_courses_in_your_plan(self) -> None:
        for language, head, line, source in (
            (
                "en",
                "Your plan · 0 courses",
                "0 planned + 18 registered · no room left under the limit",
                "Recommended for this student (3)",
            ),
            (
                "ar",
                "خطتك · 0 مقررًا",
                "0 مخطَّطة + 18 مسجّلة · لا متّسع ضمن الحد",
                "مقترحة لهذا الطالب (3)",
            ),
        ):
            with self.subTest(language=language):
                page = self._owner(language)
                self.assertEqual(self._rows(page, "new"), [])
                self.assertEqual(self._rows(page, "registered"), REGISTERED)
                self.assertEqual(self._text(page, "#plbPlanCount"), head)
                # The recommendations wait in Available, every one of them counted.
                self.assertEqual(self._text(page, "#plbSource option[value='recommended']"), source)
                self.assertEqual(
                    page.eval_on_selector_all(
                        "#plbAvailList .plb-arow", "rs => rs.map(r => r.dataset.code)"
                    ),
                    ["ENG101", "FE1", "GSE1"],
                )
                # 18 registered under an 18-credit limit and nothing added: no room, never "over".
                self.assertEqual(
                    self._load(page), ("18 / 18 " + ("cr" if language == "en" else "ساعة"), line)
                )
                self.assertEqual(page.get_attribute("#plbMeterLine", "data-over"), "false")
                self.assertEqual(page.get_attribute("#plbPlanLoad", "data-over"), "false")
                page.context.close()

    def test_in_ignore_mode_fetch_adds_no_course_either(self) -> None:
        page = self._open(owner_fake(), load=False)
        page.click("#plbRegIgnore")  # before any student: the rule rides along to the fetch
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbSource")).to_be_enabled()
        page.select_option("#plbSource", "recommended")
        expect(page.locator("#plbAvailList .plb-arow[data-code='ENG101']")).to_be_visible()
        self.assertEqual(self._rows(page, "new"), [])
        self.assertEqual(self._rows(page, "registered"), [])  # ignored: not listed
        self.assertEqual(self._load(page), ("0 / 18 cr", "0 planned · 18 cr free"))

    def test_a_reload_by_a_rule_clears_the_added_courses_and_says_so_first(self) -> None:
        for language, text, go in (
            (
                "en",
                "Changing the prerequisite rule reloads the recommended courses and clears the"
                " courses you added to Your plan.",
                "Change and clear",
            ),
            (
                "ar",
                "تغيير قاعدة المتطلبات السابقة يعيد تحميل المقررات المقترحة ويُزيل من خطتك"
                " المقررات التي أضفتها.",
                "غيّر وامسح",
            ),
        ):
            with self.subTest(language=language):
                fake = owner_fake()
                page = self._owner(language, fake)
                # Nothing added: the switch reloads at once, and asks nothing.
                page.click("#plbEligRelaxed")
                expect(page.locator("#plbEligConfirm")).to_be_hidden()
                self._wait(page, lambda fake=fake: len(fake.context_bodies) == 2)
                self._add(page, "ENG101", source="recommended")
                page.click("#plbEligStrict")
                expect(page.locator("#plbEligConfirm")).to_be_visible()
                self.assertEqual(self._text(page, "#plbEligConfirmText"), text)
                self.assertEqual(self._text(page, "#plbEligGo"), go)
                page.click("#plbEligGo")
                self._wait(page, lambda fake=fake: len(fake.context_bodies) == 3)
                expect(page.locator("#plbPlanList .plb-prow[data-code='ENG101']")).to_have_count(0)
                self.assertEqual(self._rows(page, "new"), [])
                self.assertEqual(self._rows(page, "registered"), REGISTERED)
                page.context.close()

    def test_the_empty_recommended_list_says_which_truth_it_is(self) -> None:
        for language, nothing, all_in in (
            (
                "en",
                "Nothing is recommended for this student.",
                "Every recommended course is already in Your plan.",
            ),
            ("ar", "لا مقررات مقترحة لهذا الطالب.", "كل المقررات المقترحة موجودة في خطتك."),
        ):
            with self.subTest(language=language):
                page = self._owner(language, owner_fake(recs=[], filled={}))
                expect(page.locator("#plbAvailList .plb-empty")).to_have_text(nothing)
                page.context.close()
                page = self._owner(language)
                page.click("#plbXAddAll")
                self.assertEqual(self._rows(page, "new"), ["ENG101", "FE1", "GSE1"])
                page.select_option("#plbSource", "recommended")
                expect(page.locator("#plbAvailList .plb-empty")).to_have_text(all_in)
                page.context.close()

    def test_a_course_added_where_there_is_no_room_is_said_to_have_none(self) -> None:
        for language, line, lead in (
            ("en", "4 planned + 18 registered · 4 cr over the limit", NO_ROOM_LEAD),
            ("ar", "4 مخطَّطة + 18 مسجّلة · 4 ساعات فوق الحد", NO_ROOM_LEAD_AR),
        ):
            with self.subTest(language=language):
                fake = owner_fake()
                fake.build_answer = lambda body: (200, no_room(18, codes=("ENG101",)))
                page = self._owner(language, fake)
                self._add(page, "ENG101", source="recommended")
                self.assertEqual(self._load(page)[1], line)
                self.assertEqual(page.get_attribute("#plbMeterLine", "data-over"), "true")
                page.click("#plbBuild")
                expect(page.locator("#plbBuildNote .plb-buildnote-lead")).to_have_text(lead)
                page.context.close()

    # ── B. A registered course outside the plan meets an open elective row ──────────────────
    @staticmethod
    def _tags(page) -> dict:
        return page.evaluate(
            """() => Object.fromEntries([...document.querySelectorAll('#plbAvailList .plb-arow')]
              .map((r) => { const t = r.querySelector('.plb-flag--filled');
                return [r.dataset.code, t ? t.textContent.replace(/\\s+/g, ' ').trim() : null]; }))"""
        )

    def test_keep_mode_tags_the_rows_a_registered_course_likely_counts_for(self) -> None:
        for language, fe1, gse1 in (
            (
                "en",
                "GS311 (registered) likely counts for it",
                "GS333 (registered) likely counts for it",
            ),
            ("ar", "GS311 (مسجّل) يُحتسب له على الأرجح", "GS333 (مسجّل) يُحتسب له على الأرجح"),
        ):
            with self.subTest(language=language):
                page = self._owner(language)
                self.assertEqual(self._tags(page), {"ENG101": None, "FE1": fe1, "GSE1": gse1})
                # The code is a left-to-right island inside the sentence.
                self.assertEqual(
                    page.locator(
                        "#plbAvailList .plb-arow[data-code='FE1'] .plb-flag--filled bdi[dir='ltr']"
                    ).text_content(),
                    "GS311",
                )
                # An assumption, not a rule: the row stays addable.
                row = "#plbAvailList .plb-arow[data-code='FE1']"
                expect(page.locator(f"{row} input[data-act='tick']")).to_be_enabled()
                expect(page.locator(f"{row} button[data-act='add']")).to_be_enabled()
                # The plan's own row says the same (the reply's map, by row code).
                page.select_option("#plbSource", "plan")
                self.assertEqual(self._tags(page), {"FE1": fe1})
                self._add(page, "FE1", source="plan")
                self.assertIn("FE1", self._rows(page, "new"))
                page.context.close()

    def test_the_map_by_row_code_tags_a_row_on_its_own(self) -> None:
        # The plan's rows and the eligible list are tagged from the reply's map, not only from a
        # recommendation's own `filled_by`.
        page = self._owner(fake=owner_fake(tag_recs=False))
        self.assertEqual(self._tags(page)["FE1"], "GS311 (registered) likely counts for it")
        page.select_option("#plbSource", "plan")
        self.assertEqual(self._tags(page), {"FE1": "GS311 (registered) likely counts for it"})

    def test_ignore_mode_draws_the_rows_without_the_tag(self) -> None:
        page = self._owner()
        self._rules(page, mode="ignore")
        page.select_option("#plbSource", "recommended")
        self.assertEqual(self._tags(page), {"ENG101": None, "FE1": None, "GSE1": None})
        self._rules(page, mode="keep")
        self.assertEqual(self._tags(page)["FE1"], "GS311 (registered) likely counts for it")

    @staticmethod
    def _wait(page, predicate) -> None:
        for _ in range(250):
            if predicate():
                return
            page.wait_for_timeout(20)
        raise AssertionError("timed out")

"""One name per course on the board, in the screen's language (slice S6, item E).

Registered rows showed the registrar's Arabic section name and plan rows the curriculum's English
name, side by side in one list. One helper now decides every name the board prints (Your plan,
Available, at a glance, the section popover, the week's labels): an English screen shows the
curriculum name when it is known, else the section name; an Arabic screen the section name when
it is known, else the curriculum name. The data side (names stored with scrape debris in front)
is tests/test_course_title_debris.py. Answered by the board fakes (tests/planner_board_support.py).
"""

from __future__ import annotations

import copy

from tests.planner_board_support import (
    SECTIONS_CATALOG,
    STUDENT_PLAN,
    BuildFake,
    PlannerBoardCase,
    _plan_course,
    _row,
    context_for,
    expect,
)

BASELINE = [
    # Registered, and in the plan: two names.
    _row("CS301", "الخوارزميات", "M1", 3, "SUN", "08:00", "09:15", 1),
    # Registered, in no plan: the section name only.
    _row("GS311", "مهارات التواصل", "M5", 2, "MON", "08:00", "08:50", 5),
]


def names_fake():
    fake = BuildFake()
    fake.context_answer = lambda body: (
        200,
        {**context_for(str(body["student_id"])), "baseline": BASELINE},
    )
    plan = copy.deepcopy(STUDENT_PLAN)
    plan["terms"][0]["courses"] = [
        _plan_course("CS301", "Algorithms", "studying", False),
        _plan_course("AI331", "Machine Learning", "not_taken", True),
        _plan_course("AI342", "Computer Vision", "not_taken", True),
        _plan_course("STAT305", "Probability", "not_taken", True),
    ]
    fake.plan_payload = plan
    catalog = copy.deepcopy(SECTIONS_CATALOG)
    for section in catalog["sections"]:
        if section["course_key"] == "AI331":
            section["course_name"] = "تعلم الآلة"  # the registrar's name for its sections
    fake.catalog_payload = catalog
    return fake


EN = {
    "CS301": "Algorithms",
    "GS311": "مهارات التواصل",
    "AI331": "Machine Learning",
    "AI342": "Computer Vision",
}
AR = {
    "CS301": "الخوارزميات",
    "GS311": "مهارات التواصل",
    "AI331": "تعلم الآلة",
    "AI342": "Computer Vision",
}


class PlannerBoardNamesTests(PlannerBoardCase):
    @staticmethod
    def _names(page, selector: str) -> dict:
        return page.evaluate(
            """(sel) => Object.fromEntries([...document.querySelectorAll(sel)].map((row) =>
              [row.dataset.code, (row.querySelector('.plb-nm') || {}).textContent || '']))""",
            selector,
        )

    def _board(self, language):
        page = self._open(names_fake(), language=language)
        expect(page.locator("#plbPlanList .plb-prow--kept")).to_have_count(2)
        # The catalogue has arrived (its sections carry the registrar's name for AI331).
        expect(
            page.locator("#plbAvailList .plb-arow[data-code='STAT305'] .plb-meta")
        ).to_contain_text("1 section" if language == "en" else "شعبة واحدة")
        return page

    def test_every_list_of_the_board_uses_the_screens_language(self) -> None:
        for language, want in (("en", EN), ("ar", AR)):
            with self.subTest(language=language):
                page = self._board(language)
                # Your plan: the planned and the registered rows.
                plan = self._names(page, "#plbPlanList .plb-prow[data-code]")
                self.assertEqual(plan, {code: want[code] for code in plan})
                self.assertEqual(sorted(plan), ["AI331", "AI342", "CS301", "GS311"])
                # At a glance (the registered week).
                glance = self._names(page, "#plbSecs .plb-sr")
                self.assertEqual(glance, {"CS301": want["CS301"], "GS311": want["GS311"]})
                # Available: a course of the plan whose sections carry the registrar's name.
                self._remove(page, "AI331")
                page.select_option("#plbSource", "eligible")
                avail = self._names(page, "#plbAvailList .plb-arow")
                self.assertEqual(avail["AI331"], want["AI331"])
                # The section popover of a planned course.
                self._add(page, "AI331")
                self._sections_pop(page, "AI331")
                self.assertIn(want["AI331"], self._text(page, "#plbDetailTitle"))
                page.context.close()

    def test_the_week_labels_and_the_search_use_the_same_names(self) -> None:
        page, _fake = self._built(names_fake(), language="ar")
        label = page.locator("#plbWeek button.plb-b[data-code='AI331']").first.get_attribute(
            "aria-label"
        )
        self.assertTrue(label.startswith("AI331 تعلم الآلة, "), label)
        # Either name finds the course.
        self._remove(page, "AI331")
        page.select_option("#plbSource", "eligible")
        for needle in ("تعلم", "machine"):
            page.fill("#plbFind", needle)
            self.assertEqual(self._rows(page, "could"), ["AI331"], needle)

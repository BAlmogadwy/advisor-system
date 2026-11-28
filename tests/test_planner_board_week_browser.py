"""The week's colours, in a real browser (slice S6, item C).

C. One colour per course, really. Registered courses have been coloured since PR #301, so with six
hues a seventh course repeated one (live: IS482 planned and STAT301 registered, both amber, in one
week). Ten hues now, assigned first-free; past ten the least-used hue is shared. The registered
rows of "at a glance" carry their swatch too.

The board fakes answer every request (tests/planner_board_support.py).
"""

from __future__ import annotations

import copy

from tests.planner_board_support import (
    STUDENT_ID,
    STUDENT_PLAN,
    BuildFake,
    PlannerBoardCase,
    _mapping,
    _option,
    _plan_course,
    _row,
    build_reply,
    context_for,
    expect,
)

# OKLab from an sRGB colour the browser resolved, and the least distance between any two.
OKLAB_MIN = """(tokens) => {
  const cv = document.createElement('canvas'); cv.width = cv.height = 1;
  const cx = cv.getContext('2d', { willReadFrequently: true });
  const probe = document.createElement('span');
  document.querySelector('.plb').append(probe);
  const lin = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  const lab = (token) => {
    probe.style.background = `var(${token})`;
    cx.clearRect(0, 0, 1, 1); cx.fillStyle = getComputedStyle(probe).backgroundColor; cx.fillRect(0, 0, 1, 1);
    const [r, g, b] = [...cx.getImageData(0, 0, 1, 1).data].slice(0, 3).map(lin);
    const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
    const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
    const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
    return [0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
      1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
      0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s];
  };
  const points = tokens.map(lab);
  probe.remove();
  let least = Infinity;
  for (let i = 0; i < points.length; i += 1) for (let j = i + 1; j < points.length; j += 1) {
    least = Math.min(least, Math.hypot(...points[i].map((v, k) => v - points[j][k])));
  }
  return least;
}"""
EDGES = [f"--plb-c{i}" for i in range(1, 11)]
TINTS = [f"--plb-c{i}-t" for i in range(1, 11)]

# Six registered courses (the owner's student had six), two of them 50 and 60 minutes long.
SIX = [
    _row("CS205", "Data Structures", "M1", 3, "SUN", "08:00", "09:15", 61),
    _row("CS285", "Algorithms", "M2", 3, "MON", "08:00", "09:15", 62),
    _row("COE124", "Logic Design", "M3", 3, "TUE", "08:00", "09:15", 63),
    _row("ENGL214", "English", "M4", 3, "WED", "08:00", "09:15", 64),
    _row("IS482", "Information Systems", "M15", 3, "SUN", "11:50", "12:40", 65),
    _row("STAT301", "Statistics", "F12", 3, "THU", "14:15", "15:15", 66),
]
SIX_CODES = ["CS205", "CS285", "COE124", "ENGL214", "IS482", "STAT301"]


def six_fake(options=None):
    fake = BuildFake()
    fake.context_answer = lambda body: (
        200,
        {**context_for(str(body["student_id"])), "baseline": SIX},
    )
    if options is not None:
        fake.build_answer = lambda body: (200, build_reply(options))
    return fake


# Option B1 for the planned AI331, AI342 and STAT305: a 50-minute class, a 75-minute one, a long one.
SHORT_WEEK = _option(
    "B1",
    [
        _mapping("AI331", "M2", 12, ["MON"], "11:50", "12:40"),
        _mapping("AI342", "M1", 15, ["TUE"], "10:00", "11:15"),
        _mapping("STAT305", "M1", 13, ["WED"], "10:00", "12:30"),
    ],
)


class PlannerBoardWeekColourTests(PlannerBoardCase):
    @staticmethod
    def _hues(page, selector: str) -> dict:
        return page.evaluate(
            """(sel) => Object.fromEntries([...document.querySelectorAll(sel)]
              .map((n) => [n.dataset.code || n.closest('[data-code]').dataset.code, n.dataset.hue || null]))""",
            selector,
        )

    def test_ten_hues_stay_apart_in_both_themes(self) -> None:
        page = self._open(BuildFake())
        for theme, edge, tint in (("light", 0.12, 0.05), ("dark", 0.11, 0.05)):
            page.evaluate(f"document.documentElement.setAttribute('data-theme', '{theme}')")
            page.wait_for_function("() => document.getAnimations().length === 0")
            self.assertGreaterEqual(page.evaluate(OKLAB_MIN, EDGES), edge, theme)
            self.assertGreaterEqual(page.evaluate(OKLAB_MIN, TINTS), tint, theme)

    def test_six_registered_and_three_planned_courses_have_nine_colours(self) -> None:
        page = self._open(six_fake(), plan=False)
        week = self._hues(page, "#plbWeek .plb-b")
        self.assertEqual(sorted(week), sorted(SIX_CODES))
        self._plan_recommended(page)
        self._add(page, "STAT305")
        rows = self._hues(page, "#plbPlanList .plb-prow .plb-code .plb-sw")
        colours = {**week, **rows}
        self.assertEqual(len(colours), 9)
        self.assertEqual(len(set(colours.values())), 9, colours)  # IS482 and STAT301 included
        self.assertNotIn(None, colours.values())

    def test_the_registered_rows_at_a_glance_carry_their_week_colour(self) -> None:
        page = self._open(six_fake())
        week = self._hues(page, "#plbWeek .plb-b")
        glance = self._hues(page, "#plbSecs .plb-sr--kept .plb-sw")
        self.assertEqual(glance, week)
        expect(page.locator("#plbSecs .plb-sr--kept .plb-sw[data-hue]")).to_have_count(6)

    def test_past_ten_courses_the_least_used_hue_is_shared(self) -> None:
        plan = copy.deepcopy(STUDENT_PLAN)
        plan["terms"][0]["courses"] = [
            _plan_course(f"EL{n:03d}", f"Elective {n}", "not_taken", True) for n in range(1, 14)
        ]
        fake = BuildFake()
        fake.plan_payload = plan
        fake.context_answer = lambda body: (
            200,
            {**context_for(str(body["student_id"])), "baseline": [], "recommendations": []},
        )
        page = self._open(fake, load=False)
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbSource")).to_be_enabled()
        page.select_option("#plbSource", "plan")
        for n in range(1, 13):
            page.check(f"#plbAvailList .plb-arow[data-code='EL{n:03d}'] input[data-act='tick']")
        page.click("#plbXAdd")
        hues = self._hues(page, "#plbPlanList .plb-prow .plb-code .plb-sw")
        # Ten distinct, first-free; the eleventh and twelfth share the least-used ones.
        self.assertEqual(
            [hues[f"EL{n:03d}"] for n in range(1, 13)], [*map(str, range(1, 11)), "1", "2"]
        )
        # EL001 goes: hue 1 is now held once, every other hue once or twice. The next course takes
        # hue 1, the least used (a count-based pick would have given the most-used hue 2).
        self._remove(page, "EL001")
        page.select_option("#plbSource", "plan")
        self._add(page, "EL013", source="plan")
        self.assertEqual(self._hues(page, "#plbPlanList .plb-prow .plb-code .plb-sw")["EL013"], "1")

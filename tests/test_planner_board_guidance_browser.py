"""The new planner screen, part five, in a real browser: one order for the options, their names,
the guidance column, the filled button and the week blocks that open a course's detail.

The same synthetic student and fakes as the other parts (tests/planner_board_support.py). The
options are crafted for the rule under test (`week_option`), and most builds ignore the registered
week so that an option's own days and times are the whole of what its tab says.
"""

from __future__ import annotations

import re

from tests.planner_board_support import (
    NO_FORECAST,
    OVERFLOWS,
    REG_FORECAST,
    BuildFake,
    PlannerBoardCase,
    build_reply,
    catalog_with,
    expect,
    forecast,
    projection_reply,
    week_option,
)

MORNING = (["SUN", "TUE"], "08:00", "09:15")
LATE_MORNING = (["MON", "WED"], "10:00", "11:15")
NOON = (["SUN", "TUE"], "11:30", "12:45")
AFTERNOON = (["MON", "WED"], "14:00", "15:15")
FILLED = """() => {
  const alpha = (c) => {
    if (c === 'transparent') return 0;
    const body = c.slice(c.indexOf('(') + 1, c.lastIndexOf(')'));
    if (body.includes('/')) return Number(body.split('/')[1]);
    const parts = body.split(',');
    return parts.length === 4 ? Number(parts[3]) : 1;
  };
  const read = (id) => {
    const node = document.getElementById(id);
    const style = getComputedStyle(node);
    return { filled: alpha(style.backgroundColor) >= 0.95, disabled: node.disabled, text: node.textContent.trim() };
  };
  return { build: read('plbBuild'), review: read('plbReview') };
}"""
GUIDE_FIT = """() => {
  const guide = document.getElementById('plbGuide');
  const box = guide.getBoundingClientRect();
  const bad = [];
  for (const node of guide.querySelectorAll('*')) {
    if (!node.getClientRects().length) continue;
    if (node.closest('.plb-fr--watch') === node) continue;   // the tinted row bleeds 8px by design
    const r = node.getBoundingClientRect();
    if (r.left < box.left - 8.5 || r.right > box.right + 8.5)
      bad.push(`${node.tagName.toLowerCase()}.${node.className} ${Math.round(r.left)}..${Math.round(r.right)} of ${Math.round(box.left)}..${Math.round(box.right)}`);
    if (node.scrollWidth > node.clientWidth + 1 && getComputedStyle(node).overflowX !== 'visible')
      bad.push(`clipped ${node.className}`);
  }
  return bad;
}"""


class PlannerBoardGuidanceTests(PlannerBoardCase):
    @staticmethod
    def _wait_for(page, predicate, what="the request") -> None:
        for _ in range(250):
            if predicate():
                return
            page.wait_for_timeout(20)
        raise AssertionError(f"timed out waiting for {what}")

    @staticmethod
    def _names_on_tabs(page) -> dict:
        return page.eval_on_selector_all(
            "#plbTabs [data-opt]",
            "tabs => Object.fromEntries(tabs.map(t => [t.dataset.opt,"
            " (t.querySelector('.plb-tab-nm') || {textContent: null}).textContent]))",
        )

    @staticmethod
    def _guide_rows(page) -> dict:
        return page.evaluate(
            "() => Object.fromEntries([...document.querySelectorAll('#plbGuide .plb-fr')].map("
            "r => [r.querySelector('dt').textContent.trim(),"
            " r.querySelector('dd').textContent.replace(/\\s+/g, ' ').trim()]))"
        )

    @staticmethod
    def _sentences(page) -> list[str]:
        return page.eval_on_selector_all(
            "#plbGuide .plb-gx", "n => n.map(x => x.textContent.replace(/\\s+/g, ' ').trim())"
        )

    @staticmethod
    def _suggested_tab(page) -> list[str]:
        return page.eval_on_selector_all(
            "#plbTabs [data-opt]",
            "tabs => tabs.filter(t => t.querySelector('.plb-sg')).map(t => t.dataset.opt)",
        )

    @staticmethod
    def _buttons(page) -> dict:
        """The two buttons' fill, read once no transition is still running."""
        page.wait_for_function("() => document.getAnimations().length === 0")
        return page.evaluate(FILLED)

    def _more_names(self, page) -> list[str]:
        return page.eval_on_selector_all(
            "#plbMoreList [data-more]", "n => n.map(x => x.dataset.more)"
        )

    def _said(self, page, name: str) -> str:
        """The name an option carries in the week's heading, after selecting it."""
        if page.locator(f"#plbTabs [data-opt='{name}']").count():
            page.click(f"#plbTabs [data-opt='{name}']")
        else:
            page.click("#plbMoreBtn")
            page.click(f"#plbMoreList [data-more='{name}']")
        return self._text(page, "#plbWeekName")

    def _rebuild(self, page, fake, options) -> None:
        fake.build_answer = lambda body: (200, build_reply(options))
        page.click("#plbBuild")
        self._wait_for(
            page,
            lambda: len(fake.build_bodies) >= 2 and page.locator("#plbTabs [data-opt]").count() > 0,
        )

    # ── 1. One order for the options ─────────────────────────────────────────────────────
    def _order(self, options, **kwargs) -> list[str]:
        page, _fake = self._board(options, **kwargs)
        return self._tab_names(page)[:-1]

    def test_each_key_of_the_order_decides_when_the_ones_before_it_tie(self) -> None:
        both = (MORNING, LATE_MORNING)
        # courses left unscheduled beat everything after them (P has the better week but misses one)
        self.assertEqual(
            self._order([week_option("B1", (["SUN"], "08:00", "09:15")), week_option("B2", *both)]),
            ["B2", "B1"],
        )
        # fewer days on campus (B1 comes first in the reply)
        self.assertEqual(
            self._order(
                [
                    week_option(
                        "B1", (["SUN", "MON", "TUE"], "08:00", "09:15"), (["WED"], "08:00", "09:15")
                    ),
                    week_option(
                        "B2", (["SUN", "MON"], "08:00", "09:15"), (["SUN"], "10:00", "11:15")
                    ),
                ]
            ),
            ["B2", "B1"],
        )
        # an earlier last class
        self.assertEqual(
            self._order(
                [
                    week_option("B1", (["SUN"], "08:00", "09:15"), (["SUN"], "12:00", "13:15")),
                    week_option("B2", (["SUN"], "08:00", "09:15"), (["SUN"], "10:30", "11:45")),
                ]
            ),
            ["B2", "B1"],
        )
        # a shorter longest gap (same days, same last class)
        self.assertEqual(
            self._order(
                [
                    week_option("B1", (["SUN"], "08:00", "09:15"), (["SUN"], "12:00", "13:15")),
                    week_option("B2", (["SUN"], "09:00", "10:15"), (["SUN"], "12:00", "13:15")),
                ]
            ),
            ["B2", "B1"],
        )
        # nothing differs: the reply's own order stands
        self.assertEqual(
            self._order(
                [week_option("C1", *both), week_option("B1", *both), week_option("A1", *both)]
            ),
            ["C1", "B1", "A1"],
        )

    def test_the_first_three_are_tabs_the_rest_wait_under_more_options_and_the_first_is_selected(
        self,
    ) -> None:
        options = [
            week_option(
                "A1", (["SUN", "MON", "TUE", "WED"], "08:00", "09:15"), (["THU"], "08:00", "09:15")
            ),
            week_option(
                "A2", (["SUN", "MON", "TUE"], "08:00", "09:15"), (["WED"], "08:00", "09:15")
            ),
            week_option("B1", (["SUN", "MON"], "08:00", "09:15"), (["TUE"], "08:00", "09:15")),
            week_option("B2", (["SUN"], "08:00", "09:15"), (["MON"], "08:00", "09:15")),
            week_option("C1", (["SUN"], "08:00", "09:15"), (["SUN"], "10:00", "11:15")),
        ]
        page, _fake = self._board(options)
        self.assertEqual(self._tab_names(page), ["C1", "B2", "B1", "(registered)"])
        self.assertEqual(self._more_names(page), ["A2", "A1"])
        self.assertEqual(page.get_attribute("#plbTab_C1", "aria-selected"), "true")
        self.assertEqual(self._text(page, "#plbWeekName"), "Option C1 · Mornings only")

    def test_the_forecast_moves_the_label_and_the_guidance_but_never_a_tab(self) -> None:
        options = [
            week_option("B1", (["SUN"], "08:00", "09:15"), (["MON"], "08:00", "09:15")),
            week_option("B2", (["SUN"], "08:00", "09:15"), (["SUN"], "10:00", "11:15")),
            week_option(
                "B3", (["SUN", "MON", "TUE"], "08:00", "09:15"), (["WED"], "08:00", "09:15")
            ),
        ]
        reply = projection_reply(
            REG_FORECAST,
            {
                "B1": forecast([15, 18, 18, 18, 6], delta=0),
                "B2": forecast([15, 18, 18, 18, 6], delta=0),
                "B3": forecast([15, 18, 18, 15], delta=-1),
            },
        )
        page, fake = self._board(options, projection=reply, hold_projection=True)
        # First paint: no forecast, so the order has no terms key: B2 (one day) leads, and stays selected.
        self._wait_for(page, lambda: len(fake.pending_projection) == 1)
        self.assertEqual(self._tab_names(page), ["B2", "B1", "B3", "(registered)"])
        self.assertEqual(self._suggested_tab(page), ["B2"])
        self.assertTrue(self._guide_rows(page)["Suggested"].startswith("B2"))
        # The forecast arrives: B3 finishes in fewer terms. Only the label and the guidance move.
        fake.pending_projection.pop().fulfill(json=reply)
        expect(page.locator("#plbGradLine")).to_be_hidden()
        self.assertEqual(self._suggested_tab(page), ["B3"])
        self.assertEqual(self._tab_names(page), ["B2", "B1", "B3", "(registered)"])
        self.assertEqual(page.get_attribute("#plbTab_B2", "aria-selected"), "true")
        self.assertTrue(self._guide_rows(page)["Suggested"].startswith("B3"))
        self.assertEqual(self._sentences(page)[0], "Fewer terms than B2: 4 against 5.")
        # ... and the button that goes to it.
        page.click("#plbGuideView")
        self.assertEqual(page.get_attribute("#plbTab_B3", "aria-selected"), "true")
        expect(page.locator("#plbTab_B3")).to_be_focused()
        expect(page.locator("#plbGuideView")).to_have_count(0)

    def test_the_terms_key_is_skipped_for_a_pair_when_one_forecast_is_missing(self) -> None:
        options = [
            week_option("B1", (["SUN", "MON"], "08:00", "09:15"), (["TUE"], "08:00", "09:15")),
            week_option("B2", (["SUN"], "08:00", "09:15"), (["MON"], "08:00", "09:15")),
        ]
        reply = projection_reply(REG_FORECAST, {"B1": forecast([15, 18, 18, 15], delta=-1)})
        page, _fake = self._board(options, projection=reply)
        expect(page.locator("#plbGradLine")).to_be_hidden()
        self.assertEqual(self._suggested_tab(page), ["B2"])  # B2's forecast is missing: days decide

    # ── 2. Names ────────────────────────────────────────────────────────────────────────
    def test_an_option_is_named_from_the_afternoons_in_its_own_week(self) -> None:
        days = ["SUN", "MON", "TUE", "WED", "THU"]

        def afternoons(name, n):
            late = (days[:n], "14:00", "15:15") if n else None
            early = (days[n:], "08:00", "09:15") if n < 5 else None
            return week_option(name, late or early, early if late else None)

        page, fake = self._board([afternoons("B1", 0), afternoons("B2", 1), afternoons("B3", 2)])
        self.assertEqual(
            self._names_on_tabs(page),
            {"B1": "Mornings only", "B2": "One afternoon", "B3": "Two afternoons"},
        )
        self._rebuild(page, fake, [afternoons("B1", 3), afternoons("B2", 4), afternoons("B3", 5)])
        self.assertEqual(
            self._names_on_tabs(page),
            {"B1": "Three afternoons", "B2": "Four afternoons", "B3": "Five afternoons"},
        )

    def test_a_class_that_ends_after_one_oclock_makes_its_day_an_afternoon(self) -> None:
        page, _fake = self._board(
            [
                week_option("B1", (["SUN"], "11:45", "13:00")),
                week_option("B2", (["SUN"], "11:50", "13:05")),
            ]
        )
        self.assertEqual(self._names_on_tabs(page), {"B1": "Mornings only", "B2": "One afternoon"})

    def test_an_option_with_no_timed_class_has_no_name(self) -> None:
        page, _fake = self._board(
            [week_option("B1", ([], "08:00", "09:15"), ([], "08:00", "09:15"))]
        )
        self.assertEqual(self._names_on_tabs(page), {"B1": None})
        self.assertEqual(self._text(page, "#plbWeekName"), "Option B1")

    def test_options_that_read_the_same_are_told_apart_and_the_later_ones_go_without(self) -> None:
        # Three "Two afternoons": 2, 3 and 4 days. The widest takes its days; of the other two the
        # later one in the order goes without.
        two = week_option("B1", AFTERNOON, (["MON"], "14:00", "15:15"))
        three = week_option("B2", AFTERNOON, (["THU"], "08:00", "09:15"))
        four = week_option("B3", AFTERNOON, (["SUN", "TUE"], "08:00", "09:15"))
        page, _fake = self._board([two, three, four])
        self.assertEqual(
            self._names_on_tabs(page),
            {"B1": "Two afternoons", "B2": None, "B3": "Four lighter days"},
        )
        # Renamed ones can collide too: the later one in the order goes without.
        page, _fake = self._board(
            [
                week_option(
                    "C1", AFTERNOON, (["SUN", "TUE"], "08:00", "09:15")
                ),  # 4 days, 2 afternoons
                week_option("C2", AFTERNOON, (["MON"], "14:00", "15:15")),  # 2 days, 2 afternoons
                week_option(
                    "B1", (["MON"], "14:00", "15:15"), (["SUN", "TUE", "WED"], "08:00", "09:15")
                ),
                week_option("B2", (["MON"], "14:00", "15:15"), (["SUN"], "08:00", "09:15")),
            ]
        )
        # In the order the tabs were laid out in: C2 and B2 (two days), then C1 and B1 (four).
        # C1 and B1 are each the widest of their group, so both are renamed "Four lighter days":
        # the later one, B1, goes without.
        said = {n: self._said(page, n) for n in ("C2", "B2", "C1", "B1")}
        self.assertEqual(said["C2"], "Option C2 · Two afternoons")
        self.assertEqual(said["B2"], "Option B2 · One afternoon")
        self.assertEqual(said["C1"], "Option C1 · Four lighter days")
        self.assertEqual(said["B1"], "Option B1")

    def test_weeks_with_as_many_days_each_are_not_called_lighter(self) -> None:
        # Found on real data: two options that differ by one section, as many days each. Neither
        # is lighter than the other: the first keeps the shared name and the later one goes without.
        page, _fake = self._board(
            [
                week_option("B1", AFTERNOON, (["MON"], "14:00", "15:15")),
                week_option("B2", AFTERNOON, (["MON"], "15:30", "16:45")),
            ]
        )
        self.assertEqual(self._names_on_tabs(page), {"B1": "Two afternoons", "B2": None})

    def test_names_in_arabic(self) -> None:
        page, _fake = self._board(
            [
                week_option("B1", (["SUN"], "08:00", "09:15")),
                week_option(
                    "B2", (["SUN", "MON"], "08:00", "09:15"), (["TUE", "WED"], "08:00", "09:15")
                ),
                week_option("B3", (["SUN"], "14:00", "15:15"), (["MON"], "14:00", "15:15")),
            ],
            language="ar",
        )
        self.assertEqual(
            self._names_on_tabs(page),
            {"B3": "يومان بعد الظهر", "B2": "4 أيام أخف", "B1": "صباحي فقط"},
        )
        page, _fake = self._board(
            [
                week_option("B1", (["SUN"], "14:00", "15:15"), (["SUN"], "16:00", "17:15")),
                week_option(
                    "B2", (["SUN", "MON", "TUE"], "14:00", "15:15"), (["SUN"], "16:00", "17:15")
                ),
            ],
            language="ar",
        )
        self.assertEqual(
            self._names_on_tabs(page), {"B1": "يوم واحد بعد الظهر", "B2": "3 أيام بعد الظهر"}
        )

    def test_credits_are_on_the_tabs_only_when_the_options_differ_in_credits(self) -> None:
        same = [week_option("B1", MORNING, LATE_MORNING), week_option("B2", NOON, AFTERNOON)]
        page, _fake = self._board(same)
        self.assertEqual(page.locator("#plbTabs [data-opt] .plb-tab-cr").count(), 0)
        self.assertEqual(
            self._text(page, "#plbTabCredits"), "9 cr"
        )  # the Registered tab keeps its own
        differ = [week_option("B1", MORNING, LATE_MORNING), week_option("B2", NOON)]
        page, _fake = self._board(differ)
        self.assertEqual(
            page.eval_on_selector_all(
                "#plbTabs [data-opt] .plb-tab-cr", "n => n.map(x => x.textContent)"
            ),
            ["6 cr", "3 cr"],
        )

    def test_more_options_say_their_names_and_suggested(self) -> None:
        options = [
            week_option(
                "A1", (["SUN", "MON", "TUE", "WED"], "08:00", "09:15"), (["THU"], "08:00", "09:15")
            ),
            week_option(
                "A2", (["SUN", "MON", "TUE"], "08:00", "09:15"), (["WED"], "08:00", "09:15")
            ),
            week_option("B1", (["SUN", "MON"], "08:00", "09:15"), (["TUE"], "08:00", "09:15")),
            week_option("B2", (["SUN"], "08:00", "09:15"), (["MON"], "08:00", "09:15")),
        ]
        reply = projection_reply(
            REG_FORECAST,
            {
                "A1": forecast([15, 18, 18, 9]),
                "A2": forecast([15, 18, 18, 18, 9]),
                "B1": forecast([15, 18, 18, 18, 9]),
                "B2": forecast([15, 18, 18, 18, 9]),
            },
        )
        page, _fake = self._board(options, projection=reply)
        expect(page.locator("#plbGradLine")).to_be_hidden()
        page.click("#plbMoreBtn")
        # All four read "Mornings only": A1, with the most days, takes its days; the others
        # go without but one.
        suggested = self._text(page, "#plbMoreList [data-more='A1']")
        self.assertTrue(suggested.startswith("A1 Five lighter days · 4 terms · 5 days"), suggested)
        self.assertTrue(suggested.endswith("Suggested"), suggested)
        self.assertEqual(self._suggested_tab(page), [])  # the suggested one is not on a tab

    # ── 3. Why this one ─────────────────────────────────────────────────────────────────
    def _suggest(self, options, projection=None, **kwargs):
        page, fake = self._board(options, projection=projection, **kwargs)
        if projection is not None:
            expect(page.locator("#plbGradLine")).to_be_hidden()
        return page, fake

    @staticmethod
    def _same_terms(names, count=4):
        return projection_reply(
            REG_FORECAST, {n: forecast([15] + [18] * (count - 2) + [9]) for n in names}
        )

    def test_the_only_timetable_and_the_only_one_that_schedules_every_course(self) -> None:
        page, _ = self._suggest([week_option("B1", MORNING, LATE_MORNING)])
        self.assertEqual(self._sentences(page)[0], "The only timetable this build found.")
        page, _ = self._suggest(
            [week_option("B1", MORNING), week_option("B2", MORNING, LATE_MORNING)]
        )
        self.assertEqual(self._sentences(page)[0], "The only option that schedules every course.")
        self.assertTrue(self._guide_rows(page)["Suggested"].startswith("B2"))
        page, _ = self._suggest([week_option("B1", MORNING)], language="ar")
        self.assertEqual(self._sentences(page)[0], "الجدول الوحيد الذي وجده هذا البناء.")
        page, _ = self._suggest(
            [week_option("B1", MORNING), week_option("B2", MORNING, LATE_MORNING)], language="ar"
        )
        self.assertEqual(self._sentences(page)[0], "الخيار الوحيد الذي يجدول كل المقررات.")

    # The sentence is the order in words: what the suggested option shares with the next one in
    # the order, then the key that decided between them, with both values.
    def test_terms_decide(self) -> None:
        options = [
            week_option("B1", (["SUN", "MON"], "08:00", "09:15"), (["TUE"], "08:00", "09:15")),
            week_option("B2", (["SUN"], "08:00", "09:15"), (["MON"], "08:00", "09:15")),
            week_option(
                "B3", (["SUN", "TUE", "WED"], "08:00", "09:15"), (["MON"], "08:00", "09:15")
            ),
        ]

        def reply(**terms):
            return projection_reply(
                REG_FORECAST, {n: forecast([15] + [18] * (t - 2) + [9]) for n, t in terms.items()}
            )

        page, _ = self._suggest(options, reply(B1=4, B2=5, B3=6))
        self.assertEqual(self._sentences(page)[0], "Fewer terms than B2: 4 against 5.")
        # The next one in the order is the one named, not the worst.
        page, _ = self._suggest(options, reply(B1=4, B2=6, B3=5))
        self.assertEqual(self._sentences(page)[0], "Fewer terms than B3: 4 against 5.")
        page, _ = self._suggest(options[:2], reply(B1=4, B2=5), language="ar")
        self.assertEqual(self._sentences(page)[0], "فصول أقل من B2: 4 مقابل 5.")

    def test_days_finish_and_gaps_decide_after_what_is_shared(self) -> None:
        def sentence(options, language="en"):
            page, _ = self._suggest(
                options, self._same_terms([o["name"] for o in options]), language=language
            )
            return self._sentences(page)[0]

        days = [
            week_option("B1", (["SUN"], "08:00", "09:15"), (["SUN"], "10:00", "11:15")),
            week_option("B2", (["SUN", "MON"], "08:00", "09:15"), (["SUN"], "10:00", "11:15")),
            week_option(
                "B3", (["SUN", "MON", "TUE"], "08:00", "09:15"), (["SUN"], "10:00", "11:15")
            ),
        ]
        self.assertEqual(sentence(days), "Same terms as B2; fewer days on campus: 1 against 2.")
        self.assertEqual(sentence(days, "ar"), "مثل B2 في عدد الفصول؛ أيام حضور أقل: 1 مقابل 2.")
        finish = [
            week_option("B1", (["SUN"], "08:00", "09:15"), (["MON"], "08:00", "12:45")),
            week_option("B2", (["SUN"], "08:00", "09:15"), (["MON"], "08:00", "14:15")),
        ]
        self.assertEqual(
            sentence(finish),
            "Same terms and days on campus as B2; ends earlier: 12:45 against 14:15.",
        )
        self.assertEqual(
            sentence(finish, "ar"),
            "مثل B2 في عدد الفصول وأيام الحضور؛ ينتهي أبكر: 12:45 مقابل 14:15.",
        )
        gaps = [
            week_option("B1", (["SUN"], "08:00", "09:15"), (["SUN"], "09:15", "12:00")),
            week_option("B2", (["SUN"], "08:00", "09:15"), (["SUN"], "10:00", "12:00")),
            week_option("B3", (["SUN"], "08:00", "09:15"), (["SUN"], "10:30", "12:00")),
        ]
        self.assertEqual(
            sentence(gaps),
            "Same terms, days on campus and finish time as B2; shorter gaps: none against 45 min.",
        )
        gaps = gaps[1:] + [
            week_option("B4", (["SUN"], "08:00", "09:15"), (["SUN"], "11:00", "12:00"))
        ]
        self.assertEqual(
            sentence(gaps),
            "Same terms, days on campus and finish time as B3;"
            " shorter gaps: 45 min against 1 h 15 min.",
        )
        self.assertEqual(
            sentence(gaps, "ar"),
            "مثل B3 في عدد الفصول وأيام الحضور ووقت الانتهاء؛ فجوات أقصر: 45 د مقابل 1 س 15 د.",
        )
        # Without a forecast for every option the terms are not part of the order, nor of the
        # sentence.
        page, _ = self._suggest(finish)
        self.assertEqual(
            self._sentences(page)[0],
            "Same days on campus as B2; ends earlier: 12:45 against 14:15.",
        )
        page, _ = self._suggest(days)
        self.assertEqual(self._sentences(page)[0], "Fewer days on campus than B2: 1 against 2.")

    def test_the_mock_ups_three_options(self) -> None:
        # B1 has Thursday free and a 12:45 finish; A1 has Thursday free but ends later; A2 ends at
        # 12:45 but has no free day. B1 is first, A1 next: same terms and days, an earlier finish.
        b1 = week_option(
            "B1",
            (["SUN", "MON", "TUE", "WED"], "08:00", "09:15"),
            (["SUN", "TUE", "WED"], "11:30", "12:45"),
        )
        a1 = week_option(
            "A1",
            (["SUN", "MON", "TUE", "WED"], "08:00", "09:15"),
            (["SUN", "TUE", "WED"], "13:00", "14:15"),
        )
        a2 = week_option(
            "A2",
            (["SUN", "MON", "TUE", "WED", "THU"], "08:00", "09:15"),
            (["SUN", "TUE", "WED", "THU"], "11:30", "12:45"),
        )
        for language, expected in (
            ("en", "Same terms and days on campus as A1; ends earlier: 12:45 against 14:15."),
            ("ar", "مثل A1 في عدد الفصول وأيام الحضور؛ ينتهي أبكر: 12:45 مقابل 14:15."),
        ):
            page, _ = self._suggest(
                [a1, a2, b1], self._same_terms(["A1", "A2", "B1"]), language=language
            )
            self.assertEqual(self._sentences(page)[0], expected)
            self.assertTrue(list(self._guide_rows(page).values())[0].startswith("B1"))

    def test_two_options_that_look_the_same_are_told_apart_by_the_replys_order(self) -> None:
        same = [week_option("B1", MORNING, LATE_MORNING), week_option("B2", MORNING, LATE_MORNING)]
        page, _ = self._suggest(same)
        self.assertEqual(
            self._sentences(page)[0], "Level with B2 on every count; the build listed it first."
        )
        page, _ = self._suggest(same, self._same_terms(["B1", "B2"]), language="ar")
        self.assertEqual(
            self._sentences(page)[0], "يتساوى مع B2 في كل المعايير؛ وقد ورد في نتيجة البناء قبله."
        )

    def test_two_options_that_schedule_everything_are_not_each_called_the_only_one(self) -> None:
        page, _ = self._suggest(
            [
                week_option("B1", MORNING, LATE_MORNING),
                week_option("B2", MORNING, LATE_MORNING),
                week_option("B3", MORNING),
            ]
        )
        self.assertEqual(
            self._sentences(page)[0], "Level with B2 on every count; the build listed it first."
        )

    def test_fewer_courses_left_out_when_none_schedules_them_all(self) -> None:
        page, _ = self._suggest([week_option("B1"), week_option("B2", MORNING), week_option("B3")])
        self.assertEqual(self._sentences(page)[0], "Fewer courses left out than B1: 1 against 2.")

    def test_the_order_does_not_depend_on_the_replys_order_when_a_forecast_is_missing(self) -> None:
        # A takes 4 terms on 3 days, C takes 5 terms on 1 day, B has no forecast and 2 days. Using
        # the terms for the pairs that have them and skipping them for the others would put A
        # before C, B before A and C before B: not an order. So without a forecast for every
        # option the terms take no part, and the fewest days win whatever order they came in.
        a = week_option(
            "A1", (["SUN", "MON", "TUE"], "08:00", "09:15"), (["SUN"], "10:00", "11:15")
        )
        b = week_option("B1", (["SUN", "MON"], "08:00", "09:15"), (["SUN"], "10:00", "11:15"))
        c = week_option("C1", (["SUN"], "08:00", "09:15"), (["SUN"], "10:00", "11:15"))
        reply = projection_reply(
            REG_FORECAST,
            {
                "A1": forecast([15, 18, 18, 9]),
                "B1": NO_FORECAST,
                "C1": forecast([15, 18, 18, 18, 9]),
            },
        )
        for options in ([a, b, c], [c, b, a], [b, c, a], [a, c, b]):
            page, _ = self._suggest(options, reply)
            where = [o["name"] for o in options]
            self.assertTrue(self._guide_rows(page)["Suggested"].startswith("C1"), where)
            self.assertEqual(
                self._sentences(page)[0], "Fewer days on campus than B1: 1 against 2.", where
            )

    def test_the_suggested_option_is_a_tab_with_a_label_and_a_view_button_when_not_selected(
        self,
    ) -> None:
        options = [
            week_option("B1", (["SUN", "MON"], "08:00", "09:15"), (["TUE"], "08:00", "09:15")),
            week_option("B2", (["SUN"], "08:00", "09:15"), (["MON"], "08:00", "09:15")),
        ]
        page, _ = self._board(options)
        self.assertEqual(self._suggested_tab(page), ["B2"])
        expect(page.locator("#plbGuideView")).to_have_count(0)  # it is the selected one
        page.click("#plbTab_B1")
        self.assertEqual(self._text(page, "#plbGuideView"), "View B2")
        self.assertEqual(self._suggested_tab(page), ["B2"])  # the label stays where it is

    # ── 4. Watch ────────────────────────────────────────────────────────────────────────
    def _seats(self, *rows):
        """Catalogue: (tsid, code, section, registered, capacity)."""
        return catalog_with(*rows)

    def test_watch_names_the_nearly_full_section_and_the_fallback(self) -> None:
        catalog = catalog_with(
            (15, "AI342", "M1", 36, 40),  # 4 left
            (16, "AI342", "M2", 10, 40),
            (11, "AI331", "M1", 5, 40),
        )
        options = [
            week_option(
                "B1",
                (["SUN", "MON"], "08:00", "09:15"),
                (["TUE"], "08:00", "09:15"),
                sections=("M1", "M1"),
                tsids={"AI331": 11, "AI342": 15},
            ),
            week_option(
                "A1",
                (["SUN", "MON", "TUE"], "08:00", "09:15"),
                (["WED"], "08:00", "09:15"),
                sections=("M1", "M2"),
                tsids={"AI331": 11, "AI342": 16},
            ),
        ]
        page, _ = self._suggest(options, catalog=catalog)
        rows = self._guide_rows(page)
        self.assertIn("AI342 §M1 has 4 seats left", rows["Watch"])
        self.assertEqual(self._sentences(page)[1], "A1 is the fallback: Thu free, ends 09:15.")
        self.assertEqual(self._text(page, "#plbGuideFallback"), "View A1")
        page.click("#plbGuideFallback")
        self.assertEqual(page.get_attribute("#plbTab_A1", "aria-selected"), "true")

    def test_the_fallback_is_the_first_option_in_the_order_without_a_nearly_full_section(
        self,
    ) -> None:
        catalog = catalog_with((15, "AI342", "M1", 36, 40), (16, "AI342", "M2", 10, 40))
        options = [
            week_option(
                "B1",
                (["SUN", "MON"], "08:00", "09:15"),
                (["TUE"], "08:00", "09:15"),
                tsids={"AI342": 15},
            ),
            # Second in the order, but it uses the nearly full section too: not the fallback.
            week_option(
                "B2",
                (["SUN", "MON", "TUE"], "08:00", "09:15"),
                (["WED"], "08:00", "09:15"),
                tsids={"AI342": 15},
            ),
            week_option(
                "A1",
                (["SUN", "MON", "TUE", "WED"], "08:00", "09:15"),
                (["THU"], "08:00", "09:15"),
                tsids={"AI342": 16},
            ),
        ]
        page, _ = self._suggest(options, catalog=catalog)
        self.assertEqual(self._tab_names(page)[:3], ["B1", "B2", "A1"])
        self.assertEqual(self._text(page, "#plbGuideFallback"), "View A1")
        self.assertTrue(self._sentences(page)[1].startswith("A1 is the fallback:"))

    def test_watch_without_a_fallback_and_without_anything_to_watch(self) -> None:
        catalog = catalog_with((15, "AI342", "M1", 36, 40), (16, "AI342", "M2", 37, 40))
        both = [
            week_option("B1", MORNING, (["MON"], "08:00", "09:15"), tsids={"AI342": 15}),
            week_option(
                "B2", (["SUN"], "08:00", "09:15"), (["WED"], "08:00", "09:15"), tsids={"AI342": 16}
            ),
        ]
        page, _ = self._suggest(both, catalog=catalog)
        self.assertIn(
            "Every option uses a section that is nearly full.", self._guide_rows(page)["Watch"]
        )
        self.assertEqual(page.locator("#plbGuide .plb-view").count(), 0)
        # Not nearly full: 6 seats left, an unknown capacity, a full section: nothing to watch.
        for registered, capacity in ((34, 40), (None, None), (40, 40)):
            catalog = catalog_with((15, "AI342", "M1", registered, capacity))
            page, _ = self._suggest(
                [week_option("B1", MORNING, (["MON"], "08:00", "09:15"), tsids={"AI342": 15})],
                catalog=catalog,
            )
            self.assertNotIn("Watch", self._guide_rows(page), (registered, capacity))
        # Five seats is still a warning.
        catalog = catalog_with((15, "AI342", "M1", 35, 40))
        page, _ = self._suggest(
            [
                week_option(
                    "B1",
                    MORNING,
                    (["MON"], "08:00", "09:15"),
                    sections=(None, "M1"),
                    tsids={"AI342": 15},
                )
            ],
            catalog=catalog,
        )
        self.assertIn("AI342 §M1 has 5 seats left", self._guide_rows(page)["Watch"])

    def test_watch_in_arabic(self) -> None:
        catalog = catalog_with((15, "AI342", "M1", 36, 40), (16, "AI342", "M2", 10, 40))
        options = [
            week_option(
                "B1",
                (["SUN", "MON"], "08:00", "09:15"),
                (["TUE"], "08:00", "09:15"),
                sections=("M1", "M1"),
                tsids={"AI342": 15},
            ),
            week_option(
                "A1",
                (["SUN", "MON", "TUE"], "08:00", "09:15"),
                (["WED"], "08:00", "09:15"),
                sections=("M1", "M2"),
                tsids={"AI342": 16},
            ),
        ]
        page, _ = self._suggest(options, catalog=catalog, language="ar")
        watch = self._guide_rows(page)["انتبه"]
        self.assertIn("AI342 §M1: 4 مقاعد متبقية", watch)
        self.assertIn("A1 هو البديل:", watch)
        self.assertIn("عرض A1", watch)

    # ── 5. Status ───────────────────────────────────────────────────────────────────────
    def test_status_says_what_is_wrong_with_the_selected_tab(self) -> None:
        clean = week_option("B1", (["THU"], "09:00", "10:15"), (["THU"], "11:00", "12:15"))
        left = week_option("B2", (["THU"], "09:00", "10:15"))
        one = week_option("B3", (["SUN"], "08:30", "09:45"), (["THU"], "11:00", "12:15"))
        two = week_option("A1", (["SUN", "TUE"], "08:30", "09:45"), (["THU"], "11:00", "12:15"))
        page, _ = self._board([clean, left, one, two], ignore=False)
        status = lambda: self._guide_rows(page)["Status"]  # noqa: E731
        page.click("#plbTab_B1")
        self.assertEqual(status(), "2 of 2 courses scheduled · no conflicts")
        self.assertEqual(page.locator("#plbGuide .plb-okl").get_attribute("data-kind"), "ok")
        self._pick(page, "B2")
        self.assertEqual(status(), "1 of 2 scheduled · not scheduled: AI342")
        self.assertEqual(page.locator("#plbGuide .plb-okl").get_attribute("data-kind"), "warn")
        page.click("#plbTab_B3")
        self.assertEqual(
            status(), "2 of 2 courses scheduled · 1 class clashes with a registered one"
        )
        page.click("#plbTab_A1")
        self.assertEqual(
            status(), "2 of 2 courses scheduled · 2 classes clash with registered ones"
        )
        page.click("#plbTabRegistered")
        self.assertEqual(status(), "3 courses · 9 cr")
        self.assertEqual(page.locator("#plbGuide .plb-okl").get_attribute("data-kind"), "ok")

    def test_status_in_arabic(self) -> None:
        left = week_option("B1", (["THU"], "09:00", "10:15"))
        page, _ = self._board([left], ignore=False, language="ar")
        self.assertEqual(self._guide_rows(page)["الحالة"], "1 من 2 مجدولة · غير مجدولة: AI342")
        page.click("#plbTabRegistered")
        self.assertEqual(self._guide_rows(page)["الحالة"], "3 مقررات · 9 ساعات")

    # ── 6. When the guidance is there ───────────────────────────────────────────────────
    def test_the_guidance_follows_the_build(self) -> None:
        page = self._open(BuildFake())
        expect(page.locator("#plbGuide")).to_be_hidden()
        page.click("#plbBuild")
        expect(page.locator("#plbGuide")).to_be_visible()
        # A build that kept no option has no guidance.
        fake = BuildFake()
        fake.build_answer = lambda body: (200, build_reply([]))
        page = self._open(fake)
        page.click("#plbBuild")
        expect(page.locator("#plbBuildNote")).to_be_visible()
        expect(page.locator("#plbGuide")).to_be_hidden()

    def test_an_out_of_date_build_keeps_its_guidance_marked_like_the_tabs(self) -> None:
        page, _ = self._built()
        expect(page.locator("#plbGuide")).to_be_visible()
        self.assertEqual(page.get_attribute("#plbGuide", "data-stale"), None)
        page.click("#plbTable tbody[data-group='new'] [data-act='remove'][data-code='AI342']")
        expect(page.locator("#plbGuide")).to_be_visible()
        self.assertEqual(page.get_attribute("#plbGuide", "data-stale"), "changed")
        self.assertEqual(page.get_attribute("#plbStrip", "data-stale"), "changed")
        self.assertTrue(self._guide_rows(page)["Suggested"].startswith("C1"))

    # ── 7. The filled button ────────────────────────────────────────────────────────────
    def test_exactly_one_button_is_filled_and_it_follows_the_state(self) -> None:
        page = self._open(BuildFake())
        state = self._buttons(page)
        self.assertEqual(
            (state["build"]["filled"], state["review"]["filled"]), (True, False), state
        )
        self.assertEqual(state["build"]["text"], "Build options")
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text("Rebuild")
        # A build that can be applied: Review and apply is the filled one.
        state = self._buttons(page)
        self.assertEqual(
            (state["build"]["filled"], state["review"]["filled"]), (False, True), state
        )
        self.assertFalse(state["review"]["disabled"])
        # The Registered tab: nothing to apply, both outlines.
        page.click("#plbTabRegistered")
        state = self._buttons(page)
        self.assertEqual(
            (state["build"]["filled"], state["review"]["filled"]), (False, False), state
        )
        self.assertTrue(state["review"]["disabled"])
        # Out of date: Rebuild is the outline in the brand colour and Review is disabled.
        page.click("#plbTab_B1")
        page.click("#plbTable tbody[data-group='new'] [data-act='remove'][data-code='AI342']")
        state = self._buttons(page)
        self.assertEqual(
            (state["build"]["filled"], state["review"]["filled"]), (False, False), state
        )
        self.assertTrue(state["review"]["disabled"])
        self.assertEqual(
            page.evaluate("() => getComputedStyle(document.getElementById('plbBuild')).color"),
            page.evaluate("() => getComputedStyle(document.getElementById('plbEditRules')).color"),
        )
        # Rebuilding gives the fill back to Review and apply.
        page.click("#plbBuild")
        expect(page.locator("#plbGuide")).not_to_have_attribute("data-stale", re.compile("."))
        state = self._buttons(page)
        self.assertEqual(
            (state["build"]["filled"], state["review"]["filled"]), (False, True), state
        )

    def test_a_comparison_only_build_has_no_filled_button(self) -> None:
        page = self._open(BuildFake())
        self._rules(page, prog=False)
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text("Rebuild")
        state = self._buttons(page)
        self.assertEqual(
            (state["build"]["filled"], state["review"]["filled"]), (False, False), state
        )
        self.assertTrue(state["review"]["disabled"])

    # ── 8. Planned blocks of the week open the detail ───────────────────────────────────
    def test_a_planned_block_opens_that_courses_detail_by_click_and_by_keyboard(self) -> None:
        page, _ = self._built()
        blocks = page.locator("#plbWeek button.plb-b[data-code='AI331']")
        self.assertGreaterEqual(blocks.count(), 1)
        self.assertEqual(page.locator("#plbWeek .plb-b[data-kind='new']:not(button)").count(), 0)
        # A registered block stays a picture.
        self.assertEqual(
            page.locator("#plbWeek .plb-b[data-kind='reg']").first.evaluate("n => n.tagName"), "DIV"
        )
        expect(
            page.get_by_role("button", name=re.compile(r"^AI331 Machine Learning, details"))
        ).to_have_count(blocks.count())
        expect(page.locator("#plbDetail")).to_be_hidden()
        blocks.first.click()
        expect(page.locator("#plbDetail")).to_be_visible()
        expect(page.locator("#plbDetailTitle")).to_contain_text("AI331")
        # Its blocks are marked while the panel is open; the other course's are not.
        marked = page.eval_on_selector_all(
            "#plbWeek .plb-b[data-open='true']", "n => n.map(x => x.dataset.code)"
        )
        self.assertEqual(marked, ["AI331"] * blocks.count())
        self.assertEqual(blocks.first.get_attribute("aria-expanded"), "true")
        # Closing it takes the marks away.
        page.click("#plbDetail [data-act='dt-close']")
        expect(page.locator("#plbDetail")).to_be_hidden()
        self.assertEqual(page.locator("#plbWeek .plb-b[data-open='true']").count(), 0)
        # By keyboard: focus the other course's block and press Enter, then Space on a third.
        other = page.locator("#plbWeek button.plb-b[data-code='AI342']").first
        other.focus()
        page.keyboard.press("Enter")
        expect(page.locator("#plbDetailTitle")).to_contain_text("AI342")
        self.assertEqual(
            page.eval_on_selector_all(
                "#plbWeek .plb-b[data-open='true']", "n => n.map(x => x.dataset.code)"
            ),
            ["AI342"],
        )
        page.keyboard.press("Escape")
        expect(page.locator("#plbDetail")).to_be_hidden()
        page.locator("#plbWeek button.plb-b[data-code='AI331']").first.focus()
        page.keyboard.press("Space")
        expect(page.locator("#plbDetailTitle")).to_contain_text("AI331")

    def test_the_marks_follow_a_redraw_of_the_week_and_a_course_taken_out_of_the_plan(self) -> None:
        page, _ = self._built()
        page.locator("#plbWeek button.plb-b[data-code='AI331']").first.click()
        page.click("#plbTab_B1")  # another option redraws the week; AI331 is in B1 too
        self.assertEqual(
            page.eval_on_selector_all(
                "#plbWeek .plb-b[data-open='true']", "n => n.map(x => x.dataset.code)"
            ),
            ["AI331", "AI331"],
        )
        page.click("#plbDetail [data-act='dt-remove']")
        # The course is out of the plan: its blocks are pictures now, not controls.
        self.assertEqual(page.locator("#plbWeek button.plb-b[data-code='AI331']").count(), 0)

    def test_closing_the_panel_returns_the_keyboard_to_where_it_was_opened_from(self) -> None:
        page, _ = self._built()
        blocks = page.locator("#plbWeek button.plb-b[data-code='AI331']")
        self.assertGreaterEqual(blocks.count(), 2)
        # From a week block: back to that block, not to the first one, and not to the table.
        blocks.nth(1).focus()
        page.keyboard.press("Enter")
        expect(page.locator("#plbDetailTitle")).to_be_focused()
        page.keyboard.press("Escape")
        expect(page.locator("#plbDetail")).to_be_hidden()
        expect(blocks.nth(1)).to_be_focused()
        # From the row: back to the row's button, as before.
        row = page.locator("#plbTable [data-act='detail'][data-code='AI331']")
        row.click()
        expect(page.locator("#plbDetailTitle")).to_be_focused()
        page.keyboard.press("Escape")
        expect(row).to_be_focused()

    def test_a_redraw_by_itself_leaves_the_keyboard_on_its_week_block(self) -> None:
        # Thirty minutes on, the once-a-minute check marks the build out of date and redraws the
        # week: the block the keyboard was on is a new node.
        page, _ = self._built(clock=True)
        blocks = page.locator("#plbWeek button.plb-b[data-code='AI331']")
        blocks.nth(1).focus()
        page.evaluate(
            "() => { document.querySelectorAll('#plbWeek button.plb-b').forEach("
            "(b) => { b.dataset.probe = '1'; }); }"
        )
        page.clock.fast_forward(31 * 60 * 1000)
        expect(page.locator("#plbStrip")).to_have_attribute("data-stale", "old")
        self.assertEqual(page.locator("#plbWeek button.plb-b[data-probe]").count(), 0)
        expect(blocks.nth(1)).to_be_focused()

    def test_a_planned_block_in_arabic_is_named_in_arabic_order(self) -> None:
        page, _ = self._built(language="ar")
        label = page.locator("#plbWeek button.plb-b[data-code='AI331']").first.get_attribute(
            "aria-label"
        )
        self.assertTrue(label.startswith("AI331 Machine Learning, التفاصيل"), label)

    # ── 9. Nothing overflows ────────────────────────────────────────────────────────────
    def _longest(self, language, width, font):
        """Three-digit section labels, nine-character course codes, long Arabic names, two nearly
        full sections, a fallback, and a status that lists the courses it left out."""
        from tests import planner_board_support as support

        names = (
            (
                "AI و تعلم الآلة المتقدم للأنظمة الذكية الموزعة",
                "نظم قواعد البيانات الضخمة والتحليلات",
            ),
            (
                "Advanced Distributed Machine Learning Systems",
                "Large Scale Database Systems and Analytics",
            ),
        )[0 if language == "ar" else 1]
        codes = ["CSAI33101", "CSAI34202", "CSAI45303", "CSAI46404"]
        recs = [support._rec(code, names[i % 2], "Eligible") for i, code in enumerate(codes)]

        def context(body):
            payload = support.context_for(
                str(body["student_id"]), eligibility_mode=body["eligibility_mode"]
            )
            payload["recommendations"] = recs
            return 200, payload

        sections = [
            (901, codes[0], "101", 36, 40),
            (902, codes[0], "102", 10, 40),
            (903, codes[1], "201", 37, 40),
            (904, codes[1], "202", 10, 40),
        ]

        def option(name, tsids, days):
            placed = [
                support._mapping(
                    codes[0],
                    "101" if tsids[0] == 901 else "102",
                    tsids[0],
                    days[0],
                    "08:00",
                    "09:15",
                ),
                support._mapping(
                    codes[1],
                    "201" if tsids[1] == 903 else "202",
                    tsids[1],
                    days[1],
                    "10:00",
                    "11:15",
                ),
            ]
            return support._option(
                name,
                placed,
                unscheduled=[
                    {"course_code": c, "reason_code": "ALL_SECTIONS_CLASH"} for c in codes[2:]
                ],
            )

        options = [
            option("B1", (901, 903), (["SUN", "MON"], ["TUE"])),
            option("A1", (902, 904), (["SUN", "MON", "TUE"], ["WED"])),
        ]
        fake = BuildFake()
        fake.context_answer = context
        fake.catalog_payload = catalog_with(*sections)
        fake.build_answer = lambda body: (200, support.build_reply(options))
        page = self._open(fake, language=language, width=width, font=font, load=False)
        self._fetch(page, support.STUDENT_ID)
        expect(page.locator("#plbTable tbody[data-group='new']")).to_be_visible()
        self._wait_for(page, lambda: len(fake.catalog_bodies) >= 1)
        page.click("#plbBuild")
        expect(page.locator("#plbGuide")).to_be_visible()
        return page

    def test_the_longest_guidance_wraps_at_every_width_in_both_languages_and_fonts(self) -> None:
        for language in ("en", "ar"):
            for font in ("app", "wide"):
                for width in (1440, 1152, 390):
                    page = self._longest(language, width, font)
                    where = (language, font, width)
                    rows = self._guide_rows(page)
                    self.assertEqual(len(rows), 3, (where, rows))  # Suggested, Watch, Status
                    self.assertEqual(page.evaluate(OVERFLOWS), [], where)
                    self.assertEqual(page.evaluate(GUIDE_FIT), [], where)
                    # The text is whole: nothing is cut off with an ellipsis.
                    self.assertEqual(
                        page.evaluate(
                            "() => [...document.querySelectorAll('#plbGuide *')].filter(n => getComputedStyle(n).textOverflow === 'ellipsis').length"
                        ),
                        0,
                        where,
                    )
                    self.assertIn(
                        "CSAI33101", rows["Watch" if language == "en" else "انتبه"], where
                    )
                    page.close()

"""The new planner screen, part four, in a real browser: the "Projected graduation" band.

The same synthetic student and fakes as the other parts (tests/planner_board_support.py). The
projection endpoint is mocked: READY is the mock-up's example (the registered week finishes in
Term 1, 1450 after five terms; B1 in four, one term sooner; B2 in the same term; B3 one term later
with a term in which no course can be taken; C1 sooner; C2 cannot be resolved).
"""

from __future__ import annotations

import copy
import pathlib
import re

from tests.planner_board_support import (
    FIVE,
    NO_FORECAST,
    OTHER_ID,
    OVERFLOWS,
    READY,
    REG_FORECAST,
    UNRESOLVED,
    BuildFake,
    PlannerBoardCase,
    _mapping,
    _option,
    build_reply,
    expect,
    forecast,
    projection_reply,
)

EXPIRED = "Build the options again to refresh the projection."
LOADING = "Calculating projected completion…"
NONE = "Build options to see the projection."
SESSION = "Your session has expired. Sign in again, then retry."

# ── measuring the runway (the fit tests) ─────────────────────────────────────
_GAPS = """
  const g = document.getElementById('plbGrad');
  const runway = g.querySelector('.plb-rw');
  const band = g.getBoundingClientRect();
  const textBox = (n) => { const r = document.createRange(); r.selectNodeContents(n); return r.getBoundingClientRect(); };
  const least = (nodes) => {
    const b = nodes.filter((n) => n.textContent.trim()).map(textBox).sort((x, y) => x.left - y.left);
    let m = 1e9;
    for (let i = 1; i < b.length; i += 1) m = Math.min(m, b[i].left - b[i - 1].right);
    return m;
  };
  const cells = [...runway.querySelectorAll('.plb-ax')];
  const axisGap = () => least(cells);
  const tickGap = () => Math.min(
    least([...runway.querySelectorAll('.plb-rwl--opt .plb-tk')]),
    least([...runway.querySelectorAll('.plb-rwl--reg .plb-tk')]));
  const insideBand = (n) => { const r = n.getBoundingClientRect(); return r.left >= band.left - 1 && r.right <= band.right + 1; };
  const inside = () => [...runway.querySelectorAll('.plb-rl, .plb-land, .plb-land > *')].every(insideBand);
  const isRow = () => getComputedStyle(runway).display === 'grid';
  const won = runway.querySelector('.plb-rwl--opt .plb-won');
  const wonState = () => {
    if (!won) return {oneLine: null, wonInside: null};
    const r = won.getBoundingClientRect();
    return {oneLine: r.height < parseFloat(getComputedStyle(won).lineHeight) * 1.5, wonInside: insideBand(won)};
  };
"""
MEASURE = (
    "() => {"
    + _GAPS
    + """
  const unit = runway.querySelector('.plb-u');
  return {
    row: isRow(), fit: runway.dataset.fit || '', unit: runway.dataset.unit || '',
    unitShown: !!unit && getComputedStyle(unit).display !== 'none',
    columns: cells.length,
    labelled: cells.map((c, i) => (c.textContent.trim() ? i : -1)).filter((i) => i >= 0),
    axisGap: axisGap(), tickGap: tickGap(), inside: inside(),
    result: won ? won.dataset.result : null,
    size: won ? (won.dataset.size || '') : null,
    fontSize: won ? getComputedStyle(won).fontSize : null,
    ...wonState(),
  };
}"""
)
# The band is given a width (null = the page's own), and the script is left the frames it needs
# to notice and fit again.
RESIZE = """async (w) => {
  const g = document.getElementById('plbGrad');
  g.style.width = w === null ? '' : w + 'px';
  g.style.justifySelf = w === null ? '' : 'start';
  for (let i = 0; i < 3; i += 1) await new Promise((done) => requestAnimationFrame(done));
}"""
# What one step more would have cost. Each changes the runway; a redraw puts it back.
PROBE_LABELS = (
    "(step) => {"
    + _GAPS
    + """
  cells.forEach((c, i) => { c.textContent = c.dataset.term && i % step === 0 ? c.dataset.term : ''; });
  return axisGap();
}"""
)
PROBE_UNIT = "() => {" + _GAPS + " delete runway.dataset.unit; return tickGap(); }"
PROBE_ROW = (
    "() => {"
    + _GAPS
    + """
  delete runway.dataset.fit;
  runway.dataset.unit = 'off';
  cells.forEach((c, i) => { c.textContent = c.dataset.term && i % 2 === 0 ? c.dataset.term : ''; });
  return {row: isRow(), tickGap: tickGap(), axisGap: axisGap(), inside: inside()};
}"""
)
PROBE_SIZE = "(size) => {" + _GAPS + " won.dataset.size = size; return wonState(); }"
# One style element whose rules exaggerate a text; replaced on each call.
STRESS = """(css) => {
  let style = document.getElementById('plbStress');
  if (!style) { style = document.createElement('style'); style.id = 'plbStress'; document.head.append(style); }
  style.textContent = css;
}"""
TEXT_WIDTH = """(selector) => {
  const r = document.createRange();
  r.selectNodeContents(document.querySelector(selector));
  return r.getBoundingClientRect().width;
}"""
# The result may never sit on top of a tick of its own line.
TOUCHES_A_TICK = """() => {
  const line = document.querySelector('#plbGrad .plb-rwl--opt');
  const won = line.querySelector('.plb-won');
  if (!won) return [];
  const w = won.getBoundingClientRect();
  return [...line.querySelectorAll('.plb-tk')].map((t) => t.getBoundingClientRect())
    .filter((t) => !(w.left >= t.right - 1 || w.right <= t.left + 1 || w.bottom <= t.top + 1 || w.top >= t.bottom - 1))
    .map((t) => [Math.round(t.left), Math.round(t.top)]);
}"""


def _reply(**options):
    """READY with some options replaced."""
    reply = copy.deepcopy(READY)
    reply["options"].update(options)
    return reply


class PlannerBoardGraduationBrowserTests(PlannerBoardCase):
    # The band is read for option B1; the order of the options is not what these tests are about.
    start_on = "B1"

    # ── helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _wait_for(page, predicate, what="the request") -> None:
        for _ in range(250):
            if predicate():
                return
            page.wait_for_timeout(20)
        raise AssertionError(f"timed out waiting for {what}")

    def _held(self, **kwargs):
        """A board whose first projection request is held back; returns (page, fake)."""
        fake = BuildFake()
        fake.hold_projection = 1
        page = self._open(fake, **kwargs)
        page.click("#plbBuild")
        expect(page.locator("#plbGradLine")).to_have_text(
            LOADING if kwargs.get("language", "en") == "en" else "جارٍ حساب الإكمال المتوقع…"
        )
        self._wait_for(page, lambda: len(fake.pending_projection) == 1)
        self._start(page)
        return page, fake

    def _more(self, page, name: str) -> None:
        self._pick(page, name)  # a tab, or one of "More options"

    def _line(self, page, kind: str) -> dict:
        base = f"#plbGrad .plb-rwl--{kind}"
        return {
            "num": self._grad(page, f".plb-rwl--{kind} .plb-num"),
            "word": self._grad(page, f".plb-rwl--{kind} .plb-st b"),
            "label": self._grad(page, f".plb-rwl--{kind} .plb-mic"),
            "ticks": self._grad(page, f".plb-rwl--{kind} .plb-tk"),
            "won": self._grad(page, f".plb-rwl--{kind} .plb-won"),
            "land": self._grad(page, f".plb-rwl--{kind} .plb-land b"),
            "units": page.locator(f"{base} .plb-u").count(),
            "ends": page.locator(f"{base} .plb-tk--end").count(),
        }

    @staticmethod
    def _boxes(page, selector: str) -> list[dict]:
        return page.eval_on_selector_all(
            selector,
            "ns => ns.map(n => { const r = n.getBoundingClientRect();"
            " return {left: r.left, right: r.right, top: r.top, bottom: r.bottom, x: (r.left + r.right) / 2}; })",
        )

    # ── before, during and without a projection ─────────────────────────────
    def test_the_band_says_so_before_any_build(self) -> None:
        fake = BuildFake()
        page = self._open(fake)
        expect(page.locator("#plbGradLine")).to_have_text(NONE)
        self.assertEqual(page.get_attribute("#plbGradLine", "role"), "status")
        self.assertEqual(page.locator("#plbGrad .plb-rw").count(), 0)
        self.assertTrue(page.locator("#plbGradWhyBtn").is_hidden())
        self.assertTrue(page.locator("#plbGradRetry").is_hidden())
        self.assertEqual(fake.projection_bodies, [])

    def test_loading_line_while_the_reply_is_held_then_the_runway(self) -> None:
        page, fake = self._held()
        self.assertEqual(page.locator("#plbGrad .plb-rw").count(), 0)
        self.assertEqual(page.get_attribute("#plbGrad", "data-state"), "loading")
        fake.pending_projection.pop().fulfill(json=READY)
        expect(page.locator("#plbGradLine")).to_be_hidden()
        self.assertEqual(self._text(page, "#plbGradLine"), "")
        self.assertEqual(page.locator("#plbGrad .plb-rw").count(), 1)

    def test_the_request_carries_the_builds_token(self) -> None:
        page, fake = self._projected()
        self.assertEqual(fake.projection_bodies, [{"token": "token", "add": ["STAT305", "CS351"]}])
        fake.build_answer = lambda body: (200, {**FIVE, "graduation_projection_token": "second"})
        page.click("#plbBuild")
        self._wait_for(page, lambda: len(fake.projection_bodies) == 2)
        self.assertEqual(
            fake.projection_bodies[1], {"token": "second", "add": ["STAT305", "CS351"]}
        )

    def test_no_request_without_options_or_without_a_token(self) -> None:
        for reply in (
            build_reply([]),
            {**FIVE, "graduation_projection_token": ""},
            {k: v for k, v in FIVE.items() if k != "graduation_projection_token"},
            {**FIVE, "graduation_projection_token": 7},
        ):
            fake = BuildFake()
            fake.build_answer = lambda body, reply=reply: (200, reply)
            page, fake = self._built(fake)
            page.wait_for_timeout(150)
            self.assertEqual(fake.projection_bodies, [])
            expect(page.locator("#plbGradLine")).to_have_text(NONE)
            self.assertEqual(page.locator("#plbGrad .plb-rw").count(), 0)

    # ── the runway ───────────────────────────────────────────────────────────
    def test_runway_of_the_selected_option_against_the_registered_week(self) -> None:
        page, _fake = self._projected()
        opt = self._line(page, "opt")
        self.assertEqual(opt["num"], ["4"])
        self.assertEqual(opt["word"], ["terms"])
        self.assertEqual(opt["label"], ["Option B1"])
        self.assertEqual(opt["ticks"], ["15cr", "18", "18", "15"])
        self.assertEqual(opt["units"], 1)
        self.assertEqual(
            page.locator("#plbGrad .plb-rwl--opt .plb-tk:first-child .plb-u").count(), 0
        )
        self.assertEqual(
            page.locator("#plbGrad .plb-rwl--opt .plb-tk").first.locator(".plb-u").count(), 1
        )
        self.assertEqual(opt["ends"], 1)
        self.assertIn(
            "plb-tk--end",
            page.locator("#plbGrad .plb-rwl--opt .plb-tk").last.get_attribute("class"),
        )
        self.assertEqual(opt["won"], ["1 term sooner"])
        self.assertEqual(opt["land"], ["Term 2, 1449"])
        reg = self._line(page, "reg")
        self.assertEqual(reg["num"], ["5"])
        self.assertEqual(reg["label"], ["As registered"])
        self.assertEqual(reg["ticks"], ["6cr", "18", "18", "18", "6"])
        self.assertEqual(reg["units"], 1)
        self.assertEqual(reg["won"], [])
        self.assertEqual(reg["land"], ["Term 1, 1450"])
        self.assertEqual(
            self._grad(page, ".plb-ax"), ["1448/1", "1448/2", "1449/1", "1449/2", "1450/1"]
        )
        self.assertEqual(self._grad(page, ".plb-ax--now"), ["1448/1"])
        self.assertEqual(page.locator("#plbGrad .plb-ax--now").count(), 1)
        self.assertEqual(self._grad(page, ".plb-axn"), ["Credits per term"])
        # Every tick says its term and credits.
        self.assertEqual(
            page.eval_on_selector_all(
                "#plbGrad .plb-rwl--opt .plb-tk", "ns => ns.map(n => n.title)"
            ),
            ["1448/1 · 15 cr", "1448/2 · 18 cr", "1449/1 · 18 cr", "1449/2 · 15 cr"],
        )

    def test_one_axis_for_both_lines_in_the_row_layout(self) -> None:
        page, _fake = self._projected()
        self.assertEqual(
            page.evaluate("getComputedStyle(document.querySelector('#plbGrad .plb-rw')).display"),
            "grid",
        )
        opt = self._boxes(page, "#plbGrad .plb-rwl--opt .plb-tk")
        reg = self._boxes(page, "#plbGrad .plb-rwl--reg .plb-tk")
        axis = self._boxes(page, "#plbGrad .plb-ax")
        for i, box in enumerate(opt):
            self.assertAlmostEqual(box["x"], reg[i]["x"], delta=1)
            self.assertAlmostEqual(box["x"], axis[i]["x"], delta=1)
        self.assertAlmostEqual(reg[4]["x"], axis[4]["x"], delta=1)
        # The shorter line's landing uses the column it leaves free.
        landing = self._boxes(page, "#plbGrad .plb-rwl--opt .plb-land")[0]
        self.assertAlmostEqual(landing["left"], reg[4]["left"], delta=1)
        # The option's ticks are taller than the quiet line's.
        self.assertGreater(opt[0]["bottom"] - opt[0]["top"], reg[0]["bottom"] - reg[0]["top"])

    def test_sooner_later_same_and_no_result(self) -> None:
        page, _fake = self._projected()
        # Beside the guidance column the band is narrow; given room to spare the good news is the
        # largest text of the page (how it steps down is the fit tests' business).
        page.evaluate(RESIZE, self.ROOMY)
        size = "n => getComputedStyle(n).fontSize"
        color = "n => getComputedStyle(n).color"
        won = page.locator("#plbGrad .plb-rwl--opt .plb-won")
        self.assertEqual(won.get_attribute("data-result"), "sooner")
        sooner = (won.evaluate(size), won.evaluate(color))
        self._pick(page, "B3")
        self.assertEqual(self._grad(page, ".plb-rwl--opt .plb-won"), ["1 term later"])
        self.assertEqual(won.get_attribute("data-result"), "later")
        # Good news is the largest text of the page; bad news is bold warning ink at reading size,
        # which always fits beside its own line.
        self.assertEqual(sooner[0], "22px")
        self.assertEqual(won.evaluate(size), "15px")
        self.assertEqual(won.evaluate("n => getComputedStyle(n).fontWeight"), "700")
        self.assertNotEqual(won.evaluate(color), sooner[1])
        later = won.evaluate(color)
        self._pick(page, "B2")
        self.assertEqual(later, page.locator("#plbTab_B2 .plb-tab-warn").evaluate(color))
        self.assertEqual(self._grad(page, ".plb-rwl--opt .plb-won"), ["Same term"])
        self.assertEqual(won.get_attribute("data-result"), "same")
        self.assertEqual(won.evaluate("n => getComputedStyle(n).fontWeight"), "600")
        self.assertLess(float(won.evaluate(size)[:-2]), float(sooner[0][:-2]))
        self.assertEqual(self._grad(page, ".plb-rwl--opt .plb-land b"), ["Term 1, 1450"])
        # A screen reader hears the line alone: its sentence says which term is meant.
        self.assertIn(
            "completes Term 1, 1450, the same term as registered. Credits per term:",
            self._text(page, "#plbGrad .plb-rwl--opt .sr-only"),
        )

    def test_a_null_difference_shows_no_result(self) -> None:
        page, _fake = self._projected(_reply(B1=forecast([15, 18, 18, 15], delta=None)))
        self.assertEqual(page.locator("#plbGrad .plb-won").count(), 0)
        self.assertEqual(self._grad(page, ".plb-rwl--opt .plb-land b"), ["Term 2, 1449"])
        self.assertEqual(self._grad(page, ".plb-rwl--opt .plb-num"), ["4"])

    def test_another_tab_redraws_its_own_figures(self) -> None:
        page, _fake = self._projected()
        self._pick(page, "B2")
        opt = self._line(page, "opt")
        self.assertEqual((opt["num"], opt["label"]), (["5"], ["Option B2"]))
        self.assertEqual(opt["ticks"], ["12cr", "18", "18", "18", "6"])
        self.assertEqual(self._grad(page, ".plb-ax")[-1], "1450/1")
        self.assertEqual(self._line(page, "reg")["num"], ["5"])
        self._pick(page, "B3")
        self.assertEqual(self._line(page, "opt")["num"], ["6"])
        self.assertEqual(len(self._grad(page, ".plb-ax")), 6)
        # The band follows the tab without announcing itself.
        self.assertEqual(self._text(page, "#plbGradLine"), "")

    def test_the_registered_tab_shows_one_line(self) -> None:
        page, _fake = self._projected()
        page.click("#plbTabRegistered")
        self.assertEqual(page.locator("#plbGrad .plb-rwl--opt").count(), 0)
        self.assertEqual(page.locator("#plbGrad .plb-rwl--reg").count(), 1)
        self.assertEqual(page.locator("#plbGrad .plb-won").count(), 0)
        self.assertEqual(self._line(page, "reg")["ticks"], ["6cr", "18", "18", "18", "6"])
        self.assertEqual(len(self._grad(page, ".plb-ax")), 5)
        self.assertTrue(page.locator("#plbGradWhyBtn").is_hidden())
        self.assertTrue(page.locator("#plbGradWhy").is_hidden())

    def test_a_waiting_term_shows_a_dash_and_says_why(self) -> None:
        page, _fake = self._projected()
        self._pick(page, "B3")
        ticks = page.locator("#plbGrad .plb-rwl--opt .plb-tk")
        self.assertEqual(ticks.nth(1).text_content(), "–")
        self.assertEqual(ticks.nth(1).get_attribute("title"), "No course can be taken in this term")
        self.assertIn("plb-tk--wait", ticks.nth(1).get_attribute("class"))
        self.assertEqual(
            ticks.nth(1).evaluate("n => getComputedStyle(n).borderBottomStyle"), "dashed"
        )
        self.assertEqual(
            ticks.nth(0).evaluate("n => getComputedStyle(n).borderBottomStyle"), "solid"
        )
        self.assertEqual(ticks.nth(2).text_content(), "18")

    def test_assumptions_use_the_replys_cap_and_the_retained_sentence(self) -> None:
        page, _fake = self._projected()
        self.assertEqual(
            self._text(page, "#plbGradAsm"),
            "Includes this term (1448/1) · assumes courses are passed · up to 18 cr per future term",
        )
        self.assertTrue(page.locator("#plbGradRetained").is_hidden())
        for kind, sentence in (
            ("expected_plan", "This option includes retained courses from the expected plan."),
            (
                "working_timetable",
                "This option includes retained courses from the working timetable.",
            ),
        ):
            reply = {**READY, "future_max_credits_per_term": 17, "retained_baseline_kind": kind}
            page, _fake = self._projected(reply)
            self.assertIn("up to 17 cr per future term", self._text(page, "#plbGradAsm"))
            self.assertEqual(self._text(page, "#plbGradRetained"), sentence)
        page, _fake = self._projected({**READY, "retained_baseline_kind": "registered_timetable"})
        self.assertTrue(page.locator("#plbGradRetained").is_hidden())

    # ── terms on every tab ───────────────────────────────────────────────────
    def test_terms_lead_the_facts_of_every_tab(self) -> None:
        page, _fake = self._projected()
        self.assertEqual(self._tab_names(page), ["C1", "B1", "C2", "(registered)"])
        self.assertEqual(self._tab_lines(page, "C1")[1], "4 terms · 4 days · Thu free")
        self.assertEqual(self._tab_lines(page, "B1")[1], "4 terms · 4 days · Thu free")
        self.assertEqual(self._tab_lines(page, "C2")[1], "5 days · no free day")  # unresolved
        self.assertEqual(self._text(page, "#plbTabFacts1"), "5 terms · 4 days · Thu free")
        weight = page.locator("#plbTab_B1 .plb-tab-terms").evaluate(
            "n => getComputedStyle(n).fontWeight"
        )
        self.assertEqual(weight, "700")
        self.assertEqual(page.locator("#plbTabFacts1 b").count(), 1)
        page.click("#plbMoreBtn")
        self.assertEqual(
            self._text(page, "#plbMoreList [data-more='B3']"),
            "B3 · 15 cr · 6 terms · 5 days · no free day",
        )
        self.assertEqual(
            self._text(page, "#plbMoreList [data-more='B2']"),
            "B2 · 12 cr · 5 terms · 4 days · Thu free",
        )

    def test_no_terms_while_loading_or_when_a_forecast_is_not_complete(self) -> None:
        page, fake = self._held()
        self.assertEqual(self._tab_lines(page, "B1")[1], "4 days · Thu free")
        self.assertEqual(self._text(page, "#plbTabFacts1"), "4 days · Thu free")
        page.click("#plbMoreBtn")
        self.assertEqual(
            self._text(page, "#plbMoreList [data-more='B3']"),
            "B3 · 15 cr · 5 days · no free day",
        )
        fake.pending_projection.pop().fulfill(
            json=_reply(
                B1=UNRESOLVED, B2=NO_FORECAST, B3={**forecast([15, 18]), "status": "unavailable"}
            )
        )
        expect(page.locator("#plbGradLine")).to_be_hidden()
        for name in ("B1", "B2", "B3"):
            self._pick(page, name)
            self.assertNotIn("terms", self._tab_lines(page, name)[1])
        broken = copy.deepcopy(READY)
        broken["baseline"] = {**NO_FORECAST, "reason_code": "no_registered_timetable"}
        page, _fake = self._projected(broken)
        self.assertEqual(self._text(page, "#plbTabFacts1"), "4 days · Thu free")
        self.assertEqual(self._tab_lines(page, "B1")[1], "4 terms · 4 days · Thu free")

    # ── the states ───────────────────────────────────────────────────────────
    def test_each_state_has_its_line_and_the_retry_button_where_it_belongs(self) -> None:
        cases = [
            (
                "expired",
                lambda route: route.fulfill(status=409, json={"error": "expired"}),
                EXPIRED,
                False,
            ),
            (
                "unavailable",
                lambda route: route.fulfill(status=500, json={}),
                "Projection unavailable",
                True,
            ),
            (
                "unavailable",
                lambda route: route.fulfill(status=200, body="not json"),
                "Projection unavailable",
                True,
            ),
            (
                "unavailable",
                lambda route: route.fulfill(status=200, json={"ok": True, "baseline": {}}),
                "Projection unavailable",
                True,
            ),
            (
                "unavailable",
                lambda route: route.fulfill(
                    status=200, json={"ok": False, "baseline": {}, "options": {}}
                ),
                "Projection unavailable",
                True,
            ),
            (
                "unavailable",
                lambda route: route.fulfill(status=400, json={"error": "bad"}),
                "Projection unavailable",
                True,
            ),
            ("unavailable", lambda route: route.abort(), "Projection unavailable", True),
            (
                "busy",
                lambda route: route.fulfill(status=429, json={"error": "slow down"}),
                "Too many requests. Try again in a moment.",
                True,
            ),
            ("signedout", lambda route: route.fulfill(status=401, json={}), SESSION, False),
        ]
        for state, raw, line, retry in cases:
            fake = BuildFake()
            fake.projection_raw = raw
            page, fake = self._built(fake)
            expect(page.locator("#plbGradLine")).to_have_text(line)
            self.assertEqual(page.get_attribute("#plbGrad", "data-state"), state)
            self.assertEqual(page.locator("#plbGradRetry").is_visible(), retry, line)
            self.assertEqual(page.locator("#plbGrad .plb-rw").count(), 0)
            if retry:
                self.assertEqual(self._text(page, "#plbGradRetry"), "Try again")
                self.assertIn("plb-lk", page.get_attribute("#plbGradRetry", "class"))

    def test_try_again_sends_the_token_again_and_recovers(self) -> None:
        fake = BuildFake()
        answers = [(500, {}), (200, READY)]
        fake.projection_answer = lambda body: answers.pop(0)
        page, fake = self._built(fake)
        expect(page.locator("#plbGradRetry")).to_be_visible()
        fake.hold_projection = 1
        page.click("#plbGradRetry")
        expect(page.locator("#plbGradLine")).to_have_text(LOADING)
        self._wait_for(page, lambda: len(fake.pending_projection) == 1)
        self.assertTrue(
            page.evaluate("document.getElementById('plbGrad').contains(document.activeElement)")
        )
        status, payload = answers.pop(0)
        fake.pending_projection.pop().fulfill(status=status, json=payload)
        expect(page.locator("#plbGradLine")).to_be_hidden()
        body = {"token": "token", "add": ["STAT305", "CS351"]}
        self.assertEqual(fake.projection_bodies, [body, body])
        self.assertEqual(self._line(page, "opt")["num"], ["4"])
        self.assertTrue(
            page.evaluate("document.getElementById('plbGrad').contains(document.activeElement)")
        )
        self.assertEqual(page.locator("#plbGradRetry").is_visible(), False)

    def test_try_again_from_busy_and_a_second_failure(self) -> None:
        fake = BuildFake()
        answers = [(429, {"error": "slow"}), (429, {"error": "slow"}), (200, READY)]
        fake.projection_answer = lambda body: answers.pop(0)
        page, fake = self._built(fake)
        expect(page.locator("#plbGradLine")).to_have_text(
            "Too many requests. Try again in a moment."
        )
        page.click("#plbGradRetry")
        self._wait_for(page, lambda: len(fake.projection_bodies) == 2)
        expect(page.locator("#plbGradRetry")).to_be_visible()
        page.click("#plbGradRetry")
        expect(page.locator("#plbGradLine")).to_be_hidden()
        self.assertEqual(len(fake.projection_bodies), 3)

    # ── a reply is believed only for the build it was asked for ─────────────
    def test_a_reply_for_another_student_never_writes(self) -> None:
        page, fake = self._held()
        self._fetch(page, OTHER_ID)
        expect(page.locator("#plbContext")).to_contain_text(OTHER_ID)
        expect(page.locator("#plbGradLine")).to_have_text(NONE)
        fake.pending_projection.pop().fulfill(json=READY)
        page.wait_for_timeout(150)
        expect(page.locator("#plbGradLine")).to_have_text(NONE)
        self.assertEqual(page.locator("#plbGrad .plb-rw").count(), 0)
        self.assertEqual(self._tab_names(page), ["(registered)"])
        self.assertEqual(self._text(page, "#plbTabFacts1"), "4 days · Thu free")

    def test_a_reply_for_a_rebuilt_build_never_writes(self) -> None:
        page, fake = self._held()
        fake.projection_answer = lambda body: (200, READY)
        old = copy.deepcopy(READY)
        old["baseline"] = forecast([6] * 9, kind="registered_timetable", reason_code=None)
        page.click("#plbBuild")
        self._wait_for(page, lambda: len(fake.projection_bodies) == 2)
        expect(page.locator("#plbGradLine")).to_be_hidden()
        self.assertEqual(self._line(page, "reg")["num"], ["5"])
        fake.pending_projection.pop().fulfill(json=old)
        page.wait_for_timeout(150)
        self.assertEqual(self._line(page, "reg")["num"], ["5"])
        self.assertEqual(page.locator("#plbGrad .plb-rw").count(), 1)

    def test_a_save_drops_the_band_and_a_late_reply_cannot_bring_it_back(self) -> None:
        page, fake = self._held()
        page.click("#plbReview")
        page.click("#plbApplyGo")
        expect(page.locator("#plbGradLine")).to_have_text(NONE)
        fake.pending_projection.pop().fulfill(json=READY)
        page.wait_for_timeout(150)
        expect(page.locator("#plbGradLine")).to_have_text(NONE)
        self.assertEqual(page.locator("#plbGrad .plb-rw").count(), 0)

    def test_a_reply_after_a_plan_change_still_shows_with_the_refresh_line(self) -> None:
        page, fake = self._held()
        self._rules(page, full=True)
        fake.pending_projection.pop().fulfill(json=READY)
        expect(page.locator("#plbGradLine")).to_be_hidden()
        self.assertEqual(page.locator("#plbGrad .plb-rw").count(), 1)
        self.assertEqual(self._text(page, "#plbGradStale"), EXPIRED)
        self.assertEqual(page.get_attribute("#plbGrad", "data-stale"), "changed")
        self.assertEqual(self._tab_lines(page, "B1")[1], "4 terms · 4 days · Thu free")
        # A fresh build clears the warning.
        page.click("#plbBuild")
        expect(page.locator("#plbGradStale")).to_be_hidden()
        self.assertIsNone(page.get_attribute("#plbGrad", "data-stale"))

    # ── a line that cannot be drawn ─────────────────────────────────────────
    def test_baseline_unavailable_or_unresolved_keeps_the_option_line_and_the_axis(self) -> None:
        for baseline, sentence in (
            (
                {**NO_FORECAST, "reason_code": "no_registered_timetable"},
                "No registrar registration for this term",
            ),
            ({**UNRESOLVED, "reason_code": None}, "Completion term unresolved"),
            ({**NO_FORECAST, "reason_code": "academic_plan_unavailable"}, "Projection unavailable"),
        ):
            reply = {**copy.deepcopy(READY), "baseline": baseline}
            reply["options"]["B1"] = forecast([15, 18, 18, 15], delta=-1)
            page, _fake = self._projected(reply)
            self.assertEqual(self._grad(page, ".plb-rwl--reg .plb-rwn"), [sentence])
            self.assertEqual(self._grad(page, ".plb-rwl--reg .plb-mic"), ["As registered"])
            self.assertEqual(page.locator("#plbGrad .plb-rwl--reg .plb-tk").count(), 0)
            self.assertEqual(self._line(page, "opt")["ticks"], ["15cr", "18", "18", "15"])
            self.assertEqual(len(self._grad(page, ".plb-ax")), 4)
            self.assertEqual(page.locator("#plbGrad .plb-won").count(), 0)
            self.assertEqual(page.locator("#plbGrad .plb-rwl--reg .plb-land").count(), 0)

    def test_an_option_that_is_not_complete_lists_its_reasons_open(self) -> None:
        reply = _reply(
            C2={
                **UNRESOLVED,
                "reasons": [{"code": "requirement_unresolved", "course_code": "AI480"}],
                "unresolved_requirements": [
                    {
                        "code": "AI480",
                        "missing_course_prerequisites": ["NEVER100"],
                        "credit_hour_gate": None,
                        "project_sequence_gate": None,
                    }
                ],
            },
            C1={**NO_FORECAST},
        )
        page, _fake = self._projected(reply)
        self._more(page, "C2")
        self.assertEqual(self._grad(page, ".plb-rwl--opt .plb-mic"), ["Option C2"])
        self.assertEqual(self._grad(page, ".plb-rwl--opt .plb-rwn"), ["Completion term unresolved"])
        self.assertEqual(page.locator("#plbGrad .plb-rwl--opt .plb-tk").count(), 0)
        self.assertEqual(self._line(page, "reg")["ticks"], ["6cr", "18", "18", "18", "6"])
        self.assertEqual(len(self._grad(page, ".plb-ax")), 5)
        self.assertTrue(page.locator("#plbGradWhyBtn").is_hidden())
        # The "could not be placed" reason is skipped: the blocked requirement says it already.
        self.assertEqual(
            self._grad(page, "#plbGradWhy li"), ["AI480 · missing prerequisites NEVER100"]
        )
        self.assertTrue(page.locator("#plbGradWhy").is_visible())
        self._more(page, "C1")
        self.assertEqual(self._grad(page, ".plb-rwl--opt .plb-rwn"), ["Projection unavailable"])
        self.assertEqual(
            self._grad(page, "#plbGradWhy li"), ["Section data is incomplete for this projection."]
        )

    # ── "Course-level reasons" ───────────────────────────────────────────────
    def _disclosure_reply(self):
        moved = {
            "code": "course_moved_earlier",
            "course_code": "AI331",
            "before": {"academic_year": 1449, "term": 1},
            "after": {"academic_year": 1448, "term": 2},
        }
        return _reply(
            B1=forecast([15, 18, 18, 15], delta=-1, reasons=[moved]),
            B2=forecast([12, 18, 18, 18, 6], delta=0, reasons=[]),
            B3=forecast(
                [15, 0, 18, 18, 18, 9],
                delta=1,
                waiting=(1,),
                reasons=[{"code": "course_added", "course_code": "AI342"}],
            ),
        )

    def test_the_reasons_disclosure(self) -> None:
        page, _fake = self._projected(self._disclosure_reply())
        button = page.locator("#plbGradWhyBtn")
        self.assertEqual(button.get_attribute("aria-controls"), "plbGradWhy")
        self.assertEqual(button.get_attribute("aria-expanded"), "false")
        self.assertIn("plb-lk", button.get_attribute("class"))
        self.assertTrue(page.locator("#plbGradWhy").is_hidden())
        self.assertIn("Course-level reasons", self._text(page, "#plbGradWhyBtn"))
        button.click()
        self.assertEqual(button.get_attribute("aria-expanded"), "true")
        self.assertEqual(
            self._grad(page, "#plbGradWhy li"), ["AI331 moves from Term 1, 1449 to Term 2, 1448."]
        )
        # It stays open across a tab change, with the other option's reasons.
        self._pick(page, "B3")
        self.assertEqual(button.get_attribute("aria-expanded"), "true")
        self.assertEqual(
            self._grad(page, "#plbGradWhy li"), ["This option includes AI342 in the planning term."]
        )
        # Nothing to list: no button at all.
        self._pick(page, "B2")
        self.assertTrue(button.is_hidden())
        self.assertTrue(page.locator("#plbGradWhy").is_hidden())
        page.click("#plbTab_B1")
        button.click()
        self.assertEqual(button.get_attribute("aria-expanded"), "false")
        self.assertTrue(page.locator("#plbGradWhy").is_hidden())

    def test_escape_closes_the_reasons_only_from_the_band(self) -> None:
        page, _fake = self._projected(self._disclosure_reply())
        page.click("#plbGradWhyBtn")
        expect(page.locator("#plbGradWhy")).to_be_visible()
        page.focus("#plbBuild")
        page.keyboard.press("Escape")
        expect(page.locator("#plbGradWhy")).to_be_visible()
        page.focus("#plbGradWhyBtn")
        page.keyboard.press("Escape")
        expect(page.locator("#plbGradWhy")).to_be_hidden()
        self.assertEqual(page.get_attribute("#plbGradWhyBtn", "aria-expanded"), "false")
        self.assertEqual(page.evaluate("document.activeElement.id"), "plbGradWhyBtn")

    def test_a_new_build_closes_the_reasons(self) -> None:
        page, fake = self._projected(self._disclosure_reply())
        page.click("#plbGradWhyBtn")
        expect(page.locator("#plbGradWhy")).to_be_visible()
        page.click("#plbBuild")
        expect(page.locator("#plbGradLine")).to_be_hidden()
        self.assertEqual(page.get_attribute("#plbGradWhyBtn", "aria-expanded"), "false")
        self.assertTrue(page.locator("#plbGradWhy").is_hidden())

    def test_every_reason_code_has_its_sentence_in_both_languages(self) -> None:
        moved = {
            "course_code": "AI331",
            "before": {"academic_year": 1449, "term": 1},
            "after": {"academic_year": 1448, "term": 2},
        }
        reasons = [
            {"code": "same_course_set"},
            {"code": "course_moved_earlier", **moved},
            {"code": "course_moved_later", **moved},
            {"code": "prerequisite_moved_earlier", **moved, "related_course_code": "AI451"},
            {"code": "prerequisite_moved_later", **moved, "related_course_code": "AI451"},
            {"code": "requirement_resolved", "course_code": "CS301"},
            {"code": "requirement_unresolved", "course_code": "AI499"},
            {"code": "course_added", "course_code": "AI342"},
            {"code": "course_removed", "course_code": "CS322"},
            {"code": "academic_plan_unavailable"},
            {"code": "no_registered_timetable"},
            {"code": "unsupported_planning_term"},
            {
                "code": "unmet_course_prerequisites",
                "course_code": "CS401",
                "missing_prerequisites": ["CS301", "CS302"],
            },
            {"code": "section_metadata_unavailable"},
            {"code": "a_code_from_the_future"},
        ]
        blocked = [
            {
                "code": "AI480",
                "missing_course_prerequisites": ["NEVER100", "X200"],
                "credit_hour_gate": {"remaining": 12},
                "project_sequence_gate": {"x": 1},
            },
            {
                "code": "AI481",
                "missing_course_prerequisites": [],
                "credit_hour_gate": {"remaining": 0},
                "project_sequence_gate": None,
            },
            {
                "code": "AI482",
                "missing_course_prerequisites": [],
                "credit_hour_gate": None,
                "project_sequence_gate": True,
            },
        ]
        reply = _reply(
            B1=forecast(
                [15, 18, 18, 15],
                delta=-1,
                reasons=reasons + [{"code": "requirement_unresolved", "course_code": "AI480"}],
                unresolved=blocked,
            )
        )
        expected = {
            "en": [
                "This option includes the same courses as current registration.",
                "AI331 moves from Term 1, 1449 to Term 2, 1448.",
                "AI331 moves from Term 1, 1449 to Term 2, 1448.",
                "Prerequisite AI331 moves from Term 1, 1449 to Term 2, 1448. Its dependent course AI451 also moves earlier.",
                "Prerequisite AI331 moves from Term 1, 1449 to Term 2, 1448. Its dependent course AI451 also moves later.",
                "CS301 can now be placed in the projection.",
                "AI499 could not be placed in the projection.",
                "This option includes AI342 in the planning term.",
                "This option omits CS322 from the planning term.",
                "The academic plan is unavailable.",
                "A registrar timetable is needed to compare graduation dates.",
                "Graduation forecasts are available for regular terms 1 and 2.",
                "CS401 requires CS301, CS302 before the planning term.",
                "Section data is incomplete for this projection.",
                "AI480 · missing prerequisites NEVER100, X200, 12 more credits required, project sequence requirement",
                "AI481 could not be placed in the projection.",
                "AI482 · project sequence requirement",
            ],
            "ar": [
                "يتضمن الخيار المقررات نفسها في التسجيل الحالي.",
                "انتقل AI331 من الفصل 1، 1449 إلى الفصل 2، 1448.",
                "انتقل AI331 من الفصل 1، 1449 إلى الفصل 2، 1448.",
                "انتقل المتطلب السابق AI331 من الفصل 1، 1449 إلى الفصل 2، 1448، وانتقل المقرر AI451 أيضًا إلى فصل أبكر.",
                "انتقل المتطلب السابق AI331 من الفصل 1، 1449 إلى الفصل 2، 1448، وانتقل المقرر AI451 أيضًا إلى فصل لاحق.",
                "أمكن إدراج CS301 في التوقع.",
                "تعذّر إدراج AI499 في التوقع.",
                "يتضمن هذا الخيار AI342 في فصل التخطيط.",
                "يخلو هذا الخيار من CS322 في فصل التخطيط.",
                "الخطة الدراسية غير متاحة.",
                "يلزم وجود جدول مسجّل رسميًا لمقارنة موعد التخرّج.",
                "يشمل توقع التخرّج الفصلين الدراسيين الأول والثاني فقط.",
                "لم تكتمل متطلبات CS401 قبل فصل التخطيط: CS301، CS302.",
                "بيانات إحدى الشعب غير كافية لحساب التوقع.",
                "AI480 · المتطلبات السابقة المتبقية NEVER100، X200، الساعات الإضافية المطلوبة 12، قيد تسلسل مقررات المشروع",
                "تعذّر إدراج AI481 في التوقع.",
                "AI482 · قيد تسلسل مقررات المشروع",
            ],
        }
        for language in ("en", "ar"):
            page, _fake = self._projected(reply, language=language)
            page.click("#plbGradWhyBtn")
            self.assertEqual(self._grad(page, "#plbGradWhy li"), expected[language], language)
            # Codes stay left-to-right islands inside the Arabic sentence.
            self.assertGreater(page.locator("#plbGradWhy bdi[dir='ltr']").count(), 10)

    # ── languages, columns and layouts ──────────────────────────────────────
    def test_arabic_counts_for_one_two_three_and_eleven_terms(self) -> None:
        for count, word, tab in (
            (1, "فصل", "فصل واحد"),
            (2, "فصلان", "فصلان"),
            (3, "فصول", "3 فصول"),
            (11, "فصلًا", "11 فصلًا"),
        ):
            reply = _reply(B1=forecast([9] * count, delta=count - 5))
            page, _fake = self._projected(reply, language="ar")
            self.assertEqual(self._grad(page, ".plb-rwl--opt .plb-num"), [str(count)])
            self.assertEqual(self._grad(page, ".plb-rwl--opt .plb-st b"), [word])
            self.assertEqual(self._grad(page, ".plb-rwl--opt .plb-mic"), ["الخيار B1"])
            self.assertTrue(
                self._tab_lines(page, "B1")[1].startswith(f"{tab} · "), self._tab_lines(page, "B1")
            )
            self.assertEqual(self._grad(page, ".plb-rwl--reg .plb-st b"), ["فصول"])
            self.assertEqual(self._grad(page, ".plb-rwl--reg .plb-mic"), ["كما هو مسجّل"])

    def test_arabic_band_text(self) -> None:
        page, _fake = self._projected(language="ar")
        self.assertEqual(self._grad(page, ".plb-rwl--opt .plb-won"), ["أبكر بفصل واحد"])
        self.assertEqual(self._grad(page, ".plb-rwl--opt .plb-land b"), ["الفصل 2، 1449"])
        self.assertEqual(self._grad(page, ".plb-axn"), ["الساعات في كل فصل"])
        self.assertEqual(
            self._text(page, "#plbGradAsm"),
            "يشمل هذا الفصل (1448/1) · يفترض اجتياز المقررات · حتى 18 ساعة في كل فصل لاحق",
        )
        self._pick(page, "B3")
        self.assertEqual(self._grad(page, ".plb-rwl--opt .plb-won"), ["أبعد بفصل واحد"])
        self._pick(page, "B2")
        self.assertEqual(self._grad(page, ".plb-rwl--opt .plb-won"), ["نفس الفصل"])
        # A screen reader hears the line alone: its sentence says which term is meant.
        self.assertIn(
            "، نفس فصل التسجيل الحالي.", self._text(page, "#plbGrad .plb-rwl--opt .sr-only")
        )

    def test_data_span_and_one_axis_cell_per_column(self) -> None:
        for columns, span in ((5, "s"), (6, "m"), (8, "m"), (9, "l"), (14, "l"), (15, "xl")):
            page, _fake = self._projected(_reply(B1=forecast([9] * columns, delta=0)), width=1440)
            self.assertEqual(page.get_attribute("#plbGrad .plb-rw", "data-span"), span, columns)
            self.assertEqual(
                page.eval_on_selector(
                    "#plbGrad .plb-rw", "n => n.style.getPropertyValue('--plb-cols')"
                ),
                str(max(columns, 5)),
            )
            labels = self._grad(page, ".plb-ax")
            self.assertEqual(len(labels), max(columns, 5))
            self.assertEqual(labels[0], "1448/1")
            self.assertTrue(page.locator("#plbGrad .plb-axn").is_visible())
            # The ticks keep a title, labelled or not.
            self.assertTrue(
                all(
                    page.eval_on_selector_all(
                        "#plbGrad .plb-rwl--opt .plb-tk", "ns => ns.map(n => !!n.title)"
                    )
                )
            )

    def test_the_columns_come_from_the_longer_line(self) -> None:
        short = _reply(B1=forecast([9, 9, 9], delta=-2))
        page, _fake = self._projected(short)
        self.assertEqual(len(self._grad(page, ".plb-ax")), 5)
        self.assertEqual(page.get_attribute("#plbGrad .plb-rw", "data-span"), "s")
        longer = _reply(B1=forecast([9] * 7, delta=2))
        page, _fake = self._projected(longer)
        self.assertEqual(len(self._grad(page, ".plb-ax")), 7)
        self.assertEqual(page.get_attribute("#plbGrad .plb-rw", "data-span"), "m")

    def test_stacked_layout_on_a_phone_keeps_one_axis(self) -> None:
        page, _fake = self._projected(width=390, height=900)
        self.assertEqual(
            page.evaluate("getComputedStyle(document.querySelector('#plbGrad .plb-rw')).display"),
            "flex",
        )
        label = self._boxes(page, "#plbGrad .plb-rwl--opt .plb-rl")[0]
        landing = self._boxes(page, "#plbGrad .plb-rwl--opt .plb-land")[0]
        opt = self._boxes(page, "#plbGrad .plb-rwl--opt .plb-tk")
        reg = self._boxes(page, "#plbGrad .plb-rwl--reg .plb-tk")
        axis = self._boxes(page, "#plbGrad .plb-ax")
        self.assertGreaterEqual(opt[0]["top"], label["bottom"] - 1)
        self.assertGreater(landing["left"], label["right"] - 1)
        self.assertLess(landing["top"], opt[0]["top"])
        # The ticks sit under their label and the axis under the ticks; both lines share the columns.
        self.assertEqual(len(axis), 5)
        for i, box in enumerate(opt):
            self.assertAlmostEqual(box["x"], reg[i]["x"], delta=1)
            self.assertAlmostEqual(box["x"], axis[i]["x"], delta=1)
        self.assertAlmostEqual(
            opt[0]["right"] - opt[0]["left"], axis[0]["right"] - axis[0]["left"], delta=1
        )
        self.assertGreater(axis[0]["top"], reg[0]["bottom"] - 1)
        # The caption that names the numbers is a row of its own under the term labels.
        self.assertTrue(page.locator("#plbGrad .plb-axn").is_visible())
        caption = self._boxes(page, "#plbGrad .plb-axn")[0]
        self.assertGreaterEqual(caption["top"], axis[0]["bottom"] - 1)
        self.assertLess(caption["left"], axis[0]["right"])
        # The result is on one line with the completion term, at the smaller size.
        won = page.locator("#plbGrad .plb-rwl--opt .plb-won")
        self.assertLess(float(won.evaluate("n => getComputedStyle(n).fontSize")[:-2]), 18)
        won_box = self._boxes(page, "#plbGrad .plb-rwl--opt .plb-won")[0]
        completion = self._boxes(page, "#plbGrad .plb-rwl--opt .plb-land b")[0]
        self.assertAlmostEqual(won_box["bottom"], completion["bottom"], delta=8)
        self.assertEqual(page.evaluate(OVERFLOWS), [])

    def test_the_row_layout_applies_from_each_spans_width(self) -> None:
        # The band is given an exact width: one pixel under its span's threshold the runway is
        # stacked, at the threshold it is the row layout.
        display = "getComputedStyle(document.querySelector('#plbGrad .plb-rw')).display"
        resize = (
            "w => { const g = document.getElementById('plbGrad');"
            " g.style.width = w + 'px'; g.style.justifySelf = 'start';"
            " return g.getBoundingClientRect().width; }"
        )
        for columns, span, threshold in (
            (5, "s", 440),
            (7, "m", 580),
            (9, "l", 620),
            (15, "xl", 900),
        ):
            page, _fake = self._projected(_reply(B1=forecast([9] * columns, delta=0)), width=1440)
            self.assertEqual(page.get_attribute("#plbGrad .plb-rw", "data-span"), span)
            self.assertEqual(page.evaluate(resize, threshold - 1), threshold - 1)
            self.assertEqual(page.evaluate(display), "flex", (span, threshold - 1))
            self.assertEqual(page.evaluate(resize, threshold), threshold)
            self.assertEqual(page.evaluate(display), "grid", (span, threshold))

    def test_a_long_runway_fits_the_page_in_either_layout(self) -> None:
        # Which layout a width gives depends on the band, which the guidance column narrows, and
        # on the fonts. Whichever it is, the runway has room for its texts and the page does not
        # scroll sideways.
        for width, columns in ((1440, 15), (1440, 14), (1152, 15), (600, 9)):
            page, _fake = self._projected(_reply(B1=forecast([9] * columns, delta=0)), width=width)
            self._pick(page, "B1")
            state = self._fit(page)
            self.assertEqual(state["columns"], columns, (width, columns))
            self._check_fit(page, state, (width, columns))
            self.assertEqual(page.evaluate(OVERFLOWS), [], (width, columns))

    def test_screen_reader_sentences_and_hidden_ticks(self) -> None:
        page, _fake = self._projected()
        self.assertEqual(
            self._text(page, "#plbGrad .plb-rwl--opt .sr-only"),
            "Option B1: 4 terms, completes Term 2, 1449, 1 term sooner. "
            "Credits per term: 1448/1 15, 1448/2 18, 1449/1 18, 1449/2 15.",
        )
        self.assertEqual(
            self._text(page, "#plbGrad .plb-rwl--reg .sr-only"),
            "As registered: 5 terms, completes Term 1, 1450. "
            "Credits per term: 1448/1 6, 1448/2 18, 1449/1 18, 1449/2 18, 1450/1 6.",
        )
        self._pick(page, "B3")
        self.assertIn("1448/2 no courses", self._text(page, "#plbGrad .plb-rwl--opt .sr-only"))
        self.assertTrue(self._text(page, "#plbGrad .plb-rwl--opt .sr-only").endswith("1450/2 9."))
        self.assertEqual(
            page.eval_on_selector_all(
                "#plbGrad .plb-tk", "ns => ns.every(n => n.getAttribute('aria-hidden') === 'true')"
            ),
            True,
        )
        self.assertEqual(page.get_attribute("#plbGrad .plb-rwl--ax", "aria-hidden"), "true")
        page, _fake = self._projected(language="ar")
        self.assertEqual(
            self._text(page, "#plbGrad .plb-rwl--opt .sr-only"),
            "الخيار B1: 4 فصول، يكتمل في الفصل 2، 1449، أبكر بفصل واحد. "
            "الساعات في كل فصل: 1448/1 15، 1448/2 18، 1449/1 18، 1449/2 15.",
        )

    def test_both_buttons_show_focus_and_are_touch_sized_on_a_phone(self) -> None:
        page, _fake = self._projected(self._disclosure_reply(), width=390)
        page.keyboard.press("Tab")
        page.focus("#plbGradWhyBtn")
        self.assertNotEqual(
            page.evaluate(
                "getComputedStyle(document.getElementById('plbGradWhyBtn')).outlineStyle"
            ),
            "none",
        )
        height = page.evaluate(
            "document.getElementById('plbGradWhyBtn').getBoundingClientRect().height"
        )
        self.assertGreaterEqual(height, 44)
        fake = BuildFake()
        fake.projection_answer = lambda body: (500, {})
        page, _fake = self._built(fake, width=390)
        page.keyboard.press("Tab")
        page.focus("#plbGradRetry")
        self.assertNotEqual(
            page.evaluate("getComputedStyle(document.getElementById('plbGradRetry')).outlineStyle"),
            "none",
        )
        self.assertGreaterEqual(
            page.evaluate("document.getElementById('plbGradRetry').getBoundingClientRect().height"),
            44,
        )

    def test_nothing_overflows_with_a_twelve_column_runway(self) -> None:
        reply = _reply(
            B1=forecast([15] + [18] * 10 + [9], delta=1, reasons=[{"code": "same_course_set"}]),
        )
        reply["baseline"] = forecast([6] + [18] * 10, kind="registered_timetable", reason_code=None)
        for language in ("en", "ar"):
            for width in (1440, 1152, 390):
                page, _fake = self._projected(reply, language=language, width=width, font="wide")
                page.click("#plbGradWhyBtn")
                self.assertEqual(len(self._grad(page, ".plb-ax")), 12)
                self.assertEqual(page.evaluate(OVERFLOWS), [], (language, width))
                grad = self._boxes(page, "#plbGrad")[0]
                runway = self._boxes(page, "#plbGrad .plb-rw")[0]
                self.assertLessEqual(runway["right"], grad["right"] + 1, (language, width))
                self.assertGreaterEqual(runway["left"], grad["left"] - 1, (language, width))
                for box in self._boxes(page, "#plbGrad .plb-tk, #plbGrad .plb-land"):
                    self.assertLessEqual(box["right"], grad["right"] + 3, (language, width))
                    self.assertGreaterEqual(box["left"], grad["left"] - 3, (language, width))
                self._pick(page, "B3")
                self.assertEqual(page.evaluate(OVERFLOWS), [], (language, width))

    def test_a_projection_for_every_state_leaves_other_options_alone(self) -> None:
        # An option the reply does not mention is "unavailable", never an exception.
        reply = copy.deepcopy(READY)
        del reply["options"]["B2"]
        page, _fake = self._projected(reply)
        self._pick(page, "B2")
        self.assertEqual(self._grad(page, ".plb-rwl--opt .plb-rwn"), ["Projection unavailable"])
        self.assertEqual(self._line(page, "reg")["num"], ["5"])
        self.assertNotIn("terms", self._tab_lines(page, "B2")[1])

    # ── what the forecast does to the page when it lands ────────────────────
    def test_a_forecast_that_lands_leaves_the_keyboard_where_it_is(self) -> None:
        # The tabs are redrawn when the forecast lands (they gain their term counts).
        page, fake = self._held()
        self._pick(page, "B2")
        page.focus("#plbTab_B2")
        fake.pending_projection.pop().fulfill(json=READY)
        expect(page.locator("#plbGradLine")).to_be_hidden()
        self.assertEqual(self._tab_lines(page, "B2")[1], "5 terms · 4 days · Thu free")
        expect(page.locator("#plbTab_B2")).to_be_focused()
        # An item of "More options" has no id: it is found again by the option it stands for.
        page, fake = self._held()
        page.click("#plbMoreBtn")
        page.focus("#plbMoreList [data-more='B3']")
        fake.pending_projection.pop().fulfill(json=READY)
        expect(page.locator("#plbGradLine")).to_be_hidden()
        expect(page.locator("#plbMoreList [data-more='B2']")).to_contain_text("5 terms")
        expect(page.locator("#plbMoreList [data-more='B3']")).to_be_focused()
        expect(page.locator("#plbMoreList")).to_be_visible()

    def test_a_reply_for_a_retired_build_redraws_nothing(self) -> None:
        page, fake = self._held()
        fake.projection_answer = lambda body: (200, READY)
        page.click("#plbBuild")
        self._wait_for(page, lambda: len(fake.projection_bodies) == 2)
        expect(page.locator("#plbGradLine")).to_be_hidden()
        # A redraw replaces the nodes it draws: mark the ones on screen.
        page.evaluate(
            "() => { document.getElementById('plbTab_B1').dataset.probe = '1';"
            " document.querySelector('#plbGrad .plb-rw').dataset.probe = '1'; }"
        )
        fake.pending_projection.pop().fulfill(json=READY)
        page.wait_for_timeout(150)
        self.assertEqual(page.get_attribute("#plbTab_B1", "data-probe"), "1")
        self.assertEqual(page.get_attribute("#plbGrad .plb-rw", "data-probe"), "1")

    def test_the_band_keeps_its_height_when_the_forecast_lands_and_between_tabs(self) -> None:
        # The tabs sit under the band: they must not jump away from the pointer. The band is as
        # tall as its tallest column. The guidance is there from the build and the runway arrives
        # with the forecast, so landing may add what the runway has beyond the guidance; after
        # that the band never gets shorter, so changing tab does not pull the tabs up. On a phone
        # the columns stack, nothing is held, and the strip is only ever pushed down.
        top = "document.getElementById('plbStrip').getBoundingClientRect().top"
        for kwargs in (
            {"width": 1152},
            {"width": 1152, "language": "ar"},
            {"width": 1440},
            {"width": 390, "height": 900},
            {"width": 390, "height": 900, "language": "ar"},
        ):
            side_by_side = kwargs["width"] > 880
            page, fake = self._held(**kwargs)
            loading = page.evaluate(top)
            fake.pending_projection.pop().fulfill(json=READY)
            expect(page.locator("#plbGradLine")).to_be_hidden()
            expect(page.locator("#plbGrad .plb-rwl--opt .plb-tk")).to_have_count(4)
            landed = page.evaluate(top)
            if side_by_side:
                self.assertGreaterEqual(landed, loading - 2, msg=str(kwargs))
                for tab in ("#plbTabRegistered", "#plbTab_C1", "#plbTab_B1", "#plbTab_C2"):
                    page.click(tab)
                    # Never pulled up; pushed down only by a runway taller than any seen so far.
                    self.assertGreaterEqual(page.evaluate(top), landed - 2, msg=(kwargs, tab))
                page.click("#plbTabRegistered")
                registered = page.evaluate(top)
                page.click("#plbTab_B1")
                self.assertAlmostEqual(page.evaluate(top), registered, delta=2, msg=kwargs)
            else:
                # One column: the strip sits under the whole band, which now lives in the options
                # zone below the Build button, so the runway landing pushes it down. It is never
                # pulled up (the redraw keeps the keyboard without scrolling the page).
                self.assertGreaterEqual(landed, loading - 2, msg=str(kwargs))

    def test_no_assumptions_when_no_line_is_drawn(self) -> None:
        nothing = projection_reply(
            {**NO_FORECAST, "kind": None, "reason_code": "no_registered_timetable"},
            {name: NO_FORECAST for name in ("B1", "B2", "B3", "C1", "C2")},
            retained_baseline_kind="expected_plan",
        )
        page, _fake = self._projected(nothing)
        self.assertEqual(self._grad(page, ".plb-rwl--opt .plb-rwn"), ["Projection unavailable"])
        expect(page.locator("#plbGradAsm")).to_be_hidden()
        expect(page.locator("#plbGradRetained")).to_be_hidden()
        # One line drawn is enough for them to show.
        page, _fake = self._projected(
            projection_reply(REG_FORECAST, READY["options"], retained_baseline_kind="expected_plan")
        )
        page.click("#plbTabRegistered")
        expect(page.locator("#plbGradAsm")).to_be_visible()
        expect(page.locator("#plbGradRetained")).to_be_visible()

    def test_escape_is_left_alone_when_the_reasons_button_is_not_shown(self) -> None:
        reply = _reply(
            B1=forecast([15, 18, 18, 15], delta=-1, reasons=[{"code": "same_course_set"}])
        )
        page, _fake = self._projected(reply)
        page.click("#plbGradWhyBtn")
        expect(page.locator("#plbGradWhy")).to_be_visible()
        page.click("#plbTabRegistered")
        expect(page.locator("#plbGradWhyBtn")).to_be_hidden()
        # Registered after the page's own listener, so it sees what that one decided.
        page.evaluate(
            "() => { window.__esc = []; document.addEventListener('keydown', (e) => {"
            " if (e.key === 'Escape') window.__esc.push(e.defaultPrevented); }); }"
        )
        page.focus("#plbGrad")
        page.keyboard.press("Escape")
        self.assertEqual(page.evaluate("window.__esc"), [False])
        # Back on the option the list is still open, and there Escape closes it.
        page.click("#plbTab_B1")
        expect(page.locator("#plbGradWhy")).to_be_visible()
        page.focus("#plbGradWhyBtn")
        page.keyboard.press("Escape")
        expect(page.locator("#plbGradWhy")).to_be_hidden()
        self.assertEqual(page.evaluate("window.__esc"), [False, True])

    def test_a_requirement_with_no_course_code_is_not_listed(self) -> None:
        blocked = [{"missing_course_prerequisites": ["AI331"]}, {"code": "AI431"}]
        reply = _reply(B1={**UNRESOLVED, "unresolved_requirements": blocked})
        page, _fake = self._projected(reply)
        self.assertEqual(self._grad(page, "li"), ["AI431 could not be placed in the projection."])
        self.assertNotIn("undefined", self._text(page, "#plbGrad"))

    def test_a_tab_that_stands_for_several_options_reads_its_own_forecast(self) -> None:
        def option(name: str, tsid: int, label: str) -> dict:
            return _option(
                name,
                [
                    _mapping("AI331", label, tsid, ["MON", "WED"], "10:00", "11:15"),
                    _mapping("AI342", "M1", 15, ["SUN"], "13:00", "14:15"),
                ],
            )

        fake = BuildFake()
        fake.build_answer = lambda body: (
            200,
            build_reply([option("A1", 11, "M2"), option("C1", 11, "M2"), option("B1", 12, "M3")]),
        )
        # The reply answers for every name the build returned; the merged tab is A1's.
        reply = projection_reply(
            REG_FORECAST,
            {
                "A1": forecast([15, 18, 18, 15], delta=-1),
                "C1": forecast([9] * 7, delta=2),
                "B1": forecast([12, 18, 18, 18, 6], delta=0),
            },
        )
        self.start_on = None  # the first of the order is the merged tab
        page, _fake = self._projected(reply, fake=fake)
        self.assertEqual(self._tab_names(page), ["A1", "B1", "(registered)"])
        self.assertIn("A1 | C1", self._text(page, "#plbTab_A1 .plb-tab-n"))
        line = self._line(page, "opt")
        self.assertEqual(line["num"], ["4"])
        self.assertEqual(line["label"], ["Option A1"])
        self.assertEqual(line["won"], ["1 term sooner"])
        self.assertEqual(self._tab_lines(page, "A1")[1][:7], "4 terms")
        self.assertIn("Option A1: 4 terms", self._text(page, "#plbGrad .plb-rwl--opt .sr-only"))

    # ── the runway is fitted by measurement ─────────────────────────────────
    # Text widths depend on the language and on the fonts that loaded, and this server loads none
    # of the web fonts. So nothing here expects a particular outcome at a particular width in the
    # machine's own fonts. Each rule is pinned twice: by what must hold whatever was chosen, and
    # by a style that exaggerates one text until the outcome is certain in any font.
    def _fit(self, page, width="keep") -> dict:
        if width != "keep":
            page.evaluate(RESIZE, width)
        return page.evaluate(MEASURE)

    def _check_fit(self, page, state: dict, where) -> None:
        """What must hold whatever the script chose, and that it chose the most that fits."""
        columns, labelled = state["columns"], state["labelled"]
        steps = {tuple(range(0, columns, step)): step for step in (4, 2, 1)}
        steps.setdefault((0,), 0)
        self.assertIn(tuple(labelled), steps, where)
        step = steps[tuple(labelled)]
        if len(labelled) > 1:
            self.assertGreaterEqual(state["axisGap"], 4 - 0.01, where)
        if state["row"]:
            self.assertEqual(state["fit"], "", where)
            self.assertIn(step, (1, 2), where)
            self.assertGreaterEqual(state["tickGap"], 2 - 0.01, where)
            self.assertTrue(state["inside"], where)
        elif state["tickGap"] < 2 - 0.01:
            self.assertEqual(state["unit"], "off", where)  # nothing more to give up
        if state["size"]:
            self.assertTrue(state["row"] and state["result"] == "sooner", where)
            self.assertTrue(state["oneLine"] and state["wonInside"], where)
        # The most that fits: one step denser, the unit back, the row layout back, one size up.
        if step != 1 and columns > 1:
            denser = {2: 1, 4: 2, 0: 4}[step]
            self.assertLess(page.evaluate(PROBE_LABELS, denser), 4, (where, "labels"))
        if state["unit"] == "off":
            self.assertLess(page.evaluate(PROBE_UNIT), 2, (where, "unit"))
        if state["fit"] == "stacked":
            forced = page.evaluate(PROBE_ROW)
            self.assertTrue(forced["row"], where)
            self.assertTrue(
                forced["tickGap"] < 2 or forced["axisGap"] < 4 or not forced["inside"],
                (where, forced),
            )
        if state["row"] and state["result"] == "sooner" and state["size"] != "xl":
            bigger = page.evaluate(PROBE_SIZE, "lg" if state["size"] == "" else "xl")
            self.assertFalse(bigger["oneLine"] and bigger["wonInside"], (where, bigger))

    def _sooner(self, registered: int) -> dict:
        reply = _reply(B1=forecast([15] + [18] * (registered - 3) + [12], delta=-1))
        reply["baseline"] = forecast(
            [6] + [18] * (registered - 2) + [9], kind="registered_timetable", reason_code=None
        )
        return reply

    # A band so wide that five columns have room to spare in any font (the page's own band at
    # this viewport is about 810 px; the row needs under 610 px even in the widest font measured).
    ROOMY = 1150

    def test_with_room_to_spare_nothing_is_given_up(self) -> None:
        for language in ("en", "ar"):
            page, _fake = self._projected(language=language)
            state = self._fit(page, self.ROOMY)
            where = (language, state)
            self.assertTrue(state["row"], where)
            self.assertEqual(state["labelled"], [0, 1, 2, 3, 4], where)
            self.assertTrue(state["unitShown"], where)
            self.assertEqual(state["size"], "xl", where)
            self.assertEqual(state["fontSize"], "22px", where)
            self.assertTrue(state["oneLine"] and state["wonInside"], where)
            self._check_fit(page, state, where)

    def test_whatever_the_width_language_or_font_the_runway_fits(self) -> None:
        for registered in (5, 8, 14, 20):
            reply = READY if registered == 5 else self._sooner(registered)
            for language, font in (("en", "app"), ("ar", "app"), ("en", "wide"), ("ar", "wide")):
                page, _fake = self._projected(reply, language=language, font=font)
                for width in (None, 440, 500, 580, 620, 700, 900, 1000):
                    state = self._fit(page, width)
                    where = (registered, language, font, width)
                    self.assertEqual(state["columns"], registered, where)
                    self._check_fit(page, state, where)
                    page.click("#plbTab_B1")  # a redraw undoes what the probes changed
        for registered in (5, 8, 16, 24):
            reply = READY if registered == 5 else self._sooner(registered)
            for language in ("en", "ar"):
                page, _fake = self._projected(
                    reply, language=language, width=390, height=900, font="wide"
                )
                state = self._fit(page)
                where = (registered, language, "phone")
                self.assertFalse(state["row"], where)
                self._check_fit(page, state, where)
                self.assertEqual(page.evaluate(OVERFLOWS), [], where)

    def test_ticks_that_touch_cost_the_unit_first_and_then_the_row_layout(self) -> None:
        # Every stretch is taken from the measured width of a tick, so it works in any font.
        tick = (
            "() => document.querySelector('#plbGrad .plb-rwl--opt .plb-tk')"
            ".getBoundingClientRect().width"
        )
        page, _fake = self._projected()
        self.assertTrue(self._fit(page, self.ROOMY)["unitShown"])
        # A unit wider than two ticks goes; the row layout stays.
        page.evaluate(
            STRESS, f"#plbGrad .plb-u {{ letter-spacing: {page.evaluate(tick)}px !important; }}"
        )
        state = self._fit(page, self.ROOMY + 1)
        self.assertEqual(
            (state["row"], state["unit"], state["unitShown"]), (True, "off", False), state
        )
        self._check_fit(page, state, "unit")
        # Numbers wider than their ticks: the runway is stacked, where each tick is wider.
        page, _fake = self._projected(self._sooner(14))
        self.assertTrue(self._fit(page, self.ROOMY)["row"])
        spacing = page.evaluate(tick) * 0.45
        page.evaluate(STRESS, f"#plbGrad .plb-tk {{ letter-spacing: {spacing}px !important; }}")
        state = self._fit(page, self.ROOMY + 1)
        self.assertEqual((state["row"], state["fit"]), (False, "stacked"), state)
        self._check_fit(page, state, "ticks")
        # With the numbers back to normal it is a row again.
        page.evaluate(STRESS, "")
        state = self._fit(page, self.ROOMY + 2)
        self.assertEqual((state["row"], state["fit"]), (True, ""), state)

    def test_labels_that_crowd_are_thinned_and_then_the_runway_is_stacked(self) -> None:
        page, _fake = self._projected()
        self.assertEqual(self._fit(page, self.ROOMY)["labelled"], [0, 1, 2, 3, 4])
        # Every stretch is taken from the measured widths of a column and of a label.
        cell = page.evaluate(
            "() => document.querySelector('#plbGrad .plb-ax').getBoundingClientRect().width"
        )
        label = page.evaluate(TEXT_WIDTH, "#plbGrad .plb-ax")
        # Labels wider than one column, narrower than two: every other one, still a row.
        spacing = (cell * 1.4 - label) / 6
        page.evaluate(STRESS, f"#plbGrad .plb-ax {{ letter-spacing: {spacing}px !important; }}")
        state = self._fit(page, self.ROOMY + 1)
        self.assertEqual((state["row"], state["labelled"]), (True, [0, 2, 4]), state)
        self._check_fit(page, state, "every other")
        # Wider than two columns of the row layout: stacked, and as many labels as fit there.
        spacing = (cell * 2.6 - label) / 6
        page.evaluate(STRESS, f"#plbGrad .plb-ax {{ letter-spacing: {spacing}px !important; }}")
        state = self._fit(page, self.ROOMY + 2)
        self.assertEqual((state["row"], state["fit"]), (False, "stacked"), state)
        self.assertLess(len(state["labelled"]), 5, state)
        self._check_fit(page, state, "stacked")

    def test_a_text_that_is_not_drawn_does_not_count_as_outside_the_band(self) -> None:
        # An element that is not drawn has an empty box at the page's origin.
        page, _fake = self._projected()
        page.evaluate(STRESS, "#plbGrad .plb-land b { display: none !important; }")
        state = self._fit(page, self.ROOMY)
        self.assertEqual((state["row"], state["fit"]), (True, ""), state)

    def test_the_result_is_as_large_as_its_line_allows(self) -> None:
        sizes = {"xl": "22px", "lg": "18px", "": "15px"}
        page, _fake = self._projected()
        state = self._fit(page, self.ROOMY)
        self.assertEqual((state["size"], state["fontSize"]), ("xl", "22px"), state)
        # Stretch the text a pixel per letter at a time: the size steps down once, then again,
        # and never back up. Each state is checked for being the largest that fits.
        seen: list[str] = []
        wrapped = False
        for spacing in range(41):
            page.evaluate(
                STRESS, f"#plbGrad .plb-won {{ letter-spacing: {spacing}px !important; }}"
            )
            page.click("#plbTab_B1")  # a redraw fits the runway again
            state = self._fit(page)
            if not state["row"]:
                break  # a single word no longer fits its line: stacked (the next test)
            self.assertEqual(state["fontSize"], sizes[state["size"]], state)
            self._check_fit(page, state, ("result", spacing))
            if not seen or seen[-1] != state["size"]:
                seen.append(state["size"])
            wrapped = wrapped or (state["size"] == "" and not state["oneLine"])
        self.assertEqual(seen, ["xl", "lg", ""], seen)
        # Too wide for one line even at reading size, it wraps where it is: the runway stays a
        # row (and inside the band, which _check_fit asserted) until a single word has no room.
        self.assertTrue(wrapped)
        # Bad news and "same term" are never enlarged; stacked, nothing is.
        page, _fake = self._projected()
        for tab, kind, size in (("B3", "later", "15px"), ("B2", "same", "13px")):
            self._pick(page, tab)
            state = self._fit(page)
            self.assertEqual((state["result"], state["size"], state["fontSize"]), (kind, "", size))
        page, _fake = self._projected(width=390, height=900)
        state = self._fit(page)
        self.assertEqual((state["row"], state["size"]), (False, ""))
        self.assertLessEqual(float(state["fontSize"][:-2]), 15)  # reading size, a phone's own

    def test_the_result_and_the_completion_terms_never_leave_the_band(self) -> None:
        for registered in (5, 8, 14):
            reply = READY if registered == 5 else self._sooner(registered)
            for language, font in (("en", "wide"), ("ar", "wide")):
                page, _fake = self._projected(reply, language=language, font=font)
                # Even with a text far too wide for any size, and whatever the layout.
                page.add_style_tag(
                    content="#plbGrad .plb-won, #plbGrad .plb-land b { letter-spacing: 5px !important; }"
                )
                for tab in ("B1", "B2", "B3") if registered == 5 else ("B1",):
                    self._pick(page, tab)
                    for width in (440, 520, 620, 800):
                        state = self._fit(page, width)
                        where = (registered, language, tab, width)
                        self.assertTrue(state["wonInside"], where)
                        self.assertTrue(state["inside"], where)
                        self.assertEqual(page.evaluate(TOUCHES_A_TICK), [], where)

    def test_stacked_ticks_stay_on_one_row_for_any_number_of_columns(self) -> None:
        for columns in (6, 7, 11, 13, 16):
            page, _fake = self._projected(
                _reply(B1=forecast([9] * columns, delta=0)), width=390, height=900
            )
            self.assertEqual(
                page.evaluate(
                    "getComputedStyle(document.querySelector('#plbGrad .plb-rw')).display"
                ),
                "flex",
            )
            # One row each: the same bottom edge (an unlabelled axis cell is shorter, not lower).
            for selector in ("#plbGrad .plb-rwl--opt .plb-tk", "#plbGrad .plb-ax"):
                boxes = self._boxes(page, selector)
                self.assertEqual(len(boxes), columns)
                self.assertEqual(
                    len({round(box["bottom"]) for box in boxes}), 1, (columns, selector)
                )
            self.assertEqual(page.evaluate(OVERFLOWS), [], columns)


def test_the_four_row_layout_blocks_are_one_block_repeated() -> None:
    """The row layout is written once per span because only the threshold differs.

    A change made to one copy and not the others would give one span a different layout: the four
    must stay the same text apart from the span they name.
    """
    css = (pathlib.Path(__file__).resolve().parents[1] / "static" / "css" / "global.css").read_text(
        encoding="utf-8"
    )
    blocks = {}
    for threshold, body in re.findall(
        r"@container plbgrad \(min-width: (\d+)px\) \{\n((?:  [^\n]+\n)+)\}", css
    ):
        if "subgrid" not in body:
            continue
        spans = set(re.findall(r'data-span="(\w+)"', body))
        assert len(spans) == 1, spans
        blocks[spans.pop()] = (int(threshold), re.sub(r'data-span="\w+"', "data-span", body))
    assert {span: threshold for span, (threshold, _body) in blocks.items()} == {
        "s": 440,
        "m": 580,
        "l": 620,
        "xl": 900,
    }
    bodies = {body for _threshold, body in blocks.values()}
    assert len(bodies) == 1, "the row-layout blocks differ"
    assert bodies.pop().count("\n") >= 8

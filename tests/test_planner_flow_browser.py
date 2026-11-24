"""The staff planner (/planner/): one step on screen at a time, a bar that stays in view.

Slice 5 of the planner premium pass. Measured in a real browser on the synthetic student of
tests/test_planner_layout_browser.py (imported, not copied); the context, plan, sections and build
requests are answered from made-up data, and the ones whose pending state is measured are held
until the test lets them go. Nothing leaves the machine (tests/browser_isolation.py).

1  the default is ONE step: the courses panels and the one timetable, the builder and the
   diagnostics are not on screen, and the page is short; the student strip is always above the bar;
2  Next, Back and the step buttons move you: the step's panels show and the content scrolls so its
   first panel starts just under the bar; focus stays on the control that was pressed;
3  before a student is loaded the steps, Back and Next are disabled;
4  "Show everything" shows every panel in reading order, with one week grid, and is remembered (and
   the page works without storage);
5  the step bar is sticky, opaque and fully inside the viewport after scrolling;
6  every message is on screen wherever the page is scrolled, and a long one is cut with an ellipsis
   and carries its full text;
7  Build is disabled, busy and says so while its request is in flight, and is restored on every exit;
8  the panels are busy while a student is fetched;
9  there is ONE timetable, with a source selector;
10 on a phone the bar fits in two rows, nothing cut, nothing scrolling sideways;
11 the steps are tabs, not filled pills.
"""

from __future__ import annotations

import os

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client

from tests.test_exam_rosters_browser import WIDE_FONT
from tests.test_planner_layout_browser import (
    CONTEXT,
    READY,
    SECTIONS_CATALOG,
    STUDENT_ID,
    STUDENT_PLAN,
)
from tests.test_planner_lists_browser import BUILD

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

LANGUAGES = ("en", "ar")
DESKTOP = (1440, 900)
PHONE = (390, 844)
NO_MOTION = "*, *::before, *::after { transition: none !important; animation: none !important; }"
SETTLE = "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))"

COURSES_PANELS = ["recCard", "timetableCard", "shortlistCard", "availableCard", "planCard"]
BUILD_PANELS = ["timetableCard", "builderCard", "advancedDiag"]
READING_ORDER = [
    "recCard",
    "timetableCard",
    "shortlistCard",
    "availableCard",
    "planCard",
    "builderCard",
    "advancedDiag",
]

# The nearest scrolling ancestor of the step bar, as the page itself finds it.
SCROLLER = """
const scroller = () => {
  for (let el = document.getElementById('plannerSteps').parentElement; el && el !== document.body; el = el.parentElement) {
    const oy = getComputedStyle(el).overflowY;
    if ((oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight) return el;
  }
  return document.scrollingElement || document.documentElement;
};
const viewTop = box => (box === document.scrollingElement || box === document.documentElement)
  ? 0 : box.getBoundingClientRect().top;
"""

# The panels on screen (not display:none), by id, in document order.
VISIBLE_PANELS = """() => [...document.querySelectorAll('.step-panel')]
  .filter(el => el.getClientRects().length).map(el => el.id)"""

BAR_RECT = """() => {
  const r = document.getElementById('plannerSteps').getBoundingClientRect();
  return { top: r.top, bottom: r.bottom, height: r.height };
}"""

SCROLL_TO = (
    "(y) => {" + SCROLLER + "const box = scroller(); box.scrollTop = y; return box.scrollTop; }"
)

SCROLL_INFO = (
    "() => {" + SCROLLER + "const box = scroller(); return { top: box.scrollTop,"
    " height: box.scrollHeight, client: box.clientHeight, id: box.id || box.tagName }; }"
)

# Where the first panel of a step starts, against the bottom of the bar.
PANEL_UNDER_BAR = """(id) => {
  const bar = document.getElementById('plannerSteps').getBoundingClientRect();
  const panel = document.getElementById(id).getBoundingClientRect();
  return { gap: panel.top - bar.bottom, bar: bar.height };
}"""

STICKY = (
    "() => {"
    + SCROLLER
    + """
  const bar = document.getElementById('plannerSteps');
  const r = bar.getBoundingClientRect();
  const bg = getComputedStyle(bar).backgroundColor;
  const alpha = bg.startsWith('rgba') ? parseFloat(bg.split(',').pop()) : 1;
  return { top: r.top, bottom: r.bottom, viewTop: viewTop(scroller()), alpha, bg,
           inside: r.top >= -0.5 && r.bottom <= innerHeight + 0.5 };
}"""
)

# Every element of the step bar that is on screen: where it is, against the viewport.
BAR_FIT = """() => {
  const W = innerWidth;
  const bar = document.getElementById('plannerSteps');
  const found = [];
  const sketch = el => `${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}`
    + [...el.classList].slice(0, 2).map(c => '.' + c).join('');
  for (const el of [bar, ...bar.querySelectorAll('*')]) {
    if (!el.getClientRects().length) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 1 && r.height < 1) continue;
    if (r.left < -0.5 || r.right > W + 0.5)
      found.push(`${sketch(el)} ${Math.round(r.left)}..${Math.round(r.right)} of ${W}`);
  }
  for (const id of ['prevStep', 'nextStep', 'step1', 'step2']) {
    const r = document.getElementById(id).getBoundingClientRect();
    if (r.width < 20 || r.left < -0.5 || r.right > W + 0.5)
      found.push(`#${id} not fully visible: ${Math.round(r.left)}..${Math.round(r.right)} of ${W}`);
  }
  if (bar.scrollWidth > bar.clientWidth + 1)
    found.push(`the bar scrolls sideways: ${bar.scrollWidth} > ${bar.clientWidth}`);
  const main = document.getElementById('main-content');
  if (main.scrollWidth > main.clientWidth + 1)
    found.push(`the page scrolls sideways: ${main.scrollWidth} > ${main.clientWidth}`);
  for (const el of bar.querySelectorAll('.wf-step, .wf-nav .btn')) {
    if (el.scrollWidth > el.clientWidth + 1)
      found.push(`${sketch(el)} text cut: ${el.scrollWidth} > ${el.clientWidth}`);
  }
  const text = document.querySelector('#statusBanner .planner-status-text').getBoundingClientRect();
  if (text.width < 20) found.push(`the status text is ${Math.round(text.width)}px wide`);
  return found;
}"""


def STEP_STATE(page) -> dict:  # noqa: N802 - reads like the constants beside it
    """Which step is current, whether Back and Next are disabled, and where focus is."""
    return page.evaluate(
        "() => ({ current: [...document.querySelectorAll('.wf-step')]"
        ".filter(b => b.getAttribute('aria-current') === 'step').map(b => b.id),"
        " prev: document.getElementById('prevStep').disabled,"
        " next: document.getElementById('nextStep').disabled,"
        " active: document.activeElement.id })"
    )


LONG_TEXT = "A very long status message about a student plan that will not fit one line. " * 6

# Which way each of Back / Next points ON SCREEN (its chevron's tip against its
# tails, through the SVG's screen matrix, so a mirroring transform is counted),
# how wide its word is drawn, and its accessible name.
NAV_ARROWS = """() => ['prevStep', 'nextStep'].map(id => {
  const button = document.getElementById(id);
  const svg = button.querySelector('svg'), line = svg.querySelector('polyline');
  const points = line.getAttribute('points').trim().split(/\\s+/).map(Number);
  const m = svg.getScreenCTM();
  const x = (px, py) => m.a * px + m.c * py + m.e;
  const tip = x(points[2], points[3]), tail = x(points[0], points[1]);
  const label = button.querySelector('.wf-nav-label');
  return { id, points: tip < tail ? 'left' : 'right',
           labelWidth: label ? label.getBoundingClientRect().width : 999,
           name: button.textContent.trim() };
})"""


class PlannerFlowBrowserTests(StaticLiveServerTestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls._pw.stop()
        super().tearDownClass()

    def setUp(self) -> None:
        user = get_user_model().objects.create_superuser(
            username="planner-flow", email="planner-flow@example.test", password="unused"
        )
        client = Client()
        client.force_login(user)
        self.session = client.cookies[settings.SESSION_COOKIE_NAME].value
        self.page_errors: list[str] = []
        # A route that is held keeps its requests here until a test lets them go.
        self.hold: dict[str, bool] = {"context": False, "build": False}
        self.pending: dict[str, list] = {"context": [], "build": []}
        self.addCleanup(self._no_page_errors)

    def _no_page_errors(self) -> None:
        self.assertEqual(self.page_errors, [], "the planner threw")

    def _route(self, name: str, payload: dict):
        def handler(route) -> None:
            if self.hold[name]:
                self.pending[name].append(route)
            else:
                route.fulfill(json=payload)

        return handler

    def _open(
        self,
        language: str,
        width: int,
        height: int,
        *,
        font: str = "app",
        fetch: bool = True,
        show_all: bool = False,
        storage: str = "ok",
        theme: str = "light",
    ):
        """The planner as the page loads it; with `fetch` the made-up student is loaded."""
        context = self.browser.new_context(
            locale="ar" if language == "ar" else "en-US",
            extra_http_headers={"Accept-Language": language},
            viewport={"width": width, "height": height},
        )
        self.addCleanup(context.close)
        context.add_cookies(
            [
                {
                    "name": settings.SESSION_COOKIE_NAME,
                    "value": self.session,
                    "url": self.live_server_url,
                },
                {
                    "name": settings.LANGUAGE_COOKIE_NAME,
                    "value": language,
                    "url": self.live_server_url,
                },
            ]
        )
        if storage == "blocked":
            context.add_init_script(
                "Object.defineProperty(window, 'localStorage',"
                " { get() { throw new DOMException('denied', 'SecurityError'); } });"
            )
        else:
            context.add_init_script(
                f"try {{ localStorage.setItem('theme', '{theme}'); }} catch (e) {{}}"
            )
            if show_all:
                context.add_init_script(
                    "try { if (!localStorage.getItem('plannerShowAll'))"
                    " localStorage.setItem('plannerShowAll', '1'); } catch (e) {}"
                )
        if font == "wide":
            context.add_init_script(WIDE_FONT)
        context.route("**/ops/planner/context/", self._route("context", CONTEXT))
        context.route("**/report/student-plan/**", lambda r: r.fulfill(json=STUDENT_PLAN))
        context.route(
            "**/ops/planner/sections-catalog/", lambda r: r.fulfill(json=SECTIONS_CATALOG)
        )
        context.route("**/ops/planner/build/", self._route("build", BUILD))
        page = context.new_page()
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        suffix = f"?student={STUDENT_ID}" if fetch else ""
        page.goto(f"{self.live_server_url}/planner/{suffix}")
        page.add_style_tag(content=NO_MOTION)
        self.assertEqual(page.evaluate("document.documentElement.lang"), language)
        if fetch:
            page.wait_for_function(READY, timeout=20000)
        page.evaluate("document.fonts.ready.then(() => true)")
        page.evaluate(SETTLE)
        return page

    def _release(self, name: str, **fulfil) -> None:
        """Let every held request of this route go, with this answer."""
        routes, self.pending[name] = self.pending[name], []
        for route in routes:
            route.fulfill(**fulfil)

    def _wait_pending(self, page, name: str) -> None:
        for _ in range(200):
            if self.pending[name]:
                return
            page.wait_for_timeout(25)
        self.fail(f"no {name} request reached the network")

    def _assert_no_findings(self, problems: dict[str, list[str]]) -> None:
        if problems:
            self.fail(
                "\n"
                + "\n".join(
                    f"{where}:\n    " + "\n    ".join(found) for where, found in problems.items()
                )
            )

    def _show_all(self, page, on: bool = True) -> None:
        """Flip the switch as a person does; a phone keeps it in the folded options."""
        if not page.is_visible("#showAll"):
            page.click(".planner-options-toggle")
        page.set_checked("#showAll", on)
        page.evaluate(SETTLE)

    # ── 1 ────────────────────────────────────────────────────────────────────
    def test_1_the_default_is_one_step(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            for width, height in (DESKTOP, PHONE):
                page = self._open(language, width, height)
                found: list[str] = []
                shown = page.evaluate(VISIBLE_PANELS)
                if shown != COURSES_PANELS:
                    found.append(f"panels on screen: {shown}, wanted {COURSES_PANELS}")
                info = page.evaluate(SCROLL_INFO)
                if width == DESKTOP[0] and info["height"] >= 4500:
                    found.append(f"the page is {info['height']}px tall (limit 4500)")
                summary = page.evaluate(
                    "() => { const s = document.getElementById('studentSummaryCard');"
                    " const b = document.getElementById('plannerSteps');"
                    " return { shown: !s.hidden && s.getClientRects().length > 0,"
                    " above: s.getBoundingClientRect().bottom <= b.getBoundingClientRect().top + 1,"
                    " pane: !!s.querySelector('.pane-head'), label: s.getAttribute('aria-label'),"
                    " text: s.innerText }; }"
                )
                if not summary["shown"]:
                    found.append("the student strip is not shown")
                if not summary["above"]:
                    found.append("the student strip is not above the step bar")
                if summary["pane"]:
                    found.append("the student strip still has a pane head")
                if not summary["label"]:
                    found.append("the student strip has no aria-label")
                if "Student Name" not in summary["text"]:
                    found.append(f"the strip does not show the name: {summary['text']!r}")
                if found:
                    problems[f"{language} {width}px"] = found
                page.context.close()
        self._assert_no_findings(problems)

    # ── 2 ────────────────────────────────────────────────────────────────────
    def test_2_steps_move_you(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            for width, height in (DESKTOP, PHONE):
                page = self._open(language, width, height)
                found: list[str] = []

                where = f"{language} {width}px"
                s = STEP_STATE(page)
                if s["current"] != ["step1"] or not s["prev"] or s["next"]:
                    found.append(f"first step state: {s}")
                page.evaluate(SCROLL_TO, 600)
                page.click("#nextStep")
                page.evaluate(SETTLE)
                shown = page.evaluate(VISIBLE_PANELS)
                if shown != BUILD_PANELS:
                    found.append(f"after Next the panels are {shown}, wanted {BUILD_PANELS}")
                place = page.evaluate(PANEL_UNDER_BAR, "builderCard")
                if not 0 <= place["gap"] <= place["bar"] + 24:
                    found.append(f"after Next the builder starts {place['gap']}px below the bar")
                s = STEP_STATE(page)
                if s["current"] != ["step2"] or s["prev"] or not s["next"]:
                    found.append(f"second step state: {s}")
                page.click("#prevStep")
                page.evaluate(SETTLE)
                shown = page.evaluate(VISIBLE_PANELS)
                if shown != COURSES_PANELS:
                    found.append(f"after Back the panels are {shown}")
                place = page.evaluate(PANEL_UNDER_BAR, "recCard")
                if not 0 <= place["gap"] <= place["bar"] + 24:
                    found.append(f"after Back the suggestions start {place['gap']}px below the bar")
                if STEP_STATE(page)["current"] != ["step1"]:
                    found.append("Back did not return to the first step")
                # the step button: focus stays on it
                page.click("#step2")
                page.evaluate(SETTLE)
                s = STEP_STATE(page)
                if s["current"] != ["step2"] or s["active"] != "step2":
                    found.append(f"after the step-2 button: {s}")
                if page.evaluate(VISIBLE_PANELS) != BUILD_PANELS:
                    found.append("the step-2 button did not show the build step")
                # the state text of each step
                labels = page.evaluate(
                    "() => [...document.querySelectorAll('.wf-step')].map(b => b.getAttribute('aria-label'))"
                )
                done = "مكتملة" if language == "ar" else "completed"
                current = "الخطوة الحالية" if language == "ar" else "current step"
                if done not in labels[0] or current not in labels[1]:
                    found.append(f"step labels: {labels}")
                if found:
                    problems[where] = found
                page.context.close()
        self._assert_no_findings(problems)

    # ── 3 ────────────────────────────────────────────────────────────────────
    def test_3_nothing_moves_before_a_student_is_loaded(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            for width, height in (DESKTOP, PHONE):
                page = self._open(language, width, height, fetch=False)
                found: list[str] = []
                for control in ("step1", "step2", "prevStep", "nextStep"):
                    if not page.is_disabled(f"#{control}"):
                        found.append(f"#{control} is enabled")
                if not page.is_visible(".planner-placeholder"):
                    found.append("the placeholder is not shown")
                if page.is_visible("#studentSummaryCard"):
                    found.append("the student strip is shown with no student")
                if found:
                    problems[f"{language} {width}px"] = found
                page.context.close()
        self._assert_no_findings(problems)

    def test_student_deep_link_fetches_explicit_term_or_defaults(self) -> None:
        page = self._open("en", *DESKTOP, fetch=False)
        self.addCleanup(page.context.close)
        default_year = page.input_value("#year")
        default_term = page.input_value("#term")
        url = f"{self.live_server_url}/planner/?student={STUDENT_ID}"
        for suffix, year, term in (
            ("", default_year, default_term),
            ("&year=1447&term=2", "1447", "2"),
        ):
            with self.subTest(suffix=suffix):
                with page.expect_request("**/ops/planner/context/") as request:
                    page.goto(url + suffix)
                self.assertEqual(page.input_value("#year"), year)
                self.assertEqual(page.input_value("#term"), term)
                self.assertEqual(request.value.post_data_json["academic_year"], year)
                self.assertEqual(request.value.post_data_json["term"], term)

    # ── 4 ────────────────────────────────────────────────────────────────────
    def test_4_show_everything(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            for width, height in (DESKTOP, PHONE):
                page = self._open(language, width, height)
                found: list[str] = []
                self._show_all(page)
                if page.evaluate(VISIBLE_PANELS) != READING_ORDER:
                    found.append(f"panels on screen: {page.evaluate(VISIBLE_PANELS)}")
                dom = page.evaluate(
                    "() => [...document.querySelectorAll('.step-panel')].map(el => el.id)"
                )
                if dom != READING_ORDER:
                    found.append(f"DOM order: {dom}")
                painted = page.evaluate(
                    "(ids) => { const rtl = document.documentElement.dir === 'rtl';"
                    " return ids.map(id => { const r = document.getElementById(id).getBoundingClientRect();"
                    " return { id, key: [Math.round(r.top), rtl ? -r.left : r.left] }; })"
                    ".sort((a, b) => a.key[0] - b.key[0] || a.key[1] - b.key[1]).map(o => o.id); }",
                    READING_ORDER,
                )
                if painted != READING_ORDER:
                    found.append(f"painted order: {painted}")
                grids = page.evaluate(
                    "() => ({ all: document.querySelectorAll('.wg-blocks').length,"
                    " inside: document.querySelectorAll('#timetableCard #visualGrid .wg-blocks').length,"
                    " hosts: document.querySelectorAll('#visualGrid').length })"
                )
                if grids["hosts"] != 1 or grids["all"] != grids["inside"] or not grids["all"]:
                    found.append(f"week grids: {grids}")
                # the choice is remembered, and shows after the next fetch
                page.reload()
                page.wait_for_function(READY, timeout=20000)
                page.evaluate(SETTLE)
                if not page.evaluate("() => document.getElementById('showAll').checked"):
                    found.append("the switch is off after a reload")
                if page.evaluate(VISIBLE_PANELS) != READING_ORDER:
                    found.append(f"after a reload the panels are {page.evaluate(VISIBLE_PANELS)}")
                page.evaluate("document.getElementById('fetchBtn').click()")
                page.wait_for_function("() => !document.getElementById('fetchBtn').disabled")
                page.evaluate(SETTLE)
                if page.evaluate(VISIBLE_PANELS) != READING_ORDER:
                    found.append(f"after a fetch the panels are {page.evaluate(VISIBLE_PANELS)}")
                # and turning it off again goes back to one step, remembered too
                self._show_all(page, False)
                if page.evaluate(VISIBLE_PANELS) != COURSES_PANELS:
                    found.append(f"switched off, the panels are {page.evaluate(VISIBLE_PANELS)}")
                stored = page.evaluate("localStorage.getItem('plannerShowAll')")
                if stored is not None:
                    found.append(f"switched off, storage holds {stored!r}")
                if found:
                    problems[f"{language} {width}px"] = found
                page.context.close()
        self._assert_no_findings(problems)

    def test_4_the_page_works_without_storage(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            page = self._open(language, *DESKTOP, storage="blocked")
            found: list[str] = []
            if page.evaluate(VISIBLE_PANELS) != COURSES_PANELS:
                found.append(f"panels on screen: {page.evaluate(VISIBLE_PANELS)}")
            page.set_checked("#showAll", True)
            page.evaluate(SETTLE)
            if page.evaluate(VISIBLE_PANELS) != READING_ORDER:
                found.append(f"Show everything did nothing: {page.evaluate(VISIBLE_PANELS)}")
            page.click("#step2")
            page.evaluate(SETTLE)
            if (
                page.evaluate("document.getElementById('step2').getAttribute('aria-current')")
                != "step"
            ):
                found.append("the step button did nothing")
            if found:
                problems[language] = found
            page.context.close()
        self._assert_no_findings(problems)

    # ── 5 ────────────────────────────────────────────────────────────────────
    def test_5_the_step_bar_is_sticky_and_opaque(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            for width, height in (DESKTOP, PHONE):
                page = self._open(language, width, height, show_all=True)
                found: list[str] = []
                reached = page.evaluate(SCROLL_TO, 1500)
                page.evaluate(SETTLE)
                if reached < 1400:
                    found.append(f"the page only scrolled to {reached}px")
                bar = page.evaluate(STICKY)
                if abs(bar["top"] - bar["viewTop"]) > 1:
                    found.append(
                        f"the bar is at {bar['top']}px, the column's top is {bar['viewTop']}px"
                    )
                if not bar["inside"]:
                    found.append(f"the bar is not inside the viewport: {bar}")
                if bar["alpha"] != 1:
                    found.append(f"the bar is not opaque: {bar['bg']}")
                if found:
                    problems[f"{language} {width}px"] = found
                page.context.close()
        self._assert_no_findings(problems)

    # ── 6 ────────────────────────────────────────────────────────────────────
    def test_6_messages_stay_on_screen(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            for width, height in (DESKTOP, PHONE):
                page = self._open(language, width, height, show_all=True)
                found: list[str] = []
                page.evaluate(SCROLL_TO, 1500)
                page.evaluate("document.querySelector('.avail-sec-row[data-tsid=\"2\"]').click()")
                page.evaluate(SETTLE)
                seen = page.evaluate(
                    "() => { const t = document.querySelector('#statusBanner .planner-status-text');"
                    " const r = t.getBoundingClientRect();"
                    " return { text: t.textContent, top: r.top, bottom: r.bottom, width: r.width,"
                    " h: innerHeight }; }"
                )
                if "CS111" not in seen["text"] or "M30" not in seen["text"]:
                    found.append(f"the message is {seen['text']!r}")
                if seen["top"] < 0 or seen["bottom"] > seen["h"] or seen["width"] < 40:
                    found.append(f"the message is not readable on screen: {seen}")
                # a long one: one line, an ellipsis, the whole text in the title
                page.evaluate("(t) => setBanner('info', t)", LONG_TEXT)
                page.evaluate(SETTLE)
                long = page.evaluate(
                    "() => { const b = document.getElementById('statusBanner');"
                    " const t = b.querySelector('.planner-status-text'); const cs = getComputedStyle(t);"
                    " return { title: b.title, cut: t.scrollWidth > t.clientWidth + 1,"
                    " ellipsis: cs.textOverflow, nowrap: cs.whiteSpace,"
                    " height: b.getBoundingClientRect().height,"
                    " line: parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.5,"
                    " top: b.getBoundingClientRect().top }; }"
                )
                if long["title"] != LONG_TEXT:
                    found.append("the full text is not in the title")
                if not long["cut"] or long["ellipsis"] != "ellipsis" or long["nowrap"] != "nowrap":
                    found.append(f"a long message is not cut with an ellipsis: {long}")
                if long["height"] > long["line"] * 2.6 + 16:
                    found.append(f"a long message takes {long['height']}px")
                if long["top"] < 0:
                    found.append("the long message is off screen")
                if found:
                    problems[f"{language} {width}px"] = found
                page.context.close()
        self._assert_no_findings(problems)

    def test_6b_shortlist_actions_do_not_move_the_page(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            for width, height in (DESKTOP, PHONE):
                page = self._open(language, width, height)
                found: list[str] = []
                for action, js in (
                    (
                        "a section row",
                        "document.querySelector('.avail-sec-row[data-tsid=\"2\"]').click()",
                    ),
                    ("Add selected", "document.getElementById('addSelectedPlan').click()"),
                    ("Clear shortlist", "document.getElementById('clearShortlist').click()"),
                ):
                    page.evaluate(SCROLL_TO, 1500)
                    before = page.evaluate(SCROLL_INFO)["top"]
                    page.evaluate(js)
                    page.evaluate(SETTLE)
                    after = page.evaluate(SCROLL_INFO)["top"]
                    if before < 100 or abs(after - before) > 1:
                        found.append(f"{action}: scroll {before} -> {after}")
                if found:
                    problems[f"{language} {width}px"] = found
                page.context.close()
        self._assert_no_findings(problems)

    # ── 7 ────────────────────────────────────────────────────────────────────
    def test_7_build_is_busy_while_it_runs(self) -> None:
        problems: dict[str, list[str]] = {}
        idle = {"en": "Build", "ar": "بناء"}
        busy = {"en": "Building", "ar": "البناء"}
        outcomes = {
            "success": {"json": BUILD},
            "an error answer": {"status": 500, "json": {"error": "the solver failed"}},
            "an answer that is not JSON": {"status": 500, "body": "oops"},
        }
        for language in LANGUAGES:
            for name, answer in outcomes.items():
                page = self._open(language, *DESKTOP)
                found: list[str] = []
                page.click("#nextStep")
                self.hold["build"] = True
                page.click("#runBuilder")
                self._wait_pending(page, "build")
                now = page.evaluate(
                    "() => { const b = document.getElementById('runBuilder');"
                    " return { disabled: b.disabled, busy: b.getAttribute('aria-busy'),"
                    " text: b.textContent.trim(),"
                    " banner: document.getElementById('statusBanner').textContent.trim() }; }"
                )
                if not now["disabled"] or now["busy"] != "true":
                    found.append(f"in flight: {now}")
                if busy[language] not in now["text"]:
                    found.append(f"in flight the label is {now['text']!r}")
                if not now["banner"]:
                    found.append("in flight the status line is empty")
                self.hold["build"] = False
                self._release("build", **answer)
                page.wait_for_function(
                    "() => !document.getElementById('runBuilder').disabled", timeout=10000
                )
                after = page.evaluate(
                    "() => { const b = document.getElementById('runBuilder');"
                    " return { busy: b.getAttribute('aria-busy'), text: b.textContent.trim() }; }"
                )
                if after["busy"] is not None or after["text"] != idle[language]:
                    found.append(f"after {name}: {after}")
                if found:
                    problems[f"{language} {name}"] = found
                page.context.close()
        self._assert_no_findings(problems)

    # ── 8 ────────────────────────────────────────────────────────────────────
    def test_8_the_panels_are_busy_while_a_student_is_fetched(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            for name, answer in (
                ("success", {"json": CONTEXT}),
                ("an error answer", {"json": {"error": "no such student"}}),
            ):
                page = self._open(language, *DESKTOP)
                found: list[str] = []
                busy = "() => document.querySelector('.planner-grid').getAttribute('aria-busy')"
                if page.evaluate(busy) is not None:
                    found.append("busy before any second fetch")
                self.hold["context"] = True
                page.evaluate("document.getElementById('fetchBtn').click()")
                self._wait_pending(page, "context")
                if page.evaluate(busy) != "true":
                    found.append("the panels are not busy during the fetch")
                inactive = page.evaluate(
                    "() => { const cs = getComputedStyle(document.querySelector('.planner-grid'));"
                    " return { opacity: parseFloat(cs.opacity), events: cs.pointerEvents }; }"
                )
                if not inactive["opacity"] < 1 or inactive["events"] != "none":
                    found.append(f"stale content is not inactive: {inactive}")
                self.hold["context"] = False
                self._release("context", **answer)
                page.wait_for_function("() => !document.getElementById('fetchBtn').disabled")
                if page.evaluate(busy) is not None:
                    found.append(f"still busy after {name}")
                if found:
                    problems[f"{language} {name}"] = found
                page.context.close()
        self._assert_no_findings(problems)

    # ── 9 ────────────────────────────────────────────────────────────────────
    def test_9_one_timetable_with_a_source_selector(self) -> None:
        problems: dict[str, list[str]] = {}
        registered = {"en": "Registered", "ar": "المسجّل"}
        for language in LANGUAGES:
            page = self._open(language, *DESKTOP)
            found: list[str] = []
            if page.evaluate(
                "() => !!document.getElementById('baselineWeekly') || !!document.getElementById('baselineCard')"
                " || !!document.getElementById('visualCard')"
            ):
                found.append("the second timetable still exists")
            grid_js = (
                "() => ({ visible: [...document.querySelectorAll('.wg-blocks')]"
                ".filter(e => e.getClientRects().length && e.closest('#timetableCard')).length,"
                " stray: [...document.querySelectorAll('.wg-blocks')].filter(e => !e.closest('#timetableCard')).length,"
                " cards: document.querySelectorAll('#timetableCard').length })"
            )
            for step in ("courses", "build"):
                if step == "build":
                    page.click("#nextStep")
                    page.evaluate(SETTLE)
                grids = page.evaluate(grid_js)
                if grids["cards"] != 1 or grids["stray"] or not grids["visible"]:
                    found.append(f"{step} step: {grids}")
            title = page.inner_text("#timetableCard .pane-title")
            if title.strip() != ("الجدول" if language == "ar" else "Timetable"):
                found.append(f"the panel title is {title!r}")
            first = page.evaluate(
                "() => document.getElementById('visualSource').options[0].textContent"
            )
            if first.strip() != registered[language]:
                found.append(f"the first source is {first!r}")
            totals = page.evaluate(
                "() => { const t = document.getElementById('baselineTotals');"
                " return { shown: t.getClientRects().length > 0, text: t.textContent }; }"
            )
            if not totals["shown"] or "5" not in totals["text"]:
                found.append(f"registered view totals: {totals}")
            count = "() => document.querySelectorAll('#visualGrid .wg-filled').length"
            before = page.evaluate(count)
            page.click("#runBuilder")
            page.wait_for_function(
                "() => document.querySelectorAll('#optionCards [data-opt]').length >= 3",
                timeout=10000,
            )
            page.select_option("#visualSource", "B1")
            page.evaluate(SETTLE)
            built = page.evaluate(count)
            if built <= before:
                found.append(f"a built option draws {built} blocks, the registered week {before}")
            if page.evaluate("document.getElementById('baselineTotals').getClientRects().length"):
                found.append("the registered totals show beside a built option")
            page.select_option("#visualSource", "baseline")
            page.evaluate(SETTLE)
            if page.evaluate(count) != before:
                found.append("back to registered the grid is not the registered week")
            if not page.evaluate(
                "document.getElementById('baselineTotals').getClientRects().length"
            ):
                found.append("the registered totals do not return")
            if found:
                problems[language] = found
            page.context.close()
        self._assert_no_findings(problems)

    # ── 10 ───────────────────────────────────────────────────────────────────
    def test_10_the_phone_bar_fits(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            for font in ("app", "wide"):
                for width, height in ((390, 844), (360, 800)):
                    page = self._open(language, width, height, font=font)
                    found = page.evaluate(BAR_FIT)
                    # On a phone Back / Next show only their arrow, so the arrow
                    # must say which is which: Back points to where the line
                    # starts (left in English, right in Arabic), Next the other
                    # way; the words stay as the accessible name.
                    for arrow in page.evaluate(NAV_ARROWS):
                        back_points = "right" if language == "ar" else "left"
                        want = (
                            back_points
                            if arrow["id"] == "prevStep"
                            else ("left" if back_points == "right" else "right")
                        )
                        if arrow["points"] != want:
                            found.append(
                                f"#{arrow['id']} arrow points {arrow['points']}, not {want}"
                            )
                        if arrow["labelWidth"] > 1.5 or not arrow["name"]:
                            found.append(f"#{arrow['id']} label: {arrow}")
                    height_of_bar = page.evaluate(BAR_RECT)["height"]
                    if height_of_bar > 150:
                        found.append(f"the bar is {height_of_bar}px tall")
                    page.evaluate("(t) => setBanner('info', t)", LONG_TEXT)
                    page.evaluate(SETTLE)
                    found += [f"long message: {f}" for f in page.evaluate(BAR_FIT)]
                    if found:
                        problems[f"{language} {width}px {font} fonts"] = found
                    page.context.close()
        self._assert_no_findings(problems)

    # ── 11 ───────────────────────────────────────────────────────────────────
    def test_11_steps_are_tabs_not_pills(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            for theme in ("light", "dark"):
                page = self._open(language, *DESKTOP, theme=theme)
                found: list[str] = []
                page.click("#nextStep")
                page.evaluate(SETTLE)
                seen = page.evaluate(
                    "() => { const probe = document.createElement('i'); document.body.append(probe);"
                    " probe.style.color = getComputedStyle(document.documentElement)"
                    ".getPropertyValue('--text-brand').trim();"
                    " const ink = getComputedStyle(probe).color; probe.remove();"
                    " const read = id => { const el = document.getElementById(id);"
                    " const cs = getComputedStyle(el); return { bg: cs.backgroundColor,"
                    " width: cs.borderBottomWidth, style: cs.borderBottomStyle,"
                    " edge: cs.borderBottomColor, color: cs.color, weight: cs.fontWeight,"
                    " check: getComputedStyle(el.querySelector('.wf-check')).display }; };"
                    " return { ink, current: read('step2'), done: read('step1') }; }"
                )
                now, done, ink = seen["current"], seen["done"], seen["ink"]
                if now["bg"] != "rgba(0, 0, 0, 0)":
                    found.append(f"the current step has a fill: {now['bg']}")
                if now["width"] != "2px" or now["style"] != "solid" or now["edge"] != ink:
                    found.append(f"the current step's underline: {now} (ink {ink})")
                if now["color"] != ink or int(now["weight"]) < 700:
                    found.append(f"the current step's label: {now}")
                if done["edge"] == ink and done["width"] != "0px":
                    found.append(f"a completed step is underlined: {done}")
                if done["check"] == "none" or now["check"] != "none":
                    found.append(f"the check: completed {done['check']}, current {now['check']}")
                if found:
                    problems[f"{language} {theme}"] = found
                page.context.close()
        self._assert_no_findings(problems)

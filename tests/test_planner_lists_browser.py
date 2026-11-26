"""The staff planner (/planner/): its lists and results read plainly and promise nothing false.

Measured in a real browser on the synthetic student of tests/test_planner_layout_browser.py
(imported, not copied), with the build request answered from a made-up response below. Nothing
leaves the machine (tests/browser_isolation.py).

1  Suggested courses: status and priority are translated in the Arabic UI, and what is sent to
   the server is not;
3  the shortlist is one compact row per course with a quiet icon-only Remove;
4  a section's meetings are grouped by time, one line per distinct time;
5  the importance chip and the status chips clear 4.5:1 in both themes, with no inline style;
6  option cards carry plain method names, and the selected card is unmistakable;
7  unscheduled courses are grouped by reason, in both languages, with server text escaped;
8  nothing shows a hand cursor unless clicking it does something.
"""

from __future__ import annotations

import copy
import os

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client

from tests.test_advisor_bidi import VISUAL_ORDER_JS
from tests.test_planner_layout_browser import (
    CONTEXT,
    READY,
    SECTIONS,
    SECTIONS_CATALOG,
    STUDENT_PLAN,
    PlannerLayoutBrowserTests,
    _days,
    _section,
)

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

LANGUAGES = ("en", "ar")

# ── The made-up data this file adds to the shared fixture ───────────────────

LISTS_CONTEXT = copy.deepcopy(CONTEXT)
for _rec in LISTS_CONTEXT["recommendations"]:
    if _rec["course_code"] == "CS112":
        _rec["status"] = "Blocked"  # the server's word (core/planner_views.py)
        # markup in server text must be shown, never run
        _rec["course_name"] = "PROGRAMMING II <i>x</i>"
        _rec["missing_prerequisites"] = ["CS111", "<u>y</u>"]

LISTS_PLAN = copy.deepcopy(STUDENT_PLAN)
_TERM_ONE = LISTS_PLAN["terms"][0]["courses"]
for _course in _TERM_ONE:
    # every importance tone: high (>=3), mid (>=1), low
    _course["importance_score"] = {"CS111": 0.5, "ENG101": 2.0}.get(
        _course["course_code"], _course["importance_score"]
    )
for _code, _can in (("ZZ101", True), ("FULL101", True)):  # no section / only a full section
    _TERM_ONE.append(
        {
            **_TERM_ONE[0],
            "course_code": _code,
            "course_name": _code,
            "status": "not_taken",
            "can_register": _can,
            "prerequisites": [],
            "missing_prereqs": [],
            "importance_score": 4.0,
        }
    )

LISTS_CATALOG = {
    **SECTIONS_CATALOG,
    "sections": [
        *SECTIONS,
        _section(
            16,
            "FULL101",
            "101",
            "M99",
            _days(["TUE"], ("08:00", "09:15")),
            programs=["AI2"],
            registered=30,
            capacity=30,
        ),
    ],
}


def _lost(code: str, reason: str, reason_code: str, *details: dict) -> dict:
    return {
        "course_code": code,
        "reason": reason,
        "reason_code": reason_code,
        "details": list(details),
    }


_FIT = {"course_credits": 3, "credit_limit": 18, "sections_considered": 2}
_FIT_EN = (
    "Could not fit alongside the other courses within the 18-credit limit, or its sections clash"
)


def _option(name: str, scheduled: int, unscheduled: list[dict]) -> dict:
    return {
        "name": name,
        "method": name[0],
        "rank": 1,
        "scheduled": scheduled,
        "target": 5,
        "mappings": [
            {
                "course_code": "CS111",
                "course_key": "CS111",
                "course_number": "111",
                "section": "M30",
                "term_section_id": 2,
                "meetings": [{"day": "SUN", "start_time": "10:00", "end_time": "11:15"}],
                "meeting_issue_codes": [],
            }
        ],
        "unscheduled": unscheduled,
    }


BUILD = {
    "summary": {
        "scheduled": 3,
        "target": 5,
        "conflicts": 3,
        "swaps_required": 0,
        "best_feasible": True,
        "hard_constraint_failures": [],
    },
    "options": [
        # the best (most scheduled): three courses that all did not fit
        _option(
            "A1",
            3,
            [_lost(code, _FIT_EN, "DID_NOT_FIT", _FIT) for code in ("PHYS103", "CS112", "ENG102")],
        ),
        # a code this page has never heard of, with markup in its sentence
        _option("B1", 2, [_lost("CS112", "<b>odd</b> reason", "NEW_CODE_NOBODY_KNOWS")]),
        _option(
            "C1",
            2,
            [
                _lost("MATH105", "Every section clashes", "ALL_SECTIONS_CLASH", {}),
                _lost(
                    "GS101",
                    "GS101 is 20 credits, above the 18-credit limit set for this build",
                    "DID_NOT_FIT",
                    {"course_credits": 20, "credit_limit": 18},
                ),
            ],
        ),
    ],
    "unscheduled": [],
    "swap_suggestions": [],
}
NO_PLAN = {
    "summary": {
        "scheduled": 0,
        "target": 5,
        "conflicts": 2,
        "swaps_required": 0,
        "best_feasible": False,
        "hard_constraint_failures": [],
    },
    "options": [],
    "unscheduled": [
        _lost("PHYS103", _FIT_EN, "DID_NOT_FIT", _FIT),
        _lost("CS112", _FIT_EN, "DID_NOT_FIT", _FIT),
    ],
    "swap_suggestions": [],
}

# "Keep registered" with 15 hours kept under a limit of 18: the limit is the whole
# load, so each reason carries the hours already registered (the build's own shape).
_KEPT_FIT = {**_FIT, "registered_credits": 15}
_KEPT_FIT_EN = (
    "Could not fit alongside the other courses within the 18-credit limit "
    "(15 of it registered), or its sections clash"
)
KEPT_BUILD = {
    "summary": {
        "scheduled": 1,
        "target": 4,
        "conflicts": 3,
        "swaps_required": 0,
        "best_feasible": True,
        "hard_constraint_failures": [],
    },
    "options": [
        _option(
            "A1",
            1,
            [
                _lost("PHYS103", _KEPT_FIT_EN, "DID_NOT_FIT", _KEPT_FIT),
                _lost("ENG102", _KEPT_FIT_EN, "DID_NOT_FIT", _KEPT_FIT),
                # alone it is more than the 3 hours the registered courses leave
                _lost(
                    "GS101",
                    "GS101 is 4 credits, above the 18-credit limit set for this build "
                    "(15 of it registered)",
                    "DID_NOT_FIT",
                    {"course_credits": 4, "credit_limit": 18, "registered_credits": 15},
                ),
            ],
        )
    ],
    "unscheduled": [],
    "swap_suggestions": [],
}


def _no_room(registered: int) -> dict:
    """The build's reply when the registered load leaves nothing of an 18-hour limit."""
    sentence = f"No room left under the 18-credit limit: {registered} credits are registered"
    return {
        "summary": {
            "scheduled": 0,
            "target": 2,
            "conflicts": 2,
            "swaps_required": 0,
            "best_feasible": False,
            "hard_constraint_failures": [
                {"kind": "max_credits", "course_code": "", "reason": sentence}
            ],
        },
        "options": [],
        "unscheduled": [
            _lost(
                code,
                sentence,
                "DID_NOT_FIT",
                {"course_credits": credits, "credit_limit": 18, "registered_credits": registered},
            )
            for code, credits in (("PHYS103", 4), ("CS112", 3))
        ],
        "swap_suggestions": [],
        "constraints": {
            "program_sections_only": True,
            "allow_full_sections": False,
            "registered_credits": registered,
            "credit_room": 0,
        },
    }


# What the page says when a build has no option at all: in general, and when the
# registered courses already fill the limit.
NO_RESULT_SAID = {
    "en": (
        "No valid result satisfies every must-take course and exact pinned section "
        "under the current constraints."
    ),
    "ar": "لا توجد نتيجة صالحة تحقق جميع المقررات الإلزامية والشُعب المثبّتة ضمن القيود الحالية.",
}
NO_ROOM_SAID = {
    "en": "Nothing can be added: the registered courses use the whole credit limit.",
    "ar": "لا يمكن إضافة مقررات: المقررات المسجلة تستنفد حد الساعات كاملًا.",
}
# The status strip and the options area; then the summary's inner width and the width
# of each line it holds after its four figures (failures, the heading, the reasons).
# Those were squeezed into columns a few words wide.
NO_RESULT_SHOWN = r"""() => {
  const text = id => document.querySelector(id).textContent.replace(/\s+/g, ' ').trim();
  const box = document.getElementById('builderSummary');
  const cs = getComputedStyle(box);
  return { status: text('#statusBanner'), options: text('#optionCards .planner-banner'),
           inner: Math.round(box.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)),
           lines: [...box.children].slice(4).map(c => Math.round(c.getBoundingClientRect().width)) };
}"""


METHOD_NAMES = {
    "en": {
        "A1": "Most courses scheduled",
        "B1": "Compact week: few days and few gaps together",
        "C1": "Fewest days first, then fewest gaps",
    },
    "ar": {
        "A1": "أكبر عدد من المقررات",
        "B1": "أسبوع مضغوط: أيام وفجوات أقل معًا",
        "C1": "أقل أيام أولًا ثم أقل فجوات",
    },
}
DAYS = {"en": ["Sun", "Mon", "Tue", "Wed", "Thu"], "ar": ["أحد", "اثن", "ثلا", "أرب", "خم"]}

# ── What is measured ────────────────────────────────────────────────────────

# The ratio of each element's text colour to its background, the translucent layers of the
# element and its ancestors painted over the first opaque one.
CONTRAST = r"""(selector) => {
  const cv = document.createElement('canvas'); cv.width = cv.height = 1;
  const cx = cv.getContext('2d', { willReadFrequently: true });
  const rgba = v => { cx.clearRect(0, 0, 1, 1); cx.fillStyle = 'rgba(0,0,0,0)'; cx.fillStyle = v; cx.fillRect(0, 0, 1, 1);
    const d = cx.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3] / 255]; };
  const lum = c => c.slice(0, 3).map(x => { x /= 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; })
    .reduce((s, x, i) => s + x * [0.2126, 0.7152, 0.0722][i], 0);
  const over = (top, under) => top.slice(0, 3).map((x, i) => x * top[3] + under[i] * (1 - top[3]));
  const backdrop = el => {
    const layers = [];
    for (let a = el; a; a = a.parentElement) {
      const c = rgba(getComputedStyle(a).backgroundColor);
      if (c[3] > 0) layers.push(c);
      if (c[3] >= 1) break;
    }
    let base = layers.length && layers[layers.length - 1][3] >= 1 ? layers.pop().slice(0, 3) : [255, 255, 255];
    while (layers.length) base = over(layers.pop(), base);
    return base;
  };
  return [...document.querySelectorAll(selector)].map(el => {
    const cs = getComputedStyle(el);
    const bg = over(rgba(cs.backgroundColor), backdrop(el.parentElement));
    const fg = over(rgba(cs.color), bg);
    const a = lum(fg), b = lum(bg);
    return { label: el.textContent.trim().slice(0, 30), tone: el.dataset.tone || el.dataset.state || '',
             ratio: +((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)).toFixed(2),
             style: el.getAttribute('style') };
  });
}"""

# Every element inside the planner that shows a hand cursor although nothing there is clickable.
FALSE_AFFORDANCES = r"""() => {
  const root = document.querySelector('.planner-grid');
  const found = [];
  for (const el of root.querySelectorAll('*')) {
    if (getComputedStyle(el).cursor !== 'pointer') continue;
    if (el.closest('button, a[href], summary, label, input, select, tr.is-addable')) continue;
    const cls = [...el.classList].slice(0, 2).map(c => '.' + c).join('');
    found.push(`${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}${cls}`);
  }
  return [...new Set(found)];
}"""

PAGE_TEXT = r"""() => {
  const out = [];
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
    acceptNode: n => n.parentElement.closest('script, style, template, noscript')
      ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
  for (let n = walk.nextNode(); n; n = walk.nextNode()) out.push(n.data);
  for (const el of document.querySelectorAll('[title], [aria-label]'))
    out.push(el.getAttribute('title') || '', el.getAttribute('aria-label') || '');
  for (const o of document.querySelectorAll('option')) out.push(o.textContent);
  return out.join(' ');
}"""

PLANNER_READY_AGAIN = "() => !document.getElementById('fetchBtn').disabled"

# Per reason line, the text of each isolated run (<bdi>), in order.
ISOLATED_PER_LINE = "ns => ns.map(n => [...n.querySelectorAll('bdi')].map(b => b.textContent))"


class PlannerListsBrowserTests(StaticLiveServerTestCase):
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
            username="planner-lists", email="planner-lists@example.test", password="unused"
        )
        client = Client()
        client.force_login(user)
        self.session = client.cookies[settings.SESSION_COOKIE_NAME].value
        self.page_errors: list[str] = []
        self.build_bodies: list[dict] = []
        self.addCleanup(self._no_page_errors)

    def _no_page_errors(self) -> None:
        self.assertEqual(self.page_errors, [], "the planner threw")

    # The shared fixture's page-opening and reporting, not a copy of them.
    _open = PlannerLayoutBrowserTests._open
    _assert_no_findings = PlannerLayoutBrowserTests._assert_no_findings

    def _load(self, language: str, width: int = 1440, height: int = 900, build=None):
        """The planner with this file's student loaded (one more fetch on the shared one)."""
        page, _sent = self._open(language, width, height)
        context = page.context

        def answer_build(route) -> None:
            self.build_bodies.append(route.request.post_data_json)
            route.fulfill(json=build or BUILD)

        context.route("**/ops/planner/context/", lambda r: r.fulfill(json=LISTS_CONTEXT))
        context.route("**/report/student-plan/**", lambda r: r.fulfill(json=LISTS_PLAN))
        context.route("**/ops/planner/sections-catalog/", lambda r: r.fulfill(json=LISTS_CATALOG))
        context.route("**/ops/planner/build/", answer_build)
        page.evaluate("document.getElementById('fetchBtn').click()")
        page.wait_for_function(PLANNER_READY_AGAIN, timeout=20000)
        page.wait_for_function(READY, timeout=20000)
        page.evaluate(
            "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))"
        )
        return page

    def _build(self, page, options: int = 3) -> None:
        page.evaluate("document.getElementById('runBuilder').click()")
        if options:
            page.wait_for_function(
                "n => document.querySelectorAll('#optionCards [data-opt]').length >= n", arg=options
            )
        else:
            page.wait_for_function(
                "() => document.getElementById('builderSummary').textContent.includes('PHYS103')"
            )

    @staticmethod
    def _card_state(page) -> dict[str, tuple[bool, str, str | None]]:
        """Per option card: selected?, its button's text, its button's aria-pressed."""
        return {
            name: tuple(
                page.eval_on_selector(
                    f'#optionCards [data-opt="{name}"]',
                    "c => [c.classList.contains('is-selected'),"
                    " c.querySelector('button').textContent.trim(),"
                    " c.querySelector('button').getAttribute('aria-pressed')]",
                )
            )
            for name in ("A1", "B1", "C1")
        }

    @staticmethod
    def _reason_lines(page, option: str) -> list[str]:
        return page.eval_on_selector_all(
            f'#builderOptions [data-diag="{option}"] .opt-reason',
            # textContent: the diagnostics sit in a closed <details>
            "ns => ns.map(n => n.textContent.replace(/\\s+/g, ' ').trim())",
        )

    def _set_theme(self, page, theme: str) -> None:
        if page.evaluate("document.documentElement.dataset.theme") != theme:
            page.click("#themeToggle")
            page.wait_for_function("t => document.documentElement.dataset.theme === t", arg=theme)
        page.evaluate(
            "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))"
        )

    # ── 1. Suggested courses ─────────────────────────────────────────────────
    def test_suggestions_are_translated_and_the_request_is_not(self) -> None:
        words = {
            "ar": {
                "Eligible": "مؤهل",
                "Blocked": "غير مؤهل",
                "High": "عالية",
                "Med": "متوسطة",
                "Low": "منخفضة",
            },
            "en": {
                "Eligible": "Eligible",
                "Blocked": "Blocked",
                "High": "High",
                "Med": "Med",
                "Low": "Low",
            },
        }
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            page = self._load(language)
            text = page.eval_on_selector("#recTable", "n => n.innerText")
            found = []
            for shown in words[language].values():
                if shown not in text:
                    found.append(f"{shown!r} is not shown")
            if language == "ar":
                for latin in ("Eligible", "Blocked", "High", "Med", "Low"):
                    if latin in text:
                        found.append(f"the Latin word {latin!r} is shown in the Arabic UI")
            tones = page.eval_on_selector_all(
                "#recTable .rec-status", "ns => ns.map(n => n.dataset.tone)"
            )
            if sorted(set(tones)) != ["blocked", "ok"]:
                found.append(f"status chips carry tones {tones}")
            if page.query_selector("#recTable .badge"):
                found.append("a Bootstrap badge is still the status")
            # server text is escaped
            if page.query_selector("#recTable i, #recTable u"):
                found.append("markup in server text ran")
            if "<i>x</i>" not in text or "<u>y</u>" not in text:
                found.append(f"server text is not shown whole: {text!r}")
            self._build(page)
            sent = self.build_bodies[-1]["shortlist"]
            if not sent or {c["status"] for c in sent} != {"Eligible"}:
                found.append(f"the request's statuses: {[c['status'] for c in sent]}")
            if {c["priority"] for c in sent} - {"High", "Med", "Low"}:
                found.append(f"the request's priorities: {[c['priority'] for c in sent]}")
            if found:
                problems[language] = found
            page.context.close()
        self._assert_no_findings(problems)

    # ── 3. The shortlist ─────────────────────────────────────────────────────
    def test_shortlist_is_one_compact_row_per_course(self) -> None:
        label = {"en": "Must-take — every result", "ar": "إلزامي — في كل نتيجة"}
        remove = {"en": "Remove", "ar": "إزالة"}
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            page = self._load(language)
            found: list[str] = []
            # pin one section, so a row with a pinned chip is measured too
            page.click('.avail-sec-row[data-tsid="2"]')
            page.wait_for_selector("#shortlist .sl-pin-badge")
            rows = page.query_selector_all("#shortlist .sl-row")
            if len(rows) != 4:
                found.append(f"{len(rows)} rows for 4 courses")
            for row in rows:
                height = row.bounding_box()["height"]
                name = row.inner_text().split()[0]
                if height > 48.5:
                    found.append(f"{name}: the row is {height:.1f}px tall (limit 48)")
                if row.evaluate("n => getComputedStyle(n).borderTopWidth") != "0px":
                    found.append(f"{name}: the row is a box, not a rule")
                if row.query_selector("label").inner_text().strip() != label[language]:
                    found.append(f"{name}: the must-take label changed")
                button = row.query_selector("button[data-i]")
                if button.inner_text().strip():
                    found.append(f"{name}: the remove control shows text {button.inner_text()!r}")
                if button.get_attribute("aria-label") != f"{remove[language]} {name}":
                    found.append(f"{name}: aria-label {button.get_attribute('aria-label')!r}")
                if not button.get_attribute("title"):
                    found.append(f"{name}: the remove control has no title")
                classes = (button.get_attribute("class") or "").split()
                if not {"btn", "btn-icon"} <= set(classes):
                    found.append(f"{name}: remove classes {classes}")
            page.click(f'#shortlist button[aria-label="{remove[language]} CS111"]')
            page.wait_for_function(
                "() => document.querySelectorAll('#shortlist .sl-row').length === 3"
            )
            if "CS111" in page.eval_on_selector("#shortlist", "n => n.innerText"):
                found.append("CS111 is still listed after Remove")
            if page.eval_on_selector("#shortCredits", "n => n.textContent") != "9":
                found.append("the credits total did not follow the removal")
            if found:
                problems[language] = found
            page.context.close()
        self._assert_no_findings(problems)

    # ── 4. Grouped meetings ──────────────────────────────────────────────────
    def test_meetings_are_grouped_by_time(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            days = DAYS[language]
            page = self._load(language)
            found: list[str] = []
            row = '.avail-sec-row[data-tsid="4"]'  # ENG101 M45: 8 meetings, 2 times, 4 days
            lines = page.eval_on_selector_all(
                f"{row} .sec-time .sec-time-line",
                "ns => ns.map(n => n.innerText.replace(/\\s+/g, ' ').trim())",
            )
            four = " ".join([days[0], days[1], days[3], days[4]])
            want = [f"{four} 08:00-09:40", f"{four} 09:50-11:30"]
            if lines != want:
                found.append(f"time lines {lines}, wanted {want}")
            aria = page.get_attribute(row, "aria-label")
            if ", ".join(want) not in aria:
                found.append(f"aria-label {aria!r} lacks the grouped text")
            seen = page.evaluate(VISUAL_ORDER_JS, f"{row} .sec-time")
            for start, end in (("08:00", "09:40"), ("09:50", "11:30")):
                if seen.count(f"{start}-{end}") != 1 or f"{end}-{start}" in seen:
                    found.append(f"{start}-{end} is not read start first, once: {seen!r}")
            other = page.eval_on_selector_all(
                '.avail-sec-row[data-tsid="2"] .sec-time .sec-time-line',
                "ns => ns.map(n => n.innerText.replace(/\\s+/g, ' ').trim())",
            )
            if other != [f"{days[0]} {days[2]} 10:00-11:15"]:
                found.append(f"M30 reads {other}")
            if found:
                problems[language] = found
            page.context.close()
        self._assert_no_findings(problems)

    # ── 5. Chips ─────────────────────────────────────────────────────────────
    def test_chips_clear_contrast_in_both_themes_without_inline_style(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            page = self._load(language)
            self._build(page)
            found: list[str] = []
            for theme in ("light", "dark"):
                self._set_theme(page, theme)
                for what, selector, tones in (
                    ("importance", ".avail-course-head [data-tone]", {"high", "mid", "low"}),
                    ("status", "#recTable .rec-status", {"ok", "blocked"}),
                    ("offer", "#planPalette .pl-offer", {"open", "full", "none", "prereq"}),
                ):
                    chips = page.evaluate(CONTRAST, selector)
                    if {c["tone"] for c in chips} != tones:
                        found.append(f"{theme} {what}: tones {sorted({c['tone'] for c in chips})}")
                    for chip in chips:
                        if chip["style"]:
                            found.append(f"{theme} {what}: inline style {chip['style']!r}")
                    low = [c for c in chips if c["ratio"] < 4.5]
                    if low:
                        first = low[0]
                        found.append(
                            f"{theme} {what}: {len(low)} of {len(chips)} below 4.5:1 "
                            f"(first {first['tone']} {first['ratio']}:1)"
                        )
            if found:
                problems[language] = found
            page.context.close()
        self._assert_no_findings(problems)

    def test_legend_dots_match_the_offer_chips(self) -> None:
        page = self._load("en")
        legend = {"none": "black", "full": "red", "open": "green", "prereq": "purple"}
        found = []
        for state, dot in legend.items():
            chip = page.eval_on_selector(
                f'#planPalette .pl-offer[data-state="{state}"]', "n => getComputedStyle(n).color"
            )
            shade = page.eval_on_selector(
                f".pl-legend-dot-{dot}", "n => getComputedStyle(n).backgroundColor"
            )
            if chip != shade:
                found.append(f"{state}: the chip is {chip} and its legend dot {shade}")
        self._assert_no_findings({"en": found} if found else {})

    # ── 6. Option cards ──────────────────────────────────────────────────────
    def test_option_cards_name_their_method_and_show_the_selected_one(self) -> None:
        select = {"en": ("Select", "Selected"), "ar": ("اختيار", "محدد")}
        option = {"en": "Option", "ar": "الخيار"}
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            page = self._load(language)
            self._build(page)
            found: list[str] = []
            for name, method in METHOD_NAMES[language].items():
                got = page.eval_on_selector(
                    f'#optionCards [data-opt="{name}"] .opt-method', "n => n.textContent.trim()"
                )
                if got != method:
                    found.append(f"{name}: method line {got!r}, wanted {method!r}")
            text = page.evaluate(PAGE_TEXT)
            for word in ("Bitmask", "DFS", "بت ماسك"):
                if word in text:
                    found.append(f"{word!r} is on the page")
            visual = page.eval_on_selector_all(
                "#visualSource option", "ns => ns.map(n => n.textContent)"
            )
            if f"{option[language]} A1" not in visual:
                found.append(f"visual sources {visual}")

            plain, chosen = select[language]
            before = self._card_state(page)
            want = {
                "A1": (True, chosen, "true"),
                "B1": (False, plain, "false"),
                "C1": (False, plain, "false"),
            }
            if before != want:
                found.append(f"selected state {before}, wanted {want}")
            cursor = page.eval_on_selector_all(
                "#optionCards [data-opt]", "ns => ns.map(n => getComputedStyle(n).cursor)"
            )
            if "pointer" in cursor:
                found.append(f"a card shows a hand cursor: {cursor}")
            page.click('#optionCards [data-opt="B1"] button')
            after = self._card_state(page)
            want = {
                "A1": (False, plain, "false"),
                "B1": (True, chosen, "true"),
                "C1": (False, plain, "false"),
            }
            if after != want:
                found.append(f"after choosing B1: {after}, wanted {want}")
            if found:
                problems[language] = found
            page.context.close()
        self._assert_no_findings(problems)

    # ── 7. Diagnostics ───────────────────────────────────────────────────────
    def test_unscheduled_courses_are_grouped_by_reason(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            page = self._load(language)
            self._build(page)
            found: list[str] = []

            a1 = self._reason_lines(page, "A1")
            if len(a1) != 1:
                found.append(f"A1 has {len(a1)} reason lines: {a1}")
            elif language == "en":
                want = (
                    "Not scheduled (3): PHYS103, CS112, ENG102 — "
                    "Could not fit within the 18-credit limit, or its sections clash"
                )
                if a1[0] != want:
                    found.append(f"A1 reads {a1[0]!r}")
            else:
                line = a1[0]
                for part in (
                    "PHYS103",
                    "CS112",
                    "ENG102",
                    "تعذّر إدراجه ضمن حد",
                    "ساعة، أو أن شُعبه متعارضة",
                ):
                    if part not in line:
                        found.append(f"A1 (ar) lacks {part!r}: {line!r}")
                if "Could not" in line or "credit" in line:
                    found.append(f"A1 (ar) has English: {line!r}")
                isolated = page.eval_on_selector_all(
                    '#builderOptions [data-diag="A1"] .opt-reason bdi',
                    "ns => ns.map(n => n.textContent)",
                )
                if "18" not in isolated:
                    found.append(f"the limit is not isolated: {isolated}")
            b1 = self._reason_lines(page, "B1")
            if len(b1) != 1 or "<b>odd</b> reason" not in b1[0]:
                found.append(f"B1 (unknown code) reads {b1}")
            if page.query_selector('#builderOptions [data-diag="B1"] b'):
                found.append("markup in a server reason ran")
            c1 = self._reason_lines(page, "C1")
            if len(c1) != 2:
                found.append(f"C1 has {len(c1)} reason lines: {c1}")
            elif language == "en" and not any(
                "20 credits, above the 18-credit limit of this build" in x for x in c1
            ):
                found.append(f"C1 lacks the over-limit sentence: {c1}")
            if page.query_selector("#builderOptions .text-danger"):
                found.append("a wall of red text is back")
            if found:
                problems[language] = found
            page.context.close()
        self._assert_no_findings(problems)

    def test_no_plan_summary_uses_the_same_reasons(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            page = self._load(language, build=NO_PLAN)
            self._build(page, options=0)
            text = page.eval_on_selector(
                "#builderSummary", "n => n.innerText.replace(/\\s+/g, ' ')"
            )
            found = []
            if "PHYS103, CS112" not in text:
                found.append(f"one line for both courses is missing: {text!r}")
            if language == "en" and "18-credit limit, or its sections clash" not in text:
                found.append(f"the plain reason is missing: {text!r}")
            if language == "ar" and "تعذّر إدراجه ضمن حد" not in text:
                found.append(f"the Arabic reason is missing: {text!r}")
            found += self._no_result_findings(page, NO_RESULT_SAID[language])
            if found:
                problems[language] = found
            page.context.close()
        self._assert_no_findings(problems)

    @staticmethod
    def _no_result_findings(page, sentence: str) -> list[str]:
        """A build with no option: what the page says twice, and a summary one can read."""
        shown = page.evaluate(NO_RESULT_SHOWN)
        found = []
        for where in ("status", "options"):
            if shown[where] != sentence:
                found.append(f"the {where} area reads {shown[where]!r}")
        # each failure, the heading and the reasons take the summary's full width
        narrow = [width for width in shown["lines"] if abs(width - shown["inner"]) > 1]
        if narrow or len(shown["lines"]) < 2:
            found.append(f"the summary's lines are {shown['lines']}px in {shown['inner']}px")
        return found

    def test_a_reason_says_how_much_of_the_limit_is_registered(self) -> None:
        """Keep registered: the limit covers the kept hours, and each reason says so.

        Without it a course that only lacks room reads "or its sections clash", and the
        adviser goes looking for a clash that is not there.
        """
        want = {
            "en": {
                "beside": (
                    "Not scheduled (2): PHYS103, ENG102 — Could not fit within the "
                    "18-credit limit (15 of it registered), or its sections clash"
                ),
                "alone": (
                    "Not scheduled (1): GS101 — 4 credits, above the 3 left of the "
                    "18-credit limit (15 registered)"
                ),
                "full": "No room left under the 18-credit limit: {registered} credits are registered",
            },
            "ar": {
                "beside": "تعذّر إدراجه ضمن حد 18 ساعة (المسجّل منها 15)، أو أن شُعبه متعارضة",
                "alone": "4 ساعات، والمتبقي من حد 18 ساعة 3 فقط (المسجّل 15)",
                "full": "لا متّسع ضمن حد 18 ساعة: الساعات المسجلة {registered}",
            },
        }
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            found: list[str] = []

            page = self._load(language, build=KEPT_BUILD)
            self._build(page, options=1)
            lines = self._reason_lines(page, "A1")
            if len(lines) != 2:
                found.append(f"A1 has {len(lines)} reason lines: {lines}")
            for case in ("beside", "alone"):
                sentence = want[language][case]
                exact = language == "en"
                if not any((line == sentence) if exact else (sentence in line) for line in lines):
                    found.append(f"A1 lacks the {case!r} sentence: {lines}")
            if language == "ar":
                if any("credit" in line or "Could not" in line for line in lines):
                    found.append(f"A1 (ar) has English: {lines}")
                # every number of each sentence is isolated, so it reads left to right
                isolated = page.eval_on_selector_all(
                    '#builderOptions [data-diag="A1"] .opt-reason', ISOLATED_PER_LINE
                )
                if isolated != [
                    ["2", "PHYS103", "ENG102", "18", "15"],
                    ["1", "GS101", "4", "18", "3", "15"],
                ]:
                    found.append(f"A1 (ar) isolates {isolated}")
            page.context.close()

            # a load at the limit, and one already above it
            for registered in (18, 21):
                page = self._load(language, build=_no_room(registered))
                self._build(page, options=0)
                text = page.eval_on_selector(
                    "#builderSummary", "n => n.textContent.replace(/\\s+/g, ' ')"
                )
                # one line for both courses, whatever their hours: neither has any room
                if "PHYS103, CS112" not in text:
                    found.append(f"{registered}: one line for both courses is missing: {text!r}")
                if want[language]["full"].format(registered=registered) not in text:
                    found.append(f"{registered}: the no-room reason is missing: {text!r}")
                if "clash" in text or "متعارضة" in text:
                    found.append(f"{registered}: a full load is blamed on a clash: {text!r}")
                if language == "ar":
                    isolated = page.eval_on_selector_all(
                        "#builderSummary .opt-reason", ISOLATED_PER_LINE
                    )
                    if isolated != [["2", "PHYS103", "CS112", "18", str(registered)]]:
                        found.append(f"{registered}: the no-room line (ar) isolates {isolated}")
                # said where the adviser looks first, not as a must-take problem
                found += [
                    f"{registered}: {finding}"
                    for finding in self._no_result_findings(page, NO_ROOM_SAID[language])
                ]
                page.context.close()

            if found:
                problems[language] = found
        self._assert_no_findings(problems)

    # ── 8. No false affordances ──────────────────────────────────────────────
    def test_nothing_shows_a_hand_cursor_unless_it_is_clickable(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            page = self._load(language)
            found: list[str] = []
            for stage in ("loaded", "built"):
                if stage == "built":
                    self._build(page)
                for item in page.evaluate(FALSE_AFFORDANCES):
                    found.append(f"{stage}: {item} shows a hand cursor and does nothing")
                if page.query_selector("#planPalette .plan-chip"):
                    found.append(f"{stage}: a .plan-chip is in the palette")
            addable = page.eval_on_selector_all(
                "#planPalette tr.is-addable", "ns => ns.map(n => getComputedStyle(n).cursor)"
            )
            if not addable or set(addable) != {"pointer"}:
                found.append(f"addable rows: {addable}")
            if page.query_selector("#planPalette tr[style]"):
                found.append("a palette row still carries an inline style")
            if found:
                problems[language] = found
            page.context.close()
        self._assert_no_findings(problems)

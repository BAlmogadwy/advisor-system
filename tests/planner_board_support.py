"""Shared fixtures and a base class for the planner board browser tests (parts one and two).

Part one: shell, student, rules, courses, week.

/planner/?layout=board, answered from a synthetic student made up below (no
database rows, no real data), the three data requests mocked exactly as
tests/test_planner_layout_browser.py does. Nothing leaves the machine
(tests/browser_isolation.py).

The student: three registered courses (nine credits) on four days with a Thursday
off and one 45-minute wait, two suggested courses already in the plan, two more that
could be added, and one that is blocked until a course in the plan is passed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Playwright's synchronous API runs through a greenlet; fixture creation is
# synchronous ORM work while Django serves the page on its own thread.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client

from tests.test_exam_rosters_browser import WIDE_FONT

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect
sync_playwright = playwright_api.sync_playwright

STUDENT_ID = "4500000"
OTHER_ID = "4500001"
SCRIPT = Path(settings.BASE_DIR) / "static" / "js" / "page-planner-board.js"
NO_MOTION = "*, *::before, *::after { transition: none !important; animation: none !important; }"


def _row(code: str, name: str, section: str, credits: int | None, day, start, end, tsid: int):
    return {
        "course_code": code,
        "course_key": code,
        "course_name": name,
        "course_number": "",
        "section": section,
        "registered_count": None,
        "credits": credits,
        "day": day,
        "start_time": start,
        "end_time": end,
        "room": "",
        "instructor": "",
        "term_section_id": tsid,
        "source": "registration_plan_1448_t1",
    }


# Sun and Tue: CS301 then CS322 (a 45 minute wait); Mon and Wed: CS330 at 12:45.
BASELINE = [
    _row("CS301", "Algorithms", "M1", 3, "SUN", "08:00", "09:15", 1),
    _row("CS301", "Algorithms", "M1", 3, "TUE", "08:00", "09:15", 1),
    _row("CS322", "Databases", "M2", 3, "SUN", "10:00", "11:15", 2),
    _row("CS322", "Databases", "M2", 3, "TUE", "10:00", "11:15", 2),
    _row("CS330", "Operating Systems", "M3", 3, "MON", "12:45", "14:00", 3),
    _row("CS330", "Operating Systems", "M3", 3, "WED", "12:45", "14:00", 3),
]


def _rec(code, name, status, missing=()):
    return {
        "course_code": code,
        "course_name": name,
        "credits": 3,
        "priority": "High",
        "score": 90,
        "status": status,
        "missing_prerequisites": list(missing),
        "reason_tags": ["recommended"],
    }


RECOMMENDATIONS = [
    _rec("AI331", "Machine Learning", "Eligible"),
    _rec("AI342", "Computer Vision", "Eligible"),
    _rec("AI431", "Deep Learning", "Blocked", ["AI331"]),
    _rec("AI450", "Robotics", "Blocked", ["CS999"]),
]


def context_for(
    student_id: str = STUDENT_ID,
    name: str = "Sample Student",
    eligibility_mode: str = "strict",
    **student,
) -> dict:
    return {
        "ok": True,
        "student": {
            "student_id": int(student_id),
            "name": name,
            "program": "AI",
            "cohort_section": "M",
            "advisor_id": "",
            "gpa": 3.42,
            "registered_credits": 9,
            "credit_cap": 18,
            "regulatory_max_credits": 19,
            **student,
        },
        "baseline": BASELINE,
        "baseline_totals": {"courses": 3, "credits": 9},
        "recommendations": RECOMMENDATIONS,
        "eligibility_mode": eligibility_mode,
        "strict_passed_only": eligibility_mode == "strict",
        "year": "1448",
        "term": "1",
        "warning": None,
    }


def _plan_course(code, name, status, can):
    return {
        "course_code": code,
        "course_name": name,
        "type": "Mandatory",
        "programme_term": 1,
        "credit_hours": 3,
        "status": status,
        "can_register": can,
        "prerequisites": [],
        "missing_prereqs": [],
        "importance_score": 3.0,
    }


STUDENT_PLAN = {
    "student_id": int(STUDENT_ID),
    "program": "AI",
    "eligibility_mode": "strict",
    "strict_passed_only": True,
    "terms": [
        {
            "term": 1,
            "courses": [
                _plan_course("CS301", "Algorithms", "studying", False),
                _plan_course("CS322", "Databases", "studying", False),
                _plan_course("CS330", "Operating Systems", "studying", False),
                _plan_course("AI331", "Machine Learning", "not_taken", True),
                _plan_course("AI342", "Computer Vision", "not_taken", True),
                _plan_course("STAT305", "Probability", "not_taken", True),
                _plan_course("CS351", "Networks", "not_taken", True),
                _plan_course("AI431", "Deep Learning", "not_taken", False),
            ],
        }
    ],
}


def _section(tsid, code, section, days, start, end, registered, capacity):
    return {
        "term_section_id": tsid,
        "course_key": code,
        "course_code": "".join(c for c in code if c.isalpha()),
        "course_number": "".join(c for c in code if c.isdigit()),
        "section": section,
        "course_name": code,
        "available_capacity": capacity,
        "registered_count": registered,
        "programs": ["AI"],
        "meetings": [
            {"day": d, "start_time": start, "end_time": end, "room": "", "instructor": ""}
            for d in days
        ],
    }


SECTIONS_CATALOG = {
    "ok": True,
    "count": 5,
    "program_sections_only": True,
    "sections": [
        _section(11, "AI331", "M1", ["SUN", "TUE"], "10:00", "11:15", 20, 40),
        _section(12, "AI331", "M2", ["MON", "WED"], "10:00", "11:15", 20, 40),
        _section(13, "STAT305", "M1", ["MON", "WED"], "08:00", "09:15", 30, 40),
        _section(14, "CS351", "M1", ["THU"], "09:00", "11:30", 37, 40),
        _section(15, "AI342", "M1", ["SUN"], "13:00", "14:15", None, None),
    ],
}


class Fake:
    """What the three data requests answer, and what was asked."""

    def __init__(self) -> None:
        self.context_bodies: list[dict] = []
        self.catalog_bodies: list[dict] = []
        self.pending: list = []  # routes a test holds back to answer later
        self.hold_ids: set[str] = set()
        self.context_answer = lambda body: (
            200,
            context_for(str(body["student_id"]), eligibility_mode=body["eligibility_mode"]),
        )
        self.plan_status = 200
        self.catalog_status = 200
        self.hold_catalog = 0  # how many of the next catalogue requests to hold back
        self.pending_catalog: list = []
        self.hold_plan = 0  # how many of the next plan requests to hold back
        self.pending_plan: list = []

    def context(self, route) -> None:
        body = route.request.post_data_json
        self.context_bodies.append(body)
        if str(body["student_id"]) in self.hold_ids:
            self.pending.append((body, route))
            return
        status, payload = self.context_answer(body)
        route.fulfill(status=status, json=payload)

    def plan(self, route) -> None:
        if self.hold_plan:
            self.hold_plan -= 1
            self.pending_plan.append(route)
            return
        if self.plan_status != 200:
            route.fulfill(status=self.plan_status, json={"error": "plan failed"})
            return
        route.fulfill(json=STUDENT_PLAN)

    def catalog(self, route) -> None:
        self.catalog_bodies.append(route.request.post_data_json)
        if self.hold_catalog:
            self.hold_catalog -= 1
            self.pending_catalog.append(route)
            return
        if self.catalog_status != 200:
            route.fulfill(status=self.catalog_status, json={"error": "catalog failed"})
            return
        route.fulfill(json=SECTIONS_CATALOG)


class PlannerBoardCase(StaticLiveServerTestCase):
    """The browser harness, with no tests of its own."""

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
            username="planner-board", email="planner-board@example.test", password="unused"
        )
        client = Client()
        client.force_login(user)
        self.session = client.cookies[settings.SESSION_COOKIE_NAME].value
        self.page_errors: list[str] = []
        self.addCleanup(lambda: self.assertEqual(self.page_errors, [], "the page threw"))

    def _open(self, fake: Fake, language="en", width=1440, height=900, font="app", load=True):
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
        context.add_init_script("try { localStorage.setItem('theme', 'light'); } catch (e) {}")
        if font == "wide":
            context.add_init_script(WIDE_FONT)
        context.route("**/ops/planner/context/", fake.context)
        context.route("**/report/student-plan/**", fake.plan)
        context.route("**/ops/planner/sections-catalog/", fake.catalog)
        if hasattr(fake, "build"):
            context.route("**/ops/planner/build/", fake.build)
        if hasattr(fake, "save"):
            context.route("**/ops/planner/save-student-sections/", fake.save)
        page = context.new_page()
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        page.goto(f"{self.live_server_url}/planner/?layout=board")
        page.add_style_tag(content=NO_MOTION)
        self.assertEqual(page.evaluate("document.documentElement.lang"), language)
        if load:
            self._fetch(page, STUDENT_ID)
            expect(page.locator("#plbTable tbody[data-group='blocked']")).to_be_visible()
            expect(
                page.locator("#plbTable tbody[data-group='could'] tr[data-code='STAT305']")
            ).to_be_visible()
            expect(
                page.locator("#plbTable tbody[data-group='could'] tr[data-code='CS351'] .plb-sc")
            ).not_to_have_text("—")
        return page

    @staticmethod
    def _fetch(page, student_id: str) -> None:
        page.fill("#plbStudentId", student_id)
        page.click("#plbFetch")

    @staticmethod
    def _rows(page, group: str) -> list[str]:
        return page.eval_on_selector_all(
            f"#plbTable tbody[data-group='{group}'] tr.plb-tr:not(.plb-tr--note)",
            "rows => rows.map(r => r.dataset.code)",
        )

    @staticmethod
    def _text(page, selector: str) -> str:
        return re.sub(r"\s+", " ", (page.locator(selector).first.text_content() or "").strip())

    @staticmethod
    def _rules(page, **values) -> None:
        """Open Edit rules, set the given controls, press Apply rules."""
        page.click("#plbEditRules")
        expect(page.locator("#plbRulesDialog")).to_be_visible()
        if "mode" in values:
            page.select_option("#plbRuleMode", values["mode"])
        if "elig" in values:
            page.select_option("#plbRuleElig", values["elig"])
        if "max" in values:
            page.fill("#plbRuleMax", str(values["max"]))
        for key, selector in (
            ("prog", "#plbRuleProg"),
            ("full", "#plbRuleFull"),
            ("swap", "#plbRuleSwap"),
        ):
            if key in values:
                page.set_checked(selector, values[key])
        page.click("#plbRulesApply")


# The page never scrolls sideways and nothing sits past the screen, except inside
# the week's own scroll frame (a phone scrolls the week sideways).
OVERFLOWS = """() => {
  const W = innerWidth, bad = [];
  const root = document.documentElement;
  if (root.scrollWidth > W + 1) bad.push(`the page scrolls sideways: ${root.scrollWidth} > ${W}`);
  for (const el of document.querySelectorAll('.plb *, .plb-load *')) {
    if (!el.getClientRects().length || el.closest('.plb-wkwrap') || el.closest('.sr-only')) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 2 && r.height <= 2) continue;
    if (r.left < -0.5 || r.right > W + 0.5)
      bad.push(`${el.tagName.toLowerCase()}.${[...el.classList].join('.')} ${Math.round(r.left)}..${Math.round(r.right)} of ${W}`);
  }
  const wrap = document.querySelector('.plb-wkwrap');
  if (wrap) {
    const r = wrap.getBoundingClientRect();
    if (r.left < -0.5 || r.right > W + 0.5) bad.push(`week frame ${Math.round(r.left)}..${Math.round(r.right)} of ${W}`);
    // What does not fit the frame must scroll inside it, not be cut off or spill out.
    const overflowX = getComputedStyle(wrap).overflowX;
    if (wrap.scrollWidth > wrap.clientWidth + 1 && !['auto', 'scroll'].includes(overflowX))
      bad.push(`the week is wider than its frame (${wrap.scrollWidth} > ${wrap.clientWidth}) and the frame does not scroll`);
    const grid = wrap.firstElementChild.getBoundingClientRect();
    if (grid.right > W + 0.5 && !['auto', 'scroll'].includes(overflowX))
      bad.push(`the week spills past the screen: ${Math.round(grid.right)} of ${W}`);
  }
  for (const el of document.querySelectorAll('.plb td, .plb th, .plb-b, .plb-df')) {
    if (el.closest('.plb-wkwrap') && !el.classList.contains('plb-b')) continue;
    if (el.scrollWidth > el.clientWidth + 1 && getComputedStyle(el).overflowX !== 'visible')
      bad.push(`clipped: ${el.className} ${el.scrollWidth} > ${el.clientWidth}`);
  }
  return bad;
}"""

OVERLAPS = """() => {
  const blocks = [...document.querySelectorAll('#plbWeek .plb-b')].map(b => [b, b.getBoundingClientRect()]);
  const bad = [];
  for (let i = 0; i < blocks.length; i++) for (let j = i + 1; j < blocks.length; j++) {
    const a = blocks[i][1], b = blocks[j][1];
    if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1)
      bad.push(`${blocks[i][0].dataset.code} over ${blocks[j][0].dataset.code}`);
  }
  return bad;
}"""

CONTROL_HEIGHTS = """() => {
  const out = [];
  for (const el of document.querySelectorAll('.plb button, .plb input:not([type=checkbox]), .plb-load button, .plb-load input, .plb-load select, .plb-cap, .plb label.plb-tickwrap')) {
    if (!el.getClientRects().length) continue;
    const box = el.closest('.plb-cap') || el;
    out.push([el.id || el.className || el.tagName, box.getBoundingClientRect().height]);
  }
  return out;
}"""

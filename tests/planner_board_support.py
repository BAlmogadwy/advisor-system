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
            # The registrar's running total, as the real endpoint sends it. The page must show
            # this term's registered credits (9, from the baseline below), never this figure.
            "registered_credits": 96,
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
        self.plan_payload = STUDENT_PLAN
        self.catalog_payload = SECTIONS_CATALOG
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
        route.fulfill(json=self.plan_payload)

    def catalog(self, route) -> None:
        self.catalog_bodies.append(route.request.post_data_json)
        if self.hold_catalog:
            self.hold_catalog -= 1
            self.pending_catalog.append(route)
            return
        if self.catalog_status != 200:
            route.fulfill(status=self.catalog_status, json={"error": "catalog failed"})
            return
        route.fulfill(json=self.catalog_payload)


def _mapping(code, section, tsid, days, start, end):
    return {
        "course_code": code,
        "course_key": code,
        "course_number": "",
        "section": section,
        "term_section_id": tsid,
        "meetings": [{"day": d, "start_time": start, "end_time": end} for d in days],
        "meeting_issue_codes": [],
    }


def _option(name, mappings, unscheduled=()):
    return {
        "name": name,
        "method": name[0],
        "rank": int(name[1]),
        "scheduled": len(mappings),
        "target": 2,
        "mappings": mappings,
        "unscheduled": list(unscheduled),
    }


# Each option with what its tab must say (registered week included, three credits a course):
#   B1  15 cr  4 days, Thu free   ends 14:15  longest gap 1 h 45 min
#   B2  12 cr  4 days, Thu free   ends 14:00  longest gap 45 min, one course not scheduled
#   B3  15 cr  5 days, no free    ends 16:15  longest gap 1 h
#   C1  15 cr  4 days, Thu free   ends 14:00  longest gap 2 h 30 min
#   C2  15 cr  5 days, no free    ends 14:00  longest gap 3 h 30 min
B1 = _option(
    "B1",
    [
        _mapping("AI331", "M2", 12, ["MON", "WED"], "10:00", "11:15"),
        _mapping("AI342", "M1", 15, ["SUN"], "13:00", "14:15"),
    ],
)
B2 = _option(
    "B2",
    [_mapping("AI331", "M1", 11, ["SUN", "TUE"], "11:30", "12:45")],
    unscheduled=[{"course_code": "AI342", "reason_code": "ALL_SECTIONS_CLASH"}],
)
B3 = _option(
    "B3",
    [
        _mapping("AI331", "M2", 12, ["MON", "WED"], "15:00", "16:15"),
        _mapping("AI342", "M1", 15, ["THU"], "09:00", "10:15"),
    ],
)
C1 = _option(
    "C1",
    [
        _mapping("AI331", "M1", 11, ["SUN", "TUE"], "12:00", "13:15"),
        _mapping("AI342", "M1", 15, ["MON"], "09:00", "10:15"),
    ],
)
C2 = _option(
    "C2",
    [
        _mapping("AI331", "M2", 12, ["MON", "WED"], "08:00", "09:15"),
        _mapping("AI342", "M1", 15, ["THU"], "08:00", "09:15"),
    ],
)


def build_reply(options):
    return {
        "ok": True,
        "summary": {
            "scheduled": 2,
            "target": 2,
            "conflicts": 0,
            "swaps_required": 0,
            "best_feasible": bool(options),
            "hard_constraint_failures": [],
        },
        "options": options,
        "unscheduled": [],
        "swap_suggestions": [],
        "mode": "keep",
        "eligibility_mode": "strict",
        "constraints": {"program_sections_only": True, "allow_full_sections": False},
        "graduation_projection_token": "token",
    }


FIVE = build_reply([B1, B2, B3, C1, C2])


# ── The graduation projection (part four): replies of /ops/planner/graduation-projection/ ─────
def term_rows(values, waiting=(), year=1448, term=1):
    """credits_by_term for these credits, from the planning term on (1448/1, 1448/2, 1449/1, ...)."""
    rows = []
    for i, credits in enumerate(values):
        rows.append(
            {"academic_year": year, "term": term, "credits": credits, "waiting": i in waiting}
        )
        year, term = (year, 2) if term == 1 else (year + 1, 1)
    return rows


def forecast(values, delta=None, waiting=(), reasons=(), unresolved=(), **extra):
    """A complete forecast: one tick per entry of `values`; the completion term is the last one."""
    rows = term_rows(values, waiting)
    return {
        "status": "complete",
        "course_codes": [],
        "terms_including_planning_term": len(rows),
        "completion_academic_year": rows[-1]["academic_year"],
        "completion_term": rows[-1]["term"],
        "credits_by_term": rows,
        "unresolved_requirements": list(unresolved),
        "delta_terms": delta,
        "reasons": list(reasons),
        **extra,
    }


NO_FORECAST = {
    "status": "unavailable",
    "course_codes": [],
    "terms_including_planning_term": None,
    "completion_academic_year": None,
    "completion_term": None,
    "credits_by_term": [],
    "unresolved_requirements": [],
    "delta_terms": None,
    "reasons": [{"code": "section_metadata_unavailable"}],
}
UNRESOLVED = {**NO_FORECAST, "status": "unresolved", "reasons": []}


def projection_reply(baseline, options, **top):
    return {
        "ok": True,
        "planning_term": {"academic_year": 1448, "term": 1},
        "future_max_credits_per_term": 18,
        "retained_baseline_kind": None,
        "baseline": baseline,
        "options": options,
        **top,
    }


# What every board test that does not look at the projection gets: nothing is forecast, so the
# tabs read as they did before the band existed.
DEFAULT_PROJECTION = projection_reply(
    {**NO_FORECAST, "kind": None, "reason_code": "no_registered_timetable"},
    {name: NO_FORECAST for name in ("B1", "B2", "B3", "C1", "C2")},
)
REG_FORECAST = forecast([6, 18, 18, 18, 6], kind="registered_timetable", reason_code=None)
# The mock-up's example: B1 finishes one term sooner than the registered week, B2 in the same term,
# B3 later (with a term in which no course can be taken), C1 sooner, C2 cannot be resolved.
READY = projection_reply(
    REG_FORECAST,
    {
        "B1": forecast([15, 18, 18, 15], delta=-1),
        "B2": forecast([12, 18, 18, 18, 6], delta=0),
        "B3": forecast([15, 0, 18, 18, 18, 9], delta=1, waiting=(1,)),
        "C1": forecast([15, 18, 18, 12], delta=-1),
        "C2": UNRESOLVED,
    },
)
SAVED = {
    "ok": True,
    "inserted": 2,
    "removed": [],
    "removed_count": 0,
    "clashes_with_registered": [],
}
SAVED_ROWS = [
    _row("AI331", "Machine Learning", "M2", 3, "MON", "10:00", "11:15", 12),
    _row("AI331", "Machine Learning", "M2", 3, "WED", "10:00", "11:15", 12),
    _row("AI342", "Computer Vision", "M1", 3, "SUN", "13:00", "14:15", 15),
]

EXPECTED_BUILD_BODY = {
    "student_id": STUDENT_ID,
    "academic_year": "1448",
    "term": "1",
    "mode": "keep",
    "eligibility_mode": "strict",
    "swap": False,
    "program_sections_only": True,
    "allow_full_sections": False,
    "max_credits": 18,
    "shortlist": [
        {
            "course_code": "AI331",
            "course_name": "Machine Learning",
            "credits": 3,
            "priority": "High",
            "status": "Eligible",
            "missing_prerequisites": [],
            "must_take": False,
            "score": 90,
        },
        {
            "course_code": "AI342",
            "course_name": "Computer Vision",
            "credits": 3,
            "priority": "High",
            "status": "Eligible",
            "missing_prerequisites": [],
            "must_take": False,
            "score": 90,
        },
    ],
    "baseline": BASELINE,
}
EXPECTED_SAVE_BODY = {
    "student_id": STUDENT_ID,
    "academic_year": "1448",
    "term": "1",
    "term_section_ids": [11, 15],
    "confirm_replace": True,
}


class BuildFake(Fake):
    """Fake with the build and the save endpoints, and a student who changes once saved."""

    def __init__(self) -> None:
        super().__init__()
        self.build_bodies: list[dict] = []
        self.save_bodies: list[dict] = []
        self.build_answer = lambda body: (200, FIVE)
        self.save_answer = lambda body: (200, SAVED)
        self.hold_build = 0
        self.pending_build: list = []
        self.hold_save = 0
        self.pending_save: list = []
        self.saved = False
        self.build_raw = None  # a callable(route) that answers the next build itself
        self.projection_bodies: list[dict] = []
        self.projection_answer = lambda body: (200, DEFAULT_PROJECTION)
        self.hold_projection = 0  # how many of the next projection requests to hold back
        self.pending_projection: list = []
        self.projection_raw = None  # a callable(route) that answers the next one itself
        self.context_answer = self._context

    def _context(self, body):
        payload = context_for(str(body["student_id"]), eligibility_mode=body["eligibility_mode"])
        if self.saved:
            payload["baseline"] = [*BASELINE, *SAVED_ROWS]
        return 200, payload

    def build(self, route) -> None:
        self.build_bodies.append(route.request.post_data_json)
        if self.build_raw is not None:
            self.build_raw(route)
            return
        if self.hold_build:
            self.hold_build -= 1
            self.pending_build.append(route)
            return
        status, payload = self.build_answer(route.request.post_data_json)
        route.fulfill(status=status, json=payload)

    def projection(self, route) -> None:
        self.projection_bodies.append(route.request.post_data_json)
        if self.projection_raw is not None:
            self.projection_raw(route)
            return
        if self.hold_projection:
            self.hold_projection -= 1
            self.pending_projection.append(route)
            return
        status, payload = self.projection_answer(route.request.post_data_json)
        route.fulfill(status=status, json=payload)

    def save(self, route) -> None:
        body = route.request.post_data_json
        self.save_bodies.append(body)
        if self.hold_save:
            self.hold_save -= 1
            self.pending_save.append(route)
            return
        status, payload = self.save_answer(body)
        if status == 200:
            self.saved = True
        route.fulfill(status=status, json=payload)


# ── Part five: options crafted for the order, the names and the guidance ──────────────────────
PLAN_CODES = ("AI331", "AI342")  # the two courses of the plan the fixtures build


def _tsid(name: str, k: int) -> int:
    return int(f"{ord(name[0])}{name[1]}{k}")


def week_option(name: str, first=None, second=None, *, sections=(None, None), tsids=None):
    """An option that places AI331 as `first` and AI342 as `second`, each (days, start, end);
    a course given as None is left out (and listed as unscheduled, as the server would)."""
    placed, left = [], []
    for k, (code, spec) in enumerate(zip(PLAN_CODES, (first, second), strict=True)):
        if spec is None:
            left.append({"course_code": code, "reason_code": "ALL_SECTIONS_CLASH"})
            continue
        days, start, end = spec
        tsid = (tsids or {}).get(code) or _tsid(name, k)
        placed.append(_mapping(code, sections[k] or f"{name}{k}", tsid, days, start, end))
    return _option(name, placed, unscheduled=left)


def catalog_with(*rows):
    """A sections catalogue from (tsid, code, section, registered, capacity) rows."""
    sections = [
        _section(tsid, code, section, ["SUN"], "08:00", "09:15", registered, capacity)
        for tsid, code, section, registered, capacity in rows
    ]
    return {"ok": True, "count": len(sections), "program_sections_only": True, "sections": sections}


def board_fake(options, *, projection=None, catalog=None):
    """A BuildFake whose build answers with these options (and its projection and catalogue)."""
    fake = BuildFake()
    fake.build_answer = lambda body: (200, build_reply(options))
    if projection is not None:
        fake.projection_answer = lambda body: (200, projection)
    if catalog is not None:
        fake.catalog_payload = catalog
    return fake


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

    def _open(
        self, fake: Fake, language="en", width=1440, height=900, font="app", load=True, clock=False
    ):
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
        if hasattr(fake, "projection"):
            context.route("**/ops/planner/graduation-projection/", fake.projection)
        page = context.new_page()
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        if clock:
            page.clock.install()  # the test moves time itself (the 30 minute rule)
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
    def _fetch_forced(page, student_id: str) -> None:
        """Submit the load form although Fetch is disabled (a load is in flight): the page's own
        code can still start a newer load (a rule that reloads the suggestions), and its answers
        must still win."""
        page.fill("#plbStudentId", student_id)
        page.evaluate("document.getElementById('plbLoad').requestSubmit()")

    #: After a build the page shows the first option of its own order (C1 for FIVE). A test class
    #: that is about something else than the order names the tab it reads from here.
    start_on: str | None = None

    def _start(self, page) -> None:
        if self.start_on and page.locator(f"#plbTabs [data-opt='{self.start_on}']").count():
            page.click(f"#plbTabs [data-opt='{self.start_on}']")

    def _built(self, fake=None, **kwargs):
        fake = fake or BuildFake()
        page = self._open(fake, **kwargs)
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text(
            "Rebuild" if kwargs.get("language", "en") == "en" else "أعد البناء"
        )
        expect(page.locator("#plbBuild")).to_be_enabled()
        self._start(page)
        return page, fake

    def _projected(self, reply=None, fake=None, **kwargs):
        """A built board whose projection has arrived (READY unless a reply is given)."""
        fake = fake or BuildFake()
        answer = READY if reply is None else reply
        fake.projection_answer = lambda body: (200, answer)
        page, fake = self._built(fake, **kwargs)
        expect(page.locator("#plbGradLine")).to_be_hidden()
        return page, fake

    @staticmethod
    def _grad(page, selector: str) -> list[str]:
        """The text of every node of the band that matches, whitespace collapsed."""
        return page.eval_on_selector_all(
            f"#plbGrad {selector}",
            "nodes => nodes.map(n => n.textContent.replace(/\\s+/g, ' ').trim())",
        )

    @staticmethod
    def _tab_names(page) -> list[str]:
        return page.eval_on_selector_all(
            "#plbTabs [role='tab']", "tabs => tabs.map(t => t.dataset.opt || '(registered)')"
        )

    @staticmethod
    def _tab_lines(page, name: str) -> list[str]:
        return page.eval_on_selector_all(
            f"#plbTabs [data-opt='{name}'] > span",
            "spans => spans.map(s => s.textContent.replace(/\\s+/g, ' ').trim())",
        )

    @staticmethod
    def _blocks(page, kind: str | None = None) -> list[str]:
        selector = "#plbWeek .plb-b" + (f"[data-kind='{kind}']" if kind else "")
        return page.eval_on_selector_all(
            selector, "bs => bs.map(b => b.dataset.code + ' ' + b.dataset.kind)"
        )

    def _cell(self, page, code: str) -> str:
        return self._text(page, f"#plbTable tbody[data-group='new'] tr[data-code='{code}'] .plb-sc")

    @staticmethod
    def _rows(page, group: str) -> list[str]:
        return page.eval_on_selector_all(
            f"#plbTable tbody[data-group='{group}'] tr.plb-tr:not(.plb-tr--note)",
            "rows => rows.map(r => r.dataset.code)",
        )

    @staticmethod
    def _text(page, selector: str) -> str:
        return re.sub(r"\s+", " ", (page.locator(selector).first.text_content() or "").strip())

    def _board(
        self,
        options,
        *,
        ignore=True,
        projection=None,
        catalog=None,
        hold_projection=False,
        **kwargs,
    ):
        """A board built from exactly these options (the registered week ignored unless asked)."""
        fake = board_fake(options, projection=projection, catalog=catalog)
        if hold_projection:
            fake.hold_projection = 1
        page = self._open(fake, **kwargs)
        if ignore:
            self._rules(page, mode="ignore")
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text(
            "Rebuild" if kwargs.get("language", "en") == "en" else "أعد البناء"
        )
        expect(page.locator("#plbBuild")).to_be_enabled()
        return page, fake

    @staticmethod
    def _pick(page, name: str) -> None:
        """Select an option, from its tab or from "More options"."""
        if page.locator(f"#plbTabs [data-opt='{name}']").count():
            page.click(f"#plbTabs [data-opt='{name}']")
        else:
            page.click("#plbMoreBtn")
            page.click(f"#plbMoreList [data-more='{name}']")

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

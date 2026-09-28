"""Section Planning laid out in Chromium, in English and in Arabic.

The jsdom suite (tests/frontend/section-planning.test.cjs) covers what the
page script does; this covers what only a browser can: whether a value fits
its field, what a phone shows, computed letter spacing and colours. The page
is the real view; Generate is answered with a fixed plan (the planner itself
is tested in tests/test_section_planning_sizing.py), so every run lays out the
same rows. Each check runs at 390, 1024 and 1440px, and the fit checks in the
design's font and in a wide one (Verdana, DejaVu Sans), as a CI machine
without the design's fonts renders them. Nothing leaves the machine
(tests/browser_isolation.py).
"""

from __future__ import annotations

import json
import os

# Playwright's synchronous API runs through a greenlet; fixture creation is
# synchronous ORM work while Django serves the page on its own thread.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client

from core.services.rbac import ROLE_GENERAL_ADVISOR, ensure_role_groups
from tests.test_exam_rosters_browser import WIDE_FONT

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect
sync_playwright = playwright_api.sync_playwright

WIDTHS = (390, 1024, 1440)
GENERATE = "**/ops/section-planning/generate/"


def _row(
    code: str,
    name: str,
    *,
    male: int,
    female: int,
    m_sec: int,
    f_sec: int,
    cap: int,
    fill: int,
    **extra,
) -> dict:
    return {
        "department": extra.pop("department", code.rstrip("0123456789")),
        "course_key": code,
        "course_code": code,
        "course_name": name,
        "credit_hours": 3,
        "is_external": False,
        "total_students": male + female,
        "male_students": male,
        "female_students": female,
        "unknown_students": 0,
        "male_sections": m_sec,
        "female_sections": f_sec,
        "num_sections": m_sec + f_sec,
        "max_per_section": cap,
        "avg_per_section": round((male + female) / max(1, m_sec + f_sec)),
        "fill_percent": fill,
        "status": "full" if fill >= 90 else ("underfilled" if fill < 40 else ""),
        "limit_source": "rule",
        "slots": [],
        **extra,
    }


# Long names, an elective under its slot, an external course, and each fill band.
PLAN = [
    _row(
        "AI221",
        "ARTIFICIAL INTELLIGENCE PROGRAMMING",
        male=4,
        female=0,
        m_sec=1,
        f_sec=0,
        cap=25,
        fill=16,
    ),
    _row(
        "AI463",
        "INFORMATION RETRIEVAL AND WEB SEARCH ENGINES",
        male=58,
        female=64,
        m_sec=2,
        f_sec=3,
        cap=30,
        fill=83,
        limit_source="slot",
        slots=["AI1"],
        is_external=True,
    ),
    _row(
        "AI491",
        "GRADUATION PROJECT I",
        male=15,
        female=16,
        m_sec=3,
        f_sec=4,
        cap=5,
        fill=100,
        limit_source="programme",
    ),
    _row("CS323", "OPERATING SYSTEMS", male=24, female=6, m_sec=1, f_sec=1, cap=25, fill=60),
    _row(
        "GS102",
        "ISLAMIC STUDIES: FEATURES OF THE PROPHET'S BIOGRAPHY",
        male=4,
        female=3,
        m_sec=1,
        f_sec=1,
        cap=50,
        fill=8,
        is_external=True,
    ),
    _row(
        "MATH203",
        "CALCULUS I",
        male=1,
        female=0,
        m_sec=1,
        f_sec=0,
        cap=50,
        fill=2,
        is_external=True,
    ),
]


def _summary(rows: list[dict]) -> dict:
    departments: dict[str, dict] = {}
    for row in rows:
        d = departments.setdefault(
            row["department"],
            {
                "department": row["department"],
                "courses": 0,
                "sections": 0,
                "students": 0,
                "total_credits": 0,
                "male_sections": 0,
                "female_sections": 0,
            },
        )
        d["courses"] += 1
        d["sections"] += row["num_sections"]
        d["students"] += row["total_students"]
        d["total_credits"] += row["credit_hours"] * row["num_sections"]
        d["male_sections"] += row["male_sections"]
        d["female_sections"] += row["female_sections"]
    return {
        "total_courses": len(rows),
        "total_sections": sum(r["num_sections"] for r in rows),
        "total_students": sum(r["total_students"] for r in rows),
        "avg_fill_percent": 45,
        "male_sections": sum(r["male_sections"] for r in rows),
        "female_sections": sum(r["female_sections"] for r in rows),
        "departments": [departments[k] for k in sorted(departments)],
        "no_gender": {"students": 0, "seat_demand": 0, "courses": 0},
    }


SINGLE = {
    "ok": True,
    "mode": "single",
    "year": 1448,
    "semester": 1,
    "student_count": 324,
    "cohorts": {"M": 116, "F": 208, "no_gender": 0},
    "plan": PLAN,
    "summary": _summary(PLAN),
    "electives": {"dropped": [], "dropped_total": 0},
}


class SectionPlanningBrowserTests(StaticLiveServerTestCase):
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

    def _page(
        self,
        language: str,
        width: int = 1440,
        *,
        wide_font: bool = False,
        theme: str = "light",
        plan: dict | None = None,
    ):
        ensure_role_groups()
        user = get_user_model().objects.create_user(
            username=f"sp-{language}-{get_user_model().objects.count()}", password="unused"
        )
        user.groups.add(Group.objects.get(name=ROLE_GENERAL_ADVISOR))
        client = Client()
        client.force_login(user)
        context = self.browser.new_context(
            locale="ar" if language == "ar" else "en-US",
            extra_http_headers={"Accept-Language": language},
            viewport={"width": width, "height": 900},
            color_scheme=theme,
        )
        context.add_cookies(
            [
                {
                    "name": settings.SESSION_COOKIE_NAME,
                    "value": client.cookies[settings.SESSION_COOKIE_NAME].value,
                    "url": self.live_server_url,
                },
                {
                    "name": settings.LANGUAGE_COOKIE_NAME,
                    "value": language,
                    "url": self.live_server_url,
                },
            ]
        )
        self.addCleanup(context.close)
        context.add_init_script(
            f"try {{ localStorage.setItem('theme', '{theme}'); }} catch (e) {{}}"
        )
        if wide_font:
            context.add_init_script(WIDE_FONT)
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        requests: list[str] = []
        page.on("request", lambda request: requests.append(f"{request.method} {request.url}"))
        page.requests = requests  # type: ignore[attr-defined]
        body = json.dumps(plan or SINGLE)
        page.route(
            GENERATE,
            lambda route: route.fulfill(status=200, content_type="application/json", body=body),
        )
        page.goto(f"{self.live_server_url}/section-planning/")
        self.assertEqual(page.evaluate("document.documentElement.lang"), language)
        return page

    # ── 1. The scope fields show their whole value ──────────────────

    # Whether each scope field shows its whole value (and its placeholder, and
    # for Program a few codes) and sits inside its capsule, which clips. Year and
    # Semester are measured strictly; Program grows into spare room, so its box
    # is a fractional width that Chromium rounds up for scrollWidth and down for
    # clientWidth: 1px there is rounding, not a hidden character.
    FIELDS = """() => [['spYear', 0], ['spSemester', 0], ['spProgram', 1]].map(([id, slack]) => {
      const field = document.getElementById(id);
      const capsule = field.closest('.fb-search').getBoundingClientRect();
      const box = field.getBoundingClientRect();
      const value = field.value;
      const fits = text => { field.value = text; return field.scrollWidth <= field.clientWidth + slack; };
      const shown = [value, field.placeholder, id === 'spProgram' ? 'AI,DS,CS,IS' : value];
      const misfits = shown.filter(text => !fits(text));
      field.value = value;
      return { id, value, misfits, scrollWidth: field.scrollWidth, clientWidth: field.clientWidth,
               inCapsule: box.left >= capsule.left - 0.5 && box.right <= capsule.right + 0.5 };
    })"""

    def _assert_scope_fits(self, language: str, wide_font: bool) -> None:
        page = self._page(language, wide_font=wide_font)
        for width in WIDTHS:
            page.set_viewport_size({"width": width, "height": 900})
            where = f"{language} {width}px{' wide font' if wide_font else ''}"
            fields = page.evaluate(self.FIELDS)
            self.assertEqual([f["value"] for f in fields[:2]], ["1448", "1"], where)
            for field in fields:
                self.assertEqual(field["misfits"], [], f"{where}: {field}")
                self.assertTrue(field["inCapsule"], f"{where}: {field}")

    def test_scope_values_are_whole_at_every_width(self) -> None:
        for language in ("en", "ar"):
            self._assert_scope_fits(language, wide_font=False)

    def test_scope_values_are_whole_in_a_wide_font(self) -> None:
        for language in ("en", "ar"):
            self._assert_scope_fits(language, wide_font=True)

    def test_enter_in_a_scope_field_runs_generate_and_saves_nothing(self) -> None:
        page = self._page("en")
        with page.expect_request(
            lambda request: request.url.endswith("/ops/section-planning/generate/")
        ):
            page.locator("#spSemester").press("Enter")
        expect(page.locator("#spTable tbody tr[data-code]")).to_have_count(len(PLAN))
        self.assertEqual([r for r in page.requests if "/limits/" in r], [])
        self.assertTrue(page.url.endswith("/section-planning/"), "no page load")

    # ── 2. A phone shows every course's numbers; the page never scrolls sideways ──

    # Per result row: the course and its numbers, each laid out (not display:
    # none) and, on a phone, inside the viewport; and how far the document and
    # the <main> scroller overflow sideways. Wider than a phone the 13-column
    # table may scroll inside its own box, so there "shown" means laid out.
    PHONE = """(phone) => {
      const shown = el => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        const laidOut = s.display !== 'none' && s.visibility !== 'hidden' && r.width > 1 && r.height > 1;
        return laidOut && (!phone || (r.left >= -0.5 && r.right <= innerWidth + 0.5)); };
      const parts = { course: 2, demand: 5, m: 6, f: 7, total: 8, max: 9 };
      const rows = [...document.querySelectorAll('.sp-plan-table tbody tr[data-code]')]
        .filter(tr => tr.closest('.d-none') === null)
        .map(tr => ({ code: tr.dataset.code,
          hidden: Object.entries(parts).filter(([, i]) => !shown(tr.children[i])).map(([k]) => k) }));
      const main = document.querySelector('main');
      return { rows, page: document.documentElement.scrollWidth - innerWidth,
               main: main.scrollWidth - main.clientWidth };
    }"""

    def _generate(self, page) -> None:
        page.locator("#spGenerate").click()
        expect(page.locator("#spTable tbody tr[data-code]")).to_have_count(len(PLAN))

    def _assert_phone(self, language: str, wide_font: bool) -> None:
        page = self._page(language, 390, wide_font=wide_font)
        self._generate(page)
        for width in WIDTHS:
            page.set_viewport_size({"width": width, "height": 900})
            where = f"{language} {width}px{' wide font' if wide_font else ''}"
            facts = page.evaluate(self.PHONE, width <= 768)
            self.assertEqual(len(facts["rows"]), len(PLAN), where)
            self.assertEqual([r for r in facts["rows"] if r["hidden"]], [], where)
            self.assertLessEqual(facts["page"], 0, f"{where}: the page scrolls sideways")
            self.assertLessEqual(facts["main"], 1, f"{where}: the pane scrolls sideways")

    def test_every_row_shows_its_numbers_at_every_width(self) -> None:
        for language in ("en", "ar"):
            self._assert_phone(language, wide_font=False)

    def test_every_row_shows_its_numbers_in_a_wide_font(self) -> None:
        for language in ("en", "ar"):
            self._assert_phone(language, wide_font=True)

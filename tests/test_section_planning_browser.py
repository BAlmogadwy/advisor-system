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

_AI = [row for row in PLAN if row["department"] == "AI"]
_DS = [row for row in PLAN if row["department"] != "AI"]
MULTI = {
    "ok": True,
    "mode": "multi",
    "year": 1448,
    "semester": 1,
    "student_count": 324,
    "cohorts": {"M": 116, "F": 208, "no_gender": 0},
    "combined_plan": [dict(row, programs=["AI", "DS"]) for row in PLAN],
    "combined_summary": _summary(PLAN),
    "electives": {"dropped": [], "dropped_total": 0},
    "programs": [
        {"program": "AI", "student_count": 1, "plan": _AI, "summary": _summary(_AI)},
        {"program": "DS", "student_count": 2, "plan": _DS, "summary": _summary(_DS)},
    ],
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
    # for Program a few codes) and sits inside its capsule, which clips. Every
    # width is in ch, so a box can be a fractional width that Chromium rounds up
    # for scrollWidth and down for clientWidth: on CI's Linux fonts an EMPTY
    # Semester measured 26 > 25. 1px is rounding, not a hidden character (a
    # digit is 1ch wide); a field clipped by the old 80px cap misses by far more.
    FIELDS = """() => [['spYear', 1], ['spSemester', 1], ['spProgram', 1]].map(([id, slack]) => {
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
      const boxes = [...document.querySelectorAll('.sp-table-scroll')]
        .filter(box => box.closest('.d-none') === null).map(box => box.scrollWidth - box.clientWidth);
      return { rows, page: document.documentElement.scrollWidth - innerWidth,
               main: main.scrollWidth - main.clientWidth, boxes };
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
            if width == 1440:
                # A desktop fits the whole table: no scrollbar under it.
                self.assertTrue(facts["boxes"], where)
                self.assertLessEqual(max(facts["boxes"]), 0, f"{where}: the table scrolls sideways")

    def test_a_desktop_fits_the_table_with_every_programme_tag(self) -> None:
        # Several programmes tag each course with its programmes, and the
        # catalogue's longest unbreakable name (STAT305's) sets the narrowest
        # a name column gets: the widest rows the table draws. Still no
        # scrollbar under it at 1440px.
        long_name = _row(
            "STAT305",
            "PROBABILITY&STATISTICS FOR ENGINEERS",
            male=40,
            female=41,
            m_sec=1,
            f_sec=2,
            cap=50,
            fill=54,
            is_external=True,
        )
        rows = [*PLAN, long_name]
        plan = dict(
            MULTI,
            combined_plan=[dict(row, programs=["AI", "DS"]) for row in rows],
            combined_summary=_summary(rows),
        )
        for language in ("en", "ar"):
            for wide_font in (False, True):
                where = f"{language} 1440px{' wide font' if wide_font else ''}"
                page = self._page(language, 1440, wide_font=wide_font, plan=plan)
                page.locator("#spProgram").fill("AI,DS")
                page.locator("#spGenerate").click()
                expect(page.locator("#spTable tbody tr[data-code]")).to_have_count(len(rows))
                for toggle in page.locator("#spMultiPrograms .sp-prog-toggle").all():
                    toggle.click()  # each programme's own table too
                facts = page.evaluate(self.PHONE, False)
                self.assertEqual(len(facts["boxes"]), 3, where)
                self.assertLessEqual(max(facts["boxes"]), 0, f"{where}: {facts['boxes']}")

    # At 1024px (769px and up) the table may scroll sideways in its box: at
    # rest the code column is see-through like the rest; scrolled to its far
    # end, each row's code (sticky, now opaque) and Total are still in the box.
    STICKY = r"""async () => {
      const box = document.querySelector('#spTable').closest('.sp-table-scroll');
      const alpha = el => { const n = (getComputedStyle(el).backgroundColor.match(/[\d.]+/g) || []).map(Number);
        return n.length > 3 ? n[3] : 1; };
      const rows = [...document.querySelectorAll('#spTable tbody tr[data-code]')];
      const codes = () => [document.querySelector('#spTable thead th:nth-child(3)'), ...rows.map(tr => tr.children[2])];
      const frame = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const atRest = codes().map(alpha);
      box.scrollLeft = document.documentElement.dir === 'rtl' ? -box.scrollWidth : box.scrollWidth;
      await frame();
      const edge = box.getBoundingClientRect();
      const inside = el => { const r = el.getBoundingClientRect();
        return r.width > 1 && r.left >= edge.left - 0.5 && r.right <= edge.right + 0.5; };
      const facts = { overflow: box.scrollWidth - box.clientWidth, scrolledBy: Math.abs(box.scrollLeft),
        atRest, scrolled: codes().map(alpha),
        hidden: rows.filter(tr => !inside(tr.children[2]) || !inside(tr.children[8])).map(tr => tr.dataset.code),
        headInside: inside(codes()[0]) };
      box.scrollLeft = 0;
      await frame();
      facts.backAtRest = codes().map(alpha);
      return facts;
    }"""

    def test_a_table_scrolled_sideways_keeps_each_course_code_in_view(self) -> None:
        for language in ("en", "ar"):
            for wide_font in (False, True):
                where = f"{language} 1024px{' wide font' if wide_font else ''}"
                page = self._page(language, 1024, wide_font=wide_font)
                self._generate(page)
                facts = page.evaluate(self.STICKY)
                self.assertGreater(facts["overflow"], 0, f"{where}: nothing to scroll: {facts}")
                self.assertGreater(facts["scrolledBy"], 0, f"{where}: {facts}")
                self.assertEqual(set(facts["atRest"]), {0}, f"{where}: opaque at rest: {facts}")
                self.assertEqual(facts["hidden"], [], f"{where}: {facts}")
                self.assertTrue(facts["headInside"], f"{where}: {facts}")
                self.assertEqual(
                    set(facts["scrolled"]), {1}, f"{where}: see-through when stuck: {facts}"
                )
                self.assertEqual(set(facts["backAtRest"]), {0}, f"{where}: {facts}")

    def test_every_row_shows_its_numbers_at_every_width(self) -> None:
        for language in ("en", "ar"):
            self._assert_phone(language, wide_font=False)

    def test_every_row_shows_its_numbers_in_a_wide_font(self) -> None:
        for language in ("en", "ar"):
            self._assert_phone(language, wide_font=True)

    # ── 6. A keyboard user sees where they are ──────────────────────

    FOCUS_RING = """() => { const s = getComputedStyle(document.activeElement);
      return { id: document.activeElement.id || document.activeElement.className,
               style: s.outlineStyle, width: parseFloat(s.outlineWidth) }; }"""

    def _tab_to(self, page, selector: str) -> dict:
        target = page.locator(selector)
        target.focus()
        page.keyboard.press("Shift+Tab")
        page.keyboard.press("Tab")
        ring = page.evaluate(self.FOCUS_RING)
        ring["reached"] = target.evaluate("element => element === document.activeElement")
        return ring

    def test_keyboard_focus_is_visible_on_toggles_and_sortable_headers(self) -> None:
        for language in ("en", "ar"):
            page = self._page(language, plan=MULTI)
            page.locator("#spProgram").fill("AI,DS")
            page.locator("#spGenerate").click()
            expect(page.locator("#spMultiPrograms .sp-prog-toggle")).to_have_count(2)
            for selector in (
                "#spToggleCaps",
                "#spToggleAdv",
                "#spMultiPrograms .sp-prog-toggle >> nth=0",
                "#spTable thead th >> nth=5",
            ):
                ring = self._tab_to(page, selector)
                self.assertTrue(
                    ring["reached"], f"{language} {selector}: Tab never reaches it: {ring}"
                )
                self.assertEqual(ring["style"], "solid", f"{language} {selector}: {ring}")
                self.assertGreaterEqual(ring["width"], 2, f"{language} {selector}: {ring}")
            # Enter on a programme heading opens it.
            toggle = page.locator("#spMultiPrograms .sp-prog-toggle").first
            toggle.focus()
            page.keyboard.press("Enter")
            expect(toggle).to_have_attribute("aria-expanded", "true")
            page.keyboard.press("Space")
            expect(toggle).to_have_attribute("aria-expanded", "false")

    # A scope field draws no outline of its own (the shared field style): its
    # capsule's outer ring is the focus indicator. The ring's colour and
    # spread, the theme's --teal and the page behind it, as computed.
    CAPSULE_RING = r"""(id) => {
      const rgba = value => { const n = (value.match(/[\d.]+/g) || []).map(Number); return [n[0], n[1], n[2], n.length > 3 ? n[3] : 1]; };
      const probe = document.createElement('i');
      probe.style.color = 'var(--teal)';
      document.body.append(probe);
      const teal = rgba(getComputedStyle(probe).color);
      probe.remove();
      const capsule = document.getElementById(id).closest('.fb-search');
      const rings = getComputedStyle(capsule).boxShadow.split(/,(?![^(]*\))/)
        .filter(shadow => !shadow.includes('inset'))
        .map(shadow => { const colour = (shadow.match(/rgba?\([^)]*\)/) || [''])[0];
          const lengths = shadow.replace(colour, '').trim().split(/\s+/).map(parseFloat);
          return { colour: rgba(colour), spread: lengths[3] || 0 }; });
      return { active: document.activeElement.id, rings, teal,
               page: rgba(getComputedStyle(document.body).backgroundColor) };
    }"""

    def test_scope_fields_show_a_full_strength_focus_ring_in_light_and_dark(self) -> None:
        for language in ("en", "ar"):
            for theme in ("light", "dark"):
                page = self._page(language, theme=theme)
                self.assertEqual(page.evaluate("document.documentElement.dataset.theme"), theme)
                page.add_style_tag(
                    content="*, *::before, *::after { transition: none !important; }"
                )
                for field in ("spYear", "spSemester", "spProgram"):
                    where = f"{language} {theme} #{field}"
                    self.assertTrue(self._tab_to(page, f"#{field}")["reached"], where)
                    ring = page.evaluate(self.CAPSULE_RING, field)
                    self.assertEqual(ring["active"], field, where)
                    outer = [r for r in ring["rings"] if r["spread"] >= 2]
                    self.assertEqual(len(outer), 1, f"{where}: {ring}")
                    self.assertEqual(
                        outer[0]["colour"], ring["teal"], f"{where}: a faint ring: {ring}"
                    )
                    ratio = self._contrast(outer[0]["colour"], ring["page"])
                    self.assertGreaterEqual(ratio, 3, f"{where}: {ratio:.2f} against the page")

    # The summary's row lines and the teal rule over its total run under every
    # column, and "Total" is inked like its numbers, in both themes.
    SUMMARY_LINES = r"""() => {
      const probe = document.createElement('i');
      document.body.append(probe);
      const token = name => { probe.style.color = `var(${name})`; return getComputedStyle(probe).color; };
      const tokens = { teal: token('--teal'), t5: token('--t5'), t1: token('--t1') };
      probe.remove();
      const cs = (el, prop) => getComputedStyle(el)[prop];
      return [...document.querySelectorAll('.sp-sum-table')].map(table => ({
        tokens,
        rowLines: [...table.querySelectorAll('tbody tr[data-dept] > *')].map(c => cs(c, 'borderBottomColor')),
        totalRule: [...table.querySelectorAll('tfoot tr > *')].map(c => `${cs(c, 'borderTopColor')} ${cs(c, 'borderTopWidth')}`),
        totalInk: [...table.querySelectorAll('tfoot tr > *')].map(c => cs(c, 'color')),
      }));
    }"""

    def test_the_summary_rules_run_under_every_column_in_light_and_dark(self) -> None:
        for language in ("en", "ar"):
            for theme in ("light", "dark"):
                where = f"{language} {theme}"
                page = self._page(language, theme=theme, plan=MULTI)
                page.locator("#spProgram").fill("AI,DS")
                page.locator("#spGenerate").click()
                expect(page.locator("#spMultiPrograms .sp-prog-toggle")).to_have_count(2)
                tables = page.evaluate(self.SUMMARY_LINES)
                self.assertEqual(len(tables), 3, where)  # all programmes, then AI and DS
                for table in tables:
                    tokens = table["tokens"]
                    self.assertTrue(table["rowLines"], where)
                    self.assertEqual(set(table["rowLines"]), {tokens["t5"]}, f"{where}: {table}")
                    self.assertEqual(
                        set(table["totalRule"]), {f"{tokens['teal']} 2px"}, f"{where}: {table}"
                    )
                    self.assertEqual(set(table["totalInk"]), {tokens["t1"]}, f"{where}: {table}")

    # ── 3. Arabic labels are words, not spaced-out letters; chevrons mirror ──

    # Every Arabic label the page draws in the Latin utility style (mono,
    # uppercase, tracked): its computed letter spacing, transform and font.
    LABELS = """() => {
      const style = (el, pseudo) => { const s = getComputedStyle(el, pseudo);
        return { spacing: s.letterSpacing, transform: s.textTransform, font: s.fontFamily }; };
      const first = selector => document.querySelector(selector);
      const out = {};
      for (const selector of ['#spTable thead th:nth-child(6)', '.sp-mc .k', '.sp-caps-lbl',
                              '.sp-adv-table th', '.sp-prog-count', '#spDeptSummary thead th']) {
        out[selector] = style(first(selector));
      }
      out['card label'] = style(first('#spTable tbody td.sp-c-demand'), '::before');
      return out;
    }"""
    CHEVRONS = """() => [...document.querySelectorAll('.sp-chev')].map(svg => {
      const m = new DOMMatrix(getComputedStyle(svg).transform === 'none' ? undefined : getComputedStyle(svg).transform);
      return { open: svg.parentElement.getAttribute('aria-expanded') === 'true',
               a: Math.round(m.a), b: Math.round(m.b) };
    })"""

    def test_arabic_labels_have_no_letter_spacing(self) -> None:
        for width in (390, 1440):
            page = self._page("ar", width, plan=MULTI)
            page.locator("#spProgram").fill("AI,DS")
            page.locator("#spGenerate").click()
            expect(page.locator("#spMultiPrograms .sp-prog-toggle")).to_have_count(2)
            labels = page.evaluate(self.LABELS)
            for selector, style in labels.items():
                if selector == "card label" and width > 768:
                    continue
                where = f"ar {width}px {selector}: {style}"
                self.assertIn(style["spacing"], ("normal", "0px"), where)
                self.assertEqual(style["transform"], "none", where)
                self.assertNotIn("mono", style["font"].lower(), where)

    def test_chevrons_point_along_the_reading_direction_and_down_when_open(self) -> None:
        for language, closed in (("en", 1), ("ar", -1)):
            page = self._page(language, plan=MULTI)
            page.locator("#spProgram").fill("AI,DS")
            page.locator("#spGenerate").click()
            toggle = page.locator("#spMultiPrograms .sp-prog-toggle").first
            toggle.click()
            page.wait_for_timeout(250)  # the chevron's .15s turn
            for chevron in page.evaluate(self.CHEVRONS):
                expected = (0, 1) if chevron["open"] else (closed, 0)
                self.assertEqual((chevron["a"], chevron["b"]), expected, f"{language}: {chevron}")

    # ── 4. Fill bars stand out from the page in both themes ─────────

    # Each fill band's bar colour, the page's background, the theme's
    # --surface token and the bar's track laid over the page, as sRGB.
    BARS = """() => {
      const rgba = value => { const n = (value.match(/[\d.]+/g) || []).map(Number); return [n[0], n[1], n[2], n.length > 3 ? n[3] : 1]; };
      const probe = document.createElement('div');
      probe.style.background = getComputedStyle(document.documentElement).getPropertyValue('--surface');
      document.body.append(probe);
      const surface = rgba(getComputedStyle(probe).backgroundColor);
      probe.remove();
      const page = rgba(getComputedStyle(document.body).backgroundColor);
      const track = rgba(getComputedStyle(document.querySelector('.sp-fill-wrap')).backgroundColor);
      const over = (top, under) => top.slice(0, 3).map((c, i) => c * top[3] + under[i] * (1 - top[3]));
      const bands = {};
      for (const bar of document.querySelectorAll('#spTable .sp-fill')) {
        const band = [...bar.classList].find(c => c.startsWith('sp-fill-'));
        bands[band] = rgba(getComputedStyle(bar).backgroundColor);
      }
      return { bands, backdrops: { page: page.slice(0, 3), surface: surface.slice(0, 3), track: over(track, page) } };
    }"""

    @staticmethod
    def _contrast(a, b) -> float:
        def luminance(rgb) -> float:
            def channel(c: float) -> float:
                c /= 255
                return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

            r, g, b_ = (channel(c) for c in rgb[:3])
            return 0.2126 * r + 0.7152 * g + 0.0722 * b_

        la, lb = luminance(a), luminance(b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

    def test_fill_bars_stand_out_from_the_page_in_light_and_dark(self) -> None:
        for theme in ("light", "dark"):
            page = self._page("en", theme=theme)
            self.assertEqual(page.evaluate("document.documentElement.dataset.theme"), theme)
            self._generate(page)
            facts = page.evaluate(self.BARS)
            self.assertEqual(
                sorted(facts["bands"]), ["sp-fill-hi", "sp-fill-lo", "sp-fill-md"], theme
            )
            for band, colour in facts["bands"].items():
                self.assertEqual(colour[3], 1, f"{theme} {band} is see-through: {colour}")
                for name, backdrop in facts["backdrops"].items():
                    if name == "track" and theme == "light":
                        # Pre-existing, an owner decision: against the light
                        # --t5 track every bar is under 3:1 (teal 1.76, amber
                        # 2.03, red 2.43). The dark track passes (4.5-8.4:1).
                        continue
                    ratio = self._contrast(colour, backdrop)
                    # WCAG 1.4.11: a graphic that carries meaning needs 3:1.
                    self.assertGreaterEqual(ratio, 3, f"{theme} {band} on {name}: {ratio:.2f}")
            self.assertEqual(len({tuple(c) for c in facts["bands"].values()}), 3, theme)

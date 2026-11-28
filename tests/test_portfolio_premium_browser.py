"""The advisor portfolio's 2026-11 review, in a real browser.

Each test is one defect the live screen showed:

* the High Risk tile said 3 while its "At Risk" chip opened an empty list —
  two rules under one word;
* the picker printed "40 — null (CS2,IS2)", and wrote advisor names into
  ``innerHTML`` unescaped;
* "View (3)" and "3:13:55 AM" inside the Arabic page;
* a GPA chart of four stub bars titled "0 students" for a roster with no GPA;
* (the top-section redesign the owner approved as a mock-up, 2026-11-28) the
  advisor and the count said three times, a "Loaded" time and a Term Hrs tile
  nobody used, and no visible line between choosing, the overview and the
  roster.

The roster and advisor list are served by ``page.route``, so each test controls
exactly the numbers it asserts on.
"""

from __future__ import annotations

import json
import os
import re

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.contrib.auth.models import Group, User
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client
from django.urls import reverse

from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups, set_user_scope
from tests.test_exam_rosters_browser import WIDE_FONT

playwright = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright.sync_playwright

ADVISER_ID = "A77"

STUDENTS = [
    # high risk (score), healthy GPA
    {"student_id": 990001, "name": "HIGH RISK", "risk_score": 9.0, "gpa": 3.2},
    # low GPA, low risk score
    {"student_id": 990002, "name": "LOW GPA", "risk_score": 2.0, "gpa": 1.4},
    # no GPA, a high-priority course missing
    {
        "student_id": 990003,
        "name": "HP MISSING",
        "risk_score": 0.0,
        "gpa": None,
        "has_high_priority_missing": True,
        "high_priority_missing_courses": [{"course_code": "CS101", "score": 1.0}],
    },
]


def _roster(students, distribution):
    items = []
    for s in students:
        items.append(
            {
                "program": "AI",
                "section": "M",
                "status": "ACTIVE",
                "needs_attention": False,
                "attention_reasons": [],
                "has_high_priority_missing": False,
                "high_priority_missing_courses": [],
                "current_term_registered_hours": 12,
                **s,
            }
        )
    return {
        "advisor": {"advisor_id": ADVISER_ID, "full_name": "Dr Seventy Seven"},
        "advisor_id": ADVISER_ID,
        "mapping_ready": True,
        "count": len(items),
        "items": items,
        "summary": {
            "student_count": len(items),
            "needs_attention_count": 0,
            "very_high_risk_count": sum(1 for s in items if s["risk_score"] >= 8),
            "high_priority_missing_count": sum(1 for s in items if s["has_high_priority_missing"]),
            "gpa_distribution": distribution,
            "program_breakdown": {"AI": len(items)},
            "current_term_registered_hours_total": 12 * len(items),
        },
    }


ADVISERS = [{"advisor_id": ADVISER_ID, "full_name": "Dr Seventy Seven", "department": "AI"}]

# Where each piece of the top section sits: its box, and the zone it is in.
TOP = r"""() => {
  const box = sel => { const el = document.querySelector(sel); if (!el) return null;
    const r = el.getBoundingClientRect(); return { top: r.top, bottom: r.bottom, left: r.left, right: r.right }; };
  const ZONES = ['ap-ov-alerts', 'ap-ov-profile', 'ap-roster-head', 'page-header'];
  const zoneOf = sel => { const el = document.querySelector(sel);
    const z = el && el.closest(ZONES.map(c => '.' + c).join(', '));
    return z ? ZONES.find(c => z.classList.contains(c)) : null; };
  return {
    titles: [...document.querySelectorAll('.ap-zone-title')].map(e => e.textContent.trim()),
    alerts: [...document.querySelectorAll('.ap-ov-alerts .mc-drill')].map(e => e.dataset.focus),
    zones: Object.fromEntries(['#apScope', '#apAdvisorSelect', '#apProgramFilter', '#mStudents', '#mAvgGpa',
      '#apGpaChart', '#apPrograms', '#apInsights', '#apCsvLink'].map(s => [s, zoneOf(s)])),
    h1: box('.page-header h1'), scope: box('#apScope'), alertsBox: box('.ap-ov-alerts'),
    profileBox: box('.ap-ov-profile'), band: box('#apMetricsWrap'), rosterHead: box('.ap-roster-head'),
    table: box('#apTable'),
  };
}"""


class PortfolioPremiumBrowserTests(StaticLiveServerTestCase):
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
        *,
        super_admin=False,
        lang="en",
        roster=None,
        advisors=None,
        width=1440,
        wide_font=False,
    ):
        ensure_role_groups()
        username = "boss" if super_admin else "adv"
        # a test may open the page more than once (one per language)
        user = User.objects.filter(username=username).first() or User.objects.create_user(
            username=username, password="x", is_staff=True
        )
        if super_admin:
            user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
        set_user_scope(user.id, advisor_id="" if super_admin else ADVISER_ID)
        client = Client()
        client.force_login(user)
        context = self.browser.new_context(viewport={"width": width, "height": 900})
        self.addCleanup(context.close)
        if wide_font:
            # A CI machine without the design's fonts renders wider ones.
            context.add_init_script(WIDE_FONT)
        context.add_cookies(
            [
                {
                    "name": "sessionid",
                    "value": client.cookies["sessionid"].value,
                    "url": self.live_server_url,
                },
                {"name": "advisor_language", "value": lang, "url": self.live_server_url},
            ]
        )
        page = context.new_page()
        if roster is not None:
            page.route(
                re.compile(r".*/report/students-by-advisor/.*"),
                lambda route: route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(roster)
                ),
            )
        if advisors is not None:
            page.route(
                re.compile(r".*/report/advisors/$"),
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"count": len(advisors), "items": advisors}),
                ),
            )
        page.goto(f"{self.live_server_url}{reverse('advisor_portfolio_page')}")
        page.wait_for_load_state("networkidle")
        return page

    def _loaded(self, page):
        page.wait_for_selector("#apTable tbody tr.cr-row", timeout=15_000)

    def _row_ids(self, page):
        return page.eval_on_selector_all(
            "#apTable tbody tr.cr-row", "rows => rows.map(r => Number(r.dataset.sid))"
        )

    def test_the_high_risk_tile_opens_exactly_the_students_it_counts(self):
        page = self._page(roster=_roster(STUDENTS, [1, 0, 1, 0]))
        self._loaded(page)

        tile_count = page.inner_text("#mHighRisk").strip()
        chip_count = page.inner_text('#apFilters [data-focus="highrisk"] .fb-dd-count').strip()
        assert tile_count == chip_count == "1", (tile_count, chip_count)

        page.click('#apMetrics .mc-drill[data-focus="highrisk"]')
        assert self._row_ids(page) == [990001]
        assert page.get_attribute('#apFilters [data-focus="highrisk"]', "aria-pressed") == "true"
        assert (
            page.get_attribute('#apMetrics .mc-drill[data-focus="highrisk"]', "aria-pressed")
            == "true"
        )

        # Low GPA is its own list, and labelled as one.
        page.click('#apFilters [data-focus="risk"]')
        assert self._row_ids(page) == [990002]
        assert "Low GPA" in page.inner_text('#apFilters [data-focus="risk"]')

    def test_the_picker_escapes_names_and_never_prints_a_placeholder(self):
        page = self._page(
            super_admin=True,
            advisors=[
                {
                    "advisor_id": "A1",
                    "full_name": "<img src=x onerror=window.__xss=1>",
                    "department": "AI",
                },
                {"advisor_id": "40", "full_name": "", "department": "CS2,IS2"},
            ],
        )
        page.wait_for_function(
            "() => document.querySelectorAll('#apAdvisorSelect option').length === 3",
            timeout=15_000,
        )
        labels = page.eval_on_selector_all("#apAdvisorSelect option", "os => os.map(o => o.text)")
        assert labels[1] == "A1 — <img src=x onerror=window.__xss=1> (AI)", labels
        assert labels[2] == "40 — No name on record (CS2,IS2)", labels
        assert page.evaluate("() => document.querySelectorAll('#apAdvisorSelect img').length") == 0
        assert page.evaluate("() => window.__xss") is None

    def test_the_arabic_page_carries_no_english_view_or_am_pm(self):
        page = self._page(lang="ar", roster=_roster(STUDENTS, [1, 0, 1, 0]))
        self._loaded(page)
        hp_button = page.inner_text(".ap-hp-btn").strip()
        assert hp_button == "عرض (1)", hp_button
        # The "Loaded 3:13:55 AM" label that printed English inside the Arabic
        # page is gone with the redesign (the owner: "loaded time can be deleted").
        assert page.locator("#apLoadedTime, #apLoadedLabel").count() == 0

    def test_a_roster_without_gpas_says_so_instead_of_drawing_stub_bars(self):
        no_gpa = [{**s, "gpa": None} for s in STUDENTS]
        page = self._page(roster=_roster(no_gpa, [0, 0, 0, 0]))
        self._loaded(page)
        assert page.locator("#apGpaChart .ap-gpa-bar").count() == 0
        assert page.inner_text("#apGpaChart").strip() == "No GPA data yet"
        assert not page.is_visible("#apGpaLabels")

    def test_a_roster_with_gpas_draws_bars_to_scale_with_a_label(self):
        page = self._page(roster=_roster(STUDENTS, [1, 0, 1, 0]))
        self._loaded(page)
        heights = page.eval_on_selector_all(
            "#apGpaChart .ap-gpa-bar", "bars => bars.map(b => b.style.height)"
        )
        assert heights == ["100%", "0%", "100%", "0%"], heights
        assert "below 2: 1" in (page.get_attribute("#apGpaChart", "aria-label") or "")

    def test_a_search_narrows_the_tiles_as_it_narrows_the_list(self):
        """Review finding: the tiles ignored the search box, so with a search
        for a low-GPA student "High Risk 1" opened an empty list."""
        page = self._page(roster=_roster(STUDENTS, [1, 0, 1, 0]))
        self._loaded(page)
        page.fill("#apSearch", "LOW GPA")
        page.wait_for_function(
            "() => document.querySelectorAll('#apTable tbody tr.cr-row').length === 1"
        )
        assert page.inner_text("#mHighRisk").strip() == "0"
        assert page.inner_text('#apFilters [data-focus="highrisk"] .fb-dd-count').strip() == "0"
        assert page.inner_text("#mStudents").strip() == "1"

    def test_a_pressed_tile_unpresses_back_to_all(self):
        page = self._page(roster=_roster(STUDENTS, [1, 0, 1, 0]))
        self._loaded(page)
        tile = '#apMetrics .mc-drill[data-focus="highrisk"]'
        page.click(tile)
        assert self._row_ids(page) == [990001]
        page.click(tile)
        assert sorted(self._row_ids(page)) == [990001, 990002, 990003]
        assert page.get_attribute(tile, "aria-pressed") == "false"
        assert page.get_attribute('#apFilters [data-focus="all"]', "aria-pressed") == "true"

    # ── The top section as the owner approved it (2026-11-28 mock-up) ──────

    def _chosen(self, *, lang="en", width=1440, wide_font=False):
        """A super admin's page with the adviser chosen in the picker."""
        page = self._page(
            super_admin=True,
            lang=lang,
            width=width,
            wide_font=wide_font,
            advisors=ADVISERS,
            roster=_roster(STUDENTS, [1, 0, 1, 0]),
        )
        page.wait_for_function(
            "() => document.querySelectorAll('#apAdvisorSelect option').length === 2",
            timeout=15_000,
        )
        page.select_option("#apAdvisorSelect", ADVISER_ID)
        self._loaded(page)
        return page

    def test_the_top_says_each_thing_once(self):
        """The advisor is said by the picker, the count by the Students tile:
        the header chips that repeated both, the "Loaded" time and the Term Hrs
        tile are gone, not hidden."""
        page = self._chosen()
        gone = (
            "#apAdvisorChip, #apCountChip, #apLoadedLabel, #apLoadedTime, #mTermHours, .ap-topbar"
        )
        assert page.locator(gone).count() == 0
        assert "Term Hrs" not in page.inner_text("main")
        assert page.inner_text("#mStudents").strip() == "3"
        selected = page.eval_on_selector("#apAdvisorSelect", "s => s.selectedOptions[0].text")
        assert "Dr Seventy Seven" in selected, selected
        assert "Dr Seventy Seven" not in page.inner_text(".page-header-left")

    def test_the_top_is_three_named_zones(self):
        page = self._chosen()
        top = page.evaluate(TOP)
        assert top["titles"] == ["Needs action", "Cohort profile", "Student roster"], top["titles"]
        assert top["alerts"] == ["attention", "highrisk", "missing"], top["alerts"]
        assert top["zones"] == {
            "#apScope": "page-header",
            "#apAdvisorSelect": "page-header",
            "#apProgramFilter": "page-header",
            "#mStudents": "ap-ov-profile",
            "#mAvgGpa": "ap-ov-profile",
            "#apGpaChart": "ap-ov-profile",
            "#apPrograms": "ap-ov-profile",
            "#apInsights": "ap-ov-alerts",
            "#apCsvLink": "ap-roster-head",
        }, top["zones"]
        assert page.get_attribute("#apTable", "aria-labelledby") == "apRosterTitle"
        # The scope sits on the title's line, not on a row of its own.
        h1, scope = top["h1"], top["scope"]
        assert min(h1["bottom"], scope["bottom"]) - max(h1["top"], scope["top"]) > 0, (h1, scope)
        # The overview's two halves sit side by side, and the roster opens below them.
        alerts, profile = top["alertsBox"], top["profileBox"]
        assert abs(alerts["top"] - profile["top"]) < 1, (alerts, profile)
        assert alerts["right"] <= profile["left"] + 1, (alerts, profile)
        assert top["rosterHead"]["top"] >= top["band"]["bottom"] - 1, top
        assert top["table"]["top"] > top["rosterHead"]["bottom"], top

    def test_on_a_phone_the_halves_stack_and_the_scope_takes_the_line(self):
        page = self._chosen(width=390)
        top = page.evaluate(TOP)
        assert top["profileBox"]["top"] >= top["alertsBox"]["bottom"] - 1, top
        scope_width = top["scope"]["right"] - top["scope"]["left"]
        widths = page.eval_on_selector_all(
            "#apScope .fb-search", "els => els.map(e => e.getBoundingClientRect().width)"
        )
        assert all(abs(w - scope_width) < 1 for w in widths), (widths, scope_width)
        assert page.evaluate("() => document.documentElement.scrollWidth") <= 390

    def test_the_arabic_hp_tile_says_what_its_chip_says(self):
        page = self._chosen(lang="ar")
        tile = page.inner_text('#apMetrics .mc-drill[data-focus="missing"] .mc-lbl').strip()
        chip = page.evaluate(
            """() => { const c = document.querySelector('#apFilters [data-focus="missing"]').cloneNode(true);
              c.querySelectorAll('.fb-dd-count').forEach(n => n.remove()); return c.textContent.trim(); }"""
        )
        assert tile == chip == "أولوية مفقودة", (tile, chip)
        titles = page.eval_on_selector_all(
            ".ap-zone-title", "els => els.map(e => e.textContent.trim())"
        )
        assert titles == ["بحاجة إلى إجراء", "نظرة عامة على الطلاب", "قائمة الطلاب"], titles

    def test_on_a_phone_the_exports_share_one_line_at_touch_size(self):
        """They left the toolbar for the roster heading; on a phone they are
        still one row of 44px targets, in Arabic and in a wide font too."""
        for lang in ("ar", "en"):
            page = self._chosen(lang=lang, width=390, wide_font=True)
            boxes = page.eval_on_selector_all(
                ".ap-roster-head .fb-actions > *",
                """els => els.map(e => { const r = e.getBoundingClientRect();
                  return { text: e.textContent.trim(), top: r.top, height: r.height, left: r.left,
                           right: r.right, cut: e.scrollWidth > e.clientWidth + 1 }; })""",
            )
            assert len(boxes) == 3, boxes
            assert max(b["top"] for b in boxes) - min(b["top"] for b in boxes) < 2, (lang, boxes)
            assert all(b["height"] >= 44 for b in boxes), (lang, boxes)
            assert not any(b["cut"] for b in boxes), (lang, boxes)
            assert all(b["left"] >= 0 and b["right"] <= 390 for b in boxes), (lang, boxes)
            page.context.close()

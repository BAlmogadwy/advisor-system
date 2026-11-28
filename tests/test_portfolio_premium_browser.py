"""The advisor portfolio's 2026-11 review, in a real browser.

Each test is one defect the live screen showed:

* the High Risk tile said 3 while its "At Risk" chip opened an empty list —
  two rules under one word;
* the picker printed "40 — null (CS2,IS2)", and wrote advisor names into
  ``innerHTML`` unescaped;
* "View (3)" and "3:13:55 AM" inside the Arabic page;
* a GPA chart of four stub bars titled "0 students" for a roster with no GPA.

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

    def _page(self, *, super_admin=False, lang="en", roster=None, advisors=None):
        ensure_role_groups()
        user = User.objects.create_user(
            username="boss" if super_admin else "adv", password="x", is_staff=True
        )
        if super_admin:
            user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
        set_user_scope(user.id, advisor_id="" if super_admin else ADVISER_ID)
        client = Client()
        client.force_login(user)
        context = self.browser.new_context(viewport={"width": 1440, "height": 900})
        self.addCleanup(context.close)
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
        loaded_at = page.inner_text("#apLoadedTime").strip()
        assert hp_button == "عرض (1)", hp_button
        assert not re.search(r"[A-Za-z]", loaded_at), loaded_at
        assert page.inner_text("#apAdvisorChip").strip() == f"Dr Seventy Seven · {ADVISER_ID}"

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

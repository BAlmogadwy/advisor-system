"""Browser contract for the dedicated Adviser Portfolio graduation page."""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import patch

# Playwright's sync API runs a greenlet loop, which Django otherwise mistakes for
# an async ORM context when the live server serves the roster request.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.contrib.auth.models import User
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client
from django.urls import reverse

from core.models import Student
from core.services.rbac import set_user_scope

playwright = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright.sync_playwright

STUDENT_ID = 4_801_909
ADVISOR_ID = "ADV-GRAD-BROWSER"
REGISTERED = "registered_timetable"
RECOMMENDED = "recommended_current_term"


def _report(mode: str, marker: str) -> dict:
    baseline_code = f"{marker}101"
    future_code = f"{marker}201"
    return {
        "program": "AI",
        "plan_courses_total": 6,
        "plan_courses_passed": 2,
        "percent_courses": 33,
        "remaining_courses": 4,
        "remaining_credits": 12,
        "simulation_completed": True,
        "estimated_additional_terms": 2,
        "estimated_terms_including_planning_baseline": 3,
        "lower_bound_terms_including_planning_baseline": 2,
        "planning_baseline_kind": mode,
        "planning_baseline_academic_year": 1448,
        "planning_baseline_term": 1,
        "planning_baseline_credits": 3,
        "planning_baseline_courses_assumed_passed": [
            {"code": baseline_code, "name": f"{marker} baseline course", "credits": 3}
        ],
        "term_plan": [
            {
                "sequence": 1,
                "academic_year": 1448,
                "term": 2,
                "waiting_term": True,
                "credits": 0,
                "course_codes": [],
                "courses": [],
            },
            {
                "sequence": 2,
                "academic_year": 1449,
                "term": 1,
                "waiting_term": False,
                "credits": 3,
                "course_codes": [future_code],
                "courses": [{"code": future_code, "name": f"{marker} future course", "credits": 3}],
            },
        ],
        "unresolved_requirements": [],
    }


def _presentation(mode: str, marker: str) -> dict:
    baseline_code = f"{marker}101"
    future_code = f"{marker}201"
    baseline_status = "studying" if mode == REGISTERED else "open"
    baseline_label = (
        "Registered timetable 1448/1" if mode == REGISTERED else "Recommended starting term 1448/1"
    )
    return {
        "kind": "graduation_scenario",
        "planning_baseline_kind": mode,
        "band_labels": {
            "1": baseline_label,
            "2": "Projected 1448/2",
            "3": "Projected 1449/1",
        },
        "graph": {
            "items": [
                {
                    "course_code": future_code,
                    "prerequisite_course_code": baseline_code,
                }
            ],
            "termOf": {baseline_code: 1, future_code: 3},
            "nameOf": {
                baseline_code: f"{marker} baseline course",
                future_code: f"{marker} future course",
            },
            "statusOf": {baseline_code: baseline_status, future_code: "open"},
            "extraNodes": [baseline_code, future_code],
        },
    }


class AdvisorPortfolioGraduationBrowserTests(StaticLiveServerTestCase):
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

    def _seed_advisee(self) -> None:
        Student.objects.create(
            student_id=STUDENT_ID,
            registration_no=str(STUDENT_ID),
            name="Graduation Browser Student",
            program="AI",
            section="M",
            advisor_id=ADVISOR_ID,
            status="ACTIVE",
        )

    def _page(self, *, viewport: dict[str, int] | None = None):
        self._seed_advisee()
        user = User.objects.create_user(
            username=f"portfolio-grad-{self._testMethodName}",
            password="x",
            is_staff=True,
        )
        set_user_scope(user.id, advisor_id=ADVISOR_ID)
        client = Client()
        client.force_login(user)

        context = self.browser.new_context(viewport=viewport or {"width": 1440, "height": 1000})
        context.add_cookies(
            [
                {
                    "name": "sessionid",
                    "value": client.cookies["sessionid"].value,
                    "url": self.live_server_url,
                }
            ]
        )
        self.addCleanup(context.close)
        return context.new_page()

    @contextmanager
    def _graduation_services(self, requested_modes: list[str]):
        def build(_student_id, _year, _term, *, planning_baseline_kind):
            requested_modes.append(planning_baseline_kind)
            marker = "REC" if planning_baseline_kind == RECOMMENDED else "REG"
            return _report(planning_baseline_kind, marker)

        def present(tool_results):
            mode = tool_results[0]["planning_baseline_kind"]
            marker = "REC" if mode == RECOMMENDED else "REG"
            return _presentation(mode, marker)

        with (
            patch("core.portfolio_views.build_graduation_report", side_effect=build),
            patch(
                "core.portfolio_views.graduation_presentation_from_tool_results",
                side_effect=present,
            ),
            patch(
                "core.portfolio_views.load_defaults",
                return_value={"currentYear": "1448", "currentTerm": "1"},
            ),
        ):
            yield

    def _portfolio_url(self) -> str:
        return f"{self.live_server_url}{reverse('advisor_portfolio_page')}"

    def _graduation_url(self, mode: str | None = None) -> str:
        path = reverse(
            "advisor_portfolio_student_graduation_page",
            kwargs={"student_id": STUDENT_ID},
        )
        query = f"?baseline={mode}" if mode else ""
        return f"{self.live_server_url}{path}{query}"

    def test_portfolio_action_is_a_new_page_link_not_an_embedded_panel(self):
        page = self._page()
        page.goto(self._portfolio_url())
        page.wait_for_selector(f'#apTable tbody tr[data-sid="{STUDENT_ID}"]', timeout=15_000)
        page.click(f'#apTable tbody tr[data-sid="{STUDENT_ID}"]')

        action = page.locator("#apGraduationAction")
        expected_path = reverse(
            "advisor_portfolio_student_graduation_page",
            kwargs={"student_id": STUDENT_ID},
        )
        assert action.get_attribute("href") == expected_path
        assert action.get_attribute("target") == "_blank"
        assert action.get_attribute("rel") == "noopener"
        assert page.locator("#apGraduationPanel").count() == 0

    def test_registered_default_renders_full_table_wait_and_prerequisite_graph(self):
        page = self._page()
        requested_modes: list[str] = []

        with self._graduation_services(requested_modes):
            page.goto(self._graduation_url())
            page.wait_for_selector(".ap-grad-term-table", timeout=10_000)
            page.wait_for_selector('#sgGraph .pg-node[data-c="REG101"]', timeout=10_000)

        assert requested_modes == [REGISTERED]
        assert page.get_attribute("#agRegisteredTab", "aria-selected") == "true"
        assert page.get_attribute("#agRecommendedTab", "aria-selected") == "false"
        assert "2 / 6" in page.locator(".ap-grad-summary-grid").inner_text()
        assert page.get_attribute('.ap-grad-progress[role="progressbar"]', "aria-valuenow") == "33"

        rows = page.locator(".ap-grad-term-table tbody tr")
        assert rows.count() == 2, "waiting terms are part of the full forecast"
        waiting = page.locator(".ap-grad-term-table tbody tr.ap-grad-waiting-row")
        assert waiting.count() == 1
        assert "1448/2" in waiting.inner_text()
        assert "Waiting term" in waiting.inner_text()
        assert "waiting for prerequisites" in waiting.inner_text()
        assert "REG201" in rows.nth(1).inner_text()
        assert page.locator('#sgGraph .pg-node[data-c="REG201"]').count() == 1
        assert page.locator('#sgGraph .pg-edge[data-f="REG101"][data-t="REG201"]').count() == 1
        assert page.locator(".ap-drawer").count() == 0

    def test_arrow_navigation_opens_recommended_page_and_keeps_student_context(self):
        page = self._page()
        requested_modes: list[str] = []

        with self._graduation_services(requested_modes):
            page.goto(self._graduation_url())
            page.wait_for_selector('.ap-grad-course-code:text-is("REG101")', timeout=10_000)
            page.locator("#agRegisteredTab").focus()
            page.locator("#agRegisteredTab").press("ArrowRight")
            page.wait_for_url(f"**/graduation/?baseline={RECOMMENDED}", timeout=10_000)
            page.wait_for_selector('.ap-grad-course-code:text-is("REC101")', timeout=10_000)

        assert requested_modes == [REGISTERED, RECOMMENDED]
        assert page.get_attribute("#agRecommendedTab", "aria-selected") == "true"
        assert page.get_attribute("#agRecommendedTab", "tabindex") == "0"
        assert str(STUDENT_ID) in page.locator(".page-header").inner_text()
        assert page.locator('.ap-grad-course-code:text-is("REG101")').count() == 0

    def test_mobile_table_and_tree_scroll_inside_the_page(self):
        page = self._page(viewport={"width": 390, "height": 844})
        requested_modes: list[str] = []

        with self._graduation_services(requested_modes):
            page.goto(self._graduation_url())
            page.wait_for_selector("#sgGraph svg", timeout=10_000)

        layout = page.evaluate(
            """() => {
              const table = document.querySelector('.ap-grad-table-scroll');
              const graph = document.querySelector('.ap-grad-graph-scroll');
              return {
                viewport: window.innerWidth,
                bodyScroll: document.documentElement.scrollWidth,
                tableOverflow: getComputedStyle(table).overflowX,
                tableClient: table.clientWidth,
                tableScroll: table.scrollWidth,
                graphOverflow: getComputedStyle(graph).overflowX,
                graphClient: graph.clientWidth,
                graphScroll: graph.scrollWidth,
              };
            }"""
        )

        assert requested_modes == [REGISTERED]
        assert layout["bodyScroll"] <= layout["viewport"] + 1
        assert layout["tableOverflow"] == "auto"
        assert layout["tableScroll"] > layout["tableClient"]
        assert layout["graphOverflow"] == "auto"
        assert layout["graphScroll"] > layout["graphClient"]

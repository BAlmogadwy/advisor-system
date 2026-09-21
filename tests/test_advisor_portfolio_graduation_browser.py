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
OPTIMIZED = "optimized_current_offerings"


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
    baseline_label = {
        REGISTERED: "Registered timetable 1448/1",
        RECOMMENDED: "Recommended starting term 1448/1",
        OPTIMIZED: "Optimized current offerings 1448/1",
    }[mode]
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


def _dense_presentation(mode: str) -> dict:
    """A realistic wide map that exercises both graph layout modes."""

    roots = [f"AI1{number:02d}" for number in range(1, 10)]
    middle = [f"AI2{number:02d}" for number in range(1, 10)]
    terminal = [f"AI3{number:02d}" for number in range(1, 10)]
    codes = roots + middle + terminal
    return {
        "kind": "graduation_scenario",
        "planning_baseline_kind": mode,
        "band_labels": {
            "1": "Registered timetable 1448/1",
            "2": "Projected 1448/2",
            "3": "Projected 1449/1",
        },
        "graph": {
            "items": [
                *[
                    {"course_code": target, "prerequisite_course_code": source}
                    for source, target in zip(roots, middle, strict=True)
                ],
                *[
                    {"course_code": target, "prerequisite_course_code": source}
                    for source, target in zip(middle, terminal, strict=True)
                ],
            ],
            "termOf": {
                **dict.fromkeys(roots, 1),
                **dict.fromkeys(middle, 2),
                **dict.fromkeys(terminal, 3),
            },
            "nameOf": {code: f"Course {code}" for code in codes},
            "statusOf": {
                **dict.fromkeys(roots, "studying"),
                **dict.fromkeys(middle, "open"),
                **dict.fromkeys(terminal, "open"),
            },
            "extraNodes": codes,
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
            marker = {RECOMMENDED: "REC", REGISTERED: "REG", OPTIMIZED: "OPT"}[mode]
            return _presentation(mode, marker)

        def build_optimized(
            _student_id,
            _year,
            _term,
            *,
            section_snapshot_academic_year,
            section_snapshot_term,
        ):
            assert (section_snapshot_academic_year, section_snapshot_term) == (1448, 1)
            requested_modes.append(OPTIMIZED)
            report = _report(OPTIMIZED, "OPT")
            report["planning_baseline"] = {"kind": OPTIMIZED}
            report["offering_optimization"] = {
                "candidate_count": 6,
                "evaluated_maximal_subset_count": 3,
            }
            return report

        with (
            patch("core.portfolio_views.build_graduation_report", side_effect=build),
            patch(
                "core.portfolio_views.build_optimized_current_offerings_report",
                side_effect=build_optimized,
            ),
            patch(
                "core.portfolio_views.graduation_presentation_from_tool_results",
                side_effect=present,
            ),
            patch(
                "core.portfolio_views.load_defaults",
                return_value={
                    "currentYear": "1448",
                    "currentTerm": "1",
                    "academic_year": "1448",
                    "term": "1",
                },
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
        assert page.get_attribute("#agOptimizedTab", "aria-selected") == "false"
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

    def test_third_tab_opens_exact_optimized_recorded_offerings_without_registration_claim(self):
        page = self._page()
        requested_modes: list[str] = []

        with self._graduation_services(requested_modes):
            page.goto(self._graduation_url(RECOMMENDED))
            page.wait_for_selector('.ap-grad-course-code:text-is("REC101")', timeout=10_000)
            page.locator("#agRecommendedTab").focus()
            page.locator("#agRecommendedTab").press("ArrowRight")
            page.wait_for_url(f"**/graduation/?baseline={OPTIMIZED}", timeout=10_000)
            page.wait_for_selector('.ap-grad-course-code:text-is("OPT101")', timeout=10_000)

        assert requested_modes == [RECOMMENDED, OPTIMIZED]
        assert page.get_attribute("#agOptimizedTab", "aria-selected") == "true"
        assert page.get_attribute("#agOptimizedTab", "tabindex") == "0"
        provenance = page.locator(".ap-grad-provenance").inner_text()
        assert "Exact optimization from the recorded current-section snapshot" in provenance
        assert "not guarantee registration, seats, or a clash-free timetable" in provenance
        assert "Actual registered timetable" not in provenance

    def test_mobile_table_scrolls_and_tree_stays_inside_the_page(self):
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
        assert layout["graphScroll"] <= layout["graphClient"] + 1

    def test_dense_tree_modes_fit_desktop_without_making_courses_unreadable(self):
        page = self._page(viewport={"width": 1440, "height": 1000})
        requested_modes: list[str] = []

        with self._graduation_services(requested_modes):
            with patch(
                "core.portfolio_views.graduation_presentation_from_tool_results",
                return_value=_dense_presentation(REGISTERED),
            ):
                page.goto(self._graduation_url())
                page.wait_for_selector('#sgGraph .pg-node[data-c="AI301"]', timeout=10_000)

                def measure_graph() -> dict:
                    return page.evaluate(
                        """() => {
                          const scroll = document.querySelector('.ap-grad-graph-scroll');
                          const svg = scroll.querySelector('svg');
                          const nodes = [...svg.querySelectorAll('.pg-node rect')];
                          const labels = [...svg.querySelectorAll('.pg-node text')];
                          const bandLabels = [...svg.querySelectorAll('.pg-band-lbl')];
                          const svgLeft = svg.getBoundingClientRect().left;
                          return {
                            clientWidth: scroll.clientWidth,
                            scrollWidth: scroll.scrollWidth,
                            nodeCount: nodes.length,
                            minNodeWidth: Math.min(...nodes.map(node => node.getBoundingClientRect().width)),
                            minNodeHeight: Math.min(...nodes.map(node => node.getBoundingClientRect().height)),
                            minLabelHeight: Math.min(...labels.map(label => label.getBoundingClientRect().height)),
                            minBandLabelInset: bandLabels.length
                              ? Math.min(...bandLabels.map(label => label.getBoundingClientRect().left - svgLeft))
                              : null,
                          };
                        }"""
                    )

                term_layout = measure_graph()
                page.click("#sgPgChain")
                page.wait_for_function(
                    "document.querySelector('#sgPgChain').getAttribute('aria-pressed') === 'true'"
                )
                chain_layout = measure_graph()

        assert requested_modes == [REGISTERED]
        for mode_name, layout in (
            ("projected term", term_layout),
            ("prerequisite chain", chain_layout),
        ):
            assert layout["nodeCount"] == 27
            assert layout["scrollWidth"] <= layout["clientWidth"] + 1, (mode_name, layout)
            assert layout["minNodeWidth"] >= 70, (mode_name, layout)
            assert layout["minNodeHeight"] >= 30, (mode_name, layout)
            assert layout["minLabelHeight"] >= 10, (mode_name, layout)
        assert term_layout["minBandLabelInset"] >= -1, term_layout

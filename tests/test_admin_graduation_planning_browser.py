"""Browser-level checks for the admin graduation-planning workflow."""

import os
from contextlib import contextmanager
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

# Playwright's sync greenlet otherwise looks like an async ORM context to Django.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.contrib.auth.models import Group, User
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client
from django.urls import reverse

from core.models import ProgrammeRequirement, Student
from core.services.rbac import ROLE_SUPER_ADMIN
from core.services.student_graduation import REGISTERED_TIMETABLE

playwright = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright.sync_playwright


STUDENT_ID = 4_881_765


def _report() -> dict:
    return {
        "program": "DS2",
        "plan_courses_total": 3,
        "plan_courses_passed": 1,
        "percent_courses": 33,
        "remaining_courses": 2,
        "remaining_credits": 6,
        "simulation_completed": True,
        "estimated_terms_including_planning_baseline": 2,
        "lower_bound_terms_including_planning_baseline": 2,
        "planning_baseline_kind": REGISTERED_TIMETABLE,
        "planning_baseline_academic_year": 1448,
        "planning_baseline_term": 2,
        "planning_baseline_credits": 3,
        "planning_baseline_courses_assumed_passed": [
            {"code": "DS201", "name": "Registered course", "credits": 3}
        ],
        "term_plan": [
            {
                "sequence": 1,
                "academic_year": 1449,
                "term": 1,
                "waiting_term": False,
                "credits": 6,
                "courses": [
                    {"code": "DS301", "name": "Future course", "credits": 3},
                    {"code": "DS302", "name": "Another course", "credits": 3},
                ],
            }
        ],
        "unresolved_requirements": [],
    }


def _scenario_report() -> dict:
    report = _report()
    added = {
        "code": "DS301",
        "name": "Scenario course",
        "credits": 3,
        "must_have": True,
        "scenario_role": "must_have",
    }
    report["planning_baseline_courses_assumed_passed"] = [
        *report["planning_baseline_courses_assumed_passed"],
        added,
    ]
    report["planning_baseline_credits"] = 6
    report["what_if"] = {
        "mode": "must_have_current_term",
        "valid": True,
        "must_have_courses": [added],
        "auto_added_prerequisites": [],
        "displaced_baseline_courses": [],
        "same_term_direct_prerequisite_edges": [],
        "validation_errors": [],
        "baseline": {"estimated_terms_including_planning_baseline": 3},
        "scenario": {"estimated_terms_including_planning_baseline": 2},
        "comparison": {
            "timing_effect": "EARLIER",
            "terms_saved": 1,
            "term_difference": -1,
        },
    }
    return report


class AdminGraduationPlanningBrowserTests(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._playwright = sync_playwright().start()
        cls.browser = cls._playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls._playwright.stop()
        super().tearDownClass()

    def setUp(self) -> None:
        Student.objects.create(
            student_id=STUDENT_ID,
            registration_no=str(STUDENT_ID),
            name="Admin Browser Student",
            program="DS2",
            section="F",
            status="ACTIVE",
        )
        ProgrammeRequirement.objects.create(
            program="DS2",
            course_code="DS301",
            course_name="Scenario course",
            programme_term=3,
            credit_hours=3,
            type="Mandatory",
        )
        user = User.objects.create_user(username="admin-graduation-browser", password="x")
        user.groups.add(Group.objects.get_or_create(name=ROLE_SUPER_ADMIN)[0])
        client = Client()
        client.force_login(user)
        self.context = self.browser.new_context(viewport={"width": 390, "height": 844})
        self.context.add_cookies(
            [
                {
                    "name": "sessionid",
                    "value": client.cookies["sessionid"].value,
                    "url": self.live_server_url,
                }
            ]
        )
        self.page = self.context.new_page()

    def tearDown(self) -> None:
        self.context.close()
        super().tearDown()

    @contextmanager
    def _graduation_services(self):
        with (
            patch("core.portfolio_views.build_graduation_report", return_value=_report()),
            patch(
                "core.portfolio_views.graduation_presentation_from_tool_results",
                return_value={"graph": {"extraNodes": []}},
            ),
            patch(
                "core.portfolio_views.load_defaults",
                return_value={
                    "currentYear": "1448",
                    "currentTerm": "2",
                    "academic_year": "1448",
                    "term": "2",
                },
            ),
        ):
            yield

    def test_search_opens_admin_result_with_responsive_layout_and_active_navigation(self):
        landing_path = reverse("admin_graduation_planning_page")
        result_path = reverse(
            "admin_graduation_student_page",
            kwargs={"student_id": STUDENT_ID},
        )

        with self._graduation_services():
            self.page.goto(f"{self.live_server_url}{landing_path}")
            student_input = self.page.locator("#adminGraduationStudentId")
            assert student_input.evaluate("element => element === document.activeElement")
            student_input.fill(str(STUDENT_ID))
            student_input.press("Enter")
            self.page.wait_for_url(f"**{result_path}")
            self.page.wait_for_selector(".ap-grad-term-table")

        assert self.page.locator("#adminGraduationStudentId").input_value() == str(STUDENT_ID)
        assert (
            self.page.locator('.sidebar .nav-link.active[href="/graduation-planning/"]').count()
            == 1
        )
        assert (
            self.page.locator('.sidebar .nav-link.active[href="/advisor-portfolio/"]').count() == 0
        )
        assert self.page.locator("#agRecommendedTab").get_attribute("href") == (
            f"{result_path}?baseline=recommended_current_term"
        )
        assert self.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")

    def test_invalid_search_is_inline_and_returns_focus_to_the_input(self):
        landing_path = reverse("admin_graduation_planning_page")
        self.page.goto(f"{self.live_server_url}{landing_path}")
        student_input = self.page.locator("#adminGraduationStudentId")
        student_input.fill("not-a-student")
        with self.page.expect_response(lambda response: "student_id=" in response.url) as pending:
            self.page.get_by_role("button", name="Generate prediction", exact=True).click()

        assert pending.value.status == 400
        assert self.page.locator("#adminGraduationSearchError").get_attribute("role") == "alert"
        assert student_input.get_attribute("aria-invalid") == "true"
        assert student_input.evaluate("element => element === document.activeElement")
        assert self.page.locator("#advisorGraduationResults").count() == 0

    def test_admin_can_build_a_shareable_must_have_scenario_with_keyboard_controls(self):
        result_path = reverse(
            "admin_graduation_student_page",
            kwargs={"student_id": STUDENT_ID},
        )

        with (
            self._graduation_services(),
            patch(
                "core.portfolio_views.build_graduation_must_have_scenario",
                return_value=_scenario_report(),
            ),
        ):
            self.page.goto(f"{self.live_server_url}{result_path}")
            picker = self.page.locator("#adminGradPlanPicker")
            picker.select_option("DS301")
            picker.press("Enter")
            assert self.page.locator("#adminGradMustHave").input_value() == "DS301"
            assert self.page.locator('[data-course-code="DS301"]').count() == 1
            self.page.locator("#adminGradSameTermPrerequisites").check()
            self.page.get_by_role("button", name="Run simulation", exact=True).click()
            self.page.wait_for_url("**?*must_have=DS301*")
            self.page.wait_for_selector(".admin-grad-scenario-result")

        query = parse_qs(urlparse(self.page.url).query)
        assert query == {
            "baseline": ["registered_timetable"],
            "must_have": ["DS301"],
            "allow_same_term_prerequisites": ["1"],
        }
        assert self.page.get_by_text("Hypothetical scenario applied", exact=True).count() == 1
        assert self.page.get_by_text("3 → 2 terms", exact=True).count() == 1
        assert self.page.get_by_text("Must-have scenario course", exact=True).count() == 1
        assert self.page.locator("#agRecommendedTab").get_attribute("href") == (
            f"{result_path}?baseline=recommended_current_term&must_have=DS301&"
            "allow_same_term_prerequisites=1"
        )
        assert "must_have=DS301" in self.page.locator(".advisor-grad-export").get_attribute("href")
        assert self.page.locator("#adminGradMustHave").get_attribute("aria-invalid") is None
        assert self.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")

    def test_invalid_manual_course_keeps_the_forecast_and_focuses_the_error(self):
        result_path = reverse(
            "admin_graduation_student_page",
            kwargs={"student_id": STUDENT_ID},
        )
        with self._graduation_services():
            self.page.goto(f"{self.live_server_url}{result_path}")
            self.page.locator("#adminGradMustHave").fill("NOTINPLAN")
            with self.page.expect_response(
                lambda response: "must_have=NOTINPLAN" in response.url
            ) as pending:
                self.page.get_by_role("button", name="Run simulation", exact=True).click()
            self.page.wait_for_selector("#adminGradScenarioErrors")

        assert pending.value.status == 400
        assert self.page.locator(".ap-grad-term-table").count() == 1
        assert self.page.locator(".advisor-grad-export").count() == 0
        assert self.page.locator("#adminGradScenarioErrors").evaluate(
            "element => element === document.activeElement"
        )
        assert self.page.locator("#adminGradMustHave").get_attribute("aria-invalid") == "true"
        assert self.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")

"""Request contracts for the super-admin graduation-planning workspace."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import Mock

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse

from core.models import ProgrammeRequirement, Student
from core.services.rbac import ROLE_ADVISOR, ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN
from core.services.student_graduation import REGISTERED_TIMETABLE

pytestmark = pytest.mark.django_db

STUDENT_ID = 4_880_321


@pytest.fixture
def student() -> Student:
    return Student.objects.create(
        student_id=STUDENT_ID,
        registration_no=str(STUDENT_ID),
        name="Admin Graduation Student",
        program="CS",
        section="F",
        status="ACTIVE",
    )


def _client(username: str, role: str) -> Client:
    user = User.objects.create_user(username=username, password="x")
    user.groups.add(Group.objects.get_or_create(name=role)[0])
    client = Client()
    client.force_login(user)
    return client


def _landing_url() -> str:
    return reverse("admin_graduation_planning_page")


def _student_url(student_id: int = STUDENT_ID) -> str:
    return reverse("admin_graduation_student_page", kwargs={"student_id": student_id})


def _export_url(student_id: int = STUDENT_ID) -> str:
    return reverse("admin_graduation_student_export_xlsx", kwargs={"student_id": student_id})


def _report() -> dict:
    return {
        "program": "CS",
        "plan_courses_total": 3,
        "plan_courses_passed": 1,
        "percent_courses": 33,
        "remaining_courses": 2,
        "remaining_credits": 6,
        "simulation_completed": True,
        "estimated_additional_terms": 2,
        "estimated_terms_including_planning_baseline": 3,
        "lower_bound_terms_including_planning_baseline": 2,
        "planning_baseline_kind": REGISTERED_TIMETABLE,
        "planning_baseline_academic_year": 1448,
        "planning_baseline_term": 2,
        "planning_baseline_credits": 3,
        "planning_baseline_courses_assumed_passed": [
            {"code": "CS201", "name": "Registered baseline", "credits": 3}
        ],
        "term_plan": [
            {
                "sequence": 1,
                "academic_year": 1449,
                "term": 1,
                "waiting_term": False,
                "credits": 6,
                "course_codes": ["CS301", "CS302"],
                "courses": [
                    {"code": "CS301", "name": "First", "credits": 3},
                    {"code": "CS302", "name": "Second", "credits": 3},
                ],
            }
        ],
        "unresolved_requirements": [],
    }


def _add_plan_course(code: str, *, term: int = 3, credits: int = 3) -> None:
    ProgrammeRequirement.objects.create(
        program="CS",
        course_code=code,
        course_name=f"Course {code}",
        programme_term=term,
        credit_hours=credits,
        type="Mandatory",
    )


def _scenario_report(*codes: str) -> dict:
    baseline = _report()
    scenario = deepcopy(baseline)
    added = [
        {
            "code": code,
            "name": f"Course {code}",
            "credits": 3,
            "scenario_role": "must_have",
            "must_have": True,
        }
        for code in codes
    ]
    scenario["planning_baseline_courses_assumed_passed"] = [
        *scenario["planning_baseline_courses_assumed_passed"],
        *added,
    ]
    scenario["planning_baseline_credits"] = 3 + 3 * len(added)
    scenario["what_if"] = {
        "mode": "must_have_current_term",
        "valid": True,
        "requested_must_have_course_codes": list(codes),
        "must_have_courses": added,
        "already_in_baseline_courses": [],
        "added_must_have_courses": added,
        "auto_added_prerequisites": [],
        "same_term_direct_prerequisite_edges": [],
        "displaced_baseline_courses": [],
        "validation_errors": [],
        "baseline": {
            "estimated_terms_including_planning_baseline": 3,
            "planning_baseline_credits": 3,
        },
        "scenario": {
            "estimated_terms_including_planning_baseline": 2,
            "planning_baseline_credits": scenario["planning_baseline_credits"],
        },
        "comparison": {
            "timing_effect": "EARLIER",
            "terms_saved": 1,
            "term_difference": -1,
        },
    }
    return scenario


def _install_successful_services(monkeypatch):
    report = _report()
    build = Mock(return_value=report)
    presentation = Mock(return_value={"graph": {"extraNodes": []}})
    monkeypatch.setattr("core.portfolio_views.build_graduation_report", build)
    monkeypatch.setattr(
        "core.portfolio_views.graduation_presentation_from_tool_results",
        presentation,
    )
    monkeypatch.setattr(
        "core.portfolio_views.load_defaults",
        lambda: {
            "currentYear": "1448",
            "currentTerm": "2",
            "academic_year": "1448",
            "term": "2",
        },
    )
    return build, presentation, report


@pytest.mark.parametrize(
    "url_factory",
    [_landing_url, _student_url, _export_url],
)
def test_every_admin_graduation_route_requires_super_admin(url_factory, student, monkeypatch):
    build, _presentation, _report_payload = _install_successful_services(monkeypatch)

    assert Client().get(url_factory()).status_code == 401
    assert (
        _client(f"advisor-{url_factory.__name__}", ROLE_ADVISOR).get(url_factory()).status_code
        == 403
    )
    assert (
        _client(f"general-{url_factory.__name__}", ROLE_GENERAL_ADVISOR)
        .get(url_factory())
        .status_code
        == 403
    )
    build.assert_not_called()


def test_landing_renders_an_empty_native_get_lookup(student):
    response = _client("graduation-admin-landing", ROLE_SUPER_ADMIN).get(_landing_url())

    assert response.status_code == 200
    body = response.content.decode()
    assert 'method="get"' in body.lower()
    assert 'name="student_id"' in body
    assert f'action="{_landing_url()}"' in body
    assert 'id="advisorGraduationResults"' not in body


@pytest.mark.parametrize("student_id", ["", "not-a-number", "2147483648"])
def test_landing_rejects_blank_invalid_and_oversized_ids_inline(student, monkeypatch, student_id):
    build, _presentation, _report_payload = _install_successful_services(monkeypatch)

    response = _client(f"graduation-admin-bad-{student_id or 'blank'}", ROLE_SUPER_ADMIN).get(
        _landing_url(), {"student_id": student_id}
    )

    assert response.status_code == 400
    body = response.content.decode()
    assert 'name="student_id"' in body
    assert 'role="alert"' in body
    assert 'id="advisorGraduationResults"' not in body
    build.assert_not_called()


def test_landing_returns_inline_404_for_an_unknown_numeric_id(monkeypatch):
    build, _presentation, _report_payload = _install_successful_services(monkeypatch)
    missing_id = 4_889_999

    response = _client("graduation-admin-missing", ROLE_SUPER_ADMIN).get(
        _landing_url(), {"student_id": str(missing_id)}
    )

    assert response.status_code == 404
    body = response.content.decode()
    assert str(missing_id) in body
    assert 'role="alert"' in body
    assert 'id="advisorGraduationResults"' not in body
    build.assert_not_called()


def test_landing_redirects_a_known_student_to_the_canonical_result(student):
    response = _client("graduation-admin-lookup", ROLE_SUPER_ADMIN).get(
        _landing_url(), {"student_id": str(STUDENT_ID)}
    )

    assert response.status_code == 302
    assert response.headers["Location"] == _student_url()


def test_result_defaults_to_registered_and_keeps_all_navigation_admin_local(student, monkeypatch):
    build, _presentation, report = _install_successful_services(monkeypatch)

    response = _client("graduation-admin-result", ROLE_SUPER_ADMIN).get(_student_url())

    assert response.status_code == 200
    assert response.context["baseline_kind"] == REGISTERED_TIMETABLE
    assert response.context["grad"] == report
    body = response.content.decode()
    for baseline in (
        "registered_timetable",
        "recommended_current_term",
        "optimized_current_offerings",
    ):
        assert f"{_student_url()}?baseline={baseline}" in body
    assert f"{_export_url()}?baseline={REGISTERED_TIMETABLE}" in body
    assert f'action="{_landing_url()}"' in body
    assert 'name="student_id"' in body
    assert f'href="{_landing_url()}"' in body
    # Admin users must not be routed back through adviser-portfolio navigation.
    assert (
        reverse("advisor_portfolio_student_graduation_page", kwargs={"student_id": STUDENT_ID})
        not in body
    )
    build.assert_called_once_with(
        STUDENT_ID,
        1448,
        2,
        planning_baseline_kind=REGISTERED_TIMETABLE,
    )


def test_unknown_student_result_is_404_before_prediction(monkeypatch):
    build, _presentation, _report_payload = _install_successful_services(monkeypatch)

    response = _client("graduation-admin-result-missing", ROLE_SUPER_ADMIN).get(
        _student_url(STUDENT_ID + 1)
    )

    assert response.status_code == 404
    build.assert_not_called()


def test_admin_export_reuses_the_shared_graduation_workbook(student, monkeypatch):
    build, _presentation, report = _install_successful_services(monkeypatch)
    export = Mock(return_value=b"PK-admin-graduation-workbook")
    monkeypatch.setattr("core.portfolio_views.build_graduation_xlsx", export)

    response = _client("graduation-admin-export", ROLE_SUPER_ADMIN).get(_export_url())

    assert response.status_code == 200
    assert response.content == b"PK-admin-graduation-workbook"
    assert response.headers["Content-Type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert str(STUDENT_ID) in response.headers["Content-Disposition"]
    assert "no-store" in response.headers["Cache-Control"]
    build.assert_called_once_with(
        STUDENT_ID,
        1448,
        2,
        planning_baseline_kind=REGISTERED_TIMETABLE,
    )
    export.assert_called_once_with(
        student=student,
        academic_year=1448,
        term=2,
        baseline_kind=REGISTERED_TIMETABLE,
        report=report,
        presentation={"graph": {"extraNodes": []}},
        language_code="en",
    )


def test_admin_must_have_scenario_is_applied_and_preserved_in_navigation(student, monkeypatch):
    build, presentation, report = _install_successful_services(monkeypatch)
    _add_plan_course("CS301", term=3)
    _add_plan_course("CS302", term=4)
    scenario = _scenario_report("CS301", "CS302")
    scenario_builder = Mock(return_value=scenario)
    monkeypatch.setattr(
        "core.portfolio_views.build_graduation_must_have_scenario",
        scenario_builder,
    )

    response = _client("graduation-admin-scenario", ROLE_SUPER_ADMIN).get(
        _student_url(),
        {
            "baseline": REGISTERED_TIMETABLE,
            "must_have": [" cs301 ", "CS302", "CS301"],
            "allow_same_term_prerequisites": "1",
        },
    )

    assert response.status_code == 200
    assert response.context["grad"] == scenario
    assert response.context["graduation_scenario_valid"] is True
    assert response.context["graduation_must_have_courses"] == ["CS301", "CS302"]
    scenario_builder.assert_called_once_with(
        STUDENT_ID,
        1448,
        2,
        baseline_report=report,
        must_have_courses=["CS301", "CS302"],
        allow_same_term_direct_prerequisites=True,
    )
    assert presentation.call_count == 2
    body = response.content.decode()
    preserved = "must_have=CS301&amp;must_have=CS302&amp;allow_same_term_prerequisites=1"
    assert preserved in body
    assert "Hypothetical scenario applied" in body
    assert "3 → 2 terms" in body
    assert 'data-course-code="CS301"' in body
    build.assert_called_once()


def test_admin_scenario_rejects_courses_outside_the_student_plan_and_keeps_baseline(
    student, monkeypatch
):
    _build, presentation, report = _install_successful_services(monkeypatch)
    scenario_builder = Mock()
    monkeypatch.setattr(
        "core.portfolio_views.build_graduation_must_have_scenario",
        scenario_builder,
    )

    response = _client("graduation-admin-plan-only", ROLE_SUPER_ADMIN).get(
        _student_url(),
        {"baseline": REGISTERED_TIMETABLE, "must_have": "NOTINPLAN"},
    )

    assert response.status_code == 400
    assert response.context["grad"] == report
    assert response.context["graduation_can_export"] is False
    assert response.context["graduation_scenario_errors"] == [
        {"kind": "MUST_HAVE_COURSE_NOT_IN_PLAN", "course_code": "NOTINPLAN"}
    ]
    assert "the factual baseline forecast remains visible" in response.content.decode()
    assert 'class="ap-grad-term-table"' in response.content.decode()
    assert "advisor-grad-export" not in response.content.decode()
    scenario_builder.assert_not_called()
    presentation.assert_called_once()


def test_admin_scenario_rejects_delimiter_only_course_input(student, monkeypatch):
    _build, presentation, report = _install_successful_services(monkeypatch)
    scenario_builder = Mock()
    monkeypatch.setattr(
        "core.portfolio_views.build_graduation_must_have_scenario",
        scenario_builder,
    )

    response = _client("graduation-admin-empty-scenario", ROLE_SUPER_ADMIN).get(
        _student_url(),
        {"baseline": REGISTERED_TIMETABLE, "must_have": ", ;\r\n"},
    )

    assert response.status_code == 400
    assert response.context["grad"] == report
    assert response.context["graduation_can_export"] is False
    assert response.context["graduation_scenario_errors"] == [{"kind": "NO_MUST_HAVE_COURSES"}]
    assert "Add at least one course code" in response.content.decode()
    assert 'class="ap-grad-term-table"' in response.content.decode()
    assert "advisor-grad-export" not in response.content.decode()
    scenario_builder.assert_not_called()
    presentation.assert_called_once()


def test_invalid_engine_scenario_never_replaces_or_hides_the_base_forecast(student, monkeypatch):
    _build, presentation, report = _install_successful_services(monkeypatch)
    _add_plan_course("CS301")
    invalid = {
        **deepcopy(report),
        "what_if": {
            "valid": False,
            "validation_errors": [
                {
                    "kind": "SAME_TERM_PREREQUISITE_NOT_STRICTLY_ELIGIBLE",
                    "course_code": "CS201",
                    "required_by": ["CS301"],
                    "missing_prerequisites": ["CS101"],
                    "credit_hour_gate": None,
                },
                {
                    "kind": "GRADUATION_PROJECT_SEQUENCE_REQUIRES_NEXT_TERM",
                    "course_code": "CS492",
                    "prerequisite_code": "CS491",
                },
            ],
        },
    }
    monkeypatch.setattr(
        "core.portfolio_views.build_graduation_must_have_scenario",
        Mock(return_value=invalid),
    )

    response = _client("graduation-admin-invalid-engine", ROLE_SUPER_ADMIN).get(
        _student_url(),
        {
            "baseline": REGISTERED_TIMETABLE,
            "must_have": "CS301",
            "allow_same_term_prerequisites": "1",
        },
    )

    assert response.status_code == 400
    assert response.context["grad"] == report
    assert response.context["graduation_scenario_valid"] is False
    assert "CS201" in response.content.decode()
    assert "CS101" in response.content.decode()
    assert "immediately following main term" in response.content.decode()
    # Invalid what-if presentations intentionally collapse to {}; never call it.
    presentation.assert_called_once()


def test_admin_export_applies_the_same_valid_must_have_scenario(student, monkeypatch):
    _build, _presentation, _report_payload = _install_successful_services(monkeypatch)
    _add_plan_course("CS301")
    scenario = _scenario_report("CS301")
    scenario_builder = Mock(return_value=scenario)
    export = Mock(return_value=b"PK-admin-scenario-workbook")
    monkeypatch.setattr(
        "core.portfolio_views.build_graduation_must_have_scenario",
        scenario_builder,
    )
    monkeypatch.setattr("core.portfolio_views.build_graduation_xlsx", export)

    response = _client("graduation-admin-scenario-export", ROLE_SUPER_ADMIN).get(
        _export_url(),
        {"baseline": REGISTERED_TIMETABLE, "must_have": "CS301"},
    )

    assert response.status_code == 200
    assert response.content == b"PK-admin-scenario-workbook"
    assert "_scenario_" in response.headers["Content-Disposition"]
    assert export.call_args.kwargs["report"] == scenario
    scenario_builder.assert_called_once()


def test_admin_export_rejects_an_invalid_scenario_instead_of_exporting_baseline(
    student, monkeypatch
):
    _build, _presentation, report = _install_successful_services(monkeypatch)
    _add_plan_course("CS301")
    invalid = {
        **deepcopy(report),
        "what_if": {
            "valid": False,
            "validation_errors": [{"kind": "ALREADY_PASSED", "course_code": "CS301"}],
        },
    }
    monkeypatch.setattr(
        "core.portfolio_views.build_graduation_must_have_scenario",
        Mock(return_value=invalid),
    )
    export = Mock()
    monkeypatch.setattr("core.portfolio_views.build_graduation_xlsx", export)

    response = _client("graduation-admin-invalid-export", ROLE_SUPER_ADMIN).get(
        _export_url(),
        {"baseline": REGISTERED_TIMETABLE, "must_have": "CS301"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_MUST_HAVE_SCENARIO"
    export.assert_not_called()


def test_admin_export_rejects_delimiter_only_scenario_input(student, monkeypatch):
    _install_successful_services(monkeypatch)
    export = Mock()
    scenario_builder = Mock()
    monkeypatch.setattr("core.portfolio_views.build_graduation_xlsx", export)
    monkeypatch.setattr(
        "core.portfolio_views.build_graduation_must_have_scenario",
        scenario_builder,
    )

    response = _client("graduation-admin-empty-export", ROLE_SUPER_ADMIN).get(
        _export_url(),
        {"baseline": REGISTERED_TIMETABLE, "must_have": ", ;\r\n"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": "The must-have graduation scenario is invalid.",
        "code": "INVALID_MUST_HAVE_SCENARIO",
        "validation_errors": [{"kind": "NO_MUST_HAVE_COURSES"}],
    }
    scenario_builder.assert_not_called()
    export.assert_not_called()


def test_advisor_workspace_ignores_admin_override_parameters(student, monkeypatch):
    _build, _presentation, _report_payload = _install_successful_services(monkeypatch)
    scenario_builder = Mock()
    monkeypatch.setattr(
        "core.portfolio_views.build_graduation_must_have_scenario",
        scenario_builder,
    )
    advisor_url = reverse(
        "advisor_portfolio_student_graduation_page",
        kwargs={"student_id": STUDENT_ID},
    )

    response = _client("graduation-advisor-no-override", ROLE_SUPER_ADMIN).get(
        advisor_url,
        {"baseline": REGISTERED_TIMETABLE, "must_have": "CS301"},
    )

    assert response.status_code == 200
    assert response.context["graduation_admin_mode"] is False
    assert response.context["graduation_can_export"] is True
    assert "Must-have starting-term simulation" not in response.content.decode()
    scenario_builder.assert_not_called()

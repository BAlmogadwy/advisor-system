from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from django.test import RequestFactory
from django.utils.translation import override

from core import student_auth_views
from core.models import Student
from core.services.advisor_presentations import graduation_presentation_from_tool_results

STUDENT_ID = 4402162


def _report(kind: str = "recommended_current_term") -> dict:
    return {
        "program": "AI",
        "plan_courses_total": 10,
        "plan_courses_passed": 5,
        "percent_courses": 50,
        "remaining_courses": 5,
        "remaining_credits": 15,
        "earned_credits_registrar": 60,
        "planning_baseline_academic_year": 1448,
        "planning_baseline_term": 1,
        "planning_baseline_kind": kind,
        "planning_baseline_credits": 3,
        "planning_baseline_courses_assumed_passed": [
            {"code": "AI433", "name": "Deep Learning", "credits": 3}
        ],
        "chain_floor_terms": 1,
        "capacity_floor_terms_after_planning_baseline": 1,
        "lower_bound_additional_terms": 1,
        "lower_bound_terms_including_planning_baseline": 2,
        "estimated_additional_terms": 1,
        "estimated_terms_including_planning_baseline": 2,
        "simulation_completed": True,
        "max_credits_per_term": 18,
        "term_plan": [],
        "unresolved_requirements": [],
        "hour_gates": [],
        "scenario_graph": {},
    }


def _render_page(monkeypatch, *, language: str, defaults: dict, report: dict):
    calls: list[tuple[int, int, int, dict]] = []

    def fake_report(student_id: int, year: int, term: int, **kwargs):
        calls.append((student_id, year, term, kwargs))
        return deepcopy(report)

    monkeypatch.setattr(student_auth_views, "load_defaults", lambda: defaults)
    monkeypatch.setattr(student_auth_views, "build_graduation_report", fake_report)
    monkeypatch.setattr(
        student_auth_views,
        "get_user_scope",
        lambda _user: {"role": "STUDENT", "student_id": STUDENT_ID},
    )
    monkeypatch.setattr(student_auth_views, "get_sidebar_context", lambda _request: {})
    monkeypatch.setattr(
        student_auth_views,
        "prefer_arabic_course_names_in_payload",
        lambda payload: payload,
    )

    request = RequestFactory().get("/student/graduation/")
    request.user = SimpleNamespace(is_authenticated=True)
    request.LANGUAGE_CODE = language
    with override(language):
        response = student_auth_views.student_graduation_view(request)
    return response.content.decode("utf-8"), calls


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("language", "recommended_label", "registered_label", "recommendation_note"),
    [
        (
            "ar",
            "مقررات فصل البداية الموصى بها",
            "المقررات المسجّلة فعليًا في فصل البداية",
            "هذه توصية تخطيطية وليست تسجيلًا فعليًا",
        ),
        (
            "en",
            "Recommended starting-term courses",
            "Actually registered in the baseline term",
            "This is a planning recommendation, not an actual registration",
        ),
    ],
)
def test_default_page_uses_configured_term_recommendations_and_labels_them_honestly(
    monkeypatch,
    language,
    recommended_label,
    registered_label,
    recommendation_note,
):
    Student.objects.create(student_id=STUDENT_ID, program="AI")
    html, calls = _render_page(
        monkeypatch,
        language=language,
        defaults={
            "academic_year": 1447,
            "term": 2,
            "currentYear": 1448,
            "currentTerm": 1,
        },
        report=_report(),
    )

    assert calls == [
        (
            STUDENT_ID,
            1448,
            1,
            {"planning_baseline_kind": "recommended_current_term"},
        )
    ]
    assert recommended_label in html
    assert registered_label not in html
    assert recommendation_note in html
    assert "18" in html
    assert "AI433" in html
    assert "expected_plan_comparison" not in html
    assert "Additional courses in the expected timetable" not in html
    assert "مقررات إضافية في الجدول المتوقع" not in html


@pytest.mark.django_db
def test_page_falls_back_to_legacy_academic_defaults_when_current_keys_are_absent(monkeypatch):
    Student.objects.create(student_id=STUDENT_ID, program="AI")
    _html, calls = _render_page(
        monkeypatch,
        language="en",
        defaults={"academic_year": 1449, "term": 2},
        report=_report(),
    )

    assert calls[0][1:3] == (1449, 2)


@pytest.mark.django_db
def test_registered_timetable_variant_keeps_registered_provenance(monkeypatch):
    Student.objects.create(student_id=STUDENT_ID, program="AI")
    html, _calls = _render_page(
        monkeypatch,
        language="en",
        defaults={"academic_year": 1448, "term": 1},
        report=_report("registered_timetable"),
    )

    assert "Actually registered in the baseline term" in html
    assert "Recommended starting-term courses" not in html
    assert "actually registered in the university portal" in html


def _presentation_result(kind: str) -> dict:
    return {
        "tool": "graduation_progress",
        "ok": True,
        "program": "AI",
        "planning_baseline_academic_year": 1448,
        "planning_baseline_term": 1,
        "planning_baseline_kind": kind,
        "planning_baseline_credits": 3,
        "planning_baseline_courses_assumed_passed": [
            {"code": "AI433", "name": "Deep Learning", "credits": 3}
        ],
        "term_plan": [
            {
                "sequence": 1,
                "academic_year": 1448,
                "term": 2,
                "courses": [{"code": "AI482", "name": "AI Security", "credits": 3}],
            }
        ],
        "scenario_graph": {
            "items": [
                {"course_code": "AI433", "prerequisite_course_code": "AI201"},
                {"course_code": "AI482", "prerequisite_course_code": "AI433"},
            ],
            "statusOf": {"AI201": "passed", "AI433": "studying", "AI482": "locked"},
            "nameOf": {
                "AI201": "Introduction to AI",
                "AI433": "Deep Learning",
                "AI482": "AI Security",
            },
        },
    }


def test_recommended_baseline_presentation_is_not_marked_as_registered_or_studying():
    presentation = graduation_presentation_from_tool_results(
        [_presentation_result("recommended_current_term")]
    )

    assert presentation["planning_baseline_kind"] == "recommended_current_term"
    assert presentation["planning_baseline_credits"] == 3
    assert presentation["band_labels"]["1"] == "Recommended starting term 1448/1"
    assert presentation["graph"]["statusOf"]["AI433"] == "open"


def test_registered_baseline_presentation_retains_registered_status():
    presentation = graduation_presentation_from_tool_results(
        [_presentation_result("registered_timetable")]
    )

    assert presentation["planning_baseline_kind"] == "registered_timetable"
    assert presentation["band_labels"]["1"] == "Registered timetable 1448/1"
    assert presentation["graph"]["statusOf"]["AI433"] == "studying"

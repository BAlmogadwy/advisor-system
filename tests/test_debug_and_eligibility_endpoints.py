import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from pytest import MonkeyPatch

from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups

pytestmark = pytest.mark.django_db

client = Client()


def _login_superadmin() -> None:
    ensure_role_groups()
    user, _ = User.objects.get_or_create(username="test-debug-admin")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client.force_login(user)


def test_recommendation_debug_endpoint(monkeypatch: MonkeyPatch) -> None:
    _login_superadmin()
    monkeypatch.setattr(
        "core.report_views.build_recommendation_debug_report",
        lambda current_academic_year, current_semester, section=None, program=None, join_year_prefixes=None, limit=150: {
            "count": 1,
            "filters": {"year": current_academic_year, "semester": current_semester},
            "items": [
                {
                    "student_id": 4410001,
                    "program": "AI",
                    "real_term": 3,
                    "next_term": 4,
                    "passed": ["CS101"],
                    "studying": ["CS102"],
                    "recommended_courses": ["AI201"],
                    "recommendation_details": [],
                }
            ],
        },
    )

    response = client.get("/report/recommendation-debug/?year=1448&semester=0")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["student_id"] == 4410001


def test_course_eligibility_endpoint(monkeypatch: MonkeyPatch) -> None:
    _login_superadmin()
    monkeypatch.setattr(
        "core.report_views.build_course_eligibility_report",
        lambda course_code, section=None, program=None, join_year_prefixes=None, strict_passed_only=False: {
            "course_code": course_code,
            "strict_passed_only": strict_passed_only,
            "total_students": 20,
            "total_eligible": 7,
            "per_program": [
                {
                    "program": "AI",
                    "students": 20,
                    "eligible_count": 7,
                    "eligible_student_ids": [4410001, 4410002],
                    "prerequisites": ["CS102"],
                    "blocked_samples": [],
                }
            ],
        },
    )

    response = client.get("/report/course-eligibility/?course_code=AI201&mode=strict")

    assert response.status_code == 200
    payload = response.json()
    assert payload["course_code"] == "AI201"
    assert payload["strict_passed_only"] is True
    assert payload["total_eligible"] == 7


def test_export_recommendation_debug_csv(monkeypatch: MonkeyPatch) -> None:
    _login_superadmin()
    monkeypatch.setattr(
        "core.report_views.build_recommendation_debug_report",
        lambda current_academic_year, current_semester, section=None, program=None, join_year_prefixes=None, limit=150: {
            "count": 1,
            "items": [
                {
                    "student_id": 4410001,
                    "program": "AI",
                    "real_term": 3,
                    "next_term": 4,
                    "passed": ["CS101"],
                    "studying": ["CS102"],
                    "recommended_courses": ["AI201"],
                }
            ],
        },
    )

    response = client.get("/export/recommendation-debug.csv?year=1448&semester=0")

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    text = response.content.decode("utf-8")
    assert "student_id,program,real_term,next_term" in text
    assert "4410001,AI,3,4,CS101,CS102,AI201" in text

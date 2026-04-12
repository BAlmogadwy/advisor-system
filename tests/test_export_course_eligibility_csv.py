import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from pytest import MonkeyPatch

from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups

pytestmark = pytest.mark.django_db

client = Client()


def _login_superadmin() -> None:
    ensure_role_groups()
    user, _ = User.objects.get_or_create(username="test-export-elig-admin")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client.force_login(user)


def test_export_course_eligibility_xlsx(monkeypatch: MonkeyPatch) -> None:
    _login_superadmin()
    monkeypatch.setattr(
        "core.report_views.build_course_eligibility_report",
        lambda course_code,
        section=None,
        program=None,
        join_year_prefixes=None,
        strict_passed_only=False: {
            "course_code": course_code,
            "strict_passed_only": strict_passed_only,
            "filters": {"section": section, "program": program},
            "total_students": 30,
            "total_eligible": 12,
            "per_program": [
                {
                    "program": "AI",
                    "students": 30,
                    "eligible_count": 12,
                    "eligible_student_ids": [4410001, 4410002, 4410003],
                    "prerequisites": ["CS102", "MATH101"],
                    "blocked_count": 18,
                    "blocked_ratio": 0.6,
                    "blocked_samples": [],
                    "top_missing_prerequisites": [
                        {"course_code": "CS102", "count": 10},
                    ],
                }
            ],
        },
    )

    response = client.get("/export/course-eligibility.csv?course_code=AI201&mode=relaxed")

    assert response.status_code == 200
    content_type = response["Content-Type"]
    assert "spreadsheet" in content_type or "xlsx" in content_type or "octet-stream" in content_type
    # Verify it's a valid XLSX by checking the magic bytes (PK zip header)
    content = (
        b"".join(response.streaming_content)
        if hasattr(response, "streaming_content")
        else response.content
    )
    assert content[:2] == b"PK", "Response should be a valid XLSX (ZIP) file"

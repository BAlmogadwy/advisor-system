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


def test_export_course_eligibility_csv(monkeypatch: MonkeyPatch) -> None:
    _login_superadmin()
    monkeypatch.setattr(
        "core.report_views.build_course_eligibility_report",
        lambda course_code,
        section=None,
        program=None,
        join_year_prefixes=None,
        strict_passed_only=False: {
            "course_code": course_code,
            "per_program": [
                {
                    "program": "AI",
                    "students": 30,
                    "eligible_count": 12,
                    "eligible_student_ids": [4410001, 4410002, 4410003],
                    "prerequisites": ["CS102", "MATH101"],
                }
            ],
        },
    )

    response = client.get("/export/course-eligibility.csv?course_code=AI201&mode=relaxed")

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    text = response.content.decode("utf-8")
    assert (
        "course_code,mode,program,students,eligible_count,eligible_student_ids,prerequisites"
        in text
    )
    assert 'AI201,relaxed,AI,30,12,"4410001,4410002,4410003","CS102,MATH101"' in text

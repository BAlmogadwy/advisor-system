from collections import Counter

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from pytest import MonkeyPatch

from core.models import Student
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups

pytestmark = pytest.mark.django_db

client = Client()


def _login_superadmin() -> None:
    ensure_role_groups()
    user, _ = User.objects.get_or_create(username="test-report-export-admin")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client.force_login(user)


def test_export_student_csv(monkeypatch: MonkeyPatch) -> None:
    _login_superadmin()

    def fake_recommend_next_courses(
        student_id: int,
        current_academic_year: int,
        current_semester: int,
    ) -> list[str]:
        assert student_id == 3551131
        assert current_academic_year == 1448
        assert current_semester == 0
        return ["CS323", "CS451"]

    monkeypatch.setattr(
        "core.report_views.recommend_next_courses",
        fake_recommend_next_courses,
    )
    Student.objects.update_or_create(
        student_id=3551131,
        defaults={"program": "CS", "advisor_id": "A001"},
    )

    response = client.get("/export/student.csv?student_id=3551131&year=1448&semester=0")

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    text = response.content.decode("utf-8")
    assert "student_id,year,semester,course_code" in text
    assert "3551131,1448,0,CS323" in text
    assert "3551131,1448,0,CS451" in text


def test_report_summary_json(monkeypatch: MonkeyPatch) -> None:
    _login_superadmin()

    def fake_build_aggregate_counts(
        year: int,
        semester: int,
        program: str | None = None,
        section: str | None = None,
    ) -> tuple[int, Counter[str]]:
        assert year == 1448
        assert semester == 0
        assert program == "CS"
        assert section == "A"
        return 2, Counter({"CS323": 2, "CS451": 1})

    monkeypatch.setattr(
        "core.report_views.build_aggregate_counts",
        fake_build_aggregate_counts,
    )

    response = client.get("/report/summary/?year=1448&semester=0&program=CS&section=A")

    assert response.status_code == 200
    payload = response.json()
    assert payload["student_count"] == 2
    assert payload["top_recommended_courses"][0] == {"course_code": "CS323", "count": 2}


def test_export_aggregate_csv(monkeypatch: MonkeyPatch) -> None:
    _login_superadmin()

    def fake_build_aggregate_counts(
        year: int,
        semester: int,
        program: str | None = None,
        section: str | None = None,
    ) -> tuple[int, Counter[str]]:
        return 3, Counter({"CS323": 2, "CS451": 1})

    monkeypatch.setattr(
        "core.report_views.build_aggregate_counts",
        fake_build_aggregate_counts,
    )

    response = client.get("/export/aggregate.csv?year=1448&semester=0")

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    text = response.content.decode("utf-8")
    assert "year,semester,program,section,student_count,course_code,count" in text
    assert "1448,0,,,3,CS323,2" in text

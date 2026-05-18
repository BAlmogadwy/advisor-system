from collections import Counter

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from pytest import MonkeyPatch

from core.models import ProgrammeRequirement, Student
from core.report_views import _build_batch_course_rows
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
    assert payload["top_recommended_courses"][0]["course_code"] == "CS323"
    assert payload["top_recommended_courses"][0]["course_name"] == ""
    assert payload["top_recommended_courses"][0]["count"] == 2


def test_program_plan_view_includes_course_names() -> None:
    _login_superadmin()
    ProgrammeRequirement.objects.create(
        program="AI2",
        course_code="CS111",
        course_name="FUNDAMENTALS OF PROGRAMMING",
        programme_term=1,
        credit_hours=3,
    )

    response = client.get("/report/program-plan/?program=AI2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"][0]["course_code"] == "CS111"
    assert payload["items"][0]["course_name"] == "FUNDAMENTALS OF PROGRAMMING"


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
    assert (
        "year,semester,program,section,student_count,programs,course_code,course_name,count" in text
    )
    assert "1448,0,,,3,,CS323,,2" in text


def test_batch_course_rows_split_same_code_different_plan_names(
    monkeypatch: MonkeyPatch,
) -> None:
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="CS111",
        course_name="PROGRAMMING I",
        type="core",
        programme_term=1,
        credit_hours=3,
    )
    ProgrammeRequirement.objects.create(
        program="AI2",
        course_code="CS111",
        course_name="FUNDAMENTALS OF PROGRAMMING",
        type="core",
        programme_term=1,
        credit_hours=3,
    )

    def fake_build_aggregate_counts(
        year: int,
        semester: int,
        program: str | None = None,
        section: str | None = None,
    ) -> tuple[int, Counter[str]]:
        assert year == 1448
        assert semester == 1
        assert section == "M"
        if program == "AI":
            return 4, Counter({"CS111": 4})
        if program == "AI2":
            return 4, Counter({"CS111": 4})
        return 8, Counter({"CS111": 8})

    monkeypatch.setattr(
        "core.report_views.build_aggregate_counts",
        fake_build_aggregate_counts,
    )

    student_count, rows = _build_batch_course_rows(
        year=1448,
        semester=1,
        program="AI,AI2",
        section="M",
    )

    assert student_count == 8
    assert rows == [
        {
            "course_code": "CS111",
            "course_name": "FUNDAMENTALS OF PROGRAMMING",
            "count": 4,
            "programs": ["AI2"],
            "show_programs": True,
        },
        {
            "course_code": "CS111",
            "course_name": "PROGRAMMING I",
            "count": 4,
            "programs": ["AI"],
            "show_programs": True,
        },
    ]


def test_batch_course_rows_merge_same_code_same_plan_name(
    monkeypatch: MonkeyPatch,
) -> None:
    for program in ["AI", "AI2"]:
        ProgrammeRequirement.objects.create(
            program=program,
            course_code="CS111",
            course_name="PROGRAMMING I",
            type="core",
            programme_term=1,
            credit_hours=3,
        )

    def fake_build_aggregate_counts(
        year: int,
        semester: int,
        program: str | None = None,
        section: str | None = None,
    ) -> tuple[int, Counter[str]]:
        if program == "AI":
            return 4, Counter({"CS111": 4})
        if program == "AI2":
            return 4, Counter({"CS111": 4})
        return 8, Counter({"CS111": 8})

    monkeypatch.setattr(
        "core.report_views.build_aggregate_counts",
        fake_build_aggregate_counts,
    )

    student_count, rows = _build_batch_course_rows(
        year=1448,
        semester=1,
        program="AI,AI2",
        section="M",
    )

    assert student_count == 8
    assert rows == [
        {
            "course_code": "CS111",
            "course_name": "PROGRAMMING I",
            "count": 8,
            "programs": ["AI", "AI2"],
            "show_programs": True,
        }
    ]

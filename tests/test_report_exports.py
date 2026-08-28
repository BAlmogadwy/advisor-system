from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from django.contrib.auth.models import Group, User
from django.http import FileResponse
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


@pytest.mark.parametrize(
    ("mode_query", "expected_mode", "expected_strict"),
    [
        ("", "strict", True),
        ("&mode=relaxed", "relaxed", False),
        ("&mode=unknown", "strict", True),
    ],
)
def test_export_student_csv(
    monkeypatch: MonkeyPatch,
    mode_query: str,
    expected_mode: str,
    expected_strict: bool,
) -> None:
    _login_superadmin()

    def fake_recommend_next_courses(
        student_id: int,
        current_academic_year: int,
        current_semester: int,
        *,
        strict_passed_only: bool,
    ) -> list[str]:
        assert student_id == 3551131
        assert current_academic_year == 1448
        assert current_semester == 0
        assert strict_passed_only is expected_strict
        return ["CS323", "CS451"]

    monkeypatch.setattr(
        "core.report_views.recommend_next_courses",
        fake_recommend_next_courses,
    )
    Student.objects.update_or_create(
        student_id=3551131,
        defaults={"program": "CS", "advisor_id": "A001"},
    )

    response = client.get(
        f"/export/student.csv?student_id=3551131&year=1448&semester=0{mode_query}"
    )

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    assert response["X-Recommendation-Mode"] == expected_mode
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
        *,
        strict_passed_only: bool = False,
    ) -> tuple[int, Counter[str]]:
        assert year == 1448
        assert semester == 0
        assert program == "CS"
        assert section == "A"
        assert strict_passed_only is True
        return 2, Counter({"CS323": 2, "CS451": 1})

    monkeypatch.setattr(
        "core.report_views.build_aggregate_counts",
        fake_build_aggregate_counts,
    )

    response = client.get("/report/summary/?year=1448&semester=0&program=CS&section=A")

    assert response.status_code == 200
    payload = response.json()
    assert payload["student_count"] == 2
    assert payload["mode"] == "strict"
    assert payload["strict_passed_only"] is True
    assert payload["top_recommended_courses"][0]["course_code"] == "CS323"
    assert payload["top_recommended_courses"][0]["course_name"] == ""
    assert payload["top_recommended_courses"][0]["count"] == 2


def test_report_summary_relaxed_mode_is_explicit(monkeypatch: MonkeyPatch) -> None:
    _login_superadmin()
    strict_values: list[bool] = []

    def fake_build_aggregate_counts(
        year: int,
        semester: int,
        program: str | None = None,
        section: str | None = None,
        *,
        strict_passed_only: bool = False,
    ) -> tuple[int, Counter[str]]:
        strict_values.append(strict_passed_only)
        return 1, Counter({"AI201": 1})

    monkeypatch.setattr(
        "core.report_views.build_aggregate_counts",
        fake_build_aggregate_counts,
    )

    response = client.get("/report/summary/?year=1448&semester=0&mode=%20ReLaXeD%20")

    assert response.status_code == 200
    assert response.json()["mode"] == "relaxed"
    assert response.json()["strict_passed_only"] is False
    assert strict_values == [False]


@pytest.mark.parametrize(
    "path",
    [
        "/report/summary/?year=1448&semester=0&mode=unknown",
        "/export/aggregate.csv?year=1448&semester=0&mode=unknown",
        "/export/aggregate.xlsx?year=1448&semester=0&mode=unknown",
    ],
)
def test_batch_endpoints_reject_invalid_mode(path: str) -> None:
    _login_superadmin()

    response = client.get(path)

    assert response.status_code == 400
    assert response.json()["error"] == "mode must be strict or relaxed"


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
        *,
        strict_passed_only: bool = False,
    ) -> tuple[int, Counter[str]]:
        assert strict_passed_only is True
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
        "year,semester,mode,program,section,student_count,programs,course_code,course_name,count"
        in text
    )
    assert "1448,0,strict,,,3,,CS323,,2" in text
    assert response["X-Recommendation-Mode"] == "strict"
    assert "aggregate_1448_0_strict.csv" in response["Content-Disposition"]


@pytest.mark.parametrize(
    ("mode_query", "expected_mode", "expected_strict"),
    [
        ("", "strict", True),
        ("&mode=relaxed", "relaxed", False),
    ],
)
def test_export_aggregate_xlsx_carries_mode(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    mode_query: str,
    expected_mode: str,
    expected_strict: bool,
) -> None:
    _login_superadmin()
    row_modes: list[bool] = []
    export_modes: list[bool] = []

    def fake_rows(
        *,
        year: int,
        semester: int,
        program: str | None,
        section: str | None,
        strict_passed_only: bool = True,
        limit: int | None = None,
    ) -> tuple[int, list[dict[str, object]]]:
        row_modes.append(strict_passed_only)
        return 1, []

    def fake_export(*args: object, **kwargs: object) -> Path:
        strict_passed_only = kwargs["strict_passed_only"]
        assert isinstance(strict_passed_only, bool)
        export_modes.append(strict_passed_only)
        path = tmp_path / f"batch-{expected_mode}.xlsx"
        path.write_bytes(b"PK")
        return path

    monkeypatch.setattr("core.report_views._build_batch_course_rows", fake_rows)
    monkeypatch.setattr("core.services.batch_export.export_batch_recommender_xlsx", fake_export)

    response = client.get(f"/export/aggregate.xlsx?year=1448&semester=0{mode_query}")

    assert response.status_code == 200
    assert response["X-Recommendation-Mode"] == expected_mode
    assert f"batch_recommender_all_1448_T0_{expected_mode}.xlsx" in response["Content-Disposition"]
    assert row_modes == [expected_strict]
    assert export_modes == [expected_strict]
    file_response = cast(FileResponse, response)
    streaming_content = cast(Iterator[bytes], file_response.streaming_content)
    assert b"".join(streaming_content) == b"PK"


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
        *,
        strict_passed_only: bool = False,
    ) -> tuple[int, Counter[str]]:
        assert year == 1448
        assert semester == 1
        assert section == "M"
        assert strict_passed_only is True
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
        *,
        strict_passed_only: bool = False,
    ) -> tuple[int, Counter[str]]:
        assert strict_passed_only is True
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

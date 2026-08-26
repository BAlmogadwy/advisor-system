import pytest

from core.models import AcademicAdvisor, Course, Student, StudentCourse
from core.services import advisors

pytestmark = pytest.mark.django_db


def test_upsert_academic_advisor_normalizes_multiple_departments() -> None:
    payload = advisors.upsert_academic_advisor("TESTA1", "Dr X", "testx@u.edu", "cs, ai ; cs")
    assert payload["advisor"]["department"] == "CS,AI"
    assert payload["advisor"]["departments"] == ["CS", "AI"]


def test_high_priority_cache_reuses_program_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(advisors, "_hp_missing_cache", {})

    AcademicAdvisor.objects.update_or_create(
        advisor_id="T001",
        defaults={"full_name": "Dr. One", "email": "t1@uni.edu", "department": "CS"},
    )
    s = Student.objects.create(
        student_id=9910001,
        registration_no="R-1",
        name="Student One",
        program="CS",
        section="M",
        status="active",
        gpa=2.9,
        total_registered_credits=70,
        total_earned_credits=62,
        advisor_id="T001",
    )
    c = Course.objects.create(course_code="TSTUDY101", credit_hours=12)
    StudentCourse.objects.create(student=s, course=c, status="studying")

    calls = {"n": 0}

    def fake_hp(**kwargs: object) -> dict[str, list[object]]:
        calls["n"] += 1
        return {"items": []}

    monkeypatch.setattr(advisors, "run_missing_high_priority_report", fake_hp)

    advisors.list_students_by_advisor("T001")
    advisors.list_students_by_advisor("T001")

    assert calls["n"] == 1


@pytest.mark.parametrize(
    ("current_term", "expected_report_parity", "expected_pattern"),
    [(1, 0, "odd"), (2, 1, "even")],
)
def test_portfolio_maps_global_term_to_study_plan_parity(
    monkeypatch: pytest.MonkeyPatch,
    current_term: int,
    expected_report_parity: int,
    expected_pattern: str,
) -> None:
    monkeypatch.setattr(advisors, "_hp_missing_cache", {})
    monkeypatch.setattr(
        advisors,
        "load_defaults",
        lambda: {"currentYear": 1448, "currentTerm": current_term},
    )
    received: dict[str, object] = {}

    def fake_hp(**kwargs: object) -> dict[str, list[object]]:
        received.update(kwargs)
        return {
            "items": [
                {
                    "student_id": 9910001,
                    "missing_this_parity": [{"course_code": "CS163", "score": 4.83}],
                }
            ]
        }

    monkeypatch.setattr(advisors, "run_missing_high_priority_report", fake_hp)

    result = advisors._get_high_priority_by_program("DS2")

    assert received["year"] == 1448
    assert received["semester"] == current_term
    assert received["term_parity"] == expected_report_parity
    assert result[9910001][0]["term_pattern"] == expected_pattern


def test_high_priority_cache_changes_with_global_term(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(advisors, "_hp_missing_cache", {})
    defaults = {"currentYear": 1448, "currentTerm": 1}
    monkeypatch.setattr(advisors, "load_defaults", lambda: dict(defaults))
    received_parities: list[object] = []

    def fake_hp(**kwargs: object) -> dict[str, list[object]]:
        received_parities.append(kwargs["term_parity"])
        return {"items": []}

    monkeypatch.setattr(advisors, "run_missing_high_priority_report", fake_hp)

    advisors._get_high_priority_by_program("DS2")
    defaults["currentTerm"] = 2
    advisors._get_high_priority_by_program("DS2")

    assert received_parities == [0, 1]


def test_portfolio_does_not_invent_parity_outside_main_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(advisors, "_hp_missing_cache", {})
    monkeypatch.setattr(
        advisors,
        "load_defaults",
        lambda: {"currentYear": 1448, "currentTerm": 3},
    )

    def unexpected_report(**kwargs: object) -> dict[str, list[object]]:
        raise AssertionError(f"report should not run outside terms 1 and 2: {kwargs}")

    monkeypatch.setattr(advisors, "run_missing_high_priority_report", unexpected_report)

    assert advisors._get_high_priority_by_program("DS2") == {}


@pytest.mark.parametrize(
    ("program_filter", "expected_id"),
    [(" ai ", 1), ("AI2", 2), (" ds ", 3), ("DS2", 4)],
)
def test_advisor_program_filter_matches_exact_normalized_curriculum(
    program_filter: str,
    expected_id: int,
) -> None:
    items = [
        {"student_id": 1, "program": "AI"},
        {"student_id": 2, "program": "AI2"},
        {"student_id": 3, "program": "DS"},
        {"student_id": 4, "program": "DS2"},
    ]

    result = advisors._apply_advisor_filters(items, program_filter=program_filter)

    assert [row["student_id"] for row in result] == [expected_id]


def test_list_students_by_advisor_sorted_by_attention_then_gpa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(advisors, "_hp_missing_cache", {})

    AcademicAdvisor.objects.update_or_create(
        advisor_id="T002",
        defaults={"full_name": "Dr. Two", "email": "t2@uni.edu", "department": "CS"},
    )
    c12 = Course.objects.create(course_code="THOURS12", credit_hours=12)

    s3 = Student.objects.create(
        student_id=9910003,
        registration_no="R-3",
        name="Student Three",
        program="CS",
        section="M",
        status="active",
        gpa=3.8,
        total_registered_credits=90,
        total_earned_credits=80,
        advisor_id="T002",
    )
    StudentCourse.objects.create(student=s3, course=c12, status="studying")

    Student.objects.create(
        student_id=9910002,
        registration_no="R-2",
        name="Student Two",
        program="CS",
        section="M",
        status="active",
        gpa=2.4,
        total_registered_credits=80,
        total_earned_credits=70,
        advisor_id="T002",
    )

    s1 = Student.objects.create(
        student_id=9910001,
        registration_no="R-1",
        name="Student One",
        program="CS",
        section="M",
        status="active",
        gpa=1.9,
        total_registered_credits=70,
        total_earned_credits=62,
        advisor_id="T002",
    )
    StudentCourse.objects.create(student=s1, course=c12, status="studying")

    monkeypatch.setattr(
        advisors, "run_missing_high_priority_report", lambda **kwargs: {"items": []}
    )

    payload = advisors.list_students_by_advisor("T002")
    ids = [x["student_id"] for x in payload["items"]]

    assert ids == [9910002, 9910001, 9910003]


def test_list_students_by_advisor_enriched_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(advisors, "_hp_missing_cache", {})

    AcademicAdvisor.objects.update_or_create(
        advisor_id="T003",
        defaults={"full_name": "Dr. Three", "email": "t3@uni.edu", "department": "CS"},
    )
    c15 = Course.objects.create(course_code="THOURS15", credit_hours=15)

    Student.objects.create(
        student_id=9910001,
        registration_no="R-1",
        name="Student One",
        program="CS",
        section="M",
        status="active",
        gpa=1.9,
        total_registered_credits=70,
        total_earned_credits=62,
        advisor_id="T003",
    )

    s2 = Student.objects.create(
        student_id=9910002,
        registration_no="R-2",
        name="Student Two",
        program="CS",
        section="M",
        status="active",
        gpa=3.2,
        total_registered_credits=80,
        total_earned_credits=70,
        advisor_id="T003",
    )
    StudentCourse.objects.create(student=s2, course=c15, status="studying")

    monkeypatch.setattr(
        advisors,
        "run_missing_high_priority_report",
        lambda **kwargs: {
            "items": [
                {
                    "student_id": 9910002,
                    "missing_this_parity": [{"course_code": "CS211", "score": 4.0}],
                    "missing_other": [{"course_code": "AI201", "score": 2.5}],
                }
            ]
        },
    )

    payload = advisors.list_students_by_advisor("T003")

    assert payload["mapping_ready"] is True
    assert payload["count"] == 2

    by_id = {row["student_id"]: row for row in payload["items"]}
    s1_row = by_id[9910001]
    s2_row = by_id[9910002]

    assert s1_row["current_term_registered_hours"] == 0
    assert s2_row["current_term_registered_hours"] == 15

    assert s1_row["has_high_priority_missing"] is False
    assert s2_row["has_high_priority_missing"] is True
    assert s2_row["high_priority_missing_courses"][0]["course_code"] == "CS211"
    # missing_other courses are intentionally excluded by _get_high_priority_by_program
    # (only this_parity bucket is shown to advisors for actionable recommendations)
    assert len(s2_row["high_priority_missing_courses"]) == 1

    assert s1_row["needs_attention"] is True
    assert s2_row["needs_attention"] is True
    assert s1_row["risk_score"] > 0
    assert "low_gpa" in s1_row["attention_reasons"]
    assert "high_priority_missing" in s2_row["attention_reasons"]

    summary = payload["summary"]
    assert summary["avg_gpa"] == 2.55
    assert summary["low_gpa_count"] == 1
    assert summary["high_priority_missing_count"] == 1
    assert summary["needs_attention_count"] == 2
    assert summary["current_term_registered_hours_total"] == 15
    assert summary["program_summaries"]["CS"]["student_count"] == 2
    assert summary["program_summaries"]["CS"]["high_priority_missing_count"] == 1

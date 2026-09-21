from __future__ import annotations

import pytest
from django.db import connection, models
from django.test.utils import CaptureQueriesContext

from core.management.commands.scrape_students import _process_student
from core.models import (
    Course,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
)
from core.services.course_classifier import parse_course_result
from core.services.virtual_advisor import build_verified_student_context

pytestmark = pytest.mark.django_db

_STUDENT_ID = 990_202
_TARGET_CODE = "VX101"


def _study_html(*, target_letter: str, target_mark: str) -> str:
    profile = """
    <table class="forumline" dir="ltr">
      <tr><th>Registeration No:</th><td>990202</td></tr>
      <tr><th>Student Name</th><td>Transition Student</td></tr>
      <tr><th>Nationality</th><td>Saudi</td></tr>
      <tr><th>Student Status</th><td>Active</td></tr>
      <tr><th>T.U.Registered</th><td>90</td></tr>
      <tr><th>T.U.Earned</th><td>87</td></tr>
      <tr><th>G.P.A</th><td>3.50</td></tr>
    </table>
    """

    level_tables: list[str] = []
    for level, start in (("FIRST", 101), ("SECOND", 111), ("THIRD", 121)):
        rows: list[str] = []
        for number in range(start, start + 10):
            if number == 101:
                letter = target_letter
                mark = target_mark
            else:
                letter = "A"
                mark = "90"
            rows.append(
                "<tr>"
                f"<td>{letter}</td><td>{mark}</td><td>3</td>"
                f"<td>{number}</td><td>VX</td><td>Transition Course {number}</td>"
                "</tr>"
            )
        level_tables.append(
            f'<table dir="rtl"><tr><th>{level} LEVEL</th></tr>{"".join(rows)}</table>'
        )
    return f"<html><body>{profile}{''.join(level_tables)}</body></html>"


def _timetable_html(*, register_target: bool, external: bool = False) -> str:
    course_code = "EX" if external else "VX"
    number = 999 if external else 101 if register_target else 102
    return f"""
    <html><body>
      <div>العام الدراسي : 1448 الفصل الدراسي : الأول</div>
      <table class="forumline">
        <tr><th>رقم الطالب</th><td>{_STUDENT_ID}</td></tr>
        <tr><th>مجموع الوحدات المسجلة</th><td>3</td></tr>
        <tr><th>المرشد الاكاديمي</th><td>Transition Advisor</td></tr>
      </table>
      <table class="forumline">
        <tr>
          <th>م</th><th>المادة</th><th>رمز</th><th>رقم</th><th>ساعات</th><th>شعبة</th>
          <th>من</th><th>إلى</th><th>أحد</th><th>اثنين</th><th>ثلاثاء</th>
          <th>أربعاء</th><th>خميس</th><th>مبنى</th><th>دور</th><th>قاعة</th>
        </tr>
        <tr>
          <td>1</td><td>Transition Course {number}</td><td>{course_code}</td><td>{number}</td>
          <td>3</td><td>M7</td><td>09:00</td><td>10:15</td>
          <td><img src="mark.jpg"></td><td></td><td></td><td></td><td></td>
          <td>B1</td><td>F1</td><td>R101</td>
        </tr>
      </table>
    </body></html>
    """


def _seed_existing_course(
    *,
    course_code: str = _TARGET_CODE,
    status: str = "studying",
    grade: str = "OLD",
    mark: float | None = 42.5,
    actual_term: str = "1447/2",
    is_external: bool = False,
) -> StudentCourse:
    student = Student.objects.create(
        student_id=_STUDENT_ID,
        registration_no=str(_STUDENT_ID),
        name="Existing Student",
        nationality="Saudi",
        status="Active",
        gpa=3.0,
        total_registered_credits=60,
        total_earned_credits=54,
        current_registered_credits=3,
        program="AI",
        section="M",
        advisor_id="7001",
    )
    course = Course.objects.create(
        course_code=course_code,
        department="EX" if is_external else "VX",
        description="External Retake" if is_external else "Transition Course 101",
        credit_hours=3,
        is_external=is_external,
    )
    return StudentCourse.objects.create(
        student=student,
        course=course,
        programme_term=None if is_external else 1,
        status=status,
        grade=grade,
        mark=mark,
        actual_term=actual_term,
    )


def _rows(model: type[models.Model]) -> list[dict[str, object]]:
    fields = [field.attname for field in model._meta.concrete_fields]
    return list(model._default_manager.order_by(model._meta.pk.attname).values(*fields))


def _snapshot_state() -> dict[str, list[dict[str, object]]]:
    tracked_models = (
        Student,
        Course,
        StudentCourse,
        TermSection,
        TermSectionMeeting,
        TermSectionProgram,
        StudentTermSection,
    )
    return {model._meta.label_lower: _rows(model) for model in tracked_models}


def _mutation_queries(queries: CaptureQueriesContext) -> list[str]:
    mutation_prefixes = ("INSERT ", "UPDATE ", "DELETE ", "REPLACE ")
    return [
        query["sql"]
        for query in queries.captured_queries
        if query["sql"].lstrip().upper().startswith(mutation_prefixes)
    ]


@pytest.mark.parametrize(
    (
        "target_letter",
        "target_mark",
        "register_target",
        "expected_status",
        "expected_grade",
        "expected_mark",
    ),
    [
        pytest.param(
            "B+",
            "85",
            True,
            "passed",
            "B+",
            85.0,
            id="passed-result-wins-even-when-currently-registered",
        ),
        pytest.param(
            "F",
            "55",
            False,
            "failed",
            "F",
            55.0,
            id="explicit-failure-when-not-currently-registered",
        ),
        pytest.param(
            "F",
            "55",
            True,
            "studying",
            "F",
            55.0,
            id="current-registration-wins-over-prior-failing-result",
        ),
        pytest.param(
            "",
            "",
            False,
            "not_taken",
            "OLD",
            42.5,
            id="blank-result-becomes-not-taken-without-erasing-history",
        ),
        pytest.param(
            "A",
            "",
            False,
            "passed",
            "A",
            None,
            id="valid-grade-snapshot-clears-blank-mark-counterpart",
        ),
        pytest.param(
            "",
            "70",
            False,
            "passed",
            "",
            70.0,
            id="valid-mark-snapshot-clears-blank-grade-counterpart",
        ),
    ],
)
def test_scrape_course_status_transition_and_result_preservation(
    target_letter: str,
    target_mark: str,
    register_target: bool,
    expected_status: str,
    expected_grade: str,
    expected_mark: float | None,
) -> None:
    existing = _seed_existing_course()

    result = _process_student(
        str(_STUDENT_ID),
        _study_html(target_letter=target_letter, target_mark=target_mark),
        _timetable_html(register_target=register_target),
        program="AI",
        section="M",
    )

    assert result["ok"] is True
    existing.refresh_from_db()
    assert existing.status == expected_status
    assert existing.grade == expected_grade
    assert existing.mark == expected_mark
    assert existing.actual_term == "1447/2"


@pytest.mark.parametrize(
    ("raw_grade", "normalized_grade", "expected_outcome"),
    [
        pytest.param("NP", "NP", "passed", id="official-np-pass"),
        pytest.param("ند", "ند", "passed", id="official-arabic-np-pass"),
        pytest.param("F", "F", "failed", id="official-f-fail"),
        pytest.param("NF", "NF", "failed", id="official-nf-fail"),
        pytest.param("DN", "DN", "failed", id="official-dn-fail"),
        pytest.param("هـ", "ه", "failed", id="official-arabic-f-fail"),
        pytest.param("هد", "هد", "failed", id="official-arabic-nf-fail"),
        pytest.param("ح", "ح", "failed", id="official-arabic-dn-fail"),
        pytest.param("E", "E", None, id="official-e-non-outcome"),
        pytest.param("عف", "عف", None, id="official-arabic-e-non-outcome"),
        pytest.param("IP", "IP", None, id="official-ip-non-outcome"),
        pytest.param("م", "م", None, id="official-arabic-ip-non-outcome"),
        pytest.param("IC", "IC", None, id="official-ic-non-outcome"),
        pytest.param("ل", "ل", None, id="official-arabic-ic-non-outcome"),
        pytest.param("W", "W", None, id="official-w-non-outcome"),
        pytest.param("ع", "ع", None, id="official-arabic-w-non-outcome"),
    ],
)
def test_official_portal_grade_symbols_are_recognized(
    raw_grade: str,
    normalized_grade: str,
    expected_outcome: str | None,
) -> None:
    result = parse_course_result({"letter": raw_grade, "marks": ""})

    assert result == {
        "outcome": expected_outcome,
        "grade": normalized_grade,
        "mark": None,
        "has_snapshot": True,
    }


@pytest.mark.parametrize(
    ("target_letter", "target_mark"),
    [
        pytest.param("NOT-A-GRADE", "", id="unknown-nonblank-grade"),
        pytest.param("", "not-a-mark", id="unknown-nonblank-mark"),
    ],
)
def test_unknown_result_value_is_rejected_before_any_database_write(
    target_letter: str,
    target_mark: str,
) -> None:
    _seed_existing_course()
    before = _snapshot_state()

    with CaptureQueriesContext(connection) as queries:
        with pytest.raises(ValueError):
            _process_student(
                str(_STUDENT_ID),
                _study_html(target_letter=target_letter, target_mark=target_mark),
                _timetable_html(register_target=False),
                program="DS",
                section="F",
            )

    assert _mutation_queries(queries) == []
    assert _snapshot_state() == before


@pytest.mark.parametrize(
    ("settled_status", "register_target", "expected_status"),
    [
        pytest.param("passed", False, "passed", id="passed-remains-settled"),
        pytest.param("passed", True, "passed", id="passed-retake-remains-settled"),
        pytest.param("failed", False, "failed", id="failed-remains-settled"),
        pytest.param("failed", True, "studying", id="failed-current-retake-is-studying"),
    ],
)
def test_blank_later_result_preserves_settled_status_and_metadata(
    settled_status: str,
    register_target: bool,
    expected_status: str,
) -> None:
    existing_grade = "B" if settled_status == "passed" else "F"
    existing_mark = 80.0 if settled_status == "passed" else 50.0
    existing = _seed_existing_course(
        status=settled_status,
        grade=existing_grade,
        mark=existing_mark,
    )

    _process_student(
        str(_STUDENT_ID),
        _study_html(target_letter="", target_mark=""),
        _timetable_html(register_target=register_target),
        program="AI",
        section="M",
    )

    existing.refresh_from_db()
    assert existing.status == expected_status
    assert existing.grade == existing_grade
    assert existing.mark == existing_mark
    assert existing.actual_term == "1447/2"


def test_external_failed_course_retake_preserves_metadata_without_duplicate() -> None:
    existing = _seed_existing_course(
        course_code="EX999",
        status="failed",
        grade="F",
        mark=45.0,
        is_external=True,
    )

    result = _process_student(
        str(_STUDENT_ID),
        _study_html(target_letter="", target_mark=""),
        _timetable_html(register_target=False, external=True),
        program="AI",
        section="M",
    )

    assert result["ok"] is True
    existing.refresh_from_db()
    assert existing.status == "studying"
    assert existing.grade == "F"
    assert existing.mark == 45.0
    assert existing.actual_term == "1447/2"
    assert (
        StudentCourse.objects.filter(
            student_id=_STUDENT_ID,
            course__course_code="EX999",
        ).count()
        == 1
    )


def test_failed_result_survives_retaking_then_disappearing_from_current_timetable() -> None:
    existing = _seed_existing_course(
        status="failed",
        grade="F",
        mark=45.0,
    )

    _process_student(
        str(_STUDENT_ID),
        _study_html(target_letter="", target_mark=""),
        _timetable_html(register_target=True),
        program="AI",
        section="M",
    )
    existing.refresh_from_db()
    assert existing.status == "studying"

    _process_student(
        str(_STUDENT_ID),
        _study_html(target_letter="", target_mark=""),
        _timetable_html(register_target=False),
        program="AI",
        section="M",
    )

    existing.refresh_from_db()
    assert existing.status == "failed"
    assert existing.grade == "F"
    assert existing.mark == 45.0
    assert existing.actual_term == "1447/2"


def test_failed_result_is_available_to_the_student_advisor_context() -> None:
    _seed_existing_course()
    _process_student(
        str(_STUDENT_ID),
        _study_html(target_letter="F", target_mark="55"),
        _timetable_html(register_target=False),
        program="AI",
        section="M",
    )

    context = build_verified_student_context(student_id=_STUDENT_ID)
    evidence = context["course_evidence"]

    assert _TARGET_CODE in evidence["failed"]
    assert {
        "course_code": _TARGET_CODE,
        "course_name": "Transition Course 101",
        "grade": "F",
        "mark": 55.0,
    } in evidence["failed_results"]


def test_failed_result_remains_in_advisor_context_while_course_is_being_retaken() -> None:
    existing = _seed_existing_course(
        status="failed",
        grade="F",
        mark=45.0,
    )
    _process_student(
        str(_STUDENT_ID),
        _study_html(target_letter="", target_mark=""),
        _timetable_html(register_target=True),
        program="AI",
        section="M",
    )

    existing.refresh_from_db()
    assert existing.status == "studying"

    context = build_verified_student_context(student_id=_STUDENT_ID)
    evidence = context["course_evidence"]
    assert _TARGET_CODE in evidence["studying"]
    assert _TARGET_CODE in evidence["failed"]
    assert {
        "course_code": _TARGET_CODE,
        "course_name": "Transition Course 101",
        "grade": "F",
        "mark": 45.0,
    } in evidence["failed_results"]
    assert any(
        registration["course_code"] == _TARGET_CODE
        for registration in evidence["current_term_registrations"]["registrations"]
    )

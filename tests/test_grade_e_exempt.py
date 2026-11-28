"""The registrar's exempt grade (E, and its Arabic form) settles a plan row as met (owner, 2026-11-28)."""

from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from core.services.course_classifier import classify_courses, parse_course_result

BEFORE = [("core", "0085_clean_course_title_number_debris")]
AFTER = [("core", "0086_grade_e_exempt_is_met")]


def _row(dept: str, number: str, letter: str, marks: str = "") -> dict:
    return {"dept": dept, "no": number, "letter": letter, "marks": marks}


def test_an_exempt_row_is_met_even_with_a_mark_beside_it() -> None:
    # Like a transfer, an exemption is a settlement, not a sitting: a stray mark cannot fail it.
    assert parse_course_result({"letter": "E", "marks": "12"})["outcome"] == "passed"
    assert parse_course_result({"letter": "\u0639\u0640\u0641", "marks": ""})["outcome"] == "passed"


def test_an_exempt_row_is_classified_passed_not_studying_or_not_taken() -> None:
    plan = [
        _row("ENG", "101", "E"),
        _row("ENG", "102", "\u0639\u0641"),
        _row("CS", "101", "W"),
        _row("CS", "102", ""),
    ]
    result = classify_courses(plan, timetable_courses={"ENG102", "CS102"})

    assert result["passed"] == ["ENG101", "ENG102"]
    assert result["studying"] == ["CS102"]
    assert result["not_taken"] == ["CS101"]


@pytest.mark.django_db(transaction=True)
def test_the_migration_settles_stored_exempt_rows_and_nothing_else() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    old = executor.loader.project_state(BEFORE).apps
    Student = old.get_model("core", "Student")
    Course = old.get_model("core", "Course")
    StudentCourse = old.get_model("core", "StudentCourse")
    student = Student.objects.create(student_id=4612612, program="CS2")
    rows = {
        "ENG101": ("not_taken", "E", None),
        "ENG102": ("not_taken", "\u0639\u0641", None),
        "ENGL103": ("passed", "E", None),
        "CS101": ("not_taken", "W", None),
        "CS102": ("passed", "A", 91.0),
        "CS103": ("not_taken", "", None),
    }
    for code, (status, grade, mark) in rows.items():
        course = Course.objects.create(course_code=code)
        StudentCourse.objects.create(
            student=student, course=course, status=status, grade=grade, mark=mark
        )

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(AFTER)
    new = executor.loader.project_state(AFTER).apps.get_model("core", "StudentCourse")

    after = {
        r.course.course_code: (r.status, r.grade, r.mark)
        for r in new.objects.select_related("course").all()
    }
    assert after == {
        "ENG101": ("passed", "E", None),
        "ENG102": ("passed", "\u0639\u0641", None),
        "ENGL103": ("passed", "E", None),
        "CS101": ("not_taken", "W", None),
        "CS102": ("passed", "A", 91.0),
        "CS103": ("not_taken", "", None),
    }

    # Leave the database at the latest migration for the tests that follow.
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())

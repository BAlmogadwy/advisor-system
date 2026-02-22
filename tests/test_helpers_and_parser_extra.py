import pytest

from core.models import Prerequisite, Student
from core.services.student_helpers import (
    get_all_programs,
    get_prerequisites_visualizer_style,
    normalize_code,
)
from core.services.student_parser import parse_study_plan, parse_timetable

pytestmark = pytest.mark.django_db


def _setup_small_data() -> None:
    Student.objects.create(student_id=1, program="CS", section="M")
    Student.objects.create(student_id=2, program="SE", section="F")
    Prerequisite.objects.create(
        program="CS",
        course_code="CS 300",
        prerequisite_course_code="CS 201, MATH 101",
    )


def test_visualizer_prereqs_and_programs() -> None:
    _setup_small_data()

    assert {"CS", "SE"}.issubset(set(get_all_programs()))
    exact = get_prerequisites_visualizer_style("CS 300", "CS")
    assert exact == ["CS 201", "MATH 101"]
    assert normalize_code("CS\u00A0300") == "CS300"


def test_parser_handles_logout_and_table_parsing() -> None:
    assert parse_study_plan("teachers_login.jsp") == []
    assert parse_timetable("services4GraduatedStudent.do") == set()

    html = """
    <table dir='rtl'>
      <tr><th>SECOND LEVEL</th></tr>
      <tr><td>A</td><td>90</td><td>3</td><td>101</td><td>CS</td><td>Intro</td></tr>
      <tr><td>F</td><td></td><td>2</td><td>102</td><td>CS</td><td>Algo</td></tr>
    </table>
    """
    parsed = parse_study_plan(html)
    assert len(parsed) == 2
    assert parsed[0]["programme_term"] == 2

    tt_html = """
    <table class='forumline'>
      <tr><th>المادة</th></tr>
      <tr><td>x</td><td>x</td><td>CS</td><td>101</td></tr>
      <tr><td>x</td><td>x</td><td>102</td><td>CS</td></tr>
    </table>
    """
    tt = parse_timetable(tt_html, verbose=False)
    assert "CS101" in tt
    assert "CS102" in tt

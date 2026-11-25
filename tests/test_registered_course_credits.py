"""A registered course outside the student's plan carries its real credit hours.

``get_student_term_baseline`` read a row's hours from the programme plan alone, so a
course taken against an elective slot, or a general-studies elective, came back with
``credits: 0``. Every screen that sums the rows then undercounted the student's load
(on the local data of 2026-11-25: 2,176 of 3,926 students, 6,720 hours in all).

The reader now follows the graduation forecast's order: the plan, then the
programme's elective catalogue, then the course table. A course is CODE::NAME, so a
figure is never borrowed from another course that only shares the code.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from core.models import (
    Course,
    ElectiveCourse,
    ProgrammeRequirement,
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
)
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups
from core.services.student_graduation import REGISTERED_TIMETABLE, build_graduation_report
from core.services.student_sections import (
    get_student_term_baseline,
    get_student_term_registration_summary,
    programme_course_credits,
)
from core.services.timetable_snapshots import Snapshot

pytestmark = pytest.mark.django_db

PROG = "RCP"
YEAR, TERM = "1448", "1"
SID = 4_690_101
REGISTRAR = "scraper_timetable"


def _student(sid: int = SID, program: str = PROG) -> int:
    Student.objects.create(
        student_id=sid,
        registration_no=str(sid),
        name="Sample Student",
        program=program,
        section="M",
        status="ACTIVE",
    )
    return sid


def _plan(code: str, name: str, hours: int | None, program: str = PROG) -> None:
    ProgrammeRequirement.objects.create(
        program=program,
        course_code=code,
        course_name=name,
        programme_term=1,
        credit_hours=hours,
        type="Mandatory",
    )


def _section(key: str, name: str = "", label: str = "M1") -> TermSection:
    section = TermSection.objects.create(
        course_key=key,
        course_code=key.partition("::")[0],
        course_number="",
        course_name=name,
        section=label,
    )
    # Two meetings: the reader returns one row per meeting, each with the hours.
    for day in ("SUN", "TUE"):
        TermSectionMeeting.objects.create(
            term_section=section, day=day, start_time="09:00", end_time="10:15"
        )
    return section


def _register(section: TermSection, sid: int = SID, source: str = REGISTRAR) -> None:
    StudentTermSection.objects.create(
        student_id=sid, academic_year=YEAR, term=TERM, term_section=section, source=source
    )


def _credits(sid: int = SID, snapshot: Snapshot = Snapshot.REGISTERED) -> dict[str, int]:
    rows = get_student_term_baseline(sid, YEAR, TERM, snapshot=snapshot)
    by_key: dict[str, set[int]] = {}
    for row in rows:
        hours = row["credits"]
        assert isinstance(hours, int), row
        by_key.setdefault(str(row["course_key"]), set()).add(hours)
    # Every meeting row of one course says the same hours.
    assert all(len(values) == 1 for values in by_key.values()), by_key
    return {key: values.pop() for key, values in by_key.items()}


def test_a_course_in_the_plan_keeps_the_plans_hours() -> None:
    """The plan answers first, whatever the two catalogues say about the code."""
    _student()
    _plan("RC201", "Data Structures", 3)
    Course.objects.create(course_code="RC201", description="Data Structures", credit_hours=4)
    ElectiveCourse.objects.create(
        programme=PROG, course_code="RC201", course_name="Data Structures", credit_hours=2
    )
    _register(_section("RC201", "Data Structures"))

    assert _credits() == {"RC201": 3}


def test_a_plan_course_with_no_hours_recorded_stays_zero() -> None:
    """In the plan is in the plan: the forecast does not look past it either."""
    _student()
    _plan("RC090", "Orientation", None)
    Course.objects.create(course_code="RC090", description="Orientation", credit_hours=1)
    _register(_section("RC090", "Orientation"))

    assert _credits() == {"RC090": 0}


def test_an_elective_outside_the_plan_takes_the_programmes_elective_hours() -> None:
    """RC463 fills the slot RC1. The plan names the slot, never the course."""
    _student()
    _plan("RC1", "Program Elective I", 3)
    ElectiveCourse.objects.create(
        programme=PROG, course_code="RC463", course_name="Data Mining", credit_hours=3
    )
    # Neither of these may answer for this student: another programme's catalogue,
    # and the course table, which comes after the programme's own catalogue.
    ElectiveCourse.objects.create(
        programme="OTHER", course_code="RC463", course_name="Data Mining", credit_hours=2
    )
    Course.objects.create(course_code="RC463", description="Data Mining", credit_hours=4)
    _register(_section("RC463", "Data Mining"))

    assert _credits() == {"RC463": 3}


def test_a_course_only_the_course_table_knows_takes_its_hours() -> None:
    """A general-studies elective: in no plan and in no programme's catalogue."""
    _student()
    _plan("RC201", "Data Structures", 3)
    Course.objects.create(course_code="GS303", description="Communication Skills", credit_hours=2)
    ElectiveCourse.objects.create(
        programme="OTHER", course_code="GS303", course_name="Communication Skills", credit_hours=3
    )
    _register(_section("GS303", "Communication Skills"))

    assert _credits() == {"GS303": 2}


def test_a_course_no_source_knows_stays_zero() -> None:
    _student()
    _plan("RC201", "Data Structures", 3)
    # A row that records no hours is not a source either.
    Course.objects.create(course_code="RC777", description="Elsewhere", credit_hours=0)
    _register(_section("RC777", "Elsewhere"))
    _register(_section("RC999", "Nowhere"))

    assert _credits() == {"RC777": 0, "RC999": 0}


def test_two_courses_that_share_a_code_keep_their_own_hours() -> None:
    """CS112 is Programming I in one plan and Programming II in another.

    This student's plan holds Programming I (4 hours); the course table's single
    CS112 row describes Programming II (3 hours). Each section takes the hours of
    the course it names, and a third course nobody describes takes neither.
    """
    _student()
    _plan("CS112", "Programming I", 4)
    Course.objects.create(course_code="CS112", description="Programming II", credit_hours=3)
    _register(_section("CS112::PROGRAMMING_I", "Programming I", "M1"))
    _register(_section("CS112::PROGRAMMING_II", "Programming II", "M2"))
    _register(_section("CS112::PROGRAMMING_III", "Programming III", "M3"))

    assert _credits() == {
        "CS112::PROGRAMMING_I": 4,
        "CS112::PROGRAMMING_II": 3,
        "CS112::PROGRAMMING_III": 0,
    }


def test_a_shared_code_is_read_from_each_students_own_plan() -> None:
    """PHYS103 is 4 hours in one plan and 3 in another (true of the real plans).

    One section, registered by a student of each programme. The course table can
    describe only one of the two courses, so it must not answer for either.
    """
    _student(SID, "RCA")
    _student(SID + 1, "RCB")
    _plan("PHYS103", "Physics I", 4, program="RCA")
    _plan("PHYS103", "Physics", 3, program="RCB")
    Course.objects.create(course_code="PHYS103", description="Physics I", credit_hours=4)
    section = _section("PHYS103", "Physics")
    _register(section, SID)
    _register(section, SID + 1)

    assert _credits(SID) == {"PHYS103": 4}
    assert _credits(SID + 1) == {"PHYS103": 3}


def test_an_expected_plan_row_carries_the_same_hours() -> None:
    """The hours belong to the course, not to who recorded the section."""
    _student()
    _plan("RC1", "Program Elective I", 3)
    ElectiveCourse.objects.create(
        programme=PROG, course_code="RC463", course_name="Data Mining", credit_hours=3
    )
    _register(_section("RC463", "Data Mining"), source="registration_plan_1448_t1")

    assert _credits(snapshot=Snapshot.EXPECTED) == {"RC463": 3}
    assert _credits(snapshot=Snapshot.REGISTERED) == {}


def test_a_student_with_no_programme_still_gets_the_course_tables_hours() -> None:
    _student(program="")
    Course.objects.create(course_code="GS303", description="Communication Skills", credit_hours=2)
    _register(_section("GS303", "Communication Skills"))

    assert _credits() == {"GS303": 2}


def _seven_registered_courses() -> None:
    """Five plan courses (13 hours) and two outside the plan (3 and 2): 18 in all."""
    _student()
    for code, hours in [("RC201", 3), ("RC202", 3), ("RC203", 3), ("RC204", 2), ("RC205", 2)]:
        _plan(code, f"Course {code}", hours)
        _register(_section(code, f"Course {code}"))
    _plan("RC1", "Program Elective I", 3)
    ElectiveCourse.objects.create(
        programme=PROG, course_code="RC463", course_name="Data Mining", credit_hours=3
    )
    Course.objects.create(course_code="GS303", description="Communication Skills", credit_hours=2)
    _register(_section("RC463", "Data Mining"))
    _register(_section("GS303", "Communication Skills"))


def test_the_term_summary_counts_courses_outside_the_plan() -> None:
    """The case this was found on: seven courses shown as 13 hours, really 18."""
    _seven_registered_courses()

    summary = get_student_term_registration_summary(SID, YEAR, TERM)

    assert (summary["course_count"], summary["value"]) == (7, 18)


def test_the_staff_planner_shows_the_full_registered_load() -> None:
    """The planner page and the new board both sum the rows this endpoint sends."""
    _seven_registered_courses()
    ensure_role_groups()
    user = User.objects.create(username="registered-credits")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client(SERVER_NAME="localhost")
    client.force_login(user)

    response = client.post(
        "/ops/planner/context/",
        data={"student_id": str(SID), "academic_year": YEAR, "term": TERM},
        content_type="application/json",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["baseline_totals"] == {"courses": 7, "credits": 18}
    by_course = {row["course_key"]: row["credits"] for row in payload["baseline"]}
    assert (by_course["RC463"], by_course["GS303"]) == (3, 2)


def test_the_graduation_report_starts_from_the_full_registered_load() -> None:
    """The adviser's graduation tab reads the same rows, and showed 13 here too."""
    _seven_registered_courses()

    report = build_graduation_report(
        SID, int(YEAR), int(TERM), planning_baseline_kind=REGISTERED_TIMETABLE
    )

    assert report["registered_credits_at_planning_baseline"] == 18
    hours = {
        course["code"]: course["credits"]
        for course in report["planning_baseline_courses_assumed_passed"]
    }
    assert (hours["RC463"], hours["GS303"]) == (3, 2)


def test_the_programme_is_matched_whatever_its_case() -> None:
    """As the plan lookup always was: a profile saying ``rcp`` is programme RCP."""
    _student(program="rcp")
    _plan("RC201", "Data Structures", 3)
    ElectiveCourse.objects.create(
        programme=PROG, course_code="RC463", course_name="Data Mining", credit_hours=3
    )
    _register(_section("RC201", "Data Structures"))
    _register(_section("RC463", "Data Mining"))

    assert _credits() == {"RC201": 3, "RC463": 3}


def test_a_term_all_in_the_plan_reads_neither_catalogue() -> None:
    """Most students need only the plan: the fallback costs them no query."""
    _student()
    _plan("RC201", "Data Structures", 3)
    _register(_section("RC201", "Data Structures"))

    with CaptureQueriesContext(connection) as queries:
        assert _credits() == {"RC201": 3}

    tables = " ".join(query["sql"] for query in queries)
    assert '"programme_requirements"' in tables
    assert '"elective_courses"' not in tables
    assert 'FROM "courses"' not in tables


def test_the_lookup_answers_for_every_key_it_is_asked() -> None:
    """Callers index the result by key: an unknown course is 0, not a missing key."""
    _plan("RC201", "Data Structures", 3)

    assert programme_course_credits(PROG, ["rc 201", "RC999", "", None]) == {
        "RC201": 3,
        "RC999": 0,
    }
    assert programme_course_credits(PROG, []) == {}

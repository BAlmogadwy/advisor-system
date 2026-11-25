"""A registered course outside the student's plan carries its real credit hours.

``get_student_term_baseline`` read a row's hours from the programme plan alone, so a
course taken against an elective slot, or a general-studies elective, came back with
``credits: 0``. Every screen that sums the rows then undercounted the student's load
(on the local data of 2026-11-25: 2,176 of 3,926 students, 6,720 hours in all).

The reader now follows the graduation forecast's order: the plan, then the
programme's elective catalogue, then the course table. A course is CODE::NAME, so a
figure is never borrowed from another course that only shares the code.

Those hours are the term's LOAD. Two things must not follow from them, and the
second half of this file holds both: a resolved elective slot must not be counted
beside the course that fills it, and the graduation forecast's hour ledger stays in
plan rows, so no forecast moves because a course outside the plan gained its hours.
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
    ElectiveTermMapping,
    Prerequisite,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
)
from core.services.planner_graduation import compare_planner_graduation
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups
from core.services.student_graduation import REGISTERED_TIMETABLE, build_graduation_report
from core.services.student_sections import (
    append_unmapped_studying_courses,
    get_student_term_baseline,
    get_student_term_registration_summary,
    programme_course_credits,
)
from core.services.timetable_snapshots import Snapshot

pytestmark = pytest.mark.django_db

PROG = "RCP"
YEAR, TERM = "1448", "1"
# The id's first two digits are the join year (1446), so 1448/1 is plan term 5.
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


def _plan(
    code: str,
    name: str,
    hours: int | None,
    program: str = PROG,
    *,
    term: int = 1,
    kind: str = "Mandatory",
) -> None:
    ProgrammeRequirement.objects.create(
        program=program,
        course_code=code,
        course_name=name,
        programme_term=term,
        credit_hours=hours,
        type=kind,
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


PLAN_TOTAL = 24


def _seven_registered_courses() -> None:
    """Five plan courses (13 hours) and two outside the plan (3 and 2): 18 in all.

    The plan is a zero-slack one, like the six real second-cohort plans. Still
    to take after this term are the two elective rows those two courses are taken
    against (3 and 2) and the co-op (6), which needs 18 hours: every hour that
    comes before it. The plan totals 24. All three sit at plan term 6, so only
    the hour gate decides when the co-op can be placed.
    """
    _student()
    for code, hours in [("RC201", 3), ("RC202", 3), ("RC203", 3), ("RC204", 2), ("RC205", 2)]:
        _plan(code, f"Course {code}", hours, term=5)
        _register(_section(code, f"Course {code}"))
    _plan("RC1", "Program Elective I", 3, term=6, kind="Program Elective")
    _plan("RFE1", "Free Elective I", 2, term=6, kind="Free Elective")
    _plan("RC490", "Cooperative Training", 6, term=6)
    Prerequisite.objects.create(
        program=PROG, course_code="RC490", prerequisite_course_code="18(HOURS)"
    )
    ElectiveCourse.objects.create(
        programme=PROG, course_code="RC463", course_name="Data Mining", credit_hours=3
    )
    Course.objects.create(course_code="GS303", description="Communication Skills", credit_hours=2)
    _register(_section("RC463", "Data Mining"))
    _register(_section("GS303", "Communication Skills"))


def _report() -> dict:
    return build_graduation_report(
        SID, int(YEAR), int(TERM), planning_baseline_kind=REGISTERED_TIMETABLE
    )


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

    report = _report()

    assert report["registered_credits_at_planning_baseline"] == 18
    hours = {
        course["code"]: course["credits"]
        for course in report["planning_baseline_courses_assumed_passed"]
    }
    assert (hours["RC463"], hours["GS303"]) == (3, 2)


# ---------------------------------------------------------------------------
# The forecast's hour ledger stays in plan rows
# ---------------------------------------------------------------------------


def test_the_forecast_ledger_counts_the_term_in_plan_rows() -> None:
    """Load 18, ledger 13: the two courses outside the plan are not in it.

    Their hours arrive through the elective rows the registrar posts them under,
    which the scenario still schedules. So every plan credit is counted once, the
    co-op waits for those rows, and the scenario ends on the plan total. With the
    5 hours in the ledger as well, the gate was already met: the co-op went into
    the first term beside the rows and the scenario ended on 29.

    (The co-op lands in the third term, not the second: the recommender places a
    course only in a term of its own parity.)
    """
    _seven_registered_courses()

    report = _report()

    assert report["registered_credits_at_planning_baseline"] == 18
    assert report["scenario_credit_seed_registrar"] == 13
    assert report["simulation_completed"] is True
    planned = {
        code: term["sequence"] for term in report["term_plan"] for code in term["course_codes"]
    }
    assert planned == {"RC1": 1, "RFE1": 1, "RC490": 3}
    end = report["scenario_credit_seed"] + sum(term["credits"] for term in report["term_plan"])
    assert end == PLAN_TOTAL


def test_the_forecast_does_not_move_with_the_hours_of_a_course_outside_the_plan() -> None:
    """The same student, before anything knew those two courses' hours."""
    _seven_registered_courses()
    with_hours = _report()
    ElectiveCourse.objects.update(credit_hours=0)
    Course.objects.update(credit_hours=0)

    without_hours = _report()

    assert without_hours["registered_credits_at_planning_baseline"] == 13
    for key in (
        "scenario_credit_seed",
        "estimated_additional_terms",
        "term_plan",
        "hour_gates",
        "unresolved_requirements",
    ):
        assert with_hours[key] == without_hours[key], key


def test_the_planner_forecast_keeps_the_same_ledger() -> None:
    """It names each course's hours itself, and must not seed the ledger with them."""
    _seven_registered_courses()

    planner = compare_planner_graduation(student_id=SID, year=int(YEAR), term=int(TERM), options=[])

    assert planner["baseline"]["status"] == "complete"
    # With the hours it names in the ledger it said 2: the co-op a year early.
    assert (
        planner["baseline"]["terms_including_planning_term"]
        == _report()["estimated_terms_including_planning_baseline"]
        == 4
    )


# ---------------------------------------------------------------------------
# A resolved elective slot is not a second course
# ---------------------------------------------------------------------------


def _resolved_slot(*, programme: str = PROG, mapped_under: str = PROG) -> ElectiveCourse:
    """The mapping names RC463 for the slot RC1, and the resolver marked RC1 studying."""
    _student(program=programme)
    _plan("RC201", "Data Structures", 3, programme, term=5)
    _plan("RC1", "Program Elective I", 3, programme, term=5, kind="Program Elective")
    elective = ElectiveCourse.objects.create(
        programme=mapped_under, course_code="RC463", course_name="Data Mining", credit_hours=3
    )
    ElectiveTermMapping.objects.create(
        academic_year=YEAR,
        term=int(TERM),
        programme=mapped_under,
        placeholder_code="RC1",
        elective=elective,
    )
    slot = Course.objects.create(
        course_code="RC1", description="Program Elective I", credit_hours=3
    )
    StudentCourse.objects.create(student_id=SID, course=slot, status="studying")
    _register(_section("RC201", "Data Structures"))
    return elective


def _load() -> dict[str, int]:
    rows = append_unmapped_studying_courses(
        SID, get_student_term_baseline(SID, YEAR, TERM, snapshot=Snapshot.REGISTERED)
    )
    load: dict[str, int] = {}
    for row in rows:
        hours = row["credits"]
        assert isinstance(hours, int), row
        load.setdefault(str(row["course_key"]), hours)
    return load


def test_a_resolved_slot_adds_no_hours_beside_the_course_that_fills_it() -> None:
    """One registration, 3 hours. It was 3 for the slot plus 3 for the course."""
    _resolved_slot()
    _register(_section("RC463", "Data Mining"))

    assert _load() == {"RC201": 3, "RC463": 3, "RC1": 0}


def test_a_resolved_slot_is_counted_once_when_its_course_has_no_section() -> None:
    """Both then come from the studying records; the course still carries the hours."""
    _resolved_slot()
    course = Course.objects.create(course_code="RC463", description="Data Mining", credit_hours=3)
    StudentCourse.objects.create(student_id=SID, course=course, status="studying")

    assert _load() == {"RC201": 3, "RC1": 0, "RC463": 3}


def test_a_studying_slot_keeps_its_hours_when_nothing_else_carries_them() -> None:
    """Its course is not in the timetable, or no source knows that course's hours."""
    elective = _resolved_slot()
    assert _load() == {"RC201": 3, "RC1": 3}

    elective.credit_hours = 0
    elective.save()
    _register(_section("RC463", "Data Mining"))
    assert _load() == {"RC201": 3, "RC463": 0, "RC1": 3}


def test_a_second_cohort_slot_is_read_through_its_base_programmes_mapping() -> None:
    """RCP2 uses the mappings filed under RCP, as the resolver reads them."""
    _resolved_slot(programme="RCP2", mapped_under="RCP")
    Course.objects.create(course_code="RC463", description="Data Mining", credit_hours=3)
    _register(_section("RC463", "Data Mining"))

    assert _load() == {"RC201": 3, "RC463": 3, "RC1": 0}


def test_a_course_with_a_named_key_still_carries_its_slot() -> None:
    _resolved_slot()
    _register(_section("RC463::DATA_MINING", "Data Mining"))

    assert _load() == {"RC201": 3, "RC463::DATA_MINING": 3, "RC1": 0}


def test_a_mapping_stored_in_another_spelling_or_term_still_names_the_filler() -> None:
    """The studying mark carries no term; these text columns hold spelling variants."""
    elective = _resolved_slot()
    ElectiveTermMapping.objects.all().delete()
    ElectiveTermMapping.objects.create(
        academic_year="1447", term=2, programme="rcp", placeholder_code=" rc1", elective=elective
    )
    _register(_section("RC463", "Data Mining"))

    assert _load() == {"RC201": 3, "RC463": 3, "RC1": 0}


def test_a_course_the_plan_lists_is_never_a_slots_filler() -> None:
    """A mapping that names a plan course proves nothing: that course is its own row."""
    _resolved_slot()
    plan_course = ElectiveCourse.objects.create(
        programme=PROG, course_code="RC201", course_name="Data Structures", credit_hours=3
    )
    ElectiveTermMapping.objects.all().delete()
    ElectiveTermMapping.objects.create(
        academic_year=YEAR,
        term=int(TERM),
        programme=PROG,
        placeholder_code="RC1",
        elective=plan_course,
    )

    assert _load() == {"RC201": 3, "RC1": 3}


def test_one_course_carries_one_slot() -> None:
    """Two studying slots name the same course: only one of them is that course."""
    elective = _resolved_slot()
    _plan("RC2", "Program Elective II", 3, term=5, kind="Program Elective")
    ElectiveTermMapping.objects.create(
        academic_year=YEAR,
        term=int(TERM),
        programme=PROG,
        placeholder_code="RC2",
        elective=elective,
    )
    second = Course.objects.create(
        course_code="RC2", description="Program Elective II", credit_hours=3
    )
    StudentCourse.objects.create(student_id=SID, course=second, status="studying")
    _register(_section("RC463", "Data Mining"))

    assert _load() == {"RC201": 3, "RC463": 3, "RC1": 0, "RC2": 3}


def test_an_ordinary_studying_course_without_a_section_is_still_added_with_its_hours() -> None:
    _student()
    _plan("RC201", "Data Structures", 3)
    _plan("RC202", "Algorithms", 4)
    course = Course.objects.create(course_code="RC202", description="Algorithms", credit_hours=4)
    StudentCourse.objects.create(student_id=SID, course=course, status="studying")
    _register(_section("RC201", "Data Structures"))

    assert _load() == {"RC201": 3, "RC202": 4}


def test_a_resolved_slot_is_once_in_the_load_and_once_in_the_ledger() -> None:
    """The planner and the graduation report both showed 9 hours for these 6."""
    _resolved_slot()
    _register(_section("RC463", "Data Mining"))
    ensure_role_groups()
    user = User.objects.create(username="resolved-slot")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client(SERVER_NAME="localhost")
    client.force_login(user)

    payload = client.post(
        "/ops/planner/context/",
        data={"student_id": str(SID), "academic_year": YEAR, "term": TERM},
        content_type="application/json",
    ).json()
    report = _report()

    assert payload["baseline_totals"]["credits"] == 6
    assert report["registered_credits_at_planning_baseline"] == 6
    # The slot is a plan row, so the ledger counts it at the plan's hours
    # (RC201 3 + RC1 3), whatever the load put on the slot's own row.
    assert report["scenario_credit_seed_registrar"] == 6
    assert report["term_plan"] == []


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

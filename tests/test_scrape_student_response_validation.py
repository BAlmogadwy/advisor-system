from __future__ import annotations

import pytest
from django.db import connection, models
from django.test.utils import CaptureQueriesContext

from core.management.commands.scrape_students import _process_student
from core.models import (
    Course,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
    TimetableScenario,
)
from core.services.student_sections import replace_student_term_sections

pytestmark = pytest.mark.django_db

_STUDENT_ID = 990_101


def _valid_study_html(*, include_profile: bool = True) -> str:
    profile = ""
    if include_profile:
        profile = """
        <table class="forumline" dir="ltr">
          <tr><th>Registeration No:</th><td>990101</td></tr>
          <tr><th>Student Name</th><td>Fresh Student Name</td></tr>
          <tr><th>Nationality</th><td>Saudi</td></tr>
          <tr><th>Student Status</th><td>Active</td></tr>
          <tr><th>T.U.Registered</th><td>90</td></tr>
          <tr><th>T.U.Earned</th><td>87</td></tr>
          <tr><th>G.P.A</th><td>3.75</td></tr>
        </table>
        """

    level_tables: list[str] = []
    for level, start in (("FIRST", 101), ("SECOND", 111), ("THIRD", 121)):
        rows: list[str] = []
        for number in range(start, start + 10):
            # VX101 is the sole in-progress course represented in the timetable.
            letter = "" if number == 101 else "A"
            marks = "" if number == 101 else "90"
            rows.append(
                "<tr>"
                f"<td>{letter}</td><td>{marks}</td><td>3</td>"
                f"<td>{number}</td><td>VX</td><td>Validation Course {number}</td>"
                "</tr>"
            )
        level_tables.append(
            f'<table dir="rtl"><tr><th>{level} LEVEL</th></tr>{"".join(rows)}</table>'
        )

    return f"<html><body>{profile}{''.join(level_tables)}</body></html>"


def _valid_timetable_html(
    *,
    include_term: bool = True,
    include_course: bool = True,
    student_id: int = _STUDENT_ID,
) -> str:
    term = ""
    if include_term:
        term = "<div>العام الدراسي : 1448 الفصل الدراسي : الأول</div>"

    course_row = ""
    if include_course:
        course_row = """
        <tr>
          <td>1</td><td>Validation Course 101</td><td>VX</td><td>101</td>
          <td>3</td><td>M7</td><td>09:00</td><td>10:15</td>
          <td><img src="mark.jpg"></td><td></td><td></td><td></td><td></td>
          <td>B1</td><td>F1</td><td>R101</td>
        </tr>
        """

    return f"""
    <html><body>
      {term}
      <table class="forumline">
        <tr><th>رقم الطالب</th><td>{student_id}</td></tr>
        <tr><th>مجموع الوحدات المسجلة</th><td>3</td></tr>
        <tr><th>المرشد الاكاديمي</th><td>New Advisor</td></tr>
      </table>
      <table class="forumline">
        <tr>
          <th>م</th><th>المادة</th><th>رمز</th><th>رقم</th><th>ساعات</th><th>شعبة</th>
          <th>من</th><th>إلى</th><th>أحد</th><th>اثنين</th><th>ثلاثاء</th>
          <th>أربعاء</th><th>خميس</th><th>مبنى</th><th>دور</th><th>قاعة</th>
        </tr>
        {course_row}
      </table>
    </body></html>
    """


def _confirmed_empty_timetable_html() -> str:
    return """
    <html><head><title>الجدول الدراسي لطالب</title></head>
    <body><table><tr><td>رقم الطالب به خطأ</td></tr></table></body></html>
    """


def _course_codes_without_structured_meetings_html() -> str:
    """A partial portal response that fools the lightweight course-code parser."""
    return f"""
    <html><body>
      <div>العام الدراسي : 1448 الفصل الدراسي : الأول</div>
      <table class="forumline">
        <tr><th>رقم الطالب</th><td>{_STUDENT_ID}</td></tr>
        <tr><th>مجموع الوحدات المسجلة</th><td>3</td></tr>
      </table>
      <table class="forumline">
        <tr><th>Course</th></tr>
        <tr><td>1</td><td>Validation Course 101</td><td>VX</td><td>101</td></tr>
      </table>
    </body></html>
    """


def _seed_last_known_state() -> None:
    student = Student.objects.create(
        student_id=_STUDENT_ID,
        registration_no=str(_STUDENT_ID),
        name="Last Known Name",
        nationality="Last Known Nationality",
        status="Active",
        gpa=4.2,
        total_registered_credits=72,
        total_earned_credits=66,
        current_registered_credits=6,
        program="AI",
        section="M",
        advisor_id="7001",
    )
    old_course = Course.objects.create(
        course_code="ZZ999",
        department="ZZ",
        description="Last Known Course",
        credit_hours=3,
    )
    StudentCourse.objects.create(
        student=student,
        course=old_course,
        programme_term=8,
        status="passed",
        grade="A+",
        mark=98,
        actual_term="1447/2",
    )
    old_section = TermSection.objects.create(
        source_tag="department",
        course_name="Last Known Course",
        available_capacity=25,
        registered_count=20,
        course_code="ZZ",
        course_number="999",
        course_key="ZZ999",
        section="M1",
        source_file="last-known.csv",
        created_at="before",
        updated_at="before",
    )
    TermSectionMeeting.objects.create(
        term_section=old_section,
        day="MON",
        start_time="13:00",
        end_time="14:15",
        building="B0",
        floor_wing="F0",
        room="R0",
        instructor="Dr Existing",
        created_at="before",
        updated_at="before",
    )
    TermSectionProgram.objects.create(
        term_section=old_section,
        program="AI",
        assignment_source="imported",
    )
    StudentTermSection.objects.create(
        student_id=student.student_id,
        academic_year="1447",
        term="2",
        term_section=old_section,
        source="scraper_timetable",
        created_at="before",
        updated_at="before",
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
    "bad_timetable",
    [
        pytest.param("", id="empty-response"),
        pytest.param(
            _valid_timetable_html(include_course=False),
            id="term-metadata-without-course-rows",
        ),
        pytest.param(
            _valid_timetable_html(include_course=False).replace(
                "<tr><th>مجموع الوحدات المسجلة</th><td>3</td></tr>",
                "<tr><th>مجموع الوحدات المسجلة</th><td>0</td></tr>",
            ),
            id="displayed-zero-without-confirmed-empty-marker",
        ),
        pytest.param(
            _valid_timetable_html(include_term=False),
            id="course-rows-without-term-metadata",
        ),
        pytest.param(
            _course_codes_without_structured_meetings_html(),
            id="course-code-without-structured-meetings",
        ),
        pytest.param(
            _valid_timetable_html().replace(
                "<body>",
                '<body><a href="student_login.jsp">Login</a>',
                1,
            ),
            id="portal-login-response",
        ),
        pytest.param(
            _valid_timetable_html().replace(
                "<body>",
                '<body><a href="services4GraduatedStudent.do">Service</a>',
                1,
            ),
            id="portal-service-response",
        ),
        pytest.param(
            _valid_timetable_html().replace(
                "<body>",
                "<body><div>رقم الطالب به خطأ</div>",
                1,
            ),
            id="explicit-portal-error-response",
        ),
        pytest.param(
            "<html><head><title>Login</title></head><body>رقم الطالب به خطأ</body></html>",
            id="marker-on-non-timetable-page",
        ),
        pytest.param(
            _valid_timetable_html().replace(
                "<td>09:00</td><td>10:15</td>",
                "<td>not-a-time</td><td>10:15</td>",
            ),
            id="malformed-meeting-time",
        ),
        pytest.param(
            _valid_timetable_html().replace(
                "<td>09:00</td><td>10:15</td>",
                "<td>10:15</td><td>10:15</td>",
            ),
            id="meeting-start-is-not-before-end",
        ),
        pytest.param(
            _valid_timetable_html().replace(
                '<td><img src="mark.jpg"></td>',
                "<td></td>",
            ),
            id="meeting-without-supported-day",
        ),
    ],
)
def test_invalid_timetable_is_rejected_before_any_database_write(bad_timetable: str) -> None:
    _seed_last_known_state()
    before = _snapshot_state()

    with CaptureQueriesContext(connection) as queries:
        with pytest.raises(ValueError):
            _process_student(
                str(_STUDENT_ID),
                _valid_study_html(),
                bad_timetable,
                program="DS",
                section="F",
            )

    assert _mutation_queries(queries) == []
    assert _snapshot_state() == before


def test_missing_student_profile_is_rejected_before_any_database_write() -> None:
    _seed_last_known_state()
    before = _snapshot_state()

    with CaptureQueriesContext(connection) as queries:
        with pytest.raises(ValueError):
            _process_student(
                str(_STUDENT_ID),
                _valid_study_html(include_profile=False),
                _valid_timetable_html(),
                program="DS",
                section="F",
            )

    assert _mutation_queries(queries) == []
    assert _snapshot_state() == before


@pytest.mark.parametrize(
    ("study_html", "timetable_html"),
    [
        pytest.param(
            _valid_study_html().replace(
                "<tr><th>T.U.Registered</th><td>90</td></tr>",
                "<tr><th>T.U.Registered</th><td>not-a-number</td></tr>",
            ),
            _valid_timetable_html(),
            id="malformed-profile-total",
        ),
        pytest.param(
            _valid_study_html().replace(
                "<tr><th>T.U.Earned</th><td>87</td></tr>",
                "<tr><th>T.U.Earned</th><td>not-a-number</td></tr>",
            ),
            _valid_timetable_html(),
            id="malformed-earned-total",
        ),
        pytest.param(
            _valid_study_html(),
            _valid_timetable_html().replace(
                "<tr><th>مجموع الوحدات المسجلة</th><td>3</td></tr>",
                "<tr><th>مجموع الوحدات المسجلة</th><td>not-a-number</td></tr>",
            ),
            id="malformed-current-registered-credits",
        ),
        pytest.param(
            _valid_study_html().replace(
                "<tr><th>G.P.A</th><td>3.75</td></tr>",
                "<tr><th>G.P.A</th><td>6.25</td></tr>",
            ),
            _valid_timetable_html(),
            id="out-of-range-gpa",
        ),
        pytest.param(
            _valid_study_html().replace(
                "<tr><th>G.P.A</th><td>3.75</td></tr>",
                "",
            ),
            _valid_timetable_html(),
            id="missing-gpa-field",
        ),
        pytest.param(
            _valid_study_html(),
            _valid_timetable_html().replace(
                "<tr><th>مجموع الوحدات المسجلة</th><td>3</td></tr>",
                "<tr><th>مجموع الوحدات المسجلة</th><td>4</td></tr>",
            ),
            id="declared-credits-do-not-match-course-rows",
        ),
        pytest.param(
            _valid_study_html(),
            _valid_timetable_html(student_id=_STUDENT_ID + 1),
            id="timetable-belongs-to-a-different-student",
        ),
    ],
)
def test_malformed_numeric_metadata_is_rejected_before_any_database_write(
    study_html: str,
    timetable_html: str,
) -> None:
    _seed_last_known_state()
    before = _snapshot_state()

    with CaptureQueriesContext(connection) as queries:
        with pytest.raises(ValueError):
            _process_student(
                str(_STUDENT_ID),
                study_html,
                timetable_html,
                program="DS",
                section="F",
            )

    assert _mutation_queries(queries) == []
    assert _snapshot_state() == before


def test_study_plan_from_another_programme_is_rejected_before_database_write() -> None:
    _seed_last_known_state()
    ProgrammeRequirement.objects.bulk_create(
        [
            ProgrammeRequirement(
                program="DS",
                course_code=f"DS{number}",
                course_name=f"Configured DS Course {number}",
                programme_term=((number - 1) // 10) + 1,
                credit_hours=3,
            )
            for number in range(101, 131)
        ]
    )
    before = _snapshot_state()

    with CaptureQueriesContext(connection) as queries:
        with pytest.raises(ValueError, match="does not match CSV programme DS"):
            _process_student(
                str(_STUDENT_ID),
                _valid_study_html(),
                _valid_timetable_html(),
                program="DS",
                section="F",
            )

    assert _mutation_queries(queries) == []
    assert _snapshot_state() == before


def test_confirmed_empty_summer_timetable_is_a_valid_zero_course_snapshot() -> None:
    _seed_last_known_state()
    student = Student.objects.get(student_id=_STUDENT_ID)
    expected_section = TermSection.objects.create(
        course_code="FP",
        course_number="201",
        course_key="FP201",
        course_name="Expected next-term section",
        section="M2",
    )
    expected_link = StudentTermSection.objects.create(
        student_id=student.student_id,
        academic_year="1448",
        term="1",
        term_section=expected_section,
        source="registration_plan_1448_t1",
    )
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="Preserved planner scenario",
    )
    scenario_section = TermSection.objects.create(
        scenario=scenario,
        course_code="PL",
        course_number="101",
        course_key="PL101",
        course_name="Planner-only assignment",
        section="M9",
    )
    scenario_link = StudentTermSection.objects.create(
        student_id=student.student_id,
        academic_year="1448",
        term="1",
        term_section=scenario_section,
        source="scenario_assignment",
    )
    stale_external = Course.objects.create(
        course_code="EX777",
        department="EX",
        description="Old External Registration",
        credit_hours=3,
        is_external=True,
    )
    stale_row = StudentCourse.objects.create(
        student=student,
        course=stale_external,
        status="studying",
    )
    stale_regular = Course.objects.create(
        course_code="OT888",
        department="OT",
        description="Required by another programme",
        credit_hours=3,
        is_external=False,
    )
    stale_regular_row = StudentCourse.objects.create(
        student=student,
        course=stale_regular,
        status="studying",
    )

    result = _process_student(
        str(_STUDENT_ID),
        _valid_study_html(),
        _confirmed_empty_timetable_html(),
        program="AI",
        section="M",
    )

    assert result["ok"] is True
    assert result["schedule_state"] == "confirmed_empty_current_schedule"
    assert result["mapped_sections"] == 0
    assert result["deleted_student_section_links"] == 1

    student.refresh_from_db()
    assert student.current_registered_credits == 0
    assert not StudentTermSection.objects.filter(
        student_id=_STUDENT_ID,
        source="scraper_timetable",
        term_section__scenario__isnull=True,
    ).exists()
    assert StudentTermSection.objects.filter(pk=expected_link.pk).exists()
    assert StudentTermSection.objects.filter(pk=scenario_link.pk).exists()
    assert TermSection.objects.filter(course_key="ZZ999", section="M1").exists()
    assert (
        StudentCourse.objects.get(
            student_id=_STUDENT_ID,
            course__course_code="VX101",
        ).status
        == "not_taken"
    )
    stale_row.refresh_from_db()
    assert stale_row.status == "not_taken"
    stale_regular_row.refresh_from_db()
    assert stale_regular_row.status == "not_taken"


def test_confirmed_empty_explicit_term_replaces_matching_plan_only() -> None:
    _seed_last_known_state()
    current_plan_section = TermSection.objects.create(
        course_code="CP",
        course_number="201",
        course_key="CP201",
        course_name="Plan for the term now confirmed empty",
        section="M2",
    )
    current_plan_link = StudentTermSection.objects.create(
        student_id=_STUDENT_ID,
        academic_year="1448",
        term="1",
        term_section=current_plan_section,
        source="registration_plan_1448_t1",
    )
    future_plan_section = TermSection.objects.create(
        course_code="FP",
        course_number="301",
        course_key="FP301",
        course_name="Later expected plan",
        section="M3",
    )
    future_plan_link = StudentTermSection.objects.create(
        student_id=_STUDENT_ID,
        academic_year="1449",
        term="1",
        term_section=future_plan_section,
        source="registration_plan_1449_t1",
    )

    result = _process_student(
        str(_STUDENT_ID),
        _valid_study_html(),
        _confirmed_empty_timetable_html(),
        program="AI",
        section="M",
        empty_snapshot_year="1448",
        empty_snapshot_term="1",
    )

    assert result["academic_year"] == "1448"
    assert result["term"] == "1"
    assert result["deleted_student_section_links"] == 2
    assert not StudentTermSection.objects.filter(pk=current_plan_link.pk).exists()
    assert StudentTermSection.objects.filter(pk=future_plan_link.pk).exists()
    assert TermSection.objects.filter(pk=current_plan_section.pk).exists()


def test_null_earned_credit_sentinel_is_saved_as_zero_for_confirmed_student() -> None:
    result = _process_student(
        str(_STUDENT_ID),
        _valid_study_html().replace(
            "<tr><th>T.U.Earned</th><td>87</td></tr>",
            "<tr><th>T.U.Earned</th><td>null</td></tr>",
        ),
        _confirmed_empty_timetable_html(),
        program="AI",
        section="M",
    )

    assert result["schedule_state"] == "confirmed_empty_current_schedule"
    student = Student.objects.get(student_id=_STUDENT_ID)
    assert student.total_earned_credits == 0
    assert student.current_registered_credits == 0


def test_null_gpa_sentinel_is_saved_as_missing_for_confirmed_student() -> None:
    result = _process_student(
        str(_STUDENT_ID),
        _valid_study_html().replace(
            "<tr><th>G.P.A</th><td>3.75</td></tr>",
            "<tr><th>G.P.A</th><td>null</td></tr>",
        ),
        _confirmed_empty_timetable_html(),
        program="AI",
        section="M",
    )

    assert result["schedule_state"] == "confirmed_empty_current_schedule"
    student = Student.objects.get(student_id=_STUDENT_ID)
    assert student.gpa is None


def test_empty_timetable_marker_requires_matching_study_plan_identity() -> None:
    _seed_last_known_state()
    before = _snapshot_state()
    mismatched_study = _valid_study_html().replace(
        "<tr><th>Registeration No:</th><td>990101</td></tr>",
        "<tr><th>Registeration No:</th><td>990102</td></tr>",
    )

    with CaptureQueriesContext(connection) as queries:
        with pytest.raises(ValueError, match="Study plan belongs to student"):
            _process_student(
                str(_STUDENT_ID),
                mismatched_study,
                _confirmed_empty_timetable_html(),
                program="DS",
                section="F",
            )

    assert _mutation_queries(queries) == []
    assert _snapshot_state() == before


def test_complete_student_response_reaches_the_existing_persistence_path() -> None:
    _seed_last_known_state()
    future_section = TermSection.objects.create(
        course_code="FP",
        course_number="201",
        course_key="FP201",
        course_name="Expected future section",
        section="M2",
    )
    future_link = StudentTermSection.objects.create(
        student_id=_STUDENT_ID,
        academic_year="1449",
        term="1",
        term_section=future_section,
        source="registration_plan_1449_t1",
    )
    superseded_section = TermSection.objects.create(
        course_code="SP",
        course_number="201",
        course_key="SP201",
        course_name="Expected section for term now scraped",
        section="M3",
    )
    superseded_link = StudentTermSection.objects.create(
        student_id=_STUDENT_ID,
        academic_year="1448",
        term="1",
        term_section=superseded_section,
        source="registration_plan_1448_t1",
    )

    result = _process_student(
        str(_STUDENT_ID),
        _valid_study_html(),
        _valid_timetable_html(),
        program="AI",
        section="M",
    )

    assert result["ok"] is True
    assert result["schedule_state"] == "complete_schedule"
    assert result["academic_year"] == "1448"
    assert result["term"] == "1"

    student = Student.objects.get(student_id=_STUDENT_ID)
    assert student.name == "Fresh Student Name"
    assert student.gpa == 3.75
    assert student.current_registered_credits == 3

    current_course = StudentCourse.objects.get(
        student_id=_STUDENT_ID,
        course__course_code="VX101",
    )
    assert current_course.status == "studying"

    current_link = StudentTermSection.objects.get(
        student_id=_STUDENT_ID,
        academic_year="1448",
        term="1",
    )
    assert current_link.term_section.course_key == "VX101"
    assert current_link.term_section.section == "M7"
    assert not StudentTermSection.objects.filter(
        student_id=_STUDENT_ID,
        term_section__course_key="ZZ999",
    ).exists()
    assert StudentTermSection.objects.filter(pk=future_link.pk).exists()
    assert not StudentTermSection.objects.filter(pk=superseded_link.pk).exists()
    assert TermSectionMeeting.objects.filter(
        term_section=current_link.term_section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="R101",
    ).exists()
    assert TermSectionProgram.objects.filter(
        term_section=current_link.term_section,
        program="AI",
    ).exists()


def test_scrape_retimestamps_a_reused_global_section_into_the_new_snapshot() -> None:
    _seed_last_known_state()
    reused = TermSection.objects.create(
        course_code="VX",
        course_number="101",
        course_key="VX101",
        course_name="Validation Course 101",
        section="M7",
    )
    TermSectionMeeting.objects.create(
        term_section=reused,
        day="MON",
        start_time="13:00",
        end_time="14:15",
        building="OLD",
        floor_wing="OLD",
        room="OLD101",
        instructor="Old Instructor",
    )
    StudentTermSection.objects.create(
        student_id=_STUDENT_ID,
        academic_year="1447",
        term="2",
        term_section=reused,
        source="scraper_timetable",
    )

    _process_student(
        str(_STUDENT_ID),
        _valid_study_html(),
        _valid_timetable_html(),
        program="AI",
        section="M",
    )

    refreshed = StudentTermSection.objects.get(
        student_id=_STUDENT_ID,
        term_section=reused,
    )
    assert (refreshed.academic_year, refreshed.term) == ("1448", "1")
    assert not StudentTermSection.objects.filter(
        student_id=_STUDENT_ID,
        academic_year="1447",
        term="2",
        term_section__scenario__isnull=True,
    ).exists()
    assert list(
        TermSectionMeeting.objects.filter(term_section=reused).values_list(
            "day",
            "start_time",
            "end_time",
            "room",
        )
    ) == [("SUN", "09:00", "10:15", "R101")]


def test_real_scrape_coexists_with_future_plan_for_the_same_physical_section() -> None:
    """Term-scoped uniqueness keeps both meanings of one physical section.

    The former student/section-only constraint silently discarded the real row
    when a future expected plan already referenced that section. Both links are
    valid because their academic terms and provenance are different.
    """
    _seed_last_known_state()
    reused = TermSection.objects.create(
        course_code="VX",
        course_number="101",
        course_key="VX101",
        course_name="Validation Course 101",
        section="M7",
    )
    expected = StudentTermSection.objects.create(
        student_id=_STUDENT_ID,
        academic_year="1449",
        term="1",
        term_section=reused,
        source="registration_plan_1449_t1",
    )

    _process_student(
        str(_STUDENT_ID),
        _valid_study_html(),
        _valid_timetable_html(),
        program="AI",
        section="M",
    )

    assert StudentTermSection.objects.filter(pk=expected.pk).exists()
    current = StudentTermSection.objects.get(
        student_id=_STUDENT_ID,
        term_section=reused,
        academic_year="1448",
        term="1",
    )
    assert (current.academic_year, current.term, current.source) == (
        "1448",
        "1",
        "scraper_timetable",
    )


def test_term_scoped_planner_replace_preserves_another_term_snapshot() -> None:
    _seed_last_known_state()
    future = TermSection.objects.create(
        course_code="FP",
        course_number="201",
        course_key="FP201",
        course_name="Future planner section",
        section="M2",
    )

    replace_student_term_sections(
        _STUDENT_ID,
        "1449",
        "1",
        [future.pk],
        source="planner",
    )

    assert StudentTermSection.objects.filter(
        student_id=_STUDENT_ID,
        academic_year="1447",
        term="2",
        term_section__course_key="ZZ999",
    ).exists()
    assert StudentTermSection.objects.filter(
        student_id=_STUDENT_ID,
        academic_year="1449",
        term="1",
        term_section=future,
    ).exists()


def test_persistence_failure_rolls_back_the_complete_student_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_last_known_state()
    before = _snapshot_state()

    def fail_course_write(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated course persistence failure")

    monkeypatch.setattr(StudentCourse.objects, "update_or_create", fail_course_write)

    with pytest.raises(RuntimeError, match="simulated course persistence failure"):
        _process_student(
            str(_STUDENT_ID),
            _valid_study_html(),
            _valid_timetable_html(),
            program="AI",
            section="M",
        )

    assert _snapshot_state() == before

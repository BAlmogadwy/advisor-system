from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from core.models import (
    Course,
    ElectiveCourse,
    ElectiveTermMapping,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
)
from core.services.academic_state import (
    AcademicStateUnavailable,
    EvidenceKind,
    MetadataSource,
    TranscriptStatus,
    build_student_academic_state,
)

pytestmark = pytest.mark.django_db


def _student(student_id: int, *, programme: str = "AI", cohort: str = "M") -> Student:
    return Student.objects.create(
        student_id=student_id,
        registration_no=str(student_id),
        name=f"Student {student_id}",
        program=programme,
        section=cohort,
    )


def _course(code: str, *, credits: int = 3, name: str = "") -> Course:
    return Course.objects.create(
        course_code=code,
        credit_hours=credits,
        description=name or f"Global {code}",
    )


def _requirement(
    programme: str,
    code: str,
    *,
    credits: int = 3,
    name: str = "",
    requirement_type: str = "Mandatory",
    programme_term: int = 1,
) -> ProgrammeRequirement:
    return ProgrammeRequirement.objects.create(
        program=programme,
        course_code=code,
        course_name=name or f"{programme} {code}",
        credit_hours=credits,
        type=requirement_type,
        programme_term=programme_term,
    )


def _section(code: str, label: str, *, name: str = "") -> TermSection:
    return TermSection.objects.create(
        course_code=code,
        course_number=code,
        course_key=code,
        course_name=name or f"Section {code}",
        section=label,
        available_capacity=30,
        registered_count=22,
    )


def _link(
    student: Student,
    section: TermSection,
    *,
    source: str,
    year: str = "1448",
    term: str = "1",
) -> None:
    StudentTermSection.objects.create(
        student_id=student.student_id,
        academic_year=year,
        term=term,
        term_section=section,
        source=source,
    )


def test_registered_expected_and_studying_are_three_independent_facts() -> None:
    student = _student(991_001, programme="DS")
    course = _course("CS372", credits=2, name="Wrong global name")
    _requirement(
        "DS",
        "CS372",
        credits=4,
        name="Programme Database Systems",
        programme_term=6,
    )
    StudentCourse.objects.create(
        student=student,
        course=course,
        status="studying",
        actual_term="1448/1",
    )
    registered = _section("CS372", "M7", name="Registered section name")
    expected = _section("CS372", "M9", name="Expected section name")
    TermSectionMeeting.objects.create(
        term_section=registered,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="N7",
    )
    _link(student, registered, source="scraper_timetable")
    _link(student, expected, source="registration_plan_1448_t1")

    state = build_student_academic_state(student.student_id, "1448", 1)
    fact = state.course(" cs 372 ")

    assert fact is not None
    assert fact.directly_registered is True
    assert fact.studying_recorded is True
    assert [row.section for row in fact.registered_sections] == ["M7"]
    assert [row.section for row in fact.expected_sections] == ["M9"]
    assert fact.registered_sections[0].kind is EvidenceKind.REGISTERED
    assert fact.expected_sections[0].kind is EvidenceKind.EXPECTED_PLAN
    assert fact.registered_sections[0].available_capacity == 30
    assert fact.registered_sections[0].meetings[0].room == "N7"
    # The student's programme plan, not the global Course or TermSection row,
    # owns plan-specific names and credits.
    assert fact.metadata.course_name == "Programme Database Systems"
    assert fact.metadata.credit_hours == 4
    assert fact.metadata.programme_term == 6
    assert fact.metadata.source is MetadataSource.PROGRAMME_REQUIREMENT
    assert state.registered_course_codes == ("CS372",)
    assert state.expected_course_codes == ("CS372",)
    assert state.studying_course_codes == ("CS372",)


def test_expected_or_studying_alone_never_becomes_registration() -> None:
    student = _student(991_002, programme="DS")
    studying = _course("CS323")
    expected_only = _course("AI433")
    _requirement("DS", "CS323")
    _requirement("DS", "AI433")
    StudentCourse.objects.create(student=student, course=studying, status="studying")
    StudentCourse.objects.create(student=student, course=expected_only, status="not_taken")
    expected_section = _section("AI433", "M6")
    _link(student, expected_section, source="registration_plan_1448_t1")

    state = build_student_academic_state(student.student_id, "1448", "1")

    studying_fact = state.course("CS323")
    expected_fact = state.course("AI433")
    assert studying_fact is not None and expected_fact is not None
    assert studying_fact.studying_recorded is True
    assert studying_fact.registration_confirmed_for_requirement is False
    assert expected_fact.expected_sections
    assert expected_fact.registration_confirmed_for_requirement is False
    assert state.registered_course_codes == ()


def test_term_mapping_reconciles_ai1_with_registered_ai463_without_renaming_evidence() -> None:
    student = _student(991_003, programme="AI2")
    placeholder = _course("AI1")
    _requirement(
        "AI2",
        "AI1",
        credits=3,
        name="Programme Elective I",
        requirement_type="Program Elective",
        programme_term=7,
    )
    StudentCourse.objects.create(student=student, course=placeholder, status="studying")
    elective = ElectiveCourse.objects.create(
        course_code="AI463",
        course_name="Natural Language Processing",
        programme="AI",
        category="Program Elective",
        credit_hours=3,
    )
    ElectiveTermMapping.objects.create(
        academic_year="1448",
        term=1,
        programme="AI",
        placeholder_code="AI1",
        elective=elective,
    )
    # Same catalogue course in another term is not evidence for the requested
    # term; the exact 1448/1 mapping above is what authorizes the relationship.
    ElectiveTermMapping.objects.create(
        academic_year="1448",
        term=2,
        programme="AI",
        placeholder_code="AI2",
        elective=elective,
    )
    wrong_programme = ElectiveCourse.objects.create(
        course_code="CS499",
        course_name="Foreign programme option",
        programme="CS",
        credit_hours=3,
    )
    wrong_credits = ElectiveCourse.objects.create(
        course_code="AI464",
        course_name="Wrong-credit option",
        programme="AI",
        credit_hours=4,
    )
    for invalid in (wrong_programme, wrong_credits):
        ElectiveTermMapping.objects.create(
            academic_year="1448",
            term=1,
            programme="AI",
            placeholder_code="AI1",
            elective=invalid,
        )
    actual_section = _section("AI463", "M6", name="معالجة اللغات الطبيعية")
    _link(student, actual_section, source="scraper_timetable")

    state = build_student_academic_state(student.student_id, "1448", "1")
    slot = state.course("AI1")
    actual = state.course("AI463")

    assert slot is not None and actual is not None
    assert slot.metadata.is_elective_placeholder is True
    assert slot.directly_registered is False
    assert slot.registered_via_elective_options == ("AI463",)
    assert slot.registration_confirmed_for_requirement is True
    assert slot.studying_recorded is True
    assert [option.course_code for option in slot.elective_options] == ["AI463"]
    assert actual.directly_registered is True
    assert actual.fulfills_placeholders == ("AI1",)
    assert state.requirement_course_codes_for("AI463") == ("AI1",)
    assert state.requirement_course_codes_for("AI1") == ("AI1",)
    assert actual.metadata.course_name == "Natural Language Processing"
    assert actual.metadata.source is MetadataSource.ELECTIVE_CATALOGUE
    # Registration remains expressed using the course the registrar actually
    # returned; AI1 is a linked requirement, not rewritten evidence.
    assert state.registered_course_codes == ("AI463",)


def test_registered_placeholder_suppresses_mapped_concrete_alias() -> None:
    """Production shape: registrar says AI1 while recommendation data says AI463."""
    student = _student(991_009, programme="AI")
    placeholder = _course("AI1")
    _requirement(
        "AI",
        "AI1",
        credits=3,
        requirement_type="Program Elective",
        programme_term=7,
    )
    StudentCourse.objects.create(student=student, course=placeholder, status="not_taken")
    elective = ElectiveCourse.objects.create(
        course_code="AI463",
        course_name="Natural Language Processing",
        programme="AI",
        credit_hours=3,
    )
    ElectiveTermMapping.objects.create(
        academic_year="1448",
        term=1,
        programme="AI",
        placeholder_code="AI1",
        elective=elective,
    )
    registered_placeholder = _section("AI1", "M6")
    _link(student, registered_placeholder, source="scraper_timetable")

    state = build_student_academic_state(student.student_id, "1448", "1")
    slot = state.course("AI1")
    concrete = state.course("AI463")

    assert slot is not None and concrete is not None
    assert state.registered_course_codes == ("AI1",)
    assert state.occupied_elective_slot_codes == ("AI1",)
    assert state.registered_or_equivalent_course_codes == ("AI1", "AI463")
    assert concrete.directly_registered is False
    assert concrete.fulfills_placeholders == ("AI1",)
    assert concrete.registered_via_elective_placeholders == ("AI1",)
    payload = state.as_evidence_payload()
    assert payload["registered_course_codes"] == ["AI1"]
    assert payload["registered_or_equivalent_course_codes"] == ["AI1", "AI463"]
    assert payload["occupied_elective_slot_codes"] == ["AI1"]


def test_programme_metadata_does_not_leak_from_another_plan() -> None:
    student = _student(991_004, programme="DS")
    course = _course("CS101", credits=1, name="Global name")
    _requirement("AI", "CS101", credits=5, name="AI-specific name")
    _requirement("DS", "CS101", credits=3, name="DS-specific name")
    StudentCourse.objects.create(student=student, course=course, status="not_taken")

    state = build_student_academic_state(student.student_id, "1448", "1")
    fact = state.course("CS101")

    assert fact is not None
    assert fact.metadata.course_name == "DS-specific name"
    assert fact.metadata.credit_hours == 3


def test_full_programme_plan_is_present_even_when_studentcourse_row_is_missing() -> None:
    student = _student(991_005, programme="DS")
    _requirement("DS", "DS499", credits=3, name="Graduation Project", programme_term=9)

    state = build_student_academic_state(student.student_id, "1448", "1")
    fact = state.course("DS499")

    assert fact is not None
    assert fact.student_course is None
    assert fact.metadata.programme_term == 9
    assert fact.directly_registered is False


def test_unknown_transcript_status_is_preserved_and_not_treated_as_not_taken() -> None:
    student = _student(991_006, programme="DS")
    course = _course("DS300")
    _requirement("DS", "DS300")
    StudentCourse.objects.create(student=student, course=course, status="deferred")

    fact = build_student_academic_state(student.student_id, "1448", "1").course("DS300")

    assert fact is not None and fact.student_course is not None
    assert fact.student_course.status is TranscriptStatus.UNKNOWN
    assert fact.student_course.raw_status == "deferred"


def test_builder_is_read_only_and_payload_keeps_evidence_labels() -> None:
    student = _student(991_007, programme="DS")
    course = _course("DS201")
    _requirement("DS", "DS201")
    StudentCourse.objects.create(student=student, course=course, status="studying")
    section = _section("DS201", "M1")
    _link(student, section, source="scraper_timetable")
    counts_before = (
        Student.objects.count(),
        StudentCourse.objects.count(),
        StudentTermSection.objects.count(),
    )

    with CaptureQueriesContext(connection) as captured:
        state = build_student_academic_state(student.student_id, "1448", "1")

    assert all(
        query["sql"].lstrip().upper().startswith("SELECT") for query in captured.captured_queries
    )
    assert counts_before == (
        Student.objects.count(),
        StudentCourse.objects.count(),
        StudentTermSection.objects.count(),
    )
    payload = state.as_evidence_payload()
    assert payload["registered_course_codes"] == ["DS201"]
    assert payload["expected_course_codes"] == []
    assert payload["studying_course_codes"] == ["DS201"]
    assert payload["courses"][0]["registered_sections"][0]["evidence_kind"] == "REGISTERED"


def test_missing_student_or_programme_refuses_instead_of_using_global_metadata() -> None:
    with pytest.raises(AcademicStateUnavailable, match="was not found"):
        build_student_academic_state(991_099, "1448", "1")

    student = _student(991_008, programme="")
    with pytest.raises(AcademicStateUnavailable, match="has no programme"):
        build_student_academic_state(student.student_id, "1448", "1")


def test_a_mapping_written_for_another_term_or_year_is_not_evidence_for_this_one() -> None:
    """The mapping is scoped to the exact year AND term, and both must hold.

    Either scoping term could be deleted from the query with every test still
    green, because no fixture ever put a competing mapping for the SAME
    placeholder in another term. Without the bound, a term-2 mapping proves a
    term-1 registration: the slot counts as satisfied by a course the student
    is not taking this term.
    """
    student = _student(991_010, programme="AI")
    _course("AI1")
    _requirement(
        "AI",
        "AI1",
        credits=3,
        name="Programme Elective I",
        requirement_type="Program Elective",
        programme_term=7,
    )
    this_term = ElectiveCourse.objects.create(
        course_code="AI463",
        course_name="This term's option",
        programme="AI",
        category="Program Elective",
        credit_hours=3,
    )
    other_term = ElectiveCourse.objects.create(
        course_code="AI471",
        course_name="Next term's option",
        programme="AI",
        category="Program Elective",
        credit_hours=3,
    )
    other_year = ElectiveCourse.objects.create(
        course_code="AI472",
        course_name="Last year's option",
        programme="AI",
        category="Program Elective",
        credit_hours=3,
    )
    ElectiveTermMapping.objects.create(
        academic_year="1448",
        term=1,
        programme="AI",
        placeholder_code="AI1",
        elective=this_term,
    )
    # Same placeholder, different term and different year: neither authorises
    # anything about 1448/1.
    ElectiveTermMapping.objects.create(
        academic_year="1448",
        term=2,
        programme="AI",
        placeholder_code="AI1",
        elective=other_term,
    )
    ElectiveTermMapping.objects.create(
        academic_year="1447",
        term=1,
        programme="AI",
        placeholder_code="AI1",
        elective=other_year,
    )

    state = build_student_academic_state(student.student_id, "1448", "1")
    slot = state.course("AI1")

    assert slot is not None
    assert [option.course_code for option in slot.elective_options] == ["AI463"]

    # And a registration in the other term's option does not satisfy this term.
    _link(student, _section("AI471", "M6"), source="scraper_timetable")
    state = build_student_academic_state(student.student_id, "1448", "1")
    slot = state.course("AI1")
    assert slot is not None
    assert slot.registration_confirmed_for_requirement is False


def test_a_lowercase_mapping_row_still_resolves_the_equivalence() -> None:
    """The importer's spelling must not decide whether a student is registered.

    The placeholder set beside this lookup is built with program__iexact, but
    the mapping lookup itself was case-exact against a TextField. A row written
    as 'ai' matched nothing while the requirement it belongs to matched fine,
    so the whole equivalence vanished silently - the student's registered
    elective simply stopped counting, with no error anywhere.
    """
    student = _student(991_011, programme="AI")
    _course("AI1")
    _requirement(
        "AI",
        "AI1",
        credits=3,
        name="Programme Elective I",
        requirement_type="Program Elective",
        programme_term=7,
    )
    elective = ElectiveCourse.objects.create(
        course_code="AI463",
        course_name="Natural Language Processing",
        programme="ai",
        category="Program Elective",
        credit_hours=3,
    )
    ElectiveTermMapping.objects.create(
        academic_year="1448",
        term=1,
        programme="ai",
        placeholder_code="ai1",
        elective=elective,
    )
    _link(student, _section("AI463", "M6"), source="scraper_timetable")

    state = build_student_academic_state(student.student_id, "1448", "1")
    slot = state.course("AI1")

    assert slot is not None
    assert [option.course_code for option in slot.elective_options] == ["AI463"]
    assert slot.registration_confirmed_for_requirement is True


def test_expected_evidence_never_enters_the_registered_equivalent_set() -> None:
    """The suppression sets keep their snapshots apart, on one student.

    Every earlier fixture built a registrar-only world, so unioning the
    expected codes into registered_or_equivalent_course_codes survived the
    whole suite - and that union is precisely the REGISTERED/EXPECTED collapse
    this module exists to prevent, in the one place a recommender reads.
    """
    student = _student(991_020, programme="AI")
    _course("AI331")
    _requirement("AI", "AI331", credits=3, name="NLP", programme_term=7)
    _course("CS424")
    _requirement("AI", "CS424", credits=3, name="Vision", programme_term=8)
    _link(student, _section("AI331", "M6"), source="scraper_timetable")
    _link(student, _section("CS424", "M9"), source="registration_plan_1448_t1")

    state = build_student_academic_state(student.student_id, "1448", "1")

    assert "AI331" in state.registered_or_equivalent_course_codes
    assert "CS424" not in state.registered_or_equivalent_course_codes
    assert "CS424" in state.expected_or_equivalent_course_codes
    assert "AI331" not in state.expected_or_equivalent_course_codes


def test_an_unrecognised_cohort_value_never_becomes_the_students_cohort() -> None:
    """Student.section is scraped text; only M and F are cohorts.

    Passing an arbitrary value through would hand every downstream
    cohort-scoped filter a token that matches nothing - or worse, the other
    branch's prefix - as if it were an authoritative cohort.
    """
    student = _student(991_021, programme="AI")
    student.section = "X9"
    student.save(update_fields=["section"])
    _course("AI331")
    _requirement("AI", "AI331", credits=3, name="NLP", programme_term=7)

    state = build_student_academic_state(student.student_id, "1448", "1")
    assert state.cohort == ""

    student.section = " f "
    student.save(update_fields=["section"])
    state = build_student_academic_state(student.student_id, "1448", "1")
    assert state.cohort == "F"

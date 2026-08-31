"""Hermetic regressions for the adviser-only current-offerings forecast.

``TermSection`` is a single, termless snapshot.  Every successful call in this
module therefore supplies the snapshot's trusted clock explicitly.  The tests
exercise the public service only; they do not make the student recommender or
the registered-timetable forecast depend on recorded section availability.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest

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
    TermSectionProgram,
    TimetableScenario,
)
from core.services.advisor_graduation_optimization import (
    OptimizedGraduationUnavailable,
    build_optimized_current_offerings_report,
)
from core.services.student_helpers import normalize_code

pytestmark = pytest.mark.django_db

YEAR = 1448
TERM = 1
SNAPSHOT_KWARGS = {
    "section_snapshot_academic_year": YEAR,
    "section_snapshot_term": TERM,
}
OPTIMIZED_BASELINE = "optimized_current_offerings"


def _student(
    *,
    student_id: int = 4_599_101,
    program: str = "OPT",
    section: str = "M",
    earned: int = 42,
    registered: int = 15,
) -> Student:
    return Student.objects.create(
        student_id=student_id,
        registration_no=str(student_id),
        name=f"Optimized fixture {student_id}",
        program=program,
        section=section,
        status="ACTIVE",
        total_earned_credits=earned,
        current_registered_credits=registered,
    )


def _requirement(
    program: str,
    code: str,
    *,
    name: str,
    programme_term: int,
    credits: int = 3,
    prerequisites: Iterable[str] = (),
    stored_code: str | None = None,
    requirement_type: str = "Mandatory",
) -> ProgrammeRequirement:
    canonical_code = normalize_code(code)
    Course.objects.get_or_create(
        course_code=canonical_code,
        defaults={"description": name, "credit_hours": credits},
    )
    row = ProgrammeRequirement.objects.create(
        program=program,
        course_code=stored_code if stored_code is not None else canonical_code,
        course_name=name,
        type=requirement_type,
        programme_term=programme_term,
        credit_hours=credits,
    )
    for prerequisite in prerequisites:
        Prerequisite.objects.create(
            program=program,
            course_code=canonical_code,
            prerequisite_course_code=prerequisite,
        )
    return row


def _student_course(student: Student, code: str, status: str) -> StudentCourse:
    course = Course.objects.get(course_code=normalize_code(code))
    return StudentCourse.objects.create(
        student=student,
        course=course,
        programme_term=ProgrammeRequirement.objects.filter(
            program=student.program,
            course_code__iexact=normalize_code(code),
        )
        .values_list("programme_term", flat=True)
        .first(),
        status=status,
    )


def _section(
    code: str,
    *,
    name: str,
    label: str = "M1",
    programs: Iterable[str] = ("OPT",),
    scenario: TimetableScenario | None = None,
) -> TermSection:
    canonical_code = normalize_code(code)
    prefix = "".join(ch for ch in canonical_code if ch.isalpha()) or canonical_code
    number = canonical_code[len(prefix) :]
    section = TermSection.objects.create(
        scenario=scenario,
        source_tag="optimized-test",
        course_code=prefix,
        course_number=number,
        course_key=canonical_code,
        course_name=name,
        section=label,
        available_capacity=30,
        registered_count=0,
    )
    for program in programs:
        TermSectionProgram.objects.create(
            term_section=section,
            program=program,
            assignment_source="imported",
        )
    return section


def _optimized_report(
    student: Student,
    *,
    max_credits_per_term: int = 18,
) -> dict:
    return build_optimized_current_offerings_report(
        student.student_id,
        YEAR,
        TERM,
        max_credits_per_term=max_credits_per_term,
        **SNAPSHOT_KWARGS,
    )


def _baseline_codes(report: dict) -> list[str]:
    return [
        normalize_code(row.get("code") or row.get("course_code"))
        for row in report.get("planning_baseline_courses_assumed_passed") or []
    ]


def _term_codes(report: dict, year: int, term: int) -> list[str]:
    row = next(
        planned
        for planned in report.get("term_plan") or []
        if int(planned["academic_year"]) == year and int(planned["term"]) == term
    )
    return [normalize_code(code) for code in row.get("course_codes") or []]


def _database_state() -> dict[str, list[tuple]]:
    """The optimizer is a read-only scenario, including on failed calls."""
    return {
        "student_courses": list(
            StudentCourse.objects.order_by("student_id", "course_id").values_list(
                "student_id", "course_id", "status", "grade", "mark"
            )
        ),
        "student_sections": list(
            StudentTermSection.objects.order_by("id").values_list(
                "student_id", "academic_year", "term", "term_section_id", "source"
            )
        ),
        "sections": list(
            TermSection.objects.order_by("id").values_list(
                "id", "scenario_id", "course_key", "section", "source_tag"
            )
        ),
        "program_links": list(
            TermSectionProgram.objects.order_by("id").values_list(
                "term_section_id", "program", "assignment_source"
            )
        ),
    }


def test_out_of_parity_current_course_is_selected_but_future_terms_keep_formal_parity() -> None:
    """The 4504484 regression: a term-six course is real in a term-seven snapshot.

    Only the optimized baseline may use that current recorded-section evidence.
    Later forecast terms must return to the ordinary programme parity rules.
    """
    student = _student()
    _requirement("OPT", "P100", name="Passed foundation", programme_term=1)
    _requirement(
        "OPT",
        "IS242",
        name="Information systems foundation",
        programme_term=6,
        credits=4,
        prerequisites=("P100",),
    )
    _requirement(
        "OPT",
        "IS345",
        name="Information systems analysis",
        programme_term=7,
        credits=4,
        prerequisites=("IS242",),
    )
    _requirement(
        "OPT",
        "IS346",
        name="Information systems design",
        programme_term=8,
        credits=4,
        prerequisites=("IS345",),
    )
    _requirement(
        "OPT",
        "LEAK200",
        name="Persisted studying but not selected",
        programme_term=7,
        credits=3,
    )
    _requirement(
        "OPT",
        "DEP800",
        name="Must wait for the unselected studying course",
        programme_term=8,
        credits=3,
        prerequisites=("LEAK200",),
    )
    _student_course(student, "P100", StudentCourse.Status.PASSED)
    # Persisted studying evidence must not remove a course from this hypothetical
    # optimizer.  It must, however, never satisfy another course's prerequisite.
    _student_course(student, "IS242", StudentCourse.Status.STUDYING)
    # This second studying course has no trusted current section and is not
    # selected. It must be removed from the hypothetical scenario's in-progress
    # state instead of silently accelerating DEP800.
    _student_course(student, "LEAK200", StudentCourse.Status.STUDYING)
    _section("IS242", name="Information systems foundation")

    before = _database_state()
    report = _optimized_report(student)

    assert report["planning_baseline_kind"] == OPTIMIZED_BASELINE
    assert report["planning_baseline"]["kind"] == OPTIMIZED_BASELINE
    assert _baseline_codes(report) == ["IS242"]
    assert report["planning_baseline_credits"] == 4
    assert report["optimization"]["mode"] == OPTIMIZED_BASELINE
    assert report["optimization"]["strict_passed_only"] is True
    assert report["optimization"]["earned_hours_only"] is True
    assert report["optimization"]["section_snapshot_academic_year"] == YEAR
    assert report["optimization"]["section_snapshot_term"] == TERM
    assert report["optimization"]["selected_course_codes"] == ["IS242"]

    # At 1448/2 this prefix-45 student is on even programme parity. IS345 is an
    # odd course, so it waits for 1449/1 even though IS242 is assumed passed.
    assert _term_codes(report, 1448, 2) == []
    waiting = next(
        row
        for row in report["term_plan"]
        if int(row["academic_year"]) == 1448 and int(row["term"]) == 2
    )
    assert waiting["waiting_term"] is True
    assert set(_term_codes(report, 1449, 1)) == {"IS345", "LEAK200"}
    assert set(_term_codes(report, 1449, 2)) == {"IS346", "DEP800"}
    assert "LEAK200" not in {
        normalize_code(row.get("code") or row.get("course_code"))
        for row in report.get("in_progress") or []
    }
    assert _database_state() == before


@pytest.mark.parametrize(
    ("cohort", "expected"),
    [
        ("M", {"MOWN", "SHARED"}),
        ("F", {"FOWN", "SHARED"}),
    ],
)
def test_current_sections_are_filtered_by_local_cohort_and_other_branch(
    cohort: str,
    expected: set[str],
) -> None:
    student = _student(section=cohort)
    rows = [
        ("MOWN", "Male cohort course", "M1"),
        ("FOWN", "Female cohort course", "F1"),
        ("SHARED", "Shared online course", "ONLINE"),
        ("YMONLY", "Other branch male course", "YM4"),
        ("YFONLY", "Other branch female course", "YF4"),
    ]
    for code, name, label in rows:
        _requirement("OPT", code, name=name, programme_term=6)
        _section(code, name=name, label=label)

    report = _optimized_report(student)

    assert set(_baseline_codes(report)) == expected
    surfaced = repr(report).upper()
    assert "YMONLY" not in _baseline_codes(report)
    assert "YFONLY" not in _baseline_codes(report)
    assert "YM4" not in surfaced
    assert "YF4" not in surfaced


def test_unresolved_student_cohort_fails_closed_without_writes() -> None:
    student = _student(section="")
    _requirement("OPT", "SHARED", name="Shared section", programme_term=6)
    _section("SHARED", name="Shared section", label="ONLINE")
    before = _database_state()

    with pytest.raises(OptimizedGraduationUnavailable) as exc_info:
        _optimized_report(student)

    assert exc_info.value.code == "COHORT_UNRESOLVED"
    assert _database_state() == before


def test_only_trustworthy_plan_sections_enter_the_optimized_baseline() -> None:
    student = _student()
    _requirement("OPT", "ANCHOR", name="Anchor course", programme_term=6)
    _section("ANCHOR", name="Anchor course")

    # Approved exception: a section linked to another programme is trustworthy
    # when both plans resolve to the same normalized code and normalized name.
    _requirement("OPT", "CROSS200", name="Data Mining", programme_term=6)
    _requirement(
        "DS",
        "CROSS200",
        stored_code="cross 200",
        name="  data   mining  ",
        programme_term=6,
    )
    _section("CROSS200", name="DATA MINING", programs=("DS",))

    # Same display code but a different curriculum identity must not cross plans.
    _requirement("OPT", "COLLIDE300", name="Applied AI", programme_term=6)
    _requirement("DS", "COLLIDE300", name="Cyber Security", programme_term=6)
    _section("COLLIDE300", name="Cyber Security", programs=("DS",))

    # A foreign link without a matching foreign ProgrammeRequirement is not
    # provenance. Neither is a global row with no programme link at all.
    _requirement("OPT", "UNTRUST400", name="No foreign plan proof", programme_term=6)
    _section("UNTRUST400", name="No foreign plan proof", programs=("DS",))
    _requirement("OPT", "UNLINK500", name="Unlinked section", programme_term=6)
    _section("UNLINK500", name="Unlinked section", programs=())

    # Empty names cannot prove that the same display code denotes the same
    # curriculum course across programmes.
    _requirement("OPT", "BLANK700", name="", programme_term=6)
    _requirement("DS", "BLANK700", name="", programme_term=6)
    _section("BLANK700", name="", programs=("DS",))

    # Scenario sections are private planner artifacts, not the recorded global
    # section snapshot, even when someone attached a matching programme link.
    scenario = TimetableScenario.objects.create(
        academic_year=str(YEAR),
        term=str(TERM),
        name="Private optimized fixture",
        gender="M",
        programs=["OPT"],
    )
    _requirement("OPT", "SCEN600", name="Scenario-only section", programme_term=6)
    _section("SCEN600", name="Scenario-only section", scenario=scenario)

    # A matching section/program link can never manufacture a plan requirement.
    Course.objects.create(course_code="OUT999", description="Outside plan", credit_hours=3)
    _section("OUT999", name="Outside plan")

    report = _optimized_report(student)

    assert set(_baseline_codes(report)) == {"ANCHOR", "CROSS200"}
    cross_programme = next(
        row
        for row in report["planning_baseline_courses_assumed_passed"]
        if normalize_code(row.get("code")) == "CROSS200"
    )
    assert cross_programme["recorded_sections"] == ["M1"]
    assert cross_programme["recorded_section_programmes"] == ["DS"]
    assert not (
        {
            "COLLIDE300",
            "UNTRUST400",
            "UNLINK500",
            "SCEN600",
            "BLANK700",
            "OUT999",
        }
        & set(_baseline_codes(report))
    )


def test_strict_prerequisite_and_hour_gates_use_passes_and_earned_hours_only() -> None:
    student = _student(earned=42, registered=15)
    _requirement("OPT", "P100", name="Passed prerequisite", programme_term=1)
    _requirement("OPT", "P200", name="Studying prerequisite", programme_term=1)
    _student_course(student, "P100", StudentCourse.Status.PASSED)
    _student_course(student, "P200", StudentCourse.Status.STUDYING)

    candidates = [
        ("OK200", "Passed prerequisite accepted", ("P100",)),
        ("STUDY300", "Studying prerequisite rejected", ("P200",)),
        ("HOURS42", "Earned-hours boundary", ("42(HOURS)",)),
        ("HOURS50", "Registered hours do not count", ("50(HOURS)",)),
        ("A400", "Same-baseline prerequisite", ()),
        ("B500", "No same-baseline bootstrapping", ("A400",)),
    ]
    for code, name, prerequisites in candidates:
        _requirement(
            "OPT",
            code,
            name=name,
            programme_term=6,
            prerequisites=prerequisites,
        )
        _section(code, name=name)

    report = _optimized_report(student)
    selected = set(_baseline_codes(report))

    assert {"OK200", "HOURS42", "A400"} <= selected
    assert not ({"STUDY300", "HOURS50", "B500"} & selected)


def test_exact_term_mapped_offered_elective_fulfils_its_plan_placeholder() -> None:
    student = _student(program="ELECT")
    _requirement(
        "ELECT",
        "EL1",
        name="Programme Elective I",
        programme_term=6,
        credits=3,
        requirement_type="Program Elective",
    )
    concrete = ElectiveCourse.objects.create(
        course_code="EL461",
        course_name="Applied Data Mining",
        programme="ELECT",
        category="Program Elective",
        credit_hours=3,
    )
    ElectiveTermMapping.objects.create(
        academic_year=str(YEAR),
        term=TERM,
        programme="ELECT",
        placeholder_code="EL1",
        elective=concrete,
    )
    _section(
        "EL461",
        name="Applied Data Mining",
        label="M7",
        programs=("ELECT",),
    )

    report = _optimized_report(student)

    assert _baseline_codes(report) == ["EL1"]
    assert report["optimization"]["candidate_count"] == 1
    assert report["optimization"]["selected_plan_codes"] == ["EL1"]
    assert report["optimization"]["selected_offered_course_codes"] == ["EL461"]
    (row,) = report["planning_baseline_courses_assumed_passed"]
    assert row["code"] == "EL1", "the simulation must complete the plan placeholder"
    assert row["offered_course_code"] == "EL461"
    assert row["offered_course_name"] == "Applied Data Mining"
    assert row["fulfills_plan_code"] == "EL1"
    assert row["mapping_kind"] == "TERM_MAPPED_ELECTIVE"
    assert row["elective_slot"] is True
    assert row["recorded_sections"] == ["M7"]
    assert row["recorded_section_programmes"] == ["ELECT"]


def test_wrong_term_and_credit_mismatched_elective_mappings_are_not_candidates() -> None:
    student = _student(program="EMAP")
    _requirement("EMAP", "ANCHOR", name="Trusted direct course", programme_term=6)
    _section("ANCHOR", name="Trusted direct course", programs=("EMAP",))
    _requirement(
        "EMAP",
        "EM1",
        name="Programme Elective I",
        programme_term=6,
        credits=3,
        requirement_type="Programme Elective",
    )

    wrong_term = ElectiveCourse.objects.create(
        course_code="EM461",
        course_name="Mapped only next term",
        programme="EMAP",
        category="Program Elective",
        credit_hours=3,
    )
    wrong_credits = ElectiveCourse.objects.create(
        course_code="EM462",
        course_name="Four-credit option for a three-credit slot",
        programme="EMAP",
        category="Program Elective",
        credit_hours=4,
    )
    ElectiveTermMapping.objects.create(
        academic_year=str(YEAR),
        term=2,
        programme="EMAP",
        placeholder_code="EM1",
        elective=wrong_term,
    )
    ElectiveTermMapping.objects.create(
        academic_year=str(YEAR),
        term=TERM,
        programme="EMAP",
        placeholder_code="EM1",
        elective=wrong_credits,
    )
    _section("EM461", name=wrong_term.course_name, programs=("EMAP",))
    _section("EM462", name=wrong_credits.course_name, programs=("EMAP",))

    report = _optimized_report(student)

    assert _baseline_codes(report) == ["ANCHOR"]
    assert report["optimization"]["candidate_count"] == 1
    assert report["optimization"]["selected_offered_course_codes"] == ["ANCHOR"]
    assert "EM461" not in report["optimization"]["selected_offered_course_codes"]
    assert "EM462" not in report["optimization"]["selected_offered_course_codes"]


@pytest.mark.parametrize(
    ("metrics", "expected"),
    [
        (
            {
                "A100": {"completed": False, "terms": None, "blockers": 0},
                "B100": {"completed": True, "terms": 9, "blockers": 5},
            },
            "B100",
        ),
        (
            {
                "A100": {"completed": True, "terms": 5, "blockers": 0},
                "B100": {"completed": True, "terms": 4, "blockers": 5},
            },
            "B100",
        ),
        (
            {
                "A100": {"completed": True, "terms": 4, "blockers": 2},
                "B100": {"completed": True, "terms": 4, "blockers": 1},
            },
            "B100",
        ),
    ],
)
def test_public_optimizer_applies_completion_terms_then_blocker_objective(
    monkeypatch: pytest.MonkeyPatch,
    metrics: dict[str, dict[str, int | bool | None]],
    expected: str,
) -> None:
    student = _student(program="SCORE")
    for code in ("A100", "B100"):
        _requirement("SCORE", code, name=f"Score {code}", programme_term=6, credits=3)
        _section(code, name=f"Score {code}", programs=("SCORE",))

    def fake_forecast(
        _student_id: int,
        _year: int,
        _term: int,
        **kwargs: object,
    ) -> dict:
        rows = list(kwargs["_current_courses_override"])  # type: ignore[arg-type]
        assert len(rows) == 1, "the three-credit cap permits exactly one candidate"
        code = normalize_code(rows[0]["code"])
        outcome = metrics[code]
        blockers = int(outcome["blockers"] or 0)
        return {
            "planning_baseline_kind": "recommended_current_term",
            "planning_baseline": {"kind": "recommended_current_term"},
            "planning_baseline_courses_assumed_passed": rows,
            "planning_baseline_credits": 3,
            "simulation_completed": outcome["completed"],
            "estimated_terms_including_planning_baseline": outcome["terms"],
            "unresolved_requirements": [{"code": f"BLOCK{index}"} for index in range(blockers)],
            "simulation_assumptions": [],
            "term_plan": [],
            "in_progress": [],
        }

    monkeypatch.setattr(
        "core.services.advisor_graduation_optimization.build_graduation_report",
        fake_forecast,
    )

    report = _optimized_report(student, max_credits_per_term=3)

    assert _baseline_codes(report) == [expected]
    assert report["optimization"]["selected_course_codes"] == [expected]


def test_subset_search_finds_a_fuller_load_instead_of_taking_the_first_course() -> None:
    student = _student(program="PACK")
    # Programme order puts the four-credit course first. A greedy pass would
    # strand two credits; the lexicographic optimizer must find 3 + 3 instead.
    for code, credits in (("A400", 4), ("B300", 3), ("C300", 3)):
        name = f"Packing candidate {code}"
        _requirement("PACK", code, name=name, programme_term=6, credits=credits)
        _section(code, name=name, programs=("PACK",))

    report = _optimized_report(student, max_credits_per_term=6)

    assert _baseline_codes(report) == ["B300", "C300"]
    assert report["planning_baseline_credits"] == 6


def test_downstream_unlock_value_breaks_an_otherwise_equal_subset_tie() -> None:
    student = _student(program="CHAIN")
    # A100 is earlier in ProgrammeRequirement ordering. Z600 must nevertheless
    # win because it unlocks D800; either choice otherwise produces the same
    # complete four-term forecast at a three-credit cap.
    _requirement("CHAIN", "A100", name="Independent course", programme_term=4)
    _requirement("CHAIN", "Z600", name="Chain root", programme_term=6)
    _requirement(
        "CHAIN",
        "D800",
        name="Chain destination",
        programme_term=8,
        prerequisites=("Z600",),
    )
    _section("A100", name="Independent course", programs=("CHAIN",))
    _section("Z600", name="Chain root", programs=("CHAIN",))

    report = _optimized_report(student, max_credits_per_term=3)

    assert _baseline_codes(report) == ["Z600"]


def test_exact_subset_ties_use_programme_order_then_code_not_database_order() -> None:
    student = _student(program="ORDER")
    # Insert in the reverse of the contractual order to catch dependence on
    # queryset/PK order. All three choices have identical forecast value.
    rows = [
        ("B600", 6),
        ("A600", 6),
        ("Z200", 2),
    ]
    for code, programme_term in rows:
        name = f"Deterministic {code}"
        _requirement(
            "ORDER",
            code,
            name=name,
            programme_term=programme_term,
            credits=3,
        )
    for code, _programme_term in reversed(rows):
        _section(code, name=f"Deterministic {code}", programs=("ORDER",))

    first = _optimized_report(student, max_credits_per_term=6)
    second = _optimized_report(student, max_credits_per_term=6)

    assert _baseline_codes(first) == ["Z200", "A600"]
    assert _baseline_codes(second) == ["Z200", "A600"]


@pytest.mark.parametrize(
    ("snapshot_year", "snapshot_term"),
    [
        (None, None),
        (YEAR, None),
        (None, TERM),
        (YEAR - 1, TERM),
        (YEAR, 2),
    ],
)
def test_snapshot_clock_must_be_explicit_complete_and_match_the_requested_term(
    snapshot_year: int | None,
    snapshot_term: int | None,
) -> None:
    student = _student()
    _requirement("OPT", "CLOCK100", name="Clock-bound section", programme_term=6)
    _section("CLOCK100", name="Clock-bound section")
    before = _database_state()

    with pytest.raises(OptimizedGraduationUnavailable) as exc_info:
        build_optimized_current_offerings_report(
            student.student_id,
            YEAR,
            TERM,
            section_snapshot_academic_year=snapshot_year,
            section_snapshot_term=snapshot_term,
        )

    assert exc_info.value.code == "SECTION_SNAPSHOT_TERM_MISMATCH"
    assert _database_state() == before

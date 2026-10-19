"""Instructor links follow the course IDENTITY, not the display code.

CS112 is PROGRAMMING I in the second-cohort plans (AI2/DS2, term 2) and
PROGRAMMING II in the first-cohort plans (AI/DS, term 4) — two different
courses (owner-confirmed 2026-10-19). ``CourseInstructor`` is keyed by
``(programme, course_code)``, so pooling every scenario programme's CS112 row
billed PROGRAMMING II to PROGRAMMING I's lecturers. In lab scenario 667 that
made PROGRAMMING II unplaceable (every option clashed with those lecturers'
full days), wrote their name onto its meetings, and invented daily overloads
that rolled back the optimiser. A link may only reach a section whose course
its own programme teaches.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from core.models import (
    CourseInstructor,
    DeliveryBoard,
    Instructor,
    ProgrammeRequirement,
    Room,
    ScenarioSectionBudget,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.course_identity import programmes_serving_course
from core.services.course_instructor_assignment import (
    apply_primary_instructor,
    reconcile_scenario_instructors,
)
from core.services.timetable_pr4_instructor import (
    build_section_instructor_ids,
    normalise_instructor,
)

PROGRAMS = ["DS", "AI", "AI2", "DS2"]  # the lab scenario's (and the board's) order
PI, PII = "CS112::PROGRAMMING_I", "CS112::PROGRAMMING_II"


def _instructor(name: str) -> Instructor:
    return Instructor.objects.create(full_name=name, normalised_name=normalise_instructor(name))


def _link(program: str, code: str, instructor: Instructor, role: str = "primary") -> None:
    CourseInstructor.objects.create(
        program=program, course_code=code, section="M", instructor=instructor, role=role
    )


def _section(scenario, course_key: str, label: str = "S1") -> TermSection:
    code = course_key.split("::", 1)[0]
    return TermSection.objects.create(
        scenario=scenario,
        course_key=course_key,
        course_code=code,
        course_number=code,
        course_name=course_key,
        section=label,
        source_tag="tw_auto",
    )


def _meeting(ts: TermSection, day: str = "MON", start: str = "09:00", end: str = "10:15"):
    return TermSectionMeeting.objects.create(
        term_section=ts, day=day, start_time=start, end_time=end, room="", instructor=""
    )


def _mixed_cohorts() -> tuple[TimetableScenario, DeliveryBoard, dict[str, Instructor]]:
    """AI/DS teach CS112 PROGRAMMING II, AI2/DS2 teach CS112 PROGRAMMING I; only the
    second cohort has CS112 lecturers — exactly the lab scenario 667 data."""
    for program, name, term in (
        ("AI", "PROGRAMMING II", 4),
        ("DS", "PROGRAMMING II", 4),
        ("AI2", "PROGRAMMING I", 2),
        ("DS2", "PROGRAMMING I", 2),
    ):
        ProgrammeRequirement.objects.create(
            program=program, course_code="CS112", course_name=name, programme_term=term
        )
    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="2", name="mixed cohorts", gender="M", programs=PROGRAMS
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="Term 4", nominal_term=4, program=",".join(PROGRAMS)
    )
    people = {"AI2": _instructor("Dr AI2 Programming I"), "DS2": _instructor("Dr DS2 Prog I")}
    _link("AI2", "CS112", people["AI2"])
    _link("DS2", "CS112", people["DS2"])
    return scenario, board, people


# ── the shared identity helper ───────────────────────────────────────────


@pytest.mark.django_db
def test_programmes_serving_course_splits_a_shared_code_by_name() -> None:
    _mixed_cohorts()
    serving = programmes_serving_course(PROGRAMS)

    assert serving(PII) == ["DS", "AI"]
    assert serving(PI) == ["AI2", "DS2"]
    # Nothing to tell apart: a bare key, or a key no plan names (a resolved
    # catalogue elective lives in ElectiveCourse), keeps every programme.
    assert serving("CS112") == PROGRAMS
    assert serving("AI463::INFORMATION_RETRIEVAL") == PROGRAMS


# ── every resolver of section -> instructors ─────────────────────────────


@pytest.mark.django_db
def test_section_instructor_ids_do_not_lend_programming_i_lecturers_to_programming_ii() -> None:
    scenario, _board, people = _mixed_cohorts()
    _section(scenario, PI)
    _section(scenario, PII)

    assert build_section_instructor_ids(scenario) == {
        f"{PI}|S1": {people["AI2"].id, people["DS2"].id},
    }


@pytest.mark.django_db
def test_v2_instructor_map_does_not_bill_programming_ii_to_programming_i_lecturers() -> None:
    from core.services.timetable_optimizer_v2 import build_section_instructor_map_for_scenario

    scenario, _board, people = _mixed_cohorts()
    _section(scenario, PI)
    _section(scenario, PII)

    assert build_section_instructor_map_for_scenario(scenario.id) == {
        f"{PI}_S1": frozenset({people["AI2"].id, people["DS2"].id}),
    }


@pytest.mark.django_db
def test_placeholder_and_catalogue_elective_staffing_is_kept() -> None:
    """Not a regression for electives: a placeholder both cohorts name alike keeps
    both cohorts' lecturers, a resolved catalogue elective (in no plan) keeps its
    links, and a placeholder the cohorts name differently is staffed per cohort —
    the scheduler intake's (programme, slot) rule."""
    scenario, _board, _people = _mixed_cohorts()
    for program, code, name in (
        ("AI", "AI2", "PROGRAM ELECTIVE COURSE II"),
        ("AI2", "AI2", "PROGRAM ELECTIVE COURSE II"),
        ("AI", "AI1", "PROGRAM ELECTIVE I"),
        ("AI2", "AI1", "PROGRAM ELECTIVE COURSE I"),
    ):
        ProgrammeRequirement.objects.create(program=program, course_code=code, course_name=name)
    first, second, catalogue = _instructor("Dr A"), _instructor("Dr B"), _instructor("Dr C")
    _link("AI", "AI2", first)
    _link("AI2", "AI2", second)
    _link("AI", "AI1", first)
    _link("AI2", "AI1", second)
    _link("AI", "AI463", catalogue)
    _section(scenario, "AI2::PROGRAM_ELECTIVE_COURSE_II")
    _section(scenario, "AI1::PROGRAM_ELECTIVE_I")
    _section(scenario, "AI1::PROGRAM_ELECTIVE_COURSE_I")
    _section(scenario, "AI463::INFORMATION_RETRIEVAL")

    ids = build_section_instructor_ids(scenario)

    assert ids["AI2::PROGRAM_ELECTIVE_COURSE_II|S1"] == {first.id, second.id}
    assert ids["AI1::PROGRAM_ELECTIVE_I|S1"] == {first.id}
    assert ids["AI1::PROGRAM_ELECTIVE_COURSE_I|S1"] == {second.id}
    assert ids["AI463::INFORMATION_RETRIEVAL|S1"] == {catalogue.id}


@pytest.mark.django_db
def test_solver_fallback_resolves_by_course_identity() -> None:
    """CP-SAT's fallback covers sections not generated yet AND sections whose
    identity has no lecturer — the latter used to fall through to a bare-code
    pool and borrow PROGRAMMING I's lecturers again."""
    from core.services.timetable_solver import _instructor_ids_for_sections

    scenario, board, people = _mixed_cohorts()
    _section(scenario, PII)  # exists, but no AI/DS CS112 lecturer
    sections = [
        {"code": PII, "display_code": "CS112", "label": "S1"},
        {"code": PII, "display_code": "CS112", "label": "S2"},  # not generated yet
        {"code": PI, "display_code": "CS112", "label": "S1"},  # not generated yet
    ]

    assert _instructor_ids_for_sections(board, sections) == [
        set(),
        set(),
        {people["AI2"].id, people["DS2"].id},
    ]


@pytest.mark.django_db
@override_settings(TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True)
def test_repair_report_sees_no_phantom_clash_between_the_two_cs112_courses() -> None:
    from core.services.timetable_repair_eligibility import _section_instructor_policy

    scenario, board, people = _mixed_cohorts()
    placed = {}
    for key in (PI, PII):
        ts = _section(scenario, key)
        _meeting(ts)
        SectionPlacement.objects.create(
            board=board, term_section=ts, day="MON", start_time="09:00", end_time="10:15", room="R1"
        )
        placed[key] = ts

    names, conflicts = _section_instructor_policy(
        scenario_id=scenario.id, target_section_ids={placed[PI].id, placed[PII].id}
    )

    assert placed[PII].id not in conflicts
    assert placed[PI].id not in conflicts
    assert names.get(placed[PII].id, set()) == set()
    assert names[placed[PI].id] == {people["AI2"].full_name, people["DS2"].full_name}


# ── the primary-name write-through ───────────────────────────────────────


@pytest.mark.django_db
def test_write_through_never_names_the_other_cs112_courses_lecturer() -> None:
    scenario, board, people = _mixed_cohorts()
    pi, pii = _section(scenario, PI), _section(scenario, PII)
    _meeting(pi)
    _meeting(pii)

    # Board order is DS, AI, AI2, DS2: AI/DS have no CS112 primary, so a code
    # lookup fell through to AI2 and wrote PROGRAMMING I's lecturer here.
    assert apply_primary_instructor(pii, scenario, board, "CS112") is False
    assert pii.meetings.get().instructor == ""

    # The other direction: an AI PROGRAMMING II primary, first in board order,
    # must not overwrite PROGRAMMING I's own lecturer.
    _link("AI", "CS112", _instructor("Dr AI Programming II"))
    assert apply_primary_instructor(pi, scenario, board, "CS112") is True
    assert pi.meetings.get().instructor == people["AI2"].full_name
    assert apply_primary_instructor(pii, scenario, board, "CS112") is True
    assert pii.meetings.get().instructor == "Dr AI Programming II"


@pytest.mark.django_db
def test_reconcile_never_names_the_other_cs112_courses_lecturer() -> None:
    scenario, _board, people = _mixed_cohorts()
    pi, pii = _section(scenario, PI), _section(scenario, PII)
    _meeting(pi)
    _meeting(pii)
    _link("DS", "CS112", _instructor("Dr DS Programming II"))  # DS is first in order

    assert reconcile_scenario_instructors(scenario) == 2
    assert pi.meetings.get().instructor == people["AI2"].full_name
    assert pii.meetings.get().instructor == "Dr DS Programming II"


# ── the placer: identity, and honest diagnostics ─────────────────────────


@pytest.mark.django_db
@override_settings(
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
    TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True,
    TIMETABLE_PR3_DECISION_TRACE_ENABLED=False,
)
def test_section_blocked_only_by_instructor_clash_reports_the_clash_under_its_key() -> None:
    """Every option of the section clashes with its lecturer's other class, so it
    is skipped. That used to be filed as NO_ROOM_CAPACITY for a bare "CS112" —
    a room shortage that did not exist, for a course that could not be told
    apart from the other CS112. The reason must not depend on the trace flag."""
    from core.services.timetable_autoplace import auto_place_board

    ProgrammeRequirement.objects.create(
        program="AI", course_code="CS112", course_name="PROGRAMMING II", programme_term=4
    )
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="2",
        name="busy lecturer",
        gender="M",
        programs=["AI"],
        slot_config=[{"start": "09:00", "end": "10:15"}],
        lab_slot_config=[],
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="T4", nominal_term=4, program="AI"
    )
    Room.objects.create(room_code="R1", capacity=60, room_type="lecture", department="AI")
    lecturer = _instructor("Dr Busy")
    _link("AI", "CS112", lecturer)
    _link("AI", "AI113", lecturer)
    busy = _section(scenario, "AI113")
    for day in ("SUN", "MON", "TUE", "WED", "THU"):  # every 09:00 already taught
        _meeting(busy, day=day)
    _section(scenario, PII)  # generated, not yet placed — as in scenario 667
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_code="CS112",
        course_key=PII,
        course_name="PROGRAMMING II",
        department="CS",
        credit_hours=3,
        planned_sections=1,
        max_per_section=30,
        total_demand=13,
        programme_term=4,
    )

    result = auto_place_board(board.id)

    assert result["placed"] == 0
    [failure] = result["room_failures"]
    assert failure["reason"] == "INSTRUCTOR_CLASH"
    assert failure["course_code"] == PII
    assert failure["section_code"] == "S1"
    assert failure["context"]["clashing_instructor_id"] == lecturer.id
    assert failure["context"]["rejected_by"] == {"INSTRUCTOR_CLASH": failure["context"]["options"]}
    assert result["room_failure_breakdown"] == {"INSTRUCTOR_CLASH": 1}

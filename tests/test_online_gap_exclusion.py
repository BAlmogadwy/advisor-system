"""ONLINE sessions must not be counted as on-campus gap time.

An online class is attended remotely, so it neither creates nor bridges a campus
gap. Counting it overstates ``gap_minutes`` for every student holding an online
course and — worse — lets the optimiser "close" a gap by parking an in-person
class next to an online one, which changes nothing for the student. The same
applies to instructor idle.

Gated by ``TIMETABLE_ONLINE_GAP_EXCLUSION_ENABLED``; with the flag off no section
is ever marked online, so the previous online-blind numbers are byte-identical.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from core.services.timetable_assignment_models import (
    SectionMeeting,
    SectionState,
    StudentAssignmentState,
)
from core.services.timetable_student_assignment import (
    _compute_instructor_idle_minutes,
    _compute_total_state_gap,
    calculate_added_gap,
)


def _sec(section_id, course_code, meetings, *, online=False):
    return SectionState(
        section_id=section_id,
        course_code=course_code,
        meetings=[SectionMeeting(day=d, start_min=s, end_min=e) for d, s, e in meetings],
        max_capacity=40,
        reserve_capacity=0,
        is_online=online,
    )


def _state(*section_ids):
    st = StudentAssignmentState(student_id="S1")
    for i, sid in enumerate(section_ids):
        st.assigned_sections[f"C{i}"] = sid
        st.section_ids.add(sid)  # the gap chain iterates section_ids
    return st


# ── student gap ──────────────────────────────────────────────────────────────


def test_online_session_between_two_campus_classes_is_not_a_bridge() -> None:
    """SUN 09:00-10:15 campus, 11:00-12:00 ONLINE, 14:00-15:15 campus.

    The student is on campus 09:00-10:15 then 14:00-15:15 → one 225-min campus
    gap. The online class in the middle must NOT split it into two smaller gaps.
    """
    campus_a = _sec("A_S1", "A", [(0, 540, 615)])
    online_m = _sec("B_S1", "B", [(0, 660, 720)], online=True)
    campus_b = _sec("C_S1", "C", [(0, 840, 915)])
    by_id = {s.section_id: s for s in (campus_a, online_m, campus_b)}
    gap = _compute_total_state_gap(_state("A_S1", "B_S1", "C_S1"), by_id)
    assert gap == 840 - 615  # 225: campus-to-campus only


def test_online_only_day_has_zero_campus_gap() -> None:
    a = _sec("A_S1", "A", [(0, 540, 615)], online=True)
    b = _sec("B_S1", "B", [(0, 840, 915)], online=True)
    by_id = {s.section_id: s for s in (a, b)}
    assert _compute_total_state_gap(_state("A_S1", "B_S1"), by_id) == 0


def test_campus_only_gap_is_unchanged() -> None:
    a = _sec("A_S1", "A", [(0, 540, 615)])
    b = _sec("B_S1", "B", [(0, 840, 915)])
    by_id = {s.section_id: s for s in (a, b)}
    assert _compute_total_state_gap(_state("A_S1", "B_S1"), by_id) == 225


def test_flag_off_reproduces_online_blind_numbers() -> None:
    """Byte-parity contract: with the flag off nothing is marked online, so a
    board built that way scores exactly as before. Simulated here by building the
    same sections WITHOUT the marker."""
    blind_online = _sec("B_S1", "B", [(0, 660, 720)])  # not marked
    a = _sec("A_S1", "A", [(0, 540, 615)])
    c = _sec("C_S1", "C", [(0, 840, 915)])
    by_id = {s.section_id: s for s in (a, blind_online, c)}
    # 09:00-10:15 → 11:00 (45) + 12:00 → 14:00 (120) = 165, the pre-fix number.
    assert _compute_total_state_gap(_state("A_S1", "B_S1", "C_S1"), by_id) == 165


# ── candidate pricing (seating) ──────────────────────────────────────────────


def test_online_candidate_adds_zero_gap() -> None:
    """Seating an online course must never be charged gap minutes — otherwise the
    seater declines a free enrolment."""
    seated = _sec("A_S1", "A", [(0, 540, 615)])
    by_id = {"A_S1": seated}
    candidate = _sec("ON_S1", "ON", [(0, 900, 975)], online=True)
    by_id[candidate.section_id] = candidate
    assert calculate_added_gap(_state("A_S1"), candidate, by_id) == 0


def test_campus_candidate_after_online_prices_against_campus_only() -> None:
    """An in-person candidate must be priced against the CAMPUS chain: moving it
    next to an online class is not a real gap reduction."""
    campus = _sec("A_S1", "A", [(0, 540, 615)])
    online_m = _sec("ON_S1", "ON", [(0, 660, 720)], online=True)
    by_id = {s.section_id: s for s in (campus, online_m)}
    candidate = _sec("C_S1", "C", [(0, 840, 915)])
    by_id[candidate.section_id] = candidate
    # campus 09:00-10:15 → candidate 14:00 = 225, NOT 120 (from the online end).
    assert calculate_added_gap(_state("A_S1", "ON_S1"), candidate, by_id) == 225


# ── instructor idle ──────────────────────────────────────────────────────────


def test_instructor_idle_ignores_online_sessions() -> None:
    """Teaching remotely does not strand an instructor on campus either side."""
    campus_a = _sec("A_S1", "A", [(0, 540, 615)])
    online_m = _sec("B_S1", "B", [(0, 660, 720)], online=True)
    campus_b = _sec("C_S1", "C", [(0, 840, 915)])
    by_id = {s.section_id: s for s in (campus_a, online_m, campus_b)}
    smap = {"A_S1": frozenset({7}), "B_S1": frozenset({7}), "C_S1": frozenset({7})}
    assert _compute_instructor_idle_minutes(by_id, smap) == 225


# ── end-to-end: the flag actually marks sections online ──────────────────────


@pytest.mark.django_db
@override_settings(TIMETABLE_ONLINE_GAP_EXCLUSION_ENABLED=True)
def test_build_section_states_marks_online_courses() -> None:
    """The marker must survive the real build path, resolved on the BARE course
    code (SectionState.course_code is the planner CODE::NAME identity)."""
    from core.models import (
        DeliveryBoard,
        ProgrammeRequirement,
        ScenarioSectionBudget,
        SectionPlacement,
        TermSection,
        TimetableScenario,
    )
    from core.services.timetable_optimizer_v2 import build_section_states_for_scenario

    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="online t", gender="M", programs=["AI"]
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="T1", nominal_term=1, program="AI"
    )
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="AI900",
        course_name="ONLINE C",
        type="core",
        programme_term=1,
        credit_hours=3,
        is_online=True,
    )
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="AI901",
        course_name="CAMPUS C",
        type="core",
        programme_term=1,
        credit_hours=3,
        is_online=False,
    )
    for code in ("AI900", "AI901"):
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_code=code,
            course_key=f"{code}::X",
            department="AI",
            credit_hours=3,
            planned_sections=1,
            max_per_section=30,
            total_demand=10,
        )
        ts = TermSection.objects.create(
            scenario=scenario,
            course_code=code,
            course_key=f"{code}::X",
            course_number=code,
            course_name=code,
            section="S1",
            source_tag="test",
        )
        SectionPlacement.objects.create(
            board=board,
            term_section=ts,
            day="SUN",
            start_time="09:00",
            end_time="10:15",
            room="R1",
        )

    by_id = {s.section_id: s for s in build_section_states_for_scenario(scenario.id)}
    online = [s for s in by_id.values() if s.is_online]
    campus = [s for s in by_id.values() if not s.is_online]
    assert len(online) == 1 and online[0].course_code.startswith("AI900")
    assert len(campus) == 1 and campus[0].course_code.startswith("AI901")


@pytest.mark.django_db
@override_settings(TIMETABLE_ONLINE_GAP_EXCLUSION_ENABLED=True)
def test_online_resolves_from_the_board_when_scenario_programs_is_empty() -> None:
    """``scenario.programs`` is only populated by the generate path — scenarios
    created by import/clone/manual can have it empty (live: 615/616/617/620, and
    620 carries 168 placements). Keying the marker off it made the whole feature
    silently no-op there. Online-ness must resolve from the BOARD, matching the
    convention every other online consumer uses.
    """
    from core.models import (
        DeliveryBoard,
        ProgrammeRequirement,
        ScenarioSectionBudget,
        SectionPlacement,
        TermSection,
        TimetableScenario,
    )
    from core.services.timetable_optimizer_v2 import build_section_states_for_scenario

    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="no programs",
        gender="M",
        programs=[],  # the hole
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="T1", nominal_term=1, program="AI"
    )
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="GS111",
        course_name="ONLINE GEN-ED",
        type="core",
        programme_term=1,
        credit_hours=3,
        is_online=True,
    )
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_code="GS111",
        course_key="GS111::X",
        department="GS",
        credit_hours=3,
        planned_sections=1,
        max_per_section=30,
        total_demand=10,
    )
    ts = TermSection.objects.create(
        scenario=scenario,
        course_code="GS111",
        course_key="GS111::X",
        course_number="GS111",
        course_name="GS111",
        section="S1",
        source_tag="test",
    )
    SectionPlacement.objects.create(
        board=board, term_section=ts, day="SUN", start_time="09:00", end_time="10:15", room="R1"
    )

    states = build_section_states_for_scenario(scenario.id)
    assert states, "expected the section to build"
    assert all(s.is_online for s in states), (
        "online course not detected because scenario.programs is empty — "
        "resolution must come from the board"
    )


@pytest.mark.django_db
@override_settings(TIMETABLE_ONLINE_GAP_EXCLUSION_ENABLED=False)
def test_flag_off_marks_nothing_online() -> None:
    """Byte-parity: with the flag off no section carries the marker, so every gap
    chain behaves exactly as before the fix."""
    from core.models import (
        DeliveryBoard,
        ProgrammeRequirement,
        ScenarioSectionBudget,
        SectionPlacement,
        TermSection,
        TimetableScenario,
    )
    from core.services.timetable_optimizer_v2 import build_section_states_for_scenario

    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="online off", gender="M", programs=["AI"]
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="T1", nominal_term=1, program="AI"
    )
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="AI900",
        course_name="ONLINE C",
        type="core",
        programme_term=1,
        credit_hours=3,
        is_online=True,
    )
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_code="AI900",
        course_key="AI900::X",
        department="AI",
        credit_hours=3,
        planned_sections=1,
        max_per_section=30,
        total_demand=10,
    )
    ts = TermSection.objects.create(
        scenario=scenario,
        course_code="AI900",
        course_key="AI900::X",
        course_number="AI900",
        course_name="AI900",
        section="S1",
        source_tag="test",
    )
    SectionPlacement.objects.create(
        board=board, term_section=ts, day="SUN", start_time="09:00", end_time="10:15", room="R1"
    )

    states = build_section_states_for_scenario(scenario.id)
    assert states and all(not s.is_online for s in states)

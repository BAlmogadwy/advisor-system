from dataclasses import replace

import pytest

from core.services.timetable_assignment_models import SectionMeeting, SectionState
from core.services.timetable_autoplace import generate_course_meeting_options, get_meeting_pattern
from core.services.timetable_course_policy import get_course_meeting_policy
from core.services.timetable_pattern_catalog import build_canonical_pattern_catalog


@pytest.mark.parametrize(
    "code,key,number",
    [
        ("ENG101", "", ""),
        ("ENG", "ENG101", "101"),
        ("ENG102", "ENG102::ENGLISH_LANGUAGE_SKILLS_II", "ENG102"),
        (" eng 102 ", "", ""),
    ],
)
def test_exact_identity_selects_the_two_complete_patterns(code, key, number):
    policy = get_course_meeting_policy(code, key, number)
    assert policy.durations == (180, 180, 180, 180)
    assert {tuple(m.day for m in p.meetings) for p in policy.patterns} == {
        ("SUN", "MON", "WED", "THU")
    }
    assert {tuple((m.start_min, m.end_min) for m in p.meetings) for p in policy.patterns} == {
        ((540, 720),) * 4,
        ((780, 960),) * 4,
    }


@pytest.mark.parametrize("code", ["ENGL103", "ENG103", "ENG1010", "ENG102A", "AI221"])
def test_other_courses_keep_the_ordinary_credit_pattern(code):
    assert get_course_meeting_policy(code) is None
    assert get_meeting_pattern(4, code) == [75, 75, 100]


def test_validator_rejects_mixed_partial_extra_and_shifted_patterns():
    policy = get_course_meeting_policy("ENG101")
    morning, afternoon = policy.patterns
    assert policy.accepts(reversed(morning.meetings))
    assert policy.accepts(afternoon.meetings)
    assert not policy.accepts(morning.meetings[:-1])
    assert not policy.accepts((*morning.meetings, morning.meetings[0]))
    assert not policy.accepts((*morning.meetings[:-1], afternoon.meetings[-1]))
    assert not policy.accepts((replace(morning.meetings[0], day="TUE"), *morning.meetings[1:]))
    assert not policy.accepts((replace(morning.meetings[0], end_min=719), *morning.meetings[1:]))


def test_blocked_overlap_excludes_whole_bundle_even_inside_a_regular_grid_break():
    options = generate_course_meeting_options(
        "ENG102",
        4,
        blocked_slots=[
            {"day": "MON", "start": "10:20", "end": "10:25"},
        ],
    )
    assert len(options) == 1
    assert {m["start"] for m in options[0]} == {"13:00"}
    assert (
        generate_course_meeting_options(
            "ENG102",
            4,
            blocked_slots=[
                {"day": "MON", "start": "10:20", "end": "10:25"},
                {"day": "THU", "start": "15:55", "end": "16:00"},
            ],
        )
        == []
    )
    assert (
        len(
            generate_course_meeting_options(
                "ENG102",
                4,
                blocked_slots=[
                    {"day": "TUE", "start": "09:00", "end": "16:00"},
                    {"day": "SUN", "start": "12:00", "end": "13:00"},
                ],
            )
        )
        == 2
    )


@pytest.mark.parametrize("code,key", [("ENG101", ""), ("ENG", "ENG101"), ("ENG101", "opaque")])
def test_pattern_catalog_is_scoped_to_the_policy_not_generic_long_lecture_durations(code, key):
    catalog = build_canonical_pattern_catalog(
        [
            {"course_code": code, "course_key": key, "durations": [180] * 4},
            {"course_code": "ENG102", "durations": [180] * 4},
            {"course_code": "AI221", "durations": [75, 75, 100], "has_lab": True},
        ]
    )
    patterns = catalog["ENG_INTENSIVE_V1"]
    assert len(patterns) == 2
    assert all(not p.is_lab_mixed for p in patterns)
    assert all(get_course_meeting_policy("ENG101").accepts(p.meetings) for p in patterns)
    assert len(catalog) == 2


def test_parallel_english_sections_do_not_waive_real_instructor_clashes():
    from core.services.timetable_constraints import has_instructor_clash, has_same_course_overlap

    meetings = [SectionMeeting(i, 540, 720) for i in (0, 1, 3, 4)]
    sections = {str(i): SectionState(str(i), "ENG101", meetings, 25, 0) for i in range(3)}
    assert not has_same_course_overlap(sections)
    assert not has_instructor_clash(sections, {str(i): {i} for i in range(3)})
    assert has_instructor_clash(sections, {str(i): {1} for i in range(3)})


@pytest.mark.django_db
def test_cpsat_can_switch_english_only_as_a_complete_bundle():
    from core.models import DeliveryBoard, TimetableScenario
    from core.services.timetable_assignment_models import RiskTier, StudentProfile
    from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
    from core.services.timetable_cpsat_polisher import polish_scenario_with_cpsat

    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="2", gender="M", programs=["AI"]
    )
    DeliveryBoard.objects.create(scenario=scenario, label="T2", nominal_term=2, program="AI")
    meetings = [SectionMeeting(i, 540, 720) for i in (0, 1, 3, 4)]
    english = SectionState(
        "ENG102_S1",
        "ENG102",
        meetings,
        25,
        0,
        uses_shared_room=False,
        pattern_family="ENG_INTENSIVE_V1",
        pattern_id="ENG_INTENSIVE_V1_AM",
    )
    fixed = SectionState("AI221_S1", "AI221", [SectionMeeting(1, 540, 615)], 25, 0)
    profiles = {"student": StudentProfile("student", "AI", ["ENG102", "AI221"], RiskTier.C, 0)}
    baseline = evaluate_generated_timetable_candidate("baseline", [english, fixed], profiles, {})
    result = polish_scenario_with_cpsat(
        scenario.pk,
        [english, fixed],
        profiles,
        {},
        baseline,
        time_limit_seconds=2,
        locked_section_ids={fixed.section_id},
    )
    assert result is not None
    improved = next(s for s in result["improved_sections"] if s.course_code == "ENG102")
    assert get_course_meeting_policy("ENG102").accepts(improved.meetings)
    assert {m.start_min for m in improved.meetings} == {780}
    assert improved.pattern_id == "ENG_INTENSIVE_V1_PM"
    assert not improved.uses_shared_room


@pytest.mark.django_db
@pytest.mark.parametrize(
    "code,key", [("ENG102", "ENG102::ENGLISH_LANGUAGE_SKILLS_II"), ("ENG", "ENG102")]
)
def test_new_generation_places_three_plus_english_sections_as_complete_lectures(code, key):
    from core.models import (
        DeliveryBoard,
        ScenarioSectionBudget,
        SectionPlacement,
        TermSectionMeeting,
        TimetableScenario,
    )
    from core.services.timetable_autoplace import auto_place_board
    from core.services.timetable_exact_rooming import build_meeting_demands
    from core.services.timetable_optimizer_v2 import (
        build_section_states_for_scenario,
        persist_section_states_to_scenario,
    )

    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="2", gender="M", programs=["AI"]
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="T2", nominal_term=2, program="AI"
    )
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        programme_term=2,
        course_code=code,
        course_key=key,
        credit_hours=4,
        total_demand=90,
        planned_sections=4,
        max_per_section=25,
    )
    auto_place_board(board.pk)
    states = build_section_states_for_scenario(scenario.pk)
    assert len(states) == 4
    policy = get_course_meeting_policy("ENG102")
    assert all(policy.accepts(s.meetings) for s in states)
    assert all(
        s.room_type_required == "lecture" and not s.uses_shared_room and not s.delivery_is_online
        for s in states
    )
    assert SectionPlacement.objects.filter(board=board).count() == 16
    assert build_meeting_demands(scenario.pk, {}) == []
    section = states[0]
    old_ids = set(
        SectionPlacement.objects.filter(term_section__section="S1", board=board).values_list(
            "pk", flat=True
        )
    )
    alternate = next(p for p in policy.patterns if p.pattern_id != section.pattern_id)
    section.meetings = [
        SectionMeeting(("SUN", "MON", "TUE", "WED", "THU").index(m.day), m.start_min, m.end_min)
        for m in alternate.meetings
    ]
    persist_section_states_to_scenario(scenario.pk, {s.section_id: s for s in states})
    assert (
        set(
            SectionPlacement.objects.filter(term_section__section="S1", board=board).values_list(
                "pk", flat=True
            )
        )
        == old_ids
    )
    assert policy.accepts(
        TermSectionMeeting.objects.filter(
            term_section__section="S1", term_section__scenario=scenario
        )
    )

    from core.services.timetable_board_persistence import reset_scenario

    first = SectionPlacement.objects.filter(board=board, term_section__section="S1").first()
    first.is_locked = True
    first.save(update_fields=["is_locked"])
    reset_scenario(scenario.pk, keep_locked=True)
    survivors = list(SectionPlacement.objects.filter(board=board))
    assert len(survivors) == 4
    assert {p.pk for p in survivors} == old_ids
    assert policy.accepts(survivors)
    assert policy.accepts(TermSectionMeeting.objects.filter(term_section=first.term_section))


@pytest.mark.django_db
def test_mirrored_board_copies_have_one_schedule_and_switch_together():
    from core.models import (
        DeliveryBoard,
        SectionPlacement,
        TermSection,
        TermSectionMeeting,
        TimetableScenario,
    )
    from core.services.timetable_optimizer_v2 import (
        build_section_states_for_scenario,
        persist_section_states_to_scenario,
    )

    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="2", gender="M", programs=["AI", "DS"]
    )
    boards = [
        DeliveryBoard.objects.create(scenario=scenario, label=code, nominal_term=2, program=code)
        for code in ("AI", "DS")
    ]
    section = TermSection.objects.create(
        scenario=scenario,
        course_code="ENG102",
        course_number="ENG102",
        course_key="ENG102::ENGLISH",
        section="S1",
        source_tag="tw_auto",
    )
    policy = get_course_meeting_policy("ENG102")
    for meeting in policy.patterns[0].to_dict()["meetings"]:
        for board in boards:
            SectionPlacement.objects.create(
                board=board,
                term_section=section,
                day=meeting["day"],
                start_time=meeting["start"],
                end_time=meeting["end"],
                room=board.program,
            )
        TermSectionMeeting.objects.create(
            term_section=section,
            day=meeting["day"],
            start_time=meeting["start"],
            end_time=meeting["end"],
            instructor="English teacher",
            building="English",
        )
    original_ids = set(SectionPlacement.objects.values_list("pk", flat=True))
    states = build_section_states_for_scenario(scenario.pk)
    assert len(states) == 1 and len(states[0].meetings) == 4
    states[0].meetings = [SectionMeeting(i, 780, 960) for i in (0, 1, 3, 4)]
    persist_section_states_to_scenario(scenario.pk, {states[0].section_id: states[0]})
    assert set(SectionPlacement.objects.values_list("pk", flat=True)) == original_ids
    for board in boards:
        rows = list(SectionPlacement.objects.filter(board=board))
        assert policy.pattern_for(rows).pattern_id.endswith("PM")
        assert {row.room for row in rows} == {board.program}
    assert policy.pattern_for(
        TermSectionMeeting.objects.filter(term_section=section)
    ).pattern_id.endswith("PM")
    assert set(TermSectionMeeting.objects.values_list("instructor", "building")) == {
        ("English teacher", "English")
    }

    # A partial copy must not become a plausible four-meeting state by deduping.
    SectionPlacement.objects.filter(board=boards[1], day="THU").delete()
    before = list(SectionPlacement.objects.values())
    with pytest.raises(ValueError, match="board copies"):
        build_section_states_for_scenario(scenario.pk)
    with pytest.raises(ValueError, match="board copies"):
        persist_section_states_to_scenario(scenario.pk, {states[0].section_id: states[0]})
    assert list(SectionPlacement.objects.values()) == before


@pytest.mark.django_db
def test_intensive_english_remains_on_campus_when_requirement_is_marked_online():
    from core.models import (
        DeliveryBoard,
        ProgrammeRequirement,
        ScenarioSectionBudget,
        TimetableScenario,
    )
    from core.services.timetable_autoplace import auto_place_board
    from core.services.timetable_online import OnlineCourseLookup
    from core.services.timetable_optimisation_policy import instructor_schedule_metrics
    from core.services.timetable_optimizer_v2 import build_section_states_for_scenario
    from core.services.timetable_solver import solve_board

    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="1", gender="M", programs=["AI"]
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="T1", nominal_term=1, program="AI"
    )
    for code in ("ENG101", "ENG102", "GS101"):
        ProgrammeRequirement.objects.create(
            program="AI",
            course_code=code,
            course_name=code,
            programme_term=1,
            credit_hours=4,
            is_online=True,
        )
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        programme_term=1,
        course_code="ENG101",
        course_key="ENG101::ENGLISH",
        credit_hours=4,
        total_demand=25,
        planned_sections=1,
        max_per_section=25,
    )
    assert OnlineCourseLookup().codes_for_board(board) == {"GS101"}
    result = solve_board(board.pk, time_limit_seconds=2)
    assert result["placed"] == 1 and result["placements"][0]["is_online"] is False
    auto_place_board(board.pk)
    state = build_section_states_for_scenario(scenario.pk)[0]
    assert not state.is_online and not state.delivery_is_online and not state.uses_shared_room
    metrics = instructor_schedule_metrics([state], {state.section_id: frozenset({1})})
    assert metrics["working_days"] == 4


@pytest.mark.django_db
@pytest.mark.parametrize("shared_board", [False, True])
def test_english_room_metadata_does_not_consume_an_ordinary_courses_room(shared_board):
    from core.models import DeliveryBoard, Room, SectionPlacement, TermSection, TimetableScenario
    from core.services.timetable_optimizer_v2 import (
        build_room_state_for_scenario,
        build_section_states_for_scenario,
    )
    from core.services.timetable_rooming import assign_rooms_to_board, simulate_buffer_impact

    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="1", gender="M", programs=["AI"]
    )
    english_board = DeliveryBoard.objects.create(
        scenario=scenario, label="English", nominal_term=1, program="AI"
    )
    ordinary_board = (
        english_board
        if shared_board
        else DeliveryBoard.objects.create(
            scenario=scenario, label="Ordinary", nominal_term=1, program="AI"
        )
    )
    Room.objects.create(
        room_code="AI-R1", capacity=60, room_type="lecture", department="AI", section="M"
    )
    english = TermSection.objects.create(
        scenario=scenario,
        course_code="ENG101",
        course_key="ENG101",
        course_number="ENG101",
        section="S1",
        source_tag="tw_auto",
    )
    ordinary = TermSection.objects.create(
        scenario=scenario,
        course_code="AI123",
        course_key="AI123",
        course_number="AI123",
        section="S1",
        source_tag="tw_auto",
    )
    for m in get_course_meeting_policy("ENG101").patterns[0].to_dict()["meetings"]:
        SectionPlacement.objects.create(
            board=english_board,
            term_section=english,
            day=m["day"],
            start_time=m["start"],
            end_time=m["end"],
            room="AI-R1",
        )
    physical = SectionPlacement.objects.create(
        board=ordinary_board,
        term_section=ordinary,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="",
    )
    states = build_section_states_for_scenario(scenario.pk)
    assert next(s for s in states if s.course_code == "ENG101").assigned_room_id is None
    _, occupancies, _ = build_room_state_for_scenario(scenario.pk)
    assert not any(o.assigned_section_ids for o in occupancies.values())
    simulation = simulate_buffer_impact(ordinary_board.pk, buffers=[1.0])
    assert simulation["results"][0]["assigned"] == 1
    assert simulation["results"][0]["unassigned"] == 0
    result = assign_rooms_to_board(ordinary_board.pk)
    physical.refresh_from_db()
    assert physical.room == "AI-R1"
    assert result["assigned"] == 1 and result["unassigned"] == 0

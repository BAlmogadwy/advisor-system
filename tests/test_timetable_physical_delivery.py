"""Physical delivery and teaching minutes cannot depend on gap-score flags."""

from dataclasses import replace

import pytest
from django.test import override_settings

from core.services.course_identity import course_display_code


@pytest.mark.parametrize(
    ("key", "code", "number", "expected"),
    [
        ("AI221::NAME", "AI", "221", "AI221"),
        ("", "AI", "221", "AI221"),
        ("", "AI221", "AI221", "AI221"),
        ("", "AI", "AI221", "AI221"),
        ("C1S1", "C1", "C1", "C1"),
        (" AI221::OTHER ", "IGNORED", "0", "AI221"),
    ],
)
def test_course_display_code_resolves_generated_and_imported_rows(key, code, number, expected):
    assert course_display_code(key, code, number) == expected


def remote_board(*, clash=False, legal_domain=True):
    from core.models import (
        CourseInstructor,
        DeliveryBoard,
        Instructor,
        ProgrammeRequirement,
        SectionPlacement,
        TermSection,
        TermSectionMeeting,
        TimetableScenario,
    )

    slots = [{"start": "09:00", "end": "10:15"}]
    if legal_domain:
        slots += [
            {"start": "15:00", "end": "16:40", "online_only": True},
            {"start": "17:00", "end": "18:00", "online_only": True},
        ]
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="physical remote",
        gender="M",
        programs=["AI"],
        slot_config=slots,
        lab_slot_config=slots,
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="T1", nominal_term=1, program="AI"
    )
    instructor = Instructor.objects.create(full_name="Remote Teacher", normalised_name="remote")
    for index in range(2):
        code = f"AI90{index}"
        ProgrammeRequirement.objects.create(
            program="AI",
            course_code=code,
            course_name=f"Remote {index}",
            type="core",
            programme_term=1,
            credit_hours=1,
            is_online=True,
        )
        ts = TermSection.objects.create(
            scenario=scenario,
            course_key=f"{code}::REMOTE_{index}",
            course_code="AI",
            course_number=f"90{index}",
            course_name=f"Remote {index}",
            section="S1",
            available_capacity=30,
        )
        CourseInstructor.objects.create(
            program="AI", course_code=code, section="M", instructor=instructor, role="primary"
        )
        start, end = ("09:00", "10:00") if index == 0 or clash else ("11:00", "12:00")
        SectionPlacement.objects.create(
            board=board,
            term_section=ts,
            day="MON",
            start_time=start,
            end_time=end,
            is_locked=index == 0,
        )
        TermSectionMeeting.objects.create(
            term_section=ts, day="MON", start_time=start, end_time=end
        )
    return scenario, instructor


@pytest.mark.django_db
@override_settings(TIMETABLE_ONLINE_GAP_EXCLUSION_ENABLED=False)
def test_physical_online_marker_and_teacher_links_work_with_legacy_gap_flag_off():
    from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
    from core.services.timetable_optimisation_policy import instructor_schedule_metrics
    from core.services.timetable_optimizer_v2 import (
        build_section_instructor_map_for_scenario,
        build_section_states_for_scenario,
    )
    from core.services.timetable_pr4_instructor import build_section_instructor_ids

    scenario, instructor = remote_board()
    states = build_section_states_for_scenario(scenario.id)
    assert len(states) == 2
    assert all(s.delivery_is_online and not s.is_online for s in states)
    mapping = build_section_instructor_map_for_scenario(scenario.id)
    assert mapping == {s.section_id: frozenset({instructor.id}) for s in states}
    assert build_section_instructor_ids(scenario) == {
        f"AI90{i}::REMOTE_{i}|S1": {instructor.id} for i in range(2)
    }
    assert instructor_schedule_metrics(states, mapping)["working_days"] == 0
    physical = evaluate_generated_timetable_candidate("physical", states, {}, {}, mapping)
    legacy = evaluate_generated_timetable_candidate(
        "legacy", [replace(s, delivery_is_online=False) for s in states], {}, {}, mapping
    )
    assert physical.lexicographic_score == legacy.lexicographic_score


@pytest.mark.django_db
@pytest.mark.parametrize("repair", ["cap", "clash"])
@pytest.mark.parametrize("legal_domain", [True, False], ids=["remote-domain", "no-remote-domain"])
@override_settings(
    TIMETABLE_ONLINE_GAP_EXCLUSION_ENABLED=False,
    TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True,
    TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True,
    TIMETABLE_INSTRUCTOR_DAILY_CAP=1,
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
)
def test_mandatory_repairs_preserve_remote_delivery_and_custom_duration(repair, legal_domain):
    from core.models import SectionPlacement, TermSectionMeeting
    from core.services.timetable_instructor_cap_repair import (
        repair_instructor_clashes,
        repair_instructor_daily_overloads,
    )

    scenario, _instructor = remote_board(clash=repair == "clash", legal_domain=legal_domain)
    before = list(SectionPlacement.objects.filter(board__scenario=scenario).order_by("pk").values())
    function = repair_instructor_daily_overloads if repair == "cap" else repair_instructor_clashes
    result = function(scenario.id)
    placements = list(SectionPlacement.objects.filter(board__scenario=scenario).order_by("pk"))
    assert len(placements) == 2
    assert not result["unplaced"]
    if legal_domain:
        assert len(result["repaired"]) == 1
        assert (placements[1].day, placements[1].start_time, placements[1].end_time) == (
            "SUN",
            "17:00",
            "18:00",
        )
        meeting = TermSectionMeeting.objects.get(term_section=placements[1].term_section)
        assert (meeting.day, meeting.start_time, meeting.end_time) == ("SUN", "17:00", "18:00")
        assert meeting.instructor == "Remote Teacher"
    else:
        assert not result["repaired"]
        assert (
            list(SectionPlacement.objects.filter(board__scenario=scenario).order_by("pk").values())
            == before
        )
        assert result["locked_blocked"][0]["reason"] == "no_online_slot_preserving_duration"
        assert result["remaining_violations" if repair == "cap" else "remaining_clashes"] == 1


@pytest.mark.django_db
@pytest.mark.parametrize("repair", ["cap", "clash"])
@override_settings(
    TIMETABLE_INSTRUCTOR_LINKS_ENABLED=True,
    TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True,
    TIMETABLE_INSTRUCTOR_DAILY_CAP=1,
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
)
def test_generated_offerings_with_same_prefix_can_use_parallel_repair_slot(repair):
    from core.models import (
        CourseInstructor,
        DeliveryBoard,
        Instructor,
        SectionPlacement,
        TermSection,
        TermSectionMeeting,
        TimetableScenario,
    )
    from core.services.timetable_instructor_cap_repair import (
        _creates_board_interval_overlap,
        repair_instructor_clashes,
        repair_instructor_daily_overloads,
    )

    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="split offering repairs",
        gender="M",
        programs=["AI"],
        slot_config=[{"start": "09:00", "end": "10:15"}],
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="T1", nominal_term=1, program="AI"
    )
    teachers = [
        Instructor.objects.create(full_name=f"Teacher {i}", normalised_name=f"teacher {i}")
        for i in range(2)
    ]
    sections = {}
    for number, teacher in (("221", teachers[0]), ("331", teachers[0]), ("999", teachers[1])):
        sections[number] = TermSection.objects.create(
            scenario=scenario,
            course_key=f"AI{number}::NAME_{number}",
            course_code="AI",
            course_number=number,
            course_name=f"Name {number}",
            section="S1",
            available_capacity=30,
        )
        CourseInstructor.objects.create(
            program="AI", course_code=f"AI{number}", section="M", instructor=teacher, role="primary"
        )
    plans = [("221", "MON", "09:00", "10:15", True)]
    plans.append(
        (
            "331",
            "MON",
            "09:00" if repair == "clash" else "10:30",
            "10:15" if repair == "clash" else "11:45",
            False,
        )
    )
    plans.extend(("999", day, "09:00", "10:15", True) for day in ("SUN", "TUE", "WED", "THU"))
    for number, day, start, end, locked in plans:
        SectionPlacement.objects.create(
            board=board,
            term_section=sections[number],
            day=day,
            start_time=start,
            end_time=end,
            is_locked=locked,
        )
        TermSectionMeeting.objects.create(
            term_section=sections[number], day=day, start_time=start, end_time=end
        )
    mover = SectionPlacement.objects.get(term_section=sections["331"])
    occupied = SectionPlacement.objects.get(term_section=sections["999"], day="SUN")
    # Sharing the department prefix is not sharing an offering or instructor.
    assert not _creates_board_interval_overlap(
        mover, "SUN", 540, 615, set(), [mover, occupied], lambda _p: set()
    )
    sibling = SectionPlacement(
        board=board, term_section=sections["331"], day="SUN", start_time="09:00", end_time="10:15"
    )
    assert _creates_board_interval_overlap(
        mover, "SUN", 540, 615, set(), [mover, sibling], lambda _p: set()
    )
    function = repair_instructor_daily_overloads if repair == "cap" else repair_instructor_clashes
    result = function(scenario.id)
    assert len(result["repaired"]) == 1
    assert result["unplaced"] == []
    mover.refresh_from_db()
    assert (mover.day, mover.start_time, mover.end_time) == ("SUN", "09:00", "10:15")
    assert SectionPlacement.objects.filter(board=board).count() == 6

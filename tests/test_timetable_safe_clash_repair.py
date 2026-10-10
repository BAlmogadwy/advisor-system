"""Clash repair may coordinate meetings without extending teachers' weeks."""

from types import SimpleNamespace

import pytest

from core.models import (
    CourseInstructor,
    DeliveryBoard,
    Instructor,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services import timetable_instructor_cap_repair as repair
from core.services.timetable_assignment_models import RiskTier, StudentProfile
from core.services.timetable_constraints import count_instructor_clashes
from core.services.timetable_optimizer_v2 import (
    build_section_instructor_map_for_scenario,
    build_section_states_for_scenario,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def make_board(settings, monkeypatch):
    settings.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_LINKS_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    monkeypatch.setattr(repair, "assign_rooms_to_board", lambda *_args, **_kwargs: None)

    def build(*, direct=False, locked=False):
        slots = (
            [("09:00", "10:15"), ("10:15", "11:30")]
            if direct
            else [
                ("09:00", "10:15"),
                ("10:30", "11:45"),
                ("10:50", "12:05"),
                ("13:00", "14:15"),
                ("14:30", "15:45"),
                ("16:00", "17:15"),
            ]
        )
        scenario = TimetableScenario.objects.create(
            academic_year="1448",
            term="1",
            name="Safe clash",
            gender="M",
            programs=["AI"],
            slot_config=[{"start": s, "end": e} for s, e in slots],
        )
        board = DeliveryBoard.objects.create(
            scenario=scenario, label="AI", program="AI", nominal_term=1
        )
        teacher = Instructor.objects.create(full_name="Teacher", normalised_name="teacher")
        times = [("09:00", "10:15")] * 2 if direct else slots[1:4]
        rows = []
        for i, (start, end) in enumerate(times):
            code = f"AI10{i}"
            CourseInstructor.objects.create(
                program="AI",
                course_code=code,
                section="M",
                instructor=teacher,
                role="primary",
            )
            section = TermSection.objects.create(
                scenario=scenario,
                course_code=code,
                course_key=code,
                section="S1",
                available_capacity=30,
            )
            rows.append(
                SectionPlacement.objects.create(
                    board=board,
                    term_section=section,
                    day="SUN",
                    start_time=start,
                    end_time=end,
                    is_locked=locked and i != 1,
                )
            )
            TermSectionMeeting.objects.create(
                term_section=section,
                day="SUN",
                start_time=start,
                end_time=end,
                instructor="Teacher",
                building="B1",
                floor_wing="East",
                created_at="original",
            )
        return scenario, rows

    return build


def clashes(scenario):
    states = build_section_states_for_scenario(scenario.id)
    return count_instructor_clashes(
        {s.section_id: s for s in states},
        build_section_instructor_map_for_scenario(scenario.id),
    )


def test_same_day_direct_repair_preserves_teacher_days_and_idle(make_board):
    scenario, _rows = make_board(direct=True)
    report = repair.repair_instructor_clashes(scenario.id)
    assert clashes(scenario) == 0
    assert report["instructor_before"] == report["instructor_after"]
    assert report["coordinated_repairs"] == 0
    assert report["repaired"][0]["instructor_safe"]
    assert {p.day for p in SectionPlacement.objects.filter(board__scenario=scenario)} == {"SUN"}


def test_two_meetings_repair_overlap_and_reduce_union_idle_without_adding_day(make_board):
    scenario, _rows = make_board()
    original = {m.id: m for m in TermSectionMeeting.objects.all()}
    report = repair.repair_instructor_clashes(scenario.id)
    assert clashes(scenario) == 0
    assert report["coordinated_repairs"] == 1
    assert (
        report["instructor_before"]["working_days"]
        == report["instructor_after"]["working_days"]
        == 1
    )
    assert report["instructor_before"]["idle_minutes"] == 55
    assert report["instructor_after"]["idle_minutes"] == 30
    assert all(row["instructor_safe"] for row in report["repaired"])
    for meeting in TermSectionMeeting.objects.all():
        assert meeting.id in original
        for field in ("building", "floor_wing", "created_at", "instructor"):
            assert getattr(meeting, field) == getattr(original[meeting.id], field)


def test_coordinated_repair_does_not_move_locked_partner(make_board):
    scenario, rows = make_board(locked=True)
    before = list(SectionPlacement.objects.filter(is_locked=True).values())
    report = repair.repair_instructor_clashes(scenario.id)
    assert list(SectionPlacement.objects.filter(is_locked=True).values()) == before
    assert not report["coordinated_repairs"]
    assert not report["repaired"][0]["instructor_safe"]  # Existing mandatory fallback remains.
    assert SectionPlacement.objects.get(pk=rows[0].pk).start_time == "10:30"


def test_safe_pair_rejects_hard_student_prefix_regression(make_board, monkeypatch):
    scenario, _rows = make_board()
    monkeypatch.setattr(
        "core.services.timetable_optimizer_v2.build_student_profiles_for_scenario",
        lambda _sid: {"s": object()},
    )
    monkeypatch.setattr(
        "core.services.timetable_optimizer_v2.build_course_rigidity_for_scenario", lambda _sid: {}
    )

    def evaluate(_label, states, *_args, **_kwargs):
        moved_late = any(m.day == 0 and m.start_min >= 870 for s in states for m in s.meetings)
        return SimpleNamespace(
            lexicographic_score=(int(moved_late), 0, 0, 0, 0, 0, 0, 0, 0),
            assignment_states={},
        )

    monkeypatch.setattr(
        "core.services.timetable_candidate_eval.evaluate_generated_timetable_candidate", evaluate
    )
    report = repair.repair_instructor_clashes(scenario.id)
    assert not report["coordinated_repairs"]
    assert not report["repaired"][0]["instructor_safe"]
    assert report["student_score_after"][:4] == [0, 0, 0, 0]


def test_pair_persistence_error_rolls_back_both_placements_and_original_meeting_ids(
    make_board, monkeypatch
):
    scenario, _rows = make_board()
    placements = list(SectionPlacement.objects.order_by("id").values())
    meetings = list(TermSectionMeeting.objects.order_by("id").values())
    relocate = repair._relocate
    calls = 0

    def fail_second(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second write failed")
        relocate(*args)

    monkeypatch.setattr(repair, "_relocate", fail_second)
    with pytest.raises(RuntimeError, match="second write"):
        repair.repair_instructor_clashes(scenario.id)
    assert list(SectionPlacement.objects.order_by("id").values()) == placements
    assert list(TermSectionMeeting.objects.order_by("id").values()) == meetings


def test_third_unlinked_meeting_move_preserves_high_risk_core_seat(make_board, monkeypatch):
    scenario, rows = make_board()
    blocker = TermSection.objects.create(
        scenario=scenario,
        course_code="BLOCK",
        course_key="BLOCK",
        section="S1",
        available_capacity=30,
    )
    SectionPlacement.objects.create(
        board=rows[0].board,
        term_section=blocker,
        day="SUN",
        start_time="14:45",
        end_time="16:25",
    )
    TermSectionMeeting.objects.create(
        term_section=blocker,
        day="SUN",
        start_time="14:45",
        end_time="16:25",
    )
    profiles = {"s": StudentProfile("s", "AI", ["AI100", "BLOCK"], RiskTier.A, 0)}
    monkeypatch.setattr(
        "core.services.timetable_optimizer_v2.build_student_profiles_for_scenario",
        lambda _sid: profiles,
    )
    monkeypatch.setattr(
        "core.services.timetable_optimizer_v2.build_course_rigidity_for_scenario", lambda _sid: {}
    )
    monkeypatch.setattr(
        "core.services.timetable_optimizer_v2.build_course_tier_map_for_scenario",
        lambda _sid: {"AI100": "T1", "BLOCK": "T1"},
    )
    report = repair.repair_instructor_clashes(scenario.id)
    assert report["coordinated_repairs"] == 1
    assert len(report["repaired"]) == 3
    assert report["student_score_before"][:4] == report["student_score_after"][:4] == [0, 0, 0, 0]
    assert (
        report["instructor_after"]["working_days"]
        == report["instructor_before"]["working_days"]
        == 1
    )
    assert report["instructor_after"]["idle_minutes"] < report["instructor_before"]["idle_minutes"]
    assert clashes(scenario) == 0
    assert all(row["instructor_safe"] for row in report["repaired"])
    moved = SectionPlacement.objects.get(term_section=blocker)
    assert repair._to_min(moved.end_time) - repair._to_min(moved.start_time) == 100


def test_locked_shared_physical_meeting_is_not_moved_through_unlocked_board_row(make_board):
    scenario, rows = make_board(locked=True)
    rows[1].is_locked = True
    rows[1].save(update_fields=["is_locked"])
    second_board = DeliveryBoard.objects.create(
        scenario=scenario,
        label="shared",
        program="AI",
        nominal_term=2,
    )
    SectionPlacement.objects.create(
        board=second_board,
        term_section=rows[1].term_section,
        day="SUN",
        start_time=rows[1].start_time,
        end_time=rows[1].end_time,
        is_locked=False,
    )
    before = list(SectionPlacement.objects.order_by("id").values())
    meetings = list(TermSectionMeeting.objects.order_by("id").values())
    report = repair.repair_instructor_clashes(scenario.id)
    assert report["repaired"] == []
    assert list(SectionPlacement.objects.order_by("id").values()) == before
    assert list(TermSectionMeeting.objects.order_by("id").values()) == meetings


def test_relocation_matches_full_physical_interval_and_preserves_other_meeting(make_board):
    _scenario, rows = make_board(direct=True)
    other = TermSectionMeeting.objects.create(
        term_section=rows[1].term_section,
        day="SUN",
        start_time="09:00",
        end_time="10:40",
        instructor="Different physical meeting",
        building="untouched",
    )
    original = TermSectionMeeting.objects.filter(pk=other.pk).values().get()
    repair._relocate(rows[1], "MON", "10:15", "11:30")
    assert TermSectionMeeting.objects.filter(pk=other.pk).values().get() == original


def test_unlocked_shared_physical_group_does_not_split_in_mandatory_fallback(make_board):
    scenario, rows = make_board(locked=True)
    second_board = DeliveryBoard.objects.create(
        scenario=scenario,
        label="shared",
        program="AI",
        nominal_term=2,
    )
    SectionPlacement.objects.create(
        board=second_board,
        term_section=rows[1].term_section,
        day="SUN",
        start_time=rows[1].start_time,
        end_time=rows[1].end_time,
    )
    before = list(SectionPlacement.objects.order_by("id").values())
    meetings = list(TermSectionMeeting.objects.order_by("id").values())
    report = repair.repair_instructor_clashes(scenario.id)
    assert len(report["repaired"]) == 1
    assert report["remaining_clashes"] == 0
    after = list(SectionPlacement.objects.order_by("id").values())
    assert {row["id"] for row in after} == {row["id"] for row in before}
    assert [row for row in after if row["is_locked"]] == [row for row in before if row["is_locked"]]
    shared = [row for row in after if row["term_section_id"] == rows[1].term_section_id]
    assert (
        len({(row["day"], row["start_time"], row["end_time"], row["room"]) for row in shared}) == 1
    )
    canonical = list(TermSectionMeeting.objects.order_by("id").values())
    assert {row["id"] for row in canonical} == {row["id"] for row in meetings}
    moved_meeting = next(
        row for row in canonical if row["term_section_id"] == rows[1].term_section_id
    )
    assert (moved_meeting["day"], moved_meeting["start_time"], moved_meeting["end_time"]) == (
        shared[0]["day"],
        shared[0]["start_time"],
        shared[0]["end_time"],
    )

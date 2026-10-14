"""Persistence recovery tests use real rows and independently rebuilt outcomes."""

import pytest

from core.models import (
    DeliveryBoard,
    Room,
    ScenarioSectionBudget,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services import timetable_secondary_recovery_runner as runner
from core.services.timetable_assignment_models import CanonicalPattern, RiskTier, StudentProfile
from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
from core.services.timetable_optimizer_v2 import build_section_states_for_scenario

pytestmark = pytest.mark.django_db


@pytest.fixture
def board(settings, monkeypatch):
    settings.TIMETABLE_TIERED_OBJECTIVE_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_SECONDARY_RECOVERY_ENABLED = True
    settings.TIMETABLE_SECONDARY_RECOVERY_SECONDS = 2
    settings.TIMETABLE_SECONDARY_RECOVERY_ITERATIONS = 2
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="secondary persistence",
        gender="M",
        programs=["CS"],
    )
    delivery = DeliveryBoard.objects.create(
        scenario=scenario,
        program="CS",
        nominal_term=1,
        label="CS",
    )
    Room.objects.create(
        room_code="R1", capacity=40, room_type="lecture", section="M", department="CS"
    )
    rows = []
    for course, start, end, locked in [
        ("CORE", "09:00", "10:15", True),
        ("SOFT", "10:30", "11:45", False),
    ]:
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_key=course,
            course_code=course,
            department="CS",
            credit_hours=3,
            total_demand=5,
            planned_sections=1,
            max_per_section=30,
            programme_term=1,
        )
        section = TermSection.objects.create(
            scenario=scenario,
            course_key=course,
            course_code=course,
            section="M1",
            available_capacity=30,
        )
        rows.append(
            SectionPlacement.objects.create(
                board=delivery,
                term_section=section,
                day="SUN",
                start_time=start,
                end_time=end,
                room="R1" if locked else "",
                is_locked=locked,
            )
        )
        TermSectionMeeting.objects.create(
            term_section=section,
            day="SUN",
            start_time=start,
            end_time=end,
            room="R1" if locked else "",
            instructor=f"Recorded {course}",
            building="B7",
            floor_wing="East 2",
            created_at="original creation",
            updated_at="original update",
        )
    # The two real 75-minute patterns deliberately provide a tiny exhaustive domain.
    states = build_section_states_for_scenario(scenario.id)
    patterns = [
        CanonicalPattern(
            s.pattern_id,
            "",
            s.meetings,
            s.pattern_family,
            "75",
            False,
            1,
            frozenset({0}),
            "",
        )
        for s in states
    ]
    monkeypatch.setattr(
        "core.services.timetable_optimizer_v2._build_pattern_catalog_for_scenario",
        lambda _sid: {states[0].pattern_family: patterns},
    )
    return scenario, rows


def invoke(scenario, result=None):
    profiles = {"s": StudentProfile("s", "CS", ["CORE", "SOFT"], RiskTier.C, 0)}
    result = {} if result is None else result
    report = runner.run_secondary_recovery_for_scenario(
        scenario.id,
        result,
        profiles,
        {},
        {"CORE_M1": frozenset({7})},
        {"CORE": "T1", "SOFT": "T3"},
    )
    return report, result, profiles


def all_rows(scenario):
    return list(SectionPlacement.objects.filter(board__scenario=scenario).order_by("id").values())


def test_room_only_recovery_preserves_row_identity_creation_audit_and_locked_metadata(board):
    scenario, rows = board
    before = all_rows(scenario)
    meetings_before = list(TermSectionMeeting.objects.order_by("id").values())
    report, result, _profiles = invoke(scenario)
    assert report["applied"]
    assert (report["room_shortages_before"], report["room_shortages_after"]) == (1, 0)
    assert report["soft_unresolved_before"] == report["soft_unresolved_after"] == 0
    assert report["score_before"] == report["score_after"] == result["final_score"]
    after = all_rows(scenario)
    assert after[0] == before[0]
    assert [r["id"] for r in after] == [r["id"] for r in before]
    assert after[1]["created_at"] == before[1]["created_at"]
    assert after[1]["room"] == "R1"
    meetings_after = list(TermSectionMeeting.objects.order_by("id").values())
    assert meetings_after[0] == meetings_before[0]
    assert meetings_after[1] == {**meetings_before[1], "room": "R1"}
    meeting = TermSectionMeeting.objects.get(term_section=rows[1].term_section)
    assert (meeting.day, meeting.start_time, meeting.end_time) == ("SUN", "10:30", "11:45")


def test_persisted_seat_and_room_recovery_matches_independent_canonical_evaluation(board):
    scenario, rows = board
    SectionPlacement.objects.filter(pk=rows[1].pk).update(start_time="09:00", end_time="10:15")
    TermSectionMeeting.objects.filter(term_section=rows[1].term_section).update(
        start_time="09:00",
        end_time="10:15",
    )
    report, result, profiles = invoke(scenario)
    assert report["applied"]
    assert (report["soft_unresolved_before"], report["soft_unresolved_after"]) == (1, 0)
    persisted = evaluate_generated_timetable_candidate(
        "independent",
        build_section_states_for_scenario(scenario.id),
        profiles,
        {},
        {"CORE_M1": frozenset({7})},
        {"CORE": "T1", "SOFT": "T3"},
    )
    assert list(persisted.lexicographic_score) == report["score_after"] == result["final_score"]
    assert report["instructor_before"] == report["instructor_after"]
    rows[1].refresh_from_db()
    assert (rows[1].day, rows[1].start_time, rows[1].end_time, rows[1].room) == (
        "SUN",
        "10:30",
        "11:45",
        "R1",
    )


def test_failed_persistence_rolls_back_only_secondary_changes_after_mandatory_repair(
    board, monkeypatch
):
    scenario, rows = board
    # Treat this legal relocation as a preceding mandatory repair; the optional
    # pass must roll back to this exact state rather than an earlier run snapshot.
    SectionPlacement.objects.filter(pk=rows[1].pk).update(day="MON")
    TermSectionMeeting.objects.filter(term_section=rows[1].term_section).update(day="MON")
    before = all_rows(scenario)
    meetings_before = list(TermSectionMeeting.objects.order_by("id").values())
    persist = runner._persist_exact_room_result

    def corrupt(*args):
        changed = persist(*args)
        SectionPlacement.objects.filter(pk=rows[1].pk).update(end_time="12:00")
        return changed

    monkeypatch.setattr(runner, "_persist_exact_room_result", corrupt)
    result = {"mandatory_repair": {"moved": 1}}
    report, result, _profiles = invoke(scenario, result)
    assert not report["applied"]
    assert report["stop_reason"] == "persistence_guard_rejected"
    assert all_rows(scenario) == before
    assert list(TermSectionMeeting.objects.order_by("id").values()) == meetings_before
    assert result == {"mandatory_repair": {"moved": 1}}
    assert report["score_after"] == report["score_before"]
    assert report["room_shortages_after"] == report["room_shortages_before"]


def test_concurrent_board_edit_is_not_overwritten_and_candidate_gain_is_not_claimed(
    board, monkeypatch
):
    scenario, rows = board
    recover = runner.recover_secondary_quality

    def editing_search(**kwargs):
        candidate = recover(**kwargs)
        SectionPlacement.objects.filter(pk=rows[1].pk).update(day="WED")
        return candidate

    monkeypatch.setattr(runner, "recover_secondary_quality", editing_search)
    report, result, _profiles = invoke(scenario)
    assert not report["applied"]
    assert report["stop_reason"] == "board_changed_during_search"
    assert report["room_shortages_after"] == report["room_shortages_before"] == 1
    assert report["accepted_moves"] == []
    assert result == {}
    rows[1].refresh_from_db()
    assert rows[1].day == "WED"
    assert rows[1].room == ""


def test_concurrent_meeting_metadata_edit_is_preserved(board, monkeypatch):
    scenario, rows = board
    recover = runner.recover_secondary_quality

    def editing_search(**kwargs):
        candidate = recover(**kwargs)
        TermSectionMeeting.objects.filter(term_section=rows[0].term_section).update(
            instructor="Registrar correction",
        )
        return candidate

    monkeypatch.setattr(runner, "recover_secondary_quality", editing_search)
    report, result, _profiles = invoke(scenario)
    assert not report["applied"]
    assert report["stop_reason"] == "board_changed_during_search"
    assert result == {}
    assert TermSectionMeeting.objects.get(term_section=rows[0].term_section).instructor == (
        "Registrar correction"
    )
    rows[1].refresh_from_db()
    assert rows[1].room == ""


def test_shared_meeting_room_merge_is_rejected_without_deleting_distinct_records(board):
    scenario, rows = board
    original = TermSectionMeeting.objects.get(term_section=rows[1].term_section)
    TermSectionMeeting.objects.create(
        term_section=original.term_section,
        day=original.day,
        start_time=original.start_time,
        end_time=original.end_time,
        instructor=original.instructor,
        room="Legacy shared room",
        building="Different source",
    )
    before = all_rows(scenario)
    meetings_before = list(TermSectionMeeting.objects.order_by("id").values())
    report, result, _profiles = invoke(scenario)
    assert not report["applied"]
    assert report["stop_reason"] == "meeting_snapshot_unavailable"
    assert "merge distinct" in report["error"]
    assert all_rows(scenario) == before
    assert list(TermSectionMeeting.objects.order_by("id").values()) == meetings_before
    assert result == {}


@pytest.mark.parametrize("failure_call", [1, 2], ids=["before-search", "after-persistence"])
def test_unavailable_room_snapshot_keeps_preceding_repair_and_metadata(
    board, monkeypatch, failure_call
):
    from core.services.timetable_exact_rooming import ExactRoomingError
    from core.services.timetable_recovery_rooming import build_recovery_room_evaluator

    scenario, rows = board
    SectionPlacement.objects.filter(pk=rows[1].pk).update(day="MON")
    TermSectionMeeting.objects.filter(term_section=rows[1].term_section).update(day="MON")
    before = all_rows(scenario)
    meetings_before = list(TermSectionMeeting.objects.order_by("id").values())
    calls = 0

    def unavailable(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise ExactRoomingError("Inventory snapshot unavailable")
        return build_recovery_room_evaluator(*args, **kwargs)

    monkeypatch.setattr(
        "core.services.timetable_recovery_rooming.build_recovery_room_evaluator",
        unavailable,
    )
    report, result, _profiles = invoke(scenario, {"mandatory_repair": {"moved": 1}})
    assert not report["applied"]
    assert report["stop_reason"] == (
        "room_snapshot_unavailable" if failure_call == 1 else "recovery_unavailable"
    )
    assert all_rows(scenario) == before
    assert list(TermSectionMeeting.objects.order_by("id").values()) == meetings_before
    assert result == {"mandatory_repair": {"moved": 1}}

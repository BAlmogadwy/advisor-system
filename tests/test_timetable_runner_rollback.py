from __future__ import annotations

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
from core.services import timetable_optimisation_policy as instructor_policy
from core.services import timetable_optimizer_v2 as optimiser
from core.services import timetable_v2_runner as guarded_runner
from core.services.timetable_board_persistence import reset_scenario, snapshot_scenario
from core.services.timetable_v2_runner import run_v2_optimisation_guarded


def _persisted_board(name: str):
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name=name)
    board = DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)
    section = TermSection.objects.create(
        scenario=scenario,
        course_code="AI101",
        course_number="AI101",
        course_key="AI101",
        course_name="AI101",
        section="S1",
        source_tag="test",
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=section,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="R1",
        is_locked=True,
    )
    TermSectionMeeting.objects.create(
        term_section=section,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="R1",
        instructor="Original instructor",
        building="Building A",
        floor_wing="First floor",
    )
    return scenario, board, section


@pytest.mark.django_db
@pytest.mark.parametrize("mode", ["current", "full"])
@pytest.mark.parametrize("failure", ["returned", "raised"])
def test_guarded_runner_restores_persisted_board_after_optimiser_failure(
    monkeypatch, settings, mode, failure
) -> None:
    """A returned error must undo temporary writes just like a raised exception.

    Exercise the real snapshot and restore, including meeting metadata and row
    identity, rather than merely checking that a rollback mock was called.
    """
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = False
    scenario, board, section = _persisted_board("Failing run")
    unrelated, _, _ = _persisted_board("Unrelated board")
    original = snapshot_scenario(scenario.id)
    unrelated_original = snapshot_scenario(unrelated.id)
    returned_error = {"error": "No feasible candidate", "code": "TEST_FAILURE"}
    calls = []

    def mutate_then_fail(**kwargs):
        assert kwargs["scenario_id"] == scenario.id
        calls.append(kwargs)
        reset_scenario(scenario.id, keep_locked=False)
        SectionPlacement.objects.create(
            board=board,
            term_section=section,
            day="THU",
            start_time="12:00",
            end_time="13:15",
            room="R9",
            is_locked=False,
        )
        TermSectionMeeting.objects.create(
            term_section=section,
            day="THU",
            start_time="12:00",
            end_time="13:15",
            room="R9",
            instructor="Temporary instructor",
            building="Building Z",
            floor_wing="Wrong floor",
        )
        assert snapshot_scenario(scenario.id).placements != original.placements
        if failure == "raised":
            raise RuntimeError("Failed after writing temporary candidate")
        return returned_error

    target = "optimise_scenario_timetable_v2" if mode == "full" else "optimise_current_timetable"
    monkeypatch.setattr(optimiser, target, mutate_then_fail)

    result = run_v2_optimisation_guarded(scenario.id, mode=mode)

    assert len(calls) == 1
    if failure == "returned":
        assert result == returned_error
    else:
        assert "Failed after writing temporary candidate" in result["error"]
    restored = snapshot_scenario(scenario.id)
    assert restored.placements == original.placements
    assert restored.meetings == original.meetings
    assert snapshot_scenario(unrelated.id) == unrelated_original


@pytest.mark.django_db
@pytest.mark.parametrize("increase", ["days", "idle", "both"])
def test_current_rejects_mandatory_repairs_that_exceed_saved_instructor_week(
    monkeypatch, settings, increase
) -> None:
    """An explicit repair exception cannot waive the user's saved-week ceiling.

    Exercise the real persistence rollback and distinguish discarded repair
    evidence from the restored board's actual outcome.
    """
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    scenario, _, _ = _persisted_board("Repair exceeds instructor baseline")
    original = snapshot_scenario(scenario.id)
    baseline = [0, 0, 0, 0, 0, 0]
    before = {
        "working_days": 4,
        "idle_minutes": 190,
        "working_days_by_instructor": {"134": 4},
        "idle_minutes_by_instructor": {"134": 190},
        "affected_instructors": 1,
    }
    after = {
        **before,
        "working_days": 5 if increase in ("days", "both") else 4,
        "idle_minutes": 210 if increase in ("idle", "both") else 190,
        "working_days_by_instructor": {"134": 5 if increase in ("days", "both") else 4},
        "idle_minutes_by_instructor": {"134": 210 if increase in ("idle", "both") else 190},
    }
    measurements = iter([before, after])
    monkeypatch.setattr(
        instructor_policy, "instructor_schedule_metrics", lambda *_: next(measurements)
    )
    original_safety_summary = guarded_runner.compute_scenario_safety_summary

    def safety_summary(scenario_id):
        summary = original_safety_summary(scenario_id)
        # The fixture models the original clash as present on MON, repaired by
        # the attempted WED move, and present again after snapshot restoration.
        summary["instructor_clashes_scenario"] = int(
            SectionPlacement.objects.filter(board__scenario_id=scenario_id, day="MON").exists()
        )
        return summary

    monkeypatch.setattr(guarded_runner, "compute_scenario_safety_summary", safety_summary)

    def repair(**_):
        SectionPlacement.objects.filter(board__scenario_id=scenario.id).update(day="WED")
        TermSectionMeeting.objects.filter(term_section__scenario_id=scenario.id).update(day="WED")
        return {
            "baseline_score": baseline,
            "final_score": baseline,
            "instructor_schedule_before_passes": before,
            "instructor_schedule_after_hard_repairs": after,
            "instructor_clash_repair": {
                "enabled": True,
                "detected": [{"instructor_id": 134}],
                "repaired": [{"section": "AI101 S1"}],
                "remaining_clashes": 0,
            },
            "instructor_cap_repair": {"enabled": True, "repaired": []},
            "instructor_compaction": {"enabled": True, "relocations": [{"section": "AI101"}]},
            "secondary_recovery": {"applied": True, "accepted_moves": [{"section_id": "AI101"}]},
        }

    monkeypatch.setattr(optimiser, "optimise_current_timetable", repair)
    result = run_v2_optimisation_guarded(scenario.id, mode="current")

    assert result["safety_blocked"] is True
    assert snapshot_scenario(scenario.id) == original
    assert result["instructor_schedule_metric"]["after"] == before
    assert result["instructor_schedule_metric"]["repair_exceptions"] == []
    assert result["candidate_instructor_schedule"] == after
    assert any(
        row.get("reason") == "instructor_baseline_must_be_preserved"
        for row in result["safety_regression"]["regressions"]
    )
    assert result["instructor_clash_repair"]["repaired"] == []
    assert result["instructor_clash_repair"]["remaining_clashes"] == 1
    assert result["candidate_instructor_clash_repair"]["repaired"]
    assert result["instructor_cap_repair"]["repaired"] == []
    assert result["instructor_compaction"]["relocations"] == []
    assert result["secondary_recovery"]["applied"] is False


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("shared", "cap", "expected"),
    [(False, 0, 1), (True, 0, 1), (True, 1, 0)],
)
def test_rollback_counts_remaining_overloads_on_a_scenario_with_instructor_links(
    monkeypatch, settings, shared, cap, expected
) -> None:
    """A whole-board rollback must report remaining overloads, not crash.

    The rollback path re-counts daily overloads from the restored board. With
    instructor links present (every real scenario), the counter must receive a
    section_id -> state mapping; a bare list raised AttributeError and failed the
    whole job instead of reporting the rejected run. A section shared by two
    boards at one time is one physical session, counted once like the repair.
    """
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAILY_CAP = cap
    scenario, board, section = _persisted_board("Linked rollback")
    if shared:
        other = DeliveryBoard.objects.create(scenario=scenario, label="Term 2", nominal_term=2)
        SectionPlacement.objects.create(
            board=other,
            term_section=section,
            day="MON",
            start_time="09:00",
            end_time="10:15",
            room="R1",
        )
    scenario.gender = "M"
    scenario.programs = ["AI"]
    scenario.save(update_fields=["gender", "programs"])
    teacher = Instructor.objects.create(full_name="Dr Linked", normalised_name="dr linked")
    CourseInstructor.objects.create(
        program="AI", course_code="AI101", section="M", instructor=teacher, role="primary"
    )
    assert optimiser.build_section_instructor_map_for_scenario(scenario.id)
    original = snapshot_scenario(scenario.id)

    def worse(**_):
        SectionPlacement.objects.filter(board__scenario_id=scenario.id).update(day="WED")
        return {
            "baseline_score": [0, 0, 0, 0, 0, 0],
            "final_score": [1, 0, 0, 0, 0, 0],
            "instructor_cap_repair": {"enabled": True, "detected": [], "repaired": []},
        }

    monkeypatch.setattr(optimiser, "optimise_current_timetable", worse)
    result = run_v2_optimisation_guarded(scenario.id, mode="current")

    assert result["safety_blocked"] is True
    assert snapshot_scenario(scenario.id) == original
    assert result["instructor_cap_repair"]["remaining_violations"] == expected
    assert result["instructor_cap_repair"]["rollback_reason"] == "whole_board_rollback"
    assert result["candidate_instructor_cap_repair"]["enabled"] is True

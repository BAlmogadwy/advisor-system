from __future__ import annotations

import pytest

from core.models import (
    BoardStudentLink,
    DeliveryBoard,
    Room,
    ScenarioSectionBudget,
    ScenarioStudentCourseRequest,
    ScenarioStudentMap,
    SectionPlacement,
    Student,
    TermSection,
    TimetableScenario,
)
from core.services import timetable_workspace as tws
from core.services.timetable_v2_runner import (
    optimiser_safety_regression as _optimiser_safety_regression,
)
from core.services.timetable_v2_runner import (
    optimiser_student_outcome_regression as _optimiser_student_outcome_regression,
)
from core.services.timetable_v2_runner import (
    restore_scenario_placements as _restore_scenario_placements,
)
from core.services.timetable_v2_runner import (
    snapshot_scenario_placements as _snapshot_scenario_placements,
)
from core.services.timetable_workspace import (
    apply_bulk_clean_room_assignments,
    apply_bulk_safe_time_moves,
    check_publish_readiness,
    compute_incomplete_section_patterns,
    compute_scenario_safety_summary,
    create_planned_section_placements,
    preview_bulk_clean_room_assignments,
    preview_bulk_safe_time_moves,
    preview_placement_room_candidates,
    preview_placement_slot_candidates,
    preview_placement_student_evidence,
    preview_planned_section_slot_candidates,
    validate_placement,
)

pytestmark = pytest.mark.django_db


def _add_request(scenario: TimetableScenario, student_id: int, courses: list[str]) -> None:
    for course in courses:
        ScenarioStudentCourseRequest.objects.create(
            scenario=scenario,
            student_id=student_id,
            course_key=course,
            course_code=course,
            primary_term=1,
            status=ScenarioStudentCourseRequest.STATUS_REQUESTED,
            source="test",
        )


def _term_section(scenario: TimetableScenario, code: str, section: str) -> TermSection:
    return TermSection.objects.create(
        scenario=scenario,
        course_code=code,
        course_number=code,
        course_key=code,
        course_name=code,
        section=section,
        available_capacity=30,
        registered_count=25,
        source_tag="test",
    )


def _workspace() -> tuple[TimetableScenario, DeliveryBoard, DeliveryBoard]:
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="Room availability test",
    )
    board_a = DeliveryBoard.objects.create(
        scenario=scenario,
        label="Term 1",
        nominal_term=1,
        program="AI",
        display_order=1,
    )
    board_b = DeliveryBoard.objects.create(
        scenario=scenario,
        label="Term 3",
        nominal_term=3,
        program="AI",
        display_order=2,
    )
    return scenario, board_a, board_b


def test_validate_placement_flags_room_occupied_on_another_board() -> None:
    scenario, target_board, other_board = _workspace()
    moving_ts = _term_section(scenario, "AI101", "S1")
    other_ts = _term_section(scenario, "DS201", "S1")

    SectionPlacement.objects.create(
        board=other_board,
        term_section=other_ts,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="R100",
    )

    validation = validate_placement(
        board_id=target_board.id,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="R100",
        term_section_id=moving_ts.id,
    )

    assert validation["warning_count"] == 1
    assert validation["room_clashes"][0]["scope"] == "cross_board"
    assert validation["room_clashes"][0]["board_label"] == "Term 3"


def test_slot_preview_marks_cross_board_room_as_unavailable() -> None:
    scenario, target_board, other_board = _workspace()
    moving_ts = _term_section(scenario, "AI101", "S1")
    other_ts = _term_section(scenario, "DS201", "S1")
    moving = SectionPlacement.objects.create(
        board=target_board,
        term_section=moving_ts,
        day="TUE",
        start_time="09:00",
        end_time="10:15",
        room="R100",
    )
    SectionPlacement.objects.create(
        board=other_board,
        term_section=other_ts,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="R100",
    )

    preview = preview_placement_slot_candidates(moving.id)
    candidate = next(
        row for row in preview["candidates"] if row["day"] == "MON" and row["start"] == "09:00"
    )

    assert candidate["warning_count"] >= 1
    assert candidate["tone"] == "risky"
    assert any(item["kind"] == "cross_board_room" for item in candidate["evidence"])


def test_slot_preview_includes_timetable_quality_penalty() -> None:
    _scenario, target_board, _other_board = _workspace()
    moving_ts = _term_section(_scenario, "AI101", "S1")
    moving = SectionPlacement.objects.create(
        board=target_board,
        term_section=moving_ts,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="R100",
    )

    preview = preview_placement_slot_candidates(moving.id)
    weak = next(
        row for row in preview["candidates"] if row["day"] == "THU" and row["start"] == "16:00"
    )
    normal = next(
        row for row in preview["candidates"] if row["day"] == "MON" and row["start"] == "09:00"
    )

    assert weak["timetable_quality"]["policy"] == "timetable-quality-v1"
    assert weak["timetable_quality"]["components"]["weak_slot"] > 0
    assert weak["quality_score"] > normal["quality_score"]
    assert any(reason["kind"] == "quality" for reason in weak["ranking_reasons"])
    assert normal["primary_reason"]


def test_planned_slot_preview_ranks_clean_target_above_same_course_overlap() -> None:
    scenario, target_board, _other_board = _workspace()
    existing_ts = _term_section(scenario, "AI101", "S1")
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key="AI101",
        course_code="AI101",
        course_name="AI101",
        department="AI",
        credit_hours=3,
        planned_sections=2,
        max_per_section=30,
        total_demand=50,
        programme_term=1,
    )
    SectionPlacement.objects.create(
        board=target_board,
        term_section=existing_ts,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="R100",
    )

    preview = preview_planned_section_slot_candidates(
        target_board.id,
        course_code="AI101",
        course_key="AI101",
        section_label="S2",
        credit_hours=3,
        max_per_section=30,
    )
    same_slot = next(
        row for row in preview["candidates"] if row["day"] == "SUN" and row["start"] == "09:00"
    )

    assert preview["status"] == "ready"
    assert same_slot["tone"] == "avoid"
    assert same_slot["critical_count"] >= 1
    assert any(reason["kind"] == "same_course" for reason in same_slot["ranking_reasons"])
    assert "Same course section" in same_slot["primary_reason"]
    assert preview["candidates"][0]["tone"] == "clean"
    assert preview["candidates"][0]["primary_reason"]


def test_planned_slot_preview_exposes_quality_penalty() -> None:
    scenario, target_board, _other_board = _workspace()
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key="AI101",
        course_code="AI101",
        course_name="AI101",
        department="AI",
        credit_hours=3,
        planned_sections=1,
        max_per_section=30,
        total_demand=25,
        programme_term=1,
    )

    preview = preview_planned_section_slot_candidates(
        target_board.id,
        course_code="AI101",
        course_key="AI101",
        section_label="S1",
        credit_hours=3,
        max_per_section=30,
    )
    weak = next(
        row
        for row in preview["candidates"]
        if row["kind"] == "lect" and row["day"] == "THU" and row["start"] == "16:00"
    )
    normal = next(
        row
        for row in preview["candidates"]
        if row["kind"] == "lect" and row["day"] == "MON" and row["start"] == "09:00"
    )

    assert weak["timetable_quality"]["policy"] == "timetable-quality-v1"
    assert weak["timetable_quality"]["components"]["weak_slot"] > 0
    assert weak["quality_score"] > normal["quality_score"]
    assert any(reason["kind"] == "quality" for reason in weak["ranking_reasons"])
    assert normal["primary_reason"]


def test_planned_slot_preview_returns_full_patterns_for_multi_meeting_course() -> None:
    scenario, target_board, _other_board = _workspace()
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key="AI401",
        course_code="AI401",
        course_name="AI401",
        department="AI",
        credit_hours=4,
        planned_sections=1,
        max_per_section=30,
        total_demand=25,
        programme_term=1,
    )

    preview = preview_planned_section_slot_candidates(
        target_board.id,
        course_code="AI401",
        course_key="AI401",
        section_label="S1",
        credit_hours=4,
        max_per_section=30,
    )

    assert preview["status"] == "ready"
    assert preview["request"]["requires_full_section_pattern"] is True
    assert preview["request"]["required_meetings_per_section"] == 3
    assert preview["candidates"]
    best = preview["candidates"][0]
    assert best["is_pattern"] is True
    assert len(best["meetings"]) == 3
    assert {meeting["kind"] for meeting in best["meetings"]} == {"lect", "lab"}
    assert len({meeting["day"] for meeting in best["meetings"]}) == 3


def test_create_planned_section_placements_creates_complete_multi_meeting_section() -> None:
    scenario, target_board, _other_board = _workspace()
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key="AI401",
        course_code="AI401",
        course_name="AI401",
        department="AI",
        credit_hours=4,
        planned_sections=1,
        max_per_section=30,
        total_demand=25,
        programme_term=1,
    )
    preview = preview_planned_section_slot_candidates(
        target_board.id,
        course_code="AI401",
        course_key="AI401",
        section_label="S1",
        credit_hours=4,
        max_per_section=30,
    )
    meetings = [
        {"day": row["day"], "start_time": row["start"], "end_time": row["end"]}
        for row in preview["candidates"][0]["meetings"]
    ]

    result = create_planned_section_placements(
        target_board.id,
        course_code="AI401",
        course_key="AI401",
        course_name="AI401",
        section_label="S1",
        capacity=30,
        meetings=meetings,
    )

    assert len(result["placements"]) == 3
    assert TermSection.objects.get(course_key="AI401", section="S1").meetings.count() == 3
    assert (
        SectionPlacement.objects.filter(
            board=target_board, term_section__course_key="AI401"
        ).count()
        == 3
    )
    assert compute_incomplete_section_patterns(scenario.id) == []


def test_room_candidates_rank_available_room_above_occupied_room() -> None:
    scenario, target_board, other_board = _workspace()
    moving_ts = _term_section(scenario, "AI101", "S1")
    other_ts = _term_section(scenario, "DS201", "S1")
    Room.objects.create(
        room_code="R100", capacity=35, room_type="lecture", section="M", department="AI"
    )
    Room.objects.create(
        room_code="R200", capacity=35, room_type="lecture", section="M", department="AI"
    )
    moving = SectionPlacement.objects.create(
        board=target_board,
        term_section=moving_ts,
        day="TUE",
        start_time="09:00",
        end_time="10:15",
        room="UNASSIGNED",
    )
    SectionPlacement.objects.create(
        board=other_board,
        term_section=other_ts,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="R100",
    )

    preview = preview_placement_room_candidates(
        moving.id,
        day="MON",
        start_time="09:00",
        end_time="10:15",
    )
    by_room = {row["room_code"]: row for row in preview["candidates"]}

    assert by_room["R100"]["available"] is False
    assert by_room["R100"]["occupied_by"][0]["board_label"] == "Term 3"
    assert by_room["R200"]["available"] is True
    assert preview["candidates"][0]["room_code"] == "R200"


def test_room_preview_can_ignore_filtered_hidden_overlap_without_ignoring_room_occupancy() -> None:
    scenario, target_board, _other_board = _workspace()
    moving_ts = _term_section(scenario, "AI101", "S1")
    hidden_ts = _term_section(scenario, "CS211", "S1")
    Room.objects.create(
        room_code="R100", capacity=35, room_type="lecture", section="", department="AI"
    )
    Room.objects.create(
        room_code="R200", capacity=35, room_type="lecture", section="", department="AI"
    )
    moving = SectionPlacement.objects.create(
        board=target_board,
        term_section=moving_ts,
        day="TUE",
        start_time="09:00",
        end_time="10:15",
        room="UNASSIGNED",
    )
    SectionPlacement.objects.create(
        board=target_board,
        term_section=hidden_ts,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="R100",
    )
    Student.objects.create(student_id=100, name="Shared Student", program="AI")
    ScenarioStudentMap.objects.create(
        scenario=scenario,
        student_id=100,
        primary_term=1,
        recommended_courses=["AI101", "CS211"],
    )
    _add_request(scenario, 100, ["AI101", "CS211"])

    full_pane_preview = preview_placement_room_candidates(
        moving.id,
        day="MON",
        start_time="09:00",
        end_time="10:15",
    )
    full_by_room = {row["room_code"]: row for row in full_pane_preview["candidates"]}

    assert full_by_room["R200"]["available"] is True
    assert full_by_room["R200"]["policy_clean"] is False
    assert full_by_room["R200"]["validation"]["warning_count"] >= 1

    scoped_preview = preview_placement_room_candidates(
        moving.id,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        ignore_overlap_term_section_ids={hidden_ts.id},
    )
    scoped_by_room = {row["room_code"]: row for row in scoped_preview["candidates"]}

    assert scoped_by_room["R100"]["available"] is False
    assert scoped_by_room["R100"]["occupied_by"][0]["section"] == "CS211-S1"
    assert scoped_by_room["R200"]["available"] is True
    assert scoped_by_room["R200"]["policy_clean"] is True
    assert scoped_by_room["R200"]["validation"]["warning_count"] == 0


@pytest.mark.parametrize(
    ("credit_hours", "expected_type", "matching_room"),
    [
        (3, "lecture", "R100"),
        (4, "lab", "LAB1"),
    ],
)
def test_room_candidate_preview_uses_rooming_credit_gate_for_long_meetings(
    credit_hours: int,
    expected_type: str,
    matching_room: str,
) -> None:
    scenario, target_board, _other_board = _workspace()
    moving_ts = _term_section(scenario, "AI240", "S1")
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key=moving_ts.course_key,
        course_code=moving_ts.course_code,
        department="AI",
        credit_hours=credit_hours,
        planned_sections=1,
        max_per_section=30,
        total_demand=25,
        programme_term=1,
    )
    Room.objects.create(
        room_code="R100", capacity=35, room_type="lecture", section="", department="AI"
    )
    Room.objects.create(room_code="LAB1", capacity=25, room_type="lab", section="", department="AI")
    moving = SectionPlacement.objects.create(
        board=target_board,
        term_section=moving_ts,
        day="TUE",
        start_time="09:00",
        end_time="10:45",
        room="UNASSIGNED",
    )

    preview = preview_placement_room_candidates(moving.id)
    by_room = {row["room_code"]: row for row in preview["candidates"]}

    assert preview["target"]["required_type"] == expected_type
    assert by_room[matching_room]["fits_type"] is True
    assert by_room[matching_room]["available"] is True


def test_room_candidate_preview_blocks_under_capacity_labs() -> None:
    scenario, target_board, _other_board = _workspace()
    moving_ts = _term_section(scenario, "AI340", "S1")
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key=moving_ts.course_key,
        course_code=moving_ts.course_code,
        department="AI",
        credit_hours=4,
        planned_sections=1,
        max_per_section=30,
        total_demand=25,
        programme_term=1,
    )
    Room.objects.create(
        room_code="LAB20", capacity=20, room_type="lab", section="", department="AI"
    )
    Room.objects.create(
        room_code="LAB25", capacity=25, room_type="lab", section="", department="AI"
    )
    moving = SectionPlacement.objects.create(
        board=target_board,
        term_section=moving_ts,
        day="TUE",
        start_time="09:00",
        end_time="10:45",
        room="UNASSIGNED",
    )

    preview = preview_placement_room_candidates(moving.id)
    by_room = {row["room_code"]: row for row in preview["candidates"]}

    assert preview["target"]["required_type"] == "lab"
    assert by_room["LAB20"]["fits_capacity"] is False
    assert by_room["LAB20"]["available"] is False
    assert by_room["LAB20"]["tone"] == "block"
    assert "capacity 20 < 25" in by_room["LAB20"]["reasons"]
    assert by_room["LAB25"]["fits_capacity"] is True
    assert by_room["LAB25"]["available"] is True
    assert preview["candidates"][0]["room_code"] == "LAB25"


def test_room_candidate_outside_programme_pool_is_warning_not_clean() -> None:
    scenario, target_board, _other_board = _workspace()
    moving_ts = _term_section(scenario, "AI150", "S1")
    Room.objects.create(
        room_code="DS100", capacity=35, room_type="lecture", section="", department="DS"
    )
    Room.objects.create(
        room_code="AI100", capacity=35, room_type="lecture", section="", department="AI"
    )
    moving = SectionPlacement.objects.create(
        board=target_board,
        term_section=moving_ts,
        day="TUE",
        start_time="09:00",
        end_time="10:15",
        room="UNASSIGNED",
    )

    preview = preview_placement_room_candidates(moving.id)
    by_room = {row["room_code"]: row for row in preview["candidates"]}

    assert by_room["DS100"]["available"] is True
    assert by_room["DS100"]["schedule_clean"] is True
    assert by_room["DS100"]["department_fit"] is False
    assert by_room["DS100"]["policy_clean"] is False
    assert by_room["DS100"]["slot_clean"] is False
    assert by_room["DS100"]["tone"] == "warn"
    assert "outside board programme pool" in by_room["DS100"]["reasons"]
    assert by_room["AI100"]["policy_clean"] is True
    assert preview["candidates"][0]["room_code"] == "AI100"


def test_bulk_clean_room_preview_reserves_unique_room_slots() -> None:
    scenario, target_board, _other_board = _workspace()
    first_ts = _term_section(scenario, "AI101", "S1")
    second_ts = _term_section(scenario, "DS201", "S1")
    Room.objects.create(
        room_code="R100", capacity=35, room_type="lecture", section="", department="AI"
    )
    Room.objects.create(
        room_code="R200", capacity=35, room_type="lecture", section="", department="AI"
    )
    SectionPlacement.objects.create(
        board=target_board,
        term_section=first_ts,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="UNASSIGNED",
    )
    SectionPlacement.objects.create(
        board=target_board,
        term_section=second_ts,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="UNASSIGNED",
    )

    preview = preview_bulk_clean_room_assignments(scenario.id)

    rooms = {row["new_room"] for row in preview["assignments"]}
    assert preview["ready_count"] == 2
    assert rooms == {"R100", "R200"}


def test_bulk_clean_room_apply_updates_rooms_and_returns_undo_moves() -> None:
    scenario, target_board, _other_board = _workspace()
    first_ts = _term_section(scenario, "AI101", "S1")
    second_ts = _term_section(scenario, "DS201", "S1")
    Room.objects.create(
        room_code="R100", capacity=35, room_type="lecture", section="", department="AI"
    )
    Room.objects.create(
        room_code="R200", capacity=35, room_type="lecture", section="", department="AI"
    )
    first = SectionPlacement.objects.create(
        board=target_board,
        term_section=first_ts,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="UNASSIGNED",
    )
    second = SectionPlacement.objects.create(
        board=target_board,
        term_section=second_ts,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="UNASSIGNED",
    )

    result = apply_bulk_clean_room_assignments(scenario.id)

    first.refresh_from_db()
    second.refresh_from_db()
    assert result["applied_count"] == 2
    assert {first.room, second.room} == {"R100", "R200"}
    for row in result["applied"]:
        assert row["old_room"] == "UNASSIGNED"
        assert row["old_day"] == row["new_day"]
        assert row["old_start"] == row["new_start"]


def test_bulk_clean_room_apply_rejects_published_scenario() -> None:
    scenario, target_board, _other_board = _workspace()
    scenario.status = "published"
    scenario.save(update_fields=["status"])
    term_section = _term_section(scenario, "AI101", "S1")
    Room.objects.create(
        room_code="R100", capacity=35, room_type="lecture", section="", department="AI"
    )
    SectionPlacement.objects.create(
        board=target_board,
        term_section=term_section,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="UNASSIGNED",
    )

    with pytest.raises(ValueError, match="published"):
        apply_bulk_clean_room_assignments(scenario.id)


def test_bulk_safe_time_preview_and_apply_use_server_revalidation(monkeypatch) -> None:
    scenario, target_board, _other_board = _workspace()
    term_section = _term_section(scenario, "AI101", "S1")
    placement = SectionPlacement.objects.create(
        board=target_board,
        term_section=term_section,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="R100",
    )

    def fake_actions(scenario_id: int, *, limit: int = 18) -> dict:
        return {
            "actions": [
                {
                    "kind": "student_time_clash",
                    "board_id": target_board.id,
                    "placement_ids": [placement.id],
                    "score": 90000,
                }
            ]
        }

    def fake_slot_preview(placement_id: int) -> dict:
        assert placement_id == placement.id
        return {
            "candidates": [
                {
                    "tone": "clean",
                    "day": "TUE",
                    "start": "11:00",
                    "end": "12:15",
                    "critical_count": 0,
                    "warning_count": 0,
                    "impact_improvement": 1,
                    "student_improvement": 0,
                    "critical_improvement": 1,
                    "warning_improvement": 0,
                    "primary_reason": "Clean improving target",
                }
            ]
        }

    monkeypatch.setattr(tws, "build_scenario_builder_actions", fake_actions)
    monkeypatch.setattr(tws, "preview_placement_slot_candidates", fake_slot_preview)

    preview = preview_bulk_safe_time_moves(scenario.id, board_id=target_board.id)
    result = apply_bulk_safe_time_moves(scenario.id, board_id=target_board.id)

    placement.refresh_from_db()
    assert preview["ready_count"] == 1
    assert preview["moves"][0]["new_day"] == "TUE"
    assert result["applied_count"] == 1
    assert placement.day == "TUE"
    assert placement.start_time == "11:00"
    assert result["applied"][0]["old_day"] == "MON"


def test_bulk_safe_time_apply_rejects_published_scenario() -> None:
    scenario, _target_board, _other_board = _workspace()
    scenario.status = "published"
    scenario.save(update_fields=["status"])

    with pytest.raises(ValueError, match="published"):
        apply_bulk_safe_time_moves(scenario.id)


def test_scenario_safety_summary_reports_unique_students_and_board_links() -> None:
    scenario, board_a, board_b = _workspace()
    for student_id in [10, 11]:
        Student.objects.create(student_id=student_id, name=f"S{student_id}", program="AI")
        ScenarioStudentMap.objects.create(
            scenario=scenario,
            student_id=student_id,
            primary_term=1,
            recommended_courses=["AI101"],
        )
        _add_request(scenario, student_id, ["AI101"])
    BoardStudentLink.objects.create(board=board_a, student_id=10, link_type="primary")
    BoardStudentLink.objects.create(board=board_a, student_id=11, link_type="primary")
    BoardStudentLink.objects.create(board=board_b, student_id=10, link_type="visitor")

    summary = compute_scenario_safety_summary(scenario.id)

    assert summary["unique_students"] == 2
    assert summary["board_student_links_total"] == 3
    assert summary["primary_student_links"] == 2
    assert summary["visitor_student_links"] == 1


def test_cross_board_summary_uses_exact_affected_students_not_pair_count_sample() -> None:
    scenario, board_a, board_b = _workspace()
    ai_section = _term_section(scenario, "AI101", "S1")
    ds_section = _term_section(scenario, "DS201", "S1")
    SectionPlacement.objects.create(
        board=board_a,
        term_section=ai_section,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="R100",
    )
    SectionPlacement.objects.create(
        board=board_b,
        term_section=ds_section,
        day="MON",
        start_time="09:30",
        end_time="10:45",
        room="R200",
    )

    for offset in range(45):
        student_id = 1000 + offset
        Student.objects.create(student_id=student_id, name=f"S{student_id}", program="AI")
        ScenarioStudentMap.objects.create(
            scenario=scenario,
            student_id=student_id,
            primary_term=1,
            recommended_courses=["AI101", "DS201"],
        )
        _add_request(scenario, student_id, ["AI101", "DS201"])
    for offset in range(5):
        student_id = 2000 + offset
        Student.objects.create(student_id=student_id, name=f"S{student_id}", program="AI")
        ScenarioStudentMap.objects.create(
            scenario=scenario,
            student_id=student_id,
            primary_term=1,
            recommended_courses=["AI101"],
        )
        _add_request(scenario, student_id, ["AI101"])

    conflicts = tws.detect_cross_board_conflicts(scenario.id)
    summary = compute_scenario_safety_summary(scenario.id, cross_board_conflicts=conflicts)

    assert len(conflicts) == 1
    assert conflicts[0]["overlap_count"] == 45
    assert conflicts[0]["affected_student_count"] == 45
    assert len(conflicts[0]["affected_student_ids"]) == 45
    assert len(conflicts[0]["affected_student_ids_sample"]) == 40
    assert summary["cross_board_conflicts"] == 1
    assert summary["cross_board_affected_students"] == 45
    assert summary["cross_board_student_conflict_incidences"] == 45
    assert summary["high_cross_board_affected_students"] == 45
    assert summary["max_cross_board_conflicts_per_student"] == 1


def test_optimiser_safety_regression_treats_cross_board_as_tradeoff() -> None:
    before = {
        "cross_board_conflicts": 213,
        "cross_board_affected_students": 171,
        "cross_board_student_conflict_incidences": 1101,
        "high_cross_board_affected_students": 120,
        "same_board_conflicts": {"overlaps": 0, "instructors": 0, "rooms": 0},
        "physical_unassigned_rooms": 58,
    }
    after = {
        "cross_board_conflicts": 226,
        "cross_board_affected_students": 175,
        "cross_board_student_conflict_incidences": 1362,
        "high_cross_board_affected_students": 124,
        "same_board_conflicts": {"overlaps": 0, "instructors": 0, "rooms": 0},
        "physical_unassigned_rooms": 61,
    }

    regression = _optimiser_safety_regression(before, after)

    assert regression["blocked"] is False
    assert regression["regressions"] == []


def test_optimiser_safety_regression_blocks_hard_same_board_worsening() -> None:
    before = {"same_board_conflicts": {"overlaps": 0, "instructors": 0, "rooms": 0}}
    after = {"same_board_conflicts": {"overlaps": 1, "instructors": 0, "rooms": 0}}

    regression = _optimiser_safety_regression(before, after)

    assert regression["blocked"] is True
    metrics = {item["metric"] for item in regression["regressions"]}
    assert "same_board_overlaps" in metrics


def test_optimiser_student_outcome_regression_blocks_unresolved_worsening() -> None:
    regression = _optimiser_student_outcome_regression(
        {
            "baseline_score": [0, 4, 4, 0, 200210, 82],
            "final_score": [0, 6, 6, 0, 180000, 70],
        }
    )

    assert regression["blocked"] is True
    metrics = {item["metric"] for item in regression["regressions"]}
    assert "unresolved_students" in metrics
    assert "unassigned_courses" in metrics


def test_optimizer_placement_snapshot_can_restore_mutated_board() -> None:
    scenario, board_a, _board_b = _workspace()
    term_section = _term_section(scenario, "AI101", "S1")
    placement = SectionPlacement.objects.create(
        board=board_a,
        term_section=term_section,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="R100",
    )
    snapshot = _snapshot_scenario_placements(scenario.id)

    placement.start_time = "13:00"
    placement.end_time = "14:15"
    placement.room = "R999"
    placement.save(update_fields=["start_time", "end_time", "room"])

    _restore_scenario_placements(scenario.id, snapshot)

    restored = SectionPlacement.objects.get(pk=placement.id)
    assert restored.start_time == "09:00"
    assert restored.end_time == "10:15"
    assert restored.room == "R100"


def test_publish_readiness_blocks_missing_required_sections() -> None:
    scenario, target_board, _other_board = _workspace()
    term_section = _term_section(scenario, "CS113", "S1")
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key=term_section.course_key,
        course_code=term_section.course_code,
        department="AI",
        credit_hours=3,
        planned_sections=2,
        max_per_section=30,
        total_demand=60,
        programme_term=1,
    )
    SectionPlacement.objects.create(
        board=target_board,
        term_section=term_section,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="R100",
    )

    readiness = check_publish_readiness(scenario.id)

    assert readiness["ready"] is False
    assert any("CS113 needs 1 more" in blocker for blocker in readiness["blockers"])


def test_incomplete_multi_meeting_section_blocks_publish() -> None:
    scenario, target_board, _other_board = _workspace()
    term_section = _term_section(scenario, "CS401", "S1")
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key=term_section.course_key,
        course_code=term_section.course_code,
        department="AI",
        credit_hours=4,
        planned_sections=1,
        max_per_section=30,
        total_demand=25,
        programme_term=1,
    )
    SectionPlacement.objects.create(
        board=target_board,
        term_section=term_section,
        day="MON",
        start_time="09:00",
        end_time="10:40",
        room="LAB1",
    )

    issues = compute_incomplete_section_patterns(scenario.id)
    readiness = check_publish_readiness(scenario.id)

    assert issues == [
        {
            "course_key": "CS401",
            "course_code": "CS401",
            "section": "S1",
            "placed_meetings": 1,
            "expected_meetings": 3,
            "missing_meetings": 2,
        }
    ]
    assert readiness["ready"] is False
    assert any("CS401-S1 has 1/3 meetings" in blocker for blocker in readiness["blockers"])


def test_student_evidence_lists_exact_students_for_selected_placement() -> None:
    scenario, target_board, _other_board = _workspace()
    moving_ts = _term_section(scenario, "AI101", "S1")
    other_ts = _term_section(scenario, "DS201", "S1")
    moving = SectionPlacement.objects.create(
        board=target_board,
        term_section=moving_ts,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="R100",
    )
    SectionPlacement.objects.create(
        board=target_board,
        term_section=other_ts,
        day="MON",
        start_time="09:30",
        end_time="10:45",
        room="R200",
    )
    Student.objects.create(student_id=10, name="A", program="AI", section="M")
    Student.objects.create(student_id=11, name="B", program="AI", section="M")
    Student.objects.create(student_id=12, name="C", program="AI", section="M")
    ScenarioStudentMap.objects.create(
        scenario=scenario,
        student_id=10,
        primary_term=1,
        recommended_courses=["AI101", "DS201"],
    )
    _add_request(scenario, 10, ["AI101", "DS201"])
    ScenarioStudentMap.objects.create(
        scenario=scenario,
        student_id=11,
        primary_term=1,
        recommended_courses=["AI101", "DS201"],
    )
    _add_request(scenario, 11, ["AI101", "DS201"])
    ScenarioStudentMap.objects.create(
        scenario=scenario,
        student_id=12,
        primary_term=1,
        recommended_courses=["AI101"],
    )
    _add_request(scenario, 12, ["AI101"])

    evidence = preview_placement_student_evidence(moving.id)

    assert evidence["affected_student_count"] == 2
    assert evidence["conflicts"][0]["affected_count"] == 2
    assert [row["student_id"] for row in evidence["students"]] == [10, 11]

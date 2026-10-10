"""Classic optimal, adaptive and balance paths preserve complete English weeks."""

import json
import random

import pytest
from django.test import override_settings

from core.models import (
    CourseInstructor,
    DeliveryBoard,
    Instructor,
    ScenarioSectionBudget,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.timetable_course_policy import get_course_meeting_policy
from core.services.timetable_load_balanced import rebalance_and_persist_board
from core.services.timetable_local_search import (
    _apply_move,
    _check_hard_constraints,
    _generate_relocate_move,
    _policy_bundles,
    _undo_move,
    optimize_and_persist_board,
)
from core.services.timetable_solver import (
    persist_solver_result,
    solve_board,
    solve_board_with_hints,
)

pytestmark = pytest.mark.django_db


def board_fixture(*, count=1, blocked=None, teacher=False):
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="Intensive English",
        gender="M",
        programs=["AI"],
        blocked_slots=blocked or [],
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="Term 1", nominal_term=1, program="AI"
    )
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key="ENG101::ENGLISH",
        course_code="ENG101",
        course_name="English",
        department="AI",
        programme_term=1,
        credit_hours=4,
        planned_sections=count,
        max_per_section=25,
        total_demand=25 * count,
    )
    if teacher:
        person = Instructor.objects.create(full_name="Dr English", normalised_name="dr english")
        CourseInstructor.objects.create(
            program="AI", course_code="ENG101", section="M", instructor=person
        )
    return scenario, board


@pytest.mark.parametrize("hinted", [False, True])
def test_classic_solver_parallel_sections_choose_whole_scoped_bundles(hinted):
    scenario, board = board_fixture(count=4)
    result = solve_board_with_hints(board.id, [], 2) if hinted else solve_board(board.id, 2)
    assert result["status"] in {"optimal", "feasible"}
    assert result["placed"] == 4
    policy = get_course_meeting_policy("ENG101")
    assert all(policy.accepts(section["meetings"]) for section in result["placements"])
    assert all(sum(180 for _ in section["meetings"]) == 720 for section in result["placements"])
    persist_solver_result(board.id, result)
    for section in TermSection.objects.filter(scenario=scenario):
        assert policy.accepts(TermSectionMeeting.objects.filter(term_section=section))
        assert SectionPlacement.objects.filter(term_section=section).count() == 4


@pytest.mark.parametrize("hinted", [False, True])
def test_interior_blocked_cell_excludes_entire_morning_pattern(hinted):
    _, board = board_fixture(blocked=[{"day": "MON", "start": "10:15", "end": "10:30"}])
    result = solve_board_with_hints(board.id, [], 2) if hinted else solve_board(board.id, 2)
    assert result["placed"] == 1
    assert {m["start"] for m in result["placements"][0]["meetings"]} == {"13:00"}


@override_settings(
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True, TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED=True
)
@pytest.mark.parametrize("count,expected", [(2, "optimal"), (3, "infeasible")])
def test_classic_teacher_clash_applies_to_actual_bundle_occupancy(count, expected):
    _, board = board_fixture(count=count, teacher=True)
    result = solve_board(board.id, 2)
    assert result["status"] == expected
    if count == 2:
        assert {section["meetings"][0]["start"] for section in result["placements"]} == {
            "09:00",
            "13:00",
        }


def test_invalid_mixed_result_is_rejected_before_persisting_any_rows():
    _, board = board_fixture()
    meetings = get_course_meeting_policy("ENG101").patterns[0].to_dict()["meetings"]
    meetings[1].update(start="13:00", end="16:00")
    with pytest.raises(ValueError, match="180-minute"):
        persist_solver_result(
            board.id,
            {
                "status": "optimal",
                "placements": [
                    {
                        "course_code": "ENG101::ENGLISH",
                        "display_code": "ENG101",
                        "section": "S1",
                        "meetings": meetings,
                    }
                ],
            },
        )
    assert not TermSection.objects.exists()
    assert not SectionPlacement.objects.exists()


@override_settings(TIMETABLE_ENFORCE_LOCKS=True)
def test_one_locked_meeting_preserves_its_entire_english_bundle_and_metadata():
    scenario, board = board_fixture()
    section = TermSection.objects.create(
        scenario=scenario,
        course_key="ENG101::ENGLISH",
        course_code="ENG",
        course_number="101",
        section="S1",
        source_tag="tw_auto",
    )
    for index, meeting in enumerate(
        get_course_meeting_policy("ENG101").patterns[0].to_dict()["meetings"]
    ):
        SectionPlacement.objects.create(
            board=board,
            term_section=section,
            day=meeting["day"],
            start_time=meeting["start"],
            end_time=meeting["end"],
            room="DEDICATED",
            is_locked=index == 0,
        )
        TermSectionMeeting.objects.create(
            term_section=section,
            day=meeting["day"],
            start_time=meeting["start"],
            end_time=meeting["end"],
            room="DEDICATED",
            instructor="Dr English",
        )
    before = list(SectionPlacement.objects.values())
    metadata = list(TermSectionMeeting.objects.values())
    persist_solver_result(board.id, {"status": "optimal", "placements": []})
    assert list(SectionPlacement.objects.values()) == before
    assert list(TermSectionMeeting.objects.values()) == metadata


def test_sa_neighborhood_moves_and_undoes_the_entire_bundle():
    scenario, _ = board_fixture()
    policy = get_course_meeting_policy("ENG101")
    bundles = _policy_bundles(policy, scenario, [])
    schedule = {0: bundles[0]}
    sections = [{"code": "ENG101", "meeting_policy": policy, "bundles": bundles}]
    move = _generate_relocate_move(schedule, sections, {0: []}, random.Random(5))
    assert move[1] == -1
    old = _apply_move(schedule, *move)
    assert policy.pattern_for(schedule[0]).pattern_id.endswith("PM")
    assert _check_hard_constraints(schedule, sections, {})
    _undo_move(schedule, move[0], move[1], old)
    assert policy.pattern_for(schedule[0]).pattern_id.endswith("AM")
    assert _generate_relocate_move(schedule, sections, {0: []}, random.Random(5), {0}) is None


@pytest.mark.parametrize("optimise", [optimize_and_persist_board, rebalance_and_persist_board])
def test_intentionally_unplaced_english_section_does_not_block_other_course_optimisation(optimise):
    scenario, board = board_fixture()
    TermSection.objects.create(
        scenario=scenario,
        course_key="ENG101::ENGLISH",
        course_code="ENG101",
        course_number="ENG101",
        section="S1",
        source_tag="tw_auto",
    )
    ordinary = TermSection.objects.create(
        scenario=scenario,
        course_key="AI123",
        course_code="AI123",
        course_number="AI123",
        section="S1",
        source_tag="tw_auto",
    )
    SectionPlacement.objects.create(
        board=board, term_section=ordinary, day="SUN", start_time="09:00", end_time="10:15"
    )
    result = optimise(board.id, max_seconds=0)
    assert result["status"] == "optimized"
    assert not result.get("persist_rolled_back")
    assert SectionPlacement.objects.filter(term_section=ordinary).count() == 1


@override_settings(
    TIMETABLE_ENFORCE_LOCKS=True,
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
    TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED=True,
)
@pytest.mark.parametrize("optimise", [optimize_and_persist_board, rebalance_and_persist_board])
def test_optional_classic_pass_can_improve_teacher_gap_by_switching_whole_bundle(optimise):
    scenario, board = board_fixture(teacher=True)
    person = Instructor.objects.get()
    CourseInstructor.objects.create(
        program="AI", course_code="AI123", section="M", instructor=person
    )
    section = TermSection.objects.create(
        scenario=scenario,
        course_key="ENG101::ENGLISH",
        course_code="ENG101",
        course_number="ENG101",
        section="S1",
        source_tag="tw_auto",
    )
    for meeting in get_course_meeting_policy("ENG101").patterns[0].to_dict()["meetings"]:
        SectionPlacement.objects.create(
            board=board,
            term_section=section,
            day=meeting["day"],
            start_time=meeting["start"],
            end_time=meeting["end"],
        )
        TermSectionMeeting.objects.create(
            term_section=section,
            day=meeting["day"],
            start_time=meeting["start"],
            end_time=meeting["end"],
            instructor=person.full_name,
        )
    ordinary = TermSection.objects.create(
        scenario=scenario,
        course_key="AI123",
        course_code="AI123",
        course_number="AI123",
        section="S1",
        source_tag="tw_auto",
    )
    locked = SectionPlacement.objects.create(
        board=board,
        term_section=ordinary,
        day="SUN",
        start_time="16:00",
        end_time="17:15",
        is_locked=True,
    )
    TermSectionMeeting.objects.create(
        term_section=ordinary,
        day="SUN",
        start_time="16:00",
        end_time="17:15",
        instructor=person.full_name,
    )
    result = optimise(board.id, max_seconds=0.1)
    json.dumps(result)
    assert result["status"] == "optimized"
    assert not result.get("persist_rolled_back")
    rows = list(SectionPlacement.objects.filter(term_section=section))
    assert get_course_meeting_policy("ENG101").accepts(rows)
    assert {r.start_time for r in rows} == {"13:00"}
    assert get_course_meeting_policy("ENG101").accepts(
        TermSectionMeeting.objects.filter(term_section=section)
    )
    locked.refresh_from_db()
    assert locked.is_locked and locked.start_time == "16:00"


@override_settings(
    TIMETABLE_ENFORCE_LOCKS=True,
    TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True,
    TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED=True,
)
@pytest.mark.parametrize(
    "module_name,optimise,value_key",
    [
        ("core.services.timetable_local_search", optimize_and_persist_board, "cost"),
        ("core.services.timetable_load_balanced", rebalance_and_persist_board, "score"),
    ],
)
def test_optional_writer_rolls_back_entire_pass_when_new_bundle_double_books_teacher(
    monkeypatch,
    module_name,
    optimise,
    value_key,
):
    import importlib

    scenario, board = board_fixture(teacher=True)
    person = Instructor.objects.get()
    CourseInstructor.objects.create(
        program="AI", course_code="AI123", section="M", instructor=person
    )
    section = TermSection.objects.create(
        scenario=scenario,
        course_key="ENG101::ENGLISH",
        course_code="ENG101",
        course_number="ENG101",
        section="S1",
        source_tag="tw_auto",
    )
    for meeting in get_course_meeting_policy("ENG101").patterns[0].to_dict()["meetings"]:
        SectionPlacement.objects.create(
            board=board,
            term_section=section,
            day=meeting["day"],
            start_time=meeting["start"],
            end_time=meeting["end"],
            room="DEDICATED",
        )
        TermSectionMeeting.objects.create(
            term_section=section,
            day=meeting["day"],
            start_time=meeting["start"],
            end_time=meeting["end"],
            room="DEDICATED",
            instructor=person.full_name,
        )
    ordinary = TermSection.objects.create(
        scenario=scenario,
        course_key="AI123",
        course_code="AI123",
        course_number="AI123",
        section="S1",
        source_tag="tw_auto",
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=ordinary,
        day="SUN",
        start_time="13:00",
        end_time="14:15",
        room="KEEP",
        is_locked=True,
    )
    TermSectionMeeting.objects.create(
        term_section=ordinary,
        day="SUN",
        start_time="13:00",
        end_time="14:15",
        room="KEEP",
        instructor=person.full_name,
    )
    before = list(SectionPlacement.objects.order_by("pk").values())
    metadata = list(TermSectionMeeting.objects.order_by("pk").values())
    candidate = {
        "status": "optimized",
        "schedule": {0: get_course_meeting_policy("ENG101").patterns[1].to_dict()["meetings"]},
        "sections": [
            {
                "term_section_id": section.pk,
                "code": "ENG101",
                "course_key": section.course_key,
                "label": "S1",
            }
        ],
        f"{value_key}_before": 10,
        f"{value_key}_after": 0,
        "improvements": 1,
    }
    module = importlib.import_module(module_name)
    monkeypatch.setattr(
        module,
        "optimize_board" if value_key == "cost" else "rebalance_board",
        lambda *args, **kwargs: candidate,
    )
    result = optimise(board.id, max_seconds=0)
    assert result["persist_rolled_back"]
    assert "instructor clashes increased" in result["rollback_reasons"]
    assert result[f"{value_key}_after"] == 10
    assert result["improvements"] == 0
    assert {m["start"] for m in result["schedule"][0]} == {"09:00"}
    assert {m["start"] for m in result["attempted_schedule"][0]} == {"13:00"}
    assert list(SectionPlacement.objects.order_by("pk").values()) == before
    assert list(TermSectionMeeting.objects.order_by("pk").values()) == metadata

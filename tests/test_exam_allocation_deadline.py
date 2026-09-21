"""Solver stop reason, not coarse wall-clock polling, governs cache eligibility."""

import time
from copy import deepcopy
from types import SimpleNamespace

import pytest

from core.services import exam_room_allocation as allocation
from core.services.exam_timetable import assign_rooms_to_schedule


@pytest.fixture(autouse=True)
def isolated_cache():
    with allocation._CACHE_LOCK:
        allocation._CACHE.clear()
    yield
    with allocation._CACHE_LOCK:
        allocation._CACHE.clear()


def _inputs():
    sections = {
        "A": [
            {"section": "F1", "section_key": "A1", "student_count": 40, "gender": "F"},
            {"section": "F2", "section_key": "A2", "student_count": 20, "gender": "F"},
        ],
        "B": [{"section": "F1", "section_key": "B1", "student_count": 35, "gender": "F"}],
    }
    schedule = [
        {
            "course_code": code,
            "course_identity": code,
            "slot_index": 0,
            "day": "Sun",
            "period": "08:00-10:00",
        }
        for code in sections
    ]
    rooms = [
        {"room_code": f"F{capacity}", "capacity": capacity, "section": "F"} for capacity in [60, 40]
    ]
    return schedule, sections, rooms


def _stub_solver(monkeypatch, status, deterministic_time):
    class Solver:
        def __init__(self):
            self.parameters = SimpleNamespace()
            self.response_proto = SimpleNamespace(deterministic_time=deterministic_time)

        def solve(self, model):
            return status

    monkeypatch.setattr(allocation.cp_model, "CpSolver", Solver)


@pytest.mark.parametrize("status", [allocation.cp_model.UNKNOWN, allocation.cp_model.FEASIBLE])
def test_early_wall_limited_solver_result_never_enters_cache(monkeypatch, status):
    _stub_solver(monkeypatch, status, deterministic_time=0.000006)
    schedule, sections, rooms = _inputs()
    original_inputs = deepcopy((sections, rooms))
    context = allocation.RoomAllocationContext(deadline=time.monotonic() + 60)
    with pytest.raises(allocation.RoomAllocationTimeout):
        assign_rooms_to_schedule(schedule, sections, rooms, allocation_context=context)
    assert context.remaining() > 0, "The regression must not rely on an expired outer clock."
    assert not allocation._CACHE
    assert (sections, rooms) == original_inputs


def test_deterministic_work_limit_retains_valid_conserved_fallback(monkeypatch):
    _stub_solver(
        monkeypatch,
        allocation.cp_model.UNKNOWN,
        deterministic_time=allocation._PHASE_DETERMINISTIC_LIMIT,
    )
    schedule, sections, rooms = _inputs()
    context = allocation.RoomAllocationContext(deadline=time.monotonic() + 60)
    assign_rooms_to_schedule(schedule, sections, rooms, allocation_context=context)
    assert context.limited_searches == 2
    assert len(allocation._CACHE) == 1
    for entry in schedule:
        assert sum(room["student_count"] for room in entry["rooms"]) == sum(
            section["student_count"] for section in sections[entry["course_code"]]
        )
        for room in entry["rooms"]:
            if room["room_code"] != "UNASSIGNED":
                assert room["student_count"] <= room["room_capacity"]

"""Staff balancing cannot improve its score by losing seats or splitting sections."""

from copy import deepcopy

import pytest

from core.services import exam_timetable


@pytest.mark.parametrize(
    "initial_parts,moved_parts,expected_moves",
    [
        ([40], [20], 0),
        ([40], [20, 20], 0),
        ([20, 20], [20, 10, 10], 0),
        ([40], [40], 1),
        ([40], None, 0),
    ],
    ids=["lost-seats", "new-split", "more-fragments", "safe-improvement", "timeout-restores"],
)
def test_rebalance_preserves_seating_and_original_section_fragmentation(
    monkeypatch, initial_parts, moved_parts, expected_moves
):
    entries = [
        {
            "course_code": code,
            "course_identity": f"{code}:name",
            "day": day,
            "period": "08:00-10:00",
            "slot_index": index,
        }
        for code, day, index in [
            ("CS101", "Sun", 0),
            ("CS102", "Sun", 0),
            ("CS103", "Sun", 0),
            ("CS104", "Mon", 1),
        ]
    ]
    enrollment = {
        entry["course_code"]: [
            {
                "section": "F01",
                "section_key": f"term-section:{index}",
                "term_section_id": index,
                "gender": "F",
                "mapping_status": "mapped",
                "student_count": 10 if entry["course_code"] == "CS104" else 40,
            }
        ]
        for index, entry in enumerate(entries, 1)
    }
    original_enrollment = deepcopy(enrollment)
    attempts = []

    def pack(schedule, sections, rooms, seed=None, **kwargs):
        moving = next(entry for entry in schedule if entry["course_code"] == "CS101")
        attempts.append(moving["day"])
        if moved_parts is None and moving["day"] == "Mon":
            from core.services.exam_room_allocation import RoomAllocationTimeout

            # A failed allocation may already have mutated several entries.
            for entry in schedule:
                entry["rooms"] = [{"room_code": "partial-result", "student_count": 999}]
            raise RoomAllocationTimeout("The shared room allocation deadline elapsed.")
        for entry in schedule:
            section = sections[entry["course_code"]][0]
            counts = [section["student_count"]]
            if entry["course_code"] == "CS101":
                counts = initial_parts if entry["day"] == "Sun" else moved_parts
            entry["rooms"] = [
                {
                    "room_code": f"{entry['course_code']}-R{index}",
                    "room_capacity": count,
                    "student_count": count,
                    "gender": "F",
                    "section": section["section"],
                    "section_parts": [{**section, "student_count": count}],
                }
                for index, count in enumerate(counts)
            ]
        return schedule

    monkeypatch.setattr(exam_timetable, "assign_rooms_to_schedule", pack)
    moves = exam_timetable._rebalance_invigilators_pass(
        entries,
        enrollment,
        [{"room_code": "inventory-present"}],
        [
            {"index": index, "day": day, "period": "08:00-10:00"}
            for index, day in enumerate(["Sun", "Mon"])
        ],
        {},
        {},
        {},
        max_iterations=1,
        pinned_courses={"CS102", "CS103", "CS104"},
    )
    assert "Mon" in attempts, "The candidate must actually be evaluated before rejection."
    assert moves == expected_moves
    moving = entries[0]
    assert moving["day"] == ("Mon" if expected_moves else "Sun")
    assert moving["slot_index"] == expected_moves
    assert [room["student_count"] for room in moving["rooms"]] == (
        moved_parts if expected_moves else initial_parts
    )
    assert enrollment == original_enrollment
    if moved_parts is None:
        assert attempts == ["Sun", "Mon"], "A timed-out trial must stop without repacking."
        assert [room["student_count"] for entry in entries[1:] for room in entry["rooms"]] == [
            40,
            40,
            10,
        ]

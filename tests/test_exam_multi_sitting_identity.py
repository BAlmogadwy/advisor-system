"""Room-split QA retains the course identity of every logical section."""

import pytest

from core.services.exam_run_schema import derive_multi_sitting_details, derive_status_surface
from core.services.exam_timetable import assign_rooms_to_schedule


def _entry(code, name="", *, identity="", day="Sun", count=25):
    return {
        "course_code": code,
        "source_course_code": code,
        "course_name": name,
        "course_identity": identity,
        "day": day,
        "period": "08:00-10:00",
        "rooms": [
            {
                "section": f"M1/{part}",
                "room_code": f"R{part}",
                "student_count": count,
                "room_capacity": 40,
            }
            for part in [1, 2]
        ],
    }


def test_same_section_label_in_different_courses_is_counted_separately():
    details = derive_multi_sitting_details([_entry("CS101"), _entry("CS102", count=35)])
    assert len(details) == 2
    by_course = {detail["course_code"]: detail for detail in details}
    assert by_course["CS101"]["enrolment"] == 50
    assert by_course["CS102"]["enrolment"] == 70
    for code, detail in by_course.items():
        assert detail["section"] == "M1"
        assert detail["sittings"] == 2
        assert code in detail["audit_text"]


def test_same_code_different_names_do_not_merge_section_splits():
    details = derive_multi_sitting_details(
        [_entry("CS111", "Programming I"), _entry("CS111", "Fundamentals of Programming")]
    )
    assert len(details) == 2
    assert {detail["course_name"] for detail in details} == {
        "Programming I",
        "Fundamentals of Programming",
    }
    assert len({detail["course_identity"] for detail in details}) == 2


def test_course_identity_preserved_across_slots_and_grouped_input():
    first = _entry("CS111 (1)", "Programming I", identity="CS111::PROGRAMMING_I")
    second = _entry("CS111", "Programming I", identity="CS111::PROGRAMMING_I", day="Mon")
    first["rooms"] = first["rooms"][:1]
    second["rooms"] = second["rooms"][1:]
    for schedule in [
        [first, second],
        {"Sun:08:00-10:00": [first], "Mon:08:00-10:00": [second]},
    ]:
        details = derive_multi_sitting_details(schedule)
        assert len(details) == 1
        assert details[0]["course_identity"] == "CS111::PROGRAMMING_I"
        assert details[0]["course_name"] == "Programming I"
        assert details[0]["enrolment"] == 50
        assert details[0]["sittings"] == 2
        assert details[0]["slots"] == ["Sun:08:00-10:00", "Mon:08:00-10:00"]


def test_one_split_fragment_per_different_course_does_not_create_fake_multi_sitting():
    entries = [_entry("CS101"), _entry("CS102")]
    for entry in entries:
        entry["rooms"] = entry["rooms"][:1]
    assert derive_multi_sitting_details(entries) == []


@pytest.mark.parametrize(
    "bucket_qa",
    [{"bucket_day_violations_count": 1}, {"bucket_day_violations": [{"day": "Sun"}]}],
)
def test_bucket_day_violation_never_receives_clean_status(bucket_qa):
    primary, flags = derive_status_surface(
        {
            "status": "ok",
            "schedule": [],
            "qa": {
                "manual_override_count": 0,
                "conflict_count": 0,
                **bucket_qa,
            },
        }
    )
    assert primary == "contains_manual_override"
    assert "manual_override" in flags


@pytest.mark.parametrize("unassigned", [[{"course_code": "CS111", "section": "M1"}], 1])
def test_nested_room_shortage_never_receives_clean_status(unassigned):
    primary, flags = derive_status_surface(
        {
            "status": "ok",
            "schedule": [],
            "qa": {"manual_override_count": 0, "rooms": {"unassigned_room_sections": unassigned}},
        }
    )
    assert primary == "requires_room_action"
    assert "room_action_required" in flags


def _scheduled(code):
    return {"course_code": code, "day": "Sun", "period": "08:00-10:00", "slot_index": 0}


def _section(label, count):
    return {"section": label, "student_count": count, "gender": "M", "preferred_room": ""}


def _rooms(capacities):
    return [
        {"room_code": f"R{index}", "capacity": capacity, "section": "M"}
        for index, capacity in enumerate(capacities)
    ]


def test_allocator_halved_sections_preserve_original_section_and_enrolment():
    entries = [_scheduled("A"), _scheduled("B")]
    assign_rooms_to_schedule(
        entries,
        {"A": [_section("M1", 80)], "B": [_section("M1", 70)]},
        _rooms([80, 40, 40]),
    )
    details = derive_multi_sitting_details(entries)
    assert len(details) == 1
    assert details[0]["course_code"] == "B"
    assert details[0]["section"] == "M1"
    assert details[0]["enrolment"] == 70
    assert details[0]["sittings"] == 2
    assert details[0]["incomplete"] is False


def test_packed_room_does_not_attribute_another_sections_students_to_split():
    entries = [_scheduled("A")]
    assign_rooms_to_schedule(
        entries, {"A": [_section("M1", 90), _section("M2", 15)]}, _rooms([60, 60])
    )
    assert any("+" in room["section"] for room in entries[0]["rooms"])
    details = derive_multi_sitting_details(entries)
    assert len(details) == 1
    assert details[0]["section"] == "M1"
    assert details[0]["enrolment"] == 90
    assert details[0]["sittings"] == 2


def test_unassigned_split_part_retains_incomplete_section_audit():
    entries = [_scheduled("A"), _scheduled("B")]
    assign_rooms_to_schedule(
        entries,
        {"A": [_section("M1", 80)], "B": [_section("M1", 70)]},
        _rooms([80, 40]),
    )
    details = derive_multi_sitting_details(entries)
    assert len(details) == 1
    assert details[0]["course_code"] == "B"
    assert details[0]["enrolment"] == 70
    assert details[0]["sittings"] == 2
    assert details[0]["incomplete"] is True


def test_split_parts_in_one_room_do_not_create_multiple_sittings():
    entry = _scheduled("A")
    entry["rooms"] = [
        {
            "section": "M1a+M1b",
            "room_code": "R1",
            "student_count": 40,
            "room_capacity": 50,
            "section_parts": [
                {"section": part, "student_count": 20, "_split_from": "M1"}
                for part in ["M1a", "M1b"]
            ],
        }
    ]
    assert derive_multi_sitting_details([entry]) == []


def _official_part(key, label, count, *, mapping_status="mapped"):
    return {
        "section_key": key,
        "section": label,
        "term_section_id": int(key.split(":")[1]) if key.startswith("term-section:") else None,
        "mapping_status": mapping_status,
        "student_count": count,
    }


def _official_room(code, *parts):
    return {
        "room_code": code,
        "room_capacity": 40 if code != "UNASSIGNED" else 0,
        "gender": "F",
        "student_count": sum(part["student_count"] for part in parts),
        "section_parts": list(parts),
    }


def test_official_section_label_is_never_parsed_as_a_room_group():
    entry = _scheduled("CS111")
    entry["rooms"] = [
        _official_room("R1", _official_part("term-section:1", "F3/1", 10)),
        _official_room("R2", _official_part("term-section:2", "F3/2", 12)),
    ]
    assert derive_multi_sitting_details([entry]) == []

    entry["rooms"].append(_official_room("R3", _official_part("term-section:1", "F3/1", 7)))
    details = derive_multi_sitting_details([entry])
    assert len(details) == 1
    assert details[0]["section"] == "F3/1"
    assert details[0]["section_key"] == "term-section:1"
    assert details[0]["term_section_id"] == 1
    assert details[0]["mapping_status"] == "mapped"
    assert details[0]["gender"] == "F"
    assert details[0]["enrolment"] == 17


def test_equal_visible_sections_with_distinct_keys_do_not_merge():
    entry = _scheduled("CS111")
    entry["rooms"] = [
        _official_room("R1", _official_part("term-section:1", "F01", 10)),
        _official_room("R2", _official_part("term-section:2", "F01", 15)),
    ]
    assert derive_multi_sitting_details([entry]) == []
    entry["rooms"].extend(
        [
            _official_room("R3", _official_part("term-section:1", "F01", 5)),
            _official_room("R4", _official_part("term-section:2", "F01", 6)),
        ]
    )
    details = derive_multi_sitting_details([entry])
    assert len(details) == 2
    assert {d["section_key"]: d["enrolment"] for d in details} == {
        "term-section:1": 15,
        "term-section:2": 21,
    }


def test_packed_official_fragments_count_one_placement_per_section():
    entry = _scheduled("CS111")
    entry["rooms"] = [
        _official_room(
            "R1",
            _official_part("term-section:1", "F01", 6),
            _official_part("term-section:1", "F01", 4),
            _official_part("term-section:2", "F1", 8),
        ),
        _official_room("R2", _official_part("term-section:1", "F01", 12)),
    ]
    details = derive_multi_sitting_details([entry])
    assert len(details) == 1
    assert details[0]["sittings"] == 2
    assert details[0]["enrolment"] == 22
    assert details[0]["section"] == "F01"


def test_unmapped_groups_retain_mapping_reason_in_split_qa():
    entry = _scheduled("CS111")
    entry["rooms"] = [
        _official_room(room, _official_part(f"unmapped:F:{status}", "", 8, mapping_status=status))
        for status in ("missing", "ambiguous")
        for room in ("R1", "UNASSIGNED")
    ]
    details = derive_multi_sitting_details([entry])
    assert len(details) == 2
    by_status = {detail["mapping_status"]: detail for detail in details}
    for detail in details:
        assert detail["section"] == ""
        assert detail["term_section_id"] is None
        assert detail["enrolment"] == 16
        assert detail["incomplete"] is True
    assert "Section not recorded" in by_status["missing"]["audit_text"]
    assert "Ambiguous section" in by_status["ambiguous"]["audit_text"]

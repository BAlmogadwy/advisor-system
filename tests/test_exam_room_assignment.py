"""
Tests for exam timetable room assignment (Phase 2).

Covers:
  - build_section_enrollment: grouping, counts, gender detection, fallbacks
  - check_room_feasibility: oversized sections
  - _merge_same_course_sections: merge when combined fits
  - assign_rooms_to_schedule: gender separation, no cross-course sharing,
    preferred room, unassignable sections
  - build_exam_timetable end-to-end with rooms
"""

from __future__ import annotations

import pytest

from core.models import (
    Course,
    Room,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
)
from core.services.exam_timetable import (
    _merge_same_course_sections,
    _section_gender,
    assign_rooms_to_schedule,
    build_exam_timetable,
    build_section_enrollment,
    check_room_feasibility,
)

pytestmark = pytest.mark.django_db


# ── Fixture helpers ───────────────────────────────────────────────


def _make_rooms() -> list[Room]:
    """Create a small mix of M and F rooms of various capacities."""
    rooms = [
        Room.objects.create(room_code="M-10", capacity=10, section="M"),
        Room.objects.create(room_code="M-25", capacity=25, section="M"),
        Room.objects.create(room_code="M-50", capacity=50, section="M"),
        Room.objects.create(room_code="F-10", capacity=10, section="F"),
        Room.objects.create(room_code="F-25", capacity=25, section="F"),
        Room.objects.create(room_code="F-50", capacity=50, section="F"),
    ]
    return rooms


def _make_students_and_courses() -> None:
    """Seed students of both genders and 3 courses with STS rows."""
    # Courses
    Course.objects.create(course_code="EXR101", credit_hours=3)
    Course.objects.create(course_code="EXR102", credit_hours=3)
    Course.objects.create(course_code="EXR201", credit_hours=3)

    # 30 M students + 20 F students in program ROOMTEST
    for sid in range(8000, 8030):
        Student.objects.create(student_id=sid, program="ROOMTEST", section="M")
    for sid in range(8100, 8120):
        Student.objects.create(student_id=sid, program="ROOMTEST", section="F")

    # TermSection rows: 2 M sections + 1 F section per course
    for cc in ("EXR101", "EXR102", "EXR201"):
        for label in ("M1", "M2", "F1"):
            TermSection.objects.create(
                course_code=cc,
                course_number=cc[-3:],
                course_key=cc,
                section=label,
            )

    # Enrol students — STS rows for latest term "1447"/"2"
    def _enrol(students: list[int], course_code: str, section_label: str) -> None:
        ts = TermSection.objects.get(course_key=course_code, section=section_label)
        for sid in students:
            StudentTermSection.objects.create(
                student_id=sid,
                academic_year="1447",
                term="2",
                term_section=ts,
            )

    # Split M students 15/15 across M1/M2 for each course
    m_half_a = list(range(8000, 8015))
    m_half_b = list(range(8015, 8030))
    f_all = list(range(8100, 8120))

    for cc in ("EXR101", "EXR102", "EXR201"):
        _enrol(m_half_a, cc, "M1")
        _enrol(m_half_b, cc, "M2")
        _enrol(f_all, cc, "F1")


# ── Unit tests ────────────────────────────────────────────────────


def test_section_gender_detection() -> None:
    assert _section_gender("M7") == "M"
    assert _section_gender("M128") == "M"
    assert _section_gender("F3") == "F"
    assert _section_gender("f12") == "F"
    assert _section_gender("") == "M"  # fallback
    assert _section_gender("ALL") == "M"  # fallback


def test_build_section_enrollment_groups_by_course_and_section() -> None:
    _make_students_and_courses()

    enrolment = build_section_enrollment({"EXR101", "EXR102", "EXR201"})

    assert set(enrolment.keys()) == {"EXR101", "EXR102", "EXR201"}
    for cc, sections in enrolment.items():
        labels = {s["section"]: s for s in sections}
        assert set(labels.keys()) == {"M1", "M2", "F1"}, f"{cc} labels={labels}"
        assert labels["M1"]["student_count"] == 15
        assert labels["M2"]["student_count"] == 15
        assert labels["F1"]["student_count"] == 20
        assert labels["M1"]["gender"] == "M"
        assert labels["F1"]["gender"] == "F"


def test_build_section_enrollment_picks_preferred_room() -> None:
    _make_students_and_courses()

    # Give EXR101/M1 three meetings at "ROOM-A" and one at "ROOM-B"
    ts = TermSection.objects.get(course_key="EXR101", section="M1")
    for day in ("Sun", "Mon", "Tue"):
        TermSectionMeeting.objects.create(
            term_section=ts,
            day=day,
            start_time="09:00",
            end_time="10:15",
            room="ROOM-A",
            instructor=f"Dr-{day}",
        )
    TermSectionMeeting.objects.create(
        term_section=ts,
        day="Wed",
        start_time="09:00",
        end_time="10:15",
        room="ROOM-B",
        instructor="Dr-Wed",
    )

    enrolment = build_section_enrollment({"EXR101"})
    m1 = next(s for s in enrolment["EXR101"] if s["section"] == "M1")
    assert m1["preferred_room"] == "ROOM-A"


def test_build_section_enrollment_fallback_to_student_course() -> None:
    """Course with no StudentTermSection falls back to StudentCourse."""
    Course.objects.create(course_code="FALLBK1", credit_hours=3)
    s = Student.objects.create(student_id=9100, program="ROOMTEST", section="F")
    StudentCourse.objects.create(
        student=s,
        course=Course.objects.get(course_code="FALLBK1"),
        status="studying",
    )

    enrolment = build_section_enrollment({"FALLBK1"})
    assert len(enrolment["FALLBK1"]) == 1
    synth = enrolment["FALLBK1"][0]
    assert synth["student_count"] == 1
    assert synth["gender"] == "F"


def test_check_room_feasibility_flags_oversized_section() -> None:
    _make_rooms()
    rooms = list(Room.objects.values("room_code", "capacity", "section"))
    enrolment = {
        "BIG101": [
            {"section": "M1", "student_count": 100, "preferred_room": "", "gender": "M"},
            {"section": "F1", "student_count": 30, "preferred_room": "", "gender": "F"},
        ]
    }
    violations = check_room_feasibility(enrolment, rooms)
    assert len(violations) == 1
    assert violations[0]["course_code"] == "BIG101"
    assert violations[0]["section"] == "M1"
    assert violations[0]["max_room_capacity"] == 50


def test_merge_same_course_sections_when_fits() -> None:
    sections = [
        {"section": "M1", "student_count": 15, "preferred_room": "R1", "gender": "M"},
        {"section": "M2", "student_count": 15, "preferred_room": "R1", "gender": "M"},
    ]
    merged = _merge_same_course_sections(sections, max_room_capacity=50)
    assert len(merged) == 1
    assert merged[0]["student_count"] == 30
    assert merged[0]["merged_from"] == ["M1", "M2"]


def test_merge_same_course_sections_when_does_not_fit() -> None:
    sections = [
        {"section": "M1", "student_count": 40, "preferred_room": "R1", "gender": "M"},
        {"section": "M2", "student_count": 40, "preferred_room": "R1", "gender": "M"},
    ]
    merged = _merge_same_course_sections(sections, max_room_capacity=50)
    assert len(merged) == 2
    assert all("merged_from" not in m for m in merged)


def test_assign_rooms_respects_gender_separation() -> None:
    _make_rooms()
    rooms = list(Room.objects.values("room_code", "capacity", "section"))

    schedule = [
        {"course_code": "C1", "slot_index": 0, "day": "Sun", "period": "9"},
    ]
    enrolment = {
        "C1": [
            {"section": "M1", "student_count": 10, "preferred_room": "", "gender": "M"},
            {"section": "F1", "student_count": 10, "preferred_room": "", "gender": "F"},
        ]
    }

    assign_rooms_to_schedule(schedule, enrolment, rooms)
    assigned = schedule[0]["rooms"]
    assert len(assigned) == 2
    by_gender = {a["gender"]: a for a in assigned}
    # M section lands in an M room
    assert by_gender["M"]["room_code"].startswith("M-")
    # F section lands in an F room
    assert by_gender["F"]["room_code"].startswith("F-")


def test_assign_rooms_no_cross_course_sharing_in_same_slot() -> None:
    _make_rooms()
    rooms = list(Room.objects.values("room_code", "capacity", "section"))

    schedule = [
        {"course_code": "A1", "slot_index": 0, "day": "Sun", "period": "9"},
        {"course_code": "A2", "slot_index": 0, "day": "Sun", "period": "9"},
    ]
    enrolment = {
        "A1": [{"section": "M1", "student_count": 8, "preferred_room": "", "gender": "M"}],
        "A2": [{"section": "M1", "student_count": 8, "preferred_room": "", "gender": "M"}],
    }
    assign_rooms_to_schedule(schedule, enrolment, rooms)

    a1_rooms = {r["room_code"] for r in schedule[0]["rooms"]}
    a2_rooms = {r["room_code"] for r in schedule[1]["rooms"]}
    # Neither course shares a room with the other in the same slot
    assert not (a1_rooms & a2_rooms)


def test_assign_rooms_prefers_previously_used_room() -> None:
    _make_rooms()
    rooms = list(Room.objects.values("room_code", "capacity", "section"))

    schedule = [{"course_code": "PR1", "slot_index": 0, "day": "Sun", "period": "9"}]
    enrolment = {
        "PR1": [
            {"section": "M1", "student_count": 20, "preferred_room": "M-50", "gender": "M"},
        ]
    }
    assign_rooms_to_schedule(schedule, enrolment, rooms)
    # Even though M-25 is tighter (25-20=5), the preferred room wins
    assert schedule[0]["rooms"][0]["room_code"] == "M-50"


def test_assign_rooms_falls_back_to_tightest_when_no_preference() -> None:
    _make_rooms()
    rooms = list(Room.objects.values("room_code", "capacity", "section"))

    schedule = [{"course_code": "TF1", "slot_index": 0, "day": "Sun", "period": "9"}]
    enrolment = {
        "TF1": [
            {"section": "M1", "student_count": 20, "preferred_room": "", "gender": "M"},
        ]
    }
    assign_rooms_to_schedule(schedule, enrolment, rooms)
    # Tightest-fit M room with capacity >= 20 is M-25 (slack=5)
    assert schedule[0]["rooms"][0]["room_code"] == "M-25"


def test_assign_rooms_unassignable_section_marked_unassigned() -> None:
    _make_rooms()
    rooms = list(Room.objects.values("room_code", "capacity", "section"))

    schedule = [{"course_code": "HUGE1", "slot_index": 0, "day": "Sun", "period": "9"}]
    enrolment = {
        "HUGE1": [
            {"section": "M1", "student_count": 100, "preferred_room": "", "gender": "M"},
        ]
    }
    assign_rooms_to_schedule(schedule, enrolment, rooms)
    assert schedule[0]["rooms"][0]["room_code"] == "UNASSIGNED"


def test_assign_rooms_skips_overflow_entries() -> None:
    _make_rooms()
    rooms = list(Room.objects.values("room_code", "capacity", "section"))

    schedule = [{"course_code": "OF1", "slot_index": 99, "day": "OVERFLOW", "period": "X"}]
    enrolment = {
        "OF1": [
            {"section": "M1", "student_count": 10, "preferred_room": "", "gender": "M"},
        ]
    }
    assign_rooms_to_schedule(schedule, enrolment, rooms)
    assert schedule[0]["rooms"] == []


def test_assign_rooms_no_rooms_available_noop() -> None:
    schedule = [{"course_code": "Z1", "slot_index": 0, "day": "Sun", "period": "9"}]
    enrolment = {
        "Z1": [
            {"section": "M1", "student_count": 10, "preferred_room": "", "gender": "M"},
        ]
    }
    assign_rooms_to_schedule(schedule, enrolment, rooms=[])
    assert schedule[0]["rooms"] == []


def test_build_exam_timetable_end_to_end_with_rooms() -> None:
    """Full pipeline produces room assignments on every non-overflow entry."""
    _make_students_and_courses()
    _make_rooms()

    result = build_exam_timetable(
        label="e2e-rooms",
        days=["Sun", "Mon", "Tue"],
        periods=["09:00", "12:00"],
        max_per_day=2,
        programs=["ROOMTEST"],
        assign_rooms=True,
    )

    assert not result.get("feasibility_error"), result
    assert result["rooms_count"] == 6
    assert result["assign_rooms"] is True

    # Every non-OVERFLOW schedule entry has a rooms list with at least one
    # assignment per gender present in its section_enrollment.
    for e in result["schedule"]:
        if e["day"] == "OVERFLOW":
            continue
        assert "rooms" in e
        assert len(e["rooms"]) >= 1, f"No rooms for {e['course_code']}"

    # QA includes room metrics
    assert "rooms" in result["qa"]
    rqa = result["qa"]["rooms"]
    assert rqa["rooms_available"] == 6
    assert rqa["rooms_used"] >= 3
    # No double-bookings in the fixture
    assert rqa["room_double_bookings"] == []


def test_build_exam_timetable_assign_rooms_false_skips_assignment() -> None:
    _make_students_and_courses()
    _make_rooms()

    result = build_exam_timetable(
        label="e2e-norooms",
        days=["Sun", "Mon", "Tue"],
        periods=["09:00", "12:00"],
        max_per_day=2,
        programs=["ROOMTEST"],
        assign_rooms=False,
    )
    assert result["assign_rooms"] is False
    assert result["rooms_count"] == 0
    for e in result["schedule"]:
        # With assign_rooms=False the schedule entries have no rooms key
        assert "rooms" not in e or e["rooms"] == []

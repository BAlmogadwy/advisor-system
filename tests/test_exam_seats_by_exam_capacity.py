"""Exams seat by a room's exam seats, never by its teaching capacity (owner, 2026-09-28).

``Room.exam_capacity`` is what every exam path reads - Build, Check and the
exam CSV import here; Optimise, Fix and multistart in the parity tests - under
the key the exam engine has always read (``capacity``). A room never seats
more students in one period than its exam seats, a room with no exam seats
seats nobody, and a room's teaching capacity plays no part: not in rooming,
and not in the input fingerprint a saved run is checked against.
"""

from __future__ import annotations

import json
from collections import defaultdict

import pytest
from django.core.management import call_command
from django.db.models import F

from core import models
from core.models import (
    Course,
    ExamTimetableRun,
    Room,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
)
from core.services import exam_room_allocation
from core.services.exam_evaluation import evaluate_exam_schedule
from core.services.exam_room_inventory import EXAM_ROOM_FIELDS, exam_room_inventory
from core.services.exam_timetable import build_exam_timetable
from tests import exam_linked_rooms_corpus as corpus

pytestmark = pytest.mark.django_db

UNASSIGNED = "UNASSIGNED"


@pytest.fixture(autouse=True)
def _fresh_room_cache():
    """A period another test allocated must not answer for these boards."""
    with exam_room_allocation._CACHE_LOCK:
        exam_room_allocation._CACHE.clear()
    yield
    with exam_room_allocation._CACHE_LOCK:
        exam_room_allocation._CACHE.clear()


def _rooms(gender: str) -> list[tuple[str, int, int]]:
    """(code, teaching capacity, exam seats): every room's two numbers disagree."""
    return [
        (f"{gender}-ZERO", 300, 0),
        (f"{gender}-TIGHT", 300, 7),
        *((f"{gender}-MORE-{copy}", 4, 45) for copy in range(2)),
        *((f"{gender}-MID-{copy}", 200, 20) for copy in range(4)),
    ]


def _population() -> dict[str, int]:
    """The rooms parity population, on rooms whose exam seats are not their capacity."""
    corpus.create_rooms_population(models)
    Room.objects.all().delete()
    for gender in ("M", "F"):
        for code, teaching, exam in _rooms(gender):
            Room.objects.create(
                room_code=code,
                capacity=teaching,
                exam_capacity=exam,
                section=gender,
                building="North",
                floor=1,
            )
    return dict(Room.objects.values_list("room_code", "exam_capacity"))


def _build() -> dict:
    result = build_exam_timetable(
        "Exam seats",
        days=corpus.POPULATION_DAYS,
        periods=corpus.POPULATION_PERIODS,
        max_per_day=2,
        programs=["AI", "CS", "IS"],
        seed=5,
        assign_rooms=True,
        rebalance_invigilators=True,
        thin_conflict_threshold=0,
        persist=False,
    )
    assert result["status"] == "ok", result
    return result


def _check(build: dict) -> dict:
    return evaluate_exam_schedule(
        days=corpus.POPULATION_DAYS,
        periods=corpus.POPULATION_PERIODS,
        max_per_day=2,
        schedule_raw=build["schedule"],
        selected_courses=[entry["course_code"] for entry in build["schedule"]],
        assign_rooms=True,
        seed=5,
        thin_conflict_threshold=0,
        programs=["AI", "CS", "IS"],
        sections=[],
        pinned=[],
    )


def _assert_seated_by_exam_seats(result: dict, exam_seats: dict[str, int]) -> None:
    seated: dict[tuple[int, str], int] = defaultdict(int)
    rows = 0
    for entry in result["schedule"]:
        for row in entry.get("rooms") or []:
            code = row["room_code"]
            if code == UNASSIGNED:
                continue
            rows += 1
            assert row["room_capacity"] == exam_seats[code], (entry["course_code"], row)
            seated[(entry["slot_index"], code)] += row["student_count"]
    assert rows, "nothing was roomed: the test would prove nothing"
    over = {key: count for key, count in seated.items() if count > exam_seats[key[1]]}
    assert not over, f"rooms seat more than their exam seats: {over}"
    assert not {code for _slot, code in seated if exam_seats[code] == 0}, "a no-seat room was used"
    # Not vacuous: rooms whose teaching capacity is 4 seat more than 4 students.
    assert max(count for (_slot, code), count in seated.items() if "-MORE-" in code) > 4
    # Seats a room holds are its exam seats in the room QA too.
    physical = {(slot, code) for slot, code in seated}
    assert result["qa"]["rooms"]["total_capacity_used"] == sum(
        exam_seats[code] for _slot, code in physical
    )


def test_build_and_check_seat_by_exam_seats():
    exam_seats = _population()
    build = _build()
    _assert_seated_by_exam_seats(build, exam_seats)
    check = _check(build)
    _assert_seated_by_exam_seats(check, exam_seats)
    # A Check of the build's own board, on the same inventory: the same rooms,
    # and the inputs unchanged.
    assert check["input_fingerprint"] == build["input_fingerprint"]
    assert [entry["rooms"] for entry in check["schedule"]] == [
        entry["rooms"] for entry in build["schedule"]
    ]


def test_teaching_capacity_is_not_an_exam_input_and_exam_seats_are():
    _population()
    build = _build()
    first = _check(build)["input_fingerprint"]

    Room.objects.update(capacity=F("capacity") + 11)
    assert _check(build)["input_fingerprint"] == first

    used = next(
        row["room_code"]
        for entry in build["schedule"]
        for row in entry["rooms"]
        if row["room_code"] != UNASSIGNED
    )
    Room.objects.filter(room_code=used).update(exam_capacity=F("exam_capacity") + 1)
    assert _check(build)["input_fingerprint"] != first


def test_check_reports_a_change_to_any_room_even_one_the_run_never_used():
    """Check hashes the whole exam room inventory, not only the rooms a run used
    (the exam_room_inventory docstring): each edit below is to a room the build
    never seated anyone in, and each one alone is an input change."""
    _population()
    build = _build()
    used = {row["room_code"] for entry in build["schedule"] for row in entry["rooms"]}
    assert "M-ZERO" not in used and "F-ZERO" not in used
    fingerprints = [_check(build)["input_fingerprint"]]

    Room.objects.filter(room_code="M-ZERO").update(building="South")
    fingerprints.append(_check(build)["input_fingerprint"])
    Room.objects.filter(room_code="F-ZERO").update(department="IS")
    fingerprints.append(_check(build)["input_fingerprint"])
    Room.objects.create(room_code="M-NEW", capacity=30, exam_capacity=0, section="M")
    fingerprints.append(_check(build)["input_fingerprint"])

    assert len(set(fingerprints)) == len(fingerprints), fingerprints


def _csv_population(tmp_path, students: int = 12):
    course = Course.objects.create(course_code="SEAT101", credit_hours=3)
    section = TermSection.objects.create(
        course_key="SEAT101", course_code="SEAT101", course_number="101", section="M1"
    )
    for number in range(students):
        student = Student.objects.create(student_id=772000 + number, program="CS", section="M")
        StudentCourse.objects.create(student=student, course=course, status="studying")
        StudentTermSection.objects.create(
            student_id=student.student_id,
            term_section=section,
            academic_year="1448",
            term="1",
            source="scraper_timetable",
        )
    csv_path = tmp_path / "manual_exam.csv"
    csv_path.write_text(
        "Day,Date,Period,Time,Course Name,Course Code\n"
        "Tuesday,16/12/1447 H - 02/06/2026,Period 1,08:30 AM - 10:30 AM,Seats,SEAT101\n",
        encoding="utf-8",
    )
    return csv_path


def test_the_exam_csv_import_seats_by_exam_seats(tmp_path):
    csv_path = _csv_population(tmp_path)
    # By teaching capacity ZERO alone would seat all twelve; by exam seats it
    # seats nobody, and MORE (teaching 4) seats ten.
    Room.objects.create(room_code="ZERO", capacity=40, exam_capacity=0, section="M")
    Room.objects.create(room_code="CAP", capacity=40, exam_capacity=5, section="M")
    Room.objects.create(room_code="MORE", capacity=4, exam_capacity=10, section="M")
    call_command("import_exam_timetable_csv", str(csv_path), label="Seats import")

    payload = json.loads(ExamTimetableRun.objects.get(label="Seats import").result_json)
    rows = [row for entry in payload["schedule"] for row in entry["rooms"]]
    seats = {"CAP": 5, "MORE": 10}
    assert {row["room_code"] for row in rows} == set(seats)
    for row in rows:
        assert row["room_capacity"] == seats[row["room_code"]]
        assert row["student_count"] <= seats[row["room_code"]]
    assert sum(row["student_count"] for row in rows) == 12


def test_the_inventory_hands_exam_seats_on_as_capacity_in_the_usual_shape():
    Room.objects.create(
        room_code="B2", capacity=40, exam_capacity=25, section="F", building="TV", floor=2
    )
    Room.objects.create(room_code="A1", capacity=30, exam_capacity=0, section="M", room_type="lab")
    rows = exam_room_inventory(order_by=("room_code",), extra_fields=("room_type",))
    assert [list(row) for row in rows] == [[*EXAM_ROOM_FIELDS, "room_type"]] * 2
    assert rows == [
        {
            "room_code": "A1",
            "capacity": 0,
            "section": "M",
            "department": "",
            "building": "",
            "floor": None,
            "room_type": "lab",
        },
        {
            "room_code": "B2",
            "capacity": 25,
            "section": "F",
            "department": "",
            "building": "TV",
            "floor": 2,
            "room_type": "lecture",
        },
    ]
    assert EXAM_ROOM_FIELDS == (
        "room_code",
        "capacity",
        "section",
        "department",
        "building",
        "floor",
    )

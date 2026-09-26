"""Students with two or more exams in a day, and the exam pairs they sit.

The card counts students; its detail lists exam pairs with how many students
sit both on that day. Neither ever names a student.
"""

import json
from copy import deepcopy

import pytest
from django.core.cache import cache
from django.urls import reverse

from core.models import Course, ExamTimetableRun, ProgrammeRequirement, Room, Student
from core.services.exam_timetable import _build_qa, build_exam_timetable
from tests.exam_source_factory import scraped_exam_registration

SLOTS = {
    ("Sun", "08:00-10:00"): 0,
    ("Sun", "10:30-12:30"): 1,
    ("Sun", "13:00-15:00"): 2,
    ("Mon", "08:00-10:00"): 3,
    ("Mon", "10:30-12:30"): 4,
}
EARLY, MID, LATE = "08:00-10:00", "10:30-12:30", "13:00-15:00"


def _board(placements: dict[str, tuple[str, str]]) -> list[dict]:
    return [
        {
            "course_code": code,
            "day": day,
            "period": period,
            "slot_index": 99 if day == "OVERFLOW" else SLOTS[(day, period)],
        }
        for code, (day, period) in placements.items()
    ]


def _exam(code: str, day: str, period: str) -> dict:
    return {"code": code, "slot_index": SLOTS[(day, period)], "period": period}


def _pairs(qa: dict) -> list[tuple]:
    return [
        (
            row["day"],
            *(exam["code"] for exam in row["courses"]),
            row["student_count"],
            row["clash"],
        )
        for row in qa["same_day_exam_pairs"]
    ]


def test_three_exams_on_a_day_make_three_pairs_each_counting_its_students():
    enrolled = {"A": {1, 2, 3}, "B": {1, 2}, "C": {1}, "D": {4}}
    board = _board(
        {"A": ("Sun", EARLY), "B": ("Sun", MID), "C": ("Sun", LATE), "D": ("Mon", EARLY)}
    )

    qa = _build_qa(enrolled, board)

    assert qa["multi_exam_day_students"] == 2
    assert qa["same_day_exam_pairs"] == [
        {
            "day": "Sun",
            "courses": [_exam("A", "Sun", EARLY), _exam("B", "Sun", MID)],
            "student_count": 2,
            "clash": False,
        },
        {
            "day": "Sun",
            "courses": [_exam("A", "Sun", EARLY), _exam("C", "Sun", LATE)],
            "student_count": 1,
            "clash": False,
        },
        {
            "day": "Sun",
            "courses": [_exam("B", "Sun", MID), _exam("C", "Sun", LATE)],
            "student_count": 1,
            "clash": False,
        },
    ]


def test_a_student_with_several_multi_exam_days_is_one_student_in_the_count():
    enrolled = {"A": {7}, "B": {7}, "C": {7}, "D": {7, 8}}
    board = _board(
        {"A": ("Sun", EARLY), "B": ("Sun", LATE), "C": ("Mon", EARLY), "D": ("Mon", MID)}
    )

    qa = _build_qa(enrolled, board)

    assert qa["multi_exam_day_students"] == 1
    assert _pairs(qa) == [("Sun", "A", "B", 1, False), ("Mon", "C", "D", 1, False)]


def test_a_same_period_pair_is_listed_and_marked_as_the_clash_conflicts_counts():
    enrolled = {"A": {1}, "B": {1}, "C": {1}}
    board = _board({"B": ("Sun", EARLY), "A": ("Sun", EARLY), "C": ("Sun", MID)})

    qa = _build_qa(enrolled, board)

    assert qa["conflict_count"] == 1
    assert qa["multi_exam_day_students"] == 1
    # Same period: the codes order the pair. Otherwise the earlier exam leads.
    assert _pairs(qa) == [
        ("Sun", "A", "B", 1, True),
        ("Sun", "A", "C", 1, False),
        ("Sun", "B", "C", 1, False),
    ]


def test_the_earlier_exam_leads_its_pair_whatever_its_code():
    qa = _build_qa(
        {"ZOO101": {1}, "ART101": {1}}, _board({"ZOO101": ("Sun", EARLY), "ART101": ("Sun", LATE)})
    )

    assert _pairs(qa) == [("Sun", "ZOO101", "ART101", 1, False)]


def test_overflow_exams_are_never_a_day_and_never_pair():
    enrolled = {"A": {1, 2}, "B": {1}, "C": {2}}
    board = _board({"A": ("OVERFLOW", ""), "B": ("OVERFLOW", ""), "C": ("Sun", EARLY)})

    qa = _build_qa(enrolled, board)

    assert qa["multi_exam_day_students"] == 0
    assert qa["same_day_exam_pairs"] == []


def test_pairs_sort_by_students_then_timetable_day_order_then_codes():
    # Mon sorts before Sun alphabetically; the timetable puts Sunday first.
    enrolled = {"A": {1}, "B": {1}, "C": {2, 3}, "D": {3, 4, 5}, "E": {2, 4, 5}}
    board = _board(
        {
            "A": ("Mon", EARLY),
            "B": ("Mon", MID),
            "C": ("Sun", EARLY),
            "D": ("Sun", MID),
            "E": ("Sun", LATE),
        }
    )

    qa = _build_qa(enrolled, board)

    assert qa["multi_exam_day_students"] == 5
    assert _pairs(qa) == [
        ("Sun", "D", "E", 2, False),
        ("Sun", "C", "D", 1, False),
        ("Sun", "C", "E", 1, False),
        ("Mon", "A", "B", 1, False),
    ]


def test_one_exam_a_day_is_a_measured_zero():
    qa = _build_qa({"A": {1, 2}, "B": {1}}, _board({"A": ("Sun", EARLY), "B": ("Mon", EARLY)}))

    assert qa["multi_exam_day_students"] == 0
    assert qa["same_day_exam_pairs"] == []


def test_the_pair_detail_carries_no_student_identifier():
    students = {4410001, 4410002, 4410003}
    enrolled = {"A": set(students), "B": {4410001, 4410002}, "C": {4410003}}
    board = _board({"A": ("Sun", EARLY), "B": ("Sun", MID), "C": ("Sun", MID)})

    qa = _build_qa(enrolled, board)

    assert isinstance(qa["multi_exam_day_students"], int)
    detail = json.dumps(qa["same_day_exam_pairs"])
    assert not any(str(sid) in detail for sid in students)
    for row in qa["same_day_exam_pairs"]:
        assert set(row) == {"day", "courses", "student_count", "clash"}
        assert all(set(exam) == {"code", "slot_index", "period"} for exam in row["courses"])


# ── Build, Check, Save and a reload carry the same figures ──

DAYS = ["Sun", "Mon"]
PERIODS = [EARLY, LATE]


@pytest.fixture
def board(client, django_user_model, monkeypatch):
    """Four students, three courses; pins decide where every exam sits."""
    monkeypatch.setattr("core.authz._rate_buckets", {})
    cache.clear()
    client.force_login(django_user_model.objects.create_superuser(username="pairs-editor"))
    for sid in (51, 52, 53, 54):
        Student.objects.create(student_id=sid, program="AI", section="M")
    members = {"CS101": (51, 52, 53), "CS102": (51, 52), "CS103": (51, 53, 54)}
    for term, (code, sids) in enumerate(members.items(), start=1):
        course = Course.objects.create(course_code=code, description=f"{code} name", credit_hours=3)
        ProgrammeRequirement.objects.create(
            program="AI", course_code=code, course_name=code, programme_term=term
        )
        for sid in sids:
            scraped_exam_registration(sid, course)
    Room.objects.create(room_code="M001", capacity=40, section="M", building="B1", floor="1")
    pinned = [
        {"course_code": "CS101", "day": "Sun", "period": EARLY},
        {"course_code": "CS102", "day": "Sun", "period": LATE},
        {"course_code": "CS103", "day": "Mon", "period": EARLY},
    ]
    built = build_exam_timetable(
        label="Same-day pairs",
        days=DAYS,
        periods=PERIODS,
        programs=["AI"],
        sections=["M"],
        pinned=pinned,
        seed=3,
    )
    return client, built


# Students 51 and 52 sit CS101 and CS102 on Sunday; CS103 is alone on Monday.
EXPECTED_PAIRS = [("Sun", "CS101", "CS102", 2, False)]


@pytest.mark.django_db
def test_build_reports_the_count_and_pairs(board):
    _client, built = board
    assert built["qa"]["multi_exam_day_students"] == 2
    assert _pairs(built["qa"]) == EXPECTED_PAIRS


@pytest.mark.django_db
def test_check_save_and_reload_agree_with_build_and_moves_recount(board):
    client, built = board
    payload = {
        "label": "Moved",
        "previous_run_id": built["run_id"],
        "base_schedule": deepcopy(built["schedule"]),
        "days": DAYS,
        "periods": PERIODS,
        "max_per_day": 2,
        "selected_courses": built["courses"],
        "assign_rooms": True,
        "thin_conflict_threshold": 0,
        "pinned": [],
        "editor_revision": 1,
    }
    check = client.post(
        reverse("exam_timetable_draft_impact"), payload, content_type="application/json"
    ).json()
    assert check["qa"]["multi_exam_day_students"] == built["qa"]["multi_exam_day_students"]
    assert check["qa"]["same_day_exam_pairs"] == built["qa"]["same_day_exam_pairs"]

    # CS103 joins Sunday in CS101's period: 51 sits three exams that day and
    # 53 two at once. 54 only moves its one exam, so it is still not counted.
    for row in payload["base_schedule"]:
        if row["course_code"] == "CS103":
            row.update(day="Sun", period=EARLY, slot_index=0)
    moved = client.post(
        reverse("exam_timetable_draft_impact"), payload, content_type="application/json"
    ).json()
    assert moved["qa"]["multi_exam_day_students"] == 3
    assert _pairs(moved["qa"]) == [
        ("Sun", "CS101", "CS102", 2, False),
        ("Sun", "CS101", "CS103", 2, True),
        ("Sun", "CS103", "CS102", 1, False),
    ]

    payload["expected_input_fingerprint"] = moved["input_fingerprint"]
    saved = client.post(
        reverse("exam_timetable_build"),
        {**payload, "mode": "save_loaded_changes"},
        content_type="application/json",
    ).json()
    reloaded = client.get(reverse("exam_timetable_detail", args=[saved["run_id"]])).json()
    assert reloaded["qa"]["multi_exam_day_students"] == 3
    assert reloaded["qa"]["same_day_exam_pairs"] == moved["qa"]["same_day_exam_pairs"]


@pytest.mark.django_db
def test_a_run_saved_before_the_metric_reloads_without_an_invented_zero(board):
    client, built = board
    run = ExamTimetableRun.objects.get(pk=built["run_id"])
    data = json.loads(run.result_json)
    del data["qa"]["multi_exam_day_students"]
    del data["qa"]["same_day_exam_pairs"]
    run.result_json = json.dumps(data)
    run.save(update_fields=["result_json"])

    reloaded = client.get(reverse("exam_timetable_detail", args=[run.pk])).json()

    assert "multi_exam_day_students" not in reloaded["qa"]
    assert "same_day_exam_pairs" not in reloaded["qa"]

"""Fixed external exam times survive every scheduler and saved-run operation."""

from copy import deepcopy

import pytest
from django.core.cache import cache
from django.urls import reverse

from core.models import Course, ExamTimetableRun, ProgrammeRequirement, Room, Student, StudentCourse
from core.services.exam_multistart import run_multistart
from core.services.exam_run_schema import load_normalised_run
from core.services.exam_timetable import (
    _rebalance_invigilators_pass,
    build_enrolled_sets_with_meta,
    build_exam_timetable,
    check_bucket_feasibility,
    schedule,
)
from tests.exam_source_factory import scraped_exam_registration

pytestmark = pytest.mark.django_db
PERIODS = ["08:00-10:00", "13:00-15:00"]
PIN = {"course_code": "EX101", "day": "Mon", "period": PERIODS[1]}


@pytest.fixture
def exam_courses():
    student = Student.objects.create(student_id=900101, program="AI", section="F")
    for code in ["EX101", "EX102"]:
        course = Course.objects.create(course_code=code, description=code, credit_hours=3)
        StudentCourse.objects.create(student=student, course=course, status="studying")
        scraped_exam_registration(student, course)
        ProgrammeRequirement.objects.create(
            program="AI", course_code=code, course_name=code, programme_term=1
        )


@pytest.fixture
def exam_client(client, django_user_model, monkeypatch, exam_courses):
    monkeypatch.setattr("core.authz._rate_buckets", {})
    cache.clear()
    client.force_login(django_user_model.objects.create_superuser(username="pins-admin"))
    return client


def _payload(**changes):
    return {
        "label": "External exam pins",
        "days": ["Sun", "Mon"],
        "periods": PERIODS,
        "programs": ["AI"],
        "sections": ["F"],
        "assign_rooms": False,
        "pinned": [PIN],
        **changes,
    }


def _post(client, **changes):
    return client.post(
        reverse("exam_timetable_build"), _payload(**changes), content_type="application/json"
    )


def _assert_pins(data, expected):
    assert data["pinned"] == expected
    placements = {entry["course_code"]: entry for entry in data["schedule"]}
    for pin in expected:
        assert placements[pin["course_code"]]["day"] == pin["day"]
        assert placements[pin["course_code"]]["period"] == pin["period"]


def test_fresh_build_applies_pins_and_avoids_them_for_other_courses(exam_client):
    response = _post(exam_client)
    assert response.status_code == 200, response.content
    result = response.json()
    _assert_pins(result, [PIN])
    assert result["qa"]["conflict_count"] == 0
    assert next(e for e in result["schedule"] if e["course_code"] == "EX102")["day"] == "Sun"
    _assert_pins(load_normalised_run(ExamTimetableRun.objects.get(pk=result["run_id"])), [PIN])


@pytest.mark.parametrize(
    "pins",
    [
        None,
        {},
        "EX101",
        [None],
        ["EX101"],
        [{}],
        [{**PIN, "course_code": ""}],
        [{**PIN, "day": " "}],
        [{**PIN, "period": None}],
        [{**PIN, "course_code": 101}],
        [{**PIN, "course_code": "UNKNOWN"}],
        [{**PIN, "day": "Tue"}],
        [{**PIN, "day": "OVERFLOW"}],
        [{**PIN, "period": "09:00-11:00"}],
        [PIN, PIN],
        [PIN, {**PIN, "day": "Sun"}],
    ],
)
def test_invalid_pins_are_never_silently_discarded_or_persisted(exam_client, pins):
    response = _post(exam_client, pinned=pins)
    assert response.status_code == 400, response.content
    assert "pin" in response.json()["error"].lower()
    assert not ExamTimetableRun.objects.exists()


def test_deselected_course_pin_is_rejected(exam_client):
    response = _post(exam_client, selected_courses=["EX102"])
    assert response.status_code == 400
    assert "not selected" in response.json()["error"]
    assert not ExamTimetableRun.objects.exists()


def test_pin_validation_precedes_bucket_feasibility(exam_client):
    response = _post(exam_client, days=["Sun"], pinned=[PIN])
    assert response.status_code == 400
    assert "outside this timetable" in response.json()["error"]
    assert not ExamTimetableRun.objects.exists()


def test_deliberate_conflicting_pins_are_honored_and_reported(exam_client):
    pins = [{**PIN, "day": "Sun"}, {**PIN, "course_code": "EX102", "day": "Sun"}]
    response = _post(exam_client, days=["Sun"], pinned=pins)
    assert response.status_code == 200, response.content
    data = response.json()
    _assert_pins(data, pins)
    assert data["qa"]["conflict_count"] == 1
    assert data["qa"]["manual_override_count"] == 2
    assert {row["kind"] for row in data["qa"]["manual_override_details"]} == {
        "same_slot",
        "bucket_day",
    }
    assert data["qa"]["bucket_day_violations"]
    assert "manual_override" in data["status_flags"]


def test_unpinned_bucket_still_requires_enough_days(exam_client):
    response = _post(exam_client, days=["Sun"], pinned=[])
    assert response.status_code == 400
    assert response.json()["feasibility_error"] is True
    assert not ExamTimetableRun.objects.exists()


def test_feasibility_reserves_days_for_the_remaining_unpinned_courses():
    buckets = {("AI", 1): {"A", "B", "C"}}
    same_day = [{"course_code": code, "day": "Sun", "period": PERIODS[0]} for code in ["A", "B"]]
    assert check_bucket_feasibility(buckets, 2, pinned=same_day) == []
    distinct_days = [same_day[0], {**same_day[1], "day": "Mon"}]
    assert check_bucket_feasibility(buckets, 2, pinned=distinct_days)


def test_save_optimize_and_reload_retain_fixed_times(exam_client):
    response = _post(exam_client)
    assert response.status_code == 200
    data = response.json()
    for mode in ["save_loaded_changes", "optimize_loaded"]:
        response = _post(
            exam_client, mode=mode, previous_run_id=data["run_id"], base_schedule=data["schedule"]
        )
        assert response.status_code == 200, response.content
        data = response.json()
        _assert_pins(data, [PIN])
        response = exam_client.get(reverse("exam_timetable_detail", args=[data["run_id"]]))
        assert response.status_code == 200
        _assert_pins(response.json(), [PIN])


def test_save_rejects_pin_that_disagrees_with_visible_schedule(exam_client):
    built = _post(exam_client).json()
    response = _post(
        exam_client,
        mode="save_loaded_changes",
        previous_run_id=built["run_id"],
        base_schedule=built["schedule"],
        pinned=[{**PIN, "day": "Sun"}],
    )
    assert response.status_code == 400
    assert "visible schedule placement" in response.json()["error"]
    assert ExamTimetableRun.objects.count() == 1


def test_optimize_applies_a_new_fixed_time(exam_client):
    built = _post(exam_client).json()
    changed = [{**PIN, "day": "Sun"}]
    response = _post(
        exam_client,
        mode="optimize_loaded",
        previous_run_id=built["run_id"],
        base_schedule=built["schedule"],
        pinned=changed,
    )
    assert response.status_code == 200, response.content
    _assert_pins(response.json(), changed)


@pytest.mark.parametrize("mode", ["save_loaded_changes", "optimize_loaded"])
def test_loaded_actions_reject_invalid_pins_without_saving(exam_client, mode):
    built = _post(exam_client).json()
    response = _post(
        exam_client,
        mode=mode,
        previous_run_id=built["run_id"],
        base_schedule=built["schedule"],
        pinned=[{**PIN, "course_code": "UNKNOWN"}],
    )
    assert response.status_code == 400
    assert ExamTimetableRun.objects.count() == 1


def test_explicitly_cleared_pins_stay_cleared_on_reload(exam_client):
    built = _post(exam_client).json()
    response = _post(
        exam_client,
        mode="save_loaded_changes",
        previous_run_id=built["run_id"],
        base_schedule=built["schedule"],
        pinned=[],
    )
    assert response.status_code == 200, response.content
    result = response.json()
    assert result["pinned"] == []
    assert load_normalised_run(ExamTimetableRun.objects.get(pk=result["run_id"]))["pinned"] == []


def test_same_registrar_code_variants_can_have_different_pins():
    course = Course.objects.create(course_code="CS111", credit_hours=3)
    for sid, program, name in [(1, "AI", "Programming I"), (2, "AI2", "Fundamentals")]:
        student = Student.objects.create(student_id=sid, program=program, section="F")
        StudentCourse.objects.create(student=student, course=course, status="studying")
        scraped_exam_registration(student, course)
        ProgrammeRequirement.objects.create(
            program=program, course_code="CS111", course_name=name, programme_term=1
        )
    enrolled, metadata = build_enrolled_sets_with_meta()
    codes = sorted(enrolled)
    pins = [
        {"course_code": codes[0], "day": "Sun", "period": PERIODS[0]},
        {"course_code": codes[1], "day": "Mon", "period": PERIODS[1]},
    ]
    result = build_exam_timetable(**_payload(programs=["AI", "AI2"], pinned=pins), persist=False)
    _assert_pins(result, pins)
    assert len({entry["course_identity"] for entry in result["schedule"]}) == 2

    # Filtering changes display ordinals; explicit preview identity keeps its alias.
    kept = codes[1]
    result = build_exam_timetable(
        **_payload(
            programs=metadata[kept]["programs"],
            selected_courses=[kept],
            selected_course_entries=[{"course_code": kept, **metadata[kept]}],
            pinned=[pins[1]],
        ),
        persist=False,
    )
    _assert_pins(result, [pins[1]])
    assert result["students_count"] == 1


def test_multistart_persists_pins_for_every_winner(exam_courses):
    report = run_multistart(**_payload(), seeds=[1, 2, 3])
    assert report.candidates_by_role
    for candidate in report.candidates_by_role.values():
        _assert_pins(candidate.payload, [PIN])
    for run in ExamTimetableRun.objects.all():
        _assert_pins(load_normalised_run(run), [PIN])


def test_multistart_invalid_pins_never_save_candidates(exam_courses):
    with pytest.raises(ValueError, match="not selected"):
        run_multistart(**_payload(pinned=[{**PIN, "course_code": "UNKNOWN"}]), seeds=[1, 2])
    assert not ExamTimetableRun.objects.exists()


def test_low_level_scheduler_rejects_invalid_pins():
    with pytest.raises(ValueError, match="outside this timetable"):
        schedule(["EX101"], {}, [{"index": 0, "day": "Sun", "period": PERIODS[0]}], pinned=[PIN])


def test_invigilator_balancing_never_moves_pinned_courses():
    slots = [{"index": i, "day": day, "period": PERIODS[0]} for i, day in enumerate(["Sun", "Mon"])]
    entries = [
        {"course_code": code, "day": day, "period": PERIODS[0], "slot_index": idx}
        for code, day, idx in [("A", "Sun", 0), ("B", "Sun", 0), ("C", "Sun", 0), ("D", "Mon", 1)]
    ]
    sections = {
        code: [{"section": "F", "gender": "F", "student_count": 50, "preferred_room": ""}]
        for code in ["A", "B", "C", "D"]
    }
    rooms = [{"room_code": f"F-{i}", "capacity": 50, "section": "F"} for i in range(3)]
    movable = deepcopy(entries)
    assert _rebalance_invigilators_pass(movable, sections, rooms, slots, {}, {}, {}) > 0
    partly_fixed = deepcopy(entries)
    assert (
        _rebalance_invigilators_pass(
            partly_fixed, sections, rooms, slots, {}, {}, {}, pinned_courses={"A"}
        )
        > 0
    )
    assert next(entry for entry in partly_fixed if entry["course_code"] == "A")["day"] == "Sun"
    fixed = deepcopy(entries)
    assert (
        _rebalance_invigilators_pass(
            fixed, sections, rooms, slots, {}, {}, {}, pinned_courses={"A", "B", "C"}
        )
        == 0
    )
    assert [(e["course_code"], e["day"]) for e in fixed] == [
        (e["course_code"], e["day"]) for e in entries
    ]


def test_room_enabled_build_preserves_all_fixed_exams():
    pins = []
    for index, code in enumerate(["A", "B", "C", "D"]):
        course = Course.objects.create(course_code=code, credit_hours=3)
        for offset in range(50):
            student = Student.objects.create(
                student_id=index * 50 + offset + 1, program="AI", section="F"
            )
            StudentCourse.objects.create(student=student, course=course, status="studying")
            scraped_exam_registration(student, course)
        pins.append(
            {"course_code": code, "day": "Sun" if index < 3 else "Mon", "period": PERIODS[0]}
        )
    for index in range(3):
        Room.objects.create(room_code=f"F-{index}", capacity=50, section="F")
    result = build_exam_timetable(**_payload(assign_rooms=True, pinned=pins), persist=False)
    _assert_pins(result, pins)
    assert result["qa"]["rebalance_moves"] == 0

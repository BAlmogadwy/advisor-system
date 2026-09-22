"""Real teaching sections preserve canonical populations and reviewed inputs."""

from collections import Counter

import pytest
from django.urls import reverse

from core import exam_views
from core.models import (
    Course,
    ExamTimetableRun,
    ProgrammeRequirement,
    Room,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services import exam_timetable
from core.services.exam_run_schema import derive_multi_sitting_details
from core.services.exam_sections import (
    resolve_exam_section_enrollment,
    summarize_exam_section_mapping,
)
from tests.test_exam_export_source import export_client as export_client
from tests.test_exam_export_source import export_courses as export_courses

pytestmark = pytest.mark.django_db
PERIODS = ["08:00-10:00", "13:00-15:00"]
DAYS = ["Sun", "Mon", "Tue"]


def _student(sid=101, gender="F", program="AI"):
    return Student.objects.create(student_id=sid, program=program, section=gender)


def _section(label, code="CS113", **kwargs):
    defaults = {
        "course_code": code.rstrip("0123456789"),
        "course_number": code.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
        "course_key": code,
        "course_name": "برمجة (2)",
        "source_tag": "imported_catalogue",
    }
    defaults.update(kwargs)
    return TermSection.objects.create(section=label, **defaults)


def _link(student, section, source="scraper_timetable", year="1448", term="1"):
    return StudentTermSection.objects.create(
        student_id=student.student_id,
        term_section=section,
        academic_year=year,
        term=term,
        source=source,
    )


def _resolve(*students):
    return resolve_exam_section_enrollment(
        {"CS113": {student.student_id for student in students}},
        course_meta={"CS113": {"source_course_code": "CS113"}},
    )["CS113"]


def _post(client, route, payload):
    response = client.post(reverse(route), payload, content_type="application/json")
    assert response.status_code == 200, response.content
    return response.json()


def _placements(result):
    return {
        entry["course_identity"]: (entry["day"], entry["period"]) for entry in result["schedule"]
    }


@pytest.mark.parametrize("label", ["F01", "F1", "F1/2", "F1+F2", "شعبة أ"])
def test_official_section_names_are_preserved_exactly(label):
    student = _student()
    section = _section(label)
    _link(student, section)
    groups = _resolve(student)
    assert len(groups) == 1
    assert groups[0]["section"] == label
    assert groups[0]["section_key"] == f"term-section:{section.pk}"
    assert groups[0]["term_section_id"] == section.pk
    assert groups[0]["mapping_status"] == "mapped"
    assert groups[0]["student_count"] == 1
    assert groups[0]["gender"] == "F"


def test_registered_term_wins_over_future_forecast_and_catalogue_source_tag():
    student = _student()
    actual = _section("F01", source_tag="plan_1448_T1")
    _link(student, actual)
    _link(student, _section("F99"), source="registration_plan_1449_t1", year="1449")
    groups = _resolve(student)
    assert [(group["section"], group["academic_year"], group["term"]) for group in groups] == [
        ("F01", "1448", "1")
    ]
    assert groups[0]["mapping_source"] == "scraper_timetable"


@pytest.mark.parametrize(
    "source", ["registration_plan_1448_t1", "planner", "auto_from_studying", "manual", ""]
)
def test_forecast_or_working_rows_never_become_real_sections(source):
    student = _student()
    _link(student, _section("F1"), source=source)
    groups = _resolve(student)
    assert len(groups) == 1
    assert groups[0]["mapping_status"] == "missing"
    assert groups[0]["section"] == ""
    assert groups[0]["term_section_id"] is None
    assert groups[0]["student_count"] == 1


@pytest.mark.parametrize("label", ["M1", "YM1", "YF1", "YF01", ""])
def test_wrong_cohort_other_branch_and_blank_sections_remain_unmapped(label):
    student = _student()
    _link(student, _section(label))
    groups = _resolve(student)
    assert [
        (group["mapping_status"], group["gender"], group["student_count"]) for group in groups
    ] == [("missing", "F", 1)]


def test_scenario_owned_registrar_link_is_not_a_global_teaching_section():
    student = _student()
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="Draft")
    _link(student, _section("F1", scenario=scenario))
    assert _resolve(student)[0]["mapping_status"] == "missing"


@pytest.mark.parametrize("gender", ["", "X", "Female", "MISSING"])
def test_unknown_student_cohort_stays_unmapped_and_never_uses_gendered_rooms(gender):
    student = _student(gender=gender)
    _link(student, _section("F1"))
    groups = _resolve(student)
    assert [
        (group["mapping_status"], group["gender"], group["student_count"]) for group in groups
    ] == [("missing", "U", 1)]
    entry = {"course_code": "CS113", "slot_index": 0, "day": "Sun", "period": PERIODS[0]}
    exam_timetable.assign_rooms_to_schedule(
        [entry],
        {"CS113": groups},
        [
            {"room_code": "MALE", "section": "M", "capacity": 50},
            {"room_code": "FEMALE", "section": "F", "capacity": 50},
        ],
    )
    assert [
        (room["room_code"], room["gender"], room["student_count"]) for room in entry["rooms"]
    ] == [("UNASSIGNED", "U", 1)]


def test_course_key_uses_existing_code_number_fallback_when_key_is_blank():
    student = _student()
    actual = _section("F1", course_key="", course_code="CS", course_number="113")
    _link(student, actual)
    groups = _resolve(student)
    assert groups[0]["term_section_id"] == actual.pk
    assert groups[0]["mapping_status"] == "mapped"


def test_multiple_registered_sections_are_ambiguous_and_count_each_student_once():
    ambiguous = _student(101)
    mapped = _student(102)
    missing = _student(103)
    first, second = _section("F1"), _section("F2")
    _link(ambiguous, first)
    _link(ambiguous, second)
    _link(mapped, first)
    _link(mapped, second, source="registration_plan_1448_t1")
    groups = _resolve(ambiguous, mapped, missing)
    assert Counter({group["mapping_status"]: group["student_count"] for group in groups}) == {
        "mapped": 1,
        "ambiguous": 1,
        "missing": 1,
    }
    assert sum(group["student_count"] for group in groups) == 3
    assert next(group for group in groups if group["mapping_status"] == "mapped")["section"] == "F1"
    assert (
        next(group for group in groups if group["mapping_status"] == "ambiguous")["section"] == ""
    )
    summary = summarize_exam_section_mapping({"CS113": groups})
    assert {
        key: summary[key]
        for key in (
            "mapped_sections",
            "mapped_enrollments",
            "missing_enrollments",
            "ambiguous_enrollments",
        )
    } == {
        "mapped_sections": 1,
        "mapped_enrollments": 1,
        "missing_enrollments": 1,
        "ambiguous_enrollments": 1,
    }


def test_meetings_and_duplicate_source_links_do_not_multiply_demand():
    students = [_student(101), _student(102)]
    actual = _section("F01", registered_count=999)
    for student in students:
        _link(student, actual)
    # The broader registration model accepts this source, but exams explicitly
    # exclude it. It must neither add enrollment nor change the mapped section.
    _link(students[0], actual, source="fallback_studying")
    for day, room in [("Sun", "R2"), ("Mon", "R2"), ("Tue", "R1")]:
        TermSectionMeeting.objects.create(
            term_section=actual, day=day, start_time="08:00", end_time="09:00", room=room
        )
    groups = _resolve(*students)
    assert len(groups) == 1
    assert groups[0]["student_count"] == 2
    assert groups[0]["preferred_room"] == "R2"


def test_shared_code_sections_follow_canonical_scoped_population(export_courses):  # noqa: F811
    StudentTermSection.objects.all().delete()
    TermSection.objects.all().delete()
    first, second = _section("F01", "CS111"), _section("F1", "CS111")
    students = {student.student_id: student for student in Student.objects.all()}
    _link(students[901], first)
    _link(students[902], first)
    _link(students[904], second)
    # Other-course and out-of-scope students must never increase these counts.
    _link(students[901], _section("F10", "CS999"))
    _link(students[903], _section("M1", "CS111"))
    enrolled, metadata = exam_timetable.build_enrolled_sets_with_meta(
        programs=["AI", "AI2"], sections=["F"]
    )
    enrolled = {
        code: students
        for code, students in enrolled.items()
        if metadata[code]["source_course_code"] == "CS111"
    }
    result = resolve_exam_section_enrollment(enrolled, course_meta=metadata)
    by_name = {metadata[code]["course_name"]: groups for code, groups in result.items()}
    assert [(group["section"], group["student_count"]) for group in by_name["Programming I"]] == [
        ("F01", 2)
    ]
    assert [
        (group["section"], group["student_count"])
        for group in by_name["Fundamentals of Programming"]
    ] == [("F1", 1)]
    assert sum(group["student_count"] for groups in result.values() for group in groups) == 3


def test_equal_count_student_swap_changes_membership_fingerprint():
    first_student, second_student = _student(101), _student(102)
    first, second = _section("F01"), _section("F1")
    first_link, second_link = _link(first_student, first), _link(second_student, second)
    before = _resolve(first_student, second_student)
    StudentTermSection.objects.filter(pk=first_link.pk).update(student_id=second_student.pk)
    StudentTermSection.objects.filter(pk=second_link.pk).update(student_id=first_student.pk)
    after = _resolve(first_student, second_student)
    assert [(group["section"], group["student_count"]) for group in before] == [
        (group["section"], group["student_count"]) for group in after
    ]
    assert [group["membership_fingerprint"] for group in before] != [
        group["membership_fingerprint"] for group in after
    ]


@pytest.mark.parametrize("label", ["F01", "F1/2", "شعبة أ"])
def test_room_splits_keep_official_name_and_separate_group_numbers(label):
    students = [_student(sid) for sid in range(101, 106)]
    section = _section(label)
    for student in students:
        _link(student, section)
    groups = _resolve(*students)
    entry = {"course_code": "CS113", "slot_index": 0, "day": "Sun", "period": PERIODS[0]}
    exam_timetable.assign_rooms_to_schedule(
        [entry],
        {"CS113": groups},
        [{"room_code": f"R{number}", "section": "F", "capacity": 2} for number in range(3)],
    )
    assert len(entry["rooms"]) == 3
    assert sum(room["student_count"] for room in entry["rooms"]) == 5
    assert {room["section"] for room in entry["rooms"]} == {label}
    assert {room["room_group"] for room in entry["rooms"]} == {"1/3", "2/3", "3/3"}
    for room in entry["rooms"]:
        assert room["student_count"] <= room["room_capacity"]
        assert room["section_parts"] == [
            {
                "section": label,
                "section_key": f"term-section:{section.pk}",
                "term_section_id": section.pk,
                "mapping_status": "mapped",
                "gender": "F",
                "student_count": room["student_count"],
                "room_group_index": int(room["room_group"].split("/")[0]),
                "room_group_count": 3,
            }
        ]


def test_packed_real_sections_keep_separate_counts_and_literal_names():
    first_student, second_student = _student(101), _student(102)
    first, second = _section("F01+F1"), _section("F1")
    _link(first_student, first)
    _link(second_student, second)
    groups = _resolve(first_student, second_student)
    entry = {"course_code": "CS113", "slot_index": 0, "day": "Sun", "period": PERIODS[0]}
    exam_timetable.assign_rooms_to_schedule(
        [entry], {"CS113": groups}, [{"room_code": "SHARED", "section": "F", "capacity": 5}]
    )
    assert len(entry["rooms"]) == 1
    room = entry["rooms"][0]
    assert room["student_count"] == 2
    assert room["room_group"] == ""
    assert {
        (part["section"], part["term_section_id"], part["student_count"])
        for part in room["section_parts"]
    } == {("F01+F1", first.pk, 1), ("F1", second.pk, 1)}
    assert all(part["room_group_count"] == 1 for part in room["section_parts"])


def test_shared_official_section_has_one_identity_and_both_room_cohorts():
    female, male = _student(101, "F"), _student(102, "M")
    section = _section("ONLINE")
    _link(female, section)
    _link(male, section)
    groups = _resolve(female, male)
    assert len(groups) == 2
    assert {group["gender"] for group in groups} == {"M", "F"}
    assert {group["section_key"] for group in groups} == {f"term-section:{section.pk}"}
    summary = summarize_exam_section_mapping({"CS113": groups})
    assert summary["mapped_sections"] == 1
    assert summary["mapped_enrollments"] == 2
    assert summary["missing_enrollments"] == summary["ambiguous_enrollments"] == 0
    entry = {"course_code": "CS113", "slot_index": 0, "day": "Sun", "period": PERIODS[0]}
    exam_timetable.assign_rooms_to_schedule(
        [entry],
        {"CS113": groups},
        [
            {"room_code": f"{gender}-ROOM", "section": gender, "capacity": 10}
            for gender in ("M", "F")
        ],
    )
    assert len(entry["rooms"]) == 2
    assert {room["section"] for room in entry["rooms"]} == {"ONLINE"}
    assert {room["room_group"] for room in entry["rooms"]} == {"1/2", "2/2"}
    assert {(room["gender"], room["room_code"]) for room in entry["rooms"]} == {
        ("M", "M-ROOM"),
        ("F", "F-ROOM"),
    }
    for room_order in (entry["rooms"], list(reversed(entry["rooms"]))):
        details = derive_multi_sitting_details([{**entry, "rooms": room_order}])
        assert len(details) == 1
        assert details[0]["section"] == "ONLINE"
        assert details[0]["section_key"] == f"term-section:{section.pk}"
        assert details[0]["gender"] == "M/F"
        assert details[0]["enrolment"] == 2
        assert details[0]["sittings"] == 2


def test_capacity_exhaustion_keeps_real_section_and_all_students():
    students = [_student(sid) for sid in range(101, 104)]
    section = _section("F01")
    for student in students:
        _link(student, section)
    groups = _resolve(*students)
    entry = {"course_code": "CS113", "slot_index": 0, "day": "Sun", "period": PERIODS[0]}
    exam_timetable.assign_rooms_to_schedule(
        [entry], {"CS113": groups}, [{"room_code": "ONLY", "section": "F", "capacity": 2}]
    )
    assert {(room["room_code"], room["student_count"]) for room in entry["rooms"]} == {
        ("ONLY", 2),
        ("UNASSIGNED", 1),
    }
    assert {room["section"] for room in entry["rooms"]} == {"F01"}
    assert (
        sum(part["student_count"] for room in entry["rooms"] for part in room["section_parts"]) == 3
    )


@pytest.mark.parametrize("assign_rooms", [False, True])
def test_build_check_save_use_same_sections_and_reject_equal_count_section_drift(
    export_client,  # noqa: F811 — use the shared authentication fixture.
    monkeypatch,
    assign_rooms,
):
    students = [_student(101), _student(102)]
    links = []
    for code, term in [("CS113", 1), ("EX222", 2)]:
        course = Course.objects.create(course_code=code, description=code, credit_hours=3)
        ProgrammeRequirement.objects.create(
            program="AI", course_code=code, course_name=f"Course {code}", programme_term=term
        )
        for student in students:
            StudentCourse.objects.create(student=student, course=course, status="studying")
        if code == "CS113":
            links = [
                _link(student, _section(label, code))
                for student, label in zip(students, ["F01", "F1"], strict=True)
            ]
        else:
            section = _section("F3", code)
            for student in students:
                _link(student, section)
    for number in range(4):
        Room.objects.create(room_code=f"F-R{number}", section="F", capacity=1)
    fixed = [{"course_code": "CS113", "day": "Mon", "period": PERIODS[1]}]
    payload = {
        "label": "Real teaching sections",
        "days": DAYS,
        "periods": PERIODS,
        "max_per_day": 2,
        "programs": ["AI"],
        "sections": ["F"],
        "assign_rooms": assign_rooms,
        "pinned": fixed,
        "thin_conflict_threshold": 0,
    }
    built = _post(export_client, "exam_timetable_build", payload)
    payload.update(
        previous_run_id=built["run_id"], base_schedule=built["schedule"], editor_revision=4
    )
    count_before = ExamTimetableRun.objects.count()

    def forbidden(*args, **kwargs):
        pytest.fail("Check and Save cannot reschedule or rebalance exams")

    monkeypatch.setattr(exam_views, "schedule", forbidden)
    monkeypatch.setattr(exam_timetable, "schedule", forbidden)
    monkeypatch.setattr(exam_timetable, "_rebalance_invigilators_pass", forbidden)
    # The evaluator holds its own reference to the pass; patch that binding too.
    monkeypatch.setattr("core.services.exam_evaluation._rebalance_invigilators_pass", forbidden)
    checked = _post(export_client, "exam_timetable_draft_impact", payload)
    assert ExamTimetableRun.objects.count() == count_before
    assert checked["section_enrollment"] == built["section_enrollment"]
    assert checked["input_fingerprint"] == built["input_fingerprint"]
    assert _placements(checked) == _placements(built)
    assert checked["pinned"] == fixed
    assert checked["qa"]["section_mapping"]["mapped_enrollments"] == 4
    assert checked["qa"]["section_mapping"]["mapped_sections"] == 3
    if assign_rooms:
        for entry in checked["schedule"]:
            assert sum(room["student_count"] for room in entry["rooms"]) == 2
            assert all(room["mapping_status"] == "mapped" for room in entry["rooms"])
        assert {
            part["section"]
            for entry in checked["schedule"]
            if entry["course_code"] == "CS113"
            for room in entry["rooms"]
            for part in room["section_parts"]
        } == {"F01", "F1"}
    else:
        assert all(not entry.get("rooms") for entry in checked["schedule"])
    saved = _post(
        export_client,
        "exam_timetable_build",
        {
            **payload,
            "mode": "save_loaded_changes",
            "expected_input_fingerprint": checked["input_fingerprint"],
        },
    )
    assert saved["section_enrollment"] == checked["section_enrollment"]
    assert saved["schedule"] == checked["schedule"]
    assert saved["input_fingerprint"] == checked["input_fingerprint"]
    StudentTermSection.objects.filter(pk=links[0].pk).update(student_id=students[1].pk)
    StudentTermSection.objects.filter(pk=links[1].pk).update(student_id=students[0].pk)
    count_before_drift = ExamTimetableRun.objects.count()
    rejected = export_client.post(
        reverse("exam_timetable_build"),
        {
            **payload,
            "mode": "save_loaded_changes",
            "expected_input_fingerprint": checked["input_fingerprint"],
        },
        content_type="application/json",
    )
    assert rejected.status_code == 409
    assert rejected.json()["error_code"] == "inputs_changed"
    assert ExamTimetableRun.objects.count() == count_before_drift
    refreshed = _post(export_client, "exam_timetable_draft_impact", payload)
    assert refreshed["input_fingerprint"] != checked["input_fingerprint"]
    assert _placements(refreshed) == _placements(checked)
    assert refreshed["qa"]["section_mapping"] == checked["qa"]["section_mapping"]

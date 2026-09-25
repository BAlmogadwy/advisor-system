"""A small, fully built and saved exam timetable for the student export tests.

The run is made by the real build (rooms included), so every saved key the
export reads - section groups, fingerprints, room parts, operations snapshot -
is exactly what production saves. Pins make the placements deterministic:

* Sun 08:00  MATH101 and IS201 (pinned together, so every IS student clashes)
* Sun 13:00  CS101 (so MATH101 + CS101 students have a same-day exam)
* Mon 08:00  PHYS103 (1)  - CS students' named variant
* Mon 13:00  PHYS103 (2)  - IS students' named variant

MATH101 M1 has 30 male students against 20-seat rooms, so it is split across
rooms; one student with no recorded cohort sits MATH101 as group U, which no
room serves, so that part is "Room not assigned".
"""

from __future__ import annotations

import json

from core.models import Course, ExamTimetableRun, ProgrammeRequirement, Room, Student
from core.services.exam_timetable import build_exam_timetable
from tests.exam_source_factory import scraped_exam_registration

DAYS = ["Sun", "Mon"]
PERIODS = ["08:00-10:00", "13:00-15:00"]

MALE_CS = list(range(4401001, 4401011))
MALE_IS = list(range(4401011, 4401021))
MALE_AI = list(range(4401021, 4401031))
FEMALE_CS2 = list(range(4402001, 4402007))
FEMALE_IS = list(range(4402007, 4402013))
NO_COHORT = 4409001
OVERLOADED = MALE_CS[0]
ALL_IDS = [*MALE_CS, *MALE_IS, *MALE_AI, *FEMALE_CS2, *FEMALE_IS, NO_COHORT]

PINS = [
    {"course_code": "MATH101", "day": "Sun", "period": "08:00-10:00"},
    {"course_code": "IS201", "day": "Sun", "period": "08:00-10:00"},
    {"course_code": "CS101", "day": "Sun", "period": "13:00-15:00"},
    {"course_code": "PHYS103 (1)", "day": "Mon", "period": "08:00-10:00"},
    {"course_code": "PHYS103 (2)", "day": "Mon", "period": "13:00-15:00"},
]

# Values a student export must never contain, planted on every student.
SENTINELS = {
    "registration_no": "REG-SENTINEL-9",
    "nationality": "NATIONALITY-SENTINEL",
    "status": "STATUS-SENTINEL",
    "gpa": 4.321,
    "total_registered_credits": 8641,
    "total_earned_credits": 8642,
    "current_registered_credits": 8643,
    "advisor_id": "ADVISOR-SENTINEL",
}


def student_name(sid: int) -> str:
    return f"STUDENT {sid} TESTNAME"


def _program(sid: int) -> str:
    if sid in MALE_CS:
        return "CS"
    if sid in MALE_IS or sid in FEMALE_IS:
        return "IS"
    if sid in MALE_AI:
        return "AI"
    if sid in FEMALE_CS2:
        return "CS2"
    return "CS"


def _cohort(sid: int) -> str:
    if sid == NO_COHORT:
        return ""
    return "M" if str(sid).startswith("4401") else "F"


def build_population() -> None:
    """Students, scraped registrations, programme names and rooms."""
    for code, name in [
        ("MATH101", "CALCULUS I"),
        ("CS101", "PROGRAMMING I"),
        ("IS201", "INFORMATION SYSTEMS"),
        ("PHYS103", "GENERAL PHYSICS"),
    ]:
        Course.objects.create(course_code=code, description=name, credit_hours=3)
    requirements = [
        ("CS", "MATH101", "CALCULUS I", 1),
        ("CS2", "MATH101", "CALCULUS I", 1),
        ("IS", "MATH101", "CALCULUS I", 1),
        ("AI", "MATH101", "CALCULUS I", 1),
        ("CS", "CS101", "PROGRAMMING I", 3),
        ("CS2", "CS101", "PROGRAMMING I", 3),
        ("IS", "IS201", "INFORMATION SYSTEMS", 3),
        ("CS", "PHYS103", "GENERAL PHYSICS", 5),
        ("IS", "PHYS103", "PHYSICS FOR BUSINESS", 5),
    ]
    for program, code, name, term in requirements:
        ProgrammeRequirement.objects.create(
            program=program,
            course_code=code,
            course_name=name,
            programme_term=term,
            credit_hours=3,
        )
    for sid in ALL_IDS:
        Student.objects.create(
            student_id=sid,
            name=student_name(sid),
            program=_program(sid),
            section=_cohort(sid),
            **SENTINELS,
        )
    math = Course.objects.get(course_code="MATH101")
    for sid in [*MALE_CS, *MALE_IS, *MALE_AI]:
        scraped_exam_registration(sid, math, section_label="M1")
    for sid in [*FEMALE_CS2, *FEMALE_IS]:
        scraped_exam_registration(sid, math, section_label="F1")
    scraped_exam_registration(NO_COHORT, math, section_label="M1")
    cs101 = Course.objects.get(course_code="CS101")
    for sid in MALE_CS:
        scraped_exam_registration(sid, cs101, section_label="M2")
    for sid in FEMALE_CS2:
        scraped_exam_registration(sid, cs101, section_label="F2")
    is201 = Course.objects.get(course_code="IS201")
    # One CS student also sits IS201: three exams on Sunday, above max_per_day.
    for sid in [*MALE_IS, OVERLOADED]:
        scraped_exam_registration(sid, is201, section_label="M3")
    for sid in FEMALE_IS:
        scraped_exam_registration(sid, is201, section_label="F3")
    phys = Course.objects.get(course_code="PHYS103")
    for sid in MALE_CS[:5]:
        scraped_exam_registration(sid, phys, section_label="M4")
    for sid in MALE_IS[:5]:
        scraped_exam_registration(sid, phys, section_label="M5")
    for gender in ("M", "F"):
        for letter in "ABC":
            Room.objects.create(
                room_code=f"{gender}-{letter}",
                capacity=20,
                section=gender,
                building="172" if letter != "C" else "",
                floor=1 if letter == "A" else None,
            )


def build_saved_run(
    label: str = "Student export fixture", programs: list[str] | None = None
) -> ExamTimetableRun:
    result = build_exam_timetable(
        label=label,
        days=DAYS,
        periods=PERIODS,
        programs=programs,
        pinned=PINS,
        seed=7,
        assign_rooms=True,
        rebalance_invigilators=False,
    )
    assert result.get("status") == "ok", result
    return ExamTimetableRun.objects.get(pk=result["run_id"])


def saved_payload(run: ExamTimetableRun) -> dict:
    return json.loads(run.result_json)


def save_payload(run: ExamTimetableRun, payload: dict) -> None:
    run.result_json = json.dumps(payload, ensure_ascii=False)
    run.save(update_fields=["result_json"])

"""An elective's study term on the exam timetable is the term of the slot it fills.

A plan lists elective slots (AI1 at term 9, AI2 and AI3 at term 10), never the
courses, so AI481 had no plan row and no term: the exam page's term view said
"Term not recorded" for it (the owner's saved run "S1M1_V2 — Bassam",
2026-11-13), and the same-plan-same-day rule and its spacing never saw it.

The department maps real courses onto slots per academic term
(``ElectiveTermMapping``). The per-plan export and Generate already place a
mapped elective on its slot's term; these tests pin the exam timetable reading
the same mappings through the same helper, for the academic term its
enrolments come from, and guessing nothing for an elective nobody mapped.
"""

from __future__ import annotations

import pytest

from core.exam_views import _loaded_solver_inputs
from core.models import (
    Course,
    ElectiveCourse,
    ElectiveTermMapping,
    ProgrammeRequirement,
    Student,
)
from core.services.elective_resolver import plan_elective_slots
from core.services.exam_evaluation import evaluate_exam_schedule
from core.services.exam_timetable import (
    build_enrolled_sets_with_meta,
    build_exam_timetable,
    build_plan_term_buckets,
)
from tests.exam_source_factory import scraped_exam_registration

pytestmark = pytest.mark.django_db

IOT = "Internet of Things and Smart Cities"
SLOT = "Program Elective"


def _row(program: str, code: str, term: int, name: str = "", kind: str = "Mandatory") -> None:
    ProgrammeRequirement.objects.create(
        program=program, course_code=code, course_name=name or code, programme_term=term, type=kind
    )


def _map(slot: str, code: str, *, programme: str = "AI", year: str = "1448", term: int = 1):
    elective, _ = ElectiveCourse.objects.get_or_create(
        programme=programme, course_code=code, defaults={"course_name": IOT}
    )
    ElectiveTermMapping.objects.create(
        academic_year=year, term=term, programme=programme, placeholder_code=slot, elective=elective
    )


def _plans() -> None:
    """The AI plans' real shape: AI1 at 9, AI2 and AI3 at 10; AI2's one term earlier."""
    _row("AI", "AI433", 9, "NEURAL NETWORKS AND DEEP LEARNING")
    _row("AI", "AI482", 10, "ARTIFICIAL INTELLIGENCE FOR CYBERSECURITY")
    _row("AI", "AI1", 9, "PROGRAM ELECTIVE I", SLOT)
    _row("AI", "AI2", 10, "PROGRAM ELECTIVE COURSE II", SLOT)
    _row("AI", "AI3", 10, "PROGRAM ELECTIVE COURSE III", SLOT)
    _row("AI2", "AI1", 8, "PROGRAM ELECTIVE COURSE I", SLOT)
    _row("AI2", "AI2", 9, "PROGRAM ELECTIVE COURSE II", SLOT)


def _register(code: str, description: str, student_ids: list[int], program: str = "AI"):
    course, _ = Course.objects.get_or_create(
        course_code=code, defaults={"description": description, "credit_hours": 3}
    )
    for sid in student_ids:
        student, _ = Student.objects.get_or_create(
            student_id=sid, defaults={"program": program, "section": "M"}
        )
        scraped_exam_registration(student, course)


@pytest.fixture
def running():
    """1448 T1 registrations: AI433 (term 9) and the elective AI481, no plan row."""
    _plans()
    _register("AI433", "Neural Networks", [1, 2, 3])
    # The registrar names it in Arabic, which the course identity drops: AI481
    # is known by its code alone, as on the live site.
    _register("AI481", "إنترنت الأشياء والمدن الذكية", [4, 5])
    return build_enrolled_sets_with_meta()


# ── the slots themselves ─────────────────────────────────────────


def test_a_mapped_elective_sits_at_its_slots_term_and_nothing_else_is_returned():
    _plans()
    _map("AI1", "AI481")

    slots = plan_elective_slots(["AI"], academic_year="1448", term=1)

    assert [(s.program, s.code, s.programme_term, dict(s.courses)) for s in slots] == [
        ("AI", "AI1", 9, {"AI481": IOT})
    ]


def test_a_second_cohort_plan_reads_its_base_mapping_at_its_own_slot_term():
    _plans()
    _map("AI1", "AI481")

    slots = plan_elective_slots(["AI2"], academic_year="1448", term="1")

    assert [(s.program, s.code, s.programme_term) for s in slots] == [("AI2", "AI1", 8)]


def test_a_mapping_for_another_academic_term_fills_nothing():
    _plans()
    _map("AI1", "AI481", term=2)
    _map("AI1", "AI463", year="1447")

    assert plan_elective_slots(["AI"], academic_year="1448", term=1) == []


def test_a_course_the_plan_lists_itself_keeps_its_own_row():
    _plans()
    _map("AI2", "AI482")
    _map("AI2", "AI441")

    (slot,) = plan_elective_slots(["AI"], academic_year="1448", term=1)

    assert dict(slot.courses) == {"AI441": IOT}


def test_a_slot_at_term_0_has_no_term_to_give():
    _row("DS", "DS1", 8, kind=SLOT)
    _row("DS", "DS2", 0, kind=SLOT)
    _map("DS1", "DS482", programme="DS")
    _map("DS2", "DS482", programme="DS")

    slots = plan_elective_slots(["DS"], academic_year="1448", term=1)

    assert [(s.code, s.programme_term) for s in slots] == [("DS1", 8)]


def test_the_plans_own_mapping_names_a_course_before_its_base_programmes():
    _plans()
    _map("AI1", "AI481")
    own = ElectiveCourse.objects.create(
        programme="AI2", course_code="AI481", course_name="Smart Cities (AI2)"
    )
    ElectiveTermMapping.objects.create(
        academic_year="1448", term=1, programme="AI2", placeholder_code="AI1", elective=own
    )

    (slot,) = plan_elective_slots(["AI2"], academic_year="1448", term=1)

    assert dict(slot.courses) == {"AI481": "Smart Cities (AI2)"}


# ── the exam timetable's (programme, term) buckets ───────────────


def test_a_mapped_elective_joins_its_slots_term_bucket(running):
    enrolled, meta = running
    _map("AI1", "AI481")

    buckets, course_buckets = build_plan_term_buckets(set(enrolled), meta)

    assert buckets[("AI", 9)] == {"AI433", "AI481"}
    assert course_buckets["AI481"] == [("AI", 9), ("AI2", 8)]


def test_an_unmapped_elective_gets_no_term_rather_than_a_guess(running):
    enrolled, meta = running

    buckets, course_buckets = build_plan_term_buckets(set(enrolled), meta)

    assert "AI481" not in course_buckets
    assert buckets == {("AI", 9): {"AI433"}}


def test_the_mapping_for_the_enrolments_academic_term_decides(running):
    enrolled, meta = running
    _map("AI1", "AI481", term=2)

    assert "AI481" not in build_plan_term_buckets(set(enrolled), meta)[1]

    _map("AI2", "AI481", term=1)

    assert build_plan_term_buckets(set(enrolled), meta)[1]["AI481"] == [("AI", 10), ("AI2", 9)]


def test_an_elective_filling_slots_at_two_terms_counts_at_both_and_each_term_once(running):
    enrolled, meta = running
    for slot in ("AI1", "AI2", "AI3"):
        _map(slot, "AI481")

    buckets, course_buckets = build_plan_term_buckets(set(enrolled), meta, programs=["AI"])

    assert course_buckets["AI481"] == [("AI", 9), ("AI", 10)]
    assert buckets == {("AI", 9): {"AI433", "AI481"}, ("AI", 10): {"AI481"}}


def test_the_selected_plans_bound_an_electives_buckets(running):
    enrolled, meta = running
    _map("AI1", "AI481")

    assert build_plan_term_buckets(set(enrolled), meta, programs=["AI2"])[1] == {
        "AI481": [("AI2", 8)]
    }


def test_the_one_running_course_with_the_code_is_the_elective_whatever_its_name():
    """A registrar spelling that differs from the catalogue's must not lose the term."""
    _plans()
    _register("AI481", "Internet of Things", [4, 5])
    _map("AI1", "AI481")
    enrolled, meta = build_enrolled_sets_with_meta()
    assert meta["AI481"]["course_identity"] == "AI481::INTERNET_OF_THINGS"

    assert build_plan_term_buckets(set(enrolled), meta)[1]["AI481"] == [("AI", 9), ("AI2", 8)]


@pytest.mark.parametrize(
    "registrar_name, elective_identity",
    [
        (IOT, "AI481::INTERNET_OF_THINGS_AND_SMART_CITIES"),
        ("إنترنت الأشياء والمدن الذكية", "AI481"),
    ],
    ids=["catalogue-name", "known-by-code"],
)
def test_when_two_courses_share_the_code_only_the_elective_takes_its_term(
    registrar_name, elective_identity
):
    """CS112 is two courses: a code shared by two running courses is decided by name."""
    _plans()
    _row("CS", "AI481", 5, "ROBOTICS WORKSHOP")
    _register("AI481", registrar_name, [4, 5])
    _register("AI481", registrar_name, [6, 7], program="CS")
    _map("AI1", "AI481")
    enrolled, meta = build_enrolled_sets_with_meta()
    display = {m["course_identity"]: code for code, m in meta.items()}
    elective, robotics = display[elective_identity], display["AI481::ROBOTICS_WORKSHOP"]

    course_buckets = build_plan_term_buckets(set(enrolled), meta)[1]

    assert course_buckets[elective] == [("AI", 9), ("AI2", 8)]
    assert course_buckets[robotics] == [("CS", 5)]


def test_without_the_enrolments_term_no_elective_is_placed(running):
    enrolled, meta = running
    _map("AI1", "AI481")
    no_term = {code: {**m, "term": ""} for code, m in meta.items()}

    mixed = {**meta, "AI433": {**meta["AI433"], "term": "2"}}

    assert "AI481" not in build_plan_term_buckets(set(enrolled), no_term)[1]
    assert "AI481" not in build_plan_term_buckets(set(enrolled), mixed)[1]
    assert "AI481" not in build_plan_term_buckets(set(enrolled))[1]


# ── what Build, Check and the loaded actions now see ─────────────


def test_build_refuses_two_term_9_exams_on_one_day(running):
    _map("AI1", "AI481")

    result = build_exam_timetable(
        "one day", ["Sun"], ["08:00-10:00", "11:00-13:00"], assign_rooms=False, persist=False
    )

    assert result["status"] == "feasibility_error"
    assert [(v["program"], v["programme_term"], v["courses"]) for v in result["violations"]] == [
        ("AI", 9, ["AI433", "AI481"])
    ]


def test_a_build_reports_the_electives_term_for_the_term_view(running):
    _map("AI1", "AI481")

    result = build_exam_timetable(
        "two days", ["Sun", "Mon"], ["08:00-10:00"], assign_rooms=False, persist=False
    )

    assert {
        "program": "AI",
        "programme_term": 9,
        "course_count": 2,
        "courses": ["AI433", "AI481"],
    } in result["buckets_summary"]
    days = {entry["course_code"]: entry["day"] for entry in result["schedule"]}
    assert days["AI433"] != days["AI481"]


def _check(schedule: list[dict], days: list[str], periods: list[str]) -> dict:
    return evaluate_exam_schedule(
        days=days,
        periods=periods,
        max_per_day=2,
        schedule_raw=schedule,
        selected_courses=[entry["course_code"] for entry in schedule],
        assign_rooms=False,
        seed=None,
        thin_conflict_threshold=0,
    )


def test_check_reports_an_elective_on_its_slot_terms_day_as_a_rule_break(running):
    enrolled, meta = running
    days, periods = ["Sun", "Mon"], ["08:00-10:00", "11:00-13:00"]
    schedule = [
        {"course_code": code, **meta[code], "day": "Sun", "period": period}
        for code, period in (("AI433", periods[0]), ("AI481", periods[1]))
    ]
    assert _check(schedule, days, periods)["qa"]["bucket_day_violations"] == []

    _map("AI1", "AI481")
    checked = _check(schedule, days, periods)

    assert [
        (v["program"], v["programme_term"], v["day"])
        for v in checked["qa"]["bucket_day_violations"]
    ] == [("AI", 9, "Sun")]


def test_optimise_fix_and_move_suggestions_read_the_electives_term(running):
    enrolled, meta = running
    _map("AI1", "AI481")
    base = [{"course_code": code, **meta[code]} for code in ("AI433", "AI481")]

    inputs = _loaded_solver_inputs(base, ["Sun", "Mon"], ["08:00-10:00"], None, None, 0)

    assert inputs.plan_term_buckets[("AI", 9)] == {"AI433", "AI481"}
    assert inputs.course_buckets["AI481"] == [("AI", 9), ("AI2", 8)]

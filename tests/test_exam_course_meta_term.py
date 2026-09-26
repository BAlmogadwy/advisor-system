"""An exam's ``term`` is the academic term; study-plan terms only order and bucket.

``build_enrolled_sets_with_meta`` reads two different terms: the academic term
of the scraped registrations, and each programme requirement's study-plan term.
A loop once reused one name for both, so every exam in a saved run carried the
plan term of whichever requirement row came last (run 484: ``term`` 5 on all
172 exams, while its section groups said "1"). The population below keeps the
two apart: the academic term "2" equals no plan term, and PHYS103's two named
variants sort alphabetically in the reverse of their plan-term order.
"""

import json

import pytest
from django.core.cache import cache
from django.urls import reverse

from core.models import Course, ExamTimetableRun, ProgrammeRequirement, Student
from core.services.course_identity import planner_course_key
from core.services.exam_timetable import build_enrolled_sets_with_meta, build_exam_timetable
from tests.exam_source_factory import scraped_exam_registration

pytestmark = pytest.mark.django_db

YEAR, TERM = "1447", "2"
GENERAL, APPLIED = "GENERAL PHYSICS", "APPLIED PHYSICS"
CS_STUDENTS, IS_STUDENTS = [1, 2, 3], [4, 5, 6]
DAYS = ["Sun", "Mon"]
PERIODS = ["08:00-10:00", "13:00-15:00"]
# (program, code, plan name, study-plan term): none of them is the academic term.
REQUIREMENTS = [
    ("CS", "PHYS103", GENERAL, 3),
    ("IS", "PHYS103", APPLIED, 7),
    ("CS", "MATH101", "CALCULUS I", 5),
    ("IS", "MATH101", "CALCULUS I", 4),
]


@pytest.fixture
def population():
    courses = {
        code: Course.objects.create(course_code=code, description=code, credit_hours=3)
        for code in ("PHYS103", "MATH101")
    }
    for program, code, name, plan_term in REQUIREMENTS:
        ProgrammeRequirement.objects.create(
            program=program, course_code=code, course_name=name, programme_term=plan_term
        )
    for program, sids, physics_label in [("CS", CS_STUDENTS, "M2"), ("IS", IS_STUDENTS, "M3")]:
        for sid in sids:
            Student.objects.create(student_id=sid, program=program, section="M")
            for code, label in [("MATH101", "M1"), ("PHYS103", physics_label)]:
                scraped_exam_registration(
                    sid, courses[code], section_label=label, year=YEAR, term=TERM
                )


@pytest.fixture
def admin_client(client, django_user_model, monkeypatch):
    monkeypatch.setattr("core.authz._rate_buckets", {})
    cache.clear()
    client.force_login(django_user_model.objects.create_superuser(username="meta-term-admin"))
    return client


def _terms(entries):
    return {entry["course_code"]: (entry["academic_year"], entry["term"]) for entry in entries}


def _expected(codes):
    return {code: (YEAR, TERM) for code in codes}


def test_every_course_carries_the_academic_term_never_a_study_plan_term(population):
    _enrolled, meta = build_enrolled_sets_with_meta()
    assert set(meta) == {"MATH101", "PHYS103 (1)", "PHYS103 (2)"}
    assert _terms({"course_code": code, **row} for code, row in meta.items()) == _expected(meta)


def test_same_code_variants_are_numbered_by_study_plan_term_not_by_name(population):
    # Alphabetical order would give APPLIED "(1)"; only the plan terms say otherwise.
    assert planner_course_key("PHYS103", APPLIED) < planner_course_key("PHYS103", GENERAL)
    enrolled, meta = build_enrolled_sets_with_meta()
    assert meta["PHYS103 (1)"]["course_name"] == GENERAL
    assert meta["PHYS103 (2)"]["course_name"] == APPLIED
    assert enrolled["PHYS103 (1)"] == set(CS_STUDENTS)
    assert enrolled["PHYS103 (2)"] == set(IS_STUDENTS)


def test_saved_run_and_check_agree_with_the_section_groups_term(population, admin_client):
    built = build_exam_timetable(
        label="Academic term",
        days=DAYS,
        periods=PERIODS,
        max_per_day=2,
        seed=7,
        assign_rooms=False,
        thin_conflict_threshold=0,
    )
    assert built.get("status") == "ok", built
    saved = json.loads(ExamTimetableRun.objects.get(pk=built["run_id"]).result_json)
    codes = {"MATH101", "PHYS103 (1)", "PHYS103 (2)"}
    assert _terms(saved["schedule"]) == _expected(codes)
    section_terms = {
        (row["academic_year"], row["term"])
        for rows in saved["section_enrollment"].values()
        for row in rows
    }
    assert section_terms == {(YEAR, TERM)}
    # Study-plan terms are still reported, per plan, where the Study-term view reads them.
    assert {
        (bucket["program"], bucket["programme_term"], tuple(bucket["courses"]))
        for bucket in saved["buckets_summary"]
    } == {
        ("CS", 3, ("PHYS103 (1)",)),
        ("CS", 5, ("MATH101",)),
        ("IS", 4, ("MATH101",)),
        ("IS", 7, ("PHYS103 (2)",)),
    }

    detail = admin_client.get(reverse("exam_timetable_detail", args=[built["run_id"]])).json()
    assert detail["ok"] and _terms(detail["schedule"]) == _expected(codes)

    # A Check re-derives the metadata: a stale term echoed back by a client never survives.
    base = [{**entry, "term": 5} for entry in saved["schedule"]]
    response = admin_client.post(
        reverse("exam_timetable_draft_impact"),
        {
            "previous_run_id": built["run_id"],
            "base_schedule": base,
            "days": DAYS,
            "periods": PERIODS,
            "max_per_day": 2,
            "selected_courses": built["courses"],
            "assign_rooms": False,
            "thin_conflict_threshold": 0,
            "pinned": [],
        },
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    checked = response.json()
    assert _terms(checked["schedule"]) == _expected(codes)
    assert checked["input_fingerprint"] == saved["input_fingerprint"]

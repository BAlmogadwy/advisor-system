"""Course identity must survive selection, draft moves, saving and optimization."""

import pytest
from django.core.cache import cache
from django.urls import reverse

from core.models import (
    Course,
    ExamTimetableRun,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
)
from core.services.course_identity import display_course_label, planner_course_key
from core.services.exam_multistart import run_multistart
from core.services.exam_run_schema import load_normalised_run
from core.services.exam_timetable import (
    build_enrolled_sets_with_meta,
    build_exam_timetable,
    build_plan_term_buckets,
    select_exam_course_enrollments,
)
from tests.exam_source_factory import scraped_exam_registration

pytestmark = pytest.mark.django_db
FUNDAMENTALS = "Fundamentals of Programming"
PROGRAMMING = "Programming I"


@pytest.fixture
def courses():
    course = Course.objects.create(
        course_code="CS111", description="Registrar shared code", credit_hours=3
    )
    for program, name, term in [
        ("AI2", FUNDAMENTALS, 1),
        ("DS2", FUNDAMENTALS, 1),
        ("AI", PROGRAMMING, 3),
    ]:
        ProgrammeRequirement.objects.create(
            program=program,
            course_code="CS111",
            course_name=name,
            programme_term=term,
            is_online=name == PROGRAMMING,
        )
    for sid, program, section in [
        (1, "AI2", "F"),
        (2, "DS2", "F"),
        (3, "AI", "F"),
        (4, "AI", "M"),
        (5, "AI", "F"),
    ]:
        student = Student.objects.create(student_id=sid, program=program, section=section)
        StudentCourse.objects.create(student=student, course=course, status="studying")
        scraped_exam_registration(student, course)
    return course


@pytest.fixture
def admin_client(client, django_user_model, monkeypatch):
    monkeypatch.setattr("core.authz._rate_buckets", {})
    cache.clear()
    user = django_user_model.objects.create_superuser(
        username="exam-admin", password="test-only-password"
    )
    client.force_login(user)
    return client


def _entry(metadata, name):
    return next(
        {"course_code": code, **meta}
        for code, meta in metadata.items()
        if meta["course_name"] == name
    )


def _post(client, route, payload):
    response = client.post(reverse(route), payload, content_type="application/json")
    assert response.status_code == 200, response.content
    data = response.json()
    assert data["ok"], data
    return data


def test_preview_uses_shared_identity_and_groups_equal_names_across_plans(courses, admin_client):
    data = _post(
        admin_client,
        "exam_timetable_preview_courses",
        {
            "programs": ["AI", "AI2", "DS2"],
            "sections": ["F", "M"],
        },
    )
    rows = {row["course_name"]: row for row in data["courses"]}
    assert set(rows) == {FUNDAMENTALS, PROGRAMMING}
    assert rows[FUNDAMENTALS]["programs"] == ["AI2", "DS2"]
    assert rows[FUNDAMENTALS]["enrolled_count"] == 2
    assert rows[PROGRAMMING]["enrolled_count"] == 3
    assert rows[FUNDAMENTALS]["is_online"] is False
    assert rows[PROGRAMMING]["is_online"] is True
    for name, row in rows.items():
        assert row["course_identity"] == planner_course_key("CS111", name)
        assert row["course_label"] == display_course_label("CS111", name)


def test_online_flag_respects_selected_plans_for_the_same_identity(courses):
    ProgrammeRequirement.objects.filter(program="DS2").update(is_online=True)
    _, all_meta = build_enrolled_sets_with_meta()
    assert _entry(all_meta, FUNDAMENTALS)["is_online"] is True
    _, selected_meta = build_enrolled_sets_with_meta(programs=["AI2"])
    assert _entry(selected_meta, FUNDAMENTALS)["is_online"] is False


@pytest.mark.parametrize("name,expected", [(FUNDAMENTALS, {1, 2}), (PROGRAMMING, {3, 4, 5})])
def test_one_retained_variant_never_takes_other_variant_students(courses, name, expected):
    enrolled, meta = build_enrolled_sets_with_meta()
    entry = _entry(meta, name)
    selected, _ = select_exam_course_enrollments([entry], enrolled, meta)
    assert selected == {entry["course_code"]: expected}


def test_selection_tracks_identity_when_filtered_display_number_changes(courses):
    _, all_meta = build_enrolled_sets_with_meta()
    entry = _entry(all_meta, PROGRAMMING)
    assert entry["course_code"] == "CS111 (2)"
    enrolled, meta = build_enrolled_sets_with_meta(programs=["AI"], sections=["F"])
    assert set(enrolled) == {"CS111"}
    selected, selected_meta = select_exam_course_enrollments([entry], enrolled, meta)
    assert selected == {"CS111 (2)": {3, 5}}
    assert selected_meta["CS111 (2)"]["enrolled_count"] == 2


@pytest.mark.parametrize(
    "change",
    [
        {
            "course_name": "Unrelated course",
            "course_identity": planner_course_key("CS111", "Unrelated course"),
        },
        {"course_identity": planner_course_key("CS111", FUNDAMENTALS)},
        {"course_name": "", "course_identity": "CS111"},
    ],
)
def test_unmatched_or_inconsistent_identity_is_rejected(courses, change):
    enrolled, meta = build_enrolled_sets_with_meta()
    with pytest.raises(ValueError):
        select_exam_course_enrollments([{**_entry(meta, PROGRAMMING), **change}], enrolled, meta)


def test_duplicate_identity_cannot_be_scheduled_under_two_display_codes(courses):
    enrolled, meta = build_enrolled_sets_with_meta()
    entry = _entry(meta, PROGRAMMING)
    with pytest.raises(ValueError, match="more than once"):
        select_exam_course_enrollments([entry, {**entry, "course_code": "CS111"}], enrolled, meta)


def test_plan_buckets_respect_identity_and_selected_programs(courses):
    enrolled, meta = build_enrolled_sets_with_meta()
    buckets, _ = build_plan_term_buckets(set(enrolled), meta, programs=["AI2"])
    assert buckets == {("AI2", 1): {"CS111 (1)"}}


def test_scraped_sections_without_student_courses_use_same_plan_name_identity(courses):
    StudentCourse.objects.all().delete()
    StudentTermSection.objects.all().delete()
    TermSection.objects.all().delete()
    section = TermSection.objects.create(
        course_code="CS",
        course_number="111",
        course_key="CS111",
        course_name="Registrar shared code",
        section="F1",
    )
    for sid in [1, 2, 3, 5]:
        StudentTermSection.objects.create(
            student_id=sid,
            academic_year="1448",
            term="1",
            term_section=section,
            source="scraper_timetable",
        )
    enrolled, meta = build_enrolled_sets_with_meta()
    assert enrolled[_entry(meta, FUNDAMENTALS)["course_code"]] == {1, 2}
    assert enrolled[_entry(meta, PROGRAMMING)["course_code"]] == {3, 5}


def test_build_draft_save_optimize_and_reload_keep_single_variant_and_scope(courses, admin_client):
    _, meta = build_enrolled_sets_with_meta()
    selected = _entry(meta, PROGRAMMING)
    payload = {
        "label": "Identity regression",
        "days": ["Sun", "Mon"],
        "periods": ["08:00-10:00"],
        "programs": ["AI"],
        "sections": ["F"],
        "assign_rooms": False,
        "selected_courses": [selected["course_code"]],
        "selected_course_entries": [selected],
    }
    built = _post(admin_client, "exam_timetable_build", payload)
    assert built["students_count"] == 2
    assert built["schedule"][0]["course_identity"] == selected["course_identity"]
    moved = [{**built["schedule"][0], "day": "Mon", "slot_index": 1}]
    impact = _post(
        admin_client,
        "exam_timetable_draft_impact",
        {
            "previous_run_id": built["run_id"],
            "schedule": moved,
            "programs": [],
            "sections": [],
        },
    )
    assert impact["students_count"] == 2
    assert impact["qa"]["conflict_count"] == 0
    for mode in ["save_loaded_changes", "optimize_loaded"]:
        built = _post(
            admin_client,
            "exam_timetable_build",
            {
                **payload,
                "mode": mode,
                "previous_run_id": built["run_id"],
                "base_schedule": moved,
                # Changed page filters must never broaden a loaded run.
                "programs": [],
                "sections": [],
                "pinned": [
                    {"course_code": selected["course_code"], "day": "Mon", "period": "08:00-10:00"}
                ],
            },
        )
        assert built["students_count"] == 2
        assert built["enrollment_scope"] == {"programs": ["AI"], "sections": ["F"]}
        entry = built["schedule"][0]
        assert entry["course_name"] == PROGRAMMING
        assert entry["course_identity"] == selected["course_identity"]
        assert entry["programs"] == ["AI"]
        assert entry["enrolled_count"] == 2
        assert entry["is_online"] is True
        assert entry["day"] == "Mon"
        response = admin_client.get(reverse("exam_timetable_detail", args=[built["run_id"]]))
        assert response.status_code == 200
        assert response.json()["enrollment_scope"] == built["enrollment_scope"]
        moved = built["schedule"]


def test_multistart_preserves_selected_identity_and_scope(courses):
    _, meta = build_enrolled_sets_with_meta()
    selected = _entry(meta, PROGRAMMING)
    report = run_multistart(
        label="Identity candidates",
        days=["Sun", "Mon"],
        periods=["08:00-10:00"],
        programs=["AI"],
        sections=["F"],
        selected_courses=[selected["course_code"]],
        selected_course_entries=[selected],
        seeds=[1, 2],
        assign_rooms=False,
    )
    assert report.candidates_by_role
    for run in ExamTimetableRun.objects.all():
        data = load_normalised_run(run)
        assert data["students_count"] == 2
        assert data["enrollment_scope"] == {"programs": ["AI"], "sections": ["F"]}
        assert data["schedule"][0]["course_identity"] == selected["course_identity"]


def test_draft_removing_one_variant_does_not_invent_conflicts(courses, admin_client):
    companion = Course.objects.create(course_code="EX101", credit_hours=3)
    StudentCourse.objects.create(student_id=1, course=companion, status="studying")
    scraped_exam_registration(1, companion)
    built = build_exam_timetable(
        label="Both variants",
        days=["Sun", "Mon"],
        periods=["08:00-10:00"],
        assign_rooms=False,
    )
    # Student 1 takes Fundamentals and EX101. Programming I has different students.
    retained = [
        {**entry, "day": "Sun", "slot_index": 0}
        for entry in built["schedule"]
        if entry["course_name"] == PROGRAMMING or entry["course_code"] == "EX101"
    ]
    impact = _post(
        admin_client,
        "exam_timetable_draft_impact",
        {
            "previous_run_id": built["run_id"],
            "schedule": retained,
        },
    )
    assert impact["students_count"] == 4
    assert impact["qa"]["conflict_count"] == 0


def test_build_rejects_disagreement_between_selected_codes_and_identities(courses):
    _, meta = build_enrolled_sets_with_meta()
    with pytest.raises(ValueError, match="must match"):
        build_exam_timetable(
            label="Invalid selection",
            days=["Sun"],
            periods=["08:00-10:00"],
            selected_courses=["CS111 (1)"],
            selected_course_entries=[_entry(meta, PROGRAMMING)],
            persist=False,
        )

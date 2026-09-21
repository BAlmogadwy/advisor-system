"""Invalid screen inputs must never schedule or persist an exam timetable."""

import pytest
from django.core.cache import cache
from django.urls import reverse

from core.models import Course, ExamTimetableRun, Student, StudentCourse
from tests.exam_source_factory import scraped_exam_registration

pytestmark = pytest.mark.django_db


@pytest.fixture
def exam_client(client, django_user_model, monkeypatch):
    monkeypatch.setattr("core.authz._rate_buckets", {})
    cache.clear()
    user = django_user_model.objects.create_superuser(username="validation-admin", password=None)
    client.force_login(user)
    student = Student.objects.create(student_id=900001, program="AI", section="F")
    course = Course.objects.create(course_code="CS101", credit_hours=3)
    StudentCourse.objects.create(student=student, course=course, status="studying")
    scraped_exam_registration(student, course)
    return client


def _payload(**changes):
    return {
        "label": "Input validation",
        "days": ["Sun", "Mon"],
        "periods": ["08:00-10:00"],
        "programs": ["AI"],
        "sections": ["F"],
        "max_per_day": 2,
        "assign_rooms": False,
        **changes,
    }


@pytest.mark.parametrize(
    "periods",
    [
        ["25:00-26:00"],
        ["23:00-24:00"],
        ["08:60-10:00"],
        ["8:00-10:00"],
        ["10:00-08:00"],
        ["08:00-08:00"],
        ["23:00-01:00"],
        ["08:00-"],
        ["08:00-10:00", ""],
        ["08:00-10:00", "09:00-11:00"],
        ["08:00-10:00", "08:00-10:00"],
        [],
        "08:00-10:00",
        [None],
    ],
)
def test_invalid_periods_are_rejected_before_persisting(exam_client, periods):
    response = exam_client.post(
        reverse("exam_timetable_build"), _payload(periods=periods), content_type="application/json"
    )
    assert response.status_code == 400
    assert "period" in response.json()["error"].lower()
    assert not ExamTimetableRun.objects.exists()


@pytest.mark.parametrize(
    "days", [0, [], None, "Sun", [""], [0], ["Sun", "Sun"], [str(i) for i in range(61)]]
)
def test_invalid_days_never_become_a_default_schedule(exam_client, days):
    response = exam_client.post(
        reverse("exam_timetable_build"), _payload(days=days), content_type="application/json"
    )
    assert response.status_code == 400
    assert "days" in response.json()["error"].lower()
    assert not ExamTimetableRun.objects.exists()


@pytest.mark.parametrize("maximum", [0, -1, 11, 1.5, "2abc", "", True, None])
def test_invalid_daily_limit_is_not_silently_changed(exam_client, maximum):
    response = exam_client.post(
        reverse("exam_timetable_build"),
        _payload(max_per_day=maximum),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert "Max exams/day" in response.json()["error"]
    assert not ExamTimetableRun.objects.exists()


@pytest.mark.parametrize("route", ["exam_timetable_preview_courses", "exam_timetable_build"])
@pytest.mark.parametrize("field", ["programs", "sections"])
@pytest.mark.parametrize("value", [[], None, "AI", [""]])
def test_empty_or_malformed_selection_never_expands_to_everyone(exam_client, route, field, value):
    response = exam_client.post(
        reverse(route), _payload(**{field: value}), content_type="application/json"
    )
    assert response.status_code == 400
    assert "Select at least one" in response.json()["error"]
    assert not ExamTimetableRun.objects.exists()


@pytest.mark.parametrize("mode", ["save_loaded_changes", "optimize_loaded"])
def test_loaded_actions_share_period_validation(exam_client, mode):
    response = exam_client.post(
        reverse("exam_timetable_build"),
        _payload(
            mode=mode,
            base_schedule=[],
            periods=["25:00-26:00"],
        ),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert "valid times" in response.json()["error"]
    assert not ExamTimetableRun.objects.exists()


@pytest.mark.parametrize(
    "periods",
    [
        ["00:00-01:00", "01:00-02:00"],
        ["22:00-23:59"],
        ["10:00-11:00", "08:00-09:00"],
    ],
)
def test_valid_clock_boundaries_and_adjacent_periods_build(exam_client, periods):
    response = exam_client.post(
        reverse("exam_timetable_build"), _payload(periods=periods), content_type="application/json"
    )
    assert response.status_code == 200, response.content
    assert response.json()["students_count"] == 1
    assert {slot["period"] for slot in response.json()["slots"]} == set(periods)
    assert ExamTimetableRun.objects.count() == 1


@pytest.mark.parametrize("route", ["exam_timetable_preview_courses", "exam_timetable_build"])
def test_non_object_request_is_a_validation_error(exam_client, route):
    response = exam_client.post(reverse(route), [], content_type="application/json")
    assert response.status_code == 400
    assert not ExamTimetableRun.objects.exists()

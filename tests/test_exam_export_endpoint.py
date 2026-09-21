"""Downloads contain complete workbooks and do not leave shared temporary files."""

import json
from io import BytesIO
from zipfile import ZipFile

import pytest
from django.urls import reverse

from core.models import ExamTimetableRun
from core.services import exam_timetable
from core.services.exam_run_schema import stamp_schema_version

pytestmark = pytest.mark.django_db


@pytest.fixture
def saved_export():
    return ExamTimetableRun.objects.create(
        label="Download audit",
        result_json=json.dumps(
            stamp_schema_version(
                {
                    "status": "ok",
                    "enrollment_source": "scraper_timetable",
                    "slots": [{"index": 0, "day": "Sun", "period": "08:00-10:00"}],
                    "schedule": [
                        {
                            "course_code": "CS111",
                            "course_name": "Programming I",
                            "course_identity": "CS111::PROGRAMMING_I",
                            "source_course_code": "CS111",
                            "programs": ["AI"],
                            "day": "Sun",
                            "period": "08:00-10:00",
                            "slot_index": 0,
                            "enrolled_count": 1,
                            "rooms": [],
                        }
                    ],
                    "courses": ["CS111"],
                    "courses_count": 1,
                    "students_count": 1,
                    "enrollment_scope": {"programs": ["AI"], "sections": ["F"]},
                    "section_enrollment": {
                        "CS111": [{"section": "F", "gender": "F", "student_count": 1}]
                    },
                    "pinned": [{"course_code": "CS111", "day": "Sun", "period": "08:00-10:00"}],
                    "assign_rooms": False,
                    "qa": {},
                }
            )
        ),
    )


def test_excel_download_is_complete_and_removes_temporary_files(
    client, django_user_model, saved_export, tmp_path, monkeypatch
):
    client.force_login(django_user_model.objects.create_superuser(username="export-admin"))
    monkeypatch.setattr(exam_timetable, "RUNTIME_DIR", tmp_path)
    url = reverse("exam_timetable_export", args=[saved_export.pk])
    first = client.get(url)
    second = client.get(url)
    for response in (first, second):
        assert response.status_code == 200
        assert (
            response["Content-Type"]
            == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        assert f"exam_timetable_{saved_export.pk}.xlsx" in response["Content-Disposition"]
        # A response may remain open while another request exports the same run.
        assert not list(tmp_path.glob("*.xlsx"))
        with ZipFile(BytesIO(b"".join(response.streaming_content))) as archive:
            assert archive.testzip() is None
            assert "xl/workbook.xml" in archive.namelist()
        response.close()


def test_export_rejects_anonymous_and_non_admin_users(client, django_user_model, saved_export):
    url = reverse("exam_timetable_export", args=[saved_export.pk])
    assert client.get(url).status_code in (302, 403)
    client.force_login(django_user_model.objects.create_user(username="export-reader"))
    assert client.get(url).status_code == 403


def test_export_missing_run_is_reported(client, django_user_model):
    client.force_login(django_user_model.objects.create_superuser(username="export-admin"))
    response = client.get(reverse("exam_timetable_export", args=[987654321]))
    assert response.status_code == 404


def test_earlier_source_export_returns_rebuild_guidance_without_workbook(
    client, django_user_model, saved_export, tmp_path, monkeypatch
):
    client.force_login(django_user_model.objects.create_superuser(username="export-admin"))
    payload = json.loads(saved_export.result_json)
    payload.pop("enrollment_source")
    saved_export.result_json = json.dumps(payload)
    saved_export.save(update_fields=["result_json"])
    monkeypatch.setattr(exam_timetable, "RUNTIME_DIR", tmp_path)

    response = client.get(reverse("exam_timetable_export", args=[saved_export.pk]))

    assert response.status_code == 409
    assert response.json()["code"] == "enrollment_source_changed"
    assert "Load Courses" in response.json()["error"]
    assert not list(tmp_path.glob("*.xlsx"))

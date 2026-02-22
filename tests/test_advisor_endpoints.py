import pytest
from django.contrib.auth.models import User
from django.test import Client
from pytest import MonkeyPatch

from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups

pytestmark = pytest.mark.django_db

client = Client()


def _login_superadmin() -> None:
    from django.contrib.auth.models import Group

    ensure_role_groups()
    user, _ = User.objects.get_or_create(username="test-admin")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client.force_login(user)


def test_advisors_list_endpoint(monkeypatch: MonkeyPatch) -> None:
    _login_superadmin()
    monkeypatch.setattr(
        "core.advisor_views.list_academic_advisors",
        lambda: {
            "count": 1,
            "items": [
                {
                    "advisor_id": "A001",
                    "full_name": "Dr. One",
                    "email": "a1@uni.edu",
                    "department": "CS",
                }
            ],
        },
    )

    response = client.get("/report/advisors/")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["advisor_id"] == "A001"


def test_advisor_upsert_endpoint(monkeypatch: MonkeyPatch) -> None:
    _login_superadmin()
    monkeypatch.setattr(
        "core.advisor_views.upsert_academic_advisor",
        lambda advisor_id, full_name, email, department: {
            "ok": True,
            "advisor": {
                "advisor_id": advisor_id,
                "full_name": full_name,
                "email": email,
                "department": department,
            },
        },
    )

    response = client.post(
        "/ops/advisors/upsert/",
        data='{"advisor_id":"A002","full_name":"Dr. Two","email":"a2@uni.edu","department":"AI"}',
        content_type="application/json",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["advisor"]["advisor_id"] == "A002"


def test_ensure_students_advisor_column_endpoint(monkeypatch: MonkeyPatch) -> None:
    _login_superadmin()
    monkeypatch.setattr(
        "core.advisor_views.ensure_students_advisor_column",
        lambda: {"ok": True, "added": True, "column": "students.advisor_id"},
    )

    response = client.post("/ops/advisors/ensure-students-column/")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["column"] == "students.advisor_id"


def test_assign_students_advisors_endpoint(monkeypatch: MonkeyPatch) -> None:
    _login_superadmin()
    monkeypatch.setattr(
        "core.advisor_views.assign_students_to_advisors",
        lambda mappings: {
            "ok": True,
            "received": len(mappings),
            "updated": 1,
            "errors_count": 0,
            "errors": [],
        },
    )

    response = client.post(
        "/ops/advisors/assign-students/",
        data='{"mappings":[{"student_id":4410001,"advisor_id":"A001"}]}',
        content_type="application/json",
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["updated"] == 1


def test_advisor_upsert_duplicate_email_error(monkeypatch: MonkeyPatch) -> None:
    _login_superadmin()

    def _boom(advisor_id, full_name, email, department):
        raise Exception("UNIQUE constraint failed: academic_advisors.email")

    monkeypatch.setattr("core.advisor_views.upsert_academic_advisor", _boom)

    response = client.post(
        "/ops/advisors/upsert/",
        data='{"advisor_id":"A003","full_name":"Dr. Three","email":"dup@uni.edu","department":"CS"}',
        content_type="application/json",
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["error"] == "Email already exists for another advisor."


def test_students_by_advisor_endpoint(monkeypatch: MonkeyPatch) -> None:
    _login_superadmin()
    seen: dict[str, object] = {}

    def fake_list(advisor_id, **kwargs):
        seen["advisor_id"] = advisor_id
        seen.update(kwargs)
        return {
            "advisor_id": advisor_id,
            "mapping_ready": True,
            "count": 1,
            "items": [
                {
                    "student_id": 4410001,
                    "registration_no": "R-001",
                    "name": "Student One",
                    "program": "AI",
                    "section": "M",
                    "current_term_registered_hours": 15,
                    "has_high_priority_missing": True,
                }
            ],
        }

    monkeypatch.setattr("core.advisor_views.list_students_by_advisor", fake_list)

    response = client.get(
        "/report/students-by-advisor/?advisor_id=A001&search=441&focus=attention&program_filter=AI"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["advisor_id"] == "A001"
    assert payload["count"] == 1
    assert payload["items"][0]["student_id"] == 4410001

    assert seen["advisor_id"] == "A001"
    assert seen["search"] == "441"
    assert seen["focus"] == "attention"
    assert seen["program_filter"] == "AI"

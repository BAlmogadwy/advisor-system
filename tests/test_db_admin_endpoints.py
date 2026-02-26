import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from pytest import MonkeyPatch

from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups

pytestmark = pytest.mark.django_db

client = Client()


def _login_superadmin() -> None:
    ensure_role_groups()
    user, _ = User.objects.get_or_create(username="test-admin")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client.force_login(user)


def test_backup_snapshot_endpoint(monkeypatch: MonkeyPatch) -> None:
    _login_superadmin()
    monkeypatch.setattr(
        "core.db_admin_views.create_backup_snapshot",
        lambda: {
            "ok": True,
            "db_path": "x.db",
            "backup_path": "runtime/db_backups/advisor_20260213_120000.db",
            "size_bytes": 1234,
        },
    )

    response = client.post("/ops/db/backup-snapshot/")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["backup_path"].endswith(".db")


def test_integrity_report_endpoint(monkeypatch: MonkeyPatch) -> None:
    _login_superadmin()
    monkeypatch.setattr(
        "core.db_admin_views.run_integrity_checks",
        lambda: {
            "ok": True,
            "integrity_check": "ok",
            "orphan_student_courses": 0,
            "duplicate_prerequisite_triplets": 2,
            "invalid_credit_rows": 0,
            "invalid_programme_term_rows": 1,
            "advice": {"x": "y"},
        },
    )

    response = client.get("/ops/db/integrity-report/")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["integrity_check"] == "ok"
    assert payload["duplicate_prerequisite_triplets"] == 2


def test_delete_students_returns_backup_metadata(monkeypatch: MonkeyPatch) -> None:
    _login_superadmin()
    monkeypatch.setattr(
        "core.db_admin_views.delete_students",
        lambda program=None, section=None: {
            "ok": True,
            "students_count": 5,
            "student_courses_count": 18,
            "backup": {
                "ok": True,
                "backup_path": "runtime/db_backups/advisor_20260213_120500.db",
                "size_bytes": 2222,
            },
            "program": program,
            "section": section,
        },
    )

    response = client.post(
        "/ops/db/delete-students/",
        data='{"program":"CS","section":"A","confirm":"DELETE"}',
        content_type="application/json",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["backup"]["backup_path"].endswith(".db")


def test_delete_program_catalog_returns_backup_metadata(monkeypatch: MonkeyPatch) -> None:
    _login_superadmin()
    monkeypatch.setattr(
        "core.db_admin_views.delete_program_catalog",
        lambda program: {
            "ok": True,
            "program": program,
            "requirements_count": 10,
            "prerequisites_count": 20,
            "backup": {
                "ok": True,
                "backup_path": "runtime/db_backups/advisor_20260213_120600.db",
                "size_bytes": 3333,
            },
        },
    )

    response = client.post(
        "/ops/db/delete-program-catalog/",
        data='{"program":"CS","confirm":"DELETE"}',
        content_type="application/json",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["program"] == "CS"
    assert payload["backup"]["size_bytes"] == 3333


def test_preview_oracle_plan_endpoint(monkeypatch: MonkeyPatch) -> None:
    _login_superadmin()
    monkeypatch.setattr(
        "core.db_admin_views.preview_oracle_plan",
        lambda filepath, program, encoding="windows-1256": {
            "ok": True,
            "metadata": {
                "college_ar": "كلية",
                "dept_ar": "قسم",
                "major_ar": "تخصص",
                "study_type": "انتظام",
            },
            "summary": {"total_courses": 53, "total_credits": 157, "total_levels": 10},
            "warnings": [],
            "preview_rows": [
                {
                    "code": "GS101",
                    "en_name": "ISLAMIC STUDIES",
                    "credits": 2,
                    "level_number": 1,
                    "type": "Mandatory",
                    "prereqs_str": "",
                    "is_online": 0,
                },
                {
                    "code": "CS101",
                    "en_name": "INTRO TO CS",
                    "credits": 3,
                    "level_number": 3,
                    "type": "Mandatory",
                    "prereqs_str": "GS101",
                    "is_online": 1,
                },
            ],
            "existing_db": {"requirements": 0, "prerequisites": 0},
        },
    )

    response = client.post(
        "/ops/db/preview-oracle-plan/",
        data='{"filepath":"C:/fake/getjobid31100","program":"AI"}',
        content_type="application/json",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["summary"]["total_courses"] == 53
    assert len(payload["preview_rows"]) == 2
    assert payload["preview_rows"][0]["code"] == "GS101"
    assert payload["preview_rows"][0]["is_online"] == 0
    assert payload["preview_rows"][1]["is_online"] == 1


def test_import_oracle_plan_endpoint(monkeypatch: MonkeyPatch) -> None:
    _login_superadmin()
    monkeypatch.setattr(
        "core.db_admin_views.import_oracle_plan_from_rows",
        lambda program, rows, replace_existing=False: {
            "ok": True,
            "program": program,
            "replace_existing": replace_existing,
            "requirements_upserted": len(rows),
            "prerequisites_inserted": 1,
            "courses_upserted": len(rows),
            "backup": {
                "ok": True,
                "backup_path": "runtime/db_backups/advisor_20260224_100000.db",
                "size_bytes": 4444,
            },
        },
    )

    response = client.post(
        "/ops/db/import-oracle-plan/",
        data='{"program":"AI","rows":[{"code":"GS101","en_name":"ISLAMIC STUDIES","credits":"2","level_number":"1","type":"Mandatory","is_online":1,"prereqs_str":""}],"replace_existing":false}',
        content_type="application/json",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["program"] == "AI"
    assert payload["requirements_upserted"] == 1
    assert payload["courses_upserted"] == 1
    assert payload["backup"]["size_bytes"] == 4444


def test_db_admin_requires_auth(client: Client) -> None:
    """Unauthenticated requests should get 401."""
    response = client.get("/ops/db/integrity-report/")
    assert response.status_code == 401

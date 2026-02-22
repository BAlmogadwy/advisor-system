from django.test import Client
from pytest import MonkeyPatch

client = Client()


def test_backup_snapshot_endpoint(monkeypatch: MonkeyPatch) -> None:
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

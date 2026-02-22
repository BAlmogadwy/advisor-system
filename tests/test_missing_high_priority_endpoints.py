from pathlib import Path

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from pytest import MonkeyPatch

from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups

pytestmark = pytest.mark.django_db

client = Client()


def _login_superadmin() -> None:
    ensure_role_groups()
    user, _ = User.objects.get_or_create(username="test-mhp-admin")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client.force_login(user)


def test_missing_high_priority_report_endpoint(monkeypatch: MonkeyPatch) -> None:
    _login_superadmin()
    monkeypatch.setattr(
        "core.report_views.run_missing_high_priority_report",
        lambda **kwargs: {
            "count": 1,
            "params": kwargs,
            "items": [
                {
                    "student_id": 4410001,
                    "program": "AI",
                    "missing_this_parity": [{"course_code": "AI301", "score": 3.2}],
                    "missing_other": [],
                    "missing_total": 1,
                }
            ],
        },
    )

    response = client.get("/report/missing-high-priority/?year=1448&semester=0")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["student_id"] == 4410001


def test_export_missing_high_priority_xlsx_endpoint(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    _login_superadmin()
    out_file = tmp_path / "flagged_students_missing_high_priority.xlsx"
    out_file.write_bytes(b"dummy-xlsx")

    monkeypatch.setattr(
        "core.report_views.export_missing_high_priority_xlsx",
        lambda **kwargs: out_file,
    )

    response = client.get("/export/missing-high-priority.xlsx?year=1448&semester=0")

    assert response.status_code == 200
    assert "attachment;" in (response.get("Content-Disposition") or "")


def test_export_missing_high_priority_xlsx_error(monkeypatch: MonkeyPatch) -> None:
    _login_superadmin()

    def _boom(**kwargs: object) -> Path:
        raise RuntimeError("openpyxl is required for XLSX export")

    monkeypatch.setattr("core.report_views.export_missing_high_priority_xlsx", _boom)

    response = client.get("/export/missing-high-priority.xlsx?year=1448&semester=0")

    assert response.status_code == 500
    assert "openpyxl" in response.json().get("error", "")

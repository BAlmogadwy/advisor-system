from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol
from unittest.mock import Mock

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from pytest import MonkeyPatch

from core.models import ProgrammeRequirement, TermSection, TermSectionProgram
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups

pytestmark = pytest.mark.django_db
PREVIEW_FINGERPRINT = "a" * 64


class _TestResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


def _superadmin_client() -> Client:
    ensure_role_groups()
    user = User.objects.create_user(username="current-sections-admin")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client()
    client.force_login(user)
    return client


def _post(client: Client, url: str, payload: dict[str, object]) -> _TestResponse:
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def _preview_result(*, can_import: bool = True, sections: int = 3) -> dict:
    return {
        "can_import": can_import,
        "expected_confirmation": f"IMPORT {sections}",
        "preview_fingerprint": PREVIEW_FINGERPRINT,
        "has_program_column": False,
        "default_programs": ["AI", "DS"],
        "preview_rows": [],
        "impact": {
            "sections_unique": sections,
            "meeting_rows_unique": 8,
            "sections_new": 2,
            "sections_existing": 1,
            "programme_assignments_effective": 6,
            "membership_adds": 4,
            "membership_removes": 0,
            "predicted_fully_unassigned_sections": 0 if can_import else 1,
        },
    }


def test_db_admin_programme_selector_unions_configured_and_linked_programmes() -> None:
    ProgrammeRequirement.objects.create(program="AI", course_code="AI101")
    section = TermSection.objects.create(
        scenario=None,
        source_tag="department",
        course_code="DS",
        course_number="101",
        course_key="DS101",
        section="M1",
    )
    TermSectionProgram.objects.create(
        term_section=section,
        program="DS",
        assignment_source="import",
    )

    response = _superadmin_client().get("/db-admin/")

    assert response.status_code == 200
    assert response.context["section_program_options"] == ["AI", "DS"]


def test_preview_current_sections_accepts_only_current_snapshot_inputs(
    monkeypatch: MonkeyPatch,
) -> None:
    client = _superadmin_client()
    preview = Mock(return_value=_preview_result())
    monkeypatch.setattr(
        "core.db_admin_views.validate_import_path",
        lambda raw: (Path("data/current_sections.csv"), None),
    )
    monkeypatch.setattr("core.db_admin_views.preview_term_sections_from_csv", preview)

    response = _post(
        client,
        "/ops/db/preview-term-sections/",
        {
            "csv_path": "data/current_sections.csv",
            "source_tag": "department",
            "default_programs": ["ds", "AI", "DS"],
            # Legacy fields are ignored rather than reviving historical scope
            # or replacement behavior.
            "academic_year": "1448",
            "term": "1",
            "truncate_existing": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["confirmation_phrase"] == "IMPORT 3"
    preview.assert_called_once_with(
        csv_path=str(Path("data/current_sections.csv")),
        source_tag="department",
        default_programs=["AI", "DS"],
    )


@pytest.mark.parametrize("endpoint", ["preview", "import"])
def test_current_sections_endpoints_require_default_programs_list(
    monkeypatch: MonkeyPatch,
    endpoint: str,
) -> None:
    client = _superadmin_client()
    response = _post(
        client,
        f"/ops/db/{endpoint}-term-sections/",
        {
            "csv_path": "data/current_sections.csv",
            "source_tag": "other",
            "default_programs": "AI,DS",
            "confirm": "IMPORT 3",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "default_programs must be a list"


def test_import_recalculates_preview_and_requires_exact_confirmation(
    monkeypatch: MonkeyPatch,
) -> None:
    client = _superadmin_client()
    preview = Mock(return_value=_preview_result(sections=3))
    importer = Mock(return_value={"inserted_or_updated": 8})
    backup = Mock(return_value={"ok": True, "backup_file": "before-import.sqlite3"})
    monkeypatch.setattr(
        "core.db_admin_views.validate_import_path",
        lambda raw: (Path("data/current_sections.csv"), None),
    )
    monkeypatch.setattr("core.db_admin_views.preview_term_sections_from_csv", preview)
    monkeypatch.setattr("core.db_admin_views.import_term_sections_from_csv", importer)
    monkeypatch.setattr("core.db_admin_views.create_backup_snapshot", backup)

    response = _post(
        client,
        "/ops/db/import-term-sections/",
        {
            "csv_path": "data/current_sections.csv",
            "source_tag": "other",
            "default_programs": ["DS", "AI"],
            "confirm": "IMPORT 2",
            "preview_fingerprint": PREVIEW_FINGERPRINT,
        },
    )

    assert response.status_code == 400
    assert response.json()["confirmation_phrase"] == "IMPORT 3"
    preview.assert_called_once()
    backup.assert_not_called()
    importer.assert_not_called()


def test_import_confirmation_does_not_accept_extra_whitespace(
    monkeypatch: MonkeyPatch,
) -> None:
    client = _superadmin_client()
    importer = Mock()
    backup = Mock()
    monkeypatch.setattr(
        "core.db_admin_views.validate_import_path",
        lambda raw: (Path("data/current_sections.csv"), None),
    )
    monkeypatch.setattr(
        "core.db_admin_views.preview_term_sections_from_csv",
        Mock(return_value=_preview_result(sections=3)),
    )
    monkeypatch.setattr("core.db_admin_views.import_term_sections_from_csv", importer)
    monkeypatch.setattr("core.db_admin_views.create_backup_snapshot", backup)

    response = _post(
        client,
        "/ops/db/import-term-sections/",
        {
            "csv_path": "data/current_sections.csv",
            "source_tag": "other",
            "default_programs": ["AI", "DS"],
            "confirm": "IMPORT 3 ",
            "preview_fingerprint": PREVIEW_FINGERPRINT,
        },
    )

    assert response.status_code == 400
    backup.assert_not_called()
    importer.assert_not_called()


def test_import_blocks_failed_preview_before_backup_or_mutation(
    monkeypatch: MonkeyPatch,
) -> None:
    client = _superadmin_client()
    importer = Mock()
    backup = Mock()
    monkeypatch.setattr(
        "core.db_admin_views.validate_import_path",
        lambda raw: (Path("data/current_sections.csv"), None),
    )
    monkeypatch.setattr(
        "core.db_admin_views.preview_term_sections_from_csv",
        Mock(return_value=_preview_result(can_import=False)),
    )
    monkeypatch.setattr("core.db_admin_views.import_term_sections_from_csv", importer)
    monkeypatch.setattr("core.db_admin_views.create_backup_snapshot", backup)

    response = _post(
        client,
        "/ops/db/import-term-sections/",
        {
            "csv_path": "data/current_sections.csv",
            "source_tag": "other",
            "default_programs": [],
            "confirm": "IMPORT 3",
            "preview_fingerprint": PREVIEW_FINGERPRINT,
        },
    )

    assert response.status_code == 409
    backup.assert_not_called()
    importer.assert_not_called()


def test_import_rejects_a_stale_preview_fingerprint_before_backup(
    monkeypatch: MonkeyPatch,
) -> None:
    client = _superadmin_client()
    importer = Mock()
    backup = Mock()
    changed_preview = _preview_result(sections=3)
    changed_preview["preview_fingerprint"] = "b" * 64
    monkeypatch.setattr(
        "core.db_admin_views.validate_import_path",
        lambda raw: (Path("data/current_sections.csv"), None),
    )
    monkeypatch.setattr(
        "core.db_admin_views.preview_term_sections_from_csv",
        Mock(return_value=changed_preview),
    )
    monkeypatch.setattr("core.db_admin_views.import_term_sections_from_csv", importer)
    monkeypatch.setattr("core.db_admin_views.create_backup_snapshot", backup)

    response = _post(
        client,
        "/ops/db/import-term-sections/",
        {
            "csv_path": "data/current_sections.csv",
            "source_tag": "other",
            "default_programs": ["AI", "DS"],
            "confirm": "IMPORT 3",
            "preview_fingerprint": PREVIEW_FINGERPRINT,
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "preview_stale"
    backup.assert_not_called()
    importer.assert_not_called()


def test_confirmed_import_backs_up_then_merges_without_truncate(
    monkeypatch: MonkeyPatch,
) -> None:
    client = _superadmin_client()
    events: list[str] = []

    def fake_import(**_kwargs: object) -> dict[str, object]:
        events.append("import")
        return {
            "inserted_or_updated": 8,
            "backup": {"ok": True, "backup_file": "before-import.sqlite3"},
        }

    importer = Mock(side_effect=fake_import)
    monkeypatch.setattr(
        "core.db_admin_views.validate_import_path",
        lambda raw: (Path("data/current_sections.csv"), None),
    )
    monkeypatch.setattr(
        "core.db_admin_views.preview_term_sections_from_csv",
        Mock(return_value=_preview_result(sections=3)),
    )
    monkeypatch.setattr("core.db_admin_views.import_term_sections_from_csv", importer)
    monkeypatch.setattr("core.db_admin_views.log_audit_event", lambda *args, **kwargs: None)

    response = _post(
        client,
        "/ops/db/import-term-sections/",
        {
            "csv_path": "data/current_sections.csv",
            "source_tag": "department",
            "default_programs": ["DS", "AI"],
            "confirm": "IMPORT 3",
            "preview_fingerprint": PREVIEW_FINGERPRINT,
        },
    )

    assert response.status_code == 200
    assert response.json()["backup"]["backup_file"] == "before-import.sqlite3"
    assert events == ["import"]
    importer.assert_called_once_with(
        csv_path=str(Path("data/current_sections.csv")),
        source_tag="department",
        default_programs=["AI", "DS"],
        expected_preview_fingerprint=PREVIEW_FINGERPRINT,
        backup_before_import=True,
    )


def test_guard_level_staleness_after_backup_returns_conflict(
    monkeypatch: MonkeyPatch,
) -> None:
    client = _superadmin_client()
    backup = Mock(return_value={"ok": True, "backup_file": "before-import.sqlite3"})
    importer = Mock(
        side_effect=ValueError("Import preview is stale; run preview again before importing.")
    )
    monkeypatch.setattr(
        "core.db_admin_views.validate_import_path",
        lambda raw: (Path("data/current_sections.csv"), None),
    )
    monkeypatch.setattr(
        "core.db_admin_views.preview_term_sections_from_csv",
        Mock(return_value=_preview_result(sections=3)),
    )
    monkeypatch.setattr("core.db_admin_views.create_backup_snapshot", backup)
    monkeypatch.setattr("core.db_admin_views.import_term_sections_from_csv", importer)

    response = _post(
        client,
        "/ops/db/import-term-sections/",
        {
            "csv_path": "data/current_sections.csv",
            "source_tag": "department",
            "default_programs": ["AI", "DS"],
            "confirm": "IMPORT 3",
            "preview_fingerprint": PREVIEW_FINGERPRINT,
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "preview_stale"
    backup.assert_not_called()
    importer.assert_called_once()

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Protocol, cast

import pytest
from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from core.models import ProgrammeRequirement, TermSection, TermSectionProgram
from core.services.rbac import ROLE_SUPER_ADMIN

pytestmark = pytest.mark.django_db


class _TestResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


def _fingerprint(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _preview_payload(
    *,
    default_programs: list[str],
    source_tag: str,
    fingerprint: str,
    can_import: bool = True,
) -> dict[str, object]:
    impact = {
        "sections_unique": 1,
        "meeting_rows_unique": 1,
        "sections_new": 1,
        "sections_existing": 0,
        "programme_assignments_effective": len(default_programs),
        "membership_adds": len(default_programs),
        "membership_removes": 0,
        "membership_promotions": 0,
        "membership_source_changes": 0,
        "predicted_fully_unassigned_sections": 0,
        "fully_unassigned_sections": [],
    }
    return {
        "source_tag": source_tag,
        "source": "hidden-temp.csv",
        "expected_confirmation": "IMPORT 1",
        "preview_fingerprint": fingerprint,
        "default_programs": default_programs,
        "has_program_column": False,
        "program_membership_status": "defaults" if default_programs else "legacy_preserve",
        "can_import": can_import,
        "unassigned_section_count": 0,
        "unassigned_section_basis": "predicted_database_result",
        "program_membership_warning": "",
        "impact": impact,
        "total_rows": 1,
        "preview_count": 1,
        "preview_rows": [
            {
                "course_name": "Software Engineering",
                "course_code": "CS",
                "course_number": "285",
                "course_key": "CS285",
                "section": "M3",
                "available_capacity": "30",
                "registered_count": "12",
                "day": "SUN",
                "start_time": "09:00",
                "end_time": "10:15",
                "building": "B1",
                "floor_wing": "1",
                "room": "101",
                "instructor": "Dr Test",
                "programs": [],
                "requested_programs": default_programs,
                "effective_programs": default_programs,
                "programme_source": "default" if default_programs else "unassigned",
                "source_tag": source_tag,
            }
        ],
    }


@pytest.fixture
def admin_client() -> Client:
    group, _ = Group.objects.get_or_create(name=ROLE_SUPER_ADMIN)
    user = User.objects.create_user(username="sections-import-admin", password="x")
    user.groups.add(group)
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def import_harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    monkeypatch.setattr(settings, "BASE_DIR", tmp_path)
    state: dict[str, Any] = {
        "preview_calls": [],
        "import_calls": [],
        "backup_calls": 0,
        "audit_calls": [],
        "fingerprints": [_fingerprint("stable")],
        "fingerprint_factory": None,
        "import_error": None,
        "events": [],
    }

    def fake_extract(_html_path: Path) -> list[dict[str, str]]:
        return [{"course_key": "CS285"}]

    def fake_write(_rows: list[dict[str, str]], csv_path: Path) -> None:
        Path(csv_path).write_text("csv-v1", encoding="utf-8")

    def fake_preview(
        csv_path: Path,
        *,
        source_tag: str,
        default_programs: list[str],
        **_kwargs: object,
    ) -> dict[str, object]:
        call_index = len(state["preview_calls"])
        factory = state["fingerprint_factory"]
        if factory:
            fingerprint = factory(Path(csv_path), call_index)
        else:
            fingerprints = state["fingerprints"]
            fingerprint = fingerprints[min(call_index, len(fingerprints) - 1)]
        state["preview_calls"].append(
            {
                "csv_path": Path(csv_path),
                "source_tag": source_tag,
                "default_programs": default_programs,
                "fingerprint": fingerprint,
            }
        )
        state["events"].append("preview")
        return _preview_payload(
            default_programs=default_programs,
            source_tag=source_tag,
            fingerprint=fingerprint,
        )

    def fake_import(**kwargs: object) -> dict[str, object]:
        assert kwargs["backup_before_import"] is True
        state["events"].append("backup")
        state["backup_calls"] += 1
        state["events"].append("import")
        state["import_calls"].append(kwargs)
        if state["import_error"]:
            raise state["import_error"]
        return {
            "rows_total": 1,
            "meetings_total": 1,
            "impact": _preview_payload(
                default_programs=cast(list[str], kwargs["default_programs"]),
                source_tag=str(kwargs["source_tag"]),
                fingerprint=str(kwargs["expected_preview_fingerprint"]),
            )["impact"],
            "preview_fingerprint": kwargs["expected_preview_fingerprint"],
            "backup": {"ok": True, "backup_file": "db_test.sqlite3", "size_bytes": 123},
        }

    def fake_audit(*_args: object, **kwargs: object) -> None:
        state["audit_calls"].append(kwargs)

    monkeypatch.setattr("core.sections_import_views.extract_rows_from_oracle_html", fake_extract)
    monkeypatch.setattr("core.sections_import_views.write_rows_to_csv", fake_write)
    monkeypatch.setattr("core.sections_import_views.preview_term_sections_from_csv", fake_preview)
    monkeypatch.setattr("core.sections_import_views.import_term_sections_from_csv", fake_import)
    monkeypatch.setattr("core.sections_import_views.log_audit_event", fake_audit)
    return state


def _upload(name: str = "sections.html", content: bytes = b"<html></html>") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type="text/html")


def _post_preview(
    client: Client,
    *,
    programs: list[str] | None = None,
    is_department: bool = True,
    upload: SimpleUploadedFile | None = None,
) -> _TestResponse:
    return client.post(
        "/ops/sections-import/preview/",
        data={
            "oracle_file": upload or _upload(),
            "is_department": "1" if is_department else "0",
            "default_programs": programs or [],
        },
    )


def _insert(
    client: Client,
    token: str,
    *,
    programs: object = None,
    confirmation: str = "IMPORT 1",
    is_department: bool = True,
) -> _TestResponse:
    payload = {
        "token": token,
        "is_department": is_department,
        "default_programs": ["AI", "DS"] if programs is None else programs,
        "confirmation": confirmation,
    }
    return client.post(
        "/ops/sections-import/insert/",
        data=json.dumps(payload),
        content_type="application/json",
    )


def test_page_lists_union_of_configured_and_current_section_programmes(
    admin_client: Client,
) -> None:
    ProgrammeRequirement.objects.create(program="DS", course_code="DS100")
    ProgrammeRequirement.objects.create(program="ai", course_code="AI100")
    section = TermSection.objects.create(
        course_name="Course",
        course_code="CS",
        course_number="285",
        course_key="CS285",
        section="M3",
    )
    TermSectionProgram.objects.create(term_section=section, program="CS")

    response = admin_client.get("/ops/sections-import/")

    assert response.status_code == 200
    assert response.context["section_program_options"] == ["AI", "CS", "DS"]
    html = response.content.decode("utf-8")
    assert "page-sections-import.css" in html
    assert "default_programs" not in html  # Values are collected by JS, not raw JSON.
    assert "Clear Current Sections" in html
    assert 'id="truncate"' not in html


def test_preview_passes_normalized_multi_programme_defaults_and_binds_token(
    admin_client: Client,
    import_harness: dict[str, Any],
) -> None:
    response = _post_preview(admin_client, programs=["DS", "ai", "AI"])

    assert response.status_code == 200
    payload = response.json()
    assert payload["default_programs"] == ["AI", "DS"]
    assert payload["can_import"] is True
    assert payload["confirmation_phrase"] == "IMPORT 1"
    assert payload["preview_rows"][0]["effective_programs"] == ["AI", "DS"]
    assert import_harness["preview_calls"][0]["default_programs"] == ["AI", "DS"]

    token = payload["token"]
    metadata_path = Path(settings.BASE_DIR) / "tmp" / "sections_import" / f"{token}.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["default_programs"] == ["AI", "DS"]
    assert metadata["source_tag"] == "department"
    assert metadata["preview_fingerprint"] == _fingerprint("stable")


def test_preview_without_programme_is_visible_but_insert_is_blocked(
    admin_client: Client,
    import_harness: dict[str, Any],
) -> None:
    response = _post_preview(admin_client, programs=[])

    assert response.status_code == 200
    payload = response.json()
    assert payload["program_selection_required"] is True
    assert payload["can_import"] is False
    assert payload["token"]
    assert import_harness["preview_calls"][0]["default_programs"] == []


@pytest.mark.parametrize(
    ("upload", "expected"),
    [
        (_upload("sections.txt"), ".html or .htm"),
        (_upload(content=b""), "empty"),
    ],
)
def test_preview_rejects_invalid_upload_before_parsing(
    admin_client: Client,
    import_harness: dict[str, Any],
    upload: SimpleUploadedFile,
    expected: str,
) -> None:
    response = _post_preview(admin_client, programs=["AI"], upload=upload)

    assert response.status_code == 400
    assert expected in response.json()["error"]
    assert import_harness["preview_calls"] == []


def test_preview_enforces_upload_size_before_parsing(
    admin_client: Client,
    import_harness: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.sections_import_views._MAX_ORACLE_UPLOAD_BYTES", 3)

    response = _post_preview(admin_client, programs=["AI"], upload=_upload(content=b"1234"))

    assert response.status_code == 400
    assert "10 MB" in response.json()["error"]
    assert import_harness["preview_calls"] == []


def test_parse_failure_removes_partial_preview_files(
    admin_client: Client,
    import_harness: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.sections_import_views.extract_rows_from_oracle_html",
        lambda _path: (_ for _ in ()).throw(ValueError("invalid Oracle export")),
    )

    response = _post_preview(admin_client, programs=["AI"])

    assert response.status_code == 400
    temp_dir = Path(settings.BASE_DIR) / "tmp" / "sections_import"
    assert list(temp_dir.glob("*")) == []


def test_page_open_cleans_abandoned_expired_preview_tokens(
    admin_client: Client,
    import_harness: dict[str, Any],
) -> None:
    token = "f" * 32
    temp_dir = Path(settings.BASE_DIR) / "tmp" / "sections_import"
    temp_dir.mkdir(parents=True, exist_ok=True)
    expired_at = time.time() - 3_601
    for suffix in ("html", "csv", "json"):
        path = temp_dir / f"{token}.{suffix}"
        path.write_text("expired", encoding="utf-8")
        os.utime(path, (expired_at, expired_at))

    response = admin_client.get("/ops/sections-import/")

    assert response.status_code == 200
    assert list(temp_dir.glob(f"{token}.*")) == []


def test_insert_requires_list_default_programmes(admin_client: Client) -> None:
    response = _insert(
        admin_client,
        "0" * 32,
        programs="AI,DS",
    )

    assert response.status_code == 400
    assert response.json()["error"] == "default_programs must be a list of programme codes"


def test_confirmed_insert_repreviews_backs_up_merges_and_consumes_token(
    admin_client: Client,
    import_harness: dict[str, Any],
) -> None:
    preview = _post_preview(admin_client, programs=["AI", "DS"])
    token = preview.json()["token"]

    response = _insert(admin_client, token)

    assert response.status_code == 200
    payload = response.json()
    assert payload["backup"]["backup_file"] == "db_test.sqlite3"
    assert len(import_harness["preview_calls"]) == 2
    assert import_harness["events"][-2:] == ["backup", "import"]
    imported = import_harness["import_calls"][0]
    assert imported["default_programs"] == ["AI", "DS"]
    assert imported["truncate_existing_term"] is False
    assert imported["expected_preview_fingerprint"] == _fingerprint("stable")
    assert import_harness["audit_calls"][0]["details"]["backup"]["ok"] is True
    for suffix in ("html", "csv", "json"):
        assert not (
            Path(settings.BASE_DIR) / "tmp" / "sections_import" / f"{token}.{suffix}"
        ).exists()


def test_confirmation_is_exact_and_failed_insert_retains_preview_token(
    admin_client: Client,
    import_harness: dict[str, Any],
) -> None:
    preview = _post_preview(admin_client, programs=["AI", "DS"])
    token = preview.json()["token"]

    response = _insert(admin_client, token, confirmation="IMPORT 1 ")

    assert response.status_code == 400
    assert response.json()["code"] == "confirmation_mismatch"
    assert import_harness["backup_calls"] == 0
    assert import_harness["import_calls"] == []
    temp_dir = Path(settings.BASE_DIR) / "tmp" / "sections_import"
    assert (temp_dir / f"{token}.csv").exists()
    assert (temp_dir / f"{token}.json").exists()


def test_insert_rejects_changed_programme_selection_before_backup(
    admin_client: Client,
    import_harness: dict[str, Any],
) -> None:
    preview = _post_preview(admin_client, programs=["AI"])

    response = _insert(admin_client, preview.json()["token"], programs=["DS"])

    assert response.status_code == 400
    assert response.json()["code"] == "preview_changed"
    assert import_harness["preview_calls"] == [import_harness["preview_calls"][0]]
    assert import_harness["backup_calls"] == 0


def test_insert_expires_and_removes_old_preview_token(
    admin_client: Client,
    import_harness: dict[str, Any],
) -> None:
    preview = _post_preview(admin_client, programs=["AI", "DS"])
    token = preview.json()["token"]
    temp_dir = Path(settings.BASE_DIR) / "tmp" / "sections_import"
    metadata_path = temp_dir / f"{token}.json"
    csv_path = temp_dir / f"{token}.csv"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    expired_at = time.time() - 3_601
    metadata["created_at_epoch"] = expired_at
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    os.utime(csv_path, (expired_at, expired_at))

    response = _insert(admin_client, token)

    assert response.status_code == 410
    assert response.json()["code"] == "preview_expired"
    assert import_harness["backup_calls"] == 0
    for suffix in ("html", "csv", "json"):
        assert not (temp_dir / f"{token}.{suffix}").exists()


def test_insert_rejects_database_fingerprint_change_and_retains_token(
    admin_client: Client,
    import_harness: dict[str, Any],
) -> None:
    import_harness["fingerprints"] = [_fingerprint("db-before"), _fingerprint("db-after")]
    preview = _post_preview(admin_client, programs=["AI", "DS"])
    token = preview.json()["token"]

    response = _insert(admin_client, token)

    assert response.status_code == 409
    assert response.json()["code"] == "preview_stale"
    assert import_harness["backup_calls"] == 0
    assert (Path(settings.BASE_DIR) / "tmp" / "sections_import" / f"{token}.csv").exists()


def test_insert_rejects_file_fingerprint_change(
    admin_client: Client,
    import_harness: dict[str, Any],
) -> None:
    import_harness["fingerprint_factory"] = lambda path, _index: hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    preview = _post_preview(admin_client, programs=["AI", "DS"])
    token = preview.json()["token"]
    csv_path = Path(settings.BASE_DIR) / "tmp" / "sections_import" / f"{token}.csv"
    csv_path.write_text("csv-changed", encoding="utf-8")

    response = _insert(admin_client, token)

    assert response.status_code == 409
    assert response.json()["code"] == "preview_stale"
    assert import_harness["backup_calls"] == 0
    assert csv_path.exists()


def test_import_guard_staleness_after_backup_returns_conflict_and_keeps_token(
    admin_client: Client,
    import_harness: dict[str, Any],
) -> None:
    preview = _post_preview(admin_client, programs=["AI", "DS"])
    token = preview.json()["token"]
    import_harness["import_error"] = ValueError("Import preview is stale; run preview again")

    response = _insert(admin_client, token)

    assert response.status_code == 409
    assert response.json()["code"] == "preview_stale"
    assert import_harness["backup_calls"] == 1
    assert (Path(settings.BASE_DIR) / "tmp" / "sections_import" / f"{token}.csv").exists()

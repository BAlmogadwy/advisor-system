from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from django.contrib.auth.models import Group, User
from django.core.management.base import CommandError
from django.test import Client
from pytest import MonkeyPatch

from core.management.commands import scrape_students
from core.models import AuditLog, Student
from core.services import scrape_ops
from core.services.rbac import ROLE_ADVISOR, ROLE_SUPER_ADMIN, ensure_role_groups
from core.services.scrape_student_source import (
    inspect_database_student_source,
    load_database_students,
)

pytestmark = pytest.mark.django_db


def _student(
    student_id: int,
    *,
    program: str | None = "DS",
    section: str = "M",
    status: str = "ACTIVE",
) -> Student:
    return Student.objects.create(
        student_id=student_id,
        program=program,
        section=section,
        status=status,
    )


def _superadmin_client(*, csrf: bool = False) -> Client:
    ensure_role_groups()
    user = User.objects.create_user(username=f"scrape-admin-{User.objects.count()}")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client(enforce_csrf_checks=csrf)
    client.force_login(user)
    return client


def _approved_database_roster(client: Client) -> tuple[str, int, str]:
    summary = inspect_database_student_source()
    response = client.get("/ops/scrape/source-summary/")
    assert response.status_code == 200
    database = response.json()["database"]
    return database["roster_token"], summary["valid"], summary["roster_sha256"]


def test_database_students_are_normalised_sorted_and_include_every_eligible_status() -> None:
    _student(
        5000002,
        program=" ds ",
        section=" f ",
        status=" active with academic warning 1 ",
    )
    _student(4000001, program="AI", section="M", status="GRADUATION EXPECTED")

    assert load_database_students() == [
        {"student_id": "4000001", "program": "AI", "section": "M"},
        {"student_id": "5000002", "program": "DS", "section": "F"},
    ]
    summary = inspect_database_student_source()
    assert {
        "total": summary["total"],
        "valid": summary["valid"],
        "excluded": summary["excluded"],
        "invalid": summary["invalid"],
        "ready": summary["ready"],
    } == {
        "total": 2,
        "valid": 2,
        "excluded": 0,
        "invalid": 0,
        "ready": True,
    }
    assert len(summary["roster_sha256"]) == 64
    assert summary["excluded_reasons"] == {}


def test_database_students_exclude_non_portal_and_terminal_records() -> None:
    _student(999991, program="QBZ", section="M", status="")
    _student(4000001, status="FINAL WITHDRAWN")
    _student(4000002, status="ACTIVE")

    assert load_database_students() == [{"student_id": "4000002", "program": "DS", "section": "M"}]
    summary = inspect_database_student_source()
    assert summary["total"] == 3
    assert summary["valid"] == 1
    assert summary["excluded"] == 2
    assert summary["invalid"] == 0
    assert summary["ready"] is True
    assert summary["excluded_reasons"] == {
        "non_portal_student_id": 1,
        "status_not_in_scope": 1,
    }


@pytest.mark.parametrize(
    "status",
    [
        "ACTIVE",
        "ACTIVE WITH ACADEMIC WARNING 1",
        "ACTIVE WITH ACADEMIC WARNING 2",
        "GRADUATION EXPECTED",
        "FAIL IN LAST TERM",
        "VISITOR TO ANOTHER UNIVERSITY",
    ],
)
def test_database_students_include_each_reviewed_current_status(status: str) -> None:
    _student(4000001, status=status)

    assert load_database_students() == [{"student_id": "4000001", "program": "DS", "section": "M"}]


@pytest.mark.parametrize(
    "status",
    [
        "BACHELOR OF SCIENCE",
        "FINAL WITHDRAWN",
        "EXCUSED WITHDRAWN",
        "DISMISSED DUE TO ABSENCE",
        "DISMISSED - ACADEMIC WARNING",
        "VISITOR INACTIVE",
        "WITHDRAWN SEMESTER",
        "",
        "A FUTURE UNKNOWN STATUS",
    ],
)
def test_database_students_exclude_terminal_or_unreviewed_statuses(status: str) -> None:
    _student(4000001, status=status)

    summary = inspect_database_student_source()
    assert summary["total"] == 1
    assert summary["valid"] == 0
    assert summary["excluded"] == 1
    assert summary["invalid"] == 0
    assert summary["ready"] is False
    assert summary["excluded_reasons"] == {"status_not_in_scope": 1}


def test_database_source_rejects_empty_or_partially_invalid_roster() -> None:
    with pytest.raises(RuntimeError, match="contains no students"):
        load_database_students()

    _student(4000001)
    _student(4000002, program=None)

    summary = inspect_database_student_source()
    assert {
        "total": summary["total"],
        "valid": summary["valid"],
        "excluded": summary["excluded"],
        "invalid": summary["invalid"],
        "ready": summary["ready"],
    } == {
        "total": 2,
        "valid": 1,
        "excluded": 0,
        "invalid": 1,
        "ready": False,
    }
    with pytest.raises(RuntimeError, match=r"1 eligible student record\(s\).+invalid"):
        load_database_students()


def test_database_source_excludes_unapproved_programmes_and_blocks_bad_sections() -> None:
    _student(4000001, program="QBZ", section="M", status="ACTIVE")
    _student(4000002, program="DS", section="X", status="ACTIVE")

    summary = inspect_database_student_source()
    assert summary["total"] == 2
    assert summary["valid"] == 0
    assert summary["excluded"] == 1
    assert summary["invalid"] == 1
    assert summary["ready"] is False
    assert summary["excluded_reasons"] == {"programme_not_in_scope": 1}
    with pytest.raises(RuntimeError, match="no eligible seven-digit portal students"):
        load_database_students()


def test_database_student_snapshot_refuses_roster_drift() -> None:
    _student(4000001)
    summary = inspect_database_student_source()

    assert load_database_students(
        expected_count=1,
        expected_roster_sha256=summary["roster_sha256"],
    ) == [{"student_id": "4000001", "program": "DS", "section": "M"}]

    _student(4000002)
    with pytest.raises(RuntimeError, match="roster changed"):
        load_database_students(
            expected_count=1,
            expected_roster_sha256=summary["roster_sha256"],
        )


@pytest.mark.django_db(transaction=True)
def test_command_database_source_loads_before_portal_login(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _student(4000001, program=" ds ", section=" f ")
    login = AsyncMock(side_effect=RuntimeError("stop after roster load"))
    monkeypatch.setattr(scrape_students, "login_to_portal", login)
    command = scrape_students.Command()

    with pytest.raises(RuntimeError, match="stop after roster load"):
        asyncio.run(
            command._run(
                {
                    "database_students": True,
                    "csv": None,
                    "concurrency": 1,
                    "max_retries": 0,
                    "save_html": False,
                    "debug_dir": str(tmp_path / "debug"),
                    "empty_snapshot_year": "",
                    "empty_snapshot_term": "",
                }
            )
        )

    login.assert_awaited_once()
    assert (
        "Loaded 1 student from the reviewed current-student database roster"
        in capsys.readouterr().out
    )


def test_command_source_arguments_are_exclusive_and_concurrency_is_bounded() -> None:
    parser = scrape_students.Command().create_parser("manage.py", "scrape_students")

    with pytest.raises(CommandError):
        parser.parse_args([])
    with pytest.raises(CommandError):
        parser.parse_args(["--csv", "students.csv", "--database-students"])
    with pytest.raises(CommandError):
        parser.parse_args(["--database-students", "--concurrency", "0"])

    options = vars(parser.parse_args(["--database-students", "--concurrency", "8"]))
    assert options["database_students"] is True
    assert options["csv"] is None
    assert options["concurrency"] == 8


def _prepare_scrape_ops(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    *,
    database_summary: dict[str, Any],
) -> list[str]:
    captured: list[str] = []

    @contextmanager
    def guard(*, blocking: bool) -> Iterator[bool]:
        assert blocking is False
        yield True

    class Process:
        pid = 43210

    def popen(command: list[str], **kwargs: Any) -> Process:
        del kwargs
        captured.extend(command)
        return Process()

    monkeypatch.setattr(
        "core.services.section_snapshot_guard.section_snapshot_operation_guard",
        guard,
    )
    monkeypatch.setattr(
        "core.services.scrape_student_source.inspect_database_student_source",
        lambda: database_summary,
    )
    monkeypatch.setattr(scrape_ops.subprocess, "Popen", popen)
    monkeypatch.setattr(scrape_ops, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(scrape_ops, "STATE_PATH", tmp_path / "scrape_state.json")
    monkeypatch.setattr(scrape_ops, "LOG_PATH", tmp_path / "batch_scrape.log")
    return captured


def test_service_launches_database_source_without_csv_and_records_count(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured = _prepare_scrape_ops(
        monkeypatch,
        tmp_path,
        database_summary={
            "total": 3,
            "valid": 3,
            "excluded": 0,
            "invalid": 0,
            "ready": True,
            "roster_sha256": "a" * 64,
            "excluded_reasons": {},
        },
    )

    result = scrape_ops.start_batch_scrape(concurrency=3, student_source="database")

    assert result["ok"] is True
    assert "--database-students" in captured
    assert "--csv" not in captured
    count_index = captured.index("--expected-database-student-count")
    digest_index = captured.index("--expected-database-roster-sha256")
    assert captured[count_index + 1] == "3"
    assert captured[digest_index + 1] == "a" * 64
    assert "roster_sha256" not in result["params"]
    state = json.loads((tmp_path / "scrape_state.json").read_text(encoding="utf-8"))
    assert state["params"] == {
        "concurrency": 3,
        "student_source": "database",
        "students_csv": None,
        "student_count": 3,
        "roster_sha256": "a" * 64,
    }
    assert state["history"][-1]["student_source"] == "database"
    assert state["history"][-1]["student_count"] == 3


def test_service_default_remains_csv_and_invalid_concurrency_never_starts(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured = _prepare_scrape_ops(
        monkeypatch,
        tmp_path,
        database_summary={
            "total": 0,
            "valid": 0,
            "excluded": 0,
            "invalid": 0,
            "ready": False,
            "roster_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "excluded_reasons": {},
        },
    )

    result = scrape_ops.start_batch_scrape(concurrency=2)

    assert result["ok"] is True
    assert "--csv" in captured
    assert str(scrape_ops.DEFAULT_STUDENTS_CSV) in captured

    captured.clear()
    rejected = scrape_ops.start_batch_scrape(concurrency=0, student_source="database")
    assert rejected == {"ok": False, "error": "concurrency must be between 1 and 8"}
    assert captured == []


def test_service_rejects_unready_database_before_popen(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured = _prepare_scrape_ops(
        monkeypatch,
        tmp_path,
        database_summary={
            "total": 2,
            "valid": 1,
            "excluded": 0,
            "invalid": 1,
            "ready": False,
            "roster_sha256": "b" * 64,
            "excluded_reasons": {},
        },
    )

    result = scrape_ops.start_batch_scrape(concurrency=2, student_source="database")

    assert result["ok"] is False
    assert "1 database student record" in result["error"]
    assert captured == []
    assert not (tmp_path / "scrape_state.json").exists()


def test_start_view_forwards_database_source_and_requires_post(
    monkeypatch: MonkeyPatch,
) -> None:
    client = _superadmin_client()
    captured: dict[str, Any] = {}

    def start(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"ok": True, "pid": 123, "params": kwargs}

    monkeypatch.setattr("core.scrape_views.start_batch_scrape", start)
    _student(4000001)
    roster_token, expected_count, expected_sha256 = _approved_database_roster(client)

    assert client.get("/ops/scrape/start/").status_code == 405
    response = client.post(
        "/ops/scrape/start/",
        {
            "concurrency": "4",
            "student_source": "database",
            "students_csv": "",
            "database_roster_token": roster_token,
        },
    )

    assert response.status_code == 200
    assert captured == {
        "concurrency": 4,
        "students_csv": None,
        "student_source": "database",
        "expected_database_student_count": expected_count,
        "expected_database_roster_sha256": expected_sha256,
    }
    audit = AuditLog.objects.get(action="scrape.batch_start")
    assert audit.status == "ok"
    assert '"student_source": "database"' in audit.details_json


def test_start_view_preserves_legacy_csv_default_and_rejects_ambiguous_source(
    monkeypatch: MonkeyPatch,
) -> None:
    client = _superadmin_client()
    captured: dict[str, Any] = {}

    def start(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr("core.scrape_views.start_batch_scrape", start)

    response = client.post("/ops/scrape/start/", {"concurrency": "2"})
    assert response.status_code == 200
    assert captured["student_source"] == "csv"
    assert captured["students_csv"] is None

    ambiguous = client.post(
        "/ops/scrape/start/",
        {
            "concurrency": "2",
            "student_source": "database",
            "students_csv": "data/students_list.csv",
        },
    )
    assert ambiguous.status_code == 400


def test_start_view_requires_a_current_signed_database_roster_approval() -> None:
    client = _superadmin_client()

    missing = client.post(
        "/ops/scrape/start/",
        {"concurrency": "2", "student_source": "database"},
    )
    tampered = client.post(
        "/ops/scrape/start/",
        {
            "concurrency": "2",
            "student_source": "database",
            "database_roster_token": "tampered",
        },
    )

    assert missing.status_code == 409
    assert missing.json()["error_code"] == "database_roster_token_required"
    assert tampered.status_code == 409
    assert tampered.json()["error_code"] == "database_roster_token_invalid"
    audits = list(AuditLog.objects.filter(action="scrape.batch_start").order_by("id"))
    assert [audit.status for audit in audits] == ["error", "error"]
    assert "tampered" not in " ".join(
        f"{audit.details_json} {audit.error_text}" for audit in audits
    )


def test_invalid_csv_path_audit_does_not_retain_student_identifiers() -> None:
    client = _superadmin_client()

    response = client.post(
        "/ops/scrape/start/",
        {
            "concurrency": "2",
            "student_source": "csv",
            "students_csv": "data/private-student-sensitive-marker.csv",
        },
    )

    assert response.status_code == 400
    assert "sensitive-marker" in response.json()["error"]
    audit = AuditLog.objects.get(action="scrape.batch_start")
    assert audit.error_text == "CSV path validation failed"
    assert "sensitive-marker" not in audit.details_json


def test_invalid_source_audit_does_not_retain_the_raw_source_value() -> None:
    response = _superadmin_client().post(
        "/ops/scrape/start/",
        {"concurrency": "2", "student_source": "student-sensitive-marker"},
    )

    assert response.status_code == 400
    audit = AuditLog.objects.get(action="scrape.batch_start")
    assert '"student_source": "invalid"' in audit.details_json
    assert "sensitive-marker" not in audit.details_json
    assert "sensitive-marker" not in audit.error_text


@pytest.mark.parametrize("value", ["0", "9", "not-a-number"])
def test_start_view_rejects_invalid_concurrency(value: str) -> None:
    response = _superadmin_client().post(
        "/ops/scrape/start/",
        {"concurrency": value, "student_source": "database"},
    )
    assert response.status_code == 400


def test_scrape_mutations_require_csrf_and_source_summary_is_role_protected() -> None:
    csrf_client = _superadmin_client(csrf=True)
    assert csrf_client.post("/ops/scrape/start/").status_code == 403
    assert csrf_client.post("/ops/scrape/stop/").status_code == 403

    anonymous = Client()
    assert anonymous.get("/ops/scrape/source-summary/").status_code == 401

    ensure_role_groups()
    advisor = User.objects.create_user(username="scrape-advisor")
    advisor.groups.add(Group.objects.get(name=ROLE_ADVISOR))
    advisor_client = Client()
    advisor_client.force_login(advisor)
    assert advisor_client.get("/ops/scrape/source-summary/").status_code == 403
    assert advisor_client.post("/ops/scrape/start/").status_code == 403

    _student(4000001)
    response = _superadmin_client().get("/ops/scrape/source-summary/")
    assert response.status_code == 200
    database = response.json()["database"]
    roster_token = database.pop("roster_token")
    assert isinstance(roster_token, str) and roster_token
    assert database == {
        "total": 1,
        "valid": 1,
        "excluded": 0,
        "invalid": 0,
        "ready": True,
        "excluded_reasons": {},
    }


def test_stop_requires_post_and_writes_a_central_audit_event(
    monkeypatch: MonkeyPatch,
) -> None:
    client = _superadmin_client()
    monkeypatch.setattr(
        "core.scrape_views.stop_batch_scrape",
        lambda: {"ok": True, "pid": 43210, "message": "stop sent"},
    )

    assert client.get("/ops/scrape/stop/").status_code == 405
    response = client.post("/ops/scrape/stop/")

    assert response.status_code == 200
    audit = AuditLog.objects.get(action="scrape.batch_stop")
    assert audit.status == "ok"
    assert '"pid": 43210' in audit.details_json


def test_source_summary_fails_closed_without_exposing_internal_digest(
    monkeypatch: MonkeyPatch,
) -> None:
    client = _superadmin_client()
    monkeypatch.setattr(
        "core.scrape_views.inspect_database_student_source",
        lambda: (_ for _ in ()).throw(RuntimeError("database details")),
    )

    response = client.get("/ops/scrape/source-summary/")

    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "error": "Could not inspect the database student roster.",
    }
    assert "database details" not in response.content.decode("utf-8")


def test_dashboard_scrape_source_ui_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    template = (root / "core/templates/core/partials/dashboard/_panel_scrape.html").read_text(
        encoding="utf-8"
    )
    javascript = (root / "static/js/page-dashboard.js").read_text(encoding="utf-8")
    script_include = (root / "core/templates/core/partials/dashboard/_js_main.html").read_text(
        encoding="utf-8"
    )

    assert "{% if can_db_admin %}" in template
    assert 'id="scrapeSource"' in template
    assert '<option value="database" selected>' in template
    assert 'id="scrapeStart" class="btn btn-danger" disabled' in template
    assert 'id="scrapeCsvWrap"' in template
    assert 'id="scrapeSourceMeta"' in template
    assert "Server local time" in template
    assert "Time (UTC)" not in template
    assert "const scrapePanel = q('scrape');" in javascript
    assert "if (!scrapePanel) return;" in javascript
    assert "if (scrapePanel) {" in javascript
    assert "requestId !== scrapeSourceRequestId" in javascript
    assert "requestId !== scrapeStatusRequestId" in javascript
    assert "data.process_control_available === false" in javascript
    assert "data = await res.json().catch(() => ({}));" in javascript
    assert javascript.count("if (!res.ok) {") >= 2
    assert "method: 'POST'" in javascript
    assert "'X-CSRFToken': csrfToken" in javascript
    assert "student_source: studentSource" in javascript
    assert "database_roster_token:" in javascript
    assert "scrapeDatabaseExcluded" in javascript
    assert "q('scrapeSource').value = 'csv';" in javascript
    assert "h.student_source === 'database'" in javascript
    # The CONTRACT is "the dashboard script is cache-busted", not "the buster is
    # currently 11". Pinning the literal number made every legitimate bump a
    # test failure, which teaches the next person to edit the number rather than
    # think about it — and a bump is exactly what must happen whenever
    # page-dashboard.js changes, or returning browsers keep the stale file.
    assert re.search(r"page-dashboard\.js' %}\?v=\d+", script_include), script_include


def test_dashboard_renders_scraper_only_for_superadmin() -> None:
    ensure_role_groups()
    advisor = User.objects.create_user(username="scrape-ui-advisor")
    advisor.groups.add(Group.objects.get(name=ROLE_ADVISOR))
    advisor_client = Client()
    advisor_client.force_login(advisor)

    advisor_response = advisor_client.get("/")
    assert advisor_response.status_code == 200
    advisor_html = advisor_response.content.decode()
    assert 'id="scrape"' not in advisor_html
    assert 'id="scrapeSource"' not in advisor_html

    admin_response = _superadmin_client().get("/")
    assert admin_response.status_code == 200
    admin_html = admin_response.content.decode()
    assert 'id="scrape"' in admin_html
    assert '<option value="database" selected>' in admin_html

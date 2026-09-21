from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from pytest import MonkeyPatch

from core.services import scrape_ops

ROSTER_SHA256 = "a" * 64


class FakeProcess:
    def __init__(self, pid: int = 43210, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout: int) -> int:
        del timeout
        return int(self.returncode or 0)

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _started_state(*, pid: int = 43210) -> dict[str, Any]:
    return {
        "pid": pid,
        "started_at": "2026-08-15 10:00:00",
        "stopped_at": None,
        "last_action": "started",
        "exit_code": None,
        "failure_reason": "",
        "params": {
            "concurrency": 2,
            "student_source": "database",
            "students_csv": None,
            "student_count": 3,
            "roster_sha256": ROSTER_SHA256,
        },
        "history": [
            {
                "event": "started",
                "at": "2026-08-15 10:00:00",
                "pid": pid,
                "student_source": "database",
                "student_count": 3,
                "roster_sha256": ROSTER_SHA256,
            }
        ],
    }


def _stopped_state() -> dict[str, Any]:
    state = _started_state()
    state.update(
        {
            "stopped_at": "2026-08-15 10:01:00",
            "last_action": "finished",
            "exit_code": 0,
        }
    )
    return state


def _write_state(path: Path, state: dict[str, Any]) -> str:
    serialized = json.dumps(state, ensure_ascii=False, indent=2)
    path.write_text(serialized, encoding="utf-8")
    return serialized


def _ready_summary() -> dict[str, Any]:
    return {
        "total": 4,
        "valid": 3,
        "excluded": 1,
        "invalid": 0,
        "ready": True,
        "roster_sha256": ROSTER_SHA256,
        "excluded_reasons": {"inactive": 1},
    }


@pytest.fixture(autouse=True)
def isolated_scrape_runtime(monkeypatch: MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    @contextmanager
    def guard(*, blocking: bool) -> Iterator[bool]:
        assert blocking is False
        yield True

    monkeypatch.setattr(scrape_ops, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(scrape_ops, "STATE_PATH", tmp_path / "scrape_state.json")
    monkeypatch.setattr(scrape_ops, "LOG_PATH", tmp_path / "batch_scrape.log")
    monkeypatch.setattr(
        "core.services.section_snapshot_guard.section_snapshot_operation_guard",
        guard,
    )
    scrape_ops._ACTIVE_PROCESSES.clear()
    yield
    scrape_ops._ACTIVE_PROCESSES.clear()


@pytest.mark.parametrize(
    ("returncode", "expected_action"),
    [(0, "finished"), (7, "failed")],
)
def test_status_retains_tracked_child_exit_code_and_redacts_roster_digest(
    returncode: int,
    expected_action: str,
) -> None:
    _write_state(scrape_ops.STATE_PATH, _started_state())
    scrape_ops._ACTIVE_PROCESSES[43210] = cast(Any, FakeProcess(returncode=returncode))

    status = scrape_ops.get_scrape_status()

    assert status["running"] is False
    assert status["process_control_available"] is False
    assert status["last_action"] == expected_action
    assert status["exit_code"] == returncode
    assert "roster_sha256" not in status["params"]
    assert all("roster_sha256" not in event for event in status["history"])
    if returncode:
        assert status["failure_reason"] == "scraper process exited with code 7"
    else:
        assert status["failure_reason"] == ""

    raw_state = json.loads(scrape_ops.STATE_PATH.read_text(encoding="utf-8"))
    assert raw_state["history"][-1]["event"] == expected_action
    assert raw_state["history"][-1]["exit_code"] == returncode
    assert raw_state["history"][-1]["roster_sha256"] == ROSTER_SHA256

    scrape_ops.get_scrape_status()
    unchanged_state = json.loads(scrape_ops.STATE_PATH.read_text(encoding="utf-8"))
    assert len(unchanged_state["history"]) == 2


def test_status_fails_closed_when_process_disappears_without_exit_status(
    monkeypatch: MonkeyPatch,
) -> None:
    _write_state(scrape_ops.STATE_PATH, _started_state())
    monkeypatch.setattr(scrape_ops, "_is_pid_running", lambda pid: False)

    status = scrape_ops.get_scrape_status()

    assert status["running"] is False
    assert status["last_action"] == "failed"
    assert status["exit_code"] is None
    assert status["failure_reason"] == "scraper process exit status is unavailable"


def test_database_launch_passes_fresh_roster_contract_and_keeps_digest_private(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: list[str] = []
    captured_options: dict[str, Any] = {}
    process = FakeProcess(returncode=None)

    def popen(command: list[str], **kwargs: Any) -> FakeProcess:
        captured.extend(command)
        captured_options.update(kwargs)
        return process

    monkeypatch.setattr(
        "core.services.scrape_student_source.inspect_database_student_source",
        _ready_summary,
    )
    monkeypatch.setattr(scrape_ops.sys, "platform", "linux")
    monkeypatch.setattr(scrape_ops.subprocess, "Popen", popen)

    result = scrape_ops.start_batch_scrape(
        concurrency=3,
        student_source="database",
        expected_database_student_count=3,
        expected_database_roster_sha256=ROSTER_SHA256,
    )

    assert result["ok"] is True
    assert "--database-students" in captured
    assert "--csv" not in captured
    count_index = captured.index("--expected-database-student-count")
    digest_index = captured.index("--expected-database-roster-sha256")
    assert captured[count_index + 1] == "3"
    assert captured[digest_index + 1] == ROSTER_SHA256
    assert captured_options["start_new_session"] is True
    assert "creationflags" not in captured_options
    assert "roster_sha256" not in result["params"]

    raw_state = json.loads(scrape_ops.STATE_PATH.read_text(encoding="utf-8"))
    assert raw_state["params"]["roster_sha256"] == ROSTER_SHA256
    assert raw_state["history"][-1]["roster_sha256"] == ROSTER_SHA256

    status = scrape_ops.get_scrape_status()
    assert status["running"] is True
    assert "roster_sha256" not in status["params"]
    assert all("roster_sha256" not in event for event in status["history"])


@pytest.mark.parametrize(
    ("expected_count", "expected_digest"),
    [(2, ROSTER_SHA256), (3, "b" * 64)],
)
def test_database_roster_change_fails_before_log_or_process_mutation(
    monkeypatch: MonkeyPatch,
    expected_count: int,
    expected_digest: str,
) -> None:
    original_state = _write_state(scrape_ops.STATE_PATH, _stopped_state())
    scrape_ops.LOG_PATH.write_text("previous log\n", encoding="utf-8")
    monkeypatch.setattr(
        "core.services.scrape_student_source.inspect_database_student_source",
        _ready_summary,
    )
    monkeypatch.setattr(
        scrape_ops,
        "_open_log_for_launch",
        lambda: pytest.fail("log must not be opened for a changed roster"),
    )
    monkeypatch.setattr(
        scrape_ops.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("process must not start for a changed roster"),
    )

    result = scrape_ops.start_batch_scrape(
        student_source="database",
        expected_database_student_count=expected_count,
        expected_database_roster_sha256=expected_digest,
    )

    assert result["ok"] is False
    assert result["error_code"] == "roster_changed"
    assert result["database_source"]["valid"] == 3
    assert "roster_sha256" not in result["database_source"]
    assert scrape_ops.STATE_PATH.read_text(encoding="utf-8") == original_state
    assert scrape_ops.LOG_PATH.read_text(encoding="utf-8") == "previous log\n"


def test_database_summary_failure_preserves_existing_state_and_log(
    monkeypatch: MonkeyPatch,
) -> None:
    original_state = _write_state(scrape_ops.STATE_PATH, _stopped_state())
    scrape_ops.LOG_PATH.write_text("previous log\n", encoding="utf-8")

    def inspect() -> dict[str, Any]:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        "core.services.scrape_student_source.inspect_database_student_source",
        inspect,
    )
    monkeypatch.setattr(
        scrape_ops.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("process must not start"),
    )

    result = scrape_ops.start_batch_scrape(student_source="database")

    assert result["error_code"] == "database_source_inspection_failed"
    assert scrape_ops.STATE_PATH.read_text(encoding="utf-8") == original_state
    assert scrape_ops.LOG_PATH.read_text(encoding="utf-8") == "previous log\n"


def test_database_source_with_only_excluded_students_reports_no_eligible_students(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.services.scrape_student_source.inspect_database_student_source",
        lambda: {
            "total": 2,
            "valid": 0,
            "excluded": 2,
            "invalid": 0,
            "ready": False,
            "roster_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "excluded_reasons": {"inactive": 2},
        },
    )
    monkeypatch.setattr(
        scrape_ops.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("process must not start"),
    )

    result = scrape_ops.start_batch_scrape(student_source="database")

    assert result["ok"] is False
    assert result["error_code"] == "database_source_not_ready"
    assert result["error"] == "the database contains no eligible current students to scrape"


def test_process_start_failure_restores_existing_state_and_log(
    monkeypatch: MonkeyPatch,
) -> None:
    original_state = _write_state(scrape_ops.STATE_PATH, _stopped_state())
    scrape_ops.LOG_PATH.write_text("previous log\n", encoding="utf-8")

    def popen(*args: Any, **kwargs: Any) -> FakeProcess:
        del args, kwargs
        raise OSError("process creation denied")

    monkeypatch.setattr(scrape_ops.subprocess, "Popen", popen)

    result = scrape_ops.start_batch_scrape(student_source="csv")

    assert result["error_code"] == "process_start_failed"
    assert scrape_ops.STATE_PATH.read_text(encoding="utf-8") == original_state
    assert scrape_ops.LOG_PATH.read_text(encoding="utf-8") == "previous log\n"


def test_log_open_failure_preserves_existing_state_and_log(
    monkeypatch: MonkeyPatch,
) -> None:
    original_state = _write_state(scrape_ops.STATE_PATH, _stopped_state())
    scrape_ops.LOG_PATH.write_text("previous log\n", encoding="utf-8")

    def open_log() -> tuple[Any, Path | None]:
        raise PermissionError("log is locked")

    monkeypatch.setattr(scrape_ops, "_open_log_for_launch", open_log)
    monkeypatch.setattr(
        scrape_ops.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("process must not start"),
    )

    result = scrape_ops.start_batch_scrape(student_source="csv")

    assert result["error_code"] == "log_open_failed"
    assert scrape_ops.STATE_PATH.read_text(encoding="utf-8") == original_state
    assert scrape_ops.LOG_PATH.read_text(encoding="utf-8") == "previous log\n"


def test_state_write_failure_stops_unpublished_child_and_restores_log(
    monkeypatch: MonkeyPatch,
) -> None:
    original_state = _write_state(scrape_ops.STATE_PATH, _stopped_state())
    scrape_ops.LOG_PATH.write_text("previous log\n", encoding="utf-8")
    process = FakeProcess(returncode=None)

    monkeypatch.setattr(scrape_ops.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        scrape_ops,
        "_stop_tracked_process",
        lambda proc: (proc.terminate(), -15)[1],
    )

    def fail_write(state: dict[str, Any]) -> None:
        del state
        raise OSError("disk full")

    monkeypatch.setattr(scrape_ops, "_write_state", fail_write)

    result = scrape_ops.start_batch_scrape(student_source="csv")

    assert result["error_code"] == "state_write_failed"
    assert process.terminated is True
    assert process.pid not in scrape_ops._ACTIVE_PROCESSES
    assert scrape_ops.STATE_PATH.read_text(encoding="utf-8") == original_state
    assert scrape_ops.LOG_PATH.read_text(encoding="utf-8") == "previous log\n"


def test_stop_failure_is_structured_and_does_not_claim_success(
    monkeypatch: MonkeyPatch,
) -> None:
    original_state = _write_state(scrape_ops.STATE_PATH, _started_state())
    scrape_ops._ACTIVE_PROCESSES[43210] = cast(Any, FakeProcess(returncode=None))
    monkeypatch.setattr(scrape_ops.sys, "platform", "linux")

    def kill(pid: int, sig: int) -> None:
        del pid, sig
        raise PermissionError("not permitted")

    monkeypatch.setattr(scrape_ops.os, "killpg", kill, raising=False)

    result = scrape_ops.stop_batch_scrape()

    assert result["ok"] is False
    assert result["error_code"] == "process_stop_failed"
    assert scrape_ops.STATE_PATH.read_text(encoding="utf-8") == original_state


def test_stop_records_internal_event_without_exposing_roster_digest(
    monkeypatch: MonkeyPatch,
) -> None:
    _write_state(scrape_ops.STATE_PATH, _started_state())
    scrape_ops._ACTIVE_PROCESSES[43210] = cast(Any, FakeProcess(returncode=None))
    monkeypatch.setattr(scrape_ops.sys, "platform", "linux")
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        scrape_ops.os,
        "killpg",
        lambda pid, sig: killed.append((pid, sig)),
        raising=False,
    )

    result = scrape_ops.stop_batch_scrape()

    assert result["ok"] is True
    assert killed == [(43210, scrape_ops.signal.SIGTERM)]
    assert result["exit_code"] == 0
    raw_state = json.loads(scrape_ops.STATE_PATH.read_text(encoding="utf-8"))
    assert raw_state["history"][-1]["event"] == "stopped"
    assert raw_state["history"][-1]["roster_sha256"] == ROSTER_SHA256
    public_status = scrape_ops.get_scrape_status()
    assert all("roster_sha256" not in event for event in public_status["history"])


def test_stop_escalates_and_only_records_after_confirmed_process_exit(
    monkeypatch: MonkeyPatch,
) -> None:
    class SlowProcess(FakeProcess):
        waits = 0

        def wait(self, timeout: int) -> int:
            self.waits += 1
            if self.waits == 1:
                raise scrape_ops.subprocess.TimeoutExpired(str(self.pid), timeout)
            self.returncode = -9
            return -9

    process = SlowProcess(returncode=None)
    _write_state(scrape_ops.STATE_PATH, _started_state())
    scrape_ops._ACTIVE_PROCESSES[43210] = cast(Any, process)
    monkeypatch.setattr(scrape_ops.sys, "platform", "linux")
    signals: list[int] = []
    monkeypatch.setattr(
        scrape_ops.os,
        "killpg",
        lambda pid, sig: signals.append(sig),
        raising=False,
    )

    result = scrape_ops.stop_batch_scrape()

    assert result["ok"] is True
    assert result["exit_code"] == -9
    assert signals == [scrape_ops.signal.SIGTERM, scrape_ops._FORCED_STOP_SIGNAL]
    state = json.loads(scrape_ops.STATE_PATH.read_text(encoding="utf-8"))
    assert state["last_action"] == "stopped"
    assert state["exit_code"] == -9


def test_stop_refuses_to_signal_an_untracked_persisted_pid(
    monkeypatch: MonkeyPatch,
) -> None:
    original_state = _write_state(scrape_ops.STATE_PATH, _started_state())
    monkeypatch.setattr(scrape_ops, "_is_pid_running", lambda pid: True)
    monkeypatch.setattr(
        scrape_ops,
        "_stop_tracked_process",
        lambda proc: pytest.fail("an unverified process must never be signalled"),
    )

    status = scrape_ops.get_scrape_status()
    result = scrape_ops.stop_batch_scrape()

    assert status["running"] is True
    assert status["process_control_available"] is False
    assert result["ok"] is False
    assert result["error_code"] == "process_handle_unavailable"
    assert scrape_ops.STATE_PATH.read_text(encoding="utf-8") == original_state

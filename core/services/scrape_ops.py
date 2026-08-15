import json
import logging
import os
import re
import signal
import subprocess  # nosec
import sys
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, TextIO, cast
from uuid import uuid4

BASE_DIR = Path(__file__).resolve().parents[2]
RUNTIME_DIR = BASE_DIR / "runtime"
STATE_PATH = RUNTIME_DIR / "scrape_state.json"
LOG_PATH = RUNTIME_DIR / "batch_scrape.log"
DEFAULT_STUDENTS_CSV = BASE_DIR / "data" / "students_list.csv"

logger = logging.getLogger(__name__)
_STATE_LOCK = RLock()
_ACTIVE_PROCESSES: dict[int, subprocess.Popen[Any]] = {}
_PRIVATE_STATE_FIELDS = frozenset({"roster_sha256"})
_GRACEFUL_STOP_TIMEOUT_SECONDS = 10
_FORCED_STOP_TIMEOUT_SECONDS = 5
_FORCED_STOP_SIGNAL = getattr(signal, "SIGKILL", 9)

# bandit rationale:
# - subprocess import/use is intentional for controlled local worker lifecycle management.
# - calls are shell-free and use fixed executable/arguments.


def _ensure_runtime_dir() -> None:
    RUNTIME_DIR.mkdir(exist_ok=True)


def _read_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return cast(dict[str, Any], data)
        return {}
    except Exception:
        return {}


def _write_state(data: dict[str, Any]) -> None:
    _ensure_runtime_dir()
    temporary_path = STATE_PATH.with_name(f".{STATE_PATH.name}.{uuid4().hex}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary_path, STATE_PATH)
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove temporary scrape state file", exc_info=True)


def _add_run_event(state: dict[str, Any], event: dict[str, Any]) -> None:
    history_raw = state.get("history", [])
    history: list[dict[str, Any]] = history_raw if isinstance(history_raw, list) else []
    state["history"] = [*history, event][-25:]


def _tail_log(max_lines: int = 120) -> str:
    if not LOG_PATH.exists():
        return ""
    try:
        lines = LOG_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
        return "\n".join(lines[-max_lines:])
    except OSError:
        logger.warning("Could not read the batch scrape log", exc_info=True)
        return ""


def _public_mapping(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {key: item for key, item in value.items() if key not in _PRIVATE_STATE_FIELDS}


def _public_history(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    public: list[dict[str, Any]] = []
    for item in value:
        mapped = _public_mapping(item)
        if mapped is not None:
            public.append(mapped)
    return public


def _structured_failure(error_code: str, error: str) -> dict[str, Any]:
    return {"ok": False, "error_code": error_code, "error": error}


def _open_log_for_launch() -> tuple[TextIO, Path | None]:
    """Open a fresh canonical log while retaining a recoverable prior copy."""
    backup_path: Path | None = None
    if LOG_PATH.exists():
        backup_path = LOG_PATH.with_name(f".{LOG_PATH.name}.{uuid4().hex}.bak")
        os.replace(LOG_PATH, backup_path)
    try:
        return LOG_PATH.open("w", encoding="utf-8"), backup_path
    except Exception:
        if backup_path is not None and backup_path.exists():
            os.replace(backup_path, LOG_PATH)
        raise


def _restore_previous_log(backup_path: Path | None) -> None:
    try:
        LOG_PATH.unlink(missing_ok=True)
        if backup_path is not None and backup_path.exists():
            os.replace(backup_path, LOG_PATH)
    except OSError:
        logger.error("Could not restore the previous batch scrape log", exc_info=True)


def _discard_previous_log(backup_path: Path | None) -> None:
    if backup_path is None:
        return
    try:
        backup_path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove the previous batch scrape log backup", exc_info=True)


def _terminate_unpublished_process(proc: subprocess.Popen[Any]) -> None:
    """Best-effort cleanup when launch succeeded but durable state publication did not."""
    try:
        _stop_tracked_process(proc)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=_FORCED_STOP_TIMEOUT_SECONDS)
        except Exception:
            logger.warning("Could not reap unpublished scrape process", exc_info=True)


def _stop_tracked_process(proc: subprocess.Popen[Any]) -> int:
    """Stop the verified scraper process tree and return its exit code."""
    pid = proc.pid
    if sys.platform.startswith("win"):
        subprocess.run(  # nosec
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return int(proc.wait(timeout=_FORCED_STOP_TIMEOUT_SECONDS))

    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return_code = proc.poll()
        if return_code is None:
            raise
        return int(return_code)
    try:
        return int(proc.wait(timeout=_GRACEFUL_STOP_TIMEOUT_SECONDS))
    except subprocess.TimeoutExpired:
        os.killpg(pid, _FORCED_STOP_SIGNAL)
        return int(proc.wait(timeout=_FORCED_STOP_TIMEOUT_SECONDS))


def _runtime_process_status(pid: int) -> tuple[bool, int | None, bool]:
    """Return ``(running, exit_code, was_tracked)`` for a scraper PID."""
    proc = _ACTIVE_PROCESSES.get(pid)
    if proc is None:
        return _is_pid_running(pid), None, False
    try:
        return_code = proc.poll()
    except Exception:
        logger.warning("Could not poll tracked scrape process %s", pid, exc_info=True)
        return _is_pid_running(pid), None, False
    if return_code is None:
        return True, None, True
    _ACTIVE_PROCESSES.pop(pid, None)
    return False, int(return_code), True


def _is_pid_running(pid: int) -> bool:
    if sys.platform == "win32":
        # On Windows, os.kill(pid, 0) is unreliable — use ctypes or tasklist
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            SYNCHRONIZE = 0x00100000
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if handle:
                # Process exists — check if it has actually exited
                WAIT_TIMEOUT = 258
                ret = kernel32.WaitForSingleObject(handle, 0)
                kernel32.CloseHandle(handle)
                return bool(ret == WAIT_TIMEOUT)  # Still running if WAIT_TIMEOUT
            return False
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False


def _now_local_str() -> str:
    # Server OS local time (timezone-aware), formatted as: "YYYY-MM-DD HH:MM:SS"
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _source_event_fields(state: dict[str, Any]) -> dict[str, Any]:
    params = state.get("params")
    if not isinstance(params, dict):
        return {}
    return {
        "student_source": params.get("student_source"),
        "students_csv": params.get("students_csv"),
        "student_count": params.get("student_count"),
        "roster_sha256": params.get("roster_sha256"),
    }


def get_scrape_status() -> dict[str, Any]:
    with _STATE_LOCK:
        state = _read_state()
        pid = state.get("pid")
        running = False
        exit_code = state.get("exit_code")
        tracked = False
        persistence_error = False
        if isinstance(pid, int) and not state.get("stopped_at"):
            running, observed_exit_code, tracked = _runtime_process_status(pid)
            if observed_exit_code is not None:
                exit_code = observed_exit_code
        elif isinstance(pid, int):
            _ACTIVE_PROCESSES.pop(pid, None)

        # A tracked child gives us its exact return code. If this web process
        # restarted and the child has disappeared, fail closed rather than
        # reporting an unproven success.
        if not running and pid and not state.get("stopped_at") and state.get("started_at"):
            stopped_at = _now_local_str()
            if tracked and exit_code == 0:
                terminal_action = "finished"
                failure_reason = ""
                note = "scraper process exited successfully"
            elif tracked:
                terminal_action = "failed"
                failure_reason = f"scraper process exited with code {exit_code}"
                note = failure_reason
            else:
                terminal_action = "failed"
                failure_reason = "scraper process exit status is unavailable"
                note = "process disappeared before its exit status could be retained"

            state = dict(state)
            state.update(
                {
                    "stopped_at": stopped_at,
                    "last_action": terminal_action,
                    "exit_code": exit_code if tracked else None,
                    "failure_reason": failure_reason,
                }
            )
            _add_run_event(
                state,
                {
                    "event": terminal_action,
                    "at": stopped_at,
                    "pid": pid,
                    "exit_code": exit_code if tracked else None,
                    "note": note,
                    **_source_event_fields(state),
                },
            )
            try:
                _write_state(state)
            except Exception:
                persistence_error = True
                logger.exception("Could not persist terminal batch scrape status")

        history = _public_history(state.get("history"))
        params = _public_mapping(state.get("params"))

        return {
            "running": running,
            "process_control_available": bool(running and tracked),
            "pid": pid,
            "started_at": state.get("started_at"),
            "stopped_at": state.get("stopped_at"),
            "last_action": state.get("last_action"),
            "exit_code": state.get("exit_code"),
            "failure_reason": state.get("failure_reason"),
            "status_persistence_error": persistence_error,
            "params": params,
            "log_path": str(LOG_PATH),
            "log_tail": _tail_log(),
            "history": history,
        }


def start_batch_scrape(
    concurrency: int = 2,
    students_csv: str | None = None,
    *,
    student_source: str = "csv",
    expected_database_student_count: int | None = None,
    expected_database_roster_sha256: str | None = None,
) -> dict[str, Any]:
    from core.services.scrape_student_source import inspect_database_student_source
    from core.services.section_snapshot_guard import section_snapshot_operation_guard

    if isinstance(concurrency, bool) or not isinstance(concurrency, int):
        return {"ok": False, "error": "concurrency must be an integer between 1 and 8"}
    if concurrency < 1 or concurrency > 8:
        return {"ok": False, "error": "concurrency must be between 1 and 8"}

    source = str(student_source or "").strip().lower()
    if source not in {"csv", "database"}:
        return {"ok": False, "error": "student source must be 'csv' or 'database'"}
    if source == "database" and students_csv:
        return {
            "ok": False,
            "error": "students_csv cannot be combined with the database student source",
        }

    # Serialize the status-check + process-start transition with snapshot clears.
    # Once the PID is written, clear operations can safely rely on get_scrape_status().
    with section_snapshot_operation_guard(blocking=False) as acquired:
        if not acquired:
            return _structured_failure(
                "snapshot_maintenance_in_progress",
                "section snapshot maintenance is in progress; retry shortly",
            )

        with _STATE_LOCK:
            status = get_scrape_status()
            if status["running"]:
                return {
                    **_structured_failure(
                        "scrape_already_running",
                        "batch scraper already running",
                    ),
                    **status,
                }

            csv_path: str | None = None
            student_count: int | None = None
            roster_sha256: str | None = None
            if source == "database":
                try:
                    summary = inspect_database_student_source()
                except Exception:
                    logger.exception("Could not inspect the database scrape roster")
                    return _structured_failure(
                        "database_source_inspection_failed",
                        "could not inspect the database student source",
                    )
                if not summary["ready"]:
                    if summary["total"] == 0:
                        error = "the database contains no students to scrape"
                    elif summary["valid"] == 0:
                        error = "the database contains no eligible current students to scrape"
                    else:
                        error = (
                            f"{summary['invalid']} database student record(s) have an invalid "
                            "student ID, programme, or section"
                        )
                    return {
                        **_structured_failure("database_source_not_ready", error),
                        "database_source": _public_mapping(summary),
                    }
                student_count = summary["valid"]
                roster_sha256 = str(summary.get("roster_sha256") or "").strip().lower()
                if not re.fullmatch(r"[0-9a-f]{64}", roster_sha256):
                    return _structured_failure(
                        "database_roster_fingerprint_unavailable",
                        "database student roster fingerprint is unavailable",
                    )
                expected_count_changed = expected_database_student_count is not None and (
                    isinstance(expected_database_student_count, bool)
                    or not isinstance(expected_database_student_count, int)
                    or expected_database_student_count != student_count
                )
                expected_sha256 = (
                    str(expected_database_roster_sha256).strip().lower()
                    if expected_database_roster_sha256 is not None
                    else None
                )
                expected_fingerprint_changed = (
                    expected_sha256 is not None and expected_sha256 != roster_sha256
                )
                if expected_count_changed or expected_fingerprint_changed:
                    return {
                        **_structured_failure(
                            "roster_changed",
                            "database student roster changed; preview it again before starting",
                        ),
                        "database_source": _public_mapping(summary),
                    }
            else:
                csv_path = students_csv or str(DEFAULT_STUDENTS_CSV)

            try:
                _ensure_runtime_dir()
            except Exception:
                logger.exception("Could not initialise scraper runtime directory")
                return _structured_failure(
                    "runtime_initialisation_failed",
                    "could not initialise the scraper runtime directory",
                )

            cmd = [
                sys.executable,
                str(BASE_DIR / "manage.py"),
                "scrape_students",
            ]
            if source == "database":
                cmd.extend(
                    [
                        "--database-students",
                        "--expected-database-student-count",
                        str(student_count),
                        "--expected-database-roster-sha256",
                        str(roster_sha256),
                    ]
                )
            else:
                cmd.extend(["--csv", str(csv_path)])
            cmd.extend(
                [
                    "--concurrency",
                    str(concurrency),
                    "--debug-dir",
                    str(BASE_DIR / "data" / "debug_failures"),
                ]
            )

            try:
                logf, previous_log = _open_log_for_launch()
            except Exception:
                logger.exception("Could not open the batch scrape log")
                return _structured_failure(
                    "log_open_failed",
                    "could not open the batch scrape log",
                )

            try:
                # bandit: argv is fixed and derived from trusted local config; shell is not used.
                popen_options: dict[str, Any] = {
                    "cwd": str(BASE_DIR),
                    "stdout": logf,
                    "stderr": subprocess.STDOUT,
                }
                if sys.platform.startswith("win"):
                    popen_options["creationflags"] = getattr(
                        subprocess,
                        "CREATE_NEW_PROCESS_GROUP",
                        0,
                    )
                else:
                    popen_options["start_new_session"] = True
                proc = subprocess.Popen(cmd, **popen_options)  # nosec
            except Exception:
                logger.exception("Could not start the batch scraper process")
                logf.close()
                _restore_previous_log(previous_log)
                return _structured_failure(
                    "process_start_failed",
                    "could not start the batch scraper process",
                )
            finally:
                if not logf.closed:
                    logf.close()

            started_at = _now_local_str()
            old_state = _read_state()
            history_raw = old_state.get("history", [])
            history = history_raw if isinstance(history_raw, list) else []

            state = {
                "pid": proc.pid,
                "started_at": started_at,
                "stopped_at": None,
                "last_action": "started",
                "exit_code": None,
                "failure_reason": "",
                "params": {
                    "concurrency": concurrency,
                    "student_source": source,
                    "students_csv": csv_path,
                    "student_count": student_count,
                    "roster_sha256": roster_sha256,
                },
                "history": history,
            }
            _add_run_event(
                state,
                {
                    "event": "started",
                    "at": started_at,
                    "pid": proc.pid,
                    "concurrency": concurrency,
                    "student_source": source,
                    "students_csv": csv_path,
                    "student_count": student_count,
                    "roster_sha256": roster_sha256,
                },
            )
            try:
                _write_state(state)
            except Exception:
                logger.exception("Could not publish batch scraper process state")
                _terminate_unpublished_process(proc)
                _restore_previous_log(previous_log)
                return _structured_failure(
                    "state_write_failed",
                    "scraper process was stopped because its state could not be recorded",
                )

            _ACTIVE_PROCESSES[proc.pid] = proc
            _discard_previous_log(previous_log)
            logger.info(
                "Started batch scraper pid=%s source=%s student_count=%s",
                proc.pid,
                source,
                student_count,
            )
            return {
                "ok": True,
                "pid": proc.pid,
                "params": _public_mapping(state["params"]),
            }


def stop_batch_scrape() -> dict[str, Any]:
    with _STATE_LOCK:
        state = _read_state()
        pid = state.get("pid")

        if not isinstance(pid, int):
            return _structured_failure("no_active_scrape", "no active scrape pid found")

        if state.get("stopped_at"):
            _ACTIVE_PROCESSES.pop(pid, None)
            return {
                "ok": True,
                "pid": pid,
                "exit_code": state.get("exit_code"),
                "last_action": state.get("last_action"),
                "message": "process was already stopped",
            }

        running, exit_code, tracked = _runtime_process_status(pid)
        if not running:
            stopped_at = _now_local_str()
            if tracked and exit_code == 0:
                terminal_action = "finished"
                failure_reason = ""
            else:
                terminal_action = "failed"
                failure_reason = (
                    f"scraper process exited with code {exit_code}"
                    if tracked
                    else "scraper process exit status is unavailable"
                )
            state = dict(state)
            state.update(
                {
                    "stopped_at": stopped_at,
                    "last_action": terminal_action,
                    "exit_code": exit_code if tracked else state.get("exit_code"),
                    "failure_reason": failure_reason,
                }
            )
            _add_run_event(
                state,
                {
                    "event": terminal_action,
                    "at": stopped_at,
                    "pid": pid,
                    "exit_code": exit_code if tracked else state.get("exit_code"),
                    **_source_event_fields(state),
                },
            )
            try:
                _write_state(state)
            except Exception:
                logger.exception("Could not persist already-ended scraper status")
                return _structured_failure(
                    "state_write_failed",
                    "process had already ended, but its state could not be recorded",
                )
            return {
                "ok": True,
                "pid": pid,
                "exit_code": state.get("exit_code"),
                "last_action": terminal_action,
                "message": "process was already stopped",
            }

        if not tracked:
            return _structured_failure(
                "process_handle_unavailable",
                "scraper process is running but cannot be safely identified after a web restart",
            )
        proc = _ACTIVE_PROCESSES.get(pid)
        if proc is None:
            return _structured_failure(
                "process_handle_unavailable",
                "scraper process handle is unavailable; refusing to signal an unverified pid",
            )

        try:
            exit_code = _stop_tracked_process(proc)
        except Exception:
            logger.exception("Could not stop batch scraper process %s", pid)
            return _structured_failure(
                "process_stop_failed",
                f"failed to confirm that process {pid} stopped",
            )

        _ACTIVE_PROCESSES.pop(pid, None)
        stopped_at = _now_local_str()
        state = dict(state)
        state.update(
            {
                "stopped_at": stopped_at,
                "last_action": "stopped",
                "exit_code": exit_code,
                "failure_reason": "",
            }
        )
        _add_run_event(
            state,
            {
                "event": "stopped",
                "at": stopped_at,
                "pid": pid,
                "exit_code": exit_code,
                **_source_event_fields(state),
            },
        )
        try:
            _write_state(state)
        except Exception:
            logger.exception("Could not persist stopped batch scraper state")
            return _structured_failure(
                "state_write_failed",
                "scraper was stopped, but its state could not be recorded",
            )

        logger.info("Stopped batch scraper pid=%s", pid)
        return {
            "ok": True,
            "pid": pid,
            "exit_code": exit_code,
            "message": "batch scrape process tree stopped",
        }

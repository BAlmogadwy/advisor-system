import json
import os
import signal
import subprocess  # nosec
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, cast

BASE_DIR = Path(__file__).resolve().parents[2]
RUNTIME_DIR = BASE_DIR / "runtime"
STATE_PATH = RUNTIME_DIR / "scrape_state.json"
LOG_PATH = RUNTIME_DIR / "batch_scrape.log"
DEFAULT_STUDENTS_CSV = BASE_DIR / "data" / "students_list.csv"

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
    STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_run_event(event: dict[str, Any]) -> None:
    state = _read_state()
    history_raw = state.get("history", [])
    history: list[dict[str, Any]] = history_raw if isinstance(history_raw, list) else []
    history.append(event)
    state["history"] = history[-25:]
    _write_state(state)


def _tail_log(max_lines: int = 120) -> str:
    if not LOG_PATH.exists():
        return ""
    lines = LOG_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
    return "\n".join(lines[-max_lines:])


def _is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _now_local_str() -> str:
    # Server OS local time (timezone-aware), formatted as: "YYYY-MM-DD HH:MM:SS"
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def get_scrape_status() -> dict[str, Any]:
    state = _read_state()
    pid = state.get("pid")
    running = isinstance(pid, int) and _is_pid_running(pid)

    history_raw = state.get("history", [])
    history = history_raw if isinstance(history_raw, list) else []

    return {
        "running": running,
        "pid": pid,
        "started_at": state.get("started_at"),
        "stopped_at": state.get("stopped_at"),
        "last_action": state.get("last_action"),
        "params": state.get("params"),
        "log_tail": _tail_log(),
        "history": history,
    }


def start_batch_scrape(concurrency: int = 2, students_csv: str | None = None) -> dict[str, Any]:
    status = get_scrape_status()
    if status["running"]:
        return {"ok": False, "error": "batch scraper already running", **status}

    csv_path = students_csv or str(DEFAULT_STUDENTS_CSV)
    _ensure_runtime_dir()

    cmd = [
        sys.executable,
        str(BASE_DIR / "manage.py"),
        "scrape_students",
        "--csv",
        csv_path,
        "--concurrency",
        str(concurrency),
        "--debug-dir",
        str(BASE_DIR / "data" / "debug_failures"),
    ]

    with LOG_PATH.open("w", encoding="utf-8") as logf:
        # bandit: argv is fixed and derived from trusted local config; shell is not used.
        proc = subprocess.Popen(  # nosec
            cmd,
            cwd=str(BASE_DIR),
            stdout=logf,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )

    started_at = _now_local_str()
    old_state = _read_state()
    history_raw = old_state.get("history", [])
    history = history_raw if isinstance(history_raw, list) else []

    state = {
        "pid": proc.pid,
        "started_at": started_at,
        "stopped_at": None,
        "last_action": "started",
        "params": {"concurrency": concurrency, "students_csv": csv_path},
        "history": history,
    }
    _write_state(state)
    _append_run_event(
        {
            "event": "started",
            "at": started_at,
            "pid": proc.pid,
            "concurrency": concurrency,
            "students_csv": csv_path,
        }
    )

    return {"ok": True, "pid": proc.pid, "params": state["params"]}


def stop_batch_scrape() -> dict[str, Any]:
    state = _read_state()
    pid = state.get("pid")

    if not isinstance(pid, int):
        return {"ok": False, "error": "no active scrape pid found"}

    if not _is_pid_running(pid):
        stopped_at = _now_local_str()
        state["stopped_at"] = stopped_at
        state["last_action"] = "already_stopped"
        _write_state(state)
        _append_run_event({"event": "already_stopped", "at": stopped_at, "pid": pid})
        return {"ok": True, "pid": pid, "message": "process was already stopped"}

    try:
        if sys.platform.startswith("win"):
            # bandit: fixed OS utility call to terminate known pid on Windows.
            subprocess.run(  # nosec
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except Exception as exc:
        return {"ok": False, "error": f"failed to stop process {pid}: {exc}"}

    stopped_at = _now_local_str()
    state["stopped_at"] = stopped_at
    state["last_action"] = "stopped"
    _write_state(state)
    _append_run_event({"event": "stopped", "at": stopped_at, "pid": pid})

    return {"ok": True, "pid": pid, "message": "batch scrape stop signal sent"}

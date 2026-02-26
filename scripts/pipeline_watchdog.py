from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "agent_runs"
REQUIRED_HANDOFF_FIELDS = [
    "TASK_ID:",
    "STAGE:",
    "STATUS:",
    "UPDATED_AT:",
    "## INPUT_SUMMARY",
    "## FILES_CHANGED",
    "## CHECKS_RUN",
    "## FINDINGS",
    "## OPEN_ISSUES",
    "## NEXT_ACTION",
]

STAGE_FILES = {
    "builder": "01_builder.md",
    "critic": "02_critic.md",
    "arabic_context": "03_arabic_context.md",
    "rtl_qa": "04_rtl_qa.md",
}
NEXT_STAGE = {
    "builder": "critic",
    "critic": "arabic_context",
    "arabic_context": "rtl_qa",
    "rtl_qa": "done",
}


@dataclass
class Decision:
    ok: bool
    msg: str
    next_stage: str | None = None


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_status(md_text: str) -> str | None:
    for line in md_text.splitlines():
        if line.strip().startswith("- STATUS:"):
            raw = line.split(":", 1)[1].strip().upper()
            if raw in {"PASS", "CHANGES_REQUIRED", "BLOCKED"}:
                return raw
            return None
    return None


def validate_handoff(md_text: str) -> list[str]:
    missing = [f for f in REQUIRED_HANDOFF_FIELDS if f not in md_text]
    return missing


def append_log(task_dir: Path, text: str) -> None:
    log = task_dir / "orchestrator_log.md"
    if not log.exists():
        log.write_text("# Orchestrator Log\n\n", encoding="utf-8")
    with log.open("a", encoding="utf-8") as f:
        f.write(text + "\n")


def process_task(task_dir: Path) -> Decision:
    state_path = task_dir / "state.json"
    if not state_path.exists():
        return Decision(False, f"{task_dir.name}: missing state.json")

    state = json.loads(state_path.read_text(encoding="utf-8"))
    stage = state.get("current_stage", "")
    if stage == "done":
        return Decision(True, f"{task_dir.name}: already done")
    if stage not in STAGE_FILES:
        return Decision(False, f"{task_dir.name}: unknown stage '{stage}'")

    handoff = task_dir / STAGE_FILES[stage]
    if not handoff.exists():
        return Decision(True, f"{task_dir.name}: waiting for {handoff.name}")

    text = handoff.read_text(encoding="utf-8")
    missing = validate_handoff(text)
    if missing:
        state["status"] = "changes_required"
        state["updated_at"] = now_iso()
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        append_log(
            task_dir,
            f"- {now_iso()} | {stage} -> {stage} | CHANGES_REQUIRED | Missing handoff fields: {', '.join(missing)}",
        )
        return Decision(False, f"{task_dir.name}: handoff incomplete ({len(missing)} missing)")

    st = parse_status(text)
    if st == "PASS":
        nxt = NEXT_STAGE[stage]
        state["status"] = "passed"
        state["current_stage"] = nxt
        state["updated_at"] = now_iso()
        state["last_transition"] = {
            "from": stage,
            "to": nxt,
            "decision": "PASS",
            "reason": "handoff accepted",
        }
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        append_log(task_dir, f"- {now_iso()} | {stage} -> {nxt} | PASS | handoff accepted")
        return Decision(True, f"{task_dir.name}: {stage} passed -> {nxt}", nxt)

    if st in {"CHANGES_REQUIRED", "BLOCKED"}:
        state["status"] = "changes_required" if st == "CHANGES_REQUIRED" else "blocked"
        state["updated_at"] = now_iso()
        rc = state.setdefault("retry_count", {})
        rc[stage] = int(rc.get(stage, 0)) + 1
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        append_log(task_dir, f"- {now_iso()} | {stage} -> {stage} | {st} | retry={rc[stage]}")
        return Decision(False, f"{task_dir.name}: {stage} requires changes (retry {rc[stage]})")

    return Decision(False, f"{task_dir.name}: invalid STATUS in {handoff.name}")


def main() -> int:
    if not RUNS.exists():
        logger.warning("agent_runs directory not found")
        return 1

    task_dirs = [p for p in RUNS.iterdir() if p.is_dir() and not p.name.startswith("templates")]
    if not task_dirs:
        logger.info("no tasks")
        return 0

    for t in sorted(task_dirs):
        d = process_task(t)
        logger.info(d.msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

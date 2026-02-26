from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "agent_runs"
TEMPLATES = RUNS / "templates"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_id")
    ap.add_argument("--title", default="")
    args = ap.parse_args()

    task_dir = RUNS / args.task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    state = json.loads((TEMPLATES / "state.template.json").read_text(encoding="utf-8"))
    state["task_id"] = args.task_id
    state["title"] = args.title
    state["updated_at"] = now_iso()

    (task_dir / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (task_dir / "orchestrator_log.md").write_text("# Orchestrator Log\n\n", encoding="utf-8")

    for f in ["01_builder.md", "02_critic.md", "03_arabic_context.md", "04_rtl_qa.md"]:
        if not (task_dir / f).exists():
            (task_dir / f).write_text(
                (TEMPLATES / "handoff.template.md").read_text(encoding="utf-8"), encoding="utf-8"
            )

    logger.info("created: %s", task_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

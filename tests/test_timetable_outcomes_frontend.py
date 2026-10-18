"""Exercise production timetable result renderers and deferred Generate handlers."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_timetable_outcome_frontend_regressions() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Timetable frontend regressions require Node.js")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            node,
            "--test",
            "tests/frontend/timetable-outcomes.test.cjs",
            "tests/frontend/timetable-workspace.test.cjs",
            "tests/frontend/timetable-workspace-integration.test.cjs",
            "tests/frontend/timetable-accessibility.test.cjs",
            "tests/frontend/timetable-split-lock.test.cjs",
            "tests/frontend/timetable-copy.test.cjs",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

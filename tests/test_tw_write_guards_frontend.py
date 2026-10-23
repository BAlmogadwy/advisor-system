"""Run the jsdom suite for stage 3's B1 write guards (tests/frontend/timetable-write-guards.test.cjs).

Classic and split view say the server's two new refusals (a section label
already placed in the scenario; a section another scenario owns) in their own
words, in English and in Arabic, on every create and remove path, and the
page's free-label rule runs against tests/fixtures/section_label_cases.json.
"""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_timetable_write_guard_frontend() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Timetable frontend regressions require Node.js")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--test", "tests/frontend/timetable-write-guards.test.cjs"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        # Both pages in jsdom, about two dozen tests; this only stops a hung run.
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

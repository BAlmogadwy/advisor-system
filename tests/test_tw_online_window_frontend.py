"""Run the jsdom suite for stage 3's B4 online windows (tests/frontend/timetable-online-window.test.cjs).

The classic page says the server's new publish warning (a class that needs a
room, in a window kept for online courses) in its own words, in English and
in Arabic, and Publish still publishes.
"""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_timetable_online_window_frontend() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Timetable frontend regressions require Node.js")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--test", "tests/frontend/timetable-online-window.test.cjs"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        # The classic page in jsdom, a handful of tests; this only stops a hung run.
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

"""Exercise workspace keyboard controls and truthful sibling quality evidence."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_timetable_accessibility_and_sibling_quality() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Timetable frontend regressions require Node.js")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            node,
            "--test",
            "tests/frontend/timetable-accessibility.test.cjs",
            "tests/frontend/timetable-sibling-quality.test.cjs",
            "tests/frontend/timetable-english-patterns.test.cjs",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

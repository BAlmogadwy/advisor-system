"""The teaching-time rules editor in a DOM (tests/frontend/instructor-time-rules.test.cjs).

The JavaScript mirror of ``core/services/instructor_availability.py`` is held to
every case of ``tests/fixtures/instructor_availability_cases.json`` (probes,
open counts and the full summary) over the server's own default period grid,
which this wrapper hands to the suite; the rules dialog is driven from the real
template and page script.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from core.services.instructor_availability import default_period_grid

ROOT = Path(__file__).resolve().parents[1]


def test_instructor_time_rules_editor(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node or not (ROOT / "node_modules/jsdom/package.json").is_file():
        pytest.skip("The time-rules DOM tests require Node.js and the locked npm ci dependencies")
    grid = tmp_path / "grid.json"
    grid.write_text(json.dumps(default_period_grid()), encoding="utf-8")
    result = subprocess.run(
        [node, "--test", "tests/frontend/instructor-time-rules.test.cjs"],
        cwd=ROOT,
        env={**os.environ, "ITR_GRID_FILE": str(grid)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # The suite compared its grid copy with the server's: that test ran.
    assert "# skipped 0" in result.stdout or "ℹ skipped 0" in result.stdout, result.stdout

"""The instructor management page in a DOM: global tabs, the segmented
section, the add/edit dialog, a status that is only a status and a
deactivation that asks first (tests/frontend/instructor-management.test.cjs)."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_instructor_management_page_interactions():
    node = shutil.which("node")
    if not node or not (ROOT / "node_modules/jsdom/package.json").is_file():
        pytest.skip(
            "Instructor management DOM tests require Node.js and the locked npm ci dependencies"
        )
    result = subprocess.run(
        [node, "--test", "tests/frontend/instructor-management.test.cjs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

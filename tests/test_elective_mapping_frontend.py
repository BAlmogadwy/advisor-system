"""Verify both publication dialogs do not silently omit saved mappings."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_elective_mapping_dialog_interactions():
    node = shutil.which("node")
    if not node or not (ROOT / "node_modules/jsdom/package.json").is_file():
        pytest.skip("Elective DOM tests require Node.js and the locked npm ci dependencies")
    result = subprocess.run(
        [node, "--test", "tests/frontend/elective-mapping.test.cjs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

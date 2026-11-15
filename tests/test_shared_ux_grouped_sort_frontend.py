"""Run the jsdom suite for the group-aware table sorter in shared-ux.js.

``static/js/shared-ux.js`` is loaded by base.html on every page, so its sorter
is covered on its own markup rather than through one screen:
``tests/frontend/shared-ux-grouped-sort.test.cjs``.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_shared_ux_grouped_sort():
    node = shutil.which("node")
    if not node or not (ROOT / "node_modules/jsdom/package.json").is_file():
        pytest.skip("Shared UX DOM tests require Node.js and the locked npm ci dependencies")
    result = subprocess.run(
        [node, "--test", "tests/frontend/shared-ux-grouped-sort.test.cjs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

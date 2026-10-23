"""Run the jsdom suite of the global ``safeDownload`` (static/js/safe-fetch.js).

The exports download on the page through it (stage 3, B2): each answer takes
its own branch and the trigger is busy until the download ends.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_safe_download_frontend() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("The safeDownload jsdom suite requires Node.js")
    result = subprocess.run(
        [node, "--test", "tests/frontend/safe-download.test.cjs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

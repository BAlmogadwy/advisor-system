"""The design system's overflow strip (ux-controls.js `.ux-overflow`; stage 3, C2)
under jsdom: its buttons, its end flags, paging, and the active item kept in
view clear of the cue, left to right and right to left."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_ux_overflow_strip() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Frontend regressions require Node.js")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--test", "tests/frontend/ux-overflow.test.cjs"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

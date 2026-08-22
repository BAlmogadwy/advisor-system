"""CI enforcement for the control-character gate.

The pre-commit hook only runs where pre-commit is installed; a gate whose
purpose is stopping a defect that shipped TWICE cannot depend on a local
opt-in.  This test runs the same script over every tracked file on every CI
run, so --no-verify and fresh clones are covered too.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_no_control_characters_in_tracked_source():
    # --all-tracked, not an argv list: Windows caps the command line well
    # below this repository's 900 tracked paths.
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "check_control_chars.py"), "--all-tracked"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout

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


def _run(*args):
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "check_control_chars.py"), *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_stray_nul_in_a_text_file_is_caught(tmp_path):
    """The hook used to treat ANY NUL as proof of a binary and skip the whole
    file -- so `content:'\\00b7'`, which lands as NUL + "b7", sailed through the
    one gate written to stop it."""
    probe = tmp_path / "probe.css"
    probe.write_bytes(b"a{content:'\x00b7'}\n")
    result = _run(str(probe))
    assert result.returncode == 1
    assert "0x00" in result.stdout


def test_a_real_binary_is_still_skipped(tmp_path):
    probe = tmp_path / "probe.png"
    probe.write_bytes(bytes([0x89]) + b"PNG\r\n\x1a\n" + bytes(range(32)))
    assert _run(str(probe)).returncode == 0


def test_tab_newline_and_carriage_return_are_allowed(tmp_path):
    probe = tmp_path / "probe.txt"
    probe.write_bytes(b"a\tb\r\nc\n")
    assert _run(str(probe)).returncode == 0


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

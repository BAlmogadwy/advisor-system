"""The global safeHtml tag and the safe-HTML ratchet over static/js (stage 3, A1).

``tests/frontend/html-sinks.test.cjs`` checks ``safeHtml`` in
``static/js/shared-utils.js`` and runs ``tests/frontend/template-scan.cjs``
over every ``static/js/*.js``: a raw HTML interpolation that is not in
``tests/fixtures/html_sink_allowlist.json``, or more of one than it allows,
fails here. The ratchet guards every page, so CI must run it: without Node.js
and the locked npm dependencies it skips locally and fails in CI.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _node_with_jsdom() -> str | None:
    node = shutil.which("node")
    if not node:
        return None
    probe = subprocess.run(
        [node, "-e", "require.resolve('jsdom')"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return node if probe.returncode == 0 else None


def test_html_sink_ratchet_and_safe_html() -> None:
    node = _node_with_jsdom()
    if node is None:
        message = "The safe-HTML ratchet needs Node.js and the locked npm dependencies (npm ci)"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    result = subprocess.run(
        [node, "--test", "tests/frontend/html-sinks.test.cjs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

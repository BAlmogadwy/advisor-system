"""Run the real exam page script against Django-rendered HTML in a DOM.

Install the locked development dependencies with ``npm ci`` first. No browser,
server, database, or network access is used by these interaction tests.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from django.template.loader import render_to_string
from django.utils import translation

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("language", ["en", "ar"])
@pytest.mark.parametrize(
    "suite",
    [
        "exam-timetable",
        "exam-review",
        "exam-student-export",
        "exam-linked",
        "exam-locks",
        "exam-add-courses",
    ],
)
def test_exam_page_frontend_interactions(tmp_path: Path, language: str, suite: str) -> None:
    node = shutil.which("node")
    if not node or not (ROOT / "node_modules/jsdom/package.json").is_file():
        pytest.skip("Exam DOM tests require Node.js and the locked npm ci dependencies")

    with translation.override(language):
        # As exam_timetable_page renders it: both roles may Save, so both may
        # lock; only a Super Admin may delete.
        html = render_to_string(
            "core/exam_timetable.html",
            {
                "LANGUAGE_CODE": language,
                "role": "SUPER_ADMIN",
                "can_delete_exam_timetable": True,
                "can_edit_exam_timetable": True,
                "csrf_token": "test-csrf",
            },
        )
        committee_html = render_to_string(
            "core/exam_timetable.html",
            {
                "LANGUAGE_CODE": language,
                "role": "EXAM_COMMITTEE",
                "can_delete_exam_timetable": False,
                "can_edit_exam_timetable": True,
                "csrf_token": "test-csrf",
            },
        )
        # A viewer who may not Save: locks shown, never offered.
        read_only_html = render_to_string(
            "core/exam_timetable.html",
            {
                "LANGUAGE_CODE": language,
                "role": "EXAM_COMMITTEE",
                "can_delete_exam_timetable": False,
                "can_edit_exam_timetable": False,
                "csrf_token": "test-csrf",
            },
        )
    fixture = tmp_path / f"exam-{language}.html"
    fixture.write_text(html, encoding="utf-8")
    committee_fixture = tmp_path / f"exam-committee-{language}.html"
    committee_fixture.write_text(committee_html, encoding="utf-8")
    read_only_fixture = tmp_path / f"exam-read-only-{language}.html"
    read_only_fixture.write_text(read_only_html, encoding="utf-8")
    result = subprocess.run(
        [node, "--test", f"tests/frontend/{suite}.test.cjs"],
        cwd=ROOT,
        env={
            **os.environ,
            "EXAM_TEST_HTML": str(fixture),
            "EXAM_TEST_COMMITTEE_HTML": str(committee_fixture),
            "EXAM_TEST_READ_ONLY_HTML": str(read_only_fixture),
            "EXAM_TEST_LANGUAGE": language,
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        # A guard against a hung run, not a budget: the exam-timetable suite
        # (the largest) takes 97-121 s here (measured, per language), and
        # 1.4-1.8x that on a CI runner (~170-220 s), and it only grows. 360 s
        # keeps ~1.65x headroom over the slowest CI estimate; the other suites
        # take under 10 s each.
        timeout=360,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

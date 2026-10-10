"""Exercise the actual planner card and adapter without a server or database."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from django.template.loader import render_to_string

ROOT = Path(__file__).resolve().parents[1]


def test_planner_card_arabic_controls() -> None:
    html = render_to_string(
        "core/partials/pr8_async_job_card.html",
        {"TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED": True, "LANGUAGE_CODE": "ar", "pr8_job": None},
    )
    assert "تشغيل في الخلفية" in html
    assert "عرض النتيجة" in html
    assert "تشغيل مجددًا" in html


def test_pr8_frontend_interactions(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node or not (ROOT / "node_modules/jsdom/package.json").is_file():
        pytest.skip("Planner card DOM tests require Node.js and npm ci dependencies")
    fixture = tmp_path / "planner-card.html"
    fixture.write_text(
        render_to_string(
            "core/partials/pr8_async_job_card.html",
            {
                "TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED": True,
                "pr8_job": None,
                "pr8_config_json": json.dumps(
                    {
                        "submitUrl": "/planner-jobs/",
                        "pollUrl": "/planner-jobs/{job_id}/",
                        "resultUrl": "/planner-jobs/{job_id}/result/",
                        "cancelUrl": "/planner-jobs/{job_id}/cancel/",
                        "csrfToken": "test-csrf",
                        "scenarioId": None,
                        "mode": "full_rebuild",
                        "pollIntervalMs": 2000,
                    }
                ),
            },
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, "--test", "tests/frontend/pr8-planner-card.test.cjs"],
        cwd=ROOT,
        env={**os.environ, "PR8_TEST_HTML": str(fixture)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

from __future__ import annotations

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
PROCFILE_PATH = PROJECT_ROOT / "Procfile"


def _workflow() -> dict:
    # BaseLoader preserves GitHub's literal ``on`` key instead of applying the
    # YAML 1.1 boolean coercion used by PyYAML's SafeLoader.
    document = yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(document, dict)
    return document


def test_ci_runs_once_for_feature_prs_and_again_on_master() -> None:
    triggers = _workflow()["on"]

    assert triggers == {
        "push": {"branches": ["master"]},
        "pull_request": {"branches": ["master"]},
    }


def test_mypy_is_advisory_without_making_the_job_or_its_dependencies_fail() -> None:
    typecheck = _workflow()["jobs"]["typecheck"]
    mypy_step = next(step for step in typecheck["steps"] if step.get("run") == "mypy .")

    assert "continue-on-error" not in typecheck
    assert mypy_step["continue-on-error"] == "true"


def test_production_preflight_uses_isolated_sendgrid_contract() -> None:
    jobs = _workflow()["jobs"]
    preflight_env = jobs["production-preflight"]["env"]

    assert jobs["test"]["env"]["ALLOW_NO_SMTP_PROCESS"] == "true"
    assert preflight_env["ALLOW_NO_SMTP_PROCESS"] == "false"
    assert preflight_env["STUDENT_OTP_SENDGRID_ENABLED"] == "true"
    assert preflight_env["STUDENT_OTP_ASYNC_EMAIL"] == "false"
    assert preflight_env["SENDGRID_API_KEY"] == "ci-only-invalid-sendgrid-api-key-do-not-deploy"
    assert preflight_env["SENDGRID_FROM_EMAIL"] == "ci-sender@example.invalid"


def test_production_health_probe_cannot_hang_the_release_gate() -> None:
    preflight = _workflow()["jobs"]["production-preflight"]
    health_step = next(
        step
        for step in preflight["steps"]
        if step.get("name")
        == "Boot the production web command and probe post-replacement database health"
    )
    command = health_step["run"]

    assert "curl --connect-timeout 2 --max-time 3" in command
    assert "for attempt in $(seq 1 30)" in command
    assert "gunicorn config.wsgi" in command
    assert "--no-control-socket" in command


def test_procfile_disables_the_gunicorn_control_socket() -> None:
    web_command = next(
        line
        for line in PROCFILE_PATH.read_text(encoding="utf-8").splitlines()
        if line.startswith("web:")
    )

    assert "--workers 1 --worker-class gthread --threads 4" in web_command
    assert "--no-control-socket" in web_command

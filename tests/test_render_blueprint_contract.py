from __future__ import annotations

import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured

from config import settings as project_settings
from scripts.validate_render_blueprint import (
    EXPECTED_DATABASE_NAME,
    WORKER_SERVICE_NAME,
    load_blueprint,
    validate_blueprint,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT_PATH = PROJECT_ROOT / "render.yaml"
Blueprint = dict[str, Any]


@pytest.fixture
def blueprint() -> Blueprint:
    return dict(load_blueprint(BLUEPRINT_PATH))


def _service(document: Blueprint, name: str) -> Blueprint:
    return next(service for service in document["services"] if service["name"] == name)


def test_checked_in_render_blueprint_matches_production_contract(blueprint: Blueprint) -> None:
    assert validate_blueprint(blueprint, project_root=PROJECT_ROOT) == []


def test_contract_rejects_a_second_or_unknown_service(blueprint: Blueprint) -> None:
    changed = deepcopy(blueprint)
    changed["services"].append(
        {
            "type": "worker",
            "name": "unreviewed-worker",
            "runtime": "python",
            "envVars": [],
        }
    )

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert any("services must be exactly" in error for error in errors)


def test_contract_rejects_database_reference_drift(blueprint: Blueprint) -> None:
    changed = deepcopy(blueprint)
    worker = _service(changed, WORKER_SERVICE_NAME)
    database_url = next(item for item in worker["envVars"] if item["key"] == "DATABASE_URL")
    database_url["fromDatabase"]["name"] = "old-test-database"

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert any("DATABASE_URL must reference the one declared database" in error for error in errors)
    assert any("targets an undeclared or inconsistent database" in error for error in errors)


def test_contract_rejects_declaring_or_adopting_the_existing_database(
    blueprint: Blueprint,
) -> None:
    changed = deepcopy(blueprint)
    changed["databases"] = [
        {
            "name": EXPECTED_DATABASE_NAME,
            "region": "oregon",
            "plan": "basic-256mb",
            "databaseName": "advisor_system_db",
        }
    ]

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert any("must not declare or adopt PostgreSQL" in error for error in errors)


def test_contract_rejects_missing_web_health_check(blueprint: Blueprint) -> None:
    changed = deepcopy(blueprint)
    _service(changed, "advisor-system").pop("healthCheckPath", None)

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert any("healthCheckPath" in error for error in errors)


def test_contract_requires_public_student_login_safety_settings(blueprint: Blueprint) -> None:
    changed = deepcopy(blueprint)
    web = _service(changed, "advisor-system")
    redirect = next(item for item in web["envVars"] if item["key"] == "STUDENT_OTP_REDIRECT_EMAIL")
    redirect["value"] = "test-inbox@example.invalid"
    ip_mode = next(item for item in web["envVars"] if item["key"] == "IP_FROM_XFF")
    ip_mode["value"] = "false"
    smtp_password = next(item for item in web["envVars"] if item["key"] == "EMAIL_HOST_PASSWORD")
    smtp_password.pop("sync")

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert any("STUDENT_OTP_REDIRECT_EMAIL" in error for error in errors)
    assert any("IP_FROM_XFF" in error for error in errors)
    assert any("EMAIL_HOST_PASSWORD" in error for error in errors)


def test_contract_rejects_independent_worker_runtime_setting(blueprint: Blueprint) -> None:
    changed = deepcopy(blueprint)
    worker = _service(changed, WORKER_SERVICE_NAME)
    model = next(item for item in worker["envVars"] if item["key"] == "ALIBABA_LLM_MODEL")
    model.pop("fromService")
    model["sync"] = False

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert any(f"{WORKER_SERVICE_NAME}:ALIBABA_LLM_MODEL must inherit" in error for error in errors)


@pytest.mark.parametrize("key", ["TELEGRAM_INTERNAL_BASE_URL", "LOCAL_LLM_MODEL"])
def test_contract_rejects_optional_empty_values_on_worker(
    blueprint: Blueprint,
    key: str,
) -> None:
    changed = deepcopy(blueprint)
    worker = _service(changed, WORKER_SERVICE_NAME)
    worker["envVars"].append(
        {
            "key": key,
            "fromService": {
                "name": "advisor-system",
                "type": "web",
                "envVarKey": key,
            },
        }
    )

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert any(key in error and "intentionally empty value" in error for error in errors)


def test_contract_rejects_runtime_version_drift(blueprint: Blueprint, tmp_path: Path) -> None:
    (tmp_path / ".python-version").write_text("3.12.0\n", encoding="utf-8")
    (tmp_path / "runtime.txt").write_text("python-3.11.9\n", encoding="utf-8")

    errors = validate_blueprint(blueprint, project_root=tmp_path)

    assert any(".python-version must be" in error for error in errors)
    assert any("different Python versions" in error for error in errors)


def test_contract_requires_references_to_the_existing_render_database_identity(
    blueprint: Blueprint,
) -> None:
    changed = deepcopy(blueprint)
    for service in changed["services"]:
        database_url = next(item for item in service["envVars"] if item["key"] == "DATABASE_URL")
        database_url["fromDatabase"]["name"] = "advisor-db"

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert all(
        any(service["name"] in error and "DATABASE_URL" in error for error in errors)
        for service in changed["services"]
    )


def test_contract_rejects_branch_and_auto_deploy_drift(blueprint: Blueprint) -> None:
    changed = deepcopy(blueprint)
    web = _service(changed, "advisor-system")
    web["branch"] = "feature/unreviewed"
    web["autoDeployTrigger"] = "commit"

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert any("reviewed 'master' branch" in error for error in errors)
    assert any("wait for CI" in error for error in errors)


@pytest.mark.parametrize("service_name", ["advisor-system", WORKER_SERVICE_NAME])
def test_contract_pins_one_render_instance_for_long_running_services(
    blueprint: Blueprint,
    service_name: str,
) -> None:
    changed = deepcopy(blueprint)
    _service(changed, service_name)["numInstances"] = 2

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert any(service_name in error and "numInstances=1" in error for error in errors)


def test_contract_requires_disabled_worker_standby(blueprint: Blueprint) -> None:
    changed = deepcopy(blueprint)
    worker = _service(changed, WORKER_SERVICE_NAME)
    worker["startCommand"] = "python manage.py telegram_advisor_worker"

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert any("no-lease standby" in error for error in errors)


def test_contract_keeps_public_channels_and_llm_egress_off(blueprint: Blueprint) -> None:
    changed = deepcopy(blueprint)
    web = _service(changed, "advisor-system")
    env = {entry["key"]: entry for entry in web["envVars"]}
    env["TELEGRAM_ADVISOR_ENABLED"]["value"] = "true"
    env["TELEGRAM_SEND_TIMETABLE_IMAGES"]["value"] = "true"
    env["ALIBABA_LLM_ALLOW_LIVE_REQUESTS"]["value"] = "true"

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert any("TELEGRAM_ADVISOR_ENABLED" in error for error in errors)
    assert any("TELEGRAM_SEND_TIMETABLE_IMAGES" in error for error in errors)
    assert any("ALIBABA_LLM_ALLOW_LIVE_REQUESTS" in error for error in errors)


def test_contract_allows_sync_false_only_for_reviewed_secrets(blueprint: Blueprint) -> None:
    changed = deepcopy(blueprint)
    web = _service(changed, "advisor-system")
    timeout = next(item for item in web["envVars"] if item["key"] == "EMAIL_TIMEOUT")
    password = next(item for item in web["envVars"] if item["key"] == "EMAIL_HOST_PASSWORD")
    timeout.pop("value")
    timeout["sync"] = False
    password["value"] = "must-never-be-in-yaml"

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert any("EMAIL_TIMEOUT is not a secret" in error for error in errors)
    assert any("EMAIL_HOST_PASSWORD must not contain" in error for error in errors)


def test_contract_requires_web_smtp_and_async_otp_without_exposing_it_to_workers(
    blueprint: Blueprint,
) -> None:
    changed = deepcopy(blueprint)
    web = _service(changed, "advisor-system")
    worker = _service(changed, WORKER_SERVICE_NAME)
    web_env = {entry["key"]: entry for entry in web["envVars"]}
    worker_env = {entry["key"]: entry for entry in worker["envVars"]}
    web_env["STUDENT_OTP_ASYNC_EMAIL"]["value"] = "false"
    web_env["ALLOW_NO_SMTP_PROCESS"]["value"] = "true"
    worker_env["ALLOW_NO_SMTP_PROCESS"]["value"] = "false"

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert any("STUDENT_OTP_ASYNC_EMAIL" in error for error in errors)
    assert any("ALLOW_NO_SMTP_PROCESS" in error for error in errors)
    assert any("web-only SMTP validation" in error for error in errors)


def test_contract_rejects_cross_region_database_usage(blueprint: Blueprint) -> None:
    changed = deepcopy(blueprint)
    _service(changed, WORKER_SERVICE_NAME)["region"] = "frankfurt"

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert any("run beside the database" in error for error in errors)


def test_public_production_process_requires_smtp_credentials() -> None:
    with pytest.raises(ImproperlyConfigured, match="EMAIL_HOST_USER"):
        project_settings._require_production_smtp(
            debug=False,
            allow_no_smtp_process=False,
            backend="django.core.mail.backends.smtp.EmailBackend",
            host_user="",
            host_password="",
        )

    with pytest.raises(ImproperlyConfigured, match="SMTP email backend"):
        project_settings._require_production_smtp(
            debug=False,
            allow_no_smtp_process=False,
            backend="django.core.mail.backends.console.EmailBackend",
            host_user="advisor@example.invalid",
            host_password="ci-only-password",
        )


def test_non_web_production_process_can_explicitly_omit_smtp() -> None:
    project_settings._require_production_smtp(
        debug=False,
        allow_no_smtp_process=True,
        backend="django.core.mail.backends.smtp.EmailBackend",
        host_user="",
        host_password="",
    )


def _production_settings_env(*, allow_no_smtp_process: bool) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "DJANGO_DEBUG": "false",
            "DJANGO_SECRET_KEY": "ci-only-settings-import-secret-1234567890",
            "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "EMAIL_HOST_USER": "",
            "EMAIL_HOST_PASSWORD": "",
            "ALLOW_NO_SMTP_PROCESS": "true" if allow_no_smtp_process else "false",
        }
    )
    return env


def test_real_web_settings_import_fails_closed_without_smtp() -> None:
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and fixed import.
        [sys.executable, "-c", "import config.settings"],
        cwd=PROJECT_ROOT,
        env=_production_settings_env(allow_no_smtp_process=False),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "EMAIL_HOST_USER and EMAIL_HOST_PASSWORD" in completed.stderr


def test_real_non_web_settings_import_requires_an_explicit_smtp_opt_out() -> None:
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and fixed import.
        [sys.executable, "-c", "import config.settings"],
        cwd=PROJECT_ROOT,
        env=_production_settings_env(allow_no_smtp_process=True),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr

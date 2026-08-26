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
    CRON_SERVICE_NAME,
    EXPECTED_CSRF_TRUSTED_ORIGINS,
    EXPECTED_DATABASE_NAME,
    EXPECTED_DJANGO_ALLOWED_HOSTS,
    EXPECTED_PUBLIC_ORIGIN,
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


def test_predeploy_explicitly_creates_the_legacy_cache_table(blueprint: Blueprint) -> None:
    command = _service(blueprint, "advisor-system")["preDeployCommand"]

    assert "createcachetable django_cache_table --database default" in command


def test_contract_requires_public_student_login_safety_settings(blueprint: Blueprint) -> None:
    changed = deepcopy(blueprint)
    web = _service(changed, "advisor-system")
    ip_mode = next(item for item in web["envVars"] if item["key"] == "IP_FROM_XFF")
    ip_mode["value"] = "false"
    sendgrid_key = next(item for item in web["envVars"] if item["key"] == "SENDGRID_API_KEY")
    sendgrid_key.pop("sync")

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert any("IP_FROM_XFF" in error for error in errors)
    assert any("SENDGRID_API_KEY" in error for error in errors)


def test_contract_pins_render_and_custom_domain_request_hosts(blueprint: Blueprint) -> None:
    web = _service(blueprint, "advisor-system")
    web_env = {entry["key"]: entry for entry in web["envVars"]}

    assert web_env["DJANGO_ALLOWED_HOSTS"]["value"] == EXPECTED_DJANGO_ALLOWED_HOSTS
    assert web_env["CSRF_TRUSTED_ORIGINS"]["value"] == EXPECTED_CSRF_TRUSTED_ORIGINS
    # Linking stays on the proven Render origin until a separate, deliberate
    # Telegram cutover; adding browser hosts must not silently change it.
    assert web_env["TELEGRAM_PUBLIC_BASE_URL"]["value"] == EXPECTED_PUBLIC_ORIGIN

    changed = deepcopy(blueprint)
    changed_env = {entry["key"]: entry for entry in _service(changed, "advisor-system")["envVars"]}
    changed_env["DJANGO_ALLOWED_HOSTS"]["value"] = "advisor-system-v9zs.onrender.com"
    changed_env["CSRF_TRUSTED_ORIGINS"]["value"] = EXPECTED_PUBLIC_ORIGIN

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert any("DJANGO_ALLOWED_HOSTS" in error for error in errors)
    assert any("CSRF_TRUSTED_ORIGINS" in error for error in errors)


@pytest.mark.parametrize("secret_key", ["SENDGRID_API_KEY", "SENDGRID_FROM_EMAIL"])
@pytest.mark.parametrize("service_name", [WORKER_SERVICE_NAME, CRON_SERVICE_NAME])
def test_contract_keeps_sendgrid_secrets_value_free_and_web_only(
    blueprint: Blueprint, secret_key: str, service_name: str
) -> None:
    web_entry = next(
        item
        for item in _service(blueprint, "advisor-system")["envVars"]
        if item["key"] == secret_key
    )
    assert web_entry == {"key": secret_key, "sync": False}
    assert secret_key not in {item["key"] for item in _service(blueprint, service_name)["envVars"]}

    leaked_value = deepcopy(blueprint)
    leaked_entry = next(
        item
        for item in _service(leaked_value, "advisor-system")["envVars"]
        if item["key"] == secret_key
    )
    leaked_entry["value"] = "must-never-be-in-yaml"

    copied_to_non_web = deepcopy(blueprint)
    _service(copied_to_non_web, service_name)["envVars"].append(
        {
            "key": secret_key,
            "fromService": {
                "name": "advisor-system",
                "type": "web",
                "envVarKey": secret_key,
            },
        }
    )

    assert any(
        secret_key in error and "must not contain" in error
        for error in validate_blueprint(leaked_value, project_root=PROJECT_ROOT)
    )
    assert any(
        secret_key in error and "web-only" in error
        for error in validate_blueprint(copied_to_non_web, project_root=PROJECT_ROOT)
    )


@pytest.mark.parametrize(
    "retired_key",
    [
        "STUDENT_LOGIN_NO_OTP",
        "STUDENT_OTP_REDIRECT_EMAIL",
        "TELEGRAM_LINK_OTP_REDIRECT_EMAIL",
    ],
)
@pytest.mark.parametrize("service_name", ["advisor-system", WORKER_SERVICE_NAME, CRON_SERVICE_NAME])
def test_contract_rejects_retired_student_auth_testing_controls_on_every_service(
    blueprint: Blueprint, service_name: str, retired_key: str
) -> None:
    target = _service(blueprint, service_name)
    assert retired_key not in {item["key"] for item in target["envVars"]}

    changed = deepcopy(blueprint)
    changed_target = _service(changed, service_name)
    changed_target["envVars"].append({"key": retired_key, "value": "true"})

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert any(
        service_name in error and retired_key in error and "retired" in error for error in errors
    )


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


def test_contract_requires_gunicorn_control_socket_mitigation(blueprint: Blueprint) -> None:
    changed = deepcopy(blueprint)
    web = _service(changed, "advisor-system")
    web["startCommand"] = web["startCommand"].replace(" --no-control-socket", "")

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert any("gthread Gunicorn process" in error for error in errors)


def test_contract_requires_reviewed_live_rollout_and_both_image_exports(
    blueprint: Blueprint,
) -> None:
    changed = deepcopy(blueprint)
    web = _service(changed, "advisor-system")
    env = {entry["key"]: entry for entry in web["envVars"]}
    env["TELEGRAM_ADVISOR_ENABLED"]["value"] = "false"
    env["TELEGRAM_SEND_TIMETABLE_IMAGES"]["value"] = "false"
    env["TELEGRAM_SEND_GRADUATION_IMAGES"]["value"] = "false"
    env["ALIBABA_LLM_ALLOW_LIVE_REQUESTS"]["value"] = "false"
    env["STUDENT_ADVISOR_V2_ENABLED"]["value"] = "false"

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert any("TELEGRAM_ADVISOR_ENABLED" in error for error in errors)
    assert any("TELEGRAM_SEND_TIMETABLE_IMAGES" in error for error in errors)
    assert any("TELEGRAM_SEND_GRADUATION_IMAGES" in error for error in errors)
    assert any("ALIBABA_LLM_ALLOW_LIVE_REQUESTS" in error for error in errors)
    assert any("STUDENT_ADVISOR_V2_ENABLED" in error for error in errors)


@pytest.mark.parametrize(
    ("key", "invalid_value"),
    [
        ("STUDENT_ADVISOR_V21_ENABLED", "true"),
        ("STUDENT_ADVISOR_V21_PLAN_MAX_TOKENS", "901"),
        ("STUDENT_ADVISOR_V21_PLAN_TIMEOUT_SECONDS", "46"),
    ],
)
def test_contract_pins_student_advisor_v21_rollout_and_plan_budget(
    blueprint: Blueprint,
    key: str,
    invalid_value: str,
) -> None:
    changed = deepcopy(blueprint)
    web = _service(changed, "advisor-system")
    env = {entry["key"]: entry for entry in web["envVars"]}
    env[key]["value"] = invalid_value

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert any(key in error for error in errors)


def test_contract_keeps_playwright_browser_inside_the_deployed_worker(
    blueprint: Blueprint, tmp_path: Path
) -> None:
    (tmp_path / ".python-version").write_text("3.11.9\n", encoding="utf-8")
    (tmp_path / "build.sh").write_text(
        "#!/usr/bin/env bash\npython -m playwright install chromium\n",
        encoding="utf-8",
    )
    changed = deepcopy(blueprint)
    worker = _service(changed, WORKER_SERVICE_NAME)
    playwright_path = next(
        item for item in worker["envVars"] if item["key"] == "PLAYWRIGHT_BROWSERS_PATH"
    )
    playwright_path["value"] = "/opt/render/.cache/ms-playwright"

    errors = validate_blueprint(changed, project_root=tmp_path)

    assert any("build.sh must install Chromium" in error for error in errors)
    assert any("PLAYWRIGHT_BROWSERS_PATH" in error for error in errors)


def test_contract_allows_sync_false_only_for_reviewed_secrets(blueprint: Blueprint) -> None:
    changed = deepcopy(blueprint)
    web = _service(changed, "advisor-system")
    timeout = next(item for item in web["envVars"] if item["key"] == "SENDGRID_TIMEOUT_SECONDS")
    api_key = next(item for item in web["envVars"] if item["key"] == "SENDGRID_API_KEY")
    timeout.pop("value")
    timeout["sync"] = False
    api_key["value"] = "must-never-be-in-yaml"

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert any("SENDGRID_TIMEOUT_SECONDS is not a secret" in error for error in errors)
    assert any("SENDGRID_API_KEY must not contain" in error for error in errors)


def test_contract_requires_web_sendgrid_and_synchronous_otp_without_exposing_it_to_workers(
    blueprint: Blueprint,
) -> None:
    changed = deepcopy(blueprint)
    web = _service(changed, "advisor-system")
    worker = _service(changed, WORKER_SERVICE_NAME)
    web_env = {entry["key"]: entry for entry in web["envVars"]}
    worker_env = {entry["key"]: entry for entry in worker["envVars"]}
    assert web_env["SENDGRID_TIMEOUT_SECONDS"]["value"] == "3"
    assert web_env["SENDGRID_MAX_SUBMISSIONS"]["value"] == "4700"
    assert web_env["SENDGRID_SUBMISSION_WINDOW_SECONDS"]["value"] == "86400"
    assert web_env["STUDENT_OTP_ASYNC_EMAIL"]["value"] == "false"
    assert web_env["STUDENT_OTP_RESPONSE_FLOOR_SECONDS"]["value"] == "3.5"
    web_env["STUDENT_OTP_ASYNC_EMAIL"]["value"] = "true"
    web_env["STUDENT_OTP_RESPONSE_FLOOR_SECONDS"]["value"] = "0"
    web_env["STUDENT_OTP_SENDGRID_ENABLED"]["value"] = "false"
    web_env["SENDGRID_MAX_SUBMISSIONS"]["value"] = "4701"
    web_env["SENDGRID_SUBMISSION_WINDOW_SECONDS"]["value"] = "1"
    web_env["ALLOW_NO_SMTP_PROCESS"]["value"] = "true"
    worker_env["ALLOW_NO_SMTP_PROCESS"]["value"] = "false"

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert any("STUDENT_OTP_ASYNC_EMAIL" in error for error in errors)
    assert any("STUDENT_OTP_RESPONSE_FLOOR_SECONDS" in error for error in errors)
    assert any("STUDENT_OTP_SENDGRID_ENABLED" in error for error in errors)
    assert any("SENDGRID_MAX_SUBMISSIONS" in error for error in errors)
    assert any("SENDGRID_SUBMISSION_WINDOW_SECONDS" in error for error in errors)
    assert any("ALLOW_NO_SMTP_PROCESS" in error for error in errors)
    assert any("web-only email validation" in error for error in errors)


def test_contract_rejects_cross_region_database_usage(blueprint: Blueprint) -> None:
    changed = deepcopy(blueprint)
    _service(changed, WORKER_SERVICE_NAME)["region"] = "frankfurt"

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)

    assert any("run beside the database" in error for error in errors)


def test_public_production_process_requires_sendgrid_to_be_enabled() -> None:
    with pytest.raises(ImproperlyConfigured, match="STUDENT_OTP_SENDGRID_ENABLED"):
        project_settings._require_production_sendgrid(
            debug=False,
            allow_no_smtp_process=False,
            enabled=False,
            api_key="",
            from_email="",
        )


@pytest.mark.parametrize(
    ("api_key", "from_email", "missing_setting"),
    [
        ("", "verified-sender@example.invalid", "SENDGRID_API_KEY"),
        ("SG.ci-only", "", "SENDGRID_FROM_EMAIL"),
    ],
)
def test_public_production_process_requires_sendgrid_credentials(
    api_key: str, from_email: str, missing_setting: str
) -> None:
    with pytest.raises(ImproperlyConfigured, match=missing_setting):
        project_settings._require_production_sendgrid(
            debug=False,
            allow_no_smtp_process=False,
            enabled=True,
            api_key=api_key,
            from_email=from_email,
        )


def test_public_production_process_accepts_complete_sendgrid_configuration() -> None:
    project_settings._require_production_sendgrid(
        debug=False,
        allow_no_smtp_process=False,
        enabled=True,
        api_key="SG.ci-only",
        from_email="verified-sender@example.invalid",
        async_email=False,
    )


def test_public_production_process_rejects_async_sendgrid_delivery() -> None:
    with pytest.raises(ImproperlyConfigured, match="STUDENT_OTP_ASYNC_EMAIL"):
        project_settings._require_production_sendgrid(
            debug=False,
            allow_no_smtp_process=False,
            enabled=True,
            api_key="SG.ci-only",
            from_email="verified-sender@example.invalid",
            async_email=True,
        )


@pytest.mark.parametrize(
    ("max_submissions", "window_seconds", "setting_name"),
    [
        (0, 86_400, "SENDGRID_MAX_SUBMISSIONS"),
        (4700, 0, "SENDGRID_SUBMISSION_WINDOW_SECONDS"),
    ],
)
def test_public_production_process_rejects_disabled_sendgrid_budget(
    max_submissions: int, window_seconds: int, setting_name: str
) -> None:
    with pytest.raises(ImproperlyConfigured, match=setting_name):
        project_settings._require_production_sendgrid(
            debug=False,
            allow_no_smtp_process=False,
            enabled=True,
            api_key="SG.ci-only",
            from_email="verified-sender@example.invalid",
            max_submissions=max_submissions,
            submission_window_seconds=window_seconds,
        )


def test_retention_cron_includes_student_login_otp_purge(blueprint: Blueprint) -> None:
    cron = _service(blueprint, CRON_SERVICE_NAME)
    assert "purge_student_login_otps --apply" in cron["startCommand"]

    changed = deepcopy(blueprint)
    changed_cron = _service(changed, CRON_SERVICE_NAME)
    changed_cron["startCommand"] = changed_cron["startCommand"].replace(
        " && python manage.py purge_student_login_otps --apply", ""
    )

    errors = validate_blueprint(changed, project_root=PROJECT_ROOT)
    assert any("reviewed retention command" in error for error in errors)


def test_non_web_production_process_can_explicitly_omit_sendgrid() -> None:
    project_settings._require_production_sendgrid(
        debug=False,
        allow_no_smtp_process=True,
        enabled=False,
        api_key="",
        from_email="",
    )


def _production_settings_env(
    *,
    allow_no_smtp_process: bool,
    sendgrid_enabled: bool = False,
    api_key: str = "",
    from_email: str = "",
    async_email: bool = False,
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "DJANGO_DEBUG": "false",
            "DJANGO_SECRET_KEY": "ci-only-settings-import-secret-1234567890",
            "STUDENT_OTP_SENDGRID_ENABLED": "true" if sendgrid_enabled else "false",
            "SENDGRID_API_KEY": api_key,
            "SENDGRID_FROM_EMAIL": from_email,
            "STUDENT_OTP_ASYNC_EMAIL": "true" if async_email else "false",
            "ALLOW_NO_SMTP_PROCESS": "true" if allow_no_smtp_process else "false",
        }
    )
    return env


def test_real_web_settings_import_fails_closed_when_sendgrid_is_disabled() -> None:
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and fixed import.
        [sys.executable, "-c", "import config.settings"],
        cwd=PROJECT_ROOT,
        env=_production_settings_env(allow_no_smtp_process=False),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "STUDENT_OTP_SENDGRID_ENABLED" in completed.stderr


def test_real_web_settings_import_fails_closed_without_sendgrid_credentials() -> None:
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and fixed import.
        [sys.executable, "-c", "import config.settings"],
        cwd=PROJECT_ROOT,
        env=_production_settings_env(
            allow_no_smtp_process=False,
            sendgrid_enabled=True,
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "SENDGRID_API_KEY, SENDGRID_FROM_EMAIL" in completed.stderr


def test_real_web_settings_import_fails_closed_when_async_email_is_enabled() -> None:
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and fixed import.
        [sys.executable, "-c", "import config.settings"],
        cwd=PROJECT_ROOT,
        env=_production_settings_env(
            allow_no_smtp_process=False,
            sendgrid_enabled=True,
            api_key="SG.ci-only",
            from_email="verified-sender@example.invalid",
            async_email=True,
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "STUDENT_OTP_ASYNC_EMAIL" in completed.stderr


def test_real_non_web_settings_import_requires_an_explicit_email_opt_out() -> None:
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and fixed import.
        [sys.executable, "-c", "import config.settings"],
        cwd=PROJECT_ROOT,
        env=_production_settings_env(allow_no_smtp_process=True),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr

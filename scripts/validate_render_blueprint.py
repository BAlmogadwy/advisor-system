"""Fail closed when the checked-in Render Blueprint drifts from production.

This deliberately validates the small set of deployment invariants that are
easy to break silently in Render's dashboard: resource identity, database
wiring, the migration/health contract, and the worker's inherited runtime
configuration.  It never reads environment values or contacts Render.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PYTHON_VERSION = "3.11.9"
EXPECTED_SERVICE_IDENTITIES = {
    ("web", "advisor-system"),
    ("worker", "advisor-telegram-worker"),
    ("cron", "advisor-purge-planner-drafts"),
}
EXPECTED_DATABASE_NAME = "advisor-system-db"
EXPECTED_HEALTH_PATH = "/health/"
EXPECTED_REGION = "oregon"
EXPECTED_BRANCH = "master"
EXPECTED_AUTO_DEPLOY_TRIGGER = "checksPass"
EXPECTED_SERVICE_PLAN = "starter"
EXPECTED_NUM_INSTANCES = 1
EXPECTED_PUBLIC_ORIGIN = "https://advisor-system-v9zs.onrender.com"
EXPECTED_DJANGO_ALLOWED_HOSTS = (
    "advisor-system-v9zs.onrender.com,smartacademicadviser.online,www.smartacademicadviser.online"
)
EXPECTED_CSRF_TRUSTED_ORIGINS = (
    "https://advisor-system-v9zs.onrender.com,"
    "https://smartacademicadviser.online,"
    "https://www.smartacademicadviser.online"
)
EXPECTED_NO_LEGACY_IMPORT_PATH = str(PurePosixPath("/", "tmp", "advisor-no-legacy-import.sqlite3"))
EXPECTED_BUILD_COMMAND = "chmod +x build.sh && ./build.sh"
EXPECTED_PLAYWRIGHT_BROWSERS_PATH = "0"
EXPECTED_PLAYWRIGHT_INSTALL_COMMAND = (
    "PLAYWRIGHT_BROWSERS_PATH=0 python -m playwright install chromium"
)
WEB_SERVICE_NAME = "advisor-system"
WORKER_SERVICE_NAME = "advisor-telegram-worker"
CRON_SERVICE_NAME = "advisor-purge-planner-drafts"
EXPECTED_WEB_START_COMMAND = (
    "gunicorn config.wsgi --bind 0.0.0.0:$PORT --workers 1 "
    "--worker-class gthread --threads 4 --timeout 120 --no-control-socket"
)
EXPECTED_PROCFILE_WEB_COMMAND = f"web: {EXPECTED_WEB_START_COMMAND}"
EXPECTED_WORKER_START_COMMAND = (
    "python manage.py telegram_advisor_worker --sleep 1 --max-attempts 3 --standby-when-disabled"
)
EXPECTED_CRON_START_COMMAND = (
    "python manage.py purge_planner_drafts --apply && "
    "python manage.py purge_telegram_tokens --apply && "
    "python manage.py purge_student_login_otps --apply"
)
EXPECTED_PREDEPLOY_COMMAND = (
    "python manage.py migrate --noinput && "
    "python manage.py createcachetable --database default && "
    "python manage.py normalise_exam_runs && "
    "python manage.py purge_rate_limit_buckets"
)
WEB_SECRET_KEYS = frozenset(
    {
        "SENDGRID_API_KEY",
        "SENDGRID_FROM_EMAIL",
        "PORTAL_ADMIN_USERNAME",
        "PORTAL_ADMIN_PASSWORD",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_WEBHOOK_SECRET",
        # The Alibaba endpoint contains a workspace identifier and is treated as
        # deployment-secret metadata alongside the bearer key.
        "ALIBABA_LLM_BASE_URL",
        "ALIBABA_LLM_API_KEY",
    }
)
WEB_FIXED_ENV_VALUES = {
    "DJANGO_DEBUG": "false",
    "PYTHON_VERSION": EXPECTED_PYTHON_VERSION,
    "PLAYWRIGHT_BROWSERS_PATH": EXPECTED_PLAYWRIGHT_BROWSERS_PATH,
    "ALLOW_NO_SMTP_PROCESS": "false",
    # The turn's wall-clock ceiling; sized against gunicorn's --timeout 120.
    "STUDENT_ADVISOR_V2_TURN_BUDGET_SECONDS": "90",
    "DJANGO_ALLOWED_HOSTS": EXPECTED_DJANGO_ALLOWED_HOSTS,
    "CSRF_TRUSTED_ORIGINS": EXPECTED_CSRF_TRUSTED_ORIGINS,
    "ADVISOR_DB_PATH": EXPECTED_NO_LEGACY_IMPORT_PATH,
    "SENDGRID_FROM_NAME": "بوابة الطالب",
    "SENDGRID_TIMEOUT_SECONDS": "3",
    "SENDGRID_MAX_SUBMISSIONS": "4700",
    "SENDGRID_SUBMISSION_WINDOW_SECONDS": "86400",
    "STUDENT_OTP_SENDGRID_ENABLED": "true",
    "STUDENT_OTP_RESEND_DELAY_SECONDS": "50",
    "STUDENT_OTP_RESPONSE_FLOOR_SECONDS": "3.5",
    "STUDENT_EMAIL_DOMAIN": "taibahu.edu.sa",
    "STUDENT_OTP_TTL_SECONDS": "600",
    "STUDENT_OTP_MAX_ATTEMPTS": "5",
    "STUDENT_OTP_MAX_SENDS": "3",
    "STUDENT_OTP_SEND_WINDOW_SECONDS": "900",
    "STUDENT_OTP_ASYNC_EMAIL": "false",
    "IP_FROM_XFF": "true",
    "TELEGRAM_ADVISOR_ENABLED": "true",
    "TELEGRAM_PUBLIC_BASE_URL": EXPECTED_PUBLIC_ORIGIN,
    "TELEGRAM_LINK_TOKEN_TTL_SECONDS": "900",
    "TELEGRAM_LINK_AUTH_MAX_AGE_SECONDS": "600",
    "TELEGRAM_MAX_PENDING_PER_LINK": "10",
    "TELEGRAM_API_TIMEOUT_SECONDS": "30",
    "TELEGRAM_DISPATCH_SYNC": "false",
    "TELEGRAM_INTERNAL_BASE_URL": "",
    "TELEGRAM_SEND_TIMETABLE_IMAGES": "true",
    "TELEGRAM_SEND_GRADUATION_IMAGES": "true",
    "LLM_BACKEND": "alibaba",
    "LOCAL_LLM_BASE_URL": "http://127.0.0.1:1234/v1",
    "LOCAL_LLM_MODEL": "",
    "LOCAL_LLM_TIMEOUT_SECONDS": "120",
    "LOCAL_LLM_MAX_TOKENS": "1400",
    "LOCAL_LLM_ALLOW_REMOTE": "false",
    "ALIBABA_LLM_MODEL": "qwen3.7-plus",
    "ALIBABA_LLM_ENABLE_THINKING": "false",
    "ALIBABA_LLM_TIMEOUT_SECONDS": "75",
    "ALIBABA_LLM_MAX_TOKENS": "3000",
    "ALIBABA_LLM_MAX_RETRIES": "1",
    "ALIBABA_LLM_ALLOW_LIVE_REQUESTS": "true",
    "VIRTUAL_ADVISOR_AGENT_LOOP_ENABLED": "false",
    "VIRTUAL_ADVISOR_MAX_TOOL_ITERATIONS": "5",
    "VIRTUAL_ADVISOR_MAX_TOOL_CALLS": "12",
    "VIRTUAL_ADVISOR_LOOP_MAX_TOKENS": "3000",
    "VIRTUAL_ADVISOR_TOOL_TURN_TIMEOUT_SECONDS": "75",
    "STUDENT_ADVISOR_V2_ENABLED": "true",
    "STUDENT_ADVISOR_V21_ENABLED": "false",
    "STUDENT_ADVISOR_V21_PLAN_MAX_TOKENS": "900",
    "STUDENT_ADVISOR_V21_PLAN_TIMEOUT_SECONDS": "45",
    "STUDENT_ADVISOR_V2_MAX_TOOL_ITERATIONS": "4",
    "STUDENT_ADVISOR_V2_MAX_TOOL_CALLS": "8",
    "STUDENT_ADVISOR_V2_MAX_TOKENS": "1800",
    "STUDENT_ADVISOR_V2_TOOL_TIMEOUT_SECONDS": "75",
}

# Student email delivery belongs only to the public web process. Neither
# secrets nor non-secret controls may be copied into the Telegram worker or the
# retention cron.
STUDENT_EMAIL_WEB_ONLY_ENV_KEYS = frozenset(
    {
        "SENDGRID_API_KEY",
        "SENDGRID_FROM_EMAIL",
        "SENDGRID_FROM_NAME",
        "SENDGRID_TIMEOUT_SECONDS",
        "SENDGRID_MAX_SUBMISSIONS",
        "SENDGRID_SUBMISSION_WINDOW_SECONDS",
        "STUDENT_OTP_SENDGRID_ENABLED",
        "STUDENT_OTP_RESEND_DELAY_SECONDS",
        "STUDENT_OTP_RESPONSE_FLOOR_SECONDS",
        "STUDENT_OTP_ASYNC_EMAIL",
    }
)

# These values affect the worker's ability to execute an adviser turn or its
# safety/runtime bounds.  The web service owns them; the worker must reference
# that one copy rather than acquire an independent value in Render.
WORKER_INHERITED_ENV_KEYS = frozenset(
    {
        "DJANGO_SECRET_KEY",
        "DJANGO_ALLOWED_HOSTS",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ADVISOR_ENABLED",
        "TELEGRAM_PUBLIC_BASE_URL",
        "TELEGRAM_LINK_TOKEN_TTL_SECONDS",
        "TELEGRAM_API_TIMEOUT_SECONDS",
        "TELEGRAM_SEND_TIMETABLE_IMAGES",
        "TELEGRAM_SEND_GRADUATION_IMAGES",
        "LLM_BACKEND",
        "LOCAL_LLM_BASE_URL",
        "LOCAL_LLM_TIMEOUT_SECONDS",
        "LOCAL_LLM_MAX_TOKENS",
        "LOCAL_LLM_ALLOW_REMOTE",
        "ALIBABA_LLM_BASE_URL",
        "ALIBABA_LLM_API_KEY",
        "ALIBABA_LLM_MODEL",
        "ALIBABA_LLM_ENABLE_THINKING",
        "ALIBABA_LLM_TIMEOUT_SECONDS",
        "ALIBABA_LLM_MAX_TOKENS",
        "ALIBABA_LLM_MAX_RETRIES",
        "ALIBABA_LLM_ALLOW_LIVE_REQUESTS",
        "VIRTUAL_ADVISOR_AGENT_LOOP_ENABLED",
        "VIRTUAL_ADVISOR_MAX_TOOL_ITERATIONS",
        "VIRTUAL_ADVISOR_MAX_TOOL_CALLS",
        "VIRTUAL_ADVISOR_LOOP_MAX_TOKENS",
        "VIRTUAL_ADVISOR_TOOL_TURN_TIMEOUT_SECONDS",
        "STUDENT_ADVISOR_V2_ENABLED",
        "STUDENT_ADVISOR_V21_ENABLED",
        "STUDENT_ADVISOR_V21_PLAN_MAX_TOKENS",
        "STUDENT_ADVISOR_V21_PLAN_TIMEOUT_SECONDS",
        "STUDENT_ADVISOR_V2_TURN_BUDGET_SECONDS",
        "STUDENT_ADVISOR_V2_MAX_TOOL_ITERATIONS",
        "STUDENT_ADVISOR_V2_MAX_TOOL_CALLS",
        "STUDENT_ADVISOR_V2_MAX_TOKENS",
        "STUDENT_ADVISOR_V2_TOOL_TIMEOUT_SECONDS",
    }
)
WORKER_OPTIONAL_EMPTY_ENV_KEYS = frozenset(
    {
        "TELEGRAM_INTERNAL_BASE_URL",
        "LOCAL_LLM_MODEL",
    }
)
RETIRED_STUDENT_AUTH_ENV_KEYS = frozenset(
    {
        # Production OTP delivery has no bypass or receiver-redirect mode. Keep
        # these retired controls out of every Blueprint service so a future code
        # change cannot accidentally revive stale dashboard configuration.
        "STUDENT_LOGIN_NO_OTP",
        "STUDENT_OTP_REDIRECT_EMAIL",
        "TELEGRAM_LINK_OTP_REDIRECT_EMAIL",
    }
)


def load_blueprint(path: Path) -> Mapping[str, Any]:
    """Load one Blueprint and reject a non-mapping document."""

    with path.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, Mapping):
        raise ValueError("render.yaml must contain one YAML mapping.")
    return document


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _env_entries(service: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return _mapping_list(service.get("envVars"))


def _env_map(service: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(entry.get("key")): entry
        for entry in _env_entries(service)
        if isinstance(entry.get("key"), str)
    }


def _python_version_errors(project_root: Path) -> list[str]:
    errors: list[str] = []
    version_file = project_root / ".python-version"
    legacy_file = project_root / "runtime.txt"
    try:
        version = version_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return [".python-version is required for every Render Python resource."]

    if version != EXPECTED_PYTHON_VERSION:
        errors.append(f".python-version must be {EXPECTED_PYTHON_VERSION!r}; found {version!r}.")
    if not re.fullmatch(r"3\.11\.\d+", version):
        errors.append("Render must remain on the supported Python 3.11 patch line.")

    if legacy_file.exists():
        legacy = legacy_file.read_text(encoding="utf-8").strip()
        if legacy.removeprefix("python-") != version:
            errors.append("runtime.txt and .python-version specify different Python versions.")
    return errors


def _playwright_build_errors(project_root: Path) -> list[str]:
    """Require browser binaries to survive Render build-image promotion."""

    build_script = project_root / "build.sh"
    try:
        lines = {line.strip() for line in build_script.read_text(encoding="utf-8").splitlines()}
    except FileNotFoundError:
        return ["build.sh is required for the Render image worker."]

    if EXPECTED_PLAYWRIGHT_INSTALL_COMMAND not in lines:
        return [
            "build.sh must install Chromium with PLAYWRIGHT_BROWSERS_PATH=0 so the "
            "running Render worker can resolve the browser binary."
        ]
    return []


def validate_blueprint(
    document: Mapping[str, Any], *, project_root: Path = PROJECT_ROOT
) -> list[str]:
    """Return every production-contract violation in deterministic order."""

    errors = _python_version_errors(project_root)
    errors.extend(_playwright_build_errors(project_root))
    procfile_path = project_root / "Procfile"
    try:
        procfile_lines = {
            line.strip()
            for line in procfile_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    except OSError:
        procfile_lines = set()
    if EXPECTED_PROCFILE_WEB_COMMAND not in procfile_lines:
        errors.append(
            "Procfile web command must match the reviewed Render Gunicorn command, "
            "including --no-control-socket."
        )
    services = _mapping_list(document.get("services"))
    databases = _mapping_list(document.get("databases"))

    identities = [(str(item.get("type")), str(item.get("name"))) for item in services]
    if len(identities) != len(set(identities)):
        errors.append("Render service type/name pairs must be unique.")
    if set(identities) != EXPECTED_SERVICE_IDENTITIES or len(identities) != len(
        EXPECTED_SERVICE_IDENTITIES
    ):
        errors.append(
            "Render services must be exactly: "
            + ", ".join(f"{kind}:{name}" for kind, name in sorted(EXPECTED_SERVICE_IDENTITIES))
            + "."
        )

    services_by_name = {
        str(service.get("name")): service
        for service in services
        if isinstance(service.get("name"), str)
    }
    for service in services:
        name = str(service.get("name") or "<unnamed>")
        if service.get("runtime") != "python":
            errors.append(f"{name} must use Render's Python runtime.")
        if service.get("region") != EXPECTED_REGION:
            errors.append(f"{name} must run beside the database in {EXPECTED_REGION!r}.")
        if service.get("plan") != EXPECTED_SERVICE_PLAN:
            errors.append(f"{name} must use service plan {EXPECTED_SERVICE_PLAN!r}.")
        if service.get("type") in {"web", "worker"}:
            if service.get("numInstances") != EXPECTED_NUM_INSTANCES:
                errors.append(
                    f"{name} must pin numInstances={EXPECTED_NUM_INSTANCES}; "
                    "process-local throttles require one Render instance."
                )
        elif "numInstances" in service:
            errors.append(f"{name} must not declare numInstances for a cron job.")
        if service.get("branch") != EXPECTED_BRANCH:
            errors.append(f"{name} must deploy the reviewed {EXPECTED_BRANCH!r} branch.")
        if service.get("autoDeployTrigger") != EXPECTED_AUTO_DEPLOY_TRIGGER:
            errors.append(
                f"{name} must wait for CI with autoDeployTrigger={EXPECTED_AUTO_DEPLOY_TRIGGER!r}."
            )
        if service.get("buildCommand") != EXPECTED_BUILD_COMMAND:
            errors.append(f"{name} must use the reviewed production build command.")
        keys = [str(entry.get("key")) for entry in _env_entries(service)]
        if len(keys) != len(set(keys)):
            errors.append(f"{name} contains duplicate environment-variable keys.")
        for key in sorted(RETIRED_STUDENT_AUTH_ENV_KEYS.intersection(keys)):
            errors.append(f"{name}:{key} is a retired student-auth testing control.")
        if name != WEB_SERVICE_NAME:
            for key in sorted(STUDENT_EMAIL_WEB_ONLY_ENV_KEYS.intersection(keys)):
                errors.append(f"{name}:{key} is web-only student email configuration.")
        if _env_map(service).get("PYTHON_VERSION", {}).get("value") != EXPECTED_PYTHON_VERSION:
            errors.append(
                f"{name}:PYTHON_VERSION must match .python-version at {EXPECTED_PYTHON_VERSION!r}."
            )

    if databases:
        errors.append(
            "The Blueprint must not declare or adopt PostgreSQL; it may only reference "
            f"the existing {EXPECTED_DATABASE_NAME!r} resource."
        )

    for service in services:
        service_name = str(service.get("name") or "<unnamed>")
        env = _env_map(service)
        database_url = env.get("DATABASE_URL")
        expected_database_ref = {
            "name": EXPECTED_DATABASE_NAME,
            "property": "connectionString",
        }
        if database_url is None or database_url.get("fromDatabase") != expected_database_ref:
            errors.append(f"{service_name} DATABASE_URL must reference the one declared database.")

        for entry in _env_entries(service):
            reference = entry.get("fromDatabase")
            if reference is not None and reference != expected_database_ref:
                errors.append(
                    f"{service_name}:{entry.get('key')} targets an undeclared or inconsistent "
                    "database."
                )

    web = services_by_name.get(WEB_SERVICE_NAME, {})
    worker = services_by_name.get(WORKER_SERVICE_NAME, {})
    cron = services_by_name.get(CRON_SERVICE_NAME, {})
    if web:
        web_env = _env_map(web)
        if web.get("healthCheckPath") != EXPECTED_HEALTH_PATH:
            errors.append(f"{WEB_SERVICE_NAME} healthCheckPath must be {EXPECTED_HEALTH_PATH!r}.")
        if web.get("preDeployCommand") != EXPECTED_PREDEPLOY_COMMAND:
            errors.append(f"{WEB_SERVICE_NAME} must use the reviewed preDeployCommand.")
        if web.get("startCommand") != EXPECTED_WEB_START_COMMAND:
            errors.append(
                f"{WEB_SERVICE_NAME} must use one gthread Gunicorn process so throttles "
                "remain authoritative."
            )
        for key, value in sorted(WEB_FIXED_ENV_VALUES.items()):
            if web_env.get(key, {}).get("value") != value:
                errors.append(f"{WEB_SERVICE_NAME}:{key} must be fixed to {value!r}.")
        for key in sorted(WEB_SECRET_KEYS):
            entry = web_env.get(key, {})
            if entry.get("sync") is not False:
                errors.append(f"{WEB_SERVICE_NAME}:{key} must be a server-side secret setting.")
            if any(field in entry for field in ("value", "generateValue", "fromService")):
                errors.append(
                    f"{WEB_SERVICE_NAME}:{key} must not contain or derive a secret value in YAML."
                )
        for key, entry in sorted(web_env.items()):
            if entry.get("sync") is False and key not in WEB_SECRET_KEYS:
                errors.append(
                    f"{WEB_SERVICE_NAME}:{key} is not a secret and must have an explicit "
                    "reviewed value instead of sync:false."
                )
        secret_key = web_env.get("DJANGO_SECRET_KEY", {})
        if secret_key.get("generateValue") is not True:
            errors.append(f"{WEB_SERVICE_NAME}:DJANGO_SECRET_KEY must be generated by Render.")

    if web and worker:
        web_env = _env_map(web)
        worker_env = _env_map(worker)
        for key in sorted(WORKER_INHERITED_ENV_KEYS):
            if key not in web_env:
                errors.append(f"{WEB_SERVICE_NAME} must own worker setting {key}.")
            expected_reference = {
                "name": WEB_SERVICE_NAME,
                "type": "web",
                "envVarKey": key,
            }
            if worker_env.get(key, {}).get("fromService") != expected_reference:
                errors.append(f"{WORKER_SERVICE_NAME}:{key} must inherit from {WEB_SERVICE_NAME}.")

        for key in sorted(WORKER_OPTIONAL_EMPTY_ENV_KEYS):
            if key in worker_env:
                errors.append(
                    f"{WORKER_SERVICE_NAME}:{key} must remain absent so Render does not "
                    "inherit an intentionally empty value."
                )

        if worker_env.get("DJANGO_DEBUG", {}).get("value") != "false":
            errors.append(f"{WORKER_SERVICE_NAME} must explicitly set DJANGO_DEBUG=false.")
        if (
            worker_env.get("PLAYWRIGHT_BROWSERS_PATH", {}).get("value")
            != EXPECTED_PLAYWRIGHT_BROWSERS_PATH
        ):
            errors.append(
                f"{WORKER_SERVICE_NAME}:PLAYWRIGHT_BROWSERS_PATH must be fixed to "
                f"{EXPECTED_PLAYWRIGHT_BROWSERS_PATH!r}."
            )
        if worker_env.get("ALLOW_NO_SMTP_PROCESS", {}).get("value") != "true":
            errors.append(
                f"{WORKER_SERVICE_NAME} must explicitly opt out of web-only email validation."
            )
        if worker.get("startCommand") != EXPECTED_WORKER_START_COMMAND:
            errors.append(
                f"{WORKER_SERVICE_NAME} must enter no-lease standby while Telegram is disabled."
            )

    if cron:
        cron_env = _env_map(cron)
        if cron.get("schedule") != "0 3 * * *":
            errors.append(f"{CRON_SERVICE_NAME} must retain its reviewed daily schedule.")
        if cron.get("startCommand") != EXPECTED_CRON_START_COMMAND:
            errors.append(f"{CRON_SERVICE_NAME} must use the reviewed retention command.")
        if cron_env.get("DJANGO_DEBUG", {}).get("value") != "false":
            errors.append(f"{CRON_SERVICE_NAME} must explicitly set DJANGO_DEBUG=false.")
        if cron_env.get("ALLOW_NO_SMTP_PROCESS", {}).get("value") != "true":
            errors.append(
                f"{CRON_SERVICE_NAME} must explicitly opt out of web-only email validation."
            )
        if any(entry.get("sync") is False for entry in _env_entries(cron)):
            errors.append(f"{CRON_SERVICE_NAME} must not receive independent secret values.")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=PROJECT_ROOT / "render.yaml",
        help="Blueprint path (default: project render.yaml)",
    )
    args = parser.parse_args()
    try:
        document = load_blueprint(args.path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Render Blueprint validation failed: {exc}")
        return 1

    errors = validate_blueprint(document, project_root=args.path.resolve().parent)
    if errors:
        print("Render Blueprint validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Render Blueprint production contract: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Academic defaults through the web API, worker, and one-time migration."""

import json
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from importlib import import_module

import pytest
from django.db import connection
from django.urls import reverse


def _start_worker(test_db, defaults_path):
    import django
    from django.conf import settings
    from django.db import connections

    # This process has its own filesystem settings and uses the parent's test database.
    os.environ["SITE_DEFAULTS_PATH"] = str(defaults_path)
    settings.DATABASES["default"].update(test_db)
    connections["default"].settings_dict.update(test_db)
    django.setup()


def _read_worker_settings():
    from core.services.academic_settings import load_defaults
    from core.services.planner_drafts import planning_term

    return load_defaults(), planning_term()


@pytest.mark.django_db(transaction=True)
def test_running_postgres_worker_observes_web_saves(admin_client, tmp_path):
    if connection.vendor != "postgresql":
        if os.environ.get("REQUIRE_POSTGRES_TESTS") == "1":
            pytest.fail("PostgreSQL was required")
        pytest.skip("Requires independent PostgreSQL sessions")

    url = reverse("settings_defaults")
    with ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_start_worker,
        initargs=(connection.settings_dict, tmp_path / "worker-defaults.json"),
    ) as worker:
        for values in (
            {"academic_year": 1450, "term": 2, "currentYear": 1449, "currentTerm": 0},
            {"academic_year": 1451, "term": 0, "currentYear": 1450, "currentTerm": 3},
        ):
            response = admin_client.post(url, values, content_type="application/json")
            assert response.status_code == 200
            assert admin_client.get(url).json() == values
            assert worker.submit(_read_worker_settings).result(timeout=45) == (
                values,
                (str(values["academic_year"]), str(values["term"])),
            )


@pytest.mark.django_db
def test_settings_permissions_and_omitted_current_term(client, django_user_model):
    from django.contrib.auth.models import Group

    from core.services.rbac import ROLE_GENERAL_ADVISOR

    url = reverse("settings_defaults")
    assert client.get(url).status_code == 302
    user = django_user_model.objects.create_user(username="advisor")
    client.force_login(user)
    initial = client.get(url).json()
    payload = {"academic_year": 1451, "term": 0}
    assert client.post(url, payload, content_type="application/json").status_code == 403
    assert client.get(url).json() == initial

    group, _ = Group.objects.get_or_create(name=ROLE_GENERAL_ADVISOR)
    user.groups.add(group)
    assert client.post(url, payload, content_type="application/json").status_code == 200
    assert client.get(url).json() == {**payload, "currentYear": 1447, "currentTerm": 1}


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "value"),
    [("academic_year", 1399), ("term", 4), ("currentYear", 1601), ("currentTerm", -1)],
)
def test_invalid_setting_preserves_saved_values(admin_client, field, value):
    url = reverse("settings_defaults")
    saved = {"academic_year": 1451, "term": 0, "currentYear": 1450, "currentTerm": 3}
    assert admin_client.post(url, saved, content_type="application/json").status_code == 200
    invalid = {**saved, field: value}
    assert admin_client.post(url, invalid, content_type="application/json").status_code == 400
    assert admin_client.get(url).json() == saved


@pytest.fixture
def empty_settings_migration(db):
    from django.db.migrations.executor import MigrationExecutor

    name = "0082_academic_settings"
    apps = MigrationExecutor(connection).loader.project_state([("core", name)]).apps
    model = apps.get_model("core", "AcademicSettings")
    model.objects.all().delete()
    forward = import_module(f"core.migrations.{name}").seed_academic_settings
    return model, lambda: forward(apps, connection.schema_editor())


@pytest.mark.parametrize("use_export", [False, True], ids=["legacy-file", "deployment-export"])
def test_migration_imports_four_deployment_values(
    empty_settings_migration, tmp_path, monkeypatch, use_export
):
    model, migrate = empty_settings_migration
    saved = {"academic_year": 1452, "term": 0, "currentYear": 1451, "currentTerm": 3}
    path = tmp_path / "site_defaults.json"
    monkeypatch.setenv("SITE_DEFAULTS_PATH", str(path))
    monkeypatch.delenv("ACADEMIC_DEFAULTS_SEED_JSON", raising=False)
    if use_export:
        monkeypatch.setenv("ACADEMIC_DEFAULTS_SEED_JSON", json.dumps(saved))
    else:
        path.write_text(json.dumps(saved), encoding="utf-8")
    migrate()
    assert model.objects.values_list(
        "pk", "academic_year", "term", "current_year", "current_term"
    ).get() == (1, 1452, 0, 1451, 3)


def test_invalid_export_does_not_seed_defaults(empty_settings_migration, monkeypatch):
    model, migrate = empty_settings_migration
    monkeypatch.setenv("ACADEMIC_DEFAULTS_SEED_JSON", '{"academic_year": 1452, "term": 2}')
    with pytest.raises(ValueError):
        migrate()
    assert not model.objects.exists()


def test_render_predeploy_requires_active_settings(empty_settings_migration, monkeypatch):
    model, migrate = empty_settings_migration
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.delenv("ACADEMIC_DEFAULTS_SEED_JSON", raising=False)
    monkeypatch.delenv("SITE_DEFAULTS_PATH", raising=False)
    with pytest.raises(ValueError, match="ACADEMIC_DEFAULTS_SEED_JSON"):
        migrate()
    assert not model.objects.exists()

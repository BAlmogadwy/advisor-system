from __future__ import annotations

import pytest
from django.conf import settings
from django.db import connection
from django.test.utils import CaptureQueriesContext

from config.settings import _configure_sqlite_transaction_options
from core.services.rate_limit import HISTORY, consume


def test_active_writable_sqlite_uses_immediate_transactions_and_busy_timeout():
    database = settings.DATABASES["default"]

    if database["ENGINE"] != "django.db.backends.sqlite3":
        return

    assert database["OPTIONS"]["timeout"] == 30
    assert database["OPTIONS"]["transaction_mode"] == "IMMEDIATE"


def test_sqlite_database_url_shape_receives_the_same_locking_defaults():
    database = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": "C:/tmp/adviser.sqlite3",
        "OPTIONS": {},
    }

    _configure_sqlite_transaction_options(database)

    assert database["OPTIONS"] == {"timeout": 30, "transaction_mode": "IMMEDIATE"}


def test_existing_sqlite_options_are_not_overridden():
    database = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": "C:/tmp/adviser.sqlite3",
        "OPTIONS": {"timeout": 12, "transaction_mode": "EXCLUSIVE"},
    }

    _configure_sqlite_transaction_options(database)

    assert database["OPTIONS"] == {"timeout": 12, "transaction_mode": "EXCLUSIVE"}


def test_read_only_sqlite_snapshot_does_not_start_a_write_transaction():
    database = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": "file:C:/tmp/frozen.sqlite3?mode=ro",
        "OPTIONS": {"uri": True},
    }

    _configure_sqlite_transaction_options(database)

    assert database["OPTIONS"] == {"uri": True, "timeout": 30}


def test_postgresql_configuration_is_unchanged():
    database = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "advisor",
        "OPTIONS": {"sslmode": "require"},
    }

    _configure_sqlite_transaction_options(database)

    assert database["OPTIONS"] == {"sslmode": "require"}


@pytest.mark.django_db(transaction=True)
def test_sqlite_rate_limit_claims_the_writer_before_reading_the_bucket():
    if connection.vendor != "sqlite":
        pytest.skip("SQLite transaction-mode regression")

    with CaptureQueriesContext(connection) as queries:
        assert consume(HISTORY, 9090901).allowed is True

    statements = [query["sql"].strip().upper() for query in queries]
    begin = next(index for index, sql in enumerate(statements) if sql == "BEGIN IMMEDIATE")
    read = next(index for index, sql in enumerate(statements) if "RATE_LIMIT_BUCKETS" in sql)
    assert begin < read, statements

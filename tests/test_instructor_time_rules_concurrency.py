# mypy: disable-error-code="no-untyped-def"

"""PostgreSQL only: two saves of one instructor's teaching-time rules at once.

``instructor_time_rules_set_view`` compares the posted ``expected_revision``
with the stored rules, then replaces them. The ``select_for_update()`` on the
Instructor row is what makes that compare-then-write safe: two editors who
loaded the same revision must not both be answered 200, or the first save is
silently lost. One is saved; the other is refused with 409 ``STALE`` and the
winner's rules.

SQLite erases ``select_for_update()``, so this runs on PostgreSQL only; the
narrow ``planner (PostgreSQL 16)`` CI job runs it with
``REQUIRE_POSTGRES_TESTS=1``, where a skip is a failure.

The interleaving is forced, not hoped for. The first save stops just after it
has read the stored rules (inside its transaction, holding the row lock) until
the second save has read them too, or for three seconds. With the lock, the
second waits for it instead, never reads while the first is open, and then
sees the first's rules. Without it, both read the same empty set and both write.
"""

from __future__ import annotations

import json
import os
import threading

import pytest
from django.contrib.auth.models import Group, User
from django.db import connection, connections
from django.test import Client

from core import instructor_views
from core.models import Instructor, InstructorTimeRule
from core.services.instructor_availability import rules_revision
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups

pytestmark = pytest.mark.django_db(transaction=True)

SET_URL = "/ops/instructors/time-rules/set/"
#: How long the first save holds its transaction open waiting for the second's read.
HOLD_SECONDS = 3
WINDOWS = {"first": ("09:00", "10:15"), "second": ("13:00", "14:15")}
MINUTES = {"first": (540, 615), "second": (780, 855)}


def _require_postgresql() -> None:
    if connection.vendor == "postgresql":
        return
    if os.environ.get("REQUIRE_POSTGRES_TESTS") == "1":
        pytest.fail(
            f"PostgreSQL time-rule tests were required, but Django is using {connection.vendor!r}."
        )
    pytest.skip("Requires PostgreSQL row-lock semantics.")


@pytest.fixture(autouse=True)
def _postgresql_only():
    _require_postgresql()


def _hold_the_first_after_its_read(monkeypatch):
    """Stop the thread named "first" after its first stored-rules read until
    the thread named "second" has read them too (or HOLD_SECONDS pass)."""
    first_in = threading.Event()
    second_read = threading.Event()
    original = instructor_views._stored_rules

    def hooked(instructor):
        rules = original(instructor)
        if threading.current_thread().name == "first":
            if not first_in.is_set():
                first_in.set()
                second_read.wait(timeout=HOLD_SECONDS)
        else:
            second_read.set()
        return rules

    monkeypatch.setattr(instructor_views, "_stored_rules", hooked)
    return first_in


def test_two_editors_holding_one_revision_get_one_save_and_one_stale(monkeypatch):
    instructor = Instructor.objects.create(full_name="Dr Race", normalised_name="dr race")
    revision = rules_revision([])
    ensure_role_groups()
    clients = {}
    for name in WINDOWS:
        user = User.objects.create_user(username=f"itr-race-{name}")
        user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
        clients[name] = Client()
        clients[name].force_login(user)
    first_in = _hold_the_first_after_its_read(monkeypatch)
    outcomes: dict[str, object] = {}
    warmed = threading.Barrier(2)

    def run(name):
        try:
            connection.ensure_connection()
            warmed.wait(timeout=15)
            if name == "second":
                first_in.wait(timeout=15)
            start, end = WINDOWS[name]
            rule = {"kind": "unavailable", "days": ["SUN"], "start": start, "end": end}
            body = {"instructor_id": instructor.pk, "expected_revision": revision, "rules": [rule]}
            outcomes[name] = clients[name].post(
                SET_URL, data=json.dumps(body), content_type="application/json"
            )
        except BaseException as exc:  # noqa: BLE001 - the outcome is asserted below
            outcomes[name] = exc
        finally:
            connections["default"].close()

    threads = [
        threading.Thread(target=run, name=name, args=(name,), daemon=True) for name in WINDOWS
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "a save did not finish"

    statuses = {
        name: getattr(outcome, "status_code", outcome) for name, outcome in outcomes.items()
    }
    assert sorted(statuses.values(), key=str) == [200, 409], statuses
    winner = next(name for name, status in statuses.items() if status == 200)
    loser = next(name for name, status in statuses.items() if status == 409)
    won, lost = outcomes[winner].json(), outcomes[loser].json()
    assert lost["error"]["code"] == "STALE"
    assert lost["error"]["details"]["revision"] == won["revision"]
    assert lost["error"]["details"]["rules"] == won["rules"]
    stored = InstructorTimeRule.objects.filter(instructor=instructor)
    assert list(stored.values_list("kind", "day_mask", "start_minute", "end_minute")) == [
        ("unavailable", 1, *MINUTES[winner])
    ]

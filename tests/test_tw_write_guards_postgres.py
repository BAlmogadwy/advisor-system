# mypy: disable-error-code="no-untyped-def"

"""PostgreSQL only: two creates of one section label at once (stage 3, B1).

``create_planned_section_placements`` checks that a label is free, then
writes. Two creates of one label on two boards, each on its own connection,
must not both pass the check: one is placed, the other is refused with
``SectionLabelInUse``. The section the label names already exists here, with
no placement left: the Redo of an undone create, the one shape where the
unique index on ``(scenario, course_key, section)`` does not serialise the two
(a NEW section's insert does, by itself).

SQLite erases ``select_for_update()``, so this runs on PostgreSQL only; the
narrow ``planner (PostgreSQL 16)`` CI job runs it with
``REQUIRE_POSTGRES_TESTS=1``, where a skip is a failure.

The interleaving is forced, not hoped for. Both connections are opened before
either create starts; the first create stops just after its check (in
``_drop_stale_workspace_meetings``, the next step), holding its transaction
open, until the second create has made its own check, or for three seconds.
With the section's row locked, the second waits for the lock instead, never
checks while the first is open, and then sees the first's placements. Without
the lock, the second checks, finds nothing, and both write.

Two guards hold over HTTP: the manual-write guard's scenario row lock
(``guard_manual_scenario_write``) and the service's row lock. Either alone
serialises a request pair, so the HTTP test below stays green with one of them
removed (defence in depth, as in ``test_planner_draft_concurrency.py``); the
service test is the one that pins the service's own lock.
"""

from __future__ import annotations

import json
import os
import threading

import pytest
from django.contrib.auth.models import Group, User
from django.db import connection, connections
from django.test import Client

from core.models import (
    DeliveryBoard,
    ScenarioSectionBudget,
    SectionPlacement,
    TermSection,
    TimetableScenario,
)
from core.services import timetable_workspace as tws
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups
from core.services.section_source_tags import PLANNED_SECTION_SOURCE_TAG

pytestmark = pytest.mark.django_db(transaction=True)

#: How long the first create holds its transaction open waiting for the second's check.
HOLD_SECONDS = 3


def _require_postgresql() -> None:
    if connection.vendor == "postgresql":
        return
    if os.environ.get("REQUIRE_POSTGRES_TESTS") == "1":
        pytest.fail(
            "PostgreSQL write-guard tests were required, but Django is using "
            f"{connection.vendor!r}."
        )
    pytest.skip("Requires PostgreSQL row-lock semantics.")


@pytest.fixture(autouse=True)
def _postgresql_only():
    _require_postgresql()


def _workspace():
    """Two boards, one 3-credit course, and its S1 section with no placement left."""
    sc = TimetableScenario.objects.create(
        academic_year="1448", term="2", gender="M", programs=["AI"], name="Race"
    )
    term2 = DeliveryBoard.objects.create(scenario=sc, label="Term 2", nominal_term=2, program="AI")
    term4 = DeliveryBoard.objects.create(scenario=sc, label="Term 4", nominal_term=4, program="AI")
    ScenarioSectionBudget.objects.create(
        scenario=sc,
        course_key="AI201",
        course_code="AI201",
        course_name="Data Structures",
        department="AI",
        credit_hours=3,
        planned_sections=3,
        max_per_section=40,
        total_demand=80,
        programme_term=2,
    )
    TermSection.objects.create(
        scenario=sc,
        course_key="AI201",
        course_code="AI201",
        course_number="AI201",
        course_name="Data Structures",
        section="S1",
        source_tag=PLANNED_SECTION_SOURCE_TAG,
    )
    return sc, term2, term4


def _hold_the_first_after_its_check(monkeypatch):
    """Stop the thread named "first" after its check until "second" has checked."""
    first_in = threading.Event()
    second_checked = threading.Event()
    original = tws._drop_stale_workspace_meetings

    def hooked(term_section, scenario_id):
        if threading.current_thread().name == "first":
            first_in.set()
            second_checked.wait(timeout=HOLD_SECONDS)
        else:
            second_checked.set()
        return original(term_section, scenario_id)

    monkeypatch.setattr(tws, "_drop_stale_workspace_meetings", hooked)
    return first_in


def _race(first_in, attempt, first_args, second_args):
    """Run ``attempt`` twice on two connections, the second once the first holds."""
    outcomes: dict[str, object] = {}
    warmed = threading.Barrier(2)

    def run(name, args):
        try:
            connection.ensure_connection()
            warmed.wait(timeout=15)
            if name == "second":
                first_in.wait(timeout=15)
            outcomes[name] = attempt(*args)
        except BaseException as exc:  # noqa: BLE001 - the outcome is asserted below
            outcomes[name] = exc
        finally:
            connections["default"].close()

    threads = [
        threading.Thread(target=run, name="first", args=("first", first_args), daemon=True),
        threading.Thread(target=run, name="second", args=("second", second_args), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "a create did not finish"
    return outcomes


def test_two_service_creates_of_one_label_place_it_once(monkeypatch):
    _sc, term2, term4 = _workspace()
    first_in = _hold_the_first_after_its_check(monkeypatch)

    def attempt(board, day):
        tws.create_planned_section_placements(
            board.id,
            course_code="AI201",
            course_key="AI201",
            course_name="Data Structures",
            section_label="S1",
            capacity=40,
            meetings=[{"day": day, "start_time": "09:00", "end_time": "10:15"}],
        )
        return "placed"

    outcomes = _race(first_in, attempt, (term2, "SUN"), (term4, "MON"))

    assert outcomes["first"] == "placed", outcomes
    assert isinstance(outcomes["second"], tws.SectionLabelInUse), outcomes
    assert (outcomes["second"].label, outcomes["second"].next_free) == ("S1", "S2")
    rows = SectionPlacement.objects.filter(term_section__course_key="AI201")
    assert list(rows.values_list("board_id", "day")) == [(term2.id, "SUN")]


def test_two_requests_for_one_label_answer_201_and_409(monkeypatch):
    _sc, term2, term4 = _workspace()
    first_in = _hold_the_first_after_its_check(monkeypatch)
    ensure_role_groups()
    clients = {}
    for name in ("first", "second"):
        user = User.objects.create_user(username=f"race-{name}")
        user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
        clients[name] = Client()
        clients[name].force_login(user)

    def attempt(name, board, day):
        response = clients[name].post(
            "/ops/tw/placements/create-planned/",
            json.dumps(
                {
                    "board_id": board.id,
                    "course_code": "AI201",
                    "course_key": "AI201",
                    "course_name": "Data Structures",
                    "section_label": "S1",
                    "day": day,
                    "start_time": "09:00",
                    "end_time": "10:15",
                }
            ),
            content_type="application/json",
        )
        return response.status_code, response.json().get("error", {}).get("code")

    outcomes = _race(first_in, attempt, ("first", term2, "SUN"), ("second", term4, "MON"))

    assert outcomes == {"first": (201, None), "second": (409, "SECTION_LABEL_IN_USE")}, outcomes
    rows = SectionPlacement.objects.filter(term_section__course_key="AI201")
    assert list(rows.values_list("board_id", "day")) == [(term2.id, "SUN")]

"""PostgreSQL only: the timetable-feedback rules hold when two writers race.

SQLite runs one writer at a time (BEGIN IMMEDIATE) and ignores
``select_for_update()``, so these races cannot happen there and these tests skip.
The narrow ``planner (PostgreSQL 16)`` CI job runs them with
``REQUIRE_POSTGRES_TESTS=1``, where a skip is a failure.

The interleaving is forced and then PROVEN. Both writers open their connections
before either writes. The first writes inside a transaction and holds it open
until PostgreSQL itself shows the second writer blocked BY THE FIRST
(``pg_blocking_pids``, read live), and only then commits. A run in which the
second writer was never blocked by the first fails, so a test cannot pass
without the race it is named for having happened.
"""

from __future__ import annotations

import os
import threading
import time

import pytest
from django.db import IntegrityError, connection, connections, transaction

from core.models import (
    TimetableFeedback,
    TimetableFeedbackCounter,
    TimetableFeedbackPoint,
    allocate_feedback_reference,
)

pytestmark = pytest.mark.django_db(transaction=True)

SID = 4990201
#: The longest the first writer holds its transaction for the second to queue.
HOLD_SECONDS = 10
#: How often the first writer looks for the second one waiting.
POLL_SECONDS = 0.02
#: The longest anything else here waits.
TIMEOUT = 30


@pytest.fixture(autouse=True)
def _postgresql_only():
    if connection.vendor == "postgresql":
        return
    if os.environ.get("REQUIRE_POSTGRES_TESTS") == "1":
        pytest.fail(
            "PostgreSQL timetable-feedback race tests were required, but Django is using "
            f"{connection.vendor!r}."
        )
    pytest.skip("Requires PostgreSQL row locks and concurrent unique-index checks.")


def _race(first_write, second_write):
    """Run ``second_write`` while ``first_write``'s transaction is still open.

    Returns ``(first, second)``: each writer's return value, or
    ``("refused", message)`` when the database rejected the second writer with an
    IntegrityError, so a test can check WHICH rule refused it. Any other error in
    either writer is re-raised as itself.
    """
    outcomes: dict[str, object] = {}
    warmed = threading.Barrier(2, timeout=TIMEOUT)
    first_written = threading.Event()
    second_done = threading.Event()
    seen = {"first_pid": None, "second_pid": None, "second_waited": False}

    def backend_pid():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            return cursor.fetchone()[0]

    def first():
        try:
            seen["first_pid"] = backend_pid()
            warmed.wait()
            with transaction.atomic():
                outcomes["first"] = first_write()
                first_written.set()
                deadline = time.monotonic() + HOLD_SECONDS
                with connection.cursor() as cursor:
                    while time.monotonic() < deadline and not second_done.is_set():
                        # Blocked by THIS writer, not merely waiting on some lock.
                        cursor.execute(
                            "SELECT %s = ANY(pg_blocking_pids(%s))",
                            [seen["first_pid"], seen["second_pid"]],
                        )
                        if cursor.fetchone()[0]:
                            seen["second_waited"] = True
                            break
                        time.sleep(POLL_SECONDS)
        except BaseException as exc:  # noqa: BLE001 - re-raised by the caller below
            outcomes["first"] = exc
            warmed.abort()  # never leave the other writer waiting at the barrier
        finally:
            first_written.set()  # never leave the second writer waiting on a failure
            connections.close_all()

    def second():
        try:
            seen["second_pid"] = backend_pid()
            warmed.wait()
            assert first_written.wait(TIMEOUT), "the first writer never wrote"
            if isinstance(outcomes.get("first"), BaseException):
                return
            try:
                with transaction.atomic():
                    outcomes["second"] = second_write()
            except IntegrityError as exc:
                outcomes["second"] = ("refused", str(exc))
        except BaseException as exc:  # noqa: BLE001 - re-raised by the caller below
            outcomes["second"] = exc
            warmed.abort()  # never leave the other writer waiting at the barrier
        finally:
            second_done.set()
            connections.close_all()

    threads = [
        threading.Thread(target=first, name="first", daemon=True),
        threading.Thread(target=second, name="second", daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=TIMEOUT * 2)
        assert not thread.is_alive(), f"the {thread.name} writer did not finish"
    errors = [
        outcomes[n] for n in ("first", "second") if isinstance(outcomes.get(n), BaseException)
    ]
    # A writer that failed breaks the barrier for the other; report the cause, not
    # the BrokenBarrierError it left behind.
    causes = [e for e in errors if not isinstance(e, threading.BrokenBarrierError)]
    if causes or errors:
        raise (causes or errors)[0]
    assert seen["second_waited"], (
        "the second writer was never blocked by the first, so nothing raced"
    )
    return outcomes["first"], outcomes["second"]


def _open_case(reference=""):
    return TimetableFeedback.objects.create(
        student_id=SID, academic_year="1448", term="1", reference=reference
    )


def test_two_open_cases_racing_on_the_one_open_case_rule_leave_exactly_one():
    """Distinct case numbers given up front, so the two writers meet only at the
    one-open-case-per-term index: the second waits on it, then is refused by it."""
    first, second = _race(
        lambda: _open_case("TTF-2099-90001"), lambda: _open_case("TTF-2099-90002")
    )

    assert isinstance(first, TimetableFeedback)
    assert second[0] == "refused" and "uq_ttf_one_open_case_per_term" in second[1], second
    assert TimetableFeedback.objects.filter(student_id=SID).count() == 1


def test_two_open_cases_racing_through_case_numbering_leave_exactly_one():
    """The path the service takes: each writer numbers its case first, so the
    second waits on the counter, then meets the first's committed open case."""
    first, second = _race(_open_case, _open_case)

    assert isinstance(first, TimetableFeedback)
    assert second[0] == "refused" and "uq_ttf_one_open_case_per_term" in second[1], second
    assert TimetableFeedback.objects.filter(student_id=SID).count() == 1


@pytest.mark.parametrize("counter_exists", [False, True])
def test_racing_case_numbers_are_never_the_same(counter_exists):
    """Both when the year's counter row is being created by the first writer and
    when it already exists: the second writer waits, then takes the next number."""
    if counter_exists:
        TimetableFeedbackCounter.objects.create(year=2099, last_number=7)
    start = 7 if counter_exists else 0

    first, second = _race(
        lambda: allocate_feedback_reference(year=2099),
        lambda: allocate_feedback_reference(year=2099),
    )

    assert first == f"TTF-2099-{start + 1:05d}"
    assert second == f"TTF-2099-{start + 2:05d}"
    assert TimetableFeedbackCounter.objects.get(year=2099).last_number == start + 2


def test_the_same_point_racing_into_one_case_is_written_once():
    case = _open_case()

    def add_point():
        return TimetableFeedbackPoint.objects.create(
            case=case,
            anchor_kind="TIMETABLE",
            anchor_key="TIMETABLE",
            reason_code="LOAD_TOO_HIGH",
            seen_fingerprint="a" * 64,
        )

    first, second = _race(add_point, add_point)

    assert isinstance(first, TimetableFeedbackPoint)
    assert second[0] == "refused" and "uq_ttfp_point" in second[1], second
    assert case.points.count() == 1

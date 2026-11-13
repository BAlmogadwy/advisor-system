"""PostgreSQL only: the timetable-feedback rules hold when two writers race.

SQLite runs one writer at a time (BEGIN IMMEDIATE) and ignores
``select_for_update()``, so these races cannot happen there and these tests skip.
The narrow ``planner (PostgreSQL 16)`` CI job runs them with
``REQUIRE_POSTGRES_TESTS=1``, where a skip is a failure.

The interleaving is forced, not hoped for. The first writer does its write inside
a transaction and holds that transaction open; only once the second writer is
shown to be BLOCKED behind it (still unfinished after a pause) does the first
commit. A test whose second writer was never blocked fails, so a run that did not
actually race cannot pass.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

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
#: How long the second writer must stay blocked before the first may commit.
BLOCKED_FOR = 0.5
#: The longest anything here waits for anything.
TIMEOUT = 10


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
    """Run ``first_write`` in a held transaction and ``second_write`` against it.

    Returns each writer's outcome: its return value, or ("refused", message) when
    the database rejected it with an IntegrityError, so a test can check WHICH
    rule refused it. Asserts that the second writer was blocked behind the first,
    which is what makes this a race at all.
    """
    first_written, release, second_done = (threading.Event() for _ in range(3))

    def first():
        try:
            with transaction.atomic():
                result = first_write()
                first_written.set()
                assert release.wait(TIMEOUT), "the test never released the first writer"
            return result
        finally:
            connections.close_all()

    def second():
        try:
            assert first_written.wait(TIMEOUT), "the first writer never wrote"
            try:
                with transaction.atomic():
                    return second_write()
            except IntegrityError as exc:
                return ("refused", str(exc))
        finally:
            second_done.set()
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(first)
        second_future = pool.submit(second)
        try:
            assert first_written.wait(TIMEOUT), "the first writer never wrote"
            blocked = not second_done.wait(BLOCKED_FOR)
        finally:
            release.set()
        first_result = first_future.result(timeout=TIMEOUT)
        second_result = second_future.result(timeout=TIMEOUT)
    assert blocked, "the second writer was never blocked, so nothing raced"
    return first_result, second_result


def _open_case():
    return TimetableFeedback.objects.create(student_id=SID, academic_year="1448", term="1")


def test_two_open_cases_racing_for_one_term_leave_exactly_one():
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

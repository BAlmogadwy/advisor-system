"""A seat-limit save commits its audit rows while holding the process audit lock.

``apply_limit_writes`` writes one audit row per changed limit inside one
transaction. Each audit row takes the process audit lock and a row lock on the
chain's last row; inside an outer transaction that row lock lasts until the
outer COMMIT. If the process lock were released between rows, another request
thread could take it and wait on the row lock while the save waits on the
process lock: a hang on PostgreSQL, a 30-second stall and a silently lost audit
row on SQLite. So the save holds the lock from BEGIN to COMMIT.

``transaction=True``: the save must really commit, and another thread on its own
connection must see what was committed.
"""

from __future__ import annotations

import threading
import time

import pytest
from django.contrib.auth.models import AnonymousUser
from django.db import connections, transaction
from django.test import RequestFactory

from core.models import AuditLog, ProgrammeRequirement
from core.services import audit as audit_service
from core.services.audit import log_audit_event, record_audit_event, validate_hash_chain
from core.services.section_limits import LimitChange, apply_limit_writes, plan_limit_writes

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def two_row_save():
    for program, cap in (("AI", 30), ("DS", 35)):
        ProgrammeRequirement.objects.create(
            program=program, course_code="CS211", credit_hours=3, max_capacity=cap
        )
    writes, _ = plan_limit_writes(
        ["AI", "DS"], [LimitChange(course_code="CS211", max_capacity=21, all_programmes=False)]
    )
    assert len(writes) == 2
    return writes


def _audit(write, position, total):
    record_audit_event(
        actor_username="limit-saver",
        actor_role="GENERAL_ADVISOR",
        action="section_planning.limit_change",
        endpoint="/ops/section-planning/limits/",
        method="POST",
        status="success",
        details={**write.as_dict(), "position": position, "of": total},
    )


def _another_thread_can_take_the_audit_lock() -> bool:
    result: list[bool] = []

    def probe():
        took = audit_service._AUDIT_WRITE_LOCK.acquire(blocking=False)
        if took:
            audit_service._AUDIT_WRITE_LOCK.release()
        result.append(took)

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join(5)
    return result[0]


def test_the_save_holds_the_audit_lock_from_its_first_row_until_it_has_committed(
    two_row_save,
) -> None:
    seen: list[tuple[str, bool]] = []

    def audit(write, position, total):
        _audit(write, position, total)
        seen.append((f"after audit row {position}", _another_thread_can_take_the_audit_lock()))
        if position == total:
            transaction.on_commit(
                lambda: seen.append(("at commit", _another_thread_can_take_the_audit_lock()))
            )

    apply_limit_writes(two_row_save, audit=audit)

    assert seen == [
        ("after audit row 1", False),
        ("after audit row 2", False),
        ("at commit", False),
    ]
    assert _another_thread_can_take_the_audit_lock() is True


def test_another_requests_audit_row_waits_for_the_save_and_is_kept(two_row_save) -> None:
    """The interleaving that stalled the save and lost the other request's row."""
    started = threading.Event()
    other_done = threading.Event()

    def other_request():
        try:
            request = RequestFactory().post("/ops/other-endpoint/")
            request.user = AnonymousUser()
            started.set()
            log_audit_event(request, action="probe.other_request", status="success")
        finally:
            other_done.set()
            connections.close_all()

    other = threading.Thread(target=other_request)

    def audit(write, position, total):
        _audit(write, position, total)
        if position == 1:
            other.start()
            assert started.wait(5)
            # Room for the other request to reach the audit write mid-save.
            time.sleep(0.3)

    began = time.monotonic()
    apply_limit_writes(two_row_save, audit=audit)
    elapsed = time.monotonic() - began
    assert other_done.wait(10), "the other request never finished its audit write"
    other.join(5)

    assert elapsed < 5, f"the save stalled for {elapsed:.1f}s"
    assert AuditLog.objects.filter(action="section_planning.limit_change").count() == 2
    assert AuditLog.objects.filter(action="probe.other_request").count() == 1
    assert set(ProgrammeRequirement.objects.values_list("program", "max_capacity")) == {
        ("AI", 21),
        ("DS", 21),
    }
    chain = validate_hash_chain()
    assert chain["ok"], chain
    assert chain["checked"] == 3

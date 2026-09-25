"""The fail-closed audit helper: a row is written, or the caller is told it was not."""

from __future__ import annotations

import pytest
from django.db import OperationalError

from core.models import AuditLog
from core.services import audit
from core.services.audit import AuditUnavailable, record_audit_event, validate_hash_chain

pytestmark = pytest.mark.django_db

EVENT = {
    "actor_username": "exam.committee1",
    "actor_role": "EXAM_COMMITTEE",
    "action": "exam_timetable.export_students",
    "endpoint": "/ops/exam-timetable/1/students/export/",
    "method": "POST",
    "status": "success",
}


def test_strict_event_returns_the_hash_of_the_row_it_chained():
    first = record_audit_event(**EVENT, details={"run_id": 1})
    second = record_audit_event(**EVENT, details={"run_id": 2})
    rows = list(AuditLog.objects.order_by("id"))
    assert [row.entry_hash for row in rows] == [first, second]
    assert rows[1].prev_hash == first and rows[0].prev_hash == "GENESIS"
    assert rows[0].actor_username == "exam.committee1" and rows[0].actor_role == "EXAM_COMMITTEE"
    assert validate_hash_chain()["ok"]


def test_any_write_failure_raises_audit_unavailable(monkeypatch):
    def refuse(**kwargs):
        raise RuntimeError("no table")

    monkeypatch.setattr(AuditLog.objects, "create", refuse)
    with pytest.raises(AuditUnavailable):
        record_audit_event(**EVENT)


def test_a_locked_database_is_retried_once(monkeypatch):
    calls = []
    real = audit._append_audit_row

    def flaky(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("database is locked")
        return real(**kwargs)

    monkeypatch.setattr(audit, "_append_audit_row", flaky)
    entry_hash = record_audit_event(**EVENT)
    assert len(calls) == 2 and AuditLog.objects.get().entry_hash == entry_hash


def test_a_second_lock_is_not_retried_again(monkeypatch):
    calls = []

    def locked(**kwargs):
        calls.append(1)
        raise OperationalError("database is locked")

    monkeypatch.setattr(audit, "_append_audit_row", locked)
    with pytest.raises(AuditUnavailable):
        record_audit_event(**EVENT)
    assert len(calls) == 2


def test_other_operational_errors_are_not_retried(monkeypatch):
    calls = []

    def broken(**kwargs):
        calls.append(1)
        raise OperationalError("no such table: audit_log")

    monkeypatch.setattr(audit, "_append_audit_row", broken)
    with pytest.raises(AuditUnavailable):
        record_audit_event(**EVENT)
    assert len(calls) == 1


def test_unserialisable_details_refuse_rather_than_write_a_partial_row():
    with pytest.raises(AuditUnavailable):
        record_audit_event(**EVENT, details={"bad": object()})
    assert not AuditLog.objects.exists()

import hashlib

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import AuditLog
from core.services.audit import _compute_entry_hash, query_audit_logs, validate_hash_chain
from core.services.rbac import (
    ROLE_GENERAL_ADVISOR,
    ROLE_SUPER_ADMIN,
    ensure_role_groups,
    ensure_scope_schema,
    set_user_scope,
)

pytestmark = pytest.mark.django_db


client = Client()


def _legacy_hash(
    *,
    prev_hash: str,
    ts_utc: str,
    actor_username: str,
    actor_role: str,
    action: str,
    endpoint: str,
    method: str,
    status: str,
    details_json: str = "{}",
    error_text: str = "",
) -> str:
    canonical = "|".join(
        [
            prev_hash,
            ts_utc,
            actor_username,
            actor_role,
            action,
            endpoint,
            method,
            status,
            details_json,
            error_text,
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _login_as(username: str, role: str, *, advisor_id: str = "", departments: str = "") -> User:
    ensure_role_groups()
    ensure_scope_schema()
    user, _ = User.objects.get_or_create(username=username)
    user.groups.clear()
    user.groups.add(Group.objects.get(name=role))
    set_user_scope(user.id, advisor_id=advisor_id, departments=departments)
    client.force_login(user)
    return user


def test_audit_explorer_page_and_csv() -> None:
    _login_as("audit-super", ROLE_SUPER_ADMIN)

    page = client.get("/audit-explorer/")
    assert page.status_code == 200
    assert "Audit Explorer" in page.content.decode("utf-8")

    csv_resp = client.get("/ops/audit/export.csv")
    assert csv_resp.status_code == 200
    assert "text/csv" in csv_resp["Content-Type"]


def test_policy_reason_code_and_hash_chain_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _login_as("audit-general", ROLE_GENERAL_ADVISOR, departments="AI")

    monkeypatch.setattr("core.report_views.build_aggregate_counts", lambda **kwargs: (0, {}))

    resp = client.get("/report/summary/?year=1448&semester=0")
    assert resp.status_code == 400
    body = resp.json()
    assert body.get("reason_code") == "PROGRAM_SCOPE_MISSING_PROGRAM"
    assert body.get("decision") == "deny"

    rows = query_audit_logs(action="policy.program_scope", actor_username="audit-general", limit=20)
    assert rows
    assert rows[0].get("entry_hash")
    assert rows[0].get("prev_hash")


def test_hash_chain_accepts_legacy_prefix_then_hmac_suffix() -> None:
    _login_as("audit-super-legacy", ROLE_SUPER_ADMIN)

    legacy = {
        "ts_utc": "2026-01-01T00:00:00+00:00",
        "actor_username": "legacy",
        "actor_role": "SUPER_ADMIN",
        "action": "legacy.event",
        "endpoint": "/legacy/",
        "method": "GET",
        "status": "success",
        "details_json": "{}",
        "error_text": "",
    }
    AuditLog.objects.create(
        **legacy,
        prev_hash="GENESIS",
        entry_hash=_legacy_hash(prev_hash="GENESIS", **legacy),
    )

    prev_hash = AuditLog.objects.order_by("-id").values_list("entry_hash", flat=True).first()
    assert prev_hash
    hmac_row = {
        "ts_utc": "2026-01-01T00:00:01+00:00",
        "actor_username": "hmac",
        "actor_role": "SUPER_ADMIN",
        "action": "hmac.event",
        "endpoint": "/hmac/",
        "method": "GET",
        "status": "success",
        "details_json": "{}",
        "error_text": "",
    }
    AuditLog.objects.create(
        **hmac_row,
        prev_hash=prev_hash,
        entry_hash=_compute_entry_hash(prev_hash=str(prev_hash), **hmac_row),
    )

    chain = validate_hash_chain(limit=10)
    assert chain["ok"] is True
    assert chain["legacy_count"] == 1
    assert chain["hmac_count"] >= 1


def test_hash_chain_rejects_legacy_hash_after_hmac_entry() -> None:
    _login_as("audit-super-hmac-first", ROLE_SUPER_ADMIN)

    hmac_row = {
        "ts_utc": "2026-01-01T00:00:00+00:00",
        "actor_username": "hmac-first",
        "actor_role": "SUPER_ADMIN",
        "action": "hmac.first",
        "endpoint": "/hmac-first/",
        "method": "GET",
        "status": "success",
        "details_json": "{}",
        "error_text": "",
    }
    AuditLog.objects.create(
        **hmac_row,
        prev_hash="GENESIS",
        entry_hash=_compute_entry_hash(prev_hash="GENESIS", **hmac_row),
    )
    prev_hash = AuditLog.objects.order_by("-id").values_list("entry_hash", flat=True).first()
    assert prev_hash
    legacy = {
        "ts_utc": "2026-01-01T00:00:01+00:00",
        "actor_username": "legacy-after-hmac",
        "actor_role": "SUPER_ADMIN",
        "action": "legacy.after_hmac",
        "endpoint": "/legacy-after-hmac/",
        "method": "GET",
        "status": "success",
        "details_json": "{}",
        "error_text": "",
    }
    AuditLog.objects.create(
        **legacy,
        prev_hash=prev_hash,
        entry_hash=_legacy_hash(prev_hash=str(prev_hash), **legacy),
    )

    chain = validate_hash_chain(limit=10)
    assert chain["ok"] is False
    assert (
        AuditLog.objects.order_by("-id").values_list("id", flat=True).first()
        in chain["invalid_ids"]
    )

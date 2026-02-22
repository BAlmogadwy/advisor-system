import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.services.audit import query_audit_logs
from core.services.rbac import (
    ROLE_GENERAL_ADVISOR,
    ROLE_SUPER_ADMIN,
    ensure_role_groups,
    ensure_scope_schema,
    set_user_scope,
)

pytestmark = pytest.mark.django_db


client = Client()


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


def test_policy_reason_code_and_hash_chain_present(monkeypatch) -> None:
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

import os
from unittest import mock

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client, override_settings

from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_GENERAL_ADVISOR,
    ensure_role_groups,
    ensure_scope_schema,
    get_user_scope,
    set_user_scope,
)

pytestmark = pytest.mark.django_db


client = Client()


def _login_as_advisor(username: str = "dev-switch-user") -> User:
    ensure_role_groups()
    ensure_scope_schema()
    user, _ = User.objects.get_or_create(username=username)
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_ADVISOR))
    set_user_scope(user.id, advisor_id="A001", departments="")
    client.force_login(user)
    return user


@override_settings(DEBUG=True)
@mock.patch.dict(os.environ, {"ALLOW_DEV_ROLE_SWITCH": "true"})
def test_dev_role_switch_changes_scope_in_debug() -> None:
    user = _login_as_advisor()

    resp = client.post(
        "/ops/dev/switch-role/",
        data={"role": ROLE_GENERAL_ADVISOR, "departments": "AI,CS", "advisor_id": ""},
    )
    assert resp.status_code == 200
    user.refresh_from_db()

    scope = get_user_scope(user)
    assert scope.get("role") == ROLE_GENERAL_ADVISOR
    assert set(scope.get("departments", [])) == {"AI", "CS"}


@override_settings(DEBUG=False)
def test_dev_role_switch_blocked_outside_debug() -> None:
    _login_as_advisor("dev-switch-user-2")
    resp = client.post("/ops/dev/switch-role/", data={"role": ROLE_GENERAL_ADVISOR})
    assert resp.status_code == 403


@override_settings(DEBUG=True)
@mock.patch.dict(os.environ, {"ALLOW_DEV_ROLE_SWITCH": ""})
def test_dev_role_switch_blocked_without_env_var() -> None:
    """Even with DEBUG=True, endpoint is blocked unless ALLOW_DEV_ROLE_SWITCH=true."""
    _login_as_advisor("dev-switch-user-3")
    resp = client.post("/ops/dev/switch-role/", data={"role": ROLE_GENERAL_ADVISOR})
    assert resp.status_code == 403

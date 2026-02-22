import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_GENERAL_ADVISOR,
    ROLE_SUPER_ADMIN,
    ensure_role_groups,
    ensure_scope_schema,
    get_user_scope,
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


def test_super_admin_user_management_crud() -> None:
    _login_as("super-admin-ops", ROLE_SUPER_ADMIN)

    create = client.post(
        "/ops/users/create/",
        data='{"username":"qa_user","password":"Pass@12345","role":"ADVISOR","advisor_id":"A009","departments":""}',
        content_type="application/json",
    )
    assert create.status_code == 200

    listing = client.get("/ops/users/list/")
    assert listing.status_code == 200
    items = listing.json().get("items", [])
    assert any(i.get("username") == "qa_user" and i.get("role") == ROLE_ADVISOR for i in items)

    update = client.post(
        "/ops/users/update-role/",
        data='{"username":"qa_user","role":"GENERAL_ACADEMIC_ADVISOR","departments":"AI,CS","advisor_id":""}',
        content_type="application/json",
    )
    assert update.status_code == 200

    target = User.objects.get(username="qa_user")
    scope = get_user_scope(target)
    assert scope.get("role") == ROLE_GENERAL_ADVISOR
    assert set(scope.get("departments", [])) == {"AI", "CS"}

    reset_pw = client.post(
        "/ops/users/set-password/",
        data='{"username":"qa_user","new_password":"NewPass@123"}',
        content_type="application/json",
    )
    assert reset_pw.status_code == 200

    disable = client.post(
        "/ops/users/set-active/",
        data='{"username":"qa_user","is_active":false}',
        content_type="application/json",
    )
    assert disable.status_code == 200
    assert User.objects.get(username="qa_user").is_active is False

    enable = client.post(
        "/ops/users/set-active/",
        data='{"username":"qa_user","is_active":true}',
        content_type="application/json",
    )
    assert enable.status_code == 200
    assert User.objects.get(username="qa_user").is_active is True

    delete = client.post(
        "/ops/users/delete/",
        data='{"username":"qa_user"}',
        content_type="application/json",
    )
    assert delete.status_code == 200
    assert not User.objects.filter(username="qa_user").exists()


def test_non_super_admin_forbidden() -> None:
    _login_as("advisor-no-admin", ROLE_ADVISOR, advisor_id="A001")
    resp = client.get("/ops/users/list/")
    assert resp.status_code == 403

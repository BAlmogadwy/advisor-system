import pytest
from django.contrib.auth.models import User
from django.test.client import Client

from core.models import Student
from core.services.rbac import (
    ROLE_SUPER_ADMIN,
    ensure_role_groups,
    ensure_scope_schema,
    set_user_scope,
)

pytestmark = pytest.mark.django_db


def _login_as_admin(client: Client) -> None:
    """Create a SUPER_ADMIN user so scope checks pass."""
    ensure_role_groups()
    ensure_scope_schema()
    user, _ = User.objects.get_or_create(username="test-user")
    from django.contrib.auth.models import Group

    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    set_user_scope(user.id, advisor_id="", departments="")
    client.force_login(user)


def _ensure_student() -> None:
    """Create a minimal Student record for the test student_id."""
    Student.objects.get_or_create(
        student_id=12345,
        defaults={"name": "Test Student", "program": "CS", "status": "active"},
    )


def test_recommend_endpoint_requires_params(client: Client) -> None:
    _login_as_admin(client)
    _ensure_student()
    response = client.get("/recommend/12345/")
    assert response.status_code == 400
    assert "error" in response.json()


def test_recommend_endpoint_valid_request(client: Client) -> None:
    _login_as_admin(client)
    _ensure_student()
    response = client.get("/recommend/12345/?year=1448&semester=0")
    assert response.status_code == 200
    body = response.json()
    assert body["student_id"] == 12345
    assert body["current_academic_year"] == 1448
    assert body["current_semester"] == 0
    assert isinstance(body["recommendations"], list)
    assert body["count"] == len(body["recommendations"])


def test_recommend_endpoint_requires_auth(client: Client) -> None:
    """Unauthenticated requests should redirect to login."""
    response = client.get("/recommend/12345/")
    assert response.status_code == 302

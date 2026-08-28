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


def test_recommend_endpoint_valid_request_defaults_to_strict(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login_as_admin(client)
    _ensure_student()
    received: dict[str, object] = {}

    def fake_recommend(
        student_id: int,
        current_academic_year: int,
        current_semester: int,
        *,
        strict_passed_only: bool,
    ) -> list[str]:
        received.update(
            student_id=student_id,
            year=current_academic_year,
            semester=current_semester,
            strict_passed_only=strict_passed_only,
        )
        return []

    monkeypatch.setattr("core.api_views.recommend_next_courses", fake_recommend)
    response = client.get("/recommend/12345/?year=1448&semester=0")
    assert response.status_code == 200
    body = response.json()
    assert body["student_id"] == 12345
    assert body["current_academic_year"] == 1448
    assert body["current_semester"] == 0
    assert body["mode"] == "strict"
    assert received["strict_passed_only"] is True
    assert isinstance(body["recommendations"], list)
    assert body["count"] == len(body["recommendations"])


def test_recommend_endpoint_relaxed_mode_is_explicit_opt_in(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login_as_admin(client)
    _ensure_student()
    received: dict[str, object] = {}

    def fake_recommend(
        student_id: int,
        current_academic_year: int,
        current_semester: int,
        *,
        strict_passed_only: bool,
    ) -> list[str]:
        received["strict_passed_only"] = strict_passed_only
        return ["CS101"]

    monkeypatch.setattr("core.api_views.recommend_next_courses", fake_recommend)
    response = client.get("/recommend/12345/?year=1448&semester=1&mode=relaxed")

    assert response.status_code == 200
    assert response.json()["mode"] == "relaxed"
    assert response.json()["recommendations"] == ["CS101"]
    assert received["strict_passed_only"] is False


@pytest.mark.parametrize(
    ("mode_query", "expected_mode", "expected_strict"),
    [
        ("", "strict", True),
        ("&mode=relaxed", "relaxed", False),
        ("&mode=unknown", "strict", True),
    ],
)
def test_dashboard_mode_defaults_strict_and_csv_link_preserves_it(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
    mode_query: str,
    expected_mode: str,
    expected_strict: bool,
) -> None:
    _login_as_admin(client)
    _ensure_student()
    received: dict[str, object] = {}

    def fake_recommend(
        student_id: int,
        current_academic_year: int,
        current_semester: int,
        *,
        strict_passed_only: bool,
    ) -> list[str]:
        received["strict_passed_only"] = strict_passed_only
        return ["CS101"]

    monkeypatch.setattr("core.views.recommend_next_courses", fake_recommend)
    response = client.get(f"/?student_id=12345&year=1448&semester=1{mode_query}")

    assert response.status_code == 200
    assert response.context["recommendation_mode"] == expected_mode
    assert received["strict_passed_only"] is expected_strict
    html = response.content.decode("utf-8")
    assert f'value="{expected_mode}" selected' in html
    assert f"mode={expected_mode}" in html


def test_recommend_endpoint_requires_auth(client: Client) -> None:
    """Unauthenticated requests should redirect to login."""
    response = client.get("/recommend/12345/")
    assert response.status_code == 302

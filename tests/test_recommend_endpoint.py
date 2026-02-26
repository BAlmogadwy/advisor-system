import pytest
from django.contrib.auth.models import User
from django.test.client import Client

pytestmark = pytest.mark.django_db


def _login(client: Client) -> None:
    user, _ = User.objects.get_or_create(username="test-user")
    client.force_login(user)


def test_recommend_endpoint_requires_params(client: Client) -> None:
    _login(client)
    response = client.get("/recommend/12345/")
    assert response.status_code == 400
    assert "error" in response.json()


def test_recommend_endpoint_valid_request(client: Client) -> None:
    _login(client)
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

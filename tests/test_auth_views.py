import pytest
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_staff_login_form_is_never_cached(client: Client) -> None:
    response = client.get(reverse("login"))

    assert response.status_code == 200
    cache_control = response.headers.get("Cache-Control", "")
    assert "no-store" in cache_control
    assert "no-cache" in cache_control
    assert "must-revalidate" in cache_control
    assert "private" in cache_control

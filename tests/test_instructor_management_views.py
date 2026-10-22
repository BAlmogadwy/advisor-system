"""Instructor management: what the Add/Edit form sends is stored as sent.

An empty field arrives as JSON null and is stored empty, never as the text
"None"; the form's Active box is honoured on create and on update."""

from __future__ import annotations

import json

import pytest

from core.models import Instructor


def _client():
    from django.contrib.auth.models import Group, User
    from django.test import Client

    from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups

    ensure_role_groups()
    user, _ = User.objects.get_or_create(username="im-admin")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client()
    client.force_login(user)
    return client


def _post(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


@pytest.mark.django_db
def test_create_stores_null_fields_empty_and_honours_active():
    client = _client()
    response = _post(
        client,
        "/ops/instructors/create/",
        {
            "full_name": "Dr Hala Omar",
            "full_name_ar": None,
            "email": None,
            "employee_no": None,
            "department": None,
            "max_weekly_hours": None,
            "is_active": False,
        },
    )
    assert response.status_code == 201
    row = Instructor.objects.get(full_name="Dr Hala Omar")
    assert (row.full_name_ar, row.email, row.employee_no, row.department) == ("", "", "", "")
    assert row.max_weekly_hours is None
    assert row.is_active is False
    # Omitted, a new instructor is active.
    assert (
        _post(client, "/ops/instructors/create/", {"full_name": "Dr Sami Ali"}).status_code == 201
    )
    assert Instructor.objects.get(full_name="Dr Sami Ali").is_active is True


@pytest.mark.django_db
def test_update_clears_a_field_to_empty_and_changes_active():
    client = _client()
    response = _post(
        client,
        "/ops/instructors/create/",
        {"full_name": "Dr Lina Saad", "email": "lina@example.edu", "department": "CS"},
    )
    pk = response.json()["instructor"]["id"]
    response = _post(
        client,
        "/ops/instructors/update/",
        {
            "id": pk,
            "full_name": "Dr Lina Saad",
            "email": None,
            "department": "AI",
            "is_active": False,
        },
    )
    assert response.status_code == 200
    row = Instructor.objects.get(pk=pk)
    assert (row.email, row.department, row.is_active) == ("", "AI", False)
    # Not sent, the flag is left as it is.
    assert (
        _post(client, "/ops/instructors/update/", {"id": pk, "department": "DS"}).status_code == 200
    )
    row.refresh_from_db()
    assert (row.department, row.is_active) == ("DS", False)
    assert (
        _post(client, "/ops/instructors/update/", {"id": pk, "is_active": True}).status_code == 200
    )
    row.refresh_from_db()
    assert row.is_active is True


@pytest.mark.django_db
def test_an_update_audits_what_changed_and_the_active_flag(monkeypatch):
    from core import instructor_views

    seen = []
    monkeypatch.setattr(
        instructor_views,
        "log_audit_event",
        lambda request, **kw: seen.append((kw["action"], kw["details"])),
    )
    client = _client()
    pk = _post(client, "/ops/instructors/create/", {"full_name": "Dr Audit"}).json()["instructor"][
        "id"
    ]
    _post(client, "/ops/instructors/update/", {"id": pk, "department": "AI", "is_active": False})
    assert seen[-1] == (
        "instructor_update",
        {"id": pk, "fields": ["department", "is_active"], "is_active": False},
    )
    _post(client, "/ops/instructors/update/", {"id": pk, "department": "DS"})
    assert seen[-1] == ("instructor_update", {"id": pk, "fields": ["department"]})

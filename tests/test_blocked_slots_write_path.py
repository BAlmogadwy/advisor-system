"""WS-D — registrar write-path for scenario blocked slots.

Blocked slots are enforced by construction across every placement stage and are
a publish blocker; this endpoint is how a registrar actually sets them.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import TimetableScenario
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups

pytestmark = pytest.mark.django_db

client = Client()


def _login() -> None:
    ensure_role_groups()
    user, _ = User.objects.get_or_create(username="tw-blk-admin")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client.force_login(user)


def _post(scenario_id: int, body: dict):
    return client.post(
        f"/ops/tw/scenarios/{scenario_id}/blocked-slots/",
        data=json.dumps(body),
        content_type="application/json",
    )


def test_blocked_slots_toggle_add_then_remove() -> None:
    _login()
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="blk")

    # Add
    resp = _post(scenario.id, {"day": "SUN", "start": "09:00"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["blocked"] is True
    assert {"day": "SUN", "start": "09:00"} in payload["blocked_slots"]
    scenario.refresh_from_db()
    assert {"day": "SUN", "start": "09:00"} in scenario.blocked_slots

    # Toggle off
    resp2 = _post(scenario.id, {"day": "SUN", "start": "09:00"})
    assert resp2.status_code == 200
    assert resp2.json()["blocked"] is False
    scenario.refresh_from_db()
    assert scenario.blocked_slots == []


def test_blocked_slots_requires_day_and_start() -> None:
    _login()
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="blk2")
    resp = _post(scenario.id, {"day": "SUN"})
    assert resp.status_code == 400


def test_blocked_slots_rejects_published_scenario() -> None:
    _login()
    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="blk3", status="published"
    )
    resp = _post(scenario.id, {"day": "SUN", "start": "09:00"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "SCENARIO_PUBLISHED"

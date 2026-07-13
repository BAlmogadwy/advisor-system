"""The slot-config save endpoint rejects a grid that violates the prayer windows.

Prayer compliance is a grid-construction invariant: the default grid never
starts a lecture in 11:30-12:59 or a lab in 11:10-12:59, and the placement
stages assume it (the runtime per-meeting prayer check was dropped on that
basis). A hand-edited custom grid must be validated at the save boundary rather
than silently producing non-compliant boards.
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
    user, _ = User.objects.get_or_create(username="tw-prayer-admin")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client.force_login(user)


def _post(scenario_id: int, body: dict):
    return client.post(
        f"/ops/tw/scenarios/{scenario_id}/slots/update/",
        data=json.dumps(body),
        content_type="application/json",
    )


def test_compliant_grid_is_accepted() -> None:
    _login()
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="ok grid")
    resp = _post(
        scenario.id,
        {
            "slot_config": [
                {"start": "09:00", "end": "10:15"},
                {"start": "10:30", "end": "11:45"},
                {"start": "13:00", "end": "14:15"},
            ]
        },
    )
    assert resp.status_code == 200
    scenario.refresh_from_db()
    assert len(scenario.slot_config) == 3


def test_lecture_slot_starting_in_prayer_window_is_rejected() -> None:
    _login()
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="bad grid")
    resp = _post(
        scenario.id,
        {
            "slot_config": [
                {"start": "09:00", "end": "10:15"},
                {"start": "12:00", "end": "13:15"},  # starts inside 11:30-12:59
            ]
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_PRAYER"
    # The bad grid was NOT persisted.
    scenario.refresh_from_db()
    assert not scenario.slot_config


def test_lab_slot_starting_in_prayer_window_is_rejected() -> None:
    _login()
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="bad lab grid")
    resp = _post(
        scenario.id,
        {
            "slot_config": [{"start": "09:00", "end": "10:15"}],
            "lab_slot_config": [{"start": "11:30", "end": "13:10"}],  # in 11:10-12:59
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_PRAYER"

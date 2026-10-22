"""Record the legacy quality and plan-lens JSON that ``test_tw_scenario_seating`` replays.

Recorded at ``b376afa``, before sub-PR 6a let one seating serve quality and the
lens: the oracle must describe the code before the change, or "the read without
``lens`` is unchanged" proves nothing. Never re-record it on a branch that
changes either read::

    TW_QUALITY_LEGACY_RECORD=<file> python -m pytest -p no:cacheprovider \\
        tests/record_tw_quality_legacy.py

Not collected by default (no ``test_`` prefix); pytest runs it when named.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.services.rbac import ROLE_SUPER_ADMIN
from tests.tw_scenario_seating_support import (
    FLAG_MODES,
    apply_flags,
    build_seating_fixture,
    record_legacy_reads,
)


@pytest.mark.django_db
def test_record_quality_oracle(settings) -> None:
    target = os.environ.get("TW_QUALITY_LEGACY_RECORD")
    if not target:
        pytest.fail("Set TW_QUALITY_LEGACY_RECORD to the file to write.", pytrace=False)
    user = User.objects.create_user(username="quality-oracle")
    user.groups.add(Group.objects.get_or_create(name=ROLE_SUPER_ADMIN)[0])
    client = Client()
    client.force_login(user)
    fx = build_seating_fixture()
    produced: dict[str, object] = {}
    for mode in FLAG_MODES:
        apply_flags(settings, mode)
        produced[mode] = record_legacy_reads(client, fx)
    Path(target).write_text(json.dumps(produced, ensure_ascii=False) + "\n", encoding="utf-8")

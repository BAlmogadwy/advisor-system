"""Record the legacy slot-preview JSON that ``test_tw_slot_verdict`` (V10, V13) replays.

Recorded at ``a5d8f96``, before sub-PR 5d added the verdict: the oracle must
describe the code before the change, or "the legacy output is unchanged"
proves nothing. Never re-record it on a branch that changes the previews::

    TW_SLOT_VERDICT_RECORD=<file> python -m pytest -p no:cacheprovider \\
        tests/record_tw_slot_verdict_legacy.py

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
from tests.one_teacher_support import FLAG_ON
from tests.tw_slot_verdict_support import build_rich_fixture, record_legacy_previews


@pytest.mark.django_db
def test_record_slot_preview_oracle(settings) -> None:
    target = os.environ.get("TW_SLOT_VERDICT_RECORD")
    if not target:
        pytest.fail("Set TW_SLOT_VERDICT_RECORD to the file to write.", pytrace=False)
    for name, value in FLAG_ON.items():
        setattr(settings, name, value)
    user = User.objects.create_user(username="slot-verdict-oracle")
    user.groups.add(Group.objects.get_or_create(name=ROLE_SUPER_ADMIN)[0])
    client = Client()
    client.force_login(user)
    rich = build_rich_fixture()
    produced: dict[str, object] = {}
    for mode, one_teacher in (("one_teacher_off", False), ("one_teacher_on", True)):
        settings.TIMETABLE_ONE_TEACHER_PER_SECTION = one_teacher
        produced[mode] = record_legacy_previews(client, rich)
    Path(target).write_text(json.dumps(produced, ensure_ascii=False) + "\n", encoding="utf-8")

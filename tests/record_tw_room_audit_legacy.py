"""Record the room-candidates oracle that ``test_tw_room_audit`` replays.

Run it only at ``a5d8f96`` (the base the room-audit PR starts from), never on
a branch that changes room candidates or the quality room audit: the oracle
must describe the code before the change, or the parity tests prove nothing::

    TW_ROOM_AUDIT_RECORD=<file> python -m pytest -p no:cacheprovider \\
        tests/record_tw_room_audit_legacy.py

then diff ``<file>`` against ``tests/fixtures/tw_room_audit_legacy.json``.
Every id is replaced by its fixture symbol (``tw_room_audit_support.symbols``),
so the file does not depend on autoincrement values. Not collected by default
(no ``test_`` prefix); pytest runs it when named.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
from pathlib import Path

import pytest

from core.services.timetable_workspace import preview_placement_room_candidates
from tests.tw_room_audit_support import admin_client, build_fixture, run_legacy_corpus


@pytest.mark.django_db
def test_record_room_audit_legacy(settings) -> None:
    target = os.environ.get("TW_ROOM_AUDIT_RECORD")
    if not target:
        pytest.fail("Set TW_ROOM_AUDIT_RECORD to the file to write.", pytrace=False)
    # The answers must be the base's: a checkout that already has the audit
    # flag (or the shared rule module) would record its own.
    if "include_audit" in inspect.signature(preview_placement_room_candidates).parameters:
        pytest.fail("This checkout already has include_audit; record from a5d8f96.", pytrace=False)
    if importlib.util.find_spec("core.services.timetable_room_audit") is not None:
        pytest.fail("This checkout already has timetable_room_audit; record from a5d8f96.")
    scenario = build_fixture()
    produced = run_legacy_corpus(admin_client(), settings, scenario)
    Path(target).write_text(
        json.dumps(produced, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )

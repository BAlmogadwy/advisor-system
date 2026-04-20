"""PR4 — dead counter removal tests (commit 7).

``lecture_room_reject_due_to_buffer_count`` was replaced functionally by
``buffer_only_rejects`` during PR2. The old counter is retained only by
4 legacy test assertions (3 in ``test_timetable_capacity_buffer.py``,
1 in ``test_pr2_silent_unassigned_sites.py``). Commit 7 removes the
counter from the planner payload, the summation path, and the per-run
log line; the 4 legacy assertions are rewritten to use
``buffer_only_rejects`` instead.

These tests fail at commit 1 (the counter still exists), pass from
commit 7 onward.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from django.test import TransactionTestCase
from pr4_fixture_loader import load_pr4_fixture


@pytest.mark.django_db
class TestCounterRemoved(TransactionTestCase):
    """Bar: the dead counter is gone from both payload and source.

    Using ``pr4_instructor_clash.json`` as a generic planner fixture —
    any fixture that produces a return dict would do; the assertion is
    about the dict's key-set, not the fixture's semantics."""

    def test_counter_absent_from_payload(self) -> None:
        from core.services.timetable_autoplace import auto_place_board

        _, board, _ = load_pr4_fixture("pr4_instructor_clash.json")
        result = auto_place_board(board.id)

        assert "lecture_room_reject_due_to_buffer_count" not in result, (
            "Payload still carries the dead counter. Commit 7 must remove it."
        )
        assert "buffer_only_rejects" in result, (
            "Payload must still carry the successor counter buffer_only_rejects."
        )

    def test_counter_absent_from_source(self) -> None:
        """No remaining reference to the legacy counter name in the planner
        module. Using source-text check rather than grep because A1 applies
        in spirit here — we prefer a concrete assertion over a grep gate."""
        from core.services import timetable_autoplace

        src = Path(importlib.util.find_spec(timetable_autoplace.__name__).origin).read_text(
            encoding="utf-8"
        )
        assert "lecture_room_reject_due_to_buffer_count" not in src, (
            "Source still references the legacy counter. Commit 7 must remove it."
        )

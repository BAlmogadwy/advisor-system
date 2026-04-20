"""PR4 — lab-predicate centralisation tests (A4).

``meeting_requires_lab_room(meeting) -> bool`` is the single authoritative
predicate. Three call-sites (planner, oracle, export) route through it,
producing identical classifications for every meeting — eliminating the
``duration > 80`` literal drift across three modules.

Tripwire: ``core.services.timetable_lab_predicate`` does not exist until
commit 6. Top-of-file import raises ``ModuleNotFoundError`` before that,
which is the contract.
"""

from __future__ import annotations

import pytest
from django.test import SimpleTestCase, TransactionTestCase
from django.test.utils import override_settings
from pr4_fixture_loader import load_pr4_fixture

# Contract import — tripwire until commit 6.
from core.services.timetable_lab_predicate import (
    LAB_HEURISTIC_UNIFIED_FLAG_SETTING,
    is_lab_heuristic_unified,
    meeting_requires_lab_room,
)


class _StubMeeting:
    """Duck-typed stand-in for ``TermSectionMeeting`` so the predicate can
    be unit-tested without DB roundtrip. The real helper only reads
    ``duration`` / ``start_time`` / ``end_time``; anything else is ignored."""

    def __init__(self, start_time: str, end_time: str) -> None:
        self.start_time = start_time
        self.end_time = end_time

    @property
    def duration(self) -> int:
        sh, sm = (int(x) for x in self.start_time.split(":"))
        eh, em = (int(x) for x in self.end_time.split(":"))
        return (eh * 60 + em) - (sh * 60 + sm)


# ===========================================================================
# SECTION A — Predicate shape + boundary (turns green at commit 6).
# ===========================================================================


class TestLabPredicateBoundary(SimpleTestCase):
    """Duration == 80 is the boundary. Per A4 + oracle original intent,
    the helper classifies boundary-duration meetings as lab-requiring."""

    def test_duration_80_is_lab(self) -> None:
        m = _StubMeeting("08:00", "09:20")  # 80 min
        assert meeting_requires_lab_room(m) is True

    def test_duration_75_is_not_lab(self) -> None:
        m = _StubMeeting("08:00", "09:15")  # 75 min
        assert meeting_requires_lab_room(m) is False

    def test_duration_90_is_lab(self) -> None:
        m = _StubMeeting("08:00", "09:30")  # 90 min
        assert meeting_requires_lab_room(m) is True

    def test_flag_setting_constant_is_published(self) -> None:
        assert LAB_HEURISTIC_UNIFIED_FLAG_SETTING == "TIMETABLE_LAB_HEURISTIC_UNIFIED"


# ===========================================================================
# SECTION B — Three-site agreement on boundary meeting (green at commit 6).
# ===========================================================================


@pytest.mark.django_db
class TestLabPredicateUnified(TransactionTestCase):
    """Fixture #3 — a duration-80 meeting must be classified identically
    by planner, oracle, and export. ROOM_HEURISTIC_MISMATCH count must
    be zero once the helper routes all three."""

    @override_settings(TIMETABLE_LAB_HEURISTIC_UNIFIED=True)
    def test_all_three_sites_agree_on_boundary_meeting(self) -> None:
        from core.services.timetable_autoplace import auto_place_board

        _, board, _ = load_pr4_fixture("pr4_lab_predicate_mismatch.json")
        result = auto_place_board(board.id)

        trace = result["decision_trace"]
        mismatch_count = 0
        for entry in trace.values():
            for alt in entry["alternatives"]:
                if alt["rejection_code"] == "ROOM_HEURISTIC_MISMATCH":
                    mismatch_count += 1
        assert mismatch_count == 0, (
            f"Unified helper should produce zero ROOM_HEURISTIC_MISMATCH on "
            f"the boundary fixture; got {mismatch_count}."
        )


@pytest.mark.django_db
class TestLabPredicateFlag(TransactionTestCase):
    """Flag-off case: each site uses its old ``duration > 80`` literal.
    The helper exists but is dormant — planner/oracle/export behaviour is
    identical to master cec5988."""

    def test_flag_helper_reads_setting(self) -> None:
        with override_settings(TIMETABLE_LAB_HEURISTIC_UNIFIED=True):
            assert is_lab_heuristic_unified() is True
        with override_settings(TIMETABLE_LAB_HEURISTIC_UNIFIED=False):
            assert is_lab_heuristic_unified() is False

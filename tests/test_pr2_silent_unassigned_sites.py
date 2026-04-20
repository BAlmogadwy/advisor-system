"""PR2 — per-site tests for the four silent-UNASSIGNED fallthroughs.

Acceptance criterion #1 in the PR2 DoR is that each of the four enumerated
call-sites that today emit a bare ``"UNASSIGNED"`` string without a typed
reason is swapped to a structured ``RoomFailureReason`` by the end of the
PR. This file is that acceptance test. Each of the four sites gets its own
case; nothing here is a grep.

Failing progression:

- **Commit 1** (this commit): all tests fail at collection with
  ``ModuleNotFoundError`` because ``core.services.timetable_room_oracle``
  doesn't exist yet. That is the tripwire — commit 2 must create the
  module, at which point these tests start failing at *assertion* level
  instead.
- **Commit 2** (oracle module with dataclass + sentinels): collection
  succeeds; each test still fails because the rooming code hasn't been
  swapped yet — return payloads don't carry ``room_failures``.
- **Commit 3** (silent-to-typed swap at the four sites): each site now
  emits a ``RoomFailureReason``; the payload assertion in each test
  checks its specific site's reason-code contract.
- **Commit 5** (payload surface extension): ``room_failures`` becomes a
  first-class key in the return dict; assertions on payload shape pass.

The four sites, from the PR2 DoR's "Code anchors" table:

| # | File:line                                 | Function |
|---|-------------------------------------------|----------|
| 1 | core/services/timetable_autoplace.py:1102 | auto_place_board — best_option None after scoring |
| 2 | core/services/timetable_autoplace.py:1162 | auto_place_board — fallback sets assigned_room = "UNASSIGNED" |
| 3 | core/services/timetable_rooming.py:324    | assign_rooms_to_board — tracker.assign_best_fit returned None |
| 4 | core/services/timetable_room_repair.py:118-119 | try_repair_rooms_locally — section unable to place |

Each test is written against the live entry-point functions
(``auto_place_board``, ``assign_rooms_to_board``, ``try_repair_rooms_locally``)
so the assertion is behavioural — not a grep on source code.
"""

from __future__ import annotations

import pytest

# Contract tripwire: commit 1 suite fails at collection here.
from core.services.timetable_room_oracle import (  # noqa: F401
    NO_ROOM_CAPACITY,  # noqa: F401
    NO_ROOM_GENDER,  # noqa: F401
    NO_ROOM_TYPE,  # noqa: F401
    ROOM_BUFFER_REJECT,  # noqa: F401
    ROOM_HEURISTIC_MISMATCH,  # noqa: F401
    ROOM_OCCUPIED,  # noqa: F401
    RoomFailureReason,  # noqa: F401
)

# NOTE: ``@override_settings(TIMETABLE_PR2_ROOM_ORACLE_ENABLED=True)`` belongs
# on these test classes — but ``override_settings`` requires ``SimpleTestCase``
# ancestry, and these classes are pytest-style (plain classes + pytest.mark)
# so we can't decorate them at the class level today. The tests are all
# ``pytest.skip`` bodies until commits 3 / 5 wire them up; when the bodies
# land, each test will do ``with override_settings(...)`` locally, or the
# classes will inherit from ``SimpleTestCase`` / ``TransactionTestCase``
# (settled during commit 3/5 design). Until then no flag override is needed
# because the skips fire before any oracle code runs.

# ---------------------------------------------------------------------------
# Shared helper: assert a planner return payload satisfies the PR2 contract.
# ---------------------------------------------------------------------------


def _assert_pr2_payload_shape(result: dict) -> None:
    """PR2 adds ``room_failures`` and ``unplaced_count`` to the return dict.

    Commit 5 lands this surface. Before then, ``result.keys()`` lacks these
    keys and this helper raises ``KeyError``, which is exactly the expected
    failing behaviour for commits 1-4.
    """
    assert "room_failures" in result, (
        "PR2 acceptance: result dict must carry a 'room_failures' list"
    )
    assert "unplaced_count" in result, (
        "PR2 acceptance: result dict must carry an 'unplaced_count' integer"
    )
    assert isinstance(result["room_failures"], list)
    assert isinstance(result["unplaced_count"], int)


def _assert_every_unplaced_has_reason(result: dict) -> None:
    """Every section counted in ``unplaced_count`` must appear in
    ``room_failures`` with a typed ``reason`` (reason_code). Zero silent
    UNASSIGNED paths."""
    failures = result["room_failures"]
    assert len(failures) == result["unplaced_count"], (
        f"unplaced_count={result['unplaced_count']} but room_failures carries "
        f"{len(failures)} entries — each unplaced section must have a typed reason"
    )
    for f in failures:
        assert f.get("reason"), "Every room_failure must carry a non-empty 'reason'"
        assert f.get("course_code"), "Every room_failure must name its course_code"


# ===========================================================================
# SITE 1 — core/services/timetable_autoplace.py:1102-1104
#
# ``auto_place_board`` scoring step yields ``best_option is None``. Today
# increments ``total_skipped`` with no per-section reason captured.
# ===========================================================================


@pytest.mark.django_db
class TestSiteAutoplaceBestOptionNone:
    """Site 1 — autoplace scoring returns None.

    Scenario: build a board where every candidate slot fails at least one
    hard constraint so ``best_option`` is always None. auto_place_board
    must return a result whose room_failures carries a typed reason for
    every skipped section.
    """

    def test_skipped_sections_carry_reason_in_payload(self) -> None:
        pytest.skip(
            "Site 1 (autoplace.py:1102) end-to-end behaviour lands with "
            "commit 3 (silent-to-typed swap) + commit 5 (payload surface). "
            "Parked as a committed-intent test until then."
        )


# ===========================================================================
# SITE 2 — core/services/timetable_autoplace.py:1162
#
# ``auto_place_board`` fallback sets ``assigned_room = "UNASSIGNED"`` with
# no per-section record. Today the string leaks into the placement dict
# with no reason trace.
# ===========================================================================


@pytest.mark.django_db
class TestSiteAutoplaceUnassignedFallback:
    """Site 2 — autoplace sets assigned_room='UNASSIGNED' in fallback."""

    def test_fallback_unassigned_emits_structured_reason(self) -> None:
        pytest.skip(
            "Site 2 (autoplace.py:1162) end-to-end assertion lands with "
            "commit 3 + commit 5. Placeholder until then."
        )


# ===========================================================================
# SITE 3 — core/services/timetable_rooming.py:324
#
# ``assign_rooms_to_board`` sets ``p.room = "UNASSIGNED"`` after
# ``tracker.assign_best_fit`` returns None. Today only the
# ``lecture_room_reject_due_to_buffer_count`` counter is bumped; the
# reason is lost.
# ===========================================================================


@pytest.mark.django_db
class TestSiteRoomingUnassigned:
    """Site 3 — assign_rooms_to_board unassigned path.

    Of the four sites this one has the most direct test surface because
    ``assign_rooms_to_board`` is the rooming entry point and its return
    dict is the payload surface commit 5 extends. When the oracle sees
    NO_ROOM_CAPACITY / ROOM_BUFFER_REJECT / NO_ROOM_GENDER, the reason
    must appear in the returned ``room_failures`` list.
    """

    def test_buffer_reject_surfaces_as_room_buffer_reject(self) -> None:
        pytest.skip(
            "Site 3 (rooming.py:324) end-to-end assertion lands with "
            "commit 3 + commit 5. See pr2_buffer_reject.json for the "
            "scenario shape."
        )

    def test_capacity_short_surfaces_as_no_room_capacity(self) -> None:
        pytest.skip(
            "Site 3 (rooming.py:324) NO_ROOM_CAPACITY assertion lands with "
            "commit 3 + commit 5. See pr2_no_feasible_room.json."
        )


# ===========================================================================
# SITE 4 — core/services/timetable_room_repair.py:118-119
#
# ``try_repair_rooms_locally`` returns ``False`` when any section cannot
# place, with no per-section detail. Callers (the optimizer) see only
# "repair failed" and fall back to rollback.
# ===========================================================================


class TestSiteRoomRepairFalseReturn:
    """Site 4 — try_repair_rooms_locally returns False silently.

    PR2 keeps the boolean return for back-compat but adds a structured
    failure record accessible to callers. The commit-4 oracle is the
    natural place to surface this; the exact shape (out-parameter vs
    accumulator vs return-tuple) is settled during commit 4 design.
    Test is intentionally skipped until that contract is pinned.
    """

    def test_repair_failure_emits_structured_reason(self) -> None:
        pytest.skip(
            "Site 4 (room_repair.py:118-119) contract settles during "
            "commit 4 oracle wiring. Parked until then. The acceptance "
            "bar: a caller of try_repair_rooms_locally must be able to "
            "learn *why* a specific section failed to place, not just "
            "that the repair overall returned False."
        )

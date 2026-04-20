"""PR2 — per-site tests for the four silent-UNASSIGNED fallthroughs.

Acceptance criterion #1 in the PR2 DoR is that each of the four enumerated
call-sites that today emit a bare ``"UNASSIGNED"`` string without a typed
reason is swapped to a structured ``RoomFailureReason`` by the end of the
PR. This file is that acceptance test. Each of the four sites gets its own
case; nothing here is a grep.

Failing progression:

- **Commit 1**: all tests fail at collection with ``ModuleNotFoundError``
  because ``core.services.timetable_room_oracle`` doesn't exist yet.
- **Commit 2** (oracle module with dataclass + sentinels): collection
  succeeds; skip bodies keep the site tests parked.
- **Commit 3** (silent-to-typed swap + payload surface): Site 3 (rooming
  entry point) and Site 4 (room_repair) tests unskip and assert against
  real behaviour. Sites 1 and 2 stay skipped — the autoplace pipeline
  needs a fuller fixture (scenario + student map + budgets + overlap
  matrix) that arrives with the management command in commit 5.
- **Commit 4**: the oracle can distinguish reasons; default
  ``NO_ROOM_CAPACITY`` codes emitted today get replaced by specific
  reasons (``ROOM_BUFFER_REJECT``, ``NO_ROOM_GENDER``, etc.).
- **Commit 5**: autoplace fixture lands; Sites 1 and 2 tests unskip.

The four sites, from the PR2 DoR's "Code anchors" table:

| # | File:line                                   | Function |
|---|---------------------------------------------|----------|
| 1 | core/services/timetable_autoplace.py:~1111  | auto_place_board — best_option None after scoring |
| 2 | core/services/timetable_autoplace.py:~1189  | auto_place_board — fallback sets assigned_room = "UNASSIGNED" |
| 3 | core/services/timetable_rooming.py:~324     | assign_rooms_to_board — tracker.assign_best_fit returned None |
| 4 | core/services/timetable_room_repair.py:~118 | try_repair_rooms_locally — section unable to place |

Each test is written against the live entry-point functions
(``auto_place_board``, ``assign_rooms_to_board``, ``try_repair_rooms_locally``)
so the assertion is behavioural — not a grep on source code.
"""

from __future__ import annotations

import pytest

from core.models import (
    DeliveryBoard,
    Room,
    ScenarioSectionBudget,
    SectionPlacement,
    TermSection,
    TimetableScenario,
)
from core.services.timetable_assignment_models import (
    MoveSnapshot,
    RoomOccupancy,
    RoomProfile,
    SectionGridSnapshot,
    SectionMeeting,
    SectionState,
)
from core.services.timetable_room_oracle import (
    NO_ROOM_CAPACITY,
    RoomFailureReason,
)
from core.services.timetable_room_repair import try_repair_rooms_locally
from core.services.timetable_rooming import assign_rooms_to_board

# ---------------------------------------------------------------------------
# Shared helpers: payload-shape checks for the PR2 contract.
# ---------------------------------------------------------------------------


def _assert_pr2_payload_shape(result: dict) -> None:
    """Commit 3 adds ``room_failures`` and ``unplaced_count`` to the return dict.

    Before commit 3 this helper raised ``KeyError``; from commit 3 onward
    both keys are always present on the rooming / autoplace return dicts.
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
# SITE 1 — core/services/timetable_autoplace.py (best_option is None)
# ===========================================================================


@pytest.mark.django_db
class TestSiteAutoplaceBestOptionNone:
    """Site 1 — autoplace scoring returns None.

    Commit 3 wires Site 1 to emit ``NO_ROOM_CAPACITY`` (default per PR2
    DoR when the site can't distinguish a specific cause). Behavioural
    assertion is parked until commit 5 when the full autoplace harness
    (scenario + student map + budgets + overlap matrix) lands alongside
    the management command.
    """

    def test_skipped_sections_carry_reason_in_payload(self) -> None:
        pytest.skip(
            "Site 1 (autoplace.py best_option None) is wired in commit 3, "
            "but the end-to-end acceptance harness (scenario + student map + "
            "budgets + overlap matrix) lands alongside the management "
            "command in commit 5. Parked until then."
        )


# ===========================================================================
# SITE 2 — core/services/timetable_autoplace.py (UNASSIGNED fallback)
# ===========================================================================


@pytest.mark.django_db
class TestSiteAutoplaceUnassignedFallback:
    """Site 2 — autoplace sets assigned_room='UNASSIGNED' in fallback."""

    def test_fallback_unassigned_emits_structured_reason(self) -> None:
        pytest.skip(
            "Site 2 (autoplace.py tracker None fallback) is wired in "
            "commit 3; the end-to-end acceptance fixture lands in commit 5 "
            "with the management command."
        )


# ===========================================================================
# SITE 3 — core/services/timetable_rooming.py (tracker None)
#
# assign_rooms_to_board is the rooming entry point whose return dict is
# the payload surface commit 3 extends. This is the site with the most
# direct test surface — a one-course, one-room scenario where the room
# capacity is too small is enough to drive the else branch.
# ===========================================================================


@pytest.mark.django_db
class TestSiteRoomingUnassigned:
    """Site 3 — assign_rooms_to_board unassigned path."""

    def _build_one_course_one_room_scenario(
        self,
        room_capacity: int,
        section_demand: int,
        *,
        section_code: str = "S1",
        room_gender: str = "M",
    ) -> tuple[TimetableScenario, DeliveryBoard]:
        """Build a minimal one-course / one-room fixture.

        When ``room_capacity < section_demand * buffer`` the tracker
        returns None, driving the Site 3 else branch.
        """
        scenario = TimetableScenario.objects.create(
            academic_year="1448",
            term="1",
            name="PR2 Site 3 Fixture",
        )
        board = DeliveryBoard.objects.create(
            scenario=scenario,
            label="PR2S3",
            program="PR2",
            display_order=1,
        )
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_code="PR2S3_A",
            department="PR2",
            credit_hours=3,
            planned_sections=1,
            max_per_section=60,
            total_demand=section_demand,
        )
        term_section = TermSection.objects.create(
            scenario=scenario,
            course_code="PR2S3_A",
            course_number="101",
            course_key="PR2S3_A",
            section=section_code,
        )
        SectionPlacement.objects.create(
            board=board,
            term_section=term_section,
            day="Sun",
            start_time="08:00",
            end_time="09:15",
            room="",
        )
        Room.objects.create(
            room_code="PR2S3-ROOM",
            capacity=room_capacity,
            room_type="lecture",
            department="PR2",
            section=room_gender,
        )
        return scenario, board

    def test_capacity_short_surfaces_as_no_room_capacity(self) -> None:
        """Only room is capacity 10; course needs ~40 seats.

        Tracker None → RoomFailureReason with NO_ROOM_CAPACITY code.
        """
        _, board = self._build_one_course_one_room_scenario(
            room_capacity=10,
            section_demand=40,
        )

        result = assign_rooms_to_board(board.id)

        _assert_pr2_payload_shape(result)
        assert result["assigned"] == 0
        assert result["unassigned"] == 1
        assert result["unplaced_count"] == 1
        _assert_every_unplaced_has_reason(result)

        failure = result["room_failures"][0]
        assert failure["reason"] == NO_ROOM_CAPACITY
        assert failure["course_code"] == "PR2S3_A"
        assert failure["section_code"] == "S1"
        assert failure["day"] == "Sun"
        assert failure["start_time"] == "08:00"
        assert failure["end_time"] == "09:15"

    def test_successful_assignment_leaves_room_failures_empty(self) -> None:
        """Parity case: a feasible room → room_failures == [] and
        unplaced_count == 0. This is the DoR "parity preserved for
        feasible rooming" gate — commit 3 must not spray failure records
        into payloads for successful rooming runs."""
        _, board = self._build_one_course_one_room_scenario(
            room_capacity=60,
            section_demand=40,
        )

        result = assign_rooms_to_board(board.id)

        _assert_pr2_payload_shape(result)
        assert result["assigned"] == 1
        assert result["unassigned"] == 0
        assert result["unplaced_count"] == 0
        assert result["room_failures"] == []

    def test_missing_board_returns_empty_payload_shape(self) -> None:
        """Early return (board does not exist) still satisfies the PR2
        payload contract — callers can trust the shape without branching
        on error paths."""
        result = assign_rooms_to_board(board_id=999_999_999)

        _assert_pr2_payload_shape(result)
        assert result["assigned"] == 0
        assert result["unassigned"] == 0
        assert result["unplaced_count"] == 0
        assert result["room_failures"] == []


# ===========================================================================
# SITE 4 — core/services/timetable_room_repair.py (not placed → return False)
#
# try_repair_rooms_locally keeps the boolean return for back-compat. The
# PR2 commit-3 surface is an optional ``failures_out: list | None = None``
# kwarg: when the caller passes a list, typed RoomFailureReason dicts are
# appended as sections fail to place. Default None = behaviour unchanged.
# ===========================================================================


def _meeting(day: int, start_min: int, end_min: int) -> SectionMeeting:
    return SectionMeeting(day=day, start_min=start_min, end_min=end_min, slot_size=5)


def _section(section_id: str, course: str, meetings: list[SectionMeeting]) -> SectionState:
    return SectionState(
        section_id=section_id,
        course_code=course,
        meetings=meetings,
        max_capacity=40,
        reserve_capacity=0,
        room_type_required="lecture",
    )


class TestSiteRoomRepairFalseReturn:
    """Site 4 — try_repair_rooms_locally returns False silently (or with
    a typed record when the caller opts in via ``failures_out``).

    Back-compat: the boolean return is unchanged. Callers that pass
    ``failures_out`` get the structured record appended on failure. The
    acceptance bar — a caller must be able to learn *why* a specific
    section failed to place — is met today via this opt-in accumulator.
    """

    def _snapshot_for(self, section_id: str, meetings: list[SectionMeeting]) -> MoveSnapshot:
        return MoveSnapshot(
            snapshots=[
                SectionGridSnapshot(
                    section_id=section_id,
                    old_pattern_id="",
                    old_meetings=list(meetings),
                    old_room_id=None,
                )
            ]
        )

    def test_default_call_still_returns_boolean(self) -> None:
        """No ``failures_out`` → default behaviour unchanged, return False."""
        meetings_a = [_meeting(0, 8 * 60, 9 * 60 + 15)]
        sec = _section("secA", "COURSE_A", meetings_a)
        snap = self._snapshot_for("secA", meetings_a)

        result = try_repair_rooms_locally(snap, {"secA": sec}, {}, {}, {})

        assert result is False

    def test_failures_out_accumulator_receives_typed_reason_on_failure(self) -> None:
        """Opt-in: passing ``failures_out`` yields a RoomFailureReason
        dict with reason ``NO_ROOM_CAPACITY`` when the section can't
        place."""
        meetings_a = [_meeting(0, 8 * 60, 9 * 60 + 15)]
        sec = _section("secA", "COURSE_A", meetings_a)
        snap = self._snapshot_for("secA", meetings_a)

        failures_out: list[dict] = []
        result = try_repair_rooms_locally(
            snap, {"secA": sec}, {}, {}, {}, failures_out=failures_out
        )

        assert result is False
        assert len(failures_out) == 1
        failure = failures_out[0]
        assert failure["reason"] == NO_ROOM_CAPACITY
        assert failure["course_code"] == "COURSE_A"
        assert failure["section_code"] == "secA"

    def test_failures_out_stays_empty_on_successful_repair(self) -> None:
        """Parity: when repair succeeds, the accumulator stays empty —
        zero spurious records."""
        meetings_a = [_meeting(0, 8 * 60, 9 * 60 + 15)]
        sec = _section("secA", "COURSE_A", meetings_a)

        room = RoomProfile(room_id="R1", capacity=40, room_type="lecture")
        rooms_by_id = {"R1": room}
        room_occupancies = {"R1": RoomOccupancy(room_id="R1")}

        snap = self._snapshot_for("secA", meetings_a)

        failures_out: list[dict] = []
        result = try_repair_rooms_locally(
            snap,
            {"secA": sec},
            rooms_by_id,
            room_occupancies,
            {},
            failures_out=failures_out,
        )

        assert result is True
        assert failures_out == []

    def test_typed_record_is_serialisable_roomfailurereason_shape(self) -> None:
        """The dict appended to failures_out must match
        ``RoomFailureReason.to_dict()`` — the PR1-aligned shape with
        ``reason``, ``day``, ``start_time``, ``end_time``, ``course_code``,
        ``section_code`` (and optional ``context``)."""
        meetings_a = [_meeting(0, 8 * 60, 9 * 60 + 15)]
        sec = _section("secA", "COURSE_A", meetings_a)
        snap = self._snapshot_for("secA", meetings_a)

        failures_out: list[dict] = []
        try_repair_rooms_locally(snap, {"secA": sec}, {}, {}, {}, failures_out=failures_out)

        assert len(failures_out) == 1
        record = failures_out[0]
        for required_key in (
            "reason",
            "day",
            "start_time",
            "end_time",
            "course_code",
            "section_code",
        ):
            assert required_key in record, f"Missing required key '{required_key}'"

        reference = RoomFailureReason(
            code=NO_ROOM_CAPACITY,
            day="",
            start_time="",
            end_time="",
            course_code="COURSE_A",
            section_code="secA",
        ).to_dict()
        assert set(record.keys()) == set(reference.keys())

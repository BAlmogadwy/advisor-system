"""PR2 — room-feasibility oracle tests (failing by design until commit 2+).

The module ``core.services.timetable_room_oracle`` does not exist yet — the
import at the top of this file raises ``ModuleNotFoundError`` on collection,
so every test here fails as a suite until commit 2 lands. That is the
intended contract: commit 1 freezes the public API shape *before* any
implementation code appears, so the implementation cannot drift.

Progression of passing-ness:

- **Commit 2** (RoomFailureReason + sentinels + flag): Section A turns green.
  The dataclass, the six code constants, and the flag helper all become
  importable; the shape/default tests pass.
- **Commit 3** (silent UNASSIGNED → structured emission): nothing here moves
  yet; the swap is covered by ``test_pr2_silent_unassigned_sites.py``.
- **Commit 4** (staged oracle wiring): Section B turns green — the seven
  scenario tests exercise the actual oracle helpers against the fixture
  scenarios. Parity case also flips green at this commit because it
  asserts a *negative* (no failures on obviously feasible rooming).
- **Commit 5** (payload surface): nothing here moves; payload assertions
  live in the silent-sites file.

Per the PR2 DoR:

- Reason codes: NO_ROOM_CAPACITY, NO_ROOM_GENDER, NO_ROOM_TYPE,
  ROOM_OCCUPIED, ROOM_BUFFER_REJECT, ROOM_HEURISTIC_MISMATCH.
- Test order: #1 buffer reject · #2 wrong gender · #3 wrong type ·
  #4 room occupied · #5 no feasible room · #6 parity · #7 heuristic
  mismatch. Parity is a first-class member of the set (not an
  afterthought); heuristic-mismatch is observational (still ROOM_xxx
  surfaces, no placement decision change in PR2).
- Flag: TIMETABLE_PR2_ROOM_ORACLE_ENABLED. When off, every oracle helper
  returns ``None`` — identical to PR1's flag-gated pattern.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

from django.test import SimpleTestCase
from django.test.utils import override_settings

# Contract imports — this block is the commit-1 tripwire. Every symbol below
# names something commit 2 must expose. A rename or a dropped symbol in commit
# 2 breaks the suite at collection — which is exactly what we want.
from core.services.timetable_room_oracle import (
    NO_ROOM_CAPACITY,
    NO_ROOM_GENDER,
    NO_ROOM_TYPE,
    ROOM_BUFFER_REJECT,
    ROOM_HEURISTIC_MISMATCH,
    ROOM_OCCUPIED,
    RoomFailureReason,
    check_buffer_fit,
    check_capacity_feasibility,
    check_gender_feasibility,
    check_heuristic_match,
    check_occupancy,
    check_type_feasibility,
    is_room_oracle_enabled,
)

# ---------------------------------------------------------------------------
# Fixture builders. Shape mirrors the rooming code's existing dicts so the
# oracle helpers can drop into the real flow without adapters.
# ---------------------------------------------------------------------------


def _room(
    code: str,
    capacity: int,
    rtype: str = "lecture",
    gender: str = "M",
) -> dict:
    """A room dict in the shape emitted by ``get_programme_rooms()``.

    ``section`` carries the gender segregation tag in the live code; kept
    as-is so the oracle can read either key name until commit 4 settles it.
    """
    return {
        "room_code": code,
        "capacity": capacity,
        "room_type": rtype,
        "section": gender,
        "gender": gender,
        "wing": "W1",
        "building": "B1",
    }


def _section(
    course: str,
    sec: str,
    demand: int,
    *,
    day: str = "Sun",
    start: str = "08:00",
    end: str = "09:15",
    gender: str = "M",
    rtype: str = "lecture",
    credit_rating: int = 3,
) -> dict:
    return {
        "course_code": course,
        "section_code": sec,
        "enrolment": demand,
        "day": day,
        "start_time": start,
        "end_time": end,
        "gender": gender,
        "required_type": rtype,
        "credit_rating": credit_rating,
    }


# ===========================================================================
# SECTION A — API-shape tests (turn green at commit 2).
# ===========================================================================


class TestOracleModuleShape(SimpleTestCase):
    """Commit 2 lands the dataclass, sentinels, and flag helper. These
    tests enforce that surface.
    """

    def test_reason_codes_are_stable_strings(self) -> None:
        """Payload consumers (reports, tests, future dashboards) key off
        these exact strings. Renaming any one is a breaking change."""
        assert NO_ROOM_CAPACITY == "NO_ROOM_CAPACITY"
        assert NO_ROOM_GENDER == "NO_ROOM_GENDER"
        assert NO_ROOM_TYPE == "NO_ROOM_TYPE"
        assert ROOM_OCCUPIED == "ROOM_OCCUPIED"
        assert ROOM_BUFFER_REJECT == "ROOM_BUFFER_REJECT"
        assert ROOM_HEURISTIC_MISMATCH == "ROOM_HEURISTIC_MISMATCH"

    def test_reason_codes_are_distinct(self) -> None:
        codes = {
            NO_ROOM_CAPACITY,
            NO_ROOM_GENDER,
            NO_ROOM_TYPE,
            ROOM_OCCUPIED,
            ROOM_BUFFER_REJECT,
            ROOM_HEURISTIC_MISMATCH,
        }
        assert len(codes) == 6

    def test_room_failure_reason_is_frozen_dataclass(self) -> None:
        r = RoomFailureReason(
            code=NO_ROOM_CAPACITY,
            day="Sun",
            start_time="08:00",
            end_time="09:15",
            course_code="CS101",
            section_code="S1",
            context={"needed": 50, "best_capacity": 30},
        )
        with self.assertRaises(FrozenInstanceError):
            r.code = "OTHER"  # type: ignore[misc]

    def test_to_dict_carries_code_day_times_course_section_context(self) -> None:
        r = RoomFailureReason(
            code=ROOM_BUFFER_REJECT,
            day="Mon",
            start_time="10:00",
            end_time="11:15",
            course_code="CS201",
            section_code="S2",
            context={"needed": 33, "best_capacity": 32, "buffer": 1.1},
        )
        d = r.to_dict()
        assert d["reason"] == ROOM_BUFFER_REJECT
        assert d["day"] == "Mon"
        assert d["start_time"] == "10:00"
        assert d["end_time"] == "11:15"
        assert d["course_code"] == "CS201"
        assert d["section_code"] == "S2"
        assert d["context"] == {"needed": 33, "best_capacity": 32, "buffer": 1.1}

    def test_to_dict_omits_context_when_none(self) -> None:
        r = RoomFailureReason(
            code=NO_ROOM_GENDER,
            day="Sun",
            start_time="08:00",
            end_time="09:15",
            course_code="CS101",
            section_code="S1",
        )
        d = r.to_dict()
        assert "context" not in d

    def test_flag_defaults_on_after_promotion(self) -> None:
        """``TIMETABLE_PR2_ROOM_ORACLE_ENABLED`` defaults True post-commit-6
        promotion. Env var override preserved so production can disable via
        ``TIMETABLE_PR2_ROOM_ORACLE_ENABLED=false`` if a regression appears."""
        assert is_room_oracle_enabled() is True


# ===========================================================================
# SECTION B — Scenario tests (turn green at commit 4).
#
# Each scenario mirrors a fixture under
# snapshots/planner-refactor-2026-04-20/fixtures/pr2_*.json.
# The fixture JSONs are the documentation of the expected shape; the tests
# build the data programmatically so pytest can run without fixture IO.
# ===========================================================================


@override_settings(TIMETABLE_PR2_ROOM_ORACLE_ENABLED=False)
class TestOracleFlagOffIsNoOp(SimpleTestCase):
    """With the oracle flag OFF, every helper returns ``None`` — identical
    to PR1's prayer/lock flag-off contract. This is the rollback surface:
    flipping the flag back to False must fully restore pre-PR2 behaviour."""

    def test_gender_check_returns_none_when_flag_off(self) -> None:
        section = _section("CS101", "S1", demand=20, gender="F")
        rooms = [_room("A101", capacity=30, gender="M")]
        assert check_gender_feasibility(section, rooms) is None

    def test_capacity_check_returns_none_when_flag_off(self) -> None:
        section = _section("CS101", "S1", demand=200, gender="M")
        rooms = [_room("A101", capacity=30, gender="M")]
        assert check_capacity_feasibility(section, rooms, capacity_buffer=1.1) is None


@override_settings(TIMETABLE_PR2_ROOM_ORACLE_ENABLED=True)
class TestOracleScenarios(SimpleTestCase):
    """Seven scenario tests, DoR order. Each corresponds to a
    ``pr2_<name>.json`` fixture.
    """

    # --- #1 buffer-reject: fits raw, fails buffer --------------------------

    def test_01_fits_raw_but_fails_buffer_emits_buffer_reject(self) -> None:
        """Section of 30, only room is capacity 32, buffer 1.1 → need 33.
        Fixture: pr2_buffer_reject.json."""
        section = _section("CS101", "S1", demand=30, gender="M", rtype="lecture")
        room = _room("A101", capacity=32, gender="M", rtype="lecture")
        result = check_buffer_fit(section, room, capacity_buffer=1.1)
        assert result is not None
        assert result.code == ROOM_BUFFER_REJECT
        assert result.course_code == "CS101"
        assert result.section_code == "S1"
        assert result.context is not None
        assert result.context.get("needed") == 33
        assert result.context.get("best_capacity") == 32
        assert result.context.get("buffer") == 1.1

    # --- #2 wrong gender only ---------------------------------------------

    def test_02_wrong_gender_only_emits_no_room_gender(self) -> None:
        """Female section, every room is male. Capacity + type fine.
        Fixture: pr2_wrong_gender.json."""
        section = _section("CS101", "S1", demand=20, gender="F", rtype="lecture")
        rooms = [
            _room("A101", capacity=40, gender="M", rtype="lecture"),
            _room("A102", capacity=40, gender="M", rtype="lecture"),
        ]
        result = check_gender_feasibility(section, rooms)
        assert result is not None
        assert result.code == NO_ROOM_GENDER
        assert result.course_code == "CS101"
        assert result.section_code == "S1"

    # --- #3 wrong room type only ------------------------------------------

    def test_03_wrong_type_only_emits_no_room_type(self) -> None:
        """Lab-required section, only lecture rooms available. Gender +
        capacity fine. Fixture: pr2_wrong_type.json."""
        section = _section("CS202", "S1", demand=20, gender="M", rtype="lab")
        rooms = [
            _room("A101", capacity=40, gender="M", rtype="lecture"),
            _room("A102", capacity=40, gender="M", rtype="lecture"),
        ]
        result = check_type_feasibility(section, rooms)
        assert result is not None
        assert result.code == NO_ROOM_TYPE

    # --- #4 all rooms occupied at slot ------------------------------------

    def test_04_all_rooms_occupied_emits_room_occupied(self) -> None:
        """Eligible rooms exist but every one is busy at (day, start_time).
        Fixture: pr2_all_rooms_occupied.json."""
        section = _section("CS101", "S1", demand=20, gender="M", rtype="lecture")
        rooms = [
            _room("A101", capacity=40, gender="M", rtype="lecture"),
            _room("A102", capacity=40, gender="M", rtype="lecture"),
        ]
        occupancy = {"A101", "A102"}
        result = check_occupancy(section, rooms, occupancy_at_slot=occupancy)
        assert result is not None
        assert result.code == ROOM_OCCUPIED

    # --- #5 no feasible room at all ---------------------------------------

    def test_05_no_feasible_room_at_all_emits_no_room_capacity(self) -> None:
        """Demand exceeds every room's capacity with buffer applied.
        Fixture: pr2_no_feasible_room.json."""
        section = _section("BIG101", "S1", demand=200, gender="M", rtype="lecture")
        rooms = [
            _room("A101", capacity=30, gender="M", rtype="lecture"),
            _room("A102", capacity=40, gender="M", rtype="lecture"),
        ]
        result = check_capacity_feasibility(section, rooms, capacity_buffer=1.1)
        assert result is not None
        assert result.code == NO_ROOM_CAPACITY
        assert result.context is not None
        assert result.context.get("needed") == 220  # 200 * 1.1

    # --- #6 parity: obviously feasible rooming ----------------------------

    def test_06_parity_feasible_rooming_returns_none(self) -> None:
        """Obvious feasibility: small section, large right-gender lecture
        room, no occupancy, no buffer trouble. Every oracle helper returns
        None — the oracle stays silent when rooming is uneventful.
        Fixture: pr2_parity.json."""
        section = _section("CS101", "S1", demand=20, gender="M", rtype="lecture")
        rooms = [_room("A101", capacity=40, gender="M", rtype="lecture")]

        assert check_gender_feasibility(section, rooms) is None
        assert check_type_feasibility(section, rooms) is None
        assert check_capacity_feasibility(section, rooms, capacity_buffer=1.1) is None
        assert check_occupancy(section, rooms, occupancy_at_slot=set()) is None
        assert check_buffer_fit(section, rooms[0], capacity_buffer=1.1) is None
        assert check_heuristic_match(section, rooms) is None

    # --- #7 heuristic mismatch: 2-credit long lecture ---------------------

    @override_settings(TIMETABLE_LAB_HEURISTIC_UNIFIED=False)
    def test_07_heuristic_mismatch_emits_room_heuristic_mismatch(self) -> None:
        """2-credit meeting that runs 100 minutes — the legacy
        ``duration > 80 and cr == 4`` guard in timetable_rooming.py:305
        would classify this as a lab because duration>80, but the section
        is 2-credit (cr==2, not 4). The oracle observes the mismatch but
        does NOT reassign — PR2 is observation-only on the heuristic.
        Fixture: pr2_heuristic_mismatch.json.

        PR4 commit 8 promoted ``TIMETABLE_LAB_HEURISTIC_UNIFIED`` to
        ``True`` by default; under that promoted default the planner,
        rooming, and oracle all route through ``meeting_requires_lab_room``
        and no mismatch is possible. This test is overridden to the
        kill-switch path (flag=False) so it continues to regression-guard
        the legacy observation behaviour operators fall back to when the
        flag is flipped off.
        """
        section = _section(
            "CS105",
            "S1",
            demand=20,
            gender="M",
            rtype="lecture",
            start="08:00",
            end="09:40",  # 100 minutes
            credit_rating=2,
        )
        rooms = [_room("A101", capacity=40, gender="M", rtype="lecture")]
        result = check_heuristic_match(section, rooms)
        assert result is not None
        assert result.code == ROOM_HEURISTIC_MISMATCH
        # The mismatch is observational only: the section still fits in a
        # real lecture room in PR2. The reason carries context identifying
        # which heuristic dimension (duration, cr) diverged.
        assert result.context is not None
        assert "duration_minutes" in result.context
        assert "credit_rating" in result.context

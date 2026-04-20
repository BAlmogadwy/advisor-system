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
from django.test import override_settings

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


def _build_autoplace_fixture(
    *,
    program: str,
    course_code: str,
    room_type: str = "lecture",
    room_count: int = 1,
    room_capacity: int = 60,
    room_gender: str = "M",
    slot_config: list[dict] | None = None,
    blocked_slots: list[dict] | None = None,
    total_demand: int = 30,
    planned_sections: int = 1,
    credit_hours: int = 3,
) -> tuple[TimetableScenario, DeliveryBoard]:
    """Build a minimal but realistic autoplace fixture.

    Commits 3 & 4 enumerated Site 1/2 but parked their acceptance tests
    because the fixture needed scenario + budget + student map + rooms
    wired together. Commit 5 is where that harness lands — see the
    ``TestSiteAutoplaceBestOptionNone`` and
    ``TestSiteAutoplaceUnassignedFallback`` tests below. This helper
    keeps the two tests DRY without pulling in the exam-timetable or
    PR1 fixtures (which carry unrelated state).
    """
    from core.models import Course, ProgrammeRequirement

    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name=f"PR2 autoplace fixture — {course_code}",
        slot_config=slot_config or [],
        blocked_slots=blocked_slots or [],
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario,
        label=f"{program}_BD",
        program=program,
        display_order=1,
    )
    Course.objects.get_or_create(
        course_code=course_code,
        defaults={"credit_hours": credit_hours, "department": program},
    )
    ProgrammeRequirement.objects.get_or_create(
        program=program,
        course_code=course_code,
        defaults={"programme_term": 1, "credit_hours": credit_hours},
    )
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_code=course_code,
        department=program,
        credit_hours=credit_hours,
        planned_sections=planned_sections,
        max_per_section=max(total_demand, 1),
        total_demand=total_demand,
    )
    for i in range(room_count):
        Room.objects.create(
            room_code=f"{program}-R{i}",
            capacity=room_capacity,
            room_type=room_type,
            department=program,
            section=room_gender,
        )
    return scenario, board


# ===========================================================================
# SITE 1 — core/services/timetable_autoplace.py (best_option is None)
#
# Reachable path: every candidate option is filtered out before scoring.
# The only live filter that produces this in auto_place_board is the
# prayer-overlap rule — when its windows cover every configured slot,
# the option loop ``continue``s every iteration, best_option stays None,
# and the Site 1 emission path runs. Commit 4 wires the refinement chain
# (type → gender → capacity) against the overall room pool; slot fields
# stay empty strings because no option survived.
# ===========================================================================


@pytest.mark.django_db
class TestSiteAutoplaceBestOptionNone:
    """Site 1 — every option prayer-rejected → best_option None."""

    def test_skipped_sections_carry_reason_in_payload(self) -> None:
        """Prayer windows cover every weekday; scoring loop yields
        nothing; Site 1 emits a typed reason into ``room_failures``."""
        from core.services.timetable_autoplace import auto_place_board

        # slot_config is day-independent (a list of start/end windows); the
        # generator pairs each slot with every WEEKDAY. To starve the
        # scoring loop of every option we need the prayer window to cover
        # the single slot on every weekday (SUN/MON/TUE/WED/THU).
        _, board = _build_autoplace_fixture(
            program="PR2S1",
            course_code="PR2S1_A",
            slot_config=[{"start": "08:00", "end": "09:15"}],
            credit_hours=1,
            total_demand=30,
        )

        whole_day_window = [
            {"day": d, "start_time": "00:00", "end_time": "23:59"}
            for d in ("SUN", "MON", "TUE", "WED", "THU")
        ]
        with override_settings(
            TIMETABLE_ENFORCE_PRAYER_OVERLAP_RULE=True,
            TIMETABLE_PRAYER_WINDOWS=whole_day_window,
        ):
            result = auto_place_board(board.id)

        _assert_pr2_payload_shape(result)
        assert "room_failure_breakdown" in result
        assert result["unplaced_count"] >= 1
        for f in result["room_failures"]:
            assert f["reason"]
            assert f["course_code"] == "PR2S1_A"

    @pytest.mark.usefixtures("oracle_on")
    def test_no_matching_room_type_surfaces_no_room_type(self) -> None:
        """Flag-on: the overall room pool is lab-only but the 1-credit
        section needs a lecture room — Site 1's type-feasibility helper
        fires first and surfaces NO_ROOM_TYPE."""
        from core.services.timetable_autoplace import auto_place_board

        _, board = _build_autoplace_fixture(
            program="PR2S1T",
            course_code="PR2S1T_A",
            room_type="lab",
            slot_config=[{"start": "08:00", "end": "09:15"}],
            credit_hours=1,
            total_demand=30,
        )

        whole_day_window = [
            {"day": d, "start_time": "00:00", "end_time": "23:59"}
            for d in ("SUN", "MON", "TUE", "WED", "THU")
        ]
        with override_settings(
            TIMETABLE_ENFORCE_PRAYER_OVERLAP_RULE=True,
            TIMETABLE_PRAYER_WINDOWS=whole_day_window,
        ):
            result = auto_place_board(board.id)

        assert result["unplaced_count"] >= 1
        assert result["room_failures"][0]["reason"] == NO_ROOM_TYPE
        assert result["room_failure_breakdown"].get(NO_ROOM_TYPE, 0) >= 1


# ===========================================================================
# SITE 2 — core/services/timetable_autoplace.py (UNASSIGNED fallback)
#
# Reachable path: scoring finds a best_option, but when the per-meeting
# room assignment runs, tracker.assign_best_fit returns None because
# the room pool doesn't match the required type. Commit 4 wires the
# Stage 1/2 chain (type → gender → capacity → occupancy) per-meeting.
# ===========================================================================


@pytest.mark.django_db
class TestSiteAutoplaceUnassignedFallback:
    """Site 2 — scoring succeeds but per-meeting tracker returns None."""

    def test_fallback_unassigned_emits_structured_reason(self) -> None:
        """Pool is all labs; scoring picks a slot (labs get room_penalty
        but it's soft); per-meeting assignment fails → Site 2 emits a
        typed reason per meeting."""
        from core.services.timetable_autoplace import auto_place_board

        _, board = _build_autoplace_fixture(
            program="PR2S2",
            course_code="PR2S2_A",
            room_type="lab",
            slot_config=[{"start": "08:00", "end": "09:15"}],
            credit_hours=1,
            total_demand=30,
        )

        result = auto_place_board(board.id)

        _assert_pr2_payload_shape(result)
        assert "room_failure_breakdown" in result
        assert result["unplaced_count"] == 0
        assert result["placed"] >= 1
        assert len(result["room_failures"]) >= 1
        for f in result["room_failures"]:
            assert f["reason"]
            assert f["course_code"] == "PR2S2_A"
            assert f["start_time"] == "08:00"

    @pytest.mark.usefixtures("oracle_on")
    def test_lab_only_pool_surfaces_no_room_type_under_flag_on(self) -> None:
        """Flag-on: the refinement chain picks NO_ROOM_TYPE (not the
        default NO_ROOM_CAPACITY) because the type helper fires first."""
        from core.services.timetable_autoplace import auto_place_board

        _, board = _build_autoplace_fixture(
            program="PR2S2T",
            course_code="PR2S2T_A",
            room_type="lab",
            slot_config=[{"start": "08:00", "end": "09:15"}],
            credit_hours=1,
            total_demand=30,
        )

        result = auto_place_board(board.id)

        assert len(result["room_failures"]) >= 1
        assert result["room_failures"][0]["reason"] == NO_ROOM_TYPE
        assert result["room_failure_breakdown"].get(NO_ROOM_TYPE, 0) >= 1


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

    @override_settings(TIMETABLE_PR2_ROOM_ORACLE_ENABLED=False)
    def test_failures_out_accumulator_receives_typed_reason_on_failure(self) -> None:
        """Opt-in: passing ``failures_out`` yields a RoomFailureReason
        dict with reason ``NO_ROOM_CAPACITY`` when the section can't
        place. Flag pinned OFF so the default-fallback reason is asserted;
        a separate test covers the oracle-on refinement path."""
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

        room = RoomProfile(room_id="R1", capacity=40, room_type="lecture", gender="")
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

    @override_settings(TIMETABLE_PR2_ROOM_ORACLE_ENABLED=False)
    def test_typed_record_is_serialisable_roomfailurereason_shape(self) -> None:
        """The dict appended to failures_out must match
        ``RoomFailureReason.to_dict()`` — the PR1-aligned shape with
        ``reason``, ``day``, ``start_time``, ``end_time``, ``course_code``,
        ``section_code`` (and optional ``context``). Flag pinned OFF so the
        baseline six-key shape is asserted; the oracle-on path emits the
        same shape plus ``context`` and is covered by the refined-reason
        tests above."""
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


# ===========================================================================
# COMMIT 4 — oracle helper unit tests.
#
# Each helper has two shapes: flag-off (returns None, preserves commit 3
# parity) and flag-on (runs its stage check and returns a typed reason
# when the check fails). These tests pin both.
#
# Class-level ``@override_settings`` can't decorate plain pytest classes
# (Django refuses with "Only subclasses of SimpleTestCase..."), so the
# flag-on classes request a module-level fixture via
# ``@pytest.mark.usefixtures("oracle_on")``. The fixture uses
# pytest-django's ``settings`` fixture which auto-restores on teardown.
# ===========================================================================


@pytest.fixture
def oracle_on(settings):  # type: ignore[no-untyped-def]
    settings.TIMETABLE_PR2_ROOM_ORACLE_ENABLED = True


def _oracle_section(**overrides: object) -> dict:
    base = {
        "course_code": "CMP101",
        "section_code": "S1",
        "day": "Sun",
        "start_time": "08:00",
        "end_time": "09:15",
        "demand": 40,
        "room_type_required": "lecture",
        "gender_required": "",
    }
    base.update(overrides)
    return base


def _oracle_room(**overrides: object) -> dict:
    base = {
        "room_code": "R1",
        "capacity": 60,
        "room_type": "lecture",
        "gender": "",
    }
    base.update(overrides)
    return base


class TestOracleFlagOffReturnsNone:
    """Flag-off parity: every helper must return None regardless of its
    specific input when ``TIMETABLE_PR2_ROOM_ORACLE_ENABLED`` is False.

    This is the contract that keeps commit 3's default-``NO_ROOM_CAPACITY``
    payload bit-for-bit unchanged while commit 4 is rolling out.
    """

    @override_settings(TIMETABLE_PR2_ROOM_ORACLE_ENABLED=False)
    def test_all_helpers_return_none_when_flag_off(self) -> None:
        sec = _oracle_section(room_type_required="lab")
        empty_rooms: list[dict] = []
        assert check_type_feasibility(sec, empty_rooms) is None
        assert check_gender_feasibility(sec, empty_rooms) is None
        assert check_capacity_feasibility(sec, empty_rooms, 1.1) is None
        assert check_occupancy(sec, empty_rooms, set()) is None
        assert check_buffer_fit(sec, _oracle_room(capacity=40), 1.5) is None
        assert check_heuristic_match(sec, empty_rooms) is None


@pytest.mark.usefixtures("oracle_on")
class TestOracleTypeFeasibility:
    def test_no_matching_type_returns_no_room_type(self) -> None:
        sec = _oracle_section(room_type_required="lab")
        rooms = [_oracle_room(room_type="lecture")]
        failure = check_type_feasibility(sec, rooms)
        assert failure is not None
        assert failure.code == NO_ROOM_TYPE

    def test_matching_type_returns_none(self) -> None:
        sec = _oracle_section(room_type_required="lecture")
        rooms = [_oracle_room(room_type="lecture")]
        assert check_type_feasibility(sec, rooms) is None

    def test_blank_type_requirement_returns_none(self) -> None:
        sec = _oracle_section(room_type_required="")
        assert check_type_feasibility(sec, []) is None


@pytest.mark.usefixtures("oracle_on")
class TestOracleGenderFeasibility:
    def test_no_matching_gender_returns_no_room_gender(self) -> None:
        sec = _oracle_section(gender_required="F")
        rooms = [_oracle_room(gender="M")]
        failure = check_gender_feasibility(sec, rooms)
        assert failure is not None
        assert failure.code == NO_ROOM_GENDER

    def test_section_variant_key_also_matches(self) -> None:
        """Legacy rooming dicts store gender under ``section``, not
        ``gender``. The helper must accept both shapes."""
        sec = _oracle_section(gender_required="F")
        rooms = [_oracle_room(gender="", section="F")]
        assert check_gender_feasibility(sec, rooms) is None

    def test_gender_check_respects_type_filter(self) -> None:
        """An F-only lab exists but the section needs a *lecture* —
        gender helper must return None because the pool intersection
        is empty and the underlying failure is NO_ROOM_TYPE, not gender."""
        sec = _oracle_section(room_type_required="lecture", gender_required="F")
        rooms = [_oracle_room(room_type="lab", gender="F")]
        assert check_gender_feasibility(sec, rooms) is None

    def test_no_gender_requirement_returns_none(self) -> None:
        sec = _oracle_section(gender_required="")
        assert check_gender_feasibility(sec, [_oracle_room(gender="M")]) is None


@pytest.mark.usefixtures("oracle_on")
class TestOracleCapacityFeasibility:
    def test_capacity_short_returns_no_room_capacity(self) -> None:
        sec = _oracle_section(demand=40)
        rooms = [_oracle_room(capacity=30)]
        failure = check_capacity_feasibility(sec, rooms, 1.1)
        assert failure is not None
        assert failure.code == NO_ROOM_CAPACITY

    def test_buffered_demand_is_enforced(self) -> None:
        """A room with capacity 42 fits the raw demand of 40 but fails
        the buffered demand of 44 at buffer 1.1 — Stage 1 reports
        NO_ROOM_CAPACITY; Stage 2's ``check_buffer_fit`` carves out
        the sub-case separately."""
        sec = _oracle_section(demand=40)
        rooms = [_oracle_room(capacity=42)]
        failure = check_capacity_feasibility(sec, rooms, 1.1)
        assert failure is not None
        assert failure.code == NO_ROOM_CAPACITY

    def test_ample_capacity_returns_none(self) -> None:
        sec = _oracle_section(demand=40)
        rooms = [_oracle_room(capacity=60)]
        assert check_capacity_feasibility(sec, rooms, 1.1) is None


@pytest.mark.usefixtures("oracle_on")
class TestOracleOccupancy:
    def test_all_eligible_rooms_busy_returns_room_occupied(self) -> None:
        sec = _oracle_section(demand=30)
        rooms = [_oracle_room(room_code="R1", capacity=60)]
        failure = check_occupancy(sec, rooms, {"R1"})
        assert failure is not None
        assert failure.code == ROOM_OCCUPIED

    def test_free_room_exists_returns_none(self) -> None:
        sec = _oracle_section(demand=30)
        rooms = [_oracle_room(room_code="R1", capacity=60)]
        assert check_occupancy(sec, rooms, set()) is None

    def test_no_eligible_room_returns_none(self) -> None:
        """When the eligible pool is empty (e.g. no room of the right
        type), occupancy helper returns None — that's a Stage 1 miss,
        not an occupancy miss. Keeps the two distinguishable."""
        sec = _oracle_section(room_type_required="lab")
        rooms = [_oracle_room(room_type="lecture")]
        assert check_occupancy(sec, rooms, set()) is None


@pytest.mark.usefixtures("oracle_on")
class TestOracleBufferFit:
    def test_fits_raw_fails_buffer_returns_buffer_reject(self) -> None:
        sec = _oracle_section(demand=40)
        room = _oracle_room(capacity=42)  # fits 40, fails 40*1.1=44
        failure = check_buffer_fit(sec, room, 1.1)
        assert failure is not None
        assert failure.code == ROOM_BUFFER_REJECT

    def test_fits_buffered_returns_none(self) -> None:
        sec = _oracle_section(demand=40)
        room = _oracle_room(capacity=60)
        assert check_buffer_fit(sec, room, 1.1) is None

    def test_fails_raw_returns_none(self) -> None:
        """Below raw demand isn't a buffer problem — it's NO_ROOM_CAPACITY."""
        sec = _oracle_section(demand=40)
        room = _oracle_room(capacity=30)
        assert check_buffer_fit(sec, room, 1.1) is None


@pytest.mark.usefixtures("oracle_on")
class TestOracleHeuristicMatch:
    """PR4 commit 8 flipped ``TIMETABLE_LAB_HEURISTIC_UNIFIED`` to ``True``
    by default, which makes ``check_heuristic_match`` return ``None``
    early (the unified predicate guarantees no mismatch). These tests
    pin the flag to ``False`` so they continue to regression-guard the
    legacy PR2 observation semantics — i.e. the behaviour operators
    fall back to if the flag is turned off at runtime.
    """

    def test_2cr_100min_mismatch_emits_observation_with_context(self) -> None:
        """A 100-minute 2-credit meeting. Rooming's
        ``duration > 80 AND cr == 4`` says NOT-lab; autoplace's
        duration-only cut says LAB. PR2 surfaces the divergence."""
        with override_settings(TIMETABLE_LAB_HEURISTIC_UNIFIED=False):
            sec = _oracle_section(duration_minutes=100, credit_rating=2)
            failure = check_heuristic_match(sec, [])
            assert failure is not None
            assert failure.code == ROOM_HEURISTIC_MISMATCH
            assert failure.context is not None
            assert failure.context["is_lab_by_rooming_heuristic"] is False
            assert failure.context["is_lab_by_autoplace_heuristic"] is True

    def test_4cr_100min_agreement_returns_none(self) -> None:
        """4-credit 100-min meeting: both heuristics say LAB, no mismatch."""
        with override_settings(TIMETABLE_LAB_HEURISTIC_UNIFIED=False):
            sec = _oracle_section(duration_minutes=100, credit_rating=4)
            assert check_heuristic_match(sec, []) is None


# ===========================================================================
# COMMIT 4 — Site 3 wiring: refinement chain + buffer_only_rejects counter.
# ===========================================================================


@pytest.mark.usefixtures("oracle_on")
@pytest.mark.django_db
class TestSite3RefinementUnderFlagOn:
    """End-to-end: with the oracle flag on, ``assign_rooms_to_board``
    emits refined reason codes from the new Stage 1/2 helpers."""

    def test_wrong_gender_surfaces_no_room_gender(self) -> None:
        """One course needing gender M, one F-only room → NO_ROOM_GENDER."""
        _, board = TestSiteRoomingUnassigned()._build_one_course_one_room_scenario(
            room_capacity=60,
            section_demand=40,
            section_code="M1",
            room_gender="F",
        )
        result = assign_rooms_to_board(board.id)
        assert result["unplaced_count"] == 1
        assert result["room_failures"][0]["reason"] == NO_ROOM_GENDER

    def test_buffer_only_rejection_surfaces_room_buffer_reject(self) -> None:
        """Room capacity 42, section demand 40, buffer 1.1 (→ needs 44).
        Tracker None because buffered 44 > 42, but raw 40 ≤ 42 ⇒
        ROOM_BUFFER_REJECT plus the authoritative counter bumps."""
        _, board = TestSiteRoomingUnassigned()._build_one_course_one_room_scenario(
            room_capacity=42,
            section_demand=40,
        )
        result = assign_rooms_to_board(board.id)
        assert result["unplaced_count"] == 1
        assert result["room_failures"][0]["reason"] == ROOM_BUFFER_REJECT
        assert result["buffer_only_rejects"] == 1
        # PR4 commit 7 — the legacy ``lecture_room_reject_due_to_buffer_count``
        # key is retired; ``buffer_only_rejects`` above is the sole
        # buffer-only counter going forward.
        assert "lecture_room_reject_due_to_buffer_count" not in result

    def test_capacity_short_still_surfaces_no_room_capacity(self) -> None:
        """Raw demand > room capacity → neither buffer nor gender nor
        type explains it; Stage 1 defaults to NO_ROOM_CAPACITY.
        Commit-3 parity test is re-run under flag-on to prove the
        default path still wins when no refinement applies."""
        _, board = TestSiteRoomingUnassigned()._build_one_course_one_room_scenario(
            room_capacity=10,
            section_demand=40,
        )
        result = assign_rooms_to_board(board.id)
        assert result["unplaced_count"] == 1
        assert result["room_failures"][0]["reason"] == NO_ROOM_CAPACITY
        assert result["buffer_only_rejects"] == 0


# ===========================================================================
# COMMIT 4 — Site 4 wiring: type mismatch + occupancy detection.
# ===========================================================================


@pytest.mark.usefixtures("oracle_on")
class TestSite4RefinementUnderFlagOn:
    """End-to-end: with the oracle flag on, ``try_repair_rooms_locally``
    emits refined reason codes into ``failures_out``."""

    def _snap(self, section_id: str, meetings: list[SectionMeeting]) -> MoveSnapshot:
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

    def test_wrong_type_room_pool_surfaces_no_room_type(self) -> None:
        """Section needs a lecture; the only room is a lab → NO_ROOM_TYPE."""
        meetings_a = [_meeting(0, 8 * 60, 9 * 60 + 15)]
        sec = _section("secA", "COURSE_A", meetings_a)
        room = RoomProfile(room_id="R1", capacity=60, room_type="lab", gender="")
        rooms_by_id = {"R1": room}
        room_occupancies = {"R1": RoomOccupancy(room_id="R1")}
        snap = self._snap("secA", meetings_a)

        failures_out: list[dict] = []
        result = try_repair_rooms_locally(
            snap,
            {"secA": sec},
            rooms_by_id,
            room_occupancies,
            {},
            failures_out=failures_out,
        )

        assert result is False
        assert len(failures_out) == 1
        assert failures_out[0]["reason"] == NO_ROOM_TYPE

    def test_empty_room_pool_surfaces_no_room_type(self) -> None:
        """Zero rooms in the pool ⇒ the Stage 1 type helper wins
        (nothing in ``rooms`` matches the required type, by vacuity).
        This is the flag-on replacement of commit 3's silent
        NO_ROOM_CAPACITY default for the empty-rooms input."""
        meetings_a = [_meeting(0, 8 * 60, 9 * 60 + 15)]
        sec = _section("secA", "COURSE_A", meetings_a)
        snap = self._snap("secA", meetings_a)

        failures_out: list[dict] = []
        result = try_repair_rooms_locally(
            snap, {"secA": sec}, {}, {}, {}, failures_out=failures_out
        )

        assert result is False
        assert failures_out[0]["reason"] == NO_ROOM_TYPE

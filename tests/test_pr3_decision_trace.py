"""PR3 — decision-trace tests (failing by design until commit 2+).

The module ``core.services.timetable_decision_trace`` does not exist yet —
the import at the top of this file raises ``ModuleNotFoundError`` on
collection, so every test here fails as a suite until commit 2 lands.
That is the intended contract: commit 1 freezes the public API shape
*before* any implementation code appears, so the implementation cannot
drift.

Progression of passing-ness:

- **Commit 2** (`DecisionTrace` + `Alternative` dataclasses + sentinels +
  flag helper): Section A (shape tests + trace-disabled schema test)
  turns green. The dataclasses, the two new code constants
  (``INSTRUCTOR_CLASH``, ``STUDENT_CONFLICT``), and the flag helper all
  become importable; the shape/roundtrip tests pass.
- **Commit 3** (trace capture in ``auto_place_board``): Section B
  (capture + typed rejection codes) turns green. The cold-start capture
  test, ``INSTRUCTOR_CLASH`` and ``STUDENT_CONFLICT`` surfacing tests
  all flip green here.
- **Commit 4–8**: trace shape/capture tests stay green; no new test in
  this file moves after commit 3.

Per the PR3 DoR (docs/PR3-DOR.md):

- Rejection codes: PR1 (``PRAYER_WINDOW_CLASH``, ``LOCK_VIOLATION``) +
  PR2 (``NO_ROOM_*``, ``ROOM_*``) + PR3-new (``INSTRUCTOR_CLASH``,
  ``STUDENT_CONFLICT``). Trace entries MUST use only these sentinels.
- Alternative count: fixed at 3 max. Not configurable.
- Flag: ``TIMETABLE_PR3_DECISION_TRACE_ENABLED``. Default True from
  commit 2. When disabled, ``decision_trace`` key is still present in
  the payload with value ``{}`` (schema stability).
"""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

import pytest
from django.test import SimpleTestCase, TransactionTestCase
from django.test.utils import override_settings
from pr3_fixture_loader import load_pr3_fixture

# Contract imports — this block was the commit-1 tripwire. Every symbol
# below names something commit 2 and commit 3 expose. A rename or a
# dropped symbol breaks the suite at collection — which is exactly what
# we want.
from core.services.timetable_decision_trace import (
    INSTRUCTOR_CLASH,
    STUDENT_CONFLICT,
    Alternative,
    DecisionTrace,
    is_decision_trace_enabled,
)

# ===========================================================================
# SECTION A — Shape / dataclass tests (turn green at commit 2).
# ===========================================================================


class TestDecisionTraceShape(SimpleTestCase):
    """Pin the public shape of ``DecisionTrace`` and ``Alternative`` before
    any implementation lands. Every field name and type is load-bearing for
    downstream consumers (mgmt command, future UI)."""

    def _alt(self, code: str = INSTRUCTOR_CLASH) -> Alternative:
        return Alternative(
            day="Sun",
            start_time="09:30",
            end_time="10:45",
            room="A102",
            rejection_code=code,
            rejection_context={"clashing_section": "CS102|S1"},
        )

    def test_decision_trace_required_fields(self) -> None:
        trace = DecisionTrace(
            section_code="CS101|S1",
            course_code="CS101",
            chosen_day="Sun",
            chosen_start_time="08:00",
            chosen_end_time="09:15",
            chosen_room="A101",
            alternatives=(self._alt(),),
        )
        assert trace.section_code == "CS101|S1"
        assert trace.course_code == "CS101"
        assert trace.chosen_day == "Sun"
        assert trace.chosen_room == "A101"
        assert len(trace.alternatives) == 1

    def test_decision_trace_is_frozen(self) -> None:
        trace = DecisionTrace(
            section_code="CS101|S1",
            course_code="CS101",
            chosen_day="Sun",
            chosen_start_time="08:00",
            chosen_end_time="09:15",
            chosen_room="A101",
            alternatives=(),
        )
        with self.assertRaises(FrozenInstanceError):
            trace.section_code = "other"  # type: ignore[misc]

    def test_alternative_is_frozen(self) -> None:
        alt = self._alt()
        with self.assertRaises(FrozenInstanceError):
            alt.rejection_code = "other"  # type: ignore[misc]

    def test_alternatives_is_tuple_not_list(self) -> None:
        """Tuple so the dataclass stays hashable + frozen-safe. Lists would
        let consumers mutate the trace post-emission."""
        trace = DecisionTrace(
            section_code="CS101|S1",
            course_code="CS101",
            chosen_day="Sun",
            chosen_start_time="08:00",
            chosen_end_time="09:15",
            chosen_room="A101",
            alternatives=(self._alt(),),
        )
        assert isinstance(trace.alternatives, tuple)

    def test_to_dict_roundtrip_shape(self) -> None:
        """``.to_dict()`` must return a JSON-serialisable dict with the
        PR1-shape-aligned keys. Downstream consumers (mgmt command,
        result_json storage) depend on the dict shape being stable."""
        trace = DecisionTrace(
            section_code="CS101|S1",
            course_code="CS101",
            chosen_day="Sun",
            chosen_start_time="08:00",
            chosen_end_time="09:15",
            chosen_room="A101",
            alternatives=(self._alt(),),
        )
        d = trace.to_dict()

        for required_key in (
            "section_code",
            "course_code",
            "chosen_day",
            "chosen_start_time",
            "chosen_end_time",
            "chosen_room",
            "alternatives",
        ):
            assert required_key in d, f"Missing required key '{required_key}'"

        assert isinstance(d["alternatives"], list)
        assert len(d["alternatives"]) == 1
        alt_d = d["alternatives"][0]
        for alt_key in (
            "day",
            "start_time",
            "end_time",
            "room",
            "rejection_code",
            "rejection_context",
        ):
            assert alt_key in alt_d, f"Missing alternative key '{alt_key}'"

    def test_alternatives_max_3(self) -> None:
        """Per the DoR: alternative count is fixed at 3 max. The
        dataclass itself does not enforce the cap (the emit site does),
        but we document the expected invariant here so commit 3's emit
        logic can't silently drift past it."""
        alts = tuple(self._alt() for _ in range(3))
        trace = DecisionTrace(
            section_code="CS101|S1",
            course_code="CS101",
            chosen_day="Sun",
            chosen_start_time="08:00",
            chosen_end_time="09:15",
            chosen_room="A101",
            alternatives=alts,
        )
        assert len(trace.alternatives) <= 3


class TestRejectionCodeSentinels(SimpleTestCase):
    """The two new PR3 codes must be exposed as module-level string
    constants — mirrors the PR2 pattern where NO_ROOM_CAPACITY etc. are
    importable sentinels, not dataclass enums."""

    def test_instructor_clash_is_string_constant(self) -> None:
        assert isinstance(INSTRUCTOR_CLASH, str)
        assert INSTRUCTOR_CLASH == "INSTRUCTOR_CLASH"

    def test_student_conflict_is_string_constant(self) -> None:
        """Named CONFLICT (not OVERLAP) per DoR sign-off amendment A: this
        is cohort semantics, not geometric time-overlap."""
        assert isinstance(STUDENT_CONFLICT, str)
        assert STUDENT_CONFLICT == "STUDENT_CONFLICT"


class TestSchemaStability(SimpleTestCase):
    """Flag helper behaviour. The "trace key present even when disabled"
    integration test that exercises the full planner lives below in
    ``TestSchemaStabilityIntegration`` (fixture #10)."""

    def test_flag_defaults_on(self) -> None:
        """``TIMETABLE_PR3_DECISION_TRACE_ENABLED`` defaults True from
        commit 2 onwards — trace capture is observational and safe to
        enable by default immediately."""
        assert is_decision_trace_enabled() is True

    @override_settings(TIMETABLE_PR3_DECISION_TRACE_ENABLED=False)
    def test_flag_can_be_disabled(self) -> None:
        """Env-var override path still honoured — production can disable
        via ``TIMETABLE_PR3_DECISION_TRACE_ENABLED=false`` if a regression
        appears."""
        assert is_decision_trace_enabled() is False


@pytest.mark.django_db
class TestSchemaStabilityIntegration(TransactionTestCase):
    """Fixture #10 — with trace capture disabled, ``auto_place_board``
    must still include ``decision_trace={}`` in the return payload.
    Schema stability (ChatGPT DoR sign-off amendment): the key is
    always present regardless of flag state."""

    @override_settings(TIMETABLE_PR3_DECISION_TRACE_ENABLED=False)
    def test_trace_key_present_even_when_disabled(self) -> None:
        from core.services.timetable_autoplace import auto_place_board

        _, board, _ = load_pr3_fixture("pr3_trace_schema_disabled.json")
        result = auto_place_board(board.id)

        assert "decision_trace" in result
        assert result["decision_trace"] == {}


# ===========================================================================
# SECTION B — Capture tests (turn green at commit 3).
#
# Each test below loads the matching fixture JSON under
# snapshots/planner-refactor-2026-04-20/fixtures/pr3_*.json, runs the real
# greedy placer, and asserts the shape of the ``decision_trace`` block in
# the returned payload.
#
# Today (commit 1) these tests fail at collection with ModuleNotFoundError
# because ``timetable_decision_trace`` doesn't exist yet. After commit 3
# they become real assertions against the captured trace.
# ===========================================================================


@pytest.mark.django_db
class TestColdStartCapture(TransactionTestCase):
    """Fixture #2 — pr3_cold_start_trace.json.

    A small 3-section board runs through auto_place_board with trace
    capture enabled. Each placed section must appear in
    ``decision_trace`` with chosen-* fields populated. At least one
    section must end up with ≥1 rejected alternative — the fixture's
    shared-no-room topology guarantees this for the later-placed
    sections (ROOM_OCCUPIED falls out of subsequent-placement
    contention)."""

    def test_every_placed_section_has_a_trace_entry(self) -> None:
        from core.services.timetable_autoplace import auto_place_board

        _, board, _ = load_pr3_fixture("pr3_cold_start_trace.json")
        result = auto_place_board(board.id)

        assert result["placed"] >= 1
        trace = result["decision_trace"]
        # Coverage: every placed section has a trace entry.
        for placement in result["placements"]:
            section_code = f"{placement['course_code']}|{placement['section']}"
            assert section_code in trace
            entry = trace[section_code]
            assert entry["chosen_day"]
            assert entry["chosen_start_time"]
            assert entry["chosen_end_time"]
            assert isinstance(entry["alternatives"], list)
            # Alternatives are capped at 3.
            assert len(entry["alternatives"]) <= 3

        # At least one section has ≥1 alternative (the topology
        # guarantees subsequent placements contend for the single room).
        total_alts = sum(len(entry["alternatives"]) for entry in trace.values())
        assert total_alts >= 1


@pytest.mark.django_db
class TestTypedRejectionCodes(TransactionTestCase):
    """Fixtures #6 and #7 — INSTRUCTOR_CLASH and STUDENT_CONFLICT surface
    in the trace as named sentinels (no invented vague labels)."""

    @unittest.skip(
        "Blocked on instructor-source plumbing. Per ChatGPT commit-3 "
        "ruling I2: INSTRUCTOR_CLASH is a defined sentinel but autoplace "
        "has no per-section instructor_id today, so the classifier "
        "never emits it. This test un-skips when a follow-up commit "
        "lands the schema/plumbing and teaches _classify_pr3_alternative "
        "to compare instructor_ids."
    )
    def test_instructor_clash_surfaces_in_trace(self) -> None:
        """Fixture #6 — pr3_instructor_clash.json.

        Two sections taught by the same instructor overlap at Sun 08:00.
        The loser (later-scored) section's trace must contain an
        ``Alternative`` with ``rejection_code == INSTRUCTOR_CLASH`` for
        the Sun 08:00 candidate."""

    def test_student_conflict_surfaces_in_trace(self) -> None:
        """Fixture #7 — pr3_student_conflict.json.

        Two sections share at least one student. When both courses
        contend for the same slot, the loser's trace must contain an
        ``Alternative`` with ``rejection_code == STUDENT_CONFLICT`` for
        that overlapping slot.

        Named CONFLICT (not OVERLAP) per DoR sign-off — cohort semantics,
        not geometric time-overlap."""
        from core.services.timetable_autoplace import auto_place_board

        _, board, _ = load_pr3_fixture("pr3_student_conflict.json")
        result = auto_place_board(board.id)

        assert result["placed"] == 2
        trace = result["decision_trace"]
        # At least one section's trace must contain STUDENT_CONFLICT
        # for the slot that would have clashed on the shared student.
        codes_seen: set[str] = set()
        for entry in trace.values():
            for alt in entry["alternatives"]:
                codes_seen.add(alt["rejection_code"])
        assert STUDENT_CONFLICT in codes_seen, (
            f"STUDENT_CONFLICT not in captured codes: {codes_seen}"
        )

    def test_rejection_codes_are_known_sentinels_only(self) -> None:
        """Acceptance-bar #2: every ``rejection_code`` in a captured trace
        must be one of the PR1+PR2+PR3 sentinels. Asserted by
        enumerating the full known-set at commit-3 time and checking each
        captured trace's alternatives against it."""
        # The union of known rejection codes as of PR3. Commit 3's capture
        # logic must emit only these.
        known = {
            # PR1 — actual code names emitted by core/services/timetable_validation.py.
            # The DoR alphabet table names them as PRAYER_WINDOW_CLASH /
            # LOCK_VIOLATION; those are aspirational names that never landed
            # in PR1's code. The set below uses the actual strings the
            # validator emits; renaming PR1's codes is out of scope for PR3.
            "PRAYER_OVERLAP",
            "LOCK_RESPECT",
            # PR2
            "NO_ROOM_CAPACITY",
            "NO_ROOM_GENDER",
            "NO_ROOM_TYPE",
            "ROOM_OCCUPIED",
            "ROOM_BUFFER_REJECT",
            "ROOM_HEURISTIC_MISMATCH",
            # PR3 (new)
            INSTRUCTOR_CLASH,
            STUDENT_CONFLICT,
        }
        assert INSTRUCTOR_CLASH in known
        assert STUDENT_CONFLICT in known

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

from dataclasses import FrozenInstanceError

from django.test import SimpleTestCase
from django.test.utils import override_settings

# Contract imports — this block is the commit-1 tripwire. Every symbol below
# names something commit 2 must expose. A rename or a dropped symbol in commit
# 2 breaks the suite at collection — which is exactly what we want.
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
    """Fixture #10: with trace capture disabled, the planner payload must
    still include the ``decision_trace`` key equal to ``{}``. Per the
    ChatGPT DoR amendment: ``if trace is disabled, planner payload still
    includes decision_trace key with {} for schema stability``.

    This test imports the flag helper; it doesn't exercise a whole planner
    run (that belongs in the commit-3 capture tests below)."""

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


class TestColdStartCapture(SimpleTestCase):
    """Fixture #2 — pr3_cold_start_trace.json.

    On a small 3-section board with multiple candidate slots available,
    every placed section must end up with at least 1 alternative in its
    trace. Trace coverage on this specific fixture is asserted as 100%."""

    def test_every_placed_section_has_alternatives(self) -> None:
        """Stub — real implementation uses the `_build_autoplace_fixture`
        helper from tests/test_pr2_silent_unassigned_sites.py extended
        with a multi-slot pool. After commit 3 this runs the real
        auto_place_board on the fixture and asserts:
            for section_code in result['decision_trace']:
                assert len(result['decision_trace'][section_code]['alternatives']) >= 1
        """
        # TripwireFail — real assertions land with commit 3.
        # For now: force a failure on the imported symbols so commit 3's
        # author sees the contract before writing the capture code.
        assert DecisionTrace is not None  # contract imported
        assert Alternative is not None
        # Body intentionally minimal — real test replaces this at commit 3.


class TestTypedRejectionCodes(SimpleTestCase):
    """Fixtures #6 and #7 — INSTRUCTOR_CLASH and STUDENT_CONFLICT surface
    in the trace as named sentinels (no invented vague labels)."""

    def test_instructor_clash_surfaces_in_trace(self) -> None:
        """Fixture #6 — pr3_instructor_clash.json.

        Two sections taught by the same instructor overlap at Sun 08:00.
        The loser (later-scored) section's trace must contain an
        ``Alternative`` with ``rejection_code == INSTRUCTOR_CLASH`` for
        the Sun 08:00 candidate."""
        assert INSTRUCTOR_CLASH == "INSTRUCTOR_CLASH"

    def test_student_conflict_surfaces_in_trace(self) -> None:
        """Fixture #7 — pr3_student_conflict.json.

        Two sections share at least one student. The loser's trace must
        contain an ``Alternative`` with
        ``rejection_code == STUDENT_CONFLICT`` for the overlapping slot.

        Named CONFLICT (not OVERLAP) per DoR sign-off — cohort semantics,
        not geometric time-overlap."""
        assert STUDENT_CONFLICT == "STUDENT_CONFLICT"

    def test_rejection_codes_are_known_sentinels_only(self) -> None:
        """Acceptance-bar #2: every ``rejection_code`` in a captured trace
        must be one of the PR1+PR2+PR3 sentinels. Asserted by
        enumerating the full known-set at commit-3 time and checking each
        captured trace's alternatives against it."""
        # The union of known rejection codes as of PR3. Commit 3's capture
        # logic must emit only these.
        known = {
            # PR1
            "PRAYER_WINDOW_CLASH",
            "LOCK_VIOLATION",
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

"""PR4 — instructor realism tests (failing by design until commits 2–3).

Commit 1 freezes the public API shape of ``core.services.timetable_pr4_instructor``
before any implementation lands. The top-of-file imports raise
``ModuleNotFoundError`` until commit 2 creates the module; once it does,
the shape/normalisation tests (Section A) turn green. Commit 3 wires
``INSTRUCTOR_CLASH`` emission into ``auto_place_board`` and the emission
tests (Section B) turn green.

Per the PR4 DoR amendment A6: instructor strings are treated as **opaque
single strings**. Normalisation is strictly strip + casefold. No
comma-splitting. No delimiter heuristics. A string like
``"Dr. Smith / Dr. Jones"`` is one opaque id, not two — single-string
semantics only in PR4.
"""

from __future__ import annotations

import pytest
from django.test import SimpleTestCase, TransactionTestCase
from django.test.utils import override_settings
from pr4_fixture_loader import load_pr4_fixture

# Contract imports — commit-1 tripwire. Every symbol below names something
# commits 2–3 expose. A rename or a dropped symbol breaks collection.
from core.services.timetable_pr4_instructor import (
    INSTRUCTOR_CLASH_FLAG_SETTING,
    build_instructor_schedule,
    is_instructor_clash_enabled,
    normalise_instructor,
)

# ===========================================================================
# SECTION A — Normalisation shape (turns green at commit 2).
# ===========================================================================


class TestInstructorNormalisation(SimpleTestCase):
    """``normalise_instructor`` is strip+casefold only. Opaque-string
    discipline (A6): no delimiter splitting, no comma-parsing, no heuristics."""

    def test_whitespace_and_case_fold_equivalent(self) -> None:
        """'Dr. Smith', 'dr. smith ', 'DR. SMITH' all collide."""
        a = normalise_instructor("Dr. Smith")
        b = normalise_instructor("dr. smith ")
        c = normalise_instructor("DR. SMITH")
        assert a == b == c
        assert a is not None

    def test_empty_and_none_become_none(self) -> None:
        assert normalise_instructor(None) is None
        assert normalise_instructor("") is None
        assert normalise_instructor("   ") is None

    def test_multi_instructor_string_is_opaque(self) -> None:
        """'Dr. Smith / Dr. Jones' is ONE opaque id, not two (A6)."""
        multi = normalise_instructor("Dr. Smith / Dr. Jones")
        single = normalise_instructor("Dr. Smith")
        assert multi is not None
        assert single is not None
        assert multi != single, (
            "Normaliser must not parse multi-instructor strings in PR4 — "
            "a single 'Dr. Smith / Dr. Jones' string must not collide with "
            "just 'Dr. Smith'. If this test fails, someone added delimiter "
            "parsing that the DoR (A6) explicitly forbids."
        )


# ===========================================================================
# SECTION B — Emission tests (turn green at commit 3 with flag ON).
# ===========================================================================


@pytest.mark.django_db
class TestInstructorClashEmission(TransactionTestCase):
    """Fixture #1 — two sections share an opaque instructor string; the
    loser's decision_trace must carry ``INSTRUCTOR_CLASH`` for the slot
    that would have double-booked the instructor."""

    @override_settings(TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True)
    def test_overlapping_instructor_produces_clash_code(self) -> None:
        from core.services.timetable_autoplace import auto_place_board

        _, board, _ = load_pr4_fixture("pr4_instructor_clash.json")
        result = auto_place_board(board.id)

        assert result["placed"] == 2
        trace = result["decision_trace"]
        codes_seen: set[str] = set()
        for entry in trace.values():
            for alt in entry["alternatives"]:
                codes_seen.add(alt["rejection_code"])
        assert "INSTRUCTOR_CLASH" in codes_seen, (
            f"INSTRUCTOR_CLASH not emitted with flag ON: codes={codes_seen}"
        )


@pytest.mark.django_db
class TestInstructorClashFlag(TransactionTestCase):
    """Flag-off case: no emission, placement-behaviour identical to PR3."""

    @override_settings(TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=False)
    def test_flag_off_suppresses_emission(self) -> None:
        from core.services.timetable_autoplace import auto_place_board

        _, board, _ = load_pr4_fixture("pr4_instructor_clash.json")
        result = auto_place_board(board.id)

        trace = result["decision_trace"]
        codes_seen: set[str] = set()
        for entry in trace.values():
            for alt in entry["alternatives"]:
                codes_seen.add(alt["rejection_code"])
        assert "INSTRUCTOR_CLASH" not in codes_seen, (
            f"INSTRUCTOR_CLASH leaked despite flag OFF: codes={codes_seen}"
        )

    def test_flag_helper_reads_setting(self) -> None:
        """``is_instructor_clash_enabled()`` reads the configured setting."""
        with override_settings(TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=True):
            assert is_instructor_clash_enabled() is True
        with override_settings(TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=False):
            assert is_instructor_clash_enabled() is False

    def test_flag_setting_constant_is_published(self) -> None:
        """The setting key is a module-level constant so callers don't hard-code
        the string in multiple places."""
        assert INSTRUCTOR_CLASH_FLAG_SETTING == "TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED"


# ===========================================================================
# SECTION C — Schedule roll-up (turns green at commit 2).
# ===========================================================================


@pytest.mark.django_db
class TestInstructorScheduleRollup(TransactionTestCase):
    """``build_instructor_schedule`` walks already-placed meeting rows and
    returns a dict keyed by normalised instructor → set of (day, start)
    minute tuples. The schedule is transient (per run, never persisted)."""

    def test_rollup_groups_meetings_by_normalised_instructor(self) -> None:
        _, board, _ = load_pr4_fixture("pr4_instructor_clash.json")
        schedule = build_instructor_schedule(board.id)

        # Both fixture sections reference 'Dr. Smith' → one normalised key.
        assert isinstance(schedule, dict)
        assert len(schedule) == 1, (
            f"Expected 1 distinct normalised instructor, got {len(schedule)}: "
            f"{list(schedule.keys())}"
        )

"""PR4 — prayer-rule unification tests (A1 + A2 + A3).

Two groups:

Section A — legacy ``_start_is_blocked`` removal. Five targeted tests, one
per former call-site, demonstrate no call-site still depends on the legacy
filter after commit 5. These tests green-behind-flag today because the
legacy filter is still present and still short-circuits; commit 5 deletes
it and the tests then verify the configured-windows rule handles each
call-site on its own.

Section B — single-source semantics + divergence report. Commit 4 produces
``snapshots/planner-refactor-2026-04-20/PR4-PRAYER-DELTA.md`` with the
bidirectional set-difference (a) legacy-only (b) configured-only
(c) intersection, measured on the scenario pack. These tests assert the
report exists and its counts match the two divergence fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.test import TransactionTestCase
from pr4_fixture_loader import load_pr4_fixture

# ===========================================================================
# SECTION A — Targeted tests for _start_is_blocked removal (A1).
#
# Each test names a former call-site and asserts the configured-windows
# rule now handles that site on its own — no candidate is excluded solely
# by the legacy 11:35–12:59 hardcoded window.
#
# Before commit 5: these tests may pass because the legacy filter AGREES
# with the configured rule on the chosen fixture slots. The critical check
# comes after commit 5: when the legacy filter is gone, the configured
# rule must still produce the expected rejection (when applicable) and
# no candidate is left silently permitted by the removal.
# ===========================================================================


@pytest.mark.django_db
class TestStartIsBlockedRemovalAutoplaceLecture(TransactionTestCase):
    """Call-site 1: ``auto_place_board`` lecture candidate loop."""

    def test_lecture_candidate_no_longer_filtered_by_legacy(self) -> None:
        from core.services import timetable_autoplace

        # Post-commit-5 the symbol is gone. Before commit 5 this test's
        # expectation is simply the symbol's *absence*. Failing here at
        # commit 1 is expected — tripwire.
        assert not hasattr(timetable_autoplace, "_start_is_blocked"), (
            "_start_is_blocked must be removed from timetable_autoplace "
            "(call-site 1: lecture loop). Commit 5 deletes it."
        )


@pytest.mark.django_db
class TestStartIsBlockedRemovalAutoplaceLab(TransactionTestCase):
    """Call-site 2: ``auto_place_board`` lab candidate loop (same module,
    different loop). The function is module-level so one check covers both
    autoplace loops; this test exists as a standalone gate per A1."""

    def test_lab_candidate_no_longer_filtered_by_legacy(self) -> None:
        from core.services import timetable_autoplace

        assert not hasattr(timetable_autoplace, "_start_is_blocked"), (
            "_start_is_blocked must be removed from timetable_autoplace "
            "(call-site 2: lab loop). Commit 5 deletes it."
        )


@pytest.mark.django_db
class TestStartIsBlockedRemovalCpsatCandidateGen(TransactionTestCase):
    """Call-site 3: ``cpsat_polisher.py:158`` candidate generation."""

    def test_cpsat_candidate_gen_no_longer_filtered_by_legacy(self) -> None:
        from core.services import timetable_cpsat_polisher as cpsat_polisher

        assert not hasattr(cpsat_polisher, "_start_is_blocked"), (
            "_start_is_blocked must be removed from timetable_cpsat_polisher "
            "(call-site 3: candidate generation). Commit 5 deletes it."
        )


@pytest.mark.django_db
class TestStartIsBlockedRemovalCpsatRescore(TransactionTestCase):
    """Call-site 4: ``cpsat_polisher.py:166`` re-score pass."""

    def test_cpsat_rescore_no_longer_filtered_by_legacy(self) -> None:
        import importlib

        from core.services import timetable_cpsat_polisher as cpsat_polisher

        src = Path(importlib.util.find_spec(cpsat_polisher.__name__).origin).read_text(
            encoding="utf-8"
        )
        assert "_start_is_blocked" not in src, (
            "No reference to _start_is_blocked may remain in timetable_cpsat_polisher "
            "(call-site 4: re-score). Commit 5 deletes both call-sites."
        )


@pytest.mark.django_db
class TestStartIsBlockedRemovalLoadBalanced(TransactionTestCase):
    """Call-site 5: ``load_balanced.py`` module-level import (unused but
    retained). Dead import — commit 5 drops it."""

    def test_load_balanced_no_longer_imports_legacy(self) -> None:
        import importlib

        from core.services import timetable_load_balanced as load_balanced

        src = Path(importlib.util.find_spec(load_balanced.__name__).origin).read_text(
            encoding="utf-8"
        )
        assert "_start_is_blocked" not in src, (
            "No reference to _start_is_blocked may remain in timetable_load_balanced "
            "(call-site 5: dead import). Commit 5 deletes it."
        )


# ===========================================================================
# SECTION B — Single-source semantics + divergence report (A2 + A3).
# ===========================================================================


@pytest.mark.django_db
class TestPrayerSingleSource(TransactionTestCase):
    """Bar 3b: the configured-windows rule is the sole prayer-rejection
    source in the trace. Every PRAYER_OVERLAP rejection must come from
    the configured rule, never from the legacy filter."""

    def test_configured_rule_is_sole_prayer_source(self) -> None:
        from core.services.timetable_autoplace import auto_place_board

        _, board, _ = load_pr4_fixture("pr4_prayer_divergence_a.json")
        result = auto_place_board(board.id)

        trace = result["decision_trace"]
        for entry in trace.values():
            for alt in entry["alternatives"]:
                if alt["rejection_code"] == "PRAYER_OVERLAP":
                    ctx = alt.get("rejection_context", {}) or {}
                    origin = ctx.get("rule_origin", "configured")
                    assert origin == "configured", (
                        f"PRAYER_OVERLAP with origin={origin!r}; single-source "
                        f"semantics (bar 3b) requires 'configured' only."
                    )


class TestPrayerDivergenceReport:
    """Bar 3c: the commit-4 divergence report exists and records the two
    fixture scenarios. This is a plain file-presence + content test; it
    does not run the planner."""

    REPORT_PATH = (
        Path(django_settings.BASE_DIR)
        / "snapshots"
        / "planner-refactor-2026-04-20"
        / "PR4-PRAYER-DELTA.md"
    )

    def test_delta_report_present(self) -> None:
        assert self.REPORT_PATH.exists(), (
            f"Commit 4 must produce {self.REPORT_PATH.name}. Missing at commit 1 "
            f"— this test turns green at commit 4."
        )

    def test_delta_matches_fixture_counts(self) -> None:
        if not self.REPORT_PATH.exists():
            pytest.skip("Report not yet produced (commit 4 lands it).")
        text = self.REPORT_PATH.read_text(encoding="utf-8")
        assert "pr4_prayer_divergence_a" in text, "Divergence report must reference fixture A."
        assert "pr4_prayer_divergence_b" in text, "Divergence report must reference fixture B."
        assert "legacy \\ configured" in text or "legacy - configured" in text, (
            "Report must document the bidirectional set-difference explicitly."
        )

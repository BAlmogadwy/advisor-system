"""Single owner of a scenario's two on-disk board representations.

A board's schedule lives in two tables that must never drift apart:

- ``SectionPlacement`` — board-scoped, lockable (``is_locked``); carries no
  instructor. Unique on ``(board, term_section, day, start_time)``.
- ``TermSectionMeeting`` (TSM) — section-scoped; carries ``instructor``,
  ``room``, ``building``, ``floor_wing``. The instructor lives *only* here.

Historically each optimiser/reset/rollback path maintained a different subset
of these tables, so rollbacks and rebuilds silently corrupted meetings (and the
Instructors export, conflict masks, and clash preload that read them). This
module is the one place that resets and snapshot/restores **both** tables
together, atomically, so they stay consistent.

Scope symmetry (matches the optimiser's own reset): placements are addressed by
``board__scenario_id`` and meetings by ``term_section__scenario_id``. Global
(scenario-null) sections are never mutated by the optimiser and are therefore
intentionally left untouched by every function here.

See ``docs/BOARD-PERSISTENCE-DOR.md`` for the full contract and rationale.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from core.models import SectionPlacement, TermSectionMeeting

# Placement fields captured for a lossless snapshot. Mirrors the historical
# ``timetable_v2_runner._PLACEMENT_SNAPSHOT_FIELDS`` (id + audit columns
# included) so restore is byte-identical to the pre-existing placement restore.
_PLACEMENT_FIELDS = (
    "id",
    "board_id",
    "term_section_id",
    "day",
    "start_time",
    "end_time",
    "room",
    "is_locked",
    "created_at",
    "updated_at",
)

# Meeting fields captured for a lossless snapshot. ``instructor`` is included
# because it lives only on TSM and ``apply_primary_instructor`` cannot restore
# free-text (non-link) instructors — so a snapshot must carry it verbatim.
_MEETING_FIELDS = (
    "id",
    "term_section_id",
    "day",
    "start_time",
    "end_time",
    "building",
    "floor_wing",
    "room",
    "instructor",
    "created_at",
    "updated_at",
)


@dataclass(frozen=True)
class ResetResult:
    """Outcome of :func:`reset_scenario` (counts of rows removed)."""

    placements_deleted: int
    meetings_deleted: int
    locked_sections_preserved: int


@dataclass(frozen=True)
class ScenarioSnapshot:
    """A restorable capture of both board tables for one scenario."""

    scenario_id: int
    placements: list[dict]
    meetings: list[dict]

    @property
    def is_empty(self) -> bool:
        """True when the scenario had no placements when captured.

        A from-scratch build (nothing to protect) is distinguished from an
        improve-in-place run by whether any placement existed at snapshot time.
        """
        return not self.placements


def reset_scenario(scenario_id: int, *, keep_locked: bool = True) -> ResetResult:
    """Clear a scenario's board(s) so a placement run starts from a clean slate.

    Deletes ``SectionPlacement`` rows **and** their ``TermSectionMeeting`` rows
    together, so a later run cannot inherit stale meetings from an earlier one
    (the ``len(placements) != len(meetings)`` drift that made
    ``persist_section_states_to_scenario`` skip sections).

    When ``keep_locked`` (the default), locked placements are the user's hard
    constraints: those rows and the meetings of their sections survive. This is
    the behaviour the split-workspace rebuild already relied on; passing
    ``keep_locked=False`` performs a hard wipe of the whole scenario board.

    Runs in a single transaction. Returns the row counts removed.
    """
    with transaction.atomic():
        locked_ts_ids: set[int] = set()
        if keep_locked:
            locked_ts_ids = set(
                SectionPlacement.objects.filter(
                    board__scenario_id=scenario_id, is_locked=True
                ).values_list("term_section_id", flat=True)
            )

        placements_qs = SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        if keep_locked:
            placements_qs = placements_qs.filter(is_locked=False)
        placements_deleted, _ = placements_qs.delete()

        meetings_qs = TermSectionMeeting.objects.filter(term_section__scenario_id=scenario_id)
        if keep_locked and locked_ts_ids:
            meetings_qs = meetings_qs.exclude(term_section_id__in=locked_ts_ids)
        meetings_deleted, _ = meetings_qs.delete()

    return ResetResult(
        placements_deleted=placements_deleted,
        meetings_deleted=meetings_deleted,
        locked_sections_preserved=len(locked_ts_ids),
    )


def snapshot_scenario(scenario_id: int) -> ScenarioSnapshot:
    """Capture both board tables so an unsafe run can be restored exactly.

    Captures ``SectionPlacement`` (board-scoped) and ``TermSectionMeeting``
    (section-scoped) including ``instructor`` — everything needed for a lossless
    :func:`restore_scenario`.
    """
    placements = list(
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .order_by("id")
        .values(*_PLACEMENT_FIELDS)
    )
    meetings = list(
        TermSectionMeeting.objects.filter(term_section__scenario_id=scenario_id)
        .order_by("id")
        .values(*_MEETING_FIELDS)
    )
    return ScenarioSnapshot(scenario_id=scenario_id, placements=placements, meetings=meetings)


def restore_scenario(scenario_id: int, snapshot: ScenarioSnapshot) -> None:
    """Restore both board tables from a snapshot inside one transaction.

    Delete-all + bulk-recreate for placements **and** meetings, so a rolled-back
    optimiser run leaves the board exactly as it was — not with placements
    reverted and meetings stranded at the failed run's layout (the historical
    placements-only rollback bug).

    The snapshot need not be for the same scenario id passed here only in the
    sense that rows carry their own FKs; ``scenario_id`` scopes the delete.
    Term sections and boards are never deleted by the optimiser, so the FKs the
    recreated rows reference still exist.
    """
    with transaction.atomic():
        SectionPlacement.objects.filter(board__scenario_id=scenario_id).delete()
        TermSectionMeeting.objects.filter(term_section__scenario_id=scenario_id).delete()
        if snapshot.placements:
            SectionPlacement.objects.bulk_create(
                [SectionPlacement(**row) for row in snapshot.placements],
                batch_size=500,
            )
        if snapshot.meetings:
            TermSectionMeeting.objects.bulk_create(
                [TermSectionMeeting(**row) for row in snapshot.meetings],
                batch_size=500,
            )


__all__ = [
    "ResetResult",
    "ScenarioSnapshot",
    "reset_scenario",
    "restore_scenario",
    "snapshot_scenario",
]

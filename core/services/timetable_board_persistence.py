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
from core.services.board_ownership import assert_rebuild_ownership, external_shared_section_ids

# Placement fields captured for restore. ``id`` is kept so identity is preserved
# across a snapshot/restore. ``created_at``/``updated_at`` are deliberately
# omitted: they are ``auto_now_add``/``auto_now`` columns that ``bulk_create``
# re-stamps regardless of any value we pass, and nothing reads them — carrying
# them would only imply a fidelity the DB does not honour.
_PLACEMENT_FIELDS = (
    "id",
    "board_id",
    "term_section_id",
    "day",
    "start_time",
    "end_time",
    "room",
    "is_locked",
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

# One-teacher engine rows captured for restore (``section_teacher_assignment``).
_SECTION_TEACHER_FIELDS = (
    "id",
    "scenario_id",
    "course_key",
    "section",
    "instructor_id",
    "role",
    "source",
)


@dataclass(frozen=True)
class ResetResult:
    """Outcome of :func:`reset_scenario` (counts of rows removed)."""

    placements_deleted: int
    meetings_deleted: int
    locked_sections_preserved: int


@dataclass(frozen=True)
class MeetingSyncResult:
    """Outcome of :func:`sync_meetings_from_placements`."""

    sections_synced: int
    meetings_written: int
    meetings_deleted: int


@dataclass(frozen=True)
class ScenarioSnapshot:
    """A restorable capture of both board tables for one scenario.

    ``section_teachers`` holds the scenario's engine ``SectionInstructor`` rows
    (one teacher per section) and is None whenever that rule is off, in which
    case restore never touches the table.
    """

    scenario_id: int
    placements: list[dict]
    meetings: list[dict]
    section_teachers: list[dict] | None = None

    @property
    def is_empty(self) -> bool:
        """True when the scenario had no placements when captured.

        A from-scratch build (nothing to protect) is distinguished from an
        improve-in-place run by whether any placement existed at snapshot time.
        """
        return not self.placements


def external_shared_snapshot(scenario_id: int) -> tuple[list[dict], list[dict]]:
    section_ids = external_shared_section_ids(scenario_id)
    return (
        list(
            SectionPlacement.objects.filter(term_section_id__in=section_ids).order_by("pk").values()
        ),
        list(
            TermSectionMeeting.objects.filter(term_section_id__in=section_ids)
            .order_by("pk")
            .values()
        ),
    )


def locked_physical_section_ids(scenario_id: int) -> set[int]:
    """Physical sections whose complete time pattern is anchored by any lock.

    The caller chooses whether lock enforcement applies. Reset's explicit
    ``keep_locked`` contract is independent of optional solver feature flags.
    Companion rows retain their original flags, including on other owned boards.
    """
    return set(
        SectionPlacement.objects.filter(board__scenario_id=scenario_id, is_locked=True).values_list(
            "term_section_id", flat=True
        )
    )


def reset_scenario(scenario_id: int, *, keep_locked: bool = True) -> ResetResult:
    """Clear a scenario's board(s) so a placement run starts from a clean slate.

    Deletes ``SectionPlacement`` rows **and** their ``TermSectionMeeting`` rows
    together, so a later run cannot inherit stale meetings from an earlier one
    (the ``len(placements) != len(meetings)`` drift that made
    ``persist_section_states_to_scenario`` skip sections).

    When ``keep_locked`` (the default), any locked meeting anchors its complete
    physical section time pattern, including unlocked companion meetings and
    copies on other boards owned by this scenario. Keep their canonical meetings
    and original row flags too. A partially retained section would be counted as
    already placed and its missing contact minutes would never be regenerated.
    Unrelated sections remain rebuildable. ``keep_locked=False`` clears both
    representations, including locked sections.

    Runs in a single transaction. Returns the row counts removed.
    """
    with transaction.atomic():
        assert_rebuild_ownership(scenario_id)
        locked_ts_ids = locked_physical_section_ids(scenario_id) if keep_locked else set()
        placements_deleted, _ = (
            SectionPlacement.objects.filter(board__scenario_id=scenario_id)
            .exclude(term_section_id__in=locked_ts_ids)
            .delete()
        )
        # Retain only canonical rows backed by a protected physical meeting.
        # A stale orphan must not become fixed instructor occupancy merely
        # because another meeting in its section is locked.
        retained_slots = set(
            SectionPlacement.objects.filter(
                board__scenario_id=scenario_id, term_section_id__in=locked_ts_ids
            ).values_list("term_section_id", "day", "start_time")
        )
        stale_meeting_ids = [
            pk
            for pk, section_id, day, start in TermSectionMeeting.objects.filter(
                term_section__scenario_id=scenario_id
            ).values_list("pk", "term_section_id", "day", "start_time")
            if (section_id, day, start) not in retained_slots
        ]
        meetings_deleted, _ = TermSectionMeeting.objects.filter(pk__in=stale_meeting_ids).delete()

    return ResetResult(
        placements_deleted=placements_deleted,
        meetings_deleted=meetings_deleted,
        locked_sections_preserved=len(locked_ts_ids),
    )


def snapshot_scenario(scenario_id: int) -> ScenarioSnapshot:
    """Capture both board tables so an unsafe run can be restored.

    Captures ``SectionPlacement`` (board-scoped) and ``TermSectionMeeting``
    (section-scoped) including ``instructor`` — everything needed to restore the
    schedule. The placements' ``created_at``/``updated_at`` audit columns are not
    captured (see ``_PLACEMENT_FIELDS``); every other field round-trips exactly.

    With one teacher per section it also captures EVERY engine row in the
    scenario's scope, unfiltered — exactly as placements are captured unfiltered;
    :func:`restore_scenario` filters at restore time.
    """
    from core.services.timetable_flags import is_one_teacher_active

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
    section_teachers = None
    if is_one_teacher_active():
        from core.models import SectionInstructor
        from core.services.section_teacher_assignment import SOURCE_ENGINE

        section_teachers = list(
            SectionInstructor.objects.filter(scenario_id=scenario_id, source=SOURCE_ENGINE)
            .order_by("id")
            .values(*_SECTION_TEACHER_FIELDS)
        )
    return ScenarioSnapshot(
        scenario_id=scenario_id,
        placements=placements,
        meetings=meetings,
        section_teachers=section_teachers,
    )


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

    When the snapshot carries one-teacher engine rows they are restored in the
    same transaction and with the same filter as the placements: the frozen
    keys are computed NOW (``teacher_frozen_section_ids``), a frozen section's
    engine row is neither deleted nor recreated, and a section that has a pin
    by now keeps it — so a restore can never duplicate a primary.
    """
    frozen = external_shared_section_ids(scenario_id)
    teacher_frozen_keys = (
        _teacher_frozen_row_keys(scenario_id) if snapshot.section_teachers is not None else set()
    )
    with transaction.atomic():
        SectionPlacement.objects.filter(board__scenario_id=scenario_id).exclude(
            term_section_id__in=frozen
        ).delete()
        TermSectionMeeting.objects.filter(term_section__scenario_id=scenario_id).exclude(
            term_section_id__in=frozen
        ).delete()
        if snapshot.placements:
            SectionPlacement.objects.bulk_create(
                [
                    SectionPlacement(**row)
                    for row in snapshot.placements
                    if row["term_section_id"] not in frozen
                ],
                batch_size=500,
            )
        if snapshot.meetings:
            TermSectionMeeting.objects.bulk_create(
                [
                    TermSectionMeeting(**row)
                    for row in snapshot.meetings
                    if row["term_section_id"] not in frozen
                ],
                batch_size=500,
            )
        if snapshot.section_teachers is not None:
            _restore_section_teachers(scenario_id, snapshot.section_teachers, teacher_frozen_keys)


def _row_key(course_key: object, section: object) -> tuple[str, str]:
    return (str(course_key or "").strip().upper(), str(section or "").strip().upper())


def _teacher_frozen_row_keys(scenario_id: int) -> set[tuple[str, str]]:
    from core.models import TermSection
    from core.services.board_ownership import teacher_frozen_section_ids

    return {
        _row_key(key or code, label)
        for key, code, label in TermSection.objects.filter(
            pk__in=teacher_frozen_section_ids(scenario_id)
        ).values_list("course_key", "course_code", "section")
    }


def _restore_section_teachers(
    scenario_id: int, rows: list[dict], frozen_keys: set[tuple[str, str]]
) -> None:
    """Put the snapshot's engine rows back, leaving frozen sections and pins be."""
    from core.models import SectionInstructor
    from core.services.section_teacher_assignment import (
        ROLE_PRIMARY,
        SOURCE_ENGINE,
        invalidate_scope,
    )

    current = SectionInstructor.objects.filter(scenario_id=scenario_id).values_list(
        "pk", "course_key", "section", "role", "source"
    )
    doomed, pinned = [], set()
    for pk, course_key, label, role, source in current:
        key = _row_key(course_key, label)
        if source == SOURCE_ENGINE and key not in frozen_keys:
            doomed.append(pk)
        elif source != SOURCE_ENGINE and role == ROLE_PRIMARY:
            pinned.add(key)
    if doomed:
        SectionInstructor.objects.filter(pk__in=doomed).delete()
    keep = [
        SectionInstructor(**row)
        for row in rows
        if _row_key(row["course_key"], row["section"]) not in frozen_keys
        and _row_key(row["course_key"], row["section"]) not in pinned
    ]
    if keep:
        SectionInstructor.objects.bulk_create(keep, batch_size=500)
    invalidate_scope(scenario_id)


def sync_meetings_from_placements(scenario_id: int) -> MeetingSyncResult:
    """Project the finalised placements back onto each section's meeting rows.

    The optimiser's ``persist_section_states_to_scenario`` moves placements and
    ``assign_rooms_to_board`` assigns their rooms — **neither writes TSM** — so
    after an optimise run the meeting rows are stale (wrong day/time/room, and
    the Instructors export / conflict masks read TSM). This rewrites each
    scenario-owned section's meetings to the distinct ``(day, start, end, room)``
    tuples of its placements, then re-fans the primary ``CourseInstructor`` name.

    - **Dedup** collapses a cross-board shared session (one section placed on
      several boards at the same slot) into the single row the TSM unique
      constraint ``(term_section, day, start, end, room, instructor)`` allows —
      which also clears the duplicate-meeting drift behind phantom clashes.
    - **Instructor** is carried forward from the section's existing meetings so
      free-text (non-link) instructors survive, then ``reconcile_scenario_instructors``
      overrides with the link primary where one exists (the display/clash cache
      + Instructors-export invariant). This applies one instructor per section
      (matching ``apply_primary_instructor``'s uniform write); a section with
      per-meeting-distinct free-text instructors would be flattened to the
      first — acceptable under the system's one-instructor-per-section model.
    - Global (scenario-null) sections are not owned by the scenario and are left
      untouched, matching the rest of this module.

    Call once after rooming and before the instructor cap/clash/compaction
    repairs, so those passes relocate on a board whose meetings already match
    its placements. Runs in one transaction.
    """
    from core.models import TimetableScenario
    from core.services.course_instructor_assignment import reconcile_scenario_instructors

    frozen = external_shared_section_ids(scenario_id)
    placement_rows = (
        SectionPlacement.objects.filter(
            board__scenario_id=scenario_id,
            term_section__scenario_id=scenario_id,
        )
        .exclude(term_section_id__in=frozen)
        .values("term_section_id", "day", "start_time", "end_time", "room")
    )

    by_section: dict[int, list[dict]] = {}
    for row in placement_rows:
        by_section.setdefault(row["term_section_id"], []).append(row)

    sections_synced = 0
    meetings_written = 0
    meetings_deleted = 0

    with transaction.atomic():
        for ts_id, rows in by_section.items():
            # Carry forward the section's instructor / location metadata (the
            # placement carries none of these); reconcile re-fans link primaries.
            existing = list(
                TermSectionMeeting.objects.filter(term_section_id=ts_id).values(
                    "instructor", "building", "floor_wing"
                )
            )
            instructor = next((e["instructor"] for e in existing if e["instructor"]), "")
            building = next((e["building"] for e in existing if e["building"]), "")
            floor_wing = next((e["floor_wing"] for e in existing if e["floor_wing"]), "")

            seen: set[tuple[str, str, str, str]] = set()
            new_rows: list[TermSectionMeeting] = []
            for row in rows:
                key = (row["day"], row["start_time"], row["end_time"], row["room"] or "")
                if key in seen:
                    continue
                seen.add(key)
                new_rows.append(
                    TermSectionMeeting(
                        term_section_id=ts_id,
                        day=row["day"],
                        start_time=row["start_time"],
                        end_time=row["end_time"],
                        room=row["room"] or "",
                        instructor=instructor,
                        building=building,
                        floor_wing=floor_wing,
                    )
                )

            deleted, _ = TermSectionMeeting.objects.filter(term_section_id=ts_id).delete()
            meetings_deleted += deleted
            if new_rows:
                TermSectionMeeting.objects.bulk_create(new_rows, batch_size=500)
                meetings_written += len(new_rows)
            sections_synced += 1

        scenario = TimetableScenario.objects.filter(id=scenario_id).first()
        if scenario is not None:
            reconcile_scenario_instructors(scenario, excluded_section_ids=frozen)

    return MeetingSyncResult(
        sections_synced=sections_synced,
        meetings_written=meetings_written,
        meetings_deleted=meetings_deleted,
    )


__all__ = [
    "MeetingSyncResult",
    "ResetResult",
    "ScenarioSnapshot",
    "locked_physical_section_ids",
    "reset_scenario",
    "restore_scenario",
    "snapshot_scenario",
    "sync_meetings_from_placements",
]

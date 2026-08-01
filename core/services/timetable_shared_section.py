"""Shared (multi-board) section coherence — detect and canonicalise divergence.

A ``TermSection`` is one logical class. When it is taken by students in more than
one term it is shown on more than one ``DeliveryBoard`` — a GSE/FE university
elective, say, on the term-5 board *and* the term-7 board. But ``SectionPlacement``
is per board (unique on ``board, term_section, day, start_time``) and nothing keeps
those boards' rows in agreement, so the auto-placer schedules the section
*independently* on each board. Live data: 48 shared sections across 25 scenarios,
**22 of them schedule-divergent** — the same class placed at a different day/time
on each board, which is physically impossible (one class cannot meet Tuesday on one
board and Wednesday on another).

Everything else in the system already treats a section as ONE logical entity —
``TermSectionMeeting`` (TSM) is keyed by ``term_section`` (not board),
``SectionState`` in the optimiser is keyed by ``term_section``, and the canonical
constraint engine is board-agnostic. The per-board ``SectionPlacement`` is the
*only* place identity splits, so the fix is to make those rows cohere.

**What counts as divergence.** ``sync_meetings_from_placements`` collapses a
shared session into one TSM row only when the boards agree on the full tuple
``(day, start, end, room)`` — that is TSM's own unique key. So a section whose
boards agree on the time but sit in *different rooms* is equally broken: it
survives the dedup as two TSM rows, one class apparently meeting twice in two
rooms. This module therefore treats **room as part of the identity** and reports
``schedule_divergent`` (day/time differs) and ``room_divergent`` (time agrees,
room differs) separately. Both are rewritten by canonicalisation.

Consequences the divergence hides, which canonicalisation exposes honestly:

* ``TermSectionMeeting`` gains phantom rows when the boards disagree, and every
  TSM reader (exports, Group Availability, the conflict detector's instructor
  lookup) then shows the class meeting more times than it does;
* a divergent section masks a real student clash — pretending the class meets at a
  different time for one cohort — so making it coherent can surface a genuine
  conflict that was previously invisible. That is correct: the conflict is real.

**Not delivered by this module:** instructor compaction still refuses these
scenarios. Its guard rejects on *sharedness* (any section on >1 board), not on
divergence, so a coherent shared section is refused exactly as a divergent one is
(``timetable_instructor_compaction.py``). Canonicalisation is a **prerequisite**
for unblocking compaction, not the unblock itself — that needs the guard relaxed
to reject only incoherent shapes *and* logical-replica de-duplication when
building ``SectionState``, so the double-count it fears cannot happen.

This module is **read-only by default**. ``analyze_shared_sections`` never writes;
``canonicalise_shared_sections`` writes only when called with ``apply=True``, does
so transactionally, updates rows **in place** where it can (so placement ids and
the ``SET_NULL`` repair-run references that point at them survive), and rebuilds
TSM in the same transaction. Coherence is the goal — not optimality; re-run the
optimiser afterwards to improve the now-honest board.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from django.db import transaction

from core.models import DeliveryBoard, SectionPlacement, TimetableScenario


def _hhmm(value: object) -> str:
    return value.strftime("%H:%M") if hasattr(value, "strftime") else str(value)[:5]


@dataclass(frozen=True)
class MeetingSlot:
    """One meeting occurrence, board-independent."""

    day: str
    start: str
    end: str
    room: str

    @property
    def time_key(self) -> tuple[str, str, str]:
        """Schedule identity — when the class meets."""
        return (self.day, self.start, self.end)

    @property
    def full_key(self) -> tuple[str, str, str, str]:
        """Full identity, matching TSM's dedup key ``(day, start, end, room)``."""
        return (self.day, self.start, self.end, self.room)

    def as_dict(self) -> dict:
        return {"day": self.day, "start": self.start, "end": self.end, "room": self.room}


@dataclass
class SharedSection:
    """A term-section that appears on more than one board in a scenario."""

    term_section_id: int
    course_code: str
    section: str
    board_ids: list[int]
    # board_id -> the meeting slots that board currently shows.
    schedule_by_board: dict[int, list[MeetingSlot]]
    schedule_divergent: bool
    room_divergent: bool
    canonical_board_id: int
    canonical_schedule: list[MeetingSlot]

    @property
    def divergent(self) -> bool:
        """Any incoherence at all — either the time or the room disagrees."""
        return self.schedule_divergent or self.room_divergent

    def as_dict(self) -> dict:
        return {
            "term_section_id": self.term_section_id,
            "course": f"{self.course_code}|{self.section}",
            "board_ids": self.board_ids,
            "divergent": self.divergent,
            "schedule_divergent": self.schedule_divergent,
            "room_divergent": self.room_divergent,
            "canonical_board_id": self.canonical_board_id,
            "canonical_schedule": [s.as_dict() for s in self.canonical_schedule],
            "schedule_by_board": {
                bid: [s.as_dict() for s in slots] for bid, slots in self.schedule_by_board.items()
            },
        }


@dataclass
class SharedSectionReport:
    scenario_id: int
    shared_count: int
    divergent_count: int
    schedule_divergent_count: int = 0
    room_divergent_count: int = 0
    shared_sections: list[SharedSection] = field(default_factory=list)
    # Populated only by canonicalise_shared_sections.
    applied: bool = False
    sections_canonicalised: int = 0
    sections_skipped_locked: int = 0
    placements_rewritten: int = 0
    remaining_divergent_count: int | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "shared_count": self.shared_count,
            "divergent_count": self.divergent_count,
            "schedule_divergent_count": self.schedule_divergent_count,
            "room_divergent_count": self.room_divergent_count,
            "applied": self.applied,
            "sections_canonicalised": self.sections_canonicalised,
            "sections_skipped_locked": self.sections_skipped_locked,
            "placements_rewritten": self.placements_rewritten,
            "remaining_divergent_count": self.remaining_divergent_count,
            "shared_sections": [s.as_dict() for s in self.shared_sections],
            "notes": self.notes,
        }


def _time_key_set(slots: list[MeetingSlot]) -> frozenset:
    return frozenset(s.time_key for s in slots)


def _full_key_set(slots: list[MeetingSlot]) -> frozenset:
    return frozenset(s.full_key for s in slots)


def _load_shared_sections(scenario_id: int) -> list[SharedSection]:
    """Build the shared-section list for a scenario (read-only)."""
    board_order: dict[int, tuple[int, int]] = {
        b.id: (b.display_order if b.display_order is not None else 10**6, b.id)
        for b in DeliveryBoard.objects.filter(scenario_id=scenario_id)
    }
    placements = (
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .select_related("term_section")
        .order_by("term_section_id", "board_id", "day", "start_time")
    )
    by_section: dict[int, list[SectionPlacement]] = defaultdict(list)
    for p in placements:
        by_section[p.term_section_id].append(p)

    shared: list[SharedSection] = []
    for term_section_id, rows in sorted(by_section.items()):
        board_ids = sorted({p.board_id for p in rows})
        if len(board_ids) <= 1:
            continue
        schedule_by_board: dict[int, list[MeetingSlot]] = defaultdict(list)
        for p in rows:
            schedule_by_board[p.board_id].append(
                MeetingSlot(
                    day=str(p.day).strip().upper(),
                    start=_hhmm(p.start_time),
                    end=_hhmm(p.end_time),
                    room=str(p.room or "").strip().upper(),
                )
            )
        time_keys = {bid: _time_key_set(s) for bid, s in schedule_by_board.items()}
        full_keys = {bid: _full_key_set(s) for bid, s in schedule_by_board.items()}
        schedule_divergent = len(set(time_keys.values())) > 1
        # Room divergence only *additionally* matters when the times already agree;
        # a schedule-divergent section is rewritten wholesale anyway.
        room_divergent = not schedule_divergent and len(set(full_keys.values())) > 1
        # Canonical winner: the board with the lowest (display_order, id).
        # Deterministic and stable; coherence is the goal and the optimiser
        # re-optimises for quality afterwards.
        canonical_board_id = min(board_ids, key=lambda b: board_order.get(b, (10**6, b)))
        # De-duplicate the canonical schedule on (day, start): the target board's
        # unique key is (board, term_section, day, start_time), so two canonical
        # slots colliding there would make the rewrite un-writable.
        canonical: list[MeetingSlot] = []
        seen_day_start: set[tuple[str, str]] = set()
        for slot in sorted(schedule_by_board[canonical_board_id], key=lambda s: s.full_key):
            key = (slot.day, slot.start)
            if key in seen_day_start:
                continue
            seen_day_start.add(key)
            canonical.append(slot)
        sample = rows[0]
        shared.append(
            SharedSection(
                term_section_id=term_section_id,
                course_code=str(sample.term_section.course_code),
                section=str(sample.term_section.section),
                board_ids=board_ids,
                schedule_by_board=dict(schedule_by_board),
                schedule_divergent=schedule_divergent,
                room_divergent=room_divergent,
                canonical_board_id=canonical_board_id,
                canonical_schedule=canonical,
            )
        )
    return shared


def analyze_shared_sections(scenario_id: int) -> SharedSectionReport:
    """Read-only: report every shared section and whether it diverges across boards."""
    TimetableScenario.objects.get(id=scenario_id)  # 404-style guard for callers
    shared = _load_shared_sections(scenario_id)
    divergent = [s for s in shared if s.divergent]
    report = SharedSectionReport(
        scenario_id=scenario_id,
        shared_count=len(shared),
        divergent_count=len(divergent),
        schedule_divergent_count=sum(1 for s in shared if s.schedule_divergent),
        room_divergent_count=sum(1 for s in shared if s.room_divergent),
        shared_sections=shared,
    )
    if not shared:
        report.notes.append("No section is shared across boards.")
    elif not divergent:
        report.notes.append(
            f"{len(shared)} shared sections, all coherent (every board shows the same "
            "day, time and room)."
        )
    else:
        report.notes.append(
            f"{len(divergent)} of {len(shared)} shared sections are incoherent "
            f"({report.schedule_divergent_count} scheduled at different times, "
            f"{report.room_divergent_count} same time but different rooms). Each yields "
            "phantom TermSectionMeeting rows; canonicalise to make the board honest."
        )
    return report


@transaction.atomic
def canonicalise_shared_sections(
    scenario_id: int,
    *,
    apply: bool = False,
    sync_meetings: bool = True,
) -> SharedSectionReport:
    """Rewrite each incoherent shared section so every board shows one occurrence.

    ``apply=False`` (default) is a dry run: it reports what *would* change without
    writing. ``apply=True`` rewrites, inside one transaction, every board whose
    ``(day, start, end, room)`` set differs from the canonical board's, then
    rebuilds ``TermSectionMeeting`` so placements and TSM commit together (pass
    ``sync_meetings=False`` only if the caller will rebuild TSM itself).

    Rows are updated **in place** wherever possible so placement ids survive —
    ``TimetableRepairRun.target_placement`` and ``TimetableRepairGlobalPlanItem.
    placement`` are ``SET_NULL`` foreign keys that would be silently detached by a
    delete/recreate. Surplus rows are deleted and shortfalls created only as needed.

    A board holding any **locked** placement for the section is skipped whole and
    reported — a lock is an explicit human decision, so a locked divergence is
    surfaced for a human to resolve rather than overridden.

    Coherence only: exposing a real clash the divergence had hidden is the correct
    outcome; run the optimiser afterwards to restore quality on the honest board.
    """
    report = analyze_shared_sections(scenario_id)
    divergent = [s for s in report.shared_sections if s.divergent]
    if not divergent:
        report.notes.append("Nothing to canonicalise.")
        return report

    if not apply:
        would = sum(
            1
            for s in divergent
            for bid in s.board_ids
            if bid != s.canonical_board_id
            and _full_key_set(s.schedule_by_board[bid]) != _full_key_set(s.canonical_schedule)
        )
        report.notes.append(
            f"DRY RUN — would rewrite {would} boards across {len(divergent)} incoherent "
            "sections. Pass apply=True to write."
        )
        return report

    rewritten = 0
    canonicalised = 0
    skipped_locked = 0
    for shared in divergent:
        canonical = shared.canonical_schedule
        canonical_keys = _full_key_set(canonical)
        section_touched = False
        section_skipped = False
        for board_id in shared.board_ids:
            if board_id == shared.canonical_board_id:
                continue
            # Already identical (time AND room) — nothing to do; rewriting anyway
            # would destroy a correct room assignment for no benefit.
            if _full_key_set(shared.schedule_by_board[board_id]) == canonical_keys:
                continue
            existing = list(
                SectionPlacement.objects.filter(
                    board_id=board_id, term_section_id=shared.term_section_id
                ).order_by("id")
            )
            if any(p.is_locked for p in existing):
                report.notes.append(
                    f"Skipped locked placement(s) for {shared.course_code}|{shared.section} "
                    f"on board {board_id}; resolve the lock manually."
                )
                section_skipped = True
                continue
            # Update in place (preserves ids and the FKs pointing at them), then
            # delete surplus / create shortfall.
            for row, slot in zip(existing, canonical, strict=False):
                row.day, row.start_time, row.end_time, row.room = (
                    slot.day,
                    slot.start,
                    slot.end,
                    slot.room,
                )
                row.save(update_fields=["day", "start_time", "end_time", "room", "updated_at"])
                rewritten += 1
            if len(existing) > len(canonical):
                SectionPlacement.objects.filter(
                    id__in=[p.id for p in existing[len(canonical) :]]
                ).delete()
            for slot in canonical[len(existing) :]:
                SectionPlacement.objects.create(
                    board_id=board_id,
                    term_section_id=shared.term_section_id,
                    day=slot.day,
                    start_time=slot.start,
                    end_time=slot.end,
                    room=slot.room,
                )
                rewritten += 1
            section_touched = True
        if section_touched:
            canonicalised += 1
        if section_skipped:
            skipped_locked += 1

    report.applied = True
    report.sections_canonicalised = canonicalised
    report.sections_skipped_locked = skipped_locked
    report.placements_rewritten = rewritten

    if rewritten and sync_meetings:
        from core.services.timetable_board_persistence import sync_meetings_from_placements

        sync_meetings_from_placements(scenario_id)

    # Re-read the post-write state so the returned counts describe the board as it
    # now is, not as it was before the rewrite.
    remaining = [s for s in _load_shared_sections(scenario_id) if s.divergent]
    report.remaining_divergent_count = len(remaining)
    report.notes.append(
        f"Canonicalised {canonicalised} sections ({rewritten} placements rewritten"
        f"{', ' + str(skipped_locked) + ' skipped for locks' if skipped_locked else ''})"
        f"; {len(remaining)} still incoherent. "
        + (
            "TermSectionMeeting rebuilt in the same transaction. "
            if rewritten and sync_meetings
            else ""
        )
        + "Re-run the optimiser to restore quality on the coherent board."
    )
    return report

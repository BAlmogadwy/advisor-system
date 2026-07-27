"""Why a meeting has no room — the decomposition, not the count.

"14 unroomed" is not a finding anybody can act on. It could mean the university
needs another lab, or that this particular timetable stacked six meetings into
one hour while the rest of the week sat empty. Those have opposite fixes, and the
count alone cannot tell them apart. So every unroomed meeting is sorted into
exactly one of:

* **IMPOSSIBLE** — no room of the right kind, open to that programme, is large
  enough. No timetable can ever place it. The fix is a bigger room or a smaller
  section, and it is a fact about the estate rather than about the solver;
* **SATURATED** — a compatible room exists, but the estate simply does not
  contain enough room-periods of that kind all week to hold every meeting that
  needs one. Also unfixable by rescheduling, and easy to mistake for congestion
  because each individual meeting *looks* placeable;
* **CONGESTION** — room-periods are available somewhere in the week, but every
  compatible room was taken at that particular hour. **This is the only bucket a
  better board can recover**, which is why it must not absorb the other two.

Reported per course with the actual numbers, because "CS111 needs a lab seating
35 and the largest one open to it seats 25" is a sentence somebody can act on,
and "10 unroomed" is not.
"""

from __future__ import annotations

from collections import defaultdict

from scheduler.domain import Snapshot
from scheduler.domain.board import Board, Placement

IMPOSSIBLE = "IMPOSSIBLE"
SATURATED = "SATURATED"
CONGESTION = "CONGESTION"


def _compatible(snapshot: Snapshot, placement: Placement, need: int) -> list:
    """Rooms that could ever hold this meeting — kind, capacity and programme."""
    offering = snapshot.offerings_by_id.get(placement.offering_id)
    if offering is None:
        return []
    return [
        room
        for room in snapshot.rooms
        if room.kind is placement.kind
        and room.capacity >= need
        and (offering.programs & room.programs)
    ]


def room_shortfall(snapshot: Snapshot, board: Board) -> dict:
    """Split the unroomed meetings into what no timetable can fix, and the rest."""
    sections = {s.id: s for s in snapshot.sections}
    offerings = snapshot.offerings_by_id

    # What is occupied where, so congestion can be quantified rather than asserted.
    occupied: dict[tuple[str, object], list] = defaultdict(list)
    for p in board.placements:
        if p.room_id is not None:
            occupied[(p.room_id, p.day)].append(p.window)

    # Is the whole week's supply of this room kind big enough for the demand on
    # it? Without this, a shortage spread thinly across the week reads as
    # ordinary congestion at every individual hour, and the report would promise
    # a rescheduling fix that cannot exist.
    saturated_kinds = _saturated_kinds(snapshot)

    # Classify IMPOSSIBLE first, because those meetings are unroomable for a
    # reason that has nothing to do with supply, and they must not be counted
    # twice against it.
    unroomed = []
    for p in board.placements:
        if not p.needs_room or p.room_id is not None:
            continue
        section = sections.get(p.section_id)
        need = snapshot.policy.required_room_capacity(section.capacity) if section else 0
        unroomed.append((p, need, _compatible(snapshot, p, need)))

    impossible_per_kind: dict[str, int] = defaultdict(int)
    for p, _need, options in unroomed:
        if not options:
            impossible_per_kind[p.kind.name] += 1

    # The shortage proves that *this many* meetings cannot be roomed — no more.
    # Labelling every unroomed meeting of a saturated kind SATURATED would claim
    # 78 are unfixable when the arithmetic only proves 29; the rest genuinely
    # might move. Overstating what cannot be fixed is the same failure as
    # understating it, so the budget is spent and then congestion resumes.
    saturation_budget: dict[str, int] = {
        kind: max(0, (demand - supply) - impossible_per_kind[kind])
        for kind, (supply, demand) in saturated_kinds.items()
    }

    findings: dict[tuple, dict] = {}
    for p, need, options in unroomed:
        offering = offerings.get(p.offering_id)
        code = offering.course_code if offering else p.offering_id

        if not options:
            largest = max(
                (
                    room.capacity
                    for room in snapshot.rooms
                    if room.kind is p.kind
                    and offering is not None
                    and (offering.programs & room.programs)
                ),
                default=0,
            )
            key = (IMPOSSIBLE, code, p.kind.name)
            row = findings.setdefault(
                key,
                {
                    "reason": IMPOSSIBLE,
                    "course": code,
                    "kind": p.kind.name,
                    "meetings": 0,
                    "needs_capacity": need,
                    "largest_available": largest,
                    "detail": (
                        f"needs a {p.kind.name.lower()} seating {need}; the largest one "
                        f"open to this programme seats {largest}"
                    ),
                },
            )
        elif saturation_budget.get(p.kind.name, 0) > 0:
            saturation_budget[p.kind.name] -= 1
            # Grouped by room KIND, not by course: the cause is one estate-wide
            # shortage, so listing it per course would print the same sentence
            # twenty times and bury the two findings that differ per course.
            supply, demand = saturated_kinds[p.kind.name]
            key = (SATURATED, p.kind.name)
            row = findings.setdefault(
                key,
                {
                    "reason": SATURATED,
                    "course": f"({p.kind.name.lower()} estate)",
                    "kind": p.kind.name,
                    "meetings": 0,
                    "courses": set(),
                    "room_periods": supply,
                    "meetings_needing_them": demand,
                    "detail": (
                        f"the week holds {supply} {p.kind.name.lower()} room-periods for "
                        f"{demand} meetings that need one — rescheduling cannot fix a "
                        f"shortfall of {demand - supply}"
                    ),
                },
            )
            row["courses"].add(code)
        else:
            # Verified against the board, not assumed: count how many of the
            # compatible rooms genuinely had something overlapping this window.
            blocked = sum(
                1
                for room in options
                if any(p.window.overlaps(w) for w in occupied.get((room.id, p.day), ()))
            )
            key = (CONGESTION, code, p.kind.name)
            row = findings.setdefault(
                key,
                {
                    "reason": CONGESTION,
                    "course": code,
                    "kind": p.kind.name,
                    "meetings": 0,
                    "compatible_rooms": len(options),
                    "rooms_blocked_at_that_hour": blocked,
                    "detail": (
                        f"{len(options)} compatible room(s) exist and {blocked} were "
                        f"occupied at that hour — a different time would fit"
                    ),
                },
            )
        row["meetings"] += 1

    for row in findings.values():
        if "courses" in row:
            affected = sorted(row.pop("courses"))
            row["courses_affected"] = affected
            row["detail"] += f"; {len(affected)} courses affected"

    rows = sorted(findings.values(), key=lambda r: (r["reason"], -r["meetings"], r["course"]))
    counts = {
        reason: sum(r["meetings"] for r in rows if r["reason"] == reason)
        for reason in (IMPOSSIBLE, SATURATED, CONGESTION)
    }
    return {
        "unroomed": sum(counts.values()),
        "impossible": counts[IMPOSSIBLE],
        "saturated": counts[SATURATED],
        "congestion": counts[CONGESTION],
        # Only this is worth another solve; the rest need a decision about rooms.
        "recoverable": counts[CONGESTION],
        "findings": rows,
    }


def _saturated_kinds(snapshot: Snapshot) -> dict[str, tuple[int, int]]:
    """Room kinds whose whole-week supply cannot meet demand, with the arithmetic.

    Counted over the estate as a whole rather than per programme: a per-programme
    split would need to decide how shared rooms are apportioned, and getting that
    wrong would over-report an unfixable shortage. This under-reports instead,
    which keeps every SATURATED claim provable.
    """
    supply: dict[str, int] = defaultdict(int)
    periods: dict[str, int] = defaultdict(int)
    for slot in snapshot.grid.slots:
        if slot.delivery.name == "IN_PERSON":
            periods[slot.kind.name] += 1
    for room in snapshot.rooms:
        supply[room.kind.name] += periods.get(room.kind.name, 0)

    demand: dict[str, int] = defaultdict(int)
    for section in snapshot.sections:
        offering = snapshot.offerings_by_id[section.offering_id]
        for requirement in offering.requirements:
            if requirement.needs_room:
                demand[requirement.kind.name] += requirement.count_per_week

    return {
        kind: (supply.get(kind, 0), count)
        for kind, count in demand.items()
        if count > supply.get(kind, 0)
    }


def unroomable_meetings(snapshot: Snapshot) -> int:
    """Meetings no timetable can ever room — computable before solving.

    A floor on the unroomed count that depends only on the estate and the section
    sizes, so a board can be judged against what was achievable rather than
    against zero.
    """
    total = 0
    for section in snapshot.sections:
        offering = snapshot.offerings_by_id[section.offering_id]
        need = snapshot.policy.required_room_capacity(section.capacity)
        for requirement in offering.requirements:
            if not requirement.needs_room:
                continue
            if not any(
                room.kind is requirement.kind
                and room.capacity >= need
                and (offering.programs & room.programs)
                for room in snapshot.rooms
            ):
                total += requirement.count_per_week
    return total

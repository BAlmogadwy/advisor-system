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
from dataclasses import replace

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


#: Ceiling on how many room families the saturation check will consider. The
#: closure under union is what makes a shortage confined to a subset provable;
#: the cap keeps a pathological estate from generating 2^n of them. Exceeding it
#: only weakens the finding — fewer families means less saturation proved, never
#: more — so an incomplete set can never turn congestion into a false claim.
_MAX_ROOM_FAMILIES = 96


def _room_families(sets: set[frozenset[str]]) -> list[frozenset[str]]:
    """The given room sets, closed under union up to the cap.

    Hall's condition binds on unions, not only on the sets themselves. With
    compatible sets {A}, {A,B} and {B}, no single one shows a shortage that the
    union {A,B} proves immediately.
    """
    families = {f for f in sets if f}
    frontier = list(families)
    while frontier and len(families) < _MAX_ROOM_FAMILIES:
        head = frontier.pop()
        for other in list(families):
            merged = head | other
            if merged not in families:
                families.add(merged)
                frontier.append(merged)
                if len(families) >= _MAX_ROOM_FAMILIES:
                    break
    return sorted(families, key=lambda f: (len(f), sorted(f)))


def _saturated_kinds(snapshot: Snapshot) -> dict[str, tuple[int, int]]:
    """Room kinds whose whole-week supply cannot meet demand, with the arithmetic.

    Measured over **compatible-room families**, not over the estate as a whole.
    Counting per kind only sees a shortage when the entire estate of that kind is
    short, and misses every shortage confined to a capacity tier or a programme:
    six lecture rooms of which one seats 60, ten sections needing 60 seats, and a
    week with five lecture periods a day gives five meetings that can never be
    roomed — while the whole-estate arithmetic sees 6 rooms against 10 meetings
    and reports comfortable supply. Every one of those five was then published as
    recoverable CONGESTION, telling a registrar to re-solve when the real answer
    is a second large room.

    The test is Hall's deficiency: for a set of rooms F, count the meetings whose
    compatible rooms all lie inside F. If that exceeds what F can host all week,
    the excess cannot be roomed by any timetable. Taking the maximum deficiency
    over all families gives the strongest provable claim, and the whole-estate
    case is simply the largest family, so this can only ever find more than the
    old arithmetic did — never less, and never anything that is not proved.

    Supply is what a room can actually HOST in a day, not how many cells the grid
    declares. Several declared cells are alternatives rather than extra capacity
    — 10:30-11:45 and 10:50-12:05 are one lecture opportunity offered two ways —
    so counting cells overstates the estate. That was got wrong here once:
    counting cells gave the female cohort 175 lecture-periods and reported 50
    meetings as recoverable. True throughput is 5 per room per day, so 125, and
    the shortfall is 79 — exactly what the solver leaves unroomed however the
    price of an unroomed meeting is set.

    Returns ``{kind: (supply, demand)}`` for the family that proves the most,
    expressed so the caller's budget arithmetic (``demand - supply``) is
    unchanged.
    """
    # Every meeting that needs a room, with the rooms that could ever hold it.
    by_kind: dict[str, list[tuple[int, frozenset[str]]]] = defaultdict(list)
    for section in snapshot.sections:
        offering = snapshot.offerings_by_id[section.offering_id]
        need = snapshot.policy.required_room_capacity(section.capacity)
        for requirement in offering.requirements:
            if not requirement.needs_room:
                continue
            compatible = frozenset(
                room.id
                for room in snapshot.rooms
                if room.kind is requirement.kind
                and room.capacity >= need
                and (offering.programs & room.programs)
            )
            for _ in range(requirement.count_per_week):
                by_kind[requirement.kind.name].append((requirement.duration, compatible))

    worst: dict[str, tuple[int, int]] = {}
    for kind, meetings in by_kind.items():
        # Meetings with no compatible room at all are IMPOSSIBLE, a different
        # finding entirely, and counting them here would charge one shortage
        # twice. The caller subtracts them again for the same reason.
        placeable = [(d, c) for d, c in meetings if c]
        for family in _room_families({c for _d, c in placeable}):
            inside = [(d, c) for d, c in placeable if c <= family]
            if not inside:
                continue
            supply = snapshot.grid.room_periods_per_week(
                frozenset(d for d, _c in inside), len(family)
            )
            demand = len(inside)
            if demand <= supply:
                continue
            best = worst.get(kind)
            if best is None or (demand - supply) > (best[1] - best[0]):
                worst[kind] = (supply, demand)
    return worst


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


def assign_rooms_exact(
    snapshot: Snapshot, board: Board, *, time_limit_seconds: float = 20.0
) -> Board:
    """Room a time-fixed board optimally, instead of greedily.

    Once every meeting has a time, choosing rooms is a self-contained assignment
    problem, and first-fit is not optimal at it.

    Greedy is actually strong on *capacity*: taking the largest section first and
    giving it the smallest sufficient room never wastes a big room. What it
    cannot see is **programme restriction**. A room may be large enough and still
    be closed to the course that needs it, so once greedy has spent the only
    shared room on a class that had alternatives, the class with no alternatives
    is stranded — demonstrated by a test that reproduces exactly that and shows
    greedy rooming one of two meetings where both can be roomed.

    (The "55 recoverable" figure the shortfall report gives for the female cohort
    is *not* evidence for this. That bucket means a different **time** would fit,
    which is a statement about the timing solver, not about this stage.)

    Solved exactly here: a meeting takes at most one room from those that fit it,
    a room holds at most one meeting at any instant, and the number of roomed
    meetings is maximised. Larger sections are worth marginally more, so when a
    tie must be broken the room goes to the class that is hardest to place
    elsewhere.

    Falls back to the greedy result if the solver finds nothing in time — a
    slower answer is never worth no answer.
    """
    from ortools.sat.python import cp_model

    from scheduler.solve import assign_rooms

    greedy = assign_rooms(snapshot, board)
    sections = {s.id: s for s in snapshot.sections}

    needs = [p for p in greedy.placements if p.needs_room]
    if not needs:
        return greedy

    options: dict[str, list] = {}
    for p in needs:
        section = sections.get(p.section_id)
        need = snapshot.policy.required_room_capacity(section.capacity) if section else 0
        options[p.id] = [r.id for r in _compatible(snapshot, p, need)]
    if not any(options.values()):
        return greedy

    model = cp_model.CpModel()
    y: dict[tuple[str, str], object] = {}
    for p in needs:
        choices = [
            y.setdefault((p.id, room_id), model.new_bool_var(f"y_{p.id}_{room_id}"))
            for room_id in options[p.id]
        ]
        if choices:
            model.add_at_most_one(choices)

    # A room holds one meeting per instant. Boundaries are taken from the board
    # itself, so the atoms are exactly as fine as this timetable requires.
    by_day: dict[object, list] = {}
    for p in needs:
        by_day.setdefault(p.day, []).append(p)
    for _day, items in by_day.items():
        marks = sorted({m for p in items for m in (p.window.start, p.window.end)})
        for start, end in zip(marks, marks[1:], strict=False):
            covering = [p for p in items if p.window.start < end and p.window.end > start]
            if len(covering) < 2:
                continue
            for room_id in sorted({r for p in covering for r in options[p.id]}):
                here = [y[(p.id, room_id)] for p in covering if (p.id, room_id) in y]
                if len(here) > 1:
                    model.add_at_most_one(here)

    # Every roomed meeting is worth far more than any tie-break, so the count is
    # maximised first; capacity only decides which meeting wins a contested room,
    # sending it to the class that is hardest to place anywhere else. Section
    # capacity is looked up per placement rather than parsed out of its id --
    # a placement id is "<section_id>#M<n>" and section ids themselves contain
    # "#", so splitting on it silently yields the wrong section.
    # The count must dominate ABSOLUTELY, not merely usually. With 1000 + capacity
    # the tie-break could outvote the count itself: two meetings worth 1999 each
    # beat three worth 1000 each, so the "exact" pass would room fewer classes
    # than greedy — the precise failure it exists to remove. The base is
    # therefore larger than every possible sum of tie-breaks.
    tie_break_ceiling = 1000
    base = tie_break_ceiling * (len(needs) + 1)
    weight_of = {}
    for p in needs:
        section = sections.get(p.section_id)
        weight_of[p.id] = base + min(tie_break_ceiling - 1, section.capacity if section else 0)
    model.maximize(sum(var * weight_of[p_id] for (p_id, _room), var in y.items()))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    # One worker and a fixed seed: this stage is small, and a reproducible room
    # allocation matters more here than a marginally faster one. With eight
    # workers racing a wall clock, the same board could be roomed differently on
    # two runs of the same data, which makes any comparison between runs
    # meaningless.
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    status = solver.solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return greedy

    chosen: dict[str, str] = {}
    for (p_id, room_id), var in y.items():
        if solver.value(var):
            chosen[p_id] = room_id

    exact = Board(
        tuple(
            replace(p, room_id=chosen.get(p.id)) if p.needs_room else p for p in greedy.placements
        )
    )
    # Never return a worse board than the cheap one it replaces.
    if sum(1 for p in exact.placements if p.needs_room and p.room_id) < sum(
        1 for p in greedy.placements if p.needs_room and p.room_id
    ):
        return greedy
    return exact

"""Assign each curriculum term to a half of the day (D20).

The owner's manual method, stated in their own words:

    "If term 9 is placed in the morning I will try my best to make term 5
    afternoon, because an irregular student of 9 might need something from term
    5. Then I place term 3 morning and term 1 afternoon. Then I start to modify
    each within its specified period to avoid the conflicts and reduce the gaps."

This is a **structural decomposition**, and it is worth naming precisely: rather
than asking the objective to *discover* that term-9 and term-5 courses must not
collide, it makes the collision impossible by construction and leaves the search
to do the fine work inside each half-day. Fix a good structure, then repair
locally — the same shape as the ITC-2019 winning matheuristic, and the same
lesson the Dr Nawaf experiment taught us (see the blueprint's search notes).

**Why it matters here.** On the live male cohort **285 of 390 students (73%)
take courses from more than one curriculum term** — the AI/AI2 and DS/DS2 plans
are offset by one term, so a single board carries several cohorts at once. Cross
-term collision is not an edge case in this data, it is the main event.

Formally the choice is a **MAX-CUT**: terms are nodes, an edge between two terms
weighs how many students take courses in both, and splitting the terms into two
halves of the day cuts as much of that weight as possible. Solved exactly here —
the graph has ~10 nodes, so it costs milliseconds.

**The room estate is what makes it non-trivial.** Separation alone would happily
pile every heavy term into one half of the day, and this cohort's morning holds
only `lecture rooms x morning cells x days` meetings. So the partition is
maximised for separation **subject to** each half fitting its own supply; without
that constraint the best cut on live data puts 70 room-consuming meetings into a
morning that holds 80, and any drift makes it unroomable.
"""

from __future__ import annotations

import collections
import itertools

from scheduler.domain import DeliveryMode, MeetingKind, Snapshot

#: Noon, in minutes from midnight — the boundary between the two halves.
NOON = 12 * 60

MORNING = "AM"
AFTERNOON = "PM"


def terms_by_programme(snapshot: Snapshot) -> dict[str, dict[str, int]]:
    """`{programme: {course_code: plan term}}` — the term a course sits at *for
    that programme*.

    Read fresh rather than taken from `Offering.terms`, which is the UNION across
    programmes and therefore cannot say which cohort is which.
    """
    from core.models import ProgrammeRequirement

    out: dict[str, dict[str, int]] = collections.defaultdict(dict)
    for programme, code, term in ProgrammeRequirement.objects.filter(
        program__in=list(snapshot.programs)
    ).values_list("program", "course_code", "programme_term"):
        if term is not None:
            out[str(programme).strip().upper()][str(code).strip().upper()] = int(term)
    return out


def term_of_demand(snapshot: Snapshot) -> dict[str, int]:
    """`{offering_id: the term its students are actually studying it in}`.

    **Not** `min(offering.terms)`, and the difference is not cosmetic. The
    AI/AI2 and DS/DS2 plans are offset by one term, so a course at term 9 in AI
    is at term 8 in AI2 — and taking the minimum labels it "term 8", inventing an
    even-term cohort that cannot exist. In any one semester every student's next
    term has the SAME parity (verified on the live cohort: all 398 students are
    on odd terms), so an even label is always an artefact.

    The term is therefore taken from **each student's own programme** and the
    offering keeps the term the majority of its students meet it at. Ties break
    on the lower term, so the answer does not move between runs.
    """
    table = terms_by_programme(snapshot)
    offerings = snapshot.offerings_by_id
    votes: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for demand in snapshot.demand:
        per_programme = table.get(demand.program, {})
        for offering_id in demand.offering_ids:
            offering = offerings.get(offering_id)
            if offering is None or not offering.is_scheduled:
                continue
            term = per_programme.get(offering.course_code.strip().upper())
            if term is not None:
                votes[offering_id][term] += 1
    return {
        offering_id: min(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        for offering_id, counter in votes.items()
        if counter
    }


def shared_students_by_term_pair(
    snapshot: Snapshot, term_of: dict[str, int]
) -> collections.Counter:
    """How many students take courses in both of two terms — the edge weights."""
    weights: collections.Counter = collections.Counter()
    for demand in snapshot.demand:
        terms = {term_of[o] for o in demand.offering_ids if o in term_of}
        for a, b in itertools.combinations(sorted(terms), 2):
            weights[(a, b)] += 1
    return weights


def _half_day_capacity(snapshot: Snapshot) -> dict[str, int]:
    """Room-consuming meetings each half of the day can physically hold.

    Counted as (mutually non-overlapping cells in that half) x (rooms) x (days).
    The two halves are NOT symmetric on this grid — the morning offers two
    disjoint 75-minute cells and the afternoon three — which is exactly why a
    partition that ignores supply puts the heavy terms in the wrong half.
    """
    grid = snapshot.grid
    days = len(grid.days())
    rooms = len(snapshot.rooms_of_kind(MeetingKind.LECTURE)) or 1
    out = {}
    for half in (MORNING, AFTERNOON):
        starts = sorted(
            {
                w.start
                for w in grid.windows_for(75, DeliveryMode.IN_PERSON)
                if (w.start < NOON) == (half == MORNING)
            }
        )
        # greedily count non-overlapping cells, as the block rule does
        cells, end_of_last = 0, -1
        for start in starts:
            if start >= end_of_last:
                cells += 1
                end_of_last = start + 75
        out[half] = cells * rooms * days
    return out


def _room_load_by_term(snapshot: Snapshot, term_of: dict[str, int]) -> collections.Counter:
    """Room-consuming meetings each term contributes, across all its sections."""
    offerings = snapshot.offerings_by_id
    load: collections.Counter = collections.Counter()
    for section in snapshot.sections:
        offering = offerings[section.offering_id]
        term = term_of.get(offering.id)
        if term is None or not offering.is_scheduled or offering.occupies_fixed_block:
            # A fixed-block course (D19) has its hours already decided and draws
            # on no shared room, so it neither needs phasing nor competes for
            # the supply this balance is protecting.
            continue
        load[term] += sum(r.count_per_week for r in offering.requirements if r.needs_shared_room)
    return load


def partition_terms(snapshot: Snapshot, *, time_limit_seconds: float = 10.0) -> dict[int, str]:
    """Split the curriculum terms into a morning half and an afternoon half.

    Maximises the number of student-term pairs that land in *different* halves,
    subject to neither half being asked to hold more room-consuming meetings
    than it has room-periods for.

    Returns `{term: "AM" | "PM"}`. An empty dict means phasing is not applicable
    (no cross-term demand at all), and the caller should leave the board alone
    rather than impose an arbitrary split.
    """
    from ortools.sat.python import cp_model

    labels = term_of_demand(snapshot)
    weights = shared_students_by_term_pair(snapshot, labels)
    if not weights:
        return {}
    load = _room_load_by_term(snapshot, labels)
    capacity = _half_day_capacity(snapshot)
    terms = sorted({t for pair in weights for t in pair} | set(load))

    model = cp_model.CpModel()
    #: True = morning. The labels are arbitrary until the capacity constraint
    #: below breaks the symmetry — the two halves hold different numbers of
    #: cells, so "which side is which" is a real decision, not a relabelling.
    is_morning = {t: model.new_bool_var(f"term{t}_am") for t in terms}

    separated = []
    for (a, b), students in weights.items():
        cut = model.new_bool_var("")
        model.add(cut <= is_morning[a] + is_morning[b])
        model.add(cut <= 2 - is_morning[a] - is_morning[b])
        separated.append(cut * students)

    morning_load = sum(is_morning[t] * load.get(t, 0) for t in terms)
    total_load = sum(load.get(t, 0) for t in terms)
    model.add(morning_load <= capacity[MORNING])
    model.add(total_load - morning_load <= capacity[AFTERNOON])

    model.maximize(sum(separated))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_seconds)
    solver.parameters.num_workers = 8
    status = solver.solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        # The teaching load does not fit two halves at all. Reported by returning
        # nothing rather than by forcing a split the rooms cannot serve: an
        # unroomable board is a worse answer than an unphased one.
        return {}
    return {t: (MORNING if solver.value(is_morning[t]) else AFTERNOON) for t in terms}


def starts_for_half(snapshot: Snapshot, duration: int, delivery, half: str) -> frozenset[int]:
    """The declared starts of one meeting family that fall in the given half.

    Read from the GRID (D2). Returns everything in that family when the half
    holds none of it — a lab family with no morning window must not be made
    unplaceable by a rule about students.
    """
    windows = snapshot.grid.windows_for(duration, delivery)
    wanted = frozenset(w.start for w in windows if (w.start < NOON) == (half == MORNING))
    return wanted or frozenset(w.start for w in windows)

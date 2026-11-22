"""Per-instructor teaching-time rules, as the planner uses them.

The stored rules reach the scheduler as ``Instructor.forbidden`` (see
``scheduler.intake``); this module is everything else that needs to reason about
them without touching the database:

* **the probe** (:func:`has_legal_week`) — can one instructor hold this set of
  sections at all, inside the time he may teach? It runs the REAL model
  (``solve``) on just his sections, so it cannot drift from the solver's own hard
  rules the way a second implementation of them would;
* **the safety net's tools** (:func:`restricted_instructor_ids`,
  :func:`release_instructors`) — a rule must never cost a class its place, so
  when it does the planner lets go of the affected instructors' sections;
* **the reports** (:func:`time_rule_cost`, :func:`describe_time_rule_cost`,
  :func:`no_legal_week_offerings`) — what the rules cost, and who they leave with
  no week at all.

Pure Python, no Django: the rules are plain tuples on the snapshot by the time
anything here sees them.
"""

from __future__ import annotations

from dataclasses import replace

from scheduler.domain import Instructor, Section, Snapshot
from scheduler.meeting_patterns import legal_cells

#: A probe is one instructor's handful of sections, so it is milliseconds. The
#: ceiling only bounds a pathological case; an unfinished probe says "no".
_PROBE_SECONDS = 10.0


def restricted_instructor_ids(snapshot: Snapshot) -> tuple[int, ...]:
    """Instructors that carry a rule AND hold at least one section, sorted."""
    holders = {s.instructor_id for s in snapshot.sections if s.instructor_id is not None}
    return tuple(sorted(i.id for i in snapshot.instructors if i.forbidden and i.id in holders))


def release_instructors(snapshot: Snapshot, instructor_ids) -> Snapshot:
    """The snapshot with those instructors' sections set unassigned (D5).

    Unassigned is a first-class state, so this is always legal; the sections lose
    only the instructor constraints, never their place on the board.
    """
    ids = frozenset(instructor_ids)
    if not ids:
        return snapshot
    return replace(
        snapshot,
        sections=tuple(
            replace(s, instructor_id=None) if s.instructor_id in ids else s
            for s in snapshot.sections
        ),
    )


def _meeting_requirement(offering, index: int):
    expanded = [r for r in offering.requirements for _ in range(r.count_per_week)]
    return expanded[index - 1]


def never_placeable(snapshot: Snapshot) -> set[str]:
    """The meetings (``section#M<index>``) that have no legal cell even WITHOUT any rule.

    A duration the grid never declared is unplaceable for everyone, so it is
    neither a reason to refuse an instructor a section nor a reason to release
    instructors.
    """
    offerings = snapshot.offerings_by_id
    out = set()
    for section in snapshot.sections:
        offering = offerings[section.offering_id]
        for index in range(1, offering.meetings_per_week + 1):
            requirement = _meeting_requirement(offering, index)
            if not legal_cells(snapshot, offering, requirement, index):
                out.add(f"{section.id}#M{index}")
    return out


def unplaced_by_rule(snapshot: Snapshot, unplaced) -> tuple[str, ...]:
    """Those of ``unplaced`` that have legal cells without the rules: a rule left them out."""
    free = never_placeable(snapshot)
    return tuple(u for u in unplaced if u not in free)


def has_legal_week(
    snapshot: Snapshot,
    instructor: Instructor,
    sections: list[Section] | tuple[Section, ...],
    *,
    hard_options: dict | None = None,
) -> bool:
    """Whether ``instructor`` can hold all of ``sections`` inside his allowed time.

    Probes the solver's own model on just these sections, stopping at the first
    feasible solution (there is no objective), so every hard rule it enforces —
    legal windows and ``allowed_starts``, one meeting per section per day,
    institutional bundles, no overlap among his own sections, the daily cap — is
    checked by the code that will really place them. Rooms and students are left
    out: they are other people's constraints, and the planner's own safety net
    answers for whatever the full problem adds.

    ``hard_options`` are the ``solve()`` keywords of the planner that will place
    them (the same-hour ceiling, the exact-block tiers); the default is
    ``solve.planner_hard_options()``, the one place ``plan()`` takes its own from.
    A section of a block tier is probed together with its siblings (unassigned),
    because its block is shared with them.

    A meeting that has no legal cell even WITHOUT the rule is unplaceable for
    everyone, so it does not count against this instructor. Anything the probe
    does not finish counts as "no": the cautious answer leaves the section
    unassigned, never dropped. The daily cap is ``solve()``'s own (3): there is no
    solver option for another, so this probe cannot honour a different one.
    """
    from scheduler.solve import planner_hard_options, solve

    options = dict(planner_hard_options() if hard_options is None else hard_options)
    tiers = options.get("exact_block_tiers") or frozenset()
    held = tuple(replace(s, instructor_id=instructor.id) for s in sections)
    held_ids = {s.id for s in held}
    offerings = snapshot.offerings_by_id
    siblings = tuple(
        replace(s, instructor_id=None)
        for s in snapshot.sections
        if s.id not in held_ids
        and s.offering_id in {h.offering_id for h in held}
        and offerings[s.offering_id].tier in tiers
        and not offerings[s.offering_id].is_fully_online
        and offerings[s.offering_id].meeting_policy is None
    )
    everyone = (*held, *siblings)
    wanted = {s.offering_id for s in everyone}
    probe = replace(
        snapshot,
        offerings=tuple(o for o in snapshot.offerings if o.id in wanted),
        sections=everyone,
        rooms=(),
        instructors=(instructor,),
        demand=(),
    )
    result = solve(
        probe,
        time_limit_seconds=_PROBE_SECONDS,
        workers=1,
        break_symmetry=False,
        alpha=1.0,
        **options,
    )
    if result.status not in ("OPTIMAL", "FEASIBLE"):
        return False
    return set(result.unplaced) <= never_placeable(probe)


def no_legal_week_offerings(snapshot: Snapshot) -> list[tuple[Instructor, list]]:
    """Per ruled instructor, the eligible offerings he cannot teach even ONE section of.

    ``[(instructor, [offering, ...]), ...]`` sorted; only offerings that have a
    section on this board. Instructors without a rule cost nothing here.
    """
    sections_of = snapshot.sections_by_offering
    offerings = snapshot.offerings_by_id
    out = []
    for instructor in sorted(snapshot.instructors, key=lambda i: i.id):
        if not instructor.forbidden:
            continue
        stuck = []
        for offering_id in sorted(instructor.eligible_offerings):
            siblings = sections_of.get(offering_id)
            if not siblings or offering_id not in offerings:
                continue
            first = min(siblings, key=lambda s: s.index)
            if not has_legal_week(snapshot, instructor, [first]):
                stuck.append(offerings[offering_id])
        if stuck:
            out.append((instructor, stuck))
    return out


def time_rule_cost(assigned: Snapshot) -> list[dict]:
    """What the rules cost: the sections each ruled instructor lost to them.

    ``assigned`` is the snapshot as ``assign_instructors`` returned it. The
    assignment is run once more with every rule stripped, and each ruled
    instructor is compared with what he would have been given then. Empty when
    nobody carries a rule (and then nothing is run at all).
    """
    ruled = {i.id for i in assigned.instructors if i.forbidden}
    if not ruled:
        return []
    from scheduler.instructors import assign_instructors

    free = replace(
        assigned, instructors=tuple(replace(i, forbidden=()) for i in assigned.instructors)
    )
    wanted = assign_instructors(free)
    got = {s.id: s.instructor_id for s in assigned.sections}
    offerings = assigned.offerings_by_id
    names = {i.id: i.name for i in assigned.instructors}
    rows = []
    for instructor_id in sorted(ruled):
        missed = sorted(
            (
                s
                for s in wanted.sections
                if s.instructor_id == instructor_id and got.get(s.id) != instructor_id
            ),
            key=lambda s: (offerings[s.offering_id].course_code, s.index),
        )
        if missed:
            rows.append(
                {
                    "instructor_id": instructor_id,
                    "name": names.get(instructor_id, str(instructor_id)),
                    "sections": [
                        f"{offerings[s.offering_id].course_code} {s.label}" for s in missed
                    ],
                }
            )
    return rows


def describe_time_rule_cost(rows: list[dict]) -> str | None:
    """One warning line for :func:`time_rule_cost`, or ``None`` when it cost nothing."""
    if not rows:
        return None
    parts = "; ".join(f"{row['name']}: {', '.join(row['sections'])}" for row in rows)
    return (
        "teaching-time rules kept instructors off sections they would otherwise have "
        "taught (no legal week for them inside the allowed time); each went to another "
        f"eligible instructor or stays unassigned: {parts}"
    )

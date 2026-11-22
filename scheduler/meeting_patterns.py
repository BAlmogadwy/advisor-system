"""Scoped institutional weekly patterns, independent of ordinary slot families."""

from scheduler.domain import DeliveryMode, Snapshot, TimeWindow
from scheduler.domain.calendar import Day


def pattern_cells(pattern):
    return tuple((Day(m.day), TimeWindow(m.start_min, m.end_min)) for m in pattern.meetings)


def meeting_blocked(forbidden, day: Day, window: TimeWindow) -> bool:
    """Whether a campus meeting overlaps an instructor's forbidden teaching time.

    ``forbidden`` is ``Instructor.forbidden``: ``(day_index, start, end)`` windows,
    half-open, so a meeting that merely touches one is allowed. The ONE overlap
    test every consumer of the teaching-time rules shares — the solver's cell
    pruning, the assignment probe, the naive placer and the validator — so they
    cannot disagree about what "inside the rule" means.
    """
    return any(
        d == day.index and start < window.end and window.start < end for d, start, end in forbidden
    )


def _meeting_requirements(offering) -> list:
    return [r for r in offering.requirements for _ in range(r.count_per_week)]


def allowed_patterns(offering, forbidden):
    """The institutional weekly bundles an instructor may teach.

    A bundle is legal only if EVERY campus meeting of it avoids the forbidden
    time; an online meeting is exempt. With no forbidden time this is the
    offering's own tuple, untouched.
    """
    policy = offering.meeting_policy
    if not forbidden:
        return policy.patterns
    meetings = _meeting_requirements(offering)
    return tuple(
        pattern
        for pattern in policy.patterns
        if not any(
            meeting_blocked(forbidden, day, window)
            and not (k < len(meetings) and meetings[k].delivery is DeliveryMode.ONLINE)
            for k, (day, window) in enumerate(pattern_cells(pattern))
        )
    )


def legal_cells(snapshot: Snapshot, offering, requirement, meeting_index: int, forbidden=()):
    """Every (day, window) one meeting may take.

    ``forbidden`` is the teaching-time rule of the instructor who holds the
    section; empty (the default) leaves the result exactly as it always was.
    """
    if offering.meeting_policy is not None:
        patterns = allowed_patterns(offering, forbidden)
        return sorted(
            {
                pattern_cells(pattern)[meeting_index - 1]
                for pattern in patterns
                if meeting_index <= len(pattern.meetings)
            },
            key=lambda item: (item[0].index, item[1].start),
        )
    cells = [
        (s.day, s.window)
        for s in snapshot.grid.day_windows_for(requirement.duration, requirement.delivery)
        if not requirement.allowed_starts or s.window.start in requirement.allowed_starts
    ]
    if forbidden and requirement.delivery is not DeliveryMode.ONLINE:
        cells = [
            (day, window) for day, window in cells if not meeting_blocked(forbidden, day, window)
        ]
    return cells


def policy_accepts(offering, placements) -> bool:
    policy = offering.meeting_policy
    if policy is None:
        return True
    return policy.accepts(
        [
            {"day": p.day.value, "start_min": p.window.start, "end_min": p.window.end}
            for p in placements
        ]
    )


def teaching_days(snapshot: Snapshot):
    return tuple(
        sorted(
            set(snapshot.grid.days())
            | {
                Day(m.day)
                for o in snapshot.offerings
                if o.meeting_policy is not None
                for pattern in o.meeting_policy.patterns
                for m in pattern.meetings
            },
            key=lambda day: day.index,
        )
    )


def occupancy_windows(snapshot: Snapshot):
    return list(
        {s.window for s in snapshot.grid.slots}
        | {
            window
            for o in snapshot.offerings
            if o.meeting_policy is not None
            for pattern in o.meeting_policy.patterns
            for _day, window in pattern_cells(pattern)
        }
    )


def policy_fingerprint(policy):
    if policy is None:
        return None
    return [
        policy.policy_id,
        policy.meeting_kind,
        policy.uses_shared_room,
        [
            [p.pattern_id, [[m.day, m.start_min, m.end_min] for m in p.meetings]]
            for p in policy.patterns
        ],
    ]

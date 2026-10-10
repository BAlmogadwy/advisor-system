"""Scoped institutional weekly patterns, independent of ordinary slot families."""

from scheduler.domain import Snapshot, TimeWindow
from scheduler.domain.calendar import Day


def pattern_cells(pattern):
    return tuple((Day(m.day), TimeWindow(m.start_min, m.end_min)) for m in pattern.meetings)


def legal_cells(snapshot: Snapshot, offering, requirement, meeting_index: int):
    if offering.meeting_policy is not None:
        return sorted(
            {
                pattern_cells(pattern)[meeting_index - 1]
                for pattern in offering.meeting_policy.patterns
                if meeting_index <= len(pattern.meetings)
            },
            key=lambda item: (item[0].index, item[1].start),
        )
    return [
        (s.day, s.window)
        for s in snapshot.grid.day_windows_for(requirement.duration, requirement.delivery)
        if not requirement.allowed_starts or s.window.start in requirement.allowed_starts
    ]


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

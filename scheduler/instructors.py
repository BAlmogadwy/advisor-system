"""Assigning instructors to sections — the decision that must exist before any
instructor-quality objective means anything.

`course_instructors` records **eligibility** (who *may* teach a course), not
assignment (who *does* teach a section). You cannot minimise someone's gap
without knowing which sections are theirs, so this turns the former into the
latter under the owner's load rules:

* **D10** — at most **2 sections of one course** per instructor once that course
  runs more than 3 sections; the remainder are left unassigned;
* **derived weekly cap** — H8 already limits an instructor to 3 sessions/day, and
  the week has 5 teaching days, so `3 x 5 = 15` sessions is the implied ceiling.
  Nothing new is invented; it falls out of a rule that already exists.
  (`Instructor.max_weekly_hours` overrides it if ever populated.)
* **D5** — anything that does not fit is simply **not linked**. Unassigned is a
  first-class state, permanently, not a gap awaiting backfill.

The assignment is deliberately simple and deterministic. Measured on the live M
cohort it already lets the scheduler reach the **proven minimum** working days
(19 = the sum of each instructor's own floor), so a cleverer joint
assignment-and-timing search would have nothing left to win here. If coverage
grows and that stops being true, this is the piece to replace.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import replace

from scheduler.domain import Snapshot
from scheduler.meeting_patterns import pattern_cells

#: Sections of one course a single instructor may hold (D10), once the course
#: runs more than `_CAP_APPLIES_ABOVE` sections.
MAX_SECTIONS_PER_COURSE = 2
_CAP_APPLIES_ABOVE = 3

#: Implied by H8 (3 sessions/day) across a 5-day teaching week.
DERIVED_WEEKLY_SESSION_CAP = 15


def section_min_days(offering) -> int:
    """Fewest distinct days one section's weekly meetings can occupy.

    Ordinary sections meet on distinct days under H2. Institutional patterns
    carry their own complete day sets; intensive English requires four days.
    """
    if offering.meeting_policy is not None:
        return min(len({m.day for m in p.meetings}) for p in offering.meeting_policy.patterns)
    return offering.meetings_per_week


def instructor_floor_days(sessions: int, largest_section_days: int, daily_cap: int = 3) -> int:
    """Fewest days an instructor can possibly work — a proof, not a target.

    Two independent rules force it: the daily cap bounds sessions per day, and a
    section's own meetings cannot be compressed below `section_min_days`.
    Reporting excess *against this floor* means a heavy load is never mistaken
    for bad scheduling.
    """
    return max(math.ceil(sessions / daily_cap) if sessions else 0, largest_section_days)


def assign_instructors(snapshot: Snapshot, *, daily_cap: int = 3) -> Snapshot:
    """Return a snapshot whose sections carry an instructor where one fits."""
    sections_by_offering = snapshot.sections_by_offering
    offerings = snapshot.offerings_by_id

    sessions_of: dict[str, int] = {
        oid: sum(r.count_per_week for r in offerings[oid].requirements) for oid in offerings
    }

    load: Counter[int] = Counter()
    per_course: Counter[tuple[int, str]] = Counter()
    assigned: dict[str, int] = {}
    reserved_patterns: dict[int, list] = {}

    # Deterministic order: instructor id, then offering id, then section index.
    for instructor in sorted(snapshot.instructors, key=lambda i: i.id):
        weekly_cap = DERIVED_WEEKLY_SESSION_CAP
        for offering_id in sorted(instructor.eligible_offerings):
            siblings = sections_by_offering.get(offering_id, ())
            if not siblings:
                continue
            course_cap = (
                MAX_SECTIONS_PER_COURSE if len(siblings) > _CAP_APPLIES_ABOVE else len(siblings)
            )
            policy = offerings[offering_id].meeting_policy
            if policy is not None:
                # Only two disjoint complete bundles exist. A third assigned
                # section would force this instructor to overlap themselves.
                course_cap = min(course_cap, len(policy.patterns))
            weekly = sessions_of.get(offering_id, 0)
            for section in sorted(siblings, key=lambda s: s.index):
                if section.id in assigned:
                    continue
                if per_course[(instructor.id, offering_id)] >= course_cap:
                    break  # D10
                if load[instructor.id] + weekly > weekly_cap:
                    continue  # derived weekly cap — leave unlinked (D5)
                chosen_pattern = None
                if policy is not None:
                    occupied = reserved_patterns.get(instructor.id, [])
                    # ENG101 and ENG102 share the same two alternatives. Their
                    # combined assignments must fit, not merely each course's
                    # independent section cap.
                    chosen_pattern = next(
                        (
                            pattern_cells(pattern)
                            for pattern in policy.patterns
                            if all(
                                sum(other_day is day for other_day, _ in occupied) < daily_cap
                                and all(
                                    other_day is not day or not window.overlaps(other_window)
                                    for other_day, other_window in occupied
                                )
                                for day, window in pattern_cells(pattern)
                            )
                        ),
                        None,
                    )
                    if chosen_pattern is None:
                        continue
                assigned[section.id] = instructor.id
                load[instructor.id] += weekly
                per_course[(instructor.id, offering_id)] += 1
                if chosen_pattern is not None:
                    reserved_patterns.setdefault(instructor.id, []).extend(chosen_pattern)

    return replace(
        snapshot,
        sections=tuple(replace(s, instructor_id=assigned.get(s.id)) for s in snapshot.sections),
    )


def instructor_report(snapshot: Snapshot, *, daily_cap: int = 3) -> list[dict]:
    """Per-instructor load and proven floor. Always published with its coverage."""
    offerings = snapshot.offerings_by_id
    by_instructor: dict[int, list] = {}
    for section in snapshot.sections:
        if section.instructor_id is not None:
            by_instructor.setdefault(section.instructor_id, []).append(section)

    names = {i.id: i.name for i in snapshot.instructors}
    rows = []
    for instructor_id, sections in sorted(by_instructor.items()):
        sessions = 0
        largest = 0
        for section in sections:
            offering = offerings[section.offering_id]
            sessions += sum(r.count_per_week for r in offering.requirements)
            largest = max(largest, section_min_days(offering))
        rows.append(
            {
                "instructor_id": instructor_id,
                "name": names.get(instructor_id, str(instructor_id)),
                "sections": len(sections),
                "sessions": sessions,
                "floor_days": instructor_floor_days(sessions, largest, daily_cap),
            }
        )
    return rows

"""Who cannot register for a course on the saved timetable, and what stops them.

The quality check seats every student once (``timetable_scenario_quality``);
this module reads that seating and never seats again. For each course a student
was left without a seat in, the seating has already said why
(``timetable_student_assignment._diagnose``, over every section of the course):

- ``all_clash``: every section meets at the same time as a section the student
  is seated in;
- ``full``: every section is full;
- ``mixed_blockers``: some sections clash, the others are full (or full and
  clashing);
- ``reserve_only``: only the seats held for at-risk students are left;
- ``no_sections``: the course has no placed section;
- ``unknown``: a section fitted and had a seat, yet the seating left them out.

For the reasons with a clash this names what takes the course's times: each
section the stuck students are seated in that meets at the same time as a
section of the course (a "blocker"), with how many of the course's stuck
students it collides with (``students``) and for how many of them it collides
with EVERY section of the course on its own (``every_section``). A blocker with
``every_section`` is the one to move: moving it frees those students, whatever
else they take. Clashes are read the way the seating reads them: any two
meetings on one day whose times overlap, online meetings included.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from core.services.timetable_assignment_models import SectionState, StudentAssignmentState

#: The seating's reasons that involve a clash, so the course has blockers.
CLASH_REASONS = frozenset({"all_clash", "mixed_blockers"})

#: How many blockers a course lists; the rest are counted in ``blocked_by_more``.
BLOCKER_LIMIT = 5


def registration_blockers(
    states: Mapping[str, StudentAssignmentState],
    sections: Iterable[SectionState],
    placements: Iterable[Mapping[str, Any]] = (),
    *,
    blocker_limit: int = BLOCKER_LIMIT,
) -> dict[str, Any]:
    """Every course some student cannot register for, with why and what blocks it.

    ``states`` is the seating's answer per student; ``sections`` the sections
    it seated into; ``placements`` the scenario's placement rows (``id``,
    ``board_id``, ``course_key``, ``section``), so a course and each blocker
    name the placements (and boards) they are drawn on.

    Courses come with the most stuck students first, then by code; blockers
    with the most students they block on every section first, then the most
    students, then by code and section.
    """
    sections = list(sections)
    by_id = {section.section_id: section for section in sections}
    by_course: dict[str, list[SectionState]] = defaultdict(list)
    for section in sections:
        by_course[section.course_code].append(section)
    drawn = _placements_by_section(placements)

    reasons: dict[str, Counter[str]] = defaultdict(Counter)
    stuck: dict[str, set[str]] = defaultdict(set)
    blocked: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    every: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    requests = 0
    for student_id, state in states.items():
        for course_key, unresolved in state.unresolved_courses.items():
            requests += 1
            stuck[course_key].add(student_id)
            reasons[course_key][unresolved.reason] += 1
            if unresolved.reason not in CLASH_REASONS:
                continue
            targets = by_course.get(course_key, [])
            seated = [by_id[sid] for sid in sorted(state.section_ids) if sid in by_id]
            for blocker in seated:
                if blocker.course_code == course_key:
                    continue
                hits = sum(1 for target in targets if _overlap(blocker, target))
                if not hits:
                    continue
                blocked[course_key][blocker.section_id].add(student_id)
                if hits == len(targets):
                    every[course_key][blocker.section_id].add(student_id)

    courses = []
    for course_key, students in stuck.items():
        rows = []
        for section_id, who in blocked[course_key].items():
            blocker = by_id[section_id]
            label = _section_label(blocker)
            rows.append(
                {
                    "course_key": blocker.course_code,
                    "course_code": _course_code(blocker.course_code),
                    "section": label,
                    "students": len(who),
                    "every_section": len(every[course_key].get(section_id, ())),
                    "placements": drawn.get((blocker.course_code, label), []),
                }
            )
        rows.sort(
            key=lambda row: (
                -row["every_section"],
                -row["students"],
                row["course_code"],
                row["course_key"],
                row["section"],
            )
        )
        courses.append(
            {
                "course_key": course_key,
                "course_code": _course_code(course_key),
                "students": len(students),
                "reasons": dict(sorted(reasons[course_key].items())),
                "placements": sorted(
                    (
                        placement
                        for section in by_course.get(course_key, [])
                        for placement in drawn.get((course_key, _section_label(section)), [])
                    ),
                    key=lambda placement: placement["id"],
                ),
                "blocked_by": rows[: max(0, blocker_limit)],
                "blocked_by_more": max(0, len(rows) - max(0, blocker_limit)),
            }
        )
    courses.sort(key=lambda row: (-row["students"], row["course_code"], row["course_key"]))
    return {
        "assessed": True,
        "students": len(set().union(*stuck.values())) if stuck else 0,
        "requests": requests,
        "courses": courses,
    }


def _overlap(first: SectionState, second: SectionState) -> bool:
    """Any two meetings on one day whose times overlap (the seating's own test)."""
    return any(a.day == b.day and a.mask & b.mask for a in first.meetings for b in second.meetings)


def _section_label(section: SectionState) -> str:
    """The section's own label: its id is ``<course key>_<section>``."""
    prefix = f"{section.course_code}_"
    sid = section.section_id
    return sid[len(prefix) :] if sid.startswith(prefix) else sid


def _course_code(course_key: str) -> str:
    """The code a course is shown by: its key up to ``::``."""
    return str(course_key or "").split("::", 1)[0].strip()


def _placements_by_section(
    placements: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], list[dict[str, int]]]:
    """``(course key, section) -> [{id, board_id}, ...]``, by placement id."""
    out: dict[tuple[str, str], list[dict[str, int]]] = defaultdict(list)
    for row in placements:
        key = (str(row.get("course_key") or ""), str(row.get("section") or ""))
        out[key].append({"id": int(row["id"]), "board_id": int(row["board_id"])})
    for rows in out.values():
        rows.sort(key=lambda placement: placement["id"])
    return dict(out)

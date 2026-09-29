"""
core/services/section_plan_pipeline.py
The one pipeline behind Section Planning's Generate and Export.

The department plans ONE section (campus) at a time: the male section (``M``)
or the female section (``F``), never both together. Demand is per student
(``reporting.build_planning_demand``) of the chosen section only, so a student
of the other section never changes the plan; students with no recorded section
are in neither plan, only counted (``no_section``).

Seat limits follow one order everywhere (``compute_section_plan``): a what-if
draft, then the lowest limit a programme in scope declares
(``lowest_declared_capacities``) or, for a resolved elective, its slot's limit
when lower (``slot_declared_limits`` + ``fold_slot_limits``), then the
25/40/50 rules. A limit belongs to the programme, not the section: both
sections of a programme are sized by the same limits. The timetable builder
sizes a section's scenario with the same functions.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from core.models import ProgrammeRequirement
from core.services.course_identity import planner_course_key
from core.services.reporting import (
    PLANNING_SECTIONS,
    PlanningDemand,
    StudentDemand,
    build_planning_demand,
)
from core.services.section_planning import (
    compute_plan_summary,
    compute_section_plan,
    fold_slot_limits,
    lowest_declared_capacities,
    programmes_with_students,
    slot_declared_limits,
)
from core.services.student_helpers import normalize_code

__all__ = [
    "PLANNING_SECTIONS",
    "SizingRules",
    "plan_all_programmes",
    "plan_programme",
    "plan_programmes",
    "size_demand",
]


@dataclass(frozen=True)
class SizingRules:
    max_local_4cr: int
    max_local_other: int
    max_external: int
    course_overrides: Mapping[str, int] = field(default_factory=dict)


@dataclass
class _Aggregates:
    """Seat demand keyed by planner course identity."""

    demand: Counter[str]
    metadata: dict[str, dict[str, object]]
    programs_of: dict[str, set[str]]


def _department(code: str) -> str:
    return "".join(ch for ch in code if ch.isalpha())


def _requirement_metadata(programs: Iterable[str]) -> dict[tuple[str, str], dict[str, object]]:
    """(programme, course code) -> the plan's own name, credits and department."""
    programs = sorted({p for p in programs if p})
    meta: dict[tuple[str, str], dict[str, object]] = {}
    if not programs:
        return meta
    for row in ProgrammeRequirement.objects.filter(program__in=programs).values(
        "program", "course_code", "course_name", "credit_hours"
    ):
        code = normalize_code(row["course_code"])
        if not code:
            continue
        meta[(str(row["program"]), code)] = {
            "course_code": code,
            "course_name": str(row.get("course_name") or "").strip(),
            "credit_hours": row.get("credit_hours") or 3,
            "department": _department(code),
        }
    return meta


def _aggregate(students: Iterable[StudentDemand]) -> _Aggregates:
    students = list(students)
    requirement_meta = _requirement_metadata(s.program for s in students)
    demand: Counter[str] = Counter()
    metadata: dict[str, dict[str, object]] = {}
    programs_of: dict[str, set[str]] = defaultdict(set)
    for student in students:
        for code in student.courses:
            meta = dict(requirement_meta.get((student.program, code), {}))
            if not meta:
                # Not in the student's plan (a resolved elective, for one): the
                # catalogue supplies name and credits inside compute_section_plan.
                meta = {"course_code": code, "department": _department(code)}
            key = planner_course_key(code, meta.get("course_name"))
            metadata.setdefault(key, meta)
            demand[key] += 1
            if student.program:
                programs_of[key].add(student.program)
    return _Aggregates(demand, metadata, programs_of)


def _dropped_report(dropped: Iterable[tuple[str, str, str, int]]) -> dict[str, Any]:
    """Slot demand that became no course: stated, never silently lost."""
    merged: Counter[tuple[str, str, str]] = Counter()
    for prog, slot, reason, n in dropped:
        merged[(prog, slot, reason)] += int(n)
    rows = [
        {"program": prog, "slot": slot, "reason": reason, "students": n}
        for (prog, slot, reason), n in sorted(merged.items())
    ]
    return {"dropped": rows, "dropped_total": sum(merged.values())}


def size_demand(
    demands: Iterable[PlanningDemand],
    *,
    capacity_programs: Iterable[str],
    rules: SizingRules,
    with_programs: bool = False,
) -> dict[str, Any]:
    """Size one scope of ONE section: a row per course, the summary its KPIs total."""
    demands = list(demands)
    sections = {demand.section for demand in demands}
    if len(sections) != 1:
        raise ValueError(f"a plan is for one section, not {sorted(sections)}")
    (section,) = sections
    aggregates = _aggregate(s for d in demands for s in d.students)
    capacities = lowest_declared_capacities(capacity_programs, aggregates.metadata)
    overrides = {normalize_code(k): int(v) for k, v in (rules.course_overrides or {}).items()}

    # Where each course's limit comes from; a resolved elective's slot can
    # lower it (the lowest declaration wins, as across programmes). The same
    # two functions size the timetable builder's scenario.
    slots = slot_declared_limits((p for d in demands for p in d.elective_picks), overrides)
    sources = fold_slot_limits(capacities, aggregates.metadata, slots, overrides)

    rows = compute_section_plan(
        aggregates.demand,
        max_local_4cr=rules.max_local_4cr,
        max_local_other=rules.max_local_other,
        max_external=rules.max_external,
        course_overrides=overrides,
        programme_capacities=capacities,
        course_metadata=aggregates.metadata,
    )
    rows.sort(key=lambda r: (r["department"], r["course_code"], r["course_name"]))
    for row in rows:
        row["limit_source"] = sources.get(row["course_key"], "rule")
        slot = slots.get(row["course_code"])
        row["slots"] = sorted(slot["slots"]) if slot else []
        if with_programs:
            row["programs"] = sorted(aggregates.programs_of.get(row["course_key"], set()))
    return {
        "section": section,
        "student_count": sum(len(d.students) for d in demands),
        "no_section": sum(d.no_section for d in demands),
        "plan": rows,
        "summary": compute_plan_summary(rows),
        "electives": _dropped_report(r for d in demands for r in d.dropped_slots),
    }


def plan_programme(
    year: int, semester: int, program: str, rules: SizingRules, *, section: str
) -> dict[str, Any]:
    """One programme's section, sized by the programme's own declared limits."""
    demand = build_planning_demand(year, semester, program, section=section)
    return size_demand([demand], capacity_programs=[program], rules=rules)


def plan_programmes(
    year: int, semester: int, programs: list[str], rules: SizingRules, *, section: str
) -> dict[str, Any]:
    """Several programmes' section: each on its own, and pooled as the builder pools them."""
    per_programme = []
    demands: list[PlanningDemand] = []
    for program in programs:
        demand = build_planning_demand(year, semester, program, section=section)
        demands.append(demand)
        per_programme.append(
            {
                "program": program,
                **size_demand([demand], capacity_programs=[program], rules=rules),
            }
        )
    combined = size_demand(demands, capacity_programs=programs, rules=rules, with_programs=True)
    return {
        "section": combined["section"],
        "student_count": combined["student_count"],
        "no_section": combined["no_section"],
        "combined_plan": combined["plan"],
        "combined_summary": combined["summary"],
        "electives": combined["electives"],
        "programs": per_programme,
    }


def plan_all_programmes(
    year: int, semester: int, rules: SizingRules, *, section: str
) -> dict[str, Any]:
    """Every student of the section, sized by the lowest limit any programme in scope declares.

    The programmes in scope are those with students, whatever their section:
    the limits panel shows the same lowest declared limit (a limit is the
    programme's, not the section's).
    """
    demand = build_planning_demand(year, semester, None, section=section)
    return size_demand([demand], capacity_programs=programmes_with_students(), rules=rules)

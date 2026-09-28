"""
core/services/section_plan_pipeline.py
The one pipeline behind Section Planning's Generate and Export.

Demand is per student (``reporting.build_planning_demand``), and every student
belongs to a cohort. Male and female students are never planned together, so
each course is sized once for the male cohort and once for the female cohort,
with the same seat limit, and its sections are M + F. Students with no recorded
gender are counted and reported on their own; they are never pooled silently
into either cohort, and they are not in the section totals.

Seat limits follow one order everywhere (``compute_section_plan``): a what-if
draft, then the lowest limit a programme in scope declares
(``lowest_declared_capacities``, shared with the timetable builder), then the
25/40/50 rules.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from core.models import ProgrammeRequirement
from core.services.course_identity import planner_course_key
from core.services.reporting import PlanningDemand, StudentDemand, build_planning_demand
from core.services.section_planning import (
    compute_plan_summary,
    compute_section_plan,
    lowest_declared_capacities,
)
from core.services.student_helpers import normalize_code

MALE = "M"
FEMALE = "F"
NO_GENDER = ""
COHORTS = (MALE, FEMALE)


@dataclass(frozen=True)
class SizingRules:
    max_local_4cr: int
    max_local_other: int
    max_external: int
    course_overrides: Mapping[str, int] = field(default_factory=dict)


@dataclass
class _Aggregates:
    """Seat demand per cohort, keyed by planner course identity."""

    by_cohort: dict[str, Counter[str]]
    metadata: dict[str, dict[str, object]]
    programs_of: dict[str, set[str]]
    students: Counter[str]


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
    by_cohort: dict[str, Counter[str]] = {c: Counter() for c in (*COHORTS, NO_GENDER)}
    metadata: dict[str, dict[str, object]] = {}
    programs_of: dict[str, set[str]] = defaultdict(set)
    headcount: Counter[str] = Counter()
    for student in students:
        cohort = student.cohort if student.cohort in COHORTS else NO_GENDER
        headcount[cohort] += 1
        for code in student.courses:
            meta = dict(requirement_meta.get((student.program, code), {}))
            if not meta:
                # Not in the student's plan (a resolved elective, for one): the
                # catalogue supplies name and credits inside compute_section_plan.
                meta = {"course_code": code, "department": _department(code)}
            key = planner_course_key(code, meta.get("course_name"))
            metadata.setdefault(key, meta)
            by_cohort[cohort][key] += 1
            if student.program:
                programs_of[key].add(student.program)
    return _Aggregates(by_cohort, metadata, programs_of, headcount)


def _merge_cohorts(
    sized: Mapping[str, list[dict[str, Any]]],
    unknown: Counter[str],
    programs_of: Mapping[str, set[str]] | None,
) -> list[dict[str, Any]]:
    """One row per course: M and F sized apart, Total = M + F, no-gender demand beside."""
    by_key: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for cohort, rows in sized.items():
        for row in rows:
            by_key[str(row["course_key"])][cohort] = row

    merged: list[dict[str, Any]] = []
    for key in sorted(set(by_key) | set(unknown)):
        rows = by_key.get(key, {})
        base = rows.get(MALE) or rows.get(FEMALE) or rows.get(NO_GENDER)
        if base is None:
            continue
        male = rows.get(MALE)
        female = rows.get(FEMALE)
        male_students = int(male["total_students"]) if male else 0
        female_students = int(female["total_students"]) if female else 0
        male_sections = int(male["num_sections"]) if male else 0
        female_sections = int(female["num_sections"]) if female else 0
        total_students = male_students + female_students
        num_sections = male_sections + female_sections
        max_per_section = int(base["max_per_section"])
        if num_sections:
            avg_per_section = math.ceil(total_students / num_sections)
            fill_percent = round((avg_per_section / max_per_section) * 100)
            if avg_per_section >= max_per_section:
                status = "full"
            elif avg_per_section < 10:
                status = "underfilled"
            else:
                status = ""
        else:
            avg_per_section = 0
            fill_percent = 0
            status = "no_gender"
        row = {
            "department": base["department"],
            "course_key": key,
            "course_code": base["course_code"],
            "course_name": base.get("course_name", ""),
            "credit_hours": base["credit_hours"],
            "is_external": base["is_external"],
            "max_per_section": max_per_section,
            "male_students": male_students,
            "female_students": female_students,
            "unknown_students": int(unknown.get(key, 0)),
            "male_sections": male_sections,
            "female_sections": female_sections,
            "total_students": total_students,
            "num_sections": num_sections,
            "avg_per_section": avg_per_section,
            "fill_percent": fill_percent,
            "status": status,
        }
        if programs_of is not None:
            row["programs"] = sorted(programs_of.get(key, set()))
        merged.append(row)
    merged.sort(key=lambda r: (r["department"], r["course_code"], r["course_name"]))
    return merged


def summarise(rows: list[dict[str, Any]], *, no_gender_students: int) -> dict[str, Any]:
    """Totals the KPIs, the Department Summary and the export all add up to.

    Only courses with male or female demand are planned; ``no_gender`` states
    what was left out and why, instead of folding it into a cohort.
    """
    planned = [row for row in rows if row["num_sections"]]
    summary = compute_plan_summary(planned)
    summary["male_sections"] = sum(row["male_sections"] for row in planned)
    summary["female_sections"] = sum(row["female_sections"] for row in planned)
    by_dept: dict[str, dict[str, int]] = defaultdict(lambda: {"male": 0, "female": 0})
    for row in planned:
        by_dept[row["department"]]["male"] += row["male_sections"]
        by_dept[row["department"]]["female"] += row["female_sections"]
    for dept in summary["departments"]:
        dept["male_sections"] = by_dept[dept["department"]]["male"]
        dept["female_sections"] = by_dept[dept["department"]]["female"]
    unknown_rows = [row for row in rows if row["unknown_students"]]
    summary["no_gender"] = {
        "students": int(no_gender_students),
        "seat_demand": sum(row["unknown_students"] for row in unknown_rows),
        "courses": len(unknown_rows),
    }
    return summary


def _slot_limits(
    picks: Iterable[tuple[str, str, str, int]], overrides: Mapping[str, int]
) -> dict[str, dict[str, Any]]:
    """Resolved elective code -> the slots it fills and the lowest limit among them.

    A resolved elective is taught for its SLOT: AI463 filling AI's AI1 slot,
    which declares 30, is planned at 30, not by the rule for AI463 itself (an
    external-flagged course would get 50). The slot's limit is the programme's
    own (AI2's AI1 row for an AI2 student); a what-if draft on the slot wins.
    A slot that declares nothing leaves the course to its own limit or rule.
    """
    picks = list(picks)
    keys = {(prog, slot) for prog, slot, _course, _n in picks}
    declared: dict[tuple[str, str], int] = {}
    if keys:
        for prog, code, cap in ProgrammeRequirement.objects.filter(
            program__in=sorted({p for p, _ in keys}),
            course_code__in=sorted({s for _, s in keys}),
            max_capacity__isnull=False,
        ).values_list("program", "course_code", "max_capacity"):
            if cap is not None and cap >= 1:
                declared[(str(prog), normalize_code(code))] = int(cap)
    info: dict[str, dict[str, Any]] = {}
    for prog, slot, course, _n in picks:
        entry = info.setdefault(course, {"slots": set(), "limit": None, "source": None})
        entry["slots"].add(slot)
        if slot in overrides:
            limit, source = int(overrides[slot]), "draft"
        elif (prog, slot) in declared:
            limit, source = declared[(prog, slot)], "slot"
        else:
            continue
        if entry["limit"] is None or limit < entry["limit"]:
            entry["limit"], entry["source"] = limit, source
    return info


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
    """Size one scope: per-cohort plans merged into M / F / Total rows."""
    demands = list(demands)
    aggregates = _aggregate(s for d in demands for s in d.students)
    capacities = lowest_declared_capacities(capacity_programs, aggregates.metadata)
    overrides = {normalize_code(k): int(v) for k, v in (rules.course_overrides or {}).items()}

    # Where each course's limit comes from; a resolved elective's slot can
    # lower it (the lowest declaration wins, as across programmes).
    slots = _slot_limits((p for d in demands for p in d.elective_picks), overrides)
    sources: dict[str, str] = {}
    for key, meta in aggregates.metadata.items():
        code = normalize_code(meta.get("course_code")) or key
        slot = slots.get(code)
        declared = capacities.get(key)
        if code in overrides:
            sources[key] = "draft"
        elif slot and slot["limit"] is not None and (declared is None or slot["limit"] < declared):
            capacities[key] = slot["limit"]
            sources[key] = slot["source"]
        elif declared is not None:
            sources[key] = "programme"
        else:
            sources[key] = "rule"

    def plan(aggregate: Counter[str]) -> list[dict[str, Any]]:
        return compute_section_plan(
            aggregate,
            max_local_4cr=rules.max_local_4cr,
            max_local_other=rules.max_local_other,
            max_external=rules.max_external,
            course_overrides=overrides,
            programme_capacities=capacities,
            course_metadata=aggregates.metadata,
        )

    sized = {cohort: plan(aggregates.by_cohort[cohort]) for cohort in COHORTS}
    # Sized too, only so a course nobody else needs still shows its name and
    # limit; its sections are never added to M or F.
    sized[NO_GENDER] = plan(aggregates.by_cohort[NO_GENDER])
    rows = _merge_cohorts(
        sized,
        aggregates.by_cohort[NO_GENDER],
        aggregates.programs_of if with_programs else None,
    )
    for row in rows:
        row["limit_source"] = sources.get(row["course_key"], "rule")
        slot = slots.get(row["course_code"])
        row["slots"] = sorted(slot["slots"]) if slot else []
    headcount = aggregates.students
    return {
        "student_count": sum(headcount.values()),
        "cohorts": {
            "M": int(headcount.get(MALE, 0)),
            "F": int(headcount.get(FEMALE, 0)),
            "no_gender": int(headcount.get(NO_GENDER, 0)),
        },
        "plan": rows,
        "summary": summarise(rows, no_gender_students=int(headcount.get(NO_GENDER, 0))),
        "electives": _dropped_report(r for d in demands for r in d.dropped_slots),
    }


def _programmes(demand: PlanningDemand) -> list[str]:
    return sorted({s.program for s in demand.students if s.program})


def plan_programme(year: int, semester: int, program: str, rules: SizingRules) -> dict[str, Any]:
    """One programme, sized by its own declared limits."""
    demand = build_planning_demand(year, semester, program)
    return size_demand([demand], capacity_programs=[program], rules=rules)


def plan_programmes(
    year: int, semester: int, programs: list[str], rules: SizingRules
) -> dict[str, Any]:
    """Several programmes: each on its own, and pooled as the builder pools them."""
    per_programme = []
    demands: list[PlanningDemand] = []
    for program in programs:
        demand = build_planning_demand(year, semester, program)
        demands.append(demand)
        per_programme.append(
            {
                "program": program,
                **size_demand([demand], capacity_programs=[program], rules=rules),
            }
        )
    combined = size_demand(demands, capacity_programs=programs, rules=rules, with_programs=True)
    return {
        "student_count": combined["student_count"],
        "cohorts": combined["cohorts"],
        "combined_plan": combined["plan"],
        "combined_summary": combined["summary"],
        "electives": combined["electives"],
        "programs": per_programme,
    }


def plan_all_programmes(year: int, semester: int, rules: SizingRules) -> dict[str, Any]:
    """Every student, sized by the lowest limit any programme in scope declares."""
    demand = build_planning_demand(year, semester, None)
    return size_demand([demand], capacity_programs=_programmes(demand), rules=rules)

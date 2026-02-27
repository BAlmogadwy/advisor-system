"""
core/services/section_planning.py
Section demand calculator for next-semester planning.

Takes aggregate recommendation counts (course_code → student_count) and applies
section capacity rules to compute how many sections are needed per course.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from core.models import Course, ProgrammeRequirement
from core.services.student_helpers import normalize_code

# ── Default capacity rules (matching legacy generate_course_sections.py) ──
LOCAL_DEPARTMENTS = frozenset({"AI", "DS", "CS", "IS", "CYB"})
DEFAULT_MAX_LOCAL_4CR = 25
DEFAULT_MAX_LOCAL_OTHER = 40
DEFAULT_MAX_EXTERNAL = 50


def _extract_department(course_code: str) -> str:
    """Extract the alphabetic department prefix from a course code (e.g. 'CS101' → 'CS')."""
    m = re.match(r"([A-Z]+)", normalize_code(course_code))
    return m.group(1) if m else "UNKNOWN"


def _build_course_info(
    course_codes: list[str],
) -> dict[str, dict[str, Any]]:
    """Batch-lookup credit hours, is_external, and department for a list of course codes.

    Falls back to ProgrammeRequirement if the Course row is missing.
    """
    info: dict[str, dict[str, Any]] = {}

    # 1) Bulk-query the Course table
    courses_qs = Course.objects.filter(course_code__in=course_codes).values_list(
        "course_code",
        "credit_hours",
        "is_external",
        "department",
    )
    for code, credits, is_ext, dept in courses_qs:
        ncode = normalize_code(code)
        info[ncode] = {
            "credit_hours": credits or 3,
            "is_external": bool(is_ext),
            "department": dept or _extract_department(ncode),
        }

    # 2) For any codes still missing, try ProgrammeRequirement
    missing = [c for c in course_codes if normalize_code(c) not in info]
    if missing:
        pr_qs = ProgrammeRequirement.objects.filter(course_code__in=missing).values_list(
            "course_code",
            "credit_hours",
        )
        seen: set[str] = set()
        for code, credits in pr_qs:
            ncode = normalize_code(code)
            if ncode in seen:
                continue
            seen.add(ncode)
            dept = _extract_department(ncode)
            info[ncode] = {
                "credit_hours": credits or 3,
                "is_external": dept not in LOCAL_DEPARTMENTS,
                "department": dept,
            }

    # 3) Anything still missing — derive from the code itself
    for code in course_codes:
        ncode = normalize_code(code)
        if ncode not in info:
            dept = _extract_department(ncode)
            info[ncode] = {
                "credit_hours": 3,
                "is_external": dept not in LOCAL_DEPARTMENTS,
                "department": dept,
            }

    return info


def _get_max_section_size(
    credit_hours: int,
    is_external: bool,
    department: str,
    max_local_4cr: int,
    max_local_other: int,
    max_external: int,
) -> int:
    """Determine the max students per section for a course."""
    if is_external or department not in LOCAL_DEPARTMENTS:
        return max_external
    return max_local_4cr if credit_hours >= 4 else max_local_other


def get_all_courses_with_defaults(
    max_local_4cr: int = DEFAULT_MAX_LOCAL_4CR,
    max_local_other: int = DEFAULT_MAX_LOCAL_OTHER,
    max_external: int = DEFAULT_MAX_EXTERNAL,
) -> list[dict[str, Any]]:
    """Return every distinct course from ProgrammeRequirement with its computed default capacity.

    Used by the "Advanced per-course settings" UI so the user can see and override
    individual course capacities before generating.
    """
    # Collect unique courses from ProgrammeRequirement (the curriculum catalog)
    pr_qs = ProgrammeRequirement.objects.values_list(
        "course_code",
        "credit_hours",
    ).order_by("course_code")

    seen: dict[str, dict[str, Any]] = {}
    for code, credits in pr_qs:
        ncode = normalize_code(code)
        if ncode in seen:
            continue
        dept = _extract_department(ncode)
        is_ext = dept not in LOCAL_DEPARTMENTS
        seen[ncode] = {
            "credit_hours": credits or 3,
            "is_external": is_ext,
            "department": dept,
        }

    # Overlay with Course table data (more authoritative for is_external/department)
    course_codes = list(seen.keys())
    if course_codes:
        courses_qs = Course.objects.filter(course_code__in=course_codes).values_list(
            "course_code",
            "credit_hours",
            "is_external",
            "department",
        )
        for code, credits, is_ext, dept in courses_qs:
            ncode = normalize_code(code)
            if ncode in seen:
                seen[ncode]["credit_hours"] = credits or seen[ncode]["credit_hours"]
                seen[ncode]["is_external"] = bool(is_ext)
                if dept:
                    seen[ncode]["department"] = dept

    result: list[dict[str, Any]] = []
    for ncode, ci in sorted(seen.items(), key=lambda x: (x[1]["department"], x[0])):
        default_max = _get_max_section_size(
            ci["credit_hours"],
            ci["is_external"],
            ci["department"],
            max_local_4cr,
            max_local_other,
            max_external,
        )
        result.append(
            {
                "course_code": ncode,
                "department": ci["department"],
                "credit_hours": ci["credit_hours"],
                "is_external": ci["is_external"],
                "default_max": default_max,
            }
        )

    return result


def compute_section_plan(
    aggregate: Counter[str],
    max_local_4cr: int = DEFAULT_MAX_LOCAL_4CR,
    max_local_other: int = DEFAULT_MAX_LOCAL_OTHER,
    max_external: int = DEFAULT_MAX_EXTERNAL,
    course_overrides: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Compute section demand from aggregate recommendation counts.

    Parameters
    ----------
    aggregate : Counter[str]
        Mapping of normalised course_code → number of students who need it.
    max_local_4cr : int
        Max section capacity for local-department courses with 4+ credits.
    max_local_other : int
        Max section capacity for local-department courses with <4 credits.
    max_external : int
        Max section capacity for external/service courses.
    course_overrides : dict[str, int] | None
        Optional per-course capacity overrides. Keys are normalised course codes,
        values are the custom max-per-section for that course.

    Returns
    -------
    list[dict]
        One entry per course with section breakdown, sorted by department + code.
    """
    if not aggregate:
        return []

    overrides = course_overrides or {}
    course_codes = list(aggregate.keys())
    course_info = _build_course_info(course_codes)

    plan: list[dict[str, Any]] = []

    for code, total_students in aggregate.items():
        ncode = normalize_code(code)
        ci = course_info.get(
            ncode,
            {
                "credit_hours": 3,
                "is_external": False,
                "department": _extract_department(ncode),
            },
        )

        # Use per-course override if provided, otherwise use global rule
        if ncode in overrides:
            max_per_section = max(1, overrides[ncode])
        else:
            max_per_section = _get_max_section_size(
                ci["credit_hours"],
                ci["is_external"],
                ci["department"],
                max_local_4cr,
                max_local_other,
                max_external,
            )

        num_sections = max(1, math.ceil(total_students / max_per_section))
        avg_per_section = math.ceil(total_students / num_sections)
        fill_pct = round((avg_per_section / max_per_section) * 100)

        if avg_per_section >= max_per_section:
            status = "full"
        elif avg_per_section < 10:
            status = "underfilled"
        else:
            status = ""

        plan.append(
            {
                "department": ci["department"],
                "course_code": ncode,
                "credit_hours": ci["credit_hours"],
                "is_external": ci["is_external"],
                "total_students": total_students,
                "num_sections": num_sections,
                "max_per_section": max_per_section,
                "avg_per_section": avg_per_section,
                "fill_percent": fill_pct,
                "status": status,
            }
        )

    plan.sort(key=lambda r: (r["department"], r["course_code"]))
    return plan


def compute_plan_summary(plan: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute summary statistics from a section plan.

    Returns
    -------
    dict
        Overall totals + per-department breakdown.
    """
    total_courses = len(plan)
    total_sections = sum(r["num_sections"] for r in plan)
    total_students = sum(r["total_students"] for r in plan)
    avg_fill = round(sum(r["fill_percent"] for r in plan) / total_courses) if total_courses else 0

    # Per-department breakdown
    dept_map: dict[str, dict[str, int]] = {}
    for row in plan:
        d = row["department"]
        if d not in dept_map:
            dept_map[d] = {"courses": 0, "sections": 0, "students": 0}
        dept_map[d]["courses"] += 1
        dept_map[d]["sections"] += row["num_sections"]
        dept_map[d]["students"] += row["total_students"]

    departments = [{"department": d, **v} for d, v in sorted(dept_map.items())]

    return {
        "total_courses": total_courses,
        "total_sections": total_sections,
        "total_students": total_students,
        "avg_fill_percent": avg_fill,
        "departments": departments,
    }

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
from collections.abc import Iterable, Mapping
from typing import Any

from core.models import Course, ElectiveCourse, ProgrammeRequirement
from core.services.student_helpers import normalize_code

# ── Default capacity rules (matching legacy generate_course_sections.py) ──
# "Our" departments: their courses take the local 25 (4+ credits) / 40 rules,
# every other department's the external rule. COE is one of ours (owner,
# 2026-09-28). The Section Planning page reads this set from the server; it
# must never keep a copy of its own.
LOCAL_DEPARTMENTS = frozenset({"AI", "DS", "CS", "IS", "CYB", "COE"})
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

    Falls back to the elective catalogue, then ProgrammeRequirement, if the
    Course row is missing.
    """
    info: dict[str, dict[str, Any]] = {}
    lookup_codes = sorted({normalize_code(code) for code in course_codes if normalize_code(code)})

    # 1) Bulk-query the Course table
    courses_qs = Course.objects.filter(course_code__in=lookup_codes).values_list(
        "course_code",
        "description",
        "credit_hours",
        "is_external",
        "department",
    )
    for code, description, credits, is_ext, dept in courses_qs:
        ncode = normalize_code(code)
        info[ncode] = {
            "course_name": description or "",
            "credit_hours": credits or 3,
            "is_external": bool(is_ext),
            "department": dept or _extract_department(ncode),
        }

    # 2) Real mapped programme electives live in ElectiveCourse, not Course.
    missing = [c for c in lookup_codes if normalize_code(c) not in info]
    if missing:
        elective_qs = (
            ElectiveCourse.objects.filter(course_code__in=missing)
            .values_list("course_code", "course_name", "credit_hours")
            .order_by("course_code", "programme")
        )
        seen: set[str] = set()
        for code, course_name, credits in elective_qs:
            ncode = normalize_code(code)
            if ncode in seen or ncode in info:
                continue
            seen.add(ncode)
            dept = _extract_department(ncode)
            info[ncode] = {
                "course_name": course_name or "",
                "credit_hours": credits or 3,
                "is_external": dept not in LOCAL_DEPARTMENTS,
                "department": dept,
            }

    # 3) For any codes still missing, try ProgrammeRequirement
    missing = [c for c in lookup_codes if normalize_code(c) not in info]
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
                "course_name": "",
                "credit_hours": credits or 3,
                "is_external": dept not in LOCAL_DEPARTMENTS,
                "department": dept,
            }

    # 4) Anything still missing - derive from the code itself
    for code in lookup_codes:
        ncode = normalize_code(code)
        if ncode not in info:
            dept = _extract_department(ncode)
            info[ncode] = {
                "course_name": "",
                "credit_hours": 3,
                "is_external": dept not in LOCAL_DEPARTMENTS,
                "department": dept,
            }

    return info


def _load_programme_capacities(
    program: str,
    course_codes: list[str],
) -> dict[str, int]:
    """Load non-null max_capacity values from ProgrammeRequirement for one program.

    Returns dict of normalised course_code -> max_capacity (only entries with
    non-null values >= 1).
    """
    qs = ProgrammeRequirement.objects.filter(
        program=program,
        course_code__in=course_codes,
        max_capacity__isnull=False,
    ).values_list("course_code", "max_capacity")
    result: dict[str, int] = {}
    for code, cap in qs:
        ncode = normalize_code(code)
        if cap is not None and cap >= 1:
            result[ncode] = cap
    return result


# Public alias for use by views
load_programme_capacities = _load_programme_capacities


def _course_code_of(key: str, meta: Mapping[str, object]) -> str:
    return normalize_code(meta.get("course_code")) or normalize_code(str(key).split("::", 1)[0])


def lowest_declared_capacities(
    programs: Iterable[str],
    course_metadata: Mapping[str, Mapping[str, object]],
) -> dict[str, int]:
    """The lowest seat limit any of ``programs`` declares for each planned course.

    ``course_metadata`` maps a planner course key to its metadata; the limit is
    looked up by that key's course code in every programme. A course several
    programmes share is sized for the most restrictive declaration among them.

    THE rule for a plan that pools programmes: the timetable builder applies it
    to a scenario, and Section Planning's all-programmes and multi-programme
    views apply it to the same demand, through this one function, so the two
    cannot drift apart.
    """
    programs = [str(p) for p in programs if str(p or "").strip()]
    capacities: dict[str, int] = {}
    if not programs or not course_metadata:
        return capacities
    codes_by_key = {key: _course_code_of(key, meta) for key, meta in course_metadata.items()}
    lookup_codes = sorted(set(codes_by_key.values()))
    for prog in programs:
        caps = _load_programme_capacities(prog, lookup_codes)
        for key, code in codes_by_key.items():
            cap = caps.get(code)
            if cap is None:
                continue
            if key not in capacities or cap < capacities[key]:
                capacities[key] = cap
    return capacities


def slot_declared_limits(
    picks: Iterable[tuple[str, str, str, int]], overrides: Mapping[str, int]
) -> dict[str, dict[str, Any]]:
    """Resolved elective code -> the slots it fills and the lowest limit among them.

    ``picks`` are (programme, slot, real course, students). A resolved elective
    is taught for its SLOT: AI463 filling AI's AI1 slot, which declares 30, is
    planned at 30, not by the rule for AI463 itself (an external-flagged course
    would get 50). The slot's limit is the programme's own (AI2's AI1 row for an
    AI2 student); a what-if draft on the slot wins. A slot that declares nothing
    leaves the course to its own limit or rule.

    THE slot rule: Section Planning and the timetable builder both apply it,
    through ``fold_slot_limits``, so the two cannot drift apart.
    """
    picks = list(picks)
    keys = {(str(prog), normalize_code(slot)) for prog, slot, _course, _n in picks}
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
    for prog, raw_slot, raw_course, _n in picks:
        slot = normalize_code(raw_slot)
        course = normalize_code(raw_course)
        entry = info.setdefault(course, {"slots": set(), "limit": None, "source": None})
        entry["slots"].add(slot)
        if slot in overrides:
            limit, source = int(overrides[slot]), "draft"
        elif (str(prog), slot) in declared:
            limit, source = declared[(str(prog), slot)], "slot"
        else:
            continue
        if entry["limit"] is None or limit < entry["limit"]:
            entry["limit"], entry["source"] = limit, source
    return info


def fold_slot_limits(
    capacities: dict[str, int],
    course_metadata: Mapping[str, Mapping[str, object]],
    slots: Mapping[str, Mapping[str, Any]],
    overrides: Mapping[str, int],
) -> dict[str, str]:
    """Lower each resolved elective's limit to its slot's, in place; returns each key's source.

    ``capacities`` is ``lowest_declared_capacities`` for the same metadata. A
    slot's limit replaces the course's own when it is lower (the lowest
    declaration wins, as across programmes). The source is ``draft`` (a what-if
    on the course), ``slot``/``draft`` (from the slot), ``programme`` or ``rule``.
    """
    sources: dict[str, str] = {}
    for key, meta in course_metadata.items():
        code = _course_code_of(key, meta)
        slot = slots.get(code)
        declared = capacities.get(key)
        if code in overrides:
            sources[key] = "draft"
        elif slot and slot["limit"] is not None and (declared is None or slot["limit"] < declared):
            capacities[key] = int(slot["limit"])
            sources[key] = str(slot["source"])
        elif declared is not None:
            sources[key] = "programme"
        else:
            sources[key] = "rule"
    return sources


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


def slot_electives(
    programs: Iterable[str], codes: Iterable[str], year: int, semester: int
) -> dict[str, list[dict[str, Any]]]:
    """Slot code -> per programme, the real electives it resolves to this term.

    Uses the resolver's own reading of the publication (``ElectiveSelection``),
    so the panel names exactly the courses Generate will plan under the slot.
    """
    from core.services.elective_validation import ElectiveMappingError, ElectiveSelection

    codes = sorted({normalize_code(c) for c in codes if normalize_code(c)})
    found: dict[str, list[dict[str, Any]]] = {}
    for program in programs:
        try:
            selection = ElectiveSelection(program, str(year), semester)
        except ElectiveMappingError:
            continue
        for code in codes:
            requirement, _ = selection.requirement(code)
            if requirement is None:
                continue  # not an elective slot in this programme
            status, options, _ = selection.resolve(code)
            found.setdefault(code, []).append(
                {
                    "program": program,
                    "status": status.lower(),
                    "courses": [str(o["course_code"]) for o in options],
                }
            )
    return found


def get_all_courses_with_defaults(
    max_local_4cr: int = DEFAULT_MAX_LOCAL_4CR,
    max_local_other: int = DEFAULT_MAX_LOCAL_OTHER,
    max_external: int = DEFAULT_MAX_EXTERNAL,
    program: str | list[str] | None = None,
    *,
    year: int | None = None,
    semester: int | None = None,
) -> list[dict[str, Any]]:
    """Return distinct courses from ProgrammeRequirement with computed default capacity.

    When *program* is provided (a single program string or a list of program
    strings), only courses that belong to those programmes are returned.
    When *program* is ``None``, all courses across every programme are returned
    (the original behaviour).

    Used by the "Advanced per-course settings" UI so the user can see and override
    individual course capacities before generating.
    """
    # Normalise program param into a list (or None)
    if isinstance(program, str):
        program_list: list[str] | None = [program]
    elif isinstance(program, list):
        program_list = list(program)  # defensive copy
    else:
        program_list = None

    # Collect unique courses from ProgrammeRequirement (the curriculum catalog)
    pr_qs = ProgrammeRequirement.objects.all()
    if program_list is not None:
        pr_qs = pr_qs.filter(program__in=program_list)
    pr_qs = pr_qs.values_list(
        "course_code",
        "credit_hours",
        "program",
        "max_capacity",
    ).order_by("course_code", "program")

    seen: dict[str, dict[str, Any]] = {}
    # Per programme on screen: who teaches the course, and the limit each saved.
    taught_by: dict[str, list[str]] = {}
    saved_limits: dict[str, dict[str, int]] = {}
    for code, credits, prog, cap in pr_qs:
        ncode = normalize_code(code)
        if program_list is not None:
            if prog not in taught_by.setdefault(ncode, []):
                taught_by[ncode].append(prog)
            if cap is not None and cap >= 1:
                saved_limits.setdefault(ncode, {})[prog] = cap
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

    # Overlay programme-specific max_capacity when a program is specified
    if program_list is not None and len(program_list) > 0:
        all_codes = list(seen.keys())
        if len(program_list) == 1:
            pr_caps = _load_programme_capacities(program_list[0], all_codes)
        else:
            # Multiple programs: take the minimum max_capacity across programs
            pr_caps: dict[str, int] = {}
            for prog in program_list:
                caps = _load_programme_capacities(prog, all_codes)
                for code, cap in caps.items():
                    if code in pr_caps:
                        pr_caps[code] = min(pr_caps[code], cap)
                    else:
                        pr_caps[code] = cap
        for entry in result:
            entry["programme_max"] = pr_caps.get(entry["course_code"])
            entry["programmes"] = taught_by.get(entry["course_code"], [])
            entry["programme_limits"] = saved_limits.get(entry["course_code"], {})
        # An elective slot lists the courses it resolves to this term: they are
        # planned under the slot, with the slot's limit.
        slots = (
            slot_electives(program_list, [e["course_code"] for e in result], year, semester)
            if year and semester
            else {}
        )
        for entry in result:
            entry["slot_electives"] = slots.get(entry["course_code"], [])
    else:
        # No programme on screen: Generate sizes every student by the lowest
        # limit any programme in scope declares (``lowest_declared_capacities``
        # over the programmes students belong to). Show that same limit, so the
        # panel's starting value is what the plan applies; nothing is saved here.
        lowest = lowest_declared_capacities(
            programmes_with_students(), {code: {"course_code": code} for code in seen}
        )
        for entry in result:
            entry["programme_max"] = lowest.get(entry["course_code"])
            entry["limit_scope"] = "lowest_declared"
            entry["programmes"] = []
            entry["programme_limits"] = {}
            entry["slot_electives"] = []

    return result


def programmes_with_students() -> list[str]:
    """The programmes with students: those the all-programmes plan and its panel size by."""
    from core.models import Student

    return sorted(
        {
            str(p)
            for p in Student.objects.values_list("program", flat=True).distinct()
            if str(p or "").strip()
        }
    )


def compute_section_plan(
    aggregate: Counter[str],
    max_local_4cr: int = DEFAULT_MAX_LOCAL_4CR,
    max_local_other: int = DEFAULT_MAX_LOCAL_OTHER,
    max_external: int = DEFAULT_MAX_EXTERNAL,
    course_overrides: dict[str, int] | None = None,
    programme_capacities: dict[str, int] | None = None,
    course_metadata: dict[str, dict[str, Any]] | None = None,
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
    programme_capacities : dict[str, int] | None
        Optional per-programme per-course capacity limits from ProgrammeRequirement.
        Keys are normalised course codes, values are max_capacity.
        Priority: course_overrides > programme_capacities > global rules.

    Returns
    -------
    list[dict]
        One entry per course with section breakdown, sorted by department + code.
    """
    if not aggregate:
        return []

    overrides = course_overrides or {}
    pr_caps = programme_capacities or {}
    course_metadata = course_metadata or {}
    course_codes = [
        str(course_metadata.get(code, {}).get("course_code") or code) for code in aggregate.keys()
    ]
    course_info = _build_course_info(course_codes)

    plan: list[dict[str, Any]] = []

    for course_key, total_students in aggregate.items():
        meta = course_metadata.get(course_key, {})
        code = str(meta.get("course_code") or course_key)
        ncode = normalize_code(code)
        ci = course_info.get(
            ncode,
            {
                "course_name": str(meta.get("course_name") or ""),
                "credit_hours": int(meta.get("credit_hours") or 3),
                "is_external": False,
                "department": _extract_department(ncode),
            },
        )
        if meta.get("course_name"):
            ci["course_name"] = str(meta["course_name"])
        if meta.get("credit_hours"):
            ci["credit_hours"] = int(meta["credit_hours"])
        if meta.get("department"):
            ci["department"] = str(meta["department"])

        # 3-tier capacity resolution: override > programme > global rule
        if ncode in overrides:
            max_per_section = max(1, overrides[ncode])
        elif str(course_key) in pr_caps:
            max_per_section = max(1, pr_caps[str(course_key)])
        elif ncode in pr_caps:
            max_per_section = max(1, pr_caps[ncode])
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
                "course_key": str(course_key),
                "course_code": ncode,
                "course_name": ci.get("course_name", ""),
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
            dept_map[d] = {"courses": 0, "sections": 0, "students": 0, "total_credits": 0}
        dept_map[d]["courses"] += 1
        dept_map[d]["sections"] += row["num_sections"]
        dept_map[d]["students"] += row["total_students"]
        dept_map[d]["total_credits"] += row["credit_hours"] * row["num_sections"]

    departments = [{"department": d, **v} for d, v in sorted(dept_map.items())]

    return {
        "total_courses": total_courses,
        "total_sections": total_sections,
        "total_students": total_students,
        "avg_fill_percent": avg_fill,
        "departments": departments,
    }

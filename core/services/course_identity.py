from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable

from core.services.student_helpers import normalize_code


def normalize_course_name(value: object | None) -> str:
    """Return a stable planner-safe name fragment."""
    text = str(value or "").replace("\u00a0", " ").strip().upper()
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^A-Z0-9_]+", "", text)
    return text.strip("_")


def planner_course_key(course_code: object | None, course_name: object | None = None) -> str:
    """Build the internal identity used by timetable planning.

    The registrar-facing course code remains visible, but same-code courses
    with genuinely different names must not collapse into one section budget.
    """
    code = normalize_code(course_code)
    name = normalize_course_name(course_name)
    if not name or name == code:
        return code
    return f"{code}::{name}"


def display_course_label(course_code: object | None, course_name: object | None = None) -> str:
    code = normalize_code(course_code)
    name = str(course_name or "").strip()
    if not name or normalize_course_name(name) == code:
        return code
    return f"{code} - {name}"


def course_display_code(
    course_key: object | None, course_code: object | None, course_number: object | None = None
) -> str:
    """Resolve registrar code from a planner identity or split TermSection fields.

    Generated rows may store AI / 221 while imported rows store AI221 in both
    fields. Decode the authoritative planner key first. This is for linking
    institutional facts only; it must never replace the full offering identity.
    """
    key = normalize_code(course_key)
    code, number = normalize_code(course_code), normalize_code(course_number)
    if "::" in key:
        return key.split("::", 1)[0]
    # Legacy keys can be opaque section-like identifiers (e.g. C1S1). A
    # complete explicit registrar code is safer than decoding those keys.
    if any(char.isdigit() for char in code):
        return code
    if key:
        return key
    if not number:
        return code
    if number.startswith(code):
        return number
    return f"{code}{number}"


def programmes_serving_course(programs: Iterable[str]) -> Callable[[object], list[str]]:
    """Return ``serving(course_key)``: which of ``programs`` teach that course.

    Facts keyed by ``(programme, course_code)`` — ``CourseInstructor`` above all —
    are sound per programme, because a code is unique within one plan
    (``uq_programme_requirements_program_code``). They are NOT sound across
    programmes: CS112 is PROGRAMMING I in AI2/DS2 and PROGRAMMING II in AI/DS, so
    pooling every programme's CS112 row gives PROGRAMMING II the PROGRAMMING I
    lecturers. A programme serves ``course_key`` when its own plan names a course
    whose ``planner_course_key`` is that key.

    Two cases keep the old every-programme answer, because there is nothing to
    tell apart: a bare key (no name part), and a key no plan names — a resolved
    catalogue elective such as AI463 lives in ``ElectiveCourse``, not in any
    plan, and dropping its staffing would be a regression, not a fix. The
    result keeps the order of ``programs`` (callers pick a primary by it).
    """
    from core.models import ProgrammeRequirement

    ordered = list(dict.fromkeys(p for p in programs if p))
    owners: dict[str, set[str]] = defaultdict(set)
    for program, code, name in ProgrammeRequirement.objects.filter(program__in=ordered).values_list(
        "program", "course_code", "course_name"
    ):
        owners[planner_course_key(code, name)].add(program)

    def serving(course_key: object) -> list[str]:
        key = str(course_key or "").strip().upper()
        if "::" not in key:
            return list(ordered)
        own = owners.get(key, set())
        return [p for p in ordered if p in own] or list(ordered)

    return serving

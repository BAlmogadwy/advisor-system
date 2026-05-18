from __future__ import annotations

import re

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

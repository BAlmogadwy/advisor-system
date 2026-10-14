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

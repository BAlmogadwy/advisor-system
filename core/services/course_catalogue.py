"""The set of course codes any authority in this system recognises.

This is the existence floor's authority: a code an adviser answer names must
exist in the catalogue, in the turn's own evidence, or in the student's
question.  It is deliberately the union of all three code-bearing tables — a
programme requirement can predate its catalogue row, and an elective option can
exist only in the elective table.

A tiny module of its own so BOTH adviser paths (the V2 loop and the legacy
agent) can share one loader without an import cycle: student_advisor_v2 already
imports virtual_advisor, so the legacy module must never import V2.
"""

from __future__ import annotations

import time

_CACHE: tuple[float, frozenset[str]] | None = None
_TTL_SECONDS = 600.0


def known_course_codes() -> frozenset[str]:
    """Every recognised course code, normalized, cached for ten minutes.

    Cached because the floor runs on every candidate answer of every turn and
    the set changes only on imports.
    """
    global _CACHE
    now = time.monotonic()
    if _CACHE is not None and now - _CACHE[0] < _TTL_SECONDS:
        return _CACHE[1]
    from core.models import Course, ElectiveCourse, ProgrammeRequirement
    from core.services.student_helpers import normalize_code

    codes: set[str] = set()
    for queryset in (
        Course.objects.values_list("course_code", flat=True),
        ProgrammeRequirement.objects.values_list("course_code", flat=True),
        ElectiveCourse.objects.values_list("course_code", flat=True),
    ):
        codes.update(normalize_code(value) for value in queryset if value)
    codes.discard("")
    frozen = frozenset(codes)
    _CACHE = (now, frozen)
    return frozen


def invalidate_cache() -> None:
    """Test hook: force the next read to hit the database."""
    global _CACHE
    _CACHE = None

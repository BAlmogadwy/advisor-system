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
_TTL_SECONDS = 60.0


def known_course_codes() -> frozenset[str]:
    """Every recognised course code, normalized, cached for one minute.

    Cached because the floor runs on every candidate answer of every turn.
    The TTL is short and every catalogue-writing admin view invalidates
    explicitly, so an operator import is visible to the floor immediately.
    """
    global _CACHE
    now = time.monotonic()
    if _CACHE is not None and now - _CACHE[0] < _TTL_SECONDS:
        return _CACHE[1]
    import re

    from core.models import Course, ElectiveCourse, ProgrammeRequirement

    # The CHECKER'S normalisation, not student_helpers.normalize_code: the
    # floor compares with answer_consistency._normalise_course_token, which
    # strips hyphens, while normalize_code keeps them - a hyphenated catalogue
    # row would be permanently "invented".  Spelled inline (strip everything
    # non-alphanumeric, uppercase) rather than imported, and a parity test in
    # tests/test_production_replay.py holds the two spellings together.
    codes: set[str] = set()
    for queryset in (
        Course.objects.values_list("course_code", flat=True),
        ProgrammeRequirement.objects.values_list("course_code", flat=True),
        ElectiveCourse.objects.values_list("course_code", flat=True),
    ):
        codes.update(re.sub(r"[^A-Za-z0-9]", "", str(value)).upper() for value in queryset if value)
    codes.discard("")
    frozen = frozenset(codes)
    _CACHE = (now, frozen)
    return frozen


_NAMES_CACHE: tuple[float, tuple[tuple[str, str], ...]] | None = None


def known_courses() -> tuple[tuple[str, str], ...]:
    """(code, display name) for every recognised course, cached like the codes.

    The resolver's fuzzy index: small (≈500 rows), rebuilt on the same TTL,
    dropped by the same invalidation the admin import views call.
    """
    global _NAMES_CACHE
    now = time.monotonic()
    if _NAMES_CACHE is not None and now - _NAMES_CACHE[0] < _TTL_SECONDS:
        return _NAMES_CACHE[1]
    import re

    from core.models import Course, ElectiveCourse, ProgrammeRequirement

    names: dict[str, str] = {}
    for code, name in ProgrammeRequirement.objects.values_list("course_code", "course_name"):
        key = re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()
        if key and str(name or "").strip():
            names.setdefault(key, str(name).strip())
    for code, name in ElectiveCourse.objects.values_list("course_code", "course_name"):
        key = re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()
        if key and str(name or "").strip():
            names.setdefault(key, str(name).strip())
    for code, name in Course.objects.values_list("course_code", "description"):
        key = re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()
        if key:
            names.setdefault(key, str(name or "").strip())
    frozen = tuple(sorted(names.items()))
    _NAMES_CACHE = (now, frozen)
    return frozen


def invalidate_cache() -> None:
    """Force the next read to hit the database.

    Called by every admin view that imports or deletes catalogue rows, and by
    an autouse test fixture - the cache is a process global, so without the
    fixture a warm empty-DB read from one test disabled the floor for every
    later test in the process, and rolled-back rows leaked across tests.
    """
    global _CACHE, _NAMES_CACHE
    _CACHE = None
    _NAMES_CACHE = None

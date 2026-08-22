"""The set of course codes any authority in this system recognises.

This is the existence floor's authority: a code an adviser answer names must
exist in the catalogue, in the turn's own evidence, or in the student's
question.  It is deliberately the union of all three code-bearing tables — a
programme requirement can predate its catalogue row, and an elective option can
exist only in the elective table.

A tiny module of its own so BOTH adviser paths (the V2 loop and the legacy
agent) can share one loader without an import cycle: student_advisor_v2 already
imports virtual_advisor, so the legacy module must never import V2.

ONE load, ONE timestamp.  The floor (`known_course_codes`) and the typo
resolver's index (`known_courses`) are two views of the same load: the
resolver's candidates are keys of the very mapping the floor is built from, so
a candidate the floor does not recognise cannot exist — not by test, by
construction.  They were two independently-warmed caches once, and in the skew
window after an uninvalidated write the resolver could vouch for a code the
floor had already dropped.
"""

from __future__ import annotations

import re
import time

_CACHE: tuple[float, frozenset[str], tuple[tuple[str, str], ...]] | None = None
_TTL_SECONDS = 60.0

#: Arabic-Indic (٠-٩) and Eastern Arabic-Indic (۰-۹) digits to ASCII.
_ARABIC_INDIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


def normalise_catalogue_code(value: object | None) -> str:
    """The CHECKER'S normalisation: fold Arabic-Indic digits FIRST, then strip
    everything non-alphanumeric, uppercase.

    The order matters — stripping first deletes the very digits a fold would
    have saved, so «MATH٢٤٣» became "MATH" and the row was permanently
    "invented" to the floor.  Mirrors `answer_consistency._normalise_course_token`
    (which the floor's membership test compares against) rather than
    `student_helpers.normalize_code` (which keeps hyphens); the parity test in
    tests/test_course_catalogue.py holds the two spellings together.
    """
    if value is None:
        return ""
    folded = str(value).translate(_ARABIC_INDIC_DIGITS)
    return _NON_ALNUM.sub("", folded).upper()


def _load() -> tuple[frozenset[str], tuple[tuple[str, str], ...]]:
    from core.models import Course, ElectiveCourse, ProgrammeRequirement

    # Name priority: requirement name, then elective name, then the Course
    # description.  A code whose every source has a blank name KEEPS its key
    # with an empty name — the floor must recognise it and the resolver may
    # still repair a typo of it; only the display name is missing.
    names: dict[str, str] = {}
    for source in (
        ProgrammeRequirement.objects.values_list("course_code", "course_name"),
        ElectiveCourse.objects.values_list("course_code", "course_name"),
        Course.objects.values_list("course_code", "description"),
    ):
        for code, name in source:
            key = normalise_catalogue_code(code)
            if not key:
                continue
            cleaned = str(name or "").strip()
            if key not in names:
                names[key] = cleaned
            elif not names[key] and cleaned:
                names[key] = cleaned
    return frozenset(names), tuple(sorted(names.items()))


def _cached() -> tuple[frozenset[str], tuple[tuple[str, str], ...]]:
    global _CACHE
    now = time.monotonic()
    if _CACHE is not None and now - _CACHE[0] < _TTL_SECONDS:
        return _CACHE[1], _CACHE[2]
    codes, names = _load()
    _CACHE = (now, codes, names)
    return codes, names


def known_course_codes() -> frozenset[str]:
    """Every recognised course code, normalized, cached for one minute.

    Cached because the floor runs on every candidate answer of every turn.
    The TTL is short and every catalogue-writing admin view invalidates
    explicitly, so an operator import is visible to the floor immediately.
    """
    return _cached()[0]


def known_courses() -> tuple[tuple[str, str], ...]:
    """(code, display name) for every recognised course — the resolver's
    fuzzy index, the SAME load as the floor (≈500 rows).  The name may be
    empty; the code never is."""
    return _cached()[1]


def invalidate_cache() -> None:
    """Force the next read to hit the database.

    Called by every admin view that imports or deletes catalogue rows, and by
    an autouse test fixture - the cache is a process global, so without the
    fixture a warm empty-DB read from one test disabled the floor for every
    later test in the process, and rolled-back rows leaked across tests.
    """
    global _CACHE
    _CACHE = None

"""Online-course helpers for timetable room handling."""

from __future__ import annotations

from collections.abc import Iterable

from django.db.models import F
from django.db.models.functions import Trim, Upper

from core.models import ProgrammeRequirement


def normalise_course_code(code: object | None) -> str:
    """Return the canonical comparison key for programme course codes."""
    return str(code or "").strip().upper()


def programmes_for_board(board: object) -> list[str]:
    """Split a board's comma-separated programme list without changing case."""
    return [p.strip() for p in str(getattr(board, "program", "") or "").split(",") if p.strip()]


class OnlineCourseLookup:
    """Cache online course-code lookups by exact programme set."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, ...], set[str]] = {}

    def codes_for_programmes(self, programmes: Iterable[str]) -> set[str]:
        key = tuple(sorted({str(p).strip() for p in programmes if str(p).strip()}))
        if not key:
            return set()
        if key not in self._cache:
            codes = ProgrammeRequirement.objects.filter(
                program__in=list(key),
                is_online=True,
            ).values_list("course_code", flat=True)
            self._cache[key] = {
                normalise_course_code(code) for code in codes if normalise_course_code(code)
            }
        return self._cache[key]

    def codes_for_board(self, board: object) -> set[str]:
        return self.codes_for_programmes(programmes_for_board(board))

    def is_online_course_for_board(self, board: object, course_code: object | None) -> bool:
        return normalise_course_code(course_code) in self.codes_for_board(board)


def exclude_online_course_codes(
    queryset,
    online_codes: Iterable[object],
    *,
    course_code_field: str = "term_section__course_code",
):
    """Exclude online courses from a placement queryset using DB-side normalization."""
    codes = sorted(
        {normalise_course_code(code) for code in online_codes if normalise_course_code(code)}
    )
    if not codes:
        return queryset
    return queryset.annotate(_online_course_code_norm=Upper(Trim(F(course_code_field)))).exclude(
        _online_course_code_norm__in=codes
    )


def exclude_online_courses_for_board(
    queryset,
    board: object,
    *,
    lookup: OnlineCourseLookup | None = None,
    course_code_field: str = "term_section__course_code",
):
    """Exclude placements whose visible course code is online for the board."""
    lookup = lookup or OnlineCourseLookup()
    return exclude_online_course_codes(
        queryset,
        lookup.codes_for_board(board),
        course_code_field=course_code_field,
    )

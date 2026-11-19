"""Database-backed academic terms used by the planner, adviser and settings page."""

from core.models import AcademicSettings


def load_defaults() -> dict[str, int]:
    # The migration seeds the singleton. An explicitly flushed database starts fresh.
    row, _ = AcademicSettings.objects.get_or_create(pk=1)
    return {
        "academic_year": row.academic_year,
        "term": row.term,
        "currentYear": row.current_year,
        "currentTerm": row.current_term,
    }


def save_defaults(academic_year: int, term: int, current_year: int, current_term: int) -> None:
    AcademicSettings.objects.update_or_create(
        pk=1,
        defaults={
            "academic_year": academic_year,
            "term": term,
            "current_year": current_year,
            "current_term": current_term,
        },
    )

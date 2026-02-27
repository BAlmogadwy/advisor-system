from collections import Counter

from core.models import Student
from core.services.recommender import recommend_next_courses


def get_student_ids(
    program: str | list[str] | None = None,
    section: str | None = None,
) -> list[int]:
    qs = Student.objects.all()
    if program:
        if isinstance(program, list):
            qs = qs.filter(program__in=program)
        else:
            qs = qs.filter(program=program)
    if section:
        qs = qs.filter(section=section)
    return list(qs.values_list("student_id", flat=True))


def build_aggregate_counts(
    year: int,
    semester: int,
    program: str | list[str] | None = None,
    section: str | None = None,
) -> tuple[int, Counter[str]]:
    student_ids = get_student_ids(program=program, section=section)
    aggregate: Counter[str] = Counter()

    for student_id in student_ids:
        recs = recommend_next_courses(student_id, year, semester)
        aggregate.update(recs)

    return len(student_ids), aggregate

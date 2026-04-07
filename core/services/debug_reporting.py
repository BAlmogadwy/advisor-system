from collections.abc import Iterable

from core.models import Student
from core.services.recommender import calculate_real_student_term, recommend_next_courses
from core.services.student_helpers import (
    get_prerequisites,
    get_student_passed_and_studying,
    get_student_program,
)


def _build_students_query(
    section: str | None,
    program: str | None,
    join_prefixes: Iterable[str] | None,
) -> list[int]:
    qs = Student.objects.all()
    if section:
        qs = qs.filter(section=section)
    if program:
        qs = qs.filter(program=program)
    if join_prefixes:
        from django.db.models import Q

        prefixes = [p for p in join_prefixes if p]
        if prefixes:
            prefix_q = Q()
            for p in prefixes:
                low = int(p) * (10 ** (7 - len(p)))
                high = (int(p) + 1) * (10 ** (7 - len(p)))
                prefix_q |= Q(student_id__gte=low, student_id__lt=high)
            qs = qs.filter(prefix_q)
    return list(qs.order_by("student_id").values_list("student_id", flat=True))


def build_recommendation_debug_report(
    current_academic_year: int,
    current_semester: int,
    section: str | None = None,
    program: str | None = None,
    join_year_prefixes: list[str] | None = None,
    limit: int = 150,
) -> dict:
    student_ids = _build_students_query(section, program, join_year_prefixes)[:limit]

    items: list[dict] = []
    for sid in student_ids:
        prog = get_student_program(sid) or ""
        passed, studying = get_student_passed_and_studying(sid)
        recs = recommend_next_courses(sid, current_academic_year, current_semester)
        real_term = calculate_real_student_term(sid, current_academic_year, current_semester)

        rec_details: list[dict] = []
        for code in recs:
            prereqs = get_prerequisites(code, prog)
            prereq_status = [
                {
                    "prerequisite": p,
                    "status": "PASSED"
                    if p in passed
                    else ("STUDYING" if p in studying else "MISSING"),
                }
                for p in prereqs
            ]
            rec_details.append(
                {
                    "course_code": code,
                    "prerequisites": prereqs,
                    "prerequisite_status": prereq_status,
                }
            )

        items.append(
            {
                "student_id": sid,
                "program": prog,
                "real_term": real_term,
                "next_term": real_term + 1,
                "passed": sorted(passed),
                "studying": sorted(studying),
                "recommended_courses": recs,
                "recommendation_details": rec_details,
            }
        )

    return {
        "count": len(items),
        "filters": {
            "section": section,
            "program": program,
            "join_year_prefixes": join_year_prefixes or [],
            "limit": limit,
            "year": current_academic_year,
            "semester": current_semester,
        },
        "items": items,
    }

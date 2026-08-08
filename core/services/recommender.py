from core.models import Prerequisite, ProgrammeRequirement
from core.services.credit_policy import RECOMMENDED_MAX_CREDITS
from core.services.eligibility import hour_gate, split_hour_prereqs
from core.services.student_helpers import (
    get_prerequisites,
    get_student_passed_and_studying,
    get_student_program,
    normalize_code,
)

# This is the ADVISORY cap the recommender fills up to — not the university's limit,
# which is higher. See core/services/credit_policy.py for why the two must stay apart.
# Re-exported under the old name so existing callers keep working.
MAX_CREDITS = RECOMMENDED_MAX_CREDITS


def calculate_real_student_term(
    student_id: int | str,
    current_academic_year: int,
    current_semester: int,
) -> int:
    join_year_hijri = int(str(student_id)[:2]) + 1400
    years_difference = current_academic_year - join_year_hijri
    terms_so_far = years_difference * 2 + current_semester - 1
    return terms_so_far


def get_all_department_courses(program: str) -> list[dict]:
    rows = (
        ProgrammeRequirement.objects.filter(
            program=program,
        )
        .order_by("programme_term")
        .values_list("course_code", "programme_term", "credit_hours")
    )
    return [{"code": normalize_code(r[0]), "term": r[1], "credits": r[2]} for r in rows]


def calculate_unlock_count(course_code: str, program: str) -> int:
    """Count how many courses in *program* list *course_code* as a prerequisite.

    Uses ``__contains`` (SQL LIKE) to match *course_code* inside the
    ``prerequisite_course_code`` field, which may store comma-separated values.
    """
    return Prerequisite.objects.filter(
        prerequisite_course_code__contains=course_code,
        program=program,
    ).count()


def _count_unlocks_from_prereqs(
    course_code: str,
    all_prereq_codes: list[str],
) -> int:
    """Replicate ``__contains`` semantics: count how many prerequisite rows
    have *course_code* as a substring of their ``prerequisite_course_code``
    value.  This matches the original per-candidate DB query exactly."""
    return sum(1 for p in all_prereq_codes if course_code in p)


def _next_term_candidates(
    student_id: int | str,
    current_academic_year: int,
    current_semester: int,
    program: str,
    passed: set,
    studying: set,
    effective_credits: int | None = None,
) -> list[dict]:
    """Courses the student may take in the coming term, ranked, BEFORE the credit cap.

    A candidate is not already passed/studying, has its prerequisites satisfied, sits
    on the coming term's odd/even parity, and is not a future-term course (due now or
    overdue). This is the single definition of "registrable this term" — shared by the
    recommendation list and by the eligible-count shown to students.
    """
    all_courses = get_all_department_courses(program)

    student_real_term = calculate_real_student_term(
        student_id, current_academic_year, current_semester
    )
    next_term = student_real_term + 1
    next_term_parity = next_term % 2

    # Batch-load all prerequisite_course_code values for this program (1 query
    # instead of N).  Used to compute unlock counts without per-candidate
    # queries.
    all_prereq_codes = list(
        Prerequisite.objects.filter(program=program).values_list(
            "prerequisite_course_code", flat=True
        )
    )

    def prereqs_ok(course_code: str) -> bool:
        course_prereqs, required_hours = split_hour_prereqs(get_prerequisites(course_code, program))
        courses_ok = all(pr in passed or pr in studying for pr in course_prereqs)
        if not courses_ok or not required_hours:
            return courses_ok
        if effective_credits is not None:
            return int(effective_credits) >= required_hours
        return bool(hour_gate(student_id, required_hours)["met"])

    def is_gs_course(course_code: str) -> bool:
        return normalize_code(course_code).startswith("GS")

    candidates: list[dict] = []
    for c in all_courses:
        code = c["code"]
        if code in passed or code in studying:
            continue
        if not prereqs_ok(code):
            continue
        if c["term"] % 2 != next_term_parity:
            continue
        if not (c["term"] < next_term or c["term"] == next_term):
            continue

        unlock = _count_unlocks_from_prereqs(code, all_prereq_codes)
        is_past = c["term"] < next_term
        cc = dict(c)
        cc["_unlock"] = unlock
        cc["_past_rank"] = 0 if is_past else 1
        cc["_gs_rank"] = 1 if is_gs_course(code) else 0
        candidates.append(cc)

    candidates.sort(
        key=lambda x: (-x["_unlock"], x["_past_rank"], x["term"], x["_gs_rank"], x["code"])
    )
    return candidates


def _select_within_credit_cap(candidates: list[dict], max_credits: int) -> list[str]:
    recommendations: list[str] = []
    total_credits = 0
    for course in candidates:
        credits = int(course.get("credits") or 0)
        if total_credits + credits <= max_credits:
            recommendations.append(course["code"])
            total_credits += credits
    return recommendations


def recommend_next_courses_for_state(
    student_id: int | str,
    current_academic_year: int,
    current_semester: int,
    *,
    passed: set[str],
    studying: set[str] | None = None,
    effective_credits: int | None = None,
    max_credits: int = MAX_CREDITS,
) -> list[str]:
    """Run the normal recommender against an in-memory academic state.

    This is used for read-only scenarios such as graduation forecasting. It never
    writes simulated passes to ``StudentCourse`` and deliberately leaves elective
    placeholders unresolved instead of inventing a concrete elective selection.
    """
    program = get_student_program(student_id)
    if not program:
        return []
    passed_n = {normalize_code(code) for code in passed if normalize_code(code)}
    studying_n = {normalize_code(code) for code in (studying or set()) if normalize_code(code)}
    candidates = _next_term_candidates(
        student_id,
        current_academic_year,
        current_semester,
        program,
        passed_n,
        studying_n,
        effective_credits,
    )
    return _select_within_credit_cap(candidates, max(0, int(max_credits)))


def eligible_next_term_courses(
    student_id: int | str, current_academic_year: int, current_semester: int
) -> list[str]:
    """Codes the student may register in for the coming term (no credit-load cap).

    Same rule as the recommendation list, minus the MAX_CREDITS trim — so it answers
    "what am I allowed to take this term", not "what should I take".
    """
    program = get_student_program(student_id)
    if not program:
        return []
    passed, studying = get_student_passed_and_studying(student_id)
    return [
        c["code"]
        for c in _next_term_candidates(
            student_id, current_academic_year, current_semester, program, passed, studying
        )
    ]


def recommend_next_courses(
    student_id: int | str,
    current_academic_year: int,
    current_semester: int,
    *,
    resolve_electives: bool = True,
) -> list[str]:
    program = get_student_program(student_id)
    if not program:
        return []

    passed, studying = get_student_passed_and_studying(student_id)
    candidates = _next_term_candidates(
        student_id, current_academic_year, current_semester, program, passed, studying
    )

    recommended_codes = _select_within_credit_cap(candidates, MAX_CREDITS)
    if not resolve_electives:
        return recommended_codes

    from core.services.reporting import resolve_elective_recommendations

    resolved = resolve_elective_recommendations(
        {int(student_id): recommended_codes},
        year=current_academic_year,
        semester=current_semester,
        program=program,
    )
    return resolved.get(int(student_id), recommended_codes)

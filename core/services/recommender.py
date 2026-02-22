from core.models import Prerequisite, ProgrammeRequirement
from core.services.student_helpers import (
    get_prerequisites,
    get_student_passed_and_studying,
    get_student_program,
    normalize_code,
)

MAX_CREDITS = 18


def calculate_real_student_term(
    student_id: int | str,
    current_academic_year: int,
    current_semester: int,
) -> int:
    join_year_hijri = int(str(student_id)[:2]) + 1400
    years_difference = current_academic_year - join_year_hijri
    terms_so_far = years_difference * 2 + current_semester
    return terms_so_far


def get_all_department_courses(program: str) -> list[dict]:
    rows = ProgrammeRequirement.objects.filter(
        program=program,
    ).order_by("programme_term").values_list("course_code", "programme_term", "credit_hours")
    return [{"code": normalize_code(r[0]), "term": r[1], "credits": r[2]} for r in rows]


def calculate_unlock_count(course_code: str, program: str) -> int:
    return Prerequisite.objects.filter(
        prerequisite_course_code__contains=course_code,
        program=program,
    ).count()


def recommend_next_courses(
    student_id: int | str,
    current_academic_year: int,
    current_semester: int,
) -> list[str]:
    program = get_student_program(student_id)
    if not program:
        return []

    passed, studying = get_student_passed_and_studying(student_id)
    all_courses = get_all_department_courses(program)

    student_real_term = calculate_real_student_term(student_id, current_academic_year, current_semester)
    next_term = student_real_term + 1
    next_term_parity = next_term % 2

    recommendations: list[dict] = []
    total_credits = 0

    def prereqs_ok(course_code: str) -> bool:
        return all(pr in passed or pr in studying for pr in get_prerequisites(course_code, program))

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

        unlock = calculate_unlock_count(code, program)
        is_past = c["term"] < next_term
        cc = dict(c)
        cc["_unlock"] = unlock
        cc["_past_rank"] = 0 if is_past else 1
        cc["_gs_rank"] = 1 if is_gs_course(code) else 0
        candidates.append(cc)

    candidates.sort(key=lambda x: (-x["_unlock"], x["_past_rank"], x["term"], x["_gs_rank"], x["code"]))

    for course in candidates:
        if total_credits + course["credits"] <= MAX_CREDITS:
            recommendations.append(course)
            total_credits += course["credits"]

    return [c["code"] for c in recommendations]

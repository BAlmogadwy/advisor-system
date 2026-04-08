"""
core/services/recommender_batch.py
Batch-optimized recommender for the timetable workspace.

Pre-loads ALL student data, programme requirements, and prerequisites
in a few queries, then processes all students in-memory.

Query count: O(1) instead of O(N*M) where N=students, M=courses.
"""

from __future__ import annotations

from collections import defaultdict

from core.models import Prerequisite, ProgrammeRequirement, Student, StudentCourse
from core.services.student_helpers import normalize_code

MAX_CREDITS = 18


def calculate_real_student_term(
    student_id: int | str,
    current_academic_year: int,
    current_semester: int,
) -> int:
    join_year_hijri = int(str(student_id)[:2]) + 1400
    years_difference = current_academic_year - join_year_hijri
    return years_difference * 2 + current_semester - 1


def batch_recommend(
    student_ids: list[int],
    program: str,
    current_academic_year: int,
    current_semester: int,
) -> dict[int, list[str]]:
    """Recommend courses for all students in a program in one batch.

    Returns dict: student_id -> list of recommended course codes.

    Total queries: ~5 (regardless of student count).
    """
    if not student_ids:
        return {}

    # ── Query 1: Programme requirements (courses for this program) ───
    dept_courses_raw = list(
        ProgrammeRequirement.objects.filter(program=program)
        .order_by("programme_term")
        .values_list("course_code", "programme_term", "credit_hours")
    )
    dept_courses = [
        {"code": normalize_code(r[0]), "term": r[1], "credits": r[2]}
        for r in dept_courses_raw
    ]

    # ── Query 2: All prerequisites for this program ──────────────────
    prereq_rows = list(
        Prerequisite.objects.filter(program=program)
        .values_list("course_code", "prerequisite_course_code")
    )
    # Build course -> [prerequisite_codes] lookup
    prereq_map: dict[str, list[str]] = defaultdict(list)
    all_prereq_codes: list[str] = []
    for course_code, prereq_code in prereq_rows:
        cc = normalize_code(course_code)
        for part in str(prereq_code).split(","):
            p = normalize_code(part)
            if p:
                prereq_map[cc].append(p)
        all_prereq_codes.append(prereq_code or "")

    # ── Query 3: All student courses (passed + studying) ─────────────
    sc_rows = list(
        StudentCourse.objects.filter(student_id__in=student_ids)
        .select_related("course")
        .values_list("student_id", "course__course_code", "status")
    )
    student_passed: dict[int, set[str]] = defaultdict(set)
    student_studying: dict[int, set[str]] = defaultdict(set)
    for sid, code, status in sc_rows:
        c = normalize_code(code)
        if status == "passed":
            student_passed[sid].add(c)
        elif status == "studying":
            student_studying[sid].add(c)

    # ── Pre-compute unlock counts (in-memory) ────────────────────────
    def count_unlocks(code: str) -> int:
        return sum(1 for p in all_prereq_codes if code in p)

    # ── Process each student in-memory ───────────────────────────────
    results: dict[int, list[str]] = {}

    for sid in student_ids:
        passed = student_passed.get(sid, set())
        studying = student_studying.get(sid, set())

        student_real_term = calculate_real_student_term(
            sid, current_academic_year, current_semester
        )
        next_term = student_real_term + 1
        next_term_parity = next_term % 2

        def prereqs_ok(code: str) -> bool:
            return all(pr in passed or pr in studying for pr in prereq_map.get(code, []))

        candidates: list[dict] = []
        for c in dept_courses:
            code = c["code"]
            if code in passed or code in studying:
                continue
            if not prereqs_ok(code):
                continue
            if c["term"] is None:
                continue
            if c["term"] % 2 != next_term_parity:
                continue
            if c["term"] > next_term:
                continue

            unlock = count_unlocks(code)
            is_past = c["term"] < next_term
            cc = dict(c)
            cc["_unlock"] = unlock
            cc["_past_rank"] = 0 if is_past else 1
            cc["_gs_rank"] = 1 if normalize_code(code).startswith("GS") else 0
            candidates.append(cc)

        candidates.sort(
            key=lambda x: (-x["_unlock"], x["_past_rank"], x["term"], x["_gs_rank"], x["code"])
        )

        recs: list[str] = []
        total_credits = 0
        for course in candidates:
            if total_credits + (course["credits"] or 0) <= MAX_CREDITS:
                recs.append(course["code"])
                total_credits += course["credits"] or 0

        if recs:
            results[sid] = recs

    return results


def batch_recommend_multi_program(
    student_ids: list[int],
    current_academic_year: int,
    current_semester: int,
) -> dict[int, list[str]]:
    """Recommend courses for students across ALL programs.

    Groups students by program, runs batch_recommend per group.
    Works when no program filter is specified.

    Total queries: ~5 per program (not per student).
    """
    if not student_ids:
        return {}

    # Group students by program (1 query)
    student_programs: dict[int, str] = {}
    for sid, prog in Student.objects.filter(
        student_id__in=student_ids
    ).values_list("student_id", "program"):
        if prog:
            student_programs[sid] = prog

    # Group student IDs by program
    by_program: dict[str, list[int]] = defaultdict(list)
    for sid in student_ids:
        prog = student_programs.get(sid)
        if prog:
            by_program[prog].append(sid)

    # Run batch per program
    results: dict[int, list[str]] = {}
    for prog, prog_sids in by_program.items():
        prog_results = batch_recommend(prog_sids, prog, current_academic_year, current_semester)
        results.update(prog_results)

    return results

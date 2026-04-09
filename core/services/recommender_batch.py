"""
core/services/recommender_batch.py
Batch-optimized course recommender for the timetable workspace.

Pre-loads ALL student data, programme requirements, and prerequisites in a
handful of queries, then processes every student entirely in-memory.

Performance
-----------
* **Query count**: ~5 total per programme (regardless of student count),
  compared to O(N * M) in the per-student recommender.
* Used by the timetable generation pipeline, aggregate demand reports,
  conflict matrix, and debug panels.

Algorithm (per student)
-----------------------
1. Compute the student's *real term* from their join year (first 2 digits
   of student ID) and the current academic year / semester.
2. Determine term *parity* (odd/even) for the *next* term to filter courses
   offered only in odd or even semesters.
3. Exclude courses already passed or currently being studied.
4. Verify all prerequisites are satisfied (passed or currently studying).
5. Rank remaining candidates by: unlock count (courses this course is a
   prerequisite for), past-due priority, term order, and GS-prefix
   deprioritisation.
6. Greedily fill up to ``MAX_CREDITS`` (18) credit hours.
"""

from __future__ import annotations

from collections import defaultdict

from core.models import Prerequisite, ProgrammeRequirement, Student, StudentCourse
from core.services.student_helpers import normalize_code

# Maximum credit hours a student may register for in one semester.
MAX_CREDITS = 18


def calculate_real_student_term(
    student_id: int | str,
    current_academic_year: int,
    current_semester: int,
) -> int:
    """Derive a student's actual academic term number from their ID and calendar.

    Saudi university student IDs embed the Hijri join year in the first two
    digits (e.g. ``44`` -> 1444 H).  The real term is calculated as::

        (current_year - join_year_hijri) * 2 + current_semester - 1

    This accounts for students who are ahead or behind the standard plan.

    Parameters
    ----------
    student_id : int | str
        The student's university ID.  Only the first 2 digits are used.
    current_academic_year : int
        Current Hijri academic year (e.g. 1446).
    current_semester : int
        Current semester number (1 or 2; 3 for summer).

    Returns
    -------
    int
        The 1-based term number the student is effectively in (e.g. 5 means
        they are in their 5th semester).
    """
    # First two digits of the student ID encode the short Hijri join year
    join_year_hijri = int(str(student_id)[:2]) + 1400
    years_difference = current_academic_year - join_year_hijri
    return years_difference * 2 + current_semester - 1


def batch_recommend(
    student_ids: list[int],
    program: str,
    current_academic_year: int,
    current_semester: int,
) -> dict[int, list[str]]:
    """Recommend next-semester courses for all students in a programme.

    Loads programme requirements, prerequisites, and student academic records
    in bulk (~5 queries total), then runs the recommendation algorithm
    entirely in-memory for each student.

    Parameters
    ----------
    student_ids : list[int]
        University IDs of students to process.
    program : str
        Programme code (e.g. ``"AI"``, ``"CS"``).
    current_academic_year : int
        Current Hijri academic year.
    current_semester : int
        Current semester number (1, 2, or 3).

    Returns
    -------
    dict[int, list[str]]
        Mapping of ``student_id`` -> ordered list of recommended course codes.
        Students with no recommendations are omitted from the dict.

    Business Rules
    ---------------
    * Courses already passed or being studied are excluded.
    * All prerequisites must be satisfied (passed **or** studying).
    * Only courses whose term parity matches the *next* term are eligible.
    * Courses from earlier terms (past-due) are prioritised over current-term.
    * Within a priority tier, courses that unlock the most downstream courses
      are ranked higher.
    * General Studies (``GS`` prefix) courses are deprioritised.
    * Total recommended credits never exceed ``MAX_CREDITS`` (18).
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
        {"code": normalize_code(r[0]), "term": r[1], "credits": r[2]} for r in dept_courses_raw
    ]

    # ── Query 2: All prerequisites for this program ──────────────────
    prereq_rows = list(
        Prerequisite.objects.filter(program=program).values_list(
            "course_code", "prerequisite_course_code"
        )
    )
    # Build course -> [prerequisite_codes] lookup
    # Hour-based prerequisites like "110(HOURS)" are stored separately.
    import re

    prereq_map: dict[str, list[str]] = defaultdict(list)
    hour_prereq_map: dict[str, int] = {}  # course_code -> required hours
    all_prereq_codes: list[str] = []
    _hour_pattern = re.compile(r"^(\d+)\s*\(?\s*HOURS?\s*\)?$", re.IGNORECASE)
    for course_code, prereq_code in prereq_rows:
        cc = normalize_code(course_code)
        for part in str(prereq_code).split(","):
            p = part.strip()
            hour_match = _hour_pattern.match(p)
            if hour_match:
                hour_prereq_map[cc] = int(hour_match.group(1))
            else:
                pn = normalize_code(p)
                if pn:
                    prereq_map[cc].append(pn)
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

    # ── Query 4: Student credit hours (for hour-based prerequisites) ──
    student_credits: dict[int, tuple[int, int]] = {}  # sid -> (earned, current_registered)
    if hour_prereq_map:
        from core.models import Student

        credit_rows = Student.objects.filter(student_id__in=student_ids).values_list(
            "student_id", "total_earned_credits", "current_registered_credits"
        )
        for sid, earned, current in credit_rows:
            student_credits[sid] = (earned or 0, current or 0)

    # ── Pre-compute unlock counts (in-memory) ────────────────────────
    # "Unlock count" = how many other courses list this course as a
    # prerequisite.  Higher unlock count means the course is more
    # strategically important (unblocks more of the curriculum).
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

        def prereqs_ok(code: str) -> bool:  # noqa: B023
            """True if every prerequisite for *code* has been passed or is being studied."""
            # Check hour-based prerequisite (e.g. "110(HOURS)")
            req_hours = hour_prereq_map.get(code, 0)
            if req_hours > 0:
                earned, current = student_credits.get(sid, (0, 0))  # noqa: B023
                # Relaxed: earned + current_registered (studying counts)
                if earned + current < req_hours:
                    return False
            # Check course-based prerequisites
            return all(pr in passed or pr in studying for pr in prereq_map.get(code, []))  # noqa: B023

        # Build candidate list: courses this student is eligible to take
        candidates: list[dict] = []
        for c in dept_courses:
            code = c["code"]
            if code in passed or code in studying:
                continue  # already completed or in-progress
            if not prereqs_ok(code):
                continue  # missing prerequisites
            if c["term"] is None:
                continue  # no term assigned in the plan
            if c["term"] % 2 != next_term_parity:
                continue  # course offered in the wrong semester parity
            if c["term"] > next_term:
                continue  # course is for a future term the student hasn't reached

            unlock = count_unlocks(code)
            is_past = c["term"] < next_term  # past-due course from an earlier term
            cc = dict(c)
            cc["_unlock"] = unlock  # higher = unblocks more courses
            cc["_past_rank"] = 0 if is_past else 1  # 0 = past-due (prioritised)
            cc["_gs_rank"] = 1 if normalize_code(code).startswith("GS") else 0  # deprioritise GS
            candidates.append(cc)

        # Sort candidates by priority:
        #   1. Most unlocks first (descending)
        #   2. Past-due courses first (0 < 1)
        #   3. Earlier programme term first
        #   4. Non-GS courses first (0 < 1)
        #   5. Alphabetical by code (tie-breaker for determinism)
        candidates.sort(
            key=lambda x: (-x["_unlock"], x["_past_rank"], x["term"], x["_gs_rank"], x["code"])
        )

        # Greedy knapsack: take courses in priority order until credit cap
        recs: list[str] = []
        total_credits = 0
        for course in candidates:
            if total_credits + (course["credits"] or 0) <= MAX_CREDITS:
                recs.append(course["code"])
                total_credits += course["credits"] or 0

        if recs:
            results[sid] = recs

    # NOTE: Elective placeholder resolution (AI1→AI461) happens only during
    # timetable generation (timetable_generate.py), not here. The recommender
    # returns plan codes as-is so Batch Recommender shows clean counts.

    return results


def batch_recommend_multi_program(
    student_ids: list[int],
    current_academic_year: int,
    current_semester: int,
) -> dict[int, list[str]]:
    """Recommend courses for students across ALL programmes.

    When the caller has no programme filter (e.g. the "all programmes"
    aggregate view), this function groups students by their enrolled
    programme and delegates to ``batch_recommend`` once per group.

    Parameters
    ----------
    student_ids : list[int]
        University IDs of students to process.
    current_academic_year : int
        Current Hijri academic year.
    current_semester : int
        Current semester number (1, 2, or 3).

    Returns
    -------
    dict[int, list[str]]
        Combined mapping of ``student_id`` -> recommended course codes,
        merging results from all programme-specific batches.

    Query Complexity
    ----------------
    1 query to group students by programme, then ~5 queries per distinct
    programme.  Still O(P) not O(N) where P = number of programmes.
    """
    if not student_ids:
        return {}

    # Group students by program (1 query)
    student_programs: dict[int, str] = {}
    for sid, prog in Student.objects.filter(student_id__in=student_ids).values_list(
        "student_id", "program"
    ):
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

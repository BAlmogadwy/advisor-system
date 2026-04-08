from collections import defaultdict
from collections.abc import Iterable

from core.models import Prerequisite, Student, StudentCourse
from core.services.recommender import calculate_real_student_term, recommend_next_courses
from core.services.recommender_batch import batch_recommend, batch_recommend_multi_program
from core.services.student_helpers import (
    get_prerequisites,
    get_student_passed_and_studying,
    get_student_program,
    normalize_code,
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

        prefixes = [p for p in join_prefixes if p and p.isdigit()]
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

    if not student_ids:
        return {
            "count": 0,
            "filters": {
                "section": section, "program": program,
                "join_year_prefixes": join_year_prefixes or [],
                "limit": limit, "year": current_academic_year, "semester": current_semester,
            },
            "items": [],
        }

    # ── Batch-load all data (3 queries instead of N*M) ───────────
    # 1. Student programs
    student_programs: dict[int, str] = {}
    for sid_val, prog_val in Student.objects.filter(
        student_id__in=student_ids
    ).values_list("student_id", "program"):
        student_programs[sid_val] = prog_val or ""

    # 2. Student courses (passed + studying)
    sc_rows = list(
        StudentCourse.objects.filter(student_id__in=student_ids)
        .select_related("course")
        .values_list("student_id", "course__course_code", "status")
    )
    student_passed: dict[int, set[str]] = defaultdict(set)
    student_studying: dict[int, set[str]] = defaultdict(set)
    for sid_val, code, status in sc_rows:
        c = normalize_code(code)
        if status == "passed":
            student_passed[sid_val].add(c)
        elif status == "studying":
            student_studying[sid_val].add(c)

    # 3. All prerequisites for involved programs
    involved_programs = set(student_programs.values())
    prereq_map: dict[str, dict[str, list[str]]] = {}  # program -> course -> [prereqs]
    for prog_val in involved_programs:
        if not prog_val:
            continue
        prereq_map[prog_val] = defaultdict(list)
        for course_code, prereq_code in Prerequisite.objects.filter(
            program=prog_val
        ).values_list("course_code", "prerequisite_course_code"):
            cc = normalize_code(course_code)
            for part in str(prereq_code).split(","):
                p = normalize_code(part)
                if p:
                    prereq_map[prog_val][cc].append(p)

    # 4. Batch recommendations
    if program:
        all_recs = batch_recommend(student_ids, program, current_academic_year, current_semester)
    else:
        all_recs = batch_recommend_multi_program(student_ids, current_academic_year, current_semester)

    # ── Build items from pre-loaded data ─────────────────────────
    items: list[dict] = []
    for sid in student_ids:
        prog = student_programs.get(sid, "")
        passed = student_passed.get(sid, set())
        studying = student_studying.get(sid, set())
        recs = all_recs.get(sid, [])
        real_term = calculate_real_student_term(sid, current_academic_year, current_semester)

        prog_prereqs = prereq_map.get(prog, {})
        rec_details: list[dict] = []
        for code in recs:
            prereqs = prog_prereqs.get(normalize_code(code), [])
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

"""
core/services/credit_shortfall_analysis.py
Credit shortfall analysis for students below minimum recommended credits.

Finds students whose recommended course load is below a threshold (default
12 credits) and identifies:
  - Blocked courses: prerequisites not yet passed/studying
  - Recoverable courses: prerequisites met but not recommended (could be added)

Ported from taibahScraping/utils/analyse_live_shortfall.py to use Django models.
"""

from __future__ import annotations

import logging
from collections import Counter

from core.models import (
    Course,
    Prerequisite,
    ProgrammeRequirement,
    Student,
    StudentCourse,
)
from core.services.recommender_batch import batch_recommend
from core.services.reporting import get_student_ids

logger = logging.getLogger(__name__)

MIN_CREDITS = 12


def _build_credit_map(programs: list[str]) -> dict[str, int]:
    """Build course_code → credit_hours lookup from Course table + ProgrammeRequirement."""
    credit_map: dict[str, int] = {}
    for c in Course.objects.all():
        if c.credit_hours:
            credit_map[c.course_code] = c.credit_hours
    # ProgrammeRequirement may have different credits per program — override
    for pr in ProgrammeRequirement.objects.filter(program__in=programs):
        if pr.credit_hours:
            credit_map[pr.course_code] = pr.credit_hours
    return credit_map


def _get_student_courses(student_id: int) -> tuple[set[str], set[str]]:
    """Return (passed_codes, studying_codes) for a student."""
    passed = set(
        StudentCourse.objects.filter(student_id=student_id, status="passed")
        .select_related("course")
        .values_list("course__course_code", flat=True)
    )
    studying = set(
        StudentCourse.objects.filter(student_id=student_id, status="studying")
        .select_related("course")
        .values_list("course__course_code", flat=True)
    )
    return passed, studying


def _get_prerequisites(course_code: str, program: str) -> list[str]:
    """Return list of prerequisite course codes for a course in a program."""
    return list(
        Prerequisite.objects.filter(course_code=course_code, program=program).values_list(
            "prerequisite_course_code", flat=True
        )
    )


def analyse_student_shortfall(
    student_id: int,
    program: str,
    recommendations: list[str],
    credit_map: dict[str, int],
    min_credits: int = MIN_CREDITS,
) -> dict | None:
    """Analyse a single student's credit shortfall.

    Returns None if the student has enough credits (>= min_credits).
    Otherwise returns a dict with shortfall details.
    """
    total_credits = sum(credit_map.get(c, 3) for c in recommendations)

    if total_credits >= min_credits:
        return None

    passed, studying = _get_student_courses(student_id)

    # All courses in this student's programme plan
    plan_courses = set(
        ProgrammeRequirement.objects.filter(program=program).values_list("course_code", flat=True)
    )

    # Courses remaining: not passed, not studying, not recommended
    remaining = plan_courses - passed - studying - set(recommendations)

    # Resolve elective placeholders to real courses using ElectiveTermMapping.
    # If a student still needs IS3 (placeholder), check if they can take
    # any of the mapped elective courses (IS481, IS486, IS411).
    from core.models import ElectiveTermMapping

    elective_mappings: dict[str, list[str]] = {}
    for m in ElectiveTermMapping.objects.filter(programme=program).select_related("elective"):
        elective_mappings.setdefault(m.placeholder_code, []).append(m.elective.course_code)

    blocked: list[tuple[str, str]] = []
    recoverable: list[str] = []

    for code in sorted(remaining):
        # If this is an elective placeholder, check mapped courses instead
        if code in elective_mappings:
            eligible_electives = []
            for elec_code in elective_mappings[code]:
                if elec_code in passed or elec_code in studying:
                    continue  # already taken this elective
                # Check elective prerequisites
                from core.models import ElectiveCourse

                ec = ElectiveCourse.objects.filter(course_code=elec_code).first()
                if ec and ec.prerequisites_csv:
                    prereqs = [p.strip() for p in ec.prerequisites_csv.split(",") if p.strip()]
                    if all(p in passed or p in studying for p in prereqs):
                        eligible_electives.append(elec_code)
                else:
                    eligible_electives.append(elec_code)  # no prereqs

            if eligible_electives:
                recoverable.append(f"{code} ({'/'.join(eligible_electives)})")
            else:
                blocked.append((code, "no eligible elective courses"))
        else:
            prereqs = _get_prerequisites(code, program)
            missing = [p for p in prereqs if p not in passed and p not in studying]
            if missing:
                blocked.append((code, ", ".join(missing)))
            else:
                recoverable.append(code)

    # For students with 0 recommendations, classify their status:
    # - graduating: no remaining courses in plan (all passed/studying)
    # - near_graduating: only have remaining courses they're currently studying
    # - still_needs: have courses they haven't started yet
    graduation_status = None
    remaining_courses: list[str] = []
    studying_courses: list[str] = []

    if not recommendations:
        studying_codes = studying
        all_remaining = plan_courses - passed - studying
        remaining_courses = sorted(all_remaining)
        studying_courses = sorted(studying_codes & plan_courses)

        if len(all_remaining) == 0:
            graduation_status = "graduating"
        elif all_remaining <= set(r[0] for r in blocked):
            # All remaining are blocked by prerequisites
            graduation_status = "blocked"
        else:
            graduation_status = "still_needs"

    # Student metadata
    student = Student.objects.filter(student_id=student_id).first()

    return {
        "student_id": student_id,
        "program": program,
        "recommended_credits": total_credits,
        "recommended_courses": recommendations,
        "blocked_courses": blocked,
        "recoverable_courses": recoverable,
        "graduation_status": graduation_status,
        "remaining_courses": remaining_courses,
        "studying_courses": studying_courses,
        "gpa": student.gpa if student else None,
        "earned_credits": student.total_earned_credits if student else None,
    }


def run_shortfall_analysis(
    year: int,
    semester: int,
    programs: list[str],
    section: str | None = None,
    min_credits: int = MIN_CREDITS,
) -> dict:
    """Run credit shortfall analysis for a set of programs.

    Parameters
    ----------
    year : int
        Academic year (Hijri).
    semester : int
        Semester number (1, 2, or 3).
    programs : list[str]
        Program codes to analyse (e.g. ["IS", "IS2"]).
    section : str | None
        Optional section filter ("M", "F", or None for all).
    min_credits : int
        Minimum recommended credits threshold (default 12).

    Returns
    -------
    dict with keys:
        total_students, shortfall_count, ok_count,
        shortfall_students (list of per-student dicts),
        top_recoverable (list of (course_code, count, credits)),
        summary_by_program (dict of program → counts)
    """
    student_ids = get_student_ids(
        program=programs if len(programs) > 1 else programs[0],
        section=section,
    )

    logger.info(
        "Shortfall analysis: %d students, programs=%s, section=%s",
        len(student_ids),
        programs,
        section,
    )

    # Get recommendations per program
    all_recs: dict[int, list[str]] = {}
    student_program_map: dict[int, str] = {}

    for prog in programs:
        prog_sids = list(
            Student.objects.filter(student_id__in=student_ids, program=prog).values_list(
                "student_id", flat=True
            )
        )
        if prog_sids:
            recs = batch_recommend(prog_sids, prog, year, semester)
            all_recs.update(recs)
            for sid in prog_sids:
                student_program_map[sid] = prog

    credit_map = _build_credit_map(programs)

    # Analyse each student
    shortfall_students: list[dict] = []
    all_recoverable: Counter[str] = Counter()
    program_counts: dict[str, dict[str, int]] = {
        p: {"total": 0, "shortfall": 0, "ok": 0} for p in programs
    }

    for sid in student_ids:
        prog = student_program_map.get(sid)
        if not prog:
            continue

        program_counts[prog]["total"] += 1
        recs = all_recs.get(sid, [])

        result = analyse_student_shortfall(
            student_id=sid,
            program=prog,
            recommendations=recs,
            credit_map=credit_map,
            min_credits=min_credits,
        )

        if result:
            shortfall_students.append(result)
            program_counts[prog]["shortfall"] += 1
            for code in result["recoverable_courses"]:
                all_recoverable[code] += 1
        else:
            program_counts[prog]["ok"] += 1

    # Sort by credits ascending (worst first)
    shortfall_students.sort(key=lambda s: (s["recommended_credits"], s["student_id"]))

    # Top recoverable with credit info
    top_recoverable = [
        {
            "course_code": code,
            "count": count,
            "credits": credit_map.get(code, 3),
        }
        for code, count in all_recoverable.most_common(15)
    ]

    total = len(student_ids)
    shortfall_count = len(shortfall_students)

    logger.info(
        "Shortfall analysis complete: %d/%d below %d credits",
        shortfall_count,
        total,
        min_credits,
    )

    # Classify zero-recommendation students
    zero_rec_students = [s for s in shortfall_students if s["recommended_credits"] == 0]
    graduating = [s for s in zero_rec_students if s.get("graduation_status") == "graduating"]
    still_needs = [s for s in zero_rec_students if s.get("graduation_status") == "still_needs"]
    blocked_only = [s for s in zero_rec_students if s.get("graduation_status") == "blocked"]

    zero_recommendation_summary = {
        "total": len(zero_rec_students),
        "graduating_this_term": len(graduating),
        "still_needs_courses": len(still_needs),
        "all_remaining_blocked": len(blocked_only),
        "graduating_students": [
            {
                "student_id": s["student_id"],
                "program": s["program"],
                "gpa": s["gpa"],
                "earned_credits": s["earned_credits"],
                "studying": s["studying_courses"],
            }
            for s in graduating
        ],
        "still_needs_students": [
            {
                "student_id": s["student_id"],
                "program": s["program"],
                "gpa": s["gpa"],
                "earned_credits": s["earned_credits"],
                "remaining": s["remaining_courses"],
                "recoverable": s["recoverable_courses"],
                "studying": s["studying_courses"],
            }
            for s in still_needs
        ],
    }

    return {
        "total_students": total,
        "shortfall_count": shortfall_count,
        "ok_count": total - shortfall_count,
        "min_credits": min_credits,
        "shortfall_students": shortfall_students,
        "top_recoverable": top_recoverable,
        "summary_by_program": program_counts,
        "zero_recommendation_summary": zero_recommendation_summary,
    }

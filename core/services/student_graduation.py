"""How far is this student from graduating, without inventing a date.

Two numbers that look interchangeable are not: the registrar's
``Student.total_earned_credits`` counts everything the student has earned
(transfers, retakes, courses outside the plan), while the degree plan is a fixed
list of courses. Measured on live data they disagree for a majority of students,
by up to 21 credits — one student has 162 earned credits against a 148-credit
plan. So progress is reported against the PLAN (courses remaining, which is
actionable) and the registrar's credit total is shown beside it as its own fact,
never divided into the other.

The term estimate is likewise split into what is certain and what is assumed:

    floor  = the prerequisite critical path — the fewest terms that can possibly
             work however many courses she takes, because these courses must be
             passed one after another.
    pace   = ceil(remaining / courses_per_term) — an assumption, stated as one.
"""

from __future__ import annotations

import math

from core.services.student_unlock import build_unlock_report

DEFAULT_COURSES_PER_TERM = 5


def build_graduation_report(
    student_id: int, year: int, term: int, *, courses_per_term: int = DEFAULT_COURSES_PER_TERM
) -> dict:
    report = build_unlock_report(student_id, year, term)
    if not report:
        return {}

    from core.models import Student

    done = report["done"]
    in_progress = report["in_progress"]
    open_courses = report["open_courses"]
    locked = report["locked_courses"]
    electives = report["elective_slots"]

    # Remaining = everything in the plan not yet passed. Courses being studied are
    # NOT counted as done: she still has to pass them.
    remaining = open_courses + locked + electives + in_progress
    remaining_credits = sum(int(c.get("credits") or 0) for c in remaining)
    plan_total = len(done) + len(remaining)
    passed_credits = sum(int(c.get("credits") or 0) for c in done)

    row = (
        Student.objects.filter(student_id=student_id)
        .values_list("total_earned_credits", "current_registered_credits", "gpa")
        .first()
    )
    earned_registrar, registered_now, gpa = row or (0, 0, None)

    # The fewest terms prerequisites permit: the longest chain still ahead of her.
    # steps counts course passes including the course itself, so it IS a term count
    # under one-course-per-term-per-chain. hours-only locks carry steps=None.
    chain_floor = max([c["steps"] for c in locked if c["steps"]] or [0])
    if not chain_floor and remaining:
        chain_floor = 1

    pace_terms = math.ceil(len(remaining) / courses_per_term) if remaining else 0
    terms_estimate = max(chain_floor, pace_terms)

    # Capstones gated on credit hours are the classic late surprise, so surface them.
    hour_gates = []
    for c in locked:
        for r in c["reasons"]:
            if r["kind"] == "MISSING_HOURS":
                hour_gates.append(
                    {
                        "code": c["code"],
                        "name": c["name"],
                        "required": r["required"],
                        "effective": r["effective"],
                        "remaining": r["remaining"],
                    }
                )

    return {
        "program": report["program"],
        # progress against the PLAN (internally consistent, and actionable)
        "plan_courses_total": plan_total,
        "plan_courses_passed": len(done),
        "percent_courses": round(100 * len(done) / plan_total) if plan_total else 0,
        "remaining_courses": len(remaining),
        "remaining_credits": remaining_credits,
        "passed_credits_in_plan": passed_credits,
        # the registrar's own figure, reported as its own fact
        "earned_credits_registrar": int(earned_registrar or 0),
        "registered_credits_now": int(registered_now or 0),
        "gpa": gpa,
        # how long, split into certain vs assumed
        "chain_floor_terms": chain_floor,
        "pace_terms": pace_terms,
        "terms_estimate": terms_estimate,
        "courses_per_term": courses_per_term,
        "final_term_possible": bool(remaining) and terms_estimate <= 1,
        "hour_gates": hour_gates,
        "counts": report["counts"],
        "in_progress": in_progress,
    }

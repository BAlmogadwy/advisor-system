"""Read-only graduation progress and term-by-term forecasting.

The forecast repeatedly runs the existing course recommender against an in-memory
academic state. Courses in the current Planner timetable are assumed passed first;
each later recommendation is only added to the simulated passed set after that
simulated term. No ``StudentCourse`` or timetable record is ever changed.

This is a planning scenario, not an official graduation date. It assumes every
current and simulated course is passed on the first attempt, uses an 18-credit cap
for every simulated main term, and cannot guarantee future offerings or seats.
"""

from __future__ import annotations

import math

from core.services.credit_policy import RECOMMENDED_MAX_CREDITS
from core.services.eligibility import split_hour_prereqs
from core.services.recommender import (
    calculate_real_student_term,
    recommend_next_courses_for_state,
)
from core.services.student_helpers import (
    get_prerequisites,
    get_student_passed_and_studying,
    is_elective_slot,
    normalize_code,
)
from core.services.student_sections import (
    append_unmapped_studying_courses,
    get_student_term_baseline,
)
from core.services.student_unlock import build_unlock_report

DEFAULT_MAX_CREDITS_PER_TERM = RECOMMENDED_MAX_CREDITS
MAX_SIMULATED_TERMS = 24
MAX_CURRENT_TERM_CHANGES = 10
MAX_REPLACEMENT_EVALUATIONS = 120
MAX_REPLACEMENT_RESULTS = 5
PROVEN_GRADUATION_IMPROVEMENT_EFFECTS = frozenset({"EARLIER", "FORECAST_COMPLETED"})


def _next_main_term(year: int, term: int) -> tuple[int, int]:
    if int(term) == 1:
        return int(year), 2
    return int(year) + 1, 1


def _current_course_state(
    student_id: int,
    year: int,
    term: int,
    credit_by_code: dict[str, int],
) -> tuple[list[dict], int]:
    """Return the same current-course baseline the Planner builds, deduplicated."""
    rows = append_unmapped_studying_courses(
        student_id,
        get_student_term_baseline(student_id, str(year), str(term)),
    )
    by_code: dict[str, dict] = {}
    for row in rows:
        code = normalize_code(row.get("course_key") or row.get("course_code") or "")
        if not code or code in by_code:
            continue
        credits = int(row.get("credits") or credit_by_code.get(code, 0) or 0)
        by_code[code] = {
            "code": code,
            "name": str(row.get("course_name") or ""),
            "credits": credits,
            "section": str(row.get("section") or ""),
        }
    courses = sorted(by_code.values(), key=lambda item: item["code"])
    return courses, sum(int(item["credits"]) for item in courses)


def _simulate_future_terms(
    *,
    student_id: int,
    year: int,
    term: int,
    program: str,
    plan_rows: dict[str, dict],
    actual_passed: set[str],
    current_courses: list[dict],
    earned_credits: int,
    current_credits: int,
    max_credits_per_term: int,
) -> dict:
    current_codes = {item["code"] for item in current_courses}
    simulated_passed = set(actual_passed) | current_codes
    effective_credits = int(earned_credits) + int(current_credits)
    cursor_year, cursor_term = int(year), int(term)
    term_plan: list[dict] = []
    no_progress_terms = 0
    latest_programme_term = max(
        (int(row.get("term") or 0) for row in plan_rows.values()), default=0
    )

    for _ in range(MAX_SIMULATED_TERMS):
        outstanding = set(plan_rows) - simulated_passed
        if not outstanding:
            break

        plan_year, plan_term = _next_main_term(cursor_year, cursor_term)
        next_student_term = calculate_real_student_term(student_id, cursor_year, cursor_term) + 1
        recommended = recommend_next_courses_for_state(
            student_id,
            cursor_year,
            cursor_term,
            passed=simulated_passed,
            studying=set(),
            effective_credits=effective_credits,
            max_credits=max_credits_per_term,
        )
        selected = [code for code in recommended if code in outstanding]
        courses = []
        term_credits = 0
        for code in selected:
            row = plan_rows[code]
            credits = int(row.get("credits") or 0)
            courses.append(
                {
                    "code": code,
                    "name": row.get("name") or "",
                    "credits": credits,
                    "requirement_type": row.get("type") or "",
                    "elective_slot": bool(row.get("elective_slot")),
                }
            )
            term_credits += credits

        term_plan.append(
            {
                "sequence": len(term_plan) + 1,
                "academic_year": plan_year,
                "term": plan_term,
                "courses": courses,
                "course_codes": selected,
                "credits": term_credits,
                "waiting_term": not selected,
            }
        )

        # A recommendation only becomes passed after its simulated term finishes.
        simulated_passed.update(selected)
        effective_credits += term_credits
        cursor_year, cursor_term = plan_year, plan_term
        no_progress_terms = 0 if selected else no_progress_terms + 1
        # Once the student's simulated progression has reached the end of the
        # programme map, two empty terms cover both odd/even offering parities.
        # Without a new pass or new credits, repeating them cannot change any
        # prerequisite decision, so stop and surface the unresolved records.
        if no_progress_terms >= 2 and next_student_term >= latest_programme_term:
            break

    unresolved = sorted(set(plan_rows) - simulated_passed)
    unresolved_rows = []
    for code in unresolved:
        course_prereqs, required_hours = split_hour_prereqs(get_prerequisites(code, program))
        missing_prereqs = sorted(
            prereq for prereq in course_prereqs if prereq not in simulated_passed
        )
        row = plan_rows[code]
        unresolved_rows.append(
            {
                "code": code,
                "name": row.get("name") or "",
                "credits": int(row.get("credits") or 0),
                "requirement_type": row.get("type") or "",
                "elective_slot": bool(row.get("elective_slot")),
                "missing_course_prerequisites": missing_prereqs,
                "missing_prerequisites_outside_plan": [
                    prereq for prereq in missing_prereqs if prereq not in plan_rows
                ],
                "credit_hour_gate": (
                    {
                        "required": required_hours,
                        "effective_in_scenario": effective_credits,
                        "remaining": max(0, required_hours - effective_credits),
                    }
                    if required_hours > effective_credits
                    else None
                ),
            }
        )
    return {
        "term_plan": term_plan,
        "estimated_additional_terms": len(term_plan) if not unresolved else None,
        "simulated_terms_examined": len(term_plan),
        "productive_terms_planned": sum(
            1 for planned_term in term_plan if planned_term["course_codes"]
        ),
        "simulation_completed": not unresolved,
        "unresolved_requirements": unresolved_rows,
    }


def build_graduation_report(
    student_id: int,
    year: int,
    term: int,
    *,
    max_credits_per_term: int = DEFAULT_MAX_CREDITS_PER_TERM,
    _current_courses_override: list[dict] | None = None,
    _excluded_studying_codes: set[str] | None = None,
) -> dict:
    """Build current progress plus a non-persistent future-term scenario."""
    from core.models import Course, ProgrammeRequirement, Student

    student_row = (
        Student.objects.filter(student_id=student_id)
        .values_list(
            "program",
            "total_earned_credits",
            "current_registered_credits",
            "gpa",
        )
        .first()
    )
    if not student_row or not student_row[0]:
        return {}
    program, earned_registrar, registered_registrar, gpa = student_row

    requirements = list(
        ProgrammeRequirement.objects.filter(program=program)
        .order_by("programme_term", "course_code")
        .values("course_code", "course_name", "programme_term", "credit_hours", "type")
    )
    if not requirements:
        return {}

    codes = {normalize_code(row["course_code"]) for row in requirements}
    names = {
        normalize_code(code): (description or "")
        for code, description in Course.objects.filter(course_code__in=codes).values_list(
            "course_code", "description"
        )
    }
    plan_rows: dict[str, dict] = {}
    for requirement in requirements:
        code = normalize_code(requirement["course_code"])
        plan_rows[code] = {
            "code": code,
            "name": names.get(code, "") or str(requirement.get("course_name") or ""),
            "credits": int(requirement.get("credit_hours") or 0),
            "term": int(requirement.get("programme_term") or 0),
            "type": str(requirement.get("type") or ""),
            "elective_slot": is_elective_slot(requirement.get("type")),
        }

    credit_by_code = {code: row["credits"] for code, row in plan_rows.items()}
    baseline_courses, baseline_credits = _current_course_state(
        student_id, year, term, credit_by_code
    )
    current_courses = (
        [dict(course) for course in _current_courses_override]
        if _current_courses_override is not None
        else baseline_courses
    )
    current_codes = {course["code"] for course in current_courses}
    # Prefer the Planner's concrete course baseline whenever it exists. Fall back
    # to the registrar aggregate only when no course-level current record exists.
    if _current_courses_override is not None:
        registered_now = sum(int(course.get("credits") or 0) for course in current_courses)
    else:
        registered_now = baseline_credits if current_courses else int(registered_registrar or 0)

    report = build_unlock_report(
        student_id,
        year,
        term,
        additional_studying_codes=current_codes,
        excluded_studying_codes=_excluded_studying_codes,
        registered_credits_override=registered_now,
    )
    if not report:
        return {}

    done = report["done"]
    in_progress = report["in_progress"]
    open_courses = report["open_courses"]
    locked = report["locked_courses"]
    electives = report["elective_slots"]
    remaining = open_courses + locked + electives + in_progress
    remaining_credits = sum(int(course.get("credits") or 0) for course in remaining)
    plan_total = len(done) + len(remaining)
    passed_credits = sum(int(course.get("credits") or 0) for course in done)

    chain_floor = max([course["steps"] for course in locked if course["steps"]] or [0])
    if not chain_floor and remaining:
        chain_floor = 1

    actual_passed, _actual_studying = get_student_passed_and_studying(student_id)
    simulation = _simulate_future_terms(
        student_id=student_id,
        year=year,
        term=term,
        program=str(program),
        plan_rows=plan_rows,
        actual_passed={normalize_code(code) for code in actual_passed},
        current_courses=current_courses,
        earned_credits=int(earned_registrar or 0),
        current_credits=registered_now,
        max_credits_per_term=max(1, int(max_credits_per_term)),
    )

    remaining_after_current = [
        course for course in remaining if course["code"] not in current_codes
    ]
    credits_after_current = sum(
        int(course.get("credits") or 0) for course in remaining_after_current
    )
    capacity_floor = (
        math.ceil(credits_after_current / max(1, int(max_credits_per_term)))
        if credits_after_current
        else 0
    )
    additional_terms = simulation["estimated_additional_terms"]
    including_current = (
        additional_terms + (1 if current_courses else 0) if additional_terms is not None else None
    )
    lower_bound = max(chain_floor, capacity_floor)
    lower_bound_including_current = lower_bound + (1 if current_courses else 0)

    hour_gates = []
    for course in locked:
        for reason in course["reasons"]:
            if reason["kind"] == "MISSING_HOURS":
                hour_gates.append(
                    {
                        "code": course["code"],
                        "name": course["name"],
                        "required": reason["required"],
                        "effective": reason["effective"],
                        "remaining": reason["remaining"],
                    }
                )

    return {
        "program": report["program"],
        "plan_courses_total": plan_total,
        "plan_courses_passed": len(done),
        "percent_courses": round(100 * len(done) / plan_total) if plan_total else 0,
        "remaining_courses": len(remaining),
        "remaining_credits": remaining_credits,
        "passed_credits_in_plan": passed_credits,
        "earned_credits_registrar": int(earned_registrar or 0),
        "registered_credits_now": registered_now,
        "gpa": gpa,
        "chain_floor_terms": chain_floor,
        "capacity_floor_terms_after_current": capacity_floor,
        "lower_bound_additional_terms": lower_bound,
        "lower_bound_terms_including_current": lower_bound_including_current,
        # Backward-compatible aliases now use the 18-credit scenario, not a
        # guessed number of courses per term.
        "pace_terms": capacity_floor,
        "terms_estimate": including_current,
        "courses_per_term": None,
        "max_credits_per_term": max(1, int(max_credits_per_term)),
        "estimated_additional_terms": additional_terms,
        "estimated_terms_including_current": including_current,
        "simulation_completed": simulation["simulation_completed"],
        "simulated_terms_examined": simulation["simulated_terms_examined"],
        "productive_terms_planned": simulation["productive_terms_planned"],
        "term_plan": simulation["term_plan"],
        "unresolved_requirements": simulation["unresolved_requirements"],
        # Local presentation input for the student-facing scenario map. The
        # remote LLM privacy projector deliberately does not transmit this
        # graph; the model already has the compact term plan it needs.
        "scenario_graph": report.get("graph") or {},
        "current_courses_assumed_passed": current_courses,
        "simulation_assumptions": [
            "All current and simulated courses are passed on the first attempt.",
            f"Every simulated main term uses a maximum of {max(1, int(max_credits_per_term))} credits.",
            "Elective placeholders remain plan requirements; no concrete elective is invented.",
            "Future course offerings, section times, seats, and registration permission are not guaranteed.",
            "The scenario is read-only and does not update the student record or university portal.",
        ],
        "final_term_possible": bool(current_courses)
        and simulation["simulation_completed"]
        and additional_terms == 0,
        "hour_gates": hour_gates,
        "counts": report["counts"],
        "in_progress": in_progress,
    }


def _normalise_code_list(codes: list[str] | tuple[str, ...] | None) -> list[str]:
    out: list[str] = []
    for value in codes or []:
        code = normalize_code(value)
        if code and code not in out:
            out.append(code)
    return out


def _scenario_summary(report: dict) -> dict:
    return {
        "simulation_completed": bool(report.get("simulation_completed")),
        "estimated_additional_terms": report.get("estimated_additional_terms"),
        "estimated_terms_including_current": report.get("estimated_terms_including_current"),
        "lower_bound_additional_terms": report.get("lower_bound_additional_terms"),
        "lower_bound_terms_including_current": report.get("lower_bound_terms_including_current"),
        "registered_credits_now": int(report.get("registered_credits_now") or 0),
        "current_courses_assumed_passed": report.get("current_courses_assumed_passed") or [],
        "unresolved_requirements": report.get("unresolved_requirements") or [],
    }


def _blocker_progress(baseline_row: dict, scenario_row: dict) -> dict | None:
    baseline_missing = set(baseline_row.get("missing_course_prerequisites") or [])
    scenario_missing = set(scenario_row.get("missing_course_prerequisites") or [])
    prerequisites_resolved = sorted(baseline_missing - scenario_missing)

    baseline_gate = baseline_row.get("credit_hour_gate")
    scenario_gate = scenario_row.get("credit_hour_gate")
    baseline_remaining = (
        int(baseline_gate.get("remaining") or 0) if isinstance(baseline_gate, dict) else 0
    )
    scenario_remaining = (
        int(scenario_gate.get("remaining") or 0) if isinstance(scenario_gate, dict) else 0
    )
    credit_gap_reduced_by = max(0, baseline_remaining - scenario_remaining)
    if not prerequisites_resolved and not credit_gap_reduced_by:
        return None
    return {
        "code": str(scenario_row.get("code") or baseline_row.get("code") or ""),
        "prerequisites_resolved": prerequisites_resolved,
        "credit_gap_reduced_by": credit_gap_reduced_by,
        "credit_gap_remaining": scenario_remaining,
    }


def _compare_reports(baseline: dict, scenario: dict, removed_codes: list[str]) -> dict:
    baseline_unresolved = {
        row["code"]: row
        for row in baseline.get("unresolved_requirements") or []
        if isinstance(row, dict) and row.get("code")
    }
    scenario_unresolved = {
        row["code"]: row
        for row in scenario.get("unresolved_requirements") or []
        if isinstance(row, dict) and row.get("code")
    }
    blockers_resolved = [
        baseline_unresolved[code]
        for code in sorted(set(baseline_unresolved) - set(scenario_unresolved))
    ]
    blockers_introduced = [
        scenario_unresolved[code]
        for code in sorted(set(scenario_unresolved) - set(baseline_unresolved))
    ]
    blockers_improved = [
        progress
        for code in sorted(set(baseline_unresolved) & set(scenario_unresolved))
        if (progress := _blocker_progress(baseline_unresolved[code], scenario_unresolved[code]))
    ]

    baseline_terms = baseline.get("estimated_additional_terms")
    scenario_terms = scenario.get("estimated_additional_terms")
    term_difference = (
        int(scenario_terms) - int(baseline_terms)
        if baseline_terms is not None and scenario_terms is not None
        else None
    )

    if baseline.get("simulation_completed") and scenario.get("simulation_completed"):
        if term_difference is not None and term_difference < 0:
            effect = "EARLIER"
        elif term_difference is not None and term_difference > 0:
            effect = "LATER"
        else:
            effect = "SAME"
    elif not baseline.get("simulation_completed") and scenario.get("simulation_completed"):
        effect = "FORECAST_COMPLETED"
    elif baseline.get("simulation_completed") and not scenario.get("simulation_completed"):
        effect = "FORECAST_BECAME_UNRESOLVED"
    elif (blockers_resolved or blockers_improved) and not blockers_introduced:
        effect = "UNRESOLVED_IMPROVEMENT"
    elif blockers_introduced and not blockers_resolved and not blockers_improved:
        effect = "UNRESOLVED_WORSE"
    else:
        effect = "NOT_DETERMINABLE"

    scenario_positions = {
        code: planned_term
        for planned_term in scenario.get("term_plan") or []
        for code in planned_term.get("course_codes") or []
    }
    deferred_courses = []
    for code in removed_codes:
        planned_term = scenario_positions.get(code)
        deferred_courses.append(
            {
                "code": code,
                "future_sequence": (
                    int(planned_term.get("sequence") or 0) if planned_term else None
                ),
                "academic_year": planned_term.get("academic_year") if planned_term else None,
                "term": planned_term.get("term") if planned_term else None,
                "unresolved": code in scenario_unresolved,
            }
        )

    # A partially improved prerequisite or credit-hour blocker is useful evidence
    # for an explicit what-if explanation, but it is not enough to call an
    # automatic one-for-one replacement "better".  Replacement search must only
    # surface a swap when the complete forecast is measurably earlier or changes
    # from unresolved to completed.  This also prevents an outside-plan course
    # from displacing a required current course merely because it unlocks one
    # downstream requirement.
    blocker_progress_only = effect == "UNRESOLVED_IMPROVEMENT"
    proven_improvement = effect in PROVEN_GRADUATION_IMPROVEMENT_EFFECTS
    return {
        "timing_effect": effect,
        "term_difference": term_difference,
        "terms_saved": max(0, -term_difference) if term_difference is not None else None,
        "exact_timing_comparison_available": term_difference is not None,
        "proven_improvement": proven_improvement,
        "complete_forecast_improved": proven_improvement,
        "blocker_progress_only": blocker_progress_only,
        "improvement_basis": (
            "COMPLETE_FORECAST"
            if proven_improvement
            else "BLOCKER_PROGRESS_ONLY"
            if blocker_progress_only
            else "NONE"
        ),
        "baseline_lower_bound_additional_terms": baseline.get("lower_bound_additional_terms"),
        "scenario_lower_bound_additional_terms": scenario.get("lower_bound_additional_terms"),
        "lower_bound_change": int(scenario.get("lower_bound_additional_terms") or 0)
        - int(baseline.get("lower_bound_additional_terms") or 0),
        "baseline_current_credits": int(baseline.get("registered_credits_now") or 0),
        "scenario_current_credits": int(scenario.get("registered_credits_now") or 0),
        "current_credit_change": int(scenario.get("registered_credits_now") or 0)
        - int(baseline.get("registered_credits_now") or 0),
        "blockers_resolved": blockers_resolved,
        "blockers_improved": blockers_improved,
        "blockers_introduced": blockers_introduced,
        "deferred_courses": deferred_courses,
    }


def _prepare_current_term_changes(
    *,
    student_id: int,
    program: str,
    baseline: dict,
    remove_codes: list[str],
    add_codes: list[str],
    max_credits_per_term: int,
) -> dict:
    from core.models import Course, ProgrammeRequirement, Student

    errors: list[dict] = []
    if len(remove_codes) > MAX_CURRENT_TERM_CHANGES or len(add_codes) > MAX_CURRENT_TERM_CHANGES:
        errors.append(
            {
                "kind": "TOO_MANY_CHANGES",
                "maximum_per_list": MAX_CURRENT_TERM_CHANGES,
            }
        )

    overlap = sorted(set(remove_codes) & set(add_codes))
    if overlap:
        errors.append({"kind": "SAME_COURSE_REMOVED_AND_ADDED", "course_codes": overlap})

    baseline_courses = baseline.get("current_courses_assumed_passed") or []
    baseline_by_code = {course["code"]: dict(course) for course in baseline_courses}
    for code in remove_codes:
        if code not in baseline_by_code:
            errors.append({"kind": "NOT_IN_CURRENT_TIMETABLE", "course_code": code})
    for code in add_codes:
        if code in baseline_by_code and code not in remove_codes:
            errors.append({"kind": "ALREADY_IN_CURRENT_TIMETABLE", "course_code": code})

    actual_passed, _actual_studying = get_student_passed_and_studying(student_id)
    actual_passed = {normalize_code(code) for code in actual_passed}
    for code in add_codes:
        if code in actual_passed:
            errors.append({"kind": "ALREADY_PASSED", "course_code": code})

    plan_rows = {
        normalize_code(row["course_code"]): row
        for row in ProgrammeRequirement.objects.filter(program=program).values(
            "course_code", "course_name", "credit_hours", "type"
        )
    }
    course_rows = {
        normalize_code(course.course_code): course
        for course in Course.objects.filter(course_code__in=add_codes)
    }

    additions = []
    for code in add_codes:
        plan_row = plan_rows.get(code)
        course = course_rows.get(code)
        if plan_row and is_elective_slot(plan_row.get("type")):
            errors.append({"kind": "ELECTIVE_PLACEHOLDER_NOT_A_COURSE", "course_code": code})
            continue
        if course is None and plan_row is None:
            errors.append({"kind": "COURSE_NOT_ON_FILE", "course_code": code})
            continue
        credits = int(
            (plan_row or {}).get("credit_hours") or getattr(course, "credit_hours", 0) or 0
        )
        if credits <= 0:
            errors.append({"kind": "COURSE_CREDITS_UNKNOWN", "course_code": code})
            continue
        additions.append(
            {
                "code": code,
                "name": str(
                    getattr(course, "description", "") or (plan_row or {}).get("course_name") or ""
                ),
                "credits": credits,
                "section": "",
                "source": "graduation_what_if",
                "in_degree_plan": code in plan_rows,
            }
        )

    retained = [dict(course) for course in baseline_courses if course["code"] not in remove_codes]
    modified_courses = sorted(retained + additions, key=lambda course: course["code"])
    modified_codes = {course["code"] for course in modified_courses}
    modified_credits = sum(int(course.get("credits") or 0) for course in modified_courses)
    if modified_credits > max_credits_per_term:
        errors.append(
            {
                "kind": "SCENARIO_EXCEEDS_CREDIT_CAP",
                "credits": modified_credits,
                "maximum": max_credits_per_term,
            }
        )

    earned = int(
        Student.objects.filter(student_id=student_id)
        .values_list("total_earned_credits", flat=True)
        .first()
        or 0
    )
    satisfied = actual_passed | modified_codes
    for addition in additions:
        code = addition["code"]
        course_prereqs, required_hours = split_hour_prereqs(get_prerequisites(code, program))
        missing = sorted(prereq for prereq in course_prereqs if prereq not in satisfied - {code})
        if missing:
            errors.append(
                {
                    "kind": "ADDED_COURSE_PREREQUISITES_UNMET",
                    "course_code": code,
                    "missing_prerequisites": missing,
                }
            )
        effective_credits = earned + modified_credits
        if required_hours and effective_credits < required_hours:
            errors.append(
                {
                    "kind": "ADDED_COURSE_CREDIT_GATE_UNMET",
                    "course_code": code,
                    "required": required_hours,
                    "effective": effective_credits,
                    "remaining": required_hours - effective_credits,
                }
            )

    return {
        "valid": not errors,
        "errors": errors,
        "current_courses": modified_courses,
        "removed_courses": [
            baseline_by_code[code] for code in remove_codes if code in baseline_by_code
        ],
        "added_courses": additions,
        "outside_plan_additions": [course for course in additions if not course["in_degree_plan"]],
    }


def _evaluate_current_term_changes(
    *,
    student_id: int,
    year: int,
    term: int,
    baseline: dict,
    remove_codes: list[str],
    add_codes: list[str],
    max_credits_per_term: int,
) -> dict:
    prepared = _prepare_current_term_changes(
        student_id=student_id,
        program=str(baseline.get("program") or ""),
        baseline=baseline,
        remove_codes=remove_codes,
        add_codes=add_codes,
        max_credits_per_term=max_credits_per_term,
    )
    if not prepared["valid"]:
        return {**prepared, "scenario_report": None, "comparison": None}

    scenario = build_graduation_report(
        student_id,
        year,
        term,
        max_credits_per_term=max_credits_per_term,
        _current_courses_override=prepared["current_courses"],
        _excluded_studying_codes=set(remove_codes),
    )
    return {
        **prepared,
        "scenario_report": scenario,
        "comparison": _compare_reports(baseline, scenario, remove_codes),
    }


def _replacement_rank(row: dict) -> tuple:
    effect_rank = {
        "EARLIER": 0,
        "FORECAST_COMPLETED": 1,
    }
    comparison = row["comparison"]
    return (
        effect_rank.get(comparison["timing_effect"], 9),
        comparison["term_difference"] if comparison["term_difference"] is not None else 99,
        -len(comparison["blockers_resolved"]),
        -len(comparison["blockers_improved"]),
        row["remove_course"]["code"],
        row["add_course"]["code"],
    )


def build_graduation_what_if(
    student_id: int,
    year: int,
    term: int,
    *,
    remove_current_courses: list[str] | None = None,
    add_current_courses: list[str] | None = None,
    search_better_replacements: bool = False,
    max_credits_per_term: int = DEFAULT_MAX_CREDITS_PER_TERM,
) -> dict:
    """Compare current-term changes without mutating the student's real records."""
    from core.models import Course

    cap = max(1, int(max_credits_per_term))
    remove_codes = _normalise_code_list(remove_current_courses)
    add_codes = _normalise_code_list(add_current_courses)
    baseline = build_graduation_report(student_id, year, term, max_credits_per_term=cap)
    if not baseline:
        return {}

    if search_better_replacements and (remove_codes or add_codes):
        return {
            **baseline,
            "what_if": {
                "mode": "replacement_search",
                "valid": False,
                "validation_errors": [{"kind": "SEARCH_CANNOT_BE_COMBINED_WITH_EXPLICIT_CHANGES"}],
                "baseline": _scenario_summary(baseline),
            },
        }

    if not search_better_replacements:
        evaluated = _evaluate_current_term_changes(
            student_id=student_id,
            year=year,
            term=term,
            baseline=baseline,
            remove_codes=remove_codes,
            add_codes=add_codes,
            max_credits_per_term=cap,
        )
        scenario = evaluated.get("scenario_report") or baseline
        return {
            **scenario,
            "what_if": {
                "mode": "explicit_changes",
                "valid": bool(evaluated["valid"]),
                "validation_errors": evaluated["errors"],
                "removed_current_courses": evaluated["removed_courses"],
                "added_current_courses": evaluated["added_courses"],
                "outside_plan_additions": evaluated["outside_plan_additions"],
                "baseline": _scenario_summary(baseline),
                "scenario": _scenario_summary(scenario) if evaluated["valid"] else None,
                "comparison": evaluated["comparison"],
                "timetable_check_required": bool(evaluated["valid"] and add_codes),
                "note": (
                    "Academic what-if only. It does not prove that an added course is "
                    "offered, has a seat, fits the timetable, or may be registered."
                ),
            },
        }

    current_codes = {
        course["code"] for course in baseline.get("current_courses_assumed_passed") or []
    }
    unlock = build_unlock_report(
        student_id,
        year,
        term,
        additional_studying_codes=current_codes,
        registered_credits_override=int(baseline.get("registered_credits_now") or 0),
    )
    candidates = {course["code"] for course in (unlock.get("open_courses") or [])}
    candidates.update(
        prereq
        for blocker in baseline.get("unresolved_requirements") or []
        for prereq in blocker.get("missing_prerequisites_outside_plan") or []
    )
    known_candidates = set(
        Course.objects.filter(course_code__in=candidates).values_list("course_code", flat=True)
    )
    candidate_codes = sorted(
        normalize_code(code)
        for code in known_candidates
        if normalize_code(code) not in current_codes
    )

    improving = []
    unproven_blocker_progress_pairs = 0
    evaluated_count = 0
    truncated = False
    for removed in baseline.get("current_courses_assumed_passed") or []:
        for candidate in candidate_codes:
            if evaluated_count >= MAX_REPLACEMENT_EVALUATIONS:
                truncated = True
                break
            evaluated_count += 1
            evaluated = _evaluate_current_term_changes(
                student_id=student_id,
                year=year,
                term=term,
                baseline=baseline,
                remove_codes=[removed["code"]],
                add_codes=[candidate],
                max_credits_per_term=cap,
            )
            comparison = evaluated.get("comparison")
            if not evaluated["valid"] or not comparison:
                continue
            if comparison.get("blocker_progress_only"):
                unproven_blocker_progress_pairs += 1
            if not comparison["proven_improvement"]:
                continue
            improving.append(
                {
                    "remove_course": evaluated["removed_courses"][0],
                    "add_course": evaluated["added_courses"][0],
                    "outside_plan_addition": bool(evaluated["outside_plan_additions"]),
                    "comparison": comparison,
                    "scenario": _scenario_summary(evaluated["scenario_report"]),
                }
            )
        if truncated:
            break
    improving.sort(key=_replacement_rank)

    return {
        **baseline,
        "what_if": {
            "mode": "replacement_search",
            "valid": True,
            "validation_errors": [],
            "baseline": _scenario_summary(baseline),
            "candidate_courses_considered": candidate_codes,
            "pairs_evaluated": evaluated_count,
            "search_truncated": truncated,
            "unproven_blocker_progress_pairs": unproven_blocker_progress_pairs,
            "improving_replacements": improving[:MAX_REPLACEMENT_RESULTS],
            "no_proven_improvement": not improving,
            "timetable_check_required": bool(improving),
            "note": (
                "Academic comparison only. Candidates are prerequisite-ready in recorded "
                "data, but offerings, seats, timetable clashes, and registration permission "
                "are not established."
            ),
        },
    }

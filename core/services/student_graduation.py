"""Read-only graduation progress and term-by-term forecasting.

The forecast repeatedly runs the existing course recommender against an in-memory
academic state.  By default, the supplied calendar term starts with the courses the
recommender selects from completed work only.  A caller can explicitly choose the
registrar timetable instead (for example, "based on my current timetable" or a
drop-course what-if).  Courses in that selected planning baseline are assumed passed
first; each later recommendation is only added to the simulated passed set after
that simulated term. No ``StudentCourse`` or timetable record is ever changed.

This is a planning scenario, not an official graduation date. It assumes every
planning-baseline and simulated course is passed on the first attempt, uses an
18-credit cap for every simulated main term, and cannot guarantee future offerings
or seats.
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
    get_program_prerequisites,
    get_student_passed_and_studying,
    is_elective_slot,
    normalize_code,
)
from core.services.student_sections import (
    append_unmapped_studying_courses,
    get_student_term_baseline,
)
from core.services.student_unlock import build_unlock_report
from core.services.timetable_snapshots import Snapshot

DEFAULT_MAX_CREDITS_PER_TERM = RECOMMENDED_MAX_CREDITS
RECOMMENDED_CURRENT_TERM = "recommended_current_term"
REGISTERED_TIMETABLE = "registered_timetable"
PLANNING_BASELINE_KINDS = frozenset({RECOMMENDED_CURRENT_TERM, REGISTERED_TIMETABLE})
MAX_SIMULATED_TERMS = 24
MAX_CURRENT_TERM_CHANGES = 10
MAX_REPLACEMENT_EVALUATIONS = 120
MAX_REPLACEMENT_RESULTS = 5
PROVEN_GRADUATION_IMPROVEMENT_EFFECTS = frozenset({"EARLIER", "FORECAST_COMPLETED"})


def _validate_academic_term(value: int) -> int:
    try:
        term = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"academic term must be 1 or 2, got {value!r}") from None
    if term not in {1, 2}:
        raise ValueError(f"academic term must be 1 or 2, got {value!r}")
    return term


def _validate_planning_baseline_kind(value: str) -> str:
    kind = str(value or "").strip().lower()
    if kind not in PLANNING_BASELINE_KINDS:
        raise ValueError(
            f"planning_baseline_kind must be one of {sorted(PLANNING_BASELINE_KINDS)!r}, "
            f"got {value!r}"
        )
    return kind


def _next_main_term(year: int, term: int) -> tuple[int, int]:
    if int(term) == 1:
        return int(year), 2
    return int(year) + 1, 1


def _current_course_state(
    student_id: int,
    year: int,
    term: int,
    credit_by_code: dict[str, int],
    baseline_rows: list[dict] | None = None,
    studying_rows: list[object] | None = None,
    query_cache: dict[object, object] | None = None,
) -> tuple[list[dict], int]:
    """Return a concrete course baseline from explicitly supplied registrar rows.

    The only implicit snapshot this low-level helper may read is ``REGISTERED``.
    Expected and working timetable rows must be passed deliberately by the one
    comparison that presents them as a hypothetical addition.
    """
    studying_key = ("studying_rows", int(student_id))
    cached_studying = query_cache.get(studying_key) if query_cache is not None else None
    if studying_rows is None and isinstance(cached_studying, list):
        studying_rows = cached_studying
    elif studying_rows is None:
        from core.models import StudentCourse

        studying_rows = list(
            StudentCourse.objects.filter(
                student_id=student_id,
                status__iexact="studying",
            )
            .select_related("course")
            .order_by("course__course_code")
        )
        if query_cache is not None:
            query_cache[studying_key] = studying_rows

    rows = append_unmapped_studying_courses(
        student_id,
        (
            get_student_term_baseline(
                student_id,
                str(year),
                str(term),
                snapshot=Snapshot.REGISTERED,
            )
            if baseline_rows is None
            else baseline_rows
        ),
        studying_rows=studying_rows,
        credit_map=credit_by_code,
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
    prerequisite_map: dict[str, list[str]],
    recommender_courses: list[dict],
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
        # The recommendation and the row it produces must refer to the same
        # calendar term. Passing the cursor here previously labelled a 1448/1
        # recommendation as 1448/2, which delayed term-sensitive courses and
        # made the first projected term appear artificially blocked.
        plan_student_term = calculate_real_student_term(student_id, plan_year, plan_term)
        recommended = recommend_next_courses_for_state(
            student_id,
            plan_year,
            plan_term,
            passed=simulated_passed,
            studying=set(),
            effective_credits=effective_credits,
            max_credits=max_credits_per_term,
            program=program,
            prerequisite_map=prerequisite_map,
            all_courses=recommender_courses,
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
        if no_progress_terms >= 2 and plan_student_term >= latest_programme_term:
            break

    unresolved = sorted(set(plan_rows) - simulated_passed)
    unresolved_rows = []
    for code in unresolved:
        course_prereqs, required_hours = split_hour_prereqs(prerequisite_map.get(code, []))
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
    planning_baseline_kind: str = RECOMMENDED_CURRENT_TERM,
    max_credits_per_term: int = DEFAULT_MAX_CREDITS_PER_TERM,
    _current_courses_override: list[dict] | None = None,
    _excluded_studying_codes: set[str] | None = None,
    _prerequisite_map: dict[str, list[str]] | None = None,
    _query_cache: dict[object, object] | None = None,
) -> dict:
    """Build progress plus a non-persistent scenario after a planning baseline."""
    term = _validate_academic_term(term)

    from core.models import Course, ProgrammeRequirement, Student

    baseline_kind = _validate_planning_baseline_kind(planning_baseline_kind)
    cap = max(1, int(max_credits_per_term))

    student_key = ("graduation_student", int(student_id))
    student_row = _query_cache.get(student_key) if _query_cache is not None else None
    if not isinstance(student_row, tuple):
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
        if _query_cache is not None and student_row is not None:
            _query_cache[student_key] = student_row
    if not student_row or not student_row[0]:
        return {}
    program, earned_registrar, registered_registrar, gpa = student_row

    requirements_key = ("graduation_requirements", str(program).strip().upper())
    cached_requirements = _query_cache.get(requirements_key) if _query_cache is not None else None
    if isinstance(cached_requirements, list):
        requirements = cached_requirements
    else:
        requirements = list(
            ProgrammeRequirement.objects.filter(program=program)
            .order_by("programme_term", "course_code")
            .values("course_code", "course_name", "programme_term", "credit_hours", "type")
        )
        if _query_cache is not None:
            _query_cache[requirements_key] = requirements
    if not requirements:
        return {}

    codes = {normalize_code(row["course_code"]) for row in requirements}
    names_key = ("course_names", str(program).strip().upper())
    cached_names = _query_cache.get(names_key) if _query_cache is not None else None
    if isinstance(cached_names, dict) and codes <= set(cached_names):
        names = cached_names
    else:
        names = {
            normalize_code(code): (description or "")
            for code, description in Course.objects.filter(course_code__in=codes).values_list(
                "course_code", "description"
            )
        }
        if _query_cache is not None:
            _query_cache[names_key] = names
    plan_rows: dict[str, dict] = {}
    for requirement in requirements:
        code = normalize_code(requirement["course_code"])
        plan_rows[code] = {
            "code": code,
            # A display code is not a globally unique course identity.  The same
            # code can name different courses in different programmes (AI492 is
            # Graduation Project II in AI but Cooperative Training in AI2), so
            # the selected programme plan is authoritative for this scenario.
            "name": str(requirement.get("course_name") or "").strip() or names.get(code, ""),
            "credits": int(requirement.get("credit_hours") or 0),
            "term": int(requirement.get("programme_term") or 0),
            "type": str(requirement.get("type") or ""),
            "elective_slot": is_elective_slot(requirement.get("type")),
        }

    prerequisite_key = ("program_prerequisites", str(program).strip().upper())
    cached_prerequisites = _query_cache.get(prerequisite_key) if _query_cache is not None else None
    if _prerequisite_map is not None:
        prerequisites_by_course = _prerequisite_map
    elif isinstance(cached_prerequisites, dict):
        prerequisites_by_course = cached_prerequisites
    else:
        prerequisites_by_course = get_program_prerequisites(str(program))
    if _query_cache is not None:
        _query_cache[prerequisite_key] = prerequisites_by_course
    recommender_courses = [
        {
            "code": code,
            "term": row["term"],
            "credits": row["credits"],
        }
        for code, row in plan_rows.items()
    ]

    credit_by_code = {code: row["credits"] for code, row in plan_rows.items()}
    passed_key = ("passed_and_studying", int(student_id))
    cached_status = _query_cache.get(passed_key) if _query_cache is not None else None
    if isinstance(cached_status, tuple):
        raw_passed, raw_studying = cached_status
    else:
        raw_passed, raw_studying = get_student_passed_and_studying(student_id)
        if _query_cache is not None:
            _query_cache[passed_key] = (raw_passed, raw_studying)
    actual_passed = {normalize_code(code) for code in raw_passed if normalize_code(code)}
    actual_studying = {normalize_code(code) for code in raw_studying if normalize_code(code)}

    # The baseline cache is explicitly provenance-aware.  In particular, a
    # recommended report built earlier in a request must never become the factual
    # baseline of a later registered-timetable what-if (or vice versa).
    baseline_key = (
        "graduation_baseline",
        baseline_kind,
        int(student_id),
        int(year),
        int(term),
        cap,
    )
    cached_baseline = _query_cache.get(baseline_key) if _query_cache is not None else None
    if _current_courses_override is not None:
        baseline_courses = [dict(course) for course in _current_courses_override]
        baseline_credits = sum(int(course.get("credits") or 0) for course in baseline_courses)
    elif isinstance(cached_baseline, list):
        baseline_courses = [dict(course) for course in cached_baseline]
        baseline_credits = sum(int(course.get("credits") or 0) for course in baseline_courses)
    elif baseline_kind == RECOMMENDED_CURRENT_TERM:
        recommended_codes = recommend_next_courses_for_state(
            student_id,
            int(year),
            int(term),
            passed=actual_passed,
            studying=set(),
            effective_credits=int(earned_registrar or 0),
            max_credits=cap,
            program=str(program),
            prerequisite_map=prerequisites_by_course,
            all_courses=recommender_courses,
        )
        baseline_courses = [
            {
                "code": code,
                "name": str(plan_rows[code].get("name") or ""),
                "credits": int(plan_rows[code].get("credits") or 0),
                "section": "",
            }
            for code in recommended_codes
            if code in plan_rows
        ]
        baseline_credits = sum(int(course.get("credits") or 0) for course in baseline_courses)
    else:
        registered_rows = get_student_term_baseline(
            student_id,
            str(year),
            str(term),
            snapshot=Snapshot.REGISTERED,
        )
        baseline_courses, baseline_credits = _current_course_state(
            student_id,
            year,
            term,
            credit_by_code,
            baseline_rows=registered_rows,
            query_cache=_query_cache,
        )
    if _query_cache is not None and _current_courses_override is None:
        _query_cache[baseline_key] = [dict(course) for course in baseline_courses]
    current_courses = (
        [dict(course) for course in _current_courses_override]
        if _current_courses_override is not None
        else baseline_courses
    )
    current_codes = {course["code"] for course in current_courses}
    # Prefer the concrete course baseline whenever it exists.  Only the explicit
    # registered mode may fall back to the registrar aggregate; the recommended
    # mode is intentionally independent of current registration.
    if _current_courses_override is not None:
        registered_now = sum(int(course.get("credits") or 0) for course in current_courses)
    elif baseline_kind == RECOMMENDED_CURRENT_TERM:
        registered_now = baseline_credits
    else:
        registered_now = baseline_credits if current_courses else int(registered_registrar or 0)

    # A retake still consumes timetable load, but its credits are already part
    # of the registrar's earned total. Count only newly earnable baseline
    # credits when evaluating cumulative-hour gates and future progression.
    # When only an aggregate registrar value exists, there are no course codes
    # with which to identify repeats, so retain that aggregate as the fallback.
    baseline_progress_credits = (
        sum(
            int(course.get("credits") or 0)
            for course in current_courses
            if normalize_code(course.get("code") or "") not in actual_passed
        )
        if current_courses
        else registered_now
    )

    excluded_studying_codes = {
        normalize_code(code) for code in (_excluded_studying_codes or set()) if normalize_code(code)
    }
    if baseline_kind == RECOMMENDED_CURRENT_TERM:
        # Persisted studying rows must not alter the default recommendation.  A
        # course independently selected by the recommender remains part of the
        # in-memory baseline, so exclude only the other persisted studying rows.
        excluded_studying_codes |= actual_studying - current_codes
    report = build_unlock_report(
        student_id,
        year,
        term,
        additional_studying_codes=current_codes,
        excluded_studying_codes=excluded_studying_codes,
        registered_credits_override=baseline_progress_credits,
        _prerequisite_map=prerequisites_by_course,
        _query_cache=_query_cache,
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

    remaining_after_baseline = [
        course for course in remaining if course["code"] not in current_codes
    ]
    chain_floor = max([course["steps"] for course in locked if course["steps"]] or [0])
    if not chain_floor and remaining_after_baseline:
        # The floor answers "how many terms BEYOND the baseline can this not go
        # below".  Flooring on `remaining` counted the baseline term's own
        # in-progress courses, so a student whose registered term completes the
        # plan carried a "verified minimum" of one additional term beside an
        # estimate of zero - a lower bound above the estimate, and one term too
        # long in exactly the audited P0's wording.  capacity_floor already
        # excluded the baseline; the two floors now agree about what counts.
        chain_floor = 1

    simulation = _simulate_future_terms(
        student_id=student_id,
        year=year,
        term=term,
        program=str(program),
        plan_rows=plan_rows,
        actual_passed=actual_passed,
        current_courses=current_courses,
        earned_credits=int(earned_registrar or 0),
        current_credits=baseline_progress_credits,
        max_credits_per_term=cap,
        prerequisite_map=prerequisites_by_course,
        recommender_courses=recommender_courses,
    )

    credits_after_baseline = sum(
        int(course.get("credits") or 0) for course in remaining_after_baseline
    )
    capacity_floor = (
        math.ceil(credits_after_baseline / max(1, int(max_credits_per_term)))
        if credits_after_baseline
        else 0
    )
    additional_terms = simulation["estimated_additional_terms"]
    including_baseline = (
        additional_terms + (1 if current_courses else 0) if additional_terms is not None else None
    )
    lower_bound = max(chain_floor, capacity_floor)
    lower_bound_including_baseline = lower_bound + (1 if current_courses else 0)

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
        "planning_baseline_academic_year": int(year),
        "planning_baseline_term": int(term),
        "planning_baseline_kind": baseline_kind,
        "planning_baseline_credits": registered_now,
        "registered_credits_at_planning_baseline": registered_now,
        "planning_baseline_courses_assumed_passed": current_courses,
        "planning_baseline": {
            "academic_year": int(year),
            "term": int(term),
            "kind": baseline_kind,
            "credits": registered_now,
            "courses_assumed_passed": current_courses,
        },
        # Compatibility aliases retained for existing clients. In this feature,
        # "current" means the selected simulation baseline: either the configured
        # term's recommendations or an explicitly requested registered timetable.
        "registered_credits_now": registered_now,
        "gpa": gpa,
        "chain_floor_terms": chain_floor,
        "capacity_floor_terms_after_planning_baseline": capacity_floor,
        "capacity_floor_terms_after_current": capacity_floor,
        "lower_bound_additional_terms": lower_bound,
        "lower_bound_terms_including_planning_baseline": lower_bound_including_baseline,
        "lower_bound_terms_including_current": lower_bound_including_baseline,
        # Backward-compatible aliases now use the 18-credit scenario, not a
        # guessed number of courses per term.
        "pace_terms": capacity_floor,
        "terms_estimate": including_baseline,
        "courses_per_term": None,
        "max_credits_per_term": cap,
        "estimated_additional_terms": additional_terms,
        "estimated_terms_including_planning_baseline": including_baseline,
        "estimated_terms_including_current": including_baseline,
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
            "All planning-baseline and simulated courses are passed on the first attempt.",
            f"Every simulated main term uses a maximum of {cap} credits.",
            "Elective placeholders remain plan requirements; no concrete elective is invented.",
            "Future course offerings, section times, seats, and registration permission are not guaranteed.",
            "The scenario is read-only and does not update the student record or university portal.",
        ],
        "plan_completion_in_planning_baseline_possible": bool(current_courses)
        and simulation["simulation_completed"]
        and additional_terms == 0,
        # Compatibility alias. It means completion of recorded PLAN
        # requirements in the planning baseline, never official graduation.
        "final_term_possible": bool(current_courses)
        and simulation["simulation_completed"]
        and additional_terms == 0,
        "hour_gates": hour_gates,
        "counts": report["counts"],
        "in_progress": in_progress,
    }


def build_expected_plan_graduation_comparison(
    student_id: int,
    year: int,
    term: int,
    *,
    baseline_report: dict | None = None,
    max_credits_per_term: int = DEFAULT_MAX_CREDITS_PER_TERM,
) -> dict:
    """Compare the registrar baseline with the same term's expected plan.

    Registrar rows remain the factual baseline. Expected-only courses are added
    only to a read-only scenario, so the screen can show their effect without
    presenting the expected plan as a completed registration.
    """
    from core.models import ProgrammeRequirement, Student

    registered_rows = get_student_term_baseline(
        student_id,
        str(year),
        str(term),
        snapshot=Snapshot.REGISTERED,
    )
    expected_rows = get_student_term_baseline(
        student_id,
        str(year),
        str(term),
        snapshot=Snapshot.EXPECTED,
    )
    if not registered_rows or not expected_rows:
        return {}

    baseline = baseline_report
    if not baseline or baseline.get("planning_baseline_kind") != REGISTERED_TIMETABLE:
        # The default graduation page is recommendation-based.  It is not a
        # factual registration baseline and therefore cannot be reused for this
        # registrar-vs-expected comparison.
        baseline = build_graduation_report(
            student_id,
            year,
            term,
            planning_baseline_kind=REGISTERED_TIMETABLE,
            max_credits_per_term=max_credits_per_term,
        )
    if not baseline:
        return {}

    program = str(
        baseline.get("program")
        or Student.objects.filter(student_id=student_id).values_list("program", flat=True).first()
        or ""
    ).strip()
    credit_by_code = {
        normalize_code(code): int(credits or 0)
        for code, credits in ProgrammeRequirement.objects.filter(
            program__iexact=program,
        ).values_list("course_code", "credit_hours")
    }
    expected_courses, _expected_snapshot_credits = _current_course_state(
        student_id,
        year,
        term,
        credit_by_code,
        baseline_rows=expected_rows,
        # A StudentCourse studying row is registrar evidence. Do not silently
        # mix it into the expected snapshot; the factual baseline is merged below.
        studying_rows=[],
    )
    registered_courses = [
        dict(course) for course in baseline.get("planning_baseline_courses_assumed_passed") or []
    ]
    registered_codes = {normalize_code(course.get("code") or "") for course in registered_courses}
    additions = [
        dict(course)
        for course in expected_courses
        if normalize_code(course.get("code") or "") not in registered_codes
    ]
    if not additions:
        return {}

    combined_courses = registered_courses + additions
    combined_credits = sum(int(course.get("credits") or 0) for course in combined_courses)
    cap = max(1, int(max_credits_per_term))
    scenario = None
    comparison = None
    if combined_credits <= cap:
        scenario = build_graduation_report(
            student_id,
            year,
            term,
            planning_baseline_kind=REGISTERED_TIMETABLE,
            max_credits_per_term=cap,
            _current_courses_override=combined_courses,
        )
        if scenario:
            comparison = _compare_reports(baseline, scenario, [])

    return {
        "registered_course_count": len(registered_courses),
        "registered_credits": int(baseline.get("registered_credits_at_planning_baseline") or 0),
        "additional_courses": additions,
        "additional_course_count": len(additions),
        "additional_credits": sum(int(course.get("credits") or 0) for course in additions),
        "expected_total_course_count": len(combined_courses),
        "expected_total_credits": combined_credits,
        "credit_cap": cap,
        "scenario_available": bool(scenario),
        "scenario_report": scenario,
        "comparison": comparison,
        "note": (
            "Expected-plan scenario only. These courses are not treated as registered, and the "
            "scenario does not prove offerings, sections, seats, timetable fit, or permission."
        ),
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
        "planning_baseline_kind": report.get("planning_baseline_kind"),
        "planning_baseline_credits": int(report.get("planning_baseline_credits") or 0),
        "simulation_completed": bool(report.get("simulation_completed")),
        "estimated_additional_terms": report.get("estimated_additional_terms"),
        "estimated_terms_including_planning_baseline": report.get(
            "estimated_terms_including_planning_baseline"
        ),
        "estimated_terms_including_current": report.get("estimated_terms_including_current"),
        "lower_bound_additional_terms": report.get("lower_bound_additional_terms"),
        "lower_bound_terms_including_planning_baseline": report.get(
            "lower_bound_terms_including_planning_baseline"
        ),
        "lower_bound_terms_including_current": report.get("lower_bound_terms_including_current"),
        "registered_credits_at_planning_baseline": int(
            report.get("registered_credits_at_planning_baseline") or 0
        ),
        "registered_credits_now": int(report.get("registered_credits_now") or 0),
        "planning_baseline_courses_assumed_passed": report.get(
            "planning_baseline_courses_assumed_passed"
        )
        or [],
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


def _course_term_positions(report: dict) -> dict[str, dict]:
    """Map each scheduled course to its neutral baseline/future location."""
    positions: dict[str, dict] = {}
    baseline_position = {
        "academic_year": int(report.get("planning_baseline_academic_year") or 0),
        "term": int(report.get("planning_baseline_term") or 0),
        "sequence": 0,
        "baseline": True,
    }
    for course in report.get("planning_baseline_courses_assumed_passed") or []:
        code = normalize_code(course.get("code") or "") if isinstance(course, dict) else ""
        if code:
            positions[code] = dict(baseline_position)

    for planned_term in report.get("term_plan") or []:
        position = {
            "academic_year": int(planned_term.get("academic_year") or 0),
            "term": int(planned_term.get("term") or 0),
            "sequence": int(planned_term.get("sequence") or 0),
            "baseline": False,
        }
        for raw_code in planned_term.get("course_codes") or []:
            code = normalize_code(raw_code)
            if code:
                positions[code] = dict(position)
    return positions


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
    baseline_positions = _course_term_positions(baseline)
    scenario_positions = _course_term_positions(scenario)
    term_plan_changes = [
        {
            "code": code,
            "before": baseline_positions.get(code),
            "after": scenario_positions.get(code),
            "became_unresolved": (code not in baseline_unresolved and code in scenario_unresolved),
        }
        for code in sorted(set(baseline_positions) | set(scenario_positions))
        if baseline_positions.get(code) != scenario_positions.get(code)
    ]
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

    scenario_planned_terms = {
        code: planned_term
        for planned_term in scenario.get("term_plan") or []
        for code in planned_term.get("course_codes") or []
    }
    deferred_courses = []
    for code in removed_codes:
        planned_term = scenario_planned_terms.get(code)
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
        "baseline_planning_credits": int(
            baseline.get("registered_credits_at_planning_baseline") or 0
        ),
        "scenario_planning_credits": int(
            scenario.get("registered_credits_at_planning_baseline") or 0
        ),
        "planning_credit_change": int(scenario.get("registered_credits_at_planning_baseline") or 0)
        - int(baseline.get("registered_credits_at_planning_baseline") or 0),
        # Compatibility aliases for existing local clients.
        "baseline_current_credits": int(baseline.get("registered_credits_now") or 0),
        "scenario_current_credits": int(scenario.get("registered_credits_now") or 0),
        "current_credit_change": int(scenario.get("registered_credits_now") or 0)
        - int(baseline.get("registered_credits_now") or 0),
        "plan_changed": bool(term_plan_changes),
        "term_plan_changes": term_plan_changes,
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
    query_cache: dict[object, object] | None = None,
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

    baseline_courses = baseline.get("planning_baseline_courses_assumed_passed") or []
    baseline_by_code = {course["code"]: dict(course) for course in baseline_courses}
    for code in remove_codes:
        if code not in baseline_by_code:
            errors.append({"kind": "NOT_IN_CURRENT_TIMETABLE", "course_code": code})
    for code in add_codes:
        if code in baseline_by_code and code not in remove_codes:
            errors.append({"kind": "ALREADY_IN_CURRENT_TIMETABLE", "course_code": code})

    passed_key = ("passed_and_studying", int(student_id))
    cached_status = query_cache.get(passed_key) if query_cache is not None else None
    if isinstance(cached_status, tuple):
        actual_passed, _actual_studying = cached_status
    else:
        actual_passed, _actual_studying = get_student_passed_and_studying(student_id)
        if query_cache is not None:
            query_cache[passed_key] = (actual_passed, _actual_studying)
    actual_passed = {normalize_code(code) for code in actual_passed}
    for code in add_codes:
        if code in actual_passed:
            errors.append({"kind": "ALREADY_PASSED", "course_code": code})

    plan_key = ("scenario_plan_rows", program.strip().upper())
    cached_plan_rows = query_cache.get(plan_key) if query_cache is not None else None
    if isinstance(cached_plan_rows, dict):
        plan_rows = cached_plan_rows
    else:
        plan_rows = {
            normalize_code(row["course_code"]): row
            for row in ProgrammeRequirement.objects.filter(program=program).values(
                "course_code", "course_name", "credit_hours", "type"
            )
        }
        if query_cache is not None:
            query_cache[plan_key] = plan_rows

    course_key = ("scenario_course_rows", program.strip().upper())
    cached_course_rows = query_cache.get(course_key) if query_cache is not None else None
    missing_course_codes = (
        set(add_codes) - set(cached_course_rows or {})
        if isinstance(cached_course_rows, dict)
        else set(add_codes)
    )
    course_rows = dict(cached_course_rows) if isinstance(cached_course_rows, dict) else {}
    if missing_course_codes:
        course_rows.update(
            {
                normalize_code(course.course_code): course
                for course in Course.objects.filter(course_code__in=missing_course_codes)
            }
        )
        if query_cache is not None:
            query_cache[course_key] = course_rows

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
    modified_credits = sum(int(course.get("credits") or 0) for course in modified_courses)
    if modified_credits > max_credits_per_term:
        errors.append(
            {
                "kind": "SCENARIO_EXCEEDS_CREDIT_CAP",
                "credits": modified_credits,
                "maximum": max_credits_per_term,
            }
        )

    earned_key = ("earned_credits", int(student_id))
    cached_earned = query_cache.get(earned_key) if query_cache is not None else None
    if isinstance(cached_earned, int):
        earned = cached_earned
    else:
        earned = int(
            Student.objects.filter(student_id=student_id)
            .values_list("total_earned_credits", flat=True)
            .first()
            or 0
        )
        if query_cache is not None:
            query_cache[earned_key] = earned

    prerequisite_key = ("program_prerequisites", program.strip().upper())
    cached_prerequisites = query_cache.get(prerequisite_key) if query_cache is not None else None
    if isinstance(cached_prerequisites, dict):
        prerequisites_by_course = cached_prerequisites
    else:
        prerequisites_by_course = get_program_prerequisites(program)
        if query_cache is not None:
            query_cache[prerequisite_key] = prerequisites_by_course
    # A planning-baseline peer is not a passed prerequisite. The forecast may
    # assume all baseline courses pass before the *next* simulated term, but a
    # replacement being added to that same baseline cannot use a retained course
    # as though it were already completed. Corequisites require an explicit rule;
    # none is represented in this data model.
    satisfied = set(actual_passed)
    for addition in additions:
        code = addition["code"]
        course_prereqs, required_hours = split_hour_prereqs(prerequisites_by_course.get(code, []))
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
    query_cache: dict[object, object] | None = None,
) -> dict:
    prepared = _prepare_current_term_changes(
        student_id=student_id,
        program=str(baseline.get("program") or ""),
        baseline=baseline,
        remove_codes=remove_codes,
        add_codes=add_codes,
        max_credits_per_term=max_credits_per_term,
        query_cache=query_cache,
    )
    if not prepared["valid"]:
        return {**prepared, "scenario_report": None, "comparison": None}

    scenario = build_graduation_report(
        student_id,
        year,
        term,
        planning_baseline_kind=str(baseline.get("planning_baseline_kind") or REGISTERED_TIMETABLE),
        max_credits_per_term=max_credits_per_term,
        _current_courses_override=prepared["current_courses"],
        _excluded_studying_codes=set(remove_codes),
        _prerequisite_map=(
            query_cache.get(
                ("program_prerequisites", str(baseline.get("program") or "").strip().upper())
            )
            if query_cache is not None
            else None
        ),
        _query_cache=query_cache,
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
    planning_baseline_kind: str = REGISTERED_TIMETABLE,
    remove_current_courses: list[str] | None = None,
    add_current_courses: list[str] | None = None,
    search_better_replacements: bool = False,
    max_credits_per_term: int = DEFAULT_MAX_CREDITS_PER_TERM,
    max_replacement_results: int = MAX_REPLACEMENT_RESULTS,
    replacement_remove_course: str | None = None,
    replacement_add_course: str | None = None,
    exact_result_credits: int | None = None,
    max_result_credits: int | None = None,
    _query_cache: dict[object, object] | None = None,
) -> dict:
    """Compare planning-baseline changes without mutating real records.

    Result-credit predicates constrain replacement-search candidates before the
    bounded result slice. They do not broaden the 120-pair evaluation ceiling.
    """
    from core.models import Course

    baseline_kind = _validate_planning_baseline_kind(planning_baseline_kind)
    cap = max(1, int(max_credits_per_term))
    replacement_result_limit = max(
        1,
        min(int(max_replacement_results), MAX_REPLACEMENT_EVALUATIONS),
    )
    exact_replacement_credits = (
        int(exact_result_credits) if exact_result_credits is not None else None
    )
    maximum_replacement_credits = (
        int(max_result_credits) if max_result_credits is not None else None
    )
    if exact_replacement_credits is not None and exact_replacement_credits < 0:
        raise ValueError("exact_result_credits must be zero or greater.")
    if maximum_replacement_credits is not None and maximum_replacement_credits < 0:
        raise ValueError("max_result_credits must be zero or greater.")
    if (
        exact_replacement_credits is not None
        and maximum_replacement_credits is not None
        and exact_replacement_credits > maximum_replacement_credits
    ):
        raise ValueError("exact_result_credits cannot exceed max_result_credits.")
    remove_codes = _normalise_code_list(remove_current_courses)
    add_codes = _normalise_code_list(add_current_courses)
    replacement_remove_filter = normalize_code(replacement_remove_course or "")
    replacement_add_filter = normalize_code(replacement_add_course or "")
    # All candidates in this bounded what-if call observe one immutable database
    # snapshot. Reuse those reads only for the lifetime of this request; a later
    # request always starts from fresh rows.
    query_cache: dict[object, object] = _query_cache if _query_cache is not None else {}
    baseline = build_graduation_report(
        student_id,
        year,
        term,
        planning_baseline_kind=baseline_kind,
        max_credits_per_term=cap,
        _query_cache=query_cache,
    )
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
            query_cache=query_cache,
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
        course["code"] for course in baseline.get("planning_baseline_courses_assumed_passed") or []
    }
    unlock = build_unlock_report(
        student_id,
        year,
        term,
        additional_studying_codes=current_codes,
        registered_credits_override=int(
            baseline.get("registered_credits_at_planning_baseline") or 0
        ),
        _query_cache=query_cache,
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
    if replacement_add_filter:
        candidate_codes = [code for code in candidate_codes if code == replacement_add_filter]

    removable_courses = list(baseline.get("planning_baseline_courses_assumed_passed") or [])
    if replacement_remove_filter:
        removable_courses = [
            course
            for course in removable_courses
            if normalize_code(course.get("code") or "") == replacement_remove_filter
        ]

    improving = []
    unproven_blocker_progress_pairs = 0
    result_credit_predicate_filtered_count = 0
    evaluated_count = 0
    truncated = False
    for removed in removable_courses:
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
                query_cache=query_cache,
            )
            comparison = evaluated.get("comparison")
            if not evaluated["valid"] or not comparison:
                continue
            if comparison.get("blocker_progress_only"):
                unproven_blocker_progress_pairs += 1
            if not comparison["proven_improvement"]:
                continue
            scenario_credits_raw = comparison.get("scenario_planning_credits")
            scenario_credits = (
                int(scenario_credits_raw)
                if scenario_credits_raw is not None
                else sum(int(course.get("credits") or 0) for course in evaluated["current_courses"])
            )
            if (
                exact_replacement_credits is not None
                and scenario_credits != exact_replacement_credits
            ) or (
                maximum_replacement_credits is not None
                and scenario_credits > maximum_replacement_credits
            ):
                result_credit_predicate_filtered_count += 1
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

    result_credit_search: dict[str, object] = {}
    if exact_replacement_credits is not None or maximum_replacement_credits is not None:
        result_credit_search = {
            "result_credit_predicate": {
                "exact_credit_hours": exact_replacement_credits,
                "maximum_credit_hours": maximum_replacement_credits,
            },
            "result_credit_predicate_filtered_count": (result_credit_predicate_filtered_count),
        }

    return {
        **baseline,
        "what_if": {
            "mode": "replacement_search",
            "valid": True,
            "validation_errors": [],
            "baseline": _scenario_summary(baseline),
            "candidate_courses_considered": candidate_codes,
            "requested_remove_course": replacement_remove_filter or None,
            "requested_add_course": replacement_add_filter or None,
            "pairs_evaluated": evaluated_count,
            "search_truncated": truncated,
            "unproven_blocker_progress_pairs": unproven_blocker_progress_pairs,
            "improving_replacements_found": len(improving),
            "replacement_results_truncated": len(improving) > replacement_result_limit,
            "improving_replacements": improving[:replacement_result_limit],
            **result_credit_search,
            "no_proven_improvement": not improving,
            "timetable_check_required": bool(improving),
            "note": (
                "Academic comparison only. Candidates are prerequisite-ready in recorded "
                "data, but offerings, seats, timetable clashes, and registration permission "
                "are not established."
            ),
        },
    }

"""
core/services/exam_timetable.py
In-memory exam-timetable pipeline.

1. build_enrolled_sets      – read StudentCourse(status='studying') → {course_code: {student_ids}}
2. build_conflict_graph     – pairwise overlap → adjacency dict + edge list
3. build_plan_term_buckets  – map running courses to (program, programme_term) buckets
4. check_bucket_feasibility – verify no bucket exceeds available days
5. schedule                 – greedy graph-coloring → course→slot assignments
6. build_exam_timetable     – orchestrator: runs 1→5, QA, persists JSON
"""

from __future__ import annotations

import itertools
import json
from collections import defaultdict

from core.models import ExamTimetableRun, ProgrammeRequirement, StudentCourse

# ── 1. Enrolled sets ────────────────────────────────────────────


def build_enrolled_sets(
    programs: list[str] | None = None,
    sections: list[str] | None = None,
) -> dict[str, set[int]]:
    """Return {course_code: {student_id, …}} for 'studying' enrolments.

    Optional filters narrow the student population:
        programs – only include students whose program is in this list
        sections – only include students whose section is in this list
    When a filter is None or empty, it is ignored (all values pass).
    """
    qs = StudentCourse.objects.filter(status="studying").select_related("course", "student")
    if programs:
        qs = qs.filter(student__program__in=programs)
    if sections:
        qs = qs.filter(student__section__in=sections)

    rows = qs.values_list("course__course_code", "student_id")
    enrolled: dict[str, set[int]] = defaultdict(set)
    for course_code, student_id in rows:
        enrolled[course_code].add(student_id)
    return dict(enrolled)


# ── 2. Conflict graph ──────────────────────────────────────────


def build_conflict_graph(
    enrolled_sets: dict[str, set[int]],
) -> tuple[list[dict], dict[str, dict[str, int]]]:
    """
    Build conflict edges from enrolled sets.

    Returns:
        conflicts  – list of {course_a, course_b, shared} with course_a < course_b
        adj        – adjacency dict {course: {neighbour: weight, …}}
    """
    # Invert: student → [courses]
    student_courses: dict[int, list[str]] = defaultdict(list)
    for course_code, students in enrolled_sets.items():
        for sid in students:
            student_courses[sid].append(course_code)

    # Count pairwise overlaps
    edge_counts: dict[tuple[str, str], int] = defaultdict(int)
    for courses in student_courses.values():
        for a, b in itertools.combinations(sorted(set(courses)), 2):
            edge_counts[(a, b)] += 1

    # Build output
    conflicts: list[dict] = []
    adj: dict[str, dict[str, int]] = defaultdict(dict)
    for (a, b), cnt in edge_counts.items():
        conflicts.append({"course_a": a, "course_b": b, "shared": cnt})
        adj[a][b] = cnt
        adj[b][a] = cnt

    return conflicts, dict(adj)


# ── 3. Programme-plan term buckets ─────────────────────────────


def build_plan_term_buckets(
    running_courses: set[str],
) -> tuple[dict[tuple[str, int], set[str]], dict[str, list[tuple[str, int]]]]:
    """Map running courses to (program, programme_term) buckets.

    Returns:
        buckets       – {(program, programme_term): {course_codes}}
        course_buckets – {course_code: [(program, term), …]} reverse index
    """
    rows = ProgrammeRequirement.objects.filter(
        course_code__in=running_courses, programme_term__isnull=False
    ).values_list("program", "course_code", "programme_term")

    buckets: dict[tuple[str, int], set[str]] = defaultdict(set)
    course_buckets: dict[str, list[tuple[str, int]]] = defaultdict(list)

    for program, course_code, programme_term in rows:
        key = (program, int(programme_term))
        buckets[key].add(course_code)
        course_buckets[course_code].append(key)

    return dict(buckets), dict(course_buckets)


def check_bucket_feasibility(
    buckets: dict[tuple[str, int], set[str]],
    num_days: int,
) -> list[dict]:
    """Return list of violations where a bucket has more courses than days.

    Each violation: {program, programme_term, bucket_size, num_days, courses}
    Empty list means all buckets are feasible.
    """
    violations: list[dict] = []
    for (program, term), courses in sorted(buckets.items()):
        if len(courses) > num_days:
            violations.append(
                {
                    "program": program,
                    "programme_term": term,
                    "bucket_size": len(courses),
                    "num_days": num_days,
                    "courses": sorted(courses),
                }
            )
    return violations


# ── 4. Greedy scheduler ───────────────────────────────────────


def schedule(
    courses: list[str],
    adj: dict[str, dict[str, int]],
    slots: list[dict],
    enrolled_sets: dict[str, set[int]] | None = None,
    max_per_day: int = 2,
    plan_term_buckets: dict[tuple[str, int], set[str]] | None = None,
    course_buckets: dict[str, list[tuple[str, int]]] | None = None,
    pinned: list[dict] | None = None,
) -> list[dict]:
    """
    Greedy graph-coloring with day-spread soft constraint.

    Hard constraints:
      A. No two conflicting courses in the same slot (student clash).
      B. No two courses from the same (program, programme_term) bucket
         on the same day.

    Soft constraints (in priority order):
      1. Minimise students with >max_per_day exams on one day.
      2. Maximise spacing within (program, term) buckets (penalise
         small day gaps between bucket-mates).
      3. Balance load across slots (prefer less-loaded slots as tiebreaker).

    Args:
        courses            – list of course codes to schedule
        adj                – adjacency dict from build_conflict_graph
        slots              – list of {index, day, period} dicts
        enrolled_sets      – {course_code: {student_ids}} for soft-constraint scoring
        max_per_day        – soft cap on exams per student per day (default 2)
        plan_term_buckets  – {(program, term): {course_codes}} hard day-rule buckets
        course_buckets     – {course_code: [(program, term), …]} reverse index
        pinned             – list of {course_code, day, period} to fix before scheduling

    Returns:
        list of {course_code, slot_index, day, period}
    """
    max_slot_idx = max((s["index"] for s in slots), default=-1)
    slot_by_index: dict[int, dict] = {s["index"]: s for s in slots}

    # Build day-index lookup for spacing calculation
    unique_days: list[str] = []
    day_set: set[str] = set()
    for s in slots:
        if s["day"] not in day_set:
            day_set.add(s["day"])
            unique_days.append(s["day"])
    day_to_idx: dict[str, int] = {d: i for i, d in enumerate(unique_days)}

    # Sort courses by total constraint degree DESC (most constrained first)
    _ptb = plan_term_buckets or {}
    _cb = course_buckets or {}

    def _constraint_degree(c: str) -> int:
        adj_deg = len(adj.get(c, {}))
        bucket_deg = sum(len(_ptb.get(bk, set())) for bk in _cb.get(c, []))
        return adj_deg + bucket_deg

    courses_sorted = sorted(courses, key=_constraint_degree, reverse=True)

    assignment: dict[str, int] = {}  # course_code → slot_index

    # Track how many exams each student already has per day
    student_day_count: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    # Track how many courses are assigned to each slot (for load-balancing)
    slot_load: dict[int, int] = defaultdict(int)

    # Track bucket-day assignments for hard constraint B
    # bucket_day_courses[(P,k)][day] = set of course_codes
    bucket_day_courses: dict[tuple[str, int], dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )

    # Track which day each course is assigned to (for spacing calculation)
    course_assigned_day: dict[str, str] = {}

    # Pre-assign pinned courses (user overrides — bypass constraints)
    if pinned:
        dp_to_slot = {(s["day"], s["period"]): s["index"] for s in slots}
        for pin in pinned:
            cc = pin.get("course_code", "")
            p_day = pin.get("day", "")
            p_period = pin.get("period", "")
            si = dp_to_slot.get((p_day, p_period))
            if si is None or cc not in set(courses):
                continue
            assignment[cc] = si
            slot_load[si] += 1
            course_assigned_day[cc] = p_day
            if enrolled_sets and cc in enrolled_sets:
                for sid in enrolled_sets[cc]:
                    student_day_count[sid][p_day] += 1
            for bk in _cb.get(cc, []):
                bucket_day_courses[bk][p_day].add(cc)

    for course in courses_sorted:
        if course in assignment:
            continue  # already pinned
        neighbours = adj.get(course, {})
        used_slots = {assignment[n] for n in neighbours if n in assignment}

        # Collect all conflict-free candidate slots (hard constraint A)
        candidates = [si for si in range(max_slot_idx + 1) if si not in used_slots]

        # Apply hard constraint B: remove candidates whose day
        # already has a bucket-mate from ANY of this course's buckets
        my_buckets = _cb.get(course, [])
        if my_buckets and candidates:
            blocked_days: set[str] = set()
            for bk in my_buckets:
                for day, assigned in bucket_day_courses[bk].items():
                    if assigned:  # day already has a course from this bucket
                        blocked_days.add(day)
            if blocked_days:
                candidates = [
                    si for si in candidates if slot_by_index[si]["day"] not in blocked_days
                ]

        if not candidates:
            # Overflow: no feasible slot found
            max_slot_idx += 1
            chosen = max_slot_idx
            slot_by_index[chosen] = {
                "index": chosen,
                "day": "OVERFLOW",
                "period": f"Extra-{chosen}",
            }
            assignment[course] = chosen
            slot_load[chosen] += 1
            if enrolled_sets and course in enrolled_sets:
                for sid in enrolled_sets[course]:
                    student_day_count[sid]["OVERFLOW"] += 1
            continue

        if enrolled_sets and course in enrolled_sets:
            # Score each candidate with three-level priority:
            #   (day_overload_penalty, spacing_penalty, slot_load)
            course_students = enrolled_sets[course]
            best_slot = candidates[0]
            best_score = (float("inf"), float("inf"), float("inf"))

            for si in candidates:
                day = slot_by_index[si]["day"]

                # 1) Day-overload penalty
                penalty = 0
                for sid in course_students:
                    if student_day_count[sid][day] >= max_per_day:
                        penalty += 1

                # 2) Spacing penalty within buckets
                spacing_penalty = 0
                if my_buckets and day in day_to_idx:
                    cand_di = day_to_idx[day]
                    for bk in my_buckets:
                        for mate in _ptb.get(bk, set()) - {course}:
                            if mate in course_assigned_day:
                                mate_day = course_assigned_day[mate]
                                if mate_day in day_to_idx:
                                    gap = abs(cand_di - day_to_idx[mate_day])
                                    if gap <= 1:
                                        spacing_penalty += 100
                                    elif gap <= 2:
                                        spacing_penalty += 30
                                    elif gap <= 3:
                                        spacing_penalty += 10

                score = (penalty, spacing_penalty, slot_load[si])
                if score < best_score:
                    best_score = score
                    best_slot = si

            chosen = best_slot
        else:
            # No enrolled data — pick least-loaded slot
            chosen = min(candidates, key=lambda si: slot_load[si])

        assignment[course] = chosen
        slot_load[chosen] += 1
        chosen_day = slot_by_index[chosen]["day"]
        course_assigned_day[course] = chosen_day

        # Update student day counts
        if enrolled_sets and course in enrolled_sets:
            for sid in enrolled_sets[course]:
                student_day_count[sid][chosen_day] += 1

        # Update bucket-day tracking
        for bk in my_buckets:
            bucket_day_courses[bk][chosen_day].add(course)

    # Build result
    result: list[dict] = []
    for cc, si in assignment.items():
        slot = slot_by_index[si]
        result.append(
            {
                "course_code": cc,
                "slot_index": si,
                "day": slot["day"],
                "period": slot["period"],
            }
        )

    return sorted(result, key=lambda r: (r["slot_index"], r["course_code"]))


# ── 4. QA report ────────────────────────────────────────────────


def _build_qa(
    enrolled_sets: dict[str, set[int]],
    schedule_entries: list[dict],
    adj: dict[str, dict[str, int]],
    max_per_day: int = 2,
    plan_term_buckets: dict[tuple[str, int], set[str]] | None = None,
) -> dict:
    """Validate the schedule and produce a QA report."""
    all_students: set[int] = set()
    for sids in enrolled_sets.values():
        all_students |= sids

    course_slot: dict[str, int] = {e["course_code"]: e["slot_index"] for e in schedule_entries}
    course_day: dict[str, str] = {e["course_code"]: e["day"] for e in schedule_entries}

    slots_used = len({e["slot_index"] for e in schedule_entries})

    # Invert: student → courses
    student_courses: dict[int, list[str]] = defaultdict(list)
    for cc, sids in enrolled_sets.items():
        for sid in sids:
            student_courses[sid].append(cc)

    same_slot_conflicts: list[dict] = []
    max_exams_per_day: int = 0
    students_over_limit_per_day: int = 0  # count of students exceeding max_per_day

    for sid, courses in student_courses.items():
        # Same-slot check
        slot_groups: dict[int, list[str]] = defaultdict(list)
        day_groups: dict[str, list[str]] = defaultdict(list)
        for cc in courses:
            si = course_slot.get(cc)
            if si is not None:
                slot_groups[si].append(cc)
            day = course_day.get(cc)
            if day is not None:
                day_groups[day].append(cc)

        for si, ccs in slot_groups.items():
            if len(ccs) >= 2:
                same_slot_conflicts.append(
                    {
                        "student_id": sid,
                        "slot_index": si,
                        "courses": ccs,
                    }
                )

        has_overload = False
        for _day, ccs in day_groups.items():
            day_count = len(ccs)
            max_exams_per_day = max(max_exams_per_day, day_count)
            if day_count > max_per_day:
                has_overload = True
        if has_overload:
            students_over_limit_per_day += 1

    # ── Bucket (programme-plan term) day-rule verification ──
    bucket_day_violations: list[dict] = []
    bucket_count = 0
    if plan_term_buckets:
        bucket_count = len(plan_term_buckets)
        for (program, term), bucket_courses in sorted(plan_term_buckets.items()):
            # Group bucket courses by their assigned day
            day_groups_b: dict[str, list[str]] = defaultdict(list)
            for cc in bucket_courses:
                day = course_day.get(cc)
                if day is not None and day != "OVERFLOW":
                    day_groups_b[day].append(cc)
            for day, ccs in day_groups_b.items():
                if len(ccs) >= 2:
                    bucket_day_violations.append(
                        {
                            "program": program,
                            "programme_term": term,
                            "day": day,
                            "courses": sorted(ccs),
                        }
                    )

    return {
        "total_courses": len(enrolled_sets),
        "total_students": len(all_students),
        "slots_used": slots_used,
        "max_per_day": max_per_day,
        "max_exams_per_day_per_student": max_exams_per_day,
        "students_over_limit_per_day": students_over_limit_per_day,
        "same_slot_conflicts": same_slot_conflicts,
        "conflict_count": len(same_slot_conflicts),
        "bucket_count": bucket_count,
        "bucket_day_violations": bucket_day_violations,
        "bucket_day_violations_count": len(bucket_day_violations),
    }


# ── 6. Orchestrator ────────────────────────────────────────────


def build_exam_timetable(
    label: str,
    days: list[str],
    periods: list[str],
    max_per_day: int = 2,
    programs: list[str] | None = None,
    sections: list[str] | None = None,
    selected_courses: list[str] | None = None,
    pinned: list[dict] | None = None,
) -> dict:
    """
    End-to-end pipeline: build enrolled sets → conflict graph →
    programme-plan term buckets → feasibility check → greedy schedule
    → QA → persist JSON.

    Returns the full result dict (also saved in ExamTimetableRun).
    If a bucket feasibility violation is found, returns an error dict
    without scheduling (key "feasibility_error": True).

    Args:
        selected_courses – if provided, restrict scheduling to only
                           these course codes (user-curated list from
                           the preview step).
    """
    # 1. Enrolled sets
    enrolled_sets = build_enrolled_sets(programs=programs, sections=sections)

    # 1b. Filter to user-selected courses (if provided from preview step)
    if selected_courses is not None:
        keep = set(selected_courses)
        enrolled_sets = {cc: sids for cc, sids in enrolled_sets.items() if cc in keep}

    course_list = sorted(enrolled_sets.keys())

    all_students: set[int] = set()
    for sids in enrolled_sets.values():
        all_students |= sids

    # 2. Conflict graph
    conflicts, adj = build_conflict_graph(enrolled_sets)

    # 3. Programme-plan term buckets
    ptb, cb = build_plan_term_buckets(set(course_list))

    # 4. Feasibility pre-check
    violations = check_bucket_feasibility(ptb, len(days))
    if violations:
        return {
            "feasibility_error": True,
            "violations": violations,
            "courses_count": len(course_list),
            "students_count": len(all_students),
            "bucket_count": len(ptb),
        }

    # 5. Slot pool (Cartesian product)
    slots: list[dict] = []
    idx = 0
    for day in days:
        for period in periods:
            slots.append({"index": idx, "day": day, "period": period})
            idx += 1

    # 6. Schedule (with day-spread + bucket constraints)
    schedule_entries = schedule(
        course_list,
        adj,
        slots,
        enrolled_sets=enrolled_sets,
        max_per_day=max_per_day,
        plan_term_buckets=ptb,
        course_buckets=cb,
        pinned=pinned,
    )

    # 7. QA
    qa = _build_qa(
        enrolled_sets,
        schedule_entries,
        adj,
        max_per_day=max_per_day,
        plan_term_buckets=ptb,
    )

    # Bucket summary for the result
    buckets_summary: list[dict] = []
    for (program, term), bucket_courses in sorted(ptb.items()):
        buckets_summary.append(
            {
                "program": program,
                "programme_term": term,
                "course_count": len(bucket_courses),
                "courses": sorted(bucket_courses),
            }
        )

    # Assemble result
    result: dict = {
        "students_count": len(all_students),
        "courses": course_list,
        "courses_count": len(course_list),
        "conflicts": conflicts,
        "conflicts_count": len(conflicts),
        "slots": slots,
        "schedule": schedule_entries,
        "qa": qa,
        "buckets_summary": buckets_summary,
        "bucket_count": len(ptb),
    }

    # Persist
    run = ExamTimetableRun.objects.create(
        label=label,
        result_json=json.dumps(result, ensure_ascii=False),
    )
    result["run_id"] = run.id

    return result

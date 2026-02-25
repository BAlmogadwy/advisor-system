"""
core/services/exam_timetable.py
In-memory exam-timetable pipeline.

Pipeline sections (section numbers match ``# ── N.`` markers below):

0. Credit helpers             – build_credit_map, _credit_pair_penalty
1. Enrolled sets              – build_enrolled_sets → {course_code: {student_ids}}
2. Conflict graph             – build_conflict_graph → adjacency dict + edge list
3. Programme-plan term buckets – build_plan_term_buckets, check_bucket_feasibility
4. Greedy scheduler           – schedule → course→slot assignments (graph-coloring)
5. QA report                  – _build_qa → validation + soft-constraint metrics
6. Orchestrator               – build_exam_timetable → runs 0→5, persists JSON
7. Excel export               – export_exam_timetable_xlsx → styled .xlsx workbook
"""

from __future__ import annotations

import itertools
import json
from collections import defaultdict
from pathlib import Path

from core.models import Course, ExamTimetableRun, ProgrammeRequirement, StudentCourse

# ── 0. Credit helpers ───────────────────────────────────────────

_CREDIT_DEFAULT = 3  # fallback for NULL / 0 / missing credit_hours

# Penalty ladder for the top-2 heaviest exams on a single day.
# Key = (max_credits, min_credits); value = penalty weight.
_CREDIT_PAIR_WEIGHTS: dict[tuple[int, int], int] = {
    (4, 4): 100,  # worst – two heavy courses
    (4, 3): 30,   # acceptable
    (4, 2): 0,    # ideal pairing
}
_CREDIT_PAIR_FALLBACK = 5  # other combos (3+3, 3+2, 2+2, …)


def build_credit_map(course_codes: list[str] | set[str]) -> dict[str, int]:
    """Return {course_code: credit_hours} for the given courses.

    Missing / NULL / zero credit_hours default to ``_CREDIT_DEFAULT``
    so the penalty formula degrades gracefully.
    """
    rows = Course.objects.filter(
        course_code__in=list(course_codes),
    ).values_list("course_code", "credit_hours")
    cm: dict[str, int] = {
        cc: (ch if ch and ch > 0 else _CREDIT_DEFAULT) for cc, ch in rows
    }
    for cc in course_codes:
        cm.setdefault(cc, _CREDIT_DEFAULT)
    return cm


def _credit_pair_penalty(credits_on_day: list[int]) -> int:
    """Penalty for the top-2 heaviest exams on a single day for one student.

    Returns 0 when there are fewer than 2 exams.
    """
    if len(credits_on_day) < 2:
        return 0
    top2 = sorted(credits_on_day, reverse=True)[:2]
    pair = (top2[0], top2[1])
    return _CREDIT_PAIR_WEIGHTS.get(pair, _CREDIT_PAIR_FALLBACK)


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
    # Invert: student_id → [course_codes] so we can iterate per-student
    student_courses: dict[int, list[str]] = defaultdict(list)
    for course_code, students in enrolled_sets.items():
        for sid in students:
            student_courses[sid].append(course_code)

    # Count pairwise overlaps: for each student, every pair of their courses
    # shares that student.  sorted() ensures (a,b) key is deterministic.
    edge_counts: dict[tuple[str, str], int] = defaultdict(int)
    for courses in student_courses.values():
        for a, b in itertools.combinations(sorted(set(courses)), 2):
            edge_counts[(a, b)] += 1

    # Build adjacency dict (bidirectional) + flat edge list for the frontend
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

    # Forward index: (program, term) → {course_codes}
    buckets: dict[tuple[str, int], set[str]] = defaultdict(set)
    # Reverse index: course_code → [(program, term), …]  (a course can appear
    # in multiple programmes, e.g. service courses shared across AI & DS)
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
    credit_map: dict[str, int] | None = None,
) -> list[dict]:
    """
    Greedy graph-coloring with day-spread soft constraint.

    Hard constraints:
      A. No two conflicting courses in the same slot (student clash).
      B. No two courses from the same (program, programme_term) bucket
         on the same day.

    Soft constraints (in priority order):
      1.   Minimise students with >max_per_day exams on one day.
      1.5  Credit-pair penalty: when multi-exam days are unavoidable,
           prefer lighter pairings (4+2 < 4+3 < 4+4).
      2.   Maximise spacing within (program, term) buckets (penalise
           small day gaps between bucket-mates).
      3.   Balance load across slots (prefer less-loaded slots as tiebreaker).

    Args:
        courses            – list of course codes to schedule
        adj                – adjacency dict from build_conflict_graph
        slots              – list of {index, day, period} dicts
        enrolled_sets      – {course_code: {student_ids}} for soft-constraint scoring
        max_per_day        – soft cap on exams per student per day (default 2)
        plan_term_buckets  – {(program, term): {course_codes}} hard day-rule buckets
        course_buckets     – {course_code: [(program, term), …]} reverse index
        pinned             – list of {course_code, day, period} to fix before scheduling
        credit_map         – {course_code: credit_hours} for credit-pair penalty

    Returns:
        list of {course_code, slot_index, day, period}
    """
    # ── Preparation: build lookup tables ──
    max_slot_idx = max((s["index"] for s in slots), default=-1)
    slot_by_index: dict[int, dict] = {s["index"]: s for s in slots}

    # Day-index mapping: convert day names ("Sun", "Mon") to integers (0, 1)
    # so we can compute numeric spacing gaps between bucket-mates.
    unique_days: list[str] = []
    day_set: set[str] = set()
    for s in slots:
        if s["day"] not in day_set:
            day_set.add(s["day"])
            unique_days.append(s["day"])
    day_to_idx: dict[str, int] = {d: i for i, d in enumerate(unique_days)}

    # Shorthand aliases for optional dicts (avoid repeated `or {}` everywhere)
    _ptb = plan_term_buckets or {}  # (program, term) → {course_codes}
    _cb = course_buckets or {}      # course_code → [(program, term), …]
    _cm = credit_map or {}          # course_code → credit_hours

    def _constraint_degree(c: str) -> int:
        """Heuristic: courses with more conflicts + more bucket-mates are harder
        to place, so we schedule them first (most-constrained-first ordering)."""
        adj_deg = len(adj.get(c, {}))
        bucket_deg = sum(len(_ptb.get(bk, set())) for bk in _cb.get(c, []))
        return adj_deg + bucket_deg

    courses_sorted = sorted(courses, key=_constraint_degree, reverse=True)

    # ── Mutable state: updated as each course is placed ──
    #
    # assignment:          final result — which slot each course lands in
    # student_day_count:   how many exams each student has per day (Level 1 scoring)
    # student_day_courses: which courses each student has per day (Level 1.5 credit scoring)
    # slot_load:           how many courses are in each slot (Level 3 load-balancing)
    # bucket_day_courses:  which courses are assigned to each day per bucket (hard constraint B)
    # course_assigned_day: which day each course is on (spacing calculation)

    assignment: dict[str, int] = {}  # course_code → slot_index

    student_day_count: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    student_day_courses: dict[int, dict[str, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )

    slot_load: dict[int, int] = defaultdict(int)

    bucket_day_courses: dict[tuple[str, int], dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )

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
                    student_day_courses[sid][p_day].append(cc)
            for bk in _cb.get(cc, []):
                bucket_day_courses[bk][p_day].add(cc)

    # ── Main scheduling loop ──
    # Process courses in most-constrained-first order.  For each course:
    #   1. Eliminate slots blocked by hard constraints (A: clash, B: bucket-day)
    #   2. If no slot survives → create OVERFLOW virtual slot
    #   3. Otherwise score each surviving candidate on 4-level soft priority
    #   4. Pick the candidate with the lowest (best) score tuple
    for course in courses_sorted:
        if course in assignment:
            continue  # already pinned by user

        # Hard constraint A: no two conflicting courses in the same slot.
        # Find which slots are already used by this course's neighbours.
        neighbours = adj.get(course, {})
        used_slots = {assignment[n] for n in neighbours if n in assignment}

        # Conflict-free candidates: every slot NOT used by a neighbour
        candidates = [si for si in range(max_slot_idx + 1) if si not in used_slots]

        # Hard constraint B: no two courses from the same (program, term)
        # bucket on the same day.  Remove candidates whose day already
        # has a bucket-mate from ANY of this course's buckets.
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
            # ── OVERFLOW: no feasible slot exists ──
            # All real slots are blocked by hard constraints (student clash
            # or bucket-mate same day).  Create a virtual OVERFLOW slot so
            # the course isn't silently dropped; the QA report will flag it
            # and the UI shows a red overflow row.
            max_slot_idx += 1
            chosen = max_slot_idx
            slot_by_index[chosen] = {
                "index": chosen,
                "day": "OVERFLOW",
                "period": f"Extra-{chosen}",
            }
            assignment[course] = chosen
            slot_load[chosen] += 1
            course_assigned_day[course] = "OVERFLOW"
            # Maintain student tracking structures for consistency
            if enrolled_sets and course in enrolled_sets:
                for sid in enrolled_sets[course]:
                    student_day_count[sid]["OVERFLOW"] += 1
                    student_day_courses[sid]["OVERFLOW"].append(course)
            # Maintain bucket-day tracking (use my_buckets, already computed above)
            for bk in my_buckets:
                bucket_day_courses[bk]["OVERFLOW"].add(course)
            continue

        if enrolled_sets and course in enrolled_sets:
            # ── Soft-constraint scoring ──
            # Evaluate ALL conflict-free candidates and pick the best by a
            # four-level priority tuple (lower is better):
            #   (overload, credit_pair, spacing, slot_load)
            # Python tuple comparison ensures level 1 always trumps level 2, etc.
            course_students = enrolled_sets[course]
            best_slot = candidates[0]
            best_score = (float("inf"), float("inf"), float("inf"), float("inf"))

            for si in candidates:
                day = slot_by_index[si]["day"]

                # Level 1 — Day-overload: count students who would exceed the
                # per-day cap if this course is placed on this day.
                penalty = 0
                for sid in course_students:
                    if student_day_count[sid][day] >= max_per_day:
                        penalty += 1

                # Level 1.5 — Credit-pair penalty: when multi-exam days are
                # unavoidable, prefer lighter pairings (4+2 over 4+3 over 4+4).
                credit_penalty = 0
                if _cm:
                    this_cr = _cm.get(course, _CREDIT_DEFAULT)
                    for sid in course_students:
                        existing = student_day_courses[sid][day]
                        if existing:
                            day_credits = [_cm.get(ec, _CREDIT_DEFAULT) for ec in existing] + [this_cr]
                            credit_penalty += _credit_pair_penalty(day_credits)

                # Level 2 — Spacing within programme-plan buckets:
                # penalise placing bucket-mates on adjacent days so students
                # get breathing room.  Weights: 1-day gap=100, 2-day=30, 3-day=10.
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

                # Level 3 — Load balance: prefer slots with fewer courses
                score = (penalty, credit_penalty, spacing_penalty, slot_load[si])
                if score < best_score:
                    best_score = score
                    best_slot = si

            chosen = best_slot
        else:
            # No enrolled data available — fall back to least-loaded slot
            chosen = min(candidates, key=lambda si: slot_load[si])

        assignment[course] = chosen
        slot_load[chosen] += 1
        chosen_day = slot_by_index[chosen]["day"]
        course_assigned_day[course] = chosen_day

        # Update student day counts + courses-per-day tracking
        if enrolled_sets and course in enrolled_sets:
            for sid in enrolled_sets[course]:
                student_day_count[sid][chosen_day] += 1
                student_day_courses[sid][chosen_day].append(course)

        # Update bucket-day tracking
        for bk in my_buckets:
            bucket_day_courses[bk][chosen_day].add(course)

    # ── Build result list sorted by slot index ──
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


# ── 5. QA report ────────────────────────────────────────────────


def _build_qa(
    enrolled_sets: dict[str, set[int]],
    schedule_entries: list[dict],
    max_per_day: int = 2,
    plan_term_buckets: dict[tuple[str, int], set[str]] | None = None,
    credit_map: dict[str, int] | None = None,
) -> dict:
    """Validate the schedule and produce a QA report.

    Checks hard-constraint violations (which should only happen with
    user-pinned overrides) and computes soft-constraint metrics:

    Hard constraints:
      - Same-slot conflicts:    two courses sharing students in the same slot
      - Bucket day violations:  two bucket-mates placed on the same day

    Soft-constraint metrics:
      - Day-overload count:     students exceeding max_per_day
      - Credit load metrics:    max credit-load per day, heavy-day student count

    Also collects per-student detail records for KPI drilldown in the UI.
    """
    # ── Preparation ──
    all_students: set[int] = set()
    for sids in enrolled_sets.values():
        all_students |= sids

    # Lookup maps: course → its assigned slot index / day
    course_slot: dict[str, int] = {e["course_code"]: e["slot_index"] for e in schedule_entries}
    course_day: dict[str, str] = {e["course_code"]: e["day"] for e in schedule_entries}

    slots_used = len({e["slot_index"] for e in schedule_entries})

    # Invert enrolled_sets: student_id → [course_codes] so we can iterate
    # per-student and check their personal schedule for violations.
    student_courses: dict[int, list[str]] = defaultdict(list)
    for cc, sids in enrolled_sets.items():
        for sid in sids:
            student_courses[sid].append(cc)

    _cm = credit_map or {}

    # ── Accumulators ──
    same_slot_conflicts: list[dict] = []       # hard-constraint violations
    max_exams_per_day: int = 0                  # worst-case day load globally
    students_over_limit_per_day: int = 0        # students exceeding soft cap
    max_credit_load_per_day: int = 0            # worst-case credit sum on a day
    heavy_day_students: int = 0                 # students with heavy credit pair

    # Detail records for KPI drilldown panel in the UI
    overload_details: list[dict] = []   # per-student, per-day overload records
    heavy_day_details: list[dict] = []  # per-student, per-day heavy-credit records

    # ── Per-student validation ──
    for sid, courses in student_courses.items():
        # Group this student's courses by slot and by day
        slot_groups: dict[int, list[str]] = defaultdict(list)
        day_groups: dict[str, list[str]] = defaultdict(list)
        for cc in courses:
            si = course_slot.get(cc)
            if si is not None:
                slot_groups[si].append(cc)
            day = course_day.get(cc)
            if day is not None:
                day_groups[day].append(cc)

        # If ≥2 courses land in the same slot → conflict (pinned override)
        for si, ccs in slot_groups.items():
            if len(ccs) >= 2:
                same_slot_conflicts.append(
                    {
                        "student_id": sid,
                        "slot_index": si,
                        "courses": ccs,
                    }
                )

        # ── Soft-constraint metrics per day ──
        has_overload = False   # does this student exceed the per-day cap?
        has_heavy_day = False  # does this student have a heavy credit pairing?
        for _day, ccs in day_groups.items():
            if _day == "OVERFLOW":
                continue  # OVERFLOW is a virtual day — skip for metrics

            day_count = len(ccs)
            max_exams_per_day = max(max_exams_per_day, day_count)

            # Day-overload check: student exceeds the soft cap
            if day_count > max_per_day:
                has_overload = True
                overload_details.append({
                    "student_id": sid,
                    "day": _day,
                    "count": day_count,
                    "courses": [
                        {"code": c, "credits": _cm.get(c, _CREDIT_DEFAULT) if _cm else None}
                        for c in sorted(ccs)
                    ],
                })

            # Credit-load check: evaluate the top-2 heaviest exams on this day.
            # Only relevant when credit_map is available AND student has ≥2 exams.
            if _cm and len(ccs) >= 2:
                day_credits = [_cm.get(c, _CREDIT_DEFAULT) for c in ccs]
                total_credits = sum(day_credits)
                max_credit_load_per_day = max(max_credit_load_per_day, total_credits)
                pair_penalty = _credit_pair_penalty(day_credits)
                # "Heavy day" threshold: penalty ≥ 30 catches (4,4)→100 and
                # (4,3)→30, but NOT mild combos like (3,3)→5 or (3,2)→5.
                if pair_penalty >= 30:
                    has_heavy_day = True
                    heavy_day_details.append({
                        "student_id": sid,
                        "day": _day,
                        "penalty": pair_penalty,
                        "total_credits": total_credits,
                        "courses": [
                            {"code": c, "credits": _cm.get(c, _CREDIT_DEFAULT)}
                            for c in sorted(ccs)
                        ],
                    })

        if has_overload:
            students_over_limit_per_day += 1
        if has_heavy_day:
            heavy_day_students += 1

    # ── Bucket (programme-plan term) day-rule verification ──
    # Hard constraint B says no two courses from the same (program, term)
    # bucket should share a day.  This can only be violated when the user
    # pins courses that override the scheduler's hard-constraint logic.
    bucket_day_violations: list[dict] = []
    bucket_count = 0
    if plan_term_buckets:
        bucket_count = len(plan_term_buckets)
        for (program, term), bucket_courses in sorted(plan_term_buckets.items()):
            # Group this bucket's courses by their assigned day
            day_groups_b: dict[str, list[str]] = defaultdict(list)
            for cc in bucket_courses:
                day = course_day.get(cc)
                if day is not None and day != "OVERFLOW":
                    day_groups_b[day].append(cc)
            # Any day with ≥2 bucket-mates is a violation
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
        "max_credit_load_per_day": max_credit_load_per_day,
        "heavy_day_students": heavy_day_students,
        "overload_details": overload_details,
        "heavy_day_details": heavy_day_details,
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

    # 1c. Build credit map for credit-weighted scoring
    credit_map = build_credit_map(course_list)

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

    # 6. Schedule (with day-spread + bucket + credit-pair constraints)
    schedule_entries = schedule(
        course_list,
        adj,
        slots,
        enrolled_sets=enrolled_sets,
        max_per_day=max_per_day,
        plan_term_buckets=ptb,
        course_buckets=cb,
        pinned=pinned,
        credit_map=credit_map,
    )

    # 7. QA report — validate hard constraints and compute quality metrics
    qa = _build_qa(
        enrolled_sets,
        schedule_entries,
        max_per_day=max_per_day,
        plan_term_buckets=ptb,
        credit_map=credit_map,
    )

    # Bucket summary for the result (frontend renders bucket info cards)
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

    # ── Assemble result dict ──
    # This dict is: (a) returned to the frontend as JSON, (b) persisted
    # in ExamTimetableRun.result_json for later viewing/export.
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
        "credit_map": credit_map,
    }

    # Persist
    run = ExamTimetableRun.objects.create(
        label=label,
        result_json=json.dumps(result, ensure_ascii=False),
    )
    result["run_id"] = run.id

    return result


# ── 7. Excel export ──────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parents[2]
RUNTIME_DIR = BASE_DIR / "runtime"


def export_exam_timetable_xlsx(run_id: int) -> Path:
    """Export a saved ExamTimetableRun to a styled multi-sheet .xlsx workbook.

    Sheets:
        Schedule  – day × period grid (mirrors the on-screen table)
        Courses   – flat list with course code, enrolled count, day, period
        QA Summary – key metrics + any conflict / bucket-day warnings

    Returns the Path to the written file (in the runtime/ directory).
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    run = ExamTimetableRun.objects.get(id=run_id)
    data = json.loads(run.result_json)

    schedule = data.get("schedule", [])  # list of {course_code, slot_index, day, period}
    slots = data.get("slots", [])        # list of {index, day, period}
    qa = data.get("qa", {})              # QA metrics dict from _build_qa()

    # ── Styling constants ──
    header_font = Font(bold=True, size=11)
    header_fill = PatternFill("solid", fgColor="0A8E6E")  # teal
    header_font_white = Font(bold=True, size=11, color="FFFFFF")
    thin_border = Border(
        left=Side(style="thin", color="CCCCCC"),
        right=Side(style="thin", color="CCCCCC"),
        top=Side(style="thin", color="CCCCCC"),
        bottom=Side(style="thin", color="CCCCCC"),
    )
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left_align = Alignment(horizontal="left", vertical="center", wrap_text=True)
    warn_fill = PatternFill("solid", fgColor="FFF3CD")  # light yellow
    danger_fill = PatternFill("solid", fgColor="F8D7DA")  # light red

    def style_header_row(ws, col_count: int) -> None:
        """Apply teal background + white bold font to the first row."""
        for col in range(1, col_count + 1):
            cell = ws.cell(row=1, column=col)
            cell.font = header_font_white
            cell.fill = header_fill
            cell.alignment = center
            cell.border = thin_border

    # ── Extract ordered days and periods from slots ──
    day_order: list[str] = []
    period_order: list[str] = []
    day_set: set[str] = set()
    period_set: set[str] = set()
    for s in slots:
        if s["day"] not in day_set:
            day_set.add(s["day"])
            day_order.append(s["day"])
        if s["period"] not in period_set:
            period_set.add(s["period"])
            period_order.append(s["period"])

    # Build grid lookup: grid[day][period] = [course_codes]
    grid: dict[str, dict[str, list[str]]] = {}
    for e in schedule:
        if e["day"] == "OVERFLOW":
            continue
        grid.setdefault(e["day"], {}).setdefault(e["period"], []).append(e["course_code"])

    wb = Workbook()

    # ────────────────────────────────────────────────────────────
    # Sheet 1: Schedule (day × period grid)
    # ────────────────────────────────────────────────────────────
    ws1 = wb.active
    ws1.title = "Schedule"

    # Header row: "Day \ Period", then each period
    ws1.append(["Day \\ Period"] + period_order)
    style_header_row(ws1, 1 + len(period_order))

    # One row per day
    for day in day_order:
        row: list[str] = [day]
        for period in period_order:
            courses = grid.get(day, {}).get(period, [])
            row.append(", ".join(sorted(courses)) if courses else "—")
        ws1.append(row)

    # Style body cells
    for r in range(2, ws1.max_row + 1):
        for c in range(1, ws1.max_column + 1):
            cell = ws1.cell(row=r, column=c)
            cell.border = thin_border
            cell.alignment = center if c > 1 else left_align
            if c == 1:
                cell.font = header_font  # bold day name

    # Column widths
    ws1.column_dimensions["A"].width = 14
    for i, _p in enumerate(period_order, start=2):
        ws1.column_dimensions[get_column_letter(i)].width = 22

    # ────────────────────────────────────────────────────────────
    # Sheet 2: Courses (flat list)
    # ────────────────────────────────────────────────────────────
    ws2 = wb.create_sheet("Courses")
    _credit_map = data.get("credit_map", {})
    ws2.append(["Course Code", "Credits", "Day", "Period", "Slot Index"])
    style_header_row(ws2, 5)

    sorted_schedule = sorted(schedule, key=lambda e: (e.get("slot_index", 999), e["course_code"]))
    for e in sorted_schedule:
        cr = _credit_map.get(e["course_code"], "")
        ws2.append([e["course_code"], cr, e["day"], e["period"], e.get("slot_index", "")])

    for r in range(2, ws2.max_row + 1):
        for c in range(1, 6):
            cell = ws2.cell(row=r, column=c)
            cell.border = thin_border
            cell.alignment = center

    ws2.column_dimensions["A"].width = 16
    ws2.column_dimensions["B"].width = 10
    ws2.column_dimensions["C"].width = 14
    ws2.column_dimensions["D"].width = 18
    ws2.column_dimensions["E"].width = 12

    # ────────────────────────────────────────────────────────────
    # Sheet 3: QA Summary
    # ────────────────────────────────────────────────────────────
    ws3 = wb.create_sheet("QA Summary")

    # Key metrics as label-value pairs
    metrics = [
        ("Label", run.label),
        ("Total Courses", qa.get("total_courses", data.get("courses_count", 0))),
        ("Total Students", qa.get("total_students", data.get("students_count", 0))),
        ("Slots Used", qa.get("slots_used", 0)),
        ("Max Exams/Day/Student", qa.get("max_exams_per_day_per_student", 0)),
        ("Max Per Day Cap", qa.get("max_per_day", 2)),
        ("Students Over Limit", qa.get("students_over_limit_per_day", 0)),
        ("Max Credit Load/Day", qa.get("max_credit_load_per_day", 0)),
        ("Heavy Day Students", qa.get("heavy_day_students", 0)),
        ("Same-Slot Conflicts", qa.get("conflict_count", 0)),
        ("Programme Buckets", qa.get("bucket_count", 0)),
        ("Bucket Day Violations", qa.get("bucket_day_violations_count", 0)),
    ]

    ws3.append(["Metric", "Value"])
    style_header_row(ws3, 2)

    for label, value in metrics:
        ws3.append([label, value])

    # Highlight warning rows
    for r in range(2, ws3.max_row + 1):
        for c in range(1, 3):
            cell = ws3.cell(row=r, column=c)
            cell.border = thin_border
            cell.alignment = left_align
        metric_name = ws3.cell(row=r, column=1).value
        metric_val = ws3.cell(row=r, column=2).value
        # Colour warning rows yellow/red
        if metric_name == "Same-Slot Conflicts" and metric_val and int(metric_val) > 0:
            ws3.cell(row=r, column=1).fill = danger_fill
            ws3.cell(row=r, column=2).fill = danger_fill
        elif metric_name in ("Students Over Limit", "Bucket Day Violations", "Heavy Day Students"):
            if metric_val and int(metric_val) > 0:
                ws3.cell(row=r, column=1).fill = warn_fill
                ws3.cell(row=r, column=2).fill = warn_fill

    # Append same-slot conflict details (if any)
    same_slot = qa.get("same_slot_conflicts", [])
    if same_slot:
        ws3.append([])
        ws3.append(["Same-Slot Conflict Details"])
        ws3.cell(row=ws3.max_row, column=1).font = header_font
        ws3.append(["Student ID", "Slot Index", "Courses"])
        r = ws3.max_row
        for c in range(1, 4):
            cell = ws3.cell(row=r, column=c)
            cell.font = header_font
            cell.fill = PatternFill("solid", fgColor="EEEEEE")
            cell.border = thin_border
        for conflict in same_slot:
            ws3.append([
                conflict.get("student_id", ""),
                conflict.get("slot_index", ""),
                ", ".join(conflict.get("courses", [])),
            ])

    # Append bucket day violation details (if any)
    bucket_viols = qa.get("bucket_day_violations", [])
    if bucket_viols:
        ws3.append([])
        ws3.append(["Bucket Day Violation Details"])
        ws3.cell(row=ws3.max_row, column=1).font = header_font
        ws3.append(["Programme", "Term", "Day", "Courses"])
        r = ws3.max_row
        for c in range(1, 5):
            cell = ws3.cell(row=r, column=c)
            cell.font = header_font
            cell.fill = PatternFill("solid", fgColor="EEEEEE")
            cell.border = thin_border
        for v in bucket_viols:
            ws3.append([
                v.get("program", ""),
                v.get("programme_term", ""),
                v.get("day", ""),
                ", ".join(v.get("courses", [])),
            ])

    ws3.column_dimensions["A"].width = 26
    ws3.column_dimensions["B"].width = 16
    ws3.column_dimensions["C"].width = 18
    ws3.column_dimensions["D"].width = 30

    # ── Write to disk ──
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    out = RUNTIME_DIR / f"exam_timetable_{run_id}.xlsx"
    wb.save(str(out))
    return out

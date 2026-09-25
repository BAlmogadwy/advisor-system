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
import logging
import random

# ── Invigilator calculation rules ──────────────────────────────
#
# Department courses (CS, IS, COE, CYB, AI, DS) need invigilators FROM
# our department for every exam-room: 1 invigilator if the room holds
# fewer than 30 students, 2 invigilators if 30 or more.
#
# External / general-requirements courses (GS, EDCT, GSE, ENV, MATH,
# STAT, PHYS) only need an invigilator from our department when the
# room holds MORE than 30 students (1 invigilator), otherwise 0
# (the providing college supplies its own staff).
import re as _re
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.models import (
    Course,
    ExamTimetableRun,
    ProgrammeRequirement,
    Room,
    Student,
)
from core.services.course_identity import display_course_label, planner_course_key
from core.services.exam_input_fingerprint import fingerprint_exam_inputs
from core.services.exam_operations_snapshot import build_exam_operations_snapshot
from core.services.exam_progress import Counter
from core.services.exam_progress import current as current_progress
from core.services.exam_review import build_exam_review
from core.services.exam_room_allocation import (
    RoomAllocationContext,
    RoomAllocationTimeout,
    allocate_period,
    normalized_rooms,
)
from core.services.exam_run_schema import (
    STATUS_DERIVATION_VERSION,
    compute_enrolment_snapshot,
    derive_building_footprint,
    derive_multi_sitting_details,
    derive_status_surface,
    load_normalised_run,
    stamp_schema_version,
)
from core.services.exam_sections import (
    EXAM_ENROLLMENT_SOURCE,
    annotate_exam_room_groups,
    exam_section_part,
    exam_timetable_links,
    resolve_exam_section_enrollment,
    summarize_exam_section_mapping,
)
from core.services.student_sections import (
    OTHER_BRANCH_SECTION_COHORT,
    _section_course_key,
    section_gender,
)

logger = logging.getLogger(__name__)
_DEPARTMENT_PREFIXES: set[str] = {"CS", "IS", "COE", "CYB", "AI", "DS"}
_EXTERNAL_PREFIXES: set[str] = {"GS", "EDCT", "GSE", "ENV", "MATH", "STAT", "PHYS"}

_DEPT_LARGE_THRESHOLD = 30  # >= triggers a second invigilator (department)
_EXT_LARGE_THRESHOLD = 30  # > triggers one invigilator from us (external)


def _course_prefix(course_code: str) -> str:
    """Extract the alphabetic prefix from a course code (e.g. 'CS112' → 'CS')."""
    if not course_code:
        return ""
    m = _re.match(r"^[A-Za-z]+", str(course_code))
    return m.group(0).upper() if m else ""


def _invigilators_needed(course_code: str, students_in_room: int) -> int:
    """Return how many invigilators FROM OUR DEPARTMENT this room needs.

    Department courses:  >=30 students → 2, otherwise 1
    External courses:    > 30 students → 1, otherwise 0
    Unknown prefix is treated as department (safer side).
    """
    prefix = _course_prefix(course_code)
    if prefix in _EXTERNAL_PREFIXES:
        return 1 if students_in_room > _EXT_LARGE_THRESHOLD else 0
    # Department or unknown
    return 2 if students_in_room >= _DEPT_LARGE_THRESHOLD else 1


# ── 0. Credit helpers ───────────────────────────────────────────

_CREDIT_DEFAULT = 3  # fallback for NULL / 0 / missing credit_hours

# Penalty ladder for the top-2 heaviest exams on a single day.
# Key = (max_credits, min_credits); value = penalty weight.
_CREDIT_PAIR_WEIGHTS: dict[tuple[int, int], int] = {
    (4, 4): 100,  # worst – two heavy courses
    (4, 3): 30,  # acceptable
    (4, 2): 0,  # ideal pairing
}
_CREDIT_PAIR_FALLBACK = 5  # other combos (3+3, 3+2, 2+2, …)


def build_credit_map(course_codes: list[str] | set[str]) -> dict[str, int]:
    """Return {course_code: credit_hours} for the given courses.

    Missing / NULL / zero credit_hours default to ``_CREDIT_DEFAULT``
    so the penalty formula degrades gracefully.
    """
    display_to_source = {str(cc): _source_code_for_display(str(cc)) for cc in course_codes}
    rows = Course.objects.filter(
        course_code__in=sorted(set(display_to_source.values())),
    ).values_list("course_code", "credit_hours")
    source_cm: dict[str, int] = {cc: (ch if ch and ch > 0 else _CREDIT_DEFAULT) for cc, ch in rows}
    cm: dict[str, int] = {}
    for display, source in display_to_source.items():
        cm[display] = source_cm.get(source, _CREDIT_DEFAULT)
    return cm


def _source_code_for_display(display_code: str, explicit_source: object = None) -> str:
    source = str(explicit_source or "").strip()
    if source:
        return source
    if display_code.endswith(")") and " (" in display_code:
        return display_code.rsplit(" (", 1)[0]
    return display_code


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
    """Return {course_code: {student_id, …}} for current-term enrolments.

    Uses only actual ``scraper_timetable`` student-section links.
    Same-code courses with different programme names are split into
    display keys such as "CS112 (1)" and "CS112 (2)".

    Optional filters narrow the student population:
        programs – only include students whose program is in this list
        sections – only include students whose section is in this list
    When a filter is None or empty, it is ignored (all values pass).
    """
    enrolled, _meta = build_enrolled_sets_with_meta(programs=programs, sections=sections)
    return enrolled


def build_enrolled_sets_with_meta(
    programs: list[str] | None = None,
    sections: list[str] | None = None,
) -> tuple[dict[str, set[int]], dict[str, dict]]:
    """Return actual scraped course memberships and canonical course metadata.

    Programme requirements supply names/identities, never student membership.
    Academic 'studying', planned, manual and other fallback records cannot add
    students or courses to an exam timetable.
    """
    links, year, term = exam_timetable_links()
    profiles = Student.objects.all()
    if programs:
        profiles = profiles.filter(program__in=programs)
    if sections:
        profiles = profiles.filter(section__in=sections)
    student_programs = dict(profiles.values_list("student_id", "program"))
    scoped = bool(programs or sections)
    rows = []
    # The scope is applied here, not as ``student_id IN (...)``: a literal list
    # of thousands of IDs costs SQLite ~0.9 s against ~0.04 s for reading the
    # term's links and skipping the rest. The rows kept are identical.
    for row in links.values(
        "student_id",
        "term_section__course_key",
        "term_section__course_code",
        "term_section__course_number",
        "term_section__course_name",
        "term_section__section",
    ):
        if scoped and row["student_id"] not in student_programs:
            continue
        if section_gender(row["term_section__section"]) == OTHER_BRANCH_SECTION_COHORT:
            continue
        code = _section_course_key(
            SimpleNamespace(
                **{
                    key: row[f"term_section__{key}"]
                    for key in ("course_key", "course_code", "course_number")
                }
            )
        )
        if code:
            rows.append(
                (
                    code,
                    row["term_section__course_name"],
                    row["student_id"],
                    student_programs.get(row["student_id"], ""),
                )
            )
    source_codes = {str(code) for code, _desc, _sid, _program in rows}
    if not source_codes:
        return {}, {}
    catalogue_names = dict(
        Course.objects.filter(course_code__in=source_codes).values_list(
            "course_code", "description"
        )
    )

    pr_rows = list(
        ProgrammeRequirement.objects.filter(course_code__in=source_codes).values_list(
            "program",
            "course_code",
            "course_name",
            "programme_term",
        )
    )
    pr_name_by_program_code = {
        (str(program), str(code)): str(name or "").strip() for program, code, name, _term in pr_rows
    }
    online_requirements = ProgrammeRequirement.objects.filter(
        course_code__in=source_codes, is_online=True
    )
    if programs:
        online_requirements = online_requirements.filter(program__in=programs)
    online_identities = {
        planner_course_key(code, name)
        for code, name in online_requirements.values_list("course_code", "course_name")
    }

    enrolled_by_identity: dict[tuple[str, str], set[int]] = defaultdict(set)
    identity_name: dict[tuple[str, str], str] = {}
    identity_programs: dict[tuple[str, str], set[str]] = defaultdict(set)
    for source, course_desc, student_id, program in rows:
        source_code = str(source)
        name = (
            pr_name_by_program_code.get((str(program), source_code))
            or str(catalogue_names.get(source_code) or "").strip()
            or str(course_desc or "").strip()
        )
        identity = planner_course_key(source_code, name)
        enrolled_by_identity[(source_code, identity)].add(int(student_id))
        identity_name.setdefault((source_code, identity), name)
        if program:
            identity_programs[(source_code, identity)].add(str(program))

    identity_term_rank: dict[tuple[str, str], int] = {}
    for _program, source, name, term in pr_rows:
        source_code = str(source)
        identity = planner_course_key(source_code, name)
        key = (source_code, identity)
        rank = int(term or 999)
        identity_term_rank[key] = min(identity_term_rank.get(key, rank), rank)
        identity_name.setdefault(key, str(name or "").strip())

    identities_by_source: dict[str, list[str]] = defaultdict(list)
    for source, identity in enrolled_by_identity:
        identities_by_source[source].append(identity)
    for source in identities_by_source:
        identities_by_source[source] = sorted(
            set(identities_by_source[source]),
            key=lambda identity: (
                identity_term_rank.get((source, identity), 999),
                identity,
            ),
        )

    display_by_identity: dict[tuple[str, str], str] = {}
    for source, identities in identities_by_source.items():
        if len(identities) == 1:
            display_by_identity[(source, identities[0])] = source
        else:
            for idx, identity in enumerate(identities, start=1):
                display_by_identity[(source, identity)] = f"{source} ({idx})"

    enrolled: dict[str, set[int]] = {}
    meta: dict[str, dict] = {}
    for (source, identity), student_ids in enrolled_by_identity.items():
        display = display_by_identity[(source, identity)]
        enrolled[display] = set(student_ids)
        meta[display] = {
            "source_course_code": source,
            "course_name": identity_name.get((source, identity), ""),
            "course_identity": identity,
            "course_label": display_course_label(source, identity_name.get((source, identity))),
            "programs": sorted(identity_programs[(source, identity)]),
            "is_online": identity in online_identities or source.startswith(("GS", "GSE")),
            "enrolled_count": len(student_ids),
            "enrollment_source": EXAM_ENROLLMENT_SOURCE,
            "academic_year": year,
            "term": term,
        }
    return dict(enrolled), meta


class ExamCoursesUnavailable(ValueError):
    """A selection no longer has actual scraped timetable enrollments."""

    def __init__(self, courses: list[str]):
        self.unavailable_courses = courses
        super().__init__(
            "No actual scraped-timetable enrollments were found for: "
            + ", ".join(courses)
            + ". Load Courses again and select the current courses."
        )


def select_exam_course_enrollments(
    entries: list[dict],
    enrolled: dict[str, set[int]],
    metadata: dict[str, dict],
) -> tuple[dict[str, set[int]], dict[str, dict]]:
    """Resolve saved/selected rows by the shared planner identity, never by suffix.

    Display numbers can change when the selected population changes. Even a
    single retained variant must match its name, not every enrollment with the
    registrar code. Identity follows the shared planner's code-and-name rule.
    """
    by_identity = {meta["course_identity"]: code for code, meta in metadata.items()}
    selected: dict[str, set[int]] = {}
    selected_meta: dict[str, dict] = {}
    seen_identities: set[str] = set()
    for entry in entries:
        display = str(entry.get("course_code") or "").strip()
        if not display:
            raise ValueError("A selected course is missing its course code.")
        source = _source_code_for_display(display, entry.get("source_course_code"))
        name = str(entry.get("course_name") or "").strip()
        named_identity = planner_course_key(source, name)
        identity = str(entry.get("course_identity") or named_identity)
        if identity != named_identity:
            raise ValueError(f"Course identity does not match the name for {display}.")
        match = by_identity.get(identity)
        if match is None:
            raise ExamCoursesUnavailable([display])
        meta = dict(metadata[match])
        if display in selected or meta["course_identity"] in seen_identities:
            raise ValueError(f"Course {display} was selected more than once.")
        seen_identities.add(meta["course_identity"])
        selected[display] = set(enrolled[match])
        selected_meta[display] = meta
    return selected, selected_meta


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
    for (a, b), cnt in sorted(edge_counts.items()):
        conflicts.append({"course_a": a, "course_b": b, "shared": cnt})
        adj[a][b] = cnt
        adj[b][a] = cnt

    return conflicts, dict(adj)


# ── 3. Programme-plan term buckets ─────────────────────────────


def build_plan_term_buckets(
    running_courses: set[str],
    course_meta: dict[str, dict] | None = None,
    programs: list[str] | None = None,
) -> tuple[dict[tuple[str, int], set[str]], dict[str, list[tuple[str, int]]]]:
    """Map running courses to (program, programme_term) buckets.

    Returns:
        buckets       – {(program, programme_term): {course_codes}}
        course_buckets – {course_code: [(program, term), …]} reverse index
    """
    meta = course_meta or {}
    source_to_display: dict[str, list[str]] = defaultdict(list)
    identity_by_display: dict[str, str] = {}
    for display in running_courses:
        display_code = str(display)
        m = meta.get(display_code, {})
        source = _source_code_for_display(display_code, m.get("source_course_code"))
        identity = str(m.get("course_identity") or source)
        source_to_display[source].append(display_code)
        identity_by_display[display_code] = identity

    requirements = ProgrammeRequirement.objects.filter(
        course_code__in=set(source_to_display),
        programme_term__isnull=False,
    )
    if programs:
        requirements = requirements.filter(program__in=programs)
    rows = requirements.values_list("program", "course_code", "course_name", "programme_term")

    # Forward index: (program, term) → {course_codes}
    buckets: dict[tuple[str, int], set[str]] = defaultdict(set)
    # Reverse index: course_code → [(program, term), …]  (a course can appear
    # in multiple programmes, e.g. service courses shared across AI & DS)
    course_buckets: dict[str, list[tuple[str, int]]] = defaultdict(list)

    for program, course_code, course_name, programme_term in rows:
        key = (program, int(programme_term or 0))
        row_identity = planner_course_key(course_code, course_name)
        displays = [
            display
            for display in source_to_display.get(str(course_code), [])
            if identity_by_display.get(display, display) == row_identity
        ]
        if not displays:
            displays = [
                display
                for display in source_to_display.get(str(course_code), [])
                if identity_by_display.get(display, display) == str(course_code)
            ]
        for display in displays:
            buckets[key].add(display)
            course_buckets[display].append(key)

    return dict(buckets), dict(course_buckets)


def check_bucket_feasibility(
    buckets: dict[tuple[str, int], set[str]],
    num_days: int,
    pinned: list[dict[str, str]] | None = None,
) -> list[dict]:
    """Return buckets that need more days after honoring deliberate pin overrides.

    Each violation: {program, programme_term, bucket_size, num_days, courses}
    Empty list means all buckets are feasible.
    """
    violations: list[dict] = []
    pinned_days = {pin["course_code"]: pin["day"] for pin in pinned or []}
    for (program, term), courses in sorted(buckets.items()):
        fixed_courses = courses & pinned_days.keys()
        required_days = len(courses - fixed_courses) + len(
            {pinned_days[course] for course in fixed_courses}
        )
        if required_days > num_days:
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


def validate_exam_pins(
    pinned: list[dict] | None,
    courses: list[str],
    slots: list[dict],
    *,
    schedule_entries: list[dict] | None = None,
) -> list[dict[str, str]]:
    """Resolve exact display codes and reject pins that cannot be honored.

    Display codes distinguish named variants of the same registrar code. Never
    collapse them to the source code or silently ignore an invalid fixed slot.
    When saving a visible schedule, its placements must already match the pins.
    """
    if pinned is None:
        return []
    if not isinstance(pinned, list):
        raise ValueError("Pinned exams must be a list of course, day and period entries.")
    selected = set(courses)
    available_slots = {(slot["day"], slot["period"]) for slot in slots}
    placements = (
        {entry["course_code"]: (entry["day"], entry["period"]) for entry in schedule_entries}
        if schedule_entries is not None
        else None
    )
    identities = {
        entry["course_code"]: str(entry.get("course_identity") or "")
        for entry in schedule_entries or []
    }
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for pin in pinned:
        if not isinstance(pin, dict) or any(
            not isinstance(pin.get(field), str) or not pin[field].strip()
            for field in ("course_code", "day", "period")
        ):
            raise ValueError("Each pinned exam must specify a course, day and period.")
        course, day, period = (pin[field].strip() for field in ("course_code", "day", "period"))
        if course not in selected:
            raise ValueError(f"Pinned course {course} is not selected for this timetable.")
        if (
            pin.get("course_identity")
            and identities.get(course)
            and pin["course_identity"] != identities[course]
        ):
            raise ValueError(f"Pinned course {course} has an inconsistent course identity.")
        if course in seen:
            raise ValueError(f"Course {course} is pinned more than once.")
        if (day, period) not in available_slots:
            raise ValueError(f"Pinned course {course} uses a day or period outside this timetable.")
        if placements is not None and placements.get(course) != (day, period):
            raise ValueError(
                f"Pinned course {course} does not match its visible schedule placement."
            )
        seen.add(course)
        normalized.append({"course_code": course, "day": day, "period": period})
    return normalized


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
    preferred_slots: dict[str, int] | None = None,
    seed: int | None = None,
    on_placed: Counter | None = None,
) -> list[dict]:
    """
    Greedy graph-coloring with day-spread soft constraint.

    ``on_placed(done, total)`` is told as each course is taken up, for a job
    that reports its progress; nothing else about the schedule depends on it.

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

    Randomised tie-breaking:
      When ``seed`` is provided, courses with the same constraint degree
      are shuffled randomly before scheduling.  This produces a different
      (but equally valid) timetable on each run, letting the user pick
      the best variant.  When ``seed`` is None the order is deterministic
      (alphabetical within each tier).

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
        seed               – RNG seed for tie-breaking; None = deterministic order

    Returns:
        list of {course_code, slot_index, day, period}
    """
    pinned = validate_exam_pins(pinned, courses, slots)

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
    _cb = course_buckets or {}  # course_code → [(program, term), …]
    _cm = credit_map or {}  # course_code → credit_hours
    _pref = preferred_slots or {}  # course_code → preferred slot_index

    def _constraint_degree(c: str) -> int:
        """Heuristic: courses with more conflicts + more bucket-mates are harder
        to place, so we schedule them first (most-constrained-first ordering)."""
        adj_deg = len(adj.get(c, {}))
        bucket_deg = sum(len(_ptb.get(bk, set())) for bk in _cb.get(c, []))
        return adj_deg + bucket_deg

    # Sort most-constrained first.  When seed is provided, we bucket
    # courses into WIDE degree bands (each band is ~20% of the degree
    # range) and shuffle inside every band.  Banding is wider than
    # strict equal-degree tiers so courses with close-but-different
    # degrees also mix together — this gives each run noticeably more
    # variety without letting low-degree courses leapfrog high-degree
    # ones entirely.
    if seed is not None:
        rng = random.Random(seed)
        degrees = [(_constraint_degree(c), c) for c in courses]
        if degrees:
            max_deg = max(d for d, _ in degrees)
            # Band width: ~10% of the max degree, floor of 1.
            band_width = max(1, max_deg // 10)
            # Bucket by band index (descending bands placed first)
            band_map: dict[int, list[str]] = defaultdict(list)
            for d, c in degrees:
                band_map[d // band_width].append(c)
            courses_sorted = []
            for band in sorted(band_map.keys(), reverse=True):
                members = band_map[band]
                rng.shuffle(members)
                courses_sorted.extend(members)
        else:
            courses_sorted = []
    else:
        rng = None
        # Deterministic: alphabetical within each tier (Python sort is stable)
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

    student_day_courses: dict[int, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

    slot_load: dict[int, int] = defaultdict(int)

    # Per-day course count — used as the primary load-balancing tiebreaker so
    # courses without other constraints (degree-0 / no bucket) get spread
    # across days instead of clustering on whichever slot index is lowest.
    day_load: dict[str, int] = defaultdict(int)

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
            si = dp_to_slot[(p_day, p_period)]
            assignment[cc] = si
            slot_load[si] += 1
            day_load[p_day] += 1
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
    for done, course in enumerate(courses_sorted):
        if on_placed is not None:
            on_placed(done, len(courses_sorted))
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
            day_load["OVERFLOW"] += 1
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
            # five-level priority tuple (lower is better):
            #   (overload, credit_pair, spacing, day_load, slot_load)
            # Python tuple comparison ensures level 1 always trumps level 2, etc.
            # day_load comes before slot_load so courses with no other
            # constraints spread across days first, then balance within day.
            course_students = enrolled_sets[course]
            best_slot = candidates[0]
            best_score = (
                float("inf"),
                float("inf"),
                float("inf"),
                float("inf"),
                float("inf"),
                float("inf"),
            )
            # Reservoir sampling counter for ties when seed is provided.
            ties_seen = 0

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
                            day_credits = [_cm.get(ec, _CREDIT_DEFAULT) for ec in existing] + [
                                this_cr
                            ]
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

                # Level 3 — Day load: spread across days first
                # Level 4 — Slot load: balance within day
                score = (
                    penalty,
                    credit_penalty,
                    spacing_penalty,
                    0 if _pref.get(course) == si else 1,
                    day_load[day],
                    slot_load[si],
                )
                if score < best_score:
                    best_score = score
                    best_slot = si
                    ties_seen = 1
                elif score == best_score and rng is not None:
                    # Reservoir-sample tied slots so the chosen one
                    # varies across runs (1/ties_seen probability).
                    ties_seen += 1
                    if rng.random() < 1.0 / ties_seen:
                        best_slot = si

            chosen = best_slot
        else:
            # No enrolled data available — fall back to least-loaded slot
            chosen = min(
                candidates, key=lambda si: (0 if _pref.get(course) == si else 1, slot_load[si])
            )

        assignment[course] = chosen
        slot_load[chosen] += 1
        chosen_day = slot_by_index[chosen]["day"]
        day_load[chosen_day] += 1
        course_assigned_day[course] = chosen_day

        # Update student day counts + courses-per-day tracking
        if enrolled_sets and course in enrolled_sets:
            for sid in enrolled_sets[course]:
                student_day_count[sid][chosen_day] += 1
                student_day_courses[sid][chosen_day].append(course)

        # Update bucket-day tracking
        for bk in my_buckets:
            bucket_day_courses[bk][chosen_day].add(course)
    if on_placed is not None:
        on_placed(len(courses_sorted), len(courses_sorted))

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


def apply_thin_conflict_policy(
    enrolled_sets: dict[str, set[int]],
    adjacency: dict[str, dict[str, int]],
    threshold: int,
) -> tuple[dict[str, dict[str, int]], list[dict]]:
    """Return the shared relaxed graph and report without mutating its input."""
    adj = {course: dict(neighbours) for course, neighbours in adjacency.items()}
    thin_courses_report: list[dict] = []
    if threshold > 0:
        thin_set = {cc for cc, sids in enrolled_sets.items() if len(sids) <= threshold}
        # Snapshot full neighbour list for every thin course BEFORE any
        # mutation. Otherwise, when courses A and B are mutual thin
        # neighbours, processing A first pops B's back-edge to A, so
        # B's "dropped edges" report would be short by 1 and missing A
        # from its neighbours list. The report must be independent of
        # iteration order.
        thin_neighbours = {cc: sorted(adj.get(cc, {}).keys()) for cc in thin_set}
        for cc in sorted(thin_set):
            dropped = thin_neighbours[cc]
            thin_courses_report.append(
                {
                    "course_code": cc,
                    "total_students": len(enrolled_sets[cc]),
                    "dropped_edges": len(dropped),
                    "neighbours": dropped,
                }
            )
            adj[cc] = {}
            for n in dropped:
                if n in adj:
                    adj[n].pop(cc, None)

    return adj, thin_courses_report


def _compute_thin_clash_risk(
    enrolled_sets: dict[str, set[int]],
    schedule_entries: list[dict],
    *,
    thin_courses: set[str] | None = None,
) -> list[dict]:
    """Walk the schedule and report any student whose exams collide in
    the same slot.

    Used after thin-conflict relaxation to surface the realised cost
    of dropping edges. Returns one entry per (student, slot) collision
    with the colliding course list.
    """
    slot_to_courses: dict[int, list[str]] = defaultdict(list)
    for e in schedule_entries:
        if e.get("day") == "OVERFLOW":
            continue
        slot_to_courses[e["slot_index"]].append(e["course_code"])

    clashes: list[dict] = []
    for si, course_codes in sorted(slot_to_courses.items()):
        if len(course_codes) < 2:
            continue
        # Find students enrolled in 2+ of these courses
        student_courses_in_slot: dict[int, list[str]] = defaultdict(list)
        for cc in course_codes:
            for sid in enrolled_sets.get(cc, ()):
                student_courses_in_slot[sid].append(cc)
        for sid, ccs in sorted(student_courses_in_slot.items()):
            if len(ccs) >= 2 and (thin_courses is None or thin_courses.intersection(ccs)):
                clashes.append(
                    {
                        "student_id": sid,
                        "slot_index": si,
                        "courses": sorted(ccs),
                    }
                )
    return clashes


def attach_exam_relaxation_qa(
    qa: dict,
    enrolled_sets: dict[str, set[int]],
    schedule_entries: list[dict],
    threshold: int,
    thin_courses_report: list[dict],
) -> None:
    """Keep actual clashes visible while classifying only unrelaxed pairs as hard.

    Removing a thin course's graph edges approves pairs involving that course;
    it never approves a pair of two ordinary courses or a bucket-day violation.
    A mixed student collision can therefore have both approved and hard pairs.
    """
    thin_courses = {row["course_code"] for row in thin_courses_report}
    qa["thin_threshold"] = threshold
    qa["thin_courses"] = thin_courses_report
    qa["thin_clash_risk"] = (
        _compute_thin_clash_risk(
            enrolled_sets,
            schedule_entries,
            thin_courses=thin_courses,
        )
        if threshold
        else []
    )
    hard_clashes = []
    approved_only = 0
    for row in qa["same_slot_conflicts"]:
        ordinary = [course for course in row["courses"] if course not in thin_courses]
        if len(ordinary) >= 2:
            hard_clashes.append({**row, "courses": ordinary})
        else:
            approved_only += 1
    qa["approved_thin_conflict_count"] = approved_only
    qa["hard_conflict_count"] = len(hard_clashes)
    details = [{"kind": "same_slot", **row} for row in hard_clashes] + [
        {"kind": "bucket_day", **row} for row in qa["bucket_day_violations"]
    ]
    qa["manual_override_details"] = details
    qa["manual_override_count"] = len(details)
    qa["schedule_violation_details"] = details
    qa["schedule_violation_count"] = len(details)
    qa["violation_source"] = "current_placements"


def _build_qa(
    enrolled_sets: dict[str, set[int]],
    schedule_entries: list[dict],
    max_per_day: int = 2,
    plan_term_buckets: dict[tuple[str, int], set[str]] | None = None,
    credit_map: dict[str, int] | None = None,
) -> dict:
    """Validate the schedule and produce a QA report.

    Checks hard-constraint violations in generated or manually moved exams
    and computes soft-constraint metrics:

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
    day_order: dict[str, int] = {}
    for entry in schedule_entries:
        day_order[entry["day"]] = min(
            day_order.get(entry["day"], entry["slot_index"]), entry["slot_index"]
        )
    course_identity: dict[str, str] = {
        e["course_code"]: str(
            e.get("course_identity") or e.get("source_course_code") or e["course_code"]
        )
        for e in schedule_entries
    }

    slots_used = len({e["slot_index"] for e in schedule_entries})

    # Invert enrolled_sets: student_id → [course_codes] so we can iterate
    # per-student and check their personal schedule for violations.
    student_courses: dict[int, list[str]] = defaultdict(list)
    for cc, sids in sorted(enrolled_sets.items()):
        for sid in sorted(sids):
            student_courses[sid].append(cc)

    _cm = credit_map or {}

    # ── Accumulators ──
    same_slot_conflicts: list[dict] = []  # hard-constraint violations
    max_exams_per_day: int = 0  # worst-case day load globally
    students_over_limit_per_day: int = 0  # students exceeding soft cap
    max_credit_load_per_day: int = 0  # worst-case credit sum on a day
    heavy_day_students: int = 0  # students with heavy credit pair

    # Detail records for KPI drilldown panel in the UI
    overload_details: list[dict] = []  # per-student, per-day overload records
    heavy_day_details: list[dict] = []  # per-student, per-day heavy-credit records

    # ── Per-student validation ──
    for sid, courses in sorted(student_courses.items()):
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

        # If ≥2 courses land in the same slot, report a schedule violation.
        for si, ccs in sorted(slot_groups.items()):
            if len(ccs) >= 2:
                same_slot_conflicts.append(
                    {
                        "student_id": sid,
                        "slot_index": si,
                        "courses": sorted(ccs),
                    }
                )

        # ── Soft-constraint metrics per day ──
        has_overload = False  # does this student exceed the per-day cap?
        has_heavy_day = False  # does this student have a heavy credit pairing?
        for _day, ccs in sorted(day_groups.items(), key=lambda item: day_order[item[0]]):
            if _day == "OVERFLOW":
                continue  # OVERFLOW is a virtual day — skip for metrics

            day_count = len(ccs)
            max_exams_per_day = max(max_exams_per_day, day_count)

            # Day-overload check: student exceeds the soft cap
            if day_count > max_per_day:
                has_overload = True
                overload_details.append(
                    {
                        "student_id": sid,
                        "day": _day,
                        "count": day_count,
                        "courses": [
                            {"code": c, "credits": _cm.get(c, _CREDIT_DEFAULT) if _cm else None}
                            for c in sorted(ccs)
                        ],
                    }
                )

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
                    heavy_day_details.append(
                        {
                            "student_id": sid,
                            "day": _day,
                            "penalty": pair_penalty,
                            "total_credits": total_credits,
                            "courses": [
                                {"code": c, "credits": _cm.get(c, _CREDIT_DEFAULT)}
                                for c in sorted(ccs)
                            ],
                        }
                    )

        if has_overload:
            students_over_limit_per_day += 1
        if has_heavy_day:
            heavy_day_students += 1

    # ── Bucket (programme-plan term) day-rule verification ──
    # Hard constraint B says no two courses from the same (program, term)
    # bucket should share a day. Pins and subsequent manual moves can both
    # create violations; QA does not imply that a pin caused the problem.
    bucket_day_violations: list[dict] = []
    bucket_count = 0
    if plan_term_buckets:
        bucket_count = len(plan_term_buckets)
        for (program, term), bucket_courses in sorted(plan_term_buckets.items()):
            # Group this bucket's courses by their assigned day
            day_groups_b: dict[str, dict[str, str]] = defaultdict(dict)
            for cc in sorted(bucket_courses):
                day = course_day.get(cc)
                if day is not None and day != "OVERFLOW":
                    identity = course_identity.get(cc, cc)
                    day_groups_b[day].setdefault(identity, cc)
            # Any day with ≥2 bucket-mates is a violation
            for day, ccs in sorted(day_groups_b.items(), key=lambda item: day_order[item[0]]):
                if len(ccs) >= 2:
                    bucket_day_violations.append(
                        {
                            "program": program,
                            "programme_term": term,
                            "day": day,
                            "courses": sorted(ccs.values()),
                        }
                    )

    # These are violation records, not one aggregate student count: a bucket
    # violation and a student slot clash describe different scheduling rules.
    manual_override_details = [
        {"kind": "same_slot", **detail} for detail in same_slot_conflicts
    ] + [{"kind": "bucket_day", **detail} for detail in bucket_day_violations]

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
        "manual_override_count": len(manual_override_details),
        "manual_override_details": manual_override_details,
        "max_credit_load_per_day": max_credit_load_per_day,
        "heavy_day_students": heavy_day_students,
        "overload_details": overload_details,
        "heavy_day_details": heavy_day_details,
    }


# ── 5b. Room assignment (exam rooms) ───────────────────────────
#
# Exam rooms are allocated AFTER the greedy slot scheduler has decided
# which (day, period) every course goes into.  The workflow per slot:
#
#   1. Place original sections largest first across every course in the period,
#      using the smallest fitting compatible room (preference only breaks ties).
#   2. Consolidate same-course sections without disturbing other courses.
#   3. Jointly repair difficult room combinations before splitting sections.
#   4. Preserve every student and original section identity; insufficient seats
#      remain UNASSIGNED and are surfaced by QA. Exam times never change here.
#
# Constraints enforced:
#   • Each room hosts at most one course per slot (no cross-course share)
#   • Same-course same-gender sections MAY share a room (after merging)
#   • M sections → M rooms only; F sections → F rooms only
#   • Room.capacity is respected
#   • Room.department is IGNORED during exams (all departments share)

_SYNTHETIC_SECTION_LABEL = "ALL"


def _section_gender(section_label: str) -> str:
    """Derive gender ('M' or 'F') from a TermSection.section label.

    Labels are like "M7", "M128", "F3" — the first character is the
    gender tag.  Falls back to "M" for anything unexpected.
    """
    if not section_label:
        return "M"
    first = section_label[0].upper()
    if first in ("M", "F"):
        return first
    return "M"


def build_section_enrollment(
    course_codes: set[str] | list[str],
    programs: list[str] | None = None,
    sections: list[str] | None = None,
) -> dict[str, list[dict]]:
    """Resolve actual scraped sections through the same exam population contract.

    A raw source code can select several canonical course identities; their
    result keys stay separate. There is no synthetic or academic-record fallback.
    """
    wanted = {str(code) for code in course_codes}
    if not wanted:
        return {}
    enrolled, metadata = build_enrolled_sets_with_meta(programs=programs, sections=sections)
    selected = {
        code: members
        for code, members in enrolled.items()
        if code in wanted or metadata[code]["source_course_code"] in wanted
    }
    return resolve_exam_section_enrollment(selected, course_meta=metadata)


def check_room_feasibility(
    section_enrollment: dict[str, list[dict]],
    rooms: list[dict],
) -> list[dict]:
    """Return sections that cannot fit in any single same-gender room.

    A violation means the largest compatible room is smaller than the
    official section, so seating it requires multiple room groups. This
    inventory check does not evaluate competition within a period; that
    belongs to room allocation. It reports demand without blocking a build.
    """
    if not rooms:
        return []
    max_cap_by_gender: dict[str, int] = {"M": 0, "F": 0}
    for r in rooms:
        g = str(r.get("section", "M")).upper() or "M"
        max_cap_by_gender[g] = max(max_cap_by_gender.get(g, 0), int(r.get("capacity", 0) or 0))

    violations: list[dict] = []
    for course_code, sections_data in section_enrollment.items():
        for s in sections_data:
            g = s.get("gender", "M")
            max_cap = max_cap_by_gender.get(g, 0)
            if s["student_count"] > max_cap:
                violations.append(
                    {
                        "course_code": course_code,
                        "section": s["section"],
                        "gender": g,
                        "student_count": s["student_count"],
                        "max_room_capacity": max_cap,
                        **{
                            key: s[key]
                            for key in ("section_key", "mapping_status", "term_section_id")
                            if key in s
                        },
                    }
                )
    return sorted(
        violations,
        key=lambda row: (
            row["course_code"],
            row["gender"],
            str(row.get("section_key", "")),
            row["section"],
        ),
    )


def _split_oversized_sections(
    sections: list[dict],
    max_cap: int,
) -> list[dict]:
    """Split any section larger than ``max_cap`` into roughly-equal parts
    so it can be distributed across multiple rooms.

    Needed for giant lecture sections (e.g. 100-student GS courses)
    where no single same-gender room is big enough — the registrar
    would handle this by splitting the section into two half-sized
    sitting groups during the exam.  We simulate that here.

    A section with ``student_count > max_cap`` is replaced by
    ``ceil(count / max_cap)`` parts labelled ``"<orig>/1"``, ``"<orig>/2"``,
    etc.  Only the first part inherits the preferred_room so the packer
    doesn't try to place every part in the same normal classroom.
    """
    if max_cap <= 0:
        return list(sections)
    import math

    out: list[dict] = []
    for s in sections:
        cnt = int(s.get("student_count", 0) or 0)
        if cnt <= max_cap:
            out.append(s)
            continue
        n_parts = max(2, math.ceil(cnt / max_cap))
        base = cnt // n_parts
        remainder = cnt - base * n_parts
        for i in range(n_parts):
            part_size = base + (1 if i < remainder else 0)
            out.append(
                {
                    **s,
                    "section": s["section"] if "section_key" in s else f"{s['section']}/{i + 1}",
                    "student_count": part_size,
                    "preferred_room": s.get("preferred_room", "") if i == 0 else "",
                    "gender": s.get("gender", "M"),
                    "_split_from": s["section"],
                }
            )
    return out


def _merge_same_course_sections(
    sections_data: list[dict],
    max_room_capacity: int,
) -> list[dict]:
    """Merge same-course same-gender sections if their combined size
    fits in the largest available room.

    Returns a list of demand units where each unit is either:
      - a single section (not merged), or
      - a merged group with ``merged_from`` listing the source sections.

    Merging reduces the number of rooms needed for popular courses
    (e.g. two M sections of 15 students each fit in one 30-seat room).
    Only attempts simple two-way and three-way merges — more aggressive
    bin packing isn't worth the complexity for ≤4 sections per gender.
    """
    if len(sections_data) <= 1:
        return list(sections_data)

    # Sort DESC by student_count so we try to pair up largest first
    by_size = sorted(sections_data, key=lambda s: s["student_count"], reverse=True)
    units: list[dict] = []
    used_indices: set[int] = set()

    for i, s_i in enumerate(by_size):
        if i in used_indices:
            continue
        group = [s_i]
        running = int(s_i["student_count"])
        used_indices.add(i)
        # Try to add smaller sections while they fit
        for j, s_j in enumerate(by_size):
            if j <= i or j in used_indices:
                continue
            if running + int(s_j["student_count"]) <= max_room_capacity:
                group.append(s_j)
                running += int(s_j["student_count"])
                used_indices.add(j)

        if len(group) == 1:
            units.append(s_i)
        else:
            preferred = next((g["preferred_room"] for g in group if g["preferred_room"]), "")
            units.append(
                {
                    "section": "+".join(g["section"] for g in group),
                    "student_count": running,
                    "preferred_room": preferred,
                    "gender": s_i["gender"],
                    "merged_from": [g["section"] for g in group],
                    # Keep original constituents so the assigner can split
                    # the group back if the merged room candidate fails.
                    "_constituents": [dict(g) for g in group],
                }
            )
    return units


def period_cohort_count(
    schedule_entries: list[dict], section_enrollment: dict[str, list[dict]]
) -> int:
    """Upper bound on the ``allocate_period`` calls one full pack performs.

    Rooming solves a separate allocation per scheduled slot per student cohort,
    so this — not the course count — is what a wall budget has to be sized by.
    """
    slots = {entry["slot_index"] for entry in schedule_entries if entry.get("day") != "OVERFLOW"}
    genders = {
        str(section.get("gender", "U") or "U").upper()
        for sections in section_enrollment.values()
        for section in sections
    }
    return len(slots) * max(1, len(genders))


def assign_rooms_to_schedule(
    schedule_entries: list[dict],
    section_enrollment: dict[str, list[dict]],
    rooms: list[dict],
    seed: int | None = None,
    *,
    allocation_context: RoomAllocationContext | None = None,
    on_period: Counter | None = None,
) -> list[dict]:
    """Room original sections across each period without changing exam times.

    Scheduling may use ``seed``; room assignment deliberately does not. Build,
    fixed-time Check, Save and export must agree for identical authoritative
    inputs. Existing room rows are replaced, making repeated calls idempotent.

    ``on_period(done, total)`` counts the (period, gender) packs, the unit the
    solver works in, for a job that reports its progress.
    """
    inventory = normalized_rooms(rooms)
    entries_by_slot: dict[int, list[dict]] = defaultdict(list)
    for entry in schedule_entries:
        entry["rooms"] = []
        if entry.get("day") != "OVERFLOW":
            entries_by_slot[entry["slot_index"]].append(entry)
    context = allocation_context or RoomAllocationContext.for_periods(
        period_cohort_count(schedule_entries, section_enrollment)
    )
    packs: list[tuple[dict[str, dict], str, list[dict]]] = []
    for _, entries in sorted(entries_by_slot.items()):
        by_course = {entry["course_code"]: entry for entry in entries}
        demands_by_gender: dict[str, list[dict]] = defaultdict(list)
        for code in sorted(by_course):
            for section in section_enrollment.get(code, []):
                gender = str(section.get("gender", "U") or "U").upper()
                demands_by_gender[gender].append(
                    {
                        **section,
                        "course_code": code,
                        "course_identity": by_course[code].get("course_identity", code),
                        "gender": gender,
                    }
                )
        packs.extend(
            (by_course, gender, demands) for gender, demands in sorted(demands_by_gender.items())
        )
    for done, (by_course, gender, demands) in enumerate(packs):
        if on_period is not None:
            on_period(done, len(packs))
        period_rooms = [room for room in inventory if room["section"] == gender]
        rows = allocate_period(demands, period_rooms, context)
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for row in rows:
            # Unseated original sections remain individually reviewable.
            key = (
                row["course_code"],
                row["room_code"],
                str(row.get("section_key", row["section"]))
                if row["room_code"] == "UNASSIGNED"
                else "",
            )
            groups[key].append(row)
        for (code, room_code, _), parts in sorted(groups.items()):
            by_course[code]["rooms"].append(
                {
                    "section": " + ".join(dict.fromkeys(p["section"] for p in parts)),
                    "room_code": room_code,
                    "student_count": sum(p["student_count"] for p in parts),
                    "room_capacity": parts[0]["room_capacity"],
                    "gender": gender,
                    "merged_from": list(dict.fromkeys(p["section"] for p in parts)),
                    "section_parts": [exam_section_part(p) for p in parts],
                }
            )
    if on_period is not None:
        on_period(len(packs), len(packs))
    annotate_exam_room_groups(schedule_entries)
    return schedule_entries


#: Every student-facing soft metric _build_qa reports. Balancing staff may not
#: make any of them worse, so the guard compares the whole tuple, not a total:
#: a dimension left out of this list is a dimension nothing protects.
#: These are AGGREGATES, not per-student guarantees - see student_load_signature.
STUDENT_LOAD_METRICS = (
    "students_over_limit_per_day",
    "max_exams_per_day_per_student",
    "max_credit_load_per_day",
    "heavy_day_students",
)


def student_load_signature(
    enrolled_sets: dict[str, set[int]],
    schedule_entries: list[dict],
    *,
    max_per_day: int = 2,
    plan_term_buckets: dict[tuple[str, int], set[str]] | None = None,
    credit_map: dict[str, int] | None = None,
) -> tuple[int, ...]:
    """What a board costs its students, as a tuple that may never grow.

    Staff balancing protects seats and invigilators and said nothing about
    students, so it was free to flatten the staff curve by concentrating exams
    onto somebody's day - and measurably did: on a real 167-course board it
    drove heavy_day_students from 0 to 63 and max exams/day from 2 to 3 while
    doing exactly what it had been asked to do.

    What this does NOT promise: these are the aggregates _build_qa reports, so
    two changes still read as neutral. A move can give a student a second exam
    in a day whose credit pair scores below the heavy threshold, and it can
    un-harm one student while harming another, leaving the counts flat. Both
    match how the scheduler itself trades; neither is a per-student guarantee.
    """
    qa = _build_qa(
        enrolled_sets,
        schedule_entries,
        max_per_day=max_per_day,
        plan_term_buckets=plan_term_buckets,
        credit_map=credit_map,
    )
    return tuple(int(qa.get(metric, 0) or 0) for metric in STUDENT_LOAD_METRICS)


#: Trials a rooming deadline can afford per second, and the floor below which
#: the pass is not worth starting. A trial costs ~0.15s of wall on a developer
#: workstation, so ~1.4 per budget-second leaves room for the mandatory pack
#: and keeps the deterministic cap, not the clock, as the binding constraint.
TRIALS_PER_BUDGET_SECOND = 1.4
MIN_TRIAL_BUDGET = 30
MAX_TRIAL_BUDGET = 120


def derive_trial_budget(budget_seconds: float | None) -> int:
    """Scale the search with the deadline it has to finish inside."""
    if not budget_seconds or budget_seconds <= 0:
        return MIN_TRIAL_BUDGET
    scaled = int(budget_seconds * TRIALS_PER_BUDGET_SECOND)
    return max(MIN_TRIAL_BUDGET, min(MAX_TRIAL_BUDGET, scaled))


def _rebalance_invigilators_pass(
    schedule_entries: list[dict],
    section_enrollment: dict[str, list[dict]],
    rooms_list: list[dict],
    slots: list[dict],
    adj: dict[str, dict[str, int]],
    plan_term_buckets: dict[tuple[str, int], set[str]] | None,
    course_buckets: dict[str, list[tuple[str, int]]] | None,
    *,
    max_iterations: int = 200,
    # A WORK budget, not a wall-clock one, so the same inputs produce the same
    # board on a fast workstation and on the 0.5-CPU production instance.
    #
    # It has to be proportional to the deadline, because the deadline is
    # anti-correlated with difficulty: search_budget_seconds sizes on
    # period/cohorts, which SHRINKS as the exam period shortens, while the work
    # GROWS, because the same courses pack into fewer, denser slots. Measured
    # cold-cache on the real roster, guard on, against a flat cap of 120:
    #
    #   board      budget   work    repacks   headroom   spread after
    #   15d x 3p    55.0s   9.96s      67       5.5x          2
    #   14d x 3p    52.0s  13.95s      64       3.7x          2
    #   12d x 3p    46.0s  20.22s     125       2.3x          5
    #   10d x 3p    40.0s  16.54s     122       2.4x         25
    #    8d x 3p    34.0s  17.91s     127       1.9x         50
    #
    # A flat cap is therefore tuned on the easiest shape and lets every shorter
    # board run until the WALL stops it - which makes that board host-dependent
    # and, at a 4x slowdown on twelve days, degrades it to spread 34, barely
    # better than the defect this pass exists to fix. None means "derive it",
    # which is what both production callers do.
    max_trials: int | None = None,
    pinned_courses: set[str] | None = None,
    allocation_context: RoomAllocationContext | None = None,
    enrolled_sets: dict[str, set[int]] | None = None,
    credit_map: dict[str, int] | None = None,
    max_per_day: int = 2,
    caller: str = "unknown",
    on_trial: Counter | None = None,
) -> int:
    """Final post-pass that moves courses between days to flatten the
    per-day invigilator demand.

    ``enrolled_sets`` arms the student-load guard and every production caller
    supplies it. Without it the pass can only see rooms and staff, and a move
    that flattens the invigilator curve by pushing a third exam into a
    student's day looks free.

    Local search:
      1. Recompute per-day invigilator totals using the current packing.
      2. Identify the hottest day (highest demand) and the coldest day.
      3. For each course on the hottest day, try moving it to a free slot
         on the coldest day.  A move is valid only if it doesn't create
         a same-slot student conflict and doesn't violate any
         (programme, term) bucket day rule.
      4. After each tentative move, re-pack rooms for the WHOLE schedule
         (because room demand on both affected slots changes).  Compute
         the new standard deviation of per-day invigilator totals.
         Accept the move if it strictly improves stddev; otherwise revert.
      5. Repeat until no improving move is found or max_iterations hit.

    Returns the number of moves accepted.  Idempotent and reversible —
    schedule_entries is mutated in place but never made worse than its
    starting state (we always revert non-improving moves).
    """
    if not schedule_entries or not rooms_list or not slots:
        return 0

    from copy import deepcopy

    course_buckets = course_buckets or {}
    plan_term_buckets = plan_term_buckets or {}
    pinned_courses = pinned_courses or set()
    allocation_context = allocation_context or RoomAllocationContext.for_periods(
        period_cohort_count(schedule_entries, section_enrollment)
    )

    # Slot lookup helpers
    slots_by_day: dict[str, list[dict]] = defaultdict(list)
    for s in slots:
        slots_by_day[s["day"]].append(s)

    def _repack_all() -> None:
        """Clear current room assignments and re-run the full packer."""
        for e in schedule_entries:
            e["rooms"] = []
        assign_rooms_to_schedule(
            schedule_entries,
            section_enrollment,
            rooms_list,
            seed=None,
            allocation_context=allocation_context,
        )

    def _per_day_invigilators() -> dict[str, dict[str, int]]:
        """Return ``{day: {'M': int, 'F': int, 'total': int}}`` so the
        rebalance metric can score per-gender stddev rather than only
        the combined total — a move that flattens the total but
        worsens the M-only or F-only spread should not be accepted.
        """
        per_day: dict[str, dict[str, int]] = defaultdict(lambda: {"M": 0, "F": 0, "total": 0})
        for e in schedule_entries:
            if e.get("day") == "OVERFLOW":
                continue
            for a in e.get("rooms", []) or []:
                if a.get("room_code") == "UNASSIGNED":
                    continue
                stu = int(a.get("student_count", 0) or 0)
                invigs = _invigilators_needed(e["course_code"], stu)
                gender = a.get("gender", "M")
                per_day[e["day"]][gender] += invigs
                per_day[e["day"]]["total"] += invigs
        return {k: dict(v) for k, v in per_day.items()}

    def _room_safety() -> tuple[int, int, int]:
        """Preserve seating coverage and whole sections before balancing staff.

        Count rooms and any unseated remainder by original section and cohort.
        Unassigned demand is measured against the captured enrollment, so a
        missing output row cannot make an unsafe trial improve the staff load.
        """
        unseated = 0
        section_rooms: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        for entry in schedule_entries:
            if entry.get("day") == "OVERFLOW":
                continue
            expected = sum(
                int(section.get("student_count", 0) or 0)
                for section in section_enrollment.get(entry["course_code"], [])
            )
            seated = 0
            identity = str(entry.get("course_identity") or entry["course_code"])
            for room in entry.get("rooms", []) or []:
                room_code = str(room.get("room_code") or "")
                if room_code not in {"", "UNASSIGNED"}:
                    seated += int(room.get("student_count", 0) or 0)
                for part in room.get("section_parts") or [room]:
                    logical = str(
                        part.get("section_key")
                        or part.get("_split_from")
                        or part.get("section")
                        or ""
                    )
                    gender = str(part.get("gender") or room.get("gender") or "")
                    section_rooms[(identity, gender, logical)].add(room_code or "UNASSIGNED")
            unseated += max(0, expected - seated)
        parts = [len(rooms) for rooms in section_rooms.values()]
        return (
            unseated,
            sum(count > 1 for count in parts),
            sum(max(0, count - 1) for count in parts),
        )

    def _balance_score(per_day: dict[str, dict[str, int]]) -> tuple[float, int, int]:
        """Score a per-day distribution.  Lower is flatter.

        Returns a tuple ``(combined_stddev, max_total, max_minus_min_total)``
        so we sort lexicographically: primary objective is the combined
        per-gender standard deviation (M-series and F-series concatenated),
        with the day-total max and spread as tiebreakers.  Using both
        gender series guarantees a move that helps the overall total but
        worsens one gender's distribution will be rejected.
        """
        if not per_day:
            return (0.0, 0, 0)
        m_vals = [v["M"] for v in per_day.values()]
        f_vals = [v["F"] for v in per_day.values()]
        t_vals = [v["total"] for v in per_day.values()]
        # stddev across M-series + F-series combined (so each gender is
        # weighted equally regardless of which one happens to be larger)
        combined = m_vals + f_vals
        n = len(combined)
        mean = sum(combined) / n
        var = sum((x - mean) ** 2 for x in combined) / n
        return (var**0.5, max(t_vals), max(t_vals) - min(t_vals))

    def _causes_conflict(
        course: str, target_slot_idx: int, current_slot_of: dict[str, int]
    ) -> bool:
        for other, si in current_slot_of.items():
            if other == course or si != target_slot_idx:
                continue
            if other in adj.get(course, {}) or course in adj.get(other, {}):
                return True
        return False

    def _causes_bucket_violation(
        course: str, target_day: str, current_day_of: dict[str, str]
    ) -> bool:
        for bk in course_buckets.get(course, []):
            for mate in plan_term_buckets.get(bk, set()):
                if mate != course and current_day_of.get(mate) == target_day:
                    return True
        return False

    # Make sure we start from a clean packing. The mandatory pack has already
    # produced a validated rooming, so this optional pass must never destroy
    # it: an exhausted deadline here restores that rooming and declines to
    # optimise rather than failing a build that had already succeeded.
    incoming_rooms = [deepcopy(item.get("rooms", [])) for item in schedule_entries]
    try:
        _repack_all()
    except RoomAllocationTimeout:
        allocation_context.truncated = True
        for item, rooms_snapshot in zip(schedule_entries, incoming_rooms, strict=True):
            item["rooms"] = rooms_snapshot
        logger.warning(
            "exam invigilator rebalance skipped: room allocation deadline exhausted "
            "before the opening repack (periods_solved=%s cache_hits=%s searches=%s)",
            allocation_context.periods_solved,
            allocation_context.cache_hits,
            allocation_context.searches,
        )
        return 0

    def _student_safety() -> tuple[int, ...]:
        if enrolled_sets is None:
            return ()
        return student_load_signature(
            enrolled_sets,
            schedule_entries,
            max_per_day=max_per_day,
            plan_term_buckets=plan_term_buckets,
            credit_map=credit_map,
        )

    current = _per_day_invigilators()
    base_score = _balance_score(current)
    base_room_safety = _room_safety()
    base_student_safety = _student_safety()
    moves_accepted = 0

    # Day pairs this search has already tried without finding an acceptable
    # move. Exhausting one pair is not a reason to abandon the optimisation:
    # before this, a single refused move on the widest pair ended the whole
    # pass, so adding the student veto cut Build from 27 accepted moves to 8
    # and left invigilator spread at 17 instead of 2 - with most of its wall
    # budget unspent. Any accepted move reshapes the load, so the set clears.
    exhausted_pairs: set[tuple[str, str]] = set()
    trials_spent = 0
    if max_trials is None:
        max_trials = derive_trial_budget(
            allocation_context.budget_seconds if allocation_context else None
        )

    for _iter in range(max_iterations):
        if not current or trials_spent >= max_trials:
            break
        # Widest remaining gap first; day names break ties so the search order
        # is fixed by the board alone and never by dict ordering.
        gaps = sorted(
            (
                (cold["total"] - hot["total"], hot_day, cold_day)
                for hot_day, hot in current.items()
                for cold_day, cold in current.items()
                if hot["total"] - cold["total"] > 2
            ),
            key=lambda gap: (gap[0], gap[1], gap[2]),
        )
        pair = next((gap for gap in gaps if (gap[1], gap[2]) not in exhausted_pairs), None)
        if pair is None:
            break  # every worthwhile day pair is flat or already exhausted
        _, hottest_day, coldest_day = pair

        improved = False
        # Snapshot current slot/day lookups
        current_slot_of = {e["course_code"]: e["slot_index"] for e in schedule_entries}
        current_day_of = {e["course_code"]: e["day"] for e in schedule_entries}

        # Fixed external exams participate in load totals but may never move.
        hot_entries = [
            e
            for e in schedule_entries
            if e.get("day") == hottest_day and e["course_code"] not in pinned_courses
        ]
        # Process larger courses first — they shift more invigilator weight
        hot_entries.sort(
            key=lambda e: -sum(
                int(s.get("student_count", 0) or 0)
                for s in section_enrollment.get(e["course_code"], [])
            )
        )

        for entry in hot_entries:
            cc = entry["course_code"]
            old_slot_idx = entry["slot_index"]
            old_day = entry["day"]
            old_period = entry["period"]

            for target_slot in slots_by_day.get(coldest_day, []):
                tsi = target_slot["index"]
                if tsi == old_slot_idx:
                    continue
                if _causes_conflict(cc, tsi, current_slot_of):
                    continue
                if _causes_bucket_violation(cc, coldest_day, current_day_of):
                    continue

                # Tentative move
                trials_spent += 1
                if on_trial is not None:
                    # A ceiling, not an estimate: the search may converge first.
                    on_trial(trials_spent, max_trials)
                previous_rooms = [deepcopy(item.get("rooms", [])) for item in schedule_entries]
                entry["slot_index"] = tsi
                entry["day"] = target_slot["day"]
                entry["period"] = target_slot["period"]
                try:
                    _repack_all()
                except RoomAllocationTimeout:
                    # Allocation may have cleared or partially replaced rows.
                    # Restore the exact validated incumbent and stop probing;
                    # an exhausted deadline cannot support another safe trial.
                    # This pass is optional, so the build continues — but the
                    # optimisation is now incomplete and must say so.
                    allocation_context.truncated = True
                    logger.warning(
                        "exam invigilator rebalance truncated after %s accepted move(s): "
                        "room allocation deadline exhausted "
                        "(periods_solved=%s cache_hits=%s searches=%s limited=%s)",
                        moves_accepted,
                        allocation_context.periods_solved,
                        allocation_context.cache_hits,
                        allocation_context.searches,
                        allocation_context.limited_searches,
                    )
                    entry["slot_index"] = old_slot_idx
                    entry["day"] = old_day
                    entry["period"] = old_period
                    for item, rooms_snapshot in zip(schedule_entries, previous_rooms, strict=True):
                        item["rooms"] = rooms_snapshot
                    return moves_accepted
                new_per_day = _per_day_invigilators()
                new_score = _balance_score(new_per_day)
                new_room_safety = _room_safety()
                new_student_safety = _student_safety()

                # Accept only if strictly improving the lexicographic
                # (combined-stddev, max-day, spread) score.  A 0.01
                # tolerance on the stddev component prevents oscillation
                # when several moves have indistinguishable impact.
                # Safety is a veto, never a trade: staff balance may not be
                # bought with seats, split sections or student exam days.
                safe = all(
                    new <= old for new, old in zip(new_room_safety, base_room_safety, strict=True)
                ) and all(
                    new <= old
                    for new, old in zip(new_student_safety, base_student_safety, strict=True)
                )
                accept = safe and (
                    new_score[0] + 0.01 < base_score[0]
                    or (
                        abs(new_score[0] - base_score[0]) <= 0.01 and new_score[1:] < base_score[1:]
                    )
                )
                if accept:
                    base_score = new_score
                    base_room_safety = new_room_safety
                    base_student_safety = new_student_safety
                    current = new_per_day
                    moves_accepted += 1
                    improved = True
                    break

                # Revert the exact validated room snapshot as well as time.
                # This does not spend the repair budget again on unchanged data.
                entry["slot_index"] = old_slot_idx
                entry["day"] = old_day
                entry["period"] = old_period
                for item, rooms_snapshot in zip(schedule_entries, previous_rooms, strict=True):
                    item["rooms"] = rooms_snapshot

            if improved:
                break

        if improved:
            # The board changed, so pairs that had nothing to offer may now.
            # Completeness rather than a measurable gain: on the real 15-day
            # board keeping the blacklist instead produced an identical
            # timetable in 98 trials rather than 101, so no test pins this.
            exhausted_pairs.clear()
        else:
            exhausted_pairs.add((hottest_day, coldest_day))

    # Accepted trials already carry validated rooms; rejected trials restore
    # their exact incumbent. No further solve is needed after the last trial.
    #
    # Say how the search ended. Converging, spending its trial budget and being
    # vetoed into a standstill are three different outcomes that all used to
    # look identical from outside - rebalance_moves alone cannot tell them
    # apart, and the truncation warning fires on only one of them.
    logger.info(
        "exam invigilator rebalance: caller=%s outcome=%s moves=%s trials=%s/%s",
        caller,
        "deadline"
        if allocation_context.truncated
        else ("trial_budget" if trials_spent >= max_trials else "converged"),
        moves_accepted,
        trials_spent,
        max_trials,
    )
    return moves_accepted


def _build_room_qa(
    schedule_entries: list[dict],
    rooms: list[dict],
) -> dict:
    """QA metrics for room assignment — rooms used, utilisation, unassigned,
    and double-booking defensive check (should never trigger).
    """
    rooms_used_keys: set[tuple[int, str]] = set()
    total_demand = 0
    total_capacity_used = 0
    unassigned: list[dict] = []

    # Double-booking defensive check — (slot_index, room_code) should map
    # to a single course.  Track (slot, room) → course mappings.
    slot_room_course: dict[tuple[int, str], str] = {}
    double_bookings: list[dict] = []

    # Per-day invigilator totals, split by gender.
    # invigilators_per_day[day]['M'/'F'/'total'] = int
    invigilators_per_day: dict[str, dict[str, int]] = defaultdict(
        lambda: {"M": 0, "F": 0, "total": 0}
    )

    for e in sorted(
        schedule_entries,
        key=lambda entry: (
            int(entry.get("slot_index", -1)),
            str(entry.get("course_identity") or entry["course_code"]),
            entry["course_code"],
        ),
    ):
        if e.get("day") == "OVERFLOW":
            continue
        si = int(e.get("slot_index", -1))
        for a in sorted(
            e.get("rooms", []),
            key=lambda room: (
                str(room.get("room_code", "")),
                str(room.get("gender", "")),
                str(room.get("section", "")),
                str(room.get("room_group", "")),
            ),
        ):
            code = a.get("room_code", "")
            if code == "UNASSIGNED":
                unassigned.append(
                    {
                        "course_code": e["course_code"],
                        "day": e["day"],
                        "period": e["period"],
                        "section": a.get("section", ""),
                        "student_count": int(a.get("student_count", 0) or 0),
                        "gender": a.get("gender", ""),
                        **(
                            {
                                "section_parts": a["section_parts"],
                                "room_group": a.get("room_group", ""),
                                "mapping_status": a.get("mapping_status", ""),
                                **{
                                    key: e.get(key, "")
                                    for key in (
                                        "course_identity",
                                        "source_course_code",
                                        "course_name",
                                    )
                                },
                            }
                            if a.get("mapping_status")
                            else {}
                        ),
                    }
                )
                continue
            rooms_used_keys.add((si, code))
            stu = int(a.get("student_count", 0) or 0)
            total_demand += stu
            total_capacity_used += int(a.get("room_capacity", 0) or 0)
            # Invigilator tally for this room
            invigs = _invigilators_needed(e["course_code"], stu)
            gender = a.get("gender", "M")
            day_label = e["day"]
            invigilators_per_day[day_label][gender] += invigs
            invigilators_per_day[day_label]["total"] += invigs
            prev = slot_room_course.get((si, code))
            if prev is not None and prev != e["course_code"]:
                double_bookings.append(
                    {
                        "slot_index": si,
                        "room_code": code,
                        "courses": sorted([prev, e["course_code"]]),
                    }
                )
            else:
                slot_room_course[(si, code)] = e["course_code"]

    avg_util = (total_demand / total_capacity_used) if total_capacity_used else 0.0
    # Convert invigilator dict to a stable JSON-serialisable shape
    invig_summary = {day: dict(counts) for day, counts in invigilators_per_day.items()}
    invig_grand_total = sum(c["total"] for c in invig_summary.values())
    invig_grand_M = sum(c["M"] for c in invig_summary.values())
    invig_grand_F = sum(c["F"] for c in invig_summary.values())
    return {
        "rooms_available": len(rooms),
        "rooms_used": len(rooms_used_keys),
        "total_demand": total_demand,
        "total_capacity_used": total_capacity_used,
        "avg_utilization": round(avg_util, 4),
        "unassigned_room_sections": unassigned,
        "room_double_bookings": double_bookings,
        "invigilators_per_day": invig_summary,
        "invigilators_total": invig_grand_total,
        "invigilators_total_M": invig_grand_M,
        "invigilators_total_F": invig_grand_F,
    }


def _build_section_enrollment_from_enrolled_sets(
    enrolled_sets: dict[str, set[int]],
    *,
    section_by_student: dict[int, str] | None = None,
    course_meta: dict[str, dict] | None = None,
    program_by_student: dict[int, str] | None = None,
    operations_sections: dict[str, list[dict]] | None = None,
) -> dict[str, list[dict]]:
    """Use recorded teaching sections without changing the exam population."""
    return resolve_exam_section_enrollment(
        enrolled_sets,
        course_meta=course_meta,
        section_by_student=section_by_student,
        program_by_student=program_by_student,
        operations_sections=operations_sections,
    )


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
    seed: int | None = None,
    assign_rooms: bool = True,
    rebalance_invigilators: bool = True,
    thin_conflict_threshold: int = 0,
    persist: bool = True,
    selected_course_entries: list[dict] | None = None,
) -> dict:
    """
    End-to-end pipeline: build enrolled sets → conflict graph →
    programme-plan term buckets → feasibility check → greedy schedule
    → QA → persist JSON.

    Returns the full result dict. When ``persist=True`` (the default,
    matching pre-existing single-run callers) a row is written to
    ``ExamTimetableRun`` and the row id appears in ``result["run_id"]``.
    When ``persist=False`` the row is NOT written — the multi-start
    runner uses this mode to evaluate many seeded builds before
    selecting which 4 to persist as Pareto candidates, so the history
    panel doesn't fill with throw-away exploratory runs.

    If a bucket feasibility violation is found, returns an error dict
    without scheduling (key "feasibility_error": True).

    Args:
        selected_courses – if provided, restrict scheduling to only
                           these course codes (user-curated list from
                           the preview step).
        seed             – RNG seed for randomised tie-breaking in the
                           scheduler; None = deterministic (alphabetical).
                           Different seeds produce different timetable
                           variants from the same input data.
        thin_conflict_threshold – if > 0, courses with total enrolled
                           students <= threshold are dropped from the
                           conflict graph (their tiny enrolment no
                           longer blocks other courses from a slot).
                           Default 0 = current behaviour (no relaxation).
                           Realised same-slot clashes for thin students
                           are reported in qa.thin_clash_risk so the
                           registrar can pin them manually if needed.
        persist          – when True (default), writes an
                           ``ExamTimetableRun`` row and stamps the
                           returned dict with ``run_id``. When False,
                           skips the DB write and returns the result
                           dict unstamped — used by the multi-start
                           runner to evaluate candidates before
                           persisting only the selected ones.
    """
    progress = current_progress()
    progress.stage("enrolments")
    # 1. Enrolled sets
    enrolled_sets, course_meta = build_enrolled_sets_with_meta(
        programs=programs,
        sections=sections,
    )

    # 1b. Filter to user-selected courses (if provided from preview step)
    if selected_course_entries is not None:
        if selected_courses is not None and set(selected_courses) != {
            entry.get("course_code") for entry in selected_course_entries
        }:
            raise ValueError("Selected course codes and identities must match.")
        enrolled_sets, course_meta = select_exam_course_enrollments(
            selected_course_entries, enrolled_sets, course_meta
        )
    elif selected_courses is not None:
        keep = set(selected_courses)
        unavailable = sorted(keep - enrolled_sets.keys())
        if unavailable:
            raise ExamCoursesUnavailable(unavailable)
        enrolled_sets = {cc: sids for cc, sids in enrolled_sets.items() if cc in keep}
        course_meta = {cc: meta for cc, meta in course_meta.items() if cc in keep}

    course_list = sorted(enrolled_sets.keys())
    if not course_list:
        raise ValueError(
            "No actual scraped-timetable enrollments match this selection. "
            "Import student timetables, then Load Courses again."
        )

    slots = [
        {"index": index, "day": day, "period": period}
        for index, (day, period) in enumerate((day, period) for day in days for period in periods)
    ]
    pinned = validate_exam_pins(pinned, course_list, slots)

    # 1c. Build credit map for credit-weighted scoring
    credit_map = build_credit_map(course_list)

    all_students: set[int] = set()
    for sids in enrolled_sets.values():
        all_students |= sids

    # 2. Conflict graph
    progress.stage("conflicts")
    conflicts, adj = build_conflict_graph(enrolled_sets)

    # Use the same relaxation policy for fresh builds and loaded optimization.
    adj, thin_courses_report = apply_thin_conflict_policy(
        enrolled_sets, adj, thin_conflict_threshold
    )

    # 3. Programme-plan term buckets
    ptb, cb = build_plan_term_buckets(set(course_list), course_meta=course_meta, programs=programs)

    # 4. Feasibility pre-check
    violations = check_bucket_feasibility(ptb, len(days), pinned=pinned)
    if violations:
        return stamp_schema_version(
            {
                "feasibility_error": True,
                "status": "feasibility_error",
                # v2 status surface: feasibility_error short-circuits
                # to "infeasible" with no further flags.
                "primary_status": "infeasible",
                "status_flags": [],
                "status_derivation_version": STATUS_DERIVATION_VERSION,
                "violations": violations,
                "courses_count": len(course_list),
                "students_count": len(all_students),
                "bucket_count": len(ptb),
            }
        )

    # 6. Schedule (with day-spread + bucket + credit-pair constraints)
    progress.stage("place_exams")
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
        seed=seed,
        on_placed=progress.counter("place_exams"),
    )
    for entry in schedule_entries:
        meta = course_meta.get(entry["course_code"], {})
        entry.update(meta)
        source = _source_code_for_display(entry["course_code"], meta.get("source_course_code"))
        entry["source_course_code"] = source
        entry["course_name"] = str(meta.get("course_name") or "")
        entry["course_identity"] = str(meta.get("course_identity") or source)

    # 7. QA report — validate hard constraints and compute quality metrics
    progress.stage("check_rules")
    qa = _build_qa(
        enrolled_sets,
        schedule_entries,
        max_per_day=max_per_day,
        plan_term_buckets=ptb,
        credit_map=credit_map,
    )

    # 7b. Room assignment (Phase 2) — attach rooms to each schedule entry.
    # Feasibility violations and double-bookings are surfaced in QA but
    # never block the build: unfittable sections simply land in
    # "UNASSIGNED" and the UI flags them.
    student_attribution = list(
        Student.objects.filter(student_id__in=all_students)
        .order_by("student_id")
        .values("student_id", "program", "section")
    )
    operations_sections: dict[str, list[dict]] = {}
    section_enrollment = _build_section_enrollment_from_enrolled_sets(
        enrolled_sets,
        course_meta=course_meta,
        section_by_student={
            row["student_id"]: str(row["section"] or "").strip() for row in student_attribution
        },
        program_by_student={row["student_id"]: row["program"] for row in student_attribution},
        operations_sections=operations_sections,
    )
    rooms_list: list[dict] = []
    room_feasibility: list[dict] = []
    room_qa: dict = {}
    if assign_rooms:
        rooms_list = list(
            Room.objects.all().values(
                "room_code", "capacity", "section", "department", "building", "floor"
            )
        )
        room_feasibility = check_room_feasibility(section_enrollment, rooms_list)
        # Arm the shared deadline only now: the room query and the feasibility
        # scan are not solver work, and on a networked database they were
        # spending a budget meant for the search.
        allocation_context = RoomAllocationContext.for_periods(
            period_cohort_count(schedule_entries, section_enrollment)
        )
        progress.stage("assign_rooms")
        assign_rooms_to_schedule(
            schedule_entries,
            section_enrollment,
            rooms_list,
            seed=seed,
            allocation_context=allocation_context,
            on_period=progress.counter("assign_rooms"),
        )

        # 7c. Final optimisation — flatten per-day invigilator load by
        # moving courses between days whenever the move strictly
        # improves the per-day invigilator-count standard deviation.
        # Skipped when the caller opts out, when there are no rooms, or
        # when there's nothing meaningful to balance (single day).
        rebalance_moves = 0
        if rebalance_invigilators and rooms_list and len(days) > 1:
            progress.stage("balance_invigilators")
            rebalance_moves = _rebalance_invigilators_pass(
                schedule_entries,
                section_enrollment,
                rooms_list,
                slots,
                adj,
                ptb,
                cb,
                pinned_courses={pin["course_code"] for pin in pinned},
                allocation_context=allocation_context,
                enrolled_sets=enrolled_sets,
                credit_map=credit_map,
                max_per_day=max_per_day,
                caller="build",
                on_trial=progress.counter("balance_invigilators"),
            )

        room_qa = _build_room_qa(schedule_entries, rooms_list)
        # Re-run main QA after rebalance so credit/conflict metrics
        # reflect any moved courses (cheap — no DB hits).
        qa = _build_qa(
            enrolled_sets,
            schedule_entries,
            max_per_day=max_per_day,
            plan_term_buckets=ptb,
            credit_map=credit_map,
        )
        # Merge room QA into main QA dict for a single source of truth
        qa["rooms"] = room_qa
        qa["room_feasibility_violations"] = room_feasibility
        qa["rebalance_moves"] = rebalance_moves
    # Defend the fixed-placement contract after all scheduling post-passes.
    validate_exam_pins(pinned, course_list, slots, schedule_entries=schedule_entries)
    attach_exam_relaxation_qa(
        qa,
        enrolled_sets,
        schedule_entries,
        thin_conflict_threshold,
        thin_courses_report,
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

    # ── v2 + v3 telemetry authoring ──
    # Step 4 enriches each schedule entry's ``rooms`` sub-list with the
    # ``building`` field (looked up by room_code from rooms_list) so the
    # v3 building-footprint derivation has the data it needs. This also
    # gets persisted, which means historic v3 rows can re-derive the
    # footprint on read if we ever need to — though by default we
    # populate the footprint at write time and store it under
    # ``qa.building_footprint``.
    if rooms_list:
        room_meta_by_code: dict[str, dict] = {str(r.get("room_code", "")): r for r in rooms_list}
        for entry in schedule_entries:
            for r in entry.get("rooms") or []:
                if not isinstance(r, dict):
                    continue
                meta = room_meta_by_code.get(str(r.get("room_code", "")))
                if meta:
                    r.setdefault("building", str(meta.get("building", "") or ""))
                    r.setdefault("floor", str(meta.get("floor", "") or ""))

    # Multi-sitting details computed from schedule_entries (each entry's
    # ``rooms`` list carries the section labels we detect splits from).
    # The derivation is in the schema module so the v1->v2 migrator and
    # this build site share the same logic.
    multi_sitting_details = derive_multi_sitting_details(schedule_entries)
    qa["multi_sitting_sections"] = len(multi_sitting_details)
    qa["multi_sitting_details"] = multi_sitting_details

    # ── v3 telemetry blocks (display-only — no ranking/scheduler effect) ──
    qa["building_footprint"] = derive_building_footprint(schedule_entries)

    # Section demand comes exclusively from the scraped timetable. An official
    # section may literally be named ALL; its label never indicates a fallback.
    _sections_total = sum(len(v) for v in section_enrollment.values())
    qa["enrolment_snapshot"] = compute_enrolment_snapshot(
        enrolled_sets,
        sections_count=_sections_total,
        fallback_used=False,
        synthetic_all_sections_count=0,
    )
    qa["section_mapping"] = summarize_exam_section_mapping(section_enrollment, course_meta)

    # ── Assemble result dict ──
    # This dict is: (a) returned to the frontend as JSON, (b) persisted
    # in ExamTimetableRun.result_json for later viewing/export.
    # Both consumers go through ``load_normalised_run`` /
    # ``normalise_exam_run_payload`` (see core.services.exam_run_schema),
    # so ``stamp_schema_version`` here is the only write site that needs
    # to know about the schema version constant.
    _draft: dict = {
        "status": "ok",
        "enrollment_source": EXAM_ENROLLMENT_SOURCE,
        "enrollment_scope": {"programs": programs or [], "sections": sections or []},
        "pinned": pinned,
        "students_count": len(all_students),
        "courses": course_list,
        "courses_count": len(course_list),
        "conflicts": conflicts,
        "conflicts_count": len(conflicts),
        "exam_review": build_exam_review(conflicts),
        "slots": slots,
        "schedule": schedule_entries,
        "qa": qa,
        "buckets_summary": buckets_summary,
        "bucket_count": len(ptb),
        "credit_map": credit_map,
        "seed": seed,
        "section_enrollment": section_enrollment,
        "operations_snapshot": build_exam_operations_snapshot(
            schedule_entries, operations_sections
        ),
        "rooms_count": len(rooms_list),
        "assign_rooms": assign_rooms,
    }
    # Compute the registrar status surface from the assembled draft
    # so the headline + flags reflect the real run state.
    primary_status, status_flags = derive_status_surface(_draft)
    _draft["primary_status"] = primary_status
    _draft["status_flags"] = status_flags
    _draft["status_derivation_version"] = STATUS_DERIVATION_VERSION
    result: dict = stamp_schema_version(_draft)
    result["input_fingerprint"] = fingerprint_exam_inputs(
        result=result,
        enrolled_sets=enrolled_sets,
        course_meta=course_meta,
        student_attribution=student_attribution,
        rooms=rooms_list,
        days=days,
        periods=periods,
        max_per_day=max_per_day,
        thin_conflict_threshold=thin_conflict_threshold,
    )

    # Persist (skipped in multi-start exploration mode where we evaluate
    # many candidates and only persist the selected Pareto few).
    if persist:
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
        Schedule        – day × period grid (mirrors the on-screen table)
        Schedule (M)    – same grid filtered to male sections + rooms only
        Schedule (F)    – same grid filtered to female sections + rooms only
        Courses         – flat list with course code, enrolled count, day, period
        Students (M)    – per-course male section counts + student totals
        Students (F)    – per-course female section counts + student totals
        QA Summary      – key metrics + any conflict / bucket-day warnings

    Returns the Path to the written file (in the runtime/ directory).
    """
    import math

    from openpyxl import Workbook  # type: ignore[import-untyped]
    from openpyxl.cell.rich_text import CellRichText, TextBlock  # type: ignore[import-untyped]
    from openpyxl.cell.text import InlineFont  # type: ignore[import-untyped]
    from openpyxl.styles import (  # type: ignore[import-untyped]
        Alignment,
        Border,
        Font,
        PatternFill,
        Side,
    )
    from openpyxl.utils import get_column_letter  # type: ignore[import-untyped]

    run = ExamTimetableRun.objects.get(id=run_id)
    # Read through the normaliser so historic runs (pre-schema-versioning,
    # or rows missing keys this exporter expects) render cleanly with safe
    # defaults. Single read path: never ``json.loads(run.result_json)`` here.
    data = load_normalised_run(run)

    # Hard rule for sentinel payloads: do NOT silently export an empty
    # workbook from an unrenderable run. Registrar trust attaches to the
    # exported artefact more than the web UI; a blank XLSX is worse than
    # a controlled error because the registrar may distribute it without
    # noticing the data is gone. Fail loudly with a message the view
    # layer can surface as a 500 with body text.
    status = data.get("status")
    if status == "unrenderable":
        reason = data.get("error", "payload could not be rendered")
        raise RuntimeError(
            f"Exam timetable run #{run_id} cannot be exported: {reason}. "
            "The stored payload is missing, corrupt, or otherwise unreadable; "
            "rebuild the run before exporting."
        )
    if status == "future_version_unrenderable":
        raise RuntimeError(
            f"Exam timetable run #{run_id} was created by a newer build of "
            "the exam scheduler and cannot be exported by this version. "
            "Upgrade the application or rebuild the run."
        )
    if data.get("enrollment_source") != EXAM_ENROLLMENT_SOURCE:
        raise ValueError(
            "This timetable uses an earlier enrollment source. Load Courses from "
            "actual scraped timetables and rebuild before exporting."
        )

    schedule = data["schedule"]  # list of {course_code, slot_index, day, period}
    slots = data["slots"]  # list of {index, day, period}
    qa = data["qa"]  # QA metrics dict from _build_qa()
    course_entries = {entry["course_code"]: entry for entry in schedule}
    pinned_codes = {pin["course_code"] for pin in data.get("pinned", [])}
    section_enrollment = data.get("section_enrollment", {})

    def _course_label(code: str) -> str:
        entry = course_entries.get(code, {})
        name = str(entry.get("course_name") or "")
        return f"{code} — {name}" if name else code

    def _gender_sections(code: str, gender: str) -> list[dict]:
        return [s for s in section_enrollment.get(code, []) if s.get("gender") == gender]

    def _section_label(section: dict) -> str:
        """Display recorded labels literally; make missing attribution explicit."""
        if section.get("mapping_status") == "ambiguous":
            return "Ambiguous section"
        return str(section.get("section") or "Section not recorded")

    def _section_mapping_label(section: dict) -> str:
        return {
            "mapped": "Recorded",
            "missing": "Section not recorded",
            "ambiguous": "Ambiguous section",
        }.get(str(section.get("mapping_status") or ""), "Not recorded in saved data")

    def _room_section_parts(room: dict) -> list[dict]:
        """Aggregate fragments by teaching section, without parsing its label."""
        parts = room.get("section_parts") or []
        grouped: dict[str, dict] = {}
        for part in parts:
            key = str(part.get("section_key") or "")
            if not key:
                # Older caller-provided allocation rows have no source identity.
                return []
            if key not in grouped:
                grouped[key] = {**part, "student_count": 0}
            grouped[key]["student_count"] += int(part.get("student_count", 0) or 0)
        return list(grouped.values())

    def _room_section_label(room: dict) -> str:
        parts = _room_section_parts(room)
        if not parts:
            return _section_label(room)
        if len(parts) == 1:
            return _section_label(parts[0])
        return "; ".join(f"{_section_label(part)} ({part['student_count']})" for part in parts)

    def _room_mapping_label(room: dict) -> str:
        parts = _room_section_parts(room)
        if not parts:
            return _section_mapping_label(room)
        if len(parts) == 1:
            return _section_mapping_label(parts[0])
        return "; ".join(
            f"{_section_label(part)}: {_section_mapping_label(part)}" for part in parts
        )

    def _status_label(value: str) -> str:
        """Use the same registrar-facing English labels as the screen."""
        labels = {
            "clean": "Clean",
            "clean_with_approved_thin_conflicts": "Clean (with approved tiny-course clashes)",
            "requires_room_action": "Requires room action",
            "contains_overflow": "Contains overflow exams",
            "contains_manual_override": "Contains schedule violations",
            "requires_section_review": "Teaching sections need review",
            "contains_workload_warnings": "Contains workload warnings",
            "infeasible": "Infeasible",
            "unrenderable": "Cannot render this run",
            "future_version_unrenderable": "Created by a newer system version",
            "approved_thin_conflicts": "approved thin conflicts",
            "room_action_required": "room action required",
            "overflow": "overflow",
            "manual_override": "schedule violations",
            "section_mapping_incomplete": "teaching-section mapping incomplete",
            "multi_sitting_required": "multi-sitting required",
            "legacy_incomplete_qa": "legacy / incomplete QA data",
            "daily_limit_exceeded": "daily exam limit exceeded",
            "heavy_credit_day": "heavy credit days",
        }
        return labels.get(value, value.replace("_", " "))

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

    # ── Course colour by code hash (matches frontend colorForCourse) ──
    _color_cache: dict[str, PatternFill] = {}

    def _course_color_fill(code: str) -> PatternFill:
        if code in _color_cache:
            return _color_cache[code]
        # Same hash as JS: h = (h * 31 + charCode) % 360
        h = 0
        for ch in str(code):
            h = (h * 31 + ord(ch)) % 360
        # HSL to RGB: s=70%, l=92% (light pastel, same as frontend light mode)
        import colorsys

        r, g, b = colorsys.hls_to_rgb(h / 360.0, 0.92, 0.70)
        hex_color = f"{int(r * 255):02X}{int(g * 255):02X}{int(b * 255):02X}"
        fill = PatternFill("solid", fgColor=hex_color)
        _color_cache[code] = fill
        return fill

    def style_header_row(ws: Any, col_count: int) -> None:
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

    wb = Workbook()

    # ────────────────────────────────────────────────────────────
    # Sheet 1 / 1b / 1c: Schedule grids — All / M only / F only
    # ────────────────────────────────────────────────────────────
    _credit_map_sched = data.get("credit_map", {})

    # Rich-text inline fonts: course code stands out (bold, dark teal)
    # against the lighter, smaller, gray room codes underneath.
    _course_inline_font = InlineFont(rFont="Consolas", b=True, sz=10, color="064E3B")
    _room_inline_font = InlineFont(rFont="Consolas", b=False, sz=8, color="6B7280")

    _has_gender_data = any(
        section.get("gender") in ("M", "F")
        for sections in section_enrollment.values()
        for section in sections
    )

    def _render_schedule_sheet(ws: Any, gender_filter: str | None) -> None:
        """Filter attendance using the saved enrolment, including room gaps."""
        if gender_filter and not _has_gender_data:
            ws.append(
                [
                    "Gender enrolment was not recorded for this run. "
                    f"The {gender_filter} view cannot be determined."
                ]
            )
            ws.column_dimensions["A"].width = 90
            return
        grid_local: dict[str, dict[str, list[str]]] = {}
        rooms_by_entry: dict[tuple[str, str, str], list[str]] = {}
        for e in schedule:
            if e.get("day") == "OVERFLOW":
                continue
            all_rooms = e.get("rooms", [])
            if gender_filter:
                if not _gender_sections(e["course_code"], gender_filter):
                    continue
                matching = [a for a in all_rooms if a.get("gender") == gender_filter]
                room_codes = [a.get("room_code") or "UNASSIGNED" for a in matching]
            else:
                room_codes = [a.get("room_code") or "UNASSIGNED" for a in all_rooms]
            if not room_codes:
                room_codes = [
                    "Room assignment not requested"
                    if not data.get("assign_rooms")
                    else "UNASSIGNED"
                ]
            grid_local.setdefault(e["day"], {}).setdefault(e["period"], []).append(e["course_code"])
            if room_codes:
                rooms_by_entry[(e["day"], e["period"], e["course_code"])] = room_codes

        ws.append(["Day \\ Period"] + period_order)
        style_header_row(ws, 1 + len(period_order))

        _period_col_width = 32
        ws.column_dimensions["A"].width = 14
        for i, _p in enumerate(period_order, start=2):
            ws.column_dimensions[get_column_letter(i)].width = _period_col_width

        def _visual_lines(val: Any, col_width_chars: int) -> int:
            """How many wrapped lines a cell renders at in Excel.

            Flattens rich text to a single string and counts each segment
            (between \\n) plus its wrap overflow. Uses a size-10 chars-per-
            line — the size-8 room line actually fits more, so this
            slightly over-estimates wraps for rooms, which is the safe
            direction (row ends up a touch taller, never clipped).
            """
            if isinstance(val, CellRichText):
                text = "".join(str(b.text if isinstance(b, TextBlock) else b) for b in val)
            elif isinstance(val, str):
                text = val
            else:
                return 1
            if not text:
                return 1
            cpl = max(8, int(col_width_chars * 11 / 10))
            total = 0
            for seg in text.split("\n"):
                total += 1 + (max(0, len(seg) - 1) // cpl)
            return total

        def _course_cells(code: str, day: str, period: str) -> list[CellRichText]:
            """Keep one course per cell, continuing exceptionally long blocks.

            Excel allows at most 409.5 points per row. Twenty-two estimated
            text lines leave ample space below that limit without reducing
            the font size or hiding part of a large course's room list.
            """
            cr = _credit_map_sched.get(code, "")
            head = f"{code} {cr}cr" if cr else code
            if code in pinned_codes:
                head += " [FIXED]"
            if course_entries[code].get("is_online"):
                head += " [ONLINE]"
            chunks = [CellRichText([TextBlock(_course_inline_font, head)])]
            details = []
            name = course_entries[code].get("course_name")
            if name:
                details.append(str(name))
            rooms_line = rooms_by_entry.get((day, period, code), [])
            if rooms_line:
                details.append(", ".join(rooms_line))
            for detail in details:
                remaining = "\n" + detail
                while remaining:
                    current = chunks[-1]
                    low, high = 0, len(remaining)
                    # Fit the largest literal prefix, preserving all source
                    # characters across continuation cells.
                    while low < high:
                        middle = (low + high + 1) // 2
                        candidate = CellRichText(
                            [*current, TextBlock(_room_inline_font, remaining[:middle])]
                        )
                        if _visual_lines(candidate, _period_col_width) <= 22:
                            low = middle
                        else:
                            high = middle - 1
                    if low:
                        current.append(TextBlock(_room_inline_font, remaining[:low]))
                        remaining = remaining[low:]
                    if remaining:
                        chunks.append(
                            CellRichText(
                                [
                                    TextBlock(_course_inline_font, head + " [CONTINUED]\n"),
                                ]
                            )
                        )
            return chunks

        day_border = Side(style="medium", color="0A8E6E")
        for day in day_order:
            period_cells = {
                period: [
                    (code, content)
                    for code in sorted(grid_local.get(day, {}).get(period, []))
                    for content in _course_cells(code, day, period)
                ]
                for period in period_order
            }
            row_count = max([1, *(len(cells) for cells in period_cells.values())])
            first_row = ws.max_row + 1
            for offset in range(row_count):
                row_idx = first_row + offset
                max_lines = 1
                for pi, period in enumerate(period_order):
                    cell = ws.cell(row=row_idx, column=2 + pi)
                    cell.border = Border(
                        left=thin_border.left,
                        right=thin_border.right,
                        top=day_border if offset == 0 else thin_border.top,
                        bottom=day_border if offset == row_count - 1 else thin_border.bottom,
                    )
                    cell.alignment = center
                    if offset < len(period_cells[period]):
                        code, content = period_cells[period][offset]
                        cell.value = content
                        cell.fill = _course_color_fill(code)
                        max_lines = max(max_lines, _visual_lines(content, _period_col_width))
                ws.row_dimensions[row_idx].height = max_lines * 15 + 6
                # Repeat the day so every row retains context across printed
                # page breaks and viewers with limited merged-cell support.
                day_cell = ws.cell(row=row_idx, column=1, value=day)
                day_cell.font = header_font
                day_cell.border = Border(
                    left=thin_border.left,
                    right=day_border,
                    top=day_border if offset == 0 else thin_border.top,
                    bottom=day_border if offset == row_count - 1 else thin_border.bottom,
                )
                day_cell.alignment = center
                day_cell.fill = PatternFill("solid", fgColor="E8F5E9")

        overflow = [
            e
            for e in schedule
            if e.get("day") == "OVERFLOW"
            and (not gender_filter or _gender_sections(e["course_code"], gender_filter))
        ]
        if overflow:
            ws.append([])
            ws.append(["UNSCHEDULED EXAMS — action required"])
            ws.cell(ws.max_row, 1).font = header_font
            for entry in overflow:
                label = _course_label(entry["course_code"])
                overflow_cells = (
                    _course_cells(entry["course_code"], entry["day"], entry["period"])
                    if _visual_lines(label, _period_col_width) > 22
                    else [label]
                )
                for content in overflow_cells:
                    ws.append([entry["day"], content])
                    for cell in ws[ws.max_row]:
                        cell.fill = danger_fill
                        cell.alignment = left_align
                    ws.row_dimensions[ws.max_row].height = (
                        _visual_lines(content, _period_col_width) * 15 + 6
                    )
        ws.freeze_panes = "B2"
        ws.print_title_rows = "1:1"
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_setup.orientation = "landscape"
        ws.page_setup.paperSize = ws.PAPERSIZE_A3
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0

    ws1 = wb.active
    ws1.title = "Schedule"
    _render_schedule_sheet(ws1, None)
    _render_schedule_sheet(wb.create_sheet("Schedule (M)"), "M")
    _render_schedule_sheet(wb.create_sheet("Schedule (F)"), "F")

    # ────────────────────────────────────────────────────────────
    # Sheet 2: Courses (flat list)
    # ────────────────────────────────────────────────────────────
    ws2 = wb.create_sheet("Courses")
    _credit_map = data.get("credit_map", {})
    ws2.append(
        [
            "Course Code",
            "Credits",
            "Day",
            "Period",
            "Slot Index",
            "Course Name",
            "Programmes",
            "Enrolled Students",
            "Online",
            "Fixed Time",
            "Source Course Code",
        ]
    )
    style_header_row(ws2, 11)

    sorted_schedule = sorted(schedule, key=lambda e: (e.get("slot_index", 999), e["course_code"]))
    for e in sorted_schedule:
        cr = _credit_map.get(e["course_code"], "")
        ws2.append(
            [
                e["course_code"],
                cr,
                e["day"],
                e["period"],
                e.get("slot_index", ""),
                e.get("course_name", ""),
                ", ".join(e.get("programs", [])),
                e.get("enrolled_count", ""),
                "Yes" if e.get("is_online") else "No",
                "Yes" if e["course_code"] in pinned_codes else "No",
                e.get("source_course_code", e["course_code"]),
            ]
        )

    for r in range(2, ws2.max_row + 1):
        code_val = ws2.cell(row=r, column=1).value
        for c in range(1, 12):
            cell = ws2.cell(row=r, column=c)
            cell.border = thin_border
            cell.alignment = center
            if code_val:
                cell.fill = _course_color_fill(code_val)

    ws2.column_dimensions["A"].width = 16
    ws2.column_dimensions["B"].width = 10
    ws2.column_dimensions["C"].width = 14
    ws2.column_dimensions["D"].width = 18
    ws2.column_dimensions["E"].width = 12
    ws2.column_dimensions["F"].width = 42
    ws2.column_dimensions["G"].width = 24
    for column_letter in ("H", "I", "J", "K"):
        ws2.column_dimensions[column_letter].width = 18
    ws2.freeze_panes = "B2"
    ws2.auto_filter.ref = ws2.dimensions

    # ────────────────────────────────────────────────────────────
    # Sheet 2b / 2c: Students (M) / Students (F) — per-course gender totals
    # ────────────────────────────────────────────────────────────
    def _render_students_sheet(ws: Any, gender_filter: str) -> None:
        if not _has_gender_data:
            ws.append(
                [
                    "Gender enrolment was not recorded for this run. "
                    f"The {gender_filter} view cannot be determined."
                ]
            )
            ws.column_dimensions["A"].width = 90
            return

        ws.append(
            [
                "Course Code",
                "Credits",
                "Day",
                "Period",
                "Enrolled Sections",
                "Exam Enrolments",
                "Course Name",
                "Fixed Time",
                "Missing Section Enrolments",
                "Ambiguous Section Enrolments",
            ]
        )
        style_header_row(ws, 10)

        rows: list[list[Any]] = []
        for e in sorted_schedule:
            matching = _gender_sections(e["course_code"], gender_filter)
            if not matching:
                continue
            sections = len(
                {
                    str(a.get("section_key") or a.get("section") or "")
                    for a in matching
                    if a.get("mapping_status", "mapped") == "mapped"
                }
            )
            students = sum(int(a.get("student_count", 0) or 0) for a in matching)
            missing = sum(
                int(a.get("student_count", 0) or 0)
                for a in matching
                if a.get("mapping_status") == "missing"
            )
            ambiguous = sum(
                int(a.get("student_count", 0) or 0)
                for a in matching
                if a.get("mapping_status") == "ambiguous"
            )
            cr = _credit_map.get(e["course_code"], "")
            rows.append(
                [
                    e["course_code"],
                    cr,
                    e["day"],
                    e["period"],
                    sections,
                    students,
                    e.get("course_name", ""),
                    "Yes" if e["course_code"] in pinned_codes else "No",
                    missing,
                    ambiguous,
                ]
            )

        for row in rows:
            ws.append(list(row))

        for r in range(2, ws.max_row + 1):
            code_val = ws.cell(row=r, column=1).value
            for c in range(1, 11):
                cell = ws.cell(row=r, column=c)
                cell.border = thin_border
                cell.alignment = center
                if code_val:
                    cell.fill = _course_color_fill(code_val)

        if rows:
            total_sections = sum(r[4] for r in rows)
            total_students = sum(r[5] for r in rows)
            total_row = ws.max_row + 1
            ws.cell(row=total_row, column=1, value="TOTAL EXAM ENROLMENTS").font = header_font
            ws.cell(row=total_row, column=5, value=total_sections).font = header_font
            ws.cell(row=total_row, column=6, value=total_students).font = header_font
            ws.cell(row=total_row, column=9, value=sum(r[8] for r in rows)).font = header_font
            ws.cell(row=total_row, column=10, value=sum(r[9] for r in rows)).font = header_font
            for c in range(1, 11):
                cell = ws.cell(row=total_row, column=c)
                cell.border = thin_border
                cell.alignment = center
                cell.fill = PatternFill("solid", fgColor="E8F5E9")
        else:
            ws.append([f"No {gender_filter} enrolments in this timetable", "", "", "", 0, 0])
            ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=4)
            for cell in ws[2]:
                cell.border = thin_border
                cell.alignment = left_align

        ws.column_dimensions["A"].width = 16
        ws.column_dimensions["B"].width = 10
        ws.column_dimensions["C"].width = 14
        ws.column_dimensions["D"].width = 18
        ws.column_dimensions["E"].width = 12
        ws.column_dimensions["F"].width = 12
        ws.column_dimensions["G"].width = 42
        ws.column_dimensions["H"].width = 14
        ws.column_dimensions["I"].width = 22
        ws.column_dimensions["J"].width = 22
        ws.freeze_panes = "B2"
        ws.auto_filter.ref = f"A1:J{1 + len(rows)}"

    _render_students_sheet(wb.create_sheet("Students (M)"), "M")
    _render_students_sheet(wb.create_sheet("Students (F)"), "F")

    # ────────────────────────────────────────────────────────────
    # Sheet 3: QA Summary
    # ────────────────────────────────────────────────────────────
    ws3 = wb.create_sheet("QA Summary")
    room_qa = qa.get("rooms") if isinstance(qa, dict) else None

    # Key metrics as label-value pairs
    metrics = [
        ("Label", run.label),
        ("Run ID", run_id),
        ("Status", _status_label(str(data.get("primary_status", status) or ""))),
        (
            "Status Flags",
            ", ".join(_status_label(str(flag)) for flag in data.get("status_flags", [])),
        ),
        ("Programmes in Scope", ", ".join(data.get("enrollment_scope", {}).get("programs", []))),
        ("Sections in Scope", ", ".join(data.get("enrollment_scope", {}).get("sections", []))),
        ("Total Courses", qa.get("total_courses", data.get("courses_count", 0))),
        ("Unique Students", qa.get("total_students", data.get("students_count", 0))),
        ("Fixed Exams", len(pinned_codes)),
        ("Online Courses", sum(bool(e.get("is_online")) for e in schedule)),
        ("Unscheduled Courses", sum(e.get("day") == "OVERFLOW" for e in schedule)),
        ("Room Assignment Requested", "Yes" if data.get("assign_rooms") else "No"),
        ("Split Sections", qa.get("multi_sitting_sections", 0)),
        ("Unassigned Room Groups", len((room_qa or {}).get("unassigned_room_sections", []))),
        ("Room Double Bookings", len((room_qa or {}).get("room_double_bookings", []))),
        ("Thin Conflict Threshold", qa.get("thin_threshold", 0)),
        ("Thin Clash Records", len(qa.get("thin_clash_risk", []))),
        (
            "Schedule Violations",
            qa.get("schedule_violation_count", qa.get("manual_override_count", 0)),
        ),
        ("Slots Used", qa.get("slots_used", 0)),
        ("Max Exams/Day/Student", qa.get("max_exams_per_day_per_student", 0)),
        ("Max Per Day Cap", qa.get("max_per_day", 2)),
        ("Students Over Limit", qa.get("students_over_limit_per_day", 0)),
        ("Max Credit Load/Day", qa.get("max_credit_load_per_day", 0)),
        ("Heavy Day Students", qa.get("heavy_day_students", 0)),
        ("Same-Slot Conflicts", qa.get("conflict_count", 0)),
        ("Programme Buckets", qa.get("bucket_count", 0)),
        ("Bucket Day Violations", qa.get("bucket_day_violations_count", 0)),
        ("Approved-Only Student Clashes", qa.get("approved_thin_conflict_count", 0)),
        ("Hard Student Clashes", qa.get("hard_conflict_count", qa.get("conflict_count", 0))),
    ]
    section_mapping = qa.get("section_mapping") or {}
    if section_mapping:
        metrics.extend(
            [
                ("Recorded Teaching Sections", section_mapping.get("mapped_sections", 0)),
                ("Mapped Section Enrolments", section_mapping.get("mapped_enrollments", 0)),
                ("Missing Section Enrolments", section_mapping.get("missing_enrollments", 0)),
                ("Ambiguous Section Enrolments", section_mapping.get("ambiguous_enrollments", 0)),
            ]
        )

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
        if (
            metric_name
            in (
                "Same-Slot Conflicts",
                "Unscheduled Courses",
                "Unassigned Room Groups",
                "Room Double Bookings",
            )
            and metric_val
            and int(metric_val) > 0
        ):
            ws3.cell(row=r, column=1).fill = danger_fill
            ws3.cell(row=r, column=2).fill = danger_fill
        elif metric_name in (
            "Students Over Limit",
            "Bucket Day Violations",
            "Heavy Day Students",
            "Missing Section Enrolments",
            "Ambiguous Section Enrolments",
        ):
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
            ws3.append(
                [
                    conflict.get("student_id", ""),
                    conflict.get("slot_index", ""),
                    ", ".join(_course_label(code) for code in conflict.get("courses", [])),
                ]
            )

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
            ws3.append(
                [
                    v.get("program", ""),
                    v.get("programme_term", ""),
                    v.get("day", ""),
                    ", ".join(_course_label(code) for code in v.get("courses", [])),
                ]
            )

    ws3.column_dimensions["A"].width = 26
    ws3.column_dimensions["B"].width = 16
    ws3.column_dimensions["C"].width = 18
    ws3.column_dimensions["D"].width = 30
    ws3.column_dimensions["A"].width = 32
    ws3.column_dimensions["B"].width = 48
    ws3.column_dimensions["C"].width = 55
    for title, details in (
        ("Students Over Daily Limit", qa.get("overload_details", [])),
        ("Heavy Credit Days", qa.get("heavy_day_details", [])),
    ):
        if not details:
            continue
        ws3.append([])
        ws3.append([title])
        ws3.cell(ws3.max_row, 1).font = header_font
        ws3.append(["Student ID", "Day", "Courses"])
        for detail in details:
            ws3.append(
                [
                    detail.get("student_id", ""),
                    detail.get("day", ""),
                    ", ".join(_course_label(c["code"]) for c in detail.get("courses", [])),
                ]
            )
    if qa.get("multi_sitting_details"):
        ws3.append([])
        ws3.append(["Split Section Details"])
        ws3.cell(ws3.max_row, 1).font = header_font
        for detail in qa["multi_sitting_details"]:
            ws3.append([_section_label(detail), detail.get("audit_text", "")])
    if data.get("pinned"):
        ws3.append([])
        ws3.append(["Fixed Exam Times"])
        ws3.cell(ws3.max_row, 1).font = header_font
        ws3.append(["Course", "Day", "Period"])
        for pin in data["pinned"]:
            ws3.append([_course_label(pin["course_code"]), pin["day"], pin["period"]])
    for title, headers, details, row_builder in (
        (
            "Thin Clash Risk Details",
            ["Student ID", "Slot Index", "Courses"],
            qa.get("thin_clash_risk", []),
            lambda detail: [
                detail.get("student_id", ""),
                detail.get("slot_index", ""),
                ", ".join(_course_label(code) for code in detail.get("courses", [])),
            ],
        ),
        (
            "Unassigned Room Details",
            [
                "Course",
                "Day",
                "Period",
                "Section",
                "Students",
                "Gender",
                "Room Group",
                "Section Mapping",
            ],
            (room_qa or {}).get("unassigned_room_sections", []),
            lambda detail: [
                _course_label(detail.get("course_code", "")),
                detail.get("day", ""),
                detail.get("period", ""),
                _room_section_label(detail),
                detail.get("student_count", 0),
                detail.get("gender", ""),
                detail.get("room_group", ""),
                _room_mapping_label(detail),
            ],
        ),
        (
            "Room Double Booking Details",
            ["Room", "Slot Index", "Courses"],
            (room_qa or {}).get("room_double_bookings", []),
            lambda detail: [
                detail.get("room_code", ""),
                detail.get("slot_index", ""),
                ", ".join(_course_label(code) for code in detail.get("courses", [])),
            ],
        ),
        (
            "Room Capacity Warnings",
            ["Course", "Section", "Gender", "Students", "Largest Room Capacity", "Section Mapping"],
            qa.get("room_feasibility_violations", []),
            lambda detail: [
                _course_label(detail.get("course_code", "")),
                _section_label(detail),
                detail.get("gender", ""),
                detail.get("student_count", 0),
                detail.get("max_room_capacity", 0),
                _section_mapping_label(detail),
            ],
        ),
        (
            "Section Mapping Gaps",
            ["Course", "Gender", "Section Mapping", "Students", "Reason"],
            section_mapping.get("details", []),
            lambda detail: [
                _course_label(detail.get("course_code", "")),
                detail.get("gender", ""),
                _section_mapping_label(detail),
                detail.get("student_count", 0),
                {
                    "no_recorded_section": "No registered teaching section was found.",
                    "multiple_recorded_sections": "More than one registered teaching section matches.",
                }.get(detail.get("reason", ""), detail.get("reason", "")),
            ],
        ),
    ):
        if not details:
            continue
        ws3.append([])
        ws3.append([title])
        ws3.cell(ws3.max_row, 1).font = header_font
        ws3.append(headers)
        for detail in details:
            ws3.append(row_builder(detail))
    ws3.column_dimensions["E"].width = 20
    ws3.column_dimensions["F"].width = 14
    ws3.column_dimensions["G"].width = 20
    ws3.column_dimensions["H"].width = 28

    # ────────────────────────────────────────────────────────────
    # Sheet 4: Room Assignments (one row per physical room allocation)
    # ────────────────────────────────────────────────────────────
    has_room_data = any(e.get("rooms") for e in schedule if e.get("day") != "OVERFLOW")
    if has_room_data:
        ws4 = wb.create_sheet("Room Assignments")
        ws4.append(
            [
                "Day",
                "Period",
                "Course",
                "Section",
                "Gender",
                "Students",
                "Room",
                "Capacity",
                "Utilization",
                "Course Name",
                "Fixed Time",
                "Building",
                "Floor",
                "Room Group",
                "Section Mapping",
            ]
        )
        style_header_row(ws4, 15)

        # Sort by slot then course for a stable, readable ordering
        sorted_for_rooms = sorted(
            (e for e in schedule if e.get("day") != "OVERFLOW"),
            key=lambda e: (e.get("slot_index", 999), e["course_code"]),
        )
        for e in sorted_for_rooms:
            for a in e.get("rooms", []) or []:
                cap = int(a.get("room_capacity", 0) or 0)
                cnt = int(a.get("student_count", 0) or 0)
                util = cnt / cap if cap else None
                ws4.append(
                    [
                        e["day"],
                        e["period"],
                        e["course_code"],
                        _room_section_label(a),
                        a.get("gender", ""),
                        cnt,
                        a.get("room_code", ""),
                        cap if cap else "",
                        util,
                        e.get("course_name", ""),
                        "Yes" if e["course_code"] in pinned_codes else "No",
                        a.get("building", ""),
                        a.get("floor", ""),
                        a.get("room_group", ""),
                        _room_mapping_label(a),
                    ]
                )
                ws4.cell(ws4.max_row, 9).number_format = "0%"

        for r in range(2, ws4.max_row + 1):
            code_val = ws4.cell(row=r, column=3).value
            for c in range(1, 16):
                cell = ws4.cell(row=r, column=c)
                cell.border = thin_border
                cell.alignment = center
            if code_val:
                ws4.cell(row=r, column=3).fill = _course_color_fill(str(code_val))
            room_val = ws4.cell(row=r, column=7).value
            if room_val == "UNASSIGNED":
                for c in range(1, 16):
                    ws4.cell(row=r, column=c).fill = danger_fill

        ws4.column_dimensions["A"].width = 10
        ws4.column_dimensions["B"].width = 16
        ws4.column_dimensions["C"].width = 14
        ws4.column_dimensions["D"].width = 22
        ws4.column_dimensions["E"].width = 10
        ws4.column_dimensions["F"].width = 12
        ws4.column_dimensions["G"].width = 14
        ws4.column_dimensions["H"].width = 12
        ws4.column_dimensions["I"].width = 14
        ws4.column_dimensions["J"].width = 42
        ws4.column_dimensions["K"].width = 14
        ws4.column_dimensions["L"].width = 18
        ws4.column_dimensions["M"].width = 10
        ws4.column_dimensions["N"].width = 24
        ws4.column_dimensions["O"].width = 32
        ws4.freeze_panes = "D2"
        ws4.auto_filter.ref = ws4.dimensions

        # Append the room QA summary on the same sheet below the table
        if isinstance(room_qa, dict):
            ws4.append([])
            ws4.append(["Room QA Summary"])
            ws4.cell(row=ws4.max_row, column=1).font = header_font
            room_metrics: list[tuple[str, Any]] = [
                ("Rooms Available", room_qa.get("rooms_available", 0)),
                ("Rooms Used (slot × room)", room_qa.get("rooms_used", 0)),
                ("Assigned Students", room_qa.get("total_demand", 0)),
                ("Capacity Used", room_qa.get("total_capacity_used", 0)),
                (
                    "Avg Utilization",
                    float(room_qa.get("avg_utilization", 0) or 0),
                ),
                (
                    "Unassigned Sections",
                    len(room_qa.get("unassigned_room_sections", []) or []),
                ),
                (
                    "Double Bookings",
                    len(room_qa.get("room_double_bookings", []) or []),
                ),
            ]
            for lbl, val in room_metrics:
                ws4.append([lbl, val])
                rr = ws4.max_row
                ws4.cell(row=rr, column=1).font = header_font
                if lbl == "Avg Utilization":
                    ws4.cell(rr, 2).number_format = "0.0%"
                for c in range(1, 3):
                    ws4.cell(row=rr, column=c).border = thin_border

    # ────────────────────────────────────────────────────────────
    # Sheet 5: Invigilators — per-day totals (M/F) + per-room detail
    # ────────────────────────────────────────────────────────────
    if has_room_data:
        ws5 = wb.create_sheet("Invigilators")

        # ── Section A: rules legend ──
        ws5.append(["Invigilator Rules"])
        ws5.cell(row=1, column=1).font = Font(bold=True, size=12)
        ws5.append(
            [
                "Department courses (CS/IS/COE/CYB/AI/DS):  1 invigilator if room has <30 students, "
                "2 if 30+. Other unlisted course prefixes use this department rule."
            ]
        )
        ws5.append(
            [
                "External courses (GS/EDCT/GSE/ENV/MATH/STAT/PHYS):  1 invigilator only if room "
                "has more than 30 students, 0 otherwise"
            ]
        )
        for note_row in (1, 2, 3):
            ws5.merge_cells(start_row=note_row, start_column=1, end_row=note_row, end_column=13)
            ws5.cell(note_row, 1).alignment = left_align
            ws5.row_dimensions[note_row].height = 30 if note_row > 1 else 24
        ws5.append([])

        # ── Section B: daily summary ──
        ws5.append(["Day", "M Invigilators", "F Invigilators", "Total"])
        style_header_row_at = ws5.max_row
        for col in range(1, 5):
            cell = ws5.cell(row=style_header_row_at, column=col)
            cell.font = header_font_white
            cell.fill = header_fill
            cell.alignment = center
            cell.border = thin_border

        # Iterate days in slot order so the table matches the schedule grid
        invig_per_day_data = (
            (room_qa or {}).get("invigilators_per_day", {}) if isinstance(room_qa, dict) else {}
        )
        # Re-derive on the fly from the schedule too in case room_qa wasn't computed
        if not invig_per_day_data:
            tally: dict[str, dict[str, int]] = {}
            for e in schedule:
                if e.get("day") == "OVERFLOW":
                    continue
                day = e["day"]
                day_t = tally.setdefault(day, {"M": 0, "F": 0, "total": 0})
                for a in e.get("rooms", []) or []:
                    if a.get("room_code") == "UNASSIGNED":
                        continue
                    invigs = _invigilators_needed(
                        e["course_code"], int(a.get("student_count", 0) or 0)
                    )
                    g = a.get("gender", "M")
                    day_t[g] = day_t.get(g, 0) + invigs
                    day_t["total"] += invigs
            invig_per_day_data = tally

        running_M = 0
        running_F = 0
        for day in day_order:
            counts = invig_per_day_data.get(day, {"M": 0, "F": 0, "total": 0})
            ws5.append([day, counts["M"], counts["F"], counts["total"]])
            running_M += counts["M"]
            running_F += counts["F"]
            r = ws5.max_row
            for col in range(1, 5):
                ws5.cell(row=r, column=col).border = thin_border
                ws5.cell(row=r, column=col).alignment = center

        # Grand total row
        ws5.append(["TOTAL", running_M, running_F, running_M + running_F])
        r = ws5.max_row
        for col in range(1, 5):
            cell = ws5.cell(row=r, column=col)
            cell.font = Font(bold=True)
            cell.border = thin_border
            cell.alignment = center
            cell.fill = PatternFill("solid", fgColor="E0E0E0")

        ws5.column_dimensions["A"].width = 18
        ws5.column_dimensions["B"].width = 18
        ws5.column_dimensions["C"].width = 18
        ws5.column_dimensions["D"].width = 14

        # ── Section C: per-room detail ──
        ws5.append([])
        ws5.append(
            [
                "Day",
                "Period",
                "Course",
                "Type",
                "Section",
                "Gender",
                "Students",
                "Room",
                "Invigilators",
                "Course Name",
                "Fixed Time",
                "Room Group",
                "Section Mapping",
            ]
        )
        detail_header_row = ws5.max_row
        for col in range(1, 14):
            cell = ws5.cell(row=detail_header_row, column=col)
            cell.font = header_font_white
            cell.fill = header_fill
            cell.alignment = center
            cell.border = thin_border

        sorted_for_invig = sorted(
            (e for e in schedule if e.get("day") != "OVERFLOW"),
            key=lambda e: (e.get("slot_index", 999), e["course_code"]),
        )
        for e in sorted_for_invig:
            cc = e["course_code"]
            prefix = _course_prefix(cc)
            ctype = (
                "External"
                if prefix in _EXTERNAL_PREFIXES
                else "Department"
                if prefix in _DEPARTMENT_PREFIXES
                else "Other (department rule)"
            )
            for a in e.get("rooms", []) or []:
                if a.get("room_code") == "UNASSIGNED":
                    continue
                stu = int(a.get("student_count", 0) or 0)
                invigs = _invigilators_needed(cc, stu)
                ws5.append(
                    [
                        e["day"],
                        e["period"],
                        cc,
                        ctype,
                        _room_section_label(a),
                        a.get("gender", ""),
                        stu,
                        a.get("room_code", ""),
                        invigs,
                        e.get("course_name", ""),
                        "Yes" if cc in pinned_codes else "No",
                        a.get("room_group", ""),
                        _room_mapping_label(a),
                    ]
                )
                rr = ws5.max_row
                for col in range(1, 14):
                    ws5.cell(row=rr, column=col).border = thin_border
                    ws5.cell(row=rr, column=col).alignment = center
                # Highlight rows with 0 invigilators (no department staffing needed)
                if invigs == 0:
                    for col in range(1, 14):
                        ws5.cell(row=rr, column=col).fill = PatternFill("solid", fgColor="EDEDED")
                # Highlight rows with 2 invigilators (heavy room)
                elif invigs >= 2:
                    for col in range(1, 14):
                        ws5.cell(row=rr, column=col).fill = warn_fill

        ws5.column_dimensions["E"].width = 22
        ws5.column_dimensions["F"].width = 10
        ws5.column_dimensions["G"].width = 10
        ws5.column_dimensions["H"].width = 14
        ws5.column_dimensions["I"].width = 14
        ws5.column_dimensions["J"].width = 42
        ws5.column_dimensions["K"].width = 14
        ws5.column_dimensions["L"].width = 24
        ws5.column_dimensions["M"].width = 32
        # Rules and the daily summary can occupy most of a laptop screen.
        # Let the whole report scroll instead of freezing that entire block.
        ws5.freeze_panes = None

    # ── Write to disk ──
    # All exported values are literal saved data. A course name or run label
    # beginning with '=' must not turn into an executable Excel formula.
    for sheet in wb:
        sheet.print_area = sheet.dimensions
        if sheet.title not in ("Schedule", "Schedule (M)", "Schedule (F)"):
            sheet.sheet_properties.pageSetUpPr.fitToPage = True
            sheet.page_setup.orientation = "landscape"
            # Full course names and room detail make these wide tables.
            # A3 leaves usable type at one page across; rows continue
            # vertically instead of forcing a whole report onto one page.
            sheet.page_setup.paperSize = sheet.PAPERSIZE_A3
            sheet.page_setup.fitToWidth = 1
            sheet.page_setup.fitToHeight = 0
            if sheet.title in ("Courses", "Students (M)", "Students (F)", "Room Assignments"):
                sheet.print_title_rows = "1:1"
            elif sheet.title == "QA Summary":
                sheet.freeze_panes = "B2"
        for row in sheet:
            for cell in row:
                if cell.data_type == "f":
                    cell.data_type = "s"
                if cell.value is not None:
                    cell.alignment = Alignment(
                        horizontal=cell.alignment.horizontal or "left",
                        vertical="center",
                        wrap_text=True,
                    )
            if sheet.title not in ("Schedule", "Schedule (M)", "Schedule (F)"):
                # Explicit heights travel reliably with wrapped text when
                # opened or printed by Excel, including long course names.
                row_index = row[0].row
                fitted_height = 21.0
                for cell in row:
                    if cell.value is None:
                        continue
                    width = sheet.column_dimensions[cell.column_letter].width or 13
                    for merged_range in sheet.merged_cells.ranges:
                        if cell.coordinate in merged_range:
                            width = sum(
                                sheet.column_dimensions[get_column_letter(col)].width or 13
                                for col in range(merged_range.min_col, merged_range.max_col + 1)
                            )
                            break
                    # Allow for word wrapping and padding at the default
                    # 11-point table font, without shrinking source text.
                    line_width = max(8, int(width * 0.9))
                    lines = sum(
                        max(1, math.ceil(len(part) / line_width))
                        for part in str(cell.value).split("\n")
                    )
                    fitted_height = max(fitted_height, lines * 15 + 6)
                sheet.row_dimensions[row_index].height = min(
                    409.5,
                    max(sheet.row_dimensions[row_index].height or 0, fitted_height),
                )
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    # Each request owns its artifact. In particular, Windows cannot replace
    # a fixed output path while another FileResponse is still reading it.
    # The download view closes and removes this file after serving it.
    import tempfile

    with tempfile.NamedTemporaryFile(
        dir=RUNTIME_DIR,
        prefix=f"exam_timetable_{run_id}_",
        suffix=".xlsx",
        delete=False,
    ) as temp:
        temporary_path = Path(temp.name)
    try:
        wb.save(str(temporary_path))
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path

"""
core/services/timetable_generate.py
Cohort-based timetable workspace generation.

This module is the main entry point for creating a fully scaffolded
TimetableScenario from scratch.  The pipeline:

  1. Run the batch recommender for every student in the target program(s)
     to determine which courses each student should take next semester.
  2. Filter out non-schedulable courses (capstone, training, free electives).
  3. Classify each student by their "primary term" — the curriculum term
     (e.g. Term 3, Term 5) where the majority of their recommended courses
     fall.  Students whose courses span multiple terms are flagged as
     "cross-term".
  4. Compute a section plan (how many sections of each course are needed)
     using demand counts and capacity rules from section_planning.
  5. Create the TimetableScenario, one DeliveryBoard per active term level,
     and persist:
       - ScenarioStudentMap  (student classification records)
       - ScenarioSectionBudget  (planned sections per course)
       - BoardStudentLink  (primary + visitor links between students
         and boards)
  6. Run the auto-placement engine to produce an initial draft timetable.

The single public function is ``generate_workspace_scenario()``.

Dependencies:
  - core.services.recommender_batch  (batch_recommend, batch_recommend_multi_program)
  - core.services.section_planning   (capacity rules, section plan computation)
  - core.services.timetable_autoplace (auto_place_scenario, get_meeting_pattern)
  - core.services.timetable_workspace (compute_scenario_budget)
"""

from __future__ import annotations

from collections import Counter, defaultdict

from core.models import (
    BoardStudentLink,
    DeliveryBoard,
    ProgrammeRequirement,
    ScenarioSectionBudget,
    ScenarioStudentMap,
    SectionPlacement,
    TimeSlotTemplate,
    TimetableScenario,
)
from core.services.reporting import get_student_ids
from core.services.section_planning import (
    DEFAULT_MAX_EXTERNAL,
    DEFAULT_MAX_LOCAL_4CR,
    DEFAULT_MAX_LOCAL_OTHER,
    compute_plan_summary,
    compute_section_plan,
    load_programme_capacities,
)
from core.services.student_helpers import normalize_code

# ---------------------------------------------------------------------------
# Course exclusion rules
# ---------------------------------------------------------------------------
# Certain courses must never appear on a timetable board because they are
# not delivered in a regular classroom setting.  Three exclusion layers:
#
# 1. EXCLUDED_COURSE_CODES — explicit course codes (currently empty; can be
#    populated with codes like "FE1", "FE2" for free-elective placeholders).
# 2. EXCLUDED_SUFFIXES — course numbers ending with these digits are
#    capstone / graduation-project sequences (e.g. CS490, CS491, CS492).
# 3. EXCLUDED_NAME_KEYWORDS / EXCLUDED_NAME_PATTERNS_PROJECT — fuzzy match
#    against the course description (English and Arabic) to catch training,
#    cooperative, internship, and graduation-project courses.
#
# The helper ``_is_excluded_course()`` applies all three layers.
# ---------------------------------------------------------------------------
EXCLUDED_COURSE_CODES: frozenset[str] = frozenset()  # None currently hardcoded
EXCLUDED_SUFFIXES = ("490", "491", "492")
EXCLUDED_NAME_KEYWORDS = frozenset(
    {
        "graduation",
        "practical training",
        "training",
        "cooperative",
        "co-operative",
        "coop",
        "internship",
        "تخرج",
        "مشروع تخرج",
        "تدريب",
    }
)
# Patterns that indicate a capstone/project course.
# Note: plain "project" is intentionally omitted to avoid false-positives
# like "Project Management" — only multi-word capstone patterns are listed.
EXCLUDED_NAME_PATTERNS_PROJECT = ("project i", "project ii", "graduation project")

# Lazy-loaded mapping of course_code (upper-case) to lower-cased course
# description.  Populated once by ``_load_course_names()`` and reused for
# the lifetime of the process to avoid repeated DB queries.
_course_name_cache: dict[str, str] | None = None


def _load_course_names() -> dict[str, str]:
    """Return a mapping of normalised course codes to lower-cased descriptions.

    The result is cached module-globally so the Course table is only queried
    once per process.  Used by ``_is_excluded_course()`` to perform
    keyword-based exclusion checks against course descriptions.

    Returns:
        dict mapping upper-cased course codes to lower-cased description
        strings (empty string when no description is stored).
    """
    global _course_name_cache
    if _course_name_cache is None:
        from core.models import Course

        _course_name_cache = {
            c.course_code.upper(): (c.description or "").lower() for c in Course.objects.all()
        }
    return _course_name_cache


def _is_excluded_course(code: str) -> bool:
    """Determine whether a course should be excluded from timetable scheduling.

    A course is excluded if **any** of the following are true:

    1. Its normalised code is in the ``EXCLUDED_COURSE_CODES`` set.
    2. The numeric portion of the code ends with one of the
       ``EXCLUDED_SUFFIXES`` (490, 491, 492 — capstone sequences).
    3. The course description (from the Course model) contains any of the
       ``EXCLUDED_NAME_KEYWORDS`` or ``EXCLUDED_NAME_PATTERNS_PROJECT``.

    Args:
        code: Raw course code string (e.g. "CS 490", "ARAB101").

    Returns:
        True if the course should be kept off the timetable, False otherwise.
    """
    c = normalize_code(code)

    # Layer 1: explicit code blacklist
    if c in EXCLUDED_COURSE_CODES:
        return True

    # Layer 2: numeric suffix check (e.g. "490" in "CS490")
    digits = "".join(ch for ch in c if ch.isdigit())
    if digits.endswith(EXCLUDED_SUFFIXES):
        return True

    # Layer 3: keyword / pattern match against course description
    names = _load_course_names()
    name = names.get(c, "")
    if name:
        if any(kw in name for kw in EXCLUDED_NAME_KEYWORDS):
            return True
        if any(pat in name for pat in EXCLUDED_NAME_PATTERNS_PROJECT):
            return True
    return False


def generate_workspace_scenario(
    year: int,
    semester: int,
    program: str | list[str],
    section: str | None = None,
    scenario_name: str = "",
    strategy: str = "compact",
    max_local_4cr: int = DEFAULT_MAX_LOCAL_4CR,
    max_local_other: int = DEFAULT_MAX_LOCAL_OTHER,
    max_external: int = DEFAULT_MAX_EXTERNAL,
    course_overrides: dict[str, int] | None = None,
    created_by: str = "",
) -> dict:
    """Create a fully scaffolded timetable workspace scenario end-to-end.

    This is the main orchestrator.  It runs the full pipeline described in
    the module docstring: recommend -> classify -> plan sections -> create
    scenario/boards -> persist records -> auto-place.

    Args:
        year: Academic year (e.g. 1446).
        semester: Semester number (1, 2, or 3 for summer).
        program: Single programme code (``"AI"``) **or** a list of codes
            (``["AI", "DS", "CS"]``) for multi-program generation.
        section: Optional section filter (e.g. ``"1"``).  When provided,
            only students in that section are included.
        scenario_name: Human-readable label for the scenario.  Auto-
            generated from program/semester/timestamp when left blank.
        max_local_4cr: Max students per section for local 4-credit courses.
        max_local_other: Max students per section for local non-4-credit
            courses.
        max_external: Max students per section for external (service)
            courses.
        course_overrides: Optional dict mapping course codes to explicit
            section counts, bypassing the demand-based computation.
        created_by: Username or identifier stored on the scenario record.

    Returns:
        A dict containing:
          - ``scenario``: metadata dict (id, year, term, name, status, ...).
          - ``boards``: list of board summary dicts, one per term level,
            each with student counts, courses, and auto-placement stats.
          - ``section_budget``: enriched budget with meeting-pattern info.
          - ``section_plan``: raw section plan from ``compute_section_plan``.
          - ``plan_summary``: aggregate section-plan statistics.
          - ``student_summary``: counts of total, classified, on-plan,
            and cross-term students broken down by term level.

    Business rules:
      - Non-schedulable courses (capstone, training, co-op) are filtered out
        before any section planning — see ``_is_excluded_course()``.
      - A student's **primary term** is the curriculum term that appears most
        often among their recommended courses.  They are linked to that
        term's board as ``"primary"``.
      - Cross-term students also receive ``"visitor"`` links to every other
        board whose courses they need.
      - The auto-placement engine runs at the end to produce a first-draft
        timetable that can be manually adjusted afterward.
    """

    # ── Step 1: Collect recommendations (batch-optimized) ─────────
    # Run the recommender for every student in the target program(s).
    # The batch variants pre-load shared data (prerequisites, programme
    # requirements) once, avoiding N+1 queries.

    from core.services.recommender_batch import batch_recommend, batch_recommend_multi_program

    # Normalize ``program`` to a list so the rest of the function can
    # treat single-program and multi-program cases uniformly.
    programs = [program] if isinstance(program, str) else list(program)
    program_label = ",".join(programs)

    student_ids = get_student_ids(
        program=programs if len(programs) > 1 else programs[0],
        section=section,
    )

    # Single-program uses a faster code path; multi-program must resolve
    # each student's individual programme before recommending.
    if len(programs) == 1:
        all_recs = batch_recommend(student_ids, programs[0], year, semester)
    else:
        all_recs = batch_recommend_multi_program(student_ids, year, semester)

    # ── Step 1b: Replace elective placeholders with real courses ──
    # If ElectiveTermMappings exist for this year/term/programme, replace
    # placeholder codes (AI1, AI2) with real elective courses the student
    # is eligible for (based on prerequisites).
    from core.models import ElectiveTermMapping, StudentCourse

    etm_qs = ElectiveTermMapping.objects.filter(
        academic_year=str(year),
        term=semester,
        programme__in=programs,
    ).select_related("elective")

    elective_map: dict[str, list] = defaultdict(list)  # norm_placeholder → [ElectiveCourse]
    placeholder_terms: dict[str, int] = {}  # norm_placeholder → programme_term
    for m in etm_qs:
        norm = normalize_code(m.placeholder_code)
        elective_map[norm].append(m.elective)
        if norm not in placeholder_terms:
            pr = ProgrammeRequirement.objects.filter(
                program=m.programme,
                course_code=m.placeholder_code,
            ).first()
            if pr and pr.programme_term:
                placeholder_terms[norm] = pr.programme_term

    if elective_map:
        # Load passed courses for eligibility checking
        sc_passed = StudentCourse.objects.filter(
            student_id__in=list(all_recs.keys()),
            status="passed",
        ).select_related("course")
        student_passed: dict[int, set[str]] = defaultdict(set)
        for sc in sc_passed:
            student_passed[sc.student_id].add(normalize_code(sc.course.course_code))

        for sid, recs in all_recs.items():
            expanded: list[str] = []
            passed = student_passed.get(sid, set())
            for code in recs:
                norm = normalize_code(code)
                if norm in elective_map:
                    for ec in elective_map[norm]:
                        prereqs = [
                            normalize_code(p) for p in ec.prerequisites_csv.split(",") if p.strip()
                        ]
                        if all(p in passed for p in prereqs):
                            expanded.append(normalize_code(ec.course_code))
                else:
                    expanded.append(code)
            all_recs[sid] = expanded

    # Filter out non-schedulable courses (capstone, training, etc.) and
    # build two structures:
    #   - ``aggregate``: Counter of course_code -> total demand across all
    #     students (used by the section planner).
    #   - ``student_recs``: per-student filtered recommendation lists
    #     (used for classification and board links).
    aggregate: Counter[str] = Counter()
    student_recs: dict[int, list[str]] = {}

    for sid, recs in all_recs.items():
        filtered = [c for c in recs if not _is_excluded_course(c)]
        if filtered:
            aggregate.update(filtered)
            student_recs[sid] = filtered

    # ── Step 2: Classify students by primary term ────────────────
    # Each course in ProgrammeRequirement has a ``programme_term`` (the
    # curriculum term it nominally belongs to, e.g. Term 3).  We use
    # this to figure out which term level each student "belongs to" by
    # counting how many of their recommended courses fall in each term
    # and picking the mode (most common term).

    # Build course_code -> programme_term lookup from the DB.
    # If the same course appears in multiple programs with different
    # terms, the last-seen value wins (acceptable: the primary-term
    # classification is a heuristic, not an exact science).
    pr_qs = ProgrammeRequirement.objects.filter(program__in=programs).values_list(
        "course_code", "programme_term"
    )
    course_term_map: dict[str, int] = {}
    for code, pt in pr_qs:
        if pt is not None:
            course_term_map[normalize_code(code)] = pt

    # Extend course_term_map: real electives inherit their placeholder's term
    for norm_placeholder, pt in placeholder_terms.items():
        for ec in elective_map.get(norm_placeholder, []):
            course_term_map[normalize_code(ec.course_code)] = pt

    classified: list[dict] = []
    term_student_counts: Counter[int] = Counter()

    for sid, recs in student_recs.items():
        # Count how many of this student's recommended courses fall in
        # each curriculum term.
        term_counts: Counter[int] = Counter()
        for code in recs:
            pt = course_term_map.get(normalize_code(code))
            if pt is not None:
                term_counts[pt] += 1

        # Students with no classifiable courses are skipped (e.g. all
        # their courses are electives without a programme_term).
        if not term_counts:
            continue

        # Primary term = the term with the most recommended courses.
        primary_term = term_counts.most_common(1)[0][0]
        # Cross-term = student needs courses from more than one term
        # level.  These students will get "visitor" board links later.
        is_cross_term = len(term_counts) > 1

        classified.append(
            {
                "student_id": sid,
                "primary_term": primary_term,
                "is_cross_term": is_cross_term,
                "recommended_courses": recs,
                "term_counts": dict(term_counts),
            }
        )
        term_student_counts[primary_term] += 1

    # ── Step 3: Compute section plan (section budget) ─────────────
    # The section plan determines how many sections of each course are
    # needed, based on aggregate student demand and per-section capacity
    # limits.  Capacity limits differ by department locality and credit
    # hours (see section_planning.py for the rules).

    # Load programme-specific capacity overrides.  When a course appears
    # in multiple programs with different capacities, take the minimum
    # to respect the most restrictive constraint.
    programme_capacities: dict[str, int] = {}
    if aggregate:
        for prog in programs:
            caps = load_programme_capacities(prog, list(aggregate.keys()))
            for code, cap in caps.items():
                if code not in programme_capacities or cap < programme_capacities[code]:
                    programme_capacities[code] = cap

    # Normalize any user-supplied course overrides so they match the
    # canonical upper-case-no-spaces format used everywhere else.
    norm_overrides: dict[str, int] | None = None
    if course_overrides:
        norm_overrides = {normalize_code(k): v for k, v in course_overrides.items()}

    plan = compute_section_plan(
        aggregate,
        max_local_4cr=max_local_4cr,
        max_local_other=max_local_other,
        max_external=max_external,
        course_overrides=norm_overrides,
        programme_capacities=programme_capacities,
    )
    summary = compute_plan_summary(plan)

    # ── Step 4: Create scenario + boards ─────────────────────────
    # A TimetableScenario is the top-level container.  Each scenario
    # holds one DeliveryBoard per active curriculum-term level (e.g.
    # "Term 3", "Term 5").  Boards group students and section
    # placements that belong to the same cohort.

    # Auto-generate a descriptive name when none is supplied.
    if not scenario_name:
        import datetime

        ts = datetime.datetime.now().strftime("%H%M%S")
        sec_label = f" {section}" if section else ""
        scenario_name = f"{program_label}{sec_label} T{semester} Draft {ts}"

    # Resolve the time-slot grid.  Prefer the admin-configured default
    # template; fall back to the hardcoded DEFAULT_SLOTS from the
    # auto-placement engine if no template has been marked as default.
    slot_config: list[object] = []
    default_tpl = TimeSlotTemplate.objects.filter(is_default=True).first()
    if default_tpl:
        slot_config = default_tpl.slots  # type: ignore[assignment]
    if not slot_config:
        from core.services.timetable_autoplace import DEFAULT_SLOTS

        slot_config = DEFAULT_SLOTS

    # Lab slot grid: dedicated 100-min time slots (separate from lectures)
    from core.services.timetable_autoplace import DEFAULT_LAB_SLOTS

    lab_slot_config: list[object] = DEFAULT_LAB_SLOTS

    scenario = TimetableScenario.objects.create(
        academic_year=str(year),
        term=str(semester),
        name=scenario_name,
        slot_config=slot_config,
        lab_slot_config=lab_slot_config,
        created_by=created_by,
    )

    # Create one DeliveryBoard per active term level.  "Active" means
    # at least one classified student has that term as their primary.
    active_terms = sorted(term_student_counts.keys())
    board_by_term: dict[int, DeliveryBoard] = {}

    # For multi-program scenarios the board's ``program`` field stores
    # the comma-joined label (e.g. "AI,DS,CS").
    board_program = programs[0] if len(programs) == 1 else program_label
    for pt in active_terms:
        board = DeliveryBoard.objects.create(
            scenario=scenario,
            label=f"Term {pt}",
            nominal_term=pt,
            board_type="standard",
            program=board_program,
            target_size=term_student_counts[pt],
            display_order=pt,
        )
        board_by_term[pt] = board

    # ── Step 5: Persist classification + budget + links ──────────
    # Three bulk-create operations store the generated data so it can
    # be queried later by the workspace UI and the auto-placer.

    # 5a. ScenarioStudentMap — one row per classified student, recording
    #     their primary term, cross-term flag, and recommended courses.
    ssm_objs = [
        ScenarioStudentMap(
            scenario=scenario,
            student_id=s["student_id"],
            primary_term=s["primary_term"],
            is_cross_term=s["is_cross_term"],
            recommended_courses=s["recommended_courses"],
        )
        for s in classified
    ]
    ScenarioStudentMap.objects.bulk_create(ssm_objs, ignore_conflicts=True)

    # 5b. ScenarioSectionBudget — one row per course, recording the
    #     number of planned sections, capacity per section, total
    #     demand, and the curriculum term the course belongs to.
    budget_objs = []
    for entry in plan:
        code = entry["course_code"]
        # Look up which curriculum term this course belongs to so it
        # can be associated with the correct board later.
        pt = course_term_map.get(normalize_code(code))
        budget_objs.append(
            ScenarioSectionBudget(
                scenario=scenario,
                course_code=code,
                department=entry.get("department", ""),
                credit_hours=entry.get("credit_hours", 0),
                planned_sections=entry["num_sections"],
                max_per_section=entry["max_per_section"],
                total_demand=entry["total_students"],
                programme_term=pt,
            )
        )
    ScenarioSectionBudget.objects.bulk_create(budget_objs, ignore_conflicts=True)

    # 5c. BoardStudentLink — connects students to boards.
    #     Two link types:
    #       "primary" — the student's main board (their primary term).
    #       "visitor" — boards the student must also attend because
    #                   some of their courses belong to a different
    #                   term level (cross-term students only).
    bsl_objs = []
    for s in classified:
        # Every classified student gets exactly one primary link.
        primary_board = board_by_term.get(s["primary_term"])
        if primary_board:
            bsl_objs.append(
                BoardStudentLink(
                    board=primary_board,
                    student_id=s["student_id"],
                    link_type="primary",
                )
            )

        # Cross-term students additionally get visitor links to every
        # other board whose courses they need.
        if s["is_cross_term"]:
            for code in s["recommended_courses"]:
                ct = course_term_map.get(normalize_code(code))
                if ct and ct != s["primary_term"] and ct in board_by_term:
                    bsl_objs.append(
                        BoardStudentLink(
                            board=board_by_term[ct],
                            student_id=s["student_id"],
                            link_type="visitor",
                        )
                    )

    BoardStudentLink.objects.bulk_create(bsl_objs, ignore_conflicts=True)

    # ── Step 6: Auto-place first draft ───────────────────────────
    # Run the constraint-based auto-placement engine, which assigns
    # each budgeted section to a (day, slot) on its board while
    # respecting conflict and capacity constraints.  The result is a
    # first-draft timetable that advisors can fine-tune manually.

    from core.services.timetable_autoplace import auto_place_scenario, get_meeting_pattern

    auto_result = auto_place_scenario(scenario.id, strategy=strategy)

    # ── Build response ───────────────────────────────────────────
    # Assemble a comprehensive response dict that the frontend and API
    # consumers use to render the workspace.

    boards_response = []
    for pt in active_terms:
        board = board_by_term[pt]

        # Count primary and visitor students linked to this board.
        primary_count = BoardStudentLink.objects.filter(board=board, link_type="primary").count()
        visitor_count = BoardStudentLink.objects.filter(board=board, link_type="visitor").count()
        # How many section placements the auto-placer created on this board.
        placement_count = SectionPlacement.objects.filter(board=board).count()

        # Courses that belong to this board's term level.
        board_courses = [b.course_code for b in budget_objs if b.programme_term == pt]

        # Auto-placement statistics for this board (placed vs skipped).
        board_auto = auto_result["boards"].get(board.label, {})

        boards_response.append(
            {
                "id": board.id,
                "label": board.label,
                "nominal_term": pt,
                "program": board_program,
                "primary_count": primary_count,
                "visitor_count": visitor_count,
                "courses": board_courses,
                "placement_count": placement_count,
                "auto_placed": board_auto.get("placed", 0),
                "auto_skipped": board_auto.get("skipped", 0),
                "critical": 0,
                "warning": 0,
            }
        )

    # Re-compute the budget from the DB (now includes actual placement
    # counts) and enrich each entry with meeting-pattern info so the UI
    # can show how many meetings per week each course requires and their
    # durations (e.g. a 3-credit course meets 3x50min or 2x75min).
    from core.services.timetable_workspace import compute_scenario_budget

    budget_response = compute_scenario_budget(scenario.id)
    for b in budget_response:
        cr = b.get("credit_hours", 3)
        pattern = get_meeting_pattern(cr)
        b["meetings_per_week"] = len(pattern)
        b["meeting_durations"] = pattern

    # Student summary: how many are on-plan (all courses in one term)
    # vs cross-term (courses spanning multiple term levels).
    on_plan = sum(1 for s in classified if not s["is_cross_term"])
    cross_term = sum(1 for s in classified if s["is_cross_term"])

    return {
        "scenario": {
            "id": scenario.id,
            "academic_year": scenario.academic_year,
            "term": scenario.term,
            "name": scenario.name,
            "status": scenario.status,
            "slot_config": scenario.slot_config,
            "lab_slot_config": scenario.lab_slot_config,
            "created_at": scenario.created_at.isoformat() if scenario.created_at else "",
        },
        "boards": boards_response,
        "section_budget": budget_response,
        "section_plan": plan,
        "plan_summary": summary,
        "student_summary": {
            "total": len(student_ids),
            "with_recommendations": len(student_recs),
            "classified": len(classified),
            "on_plan": on_plan,
            "cross_term": cross_term,
            "by_term": dict(term_student_counts),
        },
    }

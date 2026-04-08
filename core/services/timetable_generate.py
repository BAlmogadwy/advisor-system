"""
core/services/timetable_generate.py
Cohort-based workspace generation.

Combines the recommender pipeline with student classification to auto-create
a fully scaffolded scenario with boards per term level.
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
from core.services.recommender import recommend_next_courses
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

# Courses excluded from timetable scheduling
# - Free elective placeholders (FE1, FE2)
# - Course numbers ending with 490, 491, 492
# - Courses whose name contains graduation/project/training/coop keywords
EXCLUDED_COURSE_CODES: frozenset[str] = frozenset()  # None currently hardcoded
EXCLUDED_SUFFIXES = ("490", "491", "492")
EXCLUDED_NAME_KEYWORDS = frozenset({
    "graduation", "practical training", "training",
    "cooperative", "co-operative", "coop", "internship",
    "تخرج", "مشروع تخرج", "تدريب",
})
# Patterns that indicate a capstone/project course (but not "project management")
EXCLUDED_NAME_PATTERNS_PROJECT = ("project i", "project ii", "graduation project")

# Cache: course_code → course_name (loaded lazily)
_course_name_cache: dict[str, str] | None = None


def _load_course_names() -> dict[str, str]:
    global _course_name_cache
    if _course_name_cache is None:
        from core.models import Course
        _course_name_cache = {
            c.course_code.upper(): (c.description or "").lower()
            for c in Course.objects.all()
        }
    return _course_name_cache


def _is_excluded_course(code: str) -> bool:
    """Check if a course should be excluded from timetable scheduling."""
    c = normalize_code(code)
    if c in EXCLUDED_COURSE_CODES:
        return True
    digits = "".join(ch for ch in c if ch.isdigit())
    if digits.endswith(EXCLUDED_SUFFIXES):
        return True
    # Check course name for keywords
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
    max_local_4cr: int = DEFAULT_MAX_LOCAL_4CR,
    max_local_other: int = DEFAULT_MAX_LOCAL_OTHER,
    max_external: int = DEFAULT_MAX_EXTERNAL,
    course_overrides: dict[str, int] | None = None,
    created_by: str = "",
) -> dict:
    """Generate a fully scaffolded workspace scenario.

    Args:
        program: single program code ("AI") or list (["AI", "DS"])

    1. Run recommender for all students in the program(s)
    2. Classify each student by primary term
    3. Compute section plan (budget)
    4. Create scenario + one board per active term level
    5. Persist classification, budget, and student-board links

    Returns dict with scenario, boards, budget, and student summary.
    """

    # ── Step 1: Collect recommendations (batch-optimized) ─────────

    from core.services.recommender_batch import batch_recommend, batch_recommend_multi_program

    # Normalize to list for uniform handling
    programs = [program] if isinstance(program, str) else list(program)
    program_label = ",".join(programs)

    student_ids = get_student_ids(
        program=programs if len(programs) > 1 else programs[0],
        section=section,
    )

    if len(programs) == 1:
        all_recs = batch_recommend(student_ids, programs[0], year, semester)
    else:
        all_recs = batch_recommend_multi_program(student_ids, year, semester)

    # Filter out non-schedulable courses
    aggregate: Counter[str] = Counter()
    student_recs: dict[int, list[str]] = {}

    for sid, recs in all_recs.items():
        filtered = [c for c in recs if not _is_excluded_course(c)]
        if filtered:
            aggregate.update(filtered)
            student_recs[sid] = filtered

    # ── Step 2: Classify students by primary term ────────────────

    # Load course → programme_term mapping for all involved programs
    pr_qs = ProgrammeRequirement.objects.filter(program__in=programs).values_list(
        "course_code", "programme_term"
    )
    course_term_map: dict[str, int] = {}
    for code, pt in pr_qs:
        if pt is not None:
            course_term_map[normalize_code(code)] = pt

    classified: list[dict] = []
    term_student_counts: Counter[int] = Counter()

    for sid, recs in student_recs.items():
        term_counts: Counter[int] = Counter()
        for code in recs:
            pt = course_term_map.get(normalize_code(code))
            if pt is not None:
                term_counts[pt] += 1

        if not term_counts:
            continue

        primary_term = term_counts.most_common(1)[0][0]
        is_cross_term = len(term_counts) > 1

        classified.append({
            "student_id": sid,
            "primary_term": primary_term,
            "is_cross_term": is_cross_term,
            "recommended_courses": recs,
            "term_counts": dict(term_counts),
        })
        term_student_counts[primary_term] += 1

    # ── Step 3: Compute section plan ─────────────────────────────

    programme_capacities: dict[str, int] = {}
    if aggregate:
        # Merge capacities from all programs (use min if same course in multiple programs)
        for prog in programs:
            caps = load_programme_capacities(prog, list(aggregate.keys()))
            for code, cap in caps.items():
                if code not in programme_capacities or cap < programme_capacities[code]:
                    programme_capacities[code] = cap

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

    if not scenario_name:
        import datetime
        ts = datetime.datetime.now().strftime("%H%M%S")
        sec_label = f" {section}" if section else ""
        scenario_name = f"{program_label}{sec_label} T{semester} Draft {ts}"

    # Get default slot template, or use standard academic slots
    slot_config: list[object] = []
    default_tpl = TimeSlotTemplate.objects.filter(is_default=True).first()
    if default_tpl:
        slot_config = default_tpl.slots  # type: ignore[assignment]
    if not slot_config:
        from core.services.timetable_autoplace import DEFAULT_SLOTS
        slot_config = DEFAULT_SLOTS

    scenario = TimetableScenario.objects.create(
        academic_year=str(year),
        term=str(semester),
        name=scenario_name,
        slot_config=slot_config,
        created_by=created_by,
    )

    # Create one board per active term level
    active_terms = sorted(term_student_counts.keys())
    board_by_term: dict[int, DeliveryBoard] = {}

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

    # Persist student classification
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

    # Persist section budget
    budget_objs = []
    for entry in plan:
        code = entry["course_code"]
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

    # Persist board-student links
    bsl_objs = []
    for s in classified:
        primary_board = board_by_term.get(s["primary_term"])
        if primary_board:
            bsl_objs.append(
                BoardStudentLink(
                    board=primary_board,
                    student_id=s["student_id"],
                    link_type="primary",
                )
            )

        # Visitor links for cross-term courses
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

    from core.services.timetable_autoplace import auto_place_scenario, get_meeting_pattern

    auto_result = auto_place_scenario(scenario.id)

    # ── Build response ───────────────────────────────────────────

    boards_response = []
    for pt in active_terms:
        board = board_by_term[pt]
        primary_count = BoardStudentLink.objects.filter(
            board=board, link_type="primary"
        ).count()
        visitor_count = BoardStudentLink.objects.filter(
            board=board, link_type="visitor"
        ).count()
        placement_count = SectionPlacement.objects.filter(board=board).count()

        board_courses = [
            b.course_code
            for b in budget_objs
            if b.programme_term == pt
        ]

        board_auto = auto_result["boards"].get(board.label, {})

        boards_response.append({
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
        })

    # Budget with meeting pattern + actual used count after auto-placement
    from core.services.timetable_workspace import compute_scenario_budget

    budget_response = compute_scenario_budget(scenario.id)
    # Enrich with meeting pattern
    for b in budget_response:
        cr = b.get("credit_hours", 3)
        pattern = get_meeting_pattern(cr)
        b["meetings_per_week"] = len(pattern)
        b["meeting_durations"] = pattern

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

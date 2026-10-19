"""
core/services/timetable_optimizer_v2.py
Post-generation optimisation layer for exam timetables.

Bridges existing Django models to the in-memory evaluation/local-search
engine, providing:
  - Adapter functions (DB ↔ dataclasses)
  - Multi-candidate ranking
  - Optional local-search improvement
  - Persistence of improved results back to DB
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable

from django.conf import settings
from django.db import transaction
from django.db.models import Q

from core.models import (
    DeliveryBoard,
    Room,
    ScenarioSectionBudget,
    SectionPlacement,
    StudentCourse,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.course_classifier import FAILING_LETTER_GRADES
from core.services.course_identity import course_display_code
from core.services.student_helpers import normalize_code
from core.services.timetable_assignment_models import (
    RiskTier,
    RoomOccupancy,
    RoomProfile,
    SectionMeeting,
    SectionState,
    StudentProfile,
)
from core.services.timetable_autoplace import WEEKDAYS
from core.services.timetable_board_persistence import external_shared_section_ids
from core.services.timetable_candidate_eval import (
    evaluate_generated_timetable_candidate,
    rank_timetable_candidates,
)
from core.services.timetable_course_policy import (
    CourseMeetingPolicy,
    WeeklyPattern,
    get_course_meeting_policy,
)
from core.services.timetable_course_tier import (
    classify_course_tier,
    program_count_by_code,
)
from core.services.timetable_demand import load_scenario_course_demands
from core.services.timetable_flags import (
    is_gap_regular_students_only_enabled,
    is_online_gap_exclusion_enabled,
    is_tiered_objective_enabled,
)
from core.services.timetable_online import OnlineCourseLookup
from core.services.timetable_optimisation_policy import (
    evaluation_is_better,
    evaluation_order_key,
    instructor_schedule_metrics,
    is_instructor_priority_enabled,
    schedule_metrics_of,
)
from core.services.timetable_pr4_instructor import (
    is_instructor_clash_enabled,
    is_instructor_compaction_enabled,
    is_instructor_daily_cap_enabled,
    is_instructor_gap_penalty_enabled,
    section_instructor_resolver,
)
from core.services.timetable_stage_telemetry import STAGE_KEYS, empty_stage_telemetry
from core.services.timetable_student_assignment import instructor_idle_of
from core.services.timetable_workspace import _to_minutes

logger = logging.getLogger(__name__)

# Day-string → integer index (SUN=0 .. THU=4)
_DAY_IDX: dict[str, int] = {d: i for i, d in enumerate(WEEKDAYS)}


# ── Adapter A: Student Profiles ──────────────────────────────────


def build_student_profiles_for_scenario(
    scenario_id: int,
) -> dict[str, StudentProfile]:
    """Read canonical scenario demand + student metadata → StudentProfile dict.

    Risk tiers:
      A (highest) — student has at least one confirmed failed/retake course
      B           — graduating soon (earned ≥ 100 credits)
      C (lowest)  — everyone else

    intra_tier_score: lower GPA → higher priority within the tier
                      (students struggling most get first pick).
    """
    demand_rows = load_scenario_course_demands(scenario_id)
    if not demand_rows:
        return {}

    rec_courses_by_sid: dict[int, list[str]] = defaultdict(list)
    visitor_sids: set[int] = set()
    for demand in demand_rows:
        rec_courses_by_sid[demand.student_id].append(demand.course_key)
        if demand.is_cross_term:
            visitor_sids.add(demand.student_id)
    # Read once per run; with the flag off nobody is marked, so profiles stay
    # exactly as before.
    mark_visitors = is_gap_regular_students_only_enabled()
    student_ids = list(rec_courses_by_sid)

    # Bulk-fetch student metadata
    from core.models import Student

    students_qs = Student.objects.filter(student_id__in=student_ids).values(
        "student_id", "program", "gpa", "total_earned_credits"
    )
    student_meta: dict[int, dict] = {s["student_id"]: s for s in students_qs}

    # A failed status is authoritative. A currently studied course with a
    # failing historical symbol is a retake; D/D+ and W are deliberately absent
    # because they are pass/withdrawal outcomes, not failed attempts.
    retake_courses: dict[int, set[str]] = defaultdict(set)
    retake_records = (
        StudentCourse.objects.filter(
            student_id__in=student_ids,
        )
        .filter(Q(status="failed") | Q(status="studying", grade__in=tuple(FAILING_LETTER_GRADES)))
        .select_related("course")
        .values_list("student_id", "course__course_code")
    )
    for sid, ccode in retake_records:
        retake_courses[sid].add(ccode)

    profiles: dict[str, StudentProfile] = {}
    for sid, rec_courses in rec_courses_by_sid.items():
        meta = student_meta.get(sid, {})
        program = meta.get("program") or ""
        gpa = meta.get("gpa") or 0.0
        earned = meta.get("total_earned_credits") or 0

        # Determine risk tier
        has_retake = bool(retake_courses.get(sid))
        if has_retake:
            tier = RiskTier.A
        elif earned >= 100:
            tier = RiskTier.B
        else:
            tier = RiskTier.C

        # Lower GPA = higher priority within tier (inverted so sort ascending)
        intra_score = 4.0 - min(gpa, 4.0)

        profiles[str(sid)] = StudentProfile(
            student_id=str(sid),
            department=program,
            recommended_courses=rec_courses if rec_courses else [],
            risk_tier=tier,
            intra_tier_score=round(intra_score, 3),
            is_visitor=mark_visitors and sid in visitor_sids,
        )

    logger.info(
        "Built %d student profiles for scenario %d (A=%d B=%d C=%d)",
        len(profiles),
        scenario_id,
        sum(1 for p in profiles.values() if p.risk_tier == RiskTier.A),
        sum(1 for p in profiles.values() if p.risk_tier == RiskTier.B),
        sum(1 for p in profiles.values() if p.risk_tier == RiskTier.C),
    )
    return profiles


# ── Adapter B: Section States ────────────────────────────────────


def _compute_pattern_family(meetings: list[SectionMeeting], has_lab: bool) -> str:
    """Derive the pattern family key from meeting durations.

    Pattern family groups sections with the same meeting structure
    (e.g. all 3-credit courses share "ONCAMPUS_LEC_75_75").
    Local search uses this to find alternative time patterns within
    the same structural family — you can move a 75+75 course to any
    other 75+75 slot combination, but not to a 100-min lab slot.
    """
    durations = sorted(m.end_min - m.start_min for m in meetings)
    dur_str = "_".join(map(str, durations))
    lec_type = "MIXED" if has_lab else "LEC"
    return f"ONCAMPUS_{lec_type}_{dur_str}"


def _compute_pattern_id(meetings: list[SectionMeeting]) -> str:
    """Compute a pattern ID from meeting fingerprint."""
    from core.services.timetable_pattern_catalog import (
        generate_pattern_id,
        generate_pattern_signature,
    )

    sig = generate_pattern_signature(meetings)
    return generate_pattern_id(sig)


def _validated_policy_placement_pattern(
    policy: CourseMeetingPolicy,
    placements: list[SectionPlacement],
) -> WeeklyPattern:
    """Certify every board copy before collapsing a shared section's meetings."""
    copies: dict[int, list[SectionPlacement]] = defaultdict(list)
    for placement in placements:
        copies[placement.board_id].append(placement)
    pattern = None
    for rows in copies.values():
        current = policy.pattern_for(rows)
        if current is None or (pattern is not None and current != pattern):
            raise ValueError("English board copies must each use the same complete weekly pattern")
        pattern = current
    if pattern is None:
        raise ValueError("English section requires a complete weekly pattern")
    return pattern


def build_section_states_for_scenario(
    scenario_id: int,
) -> list[SectionState]:
    """Convert current SectionPlacement rows → list[SectionState].

    Groups placements by TermSection, converts day/time strings to
    SectionMeeting bitmasks, fills capacity from ScenarioSectionBudget,
    and computes pattern_family + pattern_id for local search.
    """
    placements = (
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .select_related("term_section", "board")
        .order_by("term_section_id", "day")
    )
    if not placements.exists():
        return []

    # Budget lookup for capacity
    budgets = {
        (b.course_key or b.course_code): b
        for b in ScenarioSectionBudget.objects.filter(scenario_id=scenario_id)
    }

    # Group placements by term_section
    grouped: dict[int, list] = defaultdict(list)
    ts_lookup: dict[int, TermSection] = {}
    for pl in placements:
        grouped[pl.term_section_id].append(pl)
        ts_lookup[pl.term_section_id] = pl.term_section

    # Online sessions are attended remotely, so they must not appear in any
    # student or instructor gap chain (see timetable_student_assignment
    # ``_campus_meetings``). Resolved PER BOARD via ``is_online_course_for_board``
    # — the same convention every other online consumer uses (rooming, workspace,
    # repair, solver, autoplace, ...). Keying off ``scenario.programs`` instead
    # would silently no-op on any scenario whose ``programs`` is empty (real
    # today: scn 615/616/617/620, and 620 has 168 placements), because
    # ``codes_for_programmes([])`` returns an empty set. The lookup caches by
    # programme set, so the per-section calls are cheap.
    online_lookup = OnlineCourseLookup()
    exclude_online_gaps = is_online_gap_exclusion_enabled()

    sections: list[SectionState] = []
    for ts_id, pls in grouped.items():
        ts = ts_lookup[ts_id]
        course_code = ts.course_key or ts.course_code
        budget = budgets.get(course_code)
        policy = get_course_meeting_policy(ts.course_code, ts.course_key, ts.course_number)
        permitted = _validated_policy_placement_pattern(policy, pls) if policy else None
        # Online-ness is a COURSE property keyed by the BARE code; course_code
        # above is the planner ``CODE::NAME`` identity, so pass ts.course_code.
        delivery_is_online = online_lookup.is_online_course_for_board(
            pls[0].board, course_display_code(ts.course_key, ts.course_code, ts.course_number)
        )
        is_online = delivery_is_online and exclude_online_gaps

        meetings: list[SectionMeeting] = []
        # A section shared across boards has one teaching schedule. Policy
        # validation above checks all copies; use one complete copy for its state.
        state_placements = [p for p in pls if p.board_id == pls[0].board_id] if policy else pls
        for pl in state_placements:
            day_idx = _DAY_IDX.get(pl.day.upper(), -1)
            if day_idx < 0:
                logger.warning("Unknown day %r for placement %d", pl.day, pl.pk)
                continue
            start_min = _to_minutes(pl.start_time)
            end_min = _to_minutes(pl.end_time)
            meetings.append(SectionMeeting(day=day_idx, start_min=start_min, end_min=end_min))

        if not meetings:
            continue

        max_cap = budget.max_per_section if budget else 40

        # Per-section demand: actual students / planned sections × TIMETABLE_CAPACITY_BUFFER.
        # This drives room sizing — use real demand, not max_per_section,
        # so a course with 25 recommended students gets a 25-cap room,
        # not a 50-cap room based on the budget ceiling.
        if budget and budget.planned_sections > 0 and budget.total_demand > 0:
            # Deferred import: hoisting to module scope risks a circular
            # import because timetable_rooming pulls in models/helpers that
            # eventually reference optimizer state. Keep this local.
            from core.services.timetable_rooming import get_capacity_buffer

            per_section_demand = -(-budget.total_demand // budget.planned_sections)  # ceil
            per_section_demand = int(per_section_demand * get_capacity_buffer())
        else:
            per_section_demand = max_cap

        # Reserve 10% for high-priority students (min 2, max 8)
        reserve = max(2, min(8, int(max_cap * 0.10)))

        # Determine room type from majority of meeting durations.
        # 4-credit courses have mixed meetings (2×75min + 1×100min) —
        # majority are lectures so they get a lecture room. Pure lab
        # courses (all meetings >80min) get lab rooms.
        has_lab = budget and budget.credit_hours == 4 and not policy
        lecture_meetings = sum(1 for m in meetings if (m.end_min - m.start_min) <= 80)
        room_type = "lecture" if policy or lecture_meetings >= len(meetings) / 2 else "lab"

        # Compute pattern family and ID for local search
        pattern_family = (
            policy.policy_id if policy else _compute_pattern_family(meetings, bool(has_lab))
        )
        pattern_id = permitted.pattern_id if permitted else _compute_pattern_id(meetings)

        section_id = f"{course_code}_{ts.section}"
        sections.append(
            SectionState(
                section_id=section_id,
                course_code=course_code,
                meetings=meetings,
                max_capacity=max_cap,
                reserve_capacity=reserve,
                room_type_required=room_type,
                demand_capacity=per_section_demand,
                assigned_room_id=pls[0].room if pls[0].room and not policy else None,
                pattern_family=pattern_family,
                pattern_id=pattern_id,
                is_online=is_online,
                delivery_is_online=delivery_is_online,
                uses_shared_room=policy.uses_shared_room if policy else True,
            )
        )

    # Make a silent no-op visible: if the exclusion is on but nothing resolved as
    # online, the gap chain is still counting every session as on-campus and the
    # cause is almost always missing programme metadata on the boards.
    if exclude_online_gaps and not any(s.is_online for s in sections):
        logger.info(
            "Online gap exclusion is ON but no section resolved as online for "
            "scenario %d — gap minutes still include any online sessions.",
            scenario_id,
        )

    # DEBUG, not INFO: this helper used to run once per optimise run, but the
    # instructor-clash backstop now calls it from interactive placement endpoints,
    # where an INFO line per drag would bury the backstop's own warnings.
    logger.debug(
        "Built %d section states from %d placements for scenario %d",
        len(sections),
        sum(len(v) for v in grouped.values()),
        scenario_id,
    )
    return sections


def build_section_instructor_map_for_scenario(scenario_id: int) -> dict[str, frozenset[int]]:
    """``{section_id: frozenset[instructor_id]}`` for the instructor idle-gap term.

    Keys use the **same** ``section_id`` convention as
    ``build_section_states_for_scenario`` (``f"{course_key or course_code}_{section}"``),
    so the evaluator can resolve a section's instructors by direct lookup — no
    ``|``-vs-``_`` key reconciliation. Resolution is the same
    ``section_instructor_resolver`` that ``build_section_instructor_ids`` uses:
    only **active** instructors assigned via ``CourseInstructor`` for the
    scenario's gender, from the programmes that teach the section's own
    ``course_key`` (CS112 PROGRAMMING II is not billed to PROGRAMMING I's
    lecturers). Sections with no assignment are simply absent
    from the map (they then contribute nothing to the idle-gap metric). Returns
    ``{}`` when the scenario has no gender — the assignment model is gender-scoped.

    With one teacher per section (``is_one_teacher_active``) every charged
    section maps to the singleton of its one teacher, pinned and frozen ones
    included; a section still to be decided charges nobody and is absent.
    """
    from core.services.timetable_flags import is_one_teacher_active

    if is_one_teacher_active():
        from core.services.section_teacher_assignment import load_assignment

        return load_assignment(scenario_id).v2_map()

    scenario = TimetableScenario.objects.filter(id=scenario_id).first()
    if scenario is None:
        return {}
    gender = getattr(scenario, "gender", "") or ""
    if not gender:
        return {}
    programs = list(getattr(scenario, "programs", []) or [])
    if not programs:
        return {}

    resolve = section_instructor_resolver(programs, gender)
    out: dict[str, frozenset[int]] = {}
    for course_key, course_code, course_number, section in TermSection.objects.filter(
        scenario_id=scenario_id
    ).values_list("course_key", "course_code", "course_number", "section"):
        ids = resolve(course_key, course_display_code(course_key, course_code, course_number))
        if ids:
            section_id = f"{course_key or course_code}_{section}"
            out[section_id] = frozenset(ids)
    return out


def build_course_tier_map_for_scenario(scenario_id: int) -> dict[str, str]:
    """``{SectionState.course_code: 'T1'|'T2'|'T3'}`` for the tiered objective.

    Keyed by the ``course_key or course_code`` identity the evaluator sees on
    ``SectionState.course_code`` (planner ``CODE::NAME`` form), so the hot loop
    resolves a course's tier by O(1) dict lookup — no DB, no string work per
    eval. One cached global plan-count read + one ~40-row in-memory build per
    run. The bare code (for prefix rules + the plan-count lookup) is derived at
    build time via ``split("::", 1)[0]``. Codes absent from the map default to
    T1 at the lookup site.
    """
    counts = program_count_by_code()
    out: dict[str, str] = {}
    for course_key, course_code in TermSection.objects.filter(scenario_id=scenario_id).values_list(
        "course_key", "course_code"
    ):
        key = course_key or course_code
        if not key:
            continue
        bare = key.split("::", 1)[0]
        tier = classify_course_tier(bare, counts.get(normalize_code(bare), 0))
        out[key] = tier
        # Belt-and-suspenders: also index by the bare normalised code so a caller
        # that passes a bare code (no ::NAME) still resolves.
        out.setdefault(normalize_code(bare), tier)
    return out


def refresh_final_score_after_instructor_passes(
    result: dict,
    scenario_id: int,
    student_profiles: dict,
    course_rigidity: dict,
    section_instructor_ids: dict | None,
    course_tiers: dict | None,
) -> None:
    """Re-score the PERSISTED board after the instructor passes have run.

    ``result["final_score"]`` is written by the optimise stages, but the
    cap-repair / clash-repair / compaction passes mutate the board afterwards.
    Without this the reported score describes a board that no longer exists —
    the registrar is shown numbers that do not match what was persisted, and the
    runner's student-outcome rollback gate judges the pre-pass board.

    Verified on scenario 643: clash repair relocated two of one instructor's
    sections to clear real double-bookings, costing 8 soft-tier seats and 775
    gap-minutes that the reported score never reflected (reported soft=48 /
    gap=104835 vs persisted soft=56 / gap=105610).

    No-ops when no instructor pass ran, so the extra evaluation is only paid
    when the board could actually have changed.
    """
    # Guard on whether a pass actually MOVED anything, not merely on the flag
    # being on: the callers set these keys unconditionally inside their
    # ``if ..._enabled()`` blocks, so a no-op repair would otherwise cost a full
    # evaluation on every build.
    _changed_keys = (
        ("instructor_cap_repair", ("repaired", "unplaced")),
        ("instructor_clash_repair", ("repaired", "unplaced")),
        ("instructor_compaction", ("relocations",)),
    )
    changed = False
    for key, change_fields in _changed_keys:
        report = result.get(key)
        if isinstance(report, dict) and any(report.get(f) for f in change_fields):
            changed = True
            break
    if not changed:
        return
    if not student_profiles:
        return
    sections = build_section_states_for_scenario(scenario_id)
    if not sections:
        return
    post = evaluate_generated_timetable_candidate(
        candidate_id="post_instructor_passes",
        generated_sections=sections,
        student_profiles=student_profiles,
        course_rigidity=course_rigidity,
        section_instructor_ids=section_instructor_ids,
        course_tiers=course_tiers,
    )
    before = list(result.get("final_score") or [])
    after = list(post.lexicographic_score)
    # Preserved for the rollback gate, which must judge the optimiser's own
    # decision (pre-pass) rather than the mandatory repair cost. See
    # timetable_v2_runner.optimiser_student_outcome_regression.
    result["score_before_instructor_passes"] = before
    result["instructor_schedule_before_passes"] = (
        result.get("quality_score", {}).get("instructor_schedule") or {}
    )
    logger.info("instructor passes changed the board: final_score %s -> %s", before, after)
    result["final_score"] = after
    # Update every board-derived field in lockstep, as all the other final_score
    # write sites do — otherwise the dialog mixes a post-pass score with pre-pass
    # hotspots/quality and misattributes them.
    result["unresolved_students"] = len(post.unresolved_student_ids)
    result["quality_score"] = post.quality_score
    result["hotspot_courses"] = post.hotspot_courses[:10]
    result["capacity_pressure_courses"] = post.capacity_pressure_courses[:10]
    result["reserve_heavy_sections"] = [
        {"section": s, "ratio": round(r, 2)} for s, r in post.reserve_heavy_sections[:10]
    ]


def refresh_final_score_after_required_section_repair(
    result: dict,
    scenario_id: int,
    student_profiles: dict,
    course_rigidity: dict,
    section_instructor_ids: dict | None,
    course_tiers: dict | None,
) -> None:
    """Re-score the persisted board after a required-section reinstatement.

    Same shape as :func:`refresh_final_score_after_instructor_passes`, run as
    its own step because the reinstatement happens later in the pipeline
    (after secondary recovery, so a newly-placed section can still receive
    student seats) — a separate call, not a second entry in that function's
    change-detection list, keeps this addition isolated from the existing
    instructor-pass re-score. No-ops when nothing was actually placed.
    """
    report = result.get("required_section_reinstatement")
    if not isinstance(report, dict) or not report.get("newly_placed"):
        return
    if not student_profiles:
        return
    sections = build_section_states_for_scenario(scenario_id)
    if not sections:
        return
    post = evaluate_generated_timetable_candidate(
        candidate_id="post_required_section_repair",
        generated_sections=sections,
        student_profiles=student_profiles,
        course_rigidity=course_rigidity,
        section_instructor_ids=section_instructor_ids,
        course_tiers=course_tiers,
    )
    before = list(result.get("final_score") or [])
    after = list(post.lexicographic_score)
    logger.info("required-section repair changed the board: final_score %s -> %s", before, after)
    result["final_score"] = after
    result["unresolved_students"] = len(post.unresolved_student_ids)
    result["quality_score"] = post.quality_score
    result["hotspot_courses"] = post.hotspot_courses[:10]
    result["capacity_pressure_courses"] = post.capacity_pressure_courses[:10]
    result["reserve_heavy_sections"] = [
        {"section": s, "ratio": round(r, 2)} for s, r in post.reserve_heavy_sections[:10]
    ]


def _gap_pos6(score) -> int:
    """Instructor idle-minutes element of a score tuple, or 0 when the gap
    penalty was off. Layout-aware: index 8 for the tiered 9-tuple, trailing
    index 6 for a legacy 7-tuple, 0 for the canonical 6-tuple."""
    return instructor_idle_of(score)


def capture_instructor_schedule_after_hard_repairs(result, scenario_id, instructor_map):
    """Record justified hard-repair day costs before optional compaction runs."""
    if not is_instructor_priority_enabled():
        return
    if not any(
        (result.get(key) or {}).get("repaired") or (result.get(key) or {}).get("unplaced")
        for key in ("instructor_cap_repair", "instructor_clash_repair")
    ):
        return
    result["instructor_schedule_after_hard_repairs"] = instructor_schedule_metrics(
        build_section_states_for_scenario(scenario_id), instructor_map
    )


def instructor_gap_metric(
    idle_before: int,
    final_score,
    section_instructor_ids: dict[str, frozenset[int]] | None,
) -> dict[str, int]:
    """Schema-stable instructor idle-gap metric for the optimise result payload.

    All-zero when the gap penalty is OFF (``section_instructor_ids`` is None and
    the score tuples are 6-element). ``idle_delta`` is positive when the run
    reduced instructor idle minutes.
    """
    idle_after = _gap_pos6(final_score) if final_score is not None else idle_before
    affected = 0
    if section_instructor_ids:
        affected = len({iid for ids in section_instructor_ids.values() for iid in ids})
    return {
        "idle_minutes_before": idle_before,
        "idle_minutes_after": idle_after,
        "idle_delta": idle_before - idle_after,
        "affected_instructors": affected,
    }


def build_locked_section_ids_for_scenario(scenario_id: int) -> set[str]:
    """Return section IDs that must stay fixed during optimisation.

    UI locks live on individual placements, while optimiser moves operate
    on whole section patterns. If any meeting for a section is locked, the
    safest behaviour is to freeze that whole section.
    """
    from django.db.models import Q

    frozen = external_shared_section_ids(scenario_id)
    rows = (
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .filter(Q(is_locked=True) | Q(term_section_id__in=frozen))
        .select_related("term_section")
        .values_list(
            "term_section__course_key", "term_section__course_code", "term_section__section"
        )
    )
    return {f"{course_key or course_code}_{section}" for course_key, course_code, section in rows}


def _baseline_room_shortages(scenario_id: int) -> int | None:
    """Retain the existing board's actual room target through teacher search."""
    if not is_instructor_priority_enabled() or not getattr(
        settings, "TIMETABLE_SECONDARY_RECOVERY_ENABLED", True
    ):
        return None
    from core.services.timetable_exact_rooming import ExactRoomingError
    from core.services.timetable_recovery_rooming import build_recovery_room_evaluator

    try:
        return int(
            build_recovery_room_evaluator(scenario_id).original_room_report[
                "original_room_shortages"
            ]
        )
    except ExactRoomingError:
        return None


# ── Adapter C: Course Rigidity ───────────────────────────────────


def build_course_rigidity_for_scenario(
    scenario_id: int,
) -> dict[str, float]:
    """Compute per-course rigidity score (0.0 = flexible, 1.0 = rigid).

    Rigidity is driven by:
      - Demand-to-capacity ratio (high demand → less room to manoeuvre)
      - Number of sections (fewer sections → harder to redistribute)
      - Credit hours (4-credit lab courses are harder to move)
    """
    budgets = ScenarioSectionBudget.objects.filter(scenario_id=scenario_id)
    rigidity: dict[str, float] = {}

    for b in budgets:
        if b.planned_sections <= 0:
            rigidity[b.course_key or b.course_code] = 0.5
            continue

        # Demand pressure: how full are the sections?
        capacity = b.planned_sections * b.max_per_section
        demand_ratio = min(b.total_demand / capacity, 1.5) if capacity > 0 else 0.5

        # Section scarcity: fewer sections = more rigid
        scarcity = 1.0 / b.planned_sections

        # Lab bonus: 4-credit courses with labs are harder to reschedule
        lab_bonus = 0.15 if b.credit_hours == 4 else 0.0

        score = min(1.0, (demand_ratio * 0.5) + (scarcity * 0.3) + lab_bonus + 0.05)
        rigidity[b.course_key or b.course_code] = round(score, 3)

    return rigidity


# ── Adapter D: Room State ────────────────────────────────────────


def build_room_state_for_scenario(
    scenario_id: int,
    sections: list[SectionState] | None = None,
) -> tuple[dict[str, RoomProfile], dict[str, RoomOccupancy], dict[str, str]]:
    """Build room profiles, occupancy masks, and course→room-type requirements.

    Only includes rooms assigned to the scenario's programmes (via the
    Room.department field) to prevent cross-department room leakage.

    Returns (rooms_by_id, room_occupancies, course_room_requirements).
    """
    # Room profiles — filtered by scenario's programmes to prevent
    # assigning rooms from other departments (e.g. IS rooms for AI courses)
    scenario = TimetableScenario.objects.get(pk=scenario_id)
    boards = DeliveryBoard.objects.filter(scenario=scenario)
    scenario_progs: set[str] = set()
    for b in boards:
        if b.program:
            scenario_progs.update(p.strip().upper() for p in b.program.split(",") if p.strip())

    rooms_by_id: dict[str, RoomProfile] = {}
    for r in Room.objects.all():
        # Include room if its department overlaps with scenario programmes
        room_depts = {d.strip().upper() for d in r.department.split(",") if d.strip()}
        if not scenario_progs or (room_depts & scenario_progs):
            rooms_by_id[r.room_code] = RoomProfile(
                room_id=r.room_code,
                capacity=r.capacity,
                room_type=r.room_type or "lecture",
                gender=(r.section or "").upper(),
            )

    # Build occupancy from current placements
    room_occupancies: dict[str, RoomOccupancy] = {}
    placements = (
        SectionPlacement.objects.filter(
            board__scenario_id=scenario_id,
        )
        .select_related("term_section")
        .values(
            "room",
            "day",
            "start_time",
            "end_time",
            "term_section_id",
            "term_section__course_key",
            "term_section__course_code",
            "term_section__course_number",
            "term_section__section",
        )
    )

    for pl in placements:
        if get_course_meeting_policy(
            pl["term_section__course_code"],
            pl["term_section__course_key"],
            pl["term_section__course_number"],
        ):
            continue
        room_code = pl["room"]
        if not room_code:
            continue
        if room_code not in room_occupancies:
            room_occupancies[room_code] = RoomOccupancy(room_id=room_code)

        occ = room_occupancies[room_code]
        day_idx = _DAY_IDX.get(pl["day"].upper(), -1)
        if day_idx < 0:
            continue
        start_min = _to_minutes(pl["start_time"])
        end_min = _to_minutes(pl["end_time"])
        meeting = SectionMeeting(day=day_idx, start_min=start_min, end_min=end_min)
        occ.occupied_mask_by_day[meeting.day] |= meeting.mask

        # Use course_code_section format to match sections_by_id keys
        section_id = f"{pl['term_section__course_key']}_{pl['term_section__section']}"
        occ.assigned_section_ids.add(section_id)

    # Course → room type requirements
    course_room_requirements: dict[str, str] = {}
    budgets = ScenarioSectionBudget.objects.filter(scenario_id=scenario_id).values(
        "course_key", "course_code", "credit_hours"
    )
    for b in budgets:
        key = b["course_key"] or b["course_code"]
        course_room_requirements[key] = (
            "lab"
            if b["credit_hours"] == 4 and not get_course_meeting_policy(b["course_code"], key)
            else "lecture"
        )

    # Ensure ALL rooms have an occupancy entry (even if empty)
    # so room repair can assign sections to currently-empty rooms
    for room_code in rooms_by_id:
        if room_code not in room_occupancies:
            room_occupancies[room_code] = RoomOccupancy(room_id=room_code)

    logger.info(
        "Built room state: %d rooms, %d with placements, %d course requirements",
        len(rooms_by_id),
        sum(1 for o in room_occupancies.values() if o.assigned_section_ids),
        len(course_room_requirements),
    )
    return rooms_by_id, room_occupancies, course_room_requirements


# ── Adapter E: Persist Back to DB ────────────────────────────────


@transaction.atomic
def persist_section_states_to_scenario(
    scenario_id: int,
    sections_by_id: dict[str, SectionState],
) -> dict:
    """Write improved SectionState back to SectionPlacement rows.

    Only updates day/start_time/end_time/room for sections that changed.
    Does NOT delete or create placements — only modifies existing ones.

    Returns {"updated": int, "skipped": int}.
    """
    # Build a lookup: (course_code, section_label) → SectionState
    state_lookup: dict[tuple[str, str], SectionState] = {}
    for sec_id, state in sections_by_id.items():
        # section_id format: "{course_code}_{section_label}"
        parts = sec_id.rsplit("_", 1)
        if len(parts) == 2:
            state_lookup[(parts[0], parts[1])] = state

    placements = (
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .select_related("term_section")
        .order_by("term_section_id", "day")
    )

    # Group placements by term_section
    grouped: dict[int, list[SectionPlacement]] = defaultdict(list)
    for pl in placements:
        grouped[pl.term_section_id].append(pl)

    updated = 0
    skipped = 0
    to_update: list[SectionPlacement] = []
    bundle_updates: list[SectionPlacement] = []

    frozen = external_shared_section_ids(scenario_id)
    for _ts_id, pls in grouped.items():
        if _ts_id in frozen:
            skipped += len(pls)
            continue
        ts = pls[0].term_section
        key = (ts.course_key or ts.course_code, ts.section)
        state = state_lookup.get(key)
        if not state:
            skipped += len(pls)
            continue

        policy = get_course_meeting_policy(ts.course_code, ts.course_key, ts.course_number)
        if policy:
            _validated_policy_placement_pattern(policy, pls)
            if not policy.accepts(state.meetings):
                raise ValueError(f"{key[0]} must use one complete English weekly pattern")
            # A lock anywhere in a shared section protects its entire bundle.
            if any(p.is_locked for p in pls):
                skipped += len(pls)
                continue
            meetings_by_day = {WEEKDAYS[m.day]: m for m in state.meetings}
            for pl in pls:
                meeting = meetings_by_day.get(pl.day.upper())
                if meeting is None:
                    raise ValueError(f"{key[0]} contains an invalid English teaching day")
                new_start = f"{meeting.start_min // 60:02d}:{meeting.start_min % 60:02d}"
                new_end = f"{meeting.end_min // 60:02d}:{meeting.end_min % 60:02d}"
                if pl.start_time != new_start or pl.end_time != new_end:
                    TermSectionMeeting.objects.filter(
                        term_section_id=pl.term_section_id,
                        day=pl.day,
                        start_time=pl.start_time,
                        end_time=pl.end_time,
                    ).update(start_time=new_start, end_time=new_end)
                    pl.start_time, pl.end_time = new_start, new_end
                    bundle_updates.append(pl)
                    updated += 1
                else:
                    skipped += 1
            continue

        # Match placement count to meeting count
        if len(pls) != len(state.meetings):
            logger.warning(
                "Meeting count mismatch for %s: %d placements vs %d meetings",
                key,
                len(pls),
                len(state.meetings),
            )
            skipped += len(pls)
            continue

        # Sort both by day to align them
        pls_sorted = sorted(pls, key=lambda p: _DAY_IDX.get(p.day.upper(), 99))
        meetings_sorted = sorted(state.meetings, key=lambda m: m.day)

        for pl, meeting in zip(pls_sorted, meetings_sorted, strict=False):
            new_day = WEEKDAYS[meeting.day]
            new_start = f"{meeting.start_min // 60:02d}:{meeting.start_min % 60:02d}"
            new_end = f"{meeting.end_min // 60:02d}:{meeting.end_min % 60:02d}"

            changed = pl.day != new_day or pl.start_time != new_start or pl.end_time != new_end
            if changed and not pl.is_locked:
                pl.day = new_day
                pl.start_time = new_start
                pl.end_time = new_end
                # Clear room so assign_rooms_to_board() can reassign
                # per-meeting (75min→lecture, 100min of 4cr→lab).
                pl.room = ""
                to_update.append(pl)
                updated += 1
            else:
                skipped += 1

    if bundle_updates:
        SectionPlacement.objects.bulk_update(bundle_updates, ["start_time", "end_time"])

    if to_update:
        # Delete-and-recreate instead of bulk_update to avoid UNIQUE
        # constraint violations when day/start_time changes (the constraint
        # is on board_id + term_section_id + day + start_time).
        pks_to_delete = [pl.pk for pl in to_update]
        new_placements = [
            SectionPlacement(
                board_id=pl.board_id,
                term_section_id=pl.term_section_id,
                day=pl.day,
                start_time=pl.start_time,
                end_time=pl.end_time,
                room=pl.room,
                is_locked=pl.is_locked,
            )
            for pl in to_update
        ]
        SectionPlacement.objects.filter(pk__in=pks_to_delete).delete()
        SectionPlacement.objects.bulk_create(new_placements)

    logger.info("Persisted section states: %d updated, %d skipped", updated, skipped)
    return {"updated": updated, "skipped": skipped}


# ── Orchestrator ─────────────────────────────────────────────────


ALL_STRATEGIES = [
    "compact",
    "morning",
    "balanced",
    "load_balanced",
    "optimal",
    "hybrid",
    "adaptive",
]


def _reset_unlocked_placements(scenario_id: int) -> None:
    """Delete unlocked placements AND their meeting rows for a fresh placement run.

    Thin wrapper over ``timetable_board_persistence.reset_scenario`` (the single
    reset implementation). Clearing meetings alongside placements keeps each run
    starting from a clean slate: otherwise a section that lands on a DIFFERENT
    slot in a later strategy keeps its earlier meeting rows, so
    ``len(placements) != len(meetings)`` and ``persist_section_states_to_scenario``
    SKIPS it. Locked placements are the user's hard constraints and survive
    (their meetings too).
    """
    from core.services.timetable_board_persistence import reset_scenario

    reset_scenario(scenario_id, keep_locked=True)


def _generate_candidates_for_scenario(
    scenario_id: int,
    strategies: list[str] | None = None,
) -> list[dict]:
    """Generate multiple timetable candidates using different strategies.

    Each candidate is produced by running auto_place_scenario with a
    different strategy, then reading back the resulting SectionPlacement
    rows and converting them to SectionState lists.
    """
    from core.services.timetable_autoplace import auto_place_scenario

    if strategies is None:
        strategies = list(ALL_STRATEGIES)

    # Candidates are only ranked to pick a winning strategy; the winner is then
    # re-placed and fully polished. So the heavy strategies' per-board solvers
    # (which on realistic multi-board scenarios produce compact-equivalent
    # candidates) run under a tight budget here — set to 0 to disable.
    _cand_budget = getattr(settings, "TIMETABLE_CANDIDATE_GEN_BUDGET_SECONDS", 0) or None

    candidates: list[dict] = []

    for idx, strategy in enumerate(strategies):
        # Clear existing unlocked placements AND their meetings for a fresh run
        # (see _reset_unlocked_placements — leaving stale meetings causes a
        # placements != meetings drift that makes the persist step skip sections).
        # Locked placements are the user's hard constraints and must survive a
        # full rebuild so the auto-placer can reserve those cells.
        _reset_unlocked_placements(scenario_id)

        logger.info(
            "Generating candidate %d/%d with strategy=%s", idx + 1, len(strategies), strategy
        )
        try:
            auto_place_scenario(scenario_id, strategy=strategy, candidate_gen_budget=_cand_budget)
        except Exception:
            logger.exception("Strategy %s failed, skipping", strategy)
            continue

        # Read back the generated placements as SectionState
        sections = build_section_states_for_scenario(scenario_id)
        if sections:
            candidates.append(
                {
                    "id": f"{strategy}_{idx}",
                    "sections": sections,
                }
            )

    return candidates


def _build_pattern_catalog_for_scenario(scenario_id: int) -> dict[str, list]:
    """Build canonical pattern catalog from scenario budget data."""
    from core.services.timetable_autoplace import get_meeting_pattern
    from core.services.timetable_pattern_catalog import build_canonical_pattern_catalog

    budgets = ScenarioSectionBudget.objects.filter(scenario_id=scenario_id)
    scenario = TimetableScenario.objects.get(pk=scenario_id)
    course_requirements = []
    for b in budgets:
        policy = get_course_meeting_policy(b.course_code, b.course_key)
        durations = get_meeting_pattern(b.credit_hours, b.course_key or b.course_code)
        course_requirements.append(
            {
                "durations": durations,
                "course_code": b.course_code,
                "course_key": b.course_key,
                "credit_hours": b.credit_hours,
                "has_lab": b.credit_hours == 4 and not policy,
                "modality": "ONCAMPUS",
                "allow_permutations": b.credit_hours == 4 and not policy,
            }
        )
    return build_canonical_pattern_catalog(
        course_requirements=course_requirements,
        slot_config=scenario.slot_config or None,
        lab_slot_config=scenario.lab_slot_config or None,
        blocked_slots=scenario.blocked_slots or None,
    )


def refresh_final_score_after_teacher_recompute(
    result: dict,
    scenario_id: int,
    student_profiles: dict,
    course_rigidity: dict,
    section_instructor_ids: dict | None,
    course_tiers: dict | None,
    *,
    changed: bool,
) -> None:
    """Re-score the persisted board after the final teacher recompute.

    Twin of :func:`refresh_final_score_after_required_section_repair`: the
    one-teacher map can change at the end of a run without a placement moving,
    and every instructor-dependent field must describe the map that was kept.
    No-op when the map did not change.
    """
    if not changed or not student_profiles:
        return
    sections = build_section_states_for_scenario(scenario_id)
    if not sections:
        return
    post = evaluate_generated_timetable_candidate(
        candidate_id="post_teacher_recompute",
        generated_sections=sections,
        student_profiles=student_profiles,
        course_rigidity=course_rigidity,
        section_instructor_ids=section_instructor_ids,
        course_tiers=course_tiers,
    )
    result["final_score"] = list(post.lexicographic_score)
    result["unresolved_students"] = len(post.unresolved_student_ids)
    result["quality_score"] = post.quality_score
    result["hotspot_courses"] = post.hotspot_courses[:10]
    result["capacity_pressure_courses"] = post.capacity_pressure_courses[:10]
    result["reserve_heavy_sections"] = [
        {"section": s, "ratio": round(r, 2)} for s, r in post.reserve_heavy_sections[:10]
    ]


def _teacher_stage(assignment, **extra) -> dict:
    from core.services.section_teacher_assignment import stage_report

    return stage_report(assignment, **extra)


class _FullRunTeachers:
    """One teacher per section through a V2 Full run (Phase 1 spec 3.1, G1-G5).

    Every map is pinned on ``stack`` as it comes into force, so every consumer
    of the run — handed the map or reading it — charges the same teachers.
    """

    def __init__(self, scenario_id: int, stack) -> None:
        self.scenario_id = scenario_id
        self.stack = stack
        self.build = self.previous = self.win = None
        self.report: dict = {"enabled": True}

    def _pin(self, assignment) -> None:
        from core.services.section_teacher_assignment import pinned_assignment

        self.stack.enter_context(pinned_assignment(self.scenario_id, assignment))

    def rebuild(self) -> None:
        """G1: the time-blind pre-build map the candidates are built and ranked under."""
        from core.services.section_teacher_assignment import prepare_section_teachers

        prepared = prepare_section_teachers(self.scenario_id, phase="rebuild")
        self.build, self.previous = prepared["assignment"], prepared["previous"]
        self.report["pre_build"] = _teacher_stage(self.build)
        self._pin(self.build)

    def choose_winner(self, best_sections) -> dict:
        """G2: the exact map on the winning candidate's layout, stable against
        the map the scenario had before this run; it keeps nothing."""
        from core.services.section_teacher_assignment import propose_layout_assignment

        self.win = propose_layout_assignment(
            self.scenario_id, sections=best_sections, current=self.previous
        )
        self.report["winner"] = _teacher_stage(self.win)
        self._pin(self.win)
        return self.win.v2_map()

    def building(self):
        """G3: the winner's skeleton is re-placed exactly as it was generated."""
        from core.services.section_teacher_assignment import pinned_assignment

        return pinned_assignment(self.scenario_id, self.build)

    def persist_winner(self) -> None:
        """G4: store the winner map with the winning board and pin what is stored."""
        from core.services.section_teacher_assignment import persist_and_read

        self.win, persisted = persist_and_read(self.scenario_id, self.win)
        self.report["winner"]["persist"] = persisted
        self._pin(self.win)

    def write_names(self) -> None:
        from core.services.section_teacher_assignment import write_names

        write_names(self.scenario_id, self.win)

    def final(self) -> tuple[dict, bool]:
        """G5: re-pick on the final board; adopt it only if it is no worse."""
        chosen, changed, final = _final_teacher_recompute(self.scenario_id, self.win)
        self.report["final"] = final
        self.report["coverage"] = final["coverage"]
        self.win = chosen
        self._pin(chosen)
        return chosen.v2_map(), changed


def _final_teacher_recompute(scenario_id: int, in_force, *, bounds=None, gate=None):
    """K = the map in force closed on the final board; M_end = the exact re-pick,
    stable against it and keeping its violations. M_end is adopted iff its
    (TBD, TBD position, over-12, days, idle) key is no worse than K's, it adds
    no clash or over-cap session K does not have, and ``gate(M_end)`` (if any)
    passes. The chosen map is persisted and its names written."""
    from core.services.section_teacher_assignment import (
        adds_violations,
        assignment_key,
        assignment_scope,
        close_on_layout,
        persist_and_read,
        propose_layout_assignment,
        write_names,
    )

    with assignment_scope(scenario_id):  # one read of the stored rows
        closed = close_on_layout(scenario_id, in_force, bounds=bounds)
        proposed = propose_layout_assignment(
            scenario_id, current=in_force, keep_violations_of=in_force, bounds=bounds
        )
        reason = "adopted"
        if assignment_key(proposed) > assignment_key(closed):
            reason = "worse_than_kept_map"
        elif adds_violations(proposed, closed):
            reason = "adds_violations"
        elif gate is not None and not gate(proposed):
            reason = "instructor_week_regression"
        chosen = proposed if reason == "adopted" else closed
        stored, persisted = persist_and_read(scenario_id, chosen)
    write_names(scenario_id, stored)
    changed = stored.v2_map() != in_force.v2_map()
    final = _teacher_stage(
        stored,
        adopted=reason == "adopted",
        reason=reason,
        persist=persisted,
        changed_sections=sorted(
            key
            for key in set(stored.teacher) | set(in_force.teacher)
            if stored.teacher.get(key) != in_force.teacher.get(key)
        ),
    )
    return stored, changed, final


def optimise_scenario_timetable_v2(
    scenario_id: int,
    strategies: list[str] | None = None,
    run_local_search: bool = True,
    max_search_iterations: int = 50,
    run_chain_search: bool = True,
    max_chain_iterations: int = 10,
    run_cpsat_polish: bool = True,
    cpsat_time_limit: float = 60.0,
    cpsat_hotspot_only: bool = False,
    baseline_placements: dict | None = None,
    chain_time_limit_seconds: float | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> dict:
    """Full optimisation pipeline: generate → rank → improve → persist.

    See :func:`_optimise_scenario_timetable_v2`. With one teacher per section
    (``is_one_teacher_active``) the run also chooses each section's teacher
    (``_FullRunTeachers``) and pins every map it uses until it returns.
    """
    from contextlib import ExitStack

    from core.services.timetable_flags import is_one_teacher_active

    kwargs = {
        "strategies": strategies,
        "run_local_search": run_local_search,
        "max_search_iterations": max_search_iterations,
        "run_chain_search": run_chain_search,
        "max_chain_iterations": max_chain_iterations,
        "run_cpsat_polish": run_cpsat_polish,
        "cpsat_time_limit": cpsat_time_limit,
        "cpsat_hotspot_only": cpsat_hotspot_only,
        "baseline_placements": baseline_placements,
        "chain_time_limit_seconds": chain_time_limit_seconds,
        "checkpoint": checkpoint,
    }
    if not is_one_teacher_active():
        return _optimise_scenario_timetable_v2(scenario_id, **kwargs)
    with ExitStack() as stack:
        return _optimise_scenario_timetable_v2(
            scenario_id, teachers=_FullRunTeachers(scenario_id, stack), **kwargs
        )


def _optimise_scenario_timetable_v2(
    scenario_id: int,
    strategies: list[str] | None = None,
    run_local_search: bool = True,
    max_search_iterations: int = 50,
    run_chain_search: bool = True,
    max_chain_iterations: int = 10,
    run_cpsat_polish: bool = True,
    cpsat_time_limit: float = 60.0,
    cpsat_hotspot_only: bool = False,
    baseline_placements: dict | None = None,
    chain_time_limit_seconds: float | None = None,
    checkpoint: Callable[[], None] | None = None,
    teachers: _FullRunTeachers | None = None,
) -> dict:
    """Full optimisation pipeline: generate → rank → improve → persist.

    Pipeline:
      1. Build student profiles + course rigidity from DB
      2. Generate N timetable candidates (one per strategy)
      3. Rank all candidates by student-assignability score
      4. Run diagnostic local search on the best candidate
      4b. Run chain-2 local search (cross-board coordinated moves)
      4c. Run global CP-SAT polisher (cross-board solver pass)
      5. Re-place the winning strategy and persist to DB

    Parameters
    ----------
    scenario_id : int
        PK of TimetableScenario.
    strategies : list[str] | None
        Strategies to try. Defaults to all strategies.
    run_local_search : bool
        Run single-move local search (default True).
    max_search_iterations : int
        Max iterations for single-move local search.
    run_chain_search : bool
        Run chain-2 local search after single-move (default True).
    max_chain_iterations : int
        Max iterations for chain search.
    run_cpsat_polish : bool
        Run global CP-SAT polisher after chain search (default True).
    cpsat_time_limit : float
        Time budget for CP-SAT solver in seconds.
    cpsat_hotspot_only : bool
        If True, only polish hotspot courses + partners (faster).
    baseline_placements : dict | None
        PR3 commit 6 — optional scenario-wide warm-start baseline. Passed
        straight through to ``auto_place_scenario`` at step 5 so the
        greedy re-placement can retain any baseline slot that survives
        feasibility. No DB persistence: the baseline is caller-supplied
        and in-memory only (DoR §warm-start-scope).
    """
    import time

    t0 = time.time()
    logger.info("V2 optimisation starting for scenario %d", scenario_id)

    # ── Step 1: Build student profiles and course rigidity ──
    student_profiles = build_student_profiles_for_scenario(scenario_id)
    course_rigidity = build_course_rigidity_for_scenario(scenario_id)

    if not student_profiles:
        return {
            "error": "No student profiles found for scenario",
            "candidates_evaluated": 0,
            "decision_trace": {},
            # PR3 commit 6 — schema-stable empty metric on early-return.
            "perturbation_metric": {
                "changes_from_baseline_count": 0,
                "unchanged_count": 0,
                "newly_placed_count": 0,
                "removed_count": 0,
            },
            # PR6 commit 5 — schema-stable empty stage_telemetry on early-return.
            "stage_telemetry": empty_stage_telemetry(),
        }

    t1 = time.time()
    logger.info("Profiles built in %.1fs (%d students)", t1 - t0, len(student_profiles))

    # ── Step 2: Generate candidates with multiple strategies ──
    if teachers is not None:
        # One teacher per section: after the ownership check, a time-blind map
        # every candidate is built and ranked under (G1).
        teachers.rebuild()
    candidates = _generate_candidates_for_scenario(scenario_id, strategies)
    if not candidates:
        return {
            "error": "No candidates generated",
            "candidates_evaluated": 0,
            "decision_trace": {},
            # PR3 commit 6 — schema-stable empty metric on early-return.
            "perturbation_metric": {
                "changes_from_baseline_count": 0,
                "unchanged_count": 0,
                "newly_placed_count": 0,
                "removed_count": 0,
            },
            # PR6 commit 5 — schema-stable empty stage_telemetry on early-return.
            "stage_telemetry": empty_stage_telemetry(),
        }

    locked_section_ids = build_locked_section_ids_for_scenario(scenario_id)

    # Priority uses current instructor links independently of the legacy idle
    # score flag. The latter still controls public score shape/values.
    section_instructor_ids = (
        build_section_instructor_map_for_scenario(scenario_id)
        if (
            is_instructor_priority_enabled()
            or is_instructor_gap_penalty_enabled()
            or is_instructor_daily_cap_enabled()
            or is_instructor_clash_enabled()
        )
        else None
    )

    # Built once per run and threaded (same object) to every evaluator call:
    # None when the tiered objective is OFF (every stage scores the legacy tuple
    # → byte parity), the course→tier map when ON (every stage scores a uniform
    # 9-tuple). Uniform threading is what guarantees no mixed-length comparison.
    course_tiers = (
        build_course_tier_map_for_scenario(scenario_id) if is_tiered_objective_enabled() else None
    )

    t2 = time.time()
    logger.info("Generated %d candidates in %.1fs", len(candidates), t2 - t1)

    # ── Step 3: Rank all candidates by student-assignability ──
    if checkpoint:
        checkpoint()
    ranked = rank_timetable_candidates(
        candidate_list=candidates,
        student_profiles=student_profiles,
        course_rigidity=course_rigidity,
        section_instructor_ids=section_instructor_ids,
        course_tiers=course_tiers,
    )
    best = ranked[0]
    if teachers is not None:
        # G2: choose the teachers for the winning layout, then judge the winner
        # under them — every later stage and every reported field uses this map
        # (``all_scores`` stays as ranked, under the pre-build map).
        best_sections = next(c["sections"] for c in candidates if c["id"] == best.candidate_id)
        winner_map = teachers.choose_winner(best_sections)
        if section_instructor_ids is not None:
            section_instructor_ids = winner_map
        best = evaluate_generated_timetable_candidate(
            candidate_id=best.candidate_id,
            generated_sections=best_sections,
            student_profiles=student_profiles,
            course_rigidity=course_rigidity,
            section_instructor_ids=section_instructor_ids,
            course_tiers=course_tiers,
        )
    _gap_idle_before = _gap_pos6(best.lexicographic_score)

    t3 = time.time()
    logger.info(
        "Ranked %d candidates in %.1fs — best=%s score=%s",
        len(ranked),
        t3 - t2,
        best.candidate_id,
        best.lexicographic_score,
    )

    result = {
        "candidates_evaluated": len(ranked),
        "best_candidate_id": best.candidate_id,
        "best_score": list(best.lexicographic_score),
        "optimisation_policy": "instructor_first" if is_instructor_priority_enabled() else "legacy",
        "baseline_instructor_schedule": schedule_metrics_of(best),
        "hotspot_courses": best.hotspot_courses[:10],
        "capacity_pressure_courses": best.capacity_pressure_courses[:10],
        "reserve_heavy_sections": [
            {"section": s, "ratio": round(r, 2)} for s, r in best.reserve_heavy_sections[:10]
        ],
        "quality_score": best.quality_score,
        "unresolved_students": len(best.unresolved_student_ids),
        "total_students": len(student_profiles),
        "local_search_applied": False,
        "final_score": list(best.lexicographic_score),
        "persist_result": None,
        "all_scores": [
            {
                "id": r.candidate_id,
                "score": list(r.lexicographic_score),
                "selection_key": list(evaluation_order_key(r)),
                "instructor_schedule": schedule_metrics_of(r),
                "quality_penalty": int((r.quality_score or {}).get("penalty") or 0),
                "quality_components": (r.quality_score or {}).get("components", {}),
            }
            for r in ranked
        ],
        # PR3 commit 4 — decision_trace preservation across V2 pipeline.
        # Populated from the greedy re-placement at step 5; carried through
        # the optimiser unchanged. Per commit-4 rulings J3/K3/L2, V2 local
        # search / chain search / CP-SAT polish do NOT update the trace
        # (chosen_* fields reflect the cold-start placement, not post-LS
        # moves). Schema-stability: the key is always present.
        "decision_trace": {},
        # PR3 commit 6 — scenario-level perturbation metric. Seeded
        # empty here so schema stability holds across every V2 exit path
        # (no-profile, no-candidates, success). Overwritten at step 5 by
        # the auto_place_scenario return value when baseline was supplied.
        "perturbation_metric": {
            "changes_from_baseline_count": 0,
            "unchanged_count": 0,
            "newly_placed_count": 0,
            "removed_count": 0,
        },
        # PR6 commit 5 — scenario-level stage_telemetry. Seeded empty here
        # so the schema is stable on every V2 exit path. CP-SAT polisher
        # populates cpsat.ms/iterations in-place when the flag is on and
        # the solver is actually invoked (not merely enabled by config).
        # Other stages' keys are wired in later PR6 commits.
        "stage_telemetry": empty_stage_telemetry(),
    }

    # The evaluated winner's sections, built once and mutated in place by the
    # local-search, chain, and CP-SAT stages below. After improvements this
    # holds the EXACT board that produced ``result["final_score"]``; step 5
    # persists it verbatim (no re-derivation) so the saved board cannot drift
    # from the reported score.
    from core.services import timetable_student_assignment as ssa

    best_sections = next((c["sections"] for c in candidates if c["id"] == best.candidate_id), None)
    sections_by_id = ssa.build_sections_by_id(best_sections) if best_sections else {}
    pattern_catalog = _build_pattern_catalog_for_scenario(scenario_id) if best_sections else {}

    # ── Step 4: Local search on the best candidate ──
    if checkpoint:
        checkpoint()
    if run_local_search:
        from core.services import timetable_student_assignment as ssa
        from core.services.timetable_local_search_v2 import diagnostic_driven_local_search

        # Find the best candidate's sections
        best_sections = None
        for c in candidates:
            if c["id"] == best.candidate_id:
                best_sections = c["sections"]
                break

        if best_sections:
            sections_by_id = ssa.build_sections_by_id(best_sections)
            pattern_catalog = _build_pattern_catalog_for_scenario(scenario_id)

            # Room repair is DISABLED in the optimizer. The auto-placer
            # handles per-meeting room types correctly (75min→lecture,
            # 100min→lab). The optimizer only moves time patterns;
            # rooms are reassigned by assign_rooms_to_board() after persist.
            logger.info("Running local search (max %d iterations)...", max_search_iterations)
            improved = diagnostic_driven_local_search(
                best_candidate=best,
                sections_by_id=sections_by_id,
                pattern_catalog=pattern_catalog,
                student_profiles=student_profiles,
                course_rigidity=course_rigidity,
                rooms_by_id=None,
                room_occupancies=None,
                course_room_requirements=None,
                max_iterations=max_search_iterations,
                locked_section_ids=locked_section_ids,
                section_instructor_ids=section_instructor_ids,
                course_tiers=course_tiers,
            )

            score_before = best.lexicographic_score
            score_after = improved.lexicographic_score
            result["local_search_applied"] = True
            result["score_before_local_search"] = list(score_before)
            result["final_score"] = list(score_after)
            result["hotspot_courses"] = improved.hotspot_courses[:10]
            result["capacity_pressure_courses"] = improved.capacity_pressure_courses[:10]
            result["quality_score"] = improved.quality_score
            result["reserve_heavy_sections"] = [
                {"section": s, "ratio": round(r, 2)}
                for s, r in improved.reserve_heavy_sections[:10]
            ]
            result["unresolved_students"] = len(improved.unresolved_student_ids)

            if evaluation_is_better(improved, best):
                logger.info("Local search improved score: %s → %s", score_before, score_after)
            else:
                logger.info("Local search found no improvement (score unchanged)")

            t4 = time.time()
            logger.info("Local search completed in %.1fs", t4 - t3)

    # ── Step 4b: Chain-2 local search ──
    if checkpoint:
        checkpoint()
    # Single-move search can get stuck when improvement requires moving
    # TWO sections simultaneously (e.g. section A blocks section B's best
    # slot — moving A alone doesn't help, but moving A+B together does).
    # Chain search explores these coordinated 2-section moves.
    current_eval_for_chain = None
    if run_chain_search:
        # Need sections_by_id and pattern_catalog — build if not already done
        if "sections_by_id" not in dir() or not sections_by_id:
            from core.services import timetable_student_assignment as ssa

            best_sections = None
            for c in candidates:
                if c["id"] == best.candidate_id:
                    best_sections = c["sections"]
                    break
            if best_sections:
                sections_by_id = ssa.build_sections_by_id(best_sections)
                pattern_catalog = _build_pattern_catalog_for_scenario(scenario_id)

        if "sections_by_id" in dir() and sections_by_id:
            from core.services.timetable_local_search_chains import chain_local_search

            # Build current eval for chain search input
            current_eval_for_chain = evaluate_generated_timetable_candidate(
                candidate_id="pre_chain",
                generated_sections=list(sections_by_id.values()),
                student_profiles=student_profiles,
                course_rigidity=course_rigidity,
                section_instructor_ids=section_instructor_ids,
                course_tiers=course_tiers,
            )

            t_chain_start = time.time()
            logger.info("Running chain-2 search (max %d iterations)...", max_chain_iterations)
            chain_trace_out: dict[str, dict] = {}
            chain_result = chain_local_search(
                best_candidate=current_eval_for_chain,
                sections_by_id=sections_by_id,
                pattern_catalog=pattern_catalog,
                student_profiles=student_profiles,
                course_rigidity=course_rigidity,
                rooms_by_id=None,
                room_occupancies=None,
                course_room_requirements=None,
                max_iterations=max_chain_iterations,
                decision_trace_out=chain_trace_out,
                stage_telemetry=result["stage_telemetry"],
                locked_section_ids=locked_section_ids,
                section_instructor_ids=section_instructor_ids,
                course_tiers=course_tiers,
                chain_time_limit_seconds=(
                    chain_time_limit_seconds
                    if chain_time_limit_seconds is not None
                    else getattr(settings, "TIMETABLE_CHAIN_TIME_LIMIT_SECONDS", 60.0)
                ),
            )

            if evaluation_is_better(chain_result, current_eval_for_chain):
                logger.info(
                    "Chain search improved: %s -> %s",
                    current_eval_for_chain.lexicographic_score,
                    chain_result.lexicographic_score,
                )
                result["chain_search_applied"] = True
                result["score_before_chain"] = list(current_eval_for_chain.lexicographic_score)
                result["final_score"] = list(chain_result.lexicographic_score)
                result["hotspot_courses"] = chain_result.hotspot_courses[:10]
                result["quality_score"] = chain_result.quality_score
                result["unresolved_students"] = len(chain_result.unresolved_student_ids)
                current_eval_for_chain = chain_result
                # PR5 commit 5: overlay chain trace (last-changer-wins).
                for k, v in chain_trace_out.items():
                    result["decision_trace"][k] = v
            else:
                result["chain_search_applied"] = False
                logger.info("Chain search: no improvement found")

            t_chain_end = time.time()
            logger.info("Chain search completed in %.1fs", t_chain_end - t_chain_start)

    # ── Step 4c: Global CP-SAT polisher ──
    if checkpoint:
        checkpoint()
    # After heuristic search exhausts, run a global constraint solver
    # across ALL boards. This can find improvements invisible to local
    # moves because it considers the entire search space at once.
    # Acceptance gate: only keep the CP-SAT result if the full student-
    # assignment evaluator confirms a strict lexicographic improvement.
    if run_cpsat_polish:
        if "sections_by_id" in dir() and sections_by_id:
            from core.services.timetable_cpsat_polisher import polish_scenario_with_cpsat

            # Use the latest evaluation as baseline
            if current_eval_for_chain is None:
                current_eval_for_chain = evaluate_generated_timetable_candidate(
                    candidate_id="pre_cpsat",
                    generated_sections=list(sections_by_id.values()),
                    student_profiles=student_profiles,
                    course_rigidity=course_rigidity,
                    section_instructor_ids=section_instructor_ids,
                    course_tiers=course_tiers,
                )

            t_cpsat_start = time.time()
            logger.info(
                "Running CP-SAT polisher (%.0fs limit, hotspot_only=%s)...",
                cpsat_time_limit,
                cpsat_hotspot_only,
            )
            cpsat_result = polish_scenario_with_cpsat(
                scenario_id=scenario_id,
                current_sections=list(sections_by_id.values()),
                student_profiles=student_profiles,
                course_rigidity=course_rigidity,
                current_eval=current_eval_for_chain,
                time_limit_seconds=cpsat_time_limit,
                hotspot_only=cpsat_hotspot_only,
                stage_telemetry=result["stage_telemetry"],
                locked_section_ids=locked_section_ids,
                section_instructor_ids=section_instructor_ids,
                course_tiers=course_tiers,
            )

            if cpsat_result is not None:
                cpsat_eval = cpsat_result["eval"]
                result["cpsat_polish_applied"] = True
                result["score_before_cpsat"] = list(current_eval_for_chain.lexicographic_score)
                result["final_score"] = list(cpsat_eval.lexicographic_score)
                result["hotspot_courses"] = cpsat_eval.hotspot_courses[:10]
                result["quality_score"] = cpsat_eval.quality_score
                result["unresolved_students"] = len(cpsat_eval.unresolved_student_ids)
                # LEAK FIX: overlay improved sections into sections_by_id
                for improved in cpsat_result["improved_sections"]:
                    sections_by_id[improved.section_id] = improved
                cpsat_decision_trace = cpsat_result["decision_trace"]
                # Update sections_by_id with polished sections
                # (cpsat_result evaluated with the improved sections)
                logger.info("CP-SAT polisher accepted improvement")
            else:
                result["cpsat_polish_applied"] = False
                cpsat_decision_trace = {}
                logger.info("CP-SAT polisher: no improvement")

            t_cpsat_end = time.time()
            logger.info("CP-SAT polisher completed in %.1fs", t_cpsat_end - t_cpsat_start)
        else:
            cpsat_decision_trace = {}
    else:
        cpsat_decision_trace = {}

    # ── Step 5: Persist to DB ──
    if checkpoint:
        checkpoint()
    # Re-place using the winning strategy first (because candidate
    # generation deletes all placements). Then overlay local search /
    # chain / CP-SAT improvements on top — these are stored as deltas
    # in sections_by_id, not as full placements.
    # First: re-place using the winning strategy (baseline placement). Clear the
    # winner's stale meetings too (see _reset_unlocked_placements): the re-placement
    # below otherwise inherits meeting rows accumulated across the candidate
    # strategies, so persist_section_states_to_scenario would skip the drifted
    # sections instead of saving the evaluated winner's improvements.
    winning_strategy = best.candidate_id.rsplit("_", 1)[0]
    _reset_unlocked_placements(scenario_id)
    from contextlib import nullcontext

    from core.services.timetable_autoplace import auto_place_scenario

    # One teacher per section: the skeleton is rebuilt under the map it was
    # generated with, so it is the same skeleton (G3).
    with teachers.building() if teachers is not None else nullcontext():
        scenario_place_result = auto_place_scenario(
            scenario_id,
            strategy=winning_strategy,
            baseline_placements=baseline_placements,
        )
    # PR3 commit 4: capture the scenario-level greedy trace. Per commit-4
    # ruling J3 we preserve this cold-start trace through the remaining V2
    # pipeline unchanged; LS / chain / CP-SAT do not mutate it.
    result["decision_trace"] = scenario_place_result.get("decision_trace", {})
    # Overlay CP-SAT decision trace (last-changer-wins)
    for k, v in cpsat_decision_trace.items():
        result["decision_trace"][k] = v
    # PR3 commit 6: capture the scenario-level perturbation metric from the
    # greedy re-placement. Like decision_trace, this reflects cold-start
    # placement (pre-LS); we intentionally do NOT recompute after local
    # search / chain / CP-SAT because those paths don't observe baseline
    # either. Schema-stable: the key is always present (seeded at result
    # init; overwritten here with real values when auto_place_scenario
    # returned them).
    if "perturbation_metric" in scenario_place_result:
        result["perturbation_metric"] = scenario_place_result["perturbation_metric"]
    # PR6 commit 7 — scenario-level greedy telemetry aggregation. Each
    # board's auto_place_board records greedy.ms/iterations into its own
    # stage_telemetry, and auto_place_scenario sums those. Fold that
    # scenario-level sum into the V2 result so scenario_sum == board_sum
    # across the full pipeline (DoR §3 aggregation rule).
    _scen_tel = scenario_place_result.get("stage_telemetry") or {}
    if isinstance(_scen_tel, dict):
        for _k in STAGE_KEYS:
            result["stage_telemetry"]["stage_ms"][_k] += int(
                _scen_tel.get("stage_ms", {}).get(_k, 0)
            )
            result["stage_telemetry"]["stage_iterations"][_k] += int(
                _scen_tel.get("stage_iterations", {}).get(_k, 0)
            )
    # PR5 commit 7 — always seed ``changes_by_stage`` so the schema is
    # stable flag-on or flag-off. When no baseline is provided the
    # changed-from-baseline set is empty, so all five buckets are 0 and
    # the invariant ``sum == changes_from_baseline_count`` holds trivially.
    from core.services.timetable_stage_summary import (
        compute_changes_by_stage as _compute_cbs,
    )

    _pm = result.get("perturbation_metric") or {}
    _changed_codes: set[str] = set()  # baseline=None in this path
    _pm["changes_by_stage"] = _compute_cbs(result.get("decision_trace") or {}, _changed_codes)
    result["perturbation_metric"] = _pm

    # ── Persist exactly the evaluated winner ──
    # The re-placement above rebuilt the row skeleton for the winning
    # strategy. ``sections_by_id`` holds the best candidate's sections after
    # greedy → local search → chain → CP-SAT (every stage evaluator-gated),
    # so it IS the board that produced ``result["final_score"]``. Overlay it
    # unconditionally and atomically so the saved board can never drift from
    # the reported score — including the "no improvement" path, which
    # previously left the re-placed board untouched and could differ from the
    # evaluated candidate for nondeterministic (CP-SAT) winners.
    from django.db import transaction

    ls_persisted = False
    if sections_by_id:
        with transaction.atomic():
            persist_result = persist_section_states_to_scenario(scenario_id, sections_by_id)
            if teachers is not None:
                teachers.persist_winner()  # G4: with the board it was chosen for
        ls_persisted = True
        logger.info(
            "Persisted evaluated winner: %d placements updated, %d unchanged",
            persist_result.get("updated", 0),
            persist_result.get("skipped", 0),
        )

    result["persist_result"] = {
        "action": "persisted_evaluated_winner",
        "strategy": winning_strategy,
    }

    # Reassign rooms per-meeting after persist. The optimizer only moves
    # time patterns — room assignment is done here by the auto-placer's
    # per-meeting logic (75min→lecture room, 100min→lab room).
    if ls_persisted:
        from core.services.timetable_rooming import assign_rooms_to_board

        boards = DeliveryBoard.objects.filter(scenario_id=scenario_id)
        for board in boards:
            # Clear rooms on changed placements so assign_rooms_to_board can redo them
            SectionPlacement.objects.filter(
                board=board,
                room="UNASSIGNED",
                is_locked=False,
            ).exclude(term_section_id__in=external_shared_section_ids(scenario_id)).update(room="")
            rooming_result = assign_rooms_to_board(board.id, respect_locked=True)
            # PR5 commit 6: overlay rooming-repair trace (last-changer-wins).
            for k, v in (rooming_result.get("decision_trace") or {}).items():
                result["decision_trace"][k] = v
            # PR6 commit 6: sum board-level rooming_repair telemetry into
            # the scenario-level aggregate. stage_ms sums wall times,
            # stage_iterations sums reassignment counts across boards.
            _board_tel = rooming_result.get("stage_telemetry") or {}
            _board_ms = _board_tel.get("stage_ms", {}).get("rooming_repair", 0)
            _board_it = _board_tel.get("stage_iterations", {}).get("rooming_repair", 0)
            result["stage_telemetry"]["stage_ms"]["rooming_repair"] += int(_board_ms)
            result["stage_telemetry"]["stage_iterations"]["rooming_repair"] += int(_board_it)

        # Persist + rooming moved placements only; project them back onto the
        # meeting rows so the two board representations match before the
        # instructor repairs relocate on a (day, start)-matched basis.
        from core.services.timetable_board_persistence import sync_meetings_from_placements

        _sync = sync_meetings_from_placements(scenario_id)
        result["meeting_sync"] = {
            "sections": _sync.sections_synced,
            "written": _sync.meetings_written,
        }
    if teachers is not None:
        if not ls_persisted:
            teachers.persist_winner()
        teachers.write_names()  # each section's own teacher, under the winner map

    # Snapshot the safety state the OPTIMISER produced, BEFORE the mandatory
    # instructor repairs run. The runner's safety gate compares against this
    # (not the post-repair board) so a repair that accepts a warning-level
    # cross-course student overlap as the least-harm way to satisfy the hard cap
    # cannot trip a rollback that would reinstate the violation — mirroring the
    # student gate's score_before_instructor_passes. Only captured when a pass
    # will run, so flag-off leaves the runner falling back to the post-run state.
    if (
        is_instructor_daily_cap_enabled()
        or is_instructor_clash_enabled()
        or is_instructor_compaction_enabled()
    ):
        from core.services.timetable_workspace import compute_scenario_safety_summary

        result["safety_before_instructor_passes"] = compute_scenario_safety_summary(scenario_id)

    # Hard instructor daily-session cap — scenario-wide backstop. The structural
    # gates keep greedy/local/chain/CP-SAT compliant; this also catches any
    # residual overload (e.g. from the per-board SA polish) and pre-existing ones.
    # Flag-gated → no-op when off.
    if is_instructor_daily_cap_enabled():
        from core.services.timetable_instructor_cap_repair import (
            repair_instructor_daily_overloads,
        )

        result["instructor_cap_repair"] = repair_instructor_daily_overloads(scenario_id)

    # Instructor-clash repair — the optimise stages now reject clash-creating
    # moves, but an already-built board can hold a legacy double-booking; clear
    # any here. Flag-gated → no-op when off.
    if is_instructor_clash_enabled():
        from core.services.timetable_instructor_cap_repair import repair_instructor_clashes

        result["instructor_clash_repair"] = repair_instructor_clashes(scenario_id)

    # Instructor-day compaction — runs AFTER the cap + clash repairs (which it
    # treats as hard gates) to shrink each instructor's within-day idle gaps.
    capture_instructor_schedule_after_hard_repairs(result, scenario_id, section_instructor_ids)
    if checkpoint:
        checkpoint()
    if is_instructor_compaction_enabled():
        from core.services.timetable_instructor_compaction import compact_instructor_schedules

        result["instructor_compaction"] = compact_instructor_schedules(scenario_id)

    # The three passes above mutate the board AFTER final_score was written, so
    # re-score the persisted board or the reported numbers describe a board that
    # no longer exists (and the rollback gate judges the wrong one).
    refresh_final_score_after_instructor_passes(
        result,
        scenario_id,
        student_profiles,
        course_rigidity,
        section_instructor_ids,
        course_tiers,
    )

    from core.services.timetable_secondary_recovery_runner import (
        run_secondary_recovery_for_scenario,
    )

    if checkpoint:
        checkpoint()
    result["secondary_recovery"] = run_secondary_recovery_for_scenario(
        scenario_id, result, student_profiles, course_rigidity, section_instructor_ids, course_tiers
    )

    # Required-section guarantee: runs LAST, after every pass that can drop or
    # move a section (re-placement, local/chain/CP-SAT persist, instructor
    # cap/clash repair, compaction, secondary recovery), so it sees the true
    # final board. See core.services.timetable_required_sections for why.
    from core.services.timetable_required_sections import (
        maybe_reinstate_missing_required_sections,
    )

    if checkpoint:
        checkpoint()
    reinstatement = maybe_reinstate_missing_required_sections(scenario_id)
    if reinstatement is not None:
        result["required_section_reinstatement"] = reinstatement
        refresh_final_score_after_required_section_repair(
            result,
            scenario_id,
            student_profiles,
            course_rigidity,
            section_instructor_ids,
            course_tiers,
        )

    if teachers is not None:
        # G5, whether or not anything was reinstated: re-pick on the final board.
        final_map, changed = teachers.final()
        if section_instructor_ids is not None:
            section_instructor_ids = final_map
        refresh_final_score_after_teacher_recompute(
            result,
            scenario_id,
            student_profiles,
            course_rigidity,
            section_instructor_ids,
            course_tiers,
            changed=changed,
        )
        result["section_teachers"] = teachers.report

    elapsed = time.time() - t0
    result["elapsed_seconds"] = round(elapsed, 1)

    logger.info(
        "V2 optimisation complete in %.1fs: best=%s final_score=%s",
        elapsed,
        best.candidate_id,
        result["final_score"],
    )
    result["instructor_gap_metric"] = instructor_gap_metric(
        _gap_idle_before,
        result.get("final_score"),
        # The map is now also built for the cap/clash gates; the gap metric must
        # stay zeroed unless the gap penalty itself is on (byte-parity contract).
        section_instructor_ids if is_instructor_gap_penalty_enabled() else None,
    )
    return result


# ── Optimise Current ─────────────────────────────────────────────


def optimise_current_timetable(
    scenario_id: int,
    max_search_iterations: int = 50,
    run_local_search: bool = True,
    run_chain_search: bool = True,
    max_chain_iterations: int = 10,
    run_cpsat_polish: bool = True,
    cpsat_time_limit: float = 60.0,
    cpsat_hotspot_only: bool = False,
    chain_time_limit_seconds: float | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> dict:
    """Improve the CURRENT timetable without regenerating from scratch.

    Unlike optimise_scenario_timetable_v2(), this function:
      - Does NOT delete existing placements
      - Does NOT run auto_place_scenario()
      - Reads the current board state as-is (including manual tweaks)
      - Runs local search → chain search → CP-SAT polish on top
      - Persists only the improvements (respects locked placements)

    Use this when you've manually adjusted the board and want to
    improve it without losing your work.
    """
    import time

    t0 = time.time()
    logger.info("Optimise-current starting for scenario %d", scenario_id)

    # ── Step 1: Build context ──
    student_profiles = build_student_profiles_for_scenario(scenario_id)
    course_rigidity = build_course_rigidity_for_scenario(scenario_id)

    if not student_profiles:
        return {
            "error": "No student profiles found for scenario",
            "candidates_evaluated": 0,
            "decision_trace": {},
            # PR3 commit 6 — schema-stable empty metric on early-return.
            "perturbation_metric": {
                "changes_from_baseline_count": 0,
                "unchanged_count": 0,
                "newly_placed_count": 0,
                "removed_count": 0,
            },
            # PR6 commit 5 — schema-stable empty stage_telemetry on early-return.
            "stage_telemetry": empty_stage_telemetry(),
        }

    # ── Step 2: Read current placements as-is ──
    sections = build_section_states_for_scenario(scenario_id)
    if not sections:
        return {
            "error": "No placements found — nothing to optimise",
            "candidates_evaluated": 0,
            "decision_trace": {},
            # PR3 commit 6 — schema-stable empty metric on early-return.
            "perturbation_metric": {
                "changes_from_baseline_count": 0,
                "unchanged_count": 0,
                "newly_placed_count": 0,
                "removed_count": 0,
            },
            # PR6 commit 5 — schema-stable empty stage_telemetry on early-return.
            "stage_telemetry": empty_stage_telemetry(),
        }

    locked_section_ids = build_locked_section_ids_for_scenario(scenario_id)

    # Resolve current links for instructor priority even with the old gap-score
    # flag off; every stage receives the same map and unchanged score layout.
    section_instructor_ids = (
        build_section_instructor_map_for_scenario(scenario_id)
        if (
            is_instructor_priority_enabled()
            or is_instructor_gap_penalty_enabled()
            or is_instructor_daily_cap_enabled()
            or is_instructor_clash_enabled()
        )
        else None
    )

    # See optimise_scenario_timetable_v2: None ⇒ legacy tuples (byte parity),
    # the course→tier map ⇒ uniform 9-tuples, threaded to every call below.
    course_tiers = (
        build_course_tier_map_for_scenario(scenario_id) if is_tiered_objective_enabled() else None
    )

    from core.services import timetable_student_assignment as ssa

    sections_by_id = ssa.build_sections_by_id(sections)

    # Evaluate current state as baseline
    baseline = evaluate_generated_timetable_candidate(
        candidate_id="current",
        generated_sections=sections,
        student_profiles=student_profiles,
        course_rigidity=course_rigidity,
        section_instructor_ids=section_instructor_ids,
        course_tiers=course_tiers,
    )
    _gap_idle_before = _gap_pos6(baseline.lexicographic_score)

    t1 = time.time()
    logger.info(
        "Current state: %d sections, score=%s (%.1fs)",
        len(sections),
        baseline.lexicographic_score,
        t1 - t0,
    )

    result = {
        "mode": "current",
        "candidates_evaluated": 1,
        "best_candidate_id": "current",
        "baseline_score": list(baseline.lexicographic_score),
        "baseline_room_shortages": _baseline_room_shortages(scenario_id),
        "optimisation_policy": "instructor_first" if is_instructor_priority_enabled() else "legacy",
        "baseline_instructor_schedule": schedule_metrics_of(baseline),
        "best_score": list(baseline.lexicographic_score),
        "final_score": list(baseline.lexicographic_score),
        "hotspot_courses": baseline.hotspot_courses[:10],
        "capacity_pressure_courses": baseline.capacity_pressure_courses[:10],
        "reserve_heavy_sections": [
            {"section": s, "ratio": round(r, 2)} for s, r in baseline.reserve_heavy_sections[:10]
        ],
        "quality_score": baseline.quality_score,
        "unresolved_students": len(baseline.unresolved_student_ids),
        # Baseline student headcount kept separately so a safety rollback can
        # restore it without decoding the score tuple (the tiered tuple carries
        # no total-unresolved-students term). unresolved_students above gets
        # overwritten by later stages; this stays pinned to the baseline board.
        "baseline_unresolved_students": len(baseline.unresolved_student_ids),
        # Regular-only gap option: the baseline's gap split, pinned so a safety
        # rollback can restore it. Absent when the option is off.
        **(
            {"baseline_student_gap": dict(baseline.quality_score["student_gap"])}
            if "student_gap" in (baseline.quality_score or {})
            else {}
        ),
        "total_students": len(student_profiles),
        "local_search_applied": False,
        "persist_result": None,
        # PR3 commit 4: "Optimise Current" works off existing DB placements
        # and never calls auto_place_scenario, so no greedy trace is
        # captured in this path. Key included for schema stability.
        "decision_trace": {},
        # PR3 commit 6 — "Optimise Current" never observes a baseline
        # (it starts from existing DB state, not a caller-supplied
        # baseline), so the perturbation metric is all-zero by design.
        "perturbation_metric": {
            "changes_from_baseline_count": 0,
            "unchanged_count": 0,
            "newly_placed_count": 0,
            "removed_count": 0,
        },
        # PR6 commit 5 — scenario-level stage_telemetry seeded empty; the
        # CP-SAT polisher populates its keys when invoked with the flag on.
        "stage_telemetry": empty_stage_telemetry(),
    }

    # ── Step 3: Local search on current state ──
    if checkpoint:
        checkpoint()
    pattern_catalog = _build_pattern_catalog_for_scenario(scenario_id)

    if run_local_search:
        # Room repair is DISABLED — the auto-placer handles per-meeting room
        # types correctly (75min→lecture, 100min→lab). The optimizer only
        # moves time patterns; rooms are reassigned after persist.
        from core.services.timetable_local_search_v2 import diagnostic_driven_local_search

        t2 = time.time()
        logger.info(
            "Running local search on current timetable (max %d iterations)...",
            max_search_iterations,
        )
        improved = diagnostic_driven_local_search(
            best_candidate=baseline,
            sections_by_id=sections_by_id,
            pattern_catalog=pattern_catalog,
            student_profiles=student_profiles,
            course_rigidity=course_rigidity,
            rooms_by_id=None,
            room_occupancies=None,
            course_room_requirements=None,
            max_iterations=max_search_iterations,
            locked_section_ids=locked_section_ids,
            section_instructor_ids=section_instructor_ids,
            course_tiers=course_tiers,
        )

        score_before = baseline.lexicographic_score
        score_after = improved.lexicographic_score
        result["local_search_applied"] = True
        result["score_before_local_search"] = list(score_before)
        result["final_score"] = list(score_after)
        result["hotspot_courses"] = improved.hotspot_courses[:10]
        result["quality_score"] = improved.quality_score
        result["unresolved_students"] = len(improved.unresolved_student_ids)

        t3 = time.time()
        if evaluation_is_better(improved, baseline):
            logger.info(
                "Local search improved: %s -> %s (%.1fs)", score_before, score_after, t3 - t2
            )
        else:
            logger.info("Local search: no improvement (%.1fs)", t3 - t2)
    else:
        improved = baseline

    # ── Step 4: Chain-2 search ──
    if checkpoint:
        checkpoint()
    current_eval = improved
    if run_chain_search:
        from core.services.timetable_local_search_chains import chain_local_search

        t_chain = time.time()
        logger.info("Running chain-2 search (max %d iterations)...", max_chain_iterations)
        chain_trace_out: dict[str, dict] = {}
        chain_result = chain_local_search(
            best_candidate=current_eval,
            sections_by_id=sections_by_id,
            pattern_catalog=pattern_catalog,
            student_profiles=student_profiles,
            course_rigidity=course_rigidity,
            rooms_by_id=None,
            room_occupancies=None,
            course_room_requirements=None,
            max_iterations=max_chain_iterations,
            decision_trace_out=chain_trace_out,
            stage_telemetry=result["stage_telemetry"],
            locked_section_ids=locked_section_ids,
            section_instructor_ids=section_instructor_ids,
            course_tiers=course_tiers,
            chain_time_limit_seconds=(
                chain_time_limit_seconds
                if chain_time_limit_seconds is not None
                else getattr(settings, "TIMETABLE_CHAIN_TIME_LIMIT_SECONDS", 60.0)
            ),
        )
        if evaluation_is_better(chain_result, current_eval):
            result["chain_search_applied"] = True
            result["final_score"] = list(chain_result.lexicographic_score)
            result["hotspot_courses"] = chain_result.hotspot_courses[:10]
            result["quality_score"] = chain_result.quality_score
            result["unresolved_students"] = len(chain_result.unresolved_student_ids)
            current_eval = chain_result
            # PR5 commit 5: overlay chain trace (last-changer-wins).
            for k, v in chain_trace_out.items():
                result["decision_trace"][k] = v
        else:
            result["chain_search_applied"] = False
        logger.info("Chain search completed in %.1fs", time.time() - t_chain)

    # ── Step 5: CP-SAT polish ──
    if checkpoint:
        checkpoint()
    if run_cpsat_polish:
        from core.services.timetable_cpsat_polisher import polish_scenario_with_cpsat

        t_cpsat = time.time()
        logger.info("Running CP-SAT polisher (%.0fs limit)...", cpsat_time_limit)
        cpsat_result = polish_scenario_with_cpsat(
            scenario_id=scenario_id,
            current_sections=list(sections_by_id.values()),
            student_profiles=student_profiles,
            course_rigidity=course_rigidity,
            current_eval=current_eval,
            time_limit_seconds=cpsat_time_limit,
            hotspot_only=cpsat_hotspot_only,
            stage_telemetry=result["stage_telemetry"],
            locked_section_ids=locked_section_ids,
            section_instructor_ids=section_instructor_ids,
            course_tiers=course_tiers,
        )
        if cpsat_result is not None:
            cpsat_eval = cpsat_result["eval"]
            current_eval = cpsat_eval
            result["cpsat_polish_applied"] = True
            result["final_score"] = list(cpsat_eval.lexicographic_score)
            result["hotspot_courses"] = cpsat_eval.hotspot_courses[:10]
            result["quality_score"] = cpsat_eval.quality_score
            result["unresolved_students"] = len(cpsat_eval.unresolved_student_ids)
            # LEAK FIX: overlay improved sections into sections_by_id
            for improved_sec in cpsat_result["improved_sections"]:
                sections_by_id[improved_sec.section_id] = improved_sec
            # PR5 commit 4: overlay CPSAT decision trace (last-changer-wins).
            for k, v in (cpsat_result.get("decision_trace") or {}).items():
                result["decision_trace"][k] = v
        else:
            result["cpsat_polish_applied"] = False
        logger.info("CP-SAT polisher completed in %.1fs", time.time() - t_cpsat)

    # ── Step 6: Persist improvements ──
    if checkpoint:
        checkpoint()
    # Only update placements that changed — no deletion, no re-generation.
    # Respects is_locked flag on placements.
    if evaluation_is_better(current_eval, baseline):
        persist_result = persist_section_states_to_scenario(scenario_id, sections_by_id)
        result["persist_result"] = {
            "action": "updated_in_place",
            "updated": persist_result.get("updated", 0),
            "skipped": persist_result.get("skipped", 0),
        }
        logger.info(
            "Persisted improvements: %d updated, %d skipped",
            persist_result.get("updated", 0),
            persist_result.get("skipped", 0),
        )
        # Reassign rooms per-meeting after persist
        from core.services.timetable_rooming import assign_rooms_to_board

        boards = DeliveryBoard.objects.filter(scenario_id=scenario_id)
        for board in boards:
            SectionPlacement.objects.filter(
                board=board,
                room="UNASSIGNED",
                is_locked=False,
            ).exclude(term_section_id__in=external_shared_section_ids(scenario_id)).update(room="")
            rooming_result = assign_rooms_to_board(board.id, respect_locked=True)
            # PR5 commit 6: overlay rooming-repair trace (last-changer-wins).
            for k, v in (rooming_result.get("decision_trace") or {}).items():
                result["decision_trace"][k] = v
            # PR6 commit 6: sum board-level rooming_repair telemetry.
            _board_tel = rooming_result.get("stage_telemetry") or {}
            _board_ms = _board_tel.get("stage_ms", {}).get("rooming_repair", 0)
            _board_it = _board_tel.get("stage_iterations", {}).get("rooming_repair", 0)
            result["stage_telemetry"]["stage_ms"]["rooming_repair"] += int(_board_ms)
            result["stage_telemetry"]["stage_iterations"]["rooming_repair"] += int(_board_it)

        # Persist + rooming moved placements only; project them back onto the
        # meeting rows so the two board representations match before the
        # instructor repairs relocate on a (day, start)-matched basis.
        from core.services.timetable_board_persistence import sync_meetings_from_placements

        _sync = sync_meetings_from_placements(scenario_id)
        result["meeting_sync"] = {
            "sections": _sync.sections_synced,
            "written": _sync.meetings_written,
        }

    else:
        result["persist_result"] = {"action": "no_change"}
        logger.info("No improvement found — board unchanged")

        # The improve branch above reconciles meeting rows to placements
        # (sync_meetings_from_placements) as a PRECONDITION for the instructor
        # repairs: _relocate / _unplace key TermSectionMeeting by the placement's
        # current (day, start_time), so on a board with pre-existing placement/TSM
        # drift they would move the placement while the meeting row stays stale.
        # The no-improve branch persisted nothing, so it must reconcile here too
        # before the hoisted repairs run — otherwise the cap fix is invisible to
        # the TSM-derived views (Instructors export, validate_placement). Guarded
        # by the instructor flags so a flag-off no-improve run stays the exact
        # no-op it was before this change (byte-parity).
        if (
            is_instructor_daily_cap_enabled()
            or is_instructor_clash_enabled()
            or is_instructor_compaction_enabled()
        ):
            from core.services.timetable_board_persistence import (
                sync_meetings_from_placements,
            )

            sync_meetings_from_placements(scenario_id)

    # Snapshot the safety state the OPTIMISER produced, BEFORE the mandatory
    # instructor repairs run, so the runner's safety gate judges the optimiser's
    # board and not a repair's accepted least-harm overlap (see
    # timetable_v2_runner: otherwise a hard-cap relocation onto a cross-course
    # student overlap could trigger a rollback that reinstates the violation).
    # Mirrors safety_before_instructor_passes in optimise_scenario_timetable_v2.
    if (
        is_instructor_daily_cap_enabled()
        or is_instructor_clash_enabled()
        or is_instructor_compaction_enabled()
    ):
        from core.services.timetable_workspace import compute_scenario_safety_summary

        result["safety_before_instructor_passes"] = compute_scenario_safety_summary(scenario_id)

    # Instructor cap / clash / compaction repairs run REGARDLESS of whether the
    # optimiser improved the student score. The daily cap is a HARD rule: an
    # already-built board can carry a legacy 4th session the optimiser has no
    # student-score reason to touch, so nesting these under "improved" (as this
    # path used to) let a violation persist whenever the board was already
    # student-optimal — the "Optimise Current does nothing, so the overload
    # stays" hole. optimise_scenario_timetable_v2 already runs them
    # unconditionally; this mirrors it. Flag-gated → no-op (and byte-parity) when
    # off. The repairs read and persist the live DB board directly, so they work
    # on either the just-persisted improvement or the untouched original.
    if is_instructor_daily_cap_enabled():
        from core.services.timetable_instructor_cap_repair import (
            repair_instructor_daily_overloads,
        )

        result["instructor_cap_repair"] = repair_instructor_daily_overloads(scenario_id)

    if is_instructor_clash_enabled():
        from core.services.timetable_instructor_cap_repair import repair_instructor_clashes

        result["instructor_clash_repair"] = repair_instructor_clashes(scenario_id)

    capture_instructor_schedule_after_hard_repairs(result, scenario_id, section_instructor_ids)
    if checkpoint:
        checkpoint()
    if is_instructor_compaction_enabled():
        from core.services.timetable_instructor_compaction import compact_instructor_schedules

        result["instructor_compaction"] = compact_instructor_schedules(scenario_id)

    # The passes above mutate the persisted board after final_score was written,
    # so re-score it. No-op when no pass actually relocated/unplaced anything →
    # byte-parity preserved when the flags are off.
    refresh_final_score_after_instructor_passes(
        result,
        scenario_id,
        student_profiles,
        course_rigidity,
        section_instructor_ids,
        course_tiers,
    )

    if checkpoint:
        checkpoint()
    from core.services.timetable_secondary_recovery_runner import (
        run_secondary_recovery_for_scenario,
    )

    result["secondary_recovery"] = run_secondary_recovery_for_scenario(
        scenario_id, result, student_profiles, course_rigidity, section_instructor_ids, course_tiers
    )

    elapsed = time.time() - t0
    result["elapsed_seconds"] = round(elapsed, 1)

    logger.info(
        "Optimise-current complete in %.1fs: %s -> %s",
        elapsed,
        list(baseline.lexicographic_score),
        result["final_score"],
    )
    result["instructor_gap_metric"] = instructor_gap_metric(
        _gap_idle_before,
        result.get("final_score"),
        # The map is now also built for the cap/clash gates; the gap metric must
        # stay zeroed unless the gap penalty itself is on (byte-parity contract).
        section_instructor_ids if is_instructor_gap_penalty_enabled() else None,
    )
    return result

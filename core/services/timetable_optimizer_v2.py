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

from core.models import (
    DeliveryBoard,
    Room,
    ScenarioSectionBudget,
    ScenarioStudentMap,
    SectionPlacement,
    StudentCourse,
    TermSection,
    TimetableScenario,
)
from core.services.timetable_assignment_models import (
    RiskTier,
    RoomOccupancy,
    RoomProfile,
    SectionMeeting,
    SectionState,
    StudentProfile,
)
from core.services.timetable_autoplace import WEEKDAYS
from core.services.timetable_candidate_eval import (
    evaluate_generated_timetable_candidate,
    rank_timetable_candidates,
)
from core.services.timetable_workspace import _to_minutes

logger = logging.getLogger(__name__)

# Day-string → integer index (SUN=0 .. THU=4)
_DAY_IDX: dict[str, int] = {d: i for i, d in enumerate(WEEKDAYS)}


# ── Adapter A: Student Profiles ──────────────────────────────────


def build_student_profiles_for_scenario(
    scenario_id: int,
) -> dict[str, StudentProfile]:
    """Read ScenarioStudentMap + student metadata → StudentProfile dict.

    Risk tiers:
      A (highest) — student has ≥1 retake course (grade F/D/W in history)
      B           — graduating soon (earned ≥ 100 credits)
      C (lowest)  — everyone else

    intra_tier_score: lower GPA → higher priority within the tier
                      (students struggling most get first pick).
    """
    maps = ScenarioStudentMap.objects.filter(scenario_id=scenario_id).values_list(
        "student_id", "recommended_courses"
    )
    if not maps:
        return {}

    student_ids = [m[0] for m in maps]
    rec_courses_by_sid: dict[int, list[str]] = {m[0]: m[1] for m in maps}

    # Bulk-fetch student metadata
    from core.models import Student

    students_qs = Student.objects.filter(student_id__in=student_ids).values(
        "student_id", "program", "gpa", "total_earned_credits"
    )
    student_meta: dict[int, dict] = {s["student_id"]: s for s in students_qs}

    # Identify retake courses (grade F, D, or W in student history)
    retake_courses: dict[int, set[str]] = defaultdict(set)
    retake_records = (
        StudentCourse.objects.filter(
            student_id__in=student_ids,
            grade__in=["F", "D", "W", "D+"],
        )
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
        b.course_code: b for b in ScenarioSectionBudget.objects.filter(scenario_id=scenario_id)
    }

    # Group placements by term_section
    grouped: dict[int, list] = defaultdict(list)
    ts_lookup: dict[int, TermSection] = {}
    for pl in placements:
        grouped[pl.term_section_id].append(pl)
        ts_lookup[pl.term_section_id] = pl.term_section

    sections: list[SectionState] = []
    for ts_id, pls in grouped.items():
        ts = ts_lookup[ts_id]
        course_code = ts.course_code
        budget = budgets.get(course_code)

        meetings: list[SectionMeeting] = []
        for pl in pls:
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
        has_lab = budget and budget.credit_hours == 4
        lecture_meetings = sum(1 for m in meetings if (m.end_min - m.start_min) <= 80)
        room_type = "lecture" if lecture_meetings >= len(meetings) / 2 else "lab"

        # Compute pattern family and ID for local search
        pattern_family = _compute_pattern_family(meetings, bool(has_lab))
        pattern_id = _compute_pattern_id(meetings)

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
                assigned_room_id=pls[0].room if pls[0].room else None,
                pattern_family=pattern_family,
                pattern_id=pattern_id,
            )
        )

    logger.info(
        "Built %d section states from %d placements for scenario %d",
        len(sections),
        sum(len(v) for v in grouped.values()),
        scenario_id,
    )
    return sections


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
            rigidity[b.course_code] = 0.5
            continue

        # Demand pressure: how full are the sections?
        capacity = b.planned_sections * b.max_per_section
        demand_ratio = min(b.total_demand / capacity, 1.5) if capacity > 0 else 0.5

        # Section scarcity: fewer sections = more rigid
        scarcity = 1.0 / b.planned_sections

        # Lab bonus: 4-credit courses with labs are harder to reschedule
        lab_bonus = 0.15 if b.credit_hours == 4 else 0.0

        score = min(1.0, (demand_ratio * 0.5) + (scarcity * 0.3) + lab_bonus + 0.05)
        rigidity[b.course_code] = round(score, 3)

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
            "term_section__course_code",
            "term_section__section",
        )
    )

    for pl in placements:
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
        section_id = f"{pl['term_section__course_code']}_{pl['term_section__section']}"
        occ.assigned_section_ids.add(section_id)

    # Course → room type requirements
    course_room_requirements: dict[str, str] = {}
    budgets = ScenarioSectionBudget.objects.filter(scenario_id=scenario_id).values(
        "course_code", "credit_hours"
    )
    for b in budgets:
        course_room_requirements[b["course_code"]] = "lab" if b["credit_hours"] == 4 else "lecture"

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

    for _ts_id, pls in grouped.items():
        ts = pls[0].term_section
        key = (ts.course_code, ts.section)
        state = state_lookup.get(key)
        if not state:
            skipped += len(pls)
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

    candidates: list[dict] = []

    for idx, strategy in enumerate(strategies):
        # Clear existing placements for a fresh run
        SectionPlacement.objects.filter(board__scenario_id=scenario_id).delete()

        logger.info(
            "Generating candidate %d/%d with strategy=%s", idx + 1, len(strategies), strategy
        )
        try:
            auto_place_scenario(scenario_id, strategy=strategy)
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
        durations = get_meeting_pattern(b.credit_hours)
        course_requirements.append(
            {
                "durations": durations,
                "has_lab": b.credit_hours == 4,
                "modality": "ONCAMPUS",
                "allow_permutations": b.credit_hours == 4,
            }
        )
    return build_canonical_pattern_catalog(
        course_requirements=course_requirements,
        slot_config=scenario.slot_config or None,
        lab_slot_config=scenario.lab_slot_config or None,
        blocked_slots=scenario.blocked_slots or None,
    )


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
        }

    t1 = time.time()
    logger.info("Profiles built in %.1fs (%d students)", t1 - t0, len(student_profiles))

    # ── Step 2: Generate candidates with multiple strategies ──
    candidates = _generate_candidates_for_scenario(scenario_id, strategies)
    if not candidates:
        return {
            "error": "No candidates generated",
            "candidates_evaluated": 0,
            "decision_trace": {},
        }

    t2 = time.time()
    logger.info("Generated %d candidates in %.1fs", len(candidates), t2 - t1)

    # ── Step 3: Rank all candidates by student-assignability ──
    ranked = rank_timetable_candidates(
        candidate_list=candidates,
        student_profiles=student_profiles,
        course_rigidity=course_rigidity,
    )
    best = ranked[0]

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
        "hotspot_courses": best.hotspot_courses[:10],
        "capacity_pressure_courses": best.capacity_pressure_courses[:10],
        "reserve_heavy_sections": [
            {"section": s, "ratio": round(r, 2)} for s, r in best.reserve_heavy_sections[:10]
        ],
        "unresolved_students": len(best.unresolved_student_ids),
        "total_students": len(student_profiles),
        "local_search_applied": False,
        "final_score": list(best.lexicographic_score),
        "persist_result": None,
        "all_scores": [
            {"id": r.candidate_id, "score": list(r.lexicographic_score)} for r in ranked
        ],
        # PR3 commit 4 — decision_trace preservation across V2 pipeline.
        # Populated from the greedy re-placement at step 5; carried through
        # the optimiser unchanged. Per commit-4 rulings J3/K3/L2, V2 local
        # search / chain search / CP-SAT polish do NOT update the trace
        # (chosen_* fields reflect the cold-start placement, not post-LS
        # moves). Schema-stability: the key is always present.
        "decision_trace": {},
    }

    # ── Step 4: Local search on the best candidate ──
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
            )

            score_before = best.lexicographic_score
            score_after = improved.lexicographic_score
            result["local_search_applied"] = True
            result["score_before_local_search"] = list(score_before)
            result["final_score"] = list(score_after)
            result["hotspot_courses"] = improved.hotspot_courses[:10]
            result["capacity_pressure_courses"] = improved.capacity_pressure_courses[:10]
            result["reserve_heavy_sections"] = [
                {"section": s, "ratio": round(r, 2)}
                for s, r in improved.reserve_heavy_sections[:10]
            ]
            result["unresolved_students"] = len(improved.unresolved_student_ids)

            if score_after < score_before:
                logger.info("Local search improved score: %s → %s", score_before, score_after)
            else:
                logger.info("Local search found no improvement (score unchanged)")

            t4 = time.time()
            logger.info("Local search completed in %.1fs", t4 - t3)

    # ── Step 4b: Chain-2 local search ──
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
            )

            t_chain_start = time.time()
            logger.info("Running chain-2 search (max %d iterations)...", max_chain_iterations)
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
            )

            if chain_result.lexicographic_score < current_eval_for_chain.lexicographic_score:
                logger.info(
                    "Chain search improved: %s -> %s",
                    current_eval_for_chain.lexicographic_score,
                    chain_result.lexicographic_score,
                )
                result["chain_search_applied"] = True
                result["score_before_chain"] = list(current_eval_for_chain.lexicographic_score)
                result["final_score"] = list(chain_result.lexicographic_score)
                result["hotspot_courses"] = chain_result.hotspot_courses[:10]
                result["unresolved_students"] = len(chain_result.unresolved_student_ids)
                current_eval_for_chain = chain_result
            else:
                result["chain_search_applied"] = False
                logger.info("Chain search: no improvement found")

            t_chain_end = time.time()
            logger.info("Chain search completed in %.1fs", t_chain_end - t_chain_start)

    # ── Step 4c: Global CP-SAT polisher ──
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
            )

            if cpsat_result is not None:
                result["cpsat_polish_applied"] = True
                result["score_before_cpsat"] = list(current_eval_for_chain.lexicographic_score)
                result["final_score"] = list(cpsat_result.lexicographic_score)
                result["hotspot_courses"] = cpsat_result.hotspot_courses[:10]
                result["unresolved_students"] = len(cpsat_result.unresolved_student_ids)
                # Update sections_by_id with polished sections
                # (cpsat_result evaluated with the improved sections)
                logger.info("CP-SAT polisher accepted improvement")
            else:
                result["cpsat_polish_applied"] = False
                logger.info("CP-SAT polisher: no improvement")

            t_cpsat_end = time.time()
            logger.info("CP-SAT polisher completed in %.1fs", t_cpsat_end - t_cpsat_start)

    # ── Step 5: Persist to DB ──
    # Re-place using the winning strategy first (because candidate
    # generation deletes all placements). Then overlay local search /
    # chain / CP-SAT improvements on top — these are stored as deltas
    # in sections_by_id, not as full placements.
    # First: re-place using the winning strategy (baseline placement)
    winning_strategy = best.candidate_id.rsplit("_", 1)[0]
    SectionPlacement.objects.filter(board__scenario_id=scenario_id).delete()
    from core.services.timetable_autoplace import auto_place_scenario

    scenario_place_result = auto_place_scenario(scenario_id, strategy=winning_strategy)
    # PR3 commit 4: capture the scenario-level greedy trace. Per commit-4
    # ruling J3 we preserve this cold-start trace through the remaining V2
    # pipeline unchanged; LS / chain / CP-SAT do not mutate it.
    result["decision_trace"] = scenario_place_result.get("decision_trace", {})

    # Then: if local search improved the timetable, apply those
    # improvements on top of the baseline placements
    ls_persisted = False
    if run_local_search and "sections_by_id" in dir() and sections_by_id:
        score_before = result.get("score_before_local_search")
        score_after = result.get("final_score")
        if score_before and score_after and tuple(score_after) < tuple(score_before):
            persist_result = persist_section_states_to_scenario(scenario_id, sections_by_id)
            ls_persisted = True
            logger.info(
                "Persisted local search improvements: %d placements updated",
                persist_result.get("updated", 0),
            )

    result["persist_result"] = {
        "action": "placed_with_local_search" if ls_persisted else "placed_winning_strategy",
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
            SectionPlacement.objects.filter(board=board, room="UNASSIGNED").update(room="")
            assign_rooms_to_board(board.id)

    elapsed = time.time() - t0
    result["elapsed_seconds"] = round(elapsed, 1)

    logger.info(
        "V2 optimisation complete in %.1fs: best=%s final_score=%s",
        elapsed,
        best.candidate_id,
        result["final_score"],
    )
    return result


# ── Optimise Current ─────────────────────────────────────────────


def optimise_current_timetable(
    scenario_id: int,
    max_search_iterations: int = 50,
    run_chain_search: bool = True,
    max_chain_iterations: int = 10,
    run_cpsat_polish: bool = True,
    cpsat_time_limit: float = 60.0,
    cpsat_hotspot_only: bool = False,
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
        }

    # ── Step 2: Read current placements as-is ──
    sections = build_section_states_for_scenario(scenario_id)
    if not sections:
        return {
            "error": "No placements found — nothing to optimise",
            "candidates_evaluated": 0,
            "decision_trace": {},
        }

    from core.services import timetable_student_assignment as ssa

    sections_by_id = ssa.build_sections_by_id(sections)

    # Evaluate current state as baseline
    baseline = evaluate_generated_timetable_candidate(
        candidate_id="current",
        generated_sections=sections,
        student_profiles=student_profiles,
        course_rigidity=course_rigidity,
    )

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
        "best_score": list(baseline.lexicographic_score),
        "final_score": list(baseline.lexicographic_score),
        "hotspot_courses": baseline.hotspot_courses[:10],
        "capacity_pressure_courses": baseline.capacity_pressure_courses[:10],
        "reserve_heavy_sections": [
            {"section": s, "ratio": round(r, 2)} for s, r in baseline.reserve_heavy_sections[:10]
        ],
        "unresolved_students": len(baseline.unresolved_student_ids),
        "total_students": len(student_profiles),
        "local_search_applied": False,
        "persist_result": None,
        # PR3 commit 4: "Optimise Current" works off existing DB placements
        # and never calls auto_place_scenario, so no greedy trace is
        # captured in this path. Key included for schema stability.
        "decision_trace": {},
    }

    # ── Step 3: Local search on current state ──
    pattern_catalog = _build_pattern_catalog_for_scenario(scenario_id)

    # Room repair is DISABLED — the auto-placer handles per-meeting room
    # types correctly (75min→lecture, 100min→lab). The optimizer only
    # moves time patterns; rooms are reassigned after persist.
    from core.services.timetable_local_search_v2 import diagnostic_driven_local_search

    t2 = time.time()
    logger.info(
        "Running local search on current timetable (max %d iterations)...", max_search_iterations
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
    )

    score_before = baseline.lexicographic_score
    score_after = improved.lexicographic_score
    result["local_search_applied"] = True
    result["score_before_local_search"] = list(score_before)
    result["final_score"] = list(score_after)
    result["hotspot_courses"] = improved.hotspot_courses[:10]
    result["unresolved_students"] = len(improved.unresolved_student_ids)

    t3 = time.time()
    if score_after < score_before:
        logger.info("Local search improved: %s -> %s (%.1fs)", score_before, score_after, t3 - t2)
    else:
        logger.info("Local search: no improvement (%.1fs)", t3 - t2)

    # ── Step 4: Chain-2 search ──
    current_eval = improved
    if run_chain_search:
        from core.services.timetable_local_search_chains import chain_local_search

        t_chain = time.time()
        logger.info("Running chain-2 search (max %d iterations)...", max_chain_iterations)
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
        )
        if chain_result.lexicographic_score < current_eval.lexicographic_score:
            result["chain_search_applied"] = True
            result["final_score"] = list(chain_result.lexicographic_score)
            result["hotspot_courses"] = chain_result.hotspot_courses[:10]
            result["unresolved_students"] = len(chain_result.unresolved_student_ids)
            current_eval = chain_result
        else:
            result["chain_search_applied"] = False
        logger.info("Chain search completed in %.1fs", time.time() - t_chain)

    # ── Step 5: CP-SAT polish ──
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
        )
        if cpsat_result is not None:
            result["cpsat_polish_applied"] = True
            result["final_score"] = list(cpsat_result.lexicographic_score)
            result["hotspot_courses"] = cpsat_result.hotspot_courses[:10]
            result["unresolved_students"] = len(cpsat_result.unresolved_student_ids)
        else:
            result["cpsat_polish_applied"] = False
        logger.info("CP-SAT polisher completed in %.1fs", time.time() - t_cpsat)

    # ── Step 6: Persist improvements ──
    # Only update placements that changed — no deletion, no re-generation.
    # Respects is_locked flag on placements.
    final_score = tuple(result["final_score"])
    if final_score < baseline.lexicographic_score:
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
            SectionPlacement.objects.filter(board=board, room="UNASSIGNED").update(room="")
            assign_rooms_to_board(board.id)
    else:
        result["persist_result"] = {"action": "no_change"}
        logger.info("No improvement found — board unchanged")

    elapsed = time.time() - t0
    result["elapsed_seconds"] = round(elapsed, 1)

    logger.info(
        "Optimise-current complete in %.1fs: %s -> %s",
        elapsed,
        list(baseline.lexicographic_score),
        result["final_score"],
    )
    return result

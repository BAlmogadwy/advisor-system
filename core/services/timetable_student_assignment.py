"""Student→section assignment and the timetable objective.

This module owns two things that must agree with each other:

1. **The seating** — ``assign_students_to_sections`` walks students in
   risk-tier order and, per student, seats each recommended course via
   ``assign_courses_for_student``; ``repair_unresolved_assignments_shallow``
   then makes a second pass that may swap a blocking course to a different
   section to rescue an unresolved one.
2. **The objective** — ``evaluate_assignability_lexicographic`` scores the
   resulting board as a lexicographic tuple (smaller = better, most-significant
   position first). It is produced here in ONE place and consumed positionally
   in several others, so decode it with :func:`decode_score` / the accessors
   rather than fixed indices.

Objective layouts
-----------------
*Legacy* (``TIMETABLE_TIERED_OBJECTIVE_ENABLED`` off) — the canonical 6-tuple,
or 7 when the instructor-gap flag appends an idle term::

    (tier_a, unresolved_students, unassigned_courses, clashes,
     gap_minutes[+spread folded in], reserve) [, instructor_idle]

*Tiered* (flag on **and** a ``course_tiers`` map threaded in) — a fixed 9-tuple
that ranks resolution by course tier (T1 specialised major / T2 shared
foundation / T3 gen-ed, see ``timetable_course_tier``)::

    0 high-risk unresolved (RiskTier.A student with an unresolved T1|T2 course)
    1 student clashes
    2 Tier-1 unresolved (student, course) pairs        -> hard, drive to 0
    3 Tier-2 unresolved beyond the per-course tolerance
    4 student cost = real_gap_minutes + budget * soft_unresolved
    5 soft count (Tier-3 + Tier-2 within tolerance)
    6 reserve used
    7 same-course section spread (its own quality term)
    8 instructor idle minutes

Position 4 is a **bounded trade**, not strict priority: gaps and soft-tier
enrolment share one axis, so a soft course is seated exactly when doing so adds
fewer than ``TIMETABLE_TIERED_SOFT_GAP_BUDGET`` gap-minutes. Real gap is
recoverable as ``score[4] - budget * score[5]``. Setting the budget to 0
reproduces strict quality-first. Note real gap is **unbundled** from the
same-course spread pseudo-penalty (position 7); the legacy tuple sums them.

Tier-awareness in the seating
-----------------------------
``course_tiers`` is load-bearing on the *assignment*, not only the score: the
per-student course order leads with the tier rank so core courses are seated
before foundation/gen-ed (scarcity remains the within-tier tiebreak), and the
repair pass rescues core first. **Any caller that reconstructs seating must
thread the same tier map**, otherwise it models a board that was never built.

Byte-parity: with ``course_tiers`` absent every course ranks 0, so the sort keys
collapse to the original ordering and the legacy tuple is returned verbatim —
flag-off output is identical to pre-feature.
"""

from __future__ import annotations

from collections import defaultdict

from core.services.timetable_assignment_models import (
    RiskTier,
    SectionMeeting,
    SectionState,
    StudentAssignmentState,
    StudentProfile,
    UnresolvedReason,
)
from core.services.timetable_flags import (
    get_tiered_soft_gap_budget,
    get_tiered_t2_tolerance,
    is_tiered_objective_enabled,
)
from core.services.timetable_pr4_instructor import is_instructor_gap_penalty_enabled
from core.services.timetable_same_course import (
    make_meeting_window,
    same_course_section_spread_penalty,
)


def build_sections_by_course(
    sections_by_id: dict[str, SectionState],
) -> dict[str, list[SectionState]]:
    out: dict[str, list[SectionState]] = defaultdict(list)
    for sec in sections_by_id.values():
        out[sec.course_code].append(sec)
    for course_code in out:
        out[course_code].sort(key=lambda s: s.section_id)
    return dict(out)


def build_sections_by_id(sections: list[SectionState]) -> dict[str, SectionState]:
    out: dict[str, SectionState] = {}
    for sec in sections:
        if sec.section_id in out:
            raise ValueError(f"Duplicate section_id detected: {sec.section_id}")
        out[sec.section_id] = sec
    return out


def reserve_allowed_for_tier(
    tier: RiskTier,
    reserves_released: bool,
    allow_tier_b: bool = True,
) -> bool:
    if reserves_released:
        return True
    if tier == RiskTier.A:
        return True
    if tier == RiskTier.B and allow_tier_b:
        return True
    return False


# Order a student's courses by tier when a tier map is supplied: core first.
# Lower rank = higher priority, so a rank comparison reads as "more important".
COURSE_TIER_RANK = {"T1": 0, "T2": 1, "T3": 2}


def _tier_rank(course_tiers: dict[str, str] | None, course_code: str) -> int:
    """Priority rank of a course (0=T1 core .. 2=T3 gen-ed); 0 with no map."""
    if not course_tiers:
        return 0
    return COURSE_TIER_RANK.get(course_tiers.get(course_code, "T1"), 0)


def assign_students_to_sections(
    profiles: dict[str, StudentProfile],
    sections_by_id: dict[str, SectionState],
    sections_by_course: dict[str, list[SectionState]],
    course_rigidity: dict[str, float],
    course_tiers: dict[str, str] | None = None,
) -> tuple[dict[str, StudentAssignmentState], list[str]]:
    states = {sid: StudentAssignmentState(student_id=sid) for sid in profiles}

    sorted_students = sorted(
        profiles.values(),
        key=lambda s: (s.risk_tier, s.intra_tier_score, s.student_id),
        reverse=True,
    )

    tier_ab = [p for p in sorted_students if p.risk_tier >= RiskTier.B]
    tier_c = [p for p in sorted_students if p.risk_tier == RiskTier.C]

    for student in tier_ab:
        assign_courses_for_student(
            student,
            states[student.student_id],
            sections_by_course,
            sections_by_id,
            course_rigidity,
            reserves_released=False,
            course_tiers=course_tiers,
        )

    for student in tier_c:
        assign_courses_for_student(
            student,
            states[student.student_id],
            sections_by_course,
            sections_by_id,
            course_rigidity,
            reserves_released=True,
            course_tiers=course_tiers,
        )

    repair_unresolved_assignments_shallow(
        states, profiles, sections_by_course, sections_by_id, course_tiers=course_tiers
    )

    unresolved_ids = [sid for sid, st in states.items() if st.unresolved_courses]
    return states, unresolved_ids


def assign_courses_for_student(
    student: StudentProfile,
    state: StudentAssignmentState,
    sections_by_course: dict[str, list[SectionState]],
    sections_by_id: dict[str, SectionState],
    course_rigidity: dict[str, float],
    reserves_released: bool,
    course_tiers: dict[str, str] | None = None,
) -> None:
    allow_reserve = reserve_allowed_for_tier(student.risk_tier, reserves_released)

    for course_code in student.recommended_courses:
        if course_code not in state.assigned_sections:
            state.unresolved_courses.pop(course_code, None)

    unassigned = set(student.recommended_courses) - set(state.assigned_sections.keys())

    def scarcity_key(course_code: str) -> tuple[int, int, float, str]:
        candidates = sections_by_course.get(course_code, [])
        feasible_count = sum(
            1 for s in candidates if s.can_enroll(allow_reserve) and not state.has_clash(s.meetings)
        )
        # Tier rank leads the key so a student's Tier-1 (core) courses are seated
        # BEFORE Tier-2/Tier-3 — otherwise the scarcity heuristic can hand a
        # scarce foundation/gen-ed course a slot that a core course needed, and
        # the core seat is lost. Without a tier map every course ranks 0, so the
        # ordering collapses to the original (feasible_count, -rigidity, code).
        rank = _tier_rank(course_tiers, course_code)
        return (rank, feasible_count, -course_rigidity.get(course_code, 0.0), course_code)

    sorted_courses = sorted(list(unassigned), key=scarcity_key)

    for course_code in sorted_courses:
        candidate_sections = sections_by_course.get(course_code, [])
        best_section_tuple = rank_and_select_best_section(
            candidate_sections, state, allow_reserve, sections_by_id
        )
        if best_section_tuple:
            best_section, _ = best_section_tuple
            apply_assignment(state, best_section, sections_by_id)
        else:
            reason_str = diagnose_unresolved(candidate_sections, state, allow_reserve)
            state.unresolved_courses[course_code] = UnresolvedReason(course_code, reason_str)


def rank_and_select_best_section(
    candidates: list[SectionState],
    state: StudentAssignmentState,
    allow_reserve: bool,
    sections_by_id: dict[str, SectionState],
) -> tuple[SectionState, tuple] | None:
    valid_options: list[tuple[tuple, SectionState]] = []
    for section in candidates:
        if not section.can_enroll(allow_reserve):
            continue
        if state.has_clash(section.meetings):
            continue
        uses_reserve = 1 if section.current_enrollment >= section.regular_limit() else 0
        added_gap = calculate_added_gap(state, section, sections_by_id)
        cap = max(1, section.max_capacity)
        fill_ratio_bp = int(((section.current_enrollment + 1) / cap) * 10000)
        score_tuple = (0, uses_reserve, added_gap, fill_ratio_bp, section.section_id)
        valid_options.append((score_tuple, section))
    if not valid_options:
        return None
    valid_options.sort(key=lambda x: x[0])
    best_score, best_section = valid_options[0]
    return best_section, best_score


def apply_assignment(
    state: StudentAssignmentState,
    section: SectionState,
    sections_by_id: dict[str, SectionState],
) -> None:
    state.assigned_sections[section.course_code] = section.section_id
    state.section_ids.add(section.section_id)
    state.unresolved_courses.pop(section.course_code, None)
    for meeting in section.meetings:
        state.occupied_mask_by_day[meeting.day] |= meeting.mask
    section.current_enrollment += 1
    section.enrolled_student_ids.add(state.student_id)
    state.total_gap_minutes = _compute_total_state_gap(state, sections_by_id)


def remove_assignment(
    state: StudentAssignmentState,
    section: SectionState,
    sections_by_id: dict[str, SectionState],
) -> None:
    if state.assigned_sections.get(section.course_code) != section.section_id:
        return
    del state.assigned_sections[section.course_code]
    state.section_ids.discard(section.section_id)
    state.occupied_mask_by_day = {i: 0 for i in range(7)}
    for rem_sec_id in state.section_ids:
        rem_sec = sections_by_id[rem_sec_id]
        for meeting in rem_sec.meetings:
            state.occupied_mask_by_day[meeting.day] |= meeting.mask
    section.current_enrollment -= 1
    section.enrolled_student_ids.discard(state.student_id)
    state.total_gap_minutes = _compute_total_state_gap(state, sections_by_id)


def meetings_clash(meetings_a: list[SectionMeeting], meetings_b: list[SectionMeeting]) -> bool:
    for a in meetings_a:
        for b in meetings_b:
            if a.day == b.day and (a.mask & b.mask):
                return True
    return False


def find_blocking_assigned_courses(
    state: StudentAssignmentState,
    candidate: SectionState,
    sections_by_id: dict[str, SectionState],
) -> list[str]:
    blockers: list[str] = []
    for assigned_sec_id in state.section_ids:
        assigned_sec = sections_by_id[assigned_sec_id]
        if meetings_clash(candidate.meetings, assigned_sec.meetings):
            blockers.append(assigned_sec.course_code)
    return blockers


def repair_unresolved_assignments_shallow(
    states: dict[str, StudentAssignmentState],
    profiles: dict[str, StudentProfile],
    sections_by_course: dict[str, list[SectionState]],
    sections_by_id: dict[str, SectionState],
    course_tiers: dict[str, str] | None = None,
) -> None:
    ordered_students = sorted(
        states.keys(),
        key=lambda sid: (profiles[sid].risk_tier, profiles[sid].intra_tier_score, sid),
        reverse=True,
    )

    for student_id in ordered_students:
        state = states[student_id]
        if not state.unresolved_courses:
            continue
        student = profiles[student_id]
        allow_reserve = reserve_allowed_for_tier(student.risk_tier, reserves_released=True)
        unresolved_list = list(state.unresolved_courses.keys())
        if course_tiers:
            # Repair core first: a Tier-1 course gets first claim on the limited
            # swap budget, so it is not spent rescuing a gen-ed course instead.
            # Key on the tier ALONE — Python's sort is stable, so the existing
            # order (unresolved_courses is insertion-ordered by the scarcity key
            # from assign_courses_for_student) is preserved *within* each tier.
            # Adding a course-code tiebreak here would silently replace that
            # deliberate hardest-first ordering with alphabetical.
            unresolved_list.sort(key=lambda c: _tier_rank(course_tiers, c))

        for unres_course in unresolved_list:
            if unres_course in state.assigned_sections:
                state.unresolved_courses.pop(unres_course, None)
                continue
            direct_tuple = rank_and_select_best_section(
                sections_by_course.get(unres_course, []),
                state,
                allow_reserve,
                sections_by_id,
            )
            if direct_tuple:
                apply_assignment(state, direct_tuple[0], sections_by_id)
                continue

            candidate_sections = sections_by_course.get(unres_course, [])
            repaired = False
            for candidate in candidate_sections:
                if not candidate.can_enroll(allow_reserve):
                    continue
                blocking_courses = sorted(
                    set(find_blocking_assigned_courses(state, candidate, sections_by_id))
                )
                if len(blocking_courses) != 1:
                    continue
                blocking_course = blocking_courses[0]
                # NOTE: deliberately NO tier guard on eviction. The swap below
                # only commits if the displaced course finds an alternative
                # section (else it rolls back), so a higher-tier course never
                # loses its seat — it merely moves. Blocking the swap would
                # forfeit an unresolved seat to protect a section choice.
                #
                # That trade is clearly right when the course being repaired is
                # T1 (objective position 2) or T2-over-tolerance (position 3),
                # since both outrank the gap-minutes (position 4) the relocation
                # costs. For a SOFT repair (T3 / T2-within-tolerance) the seat
                # lives at position 5, *below* gap — so it is only worth it while
                # the added gap stays under the soft-gap budget. Measured on scn
                # 642 the swaps came in at ~116 gap-min per soft seat against a
                # 120 budget, i.e. roughly break-even, so it is left ungated;
                # a budget-aware gate here is a possible future refinement.
                # Guarding it outright pushed Tier-2 over-tolerance 53 -> 73 and
                # soft 59 -> 93 for zero core gain.
                blocking_section_id = state.assigned_sections.get(blocking_course)
                if not blocking_section_id:
                    continue
                blocking_section = sections_by_id[blocking_section_id]
                remove_assignment(state, blocking_section, sections_by_id)
                if state.has_clash(candidate.meetings) or not candidate.can_enroll(allow_reserve):
                    apply_assignment(state, blocking_section, sections_by_id)
                    continue
                apply_assignment(state, candidate, sections_by_id)
                alt_candidates = [
                    s
                    for s in sections_by_course.get(blocking_course, [])
                    if s.section_id != blocking_section.section_id
                ]
                alt_tuple = rank_and_select_best_section(
                    alt_candidates, state, allow_reserve, sections_by_id
                )
                if alt_tuple:
                    apply_assignment(state, alt_tuple[0], sections_by_id)
                    state.unresolved_courses.pop(unres_course, None)
                    repaired = True
                    break
                remove_assignment(state, candidate, sections_by_id)
                apply_assignment(state, blocking_section, sections_by_id)
            if not repaired and unres_course not in state.assigned_sections:
                reason = diagnose_unresolved(
                    sections_by_course.get(unres_course, []), state, allow_reserve
                )
                state.unresolved_courses[unres_course] = UnresolvedReason(unres_course, reason)


def calculate_added_gap(
    state: StudentAssignmentState,
    candidate: SectionState,
    sections_by_id: dict[str, SectionState],
) -> int:
    added_gap = 0
    candidate_days = {m.day for m in candidate.meetings}
    for day in candidate_days:
        existing_meetings: list[SectionMeeting] = []
        for sec_id in state.section_ids:
            existing_meetings.extend([m for m in sections_by_id[sec_id].meetings if m.day == day])
        old_day_gap = _compute_day_gap(existing_meetings)
        new_day_gap = _compute_day_gap(
            existing_meetings + [m for m in candidate.meetings if m.day == day]
        )
        added_gap += new_day_gap - old_day_gap
    return added_gap


def _compute_day_gap(meetings: list[SectionMeeting]) -> int:
    if len(meetings) <= 1:
        return 0
    sorted_m = sorted(meetings, key=lambda x: x.start_min)
    gap = 0
    for i in range(len(sorted_m) - 1):
        gap += max(0, sorted_m[i + 1].start_min - sorted_m[i].end_min)
    return gap


def _compute_total_state_gap(
    state: StudentAssignmentState,
    sections_by_id: dict[str, SectionState],
) -> int:
    total = 0
    meetings_by_day: dict[int, list[SectionMeeting]] = defaultdict(list)
    for sec_id in state.section_ids:
        for m in sections_by_id[sec_id].meetings:
            meetings_by_day[m.day].append(m)
    for day_meetings in meetings_by_day.values():
        total += _compute_day_gap(day_meetings)
    return total


def _compute_instructor_idle_minutes(
    sections_by_id: dict[str, SectionState],
    section_instructor_ids: dict[str, frozenset[int]],
) -> int:
    """Total idle minutes between consecutive on-campus meetings, summed across
    every instructor and day.

    Mirrors the student day-gap metric (``_compute_total_state_gap``) but keyed
    by instructor: each instructor's meetings — gathered across all the sections
    they teach — are grouped by day and scored with ``_compute_day_gap``. Only
    sections present in ``section_instructor_ids`` contribute, so courses with no
    assigned instructor are naturally invisible. A section taught by N
    instructors counts its meetings toward each of those N instructors.
    """
    if not section_instructor_ids:
        return 0
    meetings_by_instructor_day: dict[tuple[int, int], list[SectionMeeting]] = defaultdict(list)
    for section_id, instructor_ids in section_instructor_ids.items():
        section = sections_by_id.get(section_id)
        if section is None:
            continue
        for instr_id in instructor_ids:
            for m in section.meetings:
                meetings_by_instructor_day[(instr_id, m.day)].append(m)
    total = 0
    for day_meetings in meetings_by_instructor_day.values():
        total += _compute_day_gap(day_meetings)
    return total


def diagnose_unresolved(
    candidates: list[SectionState],
    state: StudentAssignmentState,
    allow_reserve: bool,
) -> str:
    if not candidates:
        return "no_sections"
    clash_only = 0
    full_only = 0
    reserve_only = 0
    feasible = 0
    for c in candidates:
        clash = state.has_clash(c.meetings)
        can_regular = c.can_enroll(allow_reserve=False)
        can_with_policy = c.can_enroll(allow_reserve=allow_reserve)
        if (not clash) and can_with_policy:
            feasible += 1
            continue
        if clash and (not can_with_policy):
            continue
        if clash:
            clash_only += 1
        elif not can_with_policy:
            if can_regular:
                full_only += 1
            else:
                if not allow_reserve and c.can_enroll(allow_reserve=True):
                    reserve_only += 1
                else:
                    full_only += 1
    if feasible > 0:
        return "unknown"
    n = len(candidates)
    if clash_only == n:
        return "all_clash"
    if full_only == n:
        return "full"
    if reserve_only == n:
        return "reserve_only"
    return "mixed_blockers"


def _compute_same_course_section_spread(
    sections_by_id: dict[str, SectionState],
) -> int:
    """Penalty for scattering same-course sections across the week.

    Registrar rule: the same instructor typically teaches every section
    of a course, so (a) two sections at the same (day, slot) are
    instructor-clash and already hard-rejected upstream, and (b)
    sections *should* be consecutive on the same day so the instructor
    doesn't get a scattered schedule.

    The adjacency requirement is intentionally stronger than a normal
    student day-gap penalty:

    - two sections of a course should form one back-to-back pair
    - three or more sections should have at least one back-to-back pair

    Penalty per pair of sections of the same course, summed over every
    pair of sections of every multi-section course:

    - same day, back-to-back/consecutive slot    → 0
    - same day, gap 1-30 min                      → 30
    - same day, gap 31-120 min                    → 120
    - same day, gap > 120 min                     → gap_minutes
    - different days                              → 1000
    - same-day overlap                            → 10000

    A course that fails the required adjacent-pair rule receives an
    additional 5000-minute-equivalent penalty. A small passing-time gap
    up to 15 minutes counts as consecutive, matching the split-screen
    bundle recommender.

    Expressed in "minutes-equivalent" so the result can be folded into
    ``total_gap_minutes`` without changing the tuple shape.
    """
    by_course = defaultdict(list)
    for sec in sections_by_id.values():
        by_course[sec.course_code].append(
            [
                make_meeting_window(sec.course_code, m.day, m.start_min, m.end_min, sec.section_id)
                for m in sec.meetings
            ]
        )
    return same_course_section_spread_penalty(by_course)


# ── Objective tuple layouts ──────────────────────────────────────────────
# Legacy (tiered flag OFF): the canonical 6-tuple, or a 7-tuple when the
# instructor-gap flag adds a trailing idle term:
#   (tier_a, unres_students, unassigned_courses, clashes,
#    gap_minutes[+spread fold], reserve) [, instr_idle]
# Tiered (tiered flag ON): a fixed-length 9-tuple whose positions rank
# resolution by course tier. Real student gap-minutes sits *between* the tiers
# so the optimiser stops wrecking schedules for low-value (T3 / over-tolerance)
# enrolments. Smaller is better at every position, most-significant first.
TIERED_LEN = 9
TI_HIGHRISK = 0  # HARD: high-risk (RiskTier.A) students with an unresolved T1|T2 course
TI_CLASH = 1  # HARD: student double-booking (retained; ~always 0)
TI_T1 = 2  # HARD -> 0: unresolved (student, T1-course) pairs
TI_T2_OVER = 3  # near-hard: sum over T2 courses of max(0, unresolved - tolerance)
# Bounded student-cost: real_gap_minutes + soft_gap_budget * soft_unresolved.
# This is the position gaps and soft-tier enrolment TRADE against each other:
# the optimiser seats a soft course when it adds fewer than the budget in gap
# minutes. Pure real gap is recoverable as score[4] - budget * score[5].
TI_STUDENT_COST = 4
TI_SOFT = 5  # soft: T3 unresolved + T2 within-tolerance unresolved (pure count)
TI_RESERVE = 6  # reserve used
TI_SPREAD = 7  # same-course section spread, now its own quality term
TI_INSTR_IDLE = 8  # lowest priority; 0 unless the instructor-gap flag is on

# Legacy positions (flag OFF) for the shared accessors below.
_LEGACY_RESERVE = 5


def is_tiered_score(score: tuple[int, ...] | None) -> bool:
    """True when ``score`` is a tiered 9-tuple. Legacy is 6 or 7 — no collision.

    Tolerates ``None`` (returns False) so reporting-path accessors stay
    defensive, matching the guards the legacy decoders carried.
    """
    return score is not None and len(score) == TIERED_LEN


def reserve_used_of(score: tuple[int, ...] | None) -> int:
    """Reserve-used term, correct for either layout (idx 6 tiered / 5 legacy)."""
    if score is None:
        return 0
    if is_tiered_score(score):
        return int(score[TI_RESERVE])
    return int(score[_LEGACY_RESERVE]) if len(score) > _LEGACY_RESERVE else 0


def instructor_idle_of(score: tuple[int, ...] | None) -> int:
    """Instructor idle-minutes term: idx 8 tiered, trailing idx 6 legacy, else 0."""
    if score is None:
        return 0
    if is_tiered_score(score):
        return int(score[TI_INSTR_IDLE])
    return int(score[6]) if len(score) >= 7 else 0


def strip_instructor_idle(score: tuple[int, ...]) -> tuple[int, ...]:
    """Student + quality portion, dropping the trailing instructor-idle term.

    Lets an OFF-vs-ON comparison line up the substantive terms regardless of
    whether the instructor-gap term is present (8-slice tiered / 6-slice legacy).
    """
    return tuple(score[:8]) if is_tiered_score(score) else tuple(score[:6])


def decode_score(score: tuple[int, ...]) -> dict[str, int | str]:
    """Named view of an objective tuple, correct for either layout.

    A legacy (6/7) tuple returns exactly today's keys and values, so UI decoders
    stay byte-identical when the flag is off. A tiered (9) tuple returns the
    tier breakdown plus legacy-compatible aliases so existing keys still render.
    """
    if is_tiered_score(score):
        soft = int(score[TI_SOFT])
        student_cost = int(score[TI_STUDENT_COST])
        # Pure real student gap recovered from the blended student-cost term:
        # student_cost = real_gap + budget * soft  =>  real_gap = cost - budget*soft.
        real_gap = student_cost - get_tiered_soft_gap_budget() * soft
        return {
            "layout": "tiered",
            "highrisk_unresolved": int(score[TI_HIGHRISK]),
            "actual_assigned_clashes": int(score[TI_CLASH]),
            "t1_unresolved": int(score[TI_T1]),
            "t2_unresolved_over_tol": int(score[TI_T2_OVER]),
            "student_cost": student_cost,
            "gap_minutes": real_gap,
            "soft_unresolved": soft,
            "reserve_used": int(score[TI_RESERVE]),
            "same_course_spread": int(score[TI_SPREAD]),
            "instructor_idle": int(score[TI_INSTR_IDLE]),
            # legacy-compatible aliases so existing UI keys still render sensibly:
            "unresolved_tier_a": int(score[TI_HIGHRISK]),
            "unresolved_courses": int(score[TI_T1] + score[TI_T2_OVER] + soft),
        }
    return {
        "layout": "legacy",
        "unresolved_tier_a": int(score[0]),
        "blocked_students": int(score[1]),
        "unresolved_courses": int(score[2]),
        "actual_assigned_clashes": int(score[3]),
        "gap_minutes": int(score[4]),
        "reserve_used": int(score[5]),
    }


def evaluate_assignability_lexicographic(
    states: dict[str, StudentAssignmentState],
    profiles: dict[str, StudentProfile],
    sections_by_id: dict[str, SectionState],
    section_instructor_ids: dict[str, frozenset[int]] | None = None,
    course_tiers: dict[str, str] | None = None,
) -> tuple[int, ...]:
    # The tiered objective engages only when a per-run tier map is supplied AND
    # the flag is on. A missing map (course_tiers is None) forces the legacy
    # path even under the flag, so any un-threaded caller stays safe.
    tiered = course_tiers is not None and is_tiered_objective_enabled()
    tolerance = get_tiered_t2_tolerance() if tiered else 0
    soft_gap_budget = get_tiered_soft_gap_budget() if tiered else 0

    # Legacy accumulators (unchanged names/values; drive the OFF path).
    unresolved_tier_a = 0
    total_unresolved_students = 0
    total_unassigned_courses = 0
    total_clashes = 0
    real_gap_minutes = 0  # was total_gap_minutes; spread is NOT folded in here
    total_reserve_used = 0

    # Tiered accumulators.
    hr_unresolved = 0
    t1_unresolved = 0
    t2_unresolved_by_course: dict[str, int] = defaultdict(int)
    t3_unresolved = 0

    for sid, state in states.items():
        profile = profiles[sid]
        if state.unresolved_courses:
            total_unresolved_students += 1
            total_unassigned_courses += len(state.unresolved_courses)
            if profile.risk_tier == RiskTier.A:
                unresolved_tier_a += 1
            if tiered:
                is_high = profile.risk_tier == RiskTier.A
                hr_hit = False
                for course in state.unresolved_courses:  # key == SectionState.course_code
                    tier = course_tiers.get(course, "T1")
                    if tier == "T1":
                        t1_unresolved += 1
                    elif tier == "T2":
                        t2_unresolved_by_course[course] += 1
                    else:
                        t3_unresolved += 1
                    if is_high and tier in ("T1", "T2"):
                        hr_hit = True
                if hr_hit:
                    hr_unresolved += 1
        real_gap_minutes += state.total_gap_minutes

        meetings: list[SectionMeeting] = []
        for sec_id in state.section_ids:
            meetings.extend(sections_by_id[sec_id].meetings)
        for i in range(len(meetings)):
            for j in range(i + 1, len(meetings)):
                if meetings[i].day == meetings[j].day and (meetings[i].mask & meetings[j].mask):
                    total_clashes += 1

    for section in sections_by_id.values():
        total_reserve_used += section.reserve_used()

    # The same-course section-spread penalty (minutes-equivalent). Computed once
    # in every path — the legacy tuple folds it into gap-minutes; the tiered
    # tuple carries it as its own quality term so real student gaps are visible.
    spread = _compute_same_course_section_spread(sections_by_id)

    instr_idle = 0
    if section_instructor_ids is not None and is_instructor_gap_penalty_enabled():
        instr_idle = _compute_instructor_idle_minutes(sections_by_id, section_instructor_ids)

    if tiered:
        t2_over = 0
        t2_within = 0
        for unresolved in t2_unresolved_by_course.values():
            over = max(0, unresolved - tolerance)
            t2_over += over
            t2_within += unresolved - over  # == min(unresolved, tolerance)
        soft_unresolved = t3_unresolved + t2_within
        # Bounded trade: gaps and soft-tier enrolment compete on one axis. Each
        # unresolved soft seat is worth `soft_gap_budget` gap-minutes, so the
        # optimiser seats it iff doing so adds less than the budget in gaps.
        student_cost = real_gap_minutes + soft_gap_budget * soft_unresolved
        return (
            hr_unresolved,  # 0  A HARD
            total_clashes,  # 1    HARD
            t1_unresolved,  # 2  B HARD -> 0
            t2_over,  # 3  C near-hard
            student_cost,  # 4  D real_gap + budget * soft (bounded trade)
            soft_unresolved,  # 5  E soft count (tie-break; recovers real gap)
            total_reserve_used,  # 6  F
            spread,  # 7  F
            instr_idle,  # 8    lowest (0 unless instr-gap flag on)
        )

    # ── OFF path: byte-identical to pre-feature behaviour ──
    # real_gap_minutes + spread reproduces the old in-loop total_gap_minutes
    # plus the line-490 fold, exactly (deterministic integer add).
    total_gap_minutes = real_gap_minutes + spread
    base = (
        unresolved_tier_a,
        total_unresolved_students,
        total_unassigned_courses,
        total_clashes,
        total_gap_minutes,
        total_reserve_used,
    )
    # Append the instructor idle-gap term as a strictly-lowest-priority 7th
    # element — only when the flag is ON *and* a section→instructor map is
    # supplied. Otherwise the tuple stays the canonical 6-element shape.
    if section_instructor_ids is not None and is_instructor_gap_penalty_enabled():
        return (*base, instr_idle)
    return base

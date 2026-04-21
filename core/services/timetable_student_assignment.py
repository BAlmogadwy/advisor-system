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


def assign_students_to_sections(
    profiles: dict[str, StudentProfile],
    sections_by_id: dict[str, SectionState],
    sections_by_course: dict[str, list[SectionState]],
    course_rigidity: dict[str, float],
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
        )

    for student in tier_c:
        assign_courses_for_student(
            student,
            states[student.student_id],
            sections_by_course,
            sections_by_id,
            course_rigidity,
            reserves_released=True,
        )

    repair_unresolved_assignments_shallow(states, profiles, sections_by_course, sections_by_id)

    unresolved_ids = [sid for sid, st in states.items() if st.unresolved_courses]
    return states, unresolved_ids


def assign_courses_for_student(
    student: StudentProfile,
    state: StudentAssignmentState,
    sections_by_course: dict[str, list[SectionState]],
    sections_by_id: dict[str, SectionState],
    course_rigidity: dict[str, float],
    reserves_released: bool,
) -> None:
    allow_reserve = reserve_allowed_for_tier(student.risk_tier, reserves_released)

    for course_code in student.recommended_courses:
        if course_code not in state.assigned_sections:
            state.unresolved_courses.pop(course_code, None)

    unassigned = set(student.recommended_courses) - set(state.assigned_sections.keys())

    def scarcity_key(course_code: str) -> tuple[int, float, str]:
        candidates = sections_by_course.get(course_code, [])
        feasible_count = sum(
            1 for s in candidates if s.can_enroll(allow_reserve) and not state.has_clash(s.meetings)
        )
        return (feasible_count, -course_rigidity.get(course_code, 0.0), course_code)

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

    Penalty per pair of sections of the same course, summed over every
    pair of sections of every multi-section course:

    - same day, back-to-back (gap==0 min)        → 0
    - same day, gap 1-30 min                      → 30
    - same day, gap 31-120 min                    → 120
    - same day, gap > 120 min                     → gap_minutes
    - different days                              → 1000

    Expressed in "minutes-equivalent" so the result can be folded into
    ``total_gap_minutes`` without changing the tuple shape.
    """
    by_course: dict[str, list[SectionState]] = {}
    for sec in sections_by_id.values():
        by_course.setdefault(sec.course_code, []).append(sec)

    total = 0
    for secs in by_course.values():
        if len(secs) < 2:
            continue
        first_meetings = []
        for sec in secs:
            if not sec.meetings:
                continue
            anchor = min(sec.meetings, key=lambda m: (m.day, m.start_min))
            first_meetings.append(anchor)
        for i in range(len(first_meetings)):
            for j in range(i + 1, len(first_meetings)):
                a, b = first_meetings[i], first_meetings[j]
                if a.day != b.day:
                    total += 1000
                    continue
                gap = abs(a.start_min - b.start_min) - 75
                gap = max(0, gap)
                if gap == 0:
                    total += 0
                elif gap <= 30:
                    total += 30
                elif gap <= 120:
                    total += 120
                else:
                    total += gap
    return total


def evaluate_assignability_lexicographic(
    states: dict[str, StudentAssignmentState],
    profiles: dict[str, StudentProfile],
    sections_by_id: dict[str, SectionState],
) -> tuple[int, int, int, int, int, int]:
    unresolved_tier_a = 0
    total_unresolved_students = 0
    total_unassigned_courses = 0
    total_clashes = 0
    total_gap_minutes = 0
    total_reserve_used = 0

    for sid, state in states.items():
        profile = profiles[sid]
        if state.unresolved_courses:
            total_unresolved_students += 1
            total_unassigned_courses += len(state.unresolved_courses)
            if profile.risk_tier == RiskTier.A:
                unresolved_tier_a += 1
        total_gap_minutes += state.total_gap_minutes

        meetings: list[SectionMeeting] = []
        for sec_id in state.section_ids:
            meetings.extend(sections_by_id[sec_id].meetings)
        for i in range(len(meetings)):
            for j in range(i + 1, len(meetings)):
                if meetings[i].day == meetings[j].day and (meetings[i].mask & meetings[j].mask):
                    total_clashes += 1

    for section in sections_by_id.values():
        total_reserve_used += section.reserve_used()

    # Fold the same-course section-spread penalty into total_gap_minutes
    # so the tuple shape stays stable for downstream consumers (the
    # final_score array has been 6-element forever). The spread penalty
    # is already expressed in minutes-equivalent so the sum is
    # apples-to-apples with the student day-gap contribution.
    total_gap_minutes += _compute_same_course_section_spread(sections_by_id)

    return (
        unresolved_tier_a,
        total_unresolved_students,
        total_unassigned_courses,
        total_clashes,
        total_gap_minutes,
        total_reserve_used,
    )

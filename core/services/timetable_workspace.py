"""
core/services/timetable_workspace.py
Conflict detection and board analysis for the Timetable Workspace.

Reuses bitmask utilities from planner_builder.py for O(1) time-overlap detection.
"""

from __future__ import annotations

from dataclasses import dataclass

from collections import Counter, defaultdict

from core.models import (
    BoardStudentLink,
    DeliveryBoard,
    ScenarioSectionBudget,
    ScenarioStudentMap,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
)

# ── Bitmask constants (same as planner_builder) ─────────────────

_DAY_INDEX = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}
_SLOT_MINUTES = 5
_SLOTS_PER_DAY = 24 * 60 // _SLOT_MINUTES  # 288
_TOTAL_WEEK_SLOTS = 7 * _SLOTS_PER_DAY  # 2016


def _to_minutes(t: str) -> int:
    parts = t.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _time_mask(day: str, start_time: str, end_time: str) -> int:
    """Convert a day + start/end time to a 2016-bit bitmask."""
    d = str(day or "").upper()[:3]
    day_idx = _DAY_INDEX.get(d)
    if day_idx is None:
        return 0
    st = _to_minutes(start_time)
    en = _to_minutes(end_time)
    if en <= st:
        return 0
    start_idx = (day_idx * 24 * 60 + st) // _SLOT_MINUTES
    end_idx = (day_idx * 24 * 60 + en) // _SLOT_MINUTES
    start_idx = max(0, min(_TOTAL_WEEK_SLOTS, start_idx))
    end_idx = max(0, min(_TOTAL_WEEK_SLOTS, end_idx))
    if end_idx <= start_idx:
        return 0
    return ((1 << (end_idx - start_idx)) - 1) << start_idx


def _placement_mask(p: SectionPlacement) -> int:
    """Bitmask for a single placement row."""
    return _time_mask(p.day, p.start_time, p.end_time)


def _placement_full_mask(p: SectionPlacement) -> int:
    """Bitmask covering ALL meetings of the placed section's TermSection.

    A section may have multiple meeting rows (e.g., SUN+TUE).
    The placement row stores the primary slot, but we also need to check
    against all original meetings for accurate conflict detection.
    """
    mask = _time_mask(p.day, p.start_time, p.end_time)
    # Also include any other meetings from the TermSection
    meetings = TermSectionMeeting.objects.filter(term_section_id=p.term_section_id)
    for m in meetings:
        mask |= _time_mask(m.day, m.start_time, m.end_time)
    return mask


# ── Conflict Detection ──────────────────────────────────────────


@dataclass
class PlacementInfo:
    """Lightweight struct for conflict analysis."""
    id: int
    term_section_id: int
    course_code: str
    section: str
    day: str
    start_time: str
    end_time: str
    room: str
    instructor: str
    mask: int


def _load_board_placements(board_id: int) -> list[PlacementInfo]:
    """Load all placements for a board with computed bitmasks."""
    placements = (
        SectionPlacement.objects.filter(board_id=board_id)
        .select_related("term_section")
    )
    result = []
    for p in placements:
        # Get primary instructor from meetings
        meeting = TermSectionMeeting.objects.filter(term_section_id=p.term_section_id).first()
        instructor = meeting.instructor if meeting else ""

        result.append(PlacementInfo(
            id=p.id,
            term_section_id=p.term_section_id,
            course_code=p.term_section.course_code,
            section=p.term_section.section,
            day=p.day,
            start_time=p.start_time,
            end_time=p.end_time,
            room=p.room,
            instructor=instructor,
            mask=_time_mask(p.day, p.start_time, p.end_time),
        ))
    return result


def detect_board_conflicts(board_id: int) -> dict:
    """Detect all conflicts on a board.

    Returns:
        {
            "overlaps": [{"ids": [id1, id2], "sections": ["CS101-A", "CS201-B"], "detail": "..."}],
            "instructor_clashes": [{"ids": [...], "instructor": "...", "detail": "..."}],
            "room_clashes": [{"ids": [...], "room": "...", "detail": "..."}],
            "summary": {"critical": N, "warning": N, "info": 0},
        }
    """
    items = _load_board_placements(board_id)
    n = len(items)

    overlaps: list[dict] = []
    instructor_clashes: list[dict] = []
    room_clashes: list[dict] = []

    seen_overlap_pairs: set[tuple[int, int]] = set()
    seen_instr_pairs: set[tuple[int, int]] = set()
    seen_room_pairs: set[tuple[int, int]] = set()

    for i in range(n):
        for j in range(i + 1, n):
            a, b = items[i], items[j]
            pair = (a.id, b.id)

            # Time overlap check (bitmask AND)
            if a.mask & b.mask and pair not in seen_overlap_pairs:
                seen_overlap_pairs.add(pair)
                overlaps.append({
                    "ids": [a.id, b.id],
                    "sections": [f"{a.course_code}-{a.section}", f"{b.course_code}-{b.section}"],
                    "detail": f"{a.day} {a.start_time}-{a.end_time} vs {b.day} {b.start_time}-{b.end_time}",
                })

            # Instructor clash: same instructor + time overlap
            if (
                a.instructor
                and b.instructor
                and a.instructor.strip().upper() == b.instructor.strip().upper()
                and a.mask & b.mask
                and pair not in seen_instr_pairs
            ):
                seen_instr_pairs.add(pair)
                instructor_clashes.append({
                    "ids": [a.id, b.id],
                    "instructor": a.instructor,
                    "sections": [f"{a.course_code}-{a.section}", f"{b.course_code}-{b.section}"],
                    "detail": f"{a.instructor}: {a.day} {a.start_time} vs {b.day} {b.start_time}",
                })

            # Room clash: same non-empty room + time overlap
            if (
                a.room
                and b.room
                and a.room.strip().upper() == b.room.strip().upper()
                and a.mask & b.mask
                and pair not in seen_room_pairs
            ):
                seen_room_pairs.add(pair)
                room_clashes.append({
                    "ids": [a.id, b.id],
                    "room": a.room,
                    "sections": [f"{a.course_code}-{a.section}", f"{b.course_code}-{b.section}"],
                    "detail": f"Room {a.room}: {a.day} {a.start_time} vs {b.day} {b.start_time}",
                })

    critical = len(overlaps) + len(instructor_clashes)
    warning = len(room_clashes)

    return {
        "overlaps": overlaps,
        "instructor_clashes": instructor_clashes,
        "room_clashes": room_clashes,
        "summary": {"critical": critical, "warning": warning, "info": 0},
    }


def validate_placement(
    board_id: int,
    day: str,
    start_time: str,
    end_time: str,
    room: str,
    term_section_id: int,
    exclude_placement_id: int | None = None,
) -> dict:
    """Validate a single placement against all others on the board.

    Used for both new placements and moves (pass exclude_placement_id for moves).

    Returns:
        {
            "valid": bool,
            "overlaps": [...],
            "instructor_clashes": [...],
            "room_clashes": [...],
            "critical_count": int,
            "warning_count": int,
        }
    """
    items = _load_board_placements(board_id)
    new_mask = _time_mask(day, start_time, end_time)

    # Get instructor for the section being placed
    meeting = TermSectionMeeting.objects.filter(term_section_id=term_section_id).first()
    new_instructor = meeting.instructor if meeting else ""

    ts = TermSection.objects.filter(id=term_section_id).first()
    new_label = f"{ts.course_code}-{ts.section}" if ts else str(term_section_id)

    overlaps: list[dict] = []
    instructor_clashes: list[dict] = []
    room_clashes: list[dict] = []

    for item in items:
        if item.id == exclude_placement_id:
            continue

        # Time overlap
        if new_mask & item.mask:
            overlaps.append({
                "id": item.id,
                "section": f"{item.course_code}-{item.section}",
                "detail": f"{item.day} {item.start_time}-{item.end_time}",
            })

            # Instructor clash
            if (
                new_instructor
                and item.instructor
                and new_instructor.strip().upper() == item.instructor.strip().upper()
            ):
                instructor_clashes.append({
                    "id": item.id,
                    "instructor": new_instructor,
                    "section": f"{item.course_code}-{item.section}",
                })

            # Room clash
            if room and item.room and room.strip().upper() == item.room.strip().upper():
                room_clashes.append({
                    "id": item.id,
                    "room": room,
                    "section": f"{item.course_code}-{item.section}",
                })

    critical = len(overlaps) + len(instructor_clashes)
    warning = len(room_clashes)

    return {
        "valid": critical == 0 and warning == 0,
        "overlaps": overlaps,
        "instructor_clashes": instructor_clashes,
        "room_clashes": room_clashes,
        "critical_count": critical,
        "warning_count": warning,
    }


# ── Direct Student Feasibility ───────────────────────────────────


def compute_affected_students(board_id: int) -> dict:
    """Compute directly affected students for overlapping placements on a board.

    For each overlap pair:
    1. Find courses involved
    2. Find students who need BOTH courses (status='studying')
    3. For each affected student, check if an alternative section exists
       (same course, different section, no time conflict with the other course)
    4. Return affected/blocked/resolvable counts

    Returns:
        {
            "affected_count": int,
            "blocked_count": int,
            "resolvable_count": int,
            "overlap_details": [
                {
                    "courses": [code_a, code_b],
                    "affected": int,
                    "blocked": int,
                    "students": [student_id, ...] (first 20)
                }
            ]
        }
    """
    from core.models import Course, StudentCourse

    conflicts = detect_board_conflicts(board_id)
    overlaps = conflicts.get("overlaps", [])

    if not overlaps:
        return {"affected_count": 0, "blocked_count": 0, "resolvable_count": 0, "overlap_details": []}

    # Build a set of all placed section masks for alternative checking
    all_placements = _load_board_placements(board_id)
    # Build course → available TermSections (all, not just placed)
    from core.models import TermSectionMeeting as TSM

    total_affected = 0
    total_blocked = 0
    overlap_details = []

    for overlap in overlaps:
        sections = overlap.get("sections", [])
        ids = overlap.get("ids", [])
        if len(sections) < 2:
            continue

        # Extract course codes from "CS101-A" format
        code_a = sections[0].rsplit("-", 1)[0] if "-" in sections[0] else sections[0]
        code_b = sections[1].rsplit("-", 1)[0] if "-" in sections[1] else sections[1]

        if code_a == code_b:
            continue  # Same course different sections — not a student conflict

        # Find students studying BOTH courses
        students_a = set(
            StudentCourse.objects.filter(
                course__course_code=code_a, status="studying"
            ).values_list("student_id", flat=True)
        )
        students_b = set(
            StudentCourse.objects.filter(
                course__course_code=code_b, status="studying"
            ).values_list("student_id", flat=True)
        )
        affected_students = students_a & students_b

        if not affected_students:
            continue

        # Check if alternatives exist: other sections of course_a or course_b
        # that don't overlap with the conflicting placement
        # Get the masks of the two conflicting placements
        mask_a = 0
        mask_b = 0
        for p in all_placements:
            if p.id == ids[0]:
                mask_a = p.mask
            elif p.id == ids[1]:
                mask_b = p.mask

        # Find alternative sections for course_a (not overlapping with placement_b)
        alt_a_sections = TermSection.objects.filter(course_code=code_a).exclude(
            id__in=[p.term_section_id for p in all_placements if p.course_code == code_a]
        )
        has_alt_a = False
        for alt_ts in alt_a_sections:
            alt_meetings = TSM.objects.filter(term_section=alt_ts)
            alt_mask = 0
            for m in alt_meetings:
                alt_mask |= _time_mask(m.day, m.start_time, m.end_time)
            if not (alt_mask & mask_b):
                has_alt_a = True
                break

        # Find alternative sections for course_b (not overlapping with placement_a)
        alt_b_sections = TermSection.objects.filter(course_code=code_b).exclude(
            id__in=[p.term_section_id for p in all_placements if p.course_code == code_b]
        )
        has_alt_b = False
        for alt_ts in alt_b_sections:
            alt_meetings = TSM.objects.filter(term_section=alt_ts)
            alt_mask = 0
            for m in alt_meetings:
                alt_mask |= _time_mask(m.day, m.start_time, m.end_time)
            if not (alt_mask & mask_a):
                has_alt_b = True
                break

        resolvable = has_alt_a or has_alt_b
        affected_count = len(affected_students)
        blocked_count = 0 if resolvable else affected_count

        total_affected += affected_count
        total_blocked += blocked_count

        overlap_details.append({
            "courses": [code_a, code_b],
            "affected": affected_count,
            "blocked": blocked_count,
            "resolvable": resolvable,
            "students": sorted(affected_students)[:20],
        })

    return {
        "affected_count": total_affected,
        "blocked_count": total_blocked,
        "resolvable_count": total_affected - total_blocked,
        "overlap_details": overlap_details,
    }


# ── Board Summary ───────────────────────────────────────────────


def compute_board_summary(board_id: int) -> dict:
    """Compute summary stats for a board."""
    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return {}

    placed_count = SectionPlacement.objects.filter(board_id=board_id).count()

    # Get conflict summary
    conflicts = detect_board_conflicts(board_id)
    summary = conflicts["summary"]

    return {
        "board_id": board_id,
        "label": board.label,
        "placed_sections": placed_count,
        "target_size": board.target_size,
        "critical_conflicts": summary["critical"],
        "warning_conflicts": summary["warning"],
    }


def get_scenario_boards_summary(scenario_id: int) -> list[dict]:
    """Get all boards for a scenario with conflict summaries and student counts."""
    boards = DeliveryBoard.objects.filter(scenario_id=scenario_id).order_by("display_order")
    result = []
    for b in boards:
        placed = SectionPlacement.objects.filter(board=b).count()
        conflicts = detect_board_conflicts(b.id)
        primary_count = BoardStudentLink.objects.filter(board=b, link_type="primary").count()
        visitor_count = BoardStudentLink.objects.filter(board=b, link_type="visitor").count()
        result.append({
            "id": b.id,
            "label": b.label,
            "nominal_term": b.nominal_term,
            "board_type": b.board_type,
            "program": b.program,
            "placement_count": placed,
            "primary_count": primary_count,
            "visitor_count": visitor_count,
            "critical": conflicts["summary"]["critical"],
            "warning": conflicts["summary"]["warning"],
        })
    return result


# ── Section Budget ──────────────────────────────────────────────


def compute_scenario_budget(scenario_id: int) -> list[dict]:
    """Compute section budget consumption across all boards in a scenario.

    Returns per-course: planned, used, remaining, which boards are using it.
    """
    budgets = ScenarioSectionBudget.objects.filter(scenario_id=scenario_id)

    # Count DISTINCT sections per course (not placement rows — a 4cr section has 3 placements)
    placements = (
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .select_related("term_section")
    )
    used_sections: dict[str, set[str]] = defaultdict(set)  # course_code -> set of section labels
    board_usage: dict[str, set[int]] = defaultdict(set)
    for p in placements:
        code = p.term_section.course_code
        sec = p.term_section.section
        used_sections[code].add(sec)
        board_usage[code].add(p.board_id)
    used_counts: Counter[str] = Counter({k: len(v) for k, v in used_sections.items()})

    result = []
    for b in budgets:
        used = used_counts.get(b.course_code, 0)
        result.append({
            "course_code": b.course_code,
            "department": b.department,
            "credit_hours": b.credit_hours,
            "programme_term": b.programme_term,
            "planned_sections": b.planned_sections,
            "max_per_section": b.max_per_section,
            "total_demand": b.total_demand,
            "used_sections": used,
            "remaining_sections": max(0, b.planned_sections - used),
            "boards_using": sorted(board_usage.get(b.course_code, set())),
        })

    result.sort(key=lambda x: (x.get("programme_term") or 0, x["course_code"]))
    return result


# ── Cross-Board Conflict Detection ──────────────────────────────


def detect_cross_board_conflicts(scenario_id: int) -> list[dict]:
    """Detect conflicts between placements on different boards that affect shared students.

    For each pair of boards, checks if any placements overlap in time AND
    have students who need courses from both boards.
    """
    boards = list(DeliveryBoard.objects.filter(scenario_id=scenario_id).order_by("display_order"))
    if len(boards) < 2:
        return []

    # Load all placements per board
    board_placements: dict[int, list[PlacementInfo]] = {}
    for b in boards:
        board_placements[b.id] = _load_board_placements(b.id)

    # Load student course needs from ScenarioStudentMap
    student_maps = ScenarioStudentMap.objects.filter(scenario_id=scenario_id)
    course_students: dict[str, set[int]] = defaultdict(set)
    for sm in student_maps:
        for code in sm.recommended_courses:
            course_students[code].add(sm.student_id)

    conflicts: list[dict] = []
    seen: set[tuple] = set()

    for i, board_a in enumerate(boards):
        for board_b in boards[i + 1:]:
            pa_list = board_placements.get(board_a.id, [])
            pb_list = board_placements.get(board_b.id, [])

            for pa in pa_list:
                for pb in pb_list:
                    if pa.mask & pb.mask:  # Time overlap
                        # How many students need BOTH courses?
                        shared = course_students.get(pa.course_code, set()) & course_students.get(
                            pb.course_code, set()
                        )
                        if shared:
                            pair_key = (
                                min(pa.course_code, pb.course_code),
                                max(pa.course_code, pb.course_code),
                            )
                            if pair_key in seen:
                                continue
                            seen.add(pair_key)

                            conflicts.append({
                                "course_a": pa.course_code,
                                "section_a": f"{pa.course_code}-{pa.section}",
                                "board_a_id": board_a.id,
                                "board_a_label": board_a.label,
                                "course_b": pb.course_code,
                                "section_b": f"{pb.course_code}-{pb.section}",
                                "board_b_id": board_b.id,
                                "board_b_label": board_b.label,
                                "overlap_count": len(shared),
                                "time": f"{pa.day} {pa.start_time}-{pa.end_time} vs "
                                        f"{pb.day} {pb.start_time}-{pb.end_time}",
                            })

    conflicts.sort(key=lambda x: -x["overlap_count"])
    return conflicts


# ── Publish Readiness ───────────────────────────────────────────


def check_publish_readiness(scenario_id: int) -> dict:
    """Check if a scenario is ready for publish.

    Returns:
        {"ready": bool, "blockers": [...], "warnings": [...]}
    """
    boards = DeliveryBoard.objects.filter(scenario_id=scenario_id)
    blockers: list[str] = []
    warnings: list[str] = []

    if boards.count() == 0:
        blockers.append("No boards in this scenario")
        return {"ready": False, "blockers": blockers, "warnings": warnings}

    for board in boards:
        placed = SectionPlacement.objects.filter(board=board).count()
        if placed == 0:
            blockers.append(f"Board '{board.label}' has no placements")
            continue

        conflicts = detect_board_conflicts(board.id)
        if conflicts["summary"]["critical"] > 0:
            blockers.append(
                f"Board '{board.label}': {conflicts['summary']['critical']} critical conflicts"
            )
        if conflicts["summary"]["warning"] > 0:
            warnings.append(
                f"Board '{board.label}': {conflicts['summary']['warning']} warnings"
            )

    return {
        "ready": len(blockers) == 0,
        "blockers": blockers,
        "warnings": warnings,
    }

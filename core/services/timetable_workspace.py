"""
core/services/timetable_workspace.py
Conflict detection, student feasibility, and board analysis for the
Timetable Workspace feature.

The workspace lets advisors drag-and-drop course sections onto weekly
schedule boards (DeliveryBoards) grouped under a Scenario.  This module
provides the analytical back-end:

    1. **Bitmask time-overlap engine** -- Each weekly meeting slot is
       mapped to a single bit in a 2016-bit integer (7 days x 288
       five-minute slots per day).  Two placements overlap iff their
       bitmask AND is non-zero, giving O(1) pairwise conflict checks.
       The scheme is shared with ``planner_builder.py``.

    2. **Board conflict detection** -- finds time overlaps, instructor
       double-bookings, and room double-bookings within a single board.

    3. **Placement validation** -- pre-flight check for a new or moved
       placement against the existing board state.

    4. **Student feasibility** -- for every detected overlap, counts
       how many students need *both* clashing courses and whether an
       alternative section exists that resolves the clash.

    5. **Section budget tracking** -- compares the number of distinct
       sections placed across boards against the planned section budget.

    6. **Cross-board conflict detection** -- finds time overlaps
       between *different* boards that affect shared students (students
       whose recommended courses span both boards).

    7. **Publish readiness** -- aggregates blockers (critical conflicts,
       empty boards) and warnings before a scenario is finalised.

Key models used:
    - ``DeliveryBoard``        -- a named weekly schedule grid
    - ``SectionPlacement``     -- a section pinned to a day/time/room on a board
    - ``TermSection``          -- the catalogue section (course_code + section label)
    - ``TermSectionMeeting``   -- one meeting row of a TermSection (day + times)
    - ``ScenarioSectionBudget``-- per-course budget (planned sections / max per section)
    - ``ScenarioStudentMap``   -- per-student list of recommended courses
    - ``BoardStudentLink``     -- many-to-many link of students to boards
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

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
#
# The week is divided into 5-minute slots.  Each slot maps to one bit
# position in a Python arbitrary-precision integer:
#
#   bit index = day_index * 288  +  (minute_of_day // 5)
#
# With 7 days x 288 slots = 2016 bits total.  Two meetings overlap
# when their bitmask AND produces a non-zero result.

_DAY_INDEX = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}
_SLOT_MINUTES = 5
_SLOTS_PER_DAY = 24 * 60 // _SLOT_MINUTES  # 288 slots per day
_TOTAL_WEEK_SLOTS = 7 * _SLOTS_PER_DAY  # 2016 bits for the whole week


def _to_minutes(t: str) -> int:
    """Convert an ``"HH:MM"`` string to total minutes since midnight."""
    parts = t.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _time_mask(day: str, start_time: str, end_time: str) -> int:
    """Build a 2016-bit bitmask for a single day/start/end meeting slot.

    Parameters:
        day:        Three-letter day abbreviation (e.g. ``"SUN"``, ``"MON"``).
        start_time: Meeting start in ``"HH:MM"`` (24-hour).
        end_time:   Meeting end   in ``"HH:MM"`` (24-hour).

    Returns:
        An integer whose set bits represent the 5-minute slots occupied
        by this meeting.  Returns ``0`` for invalid or zero-length input.

    Algorithm:
        1. Map the day string to an offset (0-6) within the 2016-bit week.
        2. Convert start/end to slot indices.
        3. Create a run of ``(end_idx - start_idx)`` contiguous 1-bits
           shifted left by ``start_idx``.  Example for a 50-minute class
           (10 slots): ``0b1111111111 << start_idx``.
    """
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
    # Clamp to valid bit range [0, 2016]
    start_idx = max(0, min(_TOTAL_WEEK_SLOTS, start_idx))
    end_idx = max(0, min(_TOTAL_WEEK_SLOTS, end_idx))
    if end_idx <= start_idx:
        return 0
    # Build a contiguous run of 1-bits: ((1 << width) - 1) << offset
    return ((1 << (end_idx - start_idx)) - 1) << start_idx


def _placement_mask(p: SectionPlacement) -> int:
    """Return the bitmask for a single ``SectionPlacement`` row.

    This covers only the placement's own day/time slot.  For a mask that
    includes *all* meetings of the underlying TermSection (e.g. a
    SUN+TUE lecture), use :func:`_placement_full_mask` instead.
    """
    return _time_mask(p.day, p.start_time, p.end_time)


def _placement_full_mask(p: SectionPlacement) -> int:
    """Return a bitmask covering **all** meetings of the placed section.

    A TermSection may have multiple ``TermSectionMeeting`` rows -- for
    example a lecture on SUN and TUE.  The ``SectionPlacement`` row only
    stores the *primary* day/time, so this helper ORs in every meeting's
    mask for complete conflict coverage.

    Parameters:
        p: A ``SectionPlacement`` instance.

    Returns:
        Combined bitmask (placement's own slot | all TermSectionMeetings).

    Note:
        Issues one extra query per call.  Prefer the pre-loaded masks in
        ``_load_board_placements`` when checking an entire board.
    """
    mask = _time_mask(p.day, p.start_time, p.end_time)
    # OR-in every meeting row from the catalogue section
    meetings = TermSectionMeeting.objects.filter(term_section_id=p.term_section_id)
    for m in meetings:
        mask |= _time_mask(m.day, m.start_time, m.end_time)
    return mask


# ── Conflict Detection ──────────────────────────────────────────


@dataclass
class PlacementInfo:
    """Lightweight, in-memory representation of a placed section.

    Built by :func:`_load_board_placements` so that conflict-detection
    loops can compare placements without touching the database again.

    Attributes:
        id:               PK of the ``SectionPlacement`` row.
        term_section_id:  FK to ``TermSection``.
        course_code:      e.g. ``"CS101"``.
        section:          Section label, e.g. ``"A"``, ``"B1"``.
        day:              Three-letter day (``"SUN"`` etc.).
        start_time:       ``"HH:MM"`` start.
        end_time:         ``"HH:MM"`` end.
        room:             Room code (may be empty).
        instructor:       Instructor name from the first meeting row
                          (empty string if none recorded).
        mask:             Pre-computed 2016-bit bitmask for the
                          placement's own day/time slot.
    """

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
    """Batch-load every placement on a board into ``PlacementInfo`` structs.

    Uses ``select_related`` + ``prefetch_related`` so the entire board
    is fetched in two queries regardless of how many sections are placed.

    Parameters:
        board_id: PK of the ``DeliveryBoard``.

    Returns:
        List of :class:`PlacementInfo` objects with pre-computed masks.
    """
    placements = (
        SectionPlacement.objects.filter(board_id=board_id)
        .select_related("term_section")
        .prefetch_related("term_section__meetings")
    )
    result = []
    for p in placements:
        # Instructor comes from the first prefetched meeting (no extra query)
        meetings = list(p.term_section.meetings.all())
        instructor = meetings[0].instructor if meetings else ""

        result.append(
            PlacementInfo(
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
            )
        )
    return result


def detect_board_conflicts(board_id: int) -> dict:
    """Find every scheduling conflict on a single board.

    Performs an O(n^2) pairwise scan of all placements on the board and
    classifies conflicts into three severity buckets:

    * **overlaps** (critical) -- two sections occupy the same time slot.
      Detected via ``mask_a & mask_b != 0``.
    * **instructor_clashes** (critical) -- same instructor teaching two
      sections at overlapping times.  Compared case-insensitively.
    * **room_clashes** (warning) -- same room assigned to two sections
      at overlapping times.  Compared case-insensitively.

    Parameters:
        board_id: PK of the ``DeliveryBoard`` to analyse.

    Returns:
        A dict with the shape::

            {
                "overlaps":            [{"ids": [...], "sections": [...], "detail": "..."}],
                "instructor_clashes":  [{"ids": [...], "instructor": "...", ...}],
                "room_clashes":        [{"ids": [...], "room": "...", ...}],
                "summary":             {"critical": N, "warning": N, "info": 0},
            }

        ``critical = len(overlaps) + len(instructor_clashes)``; ``warning = len(room_clashes)``.
    """
    items = _load_board_placements(board_id)

    overlaps: list[dict] = []
    instructor_clashes: list[dict] = []
    room_clashes: list[dict] = []

    # ── Build real student-overlap matrix for this board ──
    from collections import defaultdict

    from core.services.timetable_overlap import build_overlap_matrix, courses_share_students

    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
        board_courses = {item.course_code for item in items}
        overlap_matrix = build_overlap_matrix(board.scenario_id, board_courses)
    except DeliveryBoard.DoesNotExist:
        overlap_matrix = {}

    by_instructor: dict[str, list] = defaultdict(list)
    by_room: dict[str, list] = defaultdict(list)
    for item in items:
        if item.instructor:
            by_instructor[item.instructor.strip().upper()].append(item)
        if item.room and item.room.strip().upper() != "UNASSIGNED":
            by_room[item.room.strip().upper()].append(item)

    seen_pairs: set[tuple[int, int]] = set()

    def _pair_key(a_id: int, b_id: int) -> tuple[int, int]:
        return (min(a_id, b_id), max(a_id, b_id))

    # ── Student conflict detection (real overlap, not fake cohort) ──
    n = len(items)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = items[i], items[j]
            if not (a.mask & b.mask):
                continue
            pk = _pair_key(a.id, b.id)
            if pk in seen_pairs:
                continue
            same_course = a.course_code == b.course_code
            shares = courses_share_students(overlap_matrix, a.course_code, b.course_code)
            if same_course or shares:
                seen_pairs.add(pk)
                overlaps.append(
                    {
                        "ids": [a.id, b.id],
                        "sections": [
                            f"{a.course_code}-{a.section}",
                            f"{b.course_code}-{b.section}",
                        ],
                        "detail": f"{a.day} {a.start_time}-{a.end_time} vs {b.day} {b.start_time}-{b.end_time}",
                    }
                )

    # ── Instructor clash detection (any group) ──
    seen_instr: set[tuple[int, int]] = set()
    for _instr, instr_items in by_instructor.items():
        n = len(instr_items)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = instr_items[i], instr_items[j]
                pk = _pair_key(a.id, b.id)
                if a.mask & b.mask and pk not in seen_instr:
                    seen_instr.add(pk)
                    instructor_clashes.append(
                        {
                            "ids": [a.id, b.id],
                            "instructor": a.instructor,
                            "sections": [
                                f"{a.course_code}-{a.section}",
                                f"{b.course_code}-{b.section}",
                            ],
                            "detail": f"{a.instructor}: {a.day} {a.start_time} vs {b.day} {b.start_time}",
                        }
                    )

    # ── Room clash detection (any group) ──
    seen_room: set[tuple[int, int]] = set()
    for _rm, room_items in by_room.items():
        n = len(room_items)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = room_items[i], room_items[j]
                pk = _pair_key(a.id, b.id)
                if a.mask & b.mask and pk not in seen_room:
                    seen_room.add(pk)
                    room_clashes.append(
                        {
                            "ids": [a.id, b.id],
                            "room": a.room,
                            "sections": [
                                f"{a.course_code}-{a.section}",
                                f"{b.course_code}-{b.section}",
                            ],
                            "detail": f"Room {a.room}: {a.day} {a.start_time} vs {b.day} {b.start_time}",
                        }
                    )

    # Overlaps and instructor clashes are "critical" (block publish);
    # room clashes are "warning" (advisory only).
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
    """Pre-flight validation for a new or moved placement.

    Builds a bitmask for the *candidate* placement and checks it against
    every existing placement on the board.  For a move operation, pass
    ``exclude_placement_id`` so the section's current position is not
    compared against itself.

    Parameters:
        board_id:             Target board PK.
        day:                  Candidate day (``"SUN"`` etc.).
        start_time:           Candidate start ``"HH:MM"``.
        end_time:             Candidate end ``"HH:MM"``.
        room:                 Candidate room code (may be empty).
        term_section_id:      PK of the ``TermSection`` being placed.
        exclude_placement_id: If moving an existing placement, pass its
                              PK here to skip self-comparison.

    Returns:
        A dict with the shape::

            {
                "valid":              bool,   # True when zero conflicts
                "overlaps":           [...],
                "instructor_clashes": [...],
                "room_clashes":       [...],
                "critical_count":     int,
                "warning_count":      int,
            }
    """
    items = _load_board_placements(board_id)
    new_mask = _time_mask(day, start_time, end_time)

    # Get instructor for the section being placed
    meeting = TermSectionMeeting.objects.filter(term_section_id=term_section_id).first()
    new_instructor = meeting.instructor if meeting else ""

    ts = TermSection.objects.filter(id=term_section_id).first()

    # Build real overlap matrix for this board
    from core.services.timetable_overlap import courses_share_students as _shares

    try:
        board_obj = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
        board_courses = {item.course_code for item in items}
        if ts:
            board_courses.add(ts.course_code)
        from core.services.timetable_overlap import build_overlap_matrix as _bom

        _overlap_mat = _bom(board_obj.scenario_id, board_courses)
    except DeliveryBoard.DoesNotExist:
        _overlap_mat = {}

    overlaps: list[dict] = []
    instructor_clashes: list[dict] = []
    room_clashes: list[dict] = []

    for item in items:
        if item.id == exclude_placement_id:
            continue

        # Time overlap — flag if courses share students (real overlap)
        if new_mask & item.mask:
            same_course = ts and item.course_code == ts.course_code
            shares_students = ts and _shares(_overlap_mat, ts.course_code, item.course_code)

            if same_course or shares_students:
                overlaps.append(
                    {
                        "id": item.id,
                        "section": f"{item.course_code}-{item.section}",
                        "detail": f"{item.day} {item.start_time}-{item.end_time}",
                    }
                )

            # Instructor clash
            if (
                new_instructor
                and item.instructor
                and new_instructor.strip().upper() == item.instructor.strip().upper()
            ):
                instructor_clashes.append(
                    {
                        "id": item.id,
                        "instructor": new_instructor,
                        "section": f"{item.course_code}-{item.section}",
                    }
                )

            # Room clash (exclude UNASSIGNED)
            if (
                room
                and item.room
                and room.strip().upper() != "UNASSIGNED"
                and item.room.strip().upper() != "UNASSIGNED"
                and room.strip().upper() == item.room.strip().upper()
            ):
                room_clashes.append(
                    {
                        "id": item.id,
                        "room": room,
                        "section": f"{item.course_code}-{item.section}",
                    }
                )

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
    """Assess student impact of time-overlapping placements on a board.

    For every pair of overlapping placements (detected by
    :func:`detect_board_conflicts`):

    1. Extract the two course codes from the ``"CS101-A"`` labels.
    2. Query ``StudentCourse`` for students currently ``status='studying'``
       *both* courses -- these are the "affected" students.
    3. Search for **alternative sections** of either course that do not
       overlap with the *other* placement's time slot.  If at least one
       alternative exists, the overlap is "resolvable"; otherwise
       affected students are "blocked" (no schedule exists for them).

    Parameters:
        board_id: PK of the ``DeliveryBoard``.

    Returns:
        A dict with the shape::

            {
                "affected_count":   int,  # total students across all overlaps
                "blocked_count":    int,  # students with NO alternative section
                "resolvable_count": int,  # affected - blocked
                "overlap_details":  [
                    {
                        "courses":    [code_a, code_b],
                        "affected":   int,
                        "blocked":    int,
                        "resolvable": bool,
                        "students":   [student_id, ...]  # first 20
                    },
                    ...
                ]
            }

    Business rule:
        Same-course overlaps (two sections of ``CS101``) are skipped
        because a student only registers for one section of a course.
    """
    conflicts = detect_board_conflicts(board_id)
    overlaps = conflicts.get("overlaps", [])

    if not overlaps:
        return {
            "affected_count": 0,
            "blocked_count": 0,
            "resolvable_count": 0,
            "overlap_details": [],
        }

    all_placements = _load_board_placements(board_id)
    from core.models import TermSectionMeeting as TSM

    # ── Precompute course→students map from ScenarioStudentMap (not StudentCourse) ──
    from core.services.timetable_overlap import build_course_students_map

    overlap_courses: set[str] = set()
    for overlap in overlaps:
        for sec in overlap.get("sections", []):
            code = sec.rsplit("-", 1)[0] if "-" in sec else sec
            overlap_courses.add(code)

    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
        course_students_map = build_course_students_map(board.scenario_id, overlap_courses)
    except DeliveryBoard.DoesNotExist:
        course_students_map = {}

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
            continue

        # Find students studying BOTH courses (from precomputed map)
        affected_students = course_students_map.get(code_a, set()) & course_students_map.get(
            code_b, set()
        )

        if not affected_students:
            continue

        # --- Alternative-section search ---
        # If we can find at least one *unplaced* section of course_a
        # whose meetings do not overlap with placement_b (or vice versa),
        # then the conflict is "resolvable" -- the student could be moved
        # to the alternative section.

        # Retrieve the bitmasks of the two conflicting placements.
        mask_a = 0
        mask_b = 0
        for p in all_placements:
            if p.id == ids[0]:
                mask_a = p.mask
            elif p.id == ids[1]:
                mask_b = p.mask

        # Alternative sections for course_a that don't clash with placement_b
        alt_a_sections = TermSection.objects.filter(course_code=code_a).exclude(
            id__in=[p.term_section_id for p in all_placements if p.course_code == code_a]
        )
        has_alt_a = False
        for alt_ts in alt_a_sections:
            alt_meetings = TSM.objects.filter(term_section=alt_ts)
            alt_mask = 0
            for m in alt_meetings:
                alt_mask |= _time_mask(m.day, m.start_time, m.end_time)
            if not (alt_mask & mask_b):  # No overlap with placement_b
                has_alt_a = True
                break

        # Alternative sections for course_b that don't clash with placement_a
        alt_b_sections = TermSection.objects.filter(course_code=code_b).exclude(
            id__in=[p.term_section_id for p in all_placements if p.course_code == code_b]
        )
        has_alt_b = False
        for alt_ts in alt_b_sections:
            alt_meetings = TSM.objects.filter(term_section=alt_ts)
            alt_mask = 0
            for m in alt_meetings:
                alt_mask |= _time_mask(m.day, m.start_time, m.end_time)
            if not (alt_mask & mask_a):  # No overlap with placement_a
                has_alt_b = True
                break

        # Either direction resolves the overlap for the student.
        resolvable = has_alt_a or has_alt_b
        affected_count = len(affected_students)
        blocked_count = 0 if resolvable else affected_count

        total_affected += affected_count
        total_blocked += blocked_count

        overlap_details.append(
            {
                "courses": [code_a, code_b],
                "affected": affected_count,
                "blocked": blocked_count,
                "resolvable": resolvable,
                "students": sorted(affected_students)[:20],
            }
        )

    return {
        "affected_count": total_affected,
        "blocked_count": total_blocked,
        "resolvable_count": total_affected - total_blocked,
        "overlap_details": overlap_details,
    }


# ── Board Summary ───────────────────────────────────────────────


def compute_board_summary(board_id: int) -> dict:
    """Return high-level statistics for a single board.

    Parameters:
        board_id: PK of the ``DeliveryBoard``.

    Returns:
        A dict with ``board_id``, ``label``, ``placed_sections``,
        ``target_size``, ``critical_conflicts``, and
        ``warning_conflicts``.  Empty dict if the board does not exist.
    """
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
    """Return a summary row for every board in a scenario.

    Each row includes placement count, primary/visitor student counts,
    and critical/warning conflict tallies.  Used by the scenario
    overview panel in the workspace UI.

    Parameters:
        scenario_id: PK of the parent ``Scenario``.

    Returns:
        List of dicts ordered by ``DeliveryBoard.display_order``.
    """
    boards = DeliveryBoard.objects.filter(scenario_id=scenario_id).order_by("display_order")
    result = []
    for b in boards:
        placed = SectionPlacement.objects.filter(board=b).count()
        conflicts = detect_board_conflicts(b.id)
        primary_count = BoardStudentLink.objects.filter(board=b, link_type="primary").count()
        visitor_count = BoardStudentLink.objects.filter(board=b, link_type="visitor").count()
        result.append(
            {
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
            }
        )
    return result


# ── Section Budget ──────────────────────────────────────────────


def compute_scenario_budget(scenario_id: int) -> list[dict]:
    """Compare planned section budgets against actual board usage.

    For every ``ScenarioSectionBudget`` row (one per course), counts how
    many **distinct sections** have been placed across all boards in the
    scenario and reports planned / used / remaining.

    Important:
        A 4-credit section typically generates 3 ``SectionPlacement``
        rows (lecture + lab + tutorial).  The budget counts **distinct
        section labels** (e.g. ``{"A", "B"}``), not raw placement rows.

    Parameters:
        scenario_id: PK of the parent ``Scenario``.

    Returns:
        List of per-course dicts sorted by ``(programme_term, course_code)``.
    """
    budgets = ScenarioSectionBudget.objects.filter(scenario_id=scenario_id)

    # Count DISTINCT sections per course -- keyed by course_code, valued
    # by the *set* of section labels (e.g. {"A", "B"}).  This avoids
    # double-counting multi-placement sections (lecture + lab rows).
    placements = SectionPlacement.objects.filter(board__scenario_id=scenario_id).select_related(
        "term_section"
    )
    used_sections: dict[str, set[str]] = defaultdict(set)  # course_code -> {section labels}
    board_usage: dict[str, set[int]] = defaultdict(set)  # course_code -> {board PKs}
    for p in placements:
        code = p.term_section.course_code
        sec = p.term_section.section
        used_sections[code].add(sec)
        board_usage[code].add(p.board_id)
    used_counts: Counter[str] = Counter({k: len(v) for k, v in used_sections.items()})

    result = []
    for b in budgets:
        used = used_counts.get(b.course_code, 0)
        result.append(
            {
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
            }
        )

    result.sort(key=lambda x: (x.get("programme_term") or 0, x["course_code"]))
    return result


# ── Cross-Board Conflict Detection ──────────────────────────────


def detect_cross_board_conflicts(scenario_id: int) -> list[dict]:
    """Find time-overlapping placements across *different* boards that
    impact shared students.

    Unlike :func:`detect_board_conflicts` (single-board), this checks
    every (board_a, board_b) pair.  A cross-board conflict is reported
    only when:

    1. A placement on board_a and a placement on board_b overlap in time
       (bitmask AND), **and**
    2. At least one student in ``ScenarioStudentMap`` needs courses from
       *both* placements (set intersection of recommended_courses).

    De-duplicated by sorted course-code pair so each course-pair appears
    at most once.  Results are sorted by descending ``overlap_count``
    (most-impacted pair first).

    Parameters:
        scenario_id: PK of the parent ``Scenario``.

    Returns:
        List of conflict dicts, each containing board labels, section
        labels, overlap count, and time description.  Empty list if
        fewer than two boards exist.
    """
    boards = list(DeliveryBoard.objects.filter(scenario_id=scenario_id).order_by("display_order"))
    if len(boards) < 2:
        return []

    # Load all placements per board
    board_placements: dict[int, list[PlacementInfo]] = {}
    for b in boards:
        board_placements[b.id] = _load_board_placements(b.id)

    # Build an inverted index: course_code -> set of student IDs that
    # need that course (from the scenario's pre-computed recommendations).
    student_maps = ScenarioStudentMap.objects.filter(scenario_id=scenario_id)
    course_students: dict[str, set[int]] = defaultdict(set)
    for sm in student_maps:
        for code in sm.recommended_courses:
            course_students[code].add(sm.student_id)

    conflicts: list[dict] = []
    # De-duplicate by sorted (course_a, course_b) so each course pair
    # is reported at most once regardless of how many section combos clash.
    seen: set[tuple] = set()

    for i, board_a in enumerate(boards):
        for board_b in boards[i + 1 :]:
            pa_list = board_placements.get(board_a.id, [])
            pb_list = board_placements.get(board_b.id, [])

            for pa in pa_list:
                for pb in pb_list:
                    # Bitmask AND: non-zero means the two slots overlap
                    if pa.mask & pb.mask:
                        # Students needing BOTH courses
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

                            conflicts.append(
                                {
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
                                }
                            )

    conflicts.sort(key=lambda x: -x["overlap_count"])
    return conflicts


# ── Publish Readiness ───────────────────────────────────────────


def check_publish_readiness(scenario_id: int) -> dict:
    """Determine whether a scenario can be published (finalised).

    Blockers (prevent publish):
        - No boards exist in the scenario.
        - A board has zero placements.
        - A board has any **critical** conflicts (time overlaps or
          instructor double-bookings).

    Warnings (advisory, do not block):
        - A board has room-clash warnings.

    Parameters:
        scenario_id: PK of the ``Scenario``.

    Returns:
        ``{"ready": bool, "blockers": [...], "warnings": [...]}``
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

        # Room clashes: block publish when rooms are actively assigned
        room_clashes = len(conflicts.get("room_clashes", []))
        has_rooms = (
            SectionPlacement.objects.filter(board=board)
            .exclude(room="")
            .exclude(room="UNASSIGNED")
            .exists()
        )

        if room_clashes > 0 and has_rooms:
            blockers.append(f"Board '{board.label}': {room_clashes} room conflicts")

        # Unassigned rooms when rooms are expected
        unassigned_rooms = SectionPlacement.objects.filter(board=board, room="UNASSIGNED").count()
        if unassigned_rooms > 0:
            blockers.append(f"Board '{board.label}': {unassigned_rooms} sections without rooms")

        if conflicts["summary"]["warning"] > 0 and not (room_clashes > 0 and has_rooms):
            warnings.append(f"Board '{board.label}': {conflicts['summary']['warning']} warnings")

    return {
        "ready": len(blockers) == 0,
        "blockers": blockers,
        "warnings": warnings,
    }

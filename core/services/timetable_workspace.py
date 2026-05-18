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
    Room,
    ScenarioSectionBudget,
    ScenarioStudentMap,
    SectionPlacement,
    Student,
    TermSection,
    TermSectionMeeting,
)
from core.services.student_helpers import normalize_code

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


def _same_course_code(code_a: object | None, code_b: object | None) -> bool:
    """Compare course codes using the same normalization as student maps."""
    return normalize_code(code_a) == normalize_code(code_b)


def _room_key(room: object | None) -> str:
    """Normalize a room code; return empty for no real room assignment."""
    value = str(room or "").strip().upper()
    return "" if value in {"", "UNASSIGNED"} else value


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
        course_key:       Internal planner identity.
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
    course_key: str
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
                course_key=p.term_section.course_key or p.term_section.course_code,
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

    from core.services.timetable_overlap import build_overlap_matrix

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
    # Severity levels match the engines:
    #   critical: same-course OR shared >= HARD_OVERLAP_THRESHOLD (blocks publish)
    #   warning:  shared 1..(HARD_OVERLAP_THRESHOLD-1) (flagged, doesn't block)
    from core.services.timetable_overlap import (
        HARD_OVERLAP_THRESHOLD,
        SAME_COURSE_SENTINEL,
    )
    from core.services.timetable_overlap import (
        shared_student_count as _ssc,
    )

    n = len(items)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = items[i], items[j]
            if not (a.mask & b.mask):
                continue
            pk = _pair_key(a.id, b.id)
            if pk in seen_pairs:
                continue
            same_course = _same_course_code(a.course_key, b.course_key)
            shared = (
                _ssc(overlap_matrix, a.course_key, b.course_key)
                if not same_course
                else SAME_COURSE_SENTINEL
            )
            if same_course or shared > 0:
                is_critical = same_course or shared >= HARD_OVERLAP_THRESHOLD
                seen_pairs.add(pk)
                overlaps.append(
                    {
                        "ids": [a.id, b.id],
                        "course_keys": [a.course_key, b.course_key],
                        "sections": [
                            f"{a.course_code}-{a.section}",
                            f"{b.course_code}-{b.section}",
                        ],
                        "detail": f"{a.day} {a.start_time}-{a.end_time} vs {b.day} {b.start_time}-{b.end_time}",
                        "severity": "critical" if is_critical else "warning",
                        "shared_students": shared if not same_course else None,
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

    # Critical overlaps (same-course or high shared) + instructor clashes block
    # publish.  Low-shared overlaps and room clashes are warnings.
    critical_overlaps = sum(1 for o in overlaps if o.get("severity") == "critical")
    warning_overlaps = sum(1 for o in overlaps if o.get("severity") == "warning")
    critical = critical_overlaps + len(instructor_clashes)
    warning = warning_overlaps + len(room_clashes)

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

    board_obj: DeliveryBoard | None = None
    try:
        board_obj = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
        board_courses = {item.course_key for item in items}
        if ts:
            board_courses.add(ts.course_key or ts.course_code)
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

        # Time overlap — graduated severity matching the placement engines
        if new_mask & item.mask:
            from core.services.timetable_overlap import (
                HARD_OVERLAP_THRESHOLD,
                SAME_COURSE_SENTINEL,
            )
            from core.services.timetable_overlap import (
                shared_student_count as _ssc_val,
            )

            ts_key = (ts.course_key or ts.course_code) if ts else ""
            same_course = bool(ts and _same_course_code(item.course_key, ts_key))
            shared = (
                _ssc_val(_overlap_mat, ts_key, item.course_key)
                if ts and not same_course
                else (SAME_COURSE_SENTINEL if same_course else 0)
            )

            if same_course or shared > 0:
                is_critical = same_course or shared >= HARD_OVERLAP_THRESHOLD
                overlaps.append(
                    {
                        "id": item.id,
                        "section": f"{item.course_code}-{item.section}",
                        "detail": f"{item.day} {item.start_time}-{item.end_time}",
                        "severity": "critical" if is_critical else "warning",
                        "shared_students": shared if not same_course else None,
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

            # Room clash on the same board (exclude blank/UNASSIGNED).
            room_key = _room_key(room)
            if room_key and room_key == _room_key(item.room):
                room_clashes.append(
                    {
                        "id": item.id,
                        "room": room,
                        "section": f"{item.course_code}-{item.section}",
                        "scope": "same_board",
                    }
                )

    room_key = _room_key(room)
    if board_obj and room_key:
        cross_board_placements = (
            SectionPlacement.objects.filter(board__scenario=board_obj.scenario)
            .exclude(board_id=board_id)
            .exclude(room="")
            .exclude(room="UNASSIGNED")
            .select_related("board", "term_section")
        )
        if exclude_placement_id:
            cross_board_placements = cross_board_placements.exclude(id=exclude_placement_id)

        for other in cross_board_placements:
            if room_key != _room_key(other.room):
                continue
            other_mask = _time_mask(other.day, other.start_time, other.end_time)
            if not (new_mask & other_mask):
                continue
            room_clashes.append(
                {
                    "id": other.id,
                    "room": room,
                    "section": f"{other.term_section.course_code}-{other.term_section.section}",
                    "scope": "cross_board",
                    "board_id": other.board_id,
                    "board_label": other.board.label,
                    "detail": (
                        f"{other.board.label}: {other.day} {other.start_time}-{other.end_time}"
                    ),
                }
            )

    critical_overlaps = sum(1 for o in overlaps if o.get("severity") == "critical")
    warning_overlaps = sum(1 for o in overlaps if o.get("severity") == "warning")
    critical = critical_overlaps + len(instructor_clashes)
    warning = warning_overlaps + len(room_clashes)

    return {
        "valid": critical == 0,
        "overlaps": overlaps,
        "instructor_clashes": instructor_clashes,
        "room_clashes": room_clashes,
        "critical_count": critical,
        "warning_count": warning,
    }


# ── Direct Student Feasibility ───────────────────────────────────


def preview_placement_slot_candidates(placement_id: int) -> dict:
    """Return student-aware candidate slots for moving a placement.

    The preview is read-only. It scores candidate slots against all placements
    on the same board and direct student pressure across the whole scenario, so
    visitor/cross-group students are included.  The response also includes the
    placement's current impact so the UI can show before/after improvement.
    """
    from core.services.timetable_autoplace import DEFAULT_LAB_SLOTS, DEFAULT_SLOTS
    from core.services.timetable_overlap import (
        HARD_OVERLAP_THRESHOLD,
        build_course_students_map,
    )

    placement = SectionPlacement.objects.select_related("board__scenario", "term_section").get(
        id=placement_id
    )
    scenario = placement.board.scenario
    course_key = placement.term_section.course_key or placement.term_section.course_code
    course_code = placement.term_section.course_code
    course_norm = normalize_code(course_key)
    room_norm = _room_key(placement.room)
    duration = _to_minutes(placement.end_time) - _to_minutes(placement.start_time)
    is_lab = duration > 80
    slot_config = scenario.lab_slot_config if is_lab else scenario.slot_config
    slots = slot_config or (DEFAULT_LAB_SLOTS if is_lab else DEFAULT_SLOTS)
    kind = "lab" if is_lab else "lect"

    board_items = _load_board_placements(placement.board_id)
    scenario_placements = list(
        SectionPlacement.objects.filter(board__scenario=scenario)
        .select_related("board", "term_section")
        .prefetch_related("term_section__meetings")
    )
    all_courses = {course_key}
    all_courses.update(item.course_key for item in board_items)
    all_courses.update(
        p.term_section.course_key or p.term_section.course_code for p in scenario_placements
    )
    course_students = build_course_students_map(scenario.id, all_courses)
    moving_students = course_students.get(course_norm, set())

    new_meeting = TermSectionMeeting.objects.filter(term_section=placement.term_section).first()
    new_instructor = (new_meeting.instructor if new_meeting else "").strip().upper()
    days = ["SUN", "MON", "TUE", "WED", "THU"]

    def shared_students(other_code: str) -> set[int]:
        if normalize_code(other_code) == course_norm:
            return set()
        return moving_students & course_students.get(normalize_code(other_code), set())

    def evidence(kind_: str, tone: str, title: str, detail: str, count: int = 0) -> dict:
        return {
            "kind": kind_,
            "tone": tone,
            "title": title,
            "detail": detail,
            "student_count": count,
        }

    def score_slot(day: str, start: str, end: str) -> dict:
        candidate_mask = _time_mask(day, start, end)
        affected_same_board: set[int] = set()
        affected_cross_board: set[int] = set()
        candidate_evidence: list[dict] = []
        overlap_critical = 0
        overlap_warning = 0
        instructor_critical = 0
        room_warning = 0
        cross_critical = 0
        cross_warning = 0

        for item in board_items:
            if item.id == placement.id or not (candidate_mask & item.mask):
                continue

            if _same_course_code(item.course_key, course_key):
                overlap_critical += 1
                candidate_evidence.append(
                    evidence(
                        "same_course",
                        "critical",
                        f"Same course section {item.course_code}-{item.section}",
                        f"{item.day} {item.start_time}-{item.end_time}",
                    )
                )
            else:
                shared = shared_students(item.course_key)
                if shared:
                    affected_same_board.update(shared)
                    is_hard = len(shared) >= HARD_OVERLAP_THRESHOLD
                    if is_hard:
                        overlap_critical += 1
                    else:
                        overlap_warning += 1
                    candidate_evidence.append(
                        evidence(
                            "students",
                            "critical" if is_hard else "warning",
                            f"{len(shared)} students also need {item.course_code}-{item.section}",
                            f"Same-board overlap at {item.day} {item.start_time}-{item.end_time}",
                            len(shared),
                        )
                    )

            if (
                new_instructor
                and item.instructor
                and new_instructor == item.instructor.strip().upper()
            ):
                instructor_critical += 1
                candidate_evidence.append(
                    evidence(
                        "instructor",
                        "critical",
                        f"Instructor clash: {item.instructor}",
                        f"{item.course_code}-{item.section} is at {item.day} {item.start_time}-{item.end_time}",
                    )
                )

            item_room = _room_key(item.room)
            if room_norm and room_norm == item_room:
                room_warning += 1
                candidate_evidence.append(
                    evidence(
                        "room",
                        "warning",
                        f"Room clash: {placement.room}",
                        f"{item.course_code}-{item.section} already uses this room",
                    )
                )

        for other in scenario_placements:
            if other.id == placement.id or other.board_id == placement.board_id:
                continue
            other_mask = _time_mask(other.day, other.start_time, other.end_time)
            if not (candidate_mask & other_mask):
                continue

            if room_norm and room_norm == _room_key(other.room):
                room_warning += 1
                candidate_evidence.append(
                    evidence(
                        "cross_board_room",
                        "warning",
                        f"Room occupied on {other.board.label}: {placement.room}",
                        f"{other.term_section.course_code}-{other.term_section.section} at {other.day} {other.start_time}-{other.end_time}",
                    )
                )

            shared = shared_students(
                other.term_section.course_key or other.term_section.course_code
            )
            if not shared:
                continue
            affected_cross_board.update(shared)
            is_hard = len(shared) >= HARD_OVERLAP_THRESHOLD
            if is_hard:
                cross_critical += 1
            else:
                cross_warning += 1
            candidate_evidence.append(
                evidence(
                    "cross_board_students",
                    "critical" if is_hard else "warning",
                    f"{len(shared)} students also need {other.term_section.course_code}-{other.term_section.section}",
                    f"{other.board.label} at {other.day} {other.start_time}-{other.end_time}",
                    len(shared),
                )
            )

        affected_students = affected_same_board | affected_cross_board
        critical = overlap_critical + instructor_critical + cross_critical
        warning = overlap_warning + room_warning + cross_warning
        impact_score = critical * 1000 + warning * 140 + len(affected_students) * 4
        tone = "avoid" if critical else ("risky" if warning or affected_students else "clean")

        return {
            "tone": tone,
            "critical_count": critical,
            "warning_count": warning,
            "same_board_student_count": len(affected_same_board),
            "cross_board_student_count": len(affected_cross_board),
            "student_affected_count": len(affected_students),
            "impact_score": impact_score,
            "evidence": candidate_evidence[:5],
        }

    current_impact = score_slot(placement.day, placement.start_time, placement.end_time)

    candidates: list[dict] = []
    for day in days:
        for slot in slots:
            start = str(slot.get("start", ""))
            end = str(slot.get("end", ""))
            if not start or not end:
                continue
            if day == placement.day and start == placement.start_time:
                continue

            impact = score_slot(day, start, end)
            score = (
                impact["impact_score"]
                + (0 if day == placement.day else 12)
                + round(abs(_to_minutes(start) - _to_minutes(placement.start_time)) / 10)
            )
            candidates.append(
                {
                    "kind": kind,
                    "day": day,
                    "start": start,
                    "end": end,
                    "score": score,
                    **impact,
                    "current_student_affected_count": current_impact["student_affected_count"],
                    "student_improvement": (
                        current_impact["student_affected_count"] - impact["student_affected_count"]
                    ),
                    "current_critical_count": current_impact["critical_count"],
                    "critical_improvement": (
                        current_impact["critical_count"] - impact["critical_count"]
                    ),
                    "current_warning_count": current_impact["warning_count"],
                    "warning_improvement": (
                        current_impact["warning_count"] - impact["warning_count"]
                    ),
                    "current_impact_score": current_impact["impact_score"],
                    "impact_improvement": current_impact["impact_score"] - impact["impact_score"],
                }
            )

    candidates.sort(
        key=lambda c: (
            c["score"],
            days.index(str(c["day"])),
            _to_minutes(str(c["start"])),
        )
    )
    for idx, candidate in enumerate(candidates, start=1):
        candidate["rank"] = idx
        if idx == 1:
            candidate["badge"] = "Best" if candidate["tone"] == "clean" else "Least bad"
        elif candidate["student_affected_count"]:
            candidate["badge"] = f"{candidate['student_affected_count']} students"
        elif candidate["tone"] == "clean":
            candidate["badge"] = "Clean"
        elif candidate["tone"] == "risky":
            candidate["badge"] = "Risky"
        else:
            candidate["badge"] = "Avoid"

    return {
        "placement": {
            "id": placement.id,
            "course_code": course_code,
            "section": placement.term_section.section,
            "board_id": placement.board_id,
            "board_label": placement.board.label,
            "day": placement.day,
            "start": placement.start_time,
            "end": placement.end_time,
        },
        "current_impact": current_impact,
        "student_source": "scenario_student_maps",
        "hard_student_threshold": HARD_OVERLAP_THRESHOLD,
        "candidates": candidates,
    }


def _time_overlap(
    day_a: str, start_a: str, end_a: str, day_b: str, start_b: str, end_b: str
) -> bool:
    if str(day_a).strip().upper() != str(day_b).strip().upper():
        return False
    return bool(_time_mask(day_a, start_a, end_a) & _time_mask(day_b, start_b, end_b))


def _section_gender(section: object | None) -> str:
    first = str(section or "").strip()[:1].upper()
    return first if first in {"M", "F"} else ""


def _room_type_for_slot(start_time: str, end_time: str) -> str:
    from core.services.timetable_lab_predicate import meeting_requires_lab_room

    try:
        duration = _to_minutes(end_time) - _to_minutes(start_time)
    except (TypeError, ValueError):
        duration = 0
    return "lab" if meeting_requires_lab_room(duration) else "lecture"


def _room_department_fits(room: Room, board: DeliveryBoard) -> bool:
    programmes = {p.strip().upper() for p in str(board.program or "").split(",") if p.strip()}
    if not programmes:
        return True
    room_departments = {
        p.strip().upper() for p in str(room.department or "").split(",") if p.strip()
    }
    return not room_departments or bool(programmes & room_departments)


def preview_placement_room_candidates(
    placement_id: int,
    *,
    day: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> dict:
    """Return room candidates for assigning a placement at a target slot."""
    from core.services.timetable_rooming import get_board_gender

    placement = SectionPlacement.objects.select_related("board__scenario", "term_section").get(
        id=placement_id
    )
    target_day = str(day or placement.day).strip().upper()
    target_start = str(start_time or placement.start_time).strip()
    target_end = str(end_time or placement.end_time).strip()
    term_section = placement.term_section
    board = placement.board
    required_capacity = int(
        term_section.registered_count or term_section.available_capacity or board.target_size or 0
    )
    required_type = _room_type_for_slot(target_start, target_end)
    required_gender = _section_gender(term_section.section) or get_board_gender(board.id)

    target_mask = _time_mask(target_day, target_start, target_end)
    occupied_by_room: dict[str, list[dict]] = defaultdict(list)
    occupiers = (
        SectionPlacement.objects.filter(board__scenario=board.scenario)
        .exclude(id=placement.id)
        .exclude(room="")
        .exclude(room="UNASSIGNED")
        .select_related("board", "term_section")
    )
    for other in occupiers:
        other_mask = _time_mask(other.day, other.start_time, other.end_time)
        if not (target_mask & other_mask):
            continue
        occupied_by_room[_room_key(other.room)].append(
            {
                "placement_id": other.id,
                "board_id": other.board_id,
                "board_label": other.board.label,
                "section": f"{other.term_section.course_code}-{other.term_section.section}",
                "day": other.day,
                "start": other.start_time,
                "end": other.end_time,
            }
        )

    candidates: list[dict] = []
    rooms = Room.objects.all().order_by("capacity", "room_code", "section")
    for room in rooms:
        room_code = str(room.room_code or "").strip()
        if not room_code:
            continue
        room_type = str(room.room_type or "lecture").strip().lower() or "lecture"
        room_gender = str(room.section or "").strip().upper()
        room_capacity = int(room.capacity or 0)
        occupied = occupied_by_room.get(_room_key(room_code), [])
        fits_type = room_type == required_type
        fits_gender = not required_gender or room_gender in {"", required_gender}
        fits_capacity = required_type == "lab" or room_capacity >= required_capacity
        department_fit = _room_department_fits(room, board)

        validation = validate_placement(
            board_id=board.id,
            day=target_day,
            start_time=target_start,
            end_time=target_end,
            room=room_code,
            term_section_id=term_section.id,
            exclude_placement_id=placement.id,
        )

        reasons = []
        if not fits_type:
            reasons.append(f"needs {required_type} room")
        if not fits_gender:
            reasons.append(f"needs {required_gender} room")
        if not fits_capacity:
            reasons.append(f"capacity {room_capacity} < {required_capacity}")
        if not department_fit:
            reasons.append("outside board programme pool")
        if occupied:
            reasons.append("room occupied at this time")
        if validation.get("critical_count", 0):
            reasons.append(f"{validation['critical_count']} time/instructor issue(s)")

        resource_available = fits_type and fits_gender and fits_capacity and not occupied
        slot_clean = resource_available and validation.get("critical_count", 0) == 0
        tone = (
            "clean"
            if slot_clean and validation.get("warning_count", 0) == 0
            else ("warn" if resource_available else "block")
        )
        capacity_slack = (
            room_capacity - required_capacity if required_type != "lab" else room_capacity
        )
        score = (
            (0 if resource_available else 100000)
            + (0 if validation.get("critical_count", 0) == 0 else 20000)
            + (0 if validation.get("warning_count", 0) == 0 else 1000)
            + (0 if department_fit else 800)
            + (0 if fits_type else 500)
            + (0 if fits_gender else 300)
            + max(0, capacity_slack)
        )
        candidates.append(
            {
                "room_code": room_code,
                "building": room.building,
                "wing": room.wing,
                "floor": room.floor,
                "room_type": room_type,
                "capacity": room_capacity,
                "section": room_gender,
                "available": resource_available,
                "slot_clean": slot_clean,
                "tone": tone,
                "score": score,
                "capacity_slack": capacity_slack,
                "fits_type": fits_type,
                "fits_gender": fits_gender,
                "fits_capacity": fits_capacity,
                "department_fit": department_fit,
                "occupied_by": occupied,
                "reasons": reasons,
                "validation": {
                    "critical_count": validation.get("critical_count", 0),
                    "warning_count": validation.get("warning_count", 0),
                    "room_clashes": validation.get("room_clashes", []),
                },
            }
        )

    candidates.sort(
        key=lambda c: (
            c["score"],
            abs(c["capacity_slack"]) if c["available"] else 99999,
            c["room_code"],
            c["section"],
        )
    )
    return {
        "placement": {
            "id": placement.id,
            "course_code": term_section.course_code,
            "section": term_section.section,
            "board_id": board.id,
            "board_label": board.label,
            "current_room": placement.room,
        },
        "target": {
            "day": target_day,
            "start": target_start,
            "end": target_end,
            "required_capacity": required_capacity,
            "required_type": required_type,
            "required_gender": required_gender,
        },
        "candidates": candidates[:60],
        "summary": {
            "total": len(candidates),
            "available": sum(1 for c in candidates if c["available"]),
            "clean": sum(1 for c in candidates if c["slot_clean"]),
            "blocked": sum(1 for c in candidates if not c["available"]),
        },
    }


def _student_rows(student_ids: set[int], primary_terms: dict[int, int], limit: int) -> list[dict]:
    wanted = sorted(student_ids)[:limit]
    students = {
        row["student_id"]: row
        for row in Student.objects.filter(student_id__in=wanted).values(
            "student_id",
            "registration_no",
            "name",
            "program",
            "section",
            "total_earned_credits",
            "current_registered_credits",
        )
    }
    return [
        {
            "student_id": sid,
            "registration_no": students.get(sid, {}).get("registration_no", ""),
            "name": students.get(sid, {}).get("name", ""),
            "program": students.get(sid, {}).get("program", ""),
            "section": students.get(sid, {}).get("section", ""),
            "primary_term": primary_terms.get(sid),
            "total_earned_credits": students.get(sid, {}).get("total_earned_credits"),
            "current_registered_credits": students.get(sid, {}).get("current_registered_credits"),
        }
        for sid in wanted
    ]


def preview_placement_student_evidence(placement_id: int, *, limit: int = 40) -> dict:
    """Return exact student evidence for overlaps involving one placement."""
    from core.services.timetable_overlap import build_course_students_map

    placement = SectionPlacement.objects.select_related("board__scenario", "term_section").get(
        id=placement_id
    )
    scenario = placement.board.scenario
    course_key = placement.term_section.course_key or placement.term_section.course_code
    course_code = placement.term_section.course_code
    course_norm = normalize_code(course_key)
    scenario_placements = list(
        SectionPlacement.objects.filter(board__scenario=scenario)
        .exclude(id=placement.id)
        .select_related("board", "term_section")
    )
    all_courses = {course_key}
    all_courses.update(
        p.term_section.course_key or p.term_section.course_code for p in scenario_placements
    )
    course_students = build_course_students_map(scenario.id, all_courses)
    moving_students = course_students.get(course_norm, set())
    primary_terms = {
        sm.student_id: sm.primary_term
        for sm in ScenarioStudentMap.objects.filter(scenario=scenario)
    }

    conflicts: list[dict] = []
    affected_total: set[int] = set()
    for other in scenario_placements:
        if not _time_overlap(
            placement.day,
            placement.start_time,
            placement.end_time,
            other.day,
            other.start_time,
            other.end_time,
        ):
            continue
        other_norm = normalize_code(other.term_section.course_key or other.term_section.course_code)
        if other_norm == course_norm:
            shared: set[int] = set()
            kind = "same_course"
        else:
            shared = moving_students & course_students.get(other_norm, set())
            kind = "students"
        if kind == "students" and not shared:
            continue
        affected_total.update(shared)
        conflicts.append(
            {
                "kind": kind,
                "scope": "same_board" if other.board_id == placement.board_id else "cross_board",
                "board_id": other.board_id,
                "board_label": other.board.label,
                "placement_id": other.id,
                "course_code": other.term_section.course_code,
                "section": other.term_section.section,
                "time": f"{other.day} {other.start_time}-{other.end_time}",
                "affected_count": len(shared),
                "students": _student_rows(shared, primary_terms, min(limit, 25)),
            }
        )

    conflicts.sort(
        key=lambda c: (
            0 if c["kind"] == "students" else 1,
            -int(c["affected_count"]),
            c["board_label"],
            c["course_code"],
        )
    )
    return {
        "placement": {
            "id": placement.id,
            "course_code": course_code,
            "section": placement.term_section.section,
            "board_id": placement.board_id,
            "board_label": placement.board.label,
            "time": f"{placement.day} {placement.start_time}-{placement.end_time}",
        },
        "source": "scenario_student_maps",
        "affected_student_count": len(affected_total),
        "students": _student_rows(affected_total, primary_terms, limit),
        "conflicts": conflicts,
    }


def build_scenario_builder_actions(scenario_id: int, *, limit: int = 18) -> dict:
    """Rank the next practical actions a timetable builder should take."""
    readiness = check_publish_readiness(scenario_id)
    actions: list[dict] = []

    def add(
        kind: str,
        severity: str,
        title: str,
        detail: str,
        *,
        score: int,
        board_id: int | None = None,
        board_label: str = "",
        placement_ids: list[int] | None = None,
        course_code: str = "",
        cta: str = "",
    ) -> None:
        actions.append(
            {
                "kind": kind,
                "severity": severity,
                "title": title,
                "detail": detail,
                "score": score,
                "board_id": board_id,
                "board_label": board_label,
                "placement_ids": placement_ids or [],
                "course_code": course_code,
                "cta": cta,
            }
        )

    for blocker in readiness.get("blockers", []):
        add(
            "readiness_blocker",
            "block",
            "Publish blocker",
            str(blocker),
            score=100000,
            cta="Resolve before publishing",
        )

    boards = DeliveryBoard.objects.filter(scenario_id=scenario_id).order_by("display_order", "id")
    for board in boards:
        conflicts = detect_board_conflicts(board.id)
        for overlap in conflicts.get("overlaps", []):
            ids = [int(x) for x in overlap.get("ids", []) if x]
            add(
                "student_time_clash",
                "block" if overlap.get("severity") == "critical" else "warn",
                f"Fix time clash on {board.label}",
                " / ".join(overlap.get("sections", [])) or overlap.get("detail", ""),
                score=90000 if overlap.get("severity") == "critical" else 42000,
                board_id=board.id,
                board_label=board.label,
                placement_ids=ids,
                cta="Preview safe slots",
            )
        for clash in conflicts.get("instructor_clashes", []):
            ids = [int(x) for x in clash.get("ids", []) if x]
            add(
                "instructor_clash",
                "block",
                f"Free instructor on {board.label}",
                f"{clash.get('instructor', '')}: {' / '.join(clash.get('sections', []))}",
                score=88000,
                board_id=board.id,
                board_label=board.label,
                placement_ids=ids,
                cta="Move one section",
            )
        for clash in conflicts.get("room_clashes", []):
            ids = [int(x) for x in clash.get("ids", []) if x]
            add(
                "room_clash",
                "block" if clash.get("scope") == "cross_board" else "warn",
                f"Assign another room on {board.label}",
                f"{clash.get('room', '')}: {' / '.join(clash.get('sections', []))}",
                score=76000,
                board_id=board.id,
                board_label=board.label,
                placement_ids=ids,
                cta="Open room mode",
            )
        unroomed = list(
            SectionPlacement.objects.filter(board=board, room="UNASSIGNED")
            .select_related("term_section")
            .order_by("day", "start_time")[:5]
        )
        for placement in unroomed:
            add(
                "unassigned_room",
                "block",
                f"Choose room for {placement.term_section.course_code}-{placement.term_section.section}",
                f"{board.label} {placement.day} {placement.start_time}-{placement.end_time}",
                score=73000,
                board_id=board.id,
                board_label=board.label,
                placement_ids=[placement.id],
                course_code=placement.term_section.course_code,
                cta="Open room mode",
            )

    for item in compute_scenario_budget(scenario_id):
        remaining = int(item.get("remaining_sections", 0) or 0)
        if remaining <= 0:
            continue
        add(
            "missing_section",
            "warn",
            f"Place {remaining} more section(s) of {item['course_code']}",
            f"Demand {item.get('total_demand', 0)}; planned {item.get('planned_sections', 0)}; used {item.get('used_sections', 0)}",
            score=52000 + remaining * 1000,
            course_code=str(item["course_code"]),
            cta="Drag from required sections",
        )

    actions.sort(
        key=lambda a: (
            -int(a["score"]),
            a["title"],
            a.get("board_label") or "",
        )
    )
    return {
        "readiness": readiness,
        "actions": actions[:limit],
        "summary": {
            "total": len(actions),
            "blockers": sum(1 for a in actions if a["severity"] == "block"),
            "warnings": sum(1 for a in actions if a["severity"] == "warn"),
        },
    }


def compute_affected_students(board_id: int) -> dict:
    """Assess student impact of time-overlapping placements on a board.

    For every pair of overlapping placements (detected by
    :func:`detect_board_conflicts`):

    1. Extract the two course codes from the ``"CS101-A"`` labels.
    2. Use ``ScenarioStudentMap.recommended_courses`` to find students
       who need *both* courses -- these are the "affected" students.
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
        keys = overlap.get("course_keys") or []
        if keys:
            overlap_courses.update(str(k) for k in keys if k)
        else:
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
        course_keys = overlap.get("course_keys") or []
        ids = overlap.get("ids", [])
        if len(sections) < 2:
            continue

        # Extract course codes from "CS101-A" format
        code_a = (
            str(course_keys[0])
            if len(course_keys) >= 2
            else sections[0].rsplit("-", 1)[0]
            if "-" in sections[0]
            else sections[0]
        )
        code_b = (
            str(course_keys[1])
            if len(course_keys) >= 2
            else sections[1].rsplit("-", 1)[0]
            if "-" in sections[1]
            else sections[1]
        )

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
        # Scoped to this scenario's boards (not all TermSections globally)
        scenario_boards = DeliveryBoard.objects.filter(scenario=board.scenario)
        scenario_ts_ids = set(
            SectionPlacement.objects.filter(board__in=scenario_boards).values_list(
                "term_section_id", flat=True
            )
        )
        alt_a_sections = TermSection.objects.filter(
            course_code=code_a, id__in=scenario_ts_ids
        ).exclude(id__in=[p.term_section_id for p in all_placements if p.course_code == code_a])
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
        alt_b_sections = TermSection.objects.filter(
            course_code=code_b, id__in=scenario_ts_ids
        ).exclude(id__in=[p.term_section_id for p in all_placements if p.course_code == code_b])
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

    # Count DISTINCT sections per planner course key -- keyed by course_key, valued
    # by the *set* of section labels (e.g. {"A", "B"}).  This avoids
    # double-counting multi-placement sections (lecture + lab rows).
    placements = SectionPlacement.objects.filter(board__scenario_id=scenario_id).select_related(
        "term_section"
    )
    used_sections: dict[str, set[str]] = defaultdict(set)  # course_key -> {section labels}
    board_usage: dict[str, set[int]] = defaultdict(set)  # course_key -> {board PKs}
    for p in placements:
        code = p.term_section.course_key or p.term_section.course_code
        sec = p.term_section.section
        used_sections[code].add(sec)
        board_usage[code].add(p.board_id)
    used_counts: Counter[str] = Counter({k: len(v) for k, v in used_sections.items()})

    result = []
    for b in budgets:
        key = b.course_key or b.course_code
        used = used_counts.get(key, 0)
        result.append(
            {
                "course_key": key,
                "course_code": b.course_code,
                "course_name": b.course_name,
                "department": b.department,
                "credit_hours": b.credit_hours,
                "programme_term": b.programme_term,
                "planned_sections": b.planned_sections,
                "max_per_section": b.max_per_section,
                "total_demand": b.total_demand,
                "used_sections": used,
                "remaining_sections": max(0, b.planned_sections - used),
                "boards_using": sorted(board_usage.get(key, set())),
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

    De-duplicated by sorted placement IDs so each exact section-pair clash
    appears at most once.  Results are sorted by descending ``overlap_count``
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

    # Build an inverted index: planner course key -> set of student IDs that
    # need that course (from the scenario's pre-computed recommendations).
    student_maps = list(ScenarioStudentMap.objects.filter(scenario_id=scenario_id))
    course_students: dict[str, set[int]] = defaultdict(set)
    for sm in student_maps:
        for code in sm.recommended_course_keys or sm.recommended_courses:
            course_students[code].add(sm.student_id)
    primary_terms = {sm.student_id: sm.primary_term for sm in student_maps}
    student_row_cache: dict[int, dict] = {}

    def affected_student_rows(student_ids: set[int], limit: int = 12) -> list[dict]:
        wanted = sorted(student_ids)[:limit]
        missing = [sid for sid in wanted if sid not in student_row_cache]
        if missing:
            rows = Student.objects.filter(student_id__in=missing).values(
                "student_id",
                "registration_no",
                "name",
                "program",
                "section",
                "total_earned_credits",
                "current_registered_credits",
            )
            for row in rows:
                student_row_cache[int(row["student_id"])] = row
        return [
            {
                "student_id": sid,
                "registration_no": student_row_cache.get(sid, {}).get("registration_no", ""),
                "name": student_row_cache.get(sid, {}).get("name", ""),
                "program": student_row_cache.get(sid, {}).get("program", ""),
                "section": student_row_cache.get(sid, {}).get("section", ""),
                "primary_term": primary_terms.get(sid),
                "total_earned_credits": student_row_cache.get(sid, {}).get("total_earned_credits"),
                "current_registered_credits": student_row_cache.get(sid, {}).get(
                    "current_registered_credits"
                ),
            }
            for sid in wanted
        ]

    conflicts: list[dict] = []
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
                        shared = course_students.get(pa.course_key, set()) & course_students.get(
                            pb.course_key, set()
                        )
                        if shared:
                            pair_key = (min(pa.id, pb.id), max(pa.id, pb.id))
                            if pair_key in seen:
                                continue
                            seen.add(pair_key)

                            conflicts.append(
                                {
                                    "placement_a_id": pa.id,
                                    "placement_b_id": pb.id,
                                    "ids": [pa.id, pb.id],
                                    "course_keys": [pa.course_key, pb.course_key],
                                    "course_a": pa.course_code,
                                    "section_a": f"{pa.course_code}-{pa.section}",
                                    "board_a_id": board_a.id,
                                    "board_a_label": board_a.label,
                                    "time_a": f"{pa.day} {pa.start_time}-{pa.end_time}",
                                    "course_b": pb.course_code,
                                    "section_b": f"{pb.course_code}-{pb.section}",
                                    "board_b_id": board_b.id,
                                    "board_b_label": board_b.label,
                                    "time_b": f"{pb.day} {pb.start_time}-{pb.end_time}",
                                    "overlap_count": len(shared),
                                    "affected_student_ids": sorted(shared)[:40],
                                    "affected_students": affected_student_rows(shared),
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

    # Cross-board conflicts: high-overlap pairs are warnings
    from core.services.timetable_overlap import HARD_OVERLAP_THRESHOLD

    cross_conflicts = detect_cross_board_conflicts(scenario_id)
    high_overlap_cross = [
        c for c in cross_conflicts if c["overlap_count"] >= HARD_OVERLAP_THRESHOLD
    ]
    if high_overlap_cross:
        warnings.append(
            f"{len(high_overlap_cross)} cross-board conflicts with {HARD_OVERLAP_THRESHOLD}+ shared students"
        )

    return {
        "ready": len(blockers) == 0,
        "blockers": blockers,
        "warnings": warnings,
    }

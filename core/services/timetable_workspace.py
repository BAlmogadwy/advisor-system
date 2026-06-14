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
    - ``ScenarioStudentCourseRequest`` -- canonical per-student course demand
    - ``BoardStudentLink``     -- many-to-many link of students to boards
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from django.db import transaction

from core.models import (
    BoardStudentLink,
    DeliveryBoard,
    Room,
    ScenarioSectionBudget,
    SectionPlacement,
    Student,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.student_helpers import normalize_code
from core.services.timetable_demand import (
    compute_course_students_index,
    load_scenario_course_demands,
)
from core.services.timetable_online import (
    OnlineCourseLookup,
    exclude_online_courses_for_board,
    normalise_course_code,
)
from core.services.timetable_quality import (
    DAY_LABEL_BY_INDEX,
    TIMETABLE_QUALITY_POLICY,
    day_balance_penalty,
    meeting_weak_slot_penalty,
    quality_reason_rows,
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


def _slot_ranking_reasons(
    *,
    evidence_rows: list[dict],
    quality: dict,
    affected_students: int,
) -> list[dict]:
    """Build small, user-facing diagnostics explaining candidate ranking."""

    reasons: list[dict] = []
    risk_order = {"critical": 0, "warning": 1, "note": 2}
    for row in sorted(
        evidence_rows,
        key=lambda item: (
            risk_order.get(str(item.get("tone") or "").lower(), 3),
            -int(item.get("student_count") or 0),
            str(item.get("kind") or ""),
        ),
    )[:4]:
        reasons.append(
            {
                "kind": str(row.get("kind") or "evidence"),
                "tone": str(row.get("tone") or "note"),
                "title": str(row.get("title") or "Placement evidence"),
                "detail": str(row.get("detail") or ""),
                "student_count": int(row.get("student_count") or 0),
            }
        )

    if affected_students and not any(
        reason["kind"] in {"students", "cross_board_students"} for reason in reasons
    ):
        reasons.append(
            {
                "kind": "student_pressure",
                "tone": "warning",
                "title": f"{affected_students} affected student(s)",
                "detail": "Students share another course at this time.",
                "student_count": int(affected_students),
            }
        )

    quality_reasons = list((quality or {}).get("reasons") or [])
    if quality_reasons:
        top_quality = quality_reasons[0]
        reasons.append(
            {
                "kind": "quality",
                "tone": "note",
                "title": str(top_quality.get("label") or "Timetable quality pressure"),
                "detail": f"Soft quality penalty +{int(top_quality.get('penalty') or 0)}.",
                "component": str(top_quality.get("component") or ""),
                "penalty": int(top_quality.get("penalty") or 0),
            }
        )

    if not reasons:
        reasons.append(
            {
                "kind": "clean",
                "tone": "good",
                "title": "Clean target",
                "detail": "No direct conflict, student pressure, or quality penalty detected.",
            }
        )
    return reasons


def _slot_primary_reason(reasons: list[dict]) -> str:
    if not reasons:
        return ""
    first = reasons[0]
    title = str(first.get("title") or "").strip()
    detail = str(first.get("detail") or "").strip()
    if detail and title:
        return f"{title}: {detail}"
    return title or detail


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
        is_online:        True when the course is online for this board.
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
    is_online: bool
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
    online_codes: set[str] = set()
    try:
        board = DeliveryBoard.objects.get(id=board_id)
        online_codes = OnlineCourseLookup().codes_for_board(board)
    except DeliveryBoard.DoesNotExist:
        pass

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
                is_online=normalise_course_code(p.term_section.course_code) in online_codes,
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

    # Instructor identity for the clash grouping: structured links (per-person,
    # multi-instructor) when TIMETABLE_INSTRUCTOR_LINKS_ENABLED is on and the
    # section has links, else the free-text name — kept in lock-step with the
    # planner's clash so the UI badge matches what the solver enforces.
    from core.services.timetable_pr4_instructor import (
        build_section_instructor_ids,
        is_instructor_links_enabled,
    )

    link_map: dict[str, set] = {}
    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
        board_courses = {item.course_code for item in items}
        overlap_matrix = build_overlap_matrix(board.scenario_id, board_courses)
        if is_instructor_links_enabled():
            link_map = build_section_instructor_ids(board.scenario)
    except DeliveryBoard.DoesNotExist:
        overlap_matrix = {}

    by_instructor: dict[object, list] = defaultdict(list)
    by_room: dict[str, list] = defaultdict(list)
    for item in items:
        ids = link_map.get(f"{item.course_key}|{item.section}")
        if ids:
            for iid in ids:
                by_instructor[iid].append(item)
        elif item.instructor:
            by_instructor[item.instructor.strip().upper()].append(item)
        if not item.is_online and item.room and item.room.strip().upper() != "UNASSIGNED":
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
    ignore_overlap_term_section_ids: set[int] | None = None,
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
    ignored_overlap_term_sections = {
        int(value) for value in (ignore_overlap_term_section_ids or set())
    }

    # Get instructor for the section being placed
    meeting = TermSectionMeeting.objects.filter(term_section_id=term_section_id).first()
    new_instructor = meeting.instructor if meeting else ""

    ts = TermSection.objects.filter(id=term_section_id).first()
    online_lookup = OnlineCourseLookup()
    new_is_online = False

    # Build real overlap matrix for this board

    board_obj: DeliveryBoard | None = None
    try:
        board_obj = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
        if ts:
            new_is_online = online_lookup.is_online_course_for_board(board_obj, ts.course_code)
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
            ignore_student_overlap = item.term_section_id in ignored_overlap_term_sections
            if not ignore_student_overlap:
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
            room_key = "" if new_is_online else _room_key(room)
            if room_key and not item.is_online and room_key == _room_key(item.room):
                room_clashes.append(
                    {
                        "id": item.id,
                        "room": room,
                        "section": f"{item.course_code}-{item.section}",
                        "scope": "same_board",
                    }
                )

    room_key = "" if new_is_online else _room_key(room)
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
            if online_lookup.is_online_course_for_board(
                other.board, other.term_section.course_code
            ):
                continue
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
    online_lookup = OnlineCourseLookup()
    placement_is_online = online_lookup.is_online_course_for_board(
        placement.board, placement.term_section.course_code
    )
    room_norm = "" if placement_is_online else _room_key(placement.room)
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
    base_day_counts = Counter(
        _DAY_INDEX.get(str(other.day).upper(), 99)
        for other in scenario_placements
        if other.id != placement.id and _DAY_INDEX.get(str(other.day).upper(), 99) < len(days)
    )

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

    def slot_quality(day: str, start: str, end: str) -> dict:
        day_index = _DAY_INDEX.get(str(day).upper(), 99)
        counts = Counter(base_day_counts)
        if day_index < len(days):
            counts[day_index] += 1
        components = {
            "weak_slot": meeting_weak_slot_penalty(
                day_index,
                _to_minutes(start),
                _to_minutes(end),
            ),
            "day_balance": day_balance_penalty(counts),
            "spare_capacity": 0,
            "section_balance": 0,
            "room_change": 0,
            "student_day_overload": 0,
        }
        return {
            "policy": TIMETABLE_QUALITY_POLICY,
            "penalty": sum(int(value or 0) for value in components.values()),
            "components": components,
            "day_load": {
                DAY_LABEL_BY_INDEX[index]: int(counts.get(index, 0)) for index in range(len(days))
            },
            "reasons": quality_reason_rows(components),
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
            if room_norm and not item.is_online and room_norm == item_room:
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

            if (
                room_norm
                and not online_lookup.is_online_course_for_board(
                    other.board, other.term_section.course_code
                )
                and room_norm == _room_key(other.room)
            ):
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
        quality = slot_quality(day, start, end)
        tone = "avoid" if critical else ("risky" if warning or affected_students else "clean")
        ranking_reasons = _slot_ranking_reasons(
            evidence_rows=candidate_evidence,
            quality=quality,
            affected_students=len(affected_students),
        )

        return {
            "tone": tone,
            "critical_count": critical,
            "warning_count": warning,
            "same_board_student_count": len(affected_same_board),
            "cross_board_student_count": len(affected_cross_board),
            "student_affected_count": len(affected_students),
            "impact_score": impact_score,
            "quality_score": int(quality["penalty"]),
            "timetable_quality": quality,
            "evidence": candidate_evidence[:5],
            "ranking_reasons": ranking_reasons,
            "primary_reason": _slot_primary_reason(ranking_reasons),
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
                + impact["quality_score"]
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
        "student_source": "scenario_course_requests",
        "hard_student_threshold": HARD_OVERLAP_THRESHOLD,
        "candidates": candidates,
    }


def preview_planned_section_slot_candidates(
    board_id: int,
    *,
    course_code: str,
    course_key: str | None = None,
    section_label: str | None = None,
    credit_hours: int | None = None,
    max_per_section: int | None = None,
    kind: str | None = None,
    limit: int = 80,
) -> dict:
    """Return ranked target slots for creating a planned missing section.

    This is the read-only companion to ``tw_placement_create_planned_view``.
    It lets the UI guide the advisor before any row is created, using the
    same student-overlap and timetable-quality signals as placement moves.
    """
    from core.services.timetable_autoplace import (
        DEFAULT_LAB_SLOTS,
        DEFAULT_SLOTS,
        generate_meeting_options,
        get_meeting_pattern,
    )
    from core.services.timetable_overlap import (
        HARD_OVERLAP_THRESHOLD,
        build_course_students_map,
    )

    board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    scenario = board.scenario
    code = str(course_code or "").strip().upper()
    key = str(course_key or code).strip().upper()
    section = str(section_label or "S1").strip().upper()
    if not code:
        raise ValueError("course_code is required")

    budget = (
        ScenarioSectionBudget.objects.filter(scenario=scenario, course_key=key).first()
        or ScenarioSectionBudget.objects.filter(scenario=scenario, course_code=code).first()
    )
    effective_credit_hours = int(
        credit_hours if credit_hours is not None else getattr(budget, "credit_hours", 0) or 0
    )
    required_meetings = infer_required_meeting_count(
        scenario.id,
        key,
        fallback_credit_hours=effective_credit_hours,
    )
    requested_kind = normalise_course_code(kind or "")
    include_lab = requested_kind in {"LAB", "LABS"}
    include_lecture = requested_kind not in {"LAB", "LABS"}
    slot_sets: list[tuple[str, list[dict]]] = []
    if include_lecture:
        slot_sets.append(("lect", scenario.slot_config or DEFAULT_SLOTS))
    if include_lab or not requested_kind:
        slot_sets.append(("lab", scenario.lab_slot_config or DEFAULT_LAB_SLOTS))

    existing_section = (
        TermSection.objects.filter(scenario=scenario, course_key=key, section=section)
        .prefetch_related("meetings")
        .first()
    )
    existing_meetings = list(existing_section.meetings.all()) if existing_section else []
    new_instructor = (existing_meetings[0].instructor if existing_meetings else "").strip().upper()

    board_items = _load_board_placements(board.id)
    scenario_placements = list(
        SectionPlacement.objects.filter(board__scenario=scenario)
        .select_related("board", "term_section")
        .prefetch_related("term_section__meetings")
    )
    all_courses = {key}
    all_courses.update(item.course_key for item in board_items)
    all_courses.update(
        p.term_section.course_key or p.term_section.course_code for p in scenario_placements
    )
    course_students = build_course_students_map(scenario.id, all_courses)
    planned_students = course_students.get(normalize_code(key), set())
    days = ["SUN", "MON", "TUE", "WED", "THU"]
    base_day_counts = Counter(
        _DAY_INDEX.get(str(other.day).upper(), 99)
        for other in scenario_placements
        if _DAY_INDEX.get(str(other.day).upper(), 99) < len(days)
    )

    def shared_students(other_code: str) -> set[int]:
        if normalize_code(other_code) == normalize_code(key):
            return set()
        return planned_students & course_students.get(normalize_code(other_code), set())

    def evidence(kind_: str, tone: str, title: str, detail: str, count: int = 0) -> dict:
        return {
            "kind": kind_,
            "tone": tone,
            "title": title,
            "detail": detail,
            "student_count": count,
        }

    def slot_quality(day: str, start: str, end: str) -> dict:
        day_index = _DAY_INDEX.get(str(day).upper(), 99)
        counts = Counter(base_day_counts)
        if day_index < len(days):
            counts[day_index] += 1
        components = {
            "weak_slot": meeting_weak_slot_penalty(
                day_index,
                _to_minutes(start),
                _to_minutes(end),
            ),
            "day_balance": day_balance_penalty(counts),
            "spare_capacity": 0,
            "section_balance": 0,
            "room_change": 0,
            "student_day_overload": 0,
        }
        return {
            "policy": TIMETABLE_QUALITY_POLICY,
            "penalty": sum(int(value or 0) for value in components.values()),
            "components": components,
            "day_load": {
                DAY_LABEL_BY_INDEX[index]: int(counts.get(index, 0)) for index in range(len(days))
            },
            "reasons": quality_reason_rows(components),
        }

    def score_slot(slot_kind: str, day: str, start: str, end: str) -> dict:
        candidate_mask = _time_mask(day, start, end)
        affected_same_board: set[int] = set()
        affected_cross_board: set[int] = set()
        candidate_evidence: list[dict] = []
        overlap_critical = 0
        overlap_warning = 0
        instructor_critical = 0
        cross_critical = 0
        cross_warning = 0

        for item in board_items:
            if not (candidate_mask & item.mask):
                continue
            if _same_course_code(item.course_key, key):
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

        for other in scenario_placements:
            if other.board_id == board.id:
                continue
            other_mask = _time_mask(other.day, other.start_time, other.end_time)
            if not (candidate_mask & other_mask):
                continue
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
        warning = overlap_warning + cross_warning
        impact_score = critical * 1000 + warning * 140 + len(affected_students) * 4
        quality = slot_quality(day, start, end)
        kind_penalty = 0 if slot_kind == "lect" else 35
        score = impact_score + int(quality["penalty"]) + kind_penalty
        tone = "avoid" if critical else ("risky" if warning or affected_students else "clean")
        ranking_reasons = _slot_ranking_reasons(
            evidence_rows=candidate_evidence,
            quality=quality,
            affected_students=len(affected_students),
        )
        return {
            "kind": slot_kind,
            "day": day,
            "start": start,
            "end": end,
            "score": score,
            "tone": tone,
            "critical_count": critical,
            "warning_count": warning,
            "same_board_student_count": len(affected_same_board),
            "cross_board_student_count": len(affected_cross_board),
            "student_affected_count": len(affected_students),
            "impact_score": impact_score,
            "quality_score": int(quality["penalty"]),
            "timetable_quality": quality,
            "evidence": candidate_evidence[:6],
            "ranking_reasons": ranking_reasons,
            "primary_reason": _slot_primary_reason(ranking_reasons),
        }

    def meeting_kind(start: str, end: str) -> str:
        return "lab" if (_to_minutes(end) - _to_minutes(start)) > 75 else "lect"

    def meeting_pattern_for_request() -> list[int]:
        if effective_credit_hours:
            pattern = list(get_meeting_pattern(effective_credit_hours))
        elif required_meetings >= 3:
            pattern = [75, 75, 100]
        else:
            pattern = [75] * max(1, required_meetings)
        if len(pattern) < required_meetings:
            pattern.extend([75] * (required_meetings - len(pattern)))
        return pattern[:required_meetings]

    def spacing_penalty(option: list[dict]) -> int:
        indexes = sorted(_DAY_INDEX.get(str(row.get("day", "")).upper(), 99) for row in option)
        penalty = 0
        for first, second in zip(indexes, indexes[1:], strict=False):
            if second - first == 1:
                penalty += 25
        return penalty

    def score_pattern(option: list[dict], index: int) -> dict:
        meetings: list[dict] = []
        slot_scores: list[dict] = []
        for row in option:
            day = str(row.get("day", "")).upper()
            start = str(row.get("start", ""))
            end = str(row.get("end", ""))
            kind_ = meeting_kind(start, end)
            slot_score = score_slot(kind_, day, start, end)
            slot_scores.append(slot_score)
            meetings.append(
                {
                    "kind": kind_,
                    "day": day,
                    "start": start,
                    "end": end,
                    "slot_idx": int(row.get("slot_idx") or 0),
                }
            )

        critical = sum(int(row.get("critical_count") or 0) for row in slot_scores)
        warning = sum(int(row.get("warning_count") or 0) for row in slot_scores)
        affected = sum(int(row.get("student_affected_count") or 0) for row in slot_scores)
        slot_index_penalty = 25 * max(
            0,
            len({meeting["slot_idx"] for meeting in meetings}) - 1,
        )
        day_spacing_penalty = spacing_penalty(option)
        quality_score = sum(int(row.get("quality_score") or 0) for row in slot_scores)
        impact_score = sum(int(row.get("impact_score") or 0) for row in slot_scores)
        pattern_penalty = slot_index_penalty + day_spacing_penalty
        score = sum(int(row.get("score") or 0) for row in slot_scores) + pattern_penalty
        tone = "avoid" if critical else ("risky" if warning or affected else "clean")

        evidence_rows = [
            evidence
            for slot_score in slot_scores
            for evidence in list(slot_score.get("evidence") or [])[:2]
        ][:6]
        ranking_reasons = [
            reason
            for slot_score in slot_scores
            for reason in list(slot_score.get("ranking_reasons") or [])[:2]
        ][:5]
        if day_spacing_penalty:
            ranking_reasons.append(
                {
                    "kind": "pattern_spacing",
                    "tone": "note",
                    "title": "Consecutive-day pattern",
                    "detail": "The complete section uses adjacent teaching days.",
                    "penalty": day_spacing_penalty,
                }
            )
        if slot_index_penalty:
            ranking_reasons.append(
                {
                    "kind": "pattern_consistency",
                    "tone": "note",
                    "title": "Mixed slot pattern",
                    "detail": "The complete section cannot keep the same time index for all meetings.",
                    "penalty": slot_index_penalty,
                }
            )
        if not ranking_reasons:
            ranking_reasons = [
                {
                    "kind": "clean",
                    "tone": "good",
                    "title": "Clean full pattern",
                    "detail": "All required meetings fit without direct conflict or student pressure.",
                }
            ]

        first = meetings[0]
        return {
            "candidate_id": f"pattern-{index}",
            "kind": first["kind"],
            "is_pattern": True,
            "requires_full_section_pattern": True,
            "required_meetings": required_meetings,
            "day": first["day"],
            "start": first["start"],
            "end": first["end"],
            "meetings": meetings,
            "score": score,
            "tone": tone,
            "critical_count": critical,
            "warning_count": warning,
            "same_board_student_count": sum(
                int(row.get("same_board_student_count") or 0) for row in slot_scores
            ),
            "cross_board_student_count": sum(
                int(row.get("cross_board_student_count") or 0) for row in slot_scores
            ),
            "student_affected_count": affected,
            "impact_score": impact_score,
            "quality_score": quality_score + pattern_penalty,
            "pattern_quality": {
                "slot_consistency_penalty": slot_index_penalty,
                "day_spacing_penalty": day_spacing_penalty,
            },
            "timetable_quality": {
                "policy": TIMETABLE_QUALITY_POLICY,
                "penalty": quality_score + pattern_penalty,
                "components": {
                    "meeting_quality": quality_score,
                    "slot_consistency": slot_index_penalty,
                    "day_spacing": day_spacing_penalty,
                },
                "reasons": [],
            },
            "evidence": evidence_rows,
            "ranking_reasons": ranking_reasons[:7],
            "primary_reason": _slot_primary_reason(ranking_reasons),
        }

    if required_meetings > 1:
        pattern = meeting_pattern_for_request()
        options = generate_meeting_options(
            pattern,
            scenario.slot_config or DEFAULT_SLOTS,
            scenario.lab_slot_config or DEFAULT_LAB_SLOTS,
        )
        candidates = [score_pattern(option, idx) for idx, option in enumerate(options, start=1)]
        candidates.sort(
            key=lambda c: (
                c["score"],
                _DAY_INDEX.get(str(c["day"]).upper(), 99),
                _to_minutes(str(c["start"])),
            )
        )
        for idx, candidate in enumerate(candidates, start=1):
            candidate["rank"] = idx
            if idx == 1:
                candidate["badge"] = (
                    "Best pattern" if candidate["tone"] == "clean" else "Least bad pattern"
                )
            elif candidate["tone"] == "clean":
                candidate["badge"] = "Clean pattern"
            elif candidate["student_affected_count"]:
                candidate["badge"] = f"{candidate['student_affected_count']} students"
            elif candidate["tone"] == "risky":
                candidate["badge"] = "Risky pattern"
            else:
                candidate["badge"] = "Avoid"

        limited = candidates[: max(1, int(limit or 80))]
        return {
            "board": {
                "id": board.id,
                "label": board.label,
                "nominal_term": board.nominal_term,
            },
            "request": {
                "course_code": code,
                "course_key": key,
                "section_label": section,
                "credit_hours": effective_credit_hours,
                "max_per_section": int(
                    max_per_section
                    if max_per_section is not None
                    else getattr(budget, "max_per_section", 0) or 0
                ),
                "required_meetings_per_section": required_meetings,
                "requires_full_section_pattern": True,
            },
            "status": "ready",
            "summary": {
                "total": len(candidates),
                "clean": sum(1 for c in candidates if c["tone"] == "clean"),
                "risky": sum(1 for c in candidates if c["tone"] == "risky"),
                "avoid": sum(1 for c in candidates if c["tone"] == "avoid"),
            },
            "candidates": limited,
        }

    candidates: list[dict] = []
    for slot_kind, slots in slot_sets:
        for day in days:
            for slot in slots:
                start = str(slot.get("start", ""))
                end = str(slot.get("end", ""))
                if not start or not end:
                    continue
                candidates.append(score_slot(slot_kind, day, start, end))

    candidates.sort(
        key=lambda c: (
            c["score"],
            days.index(str(c["day"])),
            _to_minutes(str(c["start"])),
            0 if c["kind"] == "lect" else 1,
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

    limited = candidates[: max(1, int(limit or 80))]
    return {
        "board": {
            "id": board.id,
            "label": board.label,
            "nominal_term": board.nominal_term,
        },
        "request": {
            "course_code": code,
            "course_key": key,
            "section_label": section,
            "credit_hours": effective_credit_hours,
            "max_per_section": int(
                max_per_section
                if max_per_section is not None
                else getattr(budget, "max_per_section", 0) or 0
            ),
            "required_meetings_per_section": required_meetings,
            "requires_full_section_pattern": False,
        },
        "status": "ready",
        "summary": {
            "total": len(candidates),
            "clean": sum(1 for c in candidates if c["tone"] == "clean"),
            "risky": sum(1 for c in candidates if c["tone"] == "risky"),
            "avoid": sum(1 for c in candidates if c["tone"] == "avoid"),
        },
        "candidates": limited,
    }


def _normalise_planned_meeting_rows(rows: list[dict]) -> list[dict[str, str]]:
    meetings: list[dict[str, str]] = []
    for row in rows:
        day = str(row.get("day", "")).strip().upper()
        start = str(row.get("start_time") or row.get("start") or "").strip()
        end = str(row.get("end_time") or row.get("end") or "").strip()
        room = str(row.get("room") or "").strip()
        if not day or not start or not end:
            raise ValueError("Each planned meeting needs day, start_time, and end_time.")
        if _time_mask(day, start, end) == 0:
            raise ValueError(f"Invalid planned meeting time: {day} {start}-{end}.")
        meetings.append({"day": day, "start_time": start, "end_time": end, "room": room})
    if not meetings:
        raise ValueError("At least one planned meeting is required.")
    if len({row["day"] for row in meetings}) != len(meetings):
        raise ValueError("A complete planned section pattern must use one meeting per day.")
    if len({(row["day"], row["start_time"]) for row in meetings}) != len(meetings):
        raise ValueError("Duplicate planned section meeting target.")
    return meetings


def create_planned_section_placements(
    board_id: int,
    *,
    course_code: str,
    course_key: str | None,
    course_name: str | None,
    section_label: str,
    capacity: int,
    meetings: list[dict],
) -> dict:
    """Create a planned section and all required placement rows atomically."""

    board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    if board.scenario.status == "published":
        raise ValueError("Cannot modify published scenario.")

    code = str(course_code or "").strip().upper()
    key = str(course_key or code).strip().upper()
    section = str(section_label or "").strip().upper()
    name = str(course_name or code).strip()
    if not code or not key or not section:
        raise ValueError("course_code, course_key, and section_label are required.")

    budget = (
        ScenarioSectionBudget.objects.filter(scenario=board.scenario, course_key=key).first()
        or ScenarioSectionBudget.objects.filter(scenario=board.scenario, course_code=code).first()
    )
    required_meetings = infer_required_meeting_count(
        board.scenario_id,
        key,
        fallback_credit_hours=getattr(budget, "credit_hours", 0) if budget else None,
    )
    meeting_rows = _normalise_planned_meeting_rows(meetings)
    if required_meetings > 1 and len(meeting_rows) != required_meetings:
        raise ValueError(f"{code} needs a complete {required_meetings}-meeting section pattern.")
    if required_meetings <= 1 and len(meeting_rows) != 1:
        raise ValueError(f"{code} only needs one planned meeting target.")

    with transaction.atomic():
        term_section, _created = TermSection.objects.get_or_create(
            scenario=board.scenario,
            course_key=key,
            section=section,
            defaults={
                "course_code": code,
                "course_number": code,
                "course_name": name,
                "available_capacity": capacity,
                "source_tag": "tw_planned",
            },
        )
        placements: list[SectionPlacement] = []
        validations = []
        for row in meeting_rows:
            TermSectionMeeting.objects.get_or_create(
                term_section=term_section,
                day=row["day"],
                start_time=row["start_time"],
                end_time=row["end_time"],
                room=row["room"],
                instructor="",
            )
            validations.append(
                validate_placement(
                    board_id=board.id,
                    day=row["day"],
                    start_time=row["start_time"],
                    end_time=row["end_time"],
                    room=row["room"],
                    term_section_id=term_section.id,
                )
            )
            placements.append(
                SectionPlacement.objects.create(
                    board=board,
                    term_section=term_section,
                    day=row["day"],
                    start_time=row["start_time"],
                    end_time=row["end_time"],
                    room=row["room"],
                )
            )

    return {
        "board": board,
        "term_section": term_section,
        "placements": placements,
        "validations": validations,
        "required_meetings": required_meetings,
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
    ignore_overlap_term_section_ids: set[int] | None = None,
) -> dict:
    """Return room candidates for assigning a placement at a target slot."""
    from core.services.timetable_rooming import get_board_gender, room_type_for_placement

    placement = SectionPlacement.objects.select_related("board__scenario", "term_section").get(
        id=placement_id
    )
    target_day = str(day or placement.day).strip().upper()
    target_start = str(start_time or placement.start_time).strip()
    target_end = str(end_time or placement.end_time).strip()
    term_section = placement.term_section
    board = placement.board
    online_lookup = OnlineCourseLookup()
    is_online = online_lookup.is_online_course_for_board(board, term_section.course_code)
    if is_online:
        return {
            "placement": {
                "id": placement.id,
                "course_code": term_section.course_code,
                "section": term_section.section,
                "board_id": board.id,
                "board_label": board.label,
                "current_room": placement.room,
                "is_online": True,
            },
            "target": {
                "day": target_day,
                "start": target_start,
                "end": target_end,
                "required_capacity": 0,
                "required_type": "online",
                "required_gender": "",
                "is_online": True,
            },
            "candidates": [],
            "summary": {
                "total": 0,
                "available": 0,
                "clean": 0,
                "blocked": 0,
                "is_online": True,
            },
        }
    required_capacity = int(
        term_section.registered_count or term_section.available_capacity or board.target_size or 0
    )
    required_type = room_type_for_placement(
        placement,
        start_time=target_start,
        end_time=target_end,
    )
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
        if online_lookup.is_online_course_for_board(other.board, other.term_section.course_code):
            continue
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
        fits_capacity = room_capacity >= required_capacity
        department_fit = _room_department_fits(room, board)

        validation = validate_placement(
            board_id=board.id,
            day=target_day,
            start_time=target_start,
            end_time=target_end,
            room=room_code,
            term_section_id=term_section.id,
            exclude_placement_id=placement.id,
            ignore_overlap_term_section_ids=ignore_overlap_term_section_ids,
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
        schedule_clean = (
            resource_available
            and validation.get("critical_count", 0) == 0
            and validation.get("warning_count", 0) == 0
        )
        policy_clean = schedule_clean and department_fit
        tone = "clean" if policy_clean else ("warn" if resource_available else "block")
        capacity_slack = room_capacity - required_capacity
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
                "slot_clean": policy_clean,
                "tone": tone,
                "score": score,
                "capacity_slack": capacity_slack,
                "resource_available": resource_available,
                "schedule_clean": schedule_clean,
                "policy_clean": policy_clean,
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
            "is_online": False,
        },
        "target": {
            "day": target_day,
            "start": target_start,
            "end": target_end,
            "required_capacity": required_capacity,
            "required_type": required_type,
            "required_gender": required_gender,
            "is_online": False,
        },
        "candidates": candidates[:60],
        "summary": {
            "total": len(candidates),
            "available": sum(1 for c in candidates if c["available"]),
            "clean": sum(1 for c in candidates if c["slot_clean"]),
            "blocked": sum(1 for c in candidates if not c["available"]),
            "is_online": False,
        },
    }


def _room_reservation_key(room_code: str, day: str, start: str, end: str) -> str:
    return "|".join(
        [
            _room_key(room_code),
            str(day or "").strip().upper(),
            str(start or "").strip(),
            str(end or "").strip(),
        ]
    )


def _candidate_is_clean_room(room: dict) -> bool:
    return (
        bool(room.get("available"))
        and bool(room.get("slot_clean"))
        and (room.get("schedule_clean", True) is not False)
        and (room.get("policy_clean", True) is not False)
    )


def _best_clean_room_candidate(
    preview: dict,
    *,
    current_room: str,
    reserved: set[str],
) -> dict | None:
    current = _room_key(current_room)
    target = preview.get("target") or {}
    for room in preview.get("candidates") or []:
        room_code = _room_key(room.get("room_code"))
        if not room_code or room_code == current:
            continue
        if not _candidate_is_clean_room(room):
            continue
        reservation = _room_reservation_key(
            room_code,
            str(target.get("day") or ""),
            str(target.get("start") or ""),
            str(target.get("end") or ""),
        )
        if reservation in reserved:
            continue
        return room
    return None


def preview_bulk_clean_room_assignments(scenario_id: int, *, limit: int = 20) -> dict:
    """Return clean room assignments that can be safely applied in one batch."""

    scenario = TimetableScenario.objects.get(id=scenario_id)
    placements = (
        SectionPlacement.objects.filter(board__scenario=scenario)
        .select_related("board", "term_section")
        .order_by("board__display_order", "board__label", "day", "start_time", "id")
    )
    reserved: set[str] = set()
    assignments: list[dict] = []
    skipped: list[dict] = []
    total_unassigned = 0
    max_items = max(1, min(100, int(limit or 20)))

    for placement in placements:
        current_room = _room_key(placement.room)
        if current_room and current_room != "UNASSIGNED":
            continue
        total_unassigned += 1
        if placement.is_locked:
            skipped.append(
                {
                    "placement_id": placement.id,
                    "reason": "locked",
                    "label": f"{placement.term_section.course_code}-{placement.term_section.section}",
                }
            )
            continue
        if len(assignments) >= max_items:
            continue

        preview = preview_placement_room_candidates(placement.id)
        if (preview.get("target") or {}).get("is_online"):
            skipped.append(
                {
                    "placement_id": placement.id,
                    "reason": "online",
                    "label": f"{placement.term_section.course_code}-{placement.term_section.section}",
                }
            )
            continue
        room = _best_clean_room_candidate(
            preview,
            current_room=placement.room,
            reserved=reserved,
        )
        if not room:
            skipped.append(
                {
                    "placement_id": placement.id,
                    "reason": "no_clean_room",
                    "label": f"{placement.term_section.course_code}-{placement.term_section.section}",
                    "summary": preview.get("summary") or {},
                }
            )
            continue

        target = preview.get("target") or {}
        reservation = _room_reservation_key(
            str(room.get("room_code") or ""),
            str(target.get("day") or placement.day),
            str(target.get("start") or placement.start_time),
            str(target.get("end") or placement.end_time),
        )
        reserved.add(reservation)
        assignments.append(
            {
                "placement_id": placement.id,
                "board_id": placement.board_id,
                "board_label": placement.board.label,
                "course_code": placement.term_section.course_code,
                "section": placement.term_section.section,
                "label": f"{placement.term_section.course_code}-{placement.term_section.section}",
                "day": str(target.get("day") or placement.day),
                "start": str(target.get("start") or placement.start_time),
                "end": str(target.get("end") or placement.end_time),
                "slot": {
                    "day": str(target.get("day") or placement.day),
                    "start": str(target.get("start") or placement.start_time),
                    "end": str(target.get("end") or placement.end_time),
                },
                "old_room": placement.room or "",
                "room": room,
                "new_room": str(room.get("room_code") or ""),
                "required_capacity": int(target.get("required_capacity") or 0),
                "required_type": str(target.get("required_type") or ""),
                "capacity_slack": int(room.get("capacity_slack") or 0),
            }
        )

    return {
        "scenario_id": scenario.id,
        "limit": max_items,
        "total_unassigned": total_unassigned,
        "ready_count": len(assignments),
        "skipped_count": len(skipped),
        "assignments": assignments,
        "skipped": skipped[:40],
        "truncated": len(assignments) >= max_items and total_unassigned > len(assignments),
    }


def apply_bulk_clean_room_assignments(scenario_id: int, *, limit: int = 20) -> dict:
    """Apply the clean room batch after re-validating each assignment."""

    scenario = TimetableScenario.objects.get(id=scenario_id)
    if scenario.status == "published":
        raise ValueError("Cannot modify published scenario")

    with transaction.atomic():
        preview = preview_bulk_clean_room_assignments(scenario_id, limit=limit)
        placement_ids = [int(item["placement_id"]) for item in preview["assignments"]]
        locked = {
            placement.id: placement
            for placement in SectionPlacement.objects.select_for_update()
            .filter(id__in=placement_ids, board__scenario=scenario)
            .select_related("board", "term_section")
        }
        applied: list[dict] = []
        skipped: list[dict] = list(preview.get("skipped") or [])
        for item in preview["assignments"]:
            placement = locked.get(int(item["placement_id"]))
            if not placement:
                skipped.append({**item, "reason": "missing_after_preview"})
                continue
            if placement.is_locked:
                skipped.append({**item, "reason": "locked_after_preview"})
                continue

            current_room = _room_key(placement.room)
            if current_room and current_room != "UNASSIGNED":
                skipped.append({**item, "reason": "already_assigned_after_preview"})
                continue

            recheck = preview_placement_room_candidates(placement.id)
            room = _best_clean_room_candidate(recheck, current_room=placement.room, reserved=set())
            if not room or _room_key(room.get("room_code")) != _room_key(item["new_room"]):
                skipped.append({**item, "reason": "room_no_longer_clean"})
                continue

            old_room = placement.room or ""
            placement.room = str(item["new_room"] or "")
            placement.save(update_fields=["room", "updated_at"])
            applied.append(
                {
                    **item,
                    "old_day": placement.day,
                    "old_start": placement.start_time,
                    "old_end": placement.end_time,
                    "old_room": old_room,
                    "new_day": placement.day,
                    "new_start": placement.start_time,
                    "new_end": placement.end_time,
                    "new_room": placement.room,
                }
            )

    return {
        "scenario_id": scenario.id,
        "requested_count": len(preview.get("assignments") or []),
        "applied_count": len(applied),
        "skipped_count": len(skipped),
        "applied": applied,
        "skipped": skipped[:60],
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
        demand.student_id: demand.primary_term
        for demand in load_scenario_course_demands(scenario.id)
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
        "source": "scenario_course_requests",
        "affected_student_count": len(affected_total),
        "students": _student_rows(affected_total, primary_terms, limit),
        "conflicts": conflicts,
    }


def build_scenario_builder_actions(scenario_id: int, *, limit: int = 18) -> dict:
    """Rank the next practical actions a timetable builder should take."""
    readiness = check_publish_readiness(scenario_id)
    actions: list[dict] = []
    online_lookup = OnlineCourseLookup()

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
        course_key: str = "",
        programme_term: int | None = None,
        cta: str = "",
        extra: dict | None = None,
    ) -> None:
        action = {
            "kind": kind,
            "severity": severity,
            "title": title,
            "detail": detail,
            "score": score,
            "board_id": board_id,
            "board_label": board_label,
            "placement_ids": placement_ids or [],
            "course_code": course_code,
            "course_key": course_key,
            "programme_term": programme_term,
            "cta": cta,
        }
        if extra:
            action.update(extra)
        actions.append(action)

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
                cta="Preview candidate slots",
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
        unroomed_qs = SectionPlacement.objects.filter(
            board=board,
            room="UNASSIGNED",
        ).select_related("term_section")
        unroomed = list(
            exclude_online_courses_for_board(
                unroomed_qs,
                board,
                lookup=online_lookup,
            ).order_by("day", "start_time")[:5]
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
            "block" if item.get("requires_full_section_pattern") else "warn",
            f"Place {remaining} more section(s) of {item['course_code']}",
            f"Demand {item.get('total_demand', 0)}; planned {item.get('planned_sections', 0)}; used {item.get('used_sections', 0)}",
            score=52000 + remaining * 1000,
            course_code=str(item["course_code"]),
            course_key=str(item.get("course_key") or item["course_code"]),
            programme_term=item.get("programme_term"),
            cta=(
                "Full-section wizard required"
                if item.get("requires_full_section_pattern")
                else "Click required section"
            ),
            extra={
                "required_meetings_per_section": item.get("required_meetings_per_section", 1),
                "requires_full_section_pattern": bool(item.get("requires_full_section_pattern")),
            },
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


def _bulk_time_move_target_key(board_id: int, day: str, start: str) -> str:
    return "|".join([str(board_id), str(day or "").upper(), str(start or "")])


def _is_bulk_safe_time_candidate(candidate: dict) -> bool:
    """Conservative bulk-safe subset of slot candidates.

    Single manual moves may still choose risky-but-useful options. Bulk auto
    moves are stricter: no hard/warning preview issues and a measurable local
    improvement.
    """

    if not candidate:
        return False
    if candidate.get("pair"):
        return False
    if str(candidate.get("tone") or "").lower() != "clean":
        return False
    if int(candidate.get("critical_count") or candidate.get("critical") or 0) > 0:
        return False
    if int(candidate.get("warning_count") or candidate.get("warning") or 0) > 0:
        return False
    return any(
        int(candidate.get(key) or 0) > 0
        for key in (
            "impact_improvement",
            "student_improvement",
            "critical_improvement",
            "warning_improvement",
        )
    )


def preview_bulk_safe_time_moves(
    scenario_id: int,
    *,
    board_id: int | None = None,
    limit: int = 3,
) -> dict:
    """Return clean time moves that can be applied as a small server-side batch."""

    scenario = TimetableScenario.objects.get(id=scenario_id)
    max_items = max(1, min(12, int(limit or 3)))
    actions = build_scenario_builder_actions(scenario_id, limit=120).get("actions") or []
    candidate_placement_ids: list[int] = []
    seen: set[int] = set()
    for action in actions:
        if action.get("kind") not in {"student_time_clash", "instructor_clash"}:
            continue
        if board_id is not None and int(action.get("board_id") or 0) != int(board_id):
            continue
        for raw_id in action.get("placement_ids") or []:
            placement_id = int(raw_id)
            if placement_id in seen:
                continue
            seen.add(placement_id)
            candidate_placement_ids.append(placement_id)

    placements = {
        placement.id: placement
        for placement in SectionPlacement.objects.filter(
            id__in=candidate_placement_ids,
            board__scenario=scenario,
        ).select_related("board", "term_section")
    }
    reserved_targets: set[str] = set()
    moves: list[dict] = []
    skipped: list[dict] = []
    for placement_id in candidate_placement_ids:
        if len(moves) >= max_items:
            break
        placement = placements.get(placement_id)
        if not placement:
            continue
        label = f"{placement.term_section.course_code}-{placement.term_section.section}"
        if placement.is_locked:
            skipped.append({"placement_id": placement_id, "label": label, "reason": "locked"})
            continue
        preview = preview_placement_slot_candidates(placement.id)
        selected = None
        for candidate in preview.get("candidates") or []:
            if not _is_bulk_safe_time_candidate(candidate):
                continue
            target_key = _bulk_time_move_target_key(
                placement.board_id,
                str(candidate.get("day") or ""),
                str(candidate.get("start") or ""),
            )
            if target_key in reserved_targets:
                continue
            occupied = (
                SectionPlacement.objects.filter(
                    board=placement.board,
                    day=str(candidate.get("day") or ""),
                    start_time=str(candidate.get("start") or ""),
                )
                .exclude(id=placement.id)
                .exists()
            )
            if occupied:
                continue
            selected = candidate
            reserved_targets.add(target_key)
            break
        if not selected:
            skipped.append(
                {"placement_id": placement_id, "label": label, "reason": "no_clean_improving_slot"}
            )
            continue
        moves.append(
            {
                "placement_id": placement.id,
                "board_id": placement.board_id,
                "board_label": placement.board.label,
                "label": label,
                "course_code": placement.term_section.course_code,
                "section": placement.term_section.section,
                "old_day": placement.day,
                "old_start": placement.start_time,
                "old_end": placement.end_time,
                "old_room": placement.room or "",
                "new_day": str(selected.get("day") or ""),
                "new_start": str(selected.get("start") or ""),
                "new_end": str(selected.get("end") or ""),
                "new_room": placement.room or "",
                "candidate": selected,
                "primary_reason": selected.get("primary_reason") or "",
            }
        )

    return {
        "scenario_id": scenario.id,
        "board_id": board_id,
        "limit": max_items,
        "ready_count": len(moves),
        "skipped_count": len(skipped),
        "moves": moves,
        "skipped": skipped[:60],
    }


def apply_bulk_safe_time_moves(
    scenario_id: int,
    *,
    board_id: int | None = None,
    limit: int = 3,
) -> dict:
    """Apply conservative clean time moves after re-validating inside a transaction."""

    scenario = TimetableScenario.objects.get(id=scenario_id)
    if scenario.status == "published":
        raise ValueError("Cannot modify published scenario")

    with transaction.atomic():
        preview = preview_bulk_safe_time_moves(scenario_id, board_id=board_id, limit=limit)
        requested = list(preview.get("moves") or [])
        placement_ids = [int(item["placement_id"]) for item in requested]
        locked = {
            placement.id: placement
            for placement in SectionPlacement.objects.select_for_update()
            .filter(id__in=placement_ids, board__scenario=scenario)
            .select_related("board", "term_section")
        }
        applied: list[dict] = []
        skipped: list[dict] = list(preview.get("skipped") or [])
        for item in requested:
            placement = locked.get(int(item["placement_id"]))
            if not placement:
                skipped.append({**item, "reason": "missing_after_preview"})
                continue
            if placement.is_locked:
                skipped.append({**item, "reason": "locked_after_preview"})
                continue

            fresh_preview = preview_placement_slot_candidates(placement.id)
            fresh = next(
                (
                    row
                    for row in fresh_preview.get("candidates") or []
                    if str(row.get("day")) == str(item["new_day"])
                    and str(row.get("start")) == str(item["new_start"])
                    and str(row.get("end")) == str(item["new_end"])
                ),
                None,
            )
            if not fresh or not _is_bulk_safe_time_candidate(fresh):
                skipped.append({**item, "reason": "candidate_no_longer_clean"})
                continue
            occupied = (
                SectionPlacement.objects.filter(
                    board=placement.board,
                    day=str(item["new_day"]),
                    start_time=str(item["new_start"]),
                )
                .exclude(id=placement.id)
                .exists()
            )
            if occupied:
                skipped.append({**item, "reason": "target_no_longer_empty"})
                continue
            validation = validate_placement(
                board_id=placement.board_id,
                day=str(item["new_day"]),
                start_time=str(item["new_start"]),
                end_time=str(item["new_end"]),
                room=placement.room or "",
                term_section_id=placement.term_section_id,
                exclude_placement_id=placement.id,
            )
            if validation.get("critical_count", 0) or validation.get("warning_count", 0):
                skipped.append({**item, "reason": "validation_no_longer_clean"})
                continue

            old_day, old_start, old_end = placement.day, placement.start_time, placement.end_time
            old_room = placement.room or ""
            placement.day = str(item["new_day"])
            placement.start_time = str(item["new_start"])
            placement.end_time = str(item["new_end"])
            placement.save(update_fields=["day", "start_time", "end_time", "updated_at"])
            applied.append(
                {
                    **item,
                    "old_day": old_day,
                    "old_start": old_start,
                    "old_end": old_end,
                    "old_room": old_room,
                    "new_day": placement.day,
                    "new_start": placement.start_time,
                    "new_end": placement.end_time,
                    "new_room": placement.room or "",
                }
            )

    return {
        "scenario_id": scenario.id,
        "board_id": board_id,
        "requested_count": len(requested),
        "applied_count": len(applied),
        "skipped_count": len(skipped),
        "applied": applied,
        "skipped": skipped[:80],
    }


def compute_affected_students(board_id: int) -> dict:
    """Assess student impact of time-overlapping placements on a board.

    For every pair of overlapping placements (detected by
    :func:`detect_board_conflicts`):

    1. Extract the two course codes from the ``"CS101-A"`` labels.
    2. Use canonical scenario course requests to find students
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

    # Precompute course->students map from canonical scenario demand.
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


def get_scenario_boards_summary(
    scenario_id: int,
    *,
    boards: list[DeliveryBoard] | None = None,
    conflicts_by_board: dict[int, dict] | None = None,
) -> list[dict]:
    """Return a summary row for every board in a scenario.

    Each row includes placement count, primary/visitor student counts,
    and critical/warning conflict tallies.  Used by the scenario
    overview panel in the workspace UI.

    Parameters:
        scenario_id: PK of the parent ``Scenario``.

    Returns:
        List of dicts ordered by ``DeliveryBoard.display_order``.
    """
    if boards is None:
        boards = list(
            DeliveryBoard.objects.filter(scenario_id=scenario_id).order_by("display_order")
        )
    conflicts_by_board = conflicts_by_board or {}
    result = []
    for b in boards:
        placed = SectionPlacement.objects.filter(board=b).count()
        conflicts = conflicts_by_board.get(b.id) or detect_board_conflicts(b.id)
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


def _section_meeting_counts(scenario_id: int) -> dict[str, dict[str, int]]:
    """Return placement counts keyed by course_key then section label."""

    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    placements = SectionPlacement.objects.filter(board__scenario_id=scenario_id).select_related(
        "term_section"
    )
    for placement in placements:
        key = placement.term_section.course_key or placement.term_section.course_code
        section = placement.term_section.section
        counts[key][section] += 1
    return {key: dict(value) for key, value in counts.items()}


def infer_required_meeting_count(
    scenario_id: int,
    course_key: str,
    *,
    fallback_credit_hours: int | None = None,
    section_counts_by_course: dict[str, dict[str, int]] | None = None,
) -> int:
    """Infer how many placement rows make a complete section for a course."""

    key = str(course_key or "").strip()
    if key:
        section_counts_by_course = section_counts_by_course or _section_meeting_counts(scenario_id)
        counts = [count for count in section_counts_by_course.get(key, {}).values() if count]
        if counts:
            inferred = max(counts)
            if inferred > 1:
                return inferred
    if int(fallback_credit_hours or 0) >= 4:
        return 3
    return 1


def compute_incomplete_section_patterns(scenario_id: int) -> list[dict]:
    """Find placed sections that look incomplete against their course pattern."""

    budget_by_key = {
        (row.course_key or row.course_code): row
        for row in ScenarioSectionBudget.objects.filter(scenario_id=scenario_id)
    }
    counts_by_course = _section_meeting_counts(scenario_id)
    issues: list[dict] = []
    for course_key, section_counts in counts_by_course.items():
        budget = budget_by_key.get(course_key)
        expected = infer_required_meeting_count(
            scenario_id,
            course_key,
            fallback_credit_hours=getattr(budget, "credit_hours", 0) if budget else None,
            section_counts_by_course=counts_by_course,
        )
        if expected <= 1:
            continue
        for section, placed_count in sorted(section_counts.items()):
            if placed_count >= expected:
                continue
            issues.append(
                {
                    "course_key": course_key,
                    "course_code": getattr(budget, "course_code", course_key.split("::", 1)[0]),
                    "section": section,
                    "placed_meetings": placed_count,
                    "expected_meetings": expected,
                    "missing_meetings": expected - placed_count,
                }
            )
    return issues


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
    section_counts_by_course = _section_meeting_counts(scenario_id)

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
        required_meetings = infer_required_meeting_count(
            scenario_id,
            key,
            fallback_credit_hours=b.credit_hours,
            section_counts_by_course=section_counts_by_course,
        )
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
                "required_meetings_per_section": required_meetings,
                "requires_full_section_pattern": required_meetings > 1,
            }
        )

    result.sort(key=lambda x: (x.get("programme_term") or 0, x["course_code"]))
    return result


def summarize_cross_board_conflict_impact(cross_conflicts: list[dict]) -> dict:
    """Summarise cross-board clashes using actual affected students first.

    ``conflict_pairs`` is still useful as a diagnostic count, but the
    operational metric for the timetable builder is how many unique students
    are affected and how many student-clash incidences remain.
    """

    from core.services.timetable_overlap import HARD_OVERLAP_THRESHOLD

    affected_students: set[int] = set()
    high_affected_students: set[int] = set()
    conflicts_per_student: Counter[int] = Counter()
    student_conflict_incidences = 0
    high_conflict_pairs = 0

    for conflict in cross_conflicts or []:
        student_ids: set[int] = set()
        for raw_id in conflict.get("affected_student_ids") or []:
            try:
                student_ids.add(int(raw_id))
            except (TypeError, ValueError):
                continue

        overlap_count = int(
            conflict.get("affected_student_count")
            or conflict.get("overlap_count")
            or len(student_ids)
            or 0
        )
        affected_students.update(student_ids)
        student_conflict_incidences += overlap_count

        for student_id in student_ids:
            conflicts_per_student[student_id] += 1

        if overlap_count >= HARD_OVERLAP_THRESHOLD:
            high_conflict_pairs += 1
            high_affected_students.update(student_ids)

    return {
        "conflict_pairs": len(cross_conflicts or []),
        "affected_students": len(affected_students),
        "student_conflict_incidences": student_conflict_incidences,
        "high_conflict_pairs": high_conflict_pairs,
        "high_affected_students": len(high_affected_students),
        "max_conflicts_per_student": max(conflicts_per_student.values() or [0]),
    }


def compute_scenario_safety_summary(
    scenario_id: int,
    *,
    boards: list[DeliveryBoard] | None = None,
    board_conflicts_by_id: dict[int, dict] | None = None,
    cross_board_conflicts: list[dict] | None = None,
) -> dict:
    """Return the backend truth contract used by the split workspace UI."""

    if boards is None:
        boards = list(
            DeliveryBoard.objects.filter(scenario_id=scenario_id).order_by("display_order")
        )
    board_conflicts_by_id = board_conflicts_by_id or {}
    placements = SectionPlacement.objects.filter(board__scenario_id=scenario_id).select_related(
        "board", "term_section"
    )
    online_lookup = OnlineCourseLookup()
    budget = compute_scenario_budget(scenario_id)
    same_board = {"overlaps": 0, "instructors": 0, "rooms": 0}
    for board in boards:
        conflicts = board_conflicts_by_id.get(board.id) or detect_board_conflicts(board.id)
        same_board["overlaps"] += len(conflicts.get("overlaps", []))
        same_board["instructors"] += len(conflicts.get("instructor_clashes", []))
        same_board["rooms"] += len(conflicts.get("room_clashes", []))

    physical_unassigned = 0
    online_without_room = 0
    room_assigned = 0
    for placement in placements:
        room = str(placement.room or "").strip()
        is_online = online_lookup.is_online_course_for_board(
            placement.board, placement.term_section.course_code
        )
        if room and room.upper() != "UNASSIGNED":
            room_assigned += 1
        if is_online:
            if not room:
                online_without_room += 1
        elif not room or room.upper() == "UNASSIGNED":
            physical_unassigned += 1

    links = BoardStudentLink.objects.filter(board__scenario_id=scenario_id)
    cross_conflicts = (
        cross_board_conflicts
        if cross_board_conflicts is not None
        else detect_cross_board_conflicts(scenario_id)
    )
    cross_impact = summarize_cross_board_conflict_impact(cross_conflicts)

    return {
        "boards": len(boards),
        "placements": placements.count(),
        "unique_students": len(
            {demand.student_id for demand in load_scenario_course_demands(scenario_id)}
        ),
        "board_student_links_total": links.count(),
        "primary_student_links": links.filter(link_type="primary").count(),
        "visitor_student_links": links.exclude(link_type="primary").count(),
        "room_assigned": room_assigned,
        "physical_unassigned_rooms": physical_unassigned,
        "online_without_room": online_without_room,
        "same_board_conflicts": same_board,
        "cross_board_conflicts": cross_impact["conflict_pairs"],
        "cross_board_affected_students": cross_impact["affected_students"],
        "cross_board_student_conflict_incidences": cross_impact["student_conflict_incidences"],
        "high_cross_board_conflicts": cross_impact["high_conflict_pairs"],
        "high_cross_board_affected_students": cross_impact["high_affected_students"],
        "max_cross_board_conflicts_per_student": cross_impact["max_conflicts_per_student"],
        "budget": {
            "rows": len(budget),
            "planned_sections": sum(int(row.get("planned_sections", 0) or 0) for row in budget),
            "used_sections": sum(int(row.get("used_sections", 0) or 0) for row in budget),
            "remaining_sections": sum(int(row.get("remaining_sections", 0) or 0) for row in budget),
            "missing_required_sections": [
                row for row in budget if int(row.get("remaining_sections", 0) or 0) > 0
            ],
        },
        "incomplete_section_patterns": compute_incomplete_section_patterns(scenario_id),
    }


# ── Cross-Board Conflict Detection ──────────────────────────────


def detect_cross_board_conflicts(scenario_id: int) -> list[dict]:
    """Find time-overlapping placements across *different* boards that
    impact shared students.

    Unlike :func:`detect_board_conflicts` (single-board), this checks
    every (board_a, board_b) pair.  A cross-board conflict is reported
    only when:

    1. A placement on board_a and a placement on board_b overlap in time
       (bitmask AND), **and**
    2. At least one student in canonical scenario course requests needs
       courses from *both* placements.

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
    # need that course from canonical scenario course requests.
    placement_course_keys = {
        placement.course_key
        for placements in board_placements.values()
        for placement in placements
        if placement.course_key
    }
    course_students = compute_course_students_index(
        scenario_id,
        course_keys=placement_course_keys,
    )
    primary_terms = {
        demand.student_id: demand.primary_term
        for demand in load_scenario_course_demands(scenario_id, course_keys=placement_course_keys)
    }
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
                                    "affected_student_count": len(shared),
                                    "affected_student_ids": sorted(shared),
                                    "affected_student_ids_sample": sorted(shared)[:40],
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
        - A board has a placement sitting on an institutionally **blocked**
          slot (legality — the timetable must not occupy a reserved cell).

    Warnings (advisory, do not block):
        - A board has room-clash warnings.

    Parameters:
        scenario_id: PK of the ``Scenario``.

    Returns:
        ``{"ready": bool, "blockers": [...], "warnings": [...]}``
    """
    from django.db.models import Q

    from core.models import TimetableScenario
    from core.services.timetable_validation import blocked_slot_keys

    boards = DeliveryBoard.objects.filter(scenario_id=scenario_id)
    blockers: list[str] = []
    warnings: list[str] = []
    online_lookup = OnlineCourseLookup()

    if boards.count() == 0:
        blockers.append("No boards in this scenario")
        return {"ready": False, "blockers": blockers, "warnings": warnings}

    _scenario = TimetableScenario.objects.filter(id=scenario_id).first()
    blocked_set = blocked_slot_keys(_scenario.blocked_slots) if _scenario else set()
    blocked_q = Q()
    for _day, _start in blocked_set:
        blocked_q |= Q(day=_day, start_time=_start)

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

        # Legality: no placement may occupy an institutionally blocked slot.
        if blocked_set:
            on_blocked = SectionPlacement.objects.filter(board=board).filter(blocked_q).count()
            if on_blocked:
                blockers.append(
                    f"Board '{board.label}': {on_blocked} placement(s) on blocked slots"
                )

        # Room clashes: block publish when rooms are actively assigned
        room_clashes = len(conflicts.get("room_clashes", []))
        offline_placements = exclude_online_courses_for_board(
            SectionPlacement.objects.filter(board=board),
            board,
            lookup=online_lookup,
        )
        has_rooms = offline_placements.exclude(room="").exclude(room="UNASSIGNED").exists()

        if room_clashes > 0 and has_rooms:
            blockers.append(f"Board '{board.label}': {room_clashes} room conflicts")

        # Unassigned rooms when rooms are expected
        unassigned_rooms = offline_placements.filter(room="UNASSIGNED").count()
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

    missing_sections = [
        item for item in compute_scenario_budget(scenario_id) if item["remaining_sections"] > 0
    ]
    for item in missing_sections:
        blockers.append(
            "Missing required section: "
            f"{item['course_code']} needs {item['remaining_sections']} more "
            f"(planned {item['planned_sections']}, used {item['used_sections']})"
        )

    for issue in compute_incomplete_section_patterns(scenario_id):
        blockers.append(
            "Incomplete section pattern: "
            f"{issue['course_code']}-{issue['section']} has "
            f"{issue['placed_meetings']}/{issue['expected_meetings']} meetings"
        )

    return {
        "ready": len(blockers) == 0,
        "blockers": blockers,
        "warnings": warnings,
    }

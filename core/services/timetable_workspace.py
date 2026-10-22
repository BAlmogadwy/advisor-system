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
       how many students need *both* clashing courses and whether a
       clash-free pair of their sections exists; an overlap is critical
       (H15) only when it leaves students without one.

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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

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
from core.services.board_ownership import external_shared_section_ids
from core.services.natural_order import natural_key
from core.services.section_source_tags import (
    PLANNED_SECTION_SOURCE_TAG,
    WORKSPACE_SECTION_SOURCE_TAGS,
)
from core.services.student_helpers import normalize_code
from core.services.timetable_course_policy import (
    get_course_meeting_policy,
    meeting_options_for_policy,
)
from core.services.timetable_demand import (
    compute_course_students_index,
    load_scenario_course_demands,
)
from core.services.timetable_flags import is_one_teacher_active
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
from core.services.timetable_section_policy import (
    SectionPolicyError,
    assert_policy_rows,
    create_policy_section_bundle,
    policy_payload,
    section_policy,
    switch_policy_section_bundle,
)

if TYPE_CHECKING:
    from core.services.timetable_room_audit import MeetingRoomAudit

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
    return normalize_code(code_a) == normalize_code(code_b) and not _parallel_policy_sections(
        code_a, code_b
    )


def _parallel_policy_sections(code_a: object | None, code_b: object | None) -> bool:
    return normalize_code(code_a) == normalize_code(code_b) and bool(
        get_course_meeting_policy(str(code_a or ""))
    )


def _room_key(room: object | None) -> str:
    """Normalize a room code; return empty for no real room assignment."""
    value = str(room or "").strip().upper()
    return "" if value in {"", "UNASSIGNED"} else value


def _same_class(
    other: SectionPlacement, term_section_id: int, day: str, start: str, end: str
) -> bool:
    """Whether ``other`` is a copy of section ``term_section_id`` at this very slot.

    A section shared by several boards is placed once per board; its copies at
    one slot are one class, in one room, with one teacher, so a copy never
    holds a room against itself or double-books its own teacher. The same
    section at another time is another class.
    """
    return (
        other.term_section_id == term_section_id
        and str(other.day).strip().upper() == str(day).strip().upper()
        and str(other.start_time).strip() == str(start).strip()
        and str(other.end_time).strip() == str(end).strip()
    )


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
                room="" if section_policy(p.term_section) else p.room,
                instructor=instructor,
                is_online=normalise_course_code(p.term_section.course_code) in online_codes,
                mask=_time_mask(p.day, p.start_time, p.end_time),
            )
        )
    return result


def _placed_section_masks(scenario_id: int, course_keys: set[str]) -> dict[str, list[int]]:
    """Weekly mask of every placed section of *course_keys* in the scenario.

    Keyed by normalised planner identity (a display code can name two
    courses).  A section's mask is the union of its placements -- each meeting
    is one placement, and a section shared across boards sits at one slot --
    so it is the whole week a student seated in that section attends.  The
    placements are the board's truth (meeting rows go stale between optimiser
    syncs), and one query covers every course.
    """
    from django.db.models import Q

    if not course_keys:
        return {}
    rows = (
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .filter(
            Q(term_section__course_key__in=course_keys)
            | Q(term_section__course_key="", term_section__course_code__in=course_keys)
        )
        .values_list(
            "term_section_id",
            "term_section__course_key",
            "term_section__course_code",
            "day",
            "start_time",
            "end_time",
        )
    )
    by_course = _section_masks_by_course(
        (ts_id, course_key, course_code, _time_mask(day, start_time, end_time))
        for ts_id, course_key, course_code, day, start_time, end_time in rows
    )
    return _nonzero_course_masks(by_course)


def _section_masks_by_course(
    rows: Iterable[tuple[int, str, str, int]],
) -> dict[str, dict[int, int]]:
    """``{course: {term_section_id: weekly mask}}`` from placement masks.

    ``rows`` are ``(term_section_id, course_key, course_code, mask)``, one per
    placement. The course is the normalised planner identity (the code when
    the key is blank) and a section's mask is the union of its placements:
    the keys and masks of :func:`_placed_section_masks`, which reads them with
    one query; the slot verdict builds them from placements a preview has
    already loaded.
    """
    by_course: dict[str, dict[int, int]] = defaultdict(dict)
    for ts_id, course_key, course_code, mask in rows:
        sections = by_course[normalize_code(course_key or course_code)]
        sections[ts_id] = sections.get(ts_id, 0) | mask
    return dict(by_course)


def _nonzero_course_masks(by_course: dict[str, dict[int, int]]) -> dict[str, list[int]]:
    """Each course's section masks as a list, empty masks and courses dropped."""
    masks = {
        course: [mask for mask in by_section.values() if mask]
        for course, by_section in by_course.items()
    }
    return {course: section_masks for course, section_masks in masks.items() if section_masks}


def _has_clash_free_section_pair(masks_a: list[int], masks_b: list[int]) -> bool:
    """Whether some placed section of one course fits round one of the other.

    A student who needs both courses is blocked only when *every* pair of
    their sections clashes; one clash-free pair is a timetable they can take.
    """
    return any(not (mask_a & mask_b) for mask_a in masks_a for mask_b in masks_b)


def _h15_blocked_students(shared: int, masks_a: list[int], masks_b: list[int]) -> int:
    """H15: how many of the ``shared`` students an overlap of two courses blocks.

    Zero while some placed section of one course fits round one of the other
    (the students take that pair), else every one of them. The overlap is
    critical when this reaches ``HARD_OVERLAP_THRESHOLD``. The publish check
    (:func:`detect_board_conflicts`) and the slot verdict both decide by it.
    """
    return 0 if _has_clash_free_section_pair(masks_a, masks_b) else shared


def _publish_teacher_keys(
    link_map: Mapping[str, set[int]],
    by_presence: bool,
    course_key: str,
    section: str,
    instructor: str,
) -> tuple:
    """Who the publish check charges with a section's meetings.

    The keys :func:`detect_board_conflicts` groups its instructor clashes by:
    two overlapping meetings clash when their keys share one. ``link_map`` is
    ``build_section_instructor_ids`` (teacher ids by ``course_key|section``;
    empty with the links flag off) and ``instructor`` the section's first
    meeting row's free-text name. With one teacher per section
    (``by_presence``) a section the map has is charged to its ids -- to nobody
    while its teacher is still to be decided, its meeting row's name never
    counting. Otherwise a section with ids is charged to all of them, and one
    with none to its name (stripped, upper-case). An id never matches a name.
    The slot verdict judges teachers by these keys too.
    """
    key = f"{course_key}|{section}"
    if link_map.get(key) or (by_presence and key in link_map):
        return tuple(link_map[key])
    return (instructor.strip().upper(),) if instructor else ()


def detect_board_conflicts(board_id: int) -> dict:
    """Find every scheduling conflict on a single board.

    Performs an O(n^2) pairwise scan of all placements on the board and
    classifies conflicts into three buckets:

    * **overlaps** -- two sections occupy the same time slot (detected via
      ``mask_a & mask_b != 0``) and students need both.  *Critical* when the
      two are sections of one course, or when the overlap **blocks** students
      (H15): at least ``HARD_OVERLAP_THRESHOLD`` students need both courses
      and no placed section of one fits round a placed section of the other.
      Every other overlap is a *warning*: usually students can take a
      clash-free pair of sibling sections, so it costs choice, not a
      timetable; a blocked overlap below the threshold stays a warning too.
    * **instructor_clashes** (critical) -- same instructor teaching two
      sections at overlapping times: who teaches a section is
      :func:`_publish_teacher_keys` (names compared case-insensitively).
    * **room_clashes** (warning) -- same room assigned to two sections
      at overlapping times.  Compared case-insensitively.

    Parameters:
        board_id: PK of the ``DeliveryBoard`` to analyse.

    Returns:
        A dict with the shape::

            {
                "overlaps":            [{"ids": [...], "sections": [...], "detail": "...",
                                         "severity": "critical" | "warning",
                                         "shared_students": N | None,
                                         "blocked_students": N | None}],
                "instructor_clashes":  [{"ids": [...], "instructor": "...", ...}],
                "room_clashes":        [{"ids": [...], "room": "...", ...}],
                "summary":             {"critical": N, "warning": N, "info": 0},
            }

        ``critical`` = critical overlaps + instructor clashes; ``warning`` =
        warning overlaps + room clashes.  ``blocked_students`` is the number
        of students left with no clash-free pair of sections (``None`` for a
        same-course overlap); :func:`compute_affected_students` reports it.
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
    scenario_id: int | None = None
    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
        scenario_id = board.scenario_id
        # Demand rows are keyed by the planner identity ("CS112::PROGRAMMING_II"),
        # so the matrix must be too: display codes match no row, which left the
        # matrix empty and every cross-course student overlap invisible.
        board_courses = {item.course_key for item in items}
        overlap_matrix = build_overlap_matrix(board.scenario_id, board_courses)
        if is_instructor_links_enabled():
            link_map = build_section_instructor_ids(board.scenario)
    except DeliveryBoard.DoesNotExist:
        overlap_matrix = {}

    by_instructor: dict[object, list] = defaultdict(list)
    by_room: dict[str, list] = defaultdict(list)
    # One teacher per section: PRESENCE in the map decides, as in the placer. A
    # section still to be decided is present with no teacher and charges
    # nobody — its meeting row may still carry a stale name.
    by_presence = is_one_teacher_active()
    for item in items:
        for teacher in _publish_teacher_keys(
            link_map, by_presence, item.course_key, item.section, item.instructor
        ):
            by_instructor[teacher].append(item)
        if not item.is_online and item.room and item.room.strip().upper() != "UNASSIGNED":
            by_room[item.room.strip().upper()].append(item)

    seen_pairs: set[tuple[int, int]] = set()

    def _pair_key(a_id: int, b_id: int) -> tuple[int, int]:
        return (min(a_id, b_id), max(a_id, b_id))

    # ── Student conflict detection (real overlap, not fake cohort) ──
    # Severity (H15) -- the one place every badge, panel, next action, export,
    # graph edge and the publish gate read it from:
    #   critical: same-course, OR a cross-course overlap that blocks students:
    #             >= HARD_OVERLAP_THRESHOLD need both courses and no placed
    #             section pair of the two courses is clash-free (blocks publish)
    #   warning:  every other cross-course overlap with shared demand -- the
    #             students can route round it through sibling sections, or
    #             fewer than HARD_OVERLAP_THRESHOLD of them are blocked
    # "Shared" counts demand (students who NEED both courses), not students
    # seated in both sections, so on its own it cannot say anyone is blocked.
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
            if _parallel_policy_sections(a.course_key, b.course_key):
                shared = 0
            if same_course or shared > 0:
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
                        "severity": "critical" if same_course else "warning",
                        "same_course": same_course,
                        "shared_students": shared if not same_course else None,
                        "blocked_students": None,
                    }
                )

    cross_course = [overlap for overlap in overlaps if not overlap["same_course"]]
    if cross_course and scenario_id is not None:
        section_masks = _placed_section_masks(
            scenario_id, {key for overlap in cross_course for key in overlap["course_keys"]}
        )
        for overlap in cross_course:
            key_a, key_b = (normalize_code(key) for key in overlap["course_keys"])
            blocked = _h15_blocked_students(
                overlap["shared_students"],
                section_masks.get(key_a, []),
                section_masks.get(key_b, []),
            )
            overlap["blocked_students"] = blocked
            if blocked >= HARD_OVERLAP_THRESHOLD:
                overlap["severity"] = "critical"

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

    # Critical overlaps (same-course or blocking students) + instructor clashes
    # block publish.  Overlaps students can route round and room clashes are
    # warnings.
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


@dataclass(frozen=True)
class _SectionTeachers:
    """One request's one-teacher map, read once (:func:`_load_section_teachers`)."""

    teacher_of: Callable[..., tuple[int | None, str] | None]
    same_teacher: Callable[..., bool]
    #: The map the publish check reads (``build_section_instructor_ids``):
    #: the same assignment's ``pr4_map``, built when called. Only a slot
    #: verdict calls it, so the legacy paths never build it.
    publish_map: Callable[[], dict[str, set[int]]]
    #: Each charged teacher's name, by id.
    names: dict[int, str]


def _section_teacher_matcher(scenario_id: int):
    """One teacher per section: ``(teacher_of, same_teacher)`` for one request.

    ``teacher_of(term_section_id, section_key)`` is ``(teacher_id, name)`` for a
    section the one-teacher map charges, ``(None, "")`` for one still to be
    decided, and None for one the map leaves out (no links, or a frozen section
    with no known teacher). ``same_teacher(a, a_name, b, b_name)`` is the clash
    test the manual-placement checks share: two charged sections clash when
    their teacher ids are equal; a section to be decided clashes with nobody;
    when either is left out, the free-text names decide, as they did before
    the rule. (The publish check never matches a charged teacher with a name:
    :func:`_publish_teacher_keys`.)
    """
    teachers = _load_section_teachers(scenario_id)
    return teachers.teacher_of, teachers.same_teacher


def _load_section_teachers(scenario_id: int) -> _SectionTeachers:
    """The one-teacher map in force, read once, as :func:`_section_teacher_matcher`
    gives it, with the charged teachers' names: one assignment, one query for
    the names. ``publish_map()`` builds the map as the publish check reads it
    (``pr4_map`` of the same assignment, no query); the legacy paths never
    call it."""
    from core.models import Instructor
    from core.services.section_teacher_assignment import CHARGED, STATUS_TBD, load_assignment

    assignment = load_assignment(scenario_id)
    names = dict(
        Instructor.objects.filter(
            pk__in={t for t in assignment.teacher.values() if t is not None}
        ).values_list("pk", "full_name")
    )
    by_ts: dict[int, tuple[int | None, str]] = {}
    by_key: dict[str, tuple[int | None, str]] = {}
    for slot in assignment.slots:
        status = assignment.status.get(slot.key)
        teacher = assignment.teacher.get(slot.key)
        if status in CHARGED and teacher is not None:
            entry: tuple[int | None, str] = (teacher, names.get(teacher, ""))
        elif status == STATUS_TBD:
            entry = (None, "")
        else:
            continue
        by_key[slot.key] = entry
        if slot.term_section_id is not None:
            by_ts[slot.term_section_id] = entry

    def teacher_of(term_section_id, section_key=None):
        if term_section_id is not None and term_section_id in by_ts:
            return by_ts[term_section_id]
        return by_key.get(section_key) if section_key is not None else None

    def same_teacher(a, a_name, b, b_name) -> bool:
        if (a is not None and a[0] is None) or (b is not None and b[0] is None):
            return False
        if a is not None and b is not None:
            return a[0] == b[0]
        left = ((a[1] if a is not None else a_name) or "").strip().upper()
        right = ((b[1] if b is not None else b_name) or "").strip().upper()
        return bool(left and right and left == right)

    return _SectionTeachers(teacher_of, same_teacher, assignment.pr4_map, names)


class _PlacementTimeCheck:
    """The room-independent half of :func:`validate_placement`, computed once.

    Student overlaps and instructor clashes depend on the board, the time and
    the section, never on the room. The room half is a lookup in an index of
    every placement whose time overlaps the target (same board and cross
    board), built on first use, so a caller that tries many rooms at one slot
    (the room-candidate preview: one ``Room`` row each) pays for the board
    load, the overlap matrix and the one-teacher map once rather than per room.
    """

    def __init__(
        self,
        board_id: int,
        day: str,
        start_time: str,
        end_time: str,
        term_section_id: int,
        exclude_placement_id: int | None = None,
        ignore_overlap_term_section_ids: set[int] | None = None,
    ) -> None:
        self.board_id = board_id
        self.exclude_placement_id = exclude_placement_id
        items = _load_board_placements(board_id)
        new_mask = _time_mask(day, start_time, end_time)
        self.new_mask = new_mask
        ignored_overlap_term_sections = {
            int(value) for value in (ignore_overlap_term_section_ids or set())
        }

        # Get instructor for the section being placed
        meeting = TermSectionMeeting.objects.filter(term_section_id=term_section_id).first()
        new_instructor = meeting.instructor if meeting else ""

        ts = TermSection.objects.filter(id=term_section_id).first()
        new_requires_room = not bool(ts and section_policy(ts))
        self.online_lookup = online_lookup = OnlineCourseLookup()
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
        self.board_obj = board_obj
        # A room clash counts only for a section that sits in a room of its own.
        self.room_rule_applies = not new_is_online and new_requires_room

        # One teacher per section: the section's own teacher, by id (as the engine
        # and the backstop charge it), not the free-text name of its meetings.
        teacher_of = same_teacher = new_teacher = None
        if board_obj is not None and is_one_teacher_active():
            teacher_of, same_teacher = _section_teacher_matcher(board_obj.scenario_id)
            new_teacher = teacher_of(
                term_section_id, f"{ts.course_key or ts.course_code}_{ts.section}" if ts else None
            )
            if new_teacher is not None:
                new_instructor = new_teacher[1]

        overlaps: list[dict] = []
        instructor_clashes: list[dict] = []
        # Same-board items the target overlaps in time: the room half's first part.
        self._same_board_timed: list[PlacementInfo] = []

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
                ignore_student_overlap = (
                    item.term_section_id in ignored_overlap_term_sections
                    or _parallel_policy_sections(item.course_key, ts_key)
                )
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
                    same_teacher(
                        new_teacher,
                        new_instructor,
                        teacher_of(item.term_section_id, f"{item.course_key}_{item.section}"),
                        item.instructor,
                    )
                    if same_teacher is not None
                    else (
                        new_instructor
                        and item.instructor
                        and new_instructor.strip().upper() == item.instructor.strip().upper()
                    )
                ):
                    instructor_clashes.append(
                        {
                            "id": item.id,
                            "instructor": new_instructor,
                            "section": f"{item.course_code}-{item.section}",
                        }
                    )

                self._same_board_timed.append(item)

        self.overlaps = overlaps
        self.instructor_clashes = instructor_clashes
        critical_overlaps = sum(1 for o in overlaps if o.get("severity") == "critical")
        self.warning_overlaps = sum(1 for o in overlaps if o.get("severity") == "warning")
        self.critical_count = critical_overlaps + len(instructor_clashes)
        self._room_index: dict[str, list[dict]] | None = None

    def _build_room_index(self) -> dict[str, list[dict]]:
        """Every roomed placement the target overlaps in time, keyed by room.

        Same-board items first, in board order, then cross-board placements in
        query order: the order :func:`validate_placement` has always listed
        them in. ``"room"`` is a placeholder the lookup fills with the caller's
        spelling of the room, in its usual key position.
        """
        index: dict[str, list[dict]] = defaultdict(list)
        # Room clash on the same board (exclude blank/UNASSIGNED).
        for item in self._same_board_timed:
            room_key = _room_key(item.room)
            if room_key and not item.is_online:
                index[room_key].append(
                    {
                        "id": item.id,
                        "room": None,
                        "section": f"{item.course_code}-{item.section}",
                        "scope": "same_board",
                    }
                )
        if self.board_obj is None:
            return index
        cross_board_placements = (
            SectionPlacement.objects.filter(board__scenario=self.board_obj.scenario)
            .exclude(board_id=self.board_id)
            .exclude(room="")
            .exclude(room="UNASSIGNED")
            .select_related("board", "term_section")
        )
        if self.exclude_placement_id:
            cross_board_placements = cross_board_placements.exclude(id=self.exclude_placement_id)

        for other in cross_board_placements:
            if section_policy(other.term_section):
                continue
            if self.online_lookup.is_online_course_for_board(
                other.board, other.term_section.course_code
            ):
                continue
            room_key = _room_key(other.room)
            if not room_key:
                continue
            other_mask = _time_mask(other.day, other.start_time, other.end_time)
            if not (self.new_mask & other_mask):
                continue
            index[room_key].append(
                {
                    "id": other.id,
                    "room": None,
                    "section": f"{other.term_section.course_code}-{other.term_section.section}",
                    "scope": "cross_board",
                    "board_id": other.board_id,
                    "board_label": other.board.label,
                    "detail": (
                        f"{other.board.label}: {other.day} {other.start_time}-{other.end_time}"
                    ),
                }
            )
        return index

    def room_clashes(self, room: str) -> list[dict]:
        """The room clashes of placing the target in ``room``: one lookup."""
        room_key = _room_key(room) if self.room_rule_applies else ""
        if not room_key:
            return []
        if self._room_index is None:
            self._room_index = self._build_room_index()
        return [dict(entry, room=room) for entry in self._room_index.get(room_key, ())]

    def result(self, room: str) -> dict:
        """:func:`validate_placement`'s answer for ``room``."""
        room_clashes = self.room_clashes(room)
        critical = self.critical_count
        warning = self.warning_overlaps + len(room_clashes)
        return {
            "valid": critical == 0,
            "overlaps": self.overlaps,
            "instructor_clashes": self.instructor_clashes,
            "room_clashes": room_clashes,
            "critical_count": critical,
            "warning_count": warning,
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

    The room-independent checks and the room lookup live in
    :class:`_PlacementTimeCheck`; a caller trying many rooms at one slot uses
    it directly.
    """
    return _PlacementTimeCheck(
        board_id,
        day,
        start_time,
        end_time,
        term_section_id,
        exclude_placement_id=exclude_placement_id,
        ignore_overlap_term_section_ids=ignore_overlap_term_section_ids,
    ).result(room)


# ── Slot verdict (the Move and Place dialogs) ────────────────────
#
# Both slot previews can say, for each candidate, what the board's Issues and
# the publish check would say about it once saved:
#   - same-board students: H15, decided by the publish check's own test
#     (_h15_blocked_students): critical only when the edit leaves at least
#     HARD_OVERLAP_THRESHOLD students with no clash-free pair of sections,
#     including a move's EFFECT on overlaps it does not touch;
#   - a same-course overlap, a blocked time, a same-board room clash: critical
#     (each blocks publishing);
#   - a same-board teacher clash: critical, the teachers judged by the publish
#     check's keys (_publish_teacher_keys) from the publish check's own map
#     (_VerdictTeachers): the one-teacher map, or, with the links on and one
#     teacher per section off, every teacher linked to a course;
#   - cross-board students: a warning at the threshold, where the publish check
#     warns (cross_board_conflicts), and info below it, as Issues lists them;
#   - a teacher or room also booked on another board: a warning, the teacher by
#     the same keys (the publish check does not look across boards for them,
#     so the verdict must not invent a blocker).
# The legacy fields (tone, counts, evidence, rank) keep their own older rules
# and order, the legacy teacher test included (_section_teacher_matcher, or the
# names). The verdict is built only when a caller asks (include_verdict), so
# the legacy answers and the other callers (safe moves, repair, the move
# outcome) never pay for it. It is built from data the preview has already
# loaded, with one exception: with the links on and one teacher per section
# off (the local default) it reads the course links as the publish check does
# (linked_section_instructor_ids, three queries, once per request). With one
# teacher per section (production) or the links off it adds no query.

_VERDICT_SEVERITIES = ("critical", "warning", "info")
_VERDICT_SEVERITY_RANK = {"ok": 0, "info": 1, "warning": 2, "critical": 3}
#: Item kinds in the order a verdict lists them within one severity.
_VERDICT_KINDS = (
    "time",
    "instructor",
    "room",
    "blocked",
    "softOverlap",
    "instructorElsewhere",
    "roomElsewhere",
    "crossBoard",
    "crossBoardMinor",
)
_VERDICT_ITEMS_SHOWN = 8


def _verdict_other(
    course_code: str,
    section: str,
    board_id: int,
    board_label: str,
    day: str,
    start: str,
    end: str,
) -> dict:
    """The placement a verdict item is about."""
    return {
        "course_code": course_code,
        "section": section,
        "board_id": board_id,
        "board_label": board_label,
        "day": day,
        "start": start,
        "end": end,
    }


def _verdict_item(
    issue_kind: str,
    severity: str,
    source: str,
    *,
    other: dict | None = None,
    room: str | None = None,
    teacher: str | None = None,
    students: int | None = None,
    blocked_students: int | None = None,
) -> dict:
    """One verdict item. ``source`` names the check: the legacy evidence kind
    of the same check (``same_course``, ``students``, ``instructor``, ``room``,
    ``cross_board_room``, ``cross_board_students``), or ``cross_board_instructor``,
    ``blocked_slot`` and ``h15_effect``, which have none. The verdict judges
    by its own rules, so an item need not have a legacy evidence row: an
    ``instructor`` item follows the publish check's teachers, while the
    legacy ``instructor`` evidence keeps its own test. ``meeting`` is the
    index of the meeting it belongs to in a planned pattern, else None."""
    return {
        "issue_kind": issue_kind,
        "severity": severity,
        "source": source,
        "meeting": None,
        "other": other,
        "room": room,
        "teacher": teacher,
        "students": students,
        "blocked_students": blocked_students,
    }


@dataclass
class _CellVerdict:
    """One cell's verdict evidence, before H15 is applied.

    ``items`` are final. Each ``pending`` entry is a same-board student overlap
    ``(shared, other_course, other)``: whether it blocks anyone depends on the
    whole week of the section being moved or placed, so it is resolved per
    candidate (a move) or per pattern (a planned section) by
    :func:`_resolve_cell_verdict`. ``students`` are the distinct students the
    cell's student overlaps count; ``mask`` is the cell's own time.
    """

    mask: int
    items: list[dict] = field(default_factory=list)
    pending: list[tuple[int, str, dict | None]] = field(default_factory=list)
    students: set[int] = field(default_factory=set)


def _resolve_cell_verdict(
    cell: _CellVerdict,
    own_masks: list[int],
    course_masks: dict[str, list[int]],
    *,
    meeting: int | None = None,
) -> list[dict]:
    """The cell's items, its student overlaps judged by H15.

    ``own_masks`` are the weekly masks of every placed section of the course
    once the edit is made (the moved section at its candidate time, or the new
    section's whole pattern); ``course_masks`` every other course's, as
    :func:`_placed_section_masks` gives them. An overlap is ``time``/critical
    when it leaves at least ``HARD_OVERLAP_THRESHOLD`` students with no
    clash-free pair, else ``softOverlap``/warning: the rule of
    :func:`detect_board_conflicts`. The items are new lists and dicts tagged
    with ``meeting``; the cell is shared and never changed.
    """
    from core.services.timetable_overlap import HARD_OVERLAP_THRESHOLD

    items = list(cell.items)
    for shared, course, other in cell.pending:
        blocked = _h15_blocked_students(shared, own_masks, course_masks.get(course, []))
        critical = blocked >= HARD_OVERLAP_THRESHOLD
        items.append(
            _verdict_item(
                "time" if critical else "softOverlap",
                "critical" if critical else "warning",
                "students",
                other=other,
                students=shared,
                blocked_students=blocked,
            )
        )
    if meeting is None:
        return items
    return [{**item, "meeting": meeting} for item in items]


def _slot_verdict(
    items: list[dict], students: int, *, own_section_meeting: bool | None = None
) -> dict:
    """A candidate's verdict: its worst severity, counts over ALL its items,
    and the first eight items (worst first) with how many more there are.

    Items are ordered by severity, then by kind (:data:`_VERDICT_KINDS`), then
    by students, most first; ties keep the order they were found in.
    ``own_section_meeting`` is set on move candidates only.
    """
    ordered = sorted(
        items,
        key=lambda item: (
            _VERDICT_SEVERITIES.index(item["severity"]),
            _VERDICT_KINDS.index(item["issue_kind"]),
            -int(item["students"] or 0),
        ),
    )
    counts = dict.fromkeys(_VERDICT_SEVERITIES, 0)
    for item in items:
        counts[item["severity"]] += 1
    verdict = {
        "severity": next((severity for severity in _VERDICT_SEVERITIES if counts[severity]), "ok"),
        "counts": counts,
        "students": students,
        "items": ordered[:_VERDICT_ITEMS_SHOWN],
        "more": max(0, len(ordered) - _VERDICT_ITEMS_SHOWN),
    }
    if own_section_meeting is not None:
        verdict["own_section_meeting"] = own_section_meeting
    return verdict


def _rank_by_verdict(candidates: list[dict]) -> None:
    """Number the candidates best verdict first (``verdict_rank``, from 1).

    The ranking is a sorted copy: the list keeps its legacy order. A slot the
    section already meets in comes last (the move would be refused), then by
    severity, the critical, warning and info counts, the legacy score, the
    day and the start.
    """

    def key(candidate: dict) -> tuple:
        verdict = candidate["verdict"]
        counts = verdict["counts"]
        return (
            bool(verdict.get("own_section_meeting")),
            _VERDICT_SEVERITY_RANK[verdict["severity"]],
            counts["critical"],
            counts["warning"],
            counts["info"],
            candidate["score"],
            _DAY_INDEX.get(str(candidate["day"]).upper(), 99),
            _to_minutes(str(candidate["start"])),
        )

    for rank, candidate in enumerate(sorted(candidates, key=key), start=1):
        candidate["verdict_rank"] = rank


def _cross_board_student_item(shared: int, other: dict | None) -> dict:
    """Students who need both courses, the other one on another board.

    A warning at ``HARD_OVERLAP_THRESHOLD`` shared students, where the publish
    check warns (``cross_board_conflicts``, never a blocker), and info below
    it, as the board's Issues list it. Both previews' rule.
    """
    from core.services.timetable_overlap import HARD_OVERLAP_THRESHOLD

    at_line = shared >= HARD_OVERLAP_THRESHOLD
    return _verdict_item(
        "crossBoard" if at_line else "crossBoardMinor",
        "warning" if at_line else "info",
        "cross_board_students",
        other=other,
        students=shared,
    )


@dataclass(frozen=True)
class _VerdictTeachers:
    """The slot verdict's teachers: the publish check's keys
    (:func:`_publish_teacher_keys`) over the map the publish check reads
    (:func:`detect_board_conflicts`, ``build_section_instructor_ids``):

    - one teacher per section (production): the one-teacher map, from the
      assignment the preview already loads (``publish_map()`` of
      :func:`_load_section_teachers`); no query;
    - the links on and one teacher per section off (the local default): every
      teacher linked to a section's course, read by
      ``linked_section_instructor_ids``, the code ``build_section_instructor_ids``
      runs in that mode, with the teachers' names from the same query: three
      queries, once per request, only for a verdict;
    - the links off: no map; the meeting rows' names decide, as there.
    """

    link_map: Mapping[str, set[int]]
    by_presence: bool
    #: The charged or linked teachers' names, by id.
    names: Mapping[int, str]
    #: The links on and one teacher per section off: who a section not created
    #: yet will be charged with once it is, ``resolve(course_key, display_code)``
    #: (the map has only the scenario's sections). None in the other modes.
    resolve: Callable[[object, object], set[int]] | None = None

    @classmethod
    def none(cls) -> _VerdictTeachers:
        """No verdict asked for: nothing read."""
        return cls({}, False, {})

    @classmethod
    def load(
        cls, scenario: TimetableScenario, section_teachers: _SectionTeachers | None
    ) -> _VerdictTeachers:
        """One request's verdict teachers. ``section_teachers`` is the request's
        one-teacher map, loaded when that rule is on."""
        from core.services.timetable_pr4_instructor import (
            is_instructor_links_enabled,
            linked_section_instructor_ids,
        )

        if section_teachers is not None:
            return cls(section_teachers.publish_map(), True, section_teachers.names)
        if is_instructor_links_enabled():
            linked = linked_section_instructor_ids(scenario)
            return cls(linked.ids, False, linked.names, linked.resolve)
        return cls.none()

    def keys(self, course_key: str, section: str, instructor: str) -> tuple:
        """A section's keys, ``instructor`` being its first meeting row's name."""
        return _publish_teacher_keys(
            self.link_map, self.by_presence, course_key, section, instructor
        )

    def new_section_keys(self, course_key: str, section: str, display_code: str) -> tuple:
        """The keys of a section not created yet (no meeting row, so no name),
        as the publish check will charge it once it is: with the links on and
        one teacher per section off, its course's linked teachers; otherwise
        by the map (the one-teacher map has every budgeted section)."""
        if self.resolve is not None:
            return tuple(self.resolve(course_key, display_code))
        return self.keys(course_key, section, "")

    def clash(self, mine: tuple, theirs: tuple, their_name: str) -> tuple[bool, str]:
        """Whether two sections share a teacher, and the teacher's name: the
        charged or linked teacher's, or ``their_name`` as written when the
        names match."""
        for key in theirs:
            if key in mine:
                return True, (self.names.get(key, "") if isinstance(key, int) else their_name)
        return False, ""


def _first_meeting_instructor(placement: SectionPlacement) -> str:
    """The free-text name on a placed section's first meeting row (prefetched),
    as :func:`_load_board_placements` reads it."""
    meetings = list(placement.term_section.meetings.all())
    return meetings[0].instructor if meetings else ""


def _other_board_teacher_clash(
    other: SectionPlacement,
    *,
    new_teacher: tuple[int | None, str] | None,
    new_instructor: str,
    teacher_of: Callable | None,
    same_teacher: Callable | None,
) -> tuple[bool, str]:
    """Whether a placement on another board has the section's teacher, and its name.

    ``other`` comes with its section and meetings loaded. Its teacher is the
    one-teacher map's when the rule is on and the map has it, else the
    free-text name on its first meeting row; the section's own is
    ``new_teacher`` / ``new_instructor`` (stripped, upper-case). The planned
    preview's legacy score counts a clash as critical evidence. The slot
    verdict does not use this test: it judges teachers by the publish check's
    keys (:class:`_VerdictTeachers`).
    """
    other_instructor = _first_meeting_instructor(other)
    other_teacher = (
        teacher_of(
            other.term_section_id,
            f"{other.term_section.course_key or other.term_section.course_code}"
            f"_{other.term_section.section}",
        )
        if teacher_of is not None
        else None
    )
    if other_teacher is not None:
        other_instructor = other_teacher[1]
    clash = (
        same_teacher(new_teacher, new_instructor, other_teacher, other_instructor)
        if same_teacher is not None
        else (new_instructor and new_instructor == other_instructor.strip().upper())
    )
    return bool(clash), other_instructor


# ── Direct Student Feasibility ───────────────────────────────────


def preview_placement_slot_candidates(
    placement_id: int, *, include_verdict: bool = False, offer_manual: bool = False
) -> dict:
    """Return student-aware candidate slots for moving a placement.

    The preview is read-only. It scores candidate slots against all placements
    on the same board and direct student pressure across the whole scenario, so
    visitor/cross-group students are included.  The response also includes the
    placement's current impact so the UI can show before/after improvement.

    ``include_verdict`` adds the slot verdict (the Move dialog's): a
    ``verdict`` and a ``verdict_rank`` on every candidate and a
    ``current_verdict`` for the slot the meeting is in now (its direct items
    only; a move has effects, the current slot none). See "Slot verdict" above.
    ``offer_manual`` offers the columns a manual move may use
    (:func:`~core.services.timetable_slot_config.offered_move_columns`): an
    evening online-only window too when the meeting already starts in one.
    Without either, the answer and its order are exactly what they were. A
    section under a course meeting policy is answered by the planned preview,
    whose patterns come from its policy (``offer_manual`` does not apply).

    Warning: that verdict is a PLACE verdict. It judges the section's new
    pattern beside what is placed, with no H15 effects: it does not see that
    the section leaves its old pattern, which can turn untouched overlaps of
    its course critical. Nothing shows it for a move (the Move dialog sends a
    policy section to the pattern chooser, 5e); add the effects before any
    caller does.
    """
    from core.services.timetable_autoplace import (
        DEFAULT_LAB_SLOTS,
        DEFAULT_SLOTS,
        placeable_slots,
    )
    from core.services.timetable_overlap import (
        HARD_OVERLAP_THRESHOLD,
        build_course_students_map,
    )

    placement = SectionPlacement.objects.select_related("board__scenario", "term_section").get(
        id=placement_id
    )
    policy = section_policy(placement.term_section)
    if policy:
        # A place verdict, not a move's: no H15 effects (see the warning above).
        result = preview_planned_section_slot_candidates(
            placement.board_id,
            course_code=placement.term_section.course_code,
            course_key=placement.term_section.course_key,
            section_label=placement.term_section.section,
            exclude_term_section_id=placement.term_section_id,
            include_verdict=include_verdict,
        )
        for candidate in result["candidates"]:
            target = next(
                (row for row in candidate["meetings"] if row["day"] == placement.day),
                candidate["meetings"][0],
            )
            candidate.update(day=target["day"], start=target["start"], end=target["end"])
        rows = list(SectionPlacement.objects.filter(term_section=placement.term_section))
        result.update(
            placement={
                "id": placement.id,
                "course_code": placement.term_section.course_code,
                "board_id": placement.board_id,
                "day": placement.day,
                "start": placement.start_time,
                "end": placement.end_time,
            },
            section_policy=policy_payload(policy, rows),
        )
        return result
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
    if offer_manual:
        from core.services.timetable_slot_config import (
            effective_lab_slots,
            effective_lecture_slots,
            offered_move_columns,
        )

        slots = offered_move_columns(
            effective_lab_slots(scenario) if is_lab else effective_lecture_slots(scenario),
            placement.start_time,
        )
    else:
        slots = placeable_slots(slot_config or (DEFAULT_LAB_SLOTS if is_lab else DEFAULT_SLOTS))
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
    # One teacher per section: compare teachers by id (see validate_placement).
    teacher_of = same_teacher = new_teacher = None
    section_teachers = None
    if is_one_teacher_active():
        section_teachers = _load_section_teachers(scenario.id)
        teacher_of, same_teacher = section_teachers.teacher_of, section_teachers.same_teacher
        new_teacher = teacher_of(
            placement.term_section_id, f"{course_key}_{placement.term_section.section}"
        )
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

    # The slot verdict's inputs, only with include_verdict: from the placements
    # already loaded (no query; O(placements) once per request), and its
    # teachers (_VerdictTeachers: three queries with the links on and one
    # teacher per section off, none otherwise).
    blocked_slots: set[tuple[str, str]] = set()
    section_masks: dict[str, dict[int, int]] = {}
    course_masks: dict[str, list[int]] = {}
    course_placements: dict[str, list[tuple[SectionPlacement, int]]] = defaultdict(list)
    own_rest = 0  # this section's other meetings, on every board
    own_slots: set[tuple[str, str]] = set()  # ...and where it meets on this board
    # The courses a move could turn critical elsewhere (H15 effects): at least
    # HARD_OVERLAP_THRESHOLD students need both, and a clash-free pair of their
    # sections exists now.
    effect_courses: dict[str, tuple[set[int], list[int]]] = {}
    verdict_teachers = _VerdictTeachers.none()
    own_teachers: tuple = ()  # who the publish check charges with this section
    if include_verdict:
        from core.services.timetable_validation import blocked_slot_keys

        verdict_teachers = _VerdictTeachers.load(scenario, section_teachers)
        own_teachers = verdict_teachers.keys(
            course_key,
            placement.term_section.section,
            # Its first meeting row's name, read as the publish check reads it.
            next((item.instructor for item in board_items if item.id == placement.id), ""),
        )
        blocked_slots = blocked_slot_keys(scenario.blocked_slots)
        placement_masks = [
            (other, _time_mask(other.day, other.start_time, other.end_time))
            for other in scenario_placements
        ]
        section_masks = _section_masks_by_course(
            (
                other.term_section_id,
                other.term_section.course_key,
                other.term_section.course_code,
                mask,
            )
            for other, mask in placement_masks
        )
        course_masks = _nonzero_course_masks(section_masks)
        for other, mask in placement_masks:
            other_course = normalize_code(
                other.term_section.course_key or other.term_section.course_code
            )
            course_placements[other_course].append((other, mask))
            if other.term_section_id == placement.term_section_id and other.id != placement.id:
                own_rest |= mask
                if other.board_id == placement.board_id:
                    own_slots.add((other.day, other.start_time))
        masks_before = course_masks.get(course_norm, [])
        for other_course, masks in course_masks.items():
            shared = shared_students(other_course)
            if (
                len(shared) >= HARD_OVERLAP_THRESHOLD
                and _h15_blocked_students(len(shared), masks_before, masks) == 0
            ):
                effect_courses[other_course] = (shared, masks)

    def masks_after(cell_mask: int) -> list[int]:
        """This course's section masks with the moving meeting at ``cell_mask``."""
        sections = dict(section_masks.get(course_norm, {}))
        sections[placement.term_section_id] = own_rest | cell_mask
        return [mask for mask in sections.values() if mask]

    def effect_items(after: list[int]) -> list[dict]:
        """The H15 effects of a move: overlaps it does not touch turned critical.

        A course of ``effect_courses`` that the move leaves with no clash-free
        pair (``after``) blocks its shared students in every overlap of the two
        courses on one board -- any board -- as detect_board_conflicts will then
        say. The moving meeting's own overlaps are direct items, not effects.
        Pairs are enumerated only on such a flip; otherwise this is one H15
        test per effect course (sections x sections masks). Their students are
        already the candidate's: a flip means the moving meeting now meets a
        section of that course, on this board or another.
        """
        items: list[dict] = []
        for other_course, (shared, masks) in effect_courses.items():
            if _h15_blocked_students(len(shared), after, masks) < HARD_OVERLAP_THRESHOLD:
                continue
            for own, own_mask in course_placements[course_norm]:
                if own.id == placement.id:
                    continue
                for other, other_mask in course_placements[other_course]:
                    if other.board_id != own.board_id or not (own_mask & other_mask):
                        continue
                    items.append(
                        _verdict_item(
                            "time",
                            "critical",
                            "h15_effect",
                            other=_verdict_other(
                                other.term_section.course_code,
                                other.term_section.section,
                                other.board_id,
                                other.board.label,
                                other.day,
                                other.start_time,
                                other.end_time,
                            ),
                            students=len(shared),
                            blocked_students=len(shared),
                        )
                    )
        return items

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

    def score_slot(day: str, start: str, end: str) -> tuple[dict, _CellVerdict | None]:
        """The legacy impact of the meeting at this slot, and with
        include_verdict its verdict evidence (H15 still to apply)."""
        candidate_mask = _time_mask(day, start, end)
        cell = _CellVerdict(candidate_mask) if include_verdict else None
        # The verdict's same-board teacher clashes, by section and time: a copy
        # of one on another board is the same class, not a second clash.
        teacher_sessions: set[tuple[int, str, str, str]] = set()
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

            item_other = (
                _verdict_other(
                    item.course_code,
                    item.section,
                    placement.board_id,
                    placement.board.label,
                    item.day,
                    item.start_time,
                    item.end_time,
                )
                if cell is not None
                else None
            )
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
                if cell is not None:
                    cell.items.append(
                        _verdict_item("time", "critical", "same_course", other=item_other)
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
                    if cell is not None:
                        cell.pending.append(
                            (len(shared), normalize_code(item.course_key), item_other)
                        )

            item_teacher = (
                teacher_of(item.term_section_id, f"{item.course_key}_{item.section}")
                if teacher_of is not None
                else None
            )
            if (
                same_teacher(new_teacher, new_instructor, item_teacher, item.instructor)
                if same_teacher is not None
                else (
                    new_instructor
                    and item.instructor
                    and new_instructor == item.instructor.strip().upper()
                )
            ):
                instructor_critical += 1
                candidate_evidence.append(
                    evidence(
                        "instructor",
                        "critical",
                        f"Instructor clash: {item_teacher[1] if item_teacher else item.instructor}",
                        f"{item.course_code}-{item.section} is at {item.day} {item.start_time}-{item.end_time}",
                    )
                )
            if cell is not None:
                # The verdict's teachers are the publish check's, not the legacy test's.
                clash, teacher = verdict_teachers.clash(
                    own_teachers,
                    verdict_teachers.keys(item.course_key, item.section, item.instructor),
                    item.instructor,
                )
                if clash:
                    teacher_sessions.add(
                        (item.term_section_id, item.day, item.start_time, item.end_time)
                    )
                    cell.items.append(
                        _verdict_item(
                            "instructor",
                            "critical",
                            "instructor",
                            other=item_other,
                            teacher=teacher,
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
                # A room clash on the board blocks publishing
                # (``board_room_conflicts``): critical in the verdict.
                if cell is not None:
                    cell.items.append(
                        _verdict_item(
                            "room", "critical", "room", other=item_other, room=placement.room
                        )
                    )

        for other in scenario_placements:
            if other.id == placement.id or other.board_id == placement.board_id:
                continue
            other_mask = _time_mask(other.day, other.start_time, other.end_time)
            if not (candidate_mask & other_mask):
                continue
            elsewhere = (
                _verdict_other(
                    other.term_section.course_code,
                    other.term_section.section,
                    other.board_id,
                    other.board.label,
                    other.day,
                    other.start_time,
                    other.end_time,
                )
                if cell is not None
                else None
            )
            # The section's own copy at this very slot (a shared section) is
            # the same class: in the verdict it neither holds the room nor
            # double-books the teacher (the legacy evidence still lists it).
            same_class = cell is not None and _same_class(
                other, placement.term_section_id, day, start, end
            )

            if (
                room_norm
                and not section_policy(other.term_section)
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
                if cell is not None and not same_class:
                    cell.items.append(
                        _verdict_item(
                            "roomElsewhere",
                            "warning",
                            "cross_board_room",
                            other=elsewhere,
                            room=placement.room,
                        )
                    )

            if cell is not None and not same_class:
                # The legacy move score never looked at teachers on other boards;
                # the verdict does, by the publish check's keys, as a warning.
                other_name = _first_meeting_instructor(other)
                clash, other_teacher = verdict_teachers.clash(
                    own_teachers,
                    verdict_teachers.keys(
                        other.term_section.course_key or other.term_section.course_code,
                        other.term_section.section,
                        other_name,
                    ),
                    other_name,
                )
                session = (other.term_section_id, other.day, other.start_time, other.end_time)
                if clash and session not in teacher_sessions:
                    teacher_sessions.add(session)
                    cell.items.append(
                        _verdict_item(
                            "instructorElsewhere",
                            "warning",
                            "cross_board_instructor",
                            other=elsewhere,
                            teacher=other_teacher,
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
            if cell is not None:
                cell.items.append(_cross_board_student_item(len(shared), elsewhere))

        affected_students = affected_same_board | affected_cross_board
        if cell is not None:
            cell.students = affected_students
            # Flagged, never removed: a blocked time blocks publishing.
            if (day, start) in blocked_slots:
                cell.items.append(_verdict_item("blocked", "critical", "blocked_slot"))
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
        }, cell

    current_impact, current_cell = score_slot(
        placement.day, placement.start_time, placement.end_time
    )

    candidates: list[dict] = []
    for day in days:
        for slot in slots:
            start = str(slot.get("start", ""))
            end = str(slot.get("end", ""))
            if not start or not end:
                continue
            if day == placement.day and start == placement.start_time:
                continue

            impact, cell = score_slot(day, start, end)
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
            if cell is not None:
                after = masks_after(cell.mask)
                candidates[-1]["verdict"] = _slot_verdict(
                    _resolve_cell_verdict(cell, after, course_masks) + effect_items(after),
                    len(cell.students),
                    own_section_meeting=(day, start) in own_slots,
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

    result = {
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
    if current_cell is not None:
        _rank_by_verdict(candidates)
        # Where the meeting is now: its direct items (a move's effects are
        # what changes, and nothing changes here).
        result["current_verdict"] = _slot_verdict(
            _resolve_cell_verdict(current_cell, masks_after(current_cell.mask), course_masks),
            len(current_cell.students),
        )
    return result


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
    exclude_term_section_id: int | None = None,
    include_verdict: bool = False,
) -> dict:
    """Return ranked target slots for creating a planned missing section.

    This is the read-only companion to ``tw_placement_create_planned_view``.
    It lets the UI guide the advisor before any row is created, using the
    same student-overlap and timetable-quality signals as placement moves.

    ``include_verdict`` adds the slot verdict (the Place dialog's) to every
    candidate returned, with a ``verdict_rank`` among them: items per meeting
    (``meeting`` is its index in ``meetings``; None for a one-meeting
    candidate), H15 judged for the whole pattern, blocked meetings flagged. A
    new section has no room yet and adds a clash-free option rather than
    removing one, so there are no room items and no effects. Without it the
    answer and its order are exactly what they were.
    """
    from core.services.timetable_autoplace import (
        DEFAULT_LAB_SLOTS,
        DEFAULT_SLOTS,
        generate_meeting_options,
        get_meeting_pattern,
        placeable_slots,
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
    policy = get_course_meeting_policy(code, key)
    requested_kind = normalise_course_code(kind or "")
    include_lab = requested_kind in {"LAB", "LABS"}
    include_lecture = requested_kind not in {"LAB", "LABS"}
    slot_sets: list[tuple[str, list[dict]]] = []
    if include_lecture:
        slot_sets.append(("lect", placeable_slots(scenario.slot_config or DEFAULT_SLOTS)))
    if include_lab or not requested_kind:
        slot_sets.append(("lab", placeable_slots(scenario.lab_slot_config or DEFAULT_LAB_SLOTS)))

    existing_section = (
        TermSection.objects.filter(pk=exclude_term_section_id).prefetch_related("meetings").first()
        if exclude_term_section_id is not None
        else (
            TermSection.objects.filter(scenario=scenario, course_key=key, section=section)
            .prefetch_related("meetings")
            .first()
        )
    )
    existing_meetings = list(existing_section.meetings.all()) if existing_section else []
    new_instructor = (existing_meetings[0].instructor if existing_meetings else "").strip().upper()
    # One teacher per section: the planned section's own teacher (a budget
    # label has one before it is placed), compared by id (see validate_placement).
    teacher_of = same_teacher = new_teacher = None
    section_teachers = None
    if is_one_teacher_active():
        section_teachers = _load_section_teachers(scenario.id)
        teacher_of, same_teacher = section_teachers.teacher_of, section_teachers.same_teacher
        new_teacher = teacher_of(
            existing_section.id if existing_section else None,
            f"{existing_section.course_key or existing_section.course_code}_{existing_section.section}"
            if existing_section
            else f"{key}_{section}",
        )

    board_items = [
        row
        for row in _load_board_placements(board.id)
        if row.term_section_id != exclude_term_section_id
    ]
    scenario_placements = list(
        SectionPlacement.objects.filter(board__scenario=scenario)
        .exclude(term_section_id=exclude_term_section_id)
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

    # The slot verdict's inputs, only with include_verdict: from the placements
    # already loaded (no query), the blocked times and every placed course's
    # section masks, this course's being the existing sections the new one
    # joins; and its teachers (_VerdictTeachers: three queries with the links
    # on and one teacher per section off, none otherwise).
    blocked_slots: set[tuple[str, str]] = set()
    course_masks: dict[str, list[int]] = {}
    verdict_teachers = _VerdictTeachers.none()
    # Who the publish check charges with the section: the one it has now (with
    # one teacher per section, creating it may name another: name_new_section).
    own_teachers: tuple = ()
    if include_verdict:
        from core.services.course_identity import course_display_code
        from core.services.timetable_validation import blocked_slot_keys

        verdict_teachers = _VerdictTeachers.load(scenario, section_teachers)
        if existing_section is not None:
            own_teachers = verdict_teachers.keys(
                existing_section.course_key or existing_section.course_code,
                existing_section.section,
                existing_meetings[0].instructor if existing_meetings else "",
            )
        else:
            # Not created yet: create_planned_section_placements stores ``code``
            # as its course code and number, and writes no teacher's name.
            own_teachers = verdict_teachers.new_section_keys(
                key, section, course_display_code(key, code, code)
            )
        blocked_slots = blocked_slot_keys(scenario.blocked_slots)
        course_masks = _nonzero_course_masks(
            _section_masks_by_course(
                (
                    other.term_section_id,
                    other.term_section.course_key,
                    other.term_section.course_code,
                    _time_mask(other.day, other.start_time, other.end_time),
                )
                for other in scenario_placements
            )
        )
    own_masks = course_masks.get(normalize_code(key), [])

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

    # One request's memo: a cell is scored once however many patterns use it
    # (a two-meeting course reuses each cell in a pattern per other day and
    # column). The kind is part of the key: a lecture and a lab column can share
    # a window and score differently. Callers never change what it returns.
    cell_scores: dict[tuple[str, str, str, str], tuple[dict, _CellVerdict | None]] = {}

    def score_slot(
        slot_kind: str, day: str, start: str, end: str
    ) -> tuple[dict, _CellVerdict | None]:
        cell_key = (slot_kind, day, start, end)
        if cell_key not in cell_scores:
            cell_scores[cell_key] = score_cell(slot_kind, day, start, end)
        return cell_scores[cell_key]

    def score_cell(
        slot_kind: str, day: str, start: str, end: str
    ) -> tuple[dict, _CellVerdict | None]:
        """The legacy score of one meeting at this cell, and with
        include_verdict its verdict evidence (H15 still to apply)."""
        candidate_mask = _time_mask(day, start, end)
        cell = _CellVerdict(candidate_mask) if include_verdict else None
        affected_same_board: set[int] = set()
        affected_cross_board: set[int] = set()
        candidate_evidence: list[dict] = []
        overlap_critical = 0
        overlap_warning = 0
        instructor_critical = 0
        instructor_sessions: set[tuple[int, str, str, str]] = set()
        # The verdict's same-board teacher clashes, by section and time: a copy
        # of one on another board is the same class, not a second clash.
        verdict_sessions: set[tuple[int, str, str, str]] = set()
        cross_critical = 0
        cross_warning = 0

        for item in board_items:
            if not (candidate_mask & item.mask):
                continue
            item_other = (
                _verdict_other(
                    item.course_code,
                    item.section,
                    board.id,
                    board.label,
                    item.day,
                    item.start_time,
                    item.end_time,
                )
                if cell is not None
                else None
            )
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
                if cell is not None:
                    cell.items.append(
                        _verdict_item("time", "critical", "same_course", other=item_other)
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
                    if cell is not None:
                        cell.pending.append(
                            (len(shared), normalize_code(item.course_key), item_other)
                        )

            item_teacher = (
                teacher_of(item.term_section_id, f"{item.course_key}_{item.section}")
                if teacher_of is not None
                else None
            )
            if (
                same_teacher(new_teacher, new_instructor, item_teacher, item.instructor)
                if same_teacher is not None
                else (
                    new_instructor
                    and item.instructor
                    and new_instructor == item.instructor.strip().upper()
                )
            ):
                instructor_critical += 1
                instructor_sessions.add(
                    (item.term_section_id, item.day, item.start_time, item.end_time)
                )
                candidate_evidence.append(
                    evidence(
                        "instructor",
                        "critical",
                        f"Instructor clash: {item_teacher[1] if item_teacher else item.instructor}",
                        f"{item.course_code}-{item.section} is at {item.day} {item.start_time}-{item.end_time}",
                    )
                )
            if cell is not None:
                # The verdict's teachers are the publish check's, not the legacy test's.
                clash, teacher = verdict_teachers.clash(
                    own_teachers,
                    verdict_teachers.keys(item.course_key, item.section, item.instructor),
                    item.instructor,
                )
                if clash:
                    verdict_sessions.add(
                        (item.term_section_id, item.day, item.start_time, item.end_time)
                    )
                    cell.items.append(
                        _verdict_item(
                            "instructor",
                            "critical",
                            "instructor",
                            other=item_other,
                            teacher=teacher,
                        )
                    )

        for other in scenario_placements:
            if other.board_id == board.id:
                continue
            other_mask = _time_mask(other.day, other.start_time, other.end_time)
            if not (candidate_mask & other_mask):
                continue
            elsewhere = (
                _verdict_other(
                    other.term_section.course_code,
                    other.term_section.section,
                    other.board_id,
                    other.board.label,
                    other.day,
                    other.start_time,
                    other.end_time,
                )
                if cell is not None
                else None
            )
            identity = (other.term_section_id, other.day, other.start_time, other.end_time)
            clash, other_instructor = _other_board_teacher_clash(
                other,
                new_teacher=new_teacher,
                new_instructor=new_instructor,
                teacher_of=teacher_of,
                same_teacher=same_teacher,
            )
            if clash and identity not in instructor_sessions:
                instructor_sessions.add(identity)
                instructor_critical += 1
                candidate_evidence.append(
                    evidence(
                        "instructor",
                        "critical",
                        f"Instructor clash: {other_instructor}",
                        f"{other.board.label}: {other.term_section.course_code}-{other.term_section.section} is at {other.day} {other.start_time}-{other.end_time}",
                    )
                )
            if cell is not None:
                # The publish check does not block a teacher on two boards at
                # once, so the verdict warns (the legacy score counts it
                # critical), the teachers judged by the publish check's keys.
                other_name = _first_meeting_instructor(other)
                clash, other_teacher = verdict_teachers.clash(
                    own_teachers,
                    verdict_teachers.keys(
                        other.term_section.course_key or other.term_section.course_code,
                        other.term_section.section,
                        other_name,
                    ),
                    other_name,
                )
                if clash and identity not in verdict_sessions:
                    verdict_sessions.add(identity)
                    cell.items.append(
                        _verdict_item(
                            "instructorElsewhere",
                            "warning",
                            "cross_board_instructor",
                            other=elsewhere,
                            teacher=other_teacher,
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
            if cell is not None:
                cell.items.append(_cross_board_student_item(len(shared), elsewhere))

        affected_students = affected_same_board | affected_cross_board
        if cell is not None:
            cell.students = affected_students
            # Flagged per meeting, never removed: a blocked time blocks publishing.
            if (day, start) in blocked_slots:
                cell.items.append(_verdict_item("blocked", "critical", "blocked_slot"))
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
        }, cell

    def candidate_verdict(meetings: list[dict], *, per_meeting: bool) -> dict:
        """The verdict of placing the section at ``meetings`` (each with its
        kind, day, start and end), from the memo. H15 is judged with the new
        section's whole week beside the course's existing sections (per
        pattern, never per cell); ``per_meeting`` tags each item with the index
        of its meeting."""
        cells: list[_CellVerdict] = []
        pattern_mask = 0
        for row in meetings:
            cell = score_slot(row["kind"], row["day"], row["start"], row["end"])[1]
            assert cell is not None  # include_verdict built it
            cells.append(cell)
            pattern_mask |= cell.mask
        masks = [*own_masks, pattern_mask]
        items = [
            item
            for index, cell in enumerate(cells)
            for item in _resolve_cell_verdict(
                cell, masks, course_masks, meeting=index if per_meeting else None
            )
        ]
        students: set[int] = set()
        for cell in cells:
            students |= cell.students
        return _slot_verdict(items, len(students))

    def meeting_kind(start: str, end: str) -> str:
        if policy:
            return "lect"
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
            slot_score, _cell = score_slot(kind_, day, start, end)
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
        options = (
            meeting_options_for_policy(
                policy,
                blocked_slots=scenario.blocked_slots or [],
                slots=[
                    *placeable_slots(scenario.slot_config or DEFAULT_SLOTS),
                    *placeable_slots(scenario.lab_slot_config or DEFAULT_LAB_SLOTS),
                ],
            )
            if policy
            else generate_meeting_options(
                pattern,
                placeable_slots(scenario.slot_config or DEFAULT_SLOTS),
                placeable_slots(scenario.lab_slot_config or DEFAULT_LAB_SLOTS),
            )
        )
        candidates = [score_pattern(option, idx) for idx, option in enumerate(options, start=1)]
        if policy:
            for candidate in candidates:
                candidate["pattern_id"] = policy.pattern_for(candidate["meetings"]).pattern_id
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
        if include_verdict:
            for candidate in limited:
                candidate["verdict"] = candidate_verdict(candidate["meetings"], per_meeting=True)
            _rank_by_verdict(limited)
        return {
            "section_policy": policy_payload(policy) if policy else None,
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
                # A copy: the memo's own dict must not take a rank or a badge.
                candidates.append(dict(score_slot(slot_kind, day, start, end)[0]))

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
    if include_verdict:
        for candidate in limited:
            candidate["verdict"] = candidate_verdict([candidate], per_meeting=False)
        _rank_by_verdict(limited)
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


def _drop_stale_workspace_meetings(term_section: TermSection, scenario_id: int) -> None:
    """Clear the meeting rows an earlier section of this label left behind.

    Removing a placement from the board leaves the section and its
    ``TermSectionMeeting`` rows in place. Creating the same label again reuses
    the section, so the old pattern's rows would pile up beside the new
    pattern's. They are dropped only for a section the workspace made itself
    (``tw_planned``, or ``tw_auto`` from the engines, which label ``S{n}``
    too), owned by this scenario, and on no board of ANY scenario: its rows
    belong to the section, not to a scenario, so a placement from another
    scenario's board keeps them. Registrar, imported and scraped sections are
    never touched. The workspace's own rows carry a derived teacher name (the
    one-teacher map, or the primary-instructor link; a pin lives in
    ``SectionInstructor``) and ``name_new_section`` writes it back.

    The reused section keeps its ``available_capacity`` and ``course_name``:
    ``get_or_create`` sets them only when the section is first made.
    """

    if term_section.source_tag not in WORKSPACE_SECTION_SOURCE_TAGS:
        return
    if term_section.scenario_id != scenario_id:
        return
    if SectionPlacement.objects.filter(term_section=term_section).exists():
        return
    TermSectionMeeting.objects.filter(term_section=term_section).delete()


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
    policy = get_course_meeting_policy(code, key)
    if policy:
        assert_policy_rows(policy, meeting_rows)
    if required_meetings > 1 and len(meeting_rows) != required_meetings:
        raise ValueError(f"{code} needs a complete {required_meetings}-meeting section pattern.")
    if required_meetings <= 1 and len(meeting_rows) != 1:
        raise ValueError(f"{code} only needs one planned meeting target.")

    # Same shape as the drag-drop path: validate_placement below is board-scoped
    # and free-text-instructor keyed, so it cannot see a cross-board instructor
    # clash. The scenario-wide backstop after the write covers what it misses.
    from core.services.timetable_instructor_backstop import (
        clashes_involving,
        section_id_for,
        verify_persisted_scenario,
    )

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
                "source_tag": PLANNED_SECTION_SOURCE_TAG,
            },
        )
        if not _created:
            _drop_stale_workspace_meetings(term_section, board.scenario_id)
        placements: list[SectionPlacement] = []
        validations = []
        if policy:
            placements = create_policy_section_bundle(board, term_section, meetings=meeting_rows)
            validations = [
                validate_placement(
                    board_id=board.id,
                    day=row.day,
                    start_time=row.start_time,
                    end_time=row.end_time,
                    room=row.room,
                    term_section_id=term_section.id,
                    exclude_placement_id=row.id,
                )
                for row in placements
            ]
        for row in [] if policy else meeting_rows:
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

    if is_one_teacher_active():
        # One teacher per section: the planned section gets a teacher it does
        # not clash with, when there is one, and shows that teacher's name.
        from core.services.section_teacher_assignment import name_new_section

        name_new_section(board.scenario_id, term_section, board)

    # Populate involving_this_section HERE rather than at the view, so every
    # caller gets the actionable field without re-deriving the section_id
    # convention (the two drag-drop views each do it themselves; this is the
    # form to copy).
    backstop = verify_persisted_scenario(board.scenario_id, context="planned_section_create")
    backstop["involving_this_section"] = clashes_involving(backstop, section_id_for(term_section))

    return {
        "board": board,
        "term_section": term_section,
        "placements": placements,
        "validations": validations,
        "instructor_clash_backstop": backstop,
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


# The most rooms one preview lists, best first (the current room is listed
# beyond it when it ranks past it).
_ROOM_CANDIDATE_LIMIT = 60


def _room_choice_audit(
    placement: SectionPlacement,
    *,
    day: str,
    start: str,
    end: str,
    rooms: list[Room],
    online_lookup: OnlineCourseLookup,
    budget_maps: dict[str, dict] | None,
    board_gender: Callable[[int], str],
) -> tuple[dict, MeetingRoomAudit | None]:
    """``target.audit``, and the quality audit's rule for each room choice.

    The meeting is the one the save leaves the placement in. A room-only save
    (the target is the current slot, as the move view decides) moves the copies
    in the placement's room (``_copies_moving_with``) and keeps the others; a
    slot change moves the placement alone into the copies already at the
    target. ``None`` (with the reason in ``target.audit``) when the audit
    cannot judge this meeting.
    """
    from core.services import timetable_room_audit as rule

    def not_assessed(reason: str) -> tuple[dict, None]:
        return {"assessed": False, "reason": reason}, None

    # The rows already read, in capacity order: the order only matters for a
    # repeated code, and a repeated code is not assessed at all.
    rows_by_code, duplicates = rule.audit_room_inventory(rooms)
    if duplicates:
        return not_assessed("duplicate_room_codes")
    if rule.audit_skips(placement, online_lookup):
        return not_assessed("online")
    room_only = (day, start, end) == (
        str(placement.day).strip().upper(),
        str(placement.start_time).strip(),
        str(placement.end_time).strip(),
    )
    key = (
        rule.meeting_key(placement)
        if room_only
        else rule.meeting_key(placement, day=day, start=start, end=end)
    )
    if key[1] not in rule.AUDIT_DAYS:
        return not_assessed("unknown_day")
    try:
        if rule.to_min(key[3]) <= rule.to_min(key[2]):
            return not_assessed("invalid_interval")
    except rule.ExactRoomingError:
        return not_assessed("invalid_interval")

    copies = list(
        SectionPlacement.objects.filter(
            board__scenario_id=placement.board.scenario_id,
            term_section_id=placement.term_section_id,
        )
        .select_related("board", "term_section")
        .order_by("id")
    )
    this = next(row for row in copies if row.pk == placement.pk)
    others = [
        row
        for row in copies
        if row.pk != this.pk
        and rule.meeting_key(row) == key
        and not rule.audit_skips(row, online_lookup)
    ]
    if room_only:
        slot = (this.day, this.start_time, this.end_time)
        moving = _copies_moving_with(
            this, [row for row in copies if (row.day, row.start_time, row.end_time) == slot]
        )
    else:
        moving = [this]
    rows_after = sorted([this, *others], key=lambda row: row.pk)
    moving_ids = {row.pk for row in moving}
    kept = [row for row in rows_after if row.pk not in moving_ids]
    if budget_maps is None:
        from core.services.timetable_rooming import (
            _build_rooming_budget_maps,
            get_capacity_buffer,
        )

        budget_maps = _build_rooming_budget_maps(
            list(ScenarioSectionBudget.objects.filter(scenario_id=placement.board.scenario_id)),
            get_capacity_buffer(),
        )
    try:
        need = rule.meeting_room_need(
            rows_after,
            day=key[1],
            start_hhmm=key[2],
            end_hhmm=key[3],
            budget_maps=budget_maps,
            board_gender=board_gender,
        )
    except rule.ExactRoomingError:
        return not_assessed("boards_disagree_on_gender")
    boards = dict(sorted({row.board_id: row.board for row in rows_after}.items()))
    programmes = {board_id: rule.board_programmes(board) for board_id, board in boards.items()}
    audit = rule.MeetingRoomAudit(
        need,
        rows_by_code,
        kept_rooms=[row.room for row in kept],
        boards=list(programmes.items()),
    )
    return {
        "assessed": True,
        "save": "room_only" if room_only else "slot_change",
        "required_type": need.required_type,
        "required_capacity": need.required_capacity,
        "capacity_source": need.capacity_source,
        "required_gender": need.required_gender,
        "boards": [
            {
                "board_id": board_id,
                "board_label": board.label,
                "programmes": sorted(programmes[board_id]),
            }
            for board_id, board in boards.items()
        ],
        "placement_ids": [row.pk for row in rows_after],
        "moving_placement_ids": sorted(moving_ids),
        "kept": [
            {
                "placement_id": row.pk,
                "board_id": row.board_id,
                "board_label": row.board.label,
                "room": row.room,
            }
            for row in kept
        ],
        "current": audit.choice(placement.room) if _room_key(placement.room) else None,
    }, audit


def preview_placement_room_candidates(
    placement_id: int,
    *,
    day: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    ignore_overlap_term_section_ids: set[int] | None = None,
    include_audit: bool = False,
) -> dict:
    """Return room candidates for assigning a placement at a target slot.

    ``include_audit`` adds what the quality audit (Issues' room rows) will say
    about each room once it is saved: ``target.audit`` and ``candidates[].audit``
    (``_room_choice_audit``), and lists past the cap every room the audit does
    not refuse for gender. Every such answer carries ``target.audit``: a meeting
    that takes no room (a policy course, a course online on this board) is
    ``{"assessed": false, "reason": ...}``. Without it the answer is unchanged.
    """
    from core.services.timetable_room_audit import board_gender_memo
    from core.services.timetable_rooming import (
        _build_rooming_budget_maps,
        get_capacity_buffer,
        room_type_for_placement,
    )

    placement = SectionPlacement.objects.select_related("board__scenario", "term_section").get(
        id=placement_id
    )
    target_day = str(day or placement.day).strip().upper()
    target_start = str(start_time or placement.start_time).strip()
    target_end = str(end_time or placement.end_time).strip()
    term_section = placement.term_section
    board = placement.board
    policy = section_policy(term_section)

    def without_rooms(answer: dict, reason: str) -> dict:
        # A meeting that takes no room: nothing for the audit to judge, and an
        # audit answer still says so (``target.audit``), as every one does.
        if include_audit:
            answer["target"]["audit"] = {"assessed": False, "reason": reason}
        return answer

    if policy and not policy.uses_shared_room:
        no_room = {
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
                "required_capacity": 0,
                "required_type": "lecture",
                "required_gender": "",
                "is_online": False,
                "uses_shared_room": False,
                "room_required": False,
                "no_room_required": True,
            },
            "candidates": [],
            "summary": {
                "total": 0,
                "available": 0,
                "clean": 0,
                "blocked": 0,
                "is_online": False,
                "uses_shared_room": False,
                "room_required": False,
                "no_room_required": True,
            },
        }
        return without_rooms(no_room, "no_room_required")
    online_lookup = OnlineCourseLookup()
    is_online = online_lookup.is_online_course_for_board(board, term_section.course_code)
    if is_online:
        online = {
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
        return without_rooms(online, "online")
    required_capacity = int(
        term_section.registered_count or term_section.available_capacity or board.target_size or 0
    )
    # One budget read for the room type and the audit's seat need (the room type
    # reads none for a policy course), and one gender read per board.
    budget_maps = (
        None
        if get_course_meeting_policy(
            term_section.course_code, term_section.course_key, term_section.course_number
        )
        else _build_rooming_budget_maps(
            list(ScenarioSectionBudget.objects.filter(scenario=board.scenario)),
            get_capacity_buffer(),
        )
    )
    board_gender = board_gender_memo()
    required_type = room_type_for_placement(
        placement,
        start_time=target_start,
        end_time=target_end,
        budget_maps=budget_maps,
    )
    required_gender = _section_gender(term_section.section) or board_gender(board.id)

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
        # Every copy of the section at this very slot (the query is already this
        # scenario) is skipped: the same section at the same time is the same
        # class, not another class holding the room. Choosing the room of a copy
        # that sits in another room makes the copies agree, and the move itself
        # takes only the copies in this placement's room with it
        # (assign_physical_meeting_room). The same section at another time is
        # another class, and stays an occupant.
        if _same_class(other, placement.term_section_id, target_day, target_start, target_end):
            continue
        if section_policy(other.term_section):
            continue
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
                "term_section_id": other.term_section_id,
                "section": f"{other.term_section.course_code}-{other.term_section.section}",
                "day": other.day,
                "start": other.start_time,
                "end": other.end_time,
            }
        )

    # Everything but the room clash is the same for every room at this slot:
    # checked once, then one index lookup per room (validate_placement's answer
    # for that room, without re-reading the board for each of ~80 rooms).
    time_check = _PlacementTimeCheck(
        board.id,
        target_day,
        target_start,
        target_end,
        term_section.id,
        exclude_placement_id=placement.id,
        ignore_overlap_term_section_ids=ignore_overlap_term_section_ids,
    )

    candidates: list[dict] = []
    rooms = list(Room.objects.all().order_by("capacity", "room_code", "section"))
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

        validation = time_check.result(room_code)

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

    target_audit: dict | None = None
    audit: MeetingRoomAudit | None = None
    if include_audit:
        target_audit, audit = _room_choice_audit(
            placement,
            day=target_day,
            start=target_start,
            end=target_end,
            rooms=rooms,
            online_lookup=online_lookup,
            budget_maps=budget_maps,
            board_gender=board_gender,
        )
        if audit is not None:
            for candidate in candidates:
                candidate["audit"] = audit.choice(candidate["room_code"])

    candidates.sort(
        key=lambda c: (
            c["score"],
            abs(c["capacity_slack"]) if c["available"] else 99999,
            c["room_code"],
            c["section"],
        )
    )
    listed = candidates[:_ROOM_CANDIDATE_LIMIT]
    # The placement's current room is always listed, even when it ranks past the
    # cap (a big hall is a poor fit for a small section): every candidate that
    # carries its code is appended, in rank order, when none made the cut.
    current_key = _room_key(placement.room)
    if current_key and not any(_room_key(c["room_code"]) == current_key for c in listed):
        listed += [
            c
            for c in candidates[_ROOM_CANDIDATE_LIMIT:]
            if _room_key(c["room_code"]) == current_key
        ]
    if audit is not None:
        # The legacy score ranks a fitting room in use below type and gender
        # misfits, so the cap could hide a room the dialog may offer. Past it,
        # every room the audit does not refuse for gender follows, in rank order.
        shown = {id(c) for c in listed}
        listed += [
            c for c in candidates if id(c) not in shown and "gender" not in c["audit"]["reasons"]
        ]
    result = {
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
        "candidates": listed,
        "summary": {
            "total": len(candidates),
            "available": sum(1 for c in candidates if c["available"]),
            "clean": sum(1 for c in candidates if c["slot_clean"]),
            "blocked": sum(1 for c in candidates if not c["available"]),
            "is_online": False,
        },
    }
    if include_audit:
        result["target"]["audit"] = target_audit
    return result


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
    accept: Callable[[dict], bool] | None = None,
) -> dict | None:
    current = _room_key(current_room)
    target = preview.get("target") or {}
    for room in preview.get("candidates") or []:
        room_code = _room_key(room.get("room_code"))
        if not room_code or room_code == current:
            continue
        if not _candidate_is_clean_room(room):
            continue
        if accept is not None and not accept(room):
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
    placements = list(
        SectionPlacement.objects.filter(board__scenario=scenario)
        .select_related("board", "term_section")
        .order_by("board__display_order", "board__label", "day", "start_time", "id")
    )
    reserved: set[str] = set()
    assignments: list[dict] = []
    skipped: list[dict] = []
    total_unassigned = 0
    max_items = max(1, min(100, int(limit or 20)))
    # A lock freezes days and times, not the room, so locked rows are roomed in
    # place. A section another scenario also places stays fully frozen here.
    external_ids = external_shared_section_ids(scenario.id)
    # One physical class at one slot gets one room: the unroomed copies of a
    # shared section are roomed together (assign_physical_meeting_room), and
    # the class is decided once, by its first copy: None once roomed, else the
    # reason it was skipped, which its other copies then share.
    rooms_by_code = _rooms_by_code()
    decided: dict[tuple, str | None] = {}
    deferred: set[tuple] = set()  # classes left for another batch

    for placement in placements:
        if section_policy(placement.term_section):
            continue
        current_room = _room_key(placement.room)
        if current_room and current_room != "UNASSIGNED":
            continue
        total_unassigned += 1
        if placement.term_section_id in external_ids:
            skipped.append(
                {
                    "placement_id": placement.id,
                    "reason": "externally_owned",
                    "label": f"{placement.term_section.course_code}-{placement.term_section.section}",
                }
            )
            continue
        group_key = (
            placement.term_section_id,
            placement.day,
            placement.start_time,
            placement.end_time,
        )
        if group_key in decided:
            reason = decided[group_key]
            if reason is not None:
                skipped.append(
                    {
                        "placement_id": placement.id,
                        "reason": reason,
                        "label": f"{placement.term_section.course_code}-{placement.term_section.section}",
                    }
                )
            continue
        if len(assignments) >= max_items:
            deferred.add(group_key)
            continue

        preview = preview_placement_room_candidates(placement.id)
        if (preview.get("target") or {}).get("is_online"):
            decided[group_key] = "online"
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
            accept=_shared_copy_room_filter(placement, rooms_by_code),
        )
        if not room:
            decided[group_key] = "no_clean_room"
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
        decided[group_key] = None
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
        # Classes another batch could still room (copies of one class count once).
        "remaining": len(deferred),
        "truncated": bool(deferred),
    }


def _rooms_by_code() -> dict[tuple[str, str], Room]:
    return {
        (_room_key(room.room_code), str(room.section or "").strip().upper()): room
        for room in Room.objects.all()
    }


def _shared_copy_room_filter(
    placement: SectionPlacement, rooms_by_code: dict[tuple[str, str], Room]
) -> Callable[[dict], bool] | None:
    """Accept only rooms whose department suits every board holding an unroomed
    copy of this physical meeting, which is roomed as one class; ``None`` when
    there is no other copy, so a single placement keeps its own check."""
    boards = {
        row.board
        for row in SectionPlacement.objects.filter(
            term_section_id=placement.term_section_id,
            board__scenario_id=placement.board.scenario_id,
            day=placement.day,
            start_time=placement.start_time,
            end_time=placement.end_time,
        ).select_related("board")
        if not _room_key(row.room)
    }
    if len({board.id for board in boards}) <= 1:
        return None

    def accept(candidate: dict) -> bool:
        room = rooms_by_code.get(
            (
                _room_key(candidate.get("room_code")),
                str(candidate.get("section") or "").strip().upper(),
            )
        )
        return room is not None and all(_room_department_fits(room, board) for board in boards)

    return accept


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
        external_ids = external_shared_section_ids(scenario.id)
        rooms_by_code = _rooms_by_code()
        applied: list[dict] = []
        skipped: list[dict] = list(preview.get("skipped") or [])
        for item in preview["assignments"]:
            placement = locked.get(int(item["placement_id"]))
            if not placement:
                skipped.append({**item, "reason": "missing_after_preview"})
                continue
            if placement.term_section_id in external_ids:
                skipped.append({**item, "reason": "externally_owned_after_preview"})
                continue

            current_room = _room_key(placement.room)
            if current_room and current_room != "UNASSIGNED":
                skipped.append({**item, "reason": "already_assigned_after_preview"})
                continue

            recheck = preview_placement_room_candidates(placement.id)
            room = _best_clean_room_candidate(
                recheck,
                current_room=placement.room,
                reserved=set(),
                accept=_shared_copy_room_filter(placement, rooms_by_code),
            )
            if not room or _room_key(room.get("room_code")) != _room_key(item["new_room"]):
                skipped.append({**item, "reason": "room_no_longer_clean"})
                continue

            old_room = placement.room or ""
            rows = assign_physical_meeting_room(placement, str(item["new_room"] or ""))
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
                    "placement_ids": [row.id for row in rows],
                    "board_ids": sorted({row.board_id for row in rows}),
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


def _copies_moving_with(
    anchor: SectionPlacement, copies: Iterable[SectionPlacement]
) -> list[SectionPlacement]:
    """The copies a room-only save moves with ``anchor``: those in its room.

    ``copies`` are the same-scenario rows of the anchor's section at its exact
    slot (``assign_physical_meeting_room``); room candidates' audit reads the
    same answer.
    """
    key = _room_key(anchor.room)
    return [row for row in copies if _room_key(row.room) == key]


def assign_physical_meeting_room(placement: SectionPlacement, room: str) -> list[SectionPlacement]:
    """Give one physical meeting a new room on every agreeing copy in its scenario.

    A section shared across boards at the same day/start/end is one class in one
    room (``timetable_exact_rooming``), so the same-scenario copies at that slot
    that are in this copy's room move with it, as do a scenario-owned section's
    canonical meetings in that room (merging any row the new room makes a
    duplicate). A copy already in another room is left for the canonicaliser,
    so moving back restores every row exactly, unless the new room is one a
    disagreeing copy already held (the copies then agree, and later room
    changes move them together). Global sections' meetings stay
    untouched, matching ``sync_meetings_from_placements``. Days, times and lock
    flags never change, so this is the room-only edit a lock admits. Callers
    reject externally owned sections first.
    """
    scenario_id = placement.board.scenario_id
    slot = {
        "day": placement.day,
        "start_time": placement.start_time,
        "end_time": placement.end_time,
    }
    with transaction.atomic():
        copies = list(
            SectionPlacement.objects.select_for_update()
            .filter(
                term_section_id=placement.term_section_id, board__scenario_id=scenario_id, **slot
            )
            .select_related("term_section", "board")
            .order_by("board_id", "id")
        )
        anchor = next(row for row in copies if row.pk == placement.pk)
        old_key = _room_key(anchor.room)
        rows = _copies_moving_with(anchor, copies)
        for row in rows:
            row.room = room
            row.save(update_fields=["room", "updated_at"])
        if placement.term_section.scenario_id == scenario_id:
            meetings = list(
                TermSectionMeeting.objects.select_for_update()
                .filter(term_section_id=placement.term_section_id, **slot)
                .order_by("pk")
            )
            # A row already holding exactly the new room string owns that unique
            # key (term section, slot, room, instructor); a moving row for the
            # same instructor merges into it, whatever spelling it started from.
            taken = {m.instructor for m in meetings if m.room == room}
            for meeting in meetings:
                if _room_key(meeting.room) != old_key or meeting.room == room:
                    continue
                if meeting.instructor in taken:
                    meeting.delete()
                    continue
                taken.add(meeting.instructor)
                meeting.room = room
                meeting.save(update_fields=["room"])
    placement.room = room
    return rows


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
        unroomed = [
            row
            for row in exclude_online_courses_for_board(
                unroomed_qs,
                board,
                lookup=online_lookup,
            ).order_by("day", "start_time")
            if not section_policy(row.term_section)
        ][:5]
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
    external_ids = external_shared_section_ids(scenario.id)
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
        if placement.term_section_id in external_ids:
            skipped.append(
                {"placement_id": placement_id, "label": label, "reason": "externally_owned"}
            )
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
        external_ids = external_shared_section_ids(scenario.id)
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
            if placement.term_section_id in external_ids:
                skipped.append({**item, "reason": "externally_owned_after_preview"})
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
            if section_policy(placement.term_section):
                try:
                    result = switch_policy_section_bundle(
                        placement,
                        day=item["new_day"],
                        start_time=item["new_start"],
                        end_time=item["new_end"],
                    )
                except SectionPolicyError as exc:
                    skipped.append({**item, "reason": str(exc)})
                    continue
                applied.append(
                    {
                        **item,
                        "old_day": old_day,
                        "old_start": old_start,
                        "old_end": old_end,
                        "old_room": old_room,
                        "before_placements": result["before_placements"],
                        "placement_ids": [row.id for row in result["placements"]],
                    }
                )
                continue
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

    # This path re-validates with preview_placement_slot_candidates /
    # validate_placement, both board-scoped and keyed on the free-text
    # meeting.instructor — structurally unable to see a cross-board clash. That
    # matters more here than on a deliberate drag: the candidates are drawn from
    # builder actions of kind "instructor_clash", so this bulk fixer relocates
    # precisely the instructors most likely to hold sessions on another board,
    # and the registrar never sees the target slot.
    from core.services.timetable_instructor_backstop import verify_persisted_scenario

    return {
        "scenario_id": scenario.id,
        "board_id": board_id,
        "requested_count": len(requested),
        "applied_count": len(applied),
        "skipped_count": len(skipped),
        "applied": applied,
        "skipped": skipped[:80],
        "instructor_clash_backstop": verify_persisted_scenario(
            scenario.id, context="bulk_safe_time_moves"
        ),
    }


def compute_affected_students(board_id: int) -> dict:
    """Assess student impact of time-overlapping placements on a board.

    For every pair of overlapping placements (detected by
    :func:`detect_board_conflicts`):

    1. Take the two planner course keys of the overlap.
    2. Use canonical scenario course requests to find students
       who need *both* courses -- these are the "affected" students.
    3. Read whether they can route round it from the overlap's
       ``blocked_students``, decided once by :func:`detect_board_conflicts`
       (which also derives H15 severity from it): the overlap is
       "resolvable" when some placed section of one course -- siblings on
       this board included -- fits round some placed section of the other;
       otherwise the affected students are "blocked" (no clash-free pair of
       sections exists for them).

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
    # Two sections of one course are not a pair a student takes together.
    overlaps = [
        overlap for overlap in conflicts.get("overlaps", []) if not overlap.get("same_course")
    ]

    if not overlaps:
        return {
            "affected_count": 0,
            "blocked_count": 0,
            "resolvable_count": 0,
            "overlap_details": [],
        }

    # Precompute course->students map from canonical scenario demand.
    from core.services.timetable_overlap import build_course_students_map

    try:
        scenario_id = DeliveryBoard.objects.only("scenario_id").get(id=board_id).scenario_id
        course_students_map = build_course_students_map(
            scenario_id, {str(key) for overlap in overlaps for key in overlap["course_keys"]}
        )
    except DeliveryBoard.DoesNotExist:
        course_students_map = {}

    total_affected = 0
    total_blocked = 0
    overlap_details = []

    for overlap in overlaps:
        code_a, code_b = (str(key) for key in overlap["course_keys"])

        # Find students studying BOTH courses (from precomputed map)
        affected_students = course_students_map.get(
            normalize_code(code_a), set()
        ) & course_students_map.get(normalize_code(code_b), set())

        if not affected_students:
            continue

        # A clash-free pair of the two courses' sections resolves the overlap
        # for the student; detect_board_conflicts decides that once, and its
        # severity follows from the same answer.
        resolvable = not overlap.get("blocked_students")
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
    policy_seen = set()
    placements = SectionPlacement.objects.filter(board__scenario_id=scenario_id).select_related(
        "term_section"
    )
    for placement in placements:
        key = placement.term_section.course_key or placement.term_section.course_code
        section = placement.term_section.section
        if section_policy(placement.term_section):
            identity = (
                placement.term_section_id,
                placement.day,
                placement.start_time,
                placement.end_time,
            )
            if identity in policy_seen:
                continue
            policy_seen.add(identity)
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
    policy = get_course_meeting_policy(key, key)
    if policy:
        return len(policy.durations)
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
        ``used_section_labels`` lists the labels in use for the course on any
        board of the scenario, in natural order (S1, S3, S10), so a page can
        name the next section without colliding with one already taken.
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
                "used_section_labels": sorted(used_sections.get(key, ()), key=natural_key),
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
    """Return the backend truth contract used by the split workspace UI.

    With one teacher per section every board's conflict scan and the
    scenario-wide backstop read the same map, loaded once for the summary.
    """
    kwargs = {
        "boards": boards,
        "board_conflicts_by_id": board_conflicts_by_id,
        "cross_board_conflicts": cross_board_conflicts,
    }
    if is_one_teacher_active():
        from core.services.section_teacher_assignment import assignment_scope

        with assignment_scope(scenario_id):
            return _scenario_safety_summary(scenario_id, **kwargs)
    return _scenario_safety_summary(scenario_id, **kwargs)


def _scenario_safety_summary(
    scenario_id: int,
    *,
    boards: list[DeliveryBoard] | None = None,
    board_conflicts_by_id: dict[int, dict] | None = None,
    cross_board_conflicts: list[dict] | None = None,
) -> dict:
    def _scenario_instructor_clash_count(sid: int) -> int:
        import logging

        from core.services.timetable_instructor_backstop import (
            scenario_instructor_clash_count,
        )

        try:
            return scenario_instructor_clash_count(sid)
        except Exception:  # pragma: no cover - never break the summary
            logging.getLogger(__name__).exception(
                "Scenario instructor clash count failed for scenario %s", sid
            )
            return 0

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
    # ``same_course_overlaps`` is the hard part of ``overlaps``: two sections of
    # one course at once. The rest are cross-course overlaps counted from course
    # demand (students who need both courses, not students seated in both
    # sections), which the optimiser's student-outcome gate judges instead.
    same_board = {"overlaps": 0, "same_course_overlaps": 0, "instructors": 0, "rooms": 0}
    for board in boards:
        conflicts = board_conflicts_by_id.get(board.id) or detect_board_conflicts(board.id)
        same_board["overlaps"] += len(conflicts.get("overlaps", []))
        same_board["same_course_overlaps"] += sum(
            1 for overlap in conflicts.get("overlaps", []) if overlap.get("same_course")
        )
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
        elif (
            not section_policy(placement.term_section)
            or section_policy(placement.term_section).uses_shared_room
        ) and (not room or room.upper() == "UNASSIGNED"):
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
        # Scenario-wide instructor double-bookings. Deliberately separate from
        # same_board_conflicts["instructors"]: that one is summed PER BOARD, so an
        # instructor teaching on two boards in the same slot is invisible to it,
        # and instructors routinely span boards. This is the whole truth.
        "instructor_clashes_scenario": _scenario_instructor_clash_count(scenario_id),
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


def _readiness_item(
    code: str,
    board: DeliveryBoard | None = None,
    *,
    count: int | None = None,
    **params: object,
) -> dict:
    """One structured publish blocker/warning (see ``check_publish_readiness``)."""
    return {
        "code": code,
        "board_id": board.id if board is not None else None,
        "board_label": board.label if board is not None else None,
        "count": count,
        "params": params,
    }


def check_publish_readiness(scenario_id: int) -> dict:
    """Determine whether a scenario can be published (finalised).

    Blockers (prevent publish):
        - No boards exist in the scenario.
        - A board has zero placements.
        - A board has any **critical** conflicts (same-course overlaps,
          overlaps that leave students without a clash-free pair of
          sections -- H15 -- or instructor double-bookings).
        - A board has a placement sitting on an institutionally **blocked**
          slot (legality — the timetable must not occupy a reserved cell).
        - A board has room clashes while rooms are assigned, or sections that
          still need a shared room and have none.
        - A planned section is not placed yet, a placed section lacks some of
          its course's meetings, or an English-pattern section breaks its
          pattern policy.

    Warnings (advisory, do not block):
        - A board has room-clash warnings, or cross-course overlaps every
          affected student can route round through sibling sections.
        - Cross-board overlaps that share at least ``HARD_OVERLAP_THRESHOLD``
          students.

    Parameters:
        scenario_id: PK of the ``Scenario``.

    Returns:
        ``{"ready": bool, "blockers": [...], "warnings": [...],
        "blocker_items": [...], "warning_items": [...]}``.

        ``blockers`` / ``warnings`` are the English display strings. The
        ``*_items`` lists are parallel to them (one item per string, same
        order) so a client can word each one in any language and link it to
        its board. Every item is::

            {"code": str, "board_id": int | None, "board_label": str | None,
             "count": int | None, "params": {...}}

        ``count`` is the headline number the English string leads with;
        ``params`` holds everything else the string names. ``board_id`` /
        ``board_label`` are set when the item belongs to one board, else None.

        Blocker codes (``code``: board / count / params):

        ``no_boards``
            no board / none / {}. Returned alone: nothing else can be checked.
        ``board_no_placements``
            board / none / {}.
        ``board_critical_conflicts``
            board / critical conflicts on the board / {}.
        ``board_blocked_slot_placements``
            board / placements on institutionally blocked slots /
            ``slots``: sorted unique ``"DAY HH:MM"`` labels they occupy.
        ``board_room_conflicts``
            board / room clashes on the board / {}.
        ``board_sections_without_rooms``
            board / placements still ``UNASSIGNED`` that need a shared room / {}.
        ``missing_required_section``
            no board / sections still to place (planned minus used) /
            ``course_code``, ``course_key``, ``programme_term``,
            ``planned_sections``, ``used_sections``.
        ``incomplete_section_pattern``
            board of the section's first placement / meetings missing /
            ``course_code``, ``course_key``, ``section``, ``placed_meetings``,
            ``expected_meetings``.
        ``invalid_section_pattern``
            board of the section's first placement / none /
            ``course_code``, ``section``, ``detail`` (the policy's English text).

        Warning codes:

        ``board_warnings``
            board / conflict warnings on the board / {}.
        ``cross_board_conflicts``
            no board / cross-board pairs at or over the threshold /
            ``threshold``: ``HARD_OVERLAP_THRESHOLD`` (shared students).
    """
    from django.db.models import Q

    from core.models import TimetableScenario
    from core.services.timetable_validation import blocked_slot_keys

    board_list = list(DeliveryBoard.objects.filter(scenario_id=scenario_id))
    blockers: list[str] = []
    warnings: list[str] = []
    blocker_items: list[dict] = []
    warning_items: list[dict] = []
    online_lookup = OnlineCourseLookup()

    def block(text: str, item: dict) -> None:
        blockers.append(text)
        blocker_items.append(item)

    def warn(text: str, item: dict) -> None:
        warnings.append(text)
        warning_items.append(item)

    def outcome() -> dict:
        return {
            "ready": len(blockers) == 0,
            "blockers": blockers,
            "warnings": warnings,
            "blocker_items": blocker_items,
            "warning_items": warning_items,
        }

    if not board_list:
        block("No boards in this scenario", _readiness_item("no_boards"))
        return outcome()

    _scenario = TimetableScenario.objects.filter(id=scenario_id).first()
    blocked_set = blocked_slot_keys(_scenario.blocked_slots) if _scenario else set()
    blocked_q = Q()
    for _day, _start in blocked_set:
        blocked_q |= Q(day=_day, start_time=_start)

    for board in board_list:
        placed = SectionPlacement.objects.filter(board=board).count()
        if placed == 0:
            block(
                f"Board '{board.label}' has no placements",
                _readiness_item("board_no_placements", board),
            )
            continue

        conflicts = detect_board_conflicts(board.id)
        if conflicts["summary"]["critical"] > 0:
            block(
                f"Board '{board.label}': {conflicts['summary']['critical']} critical conflicts",
                _readiness_item(
                    "board_critical_conflicts", board, count=conflicts["summary"]["critical"]
                ),
            )

        # Legality: no placement may occupy an institutionally blocked slot.
        if blocked_set:
            on_blocked_slots = list(
                SectionPlacement.objects.filter(board=board)
                .filter(blocked_q)
                .values_list("day", "start_time")
            )
            on_blocked = len(on_blocked_slots)
            if on_blocked:
                block(
                    f"Board '{board.label}': {on_blocked} placement(s) on blocked slots",
                    _readiness_item(
                        "board_blocked_slot_placements",
                        board,
                        count=on_blocked,
                        slots=sorted({f"{day} {start}" for day, start in on_blocked_slots}),
                    ),
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
            block(
                f"Board '{board.label}': {room_clashes} room conflicts",
                _readiness_item("board_room_conflicts", board, count=room_clashes),
            )

        # Unassigned rooms when rooms are expected
        unassigned_rooms = sum(
            1
            for row in offline_placements.filter(room="UNASSIGNED").select_related("term_section")
            if not section_policy(row.term_section)
            or section_policy(row.term_section).uses_shared_room
        )
        if unassigned_rooms > 0:
            block(
                f"Board '{board.label}': {unassigned_rooms} sections without rooms",
                _readiness_item("board_sections_without_rooms", board, count=unassigned_rooms),
            )

        if conflicts["summary"]["warning"] > 0 and not (room_clashes > 0 and has_rooms):
            warn(
                f"Board '{board.label}': {conflicts['summary']['warning']} warnings",
                _readiness_item("board_warnings", board, count=conflicts["summary"]["warning"]),
            )

    # Cross-board conflicts: high-overlap pairs are warnings
    from core.services.timetable_overlap import HARD_OVERLAP_THRESHOLD

    cross_conflicts = detect_cross_board_conflicts(scenario_id)
    high_overlap_cross = [
        c for c in cross_conflicts if c["overlap_count"] >= HARD_OVERLAP_THRESHOLD
    ]
    if high_overlap_cross:
        warn(
            f"{len(high_overlap_cross)} cross-board conflicts with {HARD_OVERLAP_THRESHOLD}+ shared students",
            _readiness_item(
                "cross_board_conflicts",
                count=len(high_overlap_cross),
                threshold=HARD_OVERLAP_THRESHOLD,
            ),
        )

    missing_sections = [
        item for item in compute_scenario_budget(scenario_id) if item["remaining_sections"] > 0
    ]
    for item in missing_sections:
        block(
            "Missing required section: "
            f"{item['course_code']} needs {item['remaining_sections']} more "
            f"(planned {item['planned_sections']}, used {item['used_sections']})",
            _readiness_item(
                "missing_required_section",
                count=item["remaining_sections"],
                course_code=item["course_code"],
                course_key=item["course_key"],
                programme_term=item["programme_term"],
                planned_sections=item["planned_sections"],
                used_sections=item["used_sections"],
            ),
        )

    # One read of the scenario's placements serves both pattern checks below
    # and says which board a section sits on (the board of its first placement).
    # Ordered, so "the board of its first placement" is the same board on every
    # request (an unordered queryset may come back in any order on PostgreSQL).
    scenario_placements = list(
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .select_related("term_section")
        .order_by("board__display_order", "board_id", "id")
    )
    board_by_id = {board.id: board for board in board_list}
    section_board_id: dict[tuple[str, str], int] = {}
    for placement in scenario_placements:
        ts = placement.term_section
        section_board_id.setdefault(
            (ts.course_key or ts.course_code, ts.section), placement.board_id
        )

    for issue in compute_incomplete_section_patterns(scenario_id):
        home_id = section_board_id.get((issue["course_key"], issue["section"]))
        block(
            "Incomplete section pattern: "
            f"{issue['course_code']}-{issue['section']} has "
            f"{issue['placed_meetings']}/{issue['expected_meetings']} meetings",
            _readiness_item(
                "incomplete_section_pattern",
                board_by_id.get(home_id) if home_id is not None else None,
                count=issue["missing_meetings"],
                course_code=issue["course_code"],
                course_key=issue["course_key"],
                section=issue["section"],
                placed_meetings=issue["placed_meetings"],
                expected_meetings=issue["expected_meetings"],
            ),
        )

    from core.services.timetable_section_policy import (
        assert_pattern_available,
        assert_policy_placement_rows,
    )

    policy_sections = {}
    policy_rows = defaultdict(list)
    for placement in scenario_placements:
        policy = section_policy(placement.term_section)
        if policy:
            policy_sections[placement.term_section_id] = (placement.term_section, policy)
            policy_rows[placement.term_section_id].append(placement)
    for section_id, (section, policy) in policy_sections.items():
        try:
            assert_policy_placement_rows(policy, policy_rows[section_id])
            assert_pattern_available(
                policy,
                [
                    row
                    for row in policy_rows[section_id]
                    if row.board_id == policy_rows[section_id][0].board_id
                ],
                _scenario,
            )
            if section.scenario_id == scenario_id:
                assert_policy_rows(
                    policy, list(TermSectionMeeting.objects.filter(term_section=section))
                )
        except (SectionPolicyError, ValueError, TypeError) as exc:
            block(
                f"Invalid English section pattern {section.course_code}-{section.section}: {exc}",
                _readiness_item(
                    "invalid_section_pattern",
                    board_by_id.get(policy_rows[section_id][0].board_id),
                    course_code=section.course_code,
                    section=section.section,
                    detail=str(exc),
                ),
            )

    return outcome()

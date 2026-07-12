"""
core/services/timetable_rooming.py
Room assignment service for the timetable workspace.

Provides:
  - RoomTracker: in-memory room usage tracker for greedy placement
  - assign_rooms_to_board(): post-placement room filler for solver/SA paths
  - get_programme_rooms(): load rooms filtered by programme codes
  - check_room_feasibility(): pre-check section sizes vs room capacities
"""

from __future__ import annotations

import time
from collections import defaultdict

from django.conf import settings
from django.utils import timezone

from core.models import DeliveryBoard, Room, SectionPlacement
from core.services.timetable_decision_trace import DecisionTrace
from core.services.timetable_lab_predicate import (
    is_lab_heuristic_unified,
    meeting_requires_lab_room,
)
from core.services.timetable_online import OnlineCourseLookup, normalise_course_code
from core.services.timetable_room_oracle import (
    NO_ROOM_CAPACITY,
    ROOM_BUFFER_REJECT,
    RoomFailureReason,
    check_capacity_feasibility,
    check_gender_feasibility,
    check_occupancy,
    check_type_feasibility,
    is_room_oracle_enabled,
    room_failure_breakdown,
)
from core.services.timetable_solver_codes import (
    ROOMING_REPAIR_REASSIGNED,
    is_stage_trace_enabled,
)
from core.services.timetable_stage_telemetry import (
    empty_stage_telemetry,
    is_stage_telemetry_enabled,
    record_stage_iterations,
    record_stage_ms,
)


def get_capacity_buffer() -> float:
    """Return the active room-sizing multiplier (e.g. 1.1 = +10% for late adds).

    Reads ``settings.TIMETABLE_CAPACITY_BUFFER`` and falls back to 1.1 if the
    setting is missing or invalid. Kept as a helper so every site that sizes
    rooms stays in sync with config.
    """
    try:
        value = float(getattr(settings, "TIMETABLE_CAPACITY_BUFFER", 1.1))
    except (TypeError, ValueError):
        return 1.1
    if value <= 0:
        return 1.1
    return value


def _rooming_identity(value: object | None) -> str:
    """Return the exact planner identity key used for rooming lookups."""
    return str(value or "").strip()


def _budget_identity(budget: object) -> str:
    return _rooming_identity(
        getattr(budget, "course_key", "") or getattr(budget, "course_code", "")
    )


def _placement_identity(placement: SectionPlacement) -> str:
    term_section = placement.term_section
    return _rooming_identity(
        getattr(term_section, "course_key", "") or getattr(term_section, "course_code", "")
    )


def _visible_course_code(obj: object) -> str:
    return _rooming_identity(getattr(obj, "course_code", ""))


def _raw_section_demand(budget: object) -> int:
    planned_sections = int(getattr(budget, "planned_sections", 0) or 0)
    total_demand = int(getattr(budget, "total_demand", 0) or 0)
    max_per_section = int(getattr(budget, "max_per_section", 40) or 40)
    return -(-total_demand // planned_sections) if planned_sections > 0 else max_per_section


def _build_rooming_budget_maps(budgets: list[object], buffer_multiplier: float) -> dict[str, dict]:
    """Build room-sizing maps keyed by planner course identity.

    Visible ``course_code`` is used only as a legacy fallback when exactly one
    budget row has that display code. Duplicate display codes are intentionally
    not guessed because their course keys may carry different demand/credits.
    """
    maps: dict[str, dict] = {
        "raw_by_key": {},
        "buffered_by_key": {},
        "credit_by_key": {},
        "raw_by_code": {},
        "buffered_by_code": {},
        "credit_by_code": {},
    }
    by_code: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
    for budget in budgets:
        raw = _raw_section_demand(budget)
        buffered = int(raw * buffer_multiplier)
        credit_hours = int(getattr(budget, "credit_hours", 0) or 0)
        key = _budget_identity(budget)
        code = _visible_course_code(budget)
        if key:
            maps["raw_by_key"][key] = raw
            maps["buffered_by_key"][key] = buffered
            maps["credit_by_key"][key] = credit_hours
        if code:
            by_code[code].append((raw, buffered, credit_hours))

    for code, rows in by_code.items():
        if len(rows) != 1:
            continue
        raw, buffered, credit_hours = rows[0]
        maps["raw_by_code"][code] = raw
        maps["buffered_by_code"][code] = buffered
        maps["credit_by_code"][code] = credit_hours
    return maps


def _budget_value_for_placement(
    placement: SectionPlacement,
    maps: dict[str, dict],
    bucket: str,
    default: int,
) -> int:
    key = _placement_identity(placement)
    by_key = maps.get(f"{bucket}_by_key", {})
    if key and key in by_key:
        return int(by_key[key])
    code = _visible_course_code(placement.term_section)
    by_code = maps.get(f"{bucket}_by_code", {})
    if code and code in by_code:
        return int(by_code[code])
    return default


def _room_type_for_credit_duration(credit_hours: int, duration_minutes: int) -> str:
    """Return the authoritative rooming room type for a meeting."""
    # Only 4-credit courses have lab meetings. 2-credit 100-min meetings are
    # long lectures, not labs. Keep the unified duration predicate as the
    # duration gate, then additionally gate on credits.
    if is_lab_heuristic_unified():
        return (
            "lab"
            if (credit_hours == 4 and meeting_requires_lab_room(duration_minutes))
            else "lecture"
        )
    return "lab" if (duration_minutes > 80 and credit_hours == 4) else "lecture"


def room_type_for_placement(
    placement: SectionPlacement,
    *,
    start_time: str | None = None,
    end_time: str | None = None,
    budget_maps: dict[str, dict] | None = None,
) -> str:
    """Classify a placement's required room type using rooming's budget rules."""
    if budget_maps is None:
        from core.models import ScenarioSectionBudget

        budget_maps = _build_rooming_budget_maps(
            list(ScenarioSectionBudget.objects.filter(scenario=placement.board.scenario)),
            get_capacity_buffer(),
        )
    credit_hours = _budget_value_for_placement(placement, budget_maps, "credit", 3)
    start = str(start_time if start_time is not None else placement.start_time).strip()
    end = str(end_time if end_time is not None else placement.end_time).strip()
    try:
        duration = _to_min(end) - _to_min(start)
    except (AttributeError, TypeError, ValueError):
        duration = 0
    return _room_type_for_credit_duration(credit_hours, duration)


def get_programme_rooms(programmes: list[str]) -> list[dict]:
    """Load rooms available for a list of programmes.

    Matches any room whose comma-separated ``department`` field contains
    at least one of the given programme codes.

    Returns list of dicts sorted by capacity ASC (for best-fit allocation):
        ``[{room_code, capacity, room_type, wing, building}, ...]``
    """
    all_rooms = Room.objects.all().order_by("capacity")
    result = []
    progs_upper = {p.strip().upper() for p in programmes if p.strip()}
    for r in all_rooms:
        room_progs = {p.strip().upper() for p in r.department.split(",") if p.strip()}
        if room_progs & progs_upper:
            result.append(
                {
                    "room_code": r.room_code,
                    "capacity": r.capacity,
                    "room_type": r.room_type or "lecture",
                    "wing": r.wing,
                    "building": r.building,
                    "section": (r.section or "").upper(),
                }
            )
    return result


def _section_gender(label: str | None) -> str:
    """Extract M/F gender from a TermSection.section label (e.g. 'M1' → 'M').

    Returns '' if the label doesn't start with M or F.
    """
    if not label:
        return ""
    first = str(label).strip()[:1].upper()
    return first if first in ("M", "F") else ""


def get_board_gender(board_id: int) -> str:
    """Derive M/F gender for a board from the students linked to it.

    Returns 'M' or 'F' if all linked students share the same section;
    '' if the board is empty or mixed (falls back to no gender filter).
    """
    from core.models import BoardStudentLink, Student

    student_ids = BoardStudentLink.objects.filter(board_id=board_id).values_list(
        "student_id", flat=True
    )
    if not student_ids:
        return ""
    genders = (
        Student.objects.filter(student_id__in=list(student_ids))
        .values_list("section", flat=True)
        .distinct()
    )
    unique = {str(g or "").strip().upper() for g in genders}
    unique.discard("")
    return next(iter(unique)) if len(unique) == 1 else ""


def _to_min(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


class RoomTracker:
    """In-memory tracker for room usage during greedy placement.

    Maintains a ``(day, start_time) -> set[room_code]`` map of occupied
    rooms.  Used by ``auto_place_board()`` to check availability and
    assign rooms as sections are placed.
    """

    def __init__(self, rooms: list[dict]):
        self.rooms = rooms
        self.lecture_rooms = sorted(
            [r for r in rooms if r["room_type"] == "lecture"],
            key=lambda r: r["capacity"],
        )
        self.lab_rooms = sorted(
            [r for r in rooms if r["room_type"] == "lab"],
            key=lambda r: r["capacity"],
        )
        self.usage: dict[tuple[str, str], set[str]] = defaultdict(set)
        # Parallel time-interval index so overlapping slots (e.g. 10:30-11:45
        # vs 10:50-12:05) correctly block the same room. ``usage`` keys by
        # exact start-time only and would miss this; scan this list for any
        # entry whose [s, e) intersects the candidate window.
        self.intervals: dict[str, list[tuple[int, int, str]]] = defaultdict(list)

    def _pool(self, room_type: str, gender: str = "") -> list[dict]:
        base = self.lab_rooms if room_type == "lab" else self.lecture_rooms
        if gender in ("M", "F"):
            return [r for r in base if r.get("section", "") == gender]
        return base

    @staticmethod
    def _mins(t: str) -> int:
        h, m = t.split(":")
        return int(h) * 60 + int(m)

    def _overlapping_rooms(self, day: str, start_min: int, end_min: int) -> set[str]:
        """Rooms occupied by any booking that overlaps [start_min, end_min)."""
        return {r for (s, e, r) in self.intervals.get(day, []) if s < end_min and e > start_min}

    def mark_used(self, day: str, start: str, end: str, room_code: str) -> None:
        """Record a (day, start, end, room) booking in both indexes.

        Used when seeding the tracker from existing DB placements or when
        reserving a preferred room for a known meeting. Keeps ``usage`` in
        sync with the time-interval index so callers that read either
        see consistent state.
        """
        self.usage[(day, start)].add(room_code)
        self.intervals[day].append((self._mins(start), self._mins(end), room_code))

    def is_feasible(
        self,
        day: str,
        start: str,
        min_capacity: int,
        room_type: str = "lecture",
        gender: str = "",
        end: str | None = None,
    ) -> bool:
        """Can we fit a section of *min_capacity* in this slot?

        When ``end`` is provided, the overlap-aware index is used so a
        10:50-12:05 booking will see a busy 10:30-11:45 in the same room.
        When omitted, falls back to exact (day, start) match.
        """
        if end is not None:
            used = self._overlapping_rooms(day, self._mins(start), self._mins(end))
        else:
            used = self.usage.get((day, start), set())
        pool = self._pool(room_type, gender)
        return any(r["room_code"] not in used and r["capacity"] >= min_capacity for r in pool)

    def assign_best_fit(
        self,
        day: str,
        start: str,
        min_capacity: int,
        room_type: str = "lecture",
        gender: str = "",
        end: str | None = None,
    ) -> str | None:
        """Assign the smallest sufficient room of matching type and gender.

        When ``end`` is provided, excludes rooms whose time on this day
        overlaps the candidate window — a fix for the bug where slots
        2 (10:30-11:45) and 2b (10:50-12:05) could both get the same
        room on the same day.

        Returns the ``room_code`` on success, or ``None`` if no room fits.
        """
        if end is not None:
            s_min = self._mins(start)
            e_min = self._mins(end)
            used = self._overlapping_rooms(day, s_min, e_min)
        else:
            s_min = e_min = None
            used = self.usage.get((day, start), set())
        pool = self._pool(room_type, gender)
        for r in pool:  # already sorted by capacity ASC
            if r["room_code"] not in used and r["capacity"] >= min_capacity:
                self.usage[(day, start)].add(r["room_code"])
                if end is not None and s_min is not None and e_min is not None:
                    self.intervals[day].append((s_min, e_min, r["room_code"]))
                return r["room_code"]
        return None

    def release(self, day: str, start: str, room_code: str, end: str | None = None) -> None:
        """Free a room (for undo/retry)."""
        key = (day, start)
        if key in self.usage:
            self.usage[key].discard(room_code)
        if end is not None and day in self.intervals:
            target = (self._mins(start), self._mins(end), room_code)
            try:
                self.intervals[day].remove(target)
            except ValueError:
                pass


def repair_unassigned_after_greedy(
    board,
    tracker: RoomTracker,
    unassigned_records: list[dict],
    meeting_result_refs: dict[tuple[int, str, str], dict],
    room_failures: list[dict],
) -> int:
    """Post-greedy 1-swap repair of UNASSIGNED meetings.

    For each meeting left UNASSIGNED by the greedy placer:

    1. **Direct fit** — the tracker state may have freed up since the
       original attempt; try ``assign_best_fit`` again.
    2. **1-swap** — find a placed meeting P at the same (day, start)
       whose room R is compatible with the unassigned meeting U
       (type + gender + capacity). If P can be moved to some other
       currently-free room R' that also fits P, swap them: P → R',
       U → R.

    Updates the tracker, the DB ``SectionPlacement`` row, the in-memory
    ``meeting_result_refs`` dict (so the returned payload reflects the
    new rooms) and prunes the corresponding ``room_failures`` entry.

    ``unassigned_records`` is a list of dicts, one per UNASSIGNED meeting,
    each carrying ``{ts_id, day, start, end, demand, room_type, gender,
    course_code, section_code}``.

    ``meeting_result_refs`` maps ``(ts_id, day, start) -> meeting_dict``
    so the caller's ``placements`` payload can be updated in place.

    Returns the number of meetings newly assigned a room.
    """
    from django.utils import timezone

    from core.models import SectionPlacement

    repaired = 0
    still_unassigned: list[dict] = []

    for rec in unassigned_records:
        day = rec["day"]
        start = rec["start"]
        end = rec.get("end")
        demand = rec["demand"]
        rtype = rec["room_type"]
        gender = rec["gender"]

        # Phase 1 — direct fit (tracker state may have changed).
        room_code = tracker.assign_best_fit(day, start, demand, rtype, gender, end=end)

        if not room_code:
            # Phase 2 — 1-swap against any room whose current booking
            # overlaps U's window (day, start..end) and which would fit U
            # AND whose occupant can be relocated. Using the overlap-aware
            # index here (rather than exact-start ``usage[(day, start)]``)
            # lets us rescue collisions like an UNASSIGNED meeting at
            # 10:50-12:05 when the only fitting room is held by an
            # occupant at 10:30-11:45.
            pool = tracker._pool(rtype, gender)
            if end is not None:
                u_s_min = tracker._mins(start)
                u_e_min = tracker._mins(end)
                used_here = tracker._overlapping_rooms(day, u_s_min, u_e_min)
            else:
                used_here = set(tracker.usage.get((day, start), set()))
            fit_rooms = [r for r in pool if r["capacity"] >= demand and r["room_code"] in used_here]

            for r in fit_rooms:
                target_room_code = r["room_code"]
                # Find the occupant whose interval actually overlaps U's
                # window in this room. With the overlap-aware switch the
                # occupant's start_time may differ from U's start_time.
                if end is not None:
                    occupant = (
                        SectionPlacement.objects.filter(
                            board=board,
                            day=day,
                            room=target_room_code,
                            start_time__lt=end,
                            end_time__gt=start,
                        )
                        .order_by("start_time")
                        .first()
                    )
                else:
                    occupant = SectionPlacement.objects.filter(
                        board=board, day=day, start_time=start, room=target_room_code
                    ).first()
                if not occupant:
                    continue

                occ_ts = occupant.term_section
                # Source demand from the budget map when available so a
                # NULL available_capacity doesn't collapse the capacity
                # filter to "any smallest room fits".
                occ_budget = (rec.get("budget_map") or {}).get(occ_ts.course_code)
                if occ_budget:
                    occ_demand = int(occ_budget)
                else:
                    occ_demand = occ_ts.available_capacity or 40
                # target_room_code is in U's pool(rtype, gender), so occupant's
                # rtype must match U's rtype — same pool, same room_type.
                occ_rtype = rtype
                occ_start = (
                    occupant.start_time.strftime("%H:%M")
                    if hasattr(occupant.start_time, "strftime")
                    else str(occupant.start_time)[:5]
                )
                occ_end = (
                    occupant.end_time.strftime("%H:%M")
                    if hasattr(occupant.end_time, "strftime")
                    else str(occupant.end_time)[:5]
                )

                tracker.release(day, occ_start, target_room_code, end=occ_end)
                alt_room = tracker.assign_best_fit(
                    day, occ_start, occ_demand, occ_rtype, gender, end=occ_end
                )
                if alt_room and alt_room != target_room_code:
                    occupant.room = alt_room
                    occupant.save(update_fields=["room", "updated_at"])
                    occ_key = (occ_ts.id, day, occ_start)
                    if occ_key in meeting_result_refs:
                        meeting_result_refs[occ_key]["room"] = alt_room
                    # Register U's new occupancy in BOTH indexes so the next
                    # iteration's overlap-aware checks see the true state.
                    if end is not None:
                        tracker.mark_used(day, start, end, target_room_code)
                    else:
                        tracker.usage[(day, start)].add(target_room_code)
                    room_code = target_room_code
                    break
                elif not alt_room:
                    # Nothing fit the occupant. ``release`` removed
                    # target_room_code with no replacement; restore it at
                    # the occupant's slot so the tracker stays consistent.
                    tracker.mark_used(day, occ_start, occ_end, target_room_code)
                # else: ``alt_room == target_room_code``. ``assign_best_fit``
                # already re-registered the room at the occupant's slot in
                # both indexes — calling ``mark_used`` again would leave a
                # duplicate ``intervals[day]`` entry that ``release`` (single
                # ``list.remove``) cannot fully clean up later. Try the next
                # candidate room instead.

        if room_code:
            SectionPlacement.objects.filter(
                board=board, term_section_id=rec["ts_id"], day=day, start_time=start
            ).update(room=room_code, updated_at=timezone.now())
            key = (rec["ts_id"], day, start)
            if key in meeting_result_refs:
                meeting_result_refs[key]["room"] = room_code
            room_failures[:] = [
                f
                for f in room_failures
                if not (
                    f.get("day") == day
                    and f.get("start_time") == start
                    and f.get("course_code") == rec["course_code"]
                    and f.get("section_code") == rec["section_code"]
                )
            ]
            repaired += 1
        else:
            still_unassigned.append(rec)

    unassigned_records[:] = still_unassigned
    return repaired


def check_room_feasibility(
    board_id: int,
    rooms: list[dict],
) -> list[dict]:
    """Pre-check: can every section find a room with sufficient capacity?

    Returns a list of violations (empty = all feasible):
        ``[{course_code, max_per_section, room_type_needed, max_room_capacity}, ...]``
    """
    from core.models import ScenarioSectionBudget
    from core.services.timetable_autoplace import get_meeting_pattern

    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return []

    budgets = ScenarioSectionBudget.objects.filter(
        scenario=board.scenario, programme_term=board.nominal_term
    )

    lecture_max = max((r["capacity"] for r in rooms if r["room_type"] == "lecture"), default=0)
    lab_max = max((r["capacity"] for r in rooms if r["room_type"] == "lab"), default=0)

    violations = []
    for b in budgets:
        pattern = get_meeting_pattern(b.credit_hours or 3)
        has_lab = any(d > 75 for d in pattern)
        cap = b.max_per_section

        if cap > lecture_max:
            violations.append(
                {
                    "course_code": b.course_code,
                    "max_per_section": cap,
                    "room_type_needed": "lecture",
                    "max_room_capacity": lecture_max,
                }
            )
        if has_lab and cap > lab_max:
            violations.append(
                {
                    "course_code": b.course_code,
                    "max_per_section": cap,
                    "room_type_needed": "lab",
                    "max_room_capacity": lab_max,
                }
            )

    return violations


def assign_rooms_to_board(board_id: int, *, respect_locked: bool = False) -> dict:
    """Post-placement room assignment for solver/annealing paths.

    Assigns rooms to ``SectionPlacement`` rows on the board that currently
    have an empty ``room`` field. Uses greedy best-fit per (day, start_time)
    slot. When ``respect_locked`` is true, locked placements are treated as
    fixed and are not assigned/repaired.

    Returns ``{assigned: int, unassigned: int}``.
    """
    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return {
            "assigned": 0,
            "unassigned": 0,
            "room_failures": [],
            "room_failure_breakdown": {},
            "unplaced_count": 0,
            "buffer_only_rejects": 0,
            # PR6 commit 6 — schema-stable empty stage_telemetry on early-return.
            "stage_telemetry": empty_stage_telemetry(),
        }

    programmes = [p.strip() for p in (board.program or "").split(",") if p.strip()]
    if not programmes:
        return {
            "assigned": 0,
            "unassigned": 0,
            "room_failures": [],
            "room_failure_breakdown": {},
            "unplaced_count": 0,
            "buffer_only_rejects": 0,
            "stage_telemetry": empty_stage_telemetry(),
        }

    online_lookup = OnlineCourseLookup()
    online_codes = online_lookup.codes_for_board(board)

    board_gender = get_board_gender(board_id)
    rooms = get_programme_rooms(programmes)

    def is_online_placement(p: SectionPlacement) -> bool:
        return normalise_course_code(p.term_section.course_code) in online_codes

    # Load all placements for THIS board
    placements = list(
        SectionPlacement.objects.filter(board=board)
        .select_related("term_section")
        .order_by("day", "start_time")
    )

    # Online courses keep their time slots but must not consume physical rooms.
    # A lock protects the placement/time, not a stale physical room value.
    online_room_updates: list[SectionPlacement] = []
    now = timezone.now()
    for p in placements:
        if is_online_placement(p) and p.room:
            p.room = ""
            p.updated_at = now
            online_room_updates.append(p)
    if online_room_updates:
        SectionPlacement.objects.bulk_update(online_room_updates, ["room", "updated_at"])

    if not rooms:
        return {
            "assigned": 0,
            "unassigned": 0,
            "room_failures": [],
            "room_failure_breakdown": {},
            "unplaced_count": 0,
            "buffer_only_rejects": 0,
            "stage_telemetry": empty_stage_telemetry(),
        }

    tracker = RoomTracker(rooms)

    # Pre-populate tracker with rooms used by OTHER boards in the same scenario.
    # Legacy online rows may still carry room text; they are intentionally not
    # seeded because online sections do not occupy physical rooms.
    other_placements = (
        SectionPlacement.objects.filter(board__scenario=board.scenario)
        .exclude(board=board)
        .exclude(room="")
        .exclude(room="UNASSIGNED")
        .select_related("board", "term_section")
    )
    for other in other_placements:
        if online_lookup.is_online_course_for_board(other.board, other.term_section.course_code):
            continue
        # Overlap-aware seeding so e.g. a 10:30-11:45 booking blocks a
        # 10:50-12:05 booking in the same room on the same day; the old
        # (day, start)-only key missed this because 10:30 != 10:50. Online
        # sections are skipped above — they occupy no physical room.
        _s = (
            other.start_time.strftime("%H:%M")
            if hasattr(other.start_time, "strftime")
            else str(other.start_time)[:5]
        )
        _e = (
            other.end_time.strftime("%H:%M")
            if hasattr(other.end_time, "strftime")
            else str(other.end_time)[:5]
        )
        tracker.mark_used(other.day, _s, _e, other.room)

    # First pass: mark rooms already assigned (from greedy or previous run).
    # Exclude the sentinel "UNASSIGNED" so placements left unroomed by a
    # prior pass become repair candidates below rather than being treated
    # as if the sentinel occupied a slot.
    for p in placements:
        if is_online_placement(p):
            continue
        if p.room and p.room != "UNASSIGNED":
            _s = (
                p.start_time.strftime("%H:%M")
                if hasattr(p.start_time, "strftime")
                else str(p.start_time)[:5]
            )
            _e = (
                p.end_time.strftime("%H:%M")
                if hasattr(p.end_time, "strftime")
                else str(p.end_time)[:5]
            )
            tracker.mark_used(p.day, _s, _e, p.room)

    # Get actual students per section and credit hours from budget
    from core.models import ScenarioSectionBudget

    buffer_multiplier = get_capacity_buffer()
    budget_maps = _build_rooming_budget_maps(
        list(ScenarioSectionBudget.objects.filter(scenario=board.scenario)),
        buffer_multiplier,
    )

    # Sort unassigned placements by capacity DESC (largest first = best-fit-decreasing).
    # PR5 commit 6: also re-process placements carrying the "UNASSIGNED"
    # sentinel so the rooming 2nd pass can repair them when a fitting room
    # exists. Capture the sentinel state per placement BEFORE mutation so
    # trace emission can gate strictly on the UNASSIGNED → assigned
    # transition (no emission for empty-string → assigned, which is the
    # normal first-pass path).
    locked_unassigned_count = sum(
        1
        for p in placements
        if (
            not is_online_placement(p)
            and respect_locked
            and p.is_locked
            and (not p.room or p.room == "UNASSIGNED")
        )
    )
    unassigned_placements = [
        p
        for p in placements
        if (
            not is_online_placement(p)
            and (not p.room or p.room == "UNASSIGNED")
            and not (respect_locked and p.is_locked)
        )
    ]
    unassigned_placements.sort(
        key=lambda p: -_budget_value_for_placement(p, budget_maps, "buffered", 40)
    )
    previous_room_by_id: dict[int, str] = {p.id: p.room for p in unassigned_placements}
    decision_trace: dict[str, dict] = {}
    emit_trace = is_stage_trace_enabled()

    # PR6 commit 6 — rooming_repair stage-boundary timing. Scoped to the
    # repair pass only (UNASSIGNED → room reassignments), per ChatGPT
    # guardrail: "keep timing scoped to the repair pass only, not the
    # whole room assignment function. The first-pass rooming belongs
    # outside this stage." We gate on whether any placement arrives
    # carrying the UNASSIGNED sentinel; if none do, this is a pure
    # first-pass call and both rooming_repair keys stay at zero.
    _stage_telemetry: dict[str, dict[str, int]] = empty_stage_telemetry()
    _repair_candidates = sum(1 for p in unassigned_placements if p.room == "UNASSIGNED")
    _telemetry_on = is_stage_telemetry_enabled() and _repair_candidates > 0
    _repair_t0 = time.perf_counter() if _telemetry_on else 0.0
    _repair_reassignments = 0

    assigned = 0
    unassigned = locked_unassigned_count
    # Labs currently ignore capacity in rooming (room_cap=0 below); buffer
    # diagnostics therefore apply to lecture room assignment only.
    #
    # Authoritative per-placement buffer-reject counter — populated only
    # when the oracle flag is on and Stage 2 confirms the rejection was
    # buffer-only. (The legacy flag-agnostic ``lecture_room_reject_due_to_buffer_count``
    # was retired in PR4 commit 7; dashboards migrated to this key.)
    buffer_only_rejects = 0
    room_failures: list[dict] = []

    for p in unassigned_placements:
        cap = _budget_value_for_placement(p, budget_maps, "buffered", 40)
        raw_cap = _budget_value_for_placement(p, budget_maps, "raw", 40)
        room_type = room_type_for_placement(p, budget_maps=budget_maps)

        # For lab meetings, don't filter by capacity — lab rooms have a
        # fixed physical size (computers/benches).
        room_cap = 0 if room_type == "lab" else cap
        # Prefer per-section gender (exam-style sections like 'M1'/'F1');
        # fall back to the board-level gender (timetable-style 'S1'/'S2').
        gender = _section_gender(p.term_section.section) or board_gender
        _s = (
            p.start_time.strftime("%H:%M")
            if hasattr(p.start_time, "strftime")
            else str(p.start_time)[:5]
        )
        _e = (
            p.end_time.strftime("%H:%M") if hasattr(p.end_time, "strftime") else str(p.end_time)[:5]
        )
        room_code = tracker.assign_best_fit(p.day, _s, room_cap, room_type, gender, end=_e)
        if room_code:
            prev_room = previous_room_by_id.get(p.id, "")
            p.room = room_code
            p.save(update_fields=["room", "updated_at"])
            assigned += 1
            # PR6 commit 6 — count only true repair reassignments
            # (UNASSIGNED → room). Matches the semantics of the
            # ROOMING_REPAIR_REASSIGNED sentinel; empty-string →
            # assigned is the normal first-pass path and is NOT a
            # repair reassignment.
            if _telemetry_on and prev_room == "UNASSIGNED":
                _repair_reassignments += 1
            # PR5 commit 6 — emit ROOMING_REPAIR_REASSIGNED when the 2nd
            # pass rescued a placement previously marked UNASSIGNED.
            # Strictly gated: empty-string → assigned is the normal
            # first-pass path and does NOT emit.
            if emit_trace and prev_room == "UNASSIGNED":
                section_code = f"{p.term_section.course_code}|{p.term_section.section}"
                start_str = (
                    p.start_time.strftime("%H:%M")
                    if hasattr(p.start_time, "strftime")
                    else str(p.start_time)[:5]
                )
                end_str = (
                    p.end_time.strftime("%H:%M")
                    if hasattr(p.end_time, "strftime")
                    else str(p.end_time)[:5]
                )
                entry = DecisionTrace(
                    section_code=section_code,
                    course_code=p.term_section.course_code,
                    chosen_day=p.day,
                    chosen_start_time=start_str,
                    chosen_end_time=end_str,
                    chosen_room=room_code,
                    alternatives=(),
                    stage_origin="rooming_repair",
                    stage_context={
                        "code": ROOMING_REPAIR_REASSIGNED,
                        "previous_room": "UNASSIGNED",
                        "new_room": room_code,
                    },
                )
                decision_trace[section_code] = entry.to_dict()
        else:
            # Would a raw-cap room have fit? Used below to refine the oracle
            # rejection code into ROOM_BUFFER_REJECT when the section could
            # have been placed without the capacity buffer. Pass ``end=_e``
            # so an overlapping booking (e.g. a 10:30-11:45 occupant when
            # placing 10:50-12:05) correctly counts as occupancy, not a
            # buffer-only reject.
            is_buffer_only = room_type != "lab" and tracker.is_feasible(
                p.day, _s, raw_cap, room_type, gender, end=_e
            )
            p.room = "UNASSIGNED"
            p.save(update_fields=["room", "updated_at"])
            unassigned += 1
            # PR2 commit 4 — oracle refinement chain. When the flag is off
            # the helpers all return None and the default NO_ROOM_CAPACITY
            # path below runs — commit 3's payload is preserved bit-for-bit.
            # When the flag is on:
            #   * Stage 2: a buffer-only rejection wins over Stage 1 codes
            #     (the section *could* have been placed at raw capacity,
            #     the buffer is what rejected it), bumps the authoritative
            #     ``buffer_only_rejects`` counter.
            #   * Stage 1: type → gender → capacity, first matching wins.
            #   * Occupancy: if Stage 1 finds an eligible pool but every
            #     room is already busy at this slot, emit ROOM_OCCUPIED.
            section_dict = {
                "course_code": p.term_section.course_code,
                "section_code": p.term_section.section,
                "day": p.day,
                "start_time": p.start_time,
                "end_time": p.end_time,
                "demand": raw_cap,
                "room_type_required": room_type,
                "gender_required": gender,
            }
            refined: RoomFailureReason | None = None
            if is_room_oracle_enabled():
                if is_buffer_only:
                    refined = RoomFailureReason(
                        code=ROOM_BUFFER_REJECT,
                        day=p.day,
                        start_time=p.start_time,
                        end_time=p.end_time,
                        course_code=p.term_section.course_code,
                        section_code=p.term_section.section,
                    )
                    buffer_only_rejects += 1
                else:
                    refined = (
                        check_type_feasibility(section_dict, tracker.rooms)
                        or check_gender_feasibility(section_dict, tracker.rooms)
                        or check_capacity_feasibility(
                            section_dict, tracker.rooms, buffer_multiplier
                        )
                        or check_occupancy(
                            section_dict,
                            tracker.rooms,
                            tracker._overlapping_rooms(p.day, tracker._mins(_s), tracker._mins(_e)),
                        )
                    )
            if refined is None:
                refined = RoomFailureReason(
                    code=NO_ROOM_CAPACITY,
                    day=p.day,
                    start_time=p.start_time,
                    end_time=p.end_time,
                    course_code=p.term_section.course_code,
                    section_code=p.term_section.section,
                )
            room_failures.append(refined.to_dict())

    if _telemetry_on:
        record_stage_ms(
            _stage_telemetry,
            "rooming_repair",
            max(1, int((time.perf_counter() - _repair_t0) * 1000)),
        )
        record_stage_iterations(_stage_telemetry, "rooming_repair", _repair_reassignments)

    return {
        "assigned": assigned,
        "unassigned": unassigned,
        "capacity_buffer": buffer_multiplier,
        "buffer_only_rejects": buffer_only_rejects,
        "room_failures": room_failures,
        "room_failure_breakdown": room_failure_breakdown(room_failures),
        "unplaced_count": unassigned,
        "locked_skipped": locked_unassigned_count,
        "decision_trace": decision_trace,
        "stage_telemetry": _stage_telemetry,
    }


def simulate_buffer_impact(board_id: int, buffers: list[float]) -> dict:
    """Dry-run rooming across several buffer values.

    For each ``buffer`` in ``buffers`` (e.g. ``[1.0, 1.1]``), simulates
    ``assign_rooms_to_board`` on a fresh in-memory room tracker and counts
    how many placements would be assigned vs left unassigned at that
    buffer. Never touches the database.

    Returns::

        {
            "board_id": int,
            "programmes": [str, ...],
            "results": [
                {"buffer": float, "assigned": int, "unassigned": int,
                 "rejected_by_buffer_vs_1_0": int},
                ...
            ],
        }
    """
    from core.models import ScenarioSectionBudget

    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return {"board_id": board_id, "programmes": [], "results": []}

    programmes = [p.strip() for p in (board.program or "").split(",") if p.strip()]
    rooms = get_programme_rooms(programmes) if programmes else []
    online_lookup = OnlineCourseLookup()
    online_codes = online_lookup.codes_for_board(board)

    board_gender = get_board_gender(board_id)

    def is_online_placement(p: SectionPlacement) -> bool:
        return normalise_course_code(p.term_section.course_code) in online_codes

    # Stable pre-population: rooms consumed by OTHER boards in this scenario.
    other_usage = list(
        SectionPlacement.objects.filter(board__scenario=board.scenario)
        .exclude(board=board)
        .exclude(room="")
        .exclude(room="UNASSIGNED")
        .select_related("board", "term_section")
    )

    budget_maps = _build_rooming_budget_maps(
        list(ScenarioSectionBudget.objects.filter(scenario=board.scenario)),
        1.0,
    )

    placements = list(
        SectionPlacement.objects.filter(board=board)
        .select_related("term_section")
        .order_by("day", "start_time")
    )

    results: list[dict] = []
    for buf in buffers:
        tracker = RoomTracker(rooms)
        for other in other_usage:
            if online_lookup.is_online_course_for_board(
                other.board, other.term_section.course_code
            ):
                continue
            _s = (
                other.start_time.strftime("%H:%M")
                if hasattr(other.start_time, "strftime")
                else str(other.start_time)[:5]
            )
            _e = (
                other.end_time.strftime("%H:%M")
                if hasattr(other.end_time, "strftime")
                else str(other.end_time)[:5]
            )
            tracker.mark_used(other.day, _s, _e, other.room)
        # Seed with rooms already permanently assigned on THIS board.
        for p in placements:
            if is_online_placement(p):
                continue
            if p.room and p.room != "UNASSIGNED":
                _s = (
                    p.start_time.strftime("%H:%M")
                    if hasattr(p.start_time, "strftime")
                    else str(p.start_time)[:5]
                )
                _e = (
                    p.end_time.strftime("%H:%M")
                    if hasattr(p.end_time, "strftime")
                    else str(p.end_time)[:5]
                )
                tracker.mark_used(p.day, _s, _e, p.room)

        assigned = 0
        unassigned = 0
        rejected_by_buffer = 0

        targets = [
            p
            for p in placements
            if not is_online_placement(p) and (not p.room or p.room == "UNASSIGNED")
        ]
        targets.sort(key=lambda p: -_budget_value_for_placement(p, budget_maps, "raw", 40))

        for p in targets:
            raw_cap = _budget_value_for_placement(p, budget_maps, "raw", 40)
            buffered_cap = int(raw_cap * buf)
            room_type = room_type_for_placement(p, budget_maps=budget_maps)
            room_cap = 0 if room_type == "lab" else buffered_cap
            gender = _section_gender(p.term_section.section) or board_gender
            _s = (
                p.start_time.strftime("%H:%M")
                if hasattr(p.start_time, "strftime")
                else str(p.start_time)[:5]
            )
            _e = (
                p.end_time.strftime("%H:%M")
                if hasattr(p.end_time, "strftime")
                else str(p.end_time)[:5]
            )
            room_code = tracker.assign_best_fit(p.day, _s, room_cap, room_type, gender, end=_e)
            if room_code:
                assigned += 1
            else:
                if room_type != "lab" and tracker.is_feasible(
                    p.day, _s, raw_cap, room_type, gender, end=_e
                ):
                    rejected_by_buffer += 1
                unassigned += 1

        results.append(
            {
                "buffer": buf,
                "assigned": assigned,
                "unassigned": unassigned,
                "rejected_by_buffer_vs_1_0": rejected_by_buffer,
            }
        )

    return {"board_id": board_id, "programmes": programmes, "results": results}

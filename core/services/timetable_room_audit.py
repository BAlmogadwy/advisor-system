"""The quality audit's room rule, in one place.

The quality panel's room rows (Issues' "Room too small", "Room unsuitable")
come from the exact-rooming audit: ``build_meeting_demands`` says what each
physical meeting needs and which rooms are compatible, and the recovery
evaluator's ``_original_room_report`` judges the rooms the meeting holds. This
module is that rule, moved here so the audit and the room candidates of the
Change room dialog call the same code rather than two copies of it:

* the requirement builder (``meeting_room_need``): type, seats, gender and
  the programme pool of every board the meeting belongs to;
* the compatibility predicate (``room_misfits``): the ONE test behind every
  ``MeetingDemand.compatible_rooms``;
* the verdict (``meeting_room_verdict``): missing, split, undersized,
  unsuitable, incompatible;
* ``MeetingRoomAudit``: what the audit will say about one meeting once a given
  room is saved for it.

It is light on purpose: no OR-Tools, and neither ``timetable_workspace`` nor
``timetable_exact_rooming`` is imported (both import this module), so the
workspace can import it without a cycle.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from core.services.course_identity import course_display_code
from core.services.timetable_course_policy import get_course_meeting_policy
from core.services.timetable_online import OnlineCourseLookup
from core.services.timetable_rooming import (
    _budget_value_for_placement,
    _section_gender,
    get_board_gender,
    room_type_for_placement,
)


class ExactRoomingError(Exception):
    """A scenario the exact room pass refuses to reason about (fail closed)."""


#: The days a physical meeting may fall on; any other day fails the audit.
AUDIT_DAYS = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")

#: Seats a meeting needs when no budget row matches its course (not buffered).
DEFAULT_ROOM_NEED = 40


class _Room(Protocol):
    room_code: Any
    capacity: Any
    room_type: Any
    section: Any
    department: Any


def hhmm(value: object) -> str:
    """``HH:MM`` of a time or a free-text placement time (its first 5 characters)."""
    return value.strftime("%H:%M") if hasattr(value, "strftime") else str(value)[:5]


def to_min(value: object) -> int:
    """Parse ``HH:MM`` to minutes, or raise ``ExactRoomingError`` (fail closed).

    ``SectionPlacement.start_time`` / ``end_time`` are free-text fields, so a
    malformed or empty value must surface as the typed error the endpoint turns
    into a clean 422 — never a raw ``ValueError`` that escapes as a 500.
    """
    text = hhmm(value)
    try:
        hh, mm = text.split(":")
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError) as exc:
        raise ExactRoomingError(f"Un-parseable placement time {value!r}.") from exc


# ── The inventory ─────────────────────────────────────────────────────────


def audit_room_row(room: _Room) -> dict[str, Any] | None:
    """One ``Room`` as the audit reads it; ``None`` for a blank code.

    The code is upper-cased, the type lower-cased ("lecture" only when the
    field is empty, so a type of spaces is ""), the gender upper-cased, and the
    department split into a programme set (an empty one fits no board).
    """
    code = str(room.room_code or "").strip().upper()
    if not code:
        return None
    return {
        "room_code": code,
        "capacity": int(room.capacity or 0),
        "room_type": str(room.room_type or "lecture").strip().lower(),
        "section": str(room.section or "").strip().upper(),
        "programmes": frozenset(
            p.strip().upper() for p in str(room.department or "").split(",") if p.strip()
        ),
    }


def audit_room_inventory(
    rooms: Iterable[_Room],
) -> tuple[dict[str, dict[str, Any]], frozenset[str]]:
    """``(rows_by_code, duplicate codes)``: the first row per code, in the given order.

    The order matters only when a code repeats, and a repeated code makes the
    audit refuse the scenario, so a caller may pass the rows it already read
    in any order.
    """
    rows_by_code: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for room in rooms:
        row = audit_room_row(room)
        if row is None:
            continue
        code = row["room_code"]
        if code in rows_by_code:
            duplicates.add(code)
            continue
        rows_by_code[code] = row
    return rows_by_code, frozenset(duplicates)


# ── The meeting ───────────────────────────────────────────────────────────


def board_gender_memo() -> Callable[[int], str]:
    """``get_board_gender`` read once per board for the caller's lifetime."""
    genders: dict[int, str] = {}

    def board_gender(board_id: int) -> str:
        if board_id not in genders:
            genders[board_id] = get_board_gender(board_id)
        return genders[board_id]

    return board_gender


def board_programmes(board: Any) -> frozenset[str]:
    """A board's programme set, upper-cased; empty when it has none."""
    return frozenset(v.strip().upper() for v in str(board.program or "").split(",") if v.strip())


def audit_skips(placement: Any, online: OnlineCourseLookup) -> bool:
    """True when the audit leaves this row out: a policy meeting that uses no
    shared room, or a course online on the row's board (by its display code)."""
    ts = placement.term_section
    policy = get_course_meeting_policy(ts.course_code, ts.course_key, ts.course_number)
    if policy and not policy.uses_shared_room:
        return True
    return online.is_online_course_for_board(
        placement.board, course_display_code(ts.course_key, ts.course_code, ts.course_number)
    )


def meeting_key(
    placement: Any,
    *,
    day: object | None = None,
    start: object | None = None,
    end: object | None = None,
) -> tuple[int, str, str, str]:
    """The audit's physical-meeting key: ``(term_section_id, DAY, HH:MM, HH:MM)``.

    ``day``/``start``/``end`` stand in for the row's own (a row about to move).
    Copies of a section shared across boards at one slot share the key.
    """
    return (
        placement.term_section_id,
        str(placement.day if day is None else day).strip().upper(),
        hhmm(placement.start_time if start is None else start),
        hhmm(placement.end_time if end is None else end),
    )


@dataclass(frozen=True, slots=True)
class MeetingRoomNeed:
    """What one physical meeting requires of its room."""

    required_type: str
    #: Buffered budget demand; ``DEFAULT_ROOM_NEED`` when no budget row matches.
    required_capacity: int
    #: "budget" or "default" (no budget row matched the course).
    capacity_source: str
    #: "M", "F" or "" (any).
    required_gender: str
    #: One programme set per board in the meeting; a room must serve EVERY one.
    programmes_per_board: tuple[frozenset[str], ...]


def meeting_room_need(
    rows: Sequence[Any],
    *,
    day: str,
    start_hhmm: str,
    end_hhmm: str,
    budget_maps: dict[str, dict],
    board_gender: Callable[[int], str],
) -> MeetingRoomNeed:
    """The requirement of the meeting made of ``rows`` (its copies, lowest id first).

    The first row is the sample for the type and the seats. Gender is each
    row's section label, else its board's; boards that disagree are a data
    defect and raise ``ExactRoomingError`` (fail closed).
    """
    sample = rows[0]
    required_type = room_type_for_placement(
        sample, start_time=start_hhmm, end_time=end_hhmm, budget_maps=budget_maps
    )
    budgeted = _budget_value_for_placement(sample, budget_maps, "buffered", None)

    # Gender: per-section first, board second. A shared meeting whose boards
    # disagree on gender is a data defect; fail closed on that meeting.
    genders = set()
    for p in rows:
        genders.add(_section_gender(p.term_section.section) or board_gender(p.board_id))
    if len({g for g in genders if g}) > 1:
        raise ExactRoomingError(
            f"Meeting {sample.term_section.course_code}|{sample.term_section.section} "
            f"on {day} {start_hhmm} spans boards with conflicting gender {sorted(genders)}."
        )
    required_gender = next(iter(g for g in genders if g), "")

    # Programme (H14): the auditor validates EACH placement against its own
    # board's programme, so the chosen room must serve EVERY constituent
    # board — an intersection over boards, not a union.
    per_board_programmes = {board_programmes(p.board) for p in rows}
    return MeetingRoomNeed(
        required_type=required_type,
        required_capacity=int(DEFAULT_ROOM_NEED if budgeted is None else budgeted),
        capacity_source="default" if budgeted is None else "budget",
        required_gender=required_gender,
        programmes_per_board=tuple(sorted(per_board_programmes, key=sorted)),
    )


# ── The predicate ─────────────────────────────────────────────────────────

MISFIT_TYPE, MISFIT_GENDER, MISFIT_PROGRAMME = 1, 2, 4
_MISFIT_NAMES = ((MISFIT_TYPE, "type"), (MISFIT_GENDER, "gender"), (MISFIT_PROGRAMME, "programme"))


def programme_fits(programmes: frozenset[str], room: Mapping[str, Any]) -> bool:
    """A room serves a board when the board's programmes meet its department."""
    return bool(programmes & room["programmes"])


def room_misfits(room: Mapping[str, Any], need: MeetingRoomNeed) -> int:
    """How ``room`` fails ``need`` as ``MISFIT_*`` bits; 0 = compatible.

    Type, gender (exact: a room with no gender fails a gendered meeting) and the
    pool of every board. Capacity is not part of it: the audit measures seats
    separately (``undersized``).
    """
    # Runs once per (meeting, room) in every audit: a plain loop, no generator
    # (a generator here tripled the cost of a scenario's domain build).
    bits = 0
    if room["room_type"] != need.required_type:
        bits = MISFIT_TYPE
    gender = need.required_gender
    if gender and room["section"] != gender:
        bits |= MISFIT_GENDER
    for programmes in need.programmes_per_board:
        if not programme_fits(programmes, room):
            return bits | MISFIT_PROGRAMME
    return bits


def misfit_reasons(bits: int) -> tuple[str, ...]:
    """The names of ``bits``, in the order type, gender, programme."""
    return tuple(name for flag, name in _MISFIT_NAMES if bits & flag)


# ── The verdict ───────────────────────────────────────────────────────────


def actual_rooms(rooms: Iterable[object]) -> frozenset[str]:
    """The real rooms among ``rooms``: normalised, without "" and UNASSIGNED."""
    return frozenset(str(r or "").strip().upper() for r in rooms) - {"", "UNASSIGNED"}


def meeting_room_verdict(
    rooms: Iterable[object],
    *,
    required_capacity: int,
    compatible_rooms: Collection[str],
    capacities: Mapping[str, int],
) -> frozenset[str]:
    """What the audit says about a meeting whose copies hold ``rooms``.

    A subset of: ``missing`` (a copy has no room), ``split`` (the copies hold
    more than one room), ``undersized`` (a known room has fewer seats than
    required), ``unsuitable`` (split, or a room outside ``compatible_rooms`` —
    a room the inventory does not know included) and ``incompatible``
    (unsuitable or undersized, where an unknown room has no seats).
    """
    codes = {str(r or "").strip().upper() for r in rooms}
    verdict = set()
    if "" in codes or "UNASSIGNED" in codes:
        verdict.add("missing")
    actual = codes - {"", "UNASSIGNED"}
    if len(actual) > 1:
        verdict.add("split")
    if any(r in capacities and capacities[r] < required_capacity for r in actual):
        verdict.add("undersized")
    if len(actual) > 1 or any(r not in compatible_rooms for r in actual):
        verdict.add("unsuitable")
    if len(actual) > 1 or any(
        r not in compatible_rooms or capacities.get(r, 0) < required_capacity for r in actual
    ):
        verdict.add("incompatible")
    return frozenset(verdict)


#: Verdict -> the quality payload's reason, in the payload's order (``clash``,
#: from the scenario-wide overlap check, follows them).
QUALITY_REASON = {"missing": "missing", "undersized": "undersized", "unsuitable": "incompatible"}


def quality_reasons(verdict: Collection[str]) -> list[str]:
    """The quality payload's reasons for ``verdict``, ``clash`` aside."""
    return [reason for name, reason in QUALITY_REASON.items() if name in verdict]


# ── One meeting, one room ─────────────────────────────────────────────────


class MeetingRoomAudit:
    """What the quality audit will say about one meeting once a room is saved.

    ``kept_rooms`` are the raw rooms of the meeting's copies the save leaves
    where they are; ``boards`` are ``(board_id, programmes)`` for every board
    the meeting is on after the save.
    """

    def __init__(
        self,
        need: MeetingRoomNeed,
        rows_by_code: Mapping[str, Mapping[str, Any]],
        *,
        kept_rooms: Sequence[object] = (),
        boards: Sequence[tuple[int, frozenset[str]]] = (),
    ) -> None:
        self.need = need
        self.rows_by_code = rows_by_code
        self.kept_rooms = tuple(kept_rooms)
        self.boards = tuple(boards)
        self.misfits = {code: room_misfits(row, need) for code, row in rows_by_code.items()}
        self.compatible = frozenset(code for code, bits in self.misfits.items() if not bits)
        self.capacities = {code: int(row["capacity"]) for code, row in rows_by_code.items()}

    def _verdict(self, rooms: Iterable[object]) -> frozenset[str]:
        return meeting_room_verdict(
            rooms,
            required_capacity=self.need.required_capacity,
            compatible_rooms=self.compatible,
            capacities=self.capacities,
        )

    def choice(self, room_code: object) -> dict[str, Any]:
        """The audit's answer for saving ``room_code`` (normalised) for this meeting.

        ``reasons``, in order: the room's own misfits (type, gender, programme),
        ``capacity`` (the room alone is too small), ``split`` (it and the kept
        copies' rooms make more than one room) and ``unroomed_copy`` (a kept
        copy has no room). ``quality_reasons`` is exactly the reasons the
        quality panel will show for the meeting after the save, ``clash``
        aside.
        """
        code = str(room_code or "").strip().upper()
        row = self.rows_by_code.get(code)
        alone = self._verdict([code])
        after = self._verdict([code, *self.kept_rooms])
        reasons = list(misfit_reasons(self.misfits.get(code, 0)))
        if "undersized" in alone:
            reasons.append("capacity")
        if "split" in after:
            reasons.append("split")
        if "missing" in after:
            reasons.append("unroomed_copy")
        return {
            "compatible": code in self.compatible,
            "undersized": "undersized" in alone,
            "reasons": reasons,
            "programme_board_ids": [
                board_id
                for board_id, programmes in self.boards
                if row is not None and not programme_fits(programmes, row)
            ],
            "quality_reasons": quality_reasons(after),
        }

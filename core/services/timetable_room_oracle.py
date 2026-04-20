"""PR2 — room-feasibility oracle: typed failure reasons + staged helpers.

Module-only step (commit 2 in the PR2 plan). Lands:

- Six stable reason-code sentinels.
- ``RoomFailureReason`` frozen dataclass with a ``.to_dict()`` shape
  aligned to PR1's ``RejectionReason`` (``reason``, ``day``,
  ``start_time``, ``end_time``, ``course_code``, ``section_code``,
  ``context``).
- ``is_room_oracle_enabled()`` helper reading
  ``TIMETABLE_PR2_ROOM_ORACLE_ENABLED`` (default ``False``).
- Six stub helpers — one per non-heuristic reason code plus the
  observational heuristic-mismatch detector. Each is flag-gated: when
  ``TIMETABLE_PR2_ROOM_ORACLE_ENABLED`` is ``False`` every helper
  returns ``None`` without enforcing anything. When the flag is on the
  helpers still return ``None`` in this commit — the actual Stage 1/2
  checks land in commit 4. The commit-2 contract is the API surface,
  not the enforcement logic.

No wiring into ``timetable_rooming`` / ``timetable_autoplace`` /
``timetable_room_repair`` in this commit. The silent-to-typed swap at
the four known call-sites is commit 3; the staged oracle (Stage 1
existence check + Stage 2 buffer-aware split) is commit 4.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from django.conf import settings

# ---------------------------------------------------------------------------
# Reason-code sentinels.
#
# Stable strings for payload consumers (tests, reports, the management
# command added in commit 5, and future dashboards). Renaming any of
# these is a breaking change — the strings are public API the moment
# commit 2 lands.
# ---------------------------------------------------------------------------

NO_ROOM_CAPACITY = "NO_ROOM_CAPACITY"
NO_ROOM_GENDER = "NO_ROOM_GENDER"
NO_ROOM_TYPE = "NO_ROOM_TYPE"
ROOM_OCCUPIED = "ROOM_OCCUPIED"
ROOM_BUFFER_REJECT = "ROOM_BUFFER_REJECT"
ROOM_HEURISTIC_MISMATCH = "ROOM_HEURISTIC_MISMATCH"


@dataclass(frozen=True)
class RoomFailureReason:
    """A single rooming-side infeasibility record.

    Same frozen / JSON-serialisable shape as PR1's ``RejectionReason`` so
    the two payload families are interchangeable for downstream consumers.
    The extra ``section_code`` field (absent from ``RejectionReason``)
    exists because rooming failures are per-section, not per-placement —
    a given course can have several sections, and the oracle needs to
    identify which one failed.
    """

    code: str
    day: str
    start_time: str
    end_time: str
    course_code: str = ""
    section_code: str = ""
    context: dict | None = None

    def to_dict(self) -> dict:
        """Serialise to the PR1-aligned payload shape.

        Field order, verbatim: ``reason``, ``day``, ``start_time``,
        ``end_time``, ``course_code``, ``section_code``, ``context``.
        ``context`` is omitted when ``None`` so empty-context failures
        don't carry a misleading key.
        """
        payload: dict[str, object] = {
            "reason": self.code,
            "day": self.day,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "course_code": self.course_code,
            "section_code": self.section_code,
        }
        if self.context is not None:
            payload["context"] = self.context
        return payload


# ---------------------------------------------------------------------------
# Flag read.
# ---------------------------------------------------------------------------


def is_room_oracle_enabled() -> bool:
    """Return whether the staged room-feasibility oracle is enabled.

    Reads ``settings.TIMETABLE_PR2_ROOM_ORACLE_ENABLED`` (default
    ``False``). When this returns ``False`` every oracle helper in
    this module returns ``None`` without running its check.
    """
    return bool(getattr(settings, "TIMETABLE_PR2_ROOM_ORACLE_ENABLED", False))


# ---------------------------------------------------------------------------
# Stub helpers.
#
# Each helper returns a ``RoomFailureReason`` when its specific check
# fails, else ``None``. When ``TIMETABLE_PR2_ROOM_ORACLE_ENABLED`` is
# ``False`` every helper returns ``None`` without enforcing anything.
#
# Commit 2 deliberately leaves the enforcement logic empty: the helpers
# are callable and respect the flag, but their flag-on path is a no-op
# too. Commit 4 fills in Stage 1 (metadata existence) and Stage 2
# (buffer-aware rejection accounting).
# ---------------------------------------------------------------------------


def check_gender_feasibility(
    section: dict,
    rooms: Iterable[dict],
) -> RoomFailureReason | None:
    """Returns a ``RoomFailureReason`` with code ``NO_ROOM_GENDER`` when
    no room in ``rooms`` matches the section's required gender, else
    ``None``.

    When ``TIMETABLE_PR2_ROOM_ORACLE_ENABLED`` is ``False``, returns
    ``None`` without enforcing anything. The enforcement body lands in
    commit 4 (Stage 1 existence check).
    """
    if not is_room_oracle_enabled():
        return None
    return None


def check_type_feasibility(
    section: dict,
    rooms: Iterable[dict],
) -> RoomFailureReason | None:
    """Returns a ``RoomFailureReason`` with code ``NO_ROOM_TYPE`` when
    no room in ``rooms`` matches the section's required type (lecture /
    lab), else ``None``.

    When ``TIMETABLE_PR2_ROOM_ORACLE_ENABLED`` is ``False``, returns
    ``None`` without enforcing anything. The enforcement body lands in
    commit 4 (Stage 1 existence check).
    """
    if not is_room_oracle_enabled():
        return None
    return None


def check_capacity_feasibility(
    section: dict,
    rooms: Iterable[dict],
    capacity_buffer: float,
) -> RoomFailureReason | None:
    """Returns a ``RoomFailureReason`` with code ``NO_ROOM_CAPACITY``
    when no room in ``rooms`` has enough capacity (demand × buffer) to
    seat the section, else ``None``.

    When ``TIMETABLE_PR2_ROOM_ORACLE_ENABLED`` is ``False``, returns
    ``None`` without enforcing anything. The enforcement body lands in
    commit 4 (Stage 1 existence check).
    """
    if not is_room_oracle_enabled():
        return None
    return None


def check_occupancy(
    section: dict,
    rooms: Iterable[dict],
    occupancy_at_slot: set[str],
) -> RoomFailureReason | None:
    """Returns a ``RoomFailureReason`` with code ``ROOM_OCCUPIED`` when
    every otherwise-eligible room is already busy at the section's
    ``(day, start_time)`` slot, else ``None``.

    When ``TIMETABLE_PR2_ROOM_ORACLE_ENABLED`` is ``False``, returns
    ``None`` without enforcing anything. The enforcement body lands in
    commit 4.
    """
    if not is_room_oracle_enabled():
        return None
    return None


def check_buffer_fit(
    section: dict,
    room: dict,
    capacity_buffer: float,
) -> RoomFailureReason | None:
    """Returns a ``RoomFailureReason`` with code ``ROOM_BUFFER_REJECT``
    when ``room`` fits the section's raw demand but fails the configured
    capacity buffer, else ``None``.

    When ``TIMETABLE_PR2_ROOM_ORACLE_ENABLED`` is ``False``, returns
    ``None`` without enforcing anything. The enforcement body lands in
    commit 4 (Stage 2 buffer-aware split).
    """
    if not is_room_oracle_enabled():
        return None
    return None


def check_heuristic_match(
    section: dict,
    rooms: Iterable[dict],
) -> RoomFailureReason | None:
    """Returns a ``RoomFailureReason`` with code
    ``ROOM_HEURISTIC_MISMATCH`` when the current
    ``duration > 80 and cr == 4`` lab-classification heuristic and the
    autoplace-side ``is_lab_course`` check disagree on the section's
    room type, else ``None``.

    Observation only — this helper never changes a placement decision
    in PR2. It surfaces the heuristic's blast radius so a future PR can
    replace the heuristic with measured impact data.

    When ``TIMETABLE_PR2_ROOM_ORACLE_ENABLED`` is ``False``, returns
    ``None`` without enforcing anything. The enforcement body lands in
    commit 4.
    """
    if not is_room_oracle_enabled():
        return None
    return None

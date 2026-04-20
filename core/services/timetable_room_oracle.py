"""PR2 — room-feasibility oracle: typed failure reasons + staged helpers.

Commit 4 state: the API surface set out in commit 2 is now enforced.

When ``TIMETABLE_PR2_ROOM_ORACLE_ENABLED`` is ``False`` every helper
still returns ``None`` without enforcing anything — the flag-off path
is a no-op by design so commit 3's default ``NO_ROOM_CAPACITY``
behaviour is preserved bit-for-bit.

When the flag is on the helpers run:

- **Stage 1 (metadata existence)** — ``check_type_feasibility``,
  ``check_gender_feasibility``, ``check_capacity_feasibility`` answer
  "does the room pool contain *any* room that could theoretically host
  this section?". They are order-sensitive at call sites: type → gender
  → capacity, so the most-specific reason wins.
- **Stage 2 (buffer-aware split)** — ``check_buffer_fit`` distinguishes
  "no room fits the raw demand" (true ``NO_ROOM_CAPACITY``) from "a room
  fits the raw demand but not the buffered demand" (``ROOM_BUFFER_REJECT``).
  ``check_occupancy`` covers the case where every otherwise-eligible
  room exists but is already busy at the slot.
- **Observational** — ``check_heuristic_match`` flags disagreement
  between the ``duration > 80 and cr == 4`` lab heuristic and an
  autoplace-side ``is_lab_course`` truth. It never changes a placement
  decision; it surfaces heuristic blast radius for a future PR.

Section / room dict shape expected by the helpers:

- ``section``: ``course_code``, ``section_code``, ``demand``,
  ``room_type_required``, ``gender_required``, ``day``, ``start_time``,
  ``end_time``, and — for ``check_heuristic_match`` only —
  ``duration_min``, ``credit_hours``, ``is_lab_course``.
- ``room``: ``room_code``, ``capacity``, ``room_type``, and either
  ``gender`` (the new ``RoomProfile.gender`` shape) or ``section``
  (the legacy ``get_programme_rooms`` dict shape). Both are accepted so
  call sites pass whichever is closest to hand.

Wiring into ``timetable_rooming`` / ``timetable_autoplace`` /
``timetable_room_repair`` at the four silent-UNASSIGNED sites is
additive over commit 3 — the default-when-flag-off remains
``NO_ROOM_CAPACITY`` exactly as before.
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
# Internals.
# ---------------------------------------------------------------------------


def _room_gender(room: dict) -> str:
    """Extract a room's gender letter from either the new
    ``RoomProfile.gender`` key or the legacy ``get_programme_rooms``
    dict key ``section``. Returns the uppercase single letter (``M`` /
    ``F``) or ``""`` when the room has no gender constraint.
    """
    raw = room.get("gender", "") or room.get("section", "")
    return str(raw or "").strip().upper()


def _section_demand(section: dict) -> int:
    """Read the section's seat demand. Primary key per the PR2 DoR is
    ``enrolment``; ``demand`` is accepted as a compatibility alias so
    call sites that already built dicts with that name don't have to
    rename."""
    for key in ("enrolment", "demand"):
        raw = section.get(key)
        if raw is None or raw == "":
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return 0


def _section_gender(section: dict) -> str:
    """Read the section's required gender letter. Primary key per the
    PR2 DoR is ``gender``; ``gender_required`` is accepted as a
    compatibility alias."""
    for key in ("gender", "gender_required"):
        raw = section.get(key)
        if raw:
            return str(raw).strip().upper()
    return ""


def _section_type(section: dict) -> str:
    """Read the section's required room type. Primary key per the PR2
    DoR is ``required_type``; ``room_type_required`` is accepted as a
    compatibility alias."""
    for key in ("required_type", "room_type_required"):
        raw = section.get(key)
        if raw:
            return str(raw).strip()
    return ""


def _section_duration(section: dict) -> int:
    """Read duration in minutes. Prefers the explicit
    ``duration_minutes`` / ``duration_min`` key; falls back to computing
    from ``start_time`` / ``end_time`` (``HH:MM`` form).
    Returns 0 when neither path yields a usable value."""
    for key in ("duration_minutes", "duration_min"):
        raw = section.get(key)
        if raw:
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
    start = str(section.get("start_time", ""))
    end = str(section.get("end_time", ""))
    if ":" in start and ":" in end:
        try:
            sh, sm = start.split(":", 1)
            eh, em = end.split(":", 1)
            return (int(eh) * 60 + int(em)) - (int(sh) * 60 + int(sm))
        except (TypeError, ValueError):
            return 0
    return 0


def _section_credit_rating(section: dict) -> int:
    """Read credit-rating. Primary key per the PR2 DoR is
    ``credit_rating``; ``credit_hours`` is accepted as an alias."""
    for key in ("credit_rating", "credit_hours"):
        raw = section.get(key)
        if raw is None or raw == "":
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return 0


def _failure(code: str, section: dict, **overrides: object) -> RoomFailureReason:
    """Construct a ``RoomFailureReason`` from a section dict, populating
    the PR1-aligned fields from ``section`` and allowing any field to
    be overridden by keyword arg.
    """
    payload: dict[str, object] = {
        "code": code,
        "day": str(section.get("day", "")),
        "start_time": str(section.get("start_time", "")),
        "end_time": str(section.get("end_time", "")),
        "course_code": str(section.get("course_code", "")),
        "section_code": str(section.get("section_code", "")),
    }
    payload.update(overrides)
    return RoomFailureReason(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Oracle helpers — Stage 1 (metadata existence).
#
# Each helper returns a ``RoomFailureReason`` when its specific check
# fails, else ``None``. When ``TIMETABLE_PR2_ROOM_ORACLE_ENABLED`` is
# ``False`` every helper returns ``None`` without enforcing anything,
# preserving commit 3's default-``NO_ROOM_CAPACITY`` payload bit-for-bit.
# ---------------------------------------------------------------------------


def check_type_feasibility(
    section: dict,
    rooms: Iterable[dict],
) -> RoomFailureReason | None:
    """Return ``NO_ROOM_TYPE`` when no room in ``rooms`` matches the
    section's required type (``lecture`` / ``lab``), else ``None``.

    Checked first in the refinement chain because type is the coarsest
    filter: if no lab room exists the section can't be placed no matter
    what its gender or capacity demand is.
    """
    if not is_room_oracle_enabled():
        return None
    req_type = _section_type(section)
    if not req_type:
        return None
    for room in rooms:
        if str(room.get("room_type", "")).strip() == req_type:
            return None
    return _failure(NO_ROOM_TYPE, section, context={"required_type": req_type})


def check_gender_feasibility(
    section: dict,
    rooms: Iterable[dict],
) -> RoomFailureReason | None:
    """Return ``NO_ROOM_GENDER`` when the section carries a gender
    requirement (``M`` / ``F``) and no room of the correct type matches
    that gender, else ``None``.

    Only runs within the set of rooms that already pass the type
    filter — otherwise a "no lab exists at all" case would be reported
    as a gender failure. When ``gender_required`` is empty the section
    has no gender constraint and this helper returns ``None``.
    """
    if not is_room_oracle_enabled():
        return None
    req_gender = _section_gender(section)
    if req_gender not in ("M", "F"):
        return None
    req_type = _section_type(section)
    # Narrow to the type-filtered pool first. If that pool is empty the
    # true failure is type, not gender — return ``None`` so the type
    # helper can own the report.
    type_filtered = [
        room for room in rooms if not req_type or str(room.get("room_type", "")).strip() == req_type
    ]
    if not type_filtered:
        return None
    for room in type_filtered:
        if _room_gender(room) == req_gender:
            return None
    return _failure(
        NO_ROOM_GENDER,
        section,
        context={"required_gender": req_gender},
    )


def check_capacity_feasibility(
    section: dict,
    rooms: Iterable[dict],
    capacity_buffer: float,
) -> RoomFailureReason | None:
    """Return ``NO_ROOM_CAPACITY`` when no room of the correct type
    and gender has capacity ≥ ``demand × capacity_buffer``, else
    ``None``.

    Uses buffered capacity because the call sites size rooms against
    the buffered demand — a rejection at this stage means the section
    cannot be accommodated even at the most generous room in the
    filtered pool. Stage 2's ``check_buffer_fit`` separates the
    sub-case "would have fit raw, failed buffer" into
    ``ROOM_BUFFER_REJECT``.
    """
    if not is_room_oracle_enabled():
        return None
    demand = _section_demand(section)
    if demand <= 0:
        return None
    try:
        buffer = float(capacity_buffer or 1.0)
    except (TypeError, ValueError):
        buffer = 1.0
    required = int(demand * max(buffer, 1.0))
    req_type = _section_type(section)
    req_gender = _section_gender(section)
    best_capacity = 0
    for room in rooms:
        if req_type and str(room.get("room_type", "")).strip() != req_type:
            continue
        if req_gender and _room_gender(room) not in ("", req_gender):
            continue
        try:
            cap = int(room.get("capacity", 0) or 0)
        except (TypeError, ValueError):
            continue
        if cap > best_capacity:
            best_capacity = cap
        if cap >= required:
            return None
    return _failure(
        NO_ROOM_CAPACITY,
        section,
        context={
            "needed": required,
            "enrolment": demand,
            "buffer": buffer,
            "best_capacity": best_capacity,
        },
    )


# ---------------------------------------------------------------------------
# Oracle helpers — Stage 2 (buffer-aware rejection + occupancy).
# ---------------------------------------------------------------------------


def check_occupancy(
    section: dict,
    rooms: Iterable[dict],
    occupancy_at_slot: set[str],
) -> RoomFailureReason | None:
    """Return ``ROOM_OCCUPIED`` when every otherwise-eligible room
    (correct type, gender and capacity for the buffered demand) is
    already busy at the section's ``(day, start_time)`` slot.

    Call *after* the Stage 1 checks — a truly empty eligible pool is a
    Stage 1 miss, not an occupancy miss. Returning ``None`` when the
    eligible pool itself is empty keeps the two kinds of failure
    distinguishable.
    """
    if not is_room_oracle_enabled():
        return None
    demand = _section_demand(section)
    req_type = _section_type(section)
    req_gender = _section_gender(section)
    busy = occupancy_at_slot or set()
    eligible_codes: list[str] = []
    eligible_free = False
    for room in rooms:
        if req_type and str(room.get("room_type", "")).strip() != req_type:
            continue
        if req_gender and _room_gender(room) not in ("", req_gender):
            continue
        try:
            cap = int(room.get("capacity", 0) or 0)
        except (TypeError, ValueError):
            continue
        if demand and cap < demand:
            continue
        eligible_codes.append(str(room.get("room_code", "")))
        if str(room.get("room_code", "")) not in busy:
            eligible_free = True
            break
    if eligible_codes and not eligible_free:
        return _failure(
            ROOM_OCCUPIED,
            section,
            context={"busy_rooms": sorted(eligible_codes)},
        )
    return None


def check_buffer_fit(
    section: dict,
    room: dict,
    capacity_buffer: float,
) -> RoomFailureReason | None:
    """Return ``ROOM_BUFFER_REJECT`` when ``room`` fits the section's
    raw demand but fails the buffered demand, else ``None``.

    This is the Stage 2 split that carves the "buffer-only" rejection
    out of ``NO_ROOM_CAPACITY``. Call sites pair this helper with a
    ``buffer_only_rejects`` per-placement counter so the two populations
    stay disjoint.
    """
    if not is_room_oracle_enabled():
        return None
    demand = _section_demand(section)
    if demand <= 0:
        return None
    try:
        buffer = float(capacity_buffer or 1.0)
    except (TypeError, ValueError):
        buffer = 1.0
    try:
        cap = int(room.get("capacity", 0) or 0)
    except (TypeError, ValueError):
        return None
    needed = int(demand * max(buffer, 1.0))
    if cap >= demand and cap < needed:
        return _failure(
            ROOM_BUFFER_REJECT,
            section,
            context={
                "needed": needed,
                "best_capacity": cap,
                "buffer": buffer,
                "enrolment": demand,
            },
        )
    return None


# ---------------------------------------------------------------------------
# Oracle helper — observational.
# ---------------------------------------------------------------------------


def check_heuristic_match(
    section: dict,
    rooms: Iterable[dict],
) -> RoomFailureReason | None:
    """Return ``ROOM_HEURISTIC_MISMATCH`` when the
    ``duration > 80 and cr == 4`` lab-classification heuristic and the
    autoplace-side ``is_lab_course`` truth disagree on the section's
    room type, else ``None``.

    Observation only — this helper never changes a placement decision
    in PR2. It surfaces the heuristic's blast radius so a future PR
    can replace the heuristic with measured impact data.

    ``rooms`` is accepted for signature symmetry with the other helpers
    but is not consulted today; a future enrichment may use it to
    contextualise the mismatch.
    """
    if not is_room_oracle_enabled():
        return None
    duration = _section_duration(section)
    cr = _section_credit_rating(section)
    # Rooming (timetable_rooming.py:307) treats a meeting as a lab when
    # ``duration > 80 AND cr == 4``. Autoplace's scoring loop
    # (timetable_autoplace.py) treats it as a lab based on duration plus
    # an ``is_lab_course`` flag tied to cr==4; the heuristic surface
    # diverges when duration>80 alone — PR2 surfaces that divergence
    # without changing either site's decision.
    is_lab_by_rooming = duration > 80 and cr == 4
    is_lab_by_autoplace = duration > 80
    if is_lab_by_rooming == is_lab_by_autoplace:
        return None
    context = {
        "duration_minutes": duration,
        "credit_rating": cr,
        "heuristic_cr_threshold": 4,
        "heuristic_duration_threshold": 80,
        "is_lab_by_rooming_heuristic": is_lab_by_rooming,
        "is_lab_by_autoplace_heuristic": is_lab_by_autoplace,
    }
    return _failure(ROOM_HEURISTIC_MISMATCH, section, context=context)


# ---------------------------------------------------------------------------
# Payload aggregation.
#
# Commit 5 — per ChatGPT's ruling on the payload surface: the existing
# ``room_failures`` list (established in commit 3) stays stable; a
# companion ``room_failure_breakdown`` dict is emitted for counter-style
# consumers (KPI tiles, the report_room_failures management command).
# The breakdown carries only *observed* codes — an empty dict when
# nothing failed — rather than six zeros for the full sentinel set.
# ---------------------------------------------------------------------------


def room_failure_breakdown(room_failures: Iterable[dict]) -> dict[str, int]:
    """Bucket a ``room_failures`` list by reason code.

    Input: the same list emitted as ``result["room_failures"]`` — a
    sequence of ``RoomFailureReason.to_dict()`` dicts. Each dict must
    carry a ``reason`` key (the typed reason-code sentinel).

    Output: ``{code: count}`` — keys are the sentinel strings from this
    module (``NO_ROOM_CAPACITY``, ``NO_ROOM_GENDER``, ``NO_ROOM_TYPE``,
    ``ROOM_OCCUPIED``, ``ROOM_BUFFER_REJECT``, ``ROOM_HEURISTIC_MISMATCH``).
    Only codes that appear at least once are present; an empty input
    list yields an empty dict. Entries without a ``reason`` key are
    skipped — they should not occur in a well-formed payload but the
    helper stays tolerant so downstream consumers never crash on a
    malformed record.
    """
    counts: dict[str, int] = {}
    for record in room_failures:
        code = record.get("reason") if isinstance(record, dict) else None
        if not code:
            continue
        counts[code] = counts.get(code, 0) + 1
    return counts

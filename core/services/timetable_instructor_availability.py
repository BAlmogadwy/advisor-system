"""Teaching-time rules as the classic timetable engine sees them.

The rules themselves (kinds, semantics, the loader) live in
:mod:`core.services.instructor_availability`, which the ``scheduler`` app may
import too. This module is the classic engine's ONE door to them:

* :func:`rules_enabled` is the only reader of ``TIMETABLE_INSTRUCTOR_AVAILABILITY``
  (``timetable_flags.is_instructor_availability_enabled``); the rules also need
  one teacher per section, since they bind the section's own teacher.
* :func:`availability_for` gives a stage the availability of a scenario's
  teachers: ``EMPTY_AVAILABILITY`` (falsy, zero queries) when the rules are off,
  else one query per :func:`availability_scope` (a run, a job, a request). Every
  stage hook is ``if av:`` on what it returns, so with the flag off, or on with
  no rule for any teacher the scenario can charge, every engine output is
  byte-identical.
* the predicates (:func:`rule_ids`, :func:`meetings_blocked`,
  :func:`option_blocked`, :func:`cell_predicate`) are what the stages test with;
  :func:`online_everywhere` / :func:`online_exempt_sections` are the one online
  exemption (a section the rules do not bind is online on EVERY board it is
  placed on).

Manual moves, validation and the slot previews never call anything here.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any

from core.services.instructor_availability import (
    DAY_CODES,
    EMPTY_AVAILABILITY,
    InstructorAvailability,
    load_instructor_availability,
    to_minutes,
)
from core.services.timetable_flags import (
    is_instructor_availability_enabled,
    is_one_teacher_active,
)

_DAY_INDEX = {code: index for index, code in enumerate(DAY_CODES)}

Meeting = tuple[int, int, int]  # (day index, start minute, end minute)


def rules_enabled() -> bool:
    """The rules bind the automatic stages: the flag AND one teacher per section."""
    return is_instructor_availability_enabled() and is_one_teacher_active()


# ── One availability object per run / job / request ──────────────────────


class _Holder:
    """The availability one scope loaded for one scenario (at most one load)."""

    __slots__ = ("loaded", "value")

    def __init__(self) -> None:
        self.loaded = False
        self.value: InstructorAvailability = EMPTY_AVAILABILITY

    def store(self, value: InstructorAvailability) -> None:
        self.loaded = True
        self.value = value


#: ``None`` = "every scenario this request touches" (a holder per scenario id is
#: made on first use, like ``section_teacher_assignment._scope_cache``).
_EVERY_SCENARIO = None
_SCOPES: ContextVar[dict[int | None, _Holder] | None] = ContextVar(
    "instructor_availability_scopes", default=None
)


@contextmanager
def availability_scope(scenario_id: int | None) -> Iterator[None]:
    """Inside, the first :func:`availability_for` of ``scenario_id`` loads (one
    query) and every later call returns that same object.

    Re-entrant: a scope already open for the scenario (or for every scenario)
    is reused. ``None`` opens it for every scenario the block touches, one
    holder per scenario id. No load happens while the rules are disabled.
    """
    scopes = _SCOPES.get()
    if scopes is not None and (scenario_id in scopes or _EVERY_SCENARIO in scopes):
        yield
        return
    new = dict(scopes or {})
    new[scenario_id] = _Holder()
    token = _SCOPES.set(new)
    try:
        yield
    finally:
        _SCOPES.reset(token)


def _holder(scenario_id: int) -> _Holder | None:
    scopes = _SCOPES.get()
    if not scopes:
        return None
    holder = scopes.get(scenario_id)
    if holder is None and _EVERY_SCENARIO in scopes:
        holder = scopes[scenario_id] = _Holder()
    return holder


def availability_for(
    scenario_id: int, *, universe: Iterable[int] | None = None
) -> InstructorAvailability:
    """The teaching-time rules of the teachers ``scenario_id`` can charge.

    ``EMPTY_AVAILABILITY`` with no query unless :func:`rules_enabled`. Inside an
    :func:`availability_scope` the first call loads and every later one returns
    the same object; outside one each call loads. ``universe`` is the teacher
    universe to load for (``TeacherAssignment.universe``: every eligible or
    fixed teacher of every slot); without it the scenario's map is read first,
    and that read's own collect loads the rules, so a call costs one rule query.
    Rules of instructors outside the universe never arrive, so they cannot make
    the availability non-empty.
    """
    if not rules_enabled():
        return EMPTY_AVAILABILITY
    holder = _holder(scenario_id)
    if holder is not None and holder.loaded:
        return holder.value
    if universe is None:
        from core.services.section_teacher_assignment import load_assignment

        with availability_scope(scenario_id):
            assignment = load_assignment(scenario_id, incomplete="collect")
            inner = _holder(scenario_id)
            value = inner.value if inner is not None and inner.loaded else None
        if value is None:
            value = load_instructor_availability(sorted(assignment.universe), scenario=scenario_id)
    else:
        value = load_instructor_availability(sorted(universe), scenario=scenario_id)
    if holder is not None:
        holder.store(value)
    return value


def bound_availability(scenario_id: int | None = None) -> InstructorAvailability:
    """What a scope already loaded, never loading (for hot paths such as the
    evaluator). Without ``scenario_id``: the one scenario loaded in the open
    scopes, else ``EMPTY_AVAILABILITY`` (none, or more than one)."""
    if not rules_enabled():
        return EMPTY_AVAILABILITY
    scopes = _SCOPES.get() or {}
    if scenario_id is not None:
        holder = scopes.get(scenario_id)
        return holder.value if holder is not None and holder.loaded else EMPTY_AVAILABILITY
    loaded = [h for key, h in scopes.items() if key is not _EVERY_SCENARIO and h.loaded]
    return loaded[0].value if len(loaded) == 1 else EMPTY_AVAILABILITY


# ── Predicates ───────────────────────────────────────────────────────────


def _is_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def rule_ids(availability: InstructorAvailability, ids: Iterable[object] | None) -> tuple[int, ...]:
    """The ids among ``ids`` that have a rule, sorted (free-text ids never do).

    ``availability.instructor_ids`` builds a set per read: call this once per
    section, never per option or meeting."""
    if not availability or not ids:
        return ()
    ruled = availability.instructor_ids
    return tuple(sorted({i for i in ids if _is_id(i) and i in ruled}))


def meetings_blocked(
    availability: InstructorAvailability, ids: Iterable[int], meetings: Iterable[Meeting]
) -> bool:
    """Some meeting ``(day index, start, end)`` breaks a rule of some id in ``ids``."""
    if not availability:
        return False
    ids = tuple(ids)
    return any(
        availability.blocks(iid, day, start, end) for day, start, end in meetings for iid in ids
    )


def option_meetings(option: Iterable[Mapping[str, Any]]) -> list[Meeting]:
    """A placer option (``[{"day": "SUN", "start": "HH:MM", "end": ...}]``) as
    ``(day index, start, end)`` meetings; a meeting that does not read is left out."""
    out: list[Meeting] = []
    for meeting in option:
        day = _DAY_INDEX.get(str(meeting.get("day", "")).strip().upper())
        start, end = to_minutes(meeting.get("start")), to_minutes(meeting.get("end"))
        if day is not None and start is not None and end is not None:
            out.append((day, start, end))
    return out


def option_blocked(
    availability: InstructorAvailability, ids: Iterable[int], option: Iterable[Mapping[str, Any]]
) -> bool:
    """Some meeting of a placer option breaks a rule of some id in ``ids``."""
    if not availability:
        return False
    return meetings_blocked(availability, ids, option_meetings(option))


def cell_predicate(
    availability: InstructorAvailability, ids: Iterable[object] | None
) -> Callable[[str, str, str], bool] | None:
    """``(DAY, "HH:MM", "HH:MM") -> True`` when that cell breaks a rule of a ruled
    id in ``ids``; None when none of ``ids`` has a rule (the generator then runs
    exactly as without rules)."""
    ruled = rule_ids(availability, ids)
    if not ruled:
        return None

    def blocked(day: str, start: str, end: str) -> bool:
        index = _DAY_INDEX.get(str(day).strip().upper())
        lo, hi = to_minutes(start), to_minutes(end)
        if index is None or lo is None or hi is None:
            return False
        return any(availability.blocks(iid, index, lo, hi) for iid in ruled)

    return blocked


def prune_relocate_domains(
    availability: InstructorAvailability,
    sections: Sequence[dict],
    valid_options: dict[int, list[list[dict]]],
    charged: Mapping[str, Iterable[object]],
    *,
    exempt: Collection[tuple[str, str]] = frozenset(),
) -> None:
    """Drop, in place, the relocate cells a ruled teacher cannot teach (simulated
    annealing and the load balancer). ``charged`` maps the placer key
    ``"course|label"`` to the section's teachers; ``exempt`` holds the
    ``(course_key, label)`` sections online on every board. A meeting-policy
    section keeps only its legal whole bundles. Sections without a ruled teacher
    keep the very same lists, so the movers' random streams are unchanged."""
    if not availability:
        return
    for index, sec in enumerate(sections):
        if (sec.get("course_key"), sec.get("label")) in exempt:
            continue
        ids = rule_ids(availability, charged.get(f"{sec.get('course_key')}|{sec.get('label')}"))
        if not ids:
            continue
        if sec.get("meeting_policy") is not None:
            sec["bundles"] = [
                bundle
                for bundle in sec.get("bundles") or []
                if not option_blocked(availability, ids, bundle)
            ]
        elif index in valid_options:
            valid_options[index] = [
                [cell for cell in cells if not option_blocked(availability, ids, [cell])]
                for cells in valid_options[index]
            ]


def fixed_section_keys(assignment: Any) -> frozenset[str]:
    """Placer keys (``"course|label"``) of the sections whose teacher the engine
    may not change (a pin or a teacher-frozen section): a rule never leaves them
    unplaced, their best blocked option is placed and reported instead."""
    from core.services.section_teacher_assignment import STATUS_FROZEN, STATUS_PINNED

    return frozenset(
        slot.pr4_key
        for slot in assignment.slots
        if assignment.status.get(slot.key) in (STATUS_PINNED, STATUS_FROZEN)
    )


# ── The online exemption ─────────────────────────────────────────────────


def online_everywhere(flags: Iterable[bool]) -> bool:
    """The one online exemption: a section the rules do not bind is online on
    EVERY board it is placed on (``flags``: one per board); a section on no
    board, or online on some boards only, is bound."""
    flags = list(flags)
    return bool(flags) and all(flags)


def placed_online_flags(scenario_id: int) -> dict[int, tuple[bool, ...]]:
    """``term_section_id -> (online on board, ...)`` for every section placed on
    the scenario's boards, one flag per distinct board programme list, judged
    like every other online consumer (``OnlineCourseLookup``, per board)."""
    return {ts_id: flags for ts_id, _key, flags in _placed_online_rows(scenario_id)}


def online_exempt_sections(scenario_id: int) -> frozenset[tuple[str, str]]:
    """``(course_key, label)`` of the placed sections online on every board
    (``course_key`` is the planner identity, ``course_key or course_code``)."""
    return frozenset(
        key for _ts_id, key, flags in _placed_online_rows(scenario_id) if online_everywhere(flags)
    )


def _placed_online_rows(scenario_id: int) -> list[tuple[int, tuple[str, str], tuple[bool, ...]]]:
    from core.models import SectionPlacement
    from core.services.course_identity import course_display_code
    from core.services.timetable_online import OnlineCourseLookup

    lookup = OnlineCourseLookup()
    flags: dict[int, set[bool]] = {}
    keys: dict[int, tuple[str, str]] = {}
    for ts_id, course_key, code, number, label, program in (
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .values_list(
            "term_section_id",
            "term_section__course_key",
            "term_section__course_code",
            "term_section__course_number",
            "term_section__section",
            "board__program",
        )
        .distinct()
        .order_by("term_section_id", "board__program")
    ):
        keys[ts_id] = (course_key or code, label)
        board = SimpleNamespace(program=program)  # the lookup reads only ``program``
        flags.setdefault(ts_id, set()).add(
            lookup.is_online_course_for_board(board, course_display_code(course_key, code, number))
        )
    return [(ts_id, keys[ts_id], tuple(sorted(flags[ts_id]))) for ts_id in sorted(flags)]


__all__ = [
    "availability_for",
    "availability_scope",
    "bound_availability",
    "cell_predicate",
    "fixed_section_keys",
    "meetings_blocked",
    "online_everywhere",
    "online_exempt_sections",
    "option_blocked",
    "option_meetings",
    "placed_online_flags",
    "prune_relocate_domains",
    "rule_ids",
    "rules_enabled",
]

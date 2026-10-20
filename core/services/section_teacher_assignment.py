"""One teacher per section: who is charged for each section's meetings.

A ``CourseInstructor`` link says who MAY teach a course (owner ruling
2026-10-19: any section of it, for the programmes whose own plan names that
course identity — ``section_instructor_resolver``). Without this module the
engine charged EVERY linked teacher with EVERY section of the course at once.
That invented clashes, over-cap days, working days and idle for people who
teach none of it: in lab scenario 667 53-61% of the reported instructor idle
belonged to nobody, and one lecturer was charged 15 sessions a week, which no
layout can fit into four days. Here each section is charged to exactly one
eligible teacher, or to nobody — "teacher to be decided" (TBD). An unassigned
section is normal (scheduler D5); it is reported as missing coverage, never
silently counted as a better week.

Everything here is behind ``TIMETABLE_ONE_TEACHER_PER_SECTION``
(``timetable_flags.is_one_teacher_active``). With it off no caller reaches this
module and nothing reads ``section_instructors``.

**Rules.** Hard, inside the assignment itself: no teacher clash (half-open
overlap of distinct physical meetings, sibling sections included) and the daily
cap, each only while the board enforces it; at most ``weekly_ceiling()``
sessions a week; and D10 — a course running more than 3 sections gives one
teacher at most 2 of them, fewer for a meeting-policy course. A section not
placed yet is charged only to a teacher who keeps one of its placer options
free of clash and of the daily cap, so the required-section retry can still
place it: on lab scenario 667 the days-first optimum otherwise gave instructor
298 all three AI124 sections, and DS331 S1, which only 298 teaches, had none
of its 190 options left. The greedy placer keeps the rule while it places
(``PlacerGuard``). Soft: the day-off target of 12 sessions a week, then
campus working days, then campus idle, then keeping the current teacher, then
the lowest id. A section nobody can take is TBD with the reason that stopped
each teacher.

**Fixed inputs.** A primary ``SectionInstructor`` row in the scenario whose
``source`` is not ``"engine"`` is a pin (department or registrar decision); the
engine never writes or deletes it. A section this scenario does not own, or
shares with another scenario's board, is teacher-frozen
(``board_ownership.teacher_frozen_section_ids``): its teacher is read from its
owner's row, else its meeting name, and counts as fixed load. A time lock does
NOT fix the teacher.

**Storage.** The engine's picks are primary rows with ``source="engine"``, one
per assigned section. TBD is never stored (``instructor`` is a non-null FK):
the read path re-derives it. Every map the engine persists is closure-complete
on its layout, so reading it back on the same layout returns that map exactly,
TBDs included; and every rule the read path applies to stored rows ignores the
layout, so a manual edit never changes or drops a stored row.

**One map per run.** ``pinned_assignment`` holds one map for a whole run, so a
consumer that reads it from the database sees the very object the others were
handed; ``assignment_scope`` memoises the layout-independent reads for one
request.
"""

from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)

#: D10 (``scheduler.instructors``): sections of one course a teacher may hold
#: once the course runs more than ``D10_APPLIES_ABOVE``. Pinned equal by a test;
#: ``core`` must not import ``scheduler`` at module level.
D10_MAX_SECTIONS = 2
D10_APPLIES_ABOVE = 3
#: Owner ruling: the weekly ceiling (H8: 3 sessions a day x 5 days).
WEEKLY_SESSION_CEILING = 15
#: The day-off target (3 a day x 4 days). SOFT only: an objective stage.
SOFT_WEEKLY_SESSIONS = 12

SOURCE_ENGINE = "engine"
SOURCE_DEPARTMENT = "department"
SOURCE_REGISTRAR = "registrar"
#: A frozen section's teacher carried into a scenario copy as a pin.
SOURCE_COPIED_FROZEN = "copied_frozen"
ROLE_PRIMARY = "primary"

STATUS_ENGINE = "engine"
STATUS_PINNED = "pinned"
STATUS_FROZEN = "frozen"
STATUS_FROZEN_UNKNOWN = "frozen_unknown"
STATUS_TBD = "tbd"
STATUS_NO_LINKS = "no_links"
CHARGED = frozenset({STATUS_ENGINE, STATUS_PINNED, STATUS_FROZEN})

#: TBD reasons, in the order a teacher's first blocker is looked for.
TBD_REASONS = ("d10", "weekly_ceiling", "clash", "daily_cap", "bound")
_LAYOUT_REASONS = frozenset({"clash", "daily_cap", "bound"})
_STAGES = ("L1", "L2", "L3", "L4", "L5", "L6", "L7")


def daily_cap() -> int:
    """The board's own daily session cap (``TIMETABLE_INSTRUCTOR_DAILY_CAP``)."""
    from core.services.timetable_pr4_instructor import get_instructor_daily_cap

    return get_instructor_daily_cap()


def weekly_ceiling() -> int:
    """15 sessions, or fewer when the daily cap cannot fill 15 in five days."""
    return min(WEEKLY_SESSION_CEILING, 5 * daily_cap())


def soft_weekly() -> int:
    """The day-off target: 12 sessions, or what four capped days can hold."""
    return min(SOFT_WEEKLY_SESSIONS, 4 * daily_cap())


def _clash_on() -> bool:
    from core.services.timetable_pr4_instructor import is_instructor_clash_enabled

    return is_instructor_clash_enabled()


def _cap_on() -> bool:
    from core.services.timetable_pr4_instructor import is_instructor_daily_cap_enabled

    return is_instructor_daily_cap_enabled()


def _label_order(label: str) -> tuple[int, str]:
    tail = label[1:] if label[:1].upper() == "S" else label
    return (int(tail) if tail.isdigit() else 10**6, label)


def _norm(value: object) -> str:
    return str(value or "").strip().upper()


# ── Data model ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Slot:
    """One section the assignment decides, or a fixed one it must respect.

    ``course_key`` is the planner identity as boards key it (``course_key or
    course_code``). ``meetings`` are the distinct physical meetings on the
    current layout as ``(day, start_min, end_min)``; empty when not placed.
    ``nominal_sessions`` is layout-free (the budget's meeting pattern) and is
    what the weekly ceiling counts. ``eligible`` is sorted, with teachers who
    hold a co/lab row on this section removed. ``course_sections`` is the D10
    count N_c and ``course_cap`` the resulting per-teacher limit (None: none).
    ``source`` says where a fixed or stored teacher comes from: a pin's row
    source, ``owner`` (the owner scenario's row) or ``meeting`` (the meeting
    name) for a frozen section, ``engine`` for a stored engine row.
    ``options`` (a section not placed yet) are the placer's own weekly options
    for it, each as distinct ``(day, start_min, end_min)`` meetings: its
    teacher must keep one of them free (``_Ledger.free_option``).
    """

    course_key: str
    label: str
    rank: int
    n_c: int
    term_section_id: int | None
    owner_scenario_id: int | None
    placed: bool
    campus: bool
    meetings: tuple[tuple[int, int, int], ...]
    nominal_sessions: int
    eligible: tuple[int, ...]
    fixed: int | None = None
    fixed_kind: str | None = None
    current: int | None = None
    course_sections: int = 0
    course_cap: int | None = None
    stored: int | None = None
    frozen_name: str = ""
    display_code: str = ""
    source: str = ""
    options: tuple[tuple[tuple[int, int, int], ...], ...] = ()

    @property
    def key(self) -> str:
        """The V2 ``section_id`` (``build_section_states_for_scenario``)."""
        return f"{self.course_key}_{self.label}"

    @property
    def pr4_key(self) -> str:
        """The placer's ``"course|label"`` key."""
        return f"{self.course_key}|{self.label}"

    @property
    def row_key(self) -> tuple[str, str]:
        return (_norm(self.course_key), _norm(self.label))

    @property
    def is_choice(self) -> bool:
        return self.fixed_kind is None and bool(self.eligible)


@dataclass(frozen=True)
class TeacherAssignment:
    """A complete one-teacher map over a slot universe, with its report."""

    teacher: dict[str, int | None]
    status: dict[str, str]
    tbd_reason: dict[str, str]
    kept: frozenset[str]
    universe: frozenset[int]
    report: dict[str, Any] = field(default_factory=dict)
    slots: tuple[Slot, ...] = ()

    def v2_map(self) -> dict[str, frozenset[int]]:
        """``{section_id: frozenset({teacher})}`` for every charged section."""
        return {
            key: frozenset({teacher})
            for key, teacher in self.teacher.items()
            if teacher is not None and self.status.get(key) in CHARGED
        }

    def pr4_map(self) -> dict[str, set[int]]:
        """The placer's map: a TBD section is PRESENT with an empty set, which
        charges nobody and suppresses the free-text fallback. No-links and
        unknown frozen sections are absent, so the free-text path applies."""
        out: dict[str, set[int]] = {}
        for slot in self.slots:
            status = self.status.get(slot.key)
            teacher = self.teacher.get(slot.key)
            if status in CHARGED and teacher is not None:
                out[slot.pr4_key] = {teacher}
            elif status == STATUS_TBD:
                out[slot.pr4_key] = set()
        return out

    def slot(self, key: str) -> Slot | None:
        return next((s for s in self.slots if s.key == key), None)


# ── Run pinning and the per-request memo ─────────────────────────────────

_PINNED: ContextVar[dict[int, TeacherAssignment] | None] = ContextVar(
    "one_teacher_pinned", default=None
)
_SCOPES: ContextVar[dict[int, dict[str, Any]] | None] = ContextVar(
    "one_teacher_scopes", default=None
)


@contextmanager
def pinned_assignment(scenario_id: int, assignment: TeacherAssignment) -> Iterator[None]:
    """Inside, ``load_assignment(scenario_id)`` returns ``assignment`` verbatim.

    A run pins its map so every consumer — handed the map or reading it from the
    database — charges the same teachers; nothing is re-derived mid-run (a
    repair unplacing a meeting cannot change who teaches what). A nested pin
    replaces the outer one for its scenario until it exits.
    """
    pins = dict(_PINNED.get() or {})
    pins[scenario_id] = assignment
    token = _PINNED.set(pins)
    try:
        yield
    finally:
        _PINNED.reset(token)


def current_pin(scenario_id: int) -> TeacherAssignment | None:
    return (_PINNED.get() or {}).get(scenario_id)


@contextmanager
def assignment_scope(scenario_id: int) -> Iterator[None]:
    """Memoise one request's layout-independent reads for ``scenario_id``.

    Slots' eligibility, budgets, instructors and the ``section_instructors``
    rows are read once. Each ``load_assignment`` inside still re-reads the
    placements and re-runs the in-memory closure when they changed, so a write
    earlier in the same request (a drag, a manual create) is seen.
    """
    scopes = _SCOPES.get()
    if scopes is not None and (scenario_id in scopes or _EVERY_SCENARIO in scopes):
        yield
        return
    new = dict(scopes or {})
    new[scenario_id] = {}
    token = _SCOPES.set(new)
    try:
        yield
    finally:
        _SCOPES.reset(token)


_EVERY_SCENARIO = 0  # never a primary key


@contextmanager
def request_scope() -> Iterator[None]:
    """``assignment_scope`` for every scenario one request touches — for a view
    that learns its scenario only after reading its arguments (a drag, a
    create, the board detail): one read of the stored rows per request."""
    scopes = _SCOPES.get()
    if scopes is not None and _EVERY_SCENARIO in scopes:
        yield
        return
    new = dict(scopes or {})
    new[_EVERY_SCENARIO] = {}
    token = _SCOPES.set(new)
    try:
        yield
    finally:
        _SCOPES.reset(token)


def _scope_cache(scenario_id: int) -> dict[str, Any] | None:
    scopes = _SCOPES.get()
    if not scopes:
        return None
    cache = scopes.get(scenario_id)
    if cache is None and _EVERY_SCENARIO in scopes:
        cache = scopes[scenario_id] = {}
    return cache


def invalidate_scope(scenario_id: int) -> None:
    cache = _scope_cache(scenario_id)
    if cache is not None:
        cache.clear()


# ── Inputs ───────────────────────────────────────────────────────────────


def assignment_programmes(scenario) -> tuple[list[str], str]:
    """The programmes whose links staff ``scenario``, and where they came from.

    ``scenario.programs`` when set. Several live scenarios (615/616/617/620)
    have it empty; then the boards' programmes, in board order — the set the
    solver fallback and ``apply_primary_instructor`` already use.
    """
    programs = [p for p in (getattr(scenario, "programs", None) or []) if p]
    if programs:
        return list(dict.fromkeys(programs)), "scenario"
    from core.models import DeliveryBoard
    from core.services.timetable_online import programmes_for_board

    out: list[str] = []
    for board in DeliveryBoard.objects.filter(scenario_id=scenario.pk).order_by(
        "display_order", "pk"
    ):
        for program in programmes_for_board(board):
            if program not in out:
                out.append(program)
    return out, "boards"


@dataclass
class _Static:
    """Reads that do not depend on the layout (memoised per request)."""

    scenario_id: int
    gender: str
    status: str
    programmes: list[str]
    programmes_source: str
    budgets: dict[str, Any]
    resolve: Any
    active: set[int]
    names: dict[int, str]
    by_normalised: dict[str, int]
    grids: tuple[list, list, list] = ([], [], [])
    resolved: dict[tuple[str, str], tuple[int, ...]] = field(default_factory=dict)
    placer_options: dict[str, tuple] = field(default_factory=dict)

    def eligible(self, course_key: str, display_code: str) -> tuple[int, ...]:
        cache_key = (course_key, display_code)
        if cache_key not in self.resolved:
            self.resolved[cache_key] = tuple(sorted(self.resolve(course_key, display_code)))
        return self.resolved[cache_key]

    def options(self, course_key: str, budget) -> tuple:
        """The weekly options ``auto_place_board`` tries for a missing section
        of ``course_key`` — the same generator, display code, credit hours and
        the scenario's own grids — as distinct ``(day, start, end)`` meetings."""
        if course_key not in self.placer_options:
            from core.services.timetable_autoplace import (
                DEFAULT_LAB_SLOTS,
                DEFAULT_SLOTS,
                WEEKDAYS,
                generate_course_meeting_options,
            )

            slot_config, lab_slot_config, blocked = self.grids
            found: set[tuple[tuple[int, int, int], ...]] = set()
            for option in generate_course_meeting_options(
                budget.course_code,
                budget.credit_hours or 3,
                slot_config or DEFAULT_SLOTS,
                lab_slot_config or DEFAULT_LAB_SLOTS,
                blocked or [],
                course_key=course_key,
            ):
                cells = set()
                for meeting in option:
                    day = str(meeting.get("day", "")).upper()
                    if day not in WEEKDAYS:
                        break
                    cells.add(
                        (WEEKDAYS.index(day), _minutes(meeting["start"]), _minutes(meeting["end"]))
                    )
                else:
                    if cells:
                        found.add(tuple(sorted(cells)))
            self.placer_options[course_key] = tuple(sorted(found))
        return self.placer_options[course_key]


def _minutes(value: object) -> int:
    hours, minutes = str(value)[:5].split(":")
    return int(hours) * 60 + int(minutes)


def _load_static(scenario_id: int) -> _Static | None:
    from core.models import Instructor, ScenarioSectionBudget, TimetableScenario
    from core.services.timetable_pr4_instructor import section_instructor_resolver

    scenario = TimetableScenario.objects.filter(pk=scenario_id).first()
    if scenario is None:
        return None
    programmes, source = assignment_programmes(scenario)
    gender = scenario.gender or ""
    budgets = {
        (b.course_key or b.course_code): b
        for b in ScenarioSectionBudget.objects.filter(scenario_id=scenario_id).order_by("pk")
    }
    active: set[int] = set()
    names: dict[int, str] = {}
    by_normalised: dict[str, int] = {}
    for pk, full_name, normalised, is_active in Instructor.objects.values_list(
        "id", "full_name", "normalised_name", "is_active"
    ):
        names[pk] = full_name
        if is_active:
            active.add(pk)
            by_normalised[normalised] = pk
    return _Static(
        scenario_id=scenario_id,
        gender=gender,
        status=scenario.status or "",
        programmes=programmes,
        programmes_source=source,
        budgets=budgets,
        resolve=section_instructor_resolver(programmes, gender),
        active=active,
        names=names,
        by_normalised=by_normalised,
        grids=(
            list(scenario.slot_config or []),
            list(scenario.lab_slot_config or []),
            list(scenario.blocked_slots or []),
        ),
    )


def _static(scenario_id: int) -> _Static | None:
    cache = _scope_cache(scenario_id)
    if cache is None:
        return _load_static(scenario_id)
    if "static" not in cache:
        cache["static"] = _load_static(scenario_id)
    return cache["static"]


def _section_instructor_rows(scenario_id: int, owners: frozenset, global_keys: frozenset):
    """One read of every row the assignment needs: this scenario's own, plus
    the owner scopes of its teacher-frozen sections."""
    from django.db.models import Q

    from core.models import SectionInstructor

    cache = _scope_cache(scenario_id)
    cache_key = ("rows", owners, global_keys)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    query = Q(scenario_id=scenario_id)
    if owners:
        query |= Q(scenario_id__in=sorted(owners))
    if global_keys:
        query |= Q(scenario__isnull=True, course_key__in=sorted(global_keys))
    rows = list(
        SectionInstructor.objects.filter(query)
        .order_by("pk")
        .values_list(
            "pk", "scenario_id", "course_key", "section", "instructor_id", "role", "source"
        )
    )
    if cache is not None:
        cache[cache_key] = rows
    return rows


def _nominal_for(budget, course_key: str) -> tuple[int, Any]:
    from core.services.timetable_autoplace import get_meeting_pattern
    from core.services.timetable_course_policy import get_course_meeting_policy

    policy = get_course_meeting_policy(budget.course_code, course_key)
    pattern = list(policy.durations) if policy else get_meeting_pattern(budget.credit_hours or 3)
    return len(pattern), policy


def _collect(
    scenario_id: int,
    *,
    sections=None,
    placed_filter: set[int] | None = None,
) -> tuple[list[Slot], dict[str, Any]]:
    """Slots plus the collection report (see :func:`collect_slots`)."""
    from core.models import SectionPlacement, TermSection, TermSectionMeeting
    from core.services.board_ownership import teacher_frozen_section_ids
    from core.services.course_identity import course_display_code
    from core.services.timetable_course_policy import get_course_meeting_policy
    from core.services.timetable_pr4_instructor import normalise_instructor

    info: dict[str, Any] = {
        "programmes_source": "",
        "pins_orphaned": [],
        "non_primary_conflicts": [],
        "key_collisions": [],
        "frozen_unknown": [],
        "stale_row_keys": [],
        "engine_rows": {},
    }
    static = _static(scenario_id)
    if static is None or not static.gender:
        return [], info
    info["programmes_source"] = static.programmes_source

    if sections is None:
        from core.services.timetable_optimizer_v2 import build_section_states_for_scenario

        sections = build_section_states_for_scenario(scenario_id)

    # Physical sections behind the keys: this scenario's own first, then any
    # section placed on its boards (another scenario's, or a global one).
    placed_rows = list(
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .values_list(
            "term_section_id",
            "term_section__scenario_id",
            "term_section__course_key",
            "term_section__course_code",
            "term_section__course_number",
            "term_section__section",
        )
        .distinct()
    )
    own_rows = list(
        TermSection.objects.filter(scenario_id=scenario_id).values_list(
            "id", "scenario_id", "course_key", "course_code", "course_number", "section"
        )
    )
    ts_by_key: dict[tuple[str, str], tuple] = {}
    for row in own_rows:
        ts_by_key[((row[2] or row[3]), row[5])] = row
    for row in placed_rows:
        key = ((row[2] or row[3]), row[5])
        existing = ts_by_key.get(key)
        if existing is not None and existing[0] != row[0]:
            # A foreign section sharing a local identity: the local slot wins.
            info["key_collisions"].append(f"{key[0]}_{key[1]}")
            continue
        ts_by_key[key] = row
    frozen_ids = teacher_frozen_section_ids(scenario_id)

    states_by_id = {state.section_id: state for state in sections}
    universe: dict[str, tuple[str, str]] = {}
    for course_key, budget in static.budgets.items():
        for n in range(1, int(budget.planned_sections or 0) + 1):
            universe[f"{course_key}_S{n}"] = (course_key, f"S{n}")
    ts_section_ids = {f"{ck}_{label}": (ck, label) for (ck, label) in ts_by_key}
    for section_id, state in states_by_id.items():
        if section_id in universe:
            continue
        pair = ts_section_ids.get(section_id)
        if pair is None:
            prefix = f"{state.course_code}_"
            label = section_id[len(prefix) :] if section_id.startswith(prefix) else section_id
            pair = (state.course_code, label)
        universe[section_id] = pair

    ts_ids = [row[0] for key, row in ts_by_key.items() if f"{key[0]}_{key[1]}" in universe]
    meeting_names: dict[int, list[str]] = defaultdict(list)
    for ts_id, name in (
        TermSectionMeeting.objects.filter(term_section_id__in=ts_ids)
        .order_by("pk")
        .values_list("term_section_id", "instructor")
    ):
        if name and name.strip():
            meeting_names[ts_id].append(name)

    frozen_rows = [ts_by_key[pair] for pair in universe.values() if pair in ts_by_key]
    frozen_rows = [row for row in frozen_rows if row[0] in frozen_ids]
    owners = frozenset(row[1] for row in frozen_rows if row[1] is not None)
    global_keys = frozenset(_norm(row[2] or row[3]) for row in frozen_rows if row[1] is None)
    rows = _section_instructor_rows(scenario_id, owners, global_keys)
    info["rows"] = [row for row in rows if row[1] == scenario_id]

    own_primary: dict[tuple[str, str], tuple[int, str]] = {}
    non_primary: dict[tuple[str, str], set[int]] = defaultdict(set)
    owner_primary: dict[tuple[object, str, str], int] = {}
    for _pk, row_scenario, course_key, label, instructor_id, role, source in rows:
        row_key = (_norm(course_key), _norm(label))
        if row_scenario == scenario_id:
            if role == ROLE_PRIMARY:
                own_primary[row_key] = (instructor_id, source or "")
            else:
                non_primary[row_key].add(instructor_id)
        if role == ROLE_PRIMARY:
            owner_primary[(row_scenario, *row_key)] = instructor_id

    def name_teacher(ts_id: int | None) -> int | None:
        for name in meeting_names.get(ts_id, ()) if ts_id is not None else ():
            normalised = normalise_instructor(name)
            if normalised and normalised in static.by_normalised:
                return static.by_normalised[normalised]
        return None

    by_course: dict[str, list[str]] = defaultdict(list)
    for key, (course_key, _label) in universe.items():
        by_course[course_key].append(key)
    for course_key in by_course:
        by_course[course_key].sort(key=lambda k: _label_order(universe[k][1]))

    slots: list[Slot] = []
    seen_row_keys: set[tuple[str, str]] = set()
    reserve = _clash_on() or _cap_on()
    for course_key, keys in sorted(by_course.items()):
        budget = static.budgets.get(course_key)
        policy = None
        nominal_budget = None
        if budget is not None:
            nominal_budget, policy = _nominal_for(budget, course_key)
        n_course = int(budget.planned_sections or 0) if budget is not None else len(keys)
        for rank, key in enumerate(keys, start=1):
            _ck, label = universe[key]
            ts = ts_by_key.get((course_key, label))
            ts_id = ts[0] if ts else None
            if ts is not None:
                display = course_display_code(ts[2], ts[3], ts[4])
            elif budget is not None:
                display = budget.course_code
            else:
                display = course_display_code(course_key, course_key, "")
            if budget is None and policy is None and ts is not None:
                policy = get_course_meeting_policy(ts[3], ts[2], ts[4])
            state = states_by_id.get(key)
            placed = state is not None and (placed_filter is None or ts_id in placed_filter)
            meetings: tuple[tuple[int, int, int], ...] = ()
            campus = True
            if placed:
                meetings = tuple(sorted({(m.day, m.start_min, m.end_min) for m in state.meetings}))
                campus = not (state.delivery_is_online or state.is_online)
            nominal = nominal_budget if nominal_budget is not None else max(1, len(meetings))
            # A missing section keeps its placer options, so its teacher keeps
            # one free (only while the board enforces clash or the daily cap).
            options = ()
            if not placed and reserve and budget is not None:
                options = static.options(course_key, budget)
            cap_c: int | None = D10_MAX_SECTIONS if n_course > D10_APPLIES_ABOVE else None
            if policy is not None:
                cap_c = (
                    min(cap_c, len(policy.patterns)) if cap_c is not None else len(policy.patterns)
                )
            row_key = (_norm(course_key), _norm(label))
            seen_row_keys.add(row_key)
            eligible = static.eligible(course_key, display)
            blocked = non_primary.get(row_key, set())
            for teacher in eligible:
                if teacher in blocked:
                    info["non_primary_conflicts"].append({"section": key, "instructor_id": teacher})
            eligible = tuple(t for t in eligible if t not in blocked)
            fixed = None
            fixed_kind = None
            stored = None
            frozen_name = ""
            origin = ""
            if ts_id is not None and ts_id in frozen_ids:
                owner = ts[1]
                fixed = owner_primary.get((owner, *row_key))
                origin = "owner"
                if fixed is None:
                    fixed, origin = name_teacher(ts_id), "meeting"
                if fixed is None:
                    fixed_kind = STATUS_FROZEN_UNKNOWN
                    frozen_name = next(iter(meeting_names.get(ts_id, ())), "")
                    origin = "meeting" if frozen_name else ""
                    info["frozen_unknown"].append(
                        {"course_key": course_key, "label": label, "name": frozen_name}
                    )
                else:
                    fixed_kind = STATUS_FROZEN
            elif row_key in own_primary:
                teacher, source = own_primary[row_key]
                origin = source or "pin"
                if source != SOURCE_ENGINE:
                    fixed, fixed_kind = teacher, "pin"
                else:
                    stored = teacher
                    info["engine_rows"][key] = teacher
            current = None
            if fixed_kind is None:
                if stored is not None and stored in eligible and stored in static.active:
                    current = stored
                else:
                    by_name = name_teacher(ts_id)
                    current = by_name if by_name in eligible else None
            slots.append(
                Slot(
                    course_key=course_key,
                    label=label,
                    rank=rank,
                    n_c=len(keys),
                    term_section_id=ts_id,
                    owner_scenario_id=ts[1] if ts else scenario_id,
                    placed=placed,
                    campus=campus,
                    meetings=meetings,
                    nominal_sessions=nominal,
                    eligible=eligible,
                    fixed=fixed,
                    fixed_kind=fixed_kind,
                    current=current,
                    course_sections=n_course,
                    course_cap=cap_c,
                    stored=stored,
                    frozen_name=frozen_name,
                    display_code=display,
                    source=origin,
                    options=options,
                )
            )
    for row_key, (_teacher, source) in sorted(own_primary.items()):
        if row_key in seen_row_keys:
            continue
        if source == SOURCE_ENGINE:
            info["stale_row_keys"].append(list(row_key))
        else:
            info["pins_orphaned"].append({"course_key": row_key[0], "label": row_key[1]})
    return slots, info


def collect_slots(scenario_id: int, *, sections=None, placed_filter=None) -> list[Slot]:
    """The slot universe of ``scenario_id`` on a layout.

    Every placed section (``build_section_states_for_scenario``, or the
    in-memory ``sections`` given) and every budget label ``S1..S{planned}``,
    placed or not. ``placed_filter`` (term-section ids) limits what counts as
    placed — a rebuild sees only the locked sections that survive its reset.
    """
    return _collect(scenario_id, sections=sections, placed_filter=placed_filter)[0]


# ── The ledger: one teacher's committed load, shared by every engine ─────


def _overlaps(a: Iterable[tuple[int, int, int]], b: Iterable[tuple[int, int, int]]) -> bool:
    b = list(b)
    return any(d1 == d2 and s1 < e2 and s2 < e1 for d1, s1, e1 in a for d2, s2, e2 in b)


def _merged_idle(intervals: Iterable[tuple[int, int]]) -> int:
    idle, end = 0, None
    for start, finish in sorted(intervals):
        if end is not None:
            idle += max(0, start - end)
        end = finish if end is None else max(end, finish)
    return idle


class _Ledger:
    """Committed load per teacher: sessions, sections per course, meetings, and
    the sections not placed yet whose options each teacher must keep free."""

    def __init__(self) -> None:
        self.load: Counter = Counter()
        self.per_course: Counter = Counter()
        self.cells: dict[int, list[tuple[int, int, int, str]]] = defaultdict(list)
        self.campus: dict[int, dict[int, list[tuple[int, int]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self.pending: dict[int, list[Slot]] = defaultdict(list)
        self.clash_on = _clash_on()
        self.cap_on = _cap_on()
        self.cap = daily_cap()
        self.ceiling = weekly_ceiling()
        self.soft = soft_weekly()

    def add(self, slot: Slot, teacher: int) -> None:
        """Charge ``slot`` to ``teacher``. A section not placed yet that has
        options is held as pending, so no later pick takes its last option."""
        self.load[teacher] += slot.nominal_sessions
        self.per_course[(teacher, slot.course_key)] += 1
        if slot.placed:
            for day, start, end in slot.meetings:
                self.cells[teacher].append((day, start, end, slot.key))
                if slot.campus:
                    self.campus[teacher][day].append((start, end))
        elif slot.options:
            self.pending[teacher].append(slot)

    def free_option(
        self, slot: Slot, teacher: int, extra: Iterable[tuple[int, int, int]] = ()
    ) -> str | None:
        """None when ``teacher`` still has an option of ``slot`` (not placed yet)
        free of clash and of the daily cap — with ``extra`` meetings added —
        as the placer's two teacher filters test it; else the rule that took the
        last one: ``"clash"`` when no option is clash-free, ``"daily_cap"``."""
        if not (self.clash_on or self.cap_on):
            return None
        mine = [(d, s, e) for d, s, e, _key in self.cells.get(teacher, ())] + list(extra)
        per_day = Counter(d for d, _s, _e in mine)
        clash_free = False
        for option in slot.options:
            if self.clash_on and _overlaps(option, mine):
                continue
            clash_free = True
            if self.cap_on and any(
                per_day[day] + count > self.cap
                for day, count in Counter(d for d, _s, _e in option).items()
            ):
                continue
            return None
        return "daily_cap" if clash_free else "clash"

    def boxes(self, slot: Slot, teacher: int) -> str | None:
        """Taking placed ``slot`` would leave one of ``teacher``'s pending
        sections without a free option. One already without (a kept section,
        or a stored row after a layout edit) stays as it is: nothing is moved
        to win it back."""
        for other in self.pending.get(teacher, ()):
            if self.free_option(other, teacher) is None:
                reason = self.free_option(other, teacher, extra=slot.meetings)
                if reason is not None:
                    return reason
        return None

    def days(self, teacher: int) -> int:
        return sum(1 for day in self.campus.get(teacher, {}).values() if day)

    def idle(self, teacher: int) -> int:
        return sum(_merged_idle(day) for day in self.campus.get(teacher, {}).values())

    def increments(self, slot: Slot, teacher: int) -> tuple[int, int]:
        """Campus days and idle minutes ``slot`` would add to ``teacher``."""
        if not (slot.placed and slot.campus and slot.meetings):
            return 0, 0
        days_now = self.campus.get(teacher, {})
        new_days = 0
        idle_added = 0
        for day in sorted({d for d, _s, _e in slot.meetings}):
            before = list(days_now.get(day, ()))
            if not before:
                new_days += 1
            after = before + [(s, e) for d, s, e in slot.meetings if d == day]
            idle_added += _merged_idle(after) - _merged_idle(before)
        return new_days, idle_added

    def layout_free_blocker(self, slot: Slot, teacher: int) -> str | None:
        """D10 or the weekly ceiling: what stops ``teacher`` at any time."""
        if (
            slot.course_cap is not None
            and self.per_course[(teacher, slot.course_key)] >= slot.course_cap
        ):
            return "d10"
        if self.load[teacher] + slot.nominal_sessions > self.ceiling:
            return "weekly_ceiling"
        return None

    def blocker(self, slot: Slot, teacher: int, bounds=None) -> str | None:
        """The first rule that stops ``teacher`` taking ``slot`` (2.5 order)."""
        reason = self.layout_free_blocker(slot, teacher)
        if reason is not None:
            return reason
        if slot.placed and slot.meetings:
            mine = [(d, s, e) for d, s, e, key in self.cells.get(teacher, ()) if key != slot.key]
            if self.clash_on and _overlaps(slot.meetings, mine):
                return "clash"
            if self.cap_on:
                per_day = Counter(d for d, _s, _e in mine)
                for day, count in Counter(d for d, _s, _e in slot.meetings).items():
                    if per_day[day] + count > self.cap:
                        return "daily_cap"
            reason = self.boxes(slot, teacher)
            if reason is not None:
                return reason
        elif not slot.placed and slot.options:
            reason = self.free_option(slot, teacher)
            if reason is not None:
                return reason
        if bounds is not None and not self.within_bounds(slot, teacher, bounds):
            return "bound"
        return None

    def within_bounds(self, slot: Slot, teacher: int, bounds) -> bool:
        limit_days, limit_idle, allowance = bounds.get(str(teacher), (0, 0, 0))
        new_days, idle_added = self.increments(slot, teacher)
        days_after = self.days(teacher) + new_days
        idle_after = self.idle(teacher) + idle_added
        return days_after <= limit_days and idle_after <= limit_idle + allowance * (
            limit_days - days_after
        )


def _fixed_ledger(slots: Iterable[Slot], kept: Mapping[str, int]) -> _Ledger:
    ledger = _Ledger()
    for slot in slots:
        if slot.fixed is not None:
            ledger.add(slot, slot.fixed)
        elif slot.key in kept:
            ledger.add(slot, kept[slot.key])
    return ledger


def _current_map(slots: Iterable[Slot], current) -> dict[str, int]:
    """Normalise ``current`` (an assignment, a key->teacher map or None)."""
    if current is None:
        return {s.key: s.current for s in slots if s.is_choice and s.current is not None}
    if isinstance(current, TeacherAssignment):
        raw = {k: t for k, t in current.teacher.items() if current.status.get(k) == STATUS_ENGINE}
    else:
        raw = dict(current)
    return {
        s.key: raw[s.key]
        for s in slots
        if s.is_choice and raw.get(s.key) is not None and raw[s.key] in s.eligible
    }


# ── Greedy: the fallback, the closure, the hint and the pre-build seed ───


def _greedy_fill(
    ledger: _Ledger,
    todo: Iterable[Slot],
    current: Mapping[str, int],
    bounds=None,
) -> dict[str, int | None]:
    picks: dict[str, int | None] = {}
    order = sorted(
        todo, key=lambda s: (len(s.eligible), not s.placed, s.course_key, s.rank, s.label)
    )
    for slot in order:
        best = None
        for teacher in slot.eligible:
            if ledger.blocker(slot, teacher, bounds) is not None:
                continue
            new_days, idle_added = ledger.increments(slot, teacher)
            rank = (
                ledger.load[teacher] + slot.nominal_sessions > ledger.soft,
                new_days,
                idle_added,
                teacher != current.get(slot.key),
                ledger.load[teacher],
                teacher,
            )
            if best is None or rank < best[0]:
                best = (rank, teacher)
        picks[slot.key] = None if best is None else best[1]
        if best is not None:
            ledger.add(slot, best[1])
    return picks


def greedy_assign(
    slots, *, current=None, bounds=None, seed: Mapping[str, int] | None = None
) -> TeacherAssignment:
    """Most constrained section first; each to the eligible teacher that keeps
    the day-off target, adds the fewest campus days, then the least idle, then
    keeps the current teacher, then the lightest load, then the lowest id.

    Every hard rule is a filter, so the result is always hard-feasible. With
    ``seed`` (key -> teacher) those sections are fixed load and only the rest
    are filled: that is the closure.
    """
    slots = list(slots)
    cur = _current_map(slots, current)
    seed = {
        k: t
        for k, t in (seed or {}).items()
        if t is not None and any(s.key == k and s.is_choice and t in s.eligible for s in slots)
    }
    ledger = _fixed_ledger(slots, seed)
    todo = [s for s in slots if s.is_choice and s.key not in seed]
    picks = dict(seed)
    picks.update(_greedy_fill(ledger, todo, cur, bounds))
    report = {"engine": "greedy", "bounded": bounds is not None}
    return _assemble(slots, picks, cur, bounds=bounds, kept=frozenset(), report=report)


def _closure(slots: list[Slot], seed: Mapping[str, int]) -> tuple[TeacherAssignment, list[str]]:
    """Fill every choice section without a seed that some teacher can still take
    on this layout. Returns the map and the keys the closure filled."""
    filled = greedy_assign(slots, seed=seed)
    added = [
        key
        for key, teacher in filled.teacher.items()
        if teacher is not None and filled.status.get(key) == STATUS_ENGINE and key not in seed
    ]
    return filled, added


# ── Exact: the staged lexicographic CP-SAT ───────────────────────────────


def _components(choice: list[Slot]) -> list[list[Slot]]:
    """Connected components of sections <-> eligible teachers, in ascending
    order of each component's smallest teacher id."""
    parent: dict[int, int] = {}

    def find(i: int) -> int:
        while parent.setdefault(i, i) != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for slot in choice:
        first = slot.eligible[0]
        for other in slot.eligible[1:]:
            a, b = find(first), find(other)
            if a != b:
                parent[max(a, b)] = min(a, b)
    groups: dict[int, list[Slot]] = defaultdict(list)
    for slot in choice:
        groups[find(slot.eligible[0])].append(slot)
    return [sorted(groups[root], key=lambda s: s.key) for root in sorted(groups)]


def _solve_component(
    comp: list[Slot],
    ledger: _Ledger,
    current: Mapping[str, int],
    bounds,
    big: int,
    hint: Mapping[str, int | None],
    deadline: float,
    stages_out: list,
    exempt: frozenset[str] = frozenset(),
) -> tuple[dict[str, int | None], dict[str, Any]]:
    from ortools.sat.python import cp_model

    from core.services.cpsat_resources import BudgetedCpModel
    from core.services.timetable_run_seed import seed_for

    model = BudgetedCpModel()
    teachers = sorted({t for s in comp for t in s.eligible})
    fixed_cells = {t: [(d, s, e) for d, s, e, _k in ledger.cells.get(t, ())] for t in teachers}
    fixed_days = {t: Counter(d for d, _s, _e in fixed_cells[t]) for t in teachers}

    def fixed_free(option, teacher: int) -> bool:
        """``option`` (of a section not placed yet) clears ``teacher``'s fixed load."""
        if ledger.clash_on and _overlaps(option, fixed_cells[teacher]):
            return False
        return not (
            ledger.cap_on
            and any(
                fixed_days[teacher][day] + count > ledger.cap
                for day, count in Counter(d for d, _s, _e in option).items()
            )
        )

    cand: dict[str, list[int]] = {}
    live: dict[tuple[str, int], list] = {}
    for slot in comp:
        allowed = []
        for teacher in slot.eligible:
            if slot.course_cap is not None and (
                ledger.per_course[(teacher, slot.course_key)] >= slot.course_cap
            ):
                continue
            if ledger.load[teacher] + slot.nominal_sessions > ledger.ceiling:
                continue
            if slot.placed and slot.meetings:
                if ledger.clash_on and _overlaps(slot.meetings, fixed_cells[teacher]):
                    continue
                if ledger.cap_on and any(
                    fixed_days[teacher][day] + count > ledger.cap
                    for day, count in Counter(d for d, _s, _e in slot.meetings).items()
                ):
                    continue
            elif not slot.placed and slot.options and (ledger.clash_on or ledger.cap_on):
                live[(slot.key, teacher)] = [o for o in slot.options if fixed_free(o, teacher)]
                if not live[(slot.key, teacher)]:
                    continue
            allowed.append(teacher)
        cand[slot.key] = allowed

    x: dict[tuple[str, int], Any] = {}
    t: dict[str, Any] = {}
    for slot in comp:
        for teacher in cand[slot.key]:
            x[(slot.key, teacher)] = model.new_bool_var("")
        t[slot.key] = model.new_bool_var("")
        model.add_exactly_one([*(x[(slot.key, i)] for i in cand[slot.key]), t[slot.key]])

    mine = {teacher: [s for s in comp if (s.key, teacher) in x] for teacher in teachers}
    for teacher in teachers:
        own = mine[teacher]
        room = ledger.ceiling - ledger.load[teacher]
        if sum(s.nominal_sessions for s in own) > room:
            model.add(sum(s.nominal_sessions * x[(s.key, teacher)] for s in own) <= room)
        courses: dict[str, list[Slot]] = defaultdict(list)
        for slot in own:
            if slot.course_cap is not None:
                courses[slot.course_key].append(slot)
        for course_key, members in courses.items():
            limit = members[0].course_cap - ledger.per_course[(teacher, course_key)]
            if len(members) > limit:
                model.add(sum(x[(s.key, teacher)] for s in members) <= limit)
        placed = [s for s in own if s.placed and s.meetings]
        if ledger.clash_on:
            for a in range(len(placed)):
                for b in range(a + 1, len(placed)):
                    if _overlaps(placed[a].meetings, placed[b].meetings):
                        model.add(x[(placed[a].key, teacher)] + x[(placed[b].key, teacher)] <= 1)
        if ledger.cap_on:
            for day in sorted({d for s in placed for d, _s, _e in s.meetings}):
                terms = [
                    (sum(1 for d, _s, _e in s.meetings if d == day), s)
                    for s in placed
                    if any(d == day for d, _s, _e in s.meetings)
                ]
                limit = ledger.cap - fixed_days[teacher][day]
                if sum(c for c, _s in terms) > limit:
                    model.add(sum(c * x[(s.key, teacher)] for c, s in terms) <= limit)

    def keep_one_free(teacher: int, options, enforce) -> None:
        """At least one of ``options`` (already clear of ``teacher``'s fixed load)
        stays clear of the placed sections ``teacher`` is given, under clash and
        the daily cap — whenever ``enforce`` holds (None: always). Options that
        ask the same of the choices share one literal."""
        placed = [s for s in mine[teacher] if s.placed and s.meetings]
        needs: set[tuple[tuple[str, ...], tuple[tuple[int, int], ...]]] = set()
        for option in options:
            hits = tuple(
                sorted(s.key for s in placed if ledger.clash_on and _overlaps(option, s.meetings))
            )
            caps = []
            if ledger.cap_on:
                for day, count in sorted(Counter(d for d, _s, _e in option).items()):
                    limit = ledger.cap - fixed_days[teacher][day] - count
                    on_day = sum(sum(1 for d, _s, _e in s.meetings if d == day) for s in placed)
                    if on_day > limit:
                        caps.append((day, limit))
            if not hits and not caps:
                return  # this option stays free whoever teaches what
            needs.add((hits, tuple(caps)))
        free_literals = []
        for hits, caps in sorted(needs):
            free = model.new_bool_var("")
            for key in hits:
                model.add(x[(key, teacher)] == 0).only_enforce_if(free)
            for day, limit in caps:
                model.add(
                    sum(
                        sum(1 for d, _s, _e in s.meetings if d == day) * x[(s.key, teacher)]
                        for s in placed
                        if any(d == day for d, _s, _e in s.meetings)
                    )
                    <= limit
                ).only_enforce_if(free)
            free_literals.append(free)
        constraint = model.add_bool_or(free_literals)
        if enforce is not None:
            constraint.only_enforce_if(enforce)

    # A section not placed yet keeps an option free for its teacher, so the
    # required-section retry can place it: a choice section whenever it is
    # given to that teacher, a pinned one always (unless the kept rule exempts
    # it: the map in force had already left it none).
    for (key, teacher), options in sorted(live.items()):
        if teacher in cand[key]:
            keep_one_free(teacher, options, x[(key, teacher)])
    for teacher in teachers:
        for pending in ledger.pending.get(teacher, ()):
            if pending.key in exempt:
                continue
            options = [o for o in pending.options if fixed_free(o, teacher)]
            if options:
                keep_one_free(teacher, options, None)

    weights = {s.key: big * int(s.placed) + (s.n_c + 1 - s.rank) for s in comp}
    l1 = [t[s.key] for s in comp]
    l2 = [weights[s.key] * t[s.key] for s in comp]
    l3, l4, l5 = [], [], []
    day_terms: dict[int, list] = defaultdict(list)
    idle_terms: dict[int, list] = defaultdict(list)
    const_days: Counter = Counter()
    const_idle: Counter = Counter()
    for teacher in teachers:
        own = mine[teacher]
        load_terms = [s.nominal_sessions * x[(s.key, teacher)] for s in own]
        most = ledger.load[teacher] + sum(s.nominal_sessions for s in own)
        if own and most > ledger.soft:
            over = model.new_int_var(0, most, "")
            model.add(over >= ledger.load[teacher] + sum(load_terms) - ledger.soft)
            l3.append(over)
        fixed_campus = ledger.campus.get(teacher, {})
        choice_days = sorted({d for s in own if s.placed and s.campus for d, _s, _e in s.meetings})
        for day, intervals in fixed_campus.items():
            if intervals and day not in choice_days:
                const_days[teacher] += 1
                const_idle[teacher] += _merged_idle(intervals)
        for day in choice_days:
            meetings = [
                (s, st, en)
                for s in own
                if s.placed and s.campus
                for d, st, en in s.meetings
                if d == day
            ]
            fixed_here = list(fixed_campus.get(day, ()))
            day_slots = sorted({s.key for s, _st, _en in meetings})
            if fixed_here:
                const_days[teacher] += 1
            else:
                present = model.new_bool_var("")
                for key in day_slots:
                    model.add_implication(x[(key, teacher)], present)
                model.add_bool_or([x[(key, teacher)] for key in day_slots]).only_enforce_if(present)
                l4.append(present)
                day_terms[teacher].append(present)
            first = model.new_int_var(0, 1440, "")
            last = model.new_int_var(0, 1440, "")
            for s, st, en in meetings:
                model.add(first <= st).only_enforce_if(x[(s.key, teacher)])
                model.add(last >= en).only_enforce_if(x[(s.key, teacher)])
            for st, en in fixed_here:
                model.add(first <= st)
                model.add(last >= en)
            model.add(last >= first)
            idle = model.new_int_var(0, 1440, "")
            busy = sum((en - st) * x[(s.key, teacher)] for s, st, en in meetings)
            model.add(idle >= last - first - busy - sum(en - st for st, en in fixed_here))
            l5.append(idle)
            idle_terms[teacher].append(idle)
        if bounds is not None:
            limit_days, limit_idle, allowance = bounds.get(str(teacher), (0, 0, 0))
            days_expr = sum(day_terms[teacher]) + const_days[teacher]
            idle_expr = sum(idle_terms[teacher]) + const_idle[teacher]
            _add_bound(model, days_expr <= limit_days)
            _add_bound(
                model, idle_expr + allowance * days_expr <= limit_idle + allowance * limit_days
            )
    l6 = [
        1 - x[(s.key, current[s.key])]
        for s in comp
        if current.get(s.key) is not None and (s.key, current[s.key]) in x
    ]
    l7 = [
        index * x[(s.key, teacher)]
        for s in comp
        for index, teacher in enumerate(s.eligible)
        if (s.key, teacher) in x and index
    ]

    for slot in comp:
        chosen = hint.get(slot.key)
        for teacher in cand[slot.key]:
            model.add_hint(x[(slot.key, teacher)], chosen == teacher)
        model.add_hint(t[slot.key], chosen not in cand[slot.key])

    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.random_seed = seed_for("classic:one_teacher")
    solver.parameters.max_deterministic_time = float(
        getattr(settings, "TIMETABLE_ONE_TEACHER_STAGE_DETERMINISTIC_TIME", 1.0)
    )
    variables = list(x.values()) + list(t.values())
    solution: dict[str, int | None] | None = None
    meta = {"proven": True, "wall_capped": False, "elements": model.elements}
    for name, terms in zip(_STAGES, (l1, l2, l3, l4, l5, l6, l7), strict=True):
        if not terms:
            stages_out.append([name, "EMPTY", 0, 0.0])
            continue
        objective = sum(terms)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            meta["wall_capped"] = True
            break
        solver.parameters.max_time_in_seconds = max(0.01, min(remaining, 3.0))
        model.minimize(objective)
        status = solver.solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            stages_out.append([name, solver.status_name(status), None, 0.0])
            if solver.wall_time >= solver.parameters.max_time_in_seconds * 0.95:
                meta["wall_capped"] = True
            break
        value = int(round(solver.objective_value))
        stages_out.append(
            [name, solver.status_name(status), value, round(float(solver.deterministic_time), 4)]
        )
        solution = {
            s.key: next((i for i in cand[s.key] if solver.value(x[(s.key, i)])), None) for s in comp
        }
        model.clear_hints()
        for var in variables:
            model.add_hint(var, solver.value(var))
        if status == cp_model.OPTIMAL:
            model.add(objective == value)
        else:
            meta["proven"] = False
            if solver.wall_time >= solver.parameters.max_time_in_seconds * 0.95:
                meta["wall_capped"] = True
            model.add(objective <= value)
    meta["elements"] = model.elements
    if solution is None:
        raise _NoSolution
    return solution, meta


class _NoSolution(Exception):
    """CP-SAT returned no solution at its first stage."""


def _add_bound(model, constraint) -> None:
    """Add a bound that may have folded to a constant (a teacher with only fixed
    days): a true constant is dropped, a false one makes the solve infeasible."""
    if isinstance(constraint, bool):
        if not constraint:
            raise _NoSolution
        return
    model.add(constraint)


def solve_layout_aware(
    slots, *, current=None, bounds=None, kept: Iterable[str] = frozenset()
) -> TeacherAssignment:
    """The exact assignment on the slots' layout (staged lexicographic CP-SAT).

    Stages: L1 TBD count, L2 TBD position (unplaced before placed, then the
    highest rank of its course), L3 sessions over the day-off target, L4
    campus working days, L5 campus idle, L6 changes from ``current``, L7 the
    lowest instructor id. Clash, the daily cap, the weekly ceiling and D10 are
    constraints, and so is a free option for each section not placed yet (see
    ``Slot.options``); ``kept`` sections (charged under ``current`` and already
    in a clash, an over-cap day, or left no option) are fixed to their current
    teacher, and a kept pinned section is exempt from the free-option rule.
    ``bounds`` (``teacher_bounds``) caps each teacher's days and idle. All-TBD
    satisfies every constraint, so a solve is always feasible; anything that
    fails falls back to the greedy, which is hard-feasible too.
    """
    started = time.monotonic()
    slots = list(slots)
    cur = _current_map(slots, current)
    exempt = frozenset(kept)
    kept_map = {k: cur[k] for k in exempt if k in cur}
    ledger = _fixed_ledger(slots, kept_map)
    choice = [s for s in slots if s.is_choice and s.key not in kept_map]
    # The hint, and a failed component's answer: the greedy (2.9), where the
    # current teacher only breaks ties. Seeding the current teachers first
    # replays a stale map instead: on lab scenario 667 the union era's meeting
    # names filled instructor 298's week to the ceiling, and a section only 298
    # can teach was left without a teacher.
    hint = _greedy_fill(_fixed_ledger(slots, kept_map), choice, cur, bounds)
    report: dict[str, Any] = {
        "engine": "cpsat",
        "components": 0,
        "stages": [],
        "wall_s": 0.0,
        "elements_max": 0,
        "fallback_reason": None,
        "kept": len(kept_map),
        "bounded": bounds is not None,
        "proven": True,
        "wall_capped": False,
    }
    picks: dict[str, int | None] = dict(kept_map)
    big = max((s.n_c for s in slots), default=0) + 1
    budget = float(getattr(settings, "TIMETABLE_ONE_TEACHER_TIME_LIMIT_SECONDS", 10.0))
    deadline = started + max(0.05, budget)
    components = _components(choice)
    report["components"] = len(components)
    fallbacks = 0
    for index, comp in enumerate(components):
        stages: list = []
        try:
            solution, meta = _solve_component(
                comp, ledger, cur, bounds, big, hint, deadline, stages, exempt=exempt
            )
            report["elements_max"] = max(report["elements_max"], meta["elements"])
            report["proven"] = report["proven"] and meta["proven"]
            report["wall_capped"] = report["wall_capped"] or meta["wall_capped"]
        except Exception as exc:  # noqa: BLE001 - every failure has the same safe answer
            from core.services.cpsat_resources import ModelTooLarge

            fallbacks += 1
            if isinstance(exc, ModelTooLarge):
                reason = "model_too_large"
            elif isinstance(exc, _NoSolution):
                reason = "no_solution"
            elif isinstance(exc, ImportError):
                reason = "solver_unavailable"
            else:
                reason = "solver_error"
                logger.exception("One-teacher solve failed; using the greedy assignment")
            report["fallback_reason"] = report["fallback_reason"] or reason
            solution = {s.key: hint.get(s.key) for s in comp}
        report["stages"].append([index, stages])
        picks.update(solution)
    if components and fallbacks == len(components):
        report["engine"] = "greedy"
    elif fallbacks:
        report["engine"] = "mixed"
    report["wall_s"] = round(time.monotonic() - started, 4)
    return _assemble(slots, picks, cur, bounds=bounds, kept=frozenset(kept_map), report=report)


# ── Assembly, reasons and measurement ────────────────────────────────────


def _assemble(
    slots: list[Slot],
    picks: Mapping[str, int | None],
    current: Mapping[str, int],
    *,
    bounds,
    kept: frozenset[str],
    report: dict[str, Any],
) -> TeacherAssignment:
    teacher: dict[str, int | None] = {}
    status: dict[str, str] = {}
    for slot in slots:
        if slot.fixed_kind == "pin":
            teacher[slot.key], status[slot.key] = slot.fixed, STATUS_PINNED
        elif slot.fixed_kind == STATUS_FROZEN:
            teacher[slot.key], status[slot.key] = slot.fixed, STATUS_FROZEN
        elif slot.fixed_kind == STATUS_FROZEN_UNKNOWN:
            teacher[slot.key], status[slot.key] = None, STATUS_FROZEN_UNKNOWN
        elif not slot.eligible:
            teacher[slot.key], status[slot.key] = None, STATUS_NO_LINKS
        else:
            chosen = picks.get(slot.key)
            chosen = chosen if chosen in slot.eligible else None
            teacher[slot.key] = chosen
            status[slot.key] = STATUS_ENGINE if chosen is not None else STATUS_TBD
    ledger = _Ledger()
    for slot in slots:
        if teacher.get(slot.key) is not None:
            ledger.add(slot, teacher[slot.key])
    reasons: dict[str, str] = {}
    blockers: dict[str, dict[str, str | None]] = {}
    for slot in slots:
        if status[slot.key] != STATUS_TBD:
            continue
        per = {str(i): ledger.blocker(slot, i, bounds) for i in slot.eligible}
        found = [b for b in per.values()]
        if found and all(b == "d10" for b in found):
            reason = "d10"
        elif found and all(b in ("d10", "weekly_ceiling") for b in found):
            reason = "weekly_ceiling"
        elif "clash" in found:
            reason = "clash"
        elif "daily_cap" in found:
            reason = "daily_cap"
        else:
            reason = "bound"
        reasons[slot.key] = reason
        if len(blockers) < 50:
            blockers[slot.key] = per
    universe = frozenset(
        {i for s in slots for i in s.eligible} | {s.fixed for s in slots if s.fixed is not None}
    )
    report = dict(report)
    report["tbd_blockers"] = blockers
    return TeacherAssignment(
        teacher=teacher,
        status=status,
        tbd_reason=reasons,
        kept=kept,
        universe=universe,
        report=report,
        slots=tuple(slots),
    )


def _charged_windows(a: TeacherAssignment) -> dict[int, list[tuple[int, int, int, str]]]:
    windows: dict[int, list[tuple[int, int, int, str]]] = defaultdict(list)
    for slot in a.slots:
        teacher = a.teacher.get(slot.key)
        if teacher is None or a.status.get(slot.key) not in CHARGED or not slot.placed:
            continue
        for day, start, end in slot.meetings:
            windows[teacher].append((day, start, end, slot.key))
    return windows


def violations(a: TeacherAssignment) -> dict[str, Any]:
    """Clash pairs and over-cap sessions of ``a`` on its own layout, counting
    each physical meeting once (a shared section's board copies are one)."""
    clash_pairs: set[tuple[str, str, int]] = set()
    over_cap = 0
    cap = daily_cap()
    for teacher, windows in _charged_windows(a).items():
        ordered = sorted(windows)
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                d1, s1, e1, k1 = ordered[i]
                d2, s2, e2, k2 = ordered[j]
                if d1 != d2:
                    break
                if k1 != k2 and s1 < e2 and s2 < e1:
                    clash_pairs.add((min(k1, k2), max(k1, k2), teacher))
        for count in Counter(d for d, _s, _e, _k in windows).values():
            over_cap += max(0, count - cap)
    return {"clash_pairs": len(clash_pairs), "over_cap_sessions": over_cap}


def _violating_keys(slots: list[Slot], current: Mapping[str, int]) -> set[str]:
    """Choice sections charged under ``current`` that sit in a clash pair or an
    over-cap day of their teacher on this layout (the kept rule), plus the
    sections not placed yet — choice or pinned — whose teacher under
    ``current`` has none of their options left (``_unplaceable_keys``)."""
    keep = _clash_or_cap_keys(slots, current)
    keep |= _unplaceable_keys(slots, current)
    return keep


def _unplaceable_keys(slots: Iterable[Slot], current: Mapping[str, int]) -> set[str]:
    """Sections not placed yet, charged (fixed, or under ``current``) to a
    teacher whose placed sections leave no option of theirs free."""
    slots = list(slots)
    ledger = _Ledger()
    for slot in slots:
        teacher = slot.fixed if slot.fixed is not None else current.get(slot.key)
        if teacher is not None:
            ledger.add(slot, teacher)
    out: set[str] = set()
    for slot in slots:
        teacher = slot.fixed if slot.fixed is not None else current.get(slot.key)
        if teacher is not None and not slot.placed and slot.options:
            if ledger.free_option(slot, teacher) is not None:
                out.add(slot.key)
    return out


def _clash_or_cap_keys(slots: list[Slot], current: Mapping[str, int]) -> set[str]:
    """Choice sections charged under ``current`` in a clash pair or an over-cap
    day of their teacher on this layout."""
    windows: dict[int, list[tuple[int, int, int, str, bool]]] = defaultdict(list)
    for slot in slots:
        teacher = slot.fixed if slot.fixed is not None else current.get(slot.key)
        if teacher is None or not slot.placed:
            continue
        choice = slot.fixed is None and slot.key in current
        for day, start, end in slot.meetings:
            windows[teacher].append((day, start, end, slot.key, choice))
    keep: set[str] = set()
    clash_on, cap_on, cap = _clash_on(), _cap_on(), daily_cap()
    for rows in windows.values():
        if clash_on:
            for i in range(len(rows)):
                for j in range(i + 1, len(rows)):
                    d1, s1, e1, k1, c1 = rows[i]
                    d2, s2, e2, k2, c2 = rows[j]
                    if d1 == d2 and k1 != k2 and s1 < e2 and s2 < e1:
                        if c1:
                            keep.add(k1)
                        if c2:
                            keep.add(k2)
        if cap_on:
            per_day = Counter(d for d, *_rest in rows)
            for d, _s, _e, key, choice in rows:
                if choice and per_day[d] > cap:
                    keep.add(key)
    return keep


def schedule_of(a: TeacherAssignment) -> dict[str, Any]:
    """Campus working days and merged idle per teacher of ``a`` on its layout —
    ``instructor_schedule_metrics`` computed on distinct physical meetings."""
    ledger = _Ledger()
    for slot in a.slots:
        teacher = a.teacher.get(slot.key)
        if teacher is not None and a.status.get(slot.key) in CHARGED:
            ledger.add(slot, teacher)
    teachers = sorted({t for t in a.teacher.values() if t is not None})
    days = {str(t): ledger.days(t) for t in teachers}
    idle = {str(t): ledger.idle(t) for t in teachers}
    return {
        "working_days": sum(days.values()),
        "idle_minutes": sum(idle.values()),
        "working_days_by_instructor": days,
        "idle_minutes_by_instructor": idle,
        "load_by_instructor": {str(t): ledger.load[t] for t in teachers},
    }


def assignment_key(a: TeacherAssignment) -> tuple[int, int, int, int, int]:
    """(L1..L5) of ``a`` measured on its own layout, merged idle for L5."""
    big = max((s.n_c for s in a.slots), default=0) + 1
    tbd = [s for s in a.slots if a.status.get(s.key) == STATUS_TBD]
    schedule = schedule_of(a)
    soft = soft_weekly()
    over = sum(max(0, load - soft) for load in schedule["load_by_instructor"].values())
    return (
        len(tbd),
        sum(big * int(s.placed) + (s.n_c + 1 - s.rank) for s in tbd),
        over,
        schedule["working_days"],
        schedule["idle_minutes"],
    )


def adds_violations(candidate: TeacherAssignment, incumbent: TeacherAssignment) -> bool:
    new, old = violations(candidate), violations(incumbent)
    return (
        new["clash_pairs"] > old["clash_pairs"]
        or new["over_cap_sessions"] > old["over_cap_sessions"]
    )


def tbd_d10_floor(slots: Iterable[Slot]) -> int:
    """A layout-free lower bound on the TBD sections D10 forces (2.10)."""
    by_course: dict[str, list[Slot]] = defaultdict(list)
    for slot in slots:
        by_course[slot.course_key].append(slot)
    floor = 0
    for members in by_course.values():
        n_c = members[0].course_sections
        cap_c = members[0].course_cap
        if n_c <= D10_APPLIES_ABOVE or cap_c is None:
            continue
        eligible = {t for s in members for t in s.eligible}
        if not eligible:
            continue
        fixed = [s for s in members if s.fixed is not None or s.fixed_kind]
        fixed_by = Counter(s.fixed for s in fixed if s.fixed is not None)
        capacity = sum(max(0, cap_c - fixed_by[t]) for t in eligible)
        floor += max(0, (n_c - len(fixed)) - capacity)
    return floor


def coverage_of(a: TeacherAssignment) -> dict[str, Any]:
    """Who teaches what, next to the days and idle (F4): a section with no
    teacher must never make the numbers look better unseen."""
    counts = Counter(a.status.values())
    tbd_slots = [s for s in a.slots if a.status.get(s.key) == STATUS_TBD]
    floor = tbd_d10_floor(a.slots)
    schedule = schedule_of(a)
    soft = soft_weekly()
    loads = schedule["load_by_instructor"]
    reasons = Counter(a.tbd_reason.get(s.key, "bound") for s in tbd_slots)
    fixed_per_course: Counter = Counter()
    course_caps: dict[str, int | None] = {}
    for slot in a.slots:
        course_caps[slot.course_key] = slot.course_cap
        if slot.fixed is not None:
            fixed_per_course[(slot.course_key, slot.fixed)] += 1
    d10_by_pins = sorted(
        (course, teacher)
        for (course, teacher), n in fixed_per_course.items()
        if course_caps.get(course) is not None and n > course_caps[course]
    )
    fixed_only = TeacherAssignment(
        teacher={s.key: s.fixed for s in a.slots if s.fixed is not None},
        status={s.key: STATUS_FROZEN for s in a.slots if s.fixed is not None},
        tbd_reason={},
        kept=frozenset(),
        universe=frozenset(),
        slots=a.slots,
    )
    report = a.report or {}
    return {
        "sections_total": len(a.slots),
        "sections_with_links": sum(1 for s in a.slots if s.eligible or s.fixed is not None),
        "assigned": sum(counts[s] for s in CHARGED),
        "assigned_engine": counts[STATUS_ENGINE],
        "pinned": counts[STATUS_PINNED],
        "frozen": counts[STATUS_FROZEN],
        "frozen_unknown": [
            {"course_key": s.course_key, "label": s.label, "name": s.frozen_name}
            for s in a.slots
            if a.status.get(s.key) == STATUS_FROZEN_UNKNOWN
        ],
        "tbd": len(tbd_slots),
        "tbd_d10_floor": floor,
        "tbd_beyond_d10": max(0, len(tbd_slots) - floor),
        "tbd_reasons": {reason: reasons.get(reason, 0) for reason in TBD_REASONS},
        "tbd_layout": sum(1 for s in tbd_slots if a.tbd_reason.get(s.key) in _LAYOUT_REASONS),
        "no_links": counts[STATUS_NO_LINKS],
        "placed_without_teacher": sum(
            1
            for s in a.slots
            if s.placed and a.status.get(s.key) in (STATUS_TBD, STATUS_FROZEN_UNKNOWN)
        ),
        # Sections not placed yet whose teacher has no option of theirs left:
        # the required-section retry cannot place them while this map holds.
        "unplaced_teacher_busy": sorted(
            _unplaceable_keys(
                a.slots,
                {
                    k: t
                    for k, t in a.teacher.items()
                    if t is not None and a.status.get(k) in CHARGED
                },
            )
        ),
        "teachers": len(loads),
        "max_weekly_sessions": max(loads.values(), default=0),
        "teachers_over_12": sorted(int(t) for t, load in loads.items() if load > soft),
        "violations": violations(a),
        "kept_in_violation": sorted(a.kept),
        "fixed_conflicts": violations(fixed_only),
        "stale_rows_ignored": int(report.get("stale_rows_ignored", 0)),
        "pins_orphaned": list(report.get("pins_orphaned", [])),
        "non_primary_conflicts": list(report.get("non_primary_conflicts", [])),
        "d10_violated_by_pins": [list(row) for row in d10_by_pins],
        "programmes_source": report.get("programmes_source", ""),
        "tbd_sections": [
            {
                "course_key": s.course_key,
                "label": s.label,
                "placed": s.placed,
                "reason": a.tbd_reason.get(s.key),
                # What stops each eligible teacher (teacher id -> rule).
                "blocked_by": (report.get("tbd_blockers") or {}).get(s.key, {}),
            }
            for s in sorted(tbd_slots, key=lambda s: s.key)
        ][:50],
    }


def stage_report(
    assignment: TeacherAssignment, *, solver: TeacherAssignment | None = None, **extra: Any
) -> dict:
    """One run stage's record in ``result["section_teachers"]``: how the map was
    chosen and what it covers. ``solver`` is the solve behind a map that was
    stored and read back (whose own report only says ``"stored"``): its stages,
    proof and fallback reason are the record; the coverage is the stored map's."""
    source = solver if solver is not None else assignment
    report = {k: v for k, v in (source.report or {}).items() if k != "tbd_blockers"}
    return {"report": report, "coverage": coverage_of(assignment), **extra}


def pad_schedule_metrics(metrics: Mapping, universe: Iterable) -> dict:
    """``instructor_schedule_metrics`` with a 0 for every teacher in
    ``universe`` it does not mention, so a teacher who lost every section reads
    0 rather than missing evidence. The totals are unchanged (they are sums of
    zeros) and ``affected_instructors`` counts the padded set, so the evidence
    checks in ``_instructor_metric_regressions`` stay consistent."""
    padded = dict(metrics)
    ids = {str(i) for i in universe}
    for key in ("working_days_by_instructor", "idle_minutes_by_instructor"):
        values = {str(i): v for i, v in (metrics.get(key) or {}).items()}
        ids |= set(values)
        padded[key] = values
    for key in ("working_days_by_instructor", "idle_minutes_by_instructor"):
        padded[key] = {i: padded[key].get(i, 0) for i in sorted(ids)}
    padded["working_days"] = int(metrics.get("working_days") or 0)
    padded["idle_minutes"] = int(metrics.get("idle_minutes") or 0)
    padded["affected_instructors"] = len(ids)
    return padded


def teacher_bounds(before_metrics: Mapping) -> dict[str, tuple[int, int, int]]:
    """Each teacher's run-start week as a bound: ``(days, idle, allowance)``.

    The allowance is the bounded day trade's idle per saved day when that trade
    is active, else 0 — the runner's own per-instructor rules (C5/C7).
    """
    from core.services.timetable_pr4_instructor import (
        get_instructor_day_trade_config,
        is_instructor_day_trade_active,
    )

    allowance = (
        get_instructor_day_trade_config()["idle_allowance"]
        if is_instructor_day_trade_active()
        else 0
    )
    days = before_metrics.get("working_days_by_instructor") or {}
    idle = before_metrics.get("idle_minutes_by_instructor") or {}
    return {
        str(i): (int(d), int(idle.get(i, idle.get(str(i), 0)) or 0), allowance)
        for i, d in days.items()
    }


# ── Read path ────────────────────────────────────────────────────────────


def _validated_rows(slots: list[Slot], info: Mapping[str, Any]) -> tuple[dict[str, int], list]:
    """The stored engine rows that still hold, every check layout-free (1.4)."""
    by_key = {s.key: s for s in slots}
    ignored: list[list[str]] = [list(k) for k in info.get("stale_row_keys", [])]
    valid: dict[str, int] = {}
    for key, teacher in info.get("engine_rows", {}).items():
        slot = by_key.get(key)
        if slot is None or not slot.is_choice or teacher not in slot.eligible:
            ignored.append(list(slot.row_key) if slot else [key, ""])
            continue
        valid[key] = teacher
    ledger = _fixed_ledger(slots, {})
    # D10 and the policy cap: keep the lowest-ranked rows of each (teacher, course).
    groups: dict[tuple[int, str], list[Slot]] = defaultdict(list)
    for key, teacher in valid.items():
        groups[(teacher, by_key[key].course_key)].append(by_key[key])
    for (teacher, course_key), members in groups.items():
        cap_c = members[0].course_cap
        if cap_c is None:
            continue
        limit = max(0, cap_c - ledger.per_course[(teacher, course_key)])
        for slot in sorted(members, key=lambda s: s.rank)[limit:]:
            valid.pop(slot.key, None)
            ignored.append(list(slot.row_key))
    # The weekly ceiling: drop the highest-ranked rows until it holds.
    per_teacher: dict[int, list[Slot]] = defaultdict(list)
    for key, teacher in valid.items():
        per_teacher[teacher].append(by_key[key])
    for teacher, members in per_teacher.items():
        load = ledger.load[teacher] + sum(s.nominal_sessions for s in members)
        for slot in sorted(members, key=lambda s: (-s.rank, s.course_key), reverse=False):
            if load <= ledger.ceiling:
                break
            valid.pop(slot.key, None)
            ignored.append(list(slot.row_key))
            load -= slot.nominal_sessions
    return valid, ignored


def _layout_fingerprint(scenario_id: int) -> tuple:
    from core.models import SectionPlacement

    return tuple(
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .order_by("pk")
        .values_list("pk", "term_section_id", "day", "start_time", "end_time")
    )


def load_assignment(scenario_id: int, *, ignore_pin: bool = False) -> TeacherAssignment:
    """The map in force for ``scenario_id`` on its current layout. Never writes.

    Pinned inside a run (``ignore_pin`` reads what is stored instead). Otherwise:
    the stored engine rows that still hold (eligible, active, within D10/the
    policy cap/the weekly ceiling — every check ignores the layout), pins and
    frozen teachers as fixed load, and the layout-aware greedy closure for the
    sections without a valid row.
    """
    pinned = None if ignore_pin else current_pin(scenario_id)
    if pinned is not None:
        return pinned
    cache = _scope_cache(scenario_id)
    fingerprint = None
    if cache is not None:
        fingerprint = _layout_fingerprint(scenario_id)
        hit = cache.get("assignment")
        if hit is not None and hit[0] == fingerprint:
            return hit[1]
    slots, info = _collect(scenario_id)
    valid, ignored = _validated_rows(slots, info)
    filled, added = _closure(slots, valid)
    report = dict(filled.report)
    report.update(
        engine="read",
        programmes_source=info["programmes_source"],
        pins_orphaned=info["pins_orphaned"],
        non_primary_conflicts=info["non_primary_conflicts"],
        key_collisions=info["key_collisions"],
        stale_rows_ignored=len(ignored),
        ignored_row_keys=ignored,
        closure_keys=added,
    )
    assignment = TeacherAssignment(
        teacher=filled.teacher,
        status=filled.status,
        tbd_reason=filled.tbd_reason,
        kept=frozenset(),
        universe=filled.universe,
        report=report,
        slots=filled.slots,
    )
    if cache is not None:
        cache["assignment"] = (fingerprint, assignment)
    return assignment


def close_on_layout(scenario_id: int, assignment: TeacherAssignment, *, bounds=None):
    """``assignment`` closure-completed on the saved layout (K of 3.1 G5 / 3.3 C7):
    its engine picks are fixed load, and every other section some teacher can
    still take gets one."""
    slots, info = _collect(scenario_id)
    seed = {
        key: teacher
        for key, teacher in assignment.teacher.items()
        if teacher is not None and assignment.status.get(key) == STATUS_ENGINE
    }
    closed = greedy_assign(slots, seed=seed, bounds=bounds)
    closed.report.update(engine="closure", programmes_source=info["programmes_source"])
    return closed


def propose_layout_assignment(
    scenario_id: int,
    *,
    sections=None,
    current=None,
    bounds=None,
    keep_violations_of=None,
) -> TeacherAssignment:
    """The exact assignment on a layout, in memory only.

    ``sections`` is an in-memory layout (a ranked candidate); default the saved
    board. ``current`` anchors stability; ``keep_violations_of`` names the map
    in force whose clashing/over-cap sections are kept (the kept rule).
    """
    slots, info = _collect(scenario_id, sections=sections)
    cur = _current_map(slots, current) if current is not None else None
    kept: set[str] = set()
    if keep_violations_of is not None:
        kept = _violating_keys(slots, _current_map(slots, keep_violations_of))
    result = solve_layout_aware(
        slots, current=cur if cur is not None else None, bounds=bounds, kept=kept
    )
    result.report.update(
        programmes_source=info["programmes_source"],
        pins_orphaned=info["pins_orphaned"],
        non_primary_conflicts=info["non_primary_conflicts"],
        key_collisions=info["key_collisions"],
    )
    return result


# ── The placer: the free-option rule while it places ─────────────────────

#: Added to the first element of an option's score in ``auto_place_board``
#: when ``PlacerGuard`` ranks it last — above any count of student conflicts.
GUARD_RANK = 10**9


class PlacerGuard:
    """The free-option rule kept while the greedy placer places (spec 3.2).

    The assignment gives a section not placed yet only to a teacher who keeps
    one of its placer options free. The placer then places that teacher's
    sections one at a time, blind to the rule, and could take the option the
    later section needed: on lab scenario 667 (board P_G333, Generate, seed
    4242) the candidate build put instructor 308's three AI124 sections on
    Sunday, Monday and Tuesday, three sessions each, so DS331 S1 — 308's and
    only 308's — had none of its 190 options left; the required-section retry
    placed it with no teacher. :meth:`check` ranks such an option after every
    option that does not do it, and likewise an option of a section still to
    be decided at which none of its eligible teachers could take it. Neither
    is a hard filter: when every option ranks last the best of them is still
    placed (the required-section guarantee) and the section stays to be
    decided, with its reason in the coverage.
    """

    RANK = GUARD_RANK

    def __init__(self, assignment: TeacherAssignment) -> None:
        from core.services.timetable_autoplace import WEEKDAYS

        ledger = _Ledger()
        for slot in assignment.slots:
            teacher = assignment.teacher.get(slot.key)
            if teacher is not None and assignment.status.get(slot.key) in CHARGED:
                ledger.add(slot, teacher)
        self.clash_on, self.cap_on, self.cap = ledger.clash_on, ledger.cap_on, ledger.cap
        self.days = {day: index for index, day in enumerate(WEEKDAYS)}
        #: teacher -> [(placer key, options)] of the sections charged to them
        #: that were not placed when the map was chosen.
        self.pending: dict[int, list[tuple[str, tuple]]] = {
            teacher: [(slot.pr4_key, slot.options) for slot in slots]
            for teacher, slots in ledger.pending.items()
        }
        #: placer key -> the eligible teachers of a section still to be decided
        #: whom only the layout stops (not D10, not the weekly ceiling).
        self.open: dict[str, tuple[int, ...]] = {}
        for slot in assignment.slots:
            if assignment.status.get(slot.key) != STATUS_TBD:
                continue
            free = tuple(t for t in slot.eligible if ledger.layout_free_blocker(slot, t) is None)
            if free:
                self.open[slot.pr4_key] = free

    def _meetings(self, option) -> list[tuple[int, int, int]]:
        return [
            (self.days[m["day"].upper()], _minutes(m["start"]), _minutes(m["end"]))
            for m in option
            if m["day"].upper() in self.days
        ]

    def _load(self, schedule, teacher) -> tuple[list, Counter]:
        cells = [
            (self.days[day], start, end)
            for _section, day, start, end in schedule.get(teacher, ())
            if day in self.days
        ]
        return cells, Counter(day for day, _s, _e in cells)

    def _free(self, option, cells, per_day) -> bool:
        """``option`` clears a teacher holding ``cells`` (clash, daily cap)."""
        if self.clash_on and _overlaps(option, cells):
            return False
        return not (
            self.cap_on
            and any(
                per_day[day] + count > self.cap
                for day, count in Counter(d for d, _s, _e in option).items()
            )
        )

    def check(self, key: str, teachers, schedule):
        """For the section ``key`` (``course|label``) about to be placed, an
        ``option -> bool`` that is True when the option ranks last, or None when
        nothing is at stake. ``teachers`` are the section's charged teachers
        (none: still to be decided) and ``schedule`` the placer's
        ``instructor_schedule_full`` (teacher -> ``{(key, DAY, start, end)}``)."""
        if not (self.clash_on or self.cap_on):
            return None
        if teachers:
            at_stake = []
            for teacher in teachers:
                cells, per_day = self._load(schedule, teacher)
                placed = {section for section, *_rest in schedule.get(teacher, ())}
                for other, options in self.pending.get(teacher, ()):
                    if other == key or other in placed:
                        continue
                    # One already without a free option stays as it is: the
                    # placer is not held back to win it a time.
                    if any(self._free(o, cells, per_day) for o in options):
                        at_stake.append((options, cells, per_day))
            if not at_stake:
                return None

            def boxes_a_later_section(option) -> bool:
                mine = self._meetings(option)
                added = Counter(day for day, _s, _e in mine)
                return any(
                    not any(self._free(o, cells + mine, per_day + added) for o in options)
                    for options, cells, per_day in at_stake
                )

            return boxes_a_later_section
        candidates = self.open.get(key)
        if not candidates:
            return None
        loads = [self._load(schedule, teacher) for teacher in candidates]

        def no_teacher_can_take_it(option) -> bool:
            mine = self._meetings(option)
            return not any(self._free(mine, cells, per_day) for cells, per_day in loads)

        return no_teacher_can_take_it


# ── Write path ───────────────────────────────────────────────────────────


def _engine_rows_by_key(rows) -> dict[tuple[str, str], tuple[int, int]]:
    """This scenario's engine rows by key, from the rows ``_collect`` read."""
    return {
        (_norm(ck), _norm(label)): (pk, teacher)
        for pk, _scenario, ck, label, teacher, role, source in rows
        if source == SOURCE_ENGINE and role == ROLE_PRIMARY
    }


def _non_engine_primary_keys(rows) -> set[tuple[str, str]]:
    return {
        (_norm(ck), _norm(label))
        for _pk, _scenario, ck, label, _teacher, role, source in rows
        if role == ROLE_PRIMARY and source != SOURCE_ENGINE
    }


def _frozen_row_keys(scenario_id: int) -> set[tuple[str, str]]:
    """Row keys of the teacher-frozen sections, computed at write time (the
    same helper the snapshot restore uses)."""
    from core.services.timetable_board_persistence import _teacher_frozen_row_keys

    return _teacher_frozen_row_keys(scenario_id)


def persist_assignment(
    scenario_id: int, assignment: TeacherAssignment, *, only_missing: bool = False
) -> dict[str, Any]:
    """Store the engine's picks as ``source="engine"`` primary rows.

    The map is first closure-completed on the saved layout, so it reads back
    exactly. A full persist replaces every engine row of a non-frozen section;
    ``only_missing`` deletes only the rows the read path ignored and inserts
    only what the closure filled. Pins and frozen sections are never written.
    """
    return _persist(scenario_id, assignment, only_missing=only_missing)[1]


def persist_and_read(
    scenario_id: int, assignment: TeacherAssignment
) -> tuple[TeacherAssignment, dict[str, Any]]:
    """Full persist, returning the stored map as the read path will see it on
    this layout (the closure-complete map that was written) without reading
    ``section_instructors`` again."""
    stored, report = _persist(scenario_id, assignment, only_missing=False)
    return stored, report


def _persist(
    scenario_id: int, assignment: TeacherAssignment, *, only_missing: bool
) -> tuple[TeacherAssignment, dict[str, Any]]:
    from core.models import SectionInstructor

    with transaction.atomic():
        slots, info = _collect(scenario_id)
        seed = {
            key: teacher
            for key, teacher in assignment.teacher.items()
            if teacher is not None and assignment.status.get(key) == STATUS_ENGINE
        }
        final, _added = _closure(slots, seed)
        frozen_keys = _frozen_row_keys(scenario_id)
        pinned_keys = _non_engine_primary_keys(info.get("rows", []))
        existing = _engine_rows_by_key(info.get("rows", []))
        if only_missing:
            ignored = {tuple(k) for k in (assignment.report or {}).get("ignored_row_keys", [])}
            doomed = [
                pk
                for key, (pk, _t) in existing.items()
                if key in ignored and key not in frozen_keys
            ]
        else:
            doomed = [pk for key, (pk, _t) in existing.items() if key not in frozen_keys]
        deleted = 0
        if doomed:
            deleted, _ = SectionInstructor.objects.filter(pk__in=doomed).delete()
        kept_rows = {key for key, (pk, _t) in existing.items() if pk not in set(doomed)}
        skipped: list[str] = []
        new_rows = []
        for slot in final.slots:
            teacher = final.teacher.get(slot.key)
            if teacher is None or final.status.get(slot.key) != STATUS_ENGINE:
                continue
            row_key = slot.row_key
            if row_key in frozen_keys:
                continue
            if row_key in pinned_keys:
                skipped.append(slot.key)
                continue
            if row_key in kept_rows:
                continue
            new_rows.append(
                SectionInstructor(
                    scenario_id=scenario_id,
                    course_key=row_key[0],
                    section=row_key[1],
                    instructor_id=teacher,
                    role=ROLE_PRIMARY,
                    source=SOURCE_ENGINE,
                )
            )
        for row in new_rows:
            if row.source != SOURCE_ENGINE or row.role != ROLE_PRIMARY:  # pragma: no cover
                raise ValueError("the engine only ever writes engine primary rows")
        if new_rows:
            SectionInstructor.objects.bulk_create(new_rows, batch_size=500)
    invalidate_scope(scenario_id)
    if skipped:
        logger.warning("One-teacher persist skipped pinned sections %s", skipped[:10])
    stored = TeacherAssignment(
        teacher=final.teacher,
        status=final.status,
        tbd_reason=final.tbd_reason,
        kept=frozenset(
            k for k in assignment.kept if final.teacher.get(k) == assignment.teacher.get(k)
        ),
        universe=final.universe,
        report={
            **final.report,
            "engine": "stored",
            "programmes_source": info.get("programmes_source", ""),
        },
        slots=final.slots,
    )
    return stored, {"written": len(new_rows), "deleted": deleted, "skipped_pinned": skipped}


def _scenario_rows(scenario_id: int) -> list[tuple]:
    from core.models import SectionInstructor

    return list(
        SectionInstructor.objects.filter(scenario_id=scenario_id).values_list(
            "pk", "scenario_id", "course_key", "section", "instructor_id", "role", "source"
        )
    )


def delete_engine_rows(scenario_id: int) -> int:
    """Drop the engine rows of every non-frozen section (a rebuild's first step)."""
    from core.models import SectionInstructor

    frozen_keys = _frozen_row_keys(scenario_id)
    doomed = [
        pk
        for key, (pk, _t) in _engine_rows_by_key(_scenario_rows(scenario_id)).items()
        if key not in frozen_keys
    ]
    deleted = SectionInstructor.objects.filter(pk__in=doomed).delete()[0] if doomed else 0
    invalidate_scope(scenario_id)
    return deleted


def prepare_section_teachers(scenario_id: int, *, phase: str, sections=None) -> dict[str, Any]:
    """The run hooks (section 3 of the Phase 1 spec).

    ``rebuild``: after the ownership check, keep the previous map, drop the
    engine rows and seed a time-blind map on what survives the reset (the
    locked sections) — ``{"assignment", "previous"}``. ``layout``: re-pick on
    the saved layout, stable against the map in force and keeping its
    violations, persist it and re-write the names. ``ensure``: persist the read
    path's closure for the sections that have no row yet, and name only those.
    """
    from core.services.board_ownership import assert_rebuild_ownership
    from core.services.timetable_board_persistence import locked_physical_section_ids

    if phase == "rebuild":
        assert_rebuild_ownership(scenario_id)
        previous = load_assignment(scenario_id)
        delete_engine_rows(scenario_id)
        slots, info = _collect(
            scenario_id, sections=sections, placed_filter=locked_physical_section_ids(scenario_id)
        )
        seed = greedy_assign(slots, current=previous)
        seed.report.update(engine="pre_build", programmes_source=info["programmes_source"])
        written = persist_assignment(scenario_id, seed)
        return {"assignment": seed, "previous": previous, "persist": written}
    if phase == "layout":
        current = load_assignment(scenario_id)
        chosen = propose_layout_assignment(scenario_id, current=current, keep_violations_of=current)
        written = persist_assignment(scenario_id, chosen)
        chosen = load_assignment(scenario_id)
        write_names(scenario_id, chosen)
        return {"assignment": chosen, "previous": current, "persist": written}
    if phase == "ensure":
        current = load_assignment(scenario_id)
        written = persist_assignment(scenario_id, current, only_missing=True)
        keys = set((current.report or {}).get("closure_keys") or [])
        write_names(scenario_id, current, only_keys=keys)
        return {"assignment": current, "persist": written}
    raise ValueError(f"unknown phase {phase!r}")


def run_placer(scenario_id: int, place, *, rebuild: bool):
    """A greedy build with one teacher per section (spec 3.2).

    ``rebuild`` (the board was just cleared): the time-blind pre-build map is
    pinned while ``place()`` runs, then the teachers are re-picked on the board
    it produced, stable against that map. Otherwise (place what is missing and
    keep the rest): the closure is stored before and after ``place()``, which
    runs under the map in force — existing teachers stay.
    """
    if rebuild:
        prepared = prepare_section_teachers(scenario_id, phase="rebuild")
        with pinned_assignment(scenario_id, prepared["assignment"]):
            result = place()
        final = prepare_section_teachers(scenario_id, phase="layout")["assignment"]
    else:
        prepare_section_teachers(scenario_id, phase="ensure")
        with pinned_assignment(scenario_id, load_assignment(scenario_id)):
            result = place()
        final = prepare_section_teachers(scenario_id, phase="ensure")["assignment"]
    if isinstance(result, dict):
        result["section_teachers"] = {"enabled": True, "final": stage_report(final)}
    return result


def name_new_section(scenario_id: int, term_section, board) -> dict:
    """After a manual create (3.4): store the closure for the sections that have
    no teacher yet — the layout-aware closure picks a teacher with no clash or
    over-cap day when there is one — and name the new section."""
    from core.services.course_identity import course_display_code
    from core.services.course_instructor_assignment import apply_primary_instructor

    prepared = prepare_section_teachers(scenario_id, phase="ensure")
    apply_primary_instructor(
        term_section,
        board.scenario,
        board,
        course_display_code(
            term_section.course_key, term_section.course_code, term_section.course_number
        ),
        assignment=prepared["assignment"],
    )
    return prepared["persist"]


def write_names(scenario_id: int, assignment: TeacherAssignment, *, only_keys=None) -> int:
    """Write each section's own teacher into its meeting rows (5.1).

    Engine and pinned sections get their teacher's name, a TBD section an
    empty name; frozen, unknown-frozen and no-links sections are not touched.
    """
    from core.models import Instructor, TermSectionMeeting

    names = dict(
        Instructor.objects.filter(
            pk__in={t for t in assignment.teacher.values() if t is not None}
        ).values_list("pk", "full_name")
    )
    updated = 0
    for slot in assignment.slots:
        if slot.term_section_id is None or (only_keys is not None and slot.key not in only_keys):
            continue
        status = assignment.status.get(slot.key)
        if status in (STATUS_ENGINE, STATUS_PINNED):
            name = names.get(assignment.teacher[slot.key], "")
        elif status == STATUS_TBD:
            name = ""
        else:
            continue
        if (
            TermSectionMeeting.objects.filter(term_section_id=slot.term_section_id)
            .exclude(instructor=name)
            .update(instructor=name)
        ):
            updated += 1
    return updated


def status_by_term_section_id(
    scenario_id: int,
) -> dict[int, tuple[int | None, str | None, str, str | None]]:
    """``{term_section_id: (teacher_id, teacher_name, status, tbd_reason)}``,
    from the one map in force — what the drawer, the export and the name
    writers show, so they agree with the engine before any reconcile."""
    return {
        ts_id: (
            row["id"],
            row["name"] if row["id"] is not None else None,
            row["status"],
            row["reason"],
        )
        for ts_id, row in section_teacher_display(scenario_id).items()
    }


def section_teacher_display(scenario_id: int) -> dict[int, dict[str, Any]]:
    """``{term_section_id: {"id", "name", "status", "source", "reason"}}`` — the
    drawer's and the export's teacher per placed section, from the one map in
    force (``status_by_term_section_id`` is the same map as tuples).

    ``source`` says where the teacher comes from: the stored row's source for
    ``engine`` and ``pinned`` (``derived`` for a teacher the read path chose
    that no row stores yet), ``owner`` or ``meeting`` for ``frozen``; for
    ``frozen_unknown`` the name is the meeting's free text. ``reason`` is the
    TBD reason.
    """
    from core.models import Instructor

    assignment = load_assignment(scenario_id)
    names = dict(
        Instructor.objects.filter(
            pk__in={t for t in assignment.teacher.values() if t is not None}
        ).values_list("pk", "full_name")
    )
    out: dict[int, dict[str, Any]] = {}
    for slot in assignment.slots:
        if slot.term_section_id is None:
            continue
        teacher = assignment.teacher.get(slot.key)
        status = assignment.status.get(slot.key, STATUS_NO_LINKS)
        name = names.get(teacher) if teacher is not None else None
        source: str | None = None
        if status == STATUS_ENGINE:
            source = SOURCE_ENGINE if slot.stored == teacher else "derived"
        elif status in (STATUS_PINNED, STATUS_FROZEN):
            source = slot.source or status
        elif status == STATUS_FROZEN_UNKNOWN and slot.frozen_name:
            name, source = slot.frozen_name, "meeting"
        out[slot.term_section_id] = {
            "id": teacher,
            "name": name,
            "status": status,
            "source": source,
            "reason": assignment.tbd_reason.get(slot.key),
        }
    return out


def bridge_section_teachers(
    scenario_id: int, rows: Mapping[tuple[str, str], int | None]
) -> dict | None:
    """The scheduler bridge's entry point: None (nothing touched) unless one
    teacher per section is on. Lets ``scheduler`` stay clear of the classic
    planner's own modules."""
    from core.services.timetable_flags import is_one_teacher_active

    if not is_one_teacher_active():
        return None
    return persist_bridge_assignment(scenario_id, rows)


def persist_bridge_assignment(scenario_id: int, rows: Mapping[tuple[str, str], int | None]) -> dict:
    """Store the scheduler bridge's per-section teachers under the classic rules.

    Touches engine rows of the bridge's own keys only. A row is written when
    its teacher is eligible for the scenario's programmes, keeps within the
    course cap and the weekly ceiling (classic nominal sessions, counting the
    rows already accepted and every fixed teacher), and the section is neither
    pinned nor frozen. The rest are returned as rejected and left to the read
    path.
    """
    from core.models import SectionInstructor

    keys = {(_norm(ck), _norm(label)) for ck, label in rows}
    with transaction.atomic():
        existing = _engine_rows_by_key(_scenario_rows(scenario_id))
        doomed = [pk for key, (pk, _t) in existing.items() if key in keys]
        if doomed:
            SectionInstructor.objects.filter(pk__in=doomed).delete()
        invalidate_scope(scenario_id)
        slots, _info = _collect(scenario_id)
        by_row = {s.row_key: s for s in slots}
        ledger = _fixed_ledger(slots, {})
        for slot in slots:
            if slot.stored is not None and slot.row_key not in keys and slot.is_choice:
                ledger.add(slot, slot.stored)
        accepted, rejected = [], []
        for (course_key, label), teacher in sorted(
            rows.items(), key=lambda kv: (_norm(kv[0][0]), _label_order(_norm(kv[0][1])))
        ):
            slot = by_row.get((_norm(course_key), _norm(label)))
            reason = None
            if teacher is None:
                continue
            if slot is None:
                reason = "not_a_slot"
            elif not slot.is_choice:
                reason = "pinned_or_frozen" if slot.fixed_kind else "not_eligible"
            elif teacher not in slot.eligible:
                reason = "not_eligible"
            elif (
                slot.course_cap is not None
                and ledger.per_course[(teacher, slot.course_key)] >= slot.course_cap
            ):
                reason = "d10"
            elif ledger.load[teacher] + slot.nominal_sessions > ledger.ceiling:
                reason = "weekly_ceiling"
            if reason is not None:
                rejected.append(
                    {
                        "course_key": course_key,
                        "label": label,
                        "instructor_id": teacher,
                        "reason": reason,
                    }
                )
                continue
            ledger.add(slot, teacher)
            accepted.append(
                SectionInstructor(
                    scenario_id=scenario_id,
                    course_key=slot.row_key[0],
                    section=slot.row_key[1],
                    instructor_id=teacher,
                    role=ROLE_PRIMARY,
                    source=SOURCE_ENGINE,
                )
            )
        if accepted:
            SectionInstructor.objects.bulk_create(accepted, batch_size=500)
    invalidate_scope(scenario_id)
    return {"written": len(accepted), "deleted": len(doomed), "bridge_rejected": rejected}

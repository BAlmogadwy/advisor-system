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
  :func:`section_online_exempt` is the one online exemption (a section the
  rules do not bind is online on EVERY board it sits on and on every board it
  would be placed on), asked through the scope's :func:`online_exemption`.

Manual moves and validation never call anything here; the workspace SHOWS the rules
through :func:`display_rule_checker` (slice C, ``timetable_workspace._display_rule_checker``).
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any

from core.services.instructor_availability import (
    DAY_CODES,
    EMPTY_AVAILABILITY,
    InstructorAvailability,
    TimeRule,
    compile_rules,
    load_instructor_availability,
    load_time_rules,
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
    """What one scope loaded for one scenario: the availability (one rule query,
    plus one more only when the teacher universe grows, fix plan D-G), the
    writers' :class:`RuleChecker` and the :class:`OnlineExemption`'s static half.

    ``universe``: the teacher ids whose rules are loaded; ``everyone``: the rule
    table held no row at all when it was read, so every universe is covered.
    ``released``: the sections the scope's run released from their engine
    teachers (:func:`record_release`, fix plan D-B)."""

    __slots__ = (
        "checker",
        "display",
        "everyone",
        "loaded",
        "online",
        "pending_rules",
        "released",
        "universe",
        "value",
    )

    def __init__(self) -> None:
        self.loaded = False
        self.value: InstructorAvailability = EMPTY_AVAILABILITY
        self.universe: frozenset[int] = frozenset()
        self.everyone = False
        self.checker: RuleChecker | None = None
        # The display door's own checker with the (availability, assignment) it was
        # built from; never ``checker`` (slice C, R7).
        self.display: tuple[Any, Any, RuleChecker] | None = None
        self.online: OnlineExemption | None = None
        # Every stored rule, read by the universe-less path while its collect
        # works out the universe (so that collect needs no second rule query).
        self.pending_rules: list[TimeRule] | None = None
        # V2 section id -> (the engine teachers it was released from, the run's
        # pinned map it was released under, or None).
        self.released: dict[str, tuple[tuple[int, ...], Any]] = {}

    def store(
        self, value: InstructorAvailability, universe: Iterable[int], *, everyone: bool = False
    ) -> None:
        self.loaded = True
        self.value = value
        self.universe = frozenset(universe)
        self.everyone = everyone
        self.checker = None  # built from the previous availability
        self.display = None

    def covers(self, universe: Iterable[int]) -> bool:
        return self.everyone or self.universe.issuperset(universe)


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
    fixed teacher of every slot). A scope that loaded a smaller universe loads
    the rules of the whole union once more when a later read brings ids outside
    it (fix plan D-G: an own section placed by a manual create enters the
    universe inside the request), so no teacher is ever unchecked.

    Without ``universe`` one query reads every stored rule: none at all costs
    that one query and nothing else (the clash-off greedy's first question, D-L);
    otherwise the scenario's map is read (``incomplete="collect"``: it never
    raises) to learn the universe, and its collect compiles from the rows already
    read. Rules of instructors outside the universe never make the availability
    non-empty.
    """
    if not rules_enabled():
        return EMPTY_AVAILABILITY
    holder = _holder(scenario_id)
    if holder is not None and holder.loaded:
        if universe is None:
            return holder.value
        wanted = frozenset(universe)
        if holder.covers(wanted):
            return holder.value
        grown = holder.universe | wanted
        value = load_instructor_availability(sorted(grown), scenario=scenario_id)
        holder.store(value, grown)
        return value
    if universe is None:
        return _availability_of_the_scenario(scenario_id)
    ids = frozenset(universe)
    if holder is not None and holder.pending_rules is not None:
        value = compile_rules(r for r in holder.pending_rules if r.instructor_id in ids)
    else:
        value = load_instructor_availability(sorted(ids), scenario=scenario_id)
    if holder is not None:
        holder.store(value, ids)
    return value


def _availability_of_the_scenario(scenario_id: int) -> InstructorAvailability:
    """:func:`availability_for` without a universe (one rule query in all)."""
    from core.services.section_teacher_assignment import load_assignment

    with availability_scope(scenario_id):
        holder = _holder(scenario_id)
        assert holder is not None  # the scope above made it
        if not holder.loaded:
            rules = load_time_rules()
            if not rules:
                holder.store(EMPTY_AVAILABILITY, (), everyone=True)
            else:
                holder.pending_rules = rules
                try:
                    assignment = load_assignment(scenario_id, incomplete="collect")
                finally:
                    holder.pending_rules = None
                if not holder.loaded:  # the collect saw no slot (no gender, say)
                    ids = frozenset(assignment.universe)
                    holder.store(compile_rules(r for r in rules if r.instructor_id in ids), ids)
        return holder.value


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


# ── Sections a run released from their engine teacher (fix plan D-B) ─────


def record_release(
    scenario_id: int, section_id: str, instructor_ids: Iterable[int], pin: Any = None
) -> None:
    """The run of the open scope released ``section_id`` (a V2 id) from its ENGINE
    teachers ``instructor_ids``: their rules leave it no legal placeable option, so
    for the rest of the run it is charged to nobody (like a TBD section) and is
    placed from its ordinary options. ``pin``: the run's pinned map it was
    released under (``section_teacher_assignment.current_pin``), whose reads
    then show it released. Without a scope nothing is kept (the release stays
    the caller's own)."""
    holder = _holder(scenario_id)
    if holder is not None:
        holder.released[section_id] = (tuple(sorted(set(instructor_ids))), pin)


def released_sections(scenario_id: int) -> dict[str, tuple[tuple[int, ...], Any]]:
    """``{V2 section id: (released teachers, pin)}`` of the open scope's run, until
    a layout-aware re-pick stores the map that resolves them
    (:func:`clear_released`). Empty without a scope or a release."""
    holder = _holder(scenario_id)
    return dict(holder.released) if holder is not None else {}


def clear_released(scenario_id: int) -> None:
    """A map closure-complete on the real layout was stored: every release of the
    scope's run is resolved (its section has the teacher the layout allows, or
    none)."""
    holder = _holder(scenario_id)
    if holder is not None:
        holder.released.clear()


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


# ── Semi-automatic writers (bulk safe moves, registration repair) ────────


class RuleChecker:
    """The rules as a semi-automatic writer asks them: may this placement's
    section move to ``(day, start, end)`` without a forbidden meeting?

    Built once per call (cached on the scope) from the scope's availability, the
    map in force (``load_assignment(..., incomplete="collect").v2_map()``) and the
    online exemption (a section online on every board it is placed on is never
    bound). Validation and the manual write paths never build one; the workspace's
    conflict check and slot previews read one (display only).
    """

    def __init__(
        self,
        availability: InstructorAvailability,
        charged: Mapping[str, Iterable[object]],
        exempt: Collection[tuple[str, str]],
    ) -> None:
        self.availability = availability
        self.charged = charged
        self.exempt = exempt
        self._names: dict[int, str] | None = None

    @functools.cached_property
    def ruled_charged(self) -> frozenset[int]:
        """Teachers the map charges who have a rule (ids only; one build)."""
        ruled = self.availability.instructor_ids
        return frozenset(
            i for ids in self.charged.values() for i in ids if _is_id(i) and i in ruled
        )

    @property
    def binds(self) -> bool:
        """Some teacher the map charges has a rule: the workspace's display switch."""
        return bool(self.ruled_charged)

    def section_ids(
        self, course_key: str, label: str, *, exempt: bool | None = None
    ) -> tuple[int, ...]:
        """The section's charged teachers that have a rule, sorted; ``()`` when the
        section is exempt (``exempt=None``: online on every board it sits on,
        ``self.exempt``; True/False: the caller's decision, for a section not placed
        yet) or charged to nobody (TBD, unknown, no links). ``course_key`` is the
        planner identity ``course_key or course_code``."""
        if ((course_key, label) in self.exempt) if exempt is None else exempt:
            return ()
        return tuple(
            sorted(
                i for i in self.charged.get(f"{course_key}_{label}", ()) if i in self.ruled_charged
            )
        )

    def meeting_blocked(
        self, ids: Iterable[int], day: str, start: str, end: str
    ) -> tuple[int, ...]:
        """Those of ``ids`` whose rules this ONE meeting breaks, in ``ids`` order. No
        bundle expansion (display judges each physical meeting alone); a day or time
        that does not read breaks nothing."""
        meetings = option_meetings([{"day": day, "start": start, "end": end}])
        return tuple(
            i for i in ids if any(self.availability.blocks(i, d, s, e) for d, s, e in meetings)
        )

    def teacher_name(self, instructor_id: int) -> str:
        """``Instructor.full_name`` of a ruled charged teacher; ONE query per checker,
        on first use."""
        if self._names is None:
            from core.models import Instructor

            self._names = dict(
                Instructor.objects.filter(pk__in=self.ruled_charged).values_list("pk", "full_name")
            )
        return self._names.get(instructor_id, "")

    def fingerprint(self) -> str:
        """The rules and the ruled teachers the map charges, for caches of an
        analysis made under them (a rule edit or a re-pick invalidates it)."""
        ruled = sorted((sid, rule_ids(self.availability, ids)) for sid, ids in self.charged.items())
        return repr((self.availability.fingerprint(), [row for row in ruled if row[1]]))

    def blocked(self, placement: Any, day: str, start: str, end: str) -> tuple[int, ...]:
        """The charged teachers of ``placement``'s section whose rules the move to
        ``(day, start, end)`` breaks, sorted; ``()`` when it breaks none. A
        meeting-policy (English) section moves as a whole bundle, so EVERY meeting
        of the target pattern is checked (the pattern ``switch_policy_section_bundle``
        selects: the one holding the anchor's new meeting)."""
        from core.services.timetable_course_policy import meeting_signature
        from core.services.timetable_section_policy import section_policy

        section = placement.term_section
        key = (section.course_key or section.course_code, section.section)
        if key in self.exempt:
            return ()
        ids = rule_ids(self.availability, self.charged.get(f"{key[0]}_{key[1]}"))
        if not ids:
            return ()
        meetings = option_meetings([{"day": day, "start": start, "end": end}])
        policy = section_policy(section)
        if policy is not None:
            target = meeting_signature({"day": day, "start_time": start, "end_time": end})
            pattern = next(
                (
                    p
                    for p in policy.patterns
                    if any(meeting_signature(m) == target for m in p.meetings)
                ),
                None,
            )
            if pattern is not None:
                meetings = option_meetings(m.to_dict() for m in pattern.meetings)
        return tuple(
            iid
            for iid in ids
            if any(self.availability.blocks(iid, d, s, e) for d, s, e in meetings)
        )


def section_rule_checker(scenario_id: int) -> RuleChecker | None:
    """The scenario's :class:`RuleChecker`, or None while no rule binds a teacher
    of the scenario (then no query beyond :func:`availability_for`'s). Inside an
    :func:`availability_scope` it is built once and reused."""
    if not rules_enabled():
        return None
    availability = availability_for(scenario_id)
    if not availability:
        return None
    holder = _holder(scenario_id)
    if holder is not None and holder.checker is not None:
        return holder.checker
    from core.services.section_teacher_assignment import load_assignment

    checker = RuleChecker(
        availability,
        load_assignment(scenario_id, incomplete="collect").v2_map(),
        online_exempt_sections(scenario_id),
    )
    if holder is not None:
        holder.checker = checker
    return checker


@contextmanager
def teaching_rules_request() -> Iterator[None]:
    """A request scope for every scenario the request touches, only while the rules
    are on (slice C: the views the display reads from); otherwise a pass-through
    that opens nothing and costs nothing."""
    if not rules_enabled():
        yield
        return
    from core.services.section_teacher_assignment import request_scope

    with request_scope():
        yield


@contextmanager
def display_scope(scenario_id: int) -> Iterator[None]:
    """``assignment_scope(scenario_id)`` while the rules are on (one map read for the
    scenario's link map and the display door), else a pass-through."""
    if not rules_enabled():
        yield
        return
    from core.services.section_teacher_assignment import assignment_scope

    with assignment_scope(scenario_id):
        yield


def display_rule_checker(scenario_id: int) -> RuleChecker | None:
    """The rules as the workspace SHOWS them: a :class:`RuleChecker` over the map in
    force, or None while the rules are off, no teacher the scenario can charge has a
    rule, or no charged teacher does (``binds``); then every answer is byte-identical.

    It builds its OWN checker and never reads or fills the writers' cached one
    (``section_rule_checker``); inside a scope it keeps its own, reused only while
    the scope's memoised map is the same object (a write changes the layout, so the
    memo returns a new one and the checker is rebuilt)."""
    if not rules_enabled():
        return None
    from core.services.section_teacher_assignment import assignment_scope, load_assignment

    with assignment_scope(scenario_id):
        availability = availability_for(scenario_id)
        if not availability:
            return None
        assignment = load_assignment(scenario_id, incomplete="collect")
        holder = _holder(scenario_id)
        cached = holder.display if holder is not None else None
        if cached is not None and cached[0] is availability and cached[1] is assignment:
            checker = cached[2]
        else:
            checker = RuleChecker(
                availability, assignment.v2_map(), online_exempt_sections(scenario_id)
            )
            if holder is not None:
                holder.display = (availability, assignment, checker)
    return checker if checker.binds else None


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
    """Online on EVERY one of ``flags`` (one per board); no board at all is not."""
    flags = list(flags)
    return bool(flags) and all(flags)


def section_online_exempt(placed: Iterable[bool] | None, course_online: bool | None) -> bool:
    """THE online exemption (fix plan D-A), the one implementation every stage
    uses: the rules do not bind a section that is online on EVERY board it sits
    on AND on every board it WOULD be placed on.

    ``placed``: one flag per board the section sits on (a placer about to put it
    on one more board includes that board's flag); ``course_online``: whether its
    course is online on every board of its term (the boards it would be placed
    on, :meth:`OnlineExemption.course_online_everywhere`), None when the course
    has no budget (an own section is placed only where it already is). So:

    * placed, budgeted: every placed copy online AND the course online everywhere;
    * placed, no budget: every placed copy online;
    * not placed yet: the course online everywhere (no budget: never exempt).

    A section online on board A and campus on board B is bound while board A is
    being placed, whatever the board order (construction:CON-1)."""
    flags = tuple(placed or ())
    if flags and not all(flags):
        return False
    if course_online is None:
        return bool(flags)
    return course_online


class OnlineExemption:
    """:func:`section_online_exempt` for one scenario's sections.

    The static half (the boards of each term, the budgets, one online-course
    lookup per programme list) is read once per object, and the scope's holder
    keeps the object (:func:`online_exemption`), so a run reads it once (fix plan
    D-A / identity:F6). Only the placed-copies scan (:meth:`placed_rows`) runs
    again on each call, one query.
    """

    def __init__(self, scenario_id: int, *, budgets: Mapping[str, Any] | None = None) -> None:
        self.scenario_id = scenario_id
        # planner course key -> its budget (``course_code``, ``programme_term``)
        self._budgets: dict[str, Any] | None = dict(budgets) if budgets is not None else None
        self._term_boards: dict[int, list[list[str]]] | None = None
        self._lookup: Any = None
        self._course_online: dict[str, bool | None] = {}

    def board_flags(self, course_code: str, programme_term: int | None) -> list[bool]:
        """Whether ``course_code`` is online on each board of ``programme_term``.

        A term without a board (FE2 at term 10, say) is placed by the greedy's
        cross-term step (1b) on a board of another term, so every board of the
        scenario is asked for it. Known residual: a budget whose term HAS boards
        but which step 1b places on another term's board is judged by its own
        term's boards (the placer adds the board it is placing on, see
        :func:`section_online_exempt`)."""
        from core.services.timetable_online import normalise_course_code

        if self._term_boards is None:
            from core.models import DeliveryBoard
            from core.services.timetable_online import programmes_for_board

            self._term_boards = {}
            for board in DeliveryBoard.objects.filter(scenario_id=self.scenario_id).order_by(
                "display_order", "pk"
            ):
                if board.nominal_term is not None:
                    self._term_boards.setdefault(board.nominal_term, []).append(
                        programmes_for_board(board)
                    )
        lookup = self.lookup()
        own = self._term_boards.get(programme_term) if programme_term is not None else None
        boards = own or [
            programmes for term_boards in self._term_boards.values() for programmes in term_boards
        ]
        code = normalise_course_code(course_code)
        return [code in lookup.codes_for_programmes(programmes) for programmes in boards]

    def lookup(self) -> Any:
        """The one ``OnlineCourseLookup`` (cached per programme list) of this object."""
        if self._lookup is None:
            from core.services.timetable_online import OnlineCourseLookup

            self._lookup = OnlineCourseLookup()
        return self._lookup

    def course_online_everywhere(self, course_key: str) -> bool | None:
        """The budgeted course ``course_key`` (planner key) is online on every
        board of its term; None when it has no budget."""
        if course_key not in self._course_online:
            budget = self._budget(course_key)
            self._course_online[course_key] = (
                None
                if budget is None
                else online_everywhere(self.board_flags(budget.course_code, budget.programme_term))
            )
        return self._course_online[course_key]

    def _budget(self, course_key: str) -> Any:
        if self._budgets is None:
            from core.models import ScenarioSectionBudget

            self._budgets = {
                (course_key or code): SimpleNamespace(course_code=code, programme_term=term)
                for course_key, code, term in ScenarioSectionBudget.objects.filter(
                    scenario_id=self.scenario_id
                )
                .order_by("pk")
                .values_list("course_key", "course_code", "programme_term")
            }
        return self._budgets.get(course_key)

    def exempt(self, course_key: str, placed: Iterable[bool] | None) -> bool:
        """:func:`section_online_exempt` of a section of ``course_key`` sitting on
        boards with these online ``placed`` flags."""
        return section_online_exempt(placed, self.course_online_everywhere(course_key))

    def placed_rows(self) -> list[tuple[int, tuple[str, str], tuple[bool, ...]]]:
        """``(term_section_id, (course_key, label), online flags)`` of every placed
        section: one flag per distinct board programme list, judged like every
        other online consumer (``OnlineCourseLookup``, per board). One query."""
        from core.models import SectionPlacement
        from core.services.course_identity import course_display_code

        lookup = self.lookup()
        flags: dict[int, set[bool]] = {}
        keys: dict[int, tuple[str, str]] = {}
        for ts_id, course_key, code, number, label, program in (
            SectionPlacement.objects.filter(board__scenario_id=self.scenario_id)
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
                lookup.is_online_course_for_board(
                    board, course_display_code(course_key, code, number)
                )
            )
        return [(ts_id, keys[ts_id], tuple(sorted(flags[ts_id]))) for ts_id in sorted(flags)]

    def placed_by_section(self) -> dict[tuple[str, str], tuple[bool, ...]]:
        """:meth:`placed_rows` by planner key ``(course_key, label)``."""
        out: dict[tuple[str, str], set[bool]] = {}
        for _ts_id, key, flags in self.placed_rows():
            out.setdefault(key, set()).update(flags)
        return {key: tuple(sorted(flags)) for key, flags in out.items()}

    def exempt_sections(self) -> frozenset[tuple[str, str]]:
        """``(course_key, label)`` of the PLACED sections the rules do not bind."""
        return frozenset(
            key for _ts_id, key, flags in self.placed_rows() if self.exempt(key[0], flags)
        )


def online_exemption(scenario_id: int) -> OnlineExemption:
    """The scenario's :class:`OnlineExemption`: the scope's one object (its static
    half read once per run, job or request), or a fresh one outside any scope.
    Building it costs no query."""
    holder = _holder(scenario_id)
    if holder is None:
        return OnlineExemption(scenario_id)
    if holder.online is None:
        holder.online = OnlineExemption(scenario_id)
    return holder.online


def placed_online_flags(scenario_id: int) -> dict[int, tuple[bool, ...]]:
    """``term_section_id -> (online on board, ...)`` for every placed section."""
    return {ts_id: flags for ts_id, _key, flags in online_exemption(scenario_id).placed_rows()}


def placed_online_by_section(scenario_id: int) -> dict[tuple[str, str], tuple[bool, ...]]:
    """``(course_key, label) -> (online on board, ...)`` for every placed section."""
    return online_exemption(scenario_id).placed_by_section()


def online_exempt_sections(scenario_id: int) -> frozenset[tuple[str, str]]:
    """``(course_key, label)`` of the placed sections the rules do not bind
    (:func:`section_online_exempt`; ``course_key`` is the planner identity,
    ``course_key or course_code``): what the annealing and load-balancer prunes,
    the persist guard, the recovery runner and :class:`RuleChecker` skip."""
    return online_exemption(scenario_id).exempt_sections()


__all__ = [
    "availability_for",
    "availability_scope",
    "bound_availability",
    "cell_predicate",
    "clear_released",
    "display_rule_checker",
    "display_scope",
    "fixed_section_keys",
    "meetings_blocked",
    "online_everywhere",
    "online_exempt_sections",
    "online_exemption",
    "OnlineExemption",
    "option_blocked",
    "option_meetings",
    "placed_online_by_section",
    "placed_online_flags",
    "prune_relocate_domains",
    "record_release",
    "released_sections",
    "RuleChecker",
    "rule_ids",
    "rules_enabled",
    "section_online_exempt",
    "section_rule_checker",
    "teaching_rules_request",
]

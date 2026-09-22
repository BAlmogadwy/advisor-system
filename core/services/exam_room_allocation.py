"""Deterministic, capacity-safe rooming of original exam teaching sections.

Exam times are inputs, never decision variables. Whole sections across every
course get first choice of the smallest fitting room. Consolidation then frees
rooms without displacing other courses. Difficult periods receive a bounded
joint search before any section is divided; every search retains a validated
incumbent. A search limit is not a proof that a split is unavoidable.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from threading import RLock

from ortools.sat.python import cp_model

logger = logging.getLogger(__name__)

ROOM_ALLOCATION_POLICY_VERSION = 1
# Sized for several concurrent requests, not one. A single exam Optimise that
# runs the invigilator post-pass leaves ~260 entries (2.41 MB pickled), so at
# 512 two concurrent registrars evicted each other's periods mid-flight and
# paid for it in fresh CP-SAT solves against a ticking deadline.
_CACHE_SIZE = 2048
_CACHE: OrderedDict[str, list[dict]] = OrderedDict()
_CACHE_LOCK = RLock()
_PHASE_DETERMINISTIC_LIMIT = 0.06
# A wall deadline is a safety valve, never a work limit. For the rooming itself
# that makes it invariant: an allocation is fixed by the per-phase deterministic
# limit above, a wall stop raises rather than returning a partial result, so a
# longer deadline changes only *whether* a rooming is produced, never *which*
# one. It is NOT invariant for the build as a whole — the optional invigilator
# pass in exam_timetable.py accepts improving day moves until this deadline cuts
# it off, so a longer budget lets it finish moves it was already making and the
# published placements can differ from a truncated run.
#
# The budget must cover the slowest supported host and grow with the problem: a
# fifteen-day, three-period, twelve-programme exam build solves ~90 period/cohort
# allocations where a one-day build solves two. A flat six seconds covered
# neither. Measured cold-cache on that build on a developer workstation: ~3.3s
# for the mandatory pack, ~7.8s for the invigilator pass — so the flat budget was
# already truncating the optimisation on the fastest host there is, and a 0.5-CPU
# production instance rejected real builds outright.
_BASE_SEARCH_SECONDS = 10.0
_PERIOD_SEARCH_SECONDS = 0.5
_MAX_SEARCH_SECONDS = 60.0


def _setting(name: str, default: float) -> float:
    """Read a Django override without making this module require a settings module."""
    try:
        from django.conf import settings

        value = getattr(settings, name, None)
    except Exception:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return float(value)


def search_budget_seconds(period_cohorts: int = 0) -> float:
    """Wall budget for one rooming phase, sized by the periods it must solve."""
    base = _setting("EXAM_ROOM_BASE_SEARCH_SECONDS", _BASE_SEARCH_SECONDS)
    per_period = _setting("EXAM_ROOM_PERIOD_SEARCH_SECONDS", _PERIOD_SEARCH_SECONDS)
    ceiling = _setting("EXAM_ROOM_MAX_SEARCH_SECONDS", _MAX_SEARCH_SECONDS)
    try:
        periods = max(0, int(period_cohorts))
    except (TypeError, ValueError):
        periods = 0
    # Clamp each term: a negative override must not shrink the budget as the
    # problem grows, and the floor keeps a misconfigured ceiling from arming a
    # deadline that is already spent.
    sized = max(0.0, base) + max(0.0, per_period) * periods
    return max(1.0, min(max(1.0, ceiling), sized))


TIMEOUT_MESSAGE = "Room checking reached its time limit. Your timetable has not been saved; please retry the check."


class RoomAllocationTimeout(ValueError):
    """A wall deadline interrupted deterministic repair; no partial result is saved."""


@dataclass
class RoomAllocationContext:
    """One shared wall deadline across a build and its room rebalance trials."""

    deadline: float = field(default_factory=lambda: time.monotonic() + search_budget_seconds())
    #: The wall budget this deadline was armed with, for diagnostics only.
    budget_seconds: float = field(default_factory=search_budget_seconds)
    cache_hits: int = 0
    periods_solved: int = 0
    searches: int = 0
    limited_searches: int = 0
    #: Set by an optional, best-effort pass that absorbed the timeout instead of
    #: failing the build, so callers can report the optimisation as truncated.
    truncated: bool = False

    @classmethod
    def for_periods(cls, period_cohorts: int) -> RoomAllocationContext:
        """Arm a deadline sized for a rooming phase of ``period_cohorts`` allocations."""
        budget = search_budget_seconds(period_cohorts)
        return cls(deadline=time.monotonic() + budget, budget_seconds=budget)

    def expired(self) -> RoomAllocationTimeout:
        """Report an exhausted phase, then hand back the error to raise."""
        logger.warning(
            "exam room allocation exhausted its %.1fs wall budget "
            "(periods_solved=%s cache_hits=%s searches=%s limited_searches=%s)",
            self.budget_seconds,
            self.periods_solved,
            self.cache_hits,
            self.searches,
            self.limited_searches,
        )
        return RoomAllocationTimeout(TIMEOUT_MESSAGE)

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise self.expired()
        return remaining


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def normalized_rooms(rooms: list[dict]) -> list[dict]:
    """A physical room code may not represent two independent exam rooms."""
    by_code: dict[str, dict] = {}
    for raw in sorted(rooms, key=_json):
        code = str(raw.get("room_code", "")).strip()
        gender = str(raw.get("section", "M") or "M").upper()
        capacity = int(raw.get("capacity", 0) or 0)
        if not code or code == "UNASSIGNED" or capacity <= 0 or gender not in {"M", "F"}:
            continue
        room = {**raw, "room_code": code, "section": gender, "capacity": capacity}
        if code in by_code and any(by_code[code][k] != room[k] for k in ("section", "capacity")):
            raise ValueError(
                f"Room {code} has conflicting capacities or student cohorts. Correct the room inventory before allocating exams."
            )
        by_code.setdefault(code, room)
    return sorted(by_code.values(), key=lambda r: (r["capacity"], r["room_code"]))


def _score(allocation: list[dict[int, int]], demands: list[dict], rooms: list[dict]) -> tuple:
    missing = [
        d["student_count"] - sum(a.values()) for d, a in zip(demands, allocation, strict=True)
    ]
    fragments = [len(a) + bool(m) for a, m in zip(allocation, missing, strict=True)]
    used = {r for a in allocation for r in a}
    return (
        sum(missing),
        sum(n > 1 for n in fragments),
        sum(max(0, n - 1) for n in fragments),
        len(used),
        sum(rooms[r]["capacity"] for r in used),
        sum(
            n
            for d, a in zip(demands, allocation, strict=True)
            for r, n in a.items()
            if d.get("preferred_room") and rooms[r]["room_code"] != d["preferred_room"]
        ),
    )


def _valid(allocation: list[dict[int, int]], demands: list[dict], rooms: list[dict]) -> bool:
    if len(allocation) != len(demands):
        return False
    occupants: dict[int, set[str]] = defaultdict(set)
    totals: dict[int, int] = defaultdict(int)
    for demand, assignments in zip(demands, allocation, strict=True):
        if sum(assignments.values()) > demand["student_count"]:
            return False
        for room, count in assignments.items():
            if room < 0 or room >= len(rooms) or count <= 0:
                return False
            occupants[room].add(demand["course_code"])
            totals[room] += count
    return all(
        len(occupants[r]) == 1 and count <= rooms[r]["capacity"] for r, count in totals.items()
    )


def _whole_first(demands: list[dict], rooms: list[dict]) -> list[dict[int, int]]:
    assignments: list[dict[int, int]] = [{} for _ in demands]
    free = set(range(len(rooms)))
    # Original sections, not course totals or pre-split fragments.
    for i in sorted(range(len(demands)), key=lambda i: (-demands[i]["student_count"], i)):
        demand = demands[i]
        candidates = [r for r in free if rooms[r]["capacity"] >= demand["student_count"]]
        if candidates:
            r = min(
                candidates,
                key=lambda r: (
                    rooms[r]["capacity"],
                    rooms[r]["room_code"] != demand.get("preferred_room"),
                    rooms[r]["room_code"],
                ),
            )
            assignments[i][r] = demand["student_count"]
            free.remove(r)
    return assignments


def _consolidate(
    allocation: list[dict[int, int]], demands: list[dict], rooms: list[dict]
) -> list[dict[int, int]]:
    """Whole-section local merges; never take a room occupied by another course."""
    best = copy.deepcopy(allocation)
    by_course: dict[str, list[int]] = defaultdict(list)
    for i, demand in enumerate(demands):
        by_course[demand["course_code"]].append(i)
    while True:
        changed = False
        owner = {r: demands[i]["course_code"] for i, a in enumerate(best) for r in a}
        for course, indices in sorted(by_course.items()):
            for target, room in enumerate(rooms):
                if target in owner and owner[target] != course:
                    continue
                # Move complete occupied groups only: a merge cannot leave a
                # displaced part behind or increase an original section's split.
                groups: dict[tuple, list[int]] = defaultdict(list)
                for i in indices:
                    if len(best[i]) <= 1 and sum(best[i].values()) in (
                        0,
                        demands[i]["student_count"],
                    ):
                        groups[("room", next(iter(best[i]))) if best[i] else ("missing", i)].append(
                            i
                        )
                packed: list[int] = []
                capacity = room["capacity"]
                for group in sorted(
                    groups.values(),
                    key=lambda g: (
                        not any(target in best[i] for i in g),
                        -sum(demands[i]["student_count"] for i in g),
                        g,
                    ),
                ):
                    size = sum(demands[i]["student_count"] for i in group)
                    if size <= capacity:
                        packed.extend(group)
                        capacity -= size
                if not packed:
                    continue
                # A target already in use must retain all of its occupants.
                if any(target in best[i] and i not in packed for i in indices):
                    continue
                candidate = copy.deepcopy(best)
                for i in packed:
                    candidate[i] = {target: demands[i]["student_count"]}
                if _score(candidate, demands, rooms) < _score(best, demands, rooms):
                    best = candidate
                    changed = True
                    break
            if changed:
                break
        if not changed:
            return best


def _split_remaining(
    allocation: list[dict[int, int]], demands: list[dict], rooms: list[dict]
) -> list[dict[int, int]]:
    """Valid fallback after whole-section repair; use actual remaining capacities."""
    result = copy.deepcopy(allocation)
    owner = {r: demands[i]["course_code"] for i, a in enumerate(result) for r in a}
    spare = {r: room["capacity"] - sum(a.get(r, 0) for a in result) for r, room in enumerate(rooms)}
    for i in sorted(range(len(demands)), key=lambda i: (-demands[i]["student_count"], i)):
        left = demands[i]["student_count"] - sum(result[i].values())
        while left:
            candidates = [
                r
                for r in spare
                if spare[r] > 0
                and owner.get(r, demands[i]["course_code"]) == demands[i]["course_code"]
            ]
            if not candidates:
                break
            fitting = [r for r in candidates if spare[r] >= left]
            r = (
                min(fitting, key=lambda r: (spare[r], rooms[r]["room_code"]))
                if fitting
                else max(candidates, key=lambda r: (spare[r], -r))
            )
            count = min(left, spare[r])
            result[i][r] = result[i].get(r, 0) + count
            owner[r] = demands[i]["course_code"]
            spare[r] -= count
            left -= count
    return result


def _repair(
    incumbent: list[dict[int, int]],
    demands: list[dict],
    rooms: list[dict],
    *,
    split: bool,
    context: RoomAllocationContext,
) -> list[dict[int, int]]:
    """Joint CP-SAT repair with hard room constraints and lexicographic priorities."""
    if not rooms:
        return incumbent
    context.remaining()
    model = cp_model.CpModel()
    amounts, present, missing, owners = {}, {}, [], {}
    courses = sorted({d["course_code"] for d in demands})
    for course in courses:
        context.remaining()
        for r in range(len(rooms)):
            owners[course, r] = model.new_bool_var(f"owner_{course}_{r}")
    for r in range(len(rooms)):
        model.add(sum(owners[c, r] for c in courses) <= 1)
    fragment_counts, split_flags = [], []
    for i, demand in enumerate(demands):
        context.remaining()
        count = demand["student_count"]
        for r, room in enumerate(rooms):
            if not split and room["capacity"] < count:
                continue
            p = model.new_bool_var(f"part_{i}_{r}")
            present[i, r] = p
            model.add(p <= owners[demand["course_code"], r])
            if split:
                x = model.new_int_var(0, min(count, room["capacity"]), f"students_{i}_{r}")
                model.add(x >= p)
                model.add(x <= min(count, room["capacity"]) * p)
            else:
                x = count * p
            amounts[i, r] = x
        u = model.new_int_var(0, count, f"unseated_{i}")
        model.add(sum(x for (j, _), x in amounts.items() if j == i) + u == count)
        missing.append(u)
        unseated = model.new_bool_var(f"has_unseated_{i}")
        model.add(u >= unseated)
        model.add(u <= count * unseated)
        n = sum(p for (j, _), p in present.items() if j == i) + unseated
        if not split:
            model.add(n == 1)
        flag = model.new_bool_var(f"split_{i}")
        model.add(n <= 1 + len(rooms) * flag)
        model.add(n >= 1 + flag)
        fragment_counts.append(n - 1)
        split_flags.append(flag)
    for r, room in enumerate(rooms):
        context.remaining()
        model.add(sum(x for (_, k), x in amounts.items() if k == r) <= room["capacity"])
        for course in courses:
            model.add(
                owners[course, r]
                <= sum(
                    p
                    for (i, k), p in present.items()
                    if k == r and demands[i]["course_code"] == course
                )
            )
    used = sum(owners.values())
    used_capacity = sum(rooms[r]["capacity"] * owner for (_, r), owner in owners.items())
    # Seats first, then original sections kept intact, then extra fragments,
    # then room use. Lower priorities can never trade away a higher one.
    objectives = [sum(missing)]
    if split:
        objectives.append(sum(split_flags) * (len(demands) * len(rooms) + 1) + sum(fragment_counts))
    objectives.append(used * (sum(r["capacity"] for r in rooms) + 1) + used_capacity)
    best = incumbent
    for (i, r), p in present.items():
        model.add_hint(p, int(r in incumbent[i]))
        if split:
            model.add_hint(amounts[i, r], incumbent[i].get(r, 0))
    for objective in objectives:
        remaining = context.remaining()
        model.minimize(objective)
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = 0
        solver.parameters.max_deterministic_time = _PHASE_DETERMINISTIC_LIMIT
        solver.parameters.max_time_in_seconds = remaining
        context.searches += 1
        status = solver.solve(model)
        # Only the deterministic work limit may return a bounded incumbent.
        # Wall-dependent partial results would make Check change with server
        # load and could poison the shared period cache.
        context.remaining()
        if status in (cp_model.MODEL_INVALID, cp_model.INFEASIBLE):
            # Leaving every section unseated is feasible by construction.
            raise RuntimeError("Exam room repair produced an invalid model.")
        if (
            status != cp_model.OPTIMAL
            and solver.response_proto.deterministic_time + 1e-9 < _PHASE_DETERMINISTIC_LIMIT
        ):
            # CP-SAT can stop just *before* its wall limit. In particular,
            # Windows clock resolution may still leave remaining() positive.
            raise context.expired()
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            context.limited_searches += 1
            break
        candidate = [
            {
                r: int(solver.value(x))
                for (j, r), x in amounts.items()
                if j == i and solver.value(x) > 0
            }
            for i in range(len(demands))
        ]
        if _valid(candidate, demands, rooms) and _score(candidate, demands, rooms) < _score(
            best, demands, rooms
        ):
            best = candidate
        if status != cp_model.OPTIMAL:
            context.limited_searches += 1
            break
        model.add(objective == int(solver.value(objective)))
        model.clear_hints()
        for (i, r), p in present.items():
            model.add_hint(p, int(r in candidate[i]))
            if split:
                model.add_hint(amounts[i, r], candidate[i].get(r, 0))
    return best


def allocate_period(
    demands: list[dict], rooms: list[dict], context: RoomAllocationContext
) -> list[dict]:
    """Allocate one period/cohort. Cache only authoritative content, never client rooms.

    Course keys are canonical scheduling identities. Section membership hashes,
    metadata, inventory and policy all participate in the key. Moving an exam
    changes exactly its source/destination demand sets; unaffected periods reuse
    deep-copied results. Different periods may independently reuse the same room.
    """
    demands = sorted(
        (d for d in demands if int(d.get("student_count", 0) or 0) > 0),
        key=lambda d: (d["course_code"], str(d.get("section_key", d["section"])), _json(d)),
    )
    key = _json([ROOM_ALLOCATION_POLICY_VERSION, demands, rooms])
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            context.cache_hits += 1
            return copy.deepcopy(_CACHE[key])
    context.periods_solved += 1
    allocation = _consolidate(_whole_first(demands, rooms), demands, rooms)
    if _score(allocation, demands, rooms)[0]:
        allocation = _repair(allocation, demands, rooms, split=False, context=context)
    if _score(allocation, demands, rooms)[0]:
        allocation = _split_remaining(allocation, demands, rooms)
        allocation = _repair(allocation, demands, rooms, split=True, context=context)
    if not _valid(allocation, demands, rooms):
        raise RuntimeError("Exam room allocation failed its capacity and identity validation.")
    rows = []
    for i, demand in enumerate(demands):
        missing = demand["student_count"] - sum(allocation[i].values())
        if "section_key" not in demand and len(allocation[i]) + bool(missing) > 1:
            # Keep the existing diagnostic contract for programmatic callers
            # without a real section key; official labels remain untouched.
            demand = {**demand, "_split_from": demand["section"]}
        for r, count in sorted(allocation[i].items()):
            rows.append(
                {
                    **demand,
                    "student_count": count,
                    "room_code": rooms[r]["room_code"],
                    "room_capacity": rooms[r]["capacity"],
                }
            )
        if missing:
            rows.append(
                {**demand, "student_count": missing, "room_code": "UNASSIGNED", "room_capacity": 0}
            )
    with _CACHE_LOCK:
        _CACHE[key] = copy.deepcopy(rows)
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    return rows

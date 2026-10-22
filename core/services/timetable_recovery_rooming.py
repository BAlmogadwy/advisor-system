"""Snapshot room feasibility for repeated secondary-recovery candidates.

The factory reads the scenario once. Candidate evaluation is pure: it uses the
existing exact per-meeting solver with hard buffered-capacity domains, reserves
externally owned rooms, and returns the exact assignment that persistence must
apply. A user lock freezes a section's meeting times, never its room.
"""

from __future__ import annotations

import gc
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from core.models import ScenarioSectionBudget, SectionPlacement
from core.services.timetable_assignment_models import SectionState
from core.services.timetable_board_persistence import external_shared_section_ids
from core.services.timetable_exact_rooming import (
    ExactRoomingError,
    MeetingDemand,
    _load_room_inventory,
    _solve_relaxed,
    build_meeting_demands,
)
from core.services.timetable_room_audit import AUDIT_DAYS, actual_rooms, meeting_room_verdict
from core.services.timetable_rooming import (
    _budget_value_for_placement,
    _build_rooming_budget_maps,
    _room_type_for_credit_duration,
    get_capacity_buffer,
)

_DAYS = AUDIT_DAYS


def _overlap(left: MeetingDemand, right: MeetingDemand) -> bool:
    return (
        left.day == right.day and left.start_min < right.end_min and right.start_min < left.end_min
    )


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class RecoveryRoomEvaluator:
    """In-memory room oracle. Room demand and modality come from the snapshot.

    ``frozen_section_ids`` freeze meeting times only. ``locked_rooms_by_placement``
    pins rooms owned by another scenario (and freezes their times too); an
    invalid pinned room is retained in the output and reported as a shortage,
    rather than silently repaired.
    """

    def __init__(
        self,
        demands: Iterable[MeetingDemand],
        rooms: Mapping[str, dict],
        section_ids: Mapping[int, str],
        credits: Mapping[int, int],
        locked_rooms_by_placement: Mapping[int, str] | None = None,
        *,
        time_limit_seconds: float = 0.15,
        frozen_section_ids: Iterable[str] = (),
        count_only: bool = True,
    ) -> None:
        self.demands = tuple(demands)
        self.rooms = {code: dict(room) for code, room in rooms.items()}
        self.capacities = {code: int(room["capacity"]) for code, room in self.rooms.items()}
        self.section_ids = dict(section_ids)
        self.credits = dict(credits)
        self.locked = dict(locked_rooms_by_placement or {})
        self.time_limit_seconds = max(0.0, float(time_limit_seconds))
        self.count_only = count_only
        self.by_section: dict[str, list[MeetingDemand]] = defaultdict(list)
        for demand in self.demands:
            if demand.day not in _DAYS:
                raise ExactRoomingError(f"Unknown physical meeting day {demand.day!r}.")
            self.by_section[self.section_ids[demand.term_section_id]].append(demand)
        self.locked_sections = set(frozen_section_ids) | {
            self.section_ids[d.term_section_id]
            for d in self.demands
            if any(pid in self.locked for pid in d.placement_ids)
        }
        self._cache: dict[tuple, dict[str, Any]] = {}
        self.original_room_report = self._original_room_report()

    def _original_room_report(self) -> dict[str, Any]:
        """Audit persisted rooms against exactly the same strict domains."""
        missing: set[str] = set()
        incompatible: set[str] = set()
        undersized: set[str] = set()
        unsuitable: set[str] = set()
        conflicts: set[str] = set()
        flagged = {
            "missing": missing,
            "undersized": undersized,
            "unsuitable": unsuitable,
            "incompatible": incompatible,
        }
        rooms_by_demand = {}
        for d in self.demands:
            rooms_by_demand[d.demand_id] = actual_rooms(d.incumbent_rooms)
            # The audit's one verdict (timetable_room_audit); "split" is reported
            # as unsuitable and incompatible.
            verdict = meeting_room_verdict(
                d.incumbent_rooms,
                required_capacity=d.required_capacity,
                compatible_rooms=d.compatible_rooms,
                capacities=self.capacities,
            )
            for name, demand_ids in flagged.items():
                if name in verdict:
                    demand_ids.add(d.demand_id)
        for i, left in enumerate(self.demands):
            for right in self.demands[i + 1 :]:
                if _overlap(left, right) and (
                    rooms_by_demand[left.demand_id] & rooms_by_demand[right.demand_id]
                ):
                    conflicts.update((left.demand_id, right.demand_id))
        return {
            "original_room_shortages": len(missing | incompatible | conflicts),
            "original_unassigned_count": len(missing),
            "original_incompatible_rooms": len(incompatible),
            "original_room_conflict_meetings": len(conflicts),
            "original_unassigned_demand_ids": sorted(missing),
            "original_incompatible_demand_ids": sorted(incompatible),
            "original_room_conflict_demand_ids": sorted(conflicts),
            "original_undersized_demand_ids": sorted(undersized),
            "original_unsuitable_demand_ids": sorted(unsuitable),
        }

    def _candidate_demands(
        self, sections: Iterable[SectionState] | Mapping[str, SectionState] | None
    ) -> list[MeetingDemand]:
        if sections is None:
            return list(self.demands)
        states = sections if isinstance(sections, Mapping) else {s.section_id: s for s in sections}
        output = []
        for sid, original in self.by_section.items():
            if sid not in states:
                raise ExactRoomingError(f"Recovery candidate is missing physical section {sid}.")
            # Shared-across-board placement rows produce duplicate SectionMeetings
            # in the classic adapter; they remain one physical demand here.
            candidate = sorted({(m.day, m.start_min, m.end_min) for m in states[sid].meetings})
            source = sorted({(_DAYS.index(d.day), d.start_min, d.end_min) for d in original})
            if Counter(e - s for _, s, e in candidate) != Counter(e - s for _, s, e in source):
                raise ExactRoomingError(f"Recovery candidate changed contact durations for {sid}.")
            if sid in self.locked_sections and candidate != source:
                raise ExactRoomingError(f"Recovery candidate moved locked section {sid}.")
            remaining = list(candidate)
            mapping = {}
            # Keep unchanged meetings' row identity before pairing relocated
            # equal-duration meetings in deterministic chronological order.
            for d in original:
                old = (_DAYS.index(d.day), d.start_min, d.end_min)
                if old in remaining:
                    mapping[d.demand_id] = old
                    remaining.remove(old)
            for d in sorted(original, key=lambda d: (d.day, d.start_min, d.end_min)):
                if d.demand_id not in mapping:
                    match = next(
                        (m for m in remaining if m[2] - m[1] == d.end_min - d.start_min), None
                    )
                    if match is None:
                        raise ExactRoomingError(f"Cannot map candidate meeting for {sid}.")
                    mapping[d.demand_id] = match
                    remaining.remove(match)
                day, start, end = mapping[d.demand_id]
                required_type = _room_type_for_credit_duration(
                    self.credits[d.term_section_id], end - start
                )
                output.append(
                    replace(
                        d, day=_DAYS[day], start_min=start, end_min=end, required_type=required_type
                    )
                )
        return output

    def __call__(
        self, sections: Iterable[SectionState] | Mapping[str, SectionState] | None
    ) -> dict[str, Any]:
        demands = self._candidate_demands(sections)
        key = tuple((d.demand_id, d.day, d.start_min, d.end_min) for d in demands)
        if key in self._cache:
            # Callers may attach diagnostics or persist mappings; do not expose
            # the cache's mutable objects across evaluations.
            from copy import deepcopy

            return deepcopy(self._cache[key])

        fixed = []
        free = []
        frozen_assignment = {}
        invalid = {}
        for d in demands:
            locked_rows = [pid for pid in d.placement_ids if pid in self.locked]
            strict_rooms = tuple(
                r for r in d.compatible_rooms if self.capacities[r] >= d.required_capacity
            )
            if not locked_rows:
                free.append(replace(d, compatible_rooms=strict_rooms))
                continue
            codes = {str(self.locked[pid] or "").strip().upper() for pid in locked_rows}
            if len(codes) != 1:
                raise ExactRoomingError(f"Conflicting locked rooms for {d.demand_id}.")
            code = next(iter(codes))
            frozen_assignment[d.demand_id] = code
            fixed.append((d, code))
            if code not in strict_rooms:
                invalid[d.demand_id] = "LOCKED_ROOM_INVALID" if code else "LOCKED_UNASSIGNED"

        # Even an invalid/unknown locked room occupies its original identity.
        for i, (left, room) in enumerate(fixed):
            if room and room != "UNASSIGNED":
                for right, other in fixed[i + 1 :]:
                    if room == other and _overlap(left, right):
                        invalid[left.demand_id] = "LOCKED_ROOM_CONFLICT"
                        invalid[right.demand_id] = "LOCKED_ROOM_CONFLICT"
        constrained = [
            replace(
                d,
                compatible_rooms=tuple(
                    r
                    for r in d.compatible_rooms
                    if not any(r == code and _overlap(d, locked_d) for locked_d, code in fixed)
                ),
            )
            for d in free
        ]
        # A user lock lets a meeting change room. The unroomed count is still
        # minimised exactly, but on a tie a locked meeting already in a valid
        # room keeps a room rather than losing it to an unlocked one.
        solve = _solve_relaxed(
            constrained,
            self.capacities,
            time_limit_seconds=self.time_limit_seconds,
            strict_budget=True,
            count_only=self.count_only,
            keep_roomed={
                d.demand_id
                for d in constrained
                if self.section_ids[d.term_section_id] in self.locked_sections
                and d.incumbent_room in d.compatible_rooms
            },
        )
        # Each solve leaves a dead CP-SAT model in reference cycles. Recovery runs
        # one per candidate, and until the cyclic collector gets to them they piled
        # up ~120 MB on the 512 MB host; a young-generation pass (~3 ms) holds it ~20.
        gc.collect(1)
        assignments = {**solve.assignment, **frozen_assignment}
        missing = set(solve.unroomed) | set(invalid)
        failures: list[dict[str, Any]] = []
        recoverable: set[str] = set()
        source_by_id = {d.demand_id: d for d in demands}
        for did in sorted(missing):
            d = source_by_id[did]
            best = max((self.capacities[r] for r in d.compatible_rooms), default=0)
            reason = invalid.get(did) or (
                "NO_COMPATIBLE_ROOM"
                if not d.compatible_rooms
                else "NO_ROOM_CAPACITY"
                if best < d.required_capacity
                else "ROOM_OCCUPIED"
            )
            if reason == "ROOM_OCCUPIED":
                recoverable.add(self.section_ids[d.term_section_id])
                eligible = {
                    r for r in d.compatible_rooms if self.capacities[r] >= d.required_capacity
                }
                for neighbour in demands:
                    if _overlap(d, neighbour) and any(
                        r in eligible and self.capacities[r] >= neighbour.required_capacity
                        for r in neighbour.compatible_rooms
                    ):
                        recoverable.add(self.section_ids[neighbour.term_section_id])
            failures.append(
                {
                    "demand_id": did,
                    "section_id": self.section_ids[d.term_section_id],
                    "course_code": d.course_code,
                    "placement_ids": d.placement_ids,
                    "reason": reason,
                    "required_capacity": d.required_capacity,
                    "best_capacity": best,
                    "required_type": d.required_type,
                }
            )
        unavoidable = len(invalid) + sum(not d.compatible_rooms for d in free)
        result = {
            **self.original_room_report,
            "unassigned_count": len(missing),
            "physical_meetings": len(demands),
            "unassigned_section_ids": sorted({f["section_id"] for f in failures}),
            "recoverable_section_ids": sorted(recoverable),
            "assignment_by_placement_id": {
                pid: (self.locked[pid] if pid in self.locked else assignments.get(d.demand_id, ""))
                for d in demands
                for pid in d.placement_ids
            },
            "candidate_placements_by_id": {
                pid: (d.day, _hhmm(d.start_min), _hhmm(d.end_min))
                for d in demands
                for pid in d.placement_ids
            },
            "failures": failures,
            "status": solve.status,
            "proven_optimal": solve.proven_optimal,
            "count_proven_optimal": solve.unroomed_proven_optimal,
            "shortage_lower_bound": len(missing) if solve.unroomed_proven_optimal else unavoidable,
            "wall_time_seconds": solve.wall_time_seconds,
        }
        from copy import deepcopy

        # Recovery searches are bounded, but a small cache cap also prevents
        # arbitrary external callers retaining every candidate indefinitely.
        if len(self._cache) >= 256:
            self._cache.clear()
        self._cache[key] = deepcopy(result)
        return result


def build_recovery_room_evaluator(
    scenario_id: int,
    sections: Iterable[SectionState] | Mapping[str, SectionState] | None = None,
    *,
    time_limit_seconds: float = 0.15,
    count_only: bool = True,
) -> RecoveryRoomEvaluator:
    """Read one authoritative inventory/demand snapshot; never write the DB.

    ``sections`` is optional and accepted for classic optimiser integration;
    requirements are deliberately derived from the persisted scenario budgets.
    """
    rooms, _ = _load_room_inventory()
    demands = build_meeting_demands(scenario_id, rooms)
    placements = list(
        SectionPlacement.objects.filter(board__scenario_id=scenario_id).select_related(
            "term_section"
        )
    )
    maps = _build_rooming_budget_maps(
        list(ScenarioSectionBudget.objects.filter(scenario_id=scenario_id)), get_capacity_buffer()
    )
    section_ids = {}
    credits = {}
    locked = {}
    frozen_sections = set()
    external_ids = external_shared_section_ids(scenario_id)
    for p in placements:
        ts = p.term_section
        section_ids[ts.id] = f"{ts.course_key or ts.course_code}_{ts.section}"
        credits[ts.id] = _budget_value_for_placement(p, maps, "credit", 3)
        # A user lock protects the section's times; its room stays assignable.
        # Another scenario owns an external section, so its room is pinned too.
        if p.term_section_id in external_ids:
            locked[p.id] = p.room or ""
        if p.is_locked or p.term_section_id in external_ids:
            frozen_sections.add(section_ids[ts.id])
    return RecoveryRoomEvaluator(
        demands,
        rooms,
        section_ids,
        credits,
        locked,
        time_limit_seconds=time_limit_seconds,
        frozen_section_ids=frozen_sections,
        count_only=count_only,
    )

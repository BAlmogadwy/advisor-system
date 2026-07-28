"""The immutable, fingerprinted input to every solve — pure Python, no Django.

This is non-negotiable N3: the solver sees **only** a `Snapshot`. It never
touches the ORM, never reads the live database, and never uses production tables
as scratch space (the current engine performs ~8 full scenario builds against
live tables per rebuild, keeping only a strategy *name*).

Three properties follow directly, and each is something the old engine lacks:

* **reproducible** — same snapshot + same config + same seed produces the same
  board, because there is no hidden state to drift;
* **testable** — a solver test needs no database at all;
* **comparable** — a run is stamped with the fingerprint of what produced it, so
  two results can be honestly compared or explicitly refused as incomparable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .calendar import Grid, MeetingKind
from .entities import (
    CapacityPolicy,
    DemandIndex,
    Instructor,
    Offering,
    OfferingId,
    Room,
    Section,
    StudentDemand,
)


@dataclass(frozen=True)
class Snapshot:
    """Everything needed to build one timetable, frozen at a point in time.

    A snapshot is **single-gender** (owner decision D1): rooms, students and
    instructors are filtered once at intake, so gender never appears as a
    constraint the solver has to reason about. A room is either in the pool or it
    does not exist.
    """

    # identity of the run
    academic_year: str
    term: int
    gender: str  # "M" | "F" — a property of the whole snapshot
    programs: tuple[str, ...]

    # the problem
    grid: Grid
    offerings: tuple[Offering, ...]
    sections: tuple[Section, ...]
    rooms: tuple[Room, ...]
    instructors: tuple[Instructor, ...]
    demand: tuple[StudentDemand, ...]
    policy: CapacityPolicy

    # provenance (N8)
    source_fingerprint: str
    created_at: str

    #: Students dropped at intake because their status says WITHDRAWN. Carried on
    #: the snapshot so the readiness report can say so out loud: a filter nobody
    #: can see is indistinguishable from a filter that is wrong.
    excluded_withdrawn: int = 0

    #: Course codes the recommender asked for that no offering could hold,
    #: with how many student-demands each cost. Carried rather than dropped:
    #: resolved electives (AI1 -> AI463) once vanished here in silence, and
    #: a filter nobody can see is indistinguishable from a filter that is
    #: wrong.
    unmatched_demand: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if self.gender not in ("M", "F"):
            raise ValueError(f"snapshot gender must be M or F, got {self.gender!r}")
        offering_ids = {o.id for o in self.offerings}
        orphans = sorted({s.offering_id for s in self.sections} - offering_ids)
        if orphans:
            raise ValueError(f"sections reference unknown offerings: {orphans[:5]}")

    # ── derived views (computed, never stored — a snapshot has one truth) ──

    @property
    def offerings_by_id(self) -> dict[OfferingId, Offering]:
        return {o.id: o for o in self.offerings}

    @property
    def sections_by_offering(self) -> dict[OfferingId, tuple[Section, ...]]:
        out: dict[OfferingId, list[Section]] = {}
        for section in self.sections:
            out.setdefault(section.offering_id, []).append(section)
        return {k: tuple(v) for k, v in out.items()}

    @property
    def demand_index(self) -> DemandIndex:
        return DemandIndex.build(self.demand)

    @property
    def student_count(self) -> int:
        return len({d.student_id for d in self.demand})

    def rooms_of_kind(self, kind: MeetingKind) -> tuple[Room, ...]:
        return tuple(r for r in self.rooms if r.kind is kind)

    def physical_meetings_required(self) -> dict[MeetingKind, int]:
        """Total room-consuming meetings per week, per kind — the demand side of
        the room-supply comparison that the readiness report makes."""
        totals: dict[MeetingKind, int] = {}
        by_offering = self.offerings_by_id
        for section in self.sections:
            offering = by_offering[section.offering_id]
            for requirement in offering.requirements:
                if not requirement.needs_room:
                    continue
                totals[requirement.kind] = (
                    totals.get(requirement.kind, 0) + requirement.count_per_week
                )
        return totals

    @property
    def instructor_coverage(self) -> tuple[int, int]:
        """``(sections with an instructor, total sections)``.

        Never presented alone — D5 requires every instructor metric to be
        published beside its coverage, because linkage is permanently partial.
        """
        return (sum(1 for s in self.sections if s.has_instructor), len(self.sections))

    # ── provenance ──

    def fingerprint(self) -> str:
        """Content hash of the whole problem. Two runs with different
        fingerprints are **not comparable** and must not be ranked against each
        other — the failure mode that made 297 of 326 research runs worthless."""
        payload = {
            "year": self.academic_year,
            "term": self.term,
            "gender": self.gender,
            "programs": sorted(self.programs),
            "grid": self.grid.fingerprint(),
            "policy": [self.policy.default_capacity, self.policy.buffer],
            "offerings": sorted(
                [
                    o.id,
                    o.capacity,
                    sorted(o.programs),
                    [
                        [r.kind.value, r.delivery.value, r.duration, r.count_per_week]
                        for r in o.requirements
                    ],
                ]
                for o in self.offerings
            ),
            "sections": sorted([s.id, s.capacity, s.instructor_id or 0] for s in self.sections),
            "rooms": sorted(
                [r.id, r.capacity, r.kind.value, sorted(r.programs)] for r in self.rooms
            ),
            "demand": sorted([d.student_id, sorted(d.offering_ids)] for d in self.demand),
        }
        blob = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def summary(self) -> dict:
        covered, total = self.instructor_coverage
        return {
            "academic_year": self.academic_year,
            "term": self.term,
            "gender": self.gender,
            "programs": list(self.programs),
            "students": self.student_count,
            "offerings": len(self.offerings),
            "sections": len(self.sections),
            "rooms": {k.value: len(self.rooms_of_kind(k)) for k in MeetingKind},
            "physical_meetings_per_week": {
                k.value: v for k, v in self.physical_meetings_required().items()
            },
            "instructor_coverage": {
                "sections_with_instructor": covered,
                "sections_total": total,
                "percent": round(100.0 * covered / total, 1) if total else 0.0,
            },
            "grid_fingerprint": self.grid.fingerprint()[:16],
            "snapshot_fingerprint": self.fingerprint()[:16],
            "source_fingerprint": self.source_fingerprint[:16],
            "created_at": self.created_at,
        }

"""Saving a plan, and proving two plans are talking about the same thing."""

from __future__ import annotations

import hashlib
import json

from scheduler.domain import Snapshot
from scheduler.domain.board import Board
from scheduler.models import SchedulerPlacement, SchedulerPlan
from scheduler.rules import rulebook_fingerprint


def config_fingerprint(config: dict) -> str:
    """Full SHA-256 of the settings a run was given.

    Not truncated, for the same reason the rulebook hash is not: a fingerprint
    that decides whether two results may be compared is not a cache key.
    """
    blob = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def save_plan(
    snapshot: Snapshot,
    board: Board,
    *,
    config: dict,
    solver_status: str,
    wall_time_seconds: float,
    certification: str,
    violation_count: int,
    expected_clashes: float,
    naive_baseline: float,
    instructors: dict,
    rooms: dict,
    pairing: dict,
    drift: dict | None = None,
    seating: dict | None = None,
    notes: list[str] | None = None,
    label: str = "",
) -> SchedulerPlan:
    """Persist a finished board and everything needed to judge it later.

    Metrics are stored beside the floors they should be read against. "19
    working days" is meaningless without "and 19 is the proven minimum" — the
    pair is what tells a reader whether there is any headroom at all.
    """
    coverage = instructors.get("coverage", {})
    plan = SchedulerPlan.objects.create(
        academic_year=snapshot.academic_year,
        term=snapshot.term,
        gender=snapshot.gender,
        programs=",".join(snapshot.programs),
        label=label,
        snapshot_fingerprint=snapshot.source_fingerprint,
        rulebook_fingerprint=rulebook_fingerprint(),
        config_fingerprint=config_fingerprint(config),
        config=config,
        solver_status=solver_status,
        wall_time_seconds=round(wall_time_seconds, 2),
        violation_count=violation_count,
        certification=certification,
        expected_clashes=round(expected_clashes, 2),
        naive_baseline=round(naive_baseline, 2),
        instructor_days=instructors.get("working_days", 0),
        instructor_days_floor=instructors.get("floor_days", 0),
        instructor_idle_minutes=instructors.get("idle_minutes", 0),
        sections_assigned=coverage.get("sections_assigned", 0),
        sections_staffable=coverage.get("sections_staffable", 0),
        unroomed=rooms.get("unroomed", 0),
        unroomed_floor=rooms.get("impossible", 0) + rooms.get("saturated", 0),
        sibling_pairs_back_to_back=pairing.get("pairs_back_to_back", 0),
        sibling_pairs_achievable=pairing.get("pairs_achievable", 0),
        sections_same_hour_percent=(drift or {}).get("percent_same_slot", 0.0),
        sections_within_one_slot_percent=(drift or {}).get("percent_within_one_slot", 0.0),
        students_seated=(seating or {}).get("students"),
        students_clash_free_percent=(seating or {}).get("clash_free_percent"),
        student_idle_minutes_avg=(seating or {}).get("average_idle_minutes"),
        notes=list(notes or []),
    )

    offerings = snapshot.offerings_by_id
    sections = {s.id: s for s in snapshot.sections}
    SchedulerPlacement.objects.bulk_create(
        [
            SchedulerPlacement(
                plan=plan,
                section_id=p.section_id,
                offering_id=p.offering_id,
                course_code=offerings[p.offering_id].course_code
                if p.offering_id in offerings
                else "?",
                section_label=sections[p.section_id].label if p.section_id in sections else "",
                meeting_index=p.meeting_index,
                kind=p.kind.name,
                delivery=p.delivery.name,
                day=p.day.value,
                start_minute=p.window.start,
                end_minute=p.window.end,
                room_id=p.room_id or "",
                instructor_id=p.instructor_id,
            )
            for p in board.placements
        ]
    )
    return plan

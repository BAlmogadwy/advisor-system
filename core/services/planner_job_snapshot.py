"""Scenario-local cancellation baseline for every planner engine.

The canonical board snapshot preserves physical meeting identities and locks.
Scheduler rebuilds also replace scenario-owned sections, so cancellation must
restore their dependants and the grid configuration as well. Global sections
and their registrar records are outside the job's ownership and never deleted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from django.db.models import Model, Q

from core.models import (
    BoardSectionVisibility,
    SectionPlacement,
    StudentTermSection,
    TermSection,
    TermSectionProgram,
    TimetableRepairGlobalPlanItem,
    TimetableRepairRun,
    TimetableScenario,
)
from core.services.timetable_board_persistence import (
    ScenarioSnapshot,
    restore_scenario,
    snapshot_scenario,
)


@dataclass(frozen=True)
class PlannerJobSnapshot:
    board: ScenarioSnapshot
    sections: list[Mapping[str, Any]]
    students: list[Mapping[str, Any]]
    programmes: list[Mapping[str, Any]]
    visibility: list[Mapping[str, Any]]
    external_placements: list[Mapping[str, Any]]
    placement_timestamps: list[Mapping[str, Any]]
    repair_targets: list[Mapping[str, Any]]
    repair_items: list[Mapping[str, Any]]
    scenario_grid: Mapping[str, Any]


def snapshot_planner_job(scenario_id: int) -> PlannerJobSnapshot:
    return PlannerJobSnapshot(
        board=snapshot_scenario(scenario_id),
        sections=list(TermSection.objects.filter(scenario_id=scenario_id).values()),
        students=list(
            StudentTermSection.objects.filter(term_section__scenario_id=scenario_id).values()
        ),
        programmes=list(
            TermSectionProgram.objects.filter(term_section__scenario_id=scenario_id).values()
        ),
        visibility=list(
            BoardSectionVisibility.objects.filter(
                Q(board__scenario_id=scenario_id) | Q(term_section__scenario_id=scenario_id)
            ).values()
        ),
        # Older/imported data can refer to a scenario's section from another
        # board. Section replacement cascades those rows too, so retain them.
        external_placements=list(
            SectionPlacement.objects.filter(term_section__scenario_id=scenario_id)
            .exclude(board__scenario_id=scenario_id)
            .values()
        ),
        placement_timestamps=list(
            SectionPlacement.objects.filter(board__scenario_id=scenario_id).values(
                "id",
                "created_at",
                "updated_at",
            )
        ),
        repair_targets=list(
            TimetableRepairRun.objects.filter(
                Q(scenario_id=scenario_id)
                | Q(target_section__scenario_id=scenario_id)
                | Q(target_placement__board__scenario_id=scenario_id)
                | Q(target_placement__term_section__scenario_id=scenario_id)
            ).values(
                "id",
                "target_placement_id",
                "target_section_id",
            )
        ),
        repair_items=list(
            TimetableRepairGlobalPlanItem.objects.filter(
                Q(plan__scenario_id=scenario_id)
                | Q(placement__board__scenario_id=scenario_id)
                | Q(placement__term_section__scenario_id=scenario_id)
            ).values(
                "id",
                "placement_id",
            )
        ),
        scenario_grid=TimetableScenario.objects.filter(pk=scenario_id)
        .values(
            "slot_config",
            "lab_slot_config",
            "updated_at",
        )
        .get(),
    )


def _recreate(model: type[Model], rows: list[Mapping[str, Any]]) -> None:
    if not rows:
        return
    objects = [model(**row) for row in rows]
    model._default_manager.bulk_create(objects, batch_size=500)
    # bulk_create stamps auto timestamps; restore the historical values too.
    timestamps = [
        field.attname
        for field in model._meta.concrete_fields
        if getattr(field, "auto_now", False) or getattr(field, "auto_now_add", False)
    ]
    if timestamps:
        for obj, row in zip(objects, rows, strict=True):
            for name in timestamps:
                setattr(obj, name, row[name])
        model._default_manager.bulk_update(objects, timestamps, batch_size=500)


def restore_planner_job(snapshot: PlannerJobSnapshot) -> None:
    """Called within the runner's atomic cancellation/terminal decision."""
    scenario_id = snapshot.board.scenario_id
    # Include original IDs if a job re-pointed an existing assignment; unrelated
    # imported/global assignments are never selected by this scope.
    StudentTermSection.objects.filter(
        Q(term_section__scenario_id=scenario_id)
        | Q(pk__in=[row["id"] for row in snapshot.students])
    ).delete()
    BoardSectionVisibility.objects.filter(
        Q(board__scenario_id=scenario_id) | Q(term_section__scenario_id=scenario_id)
    ).delete()
    TermSectionProgram.objects.filter(term_section__scenario_id=scenario_id).delete()
    TermSection.objects.filter(scenario_id=scenario_id).delete()
    _recreate(TermSection, snapshot.sections)
    restore_scenario(scenario_id, snapshot.board)
    _recreate(SectionPlacement, snapshot.external_placements)
    _recreate(StudentTermSection, snapshot.students)
    _recreate(TermSectionProgram, snapshot.programmes)
    _recreate(BoardSectionVisibility, snapshot.visibility)
    for row in snapshot.placement_timestamps:
        SectionPlacement.objects.filter(pk=row["id"]).update(
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
    for row in snapshot.repair_targets:
        TimetableRepairRun.objects.filter(pk=row["id"]).update(
            target_placement_id=row["target_placement_id"],
            target_section_id=row["target_section_id"],
        )
    for row in snapshot.repair_items:
        TimetableRepairGlobalPlanItem.objects.filter(pk=row["id"]).update(
            placement_id=row["placement_id"]
        )
    TimetableScenario.objects.filter(pk=scenario_id).update(**snapshot.scenario_grid)

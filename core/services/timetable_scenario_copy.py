"""Copy a workspace snapshot into an independent, editable draft.

Physical sections are copied once and all board references are remapped. Shared
student/instructor/room catalogues remain shared, while registration evidence,
planner jobs and repair/approval history stay with their original context.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from django.db import IntegrityError, models, transaction
from django.db.models import Q

from core.models import (
    BoardSectionVisibility,
    BoardStudentLink,
    DeliveryBoard,
    ScenarioSectionBudget,
    ScenarioStudentCourseRequest,
    ScenarioStudentMap,
    SectionInstructor,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
    TimetableScenario,
)
from core.services.timetable_mutation_guard import (
    ScenarioPlannerBusy,
    conflicting_planner_job,
    scenario_resource_ids,
    scenario_write_scope,
)

MAX_SCENARIO_COPY_NAME = 200


class ScenarioCopyError(ValueError):
    def __init__(self, message: str, *, code: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class ScenarioCopyResult:
    scenario: TimetableScenario
    boards: list[DeliveryBoard]
    counts: dict[str, int]


class _VisibilityChanged(Exception):
    """Retry before writing when visibility expanded while acquiring locks."""


def _values(row: models.Model, **overrides: Any) -> dict[str, Any]:
    """Copy concrete data, with fresh primary keys and automatic audit times."""
    values = {
        field.attname: deepcopy(getattr(row, field.attname))
        for field in row._meta.concrete_fields
        if not field.primary_key
        and not getattr(field, "auto_now", False)
        and not getattr(field, "auto_now_add", False)
    }
    return {**values, **overrides}


def _visible_section_ids(scenario_id: int) -> tuple[int, ...]:
    return tuple(
        BoardSectionVisibility.objects.filter(board__scenario_id=scenario_id)
        .order_by("term_section_id")
        .values_list("term_section_id", flat=True)
        .distinct()
    )


def _new_draft(source: TimetableScenario, name: str | None, created_by: str) -> TimetableScenario:
    automatic = not name
    index = 1
    for _attempt in range(10):
        if automatic:
            existing = set(
                TimetableScenario.objects.filter(
                    academic_year=source.academic_year, term=source.term
                ).values_list("name", flat=True)
            )
            while True:
                suffix = " (copy)" if index == 1 else f" (copy {index})"
                candidate = source.name[: MAX_SCENARIO_COPY_NAME - len(suffix)] + suffix
                if candidate not in existing:
                    break
                index += 1
        else:
            candidate = name
        try:
            # A savepoint permits a name-collision retry without losing the
            # surrounding source locks or swallowing a later graph-copy error.
            with transaction.atomic():
                return TimetableScenario.objects.create(
                    **_values(source, name=candidate, status="draft", created_by=created_by)
                )
        except IntegrityError:
            if not TimetableScenario.objects.filter(
                academic_year=source.academic_year, term=source.term, name=candidate
            ).exists():
                raise
            if not automatic:
                raise ScenarioCopyError(
                    "A scenario with this name already exists for this year and term.",
                    code="SCENARIO_EXISTS",
                ) from None
            index += 1
    raise ScenarioCopyError(
        "Could not reserve a copy name. Please try again.", code="COPY_NAME_BUSY"
    )


def _copy_locked(
    source: TimetableScenario, name: str | None, created_by: str
) -> ScenarioCopyResult:
    boards = list(
        DeliveryBoard.objects.select_for_update()
        .filter(scenario=source)
        .order_by("display_order", "pk")
    )
    board_ids = [board.pk for board in boards]
    referenced_ids = set(
        SectionPlacement.objects.filter(board_id__in=board_ids).values_list(
            "term_section_id", flat=True
        )
    )
    referenced_ids.update(
        BoardSectionVisibility.objects.filter(board_id__in=board_ids).values_list(
            "term_section_id", flat=True
        )
    )
    # Lock physical parents before their dependent rows, including imports.
    # In particular a concurrent section deletion must not deadlock with a
    # copy holding its placements while waiting for the section itself.
    sections = list(
        TermSection.objects.select_for_update()
        .filter(Q(scenario=source) | Q(pk__in=referenced_ids))
        .order_by("pk")
    )
    identities = [(section.course_key, section.section) for section in sections]
    if len(identities) != len(set(identities)):
        raise ScenarioCopyError(
            "Different physical sections share the same course key and section label. "
            "Resolve those section identities before copying this scenario.",
            code="COPY_SECTION_IDENTITY_CONFLICT",
        )
    placements = list(
        SectionPlacement.objects.select_for_update().filter(board_id__in=board_ids).order_by("pk")
    )
    visibility = list(
        BoardSectionVisibility.objects.select_for_update()
        .filter(board_id__in=board_ids)
        .order_by("pk")
    )
    section_ids = [section.pk for section in sections]
    meetings = list(
        TermSectionMeeting.objects.select_for_update()
        .filter(term_section_id__in=section_ids)
        .order_by("pk")
    )
    programs = list(
        TermSectionProgram.objects.select_for_update()
        .filter(term_section_id__in=section_ids)
        .order_by("pk")
    )
    instructors = list(
        SectionInstructor.objects.select_for_update().filter(scenario=source).order_by("pk")
    )
    # Natural instructor identities have no physical-section FK. Preserve the
    # original owner of imported references, without merging conflicting scope
    # allocations into a single new scenario identity.
    explicit: dict[tuple[str, str], set[tuple[int, str, str]]] = {}
    for row in instructors:
        explicit.setdefault((row.course_key, row.section), set()).add(
            (row.instructor_id, row.role, row.source)
        )
    external = {
        (section.scenario_id, section.course_key, section.section)
        for section in sections
        if section.scenario_id != source.pk
    }
    if external:
        owners = {owner for owner, _, _ in external if owner is not None}
        rows = (
            SectionInstructor.objects.select_for_update()
            .filter(Q(scenario_id__in=owners) | Q(scenario__isnull=True))
            .filter(course_key__in={key for _, key, _ in external})
            .order_by("pk")
        )
        external_rows = [
            row for row in rows if (row.scenario_id, row.course_key, row.section) in external
        ]
        foreign: dict[tuple[str, str], set[tuple[int, str, str]]] = {}
        for row in external_rows:
            foreign.setdefault((row.course_key, row.section), set()).add(
                (row.instructor_id, row.role, row.source)
            )
        if any(explicit[key] != foreign[key] for key in explicit.keys() & foreign.keys()):
            raise ScenarioCopyError(
                "A referenced section has conflicting instructor allocations in different scopes. "
                "Resolve those allocations before copying this scenario.",
                code="COPY_INSTRUCTOR_IDENTITY_CONFLICT",
            )
        instructors.extend(
            row for row in external_rows if (row.course_key, row.section) not in explicit
        )
    scenario_tables = (ScenarioStudentMap, ScenarioStudentCourseRequest, ScenarioSectionBudget)
    scenario_rows = {
        table: list(table.objects.select_for_update().filter(scenario=source).order_by("pk"))
        for table in scenario_tables
    }
    student_links = list(
        BoardStudentLink.objects.select_for_update().filter(board_id__in=board_ids).order_by("pk")
    )

    target = _new_draft(source, name, created_by)
    new_boards = DeliveryBoard.objects.bulk_create(
        [DeliveryBoard(**_values(board, scenario_id=target.pk)) for board in boards]
    )
    # Resolve parents by their unique natural keys rather than relying on a
    # backend returning primary keys from bulk_create.
    board_by_label = dict(DeliveryBoard.objects.filter(scenario=target).values_list("label", "pk"))
    board_map = {board.pk: board_by_label[board.label] for board in boards}
    TermSection.objects.bulk_create(
        [TermSection(**_values(section, scenario_id=target.pk)) for section in sections],
        batch_size=500,
    )
    target_sections = {
        (key, label): pk
        for key, label, pk in TermSection.objects.filter(scenario=target).values_list(
            "course_key", "section", "pk"
        )
    }
    section_map = {
        section.pk: target_sections[(section.course_key, section.section)] for section in sections
    }
    for table, rows in ((TermSectionMeeting, meetings), (TermSectionProgram, programs)):
        table.objects.bulk_create(
            [
                table(**_values(row, term_section_id=section_map[row.term_section_id]))
                for row in rows
            ],
            batch_size=500,
        )
    SectionInstructor.objects.bulk_create(
        [SectionInstructor(**_values(row, scenario_id=target.pk)) for row in instructors],
        batch_size=500,
    )
    for table, rows in ((SectionPlacement, placements), (BoardSectionVisibility, visibility)):
        table.objects.bulk_create(
            [
                table(
                    **_values(
                        row,
                        board_id=board_map[row.board_id],
                        term_section_id=section_map[row.term_section_id],
                    )
                )
                for row in rows
            ],
            batch_size=500,
        )
    for table, rows in scenario_rows.items():
        table.objects.bulk_create(
            [table(**_values(row, scenario_id=target.pk)) for row in rows], batch_size=500
        )
    BoardStudentLink.objects.bulk_create(
        [
            BoardStudentLink(**_values(row, board_id=board_map[row.board_id]))
            for row in student_links
        ],
        batch_size=500,
    )
    # Never copy StudentTermSection. Those rows can be registration evidence or
    # feed unscoped student availability. A workspace draft copies demand, not
    # another registrar commitment. Jobs, repairs and approvals also stay behind.
    return ScenarioCopyResult(
        scenario=target,
        boards=list(DeliveryBoard.objects.filter(scenario=target).order_by("display_order", "pk")),
        counts={
            "boards": len(new_boards),
            "sections": len(sections),
            "placements": len(placements),
            "locked_placements": sum(row.is_locked for row in placements),
            "students": len(scenario_rows[ScenarioStudentMap]),
        },
    )


def copy_timetable_scenario(
    scenario_id: int, *, name: object = None, created_by: str = ""
) -> ScenarioCopyResult:
    if name is not None and (not isinstance(name, str) or len(name) > MAX_SCENARIO_COPY_NAME):
        raise ScenarioCopyError(
            f"name must be text of at most {MAX_SCENARIO_COPY_NAME} characters.",
            code="VALIDATION_NAME",
            status=400,
        )
    if isinstance(name, str):
        try:
            name.encode("utf-8")
        except UnicodeEncodeError:
            raise ScenarioCopyError(
                "name must contain valid Unicode text.", code="VALIDATION_NAME", status=400
            ) from None
        if "\x00" in name:
            raise ScenarioCopyError(
                "name must not contain a null character.", code="VALIDATION_NAME", status=400
            )
    name = name.strip() if isinstance(name, str) else None
    while True:
        visible_ids = _visible_section_ids(scenario_id)
        try:
            with scenario_write_scope(scenario_id, section_ids=visible_ids) as (scenarios, related):
                source = next((row for row in scenarios if row.pk == scenario_id), None)
                if source is None:
                    raise TimetableScenario.DoesNotExist
                if _visible_section_ids(scenario_id) != visible_ids:
                    raise _VisibilityChanged
                resources = scenario_resource_ids(scenario_id, section_ids=visible_ids)
                job = conflicting_planner_job(related, resources[1])
                if job is not None:
                    raise ScenarioPlannerBusy(job)
                return _copy_locked(source, name, created_by)
        except _VisibilityChanged:
            continue

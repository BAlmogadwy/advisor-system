"""Is this elective slot ready to be shown to a student?

The screen is gated per programme, and the gate is a BACKEND answer. A template
that knew which programmes were ready would be a second copy of this rule, kept in
step by hand, in the layer least able to check itself — and the first thing to
drift when a mapping is published.

Four states, because they are operationally different and an empty list tells the
student and the registrar nothing:

* `READY` — a mapping exists, resolves to at least one course, and is coherent.
* `NOT_PUBLISHED` — no mapping for this programme, slot and term. **The common
  case**: 77 of 84 live slots, and every slot in 8 of 12 programmes.
* `INVALID_MAPPING` — a mapping exists but is wrong: another programme's elective,
  or a credit value the slot cannot accept.
* `MAPPED_BUT_EMPTY` — declared for contract stability, and **currently
  unreachable**: the mapping's FK to `ElectiveCourse` cascades, so a row cannot
  point at a missing course, and there is no active/withdrawn flag to filter on.
  It gets a name so that adding such a flag later has somewhere to put its answer,
  not a detection branch guarding a state the schema forbids.

Lifted out of the management command so the command and the student surface share
one implementation. The command reports every slot; the surface asks about one.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from django.db import models

from core.models import (
    ElectiveCourse,
    ElectiveTermMapping,
    ProgrammeRequirement,
    Student,
)
from core.services.student_helpers import is_elective_slot, normalize_code


class MappingStatus(models.TextChoices):
    """The OPERATIONAL vocabulary. Three are reachable; one is reserved."""

    READY = "READY"
    NOT_PUBLISHED = "NOT_PUBLISHED"
    INVALID_MAPPING = "INVALID_MAPPING"
    #: Reserved for a future active-course filter. Unreachable under today's
    #: schema — the mapping's FK to `ElectiveCourse` cascades, so a row cannot
    #: point at a missing course, and there is no active/withdrawn flag. Named so
    #: that adding one has somewhere to put its answer; NOT a distinct student
    #: experience.
    MAPPED_BUT_EMPTY = "MAPPED_BUT_EMPTY"


#: Module constants, so callers read `NOT_PUBLISHED` rather than `MappingStatus.X`.
READY = MappingStatus.READY.value
NOT_PUBLISHED = MappingStatus.NOT_PUBLISHED.value
INVALID_MAPPING = MappingStatus.INVALID_MAPPING.value
MAPPED_BUT_EMPTY = MappingStatus.MAPPED_BUT_EMPTY.value

#: Operational only. A state in here may appear in the readiness report and must
#: never produce a different student-facing outcome.
RESERVED_STATUSES = frozenset({MAPPED_BUT_EMPTY})

#: ONE sentence, for EVERY non-ready state.
#:
#: This was a dict, and `INVALID_MAPPING` said «غير مكتملة في النظام» — which tells
#: the student the cause is an administrative fault. That is the registrar's
#: problem, not their situation, and the difference between "nobody published this"
#: and "somebody published it wrongly" is not theirs to read. The distinction
#: survives in the report and in `problems`; it does not reach the page.
NOT_READY_AR = (
    "لم تُنشر خيارات هذا المتطلب الاختياري بعد. راجع مرشدك الأكاديمي لمعرفة الخيارات المعتمدة."
)


def student_message(status: str) -> str:
    """What a student is told. Identical for every non-ready state, by construction.

    A function rather than a lookup table: a table invites a second entry, which is
    exactly how the leak got in the first time.
    """
    return "" if status == READY else NOT_READY_AR


def _problems(program: str, slot_credits: int, options: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    foreign = sorted(
        {
            normalize_code(o["programme"])
            for o in options
            if o["programme"] and normalize_code(o["programme"]) != program
        }
    )
    if foreign:
        problems.append(f"cross-programme mapping from {', '.join(foreign)}")
    if slot_credits:
        wrong = [
            o["course_code"]
            for o in options
            if o["credit_hours"] and int(o["credit_hours"]) != slot_credits
        ]
        if wrong:
            problems.append(
                f"credit mismatch (slot {slot_credits}h): {', '.join(sorted(wrong)[:3])}"
            )
    return problems


def slot_status(
    program: str, slot_code: str, academic_year: str, term: str
) -> tuple[str, list[dict[str, Any]], list[str]]:
    """`(status, options, problems)` for ONE slot.

    Options come back only when the slot is `READY`. A caller that received them in
    any other state would be one `if` away from rendering a list the gate exists to
    withhold.
    """
    program = normalize_code(program)
    slot_code = normalize_code(slot_code)

    row = (
        ProgrammeRequirement.objects.filter(program__iexact=program, course_code__iexact=slot_code)
        .values("type", "credit_hours")
        .first()
    )
    if row is None or not is_elective_slot(row["type"]):
        return NOT_PUBLISHED, [], ["not an elective slot for this programme"]

    ids = list(
        ElectiveTermMapping.objects.filter(
            programme__iexact=program,
            placeholder_code__iexact=slot_code,
            academic_year=str(academic_year),
            term=str(term),
        ).values_list("elective_id", flat=True)
    )
    if not ids:
        return NOT_PUBLISHED, [], []

    options = list(
        ElectiveCourse.objects.filter(id__in=ids).values(
            "course_code", "course_name", "programme", "credit_hours", "prerequisites_csv"
        )
    )
    if not options:
        # Unreachable while the FK cascades — see the module docstring. Handled
        # rather than asserted, because "impossible" is a claim about today's schema.
        return MAPPED_BUT_EMPTY, [], ["mapping resolves to no course"]

    problems = _problems(program, int(row["credit_hours"] or 0), options)
    if problems:
        return INVALID_MAPPING, [], problems
    return READY, options, []


def readiness(academic_year: str = "", term: str = "") -> list[dict[str, Any]]:
    """One row per (programme, slot) — the operational report.

    TERM-SCOPED, because `ElectiveTermMapping` is: a slot mapped for a past term is
    not mapped for this one, and without the filter a programme reads as ready on
    the strength of last year's publication.
    """
    if not academic_year or not term:
        from core.services.planner_drafts import planning_term

        default_year, default_term = planning_term()
        academic_year = academic_year or default_year
        term = term or default_term

    students: dict[str, int] = defaultdict(int)
    for program in Student.objects.values_list("program", flat=True):
        students[normalize_code(program)] += 1

    slots = sorted(
        {
            (normalize_code(r["program"]), normalize_code(r["course_code"]), str(r["type"] or ""))
            for r in ProgrammeRequirement.objects.values("program", "course_code", "type")
            if is_elective_slot(r["type"])
        }
    )

    rows: list[dict[str, Any]] = []
    for program, slot, slot_type in slots:
        status, options, problems = slot_status(program, slot, academic_year, term)
        rows.append(
            {
                "programme": program,
                "slot": slot,
                "type": slot_type,
                "students": students.get(program, 0),
                "status": status,
                "mapping_exists": status != NOT_PUBLISHED,
                "active_options": len(options),
                "problems": problems,
                "ready": status == READY,
            }
        )
    return rows


__all__ = [
    "INVALID_MAPPING",
    "MAPPED_BUT_EMPTY",
    "NOT_PUBLISHED",
    "NOT_READY_AR",
    "READY",
    "RESERVED_STATUSES",
    "MappingStatus",
    "readiness",
    "slot_status",
    "student_message",
]

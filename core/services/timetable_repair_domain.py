"""Solver-domain snapshot helpers for timetable repair.

The repair optimiser still reads the existing Django models as its source of
truth. This module gives the solver and audit/reporting paths a stable,
solver-native view of those rows so future batch/global repair work does not
need to rediscover the same indexes in multiple places.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from django.db.models import Q

from core.models import SectionPlacement, Student, StudentTermSection, TermSection
from core.services.student_helpers import normalize_code
from core.services.timetable_demand import load_scenario_course_demands


@dataclass(frozen=True)
class RepairDomainStudent:
    student_id: int
    program: str = ""
    section: str = ""
    status: str = ""


@dataclass(frozen=True)
class RepairDomainSection:
    term_section_id: int
    course_key: str
    course_code: str
    section: str
    capacity: int
    current_enrolment: int


@dataclass(frozen=True)
class RepairDomainPlacement:
    placement_id: int
    board_id: int
    board_label: str
    term_section_id: int
    day: str
    start_time: str
    end_time: str
    room: str
    locked: bool


@dataclass(frozen=True)
class RepairDomainAssignment:
    student_id: int
    term_section_id: int
    course_key: str
    source: str


@dataclass(frozen=True)
class RepairDomainRequest:
    student_id: int
    course_key: str
    course_code: str
    status: str
    priority: str
    primary_term: int | None
    is_cross_term: bool
    reason_blocked: str


@dataclass(frozen=True)
class RepairSolverSection:
    id: int
    course_key: str
    course_code: str
    course_name: str
    section: str
    available_capacity: int
    registered_count: int

    @property
    def term_section_id(self) -> int:
        return self.id


@dataclass(frozen=True)
class RepairSolverProblemInput:
    scenario_id: int
    target_course_key: str
    blocked_student_ids: tuple[int, ...]
    student_ids: tuple[int, ...]
    course_keys: tuple[str, ...]
    sections: tuple[RepairSolverSection, ...]
    sections_by_course: dict[str, list[int]]
    current_by_student_course: dict[int, dict[str, int]]
    affected_current_by_section: dict[int, int]
    total_current_by_section: dict[int, int]
    requested_courses_by_student: dict[int, set[str]]
    duplicate_current_assignments: tuple[dict[str, Any], ...]
    missing_current_options: tuple[dict[str, Any], ...]
    exact_assignment_source_available: bool
    assignment_source: str = "student_term_section"
    assignment_source_summary: dict[str, Any] | None = None

    @property
    def section_ids(self) -> tuple[int, ...]:
        return tuple(section.id for section in self.sections)

    @property
    def section_by_id(self) -> dict[int, RepairSolverSection]:
        return {section.id: section for section in self.sections}

    def to_audit_payload(self) -> dict[str, Any]:
        return {
            "version": "repair-solver-problem-input-v1",
            "scenario_id": self.scenario_id,
            "target_course_key": self.target_course_key,
            "counts": {
                "students": len(self.student_ids),
                "blocked_students": len(self.blocked_student_ids),
                "courses": len(self.course_keys),
                "sections": len(self.sections),
                "current_assignments": sum(
                    len(courses) for courses in self.current_by_student_course.values()
                ),
                "requested_courses": sum(
                    len(courses) for courses in self.requested_courses_by_student.values()
                ),
                "duplicate_current_assignments": len(self.duplicate_current_assignments),
                "missing_current_options": len(self.missing_current_options),
            },
            "exact_assignment_source_available": self.exact_assignment_source_available,
            "assignment_source": self.assignment_source,
            "assignment_source_summary": self.assignment_source_summary or {},
            "course_keys": list(self.course_keys),
            "section_ids": list(self.section_ids),
            "sections_by_course": self.sections_by_course,
            "duplicate_current_assignments": list(self.duplicate_current_assignments[:20]),
            "missing_current_options": list(self.missing_current_options[:20]),
        }


@dataclass(frozen=True)
class RepairDomainSnapshot:
    scenario_id: int
    students: tuple[RepairDomainStudent, ...]
    sections: tuple[RepairDomainSection, ...]
    placements: tuple[RepairDomainPlacement, ...]
    assignments: tuple[RepairDomainAssignment, ...]
    requests: tuple[RepairDomainRequest, ...]

    def to_audit_payload(self, *, max_index_items: int = 500) -> dict[str, Any]:
        indexes = build_repair_domain_indexes(self)
        return {
            "version": "repair-domain-snapshot-v1",
            "scenario_id": self.scenario_id,
            "counts": {
                "students": len(self.students),
                "sections": len(self.sections),
                "placements": len(self.placements),
                "assignments": len(self.assignments),
                "requests": len(self.requests),
                "courses": len(indexes["sections_by_course"]),
            },
            "indexes": {
                key: _truncate_mapping(value, max_items=max_index_items)
                for key, value in indexes.items()
            },
            "index_truncated": {
                key: _mapping_item_count(value) > max_index_items for key, value in indexes.items()
            },
            "students": [asdict(row) for row in self.students[:max_index_items]],
            "sections": [asdict(row) for row in self.sections[:max_index_items]],
            "placements": [asdict(row) for row in self.placements[:max_index_items]],
            "assignments": [asdict(row) for row in self.assignments[:max_index_items]],
            "requests": [asdict(row) for row in self.requests[:max_index_items]],
            "rows_truncated": {
                "students": len(self.students) > max_index_items,
                "sections": len(self.sections) > max_index_items,
                "placements": len(self.placements) > max_index_items,
                "assignments": len(self.assignments) > max_index_items,
                "requests": len(self.requests) > max_index_items,
            },
        }


def build_repair_domain_snapshot(
    scenario_id: int,
    *,
    student_ids: Iterable[int] | None = None,
    course_keys: Iterable[str] | None = None,
    section_ids: Iterable[int] | None = None,
) -> RepairDomainSnapshot:
    """Build a bounded, solver-native snapshot for a repair component."""

    wanted_students = {int(value) for value in (student_ids or [])}
    wanted_course_norms = {normalize_code(value) for value in (course_keys or []) if value}
    wanted_section_ids = {int(value) for value in (section_ids or [])}

    section_qs = TermSection.objects.filter(scenario_id=scenario_id)
    if wanted_section_ids:
        section_qs = section_qs.filter(id__in=wanted_section_ids)
    elif wanted_course_norms:
        all_sections = list(section_qs)
        section_qs = [  # type: ignore[assignment]
            section
            for section in all_sections
            if normalize_code(section.course_key or section.course_code) in wanted_course_norms
        ]

    section_rows = list(section_qs)
    selected_section_ids = {int(section.id) for section in section_rows}
    selected_course_norms = {
        normalize_code(section.course_key or section.course_code) for section in section_rows
    }
    selected_course_norms.update(wanted_course_norms)

    assignment_qs = StudentTermSection.objects.filter(
        term_section__scenario_id=scenario_id,
    ).select_related("term_section")
    if wanted_students:
        assignment_qs = assignment_qs.filter(student_id__in=wanted_students)
    if selected_section_ids:
        assignment_qs = assignment_qs.filter(term_section_id__in=selected_section_ids)
    assignment_rows = list(assignment_qs)

    request_rows = [
        demand
        for demand in load_scenario_course_demands(scenario_id)
        if (not wanted_students or int(demand.student_id) in wanted_students)
        and (
            not selected_course_norms
            or normalize_code(demand.course_key or demand.course_code) in selected_course_norms
        )
    ]
    requested_student_ids = {int(row.student_id) for row in request_rows}
    assigned_student_ids = {int(row.student_id) for row in assignment_rows}
    selected_student_ids = wanted_students | requested_student_ids | assigned_student_ids

    student_rows = list(
        Student.objects.filter(student_id__in=selected_student_ids).values(
            "student_id",
            "program",
            "section",
            "status",
        )
    )
    placement_rows = list(
        SectionPlacement.objects.filter(term_section_id__in=selected_section_ids)
        .select_related("board", "term_section")
        .order_by("board__display_order", "term_section__course_key", "term_section__section")
    )

    return RepairDomainSnapshot(
        scenario_id=int(scenario_id),
        students=tuple(
            RepairDomainStudent(
                student_id=int(row["student_id"]),
                program=str(row.get("program") or ""),
                section=str(row.get("section") or ""),
                status=str(row.get("status") or ""),
            )
            for row in sorted(student_rows, key=lambda item: int(item["student_id"]))
        ),
        sections=tuple(
            RepairDomainSection(
                term_section_id=int(section.id),
                course_key=str(section.course_key or section.course_code or ""),
                course_code=str(section.course_code or ""),
                section=str(section.section or ""),
                capacity=int(section.available_capacity or 0),
                current_enrolment=int(section.registered_count or 0),
            )
            for section in sorted(
                section_rows,
                key=lambda item: (
                    str(item.course_key or ""),
                    str(item.section or ""),
                    int(item.id),
                ),
            )
        ),
        placements=tuple(
            RepairDomainPlacement(
                placement_id=int(row.id),
                board_id=int(row.board_id),
                board_label=str(row.board.label if row.board_id else ""),
                term_section_id=int(row.term_section_id),
                day=str(row.day or ""),
                start_time=str(row.start_time or ""),
                end_time=str(row.end_time or ""),
                room=str(row.room or ""),
                locked=bool(row.is_locked),
            )
            for row in placement_rows
        ),
        assignments=tuple(
            RepairDomainAssignment(
                student_id=int(row.student_id),
                term_section_id=int(row.term_section_id),
                course_key=str(row.term_section.course_key or row.term_section.course_code or ""),
                source=str(row.source or ""),
            )
            for row in sorted(
                assignment_rows,
                key=lambda item: (
                    int(item.student_id),
                    str(item.term_section.course_key or ""),
                    str(item.term_section.section or ""),
                ),
            )
        ),
        requests=tuple(
            RepairDomainRequest(
                student_id=int(row.student_id),
                course_key=str(row.course_key or row.course_code or ""),
                course_code=str(row.course_code or ""),
                status=str(row.status or ""),
                priority=str(row.priority or ""),
                primary_term=row.primary_term,
                is_cross_term=bool(row.is_cross_term),
                reason_blocked=str(row.reason_blocked or ""),
            )
            for row in sorted(
                request_rows,
                key=lambda item: (int(item.student_id), str(item.course_key or "")),
            )
        ),
    )


def build_repair_solver_problem_input(
    scenario_id: int,
    *,
    target_course_key: str,
    student_ids: Iterable[int],
    blocked_student_ids: Iterable[int],
    course_keys: Iterable[str],
    section_ids: Iterable[int] | None = None,
) -> RepairSolverProblemInput:
    """Build the solver's plain data boundary from Django source rows.

    Django remains the system of record, but the optimisation code should work
    from this bounded problem input rather than discovering ORM rows while it
    constructs the mathematical model.
    """

    scenario_id = int(scenario_id)
    target_course_key = str(target_course_key or "").strip()
    selected_students = sorted({int(student_id) for student_id in student_ids})
    blocked_students = tuple(sorted({int(student_id) for student_id in blocked_student_ids}))
    requested_section_ids = {int(section_id) for section_id in (section_ids or [])}
    exact_assignment_source_available = StudentTermSection.objects.filter(
        term_section__scenario_id=scenario_id
    ).exists()

    current_rows = list(
        StudentTermSection.objects.filter(
            student_id__in=selected_students,
            term_section__scenario_id=scenario_id,
        )
        .select_related("term_section")
        .order_by("student_id", "term_section__course_key", "term_section__section", "id")
    )
    current_by_student_course: dict[int, dict[str, int]] = defaultdict(dict)
    affected_current_by_section: dict[int, int] = defaultdict(int)
    duplicate_current_assignments: list[dict[str, Any]] = []
    discovered_courses = {str(course).strip() for course in course_keys if str(course).strip()}
    if target_course_key:
        discovered_courses.add(target_course_key)
    for row in current_rows:
        course = str(row.term_section.course_key or row.term_section.course_code or "").strip()
        if course:
            discovered_courses.add(course)
        existing = current_by_student_course[int(row.student_id)].get(course)
        if existing is not None:
            duplicate_current_assignments.append(
                {
                    "student_id": int(row.student_id),
                    "course_key": course,
                    "section_ids": [existing, int(row.term_section_id)],
                }
            )
            continue
        current_by_student_course[int(row.student_id)][course] = int(row.term_section_id)
        affected_current_by_section[int(row.term_section_id)] += 1

    assignment_source = "student_term_section" if exact_assignment_source_available else "none"
    assignment_source_summary: dict[str, Any] = {}
    evaluator_total_current_by_section: dict[int, int] = {}
    if not exact_assignment_source_available and selected_students:
        evaluator_baseline = _build_current_evaluator_assignment_baseline(
            scenario_id,
            selected_students=selected_students,
        )
        assignment_source_summary = dict(evaluator_baseline.get("summary") or {})
        evaluator_current_by_student_course = (
            evaluator_baseline.get("current_by_student_course") or {}
        )
        if evaluator_current_by_student_course:
            assignment_source = "current_evaluator_assignment"
            current_by_student_course = defaultdict(dict)
            affected_current_by_section = defaultdict(int)
            duplicate_current_assignments = []
            for sid, courses in evaluator_current_by_student_course.items():
                for course, section_id in courses.items():
                    current_by_student_course[int(sid)][str(course)] = int(section_id)
                    affected_current_by_section[int(section_id)] += 1
                    if str(course).strip():
                        discovered_courses.add(str(course).strip())
                    requested_section_ids.add(int(section_id))
        evaluator_total_current_by_section = {
            int(section_id): int(count)
            for section_id, count in (
                evaluator_baseline.get("total_current_by_section") or {}
            ).items()
        }

    section_filter = Q()
    if discovered_courses:
        section_filter |= Q(course_key__in=discovered_courses) | Q(
            course_code__in=discovered_courses
        )
    if requested_section_ids:
        section_filter |= Q(id__in=requested_section_ids)
    if section_filter:
        section_qs = TermSection.objects.filter(scenario_id=scenario_id).filter(section_filter)
    else:
        section_qs = TermSection.objects.filter(scenario_id=scenario_id, id__in=[])
    section_models = list(section_qs.order_by("course_key", "section", "id"))
    section_ids_in_problem = {int(section.id) for section in section_models}
    for section in section_models:
        course = str(section.course_key or section.course_code or "").strip()
        if course:
            discovered_courses.add(course)

    total_current_by_section: dict[int, int] = defaultdict(int)
    if section_ids_in_problem and exact_assignment_source_available:
        for section_id in StudentTermSection.objects.filter(
            term_section__scenario_id=scenario_id,
            term_section_id__in=section_ids_in_problem,
        ).values_list("term_section_id", flat=True):
            total_current_by_section[int(section_id)] += 1
    elif section_ids_in_problem and evaluator_total_current_by_section:
        for section_id, count in evaluator_total_current_by_section.items():
            if int(section_id) in section_ids_in_problem:
                total_current_by_section[int(section_id)] += int(count)

    requested_courses_by_student: dict[int, set[str]] = defaultdict(set)
    if selected_students:
        for demand in load_scenario_course_demands(
            scenario_id,
            course_keys=discovered_courses if discovered_courses else None,
        ):
            sid = int(demand.student_id)
            course = str(demand.course_key or demand.course_code or "").strip()
            if sid in selected_students and course:
                requested_courses_by_student[sid].add(course)

    sections_by_course: dict[str, list[int]] = defaultdict(list)
    solver_sections: list[RepairSolverSection] = []
    for section in section_models:
        course = str(section.course_key or section.course_code or "").strip()
        solver_section = RepairSolverSection(
            id=int(section.id),
            course_key=course,
            course_code=str(section.course_code or ""),
            course_name=str(section.course_name or ""),
            section=str(section.section or ""),
            available_capacity=int(section.available_capacity or 0),
            registered_count=int(section.registered_count or 0),
        )
        solver_sections.append(solver_section)
        sections_by_course[course].append(solver_section.id)

    missing_current_options = [
        {
            "student_id": sid,
            "course_key": course,
            "section_id": section_id,
        }
        for sid, courses in current_by_student_course.items()
        for course, section_id in courses.items()
        if int(section_id) not in section_ids_in_problem
    ]

    return RepairSolverProblemInput(
        scenario_id=scenario_id,
        target_course_key=target_course_key,
        blocked_student_ids=blocked_students,
        student_ids=tuple(selected_students),
        course_keys=tuple(sorted(discovered_courses)),
        sections=tuple(solver_sections),
        sections_by_course={
            course: sorted(set(ids)) for course, ids in sorted(sections_by_course.items())
        },
        current_by_student_course={
            int(sid): dict(sorted(courses.items()))
            for sid, courses in current_by_student_course.items()
        },
        affected_current_by_section=dict(sorted(affected_current_by_section.items())),
        total_current_by_section=dict(sorted(total_current_by_section.items())),
        requested_courses_by_student={
            int(sid): set(courses) for sid, courses in requested_courses_by_student.items()
        },
        duplicate_current_assignments=tuple(duplicate_current_assignments),
        missing_current_options=tuple(missing_current_options),
        exact_assignment_source_available=exact_assignment_source_available,
        assignment_source=assignment_source,
        assignment_source_summary=assignment_source_summary,
    )


def _build_current_evaluator_assignment_baseline(
    scenario_id: int,
    *,
    selected_students: Iterable[int],
) -> dict[str, Any]:
    """Use the optimiser's whole-scenario assignment as a draft baseline.

    Generated timetable scenarios often have no persisted StudentTermSection rows yet.
    In that case, the repair optimiser should not invent a selected-course-only
    assignment. It first runs the same evaluator used by the timetable optimiser over
    all scenario students and all scenario sections, then extracts the bounded repair
    students from that global assignment.
    """

    selected = {int(student_id) for student_id in selected_students}
    if not selected:
        return {
            "current_by_student_course": {},
            "total_current_by_section": {},
            "summary": {"source": "current_evaluator_assignment", "selected_student_count": 0},
        }

    from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
    from core.services.timetable_optimizer_v2 import (
        build_course_rigidity_for_scenario,
        build_section_states_for_scenario,
        build_student_profiles_for_scenario,
    )

    profiles = build_student_profiles_for_scenario(scenario_id)
    sections = build_section_states_for_scenario(scenario_id)
    rigidity = build_course_rigidity_for_scenario(scenario_id)
    if not profiles or not sections:
        return {
            "current_by_student_course": {},
            "total_current_by_section": {},
            "summary": {
                "source": "current_evaluator_assignment",
                "available": False,
                "reason": "missing_student_profiles_or_sections",
                "global_student_count": len(profiles),
                "global_section_count": len(sections),
                "selected_student_count": len(selected),
            },
        }

    evaluation = evaluate_generated_timetable_candidate(
        candidate_id="current_repair_baseline",
        generated_sections=sections,
        student_profiles=profiles,
        course_rigidity=rigidity,
    )
    section_id_to_term_section_id = _evaluator_section_id_to_term_section_id(scenario_id)
    current_by_student_course: dict[int, dict[str, int]] = defaultdict(dict)
    total_current_by_section: dict[int, int] = defaultdict(int)
    unmapped_section_ids: set[str] = set()

    for raw_student_id, state in evaluation.assignment_states.items():
        try:
            sid = int(raw_student_id)
        except (TypeError, ValueError):
            continue
        for course, evaluator_section_id in sorted(state.assigned_sections.items()):
            term_section_id = section_id_to_term_section_id.get(str(evaluator_section_id))
            if term_section_id is None:
                unmapped_section_ids.add(str(evaluator_section_id))
                continue
            total_current_by_section[int(term_section_id)] += 1
            if sid in selected:
                current_by_student_course[sid][str(course)] = int(term_section_id)

    selected_assignment_count = sum(len(courses) for courses in current_by_student_course.values())
    return {
        "current_by_student_course": {
            sid: dict(sorted(courses.items())) for sid, courses in current_by_student_course.items()
        },
        "total_current_by_section": dict(sorted(total_current_by_section.items())),
        "summary": {
            "source": "current_evaluator_assignment",
            "available": bool(current_by_student_course),
            "scope": "whole_scenario_then_bounded_repair_slice",
            "global_student_count": len(profiles),
            "global_section_count": len(sections),
            "global_assigned_student_count": sum(
                1 for state in evaluation.assignment_states.values() if state.assigned_sections
            ),
            "global_unresolved_student_count": len(evaluation.unresolved_student_ids),
            "selected_student_count": len(selected),
            "selected_students_with_assignments": len(current_by_student_course),
            "selected_assignment_count": selected_assignment_count,
            "unmapped_section_ids": sorted(unmapped_section_ids)[:20],
            "unmapped_section_id_count": len(unmapped_section_ids),
        },
    }


def build_current_evaluator_assignment_baseline(
    scenario_id: int,
    *,
    selected_students: Iterable[int],
) -> dict[str, Any]:
    """Return the optimiser/evaluator assignment baseline for selected students."""

    return _build_current_evaluator_assignment_baseline(
        scenario_id,
        selected_students=selected_students,
    )


def _evaluator_section_id_to_term_section_id(scenario_id: int) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for section in TermSection.objects.filter(scenario_id=scenario_id).only(
        "id",
        "course_key",
        "course_code",
        "section",
    ):
        section_label = str(section.section or "")
        for course in {section.course_key, section.course_code}:
            course_key = str(course or "").strip()
            if course_key:
                mapping[f"{course_key}_{section_label}"] = int(section.id)
    return mapping


def build_repair_domain_indexes(snapshot: RepairDomainSnapshot) -> dict[str, dict[str, list]]:
    sections_by_course: dict[str, list[int]] = defaultdict(list)
    students_by_section: dict[str, list[int]] = defaultdict(list)
    courses_by_student: dict[str, list[str]] = defaultdict(list)
    current_sections_by_student_course: dict[str, list[int]] = defaultdict(list)
    requested_courses_by_student: dict[str, list[str]] = defaultdict(list)
    requesting_students_by_course: dict[str, list[int]] = defaultdict(list)
    placements_by_section: dict[str, list[int]] = defaultdict(list)

    for section in snapshot.sections:
        sections_by_course[normalize_code(section.course_key)].append(section.term_section_id)
    for placement in snapshot.placements:
        placements_by_section[str(placement.term_section_id)].append(placement.placement_id)
    for assignment in snapshot.assignments:
        sid = str(assignment.student_id)
        course = normalize_code(assignment.course_key)
        students_by_section[str(assignment.term_section_id)].append(assignment.student_id)
        courses_by_student[sid].append(course)
        current_sections_by_student_course[f"{sid}:{course}"].append(assignment.term_section_id)
    for request in snapshot.requests:
        sid = str(request.student_id)
        course = normalize_code(request.course_key)
        requested_courses_by_student[sid].append(course)
        requesting_students_by_course[course].append(request.student_id)

    return {
        "sections_by_course": _sorted_mapping(sections_by_course),
        "students_by_section": _sorted_mapping(students_by_section),
        "courses_by_student": _sorted_mapping(courses_by_student),
        "current_sections_by_student_course": _sorted_mapping(current_sections_by_student_course),
        "requested_courses_by_student": _sorted_mapping(requested_courses_by_student),
        "requesting_students_by_course": _sorted_mapping(requesting_students_by_course),
        "placements_by_section": _sorted_mapping(placements_by_section),
    }


def _sorted_mapping(mapping: dict[str, list]) -> dict[str, list]:
    return {key: sorted(set(values)) for key, values in sorted(mapping.items())}


def _mapping_item_count(mapping: dict[str, list]) -> int:
    return sum(len(values) for values in mapping.values())


def _truncate_mapping(mapping: dict[str, list], *, max_items: int) -> dict[str, list]:
    remaining = max(1, int(max_items or 1))
    out: dict[str, list] = {}
    for key, values in mapping.items():
        if remaining <= 0:
            break
        sliced = list(values)[:remaining]
        out[key] = sliced
        remaining -= len(sliced)
    return out


__all__ = [
    "RepairDomainAssignment",
    "RepairDomainPlacement",
    "RepairDomainRequest",
    "RepairDomainSection",
    "RepairDomainSnapshot",
    "RepairDomainStudent",
    "RepairSolverProblemInput",
    "RepairSolverSection",
    "build_repair_domain_indexes",
    "build_repair_domain_snapshot",
    "build_repair_solver_problem_input",
]

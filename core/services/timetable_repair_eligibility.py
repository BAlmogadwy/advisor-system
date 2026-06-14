"""Eligibility policy helpers for timetable repair.

The repair solver should only reason over academically valid section choices.
This module keeps those checks separate from the CP-SAT model so the solver
stays focused on optimisation and the policy can be audited independently.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

from core.models import (
    BoardSectionVisibility,
    Prerequisite,
    Room,
    SectionPlacement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
)
from core.services.student_helpers import normalize_code
from core.services.timetable_demand import load_student_course_demand_map
from core.services.timetable_online import OnlineCourseLookup
from core.services.timetable_pr4_instructor import (
    is_instructor_links_enabled,
    normalise_instructor,
)
from core.services.timetable_rooming import room_type_for_placement

SECTION_GENDERS = {"M", "F"}
CLOSED_SECTION_SOURCE_MARKERS = (
    "closed",
    "cancelled",
    "canceled",
    "inactive",
    "disabled",
)
PROTECTED_STATUS_MARKERS = (
    "locked",
    "protected",
    "cannot_move",
    "cannot move",
    "manual",
    "manual_approval",
    "manual approval",
    "special_case",
    "special case",
)
PROTECTED_SOURCE_MARKERS = (
    "locked",
    "protected",
    "cannot_move",
    "cannot move",
    "manual",
    "manual_approval",
    "manual approval",
    "special_case",
    "special case",
)
GRADUATION_STATUS_MARKERS = (
    "graduating",
    "graduate",
    "expected graduate",
    "final year",
)
HOUR_PREREQ_RE = re.compile(r"^(\d+)\s*\(?\s*HOURS?\s*\)?$", re.IGNORECASE)


@dataclass(frozen=True)
class RepairStudentPolicy:
    student_id: int
    program: str = ""
    section: str = ""
    status: str = ""
    total_earned_credits: int = 0
    current_registered_credits: int = 0
    priority_group: str = "normal"
    graduation_priority: bool = False
    mobility_policy: str = "normal"
    protected: bool = False
    protection_reason: str = ""


@dataclass(frozen=True)
class RepairSectionPolicy:
    term_section_id: int
    course_key: str = ""
    course_code: str = ""
    academic_course_code: str = ""
    section_label: str = ""
    section_gender: str = ""
    allowed_programs: tuple[str, ...] = ()
    board_ids: tuple[int, ...] = ()
    board_labels: tuple[str, ...] = ()
    board_terms: tuple[int, ...] = ()
    visible_board_ids: tuple[int, ...] = ()
    visible_board_labels: tuple[str, ...] = ()
    visible_board_terms: tuple[int, ...] = ()
    visible_programs: tuple[str, ...] = ()
    visibility_restricted: bool = False
    placed: bool = False
    placement_count: int = 0
    online: bool = False
    room_codes: tuple[str, ...] = ()
    campus_codes: tuple[str, ...] = ()
    room_sections: tuple[str, ...] = ()
    room_departments: tuple[str, ...] = ()
    room_types: tuple[str, ...] = ()
    room_capacities: tuple[int, ...] = ()
    required_room_types: tuple[str, ...] = ()
    required_capacity: int = 0
    instructors: tuple[str, ...] = ()
    instructor_conflicts: tuple[dict[str, Any], ...] = ()
    source_tag: str = ""
    closed: bool = False
    locked: bool = False


@dataclass
class RepairEligibilityContext:
    students: dict[int, RepairStudentPolicy]
    sections: dict[int, RepairSectionPolicy]
    protected_assignments: set[tuple[int, int]]
    protected_assignment_reasons: dict[tuple[int, int], str]
    passed_by_student: dict[int, set[str]]
    studying_by_student: dict[int, set[str]]
    demand_by_student_course: dict[tuple[int, str], dict[str, Any]]
    prereqs_by_program_course: dict[tuple[str, str], tuple[str, ...]]
    rejection_counts: Counter[str] = field(default_factory=Counter)
    rejected_option_count: int = 0
    rejection_samples: list[dict[str, Any]] = field(default_factory=list)

    def record_rejection(
        self,
        *,
        student_id: int,
        course_key: str,
        section: TermSection,
        reasons: list[dict[str, Any]],
    ) -> None:
        self.rejected_option_count += 1
        for reason in reasons:
            code = str(reason.get("code") or "UNKNOWN")
            self.rejection_counts[code] += 1
        if len(self.rejection_samples) >= 25:
            return
        self.rejection_samples.append(
            {
                "student_id": student_id,
                "course_key": course_key,
                "term_section_id": section.id,
                "section": section.section,
                "reasons": reasons,
            }
        )

    def record_rejection_by_section_id(
        self,
        *,
        student_id: int,
        course_key: str,
        section_id: int,
        reasons: list[dict[str, Any]],
    ) -> None:
        policy = self.sections.get(int(section_id))
        self.rejected_option_count += 1
        for reason in reasons:
            code = str(reason.get("code") or "UNKNOWN")
            self.rejection_counts[code] += 1
        if len(self.rejection_samples) >= 25:
            return
        self.rejection_samples.append(
            {
                "student_id": student_id,
                "course_key": course_key,
                "term_section_id": int(section_id),
                "section": policy.section_label if policy else "",
                "reasons": reasons,
            }
        )


def build_repair_eligibility_context(
    *,
    scenario_id: int,
    student_ids: list[int],
    sections: list[TermSection],
) -> RepairEligibilityContext:
    """Load the policy data needed by the repair solver in bounded batches."""

    section_ids = [section.id for section in sections]
    student_rows = {
        int(row["student_id"]): row
        for row in Student.objects.filter(student_id__in=student_ids).values(
            "student_id",
            "program",
            "section",
            "status",
            "total_earned_credits",
            "current_registered_credits",
        )
    }
    students: dict[int, RepairStudentPolicy] = {}
    for sid in student_ids:
        row = student_rows.get(int(sid), {})
        status = str(row.get("status") or "")
        total_earned = _int_or_zero(row.get("total_earned_credits"))
        current_registered = _int_or_zero(row.get("current_registered_credits"))
        priority_group, graduation_priority, protected, reason = classify_repair_student_policy(
            status=status,
            total_earned_credits=total_earned,
            current_registered_credits=current_registered,
        )
        students[int(sid)] = RepairStudentPolicy(
            student_id=int(sid),
            program=_normalise_program(row.get("program")),
            section=_normalise_gender(row.get("section")),
            status=status,
            total_earned_credits=total_earned,
            current_registered_credits=current_registered,
            priority_group=priority_group,
            graduation_priority=graduation_priority,
            mobility_policy="fixed"
            if protected
            else "priority_minimise_disruption"
            if graduation_priority
            else "normal",
            protected=protected,
            protection_reason=reason,
        )

    placement_rows = list(
        SectionPlacement.objects.filter(
            board__scenario_id=scenario_id,
            term_section_id__in=section_ids,
        ).select_related("board", "term_section")
    )
    programs_by_section: dict[int, set[str]] = defaultdict(set)
    board_ids_by_section: dict[int, set[int]] = defaultdict(set)
    board_labels_by_section: dict[int, set[str]] = defaultdict(set)
    board_terms_by_section: dict[int, set[int]] = defaultdict(set)
    rooms_by_section: dict[int, set[str]] = defaultdict(set)
    online_by_section: dict[int, bool] = defaultdict(bool)
    required_types_by_section: dict[int, set[str]] = defaultdict(set)
    required_capacity_by_section: Counter[int] = Counter()
    placement_counts_by_section: Counter[int] = Counter()
    locked_sections: set[int] = set()
    online_lookup = OnlineCourseLookup()
    for placement in placement_rows:
        placement_counts_by_section[placement.term_section_id] += 1
        programs_by_section[placement.term_section_id].update(
            _normalise_program_set(placement.board.program)
        )
        board_ids_by_section[placement.term_section_id].add(int(placement.board_id))
        if placement.board.label:
            board_labels_by_section[placement.term_section_id].add(str(placement.board.label))
        if placement.board.nominal_term is not None:
            board_terms_by_section[placement.term_section_id].add(int(placement.board.nominal_term))
        is_online = online_lookup.is_online_course_for_board(
            placement.board,
            placement.term_section.course_code,
        )
        online_by_section[placement.term_section_id] = (
            online_by_section[placement.term_section_id] or is_online
        )
        if not is_online:
            required_types_by_section[placement.term_section_id].add(
                room_type_for_placement(placement)
            )
            required_capacity_by_section[placement.term_section_id] = max(
                int(required_capacity_by_section.get(placement.term_section_id, 0)),
                _int_or_zero(placement.term_section.registered_count)
                or _int_or_zero(placement.term_section.available_capacity)
                or _int_or_zero(placement.board.target_size),
            )
        room_code = _normalise_room_code(placement.room)
        if room_code:
            rooms_by_section[placement.term_section_id].add(room_code)
        if placement.is_locked:
            locked_sections.add(placement.term_section_id)

    visible_board_ids_by_section: dict[int, set[int]] = defaultdict(set)
    visible_board_labels_by_section: dict[int, set[str]] = defaultdict(set)
    visible_board_terms_by_section: dict[int, set[int]] = defaultdict(set)
    visible_programs_by_section: dict[int, set[str]] = defaultdict(set)
    visibility_rows = (
        BoardSectionVisibility.objects.filter(
            board__scenario_id=scenario_id,
            term_section_id__in=section_ids,
        )
        .select_related("board")
        .order_by("term_section_id", "board_id")
    )
    for row in visibility_rows:
        visible_board_ids_by_section[row.term_section_id].add(int(row.board_id))
        if row.board.label:
            visible_board_labels_by_section[row.term_section_id].add(str(row.board.label))
        if row.board.nominal_term is not None:
            visible_board_terms_by_section[row.term_section_id].add(int(row.board.nominal_term))
        visible_programs_by_section[row.term_section_id].update(
            _normalise_program_set(row.board.program)
        )

    instructor_names_by_section, instructor_conflicts_by_section = _section_instructor_policy(
        scenario_id=scenario_id,
        target_section_ids=set(section_ids),
    )

    room_profiles_by_code: dict[str, list[dict[str, str]]] = defaultdict(list)
    room_codes = sorted({room for rooms in rooms_by_section.values() for room in rooms})
    if room_codes:
        for row in Room.objects.filter(room_code__in=room_codes).values(
            "room_code",
            "building",
            "wing",
            "section",
            "department",
            "room_type",
            "capacity",
        ):
            room_profiles_by_code[str(row.get("room_code") or "").strip()].append(
                {
                    "campus": _normalise_campus_code(row.get("building"))
                    or _normalise_campus_code(row.get("wing")),
                    "section": _normalise_gender(row.get("section")),
                    "department": ",".join(_normalise_program_set(row.get("department"))),
                    "room_type": _normalise_room_type(row.get("room_type")),
                    "capacity": str(_int_or_zero(row.get("capacity"))),
                }
            )

    section_policies: dict[int, RepairSectionPolicy] = {}
    for section in sections:
        section_room_codes = sorted(rooms_by_section.get(section.id, set()))
        room_sections = sorted(
            {
                profile["section"]
                for room_code in section_room_codes
                for profile in room_profiles_by_code.get(room_code, [])
                if profile.get("section")
            }
        )
        campus_codes = sorted(
            {
                profile["campus"]
                for room_code in section_room_codes
                for profile in room_profiles_by_code.get(room_code, [])
                if profile.get("campus")
            }
        )
        room_departments = sorted(
            {
                department
                for room_code in section_room_codes
                for profile in room_profiles_by_code.get(room_code, [])
                for department in _normalise_program_set(profile.get("department"))
            }
        )
        room_types = sorted(
            {
                profile["room_type"]
                for room_code in section_room_codes
                for profile in room_profiles_by_code.get(room_code, [])
                if profile.get("room_type")
            }
        )
        room_capacities = sorted(
            {
                _int_or_zero(profile.get("capacity"))
                for room_code in section_room_codes
                for profile in room_profiles_by_code.get(room_code, [])
                if _int_or_zero(profile.get("capacity")) > 0
            }
        )
        course_code = _display_course_code(section.course_code or section.course_key)
        section_policies[section.id] = RepairSectionPolicy(
            term_section_id=section.id,
            course_key=str(section.course_key or section.course_code or "").strip(),
            course_code=str(section.course_code or section.course_key or "").strip(),
            academic_course_code=normalize_code(course_code),
            section_label=str(section.section or "").strip(),
            section_gender=_section_gender(section.section),
            allowed_programs=tuple(sorted(programs_by_section.get(section.id, set()))),
            board_ids=tuple(sorted(board_ids_by_section.get(section.id, set()))),
            board_labels=tuple(sorted(board_labels_by_section.get(section.id, set()))),
            board_terms=tuple(sorted(board_terms_by_section.get(section.id, set()))),
            visible_board_ids=tuple(sorted(visible_board_ids_by_section.get(section.id, set()))),
            visible_board_labels=tuple(
                sorted(visible_board_labels_by_section.get(section.id, set()))
            ),
            visible_board_terms=tuple(
                sorted(visible_board_terms_by_section.get(section.id, set()))
            ),
            visible_programs=tuple(sorted(visible_programs_by_section.get(section.id, set()))),
            visibility_restricted=section.id in visible_board_ids_by_section,
            placed=bool(placement_counts_by_section.get(section.id)),
            placement_count=int(placement_counts_by_section.get(section.id, 0)),
            online=bool(online_by_section.get(section.id)),
            room_codes=tuple(section_room_codes),
            campus_codes=tuple(campus_codes),
            room_sections=tuple(room_sections),
            room_departments=tuple(room_departments),
            room_types=tuple(room_types),
            room_capacities=tuple(room_capacities),
            required_room_types=tuple(sorted(required_types_by_section.get(section.id, set()))),
            required_capacity=int(required_capacity_by_section.get(section.id, 0)),
            instructors=tuple(sorted(instructor_names_by_section.get(section.id, set()))),
            instructor_conflicts=tuple(instructor_conflicts_by_section.get(section.id, [])),
            source_tag=str(section.source_tag or "").strip(),
            closed=_closed_section_source(section.source_tag),
            locked=section.id in locked_sections,
        )

    protected_assignments: set[tuple[int, int]] = set()
    protected_assignment_reasons: dict[tuple[int, int], str] = {}
    assignment_rows = StudentTermSection.objects.filter(
        student_id__in=student_ids,
        term_section__scenario_id=scenario_id,
    ).values("student_id", "term_section_id", "source")
    for row in assignment_rows:
        source_reason = _protected_source_reason(str(row.get("source") or ""))
        if source_reason:
            key = (int(row["student_id"]), int(row["term_section_id"]))
            protected_assignments.add(key)
            protected_assignment_reasons[key] = source_reason

    passed_by_student: dict[int, set[str]] = defaultdict(set)
    studying_by_student: dict[int, set[str]] = defaultdict(set)
    course_rows = (
        StudentCourse.objects.filter(student_id__in=student_ids)
        .select_related("course")
        .values("student_id", "course__course_code", "status")
    )
    for row in course_rows:
        sid = int(row["student_id"])
        code = normalize_code(row.get("course__course_code"))
        status = str(row.get("status") or "").strip().lower()
        if not code:
            continue
        if status == "passed":
            passed_by_student[sid].add(code)
        elif status == "studying":
            studying_by_student[sid].add(code)

    demand_by_student_course = _build_student_demand_policy_index(
        scenario_id=scenario_id,
        student_ids=student_ids,
    )

    programs = sorted({student.program for student in students.values() if student.program})
    course_codes = sorted(
        {
            _academic_course_code(policy, "")
            for policy in section_policies.values()
            if _academic_course_code(policy, "")
        }
    )
    prereqs_by_program_course: dict[tuple[str, str], tuple[str, ...]] = {}
    if programs and course_codes:
        prereq_rows = Prerequisite.objects.filter(program__in=programs).values_list(
            "program",
            "course_code",
            "prerequisite_course_code",
        )
        accumulator: dict[tuple[str, str], list[str]] = defaultdict(list)
        wanted = set(course_codes)
        for program, course_code, prereq_cell in prereq_rows:
            program_key = _normalise_program(program)
            course_key = normalize_code(course_code)
            if course_key not in wanted:
                continue
            for prereq in str(prereq_cell or "").split(","):
                code = normalize_code(prereq)
                if code:
                    accumulator[(program_key, course_key)].append(code)
        prereqs_by_program_course = {
            key: tuple(dict.fromkeys(values)) for key, values in accumulator.items()
        }

    return RepairEligibilityContext(
        students=students,
        sections=section_policies,
        protected_assignments=protected_assignments,
        protected_assignment_reasons=protected_assignment_reasons,
        passed_by_student=dict(passed_by_student),
        studying_by_student=dict(studying_by_student),
        demand_by_student_course=demand_by_student_course,
        prereqs_by_program_course=prereqs_by_program_course,
    )


def build_repair_eligibility_context_for_section_ids(
    *,
    scenario_id: int,
    student_ids: list[int],
    section_ids: list[int],
) -> RepairEligibilityContext:
    """Load eligibility policy from solver-native section ids."""

    sections = list(
        TermSection.objects.filter(
            scenario_id=scenario_id,
            id__in=[int(section_id) for section_id in section_ids],
        ).order_by("course_key", "section", "id")
    )
    return build_repair_eligibility_context(
        scenario_id=scenario_id,
        student_ids=student_ids,
        sections=sections,
    )


def eligible_repair_section_options(
    context: RepairEligibilityContext,
    *,
    student_id: int,
    course_key: str,
    sections: list[TermSection],
    current_section_id: int | None = None,
    is_new_course: bool = False,
) -> list[TermSection]:
    """Return only the section options that satisfy repair policy."""

    eligible_ids = set(
        eligible_repair_section_ids(
            context,
            student_id=student_id,
            course_key=course_key,
            section_ids=[section.id for section in sections],
            current_section_id=current_section_id,
            is_new_course=is_new_course,
        )
    )
    return [section for section in sections if section.id in eligible_ids]


def eligible_repair_section_ids(
    context: RepairEligibilityContext,
    *,
    student_id: int,
    course_key: str,
    section_ids: list[int],
    current_section_id: int | None = None,
    is_new_course: bool = False,
) -> list[int]:
    """Return eligible section ids without exposing ORM section objects to the solver."""

    eligible: list[int] = []
    for section_id in section_ids:
        section_id = int(section_id)
        reasons = repair_section_id_ineligibility_reasons(
            context,
            student_id=student_id,
            course_key=course_key,
            section_id=section_id,
            current_section_id=current_section_id,
            is_new_course=is_new_course,
        )
        if reasons:
            context.record_rejection_by_section_id(
                student_id=student_id,
                course_key=course_key,
                section_id=section_id,
                reasons=reasons,
            )
            continue
        eligible.append(section_id)
    return eligible


def repair_section_id_ineligibility_reasons(
    context: RepairEligibilityContext,
    *,
    student_id: int,
    course_key: str,
    section_id: int,
    current_section_id: int | None = None,
    is_new_course: bool = False,
) -> list[dict[str, Any]]:
    """Explain why a student cannot use a solver-native section id."""

    if current_section_id and int(section_id) == int(current_section_id):
        return []

    reasons: list[dict[str, Any]] = []
    student = context.students.get(int(student_id))
    policy = context.sections.get(int(section_id))
    if student is None or policy is None:
        return reasons

    if student.protected:
        reasons.append(
            {
                "code": "PROTECTED_STUDENT",
                "message": "Student is protected from automated repair changes",
                "details": {
                    "status": student.status,
                    "reason": student.protection_reason,
                    "priority_group": student.priority_group,
                    "mobility_policy": student.mobility_policy,
                },
            }
        )

    if current_section_id and (student_id, current_section_id) in context.protected_assignments:
        source_reason = context.protected_assignment_reasons.get(
            (student_id, current_section_id), ""
        )
        reasons.append(
            {
                "code": "PROTECTED_ASSIGNMENT",
                "message": "Current assignment source is protected from automated repair changes",
                "details": {"reason": source_reason},
            }
        )

    if policy.locked:
        reasons.append(
            {
                "code": "LOCKED_SECTION",
                "message": "Section placement is locked and cannot receive automated moves",
            }
        )

    if policy.closed:
        reasons.append(
            {
                "code": "CLOSED_SECTION",
                "message": "Section source is marked closed/cancelled/inactive",
                "details": {"source_tag": policy.source_tag},
            }
        )

    if not policy.placed:
        reasons.append(
            {
                "code": "UNPLACED_SECTION",
                "message": "Section has no timetable placement and cannot be used for automated repair",
            }
        )

    reasons.extend(_room_policy_ineligibility(student, policy))
    reasons.extend(_instructor_policy_ineligibility(policy))

    if (
        student.program
        and policy.allowed_programs
        and student.program not in policy.allowed_programs
    ):
        reasons.append(
            {
                "code": "PROGRAM_MISMATCH",
                "message": "Student programme does not match the section board programme",
                "details": {
                    "student_program": student.program,
                    "allowed_programs": list(policy.allowed_programs),
                },
            }
        )

    reasons.extend(_cohort_policy_ineligibility(context, student, policy, course_key))

    if (
        student.section
        and len(policy.room_sections) == 1
        and student.section not in set(policy.room_sections)
    ):
        reasons.append(
            {
                "code": "ROOM_SECTION_MISMATCH",
                "message": "Student section side does not match the assigned room side",
                "details": {
                    "student_section": student.section,
                    "room_sections": list(policy.room_sections),
                    "room_codes": list(policy.room_codes),
                },
            }
        )

    if student.section and policy.section_gender and student.section != policy.section_gender:
        reasons.append(
            {
                "code": "SECTION_GENDER_MISMATCH",
                "message": "Student section side does not match the section label",
                "details": {
                    "student_section": student.section,
                    "section_gender": policy.section_gender,
                },
            }
        )

    if is_new_course:
        reasons.extend(_new_course_ineligibility(context, student, policy, course_key))

    return reasons


def repair_section_ineligibility_reasons(
    context: RepairEligibilityContext,
    *,
    student_id: int,
    course_key: str,
    section: TermSection,
    current_section_id: int | None = None,
    is_new_course: bool = False,
) -> list[dict[str, Any]]:
    """Explain why a student cannot use a section in a repair solution."""

    return repair_section_id_ineligibility_reasons(
        context,
        student_id=student_id,
        course_key=course_key,
        section_id=section.id,
        current_section_id=current_section_id,
        is_new_course=is_new_course,
    )


def repair_eligibility_summary(context: RepairEligibilityContext) -> dict[str, Any]:
    priority_counts = Counter(student.priority_group for student in context.students.values())
    mobility_counts = Counter(student.mobility_policy for student in context.students.values())
    protected_students = [
        {
            "student_id": student.student_id,
            "priority_group": student.priority_group,
            "reason": student.protection_reason,
            "mobility_policy": student.mobility_policy,
        }
        for student in context.students.values()
        if student.protected
    ][:25]
    return {
        "rules": [
            "student_program_matches_section_board_when_known",
            "student_section_side_matches_gendered_section_label_when_known",
            "assigned_room_side_matches_student_section_when_room_inventory_is_unambiguous",
            "assigned_rooms_must_exist_in_room_inventory",
            "campus_policy_uses_explicit_request_campus_when_supplied",
            "online_sections_must_not_consume_physical_rooms",
            "physical_sections_require_room_type_capacity_and_department_compatible_rooms",
            "instructor_conflicts_reject_new_automated_reassignments",
            "manual_board_visibility_restricts_section_cohorts_when_configured",
            "request_primary_term_matches_section_board_term_unless_cross_term",
            "sections_must_have_a_timetable_placement_before_student_reassignment",
            "closed_cancelled_or_inactive_sections_reject_automated_moves",
            "locked_sections_reject_new_automated_moves",
            "protected_students_and_assignments_are_fixed",
            "student_priority_and_mobility_policy_are_classified_for_audit",
            "new_course_prerequisites_and_already_taken_status_are_checked",
        ],
        "priority_group_counts": dict(sorted(priority_counts.items())),
        "mobility_policy_counts": dict(sorted(mobility_counts.items())),
        "protected_student_count": sum(
            1 for student in context.students.values() if student.protected
        ),
        "graduation_priority_count": sum(
            1 for student in context.students.values() if student.graduation_priority
        ),
        "protected_assignment_count": len(context.protected_assignments),
        "protected_students": protected_students,
        "rejected_option_count": context.rejected_option_count,
        "rejection_counts": dict(sorted(context.rejection_counts.items())),
        "samples": context.rejection_samples,
    }


def _cohort_policy_ineligibility(
    context: RepairEligibilityContext,
    student: RepairStudentPolicy,
    policy: RepairSectionPolicy,
    course_key: str,
) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    demand = _demand_policy_for_course(context, student.student_id, course_key, policy)
    primary_term = _int_or_zero((demand or {}).get("primary_term"))
    is_cross_term = bool((demand or {}).get("is_cross_term"))

    if (
        primary_term
        and policy.board_terms
        and not is_cross_term
        and primary_term not in policy.board_terms
    ):
        reasons.append(
            {
                "code": "COHORT_TERM_MISMATCH",
                "message": "Student request primary term does not match the section board term",
                "details": {
                    "primary_term": primary_term,
                    "board_terms": list(policy.board_terms),
                    "course_key": course_key,
                },
            }
        )

    if policy.visibility_restricted:
        visibility_details = {
            "visible_board_ids": list(policy.visible_board_ids),
            "visible_board_labels": list(policy.visible_board_labels),
            "visible_board_terms": list(policy.visible_board_terms),
            "visible_programs": list(policy.visible_programs),
            "placement_board_ids": list(policy.board_ids),
            "placement_board_labels": list(policy.board_labels),
        }
        if policy.board_ids and not (set(policy.board_ids) & set(policy.visible_board_ids)):
            reasons.append(
                {
                    "code": "MANUAL_COHORT_RESTRICTION",
                    "message": "Section placement is outside the manual board visibility whitelist",
                    "details": visibility_details,
                }
            )
        elif (
            student.program
            and policy.visible_programs
            and student.program not in policy.visible_programs
        ):
            reasons.append(
                {
                    "code": "MANUAL_COHORT_RESTRICTION",
                    "message": "Student programme is outside the manual section visibility whitelist",
                    "details": {**visibility_details, "student_program": student.program},
                }
            )
        elif (
            primary_term
            and not is_cross_term
            and policy.visible_board_terms
            and primary_term not in policy.visible_board_terms
        ):
            reasons.append(
                {
                    "code": "MANUAL_COHORT_RESTRICTION",
                    "message": "Student request term is outside the manual section visibility whitelist",
                    "details": {**visibility_details, "primary_term": primary_term},
                }
            )

    required_campuses = _demand_campus_codes(demand)
    if (
        required_campuses
        and policy.campus_codes
        and not (required_campuses & set(policy.campus_codes))
    ):
        reasons.append(
            {
                "code": "CAMPUS_MISMATCH",
                "message": "Student request campus does not match the assigned room campus",
                "details": {
                    "required_campuses": sorted(required_campuses),
                    "section_campuses": list(policy.campus_codes),
                    "room_codes": list(policy.room_codes),
                },
            }
        )

    return reasons


def _instructor_policy_ineligibility(policy: RepairSectionPolicy) -> list[dict[str, Any]]:
    if not policy.instructor_conflicts:
        return []
    return [
        {
            "code": "INSTRUCTOR_CONFLICT",
            "message": "Section has a known instructor timetable conflict",
            "details": {
                "instructors": list(policy.instructors),
                "conflicts": list(policy.instructor_conflicts[:5]),
                "conflict_count": len(policy.instructor_conflicts),
            },
        }
    ]


def _room_policy_ineligibility(
    student: RepairStudentPolicy,
    policy: RepairSectionPolicy,
) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    if not policy.placed:
        return reasons

    if policy.online:
        if policy.room_codes:
            reasons.append(
                {
                    "code": "ONLINE_SECTION_HAS_PHYSICAL_ROOM",
                    "message": "Online section has a stale physical room assignment",
                    "details": {"room_codes": list(policy.room_codes)},
                }
            )
        return reasons

    if not policy.room_codes:
        reasons.append(
            {
                "code": "PHYSICAL_ROOM_REQUIRED",
                "message": "Physical section must have an assigned room before automated repair",
            }
        )
        return reasons

    if (
        policy.room_codes
        and not policy.room_sections
        and not policy.room_departments
        and not policy.room_types
        and not policy.room_capacities
    ):
        reasons.append(
            {
                "code": "ROOM_INVENTORY_MISSING",
                "message": "Assigned room code is not available in the room inventory",
                "details": {"room_codes": list(policy.room_codes)},
            }
        )

    required_types = set(policy.required_room_types)
    if required_types and policy.room_types and not (required_types & set(policy.room_types)):
        reasons.append(
            {
                "code": "ROOM_TYPE_MISMATCH",
                "message": "Assigned room type does not match the section room requirement",
                "details": {
                    "required_room_types": sorted(required_types),
                    "room_types": list(policy.room_types),
                    "room_codes": list(policy.room_codes),
                },
            }
        )

    max_capacity = max(policy.room_capacities, default=0)
    if policy.required_capacity > 0 and max_capacity and max_capacity < policy.required_capacity:
        reasons.append(
            {
                "code": "ROOM_CAPACITY_MISMATCH",
                "message": "Assigned room capacity is below the section capacity requirement",
                "details": {
                    "required_capacity": policy.required_capacity,
                    "max_room_capacity": max_capacity,
                    "room_codes": list(policy.room_codes),
                },
            }
        )

    if (
        policy.allowed_programs
        and policy.room_departments
        and not (set(policy.allowed_programs) & set(policy.room_departments))
    ):
        reasons.append(
            {
                "code": "ROOM_DEPARTMENT_MISMATCH",
                "message": "Assigned room department does not match the section board programme",
                "details": {
                    "allowed_programs": list(policy.allowed_programs),
                    "room_departments": list(policy.room_departments),
                    "room_codes": list(policy.room_codes),
                    "student_program": student.program,
                },
            }
        )
    return reasons


def _new_course_ineligibility(
    context: RepairEligibilityContext,
    student: RepairStudentPolicy,
    policy: RepairSectionPolicy,
    course_key: str,
) -> list[dict[str, Any]]:
    course_code = _academic_course_code(policy, course_key)
    if not course_code:
        return []

    passed = context.passed_by_student.get(student.student_id, set())
    studying = context.studying_by_student.get(student.student_id, set())
    if course_code in passed or course_code in studying:
        return [
            {
                "code": "COURSE_ALREADY_TAKEN_OR_STUDYING",
                "message": "Student already passed or is studying this course",
            }
        ]

    prereqs = context.prereqs_by_program_course.get((student.program, course_code), ())
    missing: list[str] = []
    effective_credits = student.total_earned_credits + student.current_registered_credits
    for prereq in prereqs:
        hour_match = HOUR_PREREQ_RE.match(prereq)
        if hour_match:
            required_hours = int(hour_match.group(1))
            if effective_credits < required_hours:
                missing.append(prereq)
            continue
        if prereq not in passed and prereq not in studying:
            missing.append(prereq)

    if not missing:
        return []
    return [
        {
            "code": "MISSING_PREREQUISITES",
            "message": "Student does not satisfy course prerequisites",
            "details": {"missing": missing},
        }
    ]


def _build_student_demand_policy_index(
    *,
    scenario_id: int,
    student_ids: list[int],
) -> dict[tuple[int, str], dict[str, Any]]:
    index: dict[tuple[int, str], dict[str, Any]] = {}
    for sid, rows in load_student_course_demand_map(
        scenario_id,
        student_ids=student_ids,
    ).items():
        for demand in rows:
            payload = {
                "student_id": sid,
                "course_key": demand.course_key,
                "course_code": demand.course_code,
                "primary_term": demand.primary_term,
                "is_cross_term": demand.is_cross_term,
                "status": demand.status,
                "priority": demand.priority,
                "reason_blocked": demand.reason_blocked,
                "source": demand.source,
                "source_payload": demand.source_payload or {},
            }
            for raw_key in {demand.course_key, demand.course_code}:
                key = normalize_code(raw_key)
                if key:
                    index.setdefault((int(sid), key), payload)
    return index


def _demand_policy_for_course(
    context: RepairEligibilityContext,
    student_id: int,
    course_key: str,
    policy: RepairSectionPolicy,
) -> dict[str, Any]:
    keys = [
        normalize_code(course_key),
        normalize_code(policy.course_key),
        normalize_code(policy.course_code),
        _academic_course_code(policy, course_key),
    ]
    for key in dict.fromkeys(key for key in keys if key):
        demand = context.demand_by_student_course.get((int(student_id), key))
        if demand:
            return demand
    return {}


def _demand_campus_codes(demand: dict[str, Any] | None) -> set[str]:
    payload = (demand or {}).get("source_payload") or {}
    if not isinstance(payload, dict):
        return set()
    values: list[Any] = []
    for key in (
        "campus",
        "campus_code",
        "required_campus",
        "required_campuses",
        "allowed_campus",
        "allowed_campuses",
        "campuses",
    ):
        raw = payload.get(key)
        if isinstance(raw, list | tuple | set):
            values.extend(raw)
        elif raw not in {None, ""}:
            values.append(raw)
    return {_normalise_campus_code(value) for value in values if _normalise_campus_code(value)}


def _section_instructor_policy(
    *,
    scenario_id: int,
    target_section_ids: set[int],
) -> tuple[dict[int, set[str]], dict[int, list[dict[str, Any]]]]:
    if not target_section_ids:
        return {}, {}

    placements = list(
        SectionPlacement.objects.filter(board__scenario_id=scenario_id).select_related(
            "board",
            "term_section",
        )
    )
    placement_section_ids = {int(row.term_section_id) for row in placements}
    instructor_names_by_section: dict[int, set[str]] = defaultdict(set)
    instructor_norms_by_section: dict[int, dict[str, str]] = defaultdict(dict)
    if placement_section_ids:
        for row in TermSectionMeeting.objects.filter(
            term_section_id__in=placement_section_ids,
        ).values("term_section_id", "instructor"):
            name = str(row.get("instructor") or "").strip()
            norm = normalise_instructor(name)
            if not name or not norm:
                continue
            section_id = int(row["term_section_id"])
            instructor_names_by_section[section_id].add(name)
            instructor_norms_by_section[section_id].setdefault(norm, name)

    # Structured links (per-person, multi-instructor) when the flag is on — kept
    # in lock-step with the planner clash. Per section: links if it has any, else
    # the free-text name. Keys are "id:<n>" for links, the normalised name for
    # free-text; both map to a display name.
    links_on = is_instructor_links_enabled()
    link_identities: dict[int, dict[str, str]] = defaultdict(dict)
    if links_on:
        from core.models import CourseInstructor, TimetableScenario

        scenario = TimetableScenario.objects.filter(pk=scenario_id).first()
        gender = getattr(scenario, "gender", "") if scenario else ""
        if gender:
            programs = list(getattr(scenario, "programs", []) or [])
            # (program, normalised course_code) -> {"id:<n>": name}
            by_course: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
            for prog, code, iid, name in CourseInstructor.objects.filter(
                program__in=programs, section=gender, instructor__is_active=True
            ).values_list("program", "course_code", "instructor_id", "instructor__full_name"):
                by_course[(prog, (code or "").strip().upper())][f"id:{iid}"] = name
            for placement in placements:
                sid = int(placement.term_section_id)
                norm = (placement.term_section.course_code or "").strip().upper()
                for prog in programs:
                    for key, name in by_course.get((prog, norm), {}).items():
                        link_identities[sid][key] = name
                        instructor_names_by_section[sid].add(name)

    by_instructor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for placement in placements:
        sid = int(placement.term_section_id)
        identities = (
            link_identities[sid]
            if (links_on and sid in link_identities)
            else instructor_norms_by_section.get(sid, {})
        )
        for _key, name in identities.items():
            by_instructor[_key].append(
                {
                    "placement_id": placement.id,
                    "section_id": int(placement.term_section_id),
                    "section": (
                        f"{placement.term_section.course_code}-{placement.term_section.section}"
                    ),
                    "board_id": placement.board_id,
                    "board_label": placement.board.label,
                    "day": placement.day,
                    "start_time": placement.start_time,
                    "end_time": placement.end_time,
                    "instructor": name,
                }
            )

    conflicts: dict[int, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[int, int, str, str, str]] = set()
    for rows in by_instructor.values():
        if len(rows) < 2:
            continue
        for left, right in combinations(rows, 2):
            if left["placement_id"] == right["placement_id"]:
                continue
            if not _time_overlap(
                left["day"],
                left["start_time"],
                left["end_time"],
                right["day"],
                right["start_time"],
                right["end_time"],
            ):
                continue
            for current, other in ((left, right), (right, left)):
                section_id = int(current["section_id"])
                if section_id not in target_section_ids:
                    continue
                key = (
                    section_id,
                    int(other["placement_id"]),
                    str(current["day"]),
                    str(current["start_time"]),
                    str(other["start_time"]),
                )
                if key in seen:
                    continue
                seen.add(key)
                conflicts[section_id].append(
                    {
                        "instructor": current["instructor"],
                        "conflicting_placement_id": other["placement_id"],
                        "conflicting_section": other["section"],
                        "conflicting_board_id": other["board_id"],
                        "conflicting_board_label": other["board_label"],
                        "day": current["day"],
                        "start_time": current["start_time"],
                        "end_time": current["end_time"],
                    }
                )
    return dict(instructor_names_by_section), dict(conflicts)


def _protected_status(status: str) -> tuple[bool, str]:
    value = status.strip().lower()
    for marker in PROTECTED_STATUS_MARKERS:
        if marker in value:
            return True, marker
    return False, ""


def classify_repair_student_policy(
    *,
    status: str = "",
    total_earned_credits: int = 0,
    current_registered_credits: int = 0,
) -> tuple[str, bool, bool, str]:
    """Classify student priority and mobility from the current schema."""

    value = str(status or "").strip().lower()
    protected, reason = _protected_status(value)
    if protected:
        if "manual" in reason:
            return "manual_approval", False, True, reason
        if "special" in reason:
            return "special_case", False, True, reason
        return "cannot_move", False, True, reason
    graduation_priority = any(marker in value for marker in GRADUATION_STATUS_MARKERS)
    if graduation_priority:
        return "graduating", True, False, ""
    # Credit totals are retained as evidence; no graduation threshold is inferred.
    _ = total_earned_credits + current_registered_credits
    return "normal", False, False, ""


def _protected_source_reason(source: str) -> str:
    value = source.strip().lower()
    for marker in PROTECTED_SOURCE_MARKERS:
        if marker in value:
            return marker
    return ""


def _closed_section_source(source_tag: object | None) -> bool:
    value = str(source_tag or "").strip().lower()
    return any(marker in value for marker in CLOSED_SECTION_SOURCE_MARKERS)


def _academic_course_code(policy: RepairSectionPolicy, fallback: object | None) -> str:
    return normalize_code(
        policy.academic_course_code
        or _display_course_code(policy.course_code)
        or _display_course_code(policy.course_key)
        or _display_course_code(fallback)
    )


def _display_course_code(value: object | None) -> str:
    return str(value or "").split("::", 1)[0].strip()


def _section_gender(value: object | None) -> str:
    label = str(value or "").strip().upper()
    return label[:1] if label[:1] in SECTION_GENDERS else ""


def _normalise_gender(value: object | None) -> str:
    raw = str(value or "").strip().upper()
    return raw[:1] if raw[:1] in SECTION_GENDERS else ""


def _normalise_program(value: object | None) -> str:
    return str(value or "").strip().upper()


def _normalise_program_set(value: object | None) -> set[str]:
    return {
        _normalise_program(part)
        for part in str(value or "").replace(";", ",").split(",")
        if _normalise_program(part)
    }


def _normalise_room_code(value: object | None) -> str:
    room = str(value or "").strip()
    return "" if room.upper() in {"", "UNASSIGNED"} else room


def _normalise_room_type(value: object | None) -> str:
    room_type = str(value or "lecture").strip().lower()
    return room_type or "lecture"


def _normalise_campus_code(value: object | None) -> str:
    return str(value or "").strip().upper()


def _time_overlap(
    day_a: object,
    start_a: object,
    end_a: object,
    day_b: object,
    start_b: object,
    end_b: object,
) -> bool:
    if str(day_a or "").strip().upper() != str(day_b or "").strip().upper():
        return False
    try:
        a_start = _time_to_minutes(start_a)
        a_end = _time_to_minutes(end_a)
        b_start = _time_to_minutes(start_b)
        b_end = _time_to_minutes(end_b)
    except ValueError:
        return False
    return a_start < b_end and a_end > b_start


def _time_to_minutes(value: object) -> int:
    raw = str(value or "").strip()
    hour, minute = raw.split(":", 1)
    return int(hour) * 60 + int(minute)


def _int_or_zero(value: object | None) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0

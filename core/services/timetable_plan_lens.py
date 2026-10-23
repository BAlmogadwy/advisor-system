from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.models import ScenarioSectionBudget, Student, TermSection
from core.services import timetable_student_assignment as ssa
from core.services.timetable_assignment_models import StudentAssignmentState
from core.services.timetable_demand import StudentCourseDemand, load_scenario_course_demands
from core.services.timetable_flags import is_section_programme_preference_enabled
from core.services.timetable_optimizer_v2 import (
    build_course_rigidity_for_scenario,
    build_section_states_for_scenario,
    build_student_profiles_for_scenario,
)

logger = logging.getLogger(__name__)

PREFERRED_PLAN_ORDER = ("AI", "DS", "AI2", "DS2", "CS", "CS2", "IS", "IS2", "CYB", "COE")
SHARED_OWNER = "SHARED"
#: Why a seating has no evidence: it could not be evaluated (``seating.reason``).
SEATING_FAILED = "assessment_failed"


def _clean_program(value: object | None) -> str:
    text = str(value or "").strip().upper()
    return text or "UNKNOWN"


def _clean_course_key(value: object | None) -> str:
    return str(value or "").replace("\u00a0", " ").strip()


def _section_sort_key(section: object | None) -> tuple[int, str]:
    text = str(section or "")
    match = re.search(r"\d+", text)
    return (int(match.group(0)) if match else 9999, text)


def _ordered_plans(plans: set[str]) -> list[str]:
    preferred = [plan for plan in PREFERRED_PLAN_ORDER if plan in plans]
    remaining = sorted(plan for plan in plans if plan not in PREFERRED_PLAN_ORDER)
    return preferred + remaining


def _student_program_map(student_ids: list[int]) -> dict[int, str]:
    rows = Student.objects.filter(student_id__in=student_ids).values_list("student_id", "program")
    return {int(student_id): _clean_program(program) for student_id, program in rows}


def _demand_by_course_and_plan(
    demands: list[StudentCourseDemand],
) -> tuple[dict[str, Counter[str]], set[str]]:
    student_ids = sorted({int(row.student_id) for row in demands})
    programs = _student_program_map(student_ids)
    by_course: dict[str, Counter[str]] = defaultdict(Counter)
    plans: set[str] = set()

    for row in demands:
        program = programs.get(int(row.student_id), "UNKNOWN")
        plans.add(program)
        course_key = _clean_course_key(row.course_key)
        if course_key:
            by_course[course_key][program] += 1

    return dict(by_course), plans


@dataclass(frozen=True)
class SeatingEvidence:
    """One seating of the scenario's students, as the lens reads it.

    ``counts`` holds, per ``(course_key, section label)``, how many seated
    students each programme has there; ``grouping`` is what the "mainly for
    programme" grouping did (empty when nothing is marked). ``assessed`` false
    means the seating could not be evaluated (``reason`` says why): its empty
    counts are unknown, never "nobody sits here". ``students`` counts the
    distinct students seated in at least one section.
    """

    assessed: bool
    students: int
    counts: dict[tuple[str, str], Counter[str]] = field(default_factory=dict)
    grouping: dict = field(default_factory=dict)
    reason: str | None = None

    def payload(self) -> dict[str, Any]:
        """The lens's ``seating``: ``{assessed, students[, reason]}``."""
        out: dict[str, Any] = {"assessed": self.assessed, "students": self.students}
        if self.reason:
            out["reason"] = self.reason
        return out


def seating_evidence(
    states: Mapping[str, StudentAssignmentState], *, grouping: dict
) -> SeatingEvidence:
    """The only derivation of the lens's evidence from a seating.

    Read from the seating's own ``assignment_states`` (never from the section
    states a seating mutated): each section id ``{course_key}_{label}`` a
    student is seated in adds one to that student's programme there. Ids that
    are not numbers name no roster student and are left out, as they always
    were; a roster id with no student row counts as ``UNKNOWN``.
    """
    seated = {
        int(student_id): [section_id for section_id in state.section_ids if "_" in section_id]
        for student_id, state in states.items()
        if str(student_id).isdigit()
    }
    seated = {student_id: sections for student_id, sections in seated.items() if sections}
    programs = _student_program_map(sorted(seated))
    counts: dict[tuple[str, str], Counter[str]] = {}
    for student_id, section_ids in seated.items():
        programme = programs.get(student_id, "UNKNOWN")
        for section_id in section_ids:
            course_key, section_label = section_id.rsplit("_", 1)
            counts.setdefault((course_key, section_label), Counter())[programme] += 1
    return SeatingEvidence(
        assessed=True, students=len(seated), counts=counts, grouping=dict(grouping)
    )


def _section_assignment_evidence(scenario_id: int) -> SeatingEvidence:
    """Seat the scenario's students once, read-only, for the lens.

    It runs the same assignment evaluator the optimiser uses and persists
    nothing. The states carry any "mainly for programme" mark (under the
    flag), so the counts are the grouped seating, and ``grouping`` says what
    the grouping did. No demand or no placed section: nobody to seat, which is
    assessed (0 students). A failure is logged and reads as not assessed.
    """
    try:
        profiles = build_student_profiles_for_scenario(scenario_id)
        sections = build_section_states_for_scenario(scenario_id)
        if not profiles or not sections:
            return SeatingEvidence(assessed=True, students=0)
        sections_by_id = ssa.build_sections_by_id(sections)
        sections_by_course = ssa.build_sections_by_course(sections_by_id)
        rigidity = build_course_rigidity_for_scenario(scenario_id)
        # Thread the tier map so this reconstruction seats students the SAME way
        # the optimiser did. Seating is tier-aware under the flag, so a
        # tier-blind rerun here would model a board that was never built.
        from core.services.timetable_flags import is_tiered_objective_enabled
        from core.services.timetable_optimizer_v2 import build_course_tier_map_for_scenario

        course_tiers = (
            build_course_tier_map_for_scenario(scenario_id)
            if is_tiered_objective_enabled()
            else None
        )
        grouping: dict = {}
        states, _unresolved = ssa.assign_students_to_sections(
            profiles,
            sections_by_id,
            sections_by_course,
            rigidity,
            course_tiers=course_tiers,
            grouping_report=grouping,
        )
        return seating_evidence(states, grouping=grouping)
    except Exception:
        logger.exception(
            "Could not seat the students of scenario %s for the plan lens", scenario_id
        )
        return SeatingEvidence(assessed=False, students=0, reason=SEATING_FAILED)


def _allocation_targets(
    plan_counts: Counter[str],
    *,
    planned_sections: int,
    max_per_section: int,
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, int], list[str]]:
    active = Counter({plan: count for plan, count in plan_counts.items() if count > 0})
    if not active:
        return [], {}, {}, []

    max_per_section = max(1, int(max_per_section or 1))
    planned_sections = max(1, int(planned_sections or 1))

    if len(active) == 1:
        owner = next(iter(active))
        targets = [
            {"owner": owner, "role": "core", "contributors": [owner]}
            for _ in range(planned_sections)
        ]
        return (
            targets,
            {owner: planned_sections, "shared": 0},
            {owner: active[owner] % max_per_section},
            [],
        )

    full_sections = {plan: active[plan] // max_per_section for plan in active}
    targets: list[dict[str, Any]] = []
    for plan, section_count in sorted(
        full_sections.items(),
        key=lambda item: (-item[1], -active[item[0]], item[0]),
    ):
        for _ in range(section_count):
            targets.append({"owner": plan, "role": "core", "contributors": [plan]})

    remainders = {
        plan: active[plan] % max_per_section for plan in active if active[plan] % max_per_section
    }
    remaining_slots = max(0, planned_sections - len(targets))
    contributors = [
        plan for plan, _count in sorted(remainders.items(), key=lambda item: (-item[1], item[0]))
    ]
    if remaining_slots and not contributors:
        contributors = [plan for plan, _count in active.most_common()]

    for _ in range(remaining_slots):
        if len(contributors) == 1:
            owner = contributors[0]
            targets.append({"owner": owner, "role": "overflow", "contributors": [owner]})
        else:
            targets.append({"owner": SHARED_OWNER, "role": "shared", "contributors": contributors})

    if len(targets) > planned_sections:
        targets = sorted(
            targets,
            key=lambda item: (
                item["role"] != "core",
                -active.get(item["owner"], 0),
                item["owner"],
            ),
        )[:planned_sections]

    allocation: dict[str, int] = {plan: 0 for plan in active}
    allocation["shared"] = 0
    for target in targets:
        if target["owner"] == SHARED_OWNER:
            allocation["shared"] += 1
        else:
            allocation[target["owner"]] = allocation.get(target["owner"], 0) + 1

    return targets, allocation, remainders, contributors


def _assign_targets_to_sections(
    course_key: str,
    sections: list[TermSection],
    targets: list[dict[str, Any]],
    evidence: dict[tuple[str, str], Counter[str]],
) -> dict[int, dict[str, Any]]:
    remaining = sorted(sections, key=lambda ts: _section_sort_key(ts.section))
    assigned: dict[int, dict[str, Any]] = {}

    for target in targets:
        if not remaining:
            break

        if target["owner"] == SHARED_OWNER:
            contributors = set(target.get("contributors") or [])

            def score_shared(
                ts: TermSection,
                target_contributors: set[str] = contributors,
            ) -> tuple[int, int, int, tuple[int, str]]:
                counts = evidence.get((course_key, ts.section), Counter())
                contributor_total = sum(counts.get(plan, 0) for plan in target_contributors)
                diversity = sum(1 for plan in target_contributors if counts.get(plan, 0) > 0)
                total = sum(counts.values())
                order_num, _order_text = _section_sort_key(ts.section)
                return (contributor_total, diversity, total, (-order_num, ""))

            chosen = max(remaining, key=score_shared)
        else:
            owner = str(target["owner"])

            def score_owner(
                ts: TermSection,
                target_owner: str = owner,
            ) -> tuple[int, int, tuple[int, str]]:
                counts = evidence.get((course_key, ts.section), Counter())
                total = sum(counts.values())
                order_num, _order_text = _section_sort_key(ts.section)
                return (counts.get(target_owner, 0), total, (-order_num, ""))

            chosen = max(remaining, key=score_owner)

        remaining.remove(chosen)
        assigned[chosen.id] = target

    for section in remaining:
        assigned[section.id] = {"owner": "UNALLOCATED", "role": "extra", "contributors": []}

    return assigned


def build_scenario_plan_lens(
    scenario_id: int,
    *,
    seating: SeatingEvidence | None = None,
    demands: list[StudentCourseDemand] | None = None,
) -> dict[str, Any]:
    """Build a read-only programme/plan lens over one shared scenario pool.

    ``seating``: a seating already made for this data (the quality read's), so
    the lens does not seat again; without it the lens seats on its own.
    ``demands``: this scenario's ``load_scenario_course_demands`` rows, when
    the caller has read them already. The payload ends with ``seating``
    (``SeatingEvidence.payload``).
    """
    if demands is None:
        demands = load_scenario_course_demands(scenario_id)
    demand_by_course, observed_plans = _demand_by_course_and_plan(demands)
    plans = _ordered_plans(observed_plans)

    budgets = {
        (budget.course_key or budget.course_code): budget
        for budget in ScenarioSectionBudget.objects.filter(scenario_id=scenario_id)
    }
    sections_by_course: dict[str, list[TermSection]] = defaultdict(list)
    for section in TermSection.objects.filter(scenario_id=scenario_id).order_by(
        "course_key", "section"
    ):
        sections_by_course[section.course_key or section.course_code].append(section)

    # "Mainly for programme": with the flag on, each section says its mark and the
    # lens says what the grouping did; off, the payload is exactly as before.
    marks_on = is_section_programme_preference_enabled()
    if seating is None:
        seating = _section_assignment_evidence(scenario_id)
    evidence = seating.counts
    grouping = seating.grouping
    course_keys = sorted(
        set(budgets) | set(demand_by_course) | set(sections_by_course),
        key=lambda key: (
            budgets[key].programme_term
            if key in budgets and budgets[key].programme_term is not None
            else 999,
            budgets[key].course_code if key in budgets else key,
            key,
        ),
    )

    courses: dict[str, dict[str, Any]] = {}
    sections: dict[str, dict[str, Any]] = {}

    for course_key in course_keys:
        budget = budgets.get(course_key)
        plan_counts = demand_by_course.get(course_key, Counter())
        total = sum(plan_counts.values()) or (budget.total_demand if budget else 0)
        planned_sections = (
            budget.planned_sections if budget else len(sections_by_course.get(course_key, []))
        )
        max_per_section = budget.max_per_section if budget else 40
        targets, allocation, remainders, contributors = _allocation_targets(
            plan_counts,
            planned_sections=planned_sections,
            max_per_section=max_per_section,
        )
        course_sections = sorted(
            sections_by_course.get(course_key, []),
            key=lambda ts: _section_sort_key(ts.section),
        )
        assigned_targets = _assign_targets_to_sections(
            course_key, course_sections, targets, evidence
        )

        course_payload = {
            "course_key": course_key,
            "course_code": budget.course_code if budget else course_key.split("::", 1)[0],
            "course_name": budget.course_name if budget else "",
            "department": budget.department if budget else "",
            "programme_term": budget.programme_term if budget else None,
            "max_per_section": max_per_section,
            "planned_sections": planned_sections,
            "total": total,
            "plans": {plan: plan_counts.get(plan, 0) for plan in plans},
            "allocation": allocation,
            "remainders": remainders,
            "shared": len([plan for plan, count in plan_counts.items() if count > 0]) > 1,
            "shared_overflow": allocation.get("shared", 0) > 0,
            "shared_contributors": contributors,
        }
        courses[course_key] = course_payload

        for section in course_sections:
            target = assigned_targets.get(section.id, {})
            owner = str(target.get("owner") or "UNALLOCATED")
            role = str(target.get("role") or "extra")
            target_contributors = list(target.get("contributors") or [])
            actual_counts = evidence.get((course_key, section.section), Counter())
            if owner == SHARED_OWNER:
                filter_plans = target_contributors
                owner_label = "Shared"
            elif owner in plans:
                filter_plans = [owner]
                owner_label = owner
            else:
                filter_plans = []
                owner_label = "Unallocated"

            sections[str(section.id)] = {
                "term_section_id": section.id,
                "course_key": course_key,
                "course_code": section.course_code,
                "course_name": section.course_name,
                "section": section.section,
                "owner": owner,
                "owner_label": owner_label,
                "role": role,
                "filter_plans": filter_plans,
                "actual_plans": {plan: actual_counts.get(plan, 0) for plan in plans},
                "actual_total": sum(actual_counts.values()),
                "shared": owner == SHARED_OWNER,
                "shared_contributors": target_contributors,
            }
            if marks_on:
                sections[str(section.id)]["preferred_programme"] = section.preferred_programme

    lens: dict[str, Any] = {
        "scenario_id": scenario_id,
        "plans": plans,
        "courses": courses,
        "sections": sections,
        "mode": "read_only_plan_lens",
    }
    if marks_on:
        lens["programme_grouping"] = grouping or None
    lens["seating"] = seating.payload()
    return lens

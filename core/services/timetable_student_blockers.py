"""Course-level actual student blockers for a timetable scenario.

This module is a read-only reporting layer over the same student assignment
evaluator used by the optimiser. It does not introduce another definition of
"student clash"; it groups the evaluator's current unresolved and assigned
clash outcomes into a course work queue for the split workspace inspector.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from core.models import ScenarioSectionBudget, SectionPlacement
from core.services import timetable_student_assignment as ssa
from core.services.timetable_assignment_models import SectionState, StudentAssignmentState
from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
from core.services.timetable_move_outcome import summarise_evaluation
from core.services.timetable_optimizer_v2 import (
    build_course_rigidity_for_scenario,
    build_section_states_for_scenario,
    build_student_profiles_for_scenario,
)


def build_scenario_student_blockers(
    scenario_id: int,
    *,
    sample_limit: int = 8,
) -> dict[str, Any]:
    """Return actual student blockers grouped by course for one scenario."""

    profiles = build_student_profiles_for_scenario(scenario_id)
    sections = build_section_states_for_scenario(scenario_id)
    rigidity = build_course_rigidity_for_scenario(scenario_id)
    if not profiles or not sections:
        return {
            "scenario_id": scenario_id,
            "available": False,
            "reason": "missing_student_profiles_or_sections",
            "summary": _empty_summary(),
            "courses": [],
        }

    result = evaluate_generated_timetable_candidate(
        candidate_id=f"scenario_{scenario_id}",
        generated_sections=sections,
        student_profiles=profiles,
        course_rigidity=rigidity,
    )
    sections_by_id = ssa.build_sections_by_id(sections)
    base_summary = summarise_evaluation(result, sections)
    budget_meta = _course_budget_meta(scenario_id)
    placements_by_course = _placements_by_course(scenario_id)
    assigned_clashes = _assigned_clash_courses(result.assignment_states, sections_by_id)

    rows_by_course: dict[str, dict[str, Any]] = {}
    for student_id, state in result.assignment_states.items():
        profile = profiles.get(student_id)
        for course_key, unresolved in state.unresolved_courses.items():
            row = rows_by_course.setdefault(
                course_key,
                _course_row(course_key, budget_meta, placements_by_course),
            )
            row["_student_ids"].add(student_id)
            row["unresolved_course_count"] += 1
            row["reason_counts"][unresolved.reason] += 1
            if len(row["sample_students"]) < sample_limit:
                row["sample_students"].append(_student_sample(student_id, profile))

    for course_key, clash_data in assigned_clashes.items():
        row = rows_by_course.setdefault(
            course_key,
            _course_row(course_key, budget_meta, placements_by_course),
        )
        row["_assigned_clash_student_ids"].update(clash_data["student_ids"])
        row["assigned_clash_pair_count"] += clash_data["pair_count"]

    rows = []
    all_issue_students: set[str] = set()
    assigned_clash_students: set[str] = set()
    for row in rows_by_course.values():
        student_ids = row.pop("_student_ids")
        assigned_ids = row.pop("_assigned_clash_student_ids")
        issue_ids = set(student_ids) | set(assigned_ids)
        all_issue_students.update(issue_ids)
        assigned_clash_students.update(assigned_ids)
        row["unique_student_count"] = len(student_ids)
        row["assigned_clash_student_count"] = len(assigned_ids)
        row["issue_student_count"] = len(issue_ids)
        row["reason_counts"] = dict(row["reason_counts"])
        rows.append(row)

    rows.sort(
        key=lambda row: (
            -int(row["issue_student_count"]),
            -int(row["unique_student_count"]),
            -int(row["unresolved_course_count"]),
            str(row["course_code"]),
        )
    )

    summary = {
        **base_summary,
        "course_count": len(rows),
        "issue_students": len(all_issue_students),
        "assigned_clash_students": len(assigned_clash_students),
        "blocked_course_requests": base_summary.get("unresolved_courses", 0),
    }
    return {
        "scenario_id": scenario_id,
        "available": True,
        "summary": summary,
        "courses": rows,
    }


def _empty_summary() -> dict[str, int]:
    return {
        "blocked_students": 0,
        "blocked_course_requests": 0,
        "actual_assigned_clashes": 0,
        "assigned_clash_students": 0,
        "all_clash": 0,
        "mixed_blockers": 0,
        "course_count": 0,
    }


def _course_budget_meta(scenario_id: int) -> dict[str, dict[str, Any]]:
    meta: dict[str, dict[str, Any]] = {}
    fields = (
        "course_key",
        "course_code",
        "course_name",
        "department",
        "planned_sections",
        "total_demand",
    )
    for budget in ScenarioSectionBudget.objects.filter(scenario_id=scenario_id).values(*fields):
        course_key = str(budget.get("course_key") or budget.get("course_code") or "").strip()
        if not course_key:
            continue
        meta[course_key] = {
            "course_key": course_key,
            "course_code": budget.get("course_code") or _course_code_from_key(course_key),
            "course_name": budget.get("course_name") or _course_name_from_key(course_key),
            "department": budget.get("department") or "",
            "planned_sections": int(budget.get("planned_sections") or 0),
            "total_demand": int(budget.get("total_demand") or 0),
        }
    return meta


def _placements_by_course(scenario_id: int) -> dict[str, list[dict[str, Any]]]:
    by_course: dict[str, list[dict[str, Any]]] = defaultdict(list)
    placements = (
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .select_related("board", "term_section")
        .order_by(
            "board__display_order", "term_section__course_key", "term_section__section", "day"
        )
    )
    for placement in placements:
        course_key = (
            placement.term_section.course_key
            or placement.term_section.course_code
            or placement.term_section.course_name
        )
        key = str(course_key or "").strip()
        if not key:
            continue
        by_course[key].append(
            {
                "placement_id": placement.id,
                "board_id": placement.board_id,
                "board_label": placement.board.label,
                "section": placement.term_section.section,
                "day": placement.day,
                "start": placement.start_time,
                "end": placement.end_time,
                "room": placement.room or "",
                "is_locked": bool(placement.is_locked),
            }
        )
    return dict(by_course)


def _course_row(
    course_key: str,
    budget_meta: dict[str, dict[str, Any]],
    placements_by_course: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    meta = budget_meta.get(course_key, {})
    placements = placements_by_course.get(course_key, [])
    sections = sorted({str(p.get("section") or "") for p in placements if p.get("section")})
    placement_ids = [p["placement_id"] for p in placements if p.get("placement_id") is not None]
    return {
        "course_key": course_key,
        "course_code": meta.get("course_code") or _course_code_from_key(course_key),
        "course_name": meta.get("course_name") or _course_name_from_key(course_key),
        "department": meta.get("department") or "",
        "planned_sections": meta.get("planned_sections") or len(sections),
        "total_demand": meta.get("total_demand") or 0,
        "section_count": len(sections),
        "sections": sections,
        "placement_ids": placement_ids,
        "placements": placements,
        "unique_student_count": 0,
        "issue_student_count": 0,
        "unresolved_course_count": 0,
        "assigned_clash_student_count": 0,
        "assigned_clash_pair_count": 0,
        "reason_counts": Counter(),
        "sample_students": [],
        "_student_ids": set(),
        "_assigned_clash_student_ids": set(),
    }


def _assigned_clash_courses(
    states: dict[str, StudentAssignmentState],
    sections_by_id: dict[str, SectionState],
) -> dict[str, dict[str, Any]]:
    by_course: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"student_ids": set(), "pair_count": 0}
    )
    for student_id, state in states.items():
        section_ids = list(state.section_ids)
        for idx, first_id in enumerate(section_ids):
            first = sections_by_id.get(first_id)
            if not first:
                continue
            for second_id in section_ids[idx + 1 :]:
                second = sections_by_id.get(second_id)
                if not second or not _sections_overlap(first, second):
                    continue
                for course_key in {first.course_code, second.course_code}:
                    by_course[course_key]["student_ids"].add(student_id)
                    by_course[course_key]["pair_count"] += 1
    return dict(by_course)


def _sections_overlap(first: SectionState, second: SectionState) -> bool:
    for a in first.meetings:
        for b in second.meetings:
            if a.day == b.day and (a.mask & b.mask):
                return True
    return False


def _student_sample(student_id: str, profile: Any) -> dict[str, Any]:
    return {
        "student_id": student_id,
        "program": getattr(profile, "department", "") if profile else "",
        "risk_tier": getattr(getattr(profile, "risk_tier", None), "name", "") if profile else "",
    }


def _course_code_from_key(course_key: str) -> str:
    return str(course_key or "").split("::", 1)[0].strip()


def _course_name_from_key(course_key: str) -> str:
    parts = str(course_key or "").split("::", 1)
    return parts[1].strip() if len(parts) > 1 else ""

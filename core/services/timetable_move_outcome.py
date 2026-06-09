"""Read-only student outcome previews for timetable placement moves.

This module deliberately reuses the optimiser/evaluator pipeline. It should
not grow its own assignment rules; its job is to clone the current timetable,
apply one proposed placement move in memory, and compare evaluator outcomes.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import Any

from core.models import SectionPlacement
from core.services import timetable_student_assignment as ssa
from core.services.timetable_assignment_models import (
    SectionMeeting,
    SectionState,
    StudentAssignmentState,
    TimetableEvaluationResult,
)
from core.services.timetable_autoplace import WEEKDAYS
from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
from core.services.timetable_optimizer_v2 import (
    build_course_rigidity_for_scenario,
    build_section_states_for_scenario,
    build_student_profiles_for_scenario,
)
from core.services.timetable_workspace import _to_minutes, preview_placement_slot_candidates

DAY_INDEX = {day: idx for idx, day in enumerate(WEEKDAYS)}


def preview_placement_student_outcome_candidates(
    placement_id: int,
    *,
    candidate_moves: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return slot candidates enriched with real student reassignment outcome.

    The existing slot preview answers local feasibility questions. This service
    adds the global student outcome by re-running the same evaluator used by the
    optimiser after each candidate move.
    """

    placement = SectionPlacement.objects.select_related("board__scenario", "term_section").get(
        id=placement_id
    )
    scenario_id = placement.board.scenario_id
    quick_preview = preview_placement_slot_candidates(placement_id)
    source_candidates = candidate_source_rows(quick_preview, candidate_moves)

    profiles = build_student_profiles_for_scenario(scenario_id)
    sections = build_section_states_for_scenario(scenario_id)
    rigidity = build_course_rigidity_for_scenario(scenario_id)
    if not profiles or not sections:
        return {
            **quick_preview,
            "student_outcome_available": False,
            "student_outcome_reason": "missing_student_profiles_or_sections",
        }

    baseline = evaluate_generated_timetable_candidate(
        candidate_id="current",
        generated_sections=sections,
        student_profiles=profiles,
        course_rigidity=rigidity,
    )
    baseline_summary = summarise_evaluation(baseline, sections)
    baseline_quality = baseline.quality_score or {}
    placement_ids = {placement.id}
    for row in source_candidates:
        explicit_moves = row.get("moves") if isinstance(row.get("moves"), list) else []
        for move in explicit_moves:
            if isinstance(move, dict) and move.get("placement_id"):
                placement_ids.add(int(move["placement_id"]))
        pair = row.get("pair") if isinstance(row.get("pair"), dict) else None
        pair_id = pair.get("placement_id") if pair else None
        if pair_id:
            placement_ids.add(int(pair_id))
    placements_by_id = {
        item.id: item
        for item in SectionPlacement.objects.filter(id__in=placement_ids).select_related(
            "board__scenario", "term_section"
        )
    }

    enriched = []
    for row in source_candidates:
        explicit_moves = row.get("moves") if isinstance(row.get("moves"), list) else []
        moves = [
            {
                "placement_id": int(move["placement_id"]),
                "day": str(move.get("day") or row["day"]),
                "start": str(move.get("start") or row["start"]),
                "end": str(move.get("end") or row["end"]),
            }
            for move in explicit_moves
            if isinstance(move, dict)
            and move.get("placement_id")
            and move.get("start")
            and move.get("end")
        ]
        if not moves:
            moves = [
                {
                    "placement_id": placement.id,
                    "day": str(row["day"]),
                    "start": str(row["start"]),
                    "end": str(row["end"]),
                }
            ]
            pair = row.get("pair") if isinstance(row.get("pair"), dict) else None
            if pair and pair.get("placement_id"):
                moves.append(
                    {
                        "placement_id": int(pair["placement_id"]),
                        "day": str(pair.get("day") or row["day"]),
                        "start": str(pair["start"]),
                        "end": str(pair["end"]),
                    }
                )
        candidate_sections = apply_candidate_moves(
            sections,
            placements_by_id,
            moves,
        )
        candidate_eval = evaluate_generated_timetable_candidate(
            candidate_id=f"move_{row['day']}_{row['start']}",
            generated_sections=candidate_sections,
            student_profiles=profiles,
            course_rigidity=rigidity,
        )
        outcome = compare_evaluations(
            baseline=baseline,
            candidate=candidate_eval,
            baseline_sections=sections,
            candidate_sections=candidate_sections,
        )
        enriched.append(
            {
                **row,
                "student_outcome": outcome,
                "student_outcome_tone": outcome_tone(outcome),
                "student_outcome_loaded": True,
                "timetable_quality": candidate_eval.quality_score,
                "timetable_quality_delta": int(
                    (candidate_eval.quality_score or {}).get("penalty") or 0
                )
                - int(baseline_quality.get("penalty") or 0),
            }
        )

    enriched.sort(key=lambda row: _candidate_sort_key(row))
    for idx, row in enumerate(enriched, start=1):
        row["rank"] = idx
        row["badge"] = outcome_badge(row.get("student_outcome") or {})

    return {
        **quick_preview,
        "student_outcome_available": True,
        "baseline_outcome": baseline_summary,
        "baseline_timetable_quality": baseline_quality,
        "candidates": enriched,
    }


def apply_candidate_move(
    sections: list[SectionState],
    placement: SectionPlacement,
    *,
    day: str,
    start: str,
    end: str,
) -> list[SectionState]:
    """Return a new section list with one placement meeting moved."""

    section_id = _section_id_for_placement(placement)
    new_meeting = SectionMeeting(
        day=DAY_INDEX[str(day).strip().upper()],
        start_min=_to_minutes(start),
        end_min=_to_minutes(end),
    )
    old_day = DAY_INDEX[str(placement.day).strip().upper()]
    old_start = _to_minutes(placement.start_time)
    old_end = _to_minutes(placement.end_time)

    moved_sections: list[SectionState] = []
    for section in sections:
        if section.section_id != section_id:
            moved_sections.append(section)
            continue

        meetings = list(section.meetings)
        target_idx = _matching_meeting_index(meetings, old_day, old_start, old_end)
        if target_idx is None:
            target_idx = _closest_duration_meeting_index(meetings, old_end - old_start)
        if target_idx is None:
            moved_sections.append(section)
            continue
        meetings[target_idx] = new_meeting
        moved_sections.append(replace(section, meetings=meetings))

    return moved_sections


def apply_candidate_moves(
    sections: list[SectionState],
    placements_by_id: dict[int, SectionPlacement],
    moves: list[dict[str, Any]],
) -> list[SectionState]:
    """Return a new section list with one or more placement meetings moved."""

    moved_sections = sections
    for move in moves:
        placement = placements_by_id.get(int(move["placement_id"]))
        if not placement:
            continue
        moved_sections = apply_candidate_move(
            moved_sections,
            placement,
            day=str(move["day"]),
            start=str(move["start"]),
            end=str(move["end"]),
        )
    return moved_sections


def candidate_source_rows(
    quick_preview: dict[str, Any],
    candidate_moves: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    quick_rows = list(quick_preview.get("candidates", []))
    if not candidate_moves:
        return quick_rows

    quick_by_key = {
        _candidate_key(row): row for row in quick_rows if row.get("day") and row.get("start")
    }
    rows: list[dict[str, Any]] = []
    for move in candidate_moves:
        if not isinstance(move, dict) or not move.get("day") or not move.get("start"):
            continue
        key = _candidate_key(move)
        base = quick_by_key.get(key, {})
        row = {
            **base,
            "kind": move.get("kind")
            or base.get("kind")
            or quick_preview.get("placement", {}).get("kind"),
            "day": str(move["day"]),
            "start": str(move["start"]),
            "end": str(move.get("end") or base.get("end") or ""),
        }
        moves = move.get("moves")
        if isinstance(moves, list):
            row["moves"] = [
                {
                    "placement_id": int(item["placement_id"]),
                    "day": str(item.get("day") or move["day"]),
                    "start": str(item["start"]),
                    "end": str(item["end"]),
                    "kind": str(item.get("kind") or ""),
                }
                for item in moves
                if isinstance(item, dict)
                and item.get("placement_id")
                and item.get("start")
                and item.get("end")
            ]
            row["move_scope"] = move.get("move_scope") or ""
        pair = move.get("pair")
        if (
            isinstance(pair, dict)
            and pair.get("placement_id")
            and pair.get("start")
            and pair.get("end")
        ):
            base_pair = base.get("pair") if isinstance(base.get("pair"), dict) else {}
            row["pair"] = {
                **base_pair,
                "placement_id": int(pair["placement_id"]),
                "day": str(pair.get("day") or move["day"]),
                "start": str(pair["start"]),
                "end": str(pair["end"]),
            }
        rows.append(row)
    return rows or quick_rows


def summarise_evaluation(
    result: TimetableEvaluationResult,
    sections: list[SectionState],
) -> dict[str, Any]:
    """Return the stable outcome fields the UI needs."""

    score = list(result.lexicographic_score)
    reason_counts = unresolved_reason_counts(result)
    sections_by_id = ssa.build_sections_by_id(sections)
    per_student = student_statuses(result, sections_by_id)
    return {
        "score": score,
        "quality_penalty": int((result.quality_score or {}).get("penalty") or 0),
        "quality_components": (result.quality_score or {}).get("components", {}),
        "unresolved_tier_a": score[0],
        "blocked_students": score[1],
        "unresolved_courses": score[2],
        "actual_assigned_clashes": score[3],
        "gap_minutes": score[4],
        "reserve_used": score[5],
        "all_clash": reason_counts.get("all_clash", 0),
        "mixed_blockers": reason_counts.get("mixed_blockers", 0),
        "reason_counts": dict(reason_counts),
        "students_with_assigned_clashes": sum(
            1 for status in per_student.values() if status["clashes"] > 0
        ),
        "top_unresolved_courses": top_unresolved_courses(result),
    }


def compare_evaluations(
    *,
    baseline: TimetableEvaluationResult,
    candidate: TimetableEvaluationResult,
    baseline_sections: list[SectionState],
    candidate_sections: list[SectionState],
) -> dict[str, Any]:
    """Compare candidate outcome against the current timetable outcome."""

    before = summarise_evaluation(baseline, baseline_sections)
    after = summarise_evaluation(candidate, candidate_sections)
    before_status = student_statuses(baseline, ssa.build_sections_by_id(baseline_sections))
    after_status = student_statuses(candidate, ssa.build_sections_by_id(candidate_sections))
    improved = 0
    worsened = 0
    newly_unblocked = 0
    newly_blocked = 0
    for student_id in set(before_status) | set(after_status):
        b = before_status.get(student_id, {"unresolved": 0, "clashes": 0})
        a = after_status.get(student_id, {"unresolved": 0, "clashes": 0})
        b_tuple = (b["unresolved"], b["clashes"])
        a_tuple = (a["unresolved"], a["clashes"])
        if a_tuple < b_tuple:
            improved += 1
        elif a_tuple > b_tuple:
            worsened += 1
        if b["unresolved"] > 0 and a["unresolved"] == 0:
            newly_unblocked += 1
        elif b["unresolved"] == 0 and a["unresolved"] > 0:
            newly_blocked += 1

    return {
        "before": before,
        "after": after,
        "blocked_students_delta": after["blocked_students"] - before["blocked_students"],
        "unresolved_course_delta": after["unresolved_courses"] - before["unresolved_courses"],
        "actual_clash_delta": after["actual_assigned_clashes"] - before["actual_assigned_clashes"],
        "all_clash_delta": after["all_clash"] - before["all_clash"],
        "mixed_blockers_delta": after["mixed_blockers"] - before["mixed_blockers"],
        "gap_minutes_delta": after["gap_minutes"] - before["gap_minutes"],
        "reserve_used_delta": after["reserve_used"] - before["reserve_used"],
        "improved_student_count": improved,
        "worsened_student_count": worsened,
        "newly_unblocked_student_count": newly_unblocked,
        "newly_blocked_student_count": newly_blocked,
        "score_delta": [a - b for a, b in zip(after["score"], before["score"], strict=False)],
    }


def unresolved_reason_counts(result: TimetableEvaluationResult) -> Counter[str]:
    counts: Counter[str] = Counter()
    for state in result.assignment_states.values():
        for unresolved in state.unresolved_courses.values():
            counts[unresolved.reason] += 1
    return counts


def top_unresolved_courses(
    result: TimetableEvaluationResult,
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for state in result.assignment_states.values():
        for course_code in state.unresolved_courses:
            counts[course_code] += 1
    return [{"course": course, "count": count} for course, count in counts.most_common(limit)]


def student_statuses(
    result: TimetableEvaluationResult,
    sections_by_id: dict[str, SectionState],
) -> dict[str, dict[str, int]]:
    statuses: dict[str, dict[str, int]] = {}
    for student_id, state in result.assignment_states.items():
        statuses[student_id] = {
            "unresolved": len(state.unresolved_courses),
            "clashes": assigned_clash_count(state, sections_by_id),
        }
    return statuses


def assigned_clash_count(
    state: StudentAssignmentState,
    sections_by_id: dict[str, SectionState],
) -> int:
    meetings: list[SectionMeeting] = []
    for section_id in state.section_ids:
        section = sections_by_id.get(section_id)
        if section:
            meetings.extend(section.meetings)
    clashes = 0
    for idx, first in enumerate(meetings):
        for second in meetings[idx + 1 :]:
            if first.day == second.day and (first.mask & second.mask):
                clashes += 1
    return clashes


def outcome_tone(outcome: dict[str, Any]) -> str:
    before_hard = tuple(outcome["before"]["score"][:4])
    after_hard = tuple(outcome["after"]["score"][:4])
    if after_hard < before_hard:
        return "improves"
    if after_hard > before_hard:
        return "worsens"
    return "stable"


def outcome_badge(outcome: dict[str, Any]) -> str:
    if not outcome:
        return "Pending"
    blocked_delta = int(outcome.get("blocked_students_delta") or 0)
    unresolved_delta = int(outcome.get("unresolved_course_delta") or 0)
    clash_delta = int(outcome.get("actual_clash_delta") or 0)
    if clash_delta > 0:
        return f"+{clash_delta} clash"
    if unresolved_delta < 0:
        return f"{abs(unresolved_delta)} unblocked"
    if blocked_delta < 0:
        return f"{abs(blocked_delta)} helped"
    if unresolved_delta > 0:
        return f"+{unresolved_delta} blocked"
    if blocked_delta > 0:
        return f"+{blocked_delta} students"
    return "No change"


def _candidate_sort_key(row: dict[str, Any]) -> tuple:
    outcome = row.get("student_outcome") or {}
    after = outcome.get("after") or {}
    score = after.get("score") or [999999] * 6
    return (
        tuple(score),
        int((row.get("timetable_quality") or {}).get("penalty") or 0),
        int(row.get("critical_count") or row.get("critical") or 0),
        int(row.get("warning_count") or row.get("warning") or 0),
        int(row.get("impact_score") or row.get("score") or 0),
        WEEKDAYS.index(str(row["day"])) if str(row.get("day")) in WEEKDAYS else 99,
        _to_minutes(str(row.get("start") or "00:00")),
    )


def _candidate_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("kind") or ""),
        str(row.get("day") or ""),
        str(row.get("start") or ""),
    )


def _section_id_for_placement(placement: SectionPlacement) -> str:
    course_code = placement.term_section.course_key or placement.term_section.course_code
    return f"{course_code}_{placement.term_section.section}"


def _matching_meeting_index(
    meetings: list[SectionMeeting],
    day: int,
    start: int,
    end: int,
) -> int | None:
    for idx, meeting in enumerate(meetings):
        if meeting.day == day and meeting.start_min == start and meeting.end_min == end:
            return idx
    return None


def _closest_duration_meeting_index(
    meetings: list[SectionMeeting],
    duration: int,
) -> int | None:
    matches = [
        (abs((meeting.end_min - meeting.start_min) - duration), idx)
        for idx, meeting in enumerate(meetings)
    ]
    if not matches:
        return None
    matches.sort()
    return matches[0][1]

"""Persist seat/room recovery atomically after the instructor improvement passes."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

from django.conf import settings
from django.db import transaction

from core.models import SectionPlacement, TermSectionMeeting
from core.services.timetable_assignment_models import SectionState, TimetableEvaluationResult
from core.services.timetable_board_persistence import external_shared_section_ids
from core.services.timetable_optimisation_policy import (
    current_day_off_policy,
    instructor_schedule_regressions,
    is_instructor_priority_enabled,
    schedule_metrics_of,
)
from core.services.timetable_secondary_recovery import (
    recover_secondary_quality,
    soft_unresolved_count,
)


def _signatures(sections: Iterable[SectionState]) -> dict[str, Counter[int]]:
    return {
        section.section_id: Counter(m.end_min - m.start_min for m in section.meetings)
        for section in sections
    }


@transaction.atomic
def _persist_exact_room_result(
    scenario_id: int, room_result: Mapping[str, Any], original_rows: Sequence[Mapping[str, Any]]
) -> int:
    """Retain row identities and metadata when swapping unique slot keys."""
    candidate_slots = room_result["candidate_placements_by_id"]
    assignments = room_result["assignment_by_placement_id"]
    originals = {row["id"]: row for row in original_rows}
    if set(candidate_slots) != set(assignments) or not set(assignments).issubset(originals):
        raise ValueError("Recovery room assignment does not match the original placements")
    changed = []
    external_ids = external_shared_section_ids(scenario_id)
    for pk, (day, start, end) in candidate_slots.items():
        row = originals[pk]
        replacement = {
            **row,
            "day": day,
            "start_time": start,
            "end_time": end,
            "room": assignments[pk] or "UNASSIGNED",
        }
        if all(
            replacement[field] == row[field] for field in ("day", "start_time", "end_time", "room")
        ):
            continue
        same_slot = (day, start, end) == (row["day"], row["start_time"], row["end_time"])
        unroomed_as_before = not assignments[pk] and row["room"] in {"", "UNASSIGNED"}
        if row["term_section_id"] in external_ids:
            # Another scenario owns this meeting: its time and room are frozen,
            # though an empty room keeps its own representation.
            if same_slot and unroomed_as_before:
                continue
            raise ValueError("Recovery attempted to alter an externally owned placement")
        if row["is_locked"]:
            # A lock freezes the meeting's time; only its room may change.
            if not same_slot:
                raise ValueError("Recovery attempted to move a locked placement's time")
            if unroomed_as_before:
                continue
        changed.append(replacement)
    if changed:
        # A delete/recreate with the same PK still SET_NULLs repair-run and
        # global-plan references. Vacate the unique slot keys without deleting
        # rows, then write final keys in this savepoint. Audit and lock fields
        # remain untouched by both bulk updates.
        from core.services.timetable_exact_rooming import ExactRoomingError

        changed_ids = {row["id"] for row in changed}
        final_keys = {
            (row["board_id"], row["term_section_id"], row["day"], row["start_time"])
            for row in changed
        }
        unchanged_keys = set(
            SectionPlacement.objects.filter(board__scenario_id=scenario_id)
            .exclude(id__in=changed_ids)
            .values_list("board_id", "term_section_id", "day", "start_time")
        )
        if len(final_keys) != len(changed) or final_keys & unchanged_keys:
            raise ExactRoomingError("Recovery would merge distinct placement records")
        temporary_day = "TMP"
        occupied = set(
            SectionPlacement.objects.filter(
                board__scenario_id=scenario_id, day=temporary_day
            ).values_list("board_id", "term_section_id", "start_time")
        )
        occupied.update(
            (board, section, start)
            for board, section, day, start in final_keys
            if day == temporary_day
        )
        temporary_rows = []
        for row in changed:
            start = next(
                (
                    f"{minute // 60:02d}:{minute % 60:02d}"
                    for minute in range(1440)
                    if (
                        row["board_id"],
                        row["term_section_id"],
                        f"{minute // 60:02d}:{minute % 60:02d}",
                    )
                    not in occupied
                ),
                None,
            )
            if start is None:
                raise ExactRoomingError("Recovery cannot reserve a temporary placement key")
            occupied.add((row["board_id"], row["term_section_id"], start))
            temporary_rows.append(
                SectionPlacement(**{**row, "day": temporary_day, "start_time": start})
            )
        SectionPlacement.objects.bulk_update(temporary_rows, ["day", "start_time"])
        replacements = [SectionPlacement(**row) for row in changed]
        SectionPlacement.objects.bulk_update(
            replacements, ["day", "start_time", "end_time", "room"]
        )
    return len(changed)


def _reset_rejected_report(
    report: dict[str, Any], baseline: TimetableEvaluationResult, reason: str
) -> None:
    report["candidate_score_after"] = report["score_after"]
    report["candidate_accepted_moves"] = report["accepted_moves"]
    report["candidate_student_gap_minutes_after"] = report.get("student_gap_minutes_after")
    report.update(
        applied=False,
        stop_reason=reason,
        accepted_moves=[],
        score_after=list(baseline.lexicographic_score),
        soft_unresolved_after=soft_unresolved_count(baseline),
        room_shortages_after=report["room_shortages_before"],
        instructor_after=schedule_metrics_of(baseline),
        student_gap_minutes_after=sum(
            state.total_gap_minutes for state in baseline.assignment_states.values()
        ),
    )


def _sync_recovered_meetings(
    original_rows: Sequence[Mapping[str, Any]],
    original_meetings: Sequence[Mapping[str, Any]],
    room_result: Mapping[str, Any],
    *,
    scenario_id: int,
) -> None:
    """Update only changed physical meetings, preserving their identity/metadata."""
    from core.services.timetable_exact_rooming import ExactRoomingError
    from core.services.timetable_workspace import _to_minutes

    def key(row: Mapping[str, Any]) -> tuple[int, str, int, int]:
        return (
            row["term_section_id"],
            row["day"].strip().upper(),
            _to_minutes(row["start_time"]),
            _to_minutes(row["end_time"]),
        )

    originals = {row["id"]: row for row in original_rows}
    external_ids = external_shared_section_ids(scenario_id)
    changes: dict[tuple[int, str, int, int], tuple[str, str, str, str]] = {}
    for pid, slot in room_result["candidate_placements_by_id"].items():
        row = originals[pid]
        assigned = room_result["assignment_by_placement_id"][pid]
        target = (*slot, assigned or "UNASSIGNED")
        original_slot = (row["day"], row["start_time"], row["end_time"])
        if row["term_section_id"] in external_ids or target == (*original_slot, row["room"]):
            continue
        if row["is_locked"]:
            # Mirror the placement writer: a locked time never moves, and an
            # empty locked room keeps its representation.
            if tuple(slot) != original_slot:
                raise ExactRoomingError("Recovery attempted to move a locked meeting's time")
            if not assigned and row["room"] in {"", "UNASSIGNED"}:
                continue
        source_key = key(row)
        if source_key in changes and changes[source_key] != target:
            raise ExactRoomingError("Shared recovery meeting has inconsistent final slots or rooms")
        changes[source_key] = target
    changed_meetings = []
    matched = set()
    for meeting in original_meetings:
        source_key = key(meeting)
        if source_key not in changes:
            continue
        day, start, end, room = changes[source_key]
        changed_meetings.append(
            {**meeting, "day": day, "start_time": start, "end_time": end, "room": room}
        )
        matched.add(source_key)
    if set(changes) != matched:
        raise ExactRoomingError("Recovery cannot match a changed placement to its original meeting")
    if changed_meetings:
        final_rows = {row["id"]: row for row in original_meetings}
        final_rows.update({row["id"]: row for row in changed_meetings})
        final_keys = [
            (
                row["term_section_id"],
                row["day"],
                row["start_time"],
                row["end_time"],
                row["room"],
                row["instructor"],
            )
            for row in final_rows.values()
        ]
        if len(final_keys) != len(set(final_keys)):
            raise ExactRoomingError("Recovery would merge distinct shared meeting records")
        TermSectionMeeting.objects.filter(id__in=[row["id"] for row in changed_meetings]).delete()
        TermSectionMeeting.objects.bulk_create(
            [TermSectionMeeting(**row) for row in changed_meetings]
        )


def run_secondary_recovery_for_scenario(
    scenario_id: int,
    result: dict,
    student_profiles: dict,
    course_rigidity: dict,
    section_instructor_ids: dict | None,
    course_tiers: dict | None,
) -> dict:
    from django.db import IntegrityError

    from core.services.timetable_exact_rooming import ExactRoomingError

    try:
        return _run_secondary_recovery_for_scenario(
            scenario_id,
            result,
            student_profiles,
            course_rigidity,
            section_instructor_ids,
            course_tiers,
        )
    except (ExactRoomingError, IntegrityError) as exc:
        # All optional writes occur inside the private pass's savepoint. An
        # unavailable oracle cannot discard preceding mandatory hard repairs.
        return {"applied": False, "stop_reason": "recovery_unavailable", "error": str(exc)}


def _run_secondary_recovery_for_scenario(
    scenario_id: int,
    result: dict,
    student_profiles: dict,
    course_rigidity: dict,
    section_instructor_ids: dict | None,
    course_tiers: dict | None,
) -> dict:
    """Keep only persisted, independently remeasured Pareto improvements.

    A local savepoint protects the preceding mandatory repairs and compaction.
    The original row fingerprint also prevents overwriting edits made while the
    bounded, in-memory search is running.
    """
    if not is_instructor_priority_enabled() or not getattr(
        settings, "TIMETABLE_SECONDARY_RECOVERY_ENABLED", True
    ):
        return {"applied": False, "stop_reason": "disabled"}
    from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
    from core.services.timetable_exact_rooming import ExactRoomingError
    from core.services.timetable_optimizer_v2 import (
        _build_pattern_catalog_for_scenario,
        build_locked_section_ids_for_scenario,
        build_section_states_for_scenario,
    )
    from core.services.timetable_recovery_rooming import build_recovery_room_evaluator
    from core.services.timetable_v2_runner import optimiser_safety_regression
    from core.services.timetable_workspace import compute_scenario_safety_summary

    sections = build_section_states_for_scenario(scenario_id)
    if not sections or not student_profiles:
        return {"applied": False, "stop_reason": "no_sections_or_students"}
    original_rows = list(
        SectionPlacement.objects.filter(board__scenario_id=scenario_id).order_by("id").values()
    )
    original_meetings = list(
        TermSectionMeeting.objects.filter(term_section__scenario_id=scenario_id)
        .order_by("id")
        .values()
    )
    baseline_safety = compute_scenario_safety_summary(scenario_id)

    def evaluate(states: list[SectionState]) -> TimetableEvaluationResult:
        return evaluate_generated_timetable_candidate(
            "secondary_persisted",
            states,
            student_profiles,
            course_rigidity,
            section_instructor_ids,
            course_tiers,
        )

    baseline = evaluate(sections)
    try:
        room_evaluator = build_recovery_room_evaluator(scenario_id, sections=sections)
    except ExactRoomingError as exc:
        # Unsupported inventory/physical data prevents this optional pass, but
        # does not invalidate preceding teacher improvements or hard repairs.
        return {"applied": False, "stop_reason": "room_snapshot_unavailable", "error": str(exc)}
    recovered = recover_secondary_quality(
        current_eval=baseline,
        sections_by_id={s.section_id: s for s in sections},
        pattern_catalog=_build_pattern_catalog_for_scenario(scenario_id),
        student_profiles=student_profiles,
        course_rigidity=course_rigidity,
        section_instructor_ids=section_instructor_ids,
        course_tiers=course_tiers,
        locked_section_ids=build_locked_section_ids_for_scenario(scenario_id),
        room_evaluator=room_evaluator,
        room_shortage_target=result.get("baseline_room_shortages"),
        time_limit_seconds=float(getattr(settings, "TIMETABLE_SECONDARY_RECOVERY_SECONDS", 45)),
        max_iterations=int(getattr(settings, "TIMETABLE_SECONDARY_RECOVERY_ITERATIONS", 12)),
    )
    report = cast(dict[str, Any], recovered["report"])
    report["room_shortages_before_search"] = report["room_shortages_before"]
    report["room_shortages_before"] = room_evaluator.original_room_report["original_room_shortages"]
    report["unassigned_room_rows_before"] = int(baseline_safety["physical_unassigned_rooms"])
    report["incompatible_rooms_before"] = room_evaluator.original_room_report[
        "original_incompatible_rooms"
    ]
    if (
        soft_unresolved_count(recovered["eval"]) >= soft_unresolved_count(baseline)
        and int(recovered["room_evaluation"]["unassigned_count"]) >= report["room_shortages_before"]
    ):
        _reset_rejected_report(report, baseline, report["stop_reason"])
        return report

    with transaction.atomic():
        current_rows = list(
            SectionPlacement.objects.select_for_update()
            .filter(board__scenario_id=scenario_id)
            .order_by("id")
            .values()
        )
        current_meetings = list(
            TermSectionMeeting.objects.select_for_update()
            .filter(term_section__scenario_id=scenario_id)
            .order_by("id")
            .values()
        )
        if current_rows != original_rows or current_meetings != original_meetings:
            _reset_rejected_report(report, baseline, "board_changed_during_search")
            return report
        updated = _persist_exact_room_result(
            scenario_id, recovered["room_evaluation"], original_rows
        )
        try:
            _sync_recovered_meetings(
                original_rows,
                original_meetings,
                recovered["room_evaluation"],
                scenario_id=scenario_id,
            )
        except ExactRoomingError as exc:
            transaction.set_rollback(True)
            _reset_rejected_report(report, baseline, "meeting_snapshot_unavailable")
            report["error"] = str(exc)
            return report
        persisted_sections = build_section_states_for_scenario(scenario_id)
        persisted = evaluate(persisted_sections)
        safety = compute_scenario_safety_summary(scenario_id)
        physical_after = build_recovery_room_evaluator(scenario_id).original_room_report
        failures = cast(
            list[dict[str, Any]],
            optimiser_safety_regression(baseline_safety, safety)["regressions"],
        )
        failures += instructor_schedule_regressions(
            schedule_metrics_of(baseline), schedule_metrics_of(persisted)
        )
        day_off = current_day_off_policy()
        if day_off is not None and not day_off.keeps_breaches(persisted, baseline):
            # The run's day-off ratchet: frozen days and idle can still turn a
            # spare day's student gain or an offset idle rise into a breach.
            failures.append(
                {
                    "metric": "instructor_day_off_policy",
                    "label": "Persisted recovery breached the instructor day-off rules",
                }
            )
        if (
            _signatures(sections) != _signatures(persisted_sections)
            or any(
                new > old
                for new, old in zip(
                    persisted.lexicographic_score[:4], baseline.lexicographic_score[:4], strict=True
                )
            )
            or soft_unresolved_count(persisted) > soft_unresolved_count(baseline)
            or physical_after["original_room_shortages"] > report["room_shortages_before"]
            or physical_after["original_incompatible_rooms"] > report["incompatible_rooms_before"]
            or tuple(persisted.lexicographic_score) != tuple(recovered["eval"].lexicographic_score)
        ):
            failures.append(
                {
                    "metric": "secondary_persisted_outcomes",
                    "label": "Persisted recovery differed from its protected candidate",
                }
            )
        if failures:
            transaction.set_rollback(True)
            report["regressions"] = failures
            _reset_rejected_report(report, baseline, "persistence_guard_rejected")
            return report

    report.update(
        applied=bool(updated),
        placements_updated=updated,
        soft_unresolved_after=soft_unresolved_count(persisted),
        room_shortages_after=physical_after["original_room_shortages"],
        unassigned_room_rows_after=safety["physical_unassigned_rooms"],
        incompatible_rooms_after=physical_after["original_incompatible_rooms"],
        instructor_after=schedule_metrics_of(persisted),
    )
    result.update(
        final_score=list(persisted.lexicographic_score),
        quality_score=persisted.quality_score,
        unresolved_students=len(persisted.unresolved_student_ids),
        hotspot_courses=persisted.hotspot_courses[:10],
        capacity_pressure_courses=persisted.capacity_pressure_courses[:10],
        reserve_heavy_sections=[
            {"section": sid, "ratio": round(ratio, 2)}
            for sid, ratio in persisted.reserve_heavy_sections[:10]
        ],
    )
    if updated:
        result["persist_result"] = {"action": "persisted_secondary_recovery", "updated": updated}
    return report

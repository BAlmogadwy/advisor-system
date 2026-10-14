"""Atomic workspace operations for complete course-specific weekly patterns."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from django.db import transaction

from core.models import (
    DeliveryBoard,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.section_programmes import section_is_available_to_delivery_board
from core.services.timetable_course_policy import (
    CourseMeetingPolicy,
    get_course_meeting_policy,
    meeting_options_for_policy,
    meeting_signature,
)


class SectionPolicyError(ValueError):
    code = "COURSE_SECTION_PATTERN_REQUIRED"


def section_policy(section: TermSection) -> CourseMeetingPolicy | None:
    return get_course_meeting_policy(section.course_code, section.course_key, section.course_number)


def placement_snapshot(row: SectionPlacement) -> dict[str, Any]:
    return {
        "id": row.id,
        "board_id": row.board_id,
        "term_section_id": row.term_section_id,
        "day": row.day,
        "start_time": row.start_time,
        "end_time": row.end_time,
        "room": row.room or "",
        "is_locked": row.is_locked,
    }


def policy_payload(policy: CourseMeetingPolicy, rows: Iterable[Any] = ()) -> dict[str, Any]:
    unique = {meeting_signature(row) for row in rows}
    current = policy.pattern_for(
        {"day": day, "start_min": start, "end_min": end} for day, start, end in unique
    )
    choices = []
    for pattern in policy.patterns:
        choice = pattern.to_dict()
        for meeting in choice["meetings"]:
            meeting.update(start_time=meeting["start"], end_time=meeting["end"])
        choices.append(choice)
    return {
        "kind": "fixed_weekly_pattern",
        "policy_id": policy.policy_id,
        "meeting_kind": policy.meeting_kind,
        "uses_shared_room": policy.uses_shared_room,
        "room_required": policy.uses_shared_room,
        "no_room_required": not policy.uses_shared_room,
        "required_meetings": len(policy.durations),
        "requires_full_section_pattern": True,
        "current_pattern_id": current.pattern_id if current else None,
        "patterns": choices,
    }


def assert_policy_rows(policy: CourseMeetingPolicy, rows: Iterable[Any]) -> None:
    unique = {meeting_signature(row) for row in rows}
    if not policy.accepts(
        {"day": day, "start_min": start, "end_min": end} for day, start, end in unique
    ):
        raise SectionPolicyError(
            "ENG101/ENG102 require four continuous 180-minute meetings on SUN, MON, WED, THU, "
            "all 09:00–12:00 or all 13:00–16:00. Switch the complete section pattern."
        )


def assert_policy_placement_rows(policy: CourseMeetingPolicy, rows: list[Any]) -> None:
    """Every board copy must be complete, and all copies describe one pattern."""
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["board_id"] if isinstance(row, dict) else row.board_id].append(row)
    for board_rows in grouped.values():
        assert_policy_rows(policy, board_rows)
        if len(board_rows) != len(policy.durations):
            raise SectionPolicyError(
                "A complete English section needs exactly four meetings per board."
            )
    assert_policy_rows(policy, rows)


def policy_move_rejections(
    placements_by_id: Mapping[int, SectionPlacement], moves: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Validate the complete final section, including unchanged companion rows."""
    sections = {row.term_section_id: row.term_section for row in placements_by_id.values()}
    replacements = {int(move["placement_id"]): move for move in moves}
    rejections = []
    for section_id, section in sections.items():
        policy = section_policy(section)
        if policy is None:
            continue
        final = []
        for row in SectionPlacement.objects.filter(term_section_id=section_id):
            values = placement_snapshot(row)
            move = replacements.get(row.id)
            if move:
                values.update(
                    day=move["day"],
                    start_time=move.get("start", move.get("start_time")),
                    end_time=move.get("end", move.get("end_time")),
                )
            final.append(values)
        try:
            assert_policy_placement_rows(policy, final)
            first = next(
                row for row in placements_by_id.values() if row.term_section_id == section_id
            )
            unique = {meeting_signature(row) for row in final}
            assert_pattern_available(
                policy,
                [{"day": day, "start_min": start, "end_min": end} for day, start, end in unique],
                first.board.scenario,
            )
        except (SectionPolicyError, ValueError, TypeError) as exc:
            rejections.append(
                {
                    "code": SectionPolicyError.code,
                    "message": str(exc),
                    "details": {"term_section_id": section_id},
                }
            )
    return rejections


def _mutable(board: DeliveryBoard) -> None:
    if board.scenario.status == "published":
        raise SectionPolicyError("Cannot modify published scenario.")


def assert_pattern_available(
    policy: CourseMeetingPolicy, rows: Iterable[Any], scenario: TimetableScenario
) -> None:
    pattern = policy.pattern_for(rows)
    allowed = meeting_options_for_policy(
        policy,
        blocked_slots=scenario.blocked_slots or [],
        slots=[*(scenario.slot_config or []), *(scenario.lab_slot_config or [])],
    )
    allowed_pattern_ids = {
        option_pattern.pattern_id
        for option in allowed
        if (option_pattern := policy.pattern_for(option)) is not None
    }
    if pattern is None or pattern.pattern_id not in allowed_pattern_ids:
        raise SectionPolicyError(
            "The complete English pattern intersects a blocked teaching interval."
        )


def sync_policy_meeting_changes(
    placements_by_id: Mapping[int, SectionPlacement], before_rows: list[dict[str, Any]]
) -> None:
    """Keep canonical meeting metadata attached when trusted section moves change time."""
    changes: dict[int, dict[tuple[str, int, int], SectionPlacement]] = defaultdict(dict)
    for before in before_rows:
        row = placements_by_id.get(int(before.get("placement_id") or before.get("id") or 0))
        if (
            row
            and section_policy(row.term_section)
            and row.term_section.scenario_id == row.board.scenario_id
        ):
            changes[row.term_section_id][meeting_signature(before)] = row
    for section_id, targets in changes.items():
        for meeting in TermSectionMeeting.objects.select_for_update().filter(
            term_section_id=section_id
        ):
            target = targets.get(meeting_signature(meeting))
            if target:
                meeting.day, meeting.start_time, meeting.end_time, meeting.room = (
                    target.day,
                    target.start_time,
                    target.end_time,
                    target.room,
                )
                meeting.save(update_fields=["day", "start_time", "end_time", "room"])


def create_policy_section_bundle(
    board: DeliveryBoard,
    section: TermSection,
    *,
    meetings: list[dict[str, Any]] | None = None,
    day: str = "",
    start_time: str = "",
    end_time: str = "",
    room: str = "",
) -> list[SectionPlacement]:
    policy = section_policy(section)
    if policy is None:
        raise SectionPolicyError("This course does not have a fixed weekly section pattern.")
    _mutable(board)
    if meetings is None:
        pattern = next(
            (
                p
                for p in policy.patterns
                if any(
                    meeting_signature(m)
                    == meeting_signature(
                        {"day": day, "start_time": start_time, "end_time": end_time}
                    )
                    for m in p.meetings
                )
            ),
            None,
        )
        if pattern is None:
            raise SectionPolicyError("Select an English morning or afternoon section pattern.")
        meetings = [dict(m.to_dict(), room=room) for m in pattern.meetings]
    if not isinstance(meetings, list) or any(not isinstance(row, dict) for row in meetings):
        raise SectionPolicyError(
            "meetings must be an array containing the complete English section pattern."
        )
    proposed = []
    boards = {board.id: board}
    for meeting in meetings:
        target_id = int(meeting.get("board_id") or board.id)
        if target_id not in boards:
            boards[target_id] = DeliveryBoard.objects.select_related("scenario").get(
                pk=target_id, scenario_id=board.scenario_id
            )
        target = boards[target_id]
        if not section_is_available_to_delivery_board(
            section, scenario_id=target.scenario_id, program=target.program
        ):
            raise SectionPolicyError("Section is outside the target board's scenario or programme.")
        meeting_day, start, end = meeting_signature(meeting)
        proposed.append(
            {
                "board_id": target.id,
                "term_section_id": section.id,
                "day": meeting_day,
                "start_time": f"{start // 60:02d}:{start % 60:02d}",
                "end_time": f"{end // 60:02d}:{end % 60:02d}",
                "room": str(meeting.get("room", room) or ""),
                "is_locked": bool(meeting.get("is_locked", False)),
            }
        )
    assert_policy_placement_rows(policy, proposed)
    assert_pattern_available(
        policy, [row for row in proposed if row["board_id"] == board.id], board.scenario
    )
    with transaction.atomic():
        existing = list(SectionPlacement.objects.select_for_update().filter(term_section=section))
        assert_policy_placement_rows(policy, [*existing, *proposed])
        if section.scenario_id == board.scenario_id:
            saved_meetings = list(
                TermSectionMeeting.objects.select_for_update().filter(term_section=section)
            )
            if saved_meetings:
                assert_policy_rows(policy, saved_meetings)
                targets = {row["day"]: row for row in proposed}
                for meeting in saved_meetings:
                    target = targets[meeting.day]
                    meeting.start_time, meeting.end_time, meeting.room = (
                        target["start_time"],
                        target["end_time"],
                        target["room"],
                    )
                    meeting.save(update_fields=["start_time", "end_time", "room"])
            for row in proposed:
                TermSectionMeeting.objects.get_or_create(
                    term_section=section,
                    day=row["day"],
                    start_time=row["start_time"],
                    end_time=row["end_time"],
                    defaults={"room": row["room"]},
                )
        placements = [SectionPlacement.objects.create(**row) for row in proposed]
    return placements


def switch_policy_section_bundle(
    anchor: SectionPlacement,
    *,
    pattern_id: str | None = None,
    day: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    room: str | None = None,
    override: bool = False,
    placements: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    policy = section_policy(anchor.term_section)
    _mutable(anchor.board)
    if policy is None:
        raise SectionPolicyError("This course does not have a fixed weekly section pattern.")
    with transaction.atomic():
        bundle = list(
            SectionPlacement.objects.select_for_update()
            .filter(
                term_section_id=anchor.term_section_id,
                board__scenario_id=anchor.board.scenario_id,
            )
            .select_related("term_section", "board__scenario")
            .order_by("board_id", "day", "id")
        )
        before = [placement_snapshot(row) for row in bundle]
        assert_policy_placement_rows(policy, before)
        before_policy = policy_payload(policy, before)
        pattern = next((p for p in policy.patterns if p.pattern_id == pattern_id), None)
        if pattern_id is None:
            if day is not None and str(day).upper() != anchor.day:
                raise SectionPolicyError(
                    "English section switches keep SUN, MON, WED and THU unchanged."
                )
            pattern = next(
                (
                    p
                    for p in policy.patterns
                    if any(
                        meeting_signature(m)
                        == meeting_signature(
                            {
                                "day": anchor.day,
                                "start_time": start_time or anchor.start_time,
                                "end_time": end_time or anchor.end_time,
                            }
                        )
                        for m in p.meetings
                    )
                ),
                None,
            )
        if pattern is None:
            raise SectionPolicyError("Select an English morning or afternoon section pattern.")
        scenario = anchor.board.scenario
        assert_pattern_available(policy, pattern.meetings, scenario)
        if placements is not None and (
            not isinstance(placements, list) or any(not isinstance(row, dict) for row in placements)
        ):
            raise SectionPolicyError("placements must be an array of section history snapshots.")
        overrides = {int(row["id"]): row for row in (placements or [])}
        if set(overrides) - {row.id for row in bundle}:
            raise SectionPolicyError("Pattern history contains a placement outside this section.")
        if any(row.is_locked for row in bundle) and not override:
            raise SectionPolicyError(
                "English section contains locked placements. Pass override: true to force."
            )
        targets = {m.day: m.to_dict() for m in pattern.meetings}
        changes: dict[tuple[str, int, int], SectionPlacement] = {}
        for row in bundle:
            old_key = meeting_signature(row)
            target = targets[row.day]
            row.start_time, row.end_time = target["start"], target["end"]
            snapshot = overrides.get(row.id, {})
            if "room" in snapshot:
                row.room = str(snapshot["room"] or "")
            elif row.id == anchor.id and room is not None:
                row.room = str(room or "")
            if "is_locked" in snapshot:
                row.is_locked = bool(snapshot["is_locked"])
            row.save(update_fields=["start_time", "end_time", "room", "is_locked", "updated_at"])
            changes[old_key] = row
        if anchor.term_section.scenario_id == anchor.board.scenario_id:
            meetings = list(
                TermSectionMeeting.objects.select_for_update().filter(
                    term_section=anchor.term_section
                )
            )
            for meeting in meetings:
                placement_target = changes.get(meeting_signature(meeting))
                if placement_target:
                    meeting.start_time, meeting.end_time, meeting.room = (
                        placement_target.start_time,
                        placement_target.end_time,
                        placement_target.room,
                    )
                    meeting.save(update_fields=["start_time", "end_time", "room"])
        anchor = next(row for row in bundle if row.id == anchor.id)
        return {
            "placement": anchor,
            "placements": bundle,
            "before_placements": before,
            "before_section_policy": before_policy,
            "section_policy": policy_payload(policy, bundle),
        }


def remove_policy_section_bundle(
    anchor: SectionPlacement, *, override: bool = False
) -> dict[str, Any]:
    _mutable(anchor.board)
    with transaction.atomic():
        bundle = list(
            SectionPlacement.objects.select_for_update().filter(
                term_section_id=anchor.term_section_id,
                board__scenario_id=anchor.board.scenario_id,
            )
        )
        if any(row.is_locked for row in bundle) and not override:
            raise SectionPolicyError(
                "English section contains locked placements. Pass override: true to force."
            )
        before = [placement_snapshot(row) for row in bundle]
        ids = [row.id for row in bundle]
        SectionPlacement.objects.filter(pk__in=ids).delete()
        return {"removed": anchor.id, "removed_ids": ids, "removed_placements": before}

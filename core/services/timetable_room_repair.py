from __future__ import annotations

from core.services.timetable_assignment_models import (
    CanonicalPattern,
    MoveSnapshot,
    RoomOccupancy,
    RoomProfile,
    SectionGridSnapshot,
    SectionState,
    TimetableMove,
)
from core.services.timetable_room_oracle import (
    NO_ROOM_CAPACITY,
    RoomFailureReason,
    check_capacity_feasibility,
    check_occupancy,
    check_type_feasibility,
    is_room_oracle_enabled,
)


def _find_pattern_in_catalog(
    pattern_id: str,
    family_key: str,
    catalog: dict[str, list[CanonicalPattern]],
) -> CanonicalPattern:
    for pat in catalog.get(family_key, []):
        if pat.pattern_id == pattern_id:
            return pat
    raise ValueError(f"Pattern {pattern_id} not found in family {family_key}")


def apply_move_to_grid(
    move: TimetableMove,
    sections_by_id: dict[str, SectionState],
    pattern_catalog: dict[str, list[CanonicalPattern]],
) -> MoveSnapshot:
    snapshots: list[SectionGridSnapshot] = []
    sec_a = sections_by_id[move.section_id_a]
    snapshots.append(
        SectionGridSnapshot(
            section_id=sec_a.section_id,
            old_pattern_id=sec_a.pattern_id,
            old_meetings=list(sec_a.meetings),
            old_room_id=sec_a.assigned_room_id,
        )
    )
    if move.move_type == "swap" and move.section_id_b:
        sec_b = sections_by_id[move.section_id_b]
        snapshots.append(
            SectionGridSnapshot(
                section_id=sec_b.section_id,
                old_pattern_id=sec_b.pattern_id,
                old_meetings=list(sec_b.meetings),
                old_room_id=sec_b.assigned_room_id,
            )
        )
    pat_a_new = _find_pattern_in_catalog(
        move.to_pattern_id_a, sec_a.pattern_family, pattern_catalog
    )
    if move.move_type == "repattern":
        sec_a.meetings = list(pat_a_new.meetings)
        sec_a.pattern_id = pat_a_new.pattern_id
    elif move.move_type == "swap" and move.section_id_b:
        sec_b = sections_by_id[move.section_id_b]
        pat_b_new = _find_pattern_in_catalog(
            move.to_pattern_id_b or "", sec_b.pattern_family, pattern_catalog
        )
        sec_a.meetings = list(pat_a_new.meetings)
        sec_a.pattern_id = pat_a_new.pattern_id
        sec_b.meetings = list(pat_b_new.meetings)
        sec_b.pattern_id = pat_b_new.pattern_id
    else:
        raise ValueError(f"Unsupported move_type: {move.move_type}")
    return MoveSnapshot(snapshots=snapshots)


def try_repair_rooms_locally(
    snapshot: MoveSnapshot,
    sections_by_id: dict[str, SectionState],
    rooms_by_id: dict[str, RoomProfile],
    room_occupancies: dict[str, RoomOccupancy],
    course_room_requirements: dict[str, str],
    failures_out: list[dict] | None = None,
) -> bool:
    impacted_room_ids: set[str] = set()
    for snap in snapshot.snapshots:
        sec = sections_by_id[snap.section_id]
        old_room_id = snap.old_room_id
        if old_room_id:
            room_occupancies[old_room_id].assigned_section_ids.discard(sec.section_id)
            impacted_room_ids.add(old_room_id)
            sec.assigned_room_id = None
    for rid in impacted_room_ids:
        room_occupancies[rid].rebuild_from_truth(sections_by_id)

    affected_sections = [sections_by_id[s.section_id] for s in snapshot.snapshots]
    affected_sections.sort(key=lambda s: (s.room_demand(), s.section_id), reverse=True)
    for sec in affected_sections:
        # Determine room type from the MAJORITY of meeting durations.
        # A 4-credit course has 2×75min lectures + 1×100min lab — the
        # majority are lectures, so assign a lecture room. The auto-placer
        # handles per-meeting room types, but the optimizer uses one room
        # per section, so we pick the dominant type.
        if sec.meetings:
            lecture_count = sum(1 for m in sec.meetings if (m.end_min - m.start_min) <= 80)
            req_type = "lecture" if lecture_count >= len(sec.meetings) / 2 else "lab"
        else:
            req_type = course_room_requirements.get(sec.course_code, sec.room_type_required)
        compatible_rooms = sorted(
            (
                room
                for room in rooms_by_id.values()
                if room.room_type == req_type and room.capacity >= sec.room_demand()
            ),
            key=lambda r: (r.capacity, r.room_id),
        )
        placed = False
        for room in compatible_rooms:
            occ = room_occupancies[room.room_id]
            if occ.can_accommodate(sec.meetings):
                sec.assigned_room_id = room.room_id
                occ.assigned_section_ids.add(sec.section_id)
                occ.rebuild_from_truth(sections_by_id)
                placed = True
                break
        if not placed:
            # PR2 commit 4 — Site 4: section could not be placed during
            # local room repair. Back-compat: the boolean return is
            # preserved; when callers pass ``failures_out``, a typed
            # RoomFailureReason is appended so they can learn *why* this
            # specific section failed. Commit 4 wires the Stage 1 chain
            # (type → capacity) plus an occupancy check: if eligible
            # rooms existed but were all booked across the section's
            # meetings, report ROOM_OCCUPIED. Gender is not checked
            # here because ``SectionState`` has no gender-requirement
            # field today — upstream filtering has already happened.
            if failures_out is not None:
                first_meeting = sec.meetings[0] if sec.meetings else None
                day_val = str(getattr(first_meeting, "day", "") or "")
                start_val = str(
                    getattr(first_meeting, "start_time", "")
                    or getattr(first_meeting, "start_min", "")
                )
                end_val = str(
                    getattr(first_meeting, "end_time", "") or getattr(first_meeting, "end_min", "")
                )
                section_dict = {
                    "course_code": sec.course_code,
                    "section_code": sec.section_id,
                    "day": day_val,
                    "start_time": start_val,
                    "end_time": end_val,
                    "demand": sec.room_demand(),
                    "room_type_required": req_type,
                    "gender_required": "",
                }
                rooms_dicts = [
                    {
                        "room_code": r.room_id,
                        "capacity": r.capacity,
                        "room_type": r.room_type,
                        "gender": r.gender,
                    }
                    for r in rooms_by_id.values()
                ]
                refined: RoomFailureReason | None = None
                if is_room_oracle_enabled():
                    # Occupancy set = rooms whose current state cannot
                    # host the section's full meeting list — mirrors the
                    # ``occ.can_accommodate`` filter used above, so a
                    # ROOM_OCCUPIED result is provably the reason.
                    busy_rooms: set[str] = {
                        rid
                        for rid, occ in room_occupancies.items()
                        if not occ.can_accommodate(sec.meetings)
                    }
                    refined = (
                        check_type_feasibility(section_dict, rooms_dicts)
                        or check_capacity_feasibility(section_dict, rooms_dicts, 1.0)
                        or check_occupancy(section_dict, rooms_dicts, busy_rooms)
                    )
                if refined is None:
                    refined = RoomFailureReason(
                        code=NO_ROOM_CAPACITY,
                        day=day_val,
                        start_time=start_val,
                        end_time=end_val,
                        course_code=sec.course_code,
                        section_code=sec.section_id,
                    )
                failures_out.append(refined.to_dict())
            return False
    return True


def rollback_move(
    snapshot: MoveSnapshot,
    sections_by_id: dict[str, SectionState],
    room_occupancies: dict[str, RoomOccupancy],
) -> None:
    impacted_room_ids: set[str] = set()
    for snap in snapshot.snapshots:
        sec = sections_by_id[snap.section_id]
        current_room = sec.assigned_room_id
        if current_room:
            room_occupancies[current_room].assigned_section_ids.discard(sec.section_id)
            impacted_room_ids.add(current_room)
    for snap in snapshot.snapshots:
        sec = sections_by_id[snap.section_id]
        sec.pattern_id = snap.old_pattern_id
        sec.meetings = list(snap.old_meetings)
        sec.assigned_room_id = snap.old_room_id
        if snap.old_room_id:
            room_occupancies[snap.old_room_id].assigned_section_ids.add(sec.section_id)
            impacted_room_ids.add(snap.old_room_id)
    for rid in impacted_room_ids:
        room_occupancies[rid].rebuild_from_truth(sections_by_id)


def validate_room_state(
    sections_by_id: dict[str, SectionState], room_occupancies: dict[str, RoomOccupancy]
) -> list[str]:
    errors: list[str] = []
    for sec in sections_by_id.values():
        if (
            sec.assigned_room_id
            and sec.section_id not in room_occupancies[sec.assigned_room_id].assigned_section_ids
        ):
            errors.append(
                f"Section {sec.section_id} missing from room occupancy {sec.assigned_room_id}"
            )
    for rid, occ in room_occupancies.items():
        seen_masks: dict[int, int] = {i: 0 for i in range(7)}
        for sec_id in occ.assigned_section_ids:
            sec = sections_by_id[sec_id]
            if sec.assigned_room_id != rid:
                errors.append(
                    f"Room {rid} tracks {sec_id} but section points to {sec.assigned_room_id}"
                )
                continue
            for m in sec.meetings:
                if seen_masks[m.day] & m.mask:
                    errors.append(f"Room {rid} overlap detected for section {sec_id}")
                seen_masks[m.day] |= m.mask
    return errors

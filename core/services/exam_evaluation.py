"""Complete fixed-placement evaluation shared by Check, Save and Optimise.

This module reads authoritative inputs and performs no database writes. It
never schedules, so evaluation cannot choose where an exam sits.

One caller is not a pure evaluation. Optimise re-solves every placement, which
makes it a build, and a build owes the board the same invigilator post-pass that
``build_exam_timetable`` runs — without it, Optimise silently undoes the balancing
the original build paid for. That post-pass is therefore available here behind
``rebalance_invigilators``, which defaults to off. Check and Save must never turn
it on: both exist to report on the exact board they were handed.
"""

from __future__ import annotations

from django.db import transaction

from core.models import Room, Student
from core.services.course_identity import planner_course_key
from core.services.exam_input_fingerprint import fingerprint_exam_inputs
from core.services.exam_operations_snapshot import build_exam_operations_snapshot
from core.services.exam_review import build_exam_review
from core.services.exam_room_allocation import RoomAllocationContext
from core.services.exam_run_schema import (
    STATUS_DERIVATION_VERSION,
    compute_enrolment_snapshot,
    derive_building_footprint,
    derive_multi_sitting_details,
    derive_status_surface,
    stamp_schema_version,
)
from core.services.exam_sections import EXAM_ENROLLMENT_SOURCE, summarize_exam_section_mapping
from core.services.exam_timetable import (
    _build_qa,
    _build_room_qa,
    _build_section_enrollment_from_enrolled_sets,
    _rebalance_invigilators_pass,
    _source_code_for_display,
    apply_thin_conflict_policy,
    assign_rooms_to_schedule,
    attach_exam_relaxation_qa,
    build_conflict_graph,
    build_credit_map,
    build_enrolled_sets_with_meta,
    build_plan_term_buckets,
    check_room_feasibility,
    period_cohort_count,
    select_exam_course_enrollments,
    validate_exam_pins,
)


def _course_identity_for_entry(entry: dict) -> str:
    explicit = str(entry.get("course_identity") or "").strip()
    if explicit:
        return explicit
    source = _source_code_for_display(
        str(entry.get("course_code", "")).strip(),
        entry.get("source_course_code"),
    )
    return planner_course_key(source, entry.get("course_name"))


def _build_loaded_course_enrollments(
    entries: list[dict],
    programs: list[str] | None = None,
    sections: list[str] | None = None,
) -> tuple[dict[str, set[int]], dict[str, dict]]:
    enrolled, metadata = build_enrolled_sets_with_meta(programs=programs, sections=sections)
    return select_exam_course_enrollments(entries, enrolled, metadata)


def _normalise_loaded_schedule_entries(
    schedule_raw: list,
    days: list[str],
    periods: list[str],
    selected_courses: list[str] | None,
) -> list[dict]:
    if not isinstance(schedule_raw, list) or not schedule_raw:
        raise ValueError("The current timetable must contain at least one course.")
    if selected_courses is not None and (
        not isinstance(selected_courses, list)
        or any(not isinstance(code, str) or not code.strip() for code in selected_courses)
        or len(set(selected_courses)) != len(selected_courses)
    ):
        raise ValueError("Selected courses must contain unique course codes.")
    selected = set(selected_courses) if selected_courses is not None else None
    slot_index_by_key: dict[tuple[str, str], int] = {}
    slots_len = 0
    for day in days:
        for period in periods:
            slot_index_by_key[(day, period)] = slots_len
            slots_len += 1

    schedule_entries: list[dict] = []
    overflow_idx = slots_len
    seen_codes: set[str] = set()
    seen_identities: set[str] = set()
    for raw in schedule_raw:
        if not isinstance(raw, dict):
            raise ValueError("Every timetable entry must be a course placement.")
        if any(
            not isinstance(raw.get(key), str) or not raw[key].strip()
            for key in ("course_code", "day")
        ) or not isinstance(raw.get("period"), str):
            raise ValueError("Every timetable entry must specify a course, day and period.")
        course_code, day, period = (raw[key].strip() for key in ("course_code", "day", "period"))
        if day != "OVERFLOW" and not period:
            raise ValueError(f"Course {course_code} must specify an exam period.")

        if day == "OVERFLOW":
            slot_index = raw.get("slot_index", overflow_idx)
            if (
                isinstance(slot_index, bool)
                or not isinstance(slot_index, int)
                or slot_index < slots_len
            ):
                raise ValueError(f"Overflow course {course_code} has an invalid slot index.")
            overflow_idx = max(overflow_idx, slot_index + 1)
        else:
            slot_index = slot_index_by_key.get((day, period))
            if slot_index is None:
                raise ValueError(
                    f"Loaded schedule course {course_code} uses slot {day} {period}, "
                    "which is not in the current header."
                )

        entry = dict(raw)
        entry["course_code"] = course_code
        entry["source_course_code"] = _source_code_for_display(
            course_code,
            raw.get("source_course_code"),
        )
        entry["course_name"] = str(raw.get("course_name") or "")
        entry["course_identity"] = _course_identity_for_entry(entry)
        if course_code in seen_codes or entry["course_identity"] in seen_identities:
            raise ValueError(f"Course {course_code} was selected more than once.")
        seen_codes.add(course_code)
        seen_identities.add(entry["course_identity"])
        entry["day"] = day
        entry["period"] = period
        entry["slot_index"] = slot_index
        entry["rooms"] = []
        schedule_entries.append(entry)

    if selected is not None and selected != seen_codes:
        raise ValueError("Selected courses and current timetable placements must match.")
    return sorted(schedule_entries, key=lambda e: (int(e.get("slot_index", 0)), e["course_code"]))


def _rooms_with_metadata() -> list[dict]:
    return list(
        Room.objects.order_by("room_code").values(
            "room_code",
            "capacity",
            "section",
            "department",
            "building",
            "floor",
        )
    )


def _attach_room_metadata(schedule_entries: list[dict], rooms_list: list[dict]) -> None:
    room_meta_by_code = {str(room.get("room_code", "")): room for room in rooms_list}
    for entry in schedule_entries:
        for room_row in entry.get("rooms") or []:
            if not isinstance(room_row, dict):
                continue
            meta = room_meta_by_code.get(str(room_row.get("room_code", "")))
            if meta:
                room_row.setdefault("building", str(meta.get("building", "") or ""))
                room_row.setdefault("floor", str(meta.get("floor", "") or ""))


@transaction.atomic
def evaluate_exam_schedule(
    *,
    days: list[str],
    periods: list[str],
    max_per_day: int,
    schedule_raw: list,
    selected_courses: list[str] | None,
    assign_rooms: bool,
    seed: int | None,
    thin_conflict_threshold: int,
    rebuild_mode: str = "loaded_schedule",
    programs: list[str] | None = None,
    sections: list[str] | None = None,
    pinned: list[dict] | None = None,
    rebalance_invigilators: bool = False,
) -> dict:
    """Evaluate exact exam placements without scheduling or persisting a run.

    Canonical enrollment, credit, section and room inputs are captured once.
    Room assignment may change, but the submitted exam slots never do.
    """
    schedule_entries = _normalise_loaded_schedule_entries(
        schedule_raw,
        days,
        periods,
        selected_courses,
    )
    course_list = sorted({entry["course_code"] for entry in schedule_entries})
    slots = [
        {"index": index, "day": day, "period": period}
        for index, (day, period) in enumerate((day, period) for day in days for period in periods)
    ]
    pinned = validate_exam_pins(pinned, course_list, slots, schedule_entries=schedule_entries)
    enrolled_sets, course_meta = _build_loaded_course_enrollments(
        schedule_entries, programs, sections
    )
    for entry in schedule_entries:
        entry.update(course_meta[entry["course_code"]])

    all_students: set[int] = set()
    for sids in enrolled_sets.values():
        all_students.update(sids)

    source_credit_map = build_credit_map(
        {
            _source_code_for_display(entry["course_code"], entry.get("source_course_code"))
            for entry in schedule_entries
        }
    )
    credit_map: dict[str, int] = {}
    for entry in schedule_entries:
        code = entry["course_code"]
        source = _source_code_for_display(code, entry.get("source_course_code"))
        credit_map[code] = source_credit_map.get(source, source_credit_map.get(code, 3))

    conflicts, full_adj = build_conflict_graph(enrolled_sets)
    adj, thin_courses = apply_thin_conflict_policy(enrolled_sets, full_adj, thin_conflict_threshold)
    plan_term_buckets, course_buckets = build_plan_term_buckets(
        set(course_list), course_meta, programs=programs
    )

    student_attribution = list(
        Student.objects.filter(student_id__in=all_students)
        .order_by("student_id")
        .values("student_id", "program", "section")
    )
    operations_sections: dict[str, list[dict]] = {}
    section_enrollment = _build_section_enrollment_from_enrolled_sets(
        enrolled_sets,
        course_meta=course_meta,
        section_by_student={
            row["student_id"]: str(row["section"] or "").strip() for row in student_attribution
        },
        program_by_student={row["student_id"]: row["program"] for row in student_attribution},
        operations_sections=operations_sections,
    )
    rooms_list: list[dict] = []
    room_feasibility: list[dict] = []
    rebalance_moves = 0
    if assign_rooms:
        rooms_list = _rooms_with_metadata()
        room_feasibility = check_room_feasibility(section_enrollment, rooms_list)
        for entry in schedule_entries:
            entry["rooms"] = []
        # One shared, size-sized deadline across the pack and every trial repack
        # the post-pass performs, exactly as build_exam_timetable arms it.
        allocation_context = RoomAllocationContext.for_periods(
            period_cohort_count(schedule_entries, section_enrollment)
        )
        assign_rooms_to_schedule(
            schedule_entries,
            section_enrollment,
            rooms_list,
            seed=seed,
            allocation_context=allocation_context,
        )
        if rebalance_invigilators and rooms_list and len(days) > 1:
            rebalance_moves = _rebalance_invigilators_pass(
                schedule_entries,
                section_enrollment,
                rooms_list,
                slots,
                adj,
                plan_term_buckets,
                course_buckets,
                pinned_courses={pin["course_code"] for pin in pinned},
                allocation_context=allocation_context,
                enrolled_sets=enrolled_sets,
                credit_map=credit_map,
                max_per_day=max_per_day,
            )
            # The post-pass moves exams between days; a pin may not be one of them.
            validate_exam_pins(pinned, course_list, slots, schedule_entries=schedule_entries)
        _attach_room_metadata(schedule_entries, rooms_list)

    # QA is computed over the FINAL board. When the post-pass ran, the placements
    # it produced are the ones that get persisted, reported and exported.
    qa = _build_qa(
        enrolled_sets,
        schedule_entries,
        max_per_day=max_per_day,
        plan_term_buckets=plan_term_buckets,
        credit_map=credit_map,
    )
    attach_exam_relaxation_qa(
        qa,
        enrolled_sets,
        schedule_entries,
        thin_conflict_threshold,
        thin_courses,
    )
    qa["rooms"] = _build_room_qa(schedule_entries, rooms_list if assign_rooms else [])
    qa["room_feasibility_violations"] = room_feasibility
    qa["rebalance_moves"] = rebalance_moves

    buckets_summary = [
        {
            "program": program,
            "programme_term": term,
            "course_count": len(courses),
            "courses": sorted(courses),
        }
        for (program, term), courses in sorted(plan_term_buckets.items())
    ]

    multi_sitting_details = derive_multi_sitting_details(schedule_entries)
    qa["multi_sitting_sections"] = len(multi_sitting_details)
    qa["multi_sitting_details"] = multi_sitting_details
    qa["building_footprint"] = derive_building_footprint(schedule_entries)

    sections_total = sum(len(v) for v in section_enrollment.values())
    qa["enrolment_snapshot"] = compute_enrolment_snapshot(
        enrolled_sets,
        sections_count=sections_total,
        fallback_used=False,
        synthetic_all_sections_count=0,
    )
    qa["section_mapping"] = summarize_exam_section_mapping(section_enrollment, course_meta)

    draft = {
        "status": "ok",
        "enrollment_source": EXAM_ENROLLMENT_SOURCE,
        "enrollment_scope": {"programs": programs or [], "sections": sections or []},
        "pinned": pinned,
        "students_count": len(all_students),
        "courses": course_list,
        "courses_count": len(course_list),
        "conflicts": conflicts,
        "conflicts_count": len(conflicts),
        "exam_review": build_exam_review(conflicts),
        "slots": slots,
        "schedule": schedule_entries,
        "qa": qa,
        "buckets_summary": buckets_summary,
        "bucket_count": len(plan_term_buckets),
        "credit_map": credit_map,
        "seed": seed,
        "section_enrollment": section_enrollment,
        "operations_snapshot": build_exam_operations_snapshot(
            schedule_entries, operations_sections
        ),
        "rooms_count": len(rooms_list),
        "assign_rooms": assign_rooms,
        "rebuild_mode": rebuild_mode,
    }
    primary_status, status_flags = derive_status_surface(draft)
    draft["primary_status"] = primary_status
    draft["status_flags"] = status_flags
    draft["status_derivation_version"] = STATUS_DERIVATION_VERSION
    result = stamp_schema_version(draft)

    result["input_fingerprint"] = fingerprint_exam_inputs(
        result=result,
        enrolled_sets=enrolled_sets,
        course_meta=course_meta,
        student_attribution=student_attribution,
        rooms=rooms_list,
        days=days,
        periods=periods,
        max_per_day=max_per_day,
        thin_conflict_threshold=thin_conflict_threshold,
    )
    return result

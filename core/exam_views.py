"""
core/exam_views.py
Exam Timetable Builder — page view + API endpoints.

All endpoints require SUPER_ADMIN role (enforced by ``_require_super_admin``).

Endpoints:
    GET  exam_timetable_page          – render the single-page builder UI
    GET  exam_timetable_filters_view  – return programs/sections for filter dropdowns
    POST exam_timetable_preview_courses_view – return running courses matching filters
    POST exam_timetable_build_view    – build (or rebuild) the exam timetable
    GET  exam_timetable_list_view     – paginated list of saved runs
    GET  exam_timetable_detail_view   – load a specific saved run
    GET  exam_timetable_export_view   – download a run as .xlsx
    POST exam_timetable_delete_view   – delete a saved run (requires confirm=DELETE)
"""

from __future__ import annotations

import json

from django.conf import settings
from django.http import FileResponse, HttpRequest, HttpResponse, HttpResponseBase, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from core.authz import throttle
from core.models import ExamTimetableRun, ProgrammeRequirement, Room, Student, StudentCourse
from core.services.audit import log_audit_event
from core.services.course_identity import planner_course_key
from core.services.exam_multistart import (
    is_multistart_enabled,
    report_to_dict,
    run_multistart,
)
from core.services.exam_run_schema import (
    STATUS_DERIVATION_VERSION,
    compute_enrolment_snapshot,
    derive_building_footprint,
    derive_multi_sitting_details,
    derive_status_surface,
    load_normalised_run,
    stamp_schema_version,
)
from core.services.exam_timetable import (
    _build_qa,
    _build_room_qa,
    assign_rooms_to_schedule,
    build_conflict_graph,
    build_credit_map,
    build_enrolled_sets_with_meta,
    build_exam_timetable,
    check_room_feasibility,
    export_exam_timetable_xlsx,
    schedule,
)
from core.services.rbac import ROLE_SUPER_ADMIN, get_user_role
from core.sidebar_context import get_sidebar_context


def _require_super_admin(request: HttpRequest) -> JsonResponse | None:
    """Guard: returns a 403 JsonResponse if user is not SUPER_ADMIN, else None."""
    if get_user_role(request.user) != ROLE_SUPER_ADMIN:
        return JsonResponse({"error": "SUPER_ADMIN access required"}, status=403)
    return None


@require_GET
def exam_timetable_page(request: HttpRequest) -> HttpResponse:
    """Render the exam timetable builder page (all logic is client-side JS)."""
    deny = _require_super_admin(request)
    if deny:
        return deny
    return render(request, "core/exam_timetable.html", get_sidebar_context(request))


@require_GET
def exam_timetable_filters_view(request: HttpRequest) -> JsonResponse:
    """Return distinct programs and sections for the filter dropdowns."""
    deny = _require_super_admin(request)
    if deny:
        return deny

    programs = sorted(
        p
        for p in Student.objects.exclude(program__isnull=True)
        .exclude(program="")
        .values_list("program", flat=True)
        .distinct()
        if p is not None
    )
    sections = sorted(
        Student.objects.exclude(section="").values_list("section", flat=True).distinct()
    )
    return JsonResponse({"ok": True, "programs": programs, "sections": sections})


@require_POST
def exam_timetable_preview_courses_view(request: HttpRequest) -> JsonResponse:
    """Return running courses (studying) matching the program/section filters."""
    deny = _require_super_admin(request)
    if deny:
        return deny

    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except Exception:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    programs_raw = payload.get("programs", [])
    sections_raw = payload.get("sections", [])

    # Normalise filter lists: strip whitespace, drop empties.
    # `or None` converts an empty list to None so build_enrolled_sets
    # treats it as "no filter" (include all students).
    programs = (
        [str(p).strip() for p in programs_raw if str(p).strip()]
        if isinstance(programs_raw, list)
        else []
    ) or None
    sections = (
        [str(s).strip() for s in sections_raw if str(s).strip()]
        if isinstance(sections_raw, list)
        else []
    ) or None

    enrolled_sets, course_meta = build_enrolled_sets_with_meta(
        programs=programs,
        sections=sections,
    )

    # Fetch credit hours for all preview courses
    course_codes = list(enrolled_sets.keys())
    credit_map = build_credit_map(course_codes)

    # Online flag per course: True iff at least one matching ProgrammeRequirement
    # row marks it as is_online, OR the course code is in the GS / GSE general
    # studies family (institutional convention — those are delivered online).
    from core.models import ProgrammeRequirement

    source_codes = {
        str(course_meta.get(cc, {}).get("source_course_code") or _source_code_for_display(cc))
        for cc in course_codes
    }
    pr_qs = ProgrammeRequirement.objects.filter(course_code__in=source_codes, is_online=True)
    if programs:
        pr_qs = pr_qs.filter(program__in=programs)
    online_set = set(pr_qs.values_list("course_code", flat=True))
    online_set.update(cc for cc in source_codes if cc.startswith(("GS", "GSE")))

    courses = sorted(
        [
            {
                "course_code": cc,
                "source_course_code": course_meta.get(cc, {}).get("source_course_code", cc),
                "course_name": course_meta.get(cc, {}).get("course_name", ""),
                "course_identity": course_meta.get(cc, {}).get("course_identity", cc),
                "enrolled_count": len(sids),
                "credit_hours": credit_map.get(cc, 3),
                "is_online": course_meta.get(cc, {}).get("source_course_code", cc) in online_set,
            }
            for cc, sids in enrolled_sets.items()
        ],
        key=lambda c: str(c["course_code"]),
    )

    return JsonResponse({"ok": True, "courses": courses})


# Throttle: looser in development for fast tuning, tighter in production
# to keep this expensive endpoint from being hammered.
_BUILD_MAX_CALLS = 20 if settings.DEBUG else 3


@require_POST
@throttle(max_calls=_BUILD_MAX_CALLS, window_seconds=120)
def exam_timetable_build_view(request: HttpRequest) -> JsonResponse:
    """Build (or rebuild) the exam timetable.

    Accepts JSON body with: label, days, periods, max_per_day,
    programs, sections, selected_courses, pinned overrides,
    and optional randomize flag for varied timetable generation.
    """
    deny = _require_super_admin(request)
    if deny:
        return deny

    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except Exception:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    # ── Extract raw values from JSON payload ──
    label = str(payload.get("label", "")).strip()
    days_raw = payload.get("days", [])
    periods_raw = payload.get("periods", [])
    max_per_day_raw = payload.get("max_per_day", 2)
    programs_raw = payload.get("programs", [])
    sections_raw = payload.get("sections", [])
    selected_courses_raw = payload.get("selected_courses", None)
    pinned_raw = payload.get("pinned", None)
    randomize = payload.get("randomize", False)
    assign_rooms = bool(payload.get("assign_rooms", True))
    thin_threshold_raw = payload.get("thin_conflict_threshold", 0)
    mode = str(payload.get("mode") or "").strip()

    if not label:
        return JsonResponse({"ok": False, "error": "label is required"}, status=400)

    # ── Normalise list inputs: strip whitespace, drop empties ──
    days = (
        [str(d).strip() for d in days_raw if str(d).strip()] if isinstance(days_raw, list) else []
    )
    periods = (
        [str(p).strip() for p in periods_raw if str(p).strip()]
        if isinstance(periods_raw, list)
        else []
    )

    # Soft cap on exams per student per day (default 2, minimum 1)
    try:
        max_per_day = int(max_per_day_raw)
        if max_per_day < 1:
            max_per_day = 1
    except (ValueError, TypeError):
        max_per_day = 2

    # Thin-conflict threshold: courses with total enrolment <= this value
    # are dropped from the conflict graph. 0 = current behaviour.
    # Clamped to [0, 10] to prevent the registrar from inadvertently
    # ignoring real-sized courses' conflicts. Booleans rejected (Python
    # treats True as int 1, but a JSON `true` here is almost certainly
    # a client bug, not "use threshold 1").
    if isinstance(thin_threshold_raw, bool):
        thin_conflict_threshold = 0
    else:
        try:
            thin_conflict_threshold = max(0, min(10, int(thin_threshold_raw)))
        except (ValueError, TypeError):
            thin_conflict_threshold = 0

    # `or None` → treat empty list as "no filter" (all students)
    programs = (
        [str(p).strip() for p in programs_raw if str(p).strip()]
        if isinstance(programs_raw, list)
        else []
    ) or None
    sections = (
        [str(s).strip() for s in sections_raw if str(s).strip()]
        if isinstance(sections_raw, list)
        else []
    ) or None

    # User-curated course list from the preview step (None = use all)
    selected_courses = (
        [str(c).strip() for c in selected_courses_raw if str(c).strip()]
        if isinstance(selected_courses_raw, list)
        else None
    )

    # Pinned overrides: courses the user dragged to a specific slot.
    # Each entry must have course_code, day, and period; skip invalid ones.
    pinned: list[dict[str, str]] | None = None
    if isinstance(pinned_raw, list):
        pinned_items: list[dict[str, str]] = []
        for p in pinned_raw:
            if isinstance(p, dict):
                cc = str(p.get("course_code", "")).strip()
                d = str(p.get("day", "")).strip()
                pr = str(p.get("period", "")).strip()
                if cc and d and pr:
                    pinned_items.append({"course_code": cc, "day": d, "period": pr})
        pinned = pinned_items if pinned_items else None

    if not days or not periods:
        return JsonResponse({"ok": False, "error": "days and periods are required"}, status=400)

    # Generate a random seed when the user enables randomised tie-breaking.
    # Each build produces a different timetable variant; the seed is stored
    # in the result so it can be reproduced if needed.
    import random as _rnd

    seed = _rnd.randint(1, 2**31 - 1) if randomize else None
    base_schedule_raw = payload.get("base_schedule")
    if isinstance(base_schedule_raw, list):
        try:
            if mode == "optimize_loaded":
                result = _optimise_loaded_schedule(
                    label=label,
                    days=days,
                    periods=periods,
                    max_per_day=max_per_day,
                    schedule_raw=base_schedule_raw,
                    selected_courses=selected_courses,
                    pinned=pinned,
                    assign_rooms=assign_rooms,
                    seed=seed,
                    thin_conflict_threshold=thin_conflict_threshold,
                )
            else:
                result = _rebuild_loaded_schedule(
                    label=label,
                    days=days,
                    periods=periods,
                    max_per_day=max_per_day,
                    schedule_raw=base_schedule_raw,
                    selected_courses=selected_courses,
                    assign_rooms=assign_rooms,
                    seed=seed,
                    thin_conflict_threshold=thin_conflict_threshold,
                    rebuild_mode="loaded_schedule",
                )
            return JsonResponse({"ok": True, **result})
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=500)

    # Multi-start is feature-flagged. When TIMETABLE_EXAM_MULTISTART_ENABLED
    # is set and the request opts in (``multistart=True``), the runner
    # explores N seeded builds and returns the 4 mechanically-defined
    # Pareto candidates ("recommended" / "lowest_overflow" /
    # "lowest_overload" / "best_room_feasibility") in a single response.
    # The single-run path remains the default; existing client code is
    # unaffected.
    multistart_requested = bool(payload.get("multistart", False))
    if multistart_requested and is_multistart_enabled():
        # Optional inputs; sensible defaults match the peer-review plan.
        try:
            n_runs = max(1, min(50, int(payload.get("n_runs", 20))))
        except (ValueError, TypeError):
            n_runs = 20
        try:
            time_budget_s = max(0.5, min(60.0, float(payload.get("time_budget_s", 12.0))))
        except (ValueError, TypeError):
            time_budget_s = 12.0
        previous_run_id_raw = payload.get("previous_run_id")
        try:
            previous_run_id = int(previous_run_id_raw) if previous_run_id_raw is not None else None
        except (ValueError, TypeError):
            previous_run_id = None

        try:
            report = run_multistart(
                label=label,
                days=days,
                periods=periods,
                max_per_day=max_per_day,
                programs=programs,
                sections=sections,
                selected_courses=selected_courses,
                pinned=pinned,
                n_runs=n_runs,
                time_budget_s=time_budget_s,
                assign_rooms=assign_rooms,
                thin_conflict_threshold=thin_conflict_threshold,
                previous_run_id=previous_run_id,
            )
        except Exception as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=500)

        if report.feasibility_error is not None and not report.candidates_by_role:
            return JsonResponse(
                {"ok": False, "multistart": report_to_dict(report)},
                status=400,
            )
        return JsonResponse(
            {"ok": True, "mode": "multistart", "multistart": report_to_dict(report)}
        )

    try:
        result = build_exam_timetable(
            label,
            days,
            periods,
            max_per_day=max_per_day,
            programs=programs,
            sections=sections,
            selected_courses=selected_courses,
            pinned=pinned,
            seed=seed,
            assign_rooms=assign_rooms,
            thin_conflict_threshold=thin_conflict_threshold,
        )
        # Check for feasibility error (bucket too large for available days)
        if result.get("feasibility_error"):
            return JsonResponse({"ok": False, **result}, status=400)
        return JsonResponse({"ok": True, **result})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)


def _source_code_for_display(display_code: str, explicit_source: object = None) -> str:
    source = str(explicit_source or "").strip()
    if source:
        return source
    # Imported duplicate displays are stored as "CS112 (1)" / "CS112 (2)".
    if display_code.endswith(")") and " (" in display_code:
        return display_code.rsplit(" (", 1)[0]
    return display_code


def _course_identity_for_entry(entry: dict) -> str:
    explicit = str(entry.get("course_identity") or "").strip()
    if explicit:
        return explicit
    source = _source_code_for_display(
        str(entry.get("course_code", "")).strip(),
        entry.get("source_course_code"),
    )
    return planner_course_key(source, entry.get("course_name"))


def _build_draft_enrolled_sets(schedule_entries: list[dict]) -> dict[str, set[int]]:
    source_to_display: dict[str, list[str]] = {}
    identity_by_display: dict[str, str] = {}
    for entry in schedule_entries:
        display = str(entry.get("course_code", "")).strip()
        if not display:
            continue
        source = _source_code_for_display(display, entry.get("source_course_code"))
        identity_by_display[display] = _course_identity_for_entry(entry)
        source_to_display.setdefault(source, [])
        if display not in source_to_display[source]:
            source_to_display[source].append(display)

    source_codes = set(source_to_display)
    if not source_codes:
        return {}

    source_sets: dict[str, set[int]] = {code: set() for code in source_codes}
    student_ids: set[int] = set()
    qs = StudentCourse.objects.filter(
        course__course_code__in=source_codes,
        status="studying",
    ).values_list("course__course_code", "student_id")
    for source, student_id in qs.iterator():
        sid = int(student_id)
        source_sets[str(source)].add(sid)
        student_ids.add(sid)

    pr_name_by_program_code = {
        (str(program), str(code)): str(name or "").strip()
        for program, code, name in ProgrammeRequirement.objects.filter(
            course_code__in=source_codes
        ).values_list("program", "course_code", "course_name")
    }
    student_identity: dict[tuple[str, int], str] = {}
    identity_rows = StudentCourse.objects.filter(
        course__course_code__in=source_codes,
        status="studying",
    ).select_related("course", "student")
    for sc in identity_rows:
        source = str(sc.course.course_code)
        sid = int(sc.student_id)
        program = str(sc.student.program or "")
        name = pr_name_by_program_code.get((program, source)) or str(sc.course.description or "")
        student_identity[(source, sid)] = planner_course_key(source, name)

    enrolled_sets: dict[str, set[int]] = {}
    for source, displays in source_to_display.items():
        sids = set(source_sets.get(source, set()))
        if len(displays) <= 1:
            enrolled_sets[displays[0]] = sids
            continue

        by_identity = {identity_by_display.get(display, display): display for display in displays}
        buckets: dict[str, set[int]] = {display: set() for display in displays}
        unmatched: list[int] = []
        for sid in sorted(sids):
            display = by_identity.get(student_identity.get((source, sid), ""))
            if display:
                buckets[display].add(sid)
            else:
                unmatched.append(sid)
        ordered = sorted(displays)
        for idx, sid in enumerate(unmatched):
            buckets[ordered[idx % len(ordered)]].add(sid)
        for display, bucket in buckets.items():
            enrolled_sets[display] = bucket
    return enrolled_sets


def _draft_plan_term_buckets(schedule_entries: list[dict]) -> dict[tuple[str, int], set[str]]:
    source_to_display: dict[str, list[str]] = {}
    for entry in schedule_entries:
        display = str(entry.get("course_code", "")).strip()
        if not display:
            continue
        source = _source_code_for_display(display, entry.get("source_course_code"))
        source_to_display.setdefault(source, []).append(display)

    rows = ProgrammeRequirement.objects.filter(
        course_code__in=set(source_to_display),
        programme_term__isnull=False,
    ).values_list("program", "course_code", "course_name", "programme_term")
    buckets: dict[tuple[str, int], set[str]] = {}
    identity_by_display = {
        str(entry.get("course_code", "")): _course_identity_for_entry(entry)
        for entry in schedule_entries
    }
    for program, source, name, term in rows:
        key = (str(program), int(term or 0))
        row_identity = planner_course_key(source, name)
        matches = [
            display
            for display in source_to_display.get(str(source), [])
            if identity_by_display.get(display, display) == row_identity
        ]
        if not matches and all(
            identity_by_display.get(display, display) == str(source)
            for display in source_to_display.get(str(source), [])
        ):
            matches = list(source_to_display.get(str(source), []))
        buckets.setdefault(key, set()).update(matches)
    return buckets


def _course_buckets_from_plan_terms(
    plan_term_buckets: dict[tuple[str, int], set[str]],
) -> dict[str, list[tuple[str, int]]]:
    course_buckets: dict[str, list[tuple[str, int]]] = {}
    for bucket_key, courses in plan_term_buckets.items():
        for course_code in courses:
            course_buckets.setdefault(course_code, []).append(bucket_key)
    return course_buckets


def _normalise_loaded_schedule_entries(
    schedule_raw: list,
    days: list[str],
    periods: list[str],
    selected_courses: list[str] | None,
) -> list[dict]:
    selected = set(selected_courses) if selected_courses is not None else None
    slot_index_by_key: dict[tuple[str, str], int] = {}
    slots_len = 0
    for day in days:
        for period in periods:
            slot_index_by_key[(day, period)] = slots_len
            slots_len += 1

    schedule_entries: list[dict] = []
    overflow_idx = slots_len
    for raw in schedule_raw:
        if not isinstance(raw, dict):
            continue
        course_code = str(raw.get("course_code", "")).strip()
        day = str(raw.get("day", "")).strip()
        period = str(raw.get("period", "")).strip()
        if not course_code or not day or not period:
            continue
        if selected is not None and course_code not in selected:
            continue

        if day == "OVERFLOW":
            try:
                slot_index = int(raw.get("slot_index", overflow_idx))
            except (ValueError, TypeError):
                slot_index = overflow_idx
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
        entry["day"] = day
        entry["period"] = period
        entry["slot_index"] = slot_index
        entry["rooms"] = []
        schedule_entries.append(entry)

    if not schedule_entries:
        raise ValueError("Loaded schedule has no selected courses to rebuild.")
    return sorted(schedule_entries, key=lambda e: (int(e.get("slot_index", 0)), e["course_code"]))


def _student_section_gender(section_label: str) -> str:
    first = str(section_label or "").strip().upper()[:1]
    return first if first in {"M", "F"} else "M"


def _build_section_enrollment_from_sets(
    enrolled_sets: dict[str, set[int]],
) -> dict[str, list[dict]]:
    student_ids: set[int] = set()
    for sids in enrolled_sets.values():
        student_ids.update(sids)
    section_by_student = {
        int(sid): str(section or "").strip()
        for sid, section in Student.objects.filter(student_id__in=student_ids).values_list(
            "student_id",
            "section",
        )
    }

    result: dict[str, list[dict]] = {}
    for course_code, sids in enrolled_sets.items():
        per_section: dict[str, set[int]] = {}
        for sid in sids:
            section_label = section_by_student.get(int(sid), "") or "ALL"
            per_section.setdefault(section_label, set()).add(int(sid))
        result[course_code] = [
            {
                "section": section_label,
                "student_count": len(section_sids),
                "preferred_room": "",
                "gender": _student_section_gender(section_label),
            }
            for section_label, section_sids in sorted(
                per_section.items(),
                key=lambda item: (_student_section_gender(item[0]), item[0]),
            )
        ]
    return result


def _rooms_with_metadata() -> list[dict]:
    return list(
        Room.objects.all().values(
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


def _rebuild_loaded_schedule(
    *,
    label: str,
    days: list[str],
    periods: list[str],
    max_per_day: int,
    schedule_raw: list,
    selected_courses: list[str] | None,
    assign_rooms: bool,
    seed: int | None,
    thin_conflict_threshold: int,
    rebuild_mode: str = "loaded_schedule",
) -> dict:
    """Persist a loaded Previous Run after drag/drop edits.

    This path keeps the visible schedule exactly as loaded/moved instead
    of treating display rows such as ``CS112 (1)`` as fresh DB course
    codes. Enrolment, bucket, QA and room calculations are rebuilt from
    the course identity metadata carried by the schedule entries.
    """
    schedule_entries = _normalise_loaded_schedule_entries(
        schedule_raw,
        days,
        periods,
        selected_courses,
    )
    course_list = sorted({entry["course_code"] for entry in schedule_entries})
    enrolled_sets = _build_draft_enrolled_sets(schedule_entries)

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

    conflicts, _adj = build_conflict_graph(enrolled_sets)
    plan_term_buckets = _draft_plan_term_buckets(schedule_entries)
    qa = _build_qa(
        enrolled_sets,
        schedule_entries,
        max_per_day=max_per_day,
        plan_term_buckets=plan_term_buckets,
        credit_map=credit_map,
    )
    qa["thin_threshold"] = thin_conflict_threshold
    qa["thin_courses"] = []
    qa["thin_clash_risk"] = []

    section_enrollment: dict[str, list[dict]] = {}
    rooms_list: list[dict] = []
    room_feasibility: list[dict] = []
    if assign_rooms:
        section_enrollment = _build_section_enrollment_from_sets(enrolled_sets)
        rooms_list = _rooms_with_metadata()
        room_feasibility = check_room_feasibility(section_enrollment, rooms_list)
        for entry in schedule_entries:
            entry["rooms"] = []
        assign_rooms_to_schedule(schedule_entries, section_enrollment, rooms_list, seed=seed)
        _attach_room_metadata(schedule_entries, rooms_list)
        room_qa = _build_room_qa(schedule_entries, rooms_list)
        qa = _build_qa(
            enrolled_sets,
            schedule_entries,
            max_per_day=max_per_day,
            plan_term_buckets=plan_term_buckets,
            credit_map=credit_map,
        )
        qa["rooms"] = room_qa
        qa["room_feasibility_violations"] = room_feasibility
        qa["rebalance_moves"] = 0
        qa["thin_threshold"] = thin_conflict_threshold
        qa["thin_courses"] = []
        qa["thin_clash_risk"] = []

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
    qa["manual_override_count"] = qa.get("conflict_count", 0)
    qa["manual_override_details"] = list(qa.get("same_slot_conflicts", []))
    qa["building_footprint"] = derive_building_footprint(schedule_entries)

    sections_total = sum(len(v) for v in section_enrollment.values())
    synthetic_all = sum(
        1
        for sections in section_enrollment.values()
        for section in sections
        if str(section.get("section", "")).upper() == "ALL"
    )
    qa["enrolment_snapshot"] = compute_enrolment_snapshot(
        enrolled_sets,
        sections_count=sections_total,
        fallback_used=False,
        synthetic_all_sections_count=synthetic_all,
    )

    slots: list[dict] = []
    slot_idx = 0
    for day in days:
        for period in periods:
            slots.append({"index": slot_idx, "day": day, "period": period})
            slot_idx += 1

    draft = {
        "status": "ok",
        "students_count": len(all_students),
        "courses": course_list,
        "courses_count": len(course_list),
        "conflicts": conflicts,
        "conflicts_count": len(conflicts),
        "slots": slots,
        "schedule": schedule_entries,
        "qa": qa,
        "buckets_summary": buckets_summary,
        "bucket_count": len(plan_term_buckets),
        "credit_map": credit_map,
        "seed": seed,
        "section_enrollment": section_enrollment,
        "rooms_count": len(rooms_list),
        "assign_rooms": assign_rooms,
        "rebuild_mode": rebuild_mode,
    }
    primary_status, status_flags = derive_status_surface(draft)
    draft["primary_status"] = primary_status
    draft["status_flags"] = status_flags
    draft["status_derivation_version"] = STATUS_DERIVATION_VERSION
    result = stamp_schema_version(draft)

    run = ExamTimetableRun.objects.create(
        label=label,
        result_json=json.dumps(result, ensure_ascii=False),
    )
    result["run_id"] = run.id
    return result


def _optimise_loaded_schedule(
    *,
    label: str,
    days: list[str],
    periods: list[str],
    max_per_day: int,
    schedule_raw: list,
    selected_courses: list[str] | None,
    pinned: list[dict[str, str]] | None,
    assign_rooms: bool,
    seed: int | None,
    thin_conflict_threshold: int,
) -> dict:
    base_entries = _normalise_loaded_schedule_entries(
        schedule_raw,
        days,
        periods,
        selected_courses,
    )
    meta_by_course = {entry["course_code"]: entry for entry in base_entries}
    course_list = sorted(meta_by_course)
    enrolled_sets = _build_draft_enrolled_sets(base_entries)
    conflicts, adj = build_conflict_graph(enrolled_sets)
    plan_term_buckets = _draft_plan_term_buckets(base_entries)
    course_buckets = _course_buckets_from_plan_terms(plan_term_buckets)
    source_credit_map = build_credit_map(
        {
            _source_code_for_display(entry["course_code"], entry.get("source_course_code"))
            for entry in base_entries
        }
    )
    credit_map = {
        entry["course_code"]: source_credit_map.get(
            _source_code_for_display(entry["course_code"], entry.get("source_course_code")),
            3,
        )
        for entry in base_entries
    }

    slots: list[dict] = []
    idx = 0
    for day in days:
        for period in periods:
            slots.append({"index": idx, "day": day, "period": period})
            idx += 1
    preferred_slots = {
        entry["course_code"]: int(entry.get("slot_index", 0) or 0) for entry in base_entries
    }
    optimised = schedule(
        course_list,
        adj,
        slots,
        enrolled_sets=enrolled_sets,
        max_per_day=max_per_day,
        plan_term_buckets=plan_term_buckets,
        course_buckets=course_buckets,
        pinned=pinned,
        credit_map=credit_map,
        preferred_slots=preferred_slots,
        seed=seed,
    )
    optimised_entries: list[dict] = []
    for entry in optimised:
        meta = meta_by_course.get(entry["course_code"], {})
        optimised_entries.append(
            {
                **entry,
                "source_course_code": meta.get("source_course_code"),
                "course_name": meta.get("course_name", ""),
                "course_identity": meta.get("course_identity", ""),
            }
        )
    return _rebuild_loaded_schedule(
        label=label,
        days=days,
        periods=periods,
        max_per_day=max_per_day,
        schedule_raw=optimised_entries,
        selected_courses=selected_courses,
        assign_rooms=assign_rooms,
        seed=seed,
        thin_conflict_threshold=thin_conflict_threshold,
        rebuild_mode="optimized_from_loaded",
    )


@require_POST
def exam_timetable_draft_impact_view(request: HttpRequest) -> JsonResponse:
    """Recompute lightweight QA for an unsaved drag/drop draft schedule.

    This intentionally does not run room assignment. Room utilisation,
    unassigned sections, multi-sitting, and building footprint remain the
    last backend-built values until the user rebuilds.
    """
    deny = _require_super_admin(request)
    if deny:
        return deny

    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except Exception:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    schedule_raw = payload.get("schedule", [])
    if not isinstance(schedule_raw, list):
        return JsonResponse({"ok": False, "error": "schedule must be a list"}, status=400)

    schedule_entries: list[dict] = []
    for entry in schedule_raw:
        if not isinstance(entry, dict):
            continue
        course_code = str(entry.get("course_code", "")).strip()
        day = str(entry.get("day", "")).strip()
        period = str(entry.get("period", "")).strip()
        if not course_code or not day or not period:
            continue
        try:
            slot_index = int(entry.get("slot_index", 0))
        except (ValueError, TypeError):
            slot_index = 0
        schedule_entries.append(
            {
                **entry,
                "course_code": course_code,
                "source_course_code": _source_code_for_display(
                    course_code, entry.get("source_course_code")
                ),
                "course_name": str(entry.get("course_name") or ""),
                "course_identity": _course_identity_for_entry(
                    {**entry, "course_code": course_code}
                ),
                "day": day,
                "period": period,
                "slot_index": slot_index,
            }
        )

    try:
        max_per_day = max(1, int(payload.get("max_per_day", 2)))
    except (ValueError, TypeError):
        max_per_day = 2

    enrolled_sets = _build_draft_enrolled_sets(schedule_entries)
    credit_map_raw = payload.get("credit_map", {})
    credit_map: dict[str, int] = {}
    if isinstance(credit_map_raw, dict):
        for code, value in credit_map_raw.items():
            try:
                credit_map[str(code)] = int(value or 3)
            except (ValueError, TypeError):
                credit_map[str(code)] = 3
    for code in enrolled_sets:
        credit_map.setdefault(code, 3)

    qa = _build_qa(
        enrolled_sets,
        schedule_entries,
        max_per_day=max_per_day,
        plan_term_buckets=_draft_plan_term_buckets(schedule_entries),
        credit_map=credit_map,
    )
    return JsonResponse({"ok": True, "qa": qa})


@require_GET
def exam_timetable_list_view(request: HttpRequest) -> JsonResponse:
    """Return a paginated list of saved exam-timetable runs (newest first)."""
    deny = _require_super_admin(request)
    if deny:
        return deny

    PAGE_SIZE = 10
    try:
        page = max(1, int(request.GET.get("page", 1)))
    except (ValueError, TypeError):
        page = 1

    qs = ExamTimetableRun.objects.order_by("-created_at").values("id", "label", "created_at")
    total = qs.count()
    total_pages = max(1, -(-total // PAGE_SIZE))  # ceil division
    page = min(page, total_pages)

    start = (page - 1) * PAGE_SIZE
    runs = list(qs[start : start + PAGE_SIZE])
    for r in runs:
        r["created_at"] = r["created_at"].isoformat() if r["created_at"] else ""  # type: ignore[typeddict-item]

    return JsonResponse(
        {
            "ok": True,
            "runs": runs,
            "page": page,
            "total_pages": total_pages,
            "total": total,
        }
    )


@require_GET
def exam_timetable_detail_view(request: HttpRequest, run_id: int) -> JsonResponse:
    """Load a previously saved exam-timetable run by ID (for the history panel)."""
    deny = _require_super_admin(request)
    if deny:
        return deny

    try:
        run = ExamTimetableRun.objects.get(id=run_id)
    except ExamTimetableRun.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Run not found"}, status=404)

    # Single read path: the normaliser handles legacy / corrupt /
    # missing-key payloads gracefully (returns ``status="unrenderable"``
    # rather than 500), so the UI receives a render-safe payload for
    # any historic row regardless of when it was stored.
    result = dict(load_normalised_run(run))
    result["run_id"] = run.id
    result["label"] = run.label
    result["created_at"] = run.created_at.isoformat() if run.created_at else ""

    return JsonResponse({"ok": True, **result})


@require_GET
def exam_timetable_export_view(request: HttpRequest, run_id: int) -> HttpResponseBase:
    """Download the exam timetable for a saved run as a styled .xlsx workbook."""
    deny = _require_super_admin(request)
    if deny:
        return deny

    try:
        path = export_exam_timetable_xlsx(run_id)
    except ExamTimetableRun.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Run not found"}, status=404)
    except RuntimeError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)

    return FileResponse(
        path.open("rb"),
        as_attachment=True,
        filename=f"exam_timetable_{run_id}.xlsx",
    )


@require_POST
def exam_timetable_delete_view(request: HttpRequest, run_id: int) -> JsonResponse:
    """Delete a saved exam-timetable run (requires confirm=DELETE)."""
    deny = _require_super_admin(request)
    if deny:
        return deny

    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)
    confirm = str(payload.get("confirm", ""))
    if confirm != "DELETE":
        log_audit_event(
            request,
            action="exam_timetable.delete_run",
            status="error",
            error_text="missing confirm=DELETE",
        )
        return JsonResponse(
            {"ok": False, "error": "Confirmation required: send confirm=DELETE"},
            status=400,
        )

    try:
        run = ExamTimetableRun.objects.get(id=run_id)
    except ExamTimetableRun.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Run not found"}, status=404)

    label = run.label
    run.delete()

    log_audit_event(
        request,
        action="exam_timetable.delete_run",
        status="success",
        details={"run_id": run_id, "label": label},
    )
    return JsonResponse({"ok": True})

"""
core/exam_views.py
Exam Timetable Builder — page view + API endpoints.

Super Admins and Exam Committee users share exam access; deletion is Super Admin only.

Endpoints:
    GET  exam_timetable_page          – render the single-page builder UI
    GET  exam_timetable_filters_view  – return programs/sections for filter dropdowns
    POST exam_timetable_preview_courses_view – return running courses matching filters
    POST exam_timetable_build_view    – build (or rebuild) the exam timetable
    GET  exam_timetable_list_view     – paginated list of saved runs
    GET  exam_timetable_detail_view   – load a specific saved run
    GET  exam_timetable_export_view   – download a run as .xlsx
    POST exam_timetable_copy_view     – copy a saved run without recalculating
    POST exam_timetable_delete_view   – delete a saved run (requires confirm=DELETE)
"""

from __future__ import annotations

import json
import re
from datetime import time
from io import BytesIO

from django.conf import settings
from django.db import transaction
from django.http import FileResponse, HttpRequest, HttpResponse, HttpResponseBase, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from core.authz import throttle
from core.models import ExamTimetableRun, Student
from core.services.audit import log_audit_event
from core.services.exam_evaluation import (
    _build_loaded_course_enrollments,
    _course_identity_for_entry,
    _normalise_loaded_schedule_entries,
    evaluate_exam_schedule,
)
from core.services.exam_multistart import (
    is_multistart_enabled,
    report_to_dict,
    run_multistart,
)
from core.services.exam_run_schema import (
    load_normalised_run,
)
from core.services.exam_timetable import (
    ExamCoursesUnavailable,
    _source_code_for_display,
    apply_thin_conflict_policy,
    build_conflict_graph,
    build_credit_map,
    build_enrolled_sets_with_meta,
    build_exam_timetable,
    build_plan_term_buckets,
    export_exam_timetable_xlsx,
    schedule,
)
from core.services.rbac import ROLE_EXAM_COMMITTEE, ROLE_SUPER_ADMIN, get_user_role
from core.sidebar_context import get_sidebar_context


def _require_super_admin(request: HttpRequest) -> JsonResponse | None:
    """Guard: returns a 403 JsonResponse if user is not SUPER_ADMIN, else None."""
    if get_user_role(request.user) != ROLE_SUPER_ADMIN:
        return JsonResponse({"error": "SUPER_ADMIN access required"}, status=403)
    return None


def _require_exam_access(request: HttpRequest) -> JsonResponse | None:
    """Exam runs are shared; access depends on role, never their creator."""
    if get_user_role(request.user) not in {ROLE_SUPER_ADMIN, ROLE_EXAM_COMMITTEE}:
        return JsonResponse({"error": "Exam Committee or SUPER_ADMIN access required"}, status=403)
    return None


def _exam_validation_error(exc: ValueError) -> JsonResponse:
    payload = {"ok": False, "error": str(exc)}
    if isinstance(exc, ExamCoursesUnavailable):
        payload.update(code="courses_unavailable", unavailable_courses=exc.unavailable_courses)
    return JsonResponse(payload, status=400)


@require_GET
def exam_timetable_page(request: HttpRequest) -> HttpResponse:
    """Render the exam timetable builder page (all logic is client-side JS)."""
    deny = _require_exam_access(request)
    if deny:
        return deny
    context = get_sidebar_context(request)
    context["can_delete_exam_timetable"] = get_user_role(request.user) == ROLE_SUPER_ADMIN
    return render(request, "core/exam_timetable.html", context)


@require_GET
def exam_timetable_filters_view(request: HttpRequest) -> JsonResponse:
    """Return distinct programs and sections for the filter dropdowns."""
    deny = _require_exam_access(request)
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


def _selected_enrollment_scope(payload: dict) -> tuple[list[str], list[str]]:
    """A screen selection must be explicit; clearing chips never means everyone."""
    selections = []
    for key, label in (("programs", "program"), ("sections", "section")):
        values = payload.get(key)
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(value, str) or not value.strip() for value in values)
        ):
            raise ValueError(f"Select at least one {label}.")
        selections.append(list(dict.fromkeys(value.strip() for value in values)))
    return selections[0], selections[1]


def _exam_header_settings(payload: dict) -> tuple[list[str], list[str], int]:
    """Validate the screen's clock periods before scheduling or persisting a run."""
    days = payload.get("days")
    if (
        not isinstance(days, list)
        or not 1 <= len(days) <= 60
        or any(not isinstance(day, str) or not day.strip() for day in days)
    ):
        raise ValueError("Choose between 1 and 60 exam days.")
    days = [day.strip() for day in days]
    if len(set(days)) != len(days):
        raise ValueError("Exam days must be unique.")

    raw_max = payload.get("max_per_day", 2)
    if isinstance(raw_max, bool) or not re.fullmatch(r"[0-9]+", str(raw_max)):
        raise ValueError("Max exams/day must be a whole number from 1 to 10.")
    max_per_day = int(raw_max)
    if not 1 <= max_per_day <= 10:
        raise ValueError("Max exams/day must be a whole number from 1 to 10.")

    periods = payload.get("periods")
    if not isinstance(periods, list) or not periods:
        raise ValueError("Add at least one exam period.")
    windows: list[tuple[time, time]] = []
    clean_periods = []
    for index, period in enumerate(periods, start=1):
        if not isinstance(period, str) or not re.fullmatch(
            r"[0-9]{2}:[0-9]{2}-[0-9]{2}:[0-9]{2}", period.strip()
        ):
            raise ValueError(f"Period {index} must use HH:MM-HH:MM (24-hour time).")
        start_raw, end_raw = period.strip().split("-")
        try:
            start, end = time.fromisoformat(start_raw), time.fromisoformat(end_raw)
        except ValueError as exc:
            raise ValueError(
                f"Period {index} must contain valid times from 00:00 to 23:59."
            ) from exc
        if end <= start:
            raise ValueError(f"Period {index} must end after it starts on the same day.")
        if any(start < other_end and other_start < end for other_start, other_end in windows):
            raise ValueError(f"Period {index} overlaps another exam period.")
        windows.append((start, end))
        clean_periods.append(f"{start_raw}-{end_raw}")
    return days, clean_periods, max_per_day


@require_POST
def exam_timetable_preview_courses_view(request: HttpRequest) -> JsonResponse:
    """Return actual scraped-timetable courses matching the selected population."""
    deny = _require_exam_access(request)
    if deny:
        return deny

    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except Exception:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"ok": False, "error": "Expected a JSON object."}, status=400)

    try:
        programs, sections = _selected_enrollment_scope(payload)
    except ValueError as exc:
        return _exam_validation_error(exc)

    enrolled_sets, course_meta = build_enrolled_sets_with_meta(
        programs=programs,
        sections=sections,
    )

    # Fetch credit hours for all preview courses
    course_codes = list(enrolled_sets.keys())
    credit_map = build_credit_map(course_codes)

    courses = sorted(
        [
            {
                "course_code": cc,
                **course_meta.get(cc, {}),
                "enrolled_count": len(sids),
                "credit_hours": credit_map.get(cc, 3),
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
    deny = _require_exam_access(request)
    if deny:
        return deny

    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except Exception:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"ok": False, "error": "Expected a JSON object."}, status=400)

    # ── Extract raw values from JSON payload ──
    label = str(payload.get("label", "")).strip()
    selected_courses_raw = payload.get("selected_courses", None)
    selected_entries = payload.get("selected_course_entries")
    if selected_entries is not None and (
        not isinstance(selected_entries, list)
        or any(not isinstance(entry, dict) for entry in selected_entries)
    ):
        return JsonResponse(
            {"ok": False, "error": "selected_course_entries must be a list of courses"}, status=400
        )
    pinned = payload.get("pinned", [])
    if not isinstance(pinned, list):
        return JsonResponse(
            {
                "ok": False,
                "error": "Pinned exams must be a list of course, day and period entries.",
            },
            status=400,
        )
    randomize = payload.get("randomize", False)
    assign_rooms = bool(payload.get("assign_rooms", True))
    thin_threshold_raw = payload.get("thin_conflict_threshold", 0)
    mode = str(payload.get("mode") or "").strip()

    if not label:
        return JsonResponse({"ok": False, "error": "label is required"}, status=400)

    try:
        days, periods, max_per_day = _exam_header_settings(payload)
    except ValueError as exc:
        return _exam_validation_error(exc)

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

    # User-curated course list from the preview step (None = use all)
    selected_courses = (
        [str(c).strip() for c in selected_courses_raw if str(c).strip()]
        if isinstance(selected_courses_raw, list)
        else None
    )

    # Generate a random seed when the user enables randomised tie-breaking.
    # Each build produces a different timetable variant; the seed is stored
    # in the result so it can be reproduced if needed.
    import random as _rnd

    seed = _rnd.randint(1, 2**31 - 1) if randomize else None
    base_schedule_raw = payload.get("base_schedule")
    if "base_schedule" in payload:
        try:
            context = _loaded_request_context(payload, base_schedule_raw)
            source_fingerprint = context.pop("source_input_fingerprint")
            if mode == "optimize_loaded":
                context["seed"] = seed if randomize else context["seed"]
                result = _optimise_loaded_schedule(label=label, **context)
            else:
                reviewed_fingerprint = (
                    payload.get("expected_input_fingerprint") or source_fingerprint
                )
                if not reviewed_fingerprint:
                    raise ExamCheckRequired(
                        "This timetable has no reviewed input snapshot. "
                        "Use Check changes and review the updated cards before saving."
                    )
                result = _rebuild_loaded_schedule(
                    label=label,
                    **context,
                    expected_input_fingerprint=reviewed_fingerprint,
                )
            return JsonResponse(
                {"ok": True, "editor_revision": payload.get("editor_revision", 0), **result}
            )
        except ExamCheckRequired as exc:
            return JsonResponse(
                {"ok": False, "error_code": "check_required", "error": str(exc)}, status=409
            )
        except ExamInputsChanged as exc:
            return JsonResponse(
                {"ok": False, "error_code": "inputs_changed", "error": str(exc)}, status=409
            )
        except ValueError as exc:
            return _exam_validation_error(exc)
        except Exception as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=500)

    try:
        programs, sections = _selected_enrollment_scope(payload)
    except ValueError as exc:
        return _exam_validation_error(exc)

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
                selected_course_entries=selected_entries,
                pinned=pinned,
                n_runs=n_runs,
                time_budget_s=time_budget_s,
                assign_rooms=assign_rooms,
                thin_conflict_threshold=thin_conflict_threshold,
                previous_run_id=previous_run_id,
            )
        except ValueError as exc:
            return _exam_validation_error(exc)
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
            selected_course_entries=selected_entries,
            pinned=pinned,
            seed=seed,
            assign_rooms=assign_rooms,
            thin_conflict_threshold=thin_conflict_threshold,
        )
        # Check for feasibility error (bucket too large for available days)
        if result.get("feasibility_error"):
            return JsonResponse({"ok": False, **result}, status=400)
        return JsonResponse({"ok": True, **result})
    except ValueError as exc:
        return _exam_validation_error(exc)
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)


def _saved_enrollment_scope(payload: dict) -> tuple[list[str], list[str]]:
    """Use the saved run's scope, never the page's possibly changed filter chips."""
    try:
        run_id = int(payload.get("previous_run_id") or "")
        run = ExamTimetableRun.objects.get(pk=run_id)
    except (TypeError, ValueError, ExamTimetableRun.DoesNotExist) as exc:
        raise ValueError("Reload the saved timetable before editing it.") from exc
    scope = load_normalised_run(run).get("enrollment_scope")
    if not isinstance(scope, dict) or any(
        not isinstance(scope.get(key), list)
        or any(not isinstance(value, str) for value in scope[key])
        for key in ("programs", "sections")
    ):
        raise ValueError("The timetable's enrollment scope is invalid. Build a new timetable.")
    return scope["programs"], scope["sections"]


def _loaded_request_context(payload: dict, schedule_raw: list) -> dict:
    """Resolve source settings and reject identities outside the loaded run."""
    run_id = payload.get("previous_run_id")
    if isinstance(run_id, bool):
        raise ValueError("Reload the saved timetable before editing it.")
    try:
        source = load_normalised_run(ExamTimetableRun.objects.get(pk=int(run_id)))
    except (TypeError, ValueError, ExamTimetableRun.DoesNotExist) as exc:
        raise ValueError("Reload the saved timetable before editing it.") from exc
    scope = source.get("enrollment_scope")
    if not isinstance(scope, dict) or any(
        not isinstance(scope.get(key), list)
        or any(not isinstance(value, str) for value in scope[key])
        for key in ("programs", "sections")
    ):
        raise ValueError("The timetable's enrollment scope is invalid. Build a new timetable.")
    settings_payload = {
        "days": list(dict.fromkeys(slot["day"] for slot in source.get("slots", []))),
        "periods": list(dict.fromkeys(slot["period"] for slot in source.get("slots", []))),
        "max_per_day": source.get("qa", {}).get("max_per_day", 2),
        **payload,
    }
    days, periods, max_per_day = _exam_header_settings(settings_payload)
    assign_rooms = payload.get("assign_rooms", source.get("assign_rooms", True))
    if not isinstance(assign_rooms, bool):
        raise ValueError("Room assignment must be enabled or disabled.")
    threshold = payload.get(
        "thin_conflict_threshold", source.get("qa", {}).get("thin_threshold", 0)
    )
    if isinstance(threshold, bool) or not isinstance(threshold, int) or not 0 <= threshold <= 10:
        raise ValueError("Tiny-course clash threshold must be a whole number from 0 to 10.")
    selected = payload.get("selected_courses")
    entries = _normalise_loaded_schedule_entries(schedule_raw, days, periods, selected)
    source_pairs = {
        (entry["course_code"], _course_identity_for_entry(entry))
        for entry in source.get("schedule", [])
    }
    if any(
        (entry["course_code"], entry["course_identity"]) not in source_pairs for entry in entries
    ):
        raise ValueError(
            "A current course code or identity is not part of the loaded timetable. Reload it first."
        )
    selected_entries = payload.get("selected_course_entries")
    if selected_entries is not None:
        if not isinstance(selected_entries, list) or any(
            not isinstance(row, dict) for row in selected_entries
        ):
            raise ValueError("Selected course entries must be a list of course identities.")
        current_pairs = {(entry["course_code"], entry["course_identity"]) for entry in entries}
        selected_pairs = {
            (str(row.get("course_code") or ""), _course_identity_for_entry(row))
            for row in selected_entries
        }
        if len(selected_entries) != len(entries) or current_pairs != selected_pairs:
            raise ValueError(
                "Selected course identities and current timetable placements must match."
            )
    revision = payload.get("editor_revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("The editor revision must be a non-negative whole number.")
    fingerprint = payload.get("expected_input_fingerprint")
    if fingerprint is not None and (
        not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
    ):
        raise ValueError("The checked input fingerprint is invalid. Check changes again.")
    return {
        "days": days,
        "periods": periods,
        "max_per_day": max_per_day,
        "schedule_raw": entries,
        "selected_courses": [entry["course_code"] for entry in entries],
        "programs": scope["programs"],
        "sections": scope["sections"],
        "assign_rooms": assign_rooms,
        "seed": source.get("seed"),
        "thin_conflict_threshold": threshold,
        "pinned": payload.get("pinned", source.get("pinned", [])),
        "source_input_fingerprint": source.get("input_fingerprint"),
    }


def _rebuild_loaded_schedule(
    *, label: str, expected_input_fingerprint: str | None = None, **kwargs
) -> dict:
    """Persist the same complete evaluation used by a non-saving Check."""
    result = evaluate_exam_schedule(**kwargs)
    if expected_input_fingerprint and expected_input_fingerprint != result["input_fingerprint"]:
        raise ExamInputsChanged(
            "Enrollment, course, room or policy inputs changed since the last check. "
            "Check changes again before saving."
        )
    run = ExamTimetableRun.objects.create(
        label=label,
        result_json=json.dumps(result, ensure_ascii=False),
    )
    result["run_id"] = run.id
    return result


class ExamInputsChanged(ValueError):
    """A Save was reviewed against a different authoritative input snapshot."""


class ExamCheckRequired(ValueError):
    """A source without provenance needs a reviewed Check before it can be saved."""


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
    programs: list[str] | None = None,
    sections: list[str] | None = None,
) -> dict:
    base_entries = _normalise_loaded_schedule_entries(
        schedule_raw,
        days,
        periods,
        selected_courses,
    )
    meta_by_course = {entry["course_code"]: entry for entry in base_entries}
    course_list = sorted(meta_by_course)
    enrolled_sets, course_meta = _build_loaded_course_enrollments(base_entries, programs, sections)
    conflicts, adj = build_conflict_graph(enrolled_sets)
    adj, _thin_courses = apply_thin_conflict_policy(enrolled_sets, adj, thin_conflict_threshold)
    plan_term_buckets, course_buckets = build_plan_term_buckets(
        set(course_list), course_meta, programs=programs
    )
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
        programs=programs,
        sections=sections,
        selected_courses=selected_courses,
        assign_rooms=assign_rooms,
        seed=seed,
        thin_conflict_threshold=thin_conflict_threshold,
        rebuild_mode="optimized_from_loaded",
        pinned=pinned,
    )


@require_POST
def exam_timetable_draft_impact_view(request: HttpRequest) -> JsonResponse:
    """Calculate all cards and rooms at the exact submitted exam times; never save."""
    deny = _require_exam_access(request)
    if deny:
        return deny
    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"ok": False, "error": "Expected a JSON object."}, status=400)
    try:
        schedule_raw = payload.get("base_schedule", payload.get("schedule"))
        context = _loaded_request_context(payload, schedule_raw)
        source_fingerprint = context.pop("source_input_fingerprint")
        result = evaluate_exam_schedule(**context)
        return JsonResponse(
            {
                "ok": True,
                "editor_revision": payload.get("editor_revision", 0),
                **result,
                "source_input_fingerprint": source_fingerprint,
                "source_inputs_changed": source_fingerprint != result["input_fingerprint"]
                if source_fingerprint
                else None,
            }
        )
    except ValueError as exc:
        return _exam_validation_error(exc)
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)


@require_GET
def exam_timetable_list_view(request: HttpRequest) -> JsonResponse:
    """Return a paginated list of saved exam-timetable runs (newest first)."""
    deny = _require_exam_access(request)
    if deny:
        return deny

    PAGE_SIZE = 10
    try:
        page = max(1, int(request.GET.get("page", 1)))
    except (ValueError, TypeError):
        page = 1

    qs = ExamTimetableRun.objects.order_by("-created_at", "-id").values("id", "label", "created_at")
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
    deny = _require_exam_access(request)
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


@require_POST
def exam_timetable_copy_view(request: HttpRequest, run_id: int) -> JsonResponse:
    """Clone the persisted snapshot; copying must never build or re-evaluate it."""
    deny = _require_exam_access(request)
    if deny:
        return deny
    try:
        if len(request.body) > 4096:
            raise ValueError("Copy options are too large.")
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
        if not isinstance(payload, dict) or set(payload) - {"label"}:
            raise ValueError("Copy options must contain only a timetable name.")
        if "label" in payload and not isinstance(payload["label"], str):
            raise ValueError("Enter a valid name for the copy.")
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    with transaction.atomic():
        try:
            source = ExamTimetableRun.objects.get(pk=run_id)
        except ExamTimetableRun.DoesNotExist:
            return JsonResponse({"ok": False, "error": "Run not found"}, status=404)
        maximum = ExamTimetableRun._meta.get_field("label").max_length
        assert maximum is not None  # The persisted name is a bounded CharField.
        if "label" in payload:
            label = payload["label"].strip()
        else:
            suffix = (
                " — نسخة"
                if str(getattr(request, "LANGUAGE_CODE", "en")).startswith("ar")
                else " — Copy"
            )
            label = source.label[: maximum - len(suffix)].rstrip() + suffix
        if not label or len(label) > maximum or re.search(r"[\x00-\x1f\x7f\ud800-\udfff]", label):
            return JsonResponse(
                {"ok": False, "error": f"Use a single-line name of 1–{maximum} characters."},
                status=400,
            )

        result = dict(load_normalised_run(source))
        if result.get("status") != "ok":
            return JsonResponse(
                {
                    "ok": False,
                    "code": "run_not_copyable",
                    "error": "This saved timetable cannot be opened by this application version and cannot be copied for editing.",
                },
                status=409,
            )
        # Keep even unknown/legacy fields verbatim. The normalised result is
        # only for the editor response, never a replacement for stored data.
        copied = ExamTimetableRun.objects.create(label=label, result_json=source.result_json)

    log_audit_event(
        request,
        action="exam_timetable.copy_run",
        status="success",
        details={"source_run_id": source.pk, "run_id": copied.pk, "label": copied.label},
    )
    result.update(
        ok=True,
        run_id=copied.pk,
        label=copied.label,
        created_at=copied.created_at.isoformat(),
        source_run_id=source.pk,
    )
    return JsonResponse(result, status=201)


@require_GET
def exam_timetable_export_view(request: HttpRequest, run_id: int) -> HttpResponseBase:
    """Download the exam timetable for a saved run as a styled .xlsx workbook."""
    deny = _require_exam_access(request)
    if deny:
        return deny

    try:
        path = export_exam_timetable_xlsx(run_id)
    except ExamTimetableRun.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Run not found"}, status=404)
    except ValueError as exc:
        return JsonResponse(
            {"ok": False, "code": "enrollment_source_changed", "error": str(exc)}, status=409
        )
    except RuntimeError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)

    # Each request gets a completed temporary workbook. Detach the response
    # before removing it, so Windows downloads never contend for a shared file.
    try:
        workbook_bytes = path.read_bytes()
    finally:
        path.unlink(missing_ok=True)
    return FileResponse(
        BytesIO(workbook_bytes),
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

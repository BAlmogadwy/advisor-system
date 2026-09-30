"""
core/exam_views.py
Exam Timetable Builder — page view + API endpoints.

Super Admins and Exam Committee users share exam access; deletion is Super Admin only.

Endpoints:
    GET  exam_timetable_page          – render the single-page builder UI
    GET  exam_timetable_filters_view  – return programs/sections for filter dropdowns
    POST exam_timetable_preview_courses_view – return running courses matching filters
    POST exam_timetable_build_view    – build (or rebuild) the exam timetable
    GET  exam_timetable_scope_courses_view – every live course of a saved run's scope
    GET  exam_timetable_list_view     – paginated list of saved runs
    GET  exam_timetable_detail_view   – load a specific saved run
    GET  exam_timetable_export_view   – download a run as .xlsx
    POST exam_timetable_copy_view     – copy a saved run without recalculating
    POST exam_timetable_delete_view   – delete a saved run (requires confirm=DELETE)
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import time
from io import BytesIO
from time import monotonic
from typing import Any

from django.conf import settings
from django.db import transaction
from django.http import FileResponse, HttpRequest, HttpResponse, HttpResponseBase, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from core.authz import throttle
from core.models import ExamTimetableRun, Student
from core.services import exam_add_courses as adding
from core.services import exam_jobs
from core.services.audit import log_audit_event
from core.services.exam_add_courses import AddCoursesError
from core.services.exam_evaluation import (
    _build_loaded_course_enrollments,
    _course_identity_for_entry,
    _normalise_loaded_schedule_entries,
    evaluate_exam_schedule,
    resolve_links_and_locks,
)
from core.services.exam_locks import (
    ExamLocks,
    ExamLocksError,
    current_exam_term,
    require_lock_list,
    settle_build_request,
)
from core.services.exam_min_change import (
    find_violations,
    place_added_exams,
    repair_minimum_change,
)
from core.services.exam_multistart import (
    is_multistart_enabled,
    report_to_dict,
    run_multistart,
)
from core.services.exam_optimise import DEFAULT_ROUNDS, Board, optimise_board
from core.services.exam_optimise import score as optimise_score
from core.services.exam_progress import JobCancelled
from core.services.exam_progress import current as current_progress
from core.services.exam_room_allocation import normalized_rooms
from core.services.exam_room_inventory import exam_room_inventory
from core.services.exam_run_schema import (
    load_normalised_run,
)
from core.services.exam_timetable import (
    ExamCoursesUnavailable,
    _build_section_enrollment_from_enrolled_sets,
    _invigilators_needed,
    _invigilators_per_day,
    _physical_exam_rooms,
    _source_code_for_display,
    apply_thin_conflict_policy,
    build_conflict_graph,
    build_credit_map,
    build_enrolled_sets_with_meta,
    build_exam_timetable,
    build_plan_term_buckets,
    export_exam_timetable_xlsx,
    is_heavy_credit_day,
    schedule_linked,
    validate_exam_pins,
)
from core.services.job_runtime import (
    HOLDER_CHECK,
    HOLDER_EXAM_JOB,
    HOLDER_EXAM_SYNC,
    HOLDER_MULTISTART,
    HOLDER_PLANNER,
    SolverBusy,
    solver_holder,
    solver_slot,
)
from core.services.linked_exams import (
    LinkedExams,
    LinkedExamsError,
    linked_exams_qa,
    require_link_list,
)
from core.services.rbac import ROLE_EXAM_COMMITTEE, ROLE_SUPER_ADMIN, get_user_role
from core.sidebar_context import get_sidebar_context

logger = logging.getLogger(__name__)


def _require_super_admin(request: HttpRequest) -> JsonResponse | None:
    """Guard: returns a 403 JsonResponse if user is not SUPER_ADMIN, else None."""
    if get_user_role(request.user) != ROLE_SUPER_ADMIN:
        return JsonResponse({"error": "SUPER_ADMIN access required"}, status=403)
    return None


def _exam_role(request: HttpRequest) -> str:
    """The caller's role, looked up once per request."""
    role = getattr(request, "_exam_role", None)
    if role is None:
        role = get_user_role(request.user)
        request._exam_role = role  # type: ignore[attr-defined]
    return role


def _require_exam_access(request: HttpRequest) -> JsonResponse | None:
    """Exam runs are shared; access depends on role, never their creator."""
    if _exam_role(request) not in {ROLE_SUPER_ADMIN, ROLE_EXAM_COMMITTEE}:
        return JsonResponse({"error": "Exam Committee or SUPER_ADMIN access required"}, status=403)
    return None


def _exam_validation_error(exc: ValueError) -> JsonResponse:
    status, body = _validation_error(exc)
    return JsonResponse(body, status=status)


@never_cache
@require_GET
def exam_timetable_page(request: HttpRequest) -> HttpResponse:
    """Render the exam timetable builder page (all logic is client-side JS)."""
    deny = _require_exam_access(request)
    if deny:
        return deny
    context = get_sidebar_context(request)
    context["can_delete_exam_timetable"] = get_user_role(request.user) == ROLE_SUPER_ADMIN
    # Who may change a saved timetable - lock or unlock a day or period among
    # it - is who may Save: the rule every build and save endpoint applies.
    context["can_edit_exam_timetable"] = _require_exam_access(request) is None
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

    # The same rows the Add courses list shows for a saved run's scope.
    return JsonResponse({"ok": True, "courses": _scope_course_rows(programs, sections)})


def _scope_course_rows(programs: list[str] | None, sections: list[str] | None) -> list[dict]:
    """Every live course of a scope, as Load Courses and the Add courses list show it."""
    enrolled_sets, course_meta = build_enrolled_sets_with_meta(
        programs=programs,
        sections=sections,
    )

    # Fetch credit hours for all preview courses
    course_codes = list(enrolled_sets.keys())
    credit_map = build_credit_map(course_codes)

    return sorted(
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


def _saved_scope(source: dict) -> tuple[list[str], list[str]] | None:
    scope = source.get("enrollment_scope")
    if not isinstance(scope, dict) or any(
        not isinstance(scope.get(key), list)
        or any(not isinstance(value, str) for value in scope[key])
        for key in ("programs", "sections")
    ):
        return None
    return scope["programs"], scope["sections"]


@never_cache
@require_GET
def exam_timetable_scope_courses_view(request: HttpRequest, run_id: int) -> JsonResponse:
    """Every live course of a saved run's programmes and sections, the run's own marked.

    Read-only: what the Add courses list and the setup list show on a loaded
    timetable, so neither needs Load Courses (which starts a new timetable).
    The scope is the run's saved one, never the page's filter chips.
    """
    deny = _require_exam_access(request)
    if deny:
        return deny
    try:
        run = ExamTimetableRun.objects.get(pk=run_id)
    except ExamTimetableRun.DoesNotExist:
        return JsonResponse(
            {"ok": False, "code": "run_not_found", "error": "Run not found"}, status=404
        )
    source = dict(load_normalised_run(run))
    if source.get("status") != "ok":
        return JsonResponse(
            {
                "ok": False,
                "code": "run_not_readable",
                "error": "This saved timetable cannot be opened by this application version.",
            },
            status=400,
        )
    if _saved_scope(source) is None:
        return JsonResponse(
            {
                "ok": False,
                "code": "scope_invalid",
                "error": "The timetable's enrollment scope is invalid. Build a new timetable.",
            },
            status=400,
        )
    return JsonResponse(
        {
            "ok": True,
            "run_id": run.pk,
            **adding.scope_courses(
                source,
                rows=_scope_course_rows(*_saved_scope(source)),
                current_term=current_exam_term(),
            ),
        }
    )


# Throttle for the synchronous path: looser in development for fast tuning,
# tighter in production to keep this expensive endpoint from being hammered.
_BUILD_MAX_CALLS = 20 if settings.DEBUG else 3
# A background job is guarded by the one-active-job rule, not by counting calls:
# drag, Fix, drag, Fix, Save already spent three. This only stops abuse.
_JOB_SUBMIT_MAX_CALLS = 20

#: Persists a finished result as a run and returns its id.
RunSaver = Callable[[str, dict], int]


def _save_run(label: str, result: dict) -> int:
    return ExamTimetableRun.objects.create(
        label=label,
        result_json=json.dumps(result, ensure_ascii=False),
    ).id


@require_POST
def exam_timetable_build_view(request: HttpRequest) -> JsonResponse:
    """Build, optimise, repair or save the exam timetable.

    Accepts JSON body with: label, days, periods, max_per_day,
    programs, sections, selected_courses, pinned overrides,
    and optional randomize flag for varied timetable generation.

    With background jobs on, this only submits: the answer is 202 and a job to
    poll, and the action's own status and body arrive with the job's result.
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
    # A job's seed is drawn when it is submitted; the browser never chooses one.
    payload.pop("_seed", None)
    # Only a page that can follow a job asks for one. A tab still running the
    # previous release's script, or any other client, has the action run inside
    # its request as before - but, with jobs on, it takes turns: 409 while a job
    # holds the lane, 503 while anything holds the solver.
    if (
        exam_jobs.jobs_enabled()
        and request.headers.get("X-Exam-Jobs") == "1"
        and not exam_jobs.is_multistart(payload)
    ):
        return _submit_exam_job(request, payload)
    return _run_exam_action_now(request, payload)


@throttle(max_calls=_JOB_SUBMIT_MAX_CALLS, window_seconds=120)
def _submit_exam_job(request: HttpRequest, payload: dict) -> JsonResponse:
    status, body = exam_jobs.submit(
        payload, user=request.user, is_superadmin=_is_superadmin(request)
    )
    return JsonResponse(body, status=status)


@throttle(max_calls=_BUILD_MAX_CALLS, window_seconds=120)
def _run_exam_action_now(request: HttpRequest, payload: dict) -> JsonResponse:
    if not exam_jobs.jobs_enabled():
        status, body = execute_exam_action(payload)
        return JsonResponse(body, status=status)
    # With jobs on, what runs here - multistart, or a client that did not ask
    # for a job - takes turns with them: it may not start while a job holds the
    # lane, nor share the solver.
    if exam_jobs.lane_busy():
        return JsonResponse(exam_jobs.busy_body(request.user), status=409)
    holder = HOLDER_MULTISTART if exam_jobs.is_multistart(payload) else HOLDER_EXAM_SYNC
    try:
        with solver_slot(holder=holder, wait=False):
            status, body = execute_exam_action(payload)
    except SolverBusy as busy:
        return _solver_busy_response(busy.holder)
    return JsonResponse(body, status=status)


def _solver_busy_response(holder: dict | None) -> JsonResponse:
    response = JsonResponse(
        {
            "ok": False,
            "error_code": "solver_busy",
            "error": "Another timetable action is using the solver. Try again when it finishes.",
            # What it waits for: a planner run takes minutes, a Check seconds.
            "holder": holder,
        },
        status=503,
    )
    # A planner run holds it for minutes, an exam job one or two - and the page
    # following that job checks again the moment it ends - a Check seconds.
    # Asking more often would only log a refusal each time.
    kind = (holder or {}).get("kind")
    response["Retry-After"] = {HOLDER_PLANNER: "30", HOLDER_EXAM_JOB: "15"}.get(kind, "5")
    return response


def _validation_error(exc: ValueError) -> tuple[int, dict]:
    body = {"ok": False, "error": str(exc)}
    if isinstance(exc, ExamCoursesUnavailable):
        body.update(code="courses_unavailable", unavailable_courses=exc.unavailable_courses)
    elif isinstance(exc, LinkedExamsError):
        # Which link, and which of its members, the page should point at.
        body.update(code=exc.code, field=exc.field)
    elif isinstance(exc, ExamLocksError):
        # Which lock, and the courses and cell it is about.
        body.update(code=exc.code, field=exc.field)
        if exc.courses is not None:
            body["courses"] = exc.courses
        if exc.cell is not None:
            body["cell"] = exc.cell
    elif isinstance(exc, AddCoursesError):
        # Which course to add, and why it cannot be.
        body.update(code=exc.code, field=exc.field)
        if exc.courses is not None:
            body["courses"] = exc.courses
    return 400, body


def execute_exam_action(payload: dict, *, save: RunSaver = _save_run) -> tuple[int, dict]:
    """Run one exam timetable action; return the HTTP status and body for the page.

    The synchronous view and the background job both call this, so the page
    receives the same status and body either way. ``save`` persists a finished
    result: at once for the synchronous view; for a job, in the same
    transaction that marks the job finished.
    """
    # ── Extract raw values from JSON payload ──
    label = str(payload.get("label", "")).strip()
    selected_courses_raw = payload.get("selected_courses", None)
    selected_entries = payload.get("selected_course_entries")
    if selected_entries is not None and (
        not isinstance(selected_entries, list)
        or any(not isinstance(entry, dict) for entry in selected_entries)
    ):
        return 400, {"ok": False, "error": "selected_course_entries must be a list of courses"}
    pinned = payload.get("pinned", [])
    if not isinstance(pinned, list):
        return 400, {
            "ok": False,
            "error": "Pinned exams must be a list of course, day and period entries.",
        }
    # Validated with the courses they name, where the build resolves them. Only
    # the shape is checked here: a JSON null is refused, never read as no links.
    linked_exams = payload.get("linked_exams", [])
    try:
        require_link_list(linked_exams)
    except LinkedExamsError as exc:
        return _validation_error(exc)
    randomize = payload.get("randomize", False)
    assign_rooms = bool(payload.get("assign_rooms", True))
    thin_threshold_raw = payload.get("thin_conflict_threshold", 0)
    mode = str(payload.get("mode") or "").strip()

    if not label:
        return 400, {"ok": False, "error": "label is required"}

    try:
        days, periods, max_per_day = _exam_header_settings(payload)
    except ValueError as exc:
        return _validation_error(exc)

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

    # Randomised tie-breaking draws a seed per build, stored with the result so
    # it can be reproduced. A job draws it when it is submitted, so the seed is
    # part of what was asked for rather than of when it happened to run.
    seed = exam_jobs.resolve_seed(payload) if randomize else None
    base_schedule_raw = payload.get("base_schedule")
    if "base_schedule" in payload:
        try:
            current_progress().stage("read_board")
            if mode == adding.MODE:
                # Before anything else is read: "Reload the saved timetable"
                # names no source, and adding needs one.
                _require_add_source(payload)
            context = _loaded_request_context(payload, base_schedule_raw)
            provenance = _split_provenance(context)
            source_fingerprint = provenance["source_input_fingerprint"]
            if mode == "optimize_loaded":
                context["seed"] = seed if randomize else context["seed"]
                result = _optimise_loaded_schedule(label=label, save=save, **context)
            elif mode == "minimum_change_repair":
                result = _minimum_change_schedule(
                    label=label,
                    source_placements=provenance["source_placements"],
                    carried_protection=provenance["source_repair_protected"],
                    save=save,
                    **context,
                )
            elif mode == adding.MODE:
                result = _add_courses_schedule(
                    label=label,
                    added_courses=payload.get("added_courses"),
                    carried_protection=provenance["source_repair_protected"],
                    source_run_id=int(payload["previous_run_id"]),
                    save=save,
                    **context,
                )
            elif mode != "save_loaded_changes":
                # Save used to be the fallthrough for any action. A request the
                # server did not recognise then persisted the submitted board -
                # clashes and all - and the page reported it as done.
                raise ValueError(f"Unknown timetable action: {mode or 'none'}.")
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
                    save=save,
                    **context,
                    expected_input_fingerprint=reviewed_fingerprint,
                )
            return 200, {"ok": True, "editor_revision": payload.get("editor_revision", 0), **result}
        except ExamCheckRequired as exc:
            return 409, {"ok": False, "error_code": "check_required", "error": str(exc)}
        except ExamInputsChanged as exc:
            return 409, {"ok": False, "error_code": "inputs_changed", "error": str(exc)}
        except ValueError as exc:
            return _validation_error(exc)
        except JobCancelled:
            raise
        except Exception:
            return _server_error("loaded timetable action", mode)

    if mode == adding.MODE:
        # Routed by the board, not by the mode: without one, adding courses
        # would fall through to a Build and save a new timetable.
        return _validation_error(_add_source_required())

    try:
        programs, sections = _selected_enrollment_scope(payload)
        # The locks a Build keeps, and the saved run they come from. Without
        # locks this reads nothing and changes nothing: master's Build.
        exam_locks, linked_exams, lock_source = settle_build_request(payload)
    except ValueError as exc:
        return _validation_error(exc)
    lock_args: dict[str, Any] = (
        {"exam_locks": exam_locks, "lock_source": lock_source} if exam_locks else {}
    )

    # Multi-start is feature-flagged. When TIMETABLE_EXAM_MULTISTART_ENABLED
    # is set and the request opts in (``multistart=True``), the runner
    # explores N seeded builds and returns the 4 mechanically-defined
    # Pareto candidates ("recommended" / "lowest_overflow" /
    # "lowest_overload" / "best_room_feasibility") in a single response.
    # The single-run path remains the default; existing client code is
    # unaffected. It never runs as a background job (see exam_jobs).
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
                linked_exams=linked_exams,
                n_runs=n_runs,
                time_budget_s=time_budget_s,
                assign_rooms=assign_rooms,
                thin_conflict_threshold=thin_conflict_threshold,
                previous_run_id=previous_run_id,
                **lock_args,
            )
        except ValueError as exc:
            return _validation_error(exc)
        except Exception:
            return _server_error("multistart build", mode)

        if report.feasibility_error is not None and not report.candidates_by_role:
            return 400, {"ok": False, "multistart": report_to_dict(report)}
        return 200, {"ok": True, "mode": "multistart", "multistart": report_to_dict(report)}

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
            persist=False,
            linked_exams=linked_exams,
            **lock_args,
        )
        # Check for feasibility error (bucket too large for available days)
        if result.get("feasibility_error"):
            return 400, {"ok": False, **result}
        result["run_id"] = save(label, result)
        return 200, {"ok": True, **result}
    except ValueError as exc:
        return _validation_error(exc)
    except JobCancelled:
        raise
    except Exception:
        return _server_error("build", mode)


def _server_error(action: str, mode: str) -> tuple[int, dict]:
    """A failure the page cannot act on: logged in full, reported plainly.

    The exception text used to go to the browser, which leaked internals and
    told the registrar nothing they could use.
    """
    logger.exception("exam timetable %s failed (mode=%s)", action, mode or "build")
    return 500, {
        "ok": False,
        "error": "The timetable could not be produced because of a server error. "
        "Nothing was saved. Please try again.",
    }


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


#: Keys _loaded_request_context carries about the SOURCE run rather than about
#: the evaluation. Every consumer splats the rest straight into an evaluator, so
#: they must come out first - popping each one by hand at each call site is how a
#: new key reaches evaluate_exam_schedule and turns Check into a 500.
_PROVENANCE_KEYS = (
    "source_input_fingerprint",
    "source_placements",
    "source_repair_protected",
)
# ``exam_locks`` and ``lock_source`` are NOT provenance: every evaluator takes
# them, so a lock is checked by Check, Save, Optimise and Fix alike.


def _split_provenance(context: dict) -> dict:
    """Remove the source run's provenance, leaving only evaluator arguments."""
    return {key: context.pop(key) for key in _PROVENANCE_KEYS}


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
        # Like the pins: what the page sends, else what the loaded run saved. A
        # sent null is refused, not taken as "no links": that would erase the
        # saved links and let Optimise split them with a 200.
        "linked_exams": require_link_list(payload["linked_exams"])
        if "linked_exams" in payload
        else source.get("linked_exams", []),
        # The same for the locks: what the page sends, else the loaded run's.
        # A lock ends only by an explicit unlock (``[]`` or a shorter list),
        # never by a client that did not send it; a sent null is refused.
        "exam_locks": require_lock_list(payload["exam_locks"])
        if "exam_locks" in payload
        else list(source.get("exam_locks") or []),
        # What a locked cell holds, with its rooms, is what this run saved.
        "lock_source": source,
        "source_input_fingerprint": source.get("input_fingerprint"),
        # Where each exam sat in the SAVED run, by (day, period). Slot numbers
        # would be wrong: adding a period renumbers every later slot, which
        # made almost the whole board look hand-moved and froze it. What
        # protects an exam placed from off the board - dragged out of OVERFLOW,
        # or added since the save - is the consumer's "absent or different"
        # rule, so recording OVERFLOW origins here changes nothing; the map
        # simply records every exam.
        "source_placements": {
            entry["course_code"]: (str(entry.get("day", "")), str(entry.get("period", "")))
            for entry in source.get("schedule", [])
            if entry.get("course_code")
        },
        # Exams a previous "Fix with fewest moves" had to protect. Each repair
        # saves a run, and that run becomes the next baseline - so without
        # carrying these forward, drag, Fix, drag, Fix lost the first drag.
        "source_repair_protected": [
            code for code in source.get("minimum_change_protected", []) if isinstance(code, str)
        ],
    }


def _rebuild_loaded_schedule(
    *,
    label: str,
    expected_input_fingerprint: str | None = None,
    rebalance_invigilators: bool = False,
    extra: dict | None = None,
    described: Callable[[dict], dict] | None = None,
    save: RunSaver = _save_run,
    **kwargs,
) -> dict:
    """Persist the same complete evaluation used by a non-saving Check.

    ``rebalance_invigilators`` is off for Save, which must persist the exact
    board the registrar is looking at. Only Optimise turns it on, because only
    Optimise re-solves the placements and so owes them the build post-pass.
    """
    result = evaluate_exam_schedule(**kwargs, rebalance_invigilators=rebalance_invigilators)
    if expected_input_fingerprint and expected_input_fingerprint != result["input_fingerprint"]:
        raise ExamInputsChanged(
            "Enrollment, course, room or policy inputs changed since the last check. "
            "Check changes again before saving."
        )
    # Provenance a mode wants remembered with the run, merged before it is
    # saved so a reload from history carries it too. Top-level only: qa is
    # compared between Build and Check, and nothing a Check cannot reproduce
    # may live there. ``described`` is given the evaluated result, for a report
    # about the rooms the evaluation chose.
    result.update(extra or {})
    if described is not None:
        result.update(described(result))
    result["run_id"] = save(label, result)
    return result


class ExamInputsChanged(ValueError):
    """A Save was reviewed against a different authoritative input snapshot."""


class ExamCheckRequired(ValueError):
    """A source without provenance needs a reviewed Check before it can be saved."""


@dataclass(frozen=True)
class _LoadedSolverInputs:
    """Everything a solver needs about a loaded board, built from one place.

    Optimise and the minimum-change repair both reason about the same conflict
    graph and the same buckets. Building them twice is how two solvers end up
    quietly disagreeing about what a clash is.
    """

    meta_by_course: dict[str, dict]
    course_list: list[str]
    enrolled_sets: dict[str, set[int]]
    adj: dict[str, dict[str, int]]
    plan_term_buckets: dict[tuple[str, int], set[str]]
    course_buckets: dict[str, list[tuple[str, int]]]
    credit_map: dict[str, int]
    slots: list[dict]
    #: The live metadata of each course, as the evaluation reads it.
    course_meta: dict[str, dict] = field(default_factory=dict)


def _loaded_solver_inputs(
    base_entries: list[dict],
    days: list[str],
    periods: list[str],
    programs: list[str] | None,
    sections: list[str] | None,
    thin_conflict_threshold: int,
) -> _LoadedSolverInputs:
    meta_by_course = {entry["course_code"]: entry for entry in base_entries}
    course_list = sorted(meta_by_course)
    enrolled_sets, course_meta = _build_loaded_course_enrollments(base_entries, programs, sections)
    _conflicts, adj = build_conflict_graph(enrolled_sets)
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
    slots = [
        {"index": index, "day": day, "period": period}
        for index, (day, period) in enumerate((day, period) for day in days for period in periods)
    ]
    return _LoadedSolverInputs(
        meta_by_course=meta_by_course,
        course_list=course_list,
        enrolled_sets=enrolled_sets,
        adj=adj,
        plan_term_buckets=plan_term_buckets,
        course_buckets=course_buckets,
        credit_map=credit_map,
        slots=slots,
        course_meta=course_meta,
    )


def _loaded_links_and_locks(
    linked_exams: list[dict] | None,
    exam_locks: list[dict] | None,
    lock_source: dict | None,
    inputs: _LoadedSolverInputs,
    pinned: list[dict[str, str]],
    base_entries: list[dict],
    assign_rooms: bool,
) -> tuple[LinkedExams, ExamLocks]:
    """The links and locks of a loaded board, resolved once, the same way for Optimise and Fix.

    Both start from the board the registrar submitted, so a link whose members
    sit apart there is refused, and so is an exam moved into or out of a locked
    cell - moving an exam is the registrar's decision, not a side effect of
    pressing a solver button. A refused board never reaches a solver.
    """
    days = list(dict.fromkeys(slot["day"] for slot in inputs.slots))
    periods = list(dict.fromkeys(slot["period"] for slot in inputs.slots))
    return resolve_links_and_locks(
        linked_exams,
        exam_locks,
        lock_source,
        inputs.meta_by_course,
        pinned=pinned,
        board=base_entries,
        days=days,
        periods=periods,
        assign_rooms=assign_rooms,
    )


def _lock_kwargs(locks: ExamLocks, lock_source: dict | None) -> dict[str, Any]:
    """What a solver hands the evaluation that saves its board: the locks it kept.

    Nothing without locks, so the evaluation is called exactly as before.
    """
    return {"exam_locks": locks.saved(), "lock_source": lock_source} if locks else {}


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
    linked_exams: list[dict] | None = None,
    exam_locks: list[dict] | None = None,
    lock_source: dict | None = None,
    save: RunSaver = _save_run,
) -> dict:
    base_entries = _normalise_loaded_schedule_entries(
        schedule_raw,
        days,
        periods,
        selected_courses,
    )
    inputs = _loaded_solver_inputs(
        base_entries, days, periods, programs, sections, thin_conflict_threshold
    )
    valid_pins = validate_exam_pins(pinned, inputs.course_list, inputs.slots)
    links, locks = _loaded_links_and_locks(
        linked_exams, exam_locks, lock_source, inputs, valid_pins, base_entries, assign_rooms
    )
    progress = current_progress()
    progress.stage("place_exams")
    if getattr(settings, "EXAM_OPTIMISE_EXACT", True):
        return _solver_optimised_schedule(
            label=label,
            base_entries=base_entries,
            inputs=inputs,
            valid_pins=valid_pins,
            links=links,
            locks=locks,
            lock_source=lock_source,
            seed=seed,
            save=save,
            days=days,
            periods=periods,
            max_per_day=max_per_day,
            programs=programs,
            sections=sections,
            selected_courses=selected_courses,
            assign_rooms=assign_rooms,
            thin_conflict_threshold=thin_conflict_threshold,
            pinned=pinned,
        )
    preferred_slots = {
        entry["course_code"]: int(entry.get("slot_index", 0) or 0) for entry in base_entries
    }
    lock_args: dict[str, Any] = (
        {"locked": locks.pins(), "closed_slots": locks.closed_slots(inputs.slots)} if locks else {}
    )
    optimised = schedule_linked(
        inputs.course_list,
        inputs.adj,
        inputs.slots,
        links=links,
        enrolled_sets=inputs.enrolled_sets,
        max_per_day=max_per_day,
        plan_term_buckets=inputs.plan_term_buckets,
        course_buckets=inputs.course_buckets,
        # A locked exam is placed by its lock; a pin of it at its own cell adds nothing.
        pinned=[pin for pin in valid_pins if pin["course_code"] not in locks.placements]
        if locks
        else pinned,
        credit_map=inputs.credit_map,
        preferred_slots=preferred_slots,
        seed=seed,
        on_placed=progress.counter("place_exams"),
        **lock_args,
    )
    optimised_entries: list[dict] = []
    for entry in optimised:
        meta = inputs.meta_by_course.get(entry["course_code"], {})
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
        # Optimise re-solved every placement, discarding the day assignments the
        # original build's invigilator pass chose. Re-run that pass so the
        # optimised board is the same class of artefact a build produces.
        rebalance_invigilators=True,
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
        linked_exams=links.saved(),
        save=save,
        **_lock_kwargs(locks, lock_source),
    )


#: Rounds of the optimiser for an action that runs inside its request: about a
#: tenth of a job's, which on the real board already takes 443 students with
#: two exams on one day down to about 180.
_SYNC_OPTIMISE_ROUNDS = 50

#: The kinds of invigilator a day is limited in: men, women, and both together.
_STAFF_KINDS = ("M", "F", "total")


def _exam_staff(source: dict | None, links: LinkedExams, exams: list[str]) -> dict[str, dict]:
    """What each exam needs of each kind of invigilator, as the saved run roomed it.

    An exam's rooms barely depend on its period, so the rooms the saved run gave
    it say what it will need wherever it sits. An exam the saved run gave no
    room - it was in OVERFLOW - is counted from its sections instead, one room
    each. Linked courses share rooms, so a link is counted as one exam.

    An estimate: the evaluation rooms every period afresh, and a different
    split of a section can need one invigilator more or fewer. The report
    therefore also states the busiest day the saved board really has.
    """
    staff: dict[str, dict[str, int]] = {}
    if not source:
        return staff
    known = set(exams)
    roomed: dict[str, list[tuple[dict, dict]]] = {}
    for entry in source.get("schedule") or []:
        unit = links.unit(str(entry.get("course_code", "")))
        if unit in known:
            roomed.setdefault(unit, []).extend(
                (entry, room) for room in entry.get("rooms") or [] if isinstance(room, dict)
            )
    sections = source.get("section_enrollment") or {}
    for unit in exams:
        need = dict.fromkeys(_STAFF_KINDS, 0)
        for counts in _invigilators_per_day(_physical_exam_rooms(roomed.get(unit, []))).values():
            for kind in _STAFF_KINDS:
                need[kind] += counts[kind]
        if not roomed.get(unit):
            for code in links.members_of(unit):
                for row in sections.get(code) or []:
                    gender = "F" if row.get("gender") == "F" else "M"
                    invigilators = _invigilators_needed(code, int(row.get("student_count") or 0))
                    need[gender] += invigilators
                    need["total"] += invigilators
        staff[unit] = need
    return staff


def _staff_peak(
    staff: dict[str, dict], placements: dict[str, int], periods_per_day: int
) -> dict[str, int]:
    """The most of each kind of invigilator the board uses on one day."""
    load: dict[tuple[int, str], int] = {}
    for exam, slot in placements.items():
        for kind, need in staff.get(exam, {}).items():
            key = (slot // periods_per_day, kind)
            load[key] = load.get(key, 0) + need
    return {
        kind: max((used for (_, k), used in load.items() if k == kind), default=0)
        for kind in _STAFF_KINDS
    }


def _solver_optimised_schedule(
    *,
    label: str,
    base_entries: list[dict],
    inputs: _LoadedSolverInputs,
    valid_pins: list[dict[str, str]],
    links: LinkedExams,
    locks: ExamLocks,
    lock_source: dict | None,
    seed: int | None,
    save: RunSaver,
    days: list[str],
    periods: list[str],
    max_per_day: int,
    programs: list[str] | None,
    sections: list[str] | None,
    selected_courses: list[str] | None,
    assign_rooms: bool,
    thin_conflict_threshold: int,
    pinned: list[dict[str, str]] | None,
) -> dict:
    """Optimise by solving the whole board at once (``exam_optimise``).

    Pinned exams and the exams of locked cells never move, no exam enters a
    locked cell, and a link moves as one exam. The search holds each day's
    invigilators to the most the submitted board uses on any day, by its
    estimate of what each exam needs, so the search - not a pass after it -
    keeps the staff level while it spares the students.

    A board the search cannot better is not saved again, provided the request
    asks for nothing else: pressing Optimise on an already optimised timetable
    used to add a copy to the history each time. A request that also changes
    a pin, a link, a lock or a setting is saved, as it always was.
    """
    exams = list(links.units(inputs.course_list))
    slot_of = {(slot["day"], slot["period"]): slot["index"] for slot in inputs.slots}
    submitted = links.placements(
        {
            entry["course_code"]: int(entry["slot_index"])
            for entry in base_entries
            if entry.get("day") != "OVERFLOW"
        }
    )
    current = dict(submitted)
    # A pin says where its exam sits, whatever the submitted board shows; a
    # locked exam sits in its cell. Both are placed first, as the greedy did.
    fixed_at = links.pins(
        [pin for pin in valid_pins if not locks or pin["course_code"] not in locks.placements]
        + (locks.pins() if locks else [])
    )
    for pin in fixed_at or []:
        current[pin["course_code"]] = slot_of[pin["day"], pin["period"]]
    fixed = frozenset(pin["course_code"] for pin in fixed_at or [])

    sittings: dict[int, list[tuple[str, int]]] = {}
    for code in inputs.course_list:
        credit = inputs.credit_map.get(code, 3)
        for student in inputs.enrolled_sets.get(code, ()):
            sittings.setdefault(student, []).append((links.unit(code), credit))
    unit_buckets, _ = links.buckets(inputs.plan_term_buckets, None)
    staff = _exam_staff(lock_source, links, exams) if assign_rooms else {}
    staff_limits = {
        kind: peak for kind, peak in _staff_peak(staff, current, len(periods)).items() if peak
    }
    board = Board(
        exams=exams,
        current=current,
        fixed=fixed,
        adj=links.adjacency(inputs.adj),
        buckets=unit_buckets or {},
        slot_count=len(inputs.slots),
        periods_per_day=len(periods),
        sittings=sittings,
        max_per_day=max_per_day,
        closed_slots=locks.closed_slots(inputs.slots) if locks else frozenset(),
        weights=dict(links.weights),
        staff=staff,
        staff_limits=staff_limits,
    )
    progress = current_progress()
    # A background job may take its minutes; an action answering inside its
    # request has the web server's timeout to beat, so it searches far less.
    rounds = (
        int(getattr(settings, "EXAM_OPTIMISE_ROUNDS", DEFAULT_ROUNDS))
        if progress.background
        else int(getattr(settings, "EXAM_OPTIMISE_SYNC_ROUNDS", _SYNC_OPTIMISE_ROUNDS))
    )
    found = optimise_board(
        board,
        day_is_heavy=is_heavy_credit_day,
        rounds=rounds,
        seed=(seed or 0) % 2_000_000_000,
        on_round=progress.counter("place_exams"),
    )
    progress.check_cancelled()

    # Compared with the board as it was submitted: a pin that puts its exam
    # back in its place is a move, and may itself be the improvement.
    before = optimise_score(board, submitted, is_heavy_credit_day)
    report = {
        "improved": found.after < before,
        "proven": found.proven,
        "before": before.as_dict(),
        "after": found.after.as_dict(),
        "moved": sum(
            links.weight(exam)
            for exam in exams
            if found.placements.get(exam) != submitted.get(exam)
        ),
        "movable": sum(links.weight(exam) for exam in exams if exam not in fixed),
        "fixed": sum(links.weight(exam) for exam in fixed),
        "invigilator_day_limits": staff_limits,
    }
    unchanged = _asks_only_what_the_source_has(
        lock_source,
        days=days,
        periods=periods,
        max_per_day=max_per_day,
        thin_conflict_threshold=thin_conflict_threshold,
        assign_rooms=assign_rooms,
        pins=valid_pins,
        links=links,
        locks=locks,
    )
    if not found.improved and current == submitted and unchanged:
        # No better board was found, the board is exactly the one submitted,
        # and the request changes nothing else. Saving it again would add a
        # duplicate run to the history and, when the registrar has unsaved
        # drags, save them without the review Save asks for. The page keeps
        # the draft and shows the report.
        return {"saved": False, "optimisation": report}

    slot_by_index = {slot["index"]: slot for slot in inputs.slots}
    overflow_index = max(
        [len(inputs.slots) - 1]
        + [
            int(entry["slot_index"])
            for entry in base_entries
            if entry.get("day") == "OVERFLOW" and isinstance(entry.get("slot_index"), int)
        ]
    )
    # One Extra-n per unseated exam: a link leaves the board as one exam.
    shared_overflow: dict[str, int] = {}
    optimised_entries: list[dict] = []
    for entry in base_entries:
        unit = links.unit(entry["course_code"])
        if unit in found.placements:
            slot = slot_by_index[found.placements[unit]]
            optimised_entries.append(
                {**entry, "slot_index": slot["index"], "day": slot["day"], "period": slot["period"]}
            )
        elif entry.get("day") == "OVERFLOW":
            optimised_entries.append(entry)
        else:
            if unit not in shared_overflow:
                overflow_index += 1
                shared_overflow[unit] = overflow_index
            optimised_entries.append(
                {
                    **entry,
                    "day": "OVERFLOW",
                    "period": f"Extra-{shared_overflow[unit]}",
                    "slot_index": shared_overflow[unit],
                }
            )
    return _rebuild_loaded_schedule(
        label=label,
        # The search already held each day's invigilators to the board's own
        # peak. The pass that flattens them afterwards moves exams again and,
        # on the real board, put 80 to 115 more students on a two-exam day.
        rebalance_invigilators=False,
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
        linked_exams=links.saved(),
        save=save,
        **_lock_kwargs(locks, lock_source),
        # Top-level, never in qa: a Check cannot reproduce how a board was made.
        described=lambda result: {
            "optimisation": {
                **report,
                # The busiest day as the rooms really came out, beside the
                # saved run's: the limit above is held by an estimate.
                "invigilator_peak": {
                    "before": _invigilator_peak(lock_source),
                    "after": _invigilator_peak(result),
                },
            }
        },
    )


def _invigilator_peak(run: dict | None) -> int | None:
    """The most invigilators ``run`` needs on one day; None when it has no rooms."""
    rooms = ((run or {}).get("qa") or {}).get("rooms") or {}
    per_day = rooms.get("invigilators_per_day") or {}
    totals = [
        int(counts.get("total", 0)) for counts in per_day.values() if isinstance(counts, dict)
    ]
    return max(totals) if totals else None


def _asks_only_what_the_source_has(
    source: dict | None,
    *,
    days: list[str],
    periods: list[str],
    max_per_day: int,
    thin_conflict_threshold: int,
    assign_rooms: bool,
    pins: list[dict[str, str]],
    links: LinkedExams,
    locks: ExamLocks,
) -> bool:
    """The request's pins, links, locks and settings are the saved run's own.

    Placements are not compared: a drag is a draft the page keeps. Anything
    else the request changes lives only in the request, so an Optimise that
    saved nothing would lose it while answering that all is well.
    """
    if not source:
        return False
    slots = [slot for slot in source.get("slots") or [] if slot.get("day") != "OVERFLOW"]

    def canonical(value: Any) -> str:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)

    def pin_set(rows: Any) -> list[tuple[str, str, str]]:
        return sorted(
            (str(row.get("course_code")), str(row.get("day")), str(row.get("period")))
            for row in rows or []
            if isinstance(row, dict)
        )

    qa = source.get("qa") or {}
    return (
        list(dict.fromkeys(slot["day"] for slot in slots)) == list(days)
        and list(dict.fromkeys(slot["period"] for slot in slots)) == list(periods)
        and qa.get("max_per_day", 2) == max_per_day
        and qa.get("thin_threshold", 0) == thin_conflict_threshold
        and bool(source.get("assign_rooms", True)) == bool(assign_rooms)
        and pin_set(source.get("pinned")) == pin_set(pins)
        and canonical(source.get("linked_exams") or []) == canonical(links.saved())
        and canonical(source.get("exam_locks") or []) == canonical(locks.saved() if locks else [])
    )


def _minimum_change_schedule(
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
    source_placements: dict[str, tuple[str, str]],
    carried_protection: list[str] | None = None,
    programs: list[str] | None = None,
    sections: list[str] | None = None,
    linked_exams: list[dict] | None = None,
    exam_locks: list[dict] | None = None,
    lock_source: dict | None = None,
    save: RunSaver = _save_run,
) -> dict:
    """Repair the registrar's board by moving as few exams as possible.

    Optimise answers "what is the best board?"; this answers "what is the
    smallest change that makes this one legal?". Pinned exams, every exam moved
    since the board was last saved, and every exam an earlier repair in this run
    of repairs protected are all frozen, so the registrar's own work is never the
    thing that gets moved.

    A link is repaired as one exam that weighs as many courses as it has: it is
    frozen if any member is, moves whole, and goes to OVERFLOW whole, into one
    shared ``Extra-n``. The report still names every real course that moved.

    A locked exam is frozen too, and no exam moves into a locked cell. A clash
    among locked exams alone is left where it is and counted in the report
    (``locked_violations``): no repair may touch it.
    """
    base_entries = _normalise_loaded_schedule_entries(
        schedule_raw,
        days,
        periods,
        selected_courses,
    )
    inputs = _loaded_solver_inputs(
        base_entries, days, periods, programs, sections, thin_conflict_threshold
    )
    # Validate before use: a malformed pin 500'd here while Optimise returned a
    # 400, and an unstripped code silently went unprotected.
    pinned = validate_exam_pins(
        pinned, inputs.course_list, inputs.slots, schedule_entries=base_entries
    )
    links, locks = _loaded_links_and_locks(
        linked_exams, exam_locks, lock_source, inputs, pinned, base_entries, assign_rooms
    )
    current = {
        entry["course_code"]: int(entry["slot_index"])
        for entry in base_entries
        if entry.get("day") != "OVERFLOW"
    }
    # An exam counts as the registrar's if it now sits somewhere other than
    # where the saved run had it - compared by (day, period), and an exam the
    # saved run did not place on the board at all counts as moved.
    edited = {
        entry["course_code"]
        for entry in base_entries
        if entry.get("day") != "OVERFLOW"
        and source_placements.get(entry["course_code"]) != (entry["day"], entry["period"])
    }
    carried = {code for code in carried_protection or [] if code in current}
    hand_placed = edited | carried
    protected = hand_placed | {pin["course_code"] for pin in pinned}
    current_progress().stage("fewest_moves")
    closed: dict[str, Any] = {"closed_slots": locks.closed_slots(inputs.slots)} if locks else {}
    # The repair sees each link as one exam; with no links these are the
    # board's own placements, graph, buckets and protection.
    unit_buckets, _ = links.buckets(inputs.plan_term_buckets, None)
    repair = repair_minimum_change(
        placements=links.placements(current),
        adj=links.adjacency(inputs.adj),
        slot_count=len(inputs.slots),
        periods_per_day=len(periods),
        plan_term_buckets=unit_buckets,
        # A locked exam is frozen as firmly as a pin, and its cell is closed.
        protected=links.units_of(protected | set(locks.placements) if locks else protected),
        weights=links.weights or None,
        **closed,
    )
    placed = {
        code: slot for unit, slot in repair.placements.items() for code in links.members_of(unit)
    }
    moved = links.expand_codes(repair.moved)

    slot_by_index = {slot["index"]: slot for slot in inputs.slots}
    overflow_index = max(
        [len(inputs.slots) - 1]
        + [
            int(entry["slot_index"])
            for entry in base_entries
            if entry.get("day") == "OVERFLOW" and isinstance(entry.get("slot_index"), int)
        ]
    )
    unseated = set(links.expand_codes(repair.unseated))
    # One Extra-n per unseated unit: a link leaves the board as one exam.
    shared_overflow: dict[str, int] = {}
    repaired_entries: list[dict] = []
    for entry in base_entries:
        code = entry["course_code"]
        if code in unseated:
            # No legal slot exists without moving a protected exam. Park it in
            # OVERFLOW rather than leave it on the slot that broke the rules.
            unit = links.unit(code)
            if unit not in shared_overflow:
                overflow_index += 1
                shared_overflow[unit] = overflow_index
            repaired_entries.append(
                {
                    **entry,
                    "day": "OVERFLOW",
                    "period": f"Extra-{shared_overflow[unit]}",
                    "slot_index": shared_overflow[unit],
                }
            )
        elif code in placed:
            slot = slot_by_index[placed[code]]
            repaired_entries.append(
                {**entry, "slot_index": slot["index"], "day": slot["day"], "period": slot["period"]}
            )
        else:
            repaired_entries.append(entry)

    def where(slot_index: int) -> dict[str, str]:
        slot = slot_by_index[slot_index]
        return {"day": slot["day"], "period": slot["period"]}

    already_overflow = sum(1 for entry in base_entries if entry.get("day") == "OVERFLOW")
    # Top-level keys, deliberately NOT in qa: the frontend compares qa between
    # Build and a fixed-time Check, a Check cannot reproduce how a board was
    # repaired, and the same key in qa would mark every saved run as changed
    # and gate XLSX export. They ARE persisted, so a repaired run reloaded from
    # history still says what the system moved and what it protected.
    report = {
        "moves": [
            {
                "course_code": code,
                "course_name": inputs.meta_by_course.get(code, {}).get("course_name", ""),
                "from": where(current[code]),
                "to": where(placed[code]),
            }
            for code in moved
        ],
        "unseated": sorted(unseated),
        "already_overflow": already_overflow,
        "violations_before": repair.violations_before,
        "violations_after": repair.violations_after,
        "protected_count": len(protected),
        # True when exams outside any clash had to step aside to make room -
        # without it, a registrar sees an untouched exam move and cannot tell why.
        "widened": repair.widened,
        "proven_minimal": repair.proven_minimal,
        "status": repair.status,
    }
    if links:
        # A student registered in two linked courses sits both papers at one
        # time. That clash is real, but no repair may separate linked courses,
        # so the report says how many there are rather than leaving it unfixed.
        report["linked_clash_students"] = linked_exams_qa(
            links, inputs.enrolled_sets, inputs.credit_map, inputs.meta_by_course
        )["students_in_two_linked_courses"]
    if locks:
        # Breaches among locked exams alone: reported, never repaired.
        locked_units = set(links.units_of(set(locks.placements)))
        unit_placements = links.placements(current)
        report["locked_count"] = len(locks.placements)
        report["locked_violations"] = find_violations(
            {unit: slot for unit, slot in unit_placements.items() if unit in locked_units},
            links.adjacency(inputs.adj),
            {key: members & locked_units for key, members in unit_buckets.items()},
            len(periods),
        )[1]
    if not repair.moved and not unseated:
        # Nothing to fix, nothing the rules let it fix, or no board found in
        # time: the board is exactly the one submitted. Evaluating it again and
        # saving it would add a duplicate run to the history and, when the
        # registrar has unsaved drags, save them without the review Save asks
        # for. The page keeps the draft and shows the report.
        return {"saved": False, "minimum_change": report}
    return _rebuild_loaded_schedule(
        label=label,
        # Never run the invigilator post-pass here: it relocates exams to
        # flatten staff load, which is precisely what this mode must not do.
        rebalance_invigilators=False,
        days=days,
        periods=periods,
        max_per_day=max_per_day,
        schedule_raw=repaired_entries,
        programs=programs,
        sections=sections,
        selected_courses=selected_courses,
        assign_rooms=assign_rooms,
        seed=seed,
        thin_conflict_threshold=thin_conflict_threshold,
        rebuild_mode="minimum_change_from_loaded",
        pinned=pinned,
        linked_exams=links.saved(),
        save=save,
        **_lock_kwargs(locks, lock_source),
        extra={
            "minimum_change": report,
            # Carried into the next repair, so drag, Fix, drag, Fix keeps the
            # first drag. A plain Save starts a fresh baseline. Pins are not
            # carried: they travel with the pin list, and unpinning must free them.
            "minimum_change_protected": sorted(hand_placed),
        },
    )


def _add_source_required() -> AddCoursesError:
    return AddCoursesError(
        "Open the saved timetable, then add courses to it.",
        code=adding.SOURCE_REQUIRED,
        field="previous_run_id",
    )


def _require_add_source(payload: dict) -> None:
    """Adding courses extends a saved run: it must exist and be readable."""
    run_id = payload.get("previous_run_id")
    if isinstance(run_id, bool):
        raise _add_source_required()
    try:
        run = ExamTimetableRun.objects.get(pk=int(run_id))
    except (TypeError, ValueError, ExamTimetableRun.DoesNotExist) as exc:
        raise _add_source_required() from exc
    if load_normalised_run(run).get("status") != "ok":
        raise _add_source_required()


def _seat_rows(inputs: _LoadedSolverInputs) -> dict[str, list[dict]]:
    """The section rows the rooming will seat, for the seats the greedy counts."""
    students = sorted({student for members in inputs.enrolled_sets.values() for student in members})
    attribution = list(
        Student.objects.filter(student_id__in=students).values("student_id", "program", "section")
    )
    return _build_section_enrollment_from_enrolled_sets(
        inputs.enrolled_sets,
        course_meta=inputs.course_meta,
        section_by_student={
            row["student_id"]: str(row["section"] or "").strip() for row in attribution
        },
        program_by_student={row["student_id"]: row["program"] for row in attribution},
    )


def _add_courses_schedule(
    *,
    label: str,
    added_courses: Any,
    carried_protection: list[str] | None,
    source_run_id: int | None,
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
    linked_exams: list[dict] | None = None,
    exam_locks: list[dict] | None = None,
    lock_source: dict | None = None,
    save: RunSaver = _save_run,
) -> dict:
    """Add courses of the run's own scope to a saved timetable, moving the fewest exams.

    Build answers "place everything"; this answers "place these, and leave
    what I perfected alone". The board must be the saved one - the saved run is
    what locks keep, what rooms are kept from, and what "moved" is measured
    against. Build's greedy places what fits with every existing exam fixed;
    the minimum-change solver makes room for the rest, never moving a pinned
    or locked exam, one an earlier Fix protected, or one in a rule break the
    saved board already had. A course nothing can seat stays in OVERFLOW with
    the reason. The result is saved as a new run; the source is never changed.
    """
    started = monotonic()
    source = lock_source or {}
    progress = current_progress()
    identities = adding.parse_added_courses(added_courses)
    base_entries = _normalise_loaded_schedule_entries(schedule_raw, days, periods, selected_courses)
    adding.require_clean_board(
        source=source,
        entries=base_entries,
        pinned=pinned,
        linked_exams=linked_exams,
        exam_locks=exam_locks,
        days=days,
        periods=periods,
        max_per_day=max_per_day,
        thin_conflict_threshold=thin_conflict_threshold,
        assign_rooms=assign_rooms,
    )
    adding.require_same_term(source, current_exam_term())
    added = adding.resolve_added_courses(
        identities, source=source, programs=programs, sections=sections
    )
    slot_count = len(days) * len(periods)
    overflow_index = max(
        [slot_count - 1]
        + [
            int(entry["slot_index"])
            for entry in base_entries
            if entry.get("day") == adding.OVERFLOW and isinstance(entry.get("slot_index"), int)
        ]
    )
    # For the inputs only: every new exam starts unplaced.
    waiting = [
        {**entry, "day": adding.OVERFLOW, "period": f"Extra-{number}", "slot_index": number}
        for number, entry in enumerate(added, start=overflow_index + 1)
    ]
    inputs = _loaded_solver_inputs(
        base_entries + waiting, days, periods, programs, sections, thin_conflict_threshold
    )
    pins = validate_exam_pins(
        pinned, inputs.course_list, inputs.slots, schedule_entries=base_entries
    )
    links, locks = _loaded_links_and_locks(
        linked_exams, exam_locks, lock_source, inputs, pins, base_entries, assign_rooms
    )
    existing = {
        entry["course_code"]: int(entry["slot_index"])
        for entry in base_entries
        if entry.get("day") != adding.OVERFLOW
    }
    new_codes = [entry["course_code"] for entry in added]
    closed = locks.closed_slots(inputs.slots) if locks else frozenset()
    capacity: dict[str, int] = {}
    demand: dict[str, dict[str, int]] = {}
    if assign_rooms:
        capacity = adding.seat_capacity(
            normalized_rooms(exam_room_inventory(order_by=("room_code",)))
        )
        demand = adding.seat_demand(_seat_rows(inputs))

    # Phase 1: Build's greedy around the saved board. Nothing moves.
    progress.stage("place_exams")
    greedy = adding.place_with_greedy(
        existing=existing,
        new_codes=new_codes,
        adj=inputs.adj,
        slots=inputs.slots,
        enrolled_sets=inputs.enrolled_sets,
        max_per_day=max_per_day,
        plan_term_buckets=inputs.plan_term_buckets,
        course_buckets=inputs.course_buckets,
        credit_map=inputs.credit_map,
        locked=locks.pins() if locks else [],
        closed_slots=closed,
        seats=adding.SeatLedger(demand, capacity, existing) if capacity else None,
        on_placed=progress.counter("place_exams"),
    )
    greedy_seconds = monotonic() - started

    # Phase 2, in units: a link is one exam that weighs its courses.
    unit_adj = links.adjacency(inputs.adj)
    unit_buckets, _ = links.buckets(inputs.plan_term_buckets, None)
    unit_home = links.placements(existing)
    # A rule break the saved board already has is not Add's to fix: its exams stay.
    breaking, untouched = find_violations(unit_home, unit_adj, unit_buckets, len(periods))
    carried = {code for code in carried_protection or [] if code in existing}
    fixed_codes = {pin["course_code"] for pin in pins} | carried
    if locks:
        fixed_codes |= set(locks.placements)
    protected = set(links.units_of(fixed_codes)) | set(breaking)
    unit_students = links.enrolled(inputs.enrolled_sets)
    first_board = {**unit_home, **greedy}
    unit_demand: dict[str, dict[str, int]] = {}
    for code, seats in demand.items():
        per = unit_demand.setdefault(links.unit(code), {})
        for gender, count in seats.items():
            per[gender] = per.get(gender, 0) + count
    if len(greedy) < len(new_codes):
        progress.stage("fewest_moves")
    engine = place_added_exams(
        placements=unit_home,
        new_units=new_codes,
        greedy=greedy,
        adj=unit_adj,
        slot_count=slot_count,
        periods_per_day=len(periods),
        plan_term_buckets=unit_buckets,
        protected=protected,
        weights=links.weights or None,
        closed_slots=closed,
        day_cost=adding.day_cost_function(first_board, unit_students, max_per_day, len(periods)),
        day_load=Counter(slot // len(periods) for slot in unit_home.values()),
        seat_demand=unit_demand,
        seat_capacity=capacity,
        check_cancelled=progress.check_cancelled,
    )

    placed = {
        code: slot for unit, slot in engine.placements.items() for code in links.members_of(unit)
    }
    moved = links.expand_codes(engine.moved)
    slot_by_index = {slot["index"]: slot for slot in inputs.slots}
    final_entries: list[dict] = []
    for entry in base_entries:
        code = entry["course_code"]
        if entry.get("day") != adding.OVERFLOW and placed.get(code) != entry["slot_index"]:
            if code not in placed:
                raise RuntimeError(f"Existing exam {code} was left off the board.")
            slot = slot_by_index[placed[code]]
            entry = {
                **entry,
                "slot_index": slot["index"],
                "day": slot["day"],
                "period": slot["period"],
            }
        final_entries.append(entry)
    for entry in added:
        code = entry["course_code"]
        if code in placed:
            slot = slot_by_index[placed[code]]
            final_entries.append(
                {**entry, "slot_index": slot["index"], "day": slot["day"], "period": slot["period"]}
            )
        else:
            overflow_index += 1
            final_entries.append(
                {
                    **entry,
                    "day": adding.OVERFLOW,
                    "period": f"Extra-{overflow_index}",
                    "slot_index": overflow_index,
                }
            )
    # An engine error must never read as the registrar's: before the evaluator
    # judges the board, what this action promised is checked here, as a bug.
    at = {entry["course_code"]: (entry["day"], entry["period"]) for entry in final_entries}
    if locks:
        locks.require_board(final_entries)
    for pin in pins:
        if at.get(pin["course_code"]) != (pin["day"], pin["period"]):
            raise RuntimeError(f"Pinned exam {pin['course_code']} moved while adding courses.")
    if links.split_units(final_entries):
        raise RuntimeError("A linked exam was split while adding courses.")
    unit_final = {unit: slot for unit, slot in engine.placements.items()}
    _, after = find_violations(unit_final, unit_adj, unit_buckets, len(periods))
    if after != untouched:
        raise RuntimeError("Adding courses broke a timetable rule.")

    names = {entry["course_code"]: entry.get("course_name", "") for entry in base_entries + added}

    def where(slot_index: int) -> dict[str, str]:
        slot = slot_by_index[slot_index]
        return {"day": slot["day"], "period": slot["period"]}

    mates = {
        code: {mate for members in unit_buckets.values() if code in members for mate in members}
        - {code}
        for code in {links.unit(code) for code in moved}
    }

    def made_room_for(code: str) -> list[str]:
        unit, home = links.unit(code), existing[code]
        return sorted(
            new
            for new in new_codes
            if new in placed
            and (
                (new in unit_adj.get(unit, {}) and placed[new] == home)
                or (new in mates[unit] and placed[new] // len(periods) == home // len(periods))
            )
        )

    report: dict[str, Any] = {
        "source_run_id": source_run_id,
        "added": [
            {
                "course_code": entry["course_code"],
                "course_identity": entry["course_identity"],
                "course_name": entry["course_name"],
                "placed": where(placed[entry["course_code"]])
                if entry["course_code"] in placed
                else None,
            }
            for entry in added
        ],
        "requested_count": len(added),
        "placed_count": len(engine.placed_new),
        "moves": [
            {
                "course_code": code,
                "course_name": names.get(code, ""),
                "from": where(existing[code]),
                "to": where(placed[code]),
                "made_room_for": made_room_for(code),
            }
            for code in moved
        ],
        "not_placed": [
            {
                "course_code": entry["course_code"],
                "course_identity": entry["course_identity"],
                "course_name": entry["course_name"],
                **adding.explain_unplaced(
                    entry["course_code"],
                    board=engine.placements,
                    adj=unit_adj,
                    plan_term_buckets=unit_buckets,
                    protected=protected,
                    closed_slots=closed,
                    slot_count=slot_count,
                    periods_per_day=len(periods),
                    search_stopped=engine.search_stopped,
                    members_of=links.members_of,
                ),
            }
            for entry in added
            if entry["course_code"] not in placed
        ],
        "moved_weight": sum(links.weight(unit) for unit in engine.moved),
        "protected_count": len({code for unit in protected for code in links.members_of(unit)}),
        "locked_count": len(locks.placements) if locks else 0,
        "untouched_violations": untouched,
        "violations_after": after,
        "widened": engine.widened,
        "proven_minimal": engine.proven_minimal,
        "status": engine.status,
    }
    if links:
        report["linked_clash_students"] = linked_exams_qa(
            links, inputs.enrolled_sets, inputs.credit_map, inputs.meta_by_course
        )["students_in_two_linked_courses"]
    logger.info(
        "exam add: %s of %s placed, %s moved, widened %s, %s attempt(s), work %.2f, "
        "greedy %.2fs, placing %.2fs",
        report["placed_count"],
        report["requested_count"],
        len(moved),
        engine.widened,
        engine.attempts,
        engine.work,
        greedy_seconds,
        monotonic() - started,
    )

    def with_report(result: dict) -> dict:
        # Measured on the evaluated run: the rooms the evaluation chose.
        report["rooms_changed"] = adding.rooms_changed(source, result, inputs.slots)
        report["sections_changed"] = adding.sections_changed(source, result)
        report["unassigned_added"] = adding.unassigned_added(result, set(new_codes))
        extra: dict[str, Any] = {"add_courses": report}
        if carried:
            # Still the registrar's: drag, Fix, drag, Fix keeps the first drag.
            extra["minimum_change_protected"] = sorted(carried)
        return extra

    return _rebuild_loaded_schedule(
        label=label,
        # Never the invigilator post-pass: it moves exams between days.
        rebalance_invigilators=False,
        days=days,
        periods=periods,
        max_per_day=max_per_day,
        schedule_raw=final_entries,
        programs=programs,
        sections=sections,
        selected_courses=[entry["course_code"] for entry in final_entries],
        assign_rooms=assign_rooms,
        seed=seed,
        thin_conflict_threshold=thin_conflict_threshold,
        rebuild_mode=adding.REBUILD_MODE,
        pinned=pins,
        linked_exams=links.saved(),
        save=save,
        keep_rooms_from=source,
        described=with_report,
        **_lock_kwargs(locks, lock_source),
    )


def _check_under_solver_slot(context: dict) -> dict:
    """Run a Check's evaluation; raises SolverBusy if the solver is taken.

    With background jobs on, a Check waits for no one: it holds one of four
    request threads, and a job can hold the solver for a minute. Off, it runs
    as it always has - the page that knows the busy answer ships with the jobs.
    """
    if not exam_jobs.jobs_enabled():
        return evaluate_exam_schedule(**context)
    with solver_slot(holder=HOLDER_CHECK, wait=False):
        return evaluate_exam_schedule(**context)


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
    # Busy is answered before the saved board (about 1.4 MB) is read and checked:
    # a refused Check must not take CPU from the job it waits for. The slot
    # itself, taken below, stays the real test - this one can race.
    if exam_jobs.jobs_enabled() and (busy_with := solver_holder()) is not None:
        return _solver_busy_response(busy_with)
    try:
        schedule_raw = payload.get("base_schedule", payload.get("schedule"))
        context = _loaded_request_context(payload, schedule_raw)
        source_fingerprint = _split_provenance(context)["source_input_fingerprint"]
        try:
            result = _check_under_solver_slot(context)
        except SolverBusy as busy:
            return _solver_busy_response(busy.holder)
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
    except Exception:
        status, body = _server_error("check", "draft_impact")
        return JsonResponse(body, status=status)


def _job_response(status: int, body: dict) -> JsonResponse:
    return JsonResponse(body, status=status)


def _is_superadmin(request: HttpRequest) -> bool:
    return _exam_role(request) == ROLE_SUPER_ADMIN


@require_POST
def exam_timetable_job_seen_view(request: HttpRequest, job_id) -> JsonResponse:
    """The submitter opened or closed an offered result; stop offering it."""
    deny = _require_exam_access(request)
    if deny:
        return deny
    return _job_response(*exam_jobs.seen(job_id, user=request.user))


@require_GET
def exam_timetable_job_active_view(request: HttpRequest) -> JsonResponse:
    """The job a page opening now should show: the one running, or the caller's
    own result that finished while their page was closed."""
    deny = _require_exam_access(request)
    if deny:
        return deny
    # Only ever compared with the caller's own latest job id: any other value
    # simply matches nothing.
    owed = request.GET.get("owed") or None
    return JsonResponse(
        exam_jobs.active(user=request.user, is_superadmin=_is_superadmin(request), owed=owed)
    )


@require_GET
def exam_timetable_job_view(request: HttpRequest, job_id) -> JsonResponse:
    """Poll a job's status and stages. Cheap: never loads the payload or result."""
    deny = _require_exam_access(request)
    if deny:
        return deny
    return _job_response(
        *exam_jobs.poll(job_id, user=request.user, is_superadmin=_is_superadmin(request))
    )


@require_GET
def exam_timetable_job_result_view(request: HttpRequest, job_id) -> JsonResponse:
    """The finished action's own status and body, as the synchronous view gives them."""
    deny = _require_exam_access(request)
    if deny:
        return deny
    return _job_response(*exam_jobs.result(job_id, user=request.user))


@require_POST
def exam_timetable_job_cancel_view(request: HttpRequest, job_id) -> JsonResponse:
    deny = _require_exam_access(request)
    if deny:
        return deny
    status, body = exam_jobs.cancel(
        job_id,
        user=request.user,
        is_superadmin=_is_superadmin(request),
    )
    if status == 202 and body.get("stopped_here") and not body["job"]["mine"]:
        # Only a SUPER_ADMIN gets here; stopping someone else's work is recorded
        # - once, by whoever actually stopped it.
        log_audit_event(
            request,
            action="exam_timetable.cancel_others_job",
            status="success",
            details={"job_id": body["job"]["id"], "kind": body["job"]["kind"]},
        )
    return _job_response(status, body)


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
        # Coded, so a page offering this run can say it was deleted.
        return JsonResponse(
            {"ok": False, "code": "run_not_found", "error": "Run not found"}, status=404
        )

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

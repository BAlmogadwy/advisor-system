"""
core/timetable_workspace_views.py
Timetable Builder Workspace -- page view + JSON API endpoints.

All API endpoints return ``JsonResponse`` with a top-level ``"ok"`` boolean.
Successful responses include ``"ok": true`` plus the data payload; errors
include ``"ok": false`` plus an ``"error"`` object with ``code`` and
``message`` fields.

Authentication / Authorisation
------------------------------
Every endpoint requires ``@login_required`` **and** the General Advisor or
Super Admin role (enforced by ``_require_general_advisor``).  Published
scenarios are immutable -- any mutation attempt returns
``SCENARIO_PUBLISHED`` (HTTP 400).

Endpoint catalogue
------------------
Page
    GET  timetable_workspace_page        -- render the SPA shell

Scenarios
    GET  tw_scenarios_list_view          -- list scenarios for year/term
    POST tw_scenario_create_view         -- create a new scenario
    GET  tw_scenario_detail_view         -- get scenario detail
    POST tw_scenario_slots_update_view   -- edit scenario slot config
    POST tw_scenario_publish_view        -- publish (lock) a scenario
    GET  tw_scenario_budget_view         -- section budget consumption
    GET  tw_scenario_export_view         -- download XLSX export

Generate
    POST tw_generate_workspace_view      -- create scenario + boards + students

Boards
    GET  tw_boards_list_view             -- list boards for a scenario
    POST tw_board_create_view            -- create a board
    GET  tw_board_detail_view            -- board detail + placements
    GET  tw_board_summary_view           -- board summary stats
    GET  tw_board_conflicts_view         -- full conflict analysis
    GET  tw_board_capacity_view          -- demand vs raw capacity
    GET  tw_board_unplaced_view          -- unplaced sections for a board

Placements
    POST tw_placement_create_view        -- place an existing section on a board
    POST tw_placement_create_planned_view -- auto-create section + place it
    POST tw_placement_move_view          -- move placement to new slot
    POST tw_placement_remove_view        -- remove placement
    POST tw_placement_lock_view          -- toggle lock on a placement

Slot Templates
    GET  tw_slot_templates_list_view     -- list slot templates
    POST tw_slot_template_create_view    -- create a slot template
"""

from __future__ import annotations

import json
import logging
import re

from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.http import FileResponse, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from core.models import (
    DeliveryBoard,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TimeSlotTemplate,
    TimetableRepairGlobalPlan,
    TimetableRepairJob,
    TimetableRepairRun,
    TimetableScenario,
)
from core.services.audit import log_audit_event
from core.services.rbac import ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN, get_user_role
from core.services.section_move_optimisation import section_move_optimisation_engine
from core.services.timetable_demand import compute_board_capacity
from core.services.timetable_generate import generate_workspace_scenario
from core.services.timetable_graph_twin import (
    TimetableGraphError,
    build_scenario_graph_summary,
    build_scenario_graph_view,
    neo4j_status,
    sync_scenario_graph_to_neo4j,
)
from core.services.timetable_move_outcome import preview_placement_student_outcome_candidates
from core.services.timetable_online import OnlineCourseLookup
from core.services.timetable_plan_lens import build_scenario_plan_lens
from core.services.timetable_repair import (
    TimetableRepairOperationError,
    apply_global_repair_plan,
    approve_global_repair_plan,
    create_global_repair_plan,
    global_repair_plan_detail,
    rollback_global_repair_plan,
    simulate_timetable_repair_scope,
)
from core.services.timetable_repair_jobs import (
    cancel_repair_job,
    get_repair_job,
    list_repair_jobs,
    recover_stale_repair_jobs,
    repair_job_collection_api_contract,
    retry_repair_job,
    serialize_repair_job,
    submit_repair_analysis_job,
    submit_repair_simulation_job,
)
from core.services.timetable_student_blockers import build_scenario_student_blockers
from core.services.timetable_workspace import (
    apply_bulk_clean_room_assignments,
    apply_bulk_safe_time_moves,
    build_scenario_builder_actions,
    check_publish_readiness,
    compute_affected_students,
    compute_board_summary,
    compute_scenario_budget,
    compute_scenario_safety_summary,
    create_planned_section_placements,
    detect_board_conflicts,
    detect_cross_board_conflicts,
    get_scenario_boards_summary,
    preview_bulk_clean_room_assignments,
    preview_bulk_safe_time_moves,
    preview_placement_room_candidates,
    preview_placement_slot_candidates,
    preview_placement_student_evidence,
    preview_planned_section_slot_candidates,
    summarize_cross_board_conflict_impact,
    validate_placement,
)
from core.settings_views import load_defaults
from core.sidebar_context import get_sidebar_context

logger = logging.getLogger(__name__)

# WS-E — the V2 optimiser's safety gate (snapshot/restore + regression checks)
# lives in ``timetable_v2_runner`` so it runs identically from the request
# thread and the async planner job runner.
from core.services.timetable_v2_runner import run_v2_optimisation_guarded  # noqa: E402

# ── Helpers ──────────────────────────────────────────────────────


def _ok(data: dict[str, object], status: int = 200) -> JsonResponse:
    """Return a success JSON envelope: ``{"ok": true, ...data}``."""
    return JsonResponse({"ok": True, **data}, status=status)


def _err(
    message: str, *, code: str, status: int = 400, details: dict[str, object] | None = None
) -> JsonResponse:
    """Return an error JSON envelope: ``{"ok": false, "error": {...}}``.

    Parameters
    ----------
    message : str
        Human-readable error description.
    code : str
        Machine-readable error code (e.g. ``"NOT_FOUND"``).
    status : int
        HTTP status code (default 400).
    details : dict, optional
        Extra structured data attached to the error object.
    """
    payload: dict[str, object] = {"ok": False, "error": {"code": code, "message": message}}
    if details:
        payload["error"]["details"] = details  # type: ignore[index]
    return JsonResponse(payload, status=status)


def _require_general_advisor(request: HttpRequest) -> JsonResponse | None:
    """Guard: returns a 403 error response if the user lacks the required role.

    Returns ``None`` if the user is a General Advisor or Super Admin,
    allowing the caller to proceed.
    """
    role = get_user_role(request.user)
    if role not in {ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN}:
        return _err("General Advisor access required", code="ROLE_REQUIRED", status=403)
    return None


def _safe_json(request: HttpRequest) -> tuple[dict[str, object], JsonResponse | None]:
    """Parse the request body as JSON, returning ``(payload, None)`` on success.

    On failure returns ``({}, error_response)``.  An empty body is treated as
    an empty dict (not an error), to support POST endpoints with optional bodies.
    """
    if not request.body:
        return {}, None
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return {}, _err("Invalid JSON payload", code="INVALID_JSON", status=400)
    if payload is None:
        return {}, None
    if not isinstance(payload, dict):
        return {}, _err("JSON body must be an object", code="INVALID_JSON_SHAPE", status=400)
    return payload, None


def _validate_term_inputs(year: str, term: str) -> JsonResponse | None:
    """Validate academic year (4 digits) and term (1, 2, or 3).

    Returns ``None`` on success or a 400 error response on failure.
    """
    if not re.fullmatch(r"\d{4}", year):
        return _err("academic_year must be 4 digits", code="VALIDATION_YEAR", status=400)
    if term not in {"1", "2", "3"}:
        return _err("term must be 1, 2, or 3", code="VALIDATION_TERM", status=400)
    return None


def _scenario_to_dict(s: TimetableScenario) -> dict[str, object]:
    """Serialise a ``TimetableScenario`` instance to a JSON-safe dict."""
    return {
        "id": s.id,
        "academic_year": s.academic_year,
        "term": s.term,
        "name": s.name,
        "status": s.status,
        "slot_config": s.slot_config,
        "lab_slot_config": s.lab_slot_config,
        "blocked_slots": s.blocked_slots,
        "created_by": s.created_by,
        "created_at": s.created_at.isoformat() if s.created_at else "",
        "updated_at": s.updated_at.isoformat() if s.updated_at else "",
        "notes": s.notes,
    }


def _board_to_dict(b: DeliveryBoard) -> dict[str, object]:
    """Serialise a ``DeliveryBoard`` instance to a JSON-safe dict."""
    return {
        "id": b.id,
        "scenario_id": b.scenario_id,
        "label": b.label,
        "nominal_term": b.nominal_term,
        "board_type": b.board_type,
        "program": b.program,
        "target_size": b.target_size,
        "display_order": b.display_order,
        "notes": b.notes,
    }


def _placement_to_dict(p: SectionPlacement) -> dict[str, object]:
    """Serialise a ``SectionPlacement`` with its meetings to a JSON-safe dict.

    Includes the related ``TermSection`` fields (course code, capacity, etc.)
    and all ``TermSectionMeeting`` records for that section.
    """
    from core.services.instructor_assignment import serialize_section_instructors

    ts = p.term_section
    meetings = list(
        TermSectionMeeting.objects.filter(term_section=ts).values(
            "day", "start_time", "end_time", "building", "room", "instructor"
        )
    )
    return {
        "id": p.id,
        "board_id": p.board_id,
        "term_section_id": ts.id,
        "course_code": ts.course_code,
        "course_name": ts.course_name,
        "course_key": ts.course_key,
        "section": ts.section,
        "available_capacity": ts.available_capacity,
        "registered_count": ts.registered_count,
        "day": p.day,
        "start_time": p.start_time,
        "end_time": p.end_time,
        "room": p.room,
        "is_locked": p.is_locked,
        "meetings": meetings,
        "instructors": serialize_section_instructors(ts),
    }


# ── Page View ────────────────────────────────────────────────────


@login_required(login_url="login")
@require_GET
def timetable_workspace_page(request: HttpRequest) -> HttpResponse:
    """Render the Timetable Workspace SPA shell page.

    Injects default academic year and term from the system settings so the
    frontend can pre-populate filter controls.
    """
    deny = _require_general_advisor(request)
    if deny:
        return deny
    defaults = load_defaults()
    import json as _json

    from django.conf import settings as _settings
    from django.middleware.csrf import get_token as _csrf

    from core.services.pr8_async_job_ui import (
        POLL_INTERVAL_MS,
        endpoint_map,
        is_async_job_ui_effective,
    )

    pr8_on = is_async_job_ui_effective()
    pr8_config = {
        "submitUrl": endpoint_map()["submit"],
        "pollUrl": endpoint_map()["poll"],
        "resultUrl": endpoint_map()["result"],
        "cancelUrl": endpoint_map()["cancel"],
        "pollIntervalMs": POLL_INTERVAL_MS,
        "csrfToken": _csrf(request) if pr8_on else "",
        "scenarioId": None,
        "mode": "full_rebuild",
    }
    context = {
        **get_sidebar_context(request),
        "default_year": defaults.get("academic_year", ""),
        "default_term": defaults.get("term", ""),
        "TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED": pr8_on,
        "TIMETABLE_PR7_ASYNC_PLANNER_ENABLED": getattr(
            _settings, "TIMETABLE_PR7_ASYNC_PLANNER_ENABLED", False
        ),
        "pr8_job": None,
        "pr8_config_json": _json.dumps(pr8_config),
    }
    return render(request, "core/timetable_workspace.html", context)


@login_required(login_url="login")
def timetable_workspace_split_page(request: HttpRequest) -> HttpResponse:
    """Render the split-pane timetable workspace shell.

    Hosts four side-by-side compact boards (lecture + lab grids each),
    each rendering real placements directly from the existing ``/ops/tw/``
    API. Coordinates scenario selection, cross-pane drag-drop, undo/redo,
    optimisation, publish, and export at the shell level.
    """
    deny = _require_general_advisor(request)
    if deny:
        return deny
    defaults = load_defaults()
    context = {
        **get_sidebar_context(request),
        "default_year": defaults.get("academic_year", ""),
        "default_term": defaults.get("term", ""),
        "initial_scenario": request.GET.get("scenario") or "",
        "initial_board": request.GET.get("board") or "",
    }
    return render(request, "core/timetable_workspace_split.html", context)


@login_required(login_url="login")
@require_GET
def timetable_workspace_mri_page(request: HttpRequest) -> HttpResponse:
    """Render the full-screen Timetable MRI diagnostic page."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    defaults = load_defaults()
    context = {
        **get_sidebar_context(request),
        "default_year": defaults.get("academic_year", ""),
        "default_term": defaults.get("term", ""),
        "initial_scenario": request.GET.get("scenario") or "",
        "initial_board": request.GET.get("board") or "",
    }
    return render(request, "core/timetable_workspace_mri.html", context)


@login_required(login_url="login")
@require_GET
def timetable_workspace_graph_page(request: HttpRequest) -> HttpResponse:
    """Render the Neo4j timetable graph twin control page."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    defaults = load_defaults()
    context = {
        **get_sidebar_context(request),
        "default_year": defaults.get("academic_year", ""),
        "default_term": defaults.get("term", ""),
        "initial_scenario": request.GET.get("scenario") or "",
    }
    return render(request, "core/timetable_workspace_graph.html", context)


# ── Generate Workspace Endpoint ──────────────────────────────────


@login_required(login_url="login")
@require_POST
def tw_generate_workspace_view(request: HttpRequest) -> JsonResponse:
    """Generate a full workspace: scenario + boards + student classification + budget.

    Creates a TimetableScenario with one board per term level, classifies students
    by primary term, and computes the section budget.
    """
    deny = _require_general_advisor(request)
    if deny:
        return deny
    payload, err = _safe_json(request)
    if err:
        return err

    year = payload.get("year")
    semester = payload.get("semester")
    program_raw = str(payload.get("program", "")).strip().upper()
    # Accept comma-separated programmes: "AI,DS" -> ["AI", "DS"].
    # When a single programme is given, pass it as a string to the generator;
    # when multiple are given, pass the list so boards are created per programme.
    programs = [p.strip() for p in program_raw.split(",") if p.strip()] if program_raw else []
    program = programs[0] if len(programs) == 1 else None

    if not year or not semester or not programs:
        return _err(
            "year, semester, and program are required",
            code="VALIDATION_GENERATE",
            status=400,
        )

    try:
        year_int = int(year)
        semester_int = int(semester)
    except (TypeError, ValueError):
        return _err("year and semester must be integers", code="VALIDATION_GENERATE", status=400)

    section = str(payload.get("section", "")).strip().upper() or None
    strategy = str(payload.get("strategy", "compact")).strip().lower()
    scenario_name = str(payload.get("scenario_name", "")).strip()
    max_local_4cr = int(payload.get("max_local_4cr", 25))
    max_local_other = int(payload.get("max_local_other", 40))
    max_external = int(payload.get("max_external", 50))
    course_overrides = payload.get("course_overrides") or None

    try:
        result = generate_workspace_scenario(
            year=year_int,
            semester=semester_int,
            program=programs if len(programs) > 1 else programs[0],
            section=section,
            scenario_name=scenario_name,
            strategy=strategy,
            max_local_4cr=max_local_4cr,
            max_local_other=max_local_other,
            max_external=max_external,
            course_overrides=course_overrides,
            created_by=request.user.username,
        )
    except Exception as exc:
        logger.exception("Generate workspace failed")
        return _err(str(exc), code="GENERATE_FAILED", status=500)

    log_audit_event(
        request,
        action="tw.generate_workspace",
        status="success",
        details={
            "scenario_id": result["scenario"]["id"],
            "program": program,
            "boards": len(result["boards"]),
            "students": result["student_summary"]["classified"],
        },
    )
    return _ok(result, status=201)


@login_required(login_url="login")
@require_GET
def tw_scenario_budget_view(request: HttpRequest, scenario_id: int) -> JsonResponse:
    """Return the section budget consumption table for a scenario.

    Shows planned vs used vs remaining sections and total demand per course.
    """
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        TimetableScenario.objects.get(id=scenario_id)
    except TimetableScenario.DoesNotExist:
        return _err("Scenario not found", code="NOT_FOUND", status=404)

    budget = compute_scenario_budget(scenario_id)
    return _ok({"budget": budget})


@login_required(login_url="login")
@require_GET
def tw_scenario_plan_lens_view(request: HttpRequest, scenario_id: int) -> JsonResponse:
    """Return read-only programme demand and section ownership metadata."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        TimetableScenario.objects.get(id=scenario_id)
    except TimetableScenario.DoesNotExist:
        return _err("Scenario not found", code="NOT_FOUND", status=404)

    return _ok({"plan_lens": build_scenario_plan_lens(scenario_id)})


# ── Scenario Endpoints ───────────────────────────────────────────


@login_required(login_url="login")
@require_GET
def tw_scenarios_list_view(request: HttpRequest) -> JsonResponse:
    """List all scenarios, optionally filtered by ``?year=`` and ``?term=``."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    year = request.GET.get("year", "")
    term = request.GET.get("term", "")
    qs = TimetableScenario.objects.all().order_by("-created_at")
    if year:
        qs = qs.filter(academic_year=year)
    if term:
        qs = qs.filter(term=term)
    return _ok({"scenarios": [_scenario_to_dict(s) for s in qs]})


@login_required(login_url="login")
@require_POST
def tw_scenario_create_view(request: HttpRequest) -> JsonResponse:
    """Create a new empty scenario.

    Accepts ``academic_year``, ``term``, ``name``, optional ``template_id``
    for slot config seeding, and optional ``notes``.  If no template is
    given, the system default template is used (if one exists).
    """
    deny = _require_general_advisor(request)
    if deny:
        return deny
    payload, err = _safe_json(request)
    if err:
        return err

    year = str(payload.get("academic_year", "")).strip()
    term = str(payload.get("term", "")).strip()
    name = str(payload.get("name", "")).strip()
    template_id = payload.get("template_id")
    notes = str(payload.get("notes", "")).strip()

    term_err = _validate_term_inputs(year, term)
    if term_err:
        return term_err
    if not name:
        return _err("name is required", code="VALIDATION_NAME", status=400)

    # Seed slot_config from template if provided
    slot_config: list[object] = []
    if template_id:
        try:
            tpl = TimeSlotTemplate.objects.get(id=int(template_id))
            slot_config = tpl.slots  # type: ignore[assignment]
        except TimeSlotTemplate.DoesNotExist:
            return _err("Slot template not found", code="TEMPLATE_NOT_FOUND", status=404)
    else:
        # Try default template
        default_tpl = TimeSlotTemplate.objects.filter(is_default=True).first()
        if default_tpl:
            slot_config = default_tpl.slots  # type: ignore[assignment]

    try:
        scenario = TimetableScenario.objects.create(
            academic_year=year,
            term=term,
            name=name,
            slot_config=slot_config,
            created_by=request.user.username,
            notes=notes,
        )
    except IntegrityError:
        return _err(
            f"Scenario '{name}' already exists for {year}/{term}",
            code="SCENARIO_EXISTS",
            status=409,
        )

    log_audit_event(
        request,
        action="tw.scenario.create",
        status="success",
        details={"scenario_id": scenario.id, "name": name, "year": year, "term": term},
    )
    return _ok({"scenario": _scenario_to_dict(scenario)}, status=201)


@login_required(login_url="login")
@require_GET
def tw_scenario_detail_view(request: HttpRequest, scenario_id: int) -> JsonResponse:
    """Return the full detail of a single scenario."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        scenario = TimetableScenario.objects.get(id=scenario_id)
    except TimetableScenario.DoesNotExist:
        return _err("Scenario not found", code="NOT_FOUND", status=404)
    return _ok({"scenario": _scenario_to_dict(scenario)})


@login_required(login_url="login")
@require_POST
def tw_scenario_slots_update_view(request: HttpRequest, scenario_id: int) -> JsonResponse:
    """Replace the slot configuration (time periods) for a draft scenario.

    Blocked if the scenario is already published.  Accepts a JSON body
    with ``slot_config`` (array of ``{start, end}`` objects).
    """
    deny = _require_general_advisor(request)
    if deny:
        return deny
    payload, err = _safe_json(request)
    if err:
        return err

    try:
        scenario = TimetableScenario.objects.get(id=scenario_id)
    except TimetableScenario.DoesNotExist:
        return _err("Scenario not found", code="NOT_FOUND", status=404)

    if scenario.status == "published":
        return _err("Cannot edit published scenario", code="SCENARIO_PUBLISHED", status=400)

    slots = payload.get("slot_config")
    if not isinstance(slots, list):
        return _err("slot_config must be an array", code="VALIDATION_SLOTS", status=400)

    lab_slots = payload.get("lab_slot_config")

    scenario.slot_config = slots
    update_fields = ["slot_config", "updated_at"]
    if isinstance(lab_slots, list):
        scenario.lab_slot_config = lab_slots
        update_fields.append("lab_slot_config")
    scenario.save(update_fields=update_fields)

    log_audit_event(
        request,
        action="tw.scenario.slots_update",
        status="success",
        details={"scenario_id": scenario_id, "slot_count": len(slots)},
    )
    return _ok({"scenario": _scenario_to_dict(scenario)})


@login_required(login_url="login")
@require_GET
def tw_scenario_readiness_view(request: HttpRequest, scenario_id: int) -> JsonResponse:
    """Return publish readiness without mutating the scenario."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        TimetableScenario.objects.get(id=scenario_id)
    except TimetableScenario.DoesNotExist:
        return _err("Scenario not found", code="NOT_FOUND", status=404)
    readiness = check_publish_readiness(scenario_id)
    return _ok(
        {
            "readiness": readiness,
            "summary": {
                "ready": readiness.get("ready", False),
                "blockers": len(readiness.get("blockers", [])),
                "warnings": len(readiness.get("warnings", [])),
            },
        }
    )


@login_required(login_url="login")
@require_GET
def tw_scenario_builder_actions_view(request: HttpRequest, scenario_id: int) -> JsonResponse:
    """Return the ranked next actions for professional timetable building."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        TimetableScenario.objects.get(id=scenario_id)
    except TimetableScenario.DoesNotExist:
        return _err("Scenario not found", code="NOT_FOUND", status=404)
    try:
        limit = max(1, min(50, int(request.GET.get("limit", "18"))))
    except ValueError:
        limit = 18
    return _ok(build_scenario_builder_actions(scenario_id, limit=limit))


@login_required(login_url="login")
@require_GET
def tw_scenario_clean_room_assignments_view(request: HttpRequest, scenario_id: int) -> JsonResponse:
    """Preview clean room assignments that can be applied as one batch."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        TimetableScenario.objects.get(id=scenario_id)
    except TimetableScenario.DoesNotExist:
        return _err("Scenario not found", code="NOT_FOUND", status=404)
    try:
        limit = max(1, min(100, int(request.GET.get("limit", "20"))))
    except ValueError:
        limit = 20
    return _ok(preview_bulk_clean_room_assignments(scenario_id, limit=limit))


@login_required(login_url="login")
@require_POST
def tw_scenario_clean_room_assignments_apply_view(
    request: HttpRequest, scenario_id: int
) -> JsonResponse:
    """Apply clean room assignments as a single audited batch operation."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    payload, err = _safe_json(request)
    if err:
        return err
    try:
        TimetableScenario.objects.get(id=scenario_id)
    except TimetableScenario.DoesNotExist:
        return _err("Scenario not found", code="NOT_FOUND", status=404)
    try:
        limit = max(1, min(100, int(payload.get("limit", 20))))
    except (TypeError, ValueError):
        limit = 20
    try:
        result = apply_bulk_clean_room_assignments(scenario_id, limit=limit)
    except ValueError as exc:
        return _err(str(exc), code="SCENARIO_PUBLISHED", status=400)
    log_audit_event(
        request,
        action="tw.clean_rooms.apply_bulk",
        status="success",
        details={
            "scenario_id": scenario_id,
            "applied_count": result.get("applied_count", 0),
            "requested_count": result.get("requested_count", 0),
        },
    )
    return _ok(result)


@login_required(login_url="login")
@require_GET
def tw_scenario_safe_time_moves_view(request: HttpRequest, scenario_id: int) -> JsonResponse:
    """Preview conservative clean time moves for auto-fix workflows."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        TimetableScenario.objects.get(id=scenario_id)
    except TimetableScenario.DoesNotExist:
        return _err("Scenario not found", code="NOT_FOUND", status=404)
    try:
        limit = max(1, min(12, int(request.GET.get("limit", "3"))))
    except ValueError:
        limit = 3
    board_id = request.GET.get("board_id")
    try:
        parsed_board_id = int(board_id) if board_id else None
    except ValueError:
        parsed_board_id = None
    return _ok(preview_bulk_safe_time_moves(scenario_id, board_id=parsed_board_id, limit=limit))


@login_required(login_url="login")
@require_POST
def tw_scenario_safe_time_moves_apply_view(request: HttpRequest, scenario_id: int) -> JsonResponse:
    """Apply conservative clean time moves in a single revalidated batch."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    payload, err = _safe_json(request)
    if err:
        return err
    try:
        TimetableScenario.objects.get(id=scenario_id)
    except TimetableScenario.DoesNotExist:
        return _err("Scenario not found", code="NOT_FOUND", status=404)
    try:
        limit = max(1, min(12, int(payload.get("limit", 3))))
    except (TypeError, ValueError):
        limit = 3
    try:
        board_id = int(payload["board_id"]) if payload.get("board_id") else None
    except (TypeError, ValueError):
        board_id = None
    try:
        result = apply_bulk_safe_time_moves(scenario_id, board_id=board_id, limit=limit)
    except ValueError as exc:
        return _err(str(exc), code="SCENARIO_PUBLISHED", status=400)
    log_audit_event(
        request,
        action="tw.safe_time_moves.apply_bulk",
        status="success",
        details={
            "scenario_id": scenario_id,
            "board_id": board_id,
            "applied_count": result.get("applied_count", 0),
            "requested_count": result.get("requested_count", 0),
        },
    )
    return _ok(result)


@login_required(login_url="login")
@require_GET
def tw_graph_status_view(request: HttpRequest) -> JsonResponse:
    """Return local Neo4j driver/configuration readiness."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    return _ok({"neo4j": neo4j_status()})


@login_required(login_url="login")
@require_GET
def tw_scenario_graph_summary_view(request: HttpRequest, scenario_id: int) -> JsonResponse:
    """Preview the scenario graph twin generated from Django source tables."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        return _ok(build_scenario_graph_summary(scenario_id))
    except TimetableGraphError as exc:
        return _err(str(exc), code="GRAPH_SUMMARY_FAILED", status=404)


@login_required(login_url="login")
@require_GET
def tw_scenario_graph_view_view(request: HttpRequest, scenario_id: int) -> JsonResponse:
    """Return an embedded graph slice for the selected timetable lens."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    mode = str(request.GET.get("mode") or "clashes")
    progressive = str(request.GET.get("progressive") or "").lower() in {"1", "true", "yes"}
    try:
        max_limit = 1200 if mode == "plan" and progressive else 260
        limit = max(20, min(max_limit, int(request.GET.get("limit", "80"))))
    except ValueError:
        limit = 80
    include_students = str(request.GET.get("include_students") or "").lower() in {
        "1",
        "true",
        "yes",
    }
    try:
        return _ok(
            build_scenario_graph_view(
                scenario_id,
                mode=mode,
                limit=limit,
                program=str(request.GET.get("program") or ""),
                plan_term=str(request.GET.get("plan_term") or ""),
                include_students=include_students,
                progressive=progressive,
            )
        )
    except TimetableGraphError as exc:
        return _err(str(exc), code="GRAPH_VIEW_FAILED", status=404)


@login_required(login_url="login")
@require_POST
def tw_scenario_graph_sync_view(request: HttpRequest, scenario_id: int) -> JsonResponse:
    """Rebuild the selected scenario inside Neo4j."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        result = sync_scenario_graph_to_neo4j(scenario_id)
    except TimetableGraphError as exc:
        return _err(str(exc), code="NEO4J_SYNC_FAILED", status=503)

    log_audit_event(
        request,
        action="tw.graph.sync",
        status="success",
        details={
            "scenario_id": scenario_id,
            "node_count": result.get("summary", {}).get("node_count"),
            "relationship_count": result.get("summary", {}).get("relationship_count"),
        },
    )
    return _ok(result)


@login_required(login_url="login")
@require_POST
def tw_scenario_publish_view(request: HttpRequest, scenario_id: int) -> JsonResponse:
    """Publish (lock) a scenario, preventing further modifications.

    Runs a readiness check first; if critical blockers exist the publish
    is rejected with ``PUBLISH_BLOCKED`` and the list of blockers/warnings.
    """
    deny = _require_general_advisor(request)
    if deny:
        return deny

    try:
        scenario = TimetableScenario.objects.get(id=scenario_id)
    except TimetableScenario.DoesNotExist:
        return _err("Scenario not found", code="NOT_FOUND", status=404)

    if scenario.status == "published":
        return _err("Scenario is already published", code="ALREADY_PUBLISHED", status=400)

    readiness = check_publish_readiness(scenario_id)

    if not readiness["ready"]:
        return _err(
            "Cannot publish: critical issues",
            code="PUBLISH_BLOCKED",
            status=400,
            details={
                "blockers": readiness["blockers"],
                "warnings": readiness["warnings"],
            },
        )

    scenario.status = "published"
    scenario.save(update_fields=["status", "updated_at"])

    log_audit_event(
        request,
        action="tw.scenario.publish",
        status="success",
        details={"scenario_id": scenario_id, "name": scenario.name},
    )
    return _ok({"scenario": _scenario_to_dict(scenario)})


@login_required(login_url="login")
@require_GET
def tw_scenario_export_view(request: HttpRequest, scenario_id: int) -> HttpResponse:
    """Export scenario timetable as XLSX."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        scenario = TimetableScenario.objects.get(id=scenario_id)
    except TimetableScenario.DoesNotExist:
        return _err("Scenario not found", code="NOT_FOUND", status=404)

    from core.services.timetable_export import export_scenario_xlsx

    path = export_scenario_xlsx(scenario_id)
    filename = f"timetable_{scenario.name.replace(' ', '_')}_{scenario.academic_year}_T{scenario.term}.xlsx"
    response = FileResponse(
        open(path, "rb"),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required(login_url="login")
@require_GET
def tw_scenario_export_per_plan_view(request: HttpRequest, scenario_id: int) -> HttpResponse:
    """Export one XLSX per program in the scenario, organised by plan term.

    When the scenario spans a single program, returns a plain ``.xlsx``.
    Otherwise, bundles each program's workbook into a ``.zip`` archive named
    ``<scenario_name>__per_plan.zip``.

    Within each program's workbook:

    * **Plan Coverage** sheet — per-course audit (matched / misplaced /
      missing) so the registrar can spot off-plan placements at a glance.
    * **Term N** sheets — one per ``programme_term`` from this program's
      ``ProgrammeRequirement`` rows.  Courses are placed under their plan
      term, even when the scenario placed them on a different board's
      nominal term.  Misplaced courses are tinted amber and labelled with
      the actual board term ("AI113 S1 (on T3)").
    """
    deny = _require_general_advisor(request)
    if deny:
        return deny
    if not TimetableScenario.objects.filter(id=scenario_id).exists():
        return _err("Scenario not found", code="NOT_FOUND", status=404)

    from core.services.timetable_per_plan_export import export_scenario_per_plan

    search = (request.GET.get("search") or request.GET.get("q") or "").strip()
    try:
        path, filename, is_zip = export_scenario_per_plan(scenario_id, search=search)
    except RuntimeError as exc:
        return _err(str(exc), code="EXPORT_FAILED", status=500)

    content_type = (
        "application/zip"
        if is_zip
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response = FileResponse(open(path, "rb"), content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required(login_url="login")
@require_GET
def tw_scenario_student_blockers_view(request: HttpRequest, scenario_id: int) -> JsonResponse:
    """Return actual student blockers grouped by course for the inspector."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    if not TimetableScenario.objects.filter(id=scenario_id).exists():
        return _err("Scenario not found", code="NOT_FOUND", status=404)
    return _ok(build_scenario_student_blockers(scenario_id))


# ── Board Endpoints ──────────────────────────────────────────────


@login_required(login_url="login")
@require_GET
def tw_boards_list_view(request: HttpRequest) -> JsonResponse:
    """List all boards for a scenario with summary stats (``?scenario_id=``)."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    scenario_id = request.GET.get("scenario_id")
    if not scenario_id:
        return _err("scenario_id is required", code="VALIDATION_SCENARIO", status=400)
    scenario_id_int = int(scenario_id)
    boards = list(
        DeliveryBoard.objects.filter(scenario_id=scenario_id_int).order_by("display_order")
    )
    board_conflicts = {board.id: detect_board_conflicts(board.id) for board in boards}
    boards_summary = get_scenario_boards_summary(
        scenario_id_int,
        boards=boards,
        conflicts_by_board=board_conflicts,
    )
    cross_board_clashes = detect_cross_board_conflicts(scenario_id_int)
    cross_board_impact = summarize_cross_board_conflict_impact(cross_board_clashes)
    return _ok(
        {
            "boards": boards_summary,
            "cross_board_clashes": cross_board_impact["conflict_pairs"],
            "cross_board_affected_students": cross_board_impact["affected_students"],
            "cross_board_student_conflict_incidences": cross_board_impact[
                "student_conflict_incidences"
            ],
            "scenario_summary": compute_scenario_safety_summary(
                scenario_id_int,
                boards=boards,
                board_conflicts_by_id=board_conflicts,
                cross_board_conflicts=cross_board_clashes,
            ),
        }
    )


@login_required(login_url="login")
@require_POST
def tw_board_create_view(request: HttpRequest) -> JsonResponse:
    """Create a new delivery board within a draft scenario.

    Requires ``scenario_id`` and ``label``; optional fields include
    ``nominal_term``, ``board_type``, ``program``, ``target_size``,
    ``display_order``, and ``notes``.
    """
    deny = _require_general_advisor(request)
    if deny:
        return deny
    payload, err = _safe_json(request)
    if err:
        return err

    scenario_id = payload.get("scenario_id")
    label = str(payload.get("label", "")).strip()
    if not scenario_id or not label:
        return _err("scenario_id and label are required", code="VALIDATION_BOARD", status=400)

    try:
        scenario = TimetableScenario.objects.get(id=int(scenario_id))  # type: ignore[arg-type]
    except TimetableScenario.DoesNotExist:
        return _err("Scenario not found", code="NOT_FOUND", status=404)

    if scenario.status == "published":
        return _err("Cannot modify published scenario", code="SCENARIO_PUBLISHED", status=400)

    try:
        board = DeliveryBoard.objects.create(
            scenario=scenario,
            label=label,
            nominal_term=payload.get("nominal_term"),
            board_type=str(payload.get("board_type", "standard")),
            program=payload.get("program") or None,
            target_size=int(payload.get("target_size", 0)),  # type: ignore[arg-type]
            display_order=int(payload.get("display_order", 0)),  # type: ignore[arg-type]
            notes=str(payload.get("notes", "")),
        )
    except IntegrityError:
        return _err(
            f"Board '{label}' already exists in this scenario",
            code="BOARD_EXISTS",
            status=409,
        )

    log_audit_event(
        request,
        action="tw.board.create",
        status="success",
        details={"board_id": board.id, "label": label, "scenario_id": scenario.id},
    )
    return _ok({"board": _board_to_dict(board)}, status=201)


@login_required(login_url="login")
@require_GET
def tw_board_detail_view(request: HttpRequest, board_id: int) -> JsonResponse:
    """Return a board's metadata, slot config, all placements, and student counts."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return _err("Board not found", code="NOT_FOUND", status=404)

    placements = SectionPlacement.objects.filter(board=board).select_related("term_section")
    from core.models import BoardStudentLink

    primary_count = BoardStudentLink.objects.filter(board=board, link_type="primary").count()
    visitor_count = BoardStudentLink.objects.filter(board=board, link_type="visitor").count()
    return _ok(
        {
            "board": _board_to_dict(board),
            "slot_config": board.scenario.slot_config,
            "lab_slot_config": board.scenario.lab_slot_config,
            "placements": [_placement_to_dict(p) for p in placements],
            "primary_student_count": primary_count,
            "visitor_student_count": visitor_count,
        }
    )


@login_required(login_url="login")
@require_GET
def tw_board_summary_view(request: HttpRequest, board_id: int) -> JsonResponse:
    """Return aggregate summary stats for a board (sections, students, etc.)."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        DeliveryBoard.objects.get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return _err("Board not found", code="NOT_FOUND", status=404)

    summary = compute_board_summary(board_id)
    return _ok({"summary": summary})


@login_required(login_url="login")
@require_GET
def tw_board_conflicts_view(request: HttpRequest, board_id: int) -> JsonResponse:
    """Return the full conflict analysis for a board.

    Includes time-slot overlaps, instructor clashes, room clashes, the
    student impact count, and cross-board conflicts involving this board.
    """
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        DeliveryBoard.objects.get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return _err("Board not found", code="NOT_FOUND", status=404)

    conflicts = detect_board_conflicts(board_id)
    student_impact = compute_affected_students(board_id)

    # Cross-board conflicts for this board's scenario
    try:
        board = DeliveryBoard.objects.get(id=board_id)
        cross_board = detect_cross_board_conflicts(board.scenario_id)
        # Filter to only cross-board conflicts involving this board
        cross_board = [
            c for c in cross_board if c["board_a_id"] == board_id or c["board_b_id"] == board_id
        ]
    except DeliveryBoard.DoesNotExist:
        cross_board = []

    return _ok(
        {
            "board_id": board_id,
            **conflicts,
            "student_impact": student_impact,
            "cross_board_conflicts": cross_board,
        }
    )


@login_required(login_url="login")
@require_GET
def tw_board_capacity_view(request: HttpRequest, board_id: int) -> JsonResponse:
    """Return demand vs raw capacity for each course on a board.

    Includes per-course deficit and aggregate totals.
    """
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        DeliveryBoard.objects.get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return _err("Board not found", code="NOT_FOUND", status=404)

    courses = compute_board_capacity(board_id)
    total_demand = sum(c["demand"] for c in courses)
    total_raw = sum(c["raw_capacity"] for c in courses)
    total_deficit = sum(c["deficit"] for c in courses)
    return _ok(
        {
            "board_id": board_id,
            "courses": courses,
            "totals": {
                "demand": total_demand,
                "raw_capacity": total_raw,
                "deficit": total_deficit,
                "course_count": len(courses),
            },
        }
    )


@login_required(login_url="login")
@require_GET
def tw_board_unplaced_view(request: HttpRequest, board_id: int) -> JsonResponse:
    """Return sections not yet placed on this board.

    Supports ``?search=`` to filter by course code, name, or section label.
    Used by the UI's section picker sidebar.
    """
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return _err("Board not found", code="NOT_FOUND", status=404)

    # Get term sections for this scenario (scenario-owned + global imported)
    from django.db.models import Q

    all_sections = TermSection.objects.filter(Q(scenario=board.scenario) | Q(scenario__isnull=True))

    # Get sections already placed on this board
    placed_ids = set(
        SectionPlacement.objects.filter(board=board).values_list("term_section_id", flat=True)
    )

    search = request.GET.get("search", "").strip().upper()

    unplaced = []
    for ts in all_sections:
        if ts.id in placed_ids:
            continue

        # Apply search filter
        if (
            search
            and search not in (ts.course_code or "").upper()
            and search not in (ts.course_name or "").upper()
            and search not in (ts.section or "").upper()
        ):
            continue

        meetings = list(
            TermSectionMeeting.objects.filter(term_section=ts).values(
                "day", "start_time", "end_time", "room", "instructor"
            )
        )
        unplaced.append(
            {
                "term_section_id": ts.id,
                "course_code": ts.course_code,
                "course_name": ts.course_name,
                "course_key": ts.course_key,
                "section": ts.section,
                "available_capacity": ts.available_capacity,
                "registered_count": ts.registered_count,
                "meetings": meetings,
            }
        )

    return _ok({"unplaced": unplaced, "count": len(unplaced)})


@login_required(login_url="login")
@require_GET
def tw_board_planned_slot_candidates_view(request: HttpRequest, board_id: int) -> JsonResponse:
    """Return ranked read-only target slots for a missing planned section."""
    deny = _require_general_advisor(request)
    if deny:
        return deny

    course_code = str(request.GET.get("course_code", "")).strip().upper()
    if not course_code:
        return _err("course_code is required", code="VALIDATION_COURSE", status=400)

    def _optional_int(name: str) -> int | None:
        raw = str(request.GET.get(name, "")).strip()
        if not raw:
            return None
        return int(raw)

    try:
        preview = preview_planned_section_slot_candidates(
            board_id,
            course_code=course_code,
            course_key=str(request.GET.get("course_key") or course_code).strip().upper(),
            section_label=str(request.GET.get("section_label") or "S1").strip().upper(),
            credit_hours=_optional_int("credit_hours"),
            max_per_section=_optional_int("max_per_section"),
            kind=str(request.GET.get("kind") or "").strip(),
            limit=_optional_int("limit") or 80,
        )
    except DeliveryBoard.DoesNotExist:
        return _err("Board not found", code="NOT_FOUND", status=404)
    except ValueError as exc:
        return _err(str(exc), code="VALIDATION_PLANNED_SLOT", status=400)
    return _ok(preview)


# ── Placement Endpoints ──────────────────────────────────────────


@login_required(login_url="login")
@require_POST
def tw_placement_create_planned_view(request: HttpRequest) -> JsonResponse:
    """Create one planned section, including full multi-meeting patterns."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    payload, err = _safe_json(request)
    if err:
        return err

    board_id = payload.get("board_id")
    course_code = str(payload.get("course_code", "")).strip().upper()
    course_key = str(payload.get("course_key") or course_code).strip().upper()
    course_name = str(payload.get("course_name") or course_code).strip()
    section_label = str(payload.get("section_label", "")).strip().upper()
    day = str(payload.get("day", "")).strip().upper()
    start_time = str(payload.get("start_time", "")).strip()
    end_time = str(payload.get("end_time", "")).strip()
    capacity = int(payload.get("capacity", 40))
    raw_meetings = payload.get("meetings")

    if isinstance(raw_meetings, list) and raw_meetings:
        meetings = raw_meetings
    elif day and start_time and end_time:
        meetings = [{"day": day, "start_time": start_time, "end_time": end_time}]
    else:
        meetings = []

    if not all([board_id, course_code, section_label]) or not meetings:
        return _err(
            "board_id, course_code, section_label, and at least one meeting are required",
            code="VALIDATION_PLACEMENT",
            status=400,
        )

    try:
        result = create_planned_section_placements(
            int(board_id),  # type: ignore[arg-type]
            course_code=course_code,
            course_key=course_key,
            course_name=course_name,
            section_label=section_label,
            capacity=capacity,
            meetings=meetings,
        )
    except DeliveryBoard.DoesNotExist:
        return _err("Board not found", code="NOT_FOUND", status=404)
    except ValueError as exc:
        message = str(exc)
        code = (
            "SCENARIO_PUBLISHED"
            if "published" in message.lower()
            else "FULL_SECTION_PATTERN_REQUIRED"
            if "complete" in message.lower() and "meeting" in message.lower()
            else "VALIDATION_PLANNED_PATTERN"
        )
        return _err(
            message,
            code=code,
            status=400,
        )
    except IntegrityError:
        return _err(
            "Duplicate placement for this section/day/time",
            code="PLACEMENT_DUPLICATE",
            status=409,
        )

    placements = list(result["placements"])
    validations = list(result["validations"])
    placement = placements[0]
    validation = {
        "valid": not any(int(v.get("critical_count") or 0) for v in validations),
        "critical_count": sum(int(v.get("critical_count") or 0) for v in validations),
        "warning_count": sum(int(v.get("warning_count") or 0) for v in validations),
        "items": validations,
    }

    log_audit_event(
        request,
        action="tw.placement.create_planned",
        status="success",
        details={
            "placement_id": placement.id,
            "placement_ids": [item.id for item in placements],
            "board_id": result["board"].id,
            "course_code": course_code,
            "course_key": course_key,
            "section_label": section_label,
            "meeting_count": len(placements),
        },
    )
    return _ok(
        {
            "placement": _placement_to_dict(placement),
            "placements": [_placement_to_dict(item) for item in placements],
            "validation": validation,
            "validations": validations,
            "required_meetings": result["required_meetings"],
        },
        status=201,
    )


@login_required(login_url="login")
@require_POST
def tw_placement_create_view(request: HttpRequest) -> JsonResponse:
    """Place an existing ``TermSection`` onto a board at a given day/time.

    Validates the placement for conflicts before persisting but still creates
    it even if warnings exist (the validation result is returned alongside).
    """
    deny = _require_general_advisor(request)
    if deny:
        return deny
    payload, err = _safe_json(request)
    if err:
        return err

    board_id = payload.get("board_id")
    term_section_id = payload.get("term_section_id")
    day = str(payload.get("day", "")).strip().upper()
    start_time = str(payload.get("start_time", "")).strip()
    end_time = str(payload.get("end_time", "")).strip()
    room = str(payload.get("room", "")).strip()

    if not all([board_id, term_section_id, day, start_time, end_time]):
        return _err(
            "board_id, term_section_id, day, start_time, end_time are required",
            code="VALIDATION_PLACEMENT",
            status=400,
        )

    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=int(board_id))  # type: ignore[arg-type]
    except DeliveryBoard.DoesNotExist:
        return _err("Board not found", code="NOT_FOUND", status=404)

    if board.scenario.status == "published":
        return _err("Cannot modify published scenario", code="SCENARIO_PUBLISHED", status=400)

    try:
        ts = TermSection.objects.get(id=int(term_section_id))  # type: ignore[arg-type]
    except TermSection.DoesNotExist:
        return _err("TermSection not found", code="NOT_FOUND", status=404)

    if OnlineCourseLookup().is_online_course_for_board(board, ts.course_code):
        room = ""

    # Validate before persisting
    validation = validate_placement(
        board_id=board.id,
        day=day,
        start_time=start_time,
        end_time=end_time,
        room=room,
        term_section_id=ts.id,
    )

    try:
        placement = SectionPlacement.objects.create(
            board=board,
            term_section=ts,
            day=day,
            start_time=start_time,
            end_time=end_time,
            room=room,
        )
    except IntegrityError:
        return _err(
            "Duplicate placement for this section/day/time on this board",
            code="PLACEMENT_DUPLICATE",
            status=409,
        )

    log_audit_event(
        request,
        action="tw.placement.create",
        status="success",
        details={
            "placement_id": placement.id,
            "board_id": board.id,
            "term_section_id": ts.id,
            "day": day,
            "start_time": start_time,
        },
    )
    return _ok(
        {
            "placement": _placement_to_dict(placement),
            "validation": validation,
        },
        status=201,
    )


@login_required(login_url="login")
@require_GET
def tw_placement_slot_candidates_view(request: HttpRequest, placement_id: int) -> JsonResponse:
    """Return student-aware move candidates for a placement without mutating data."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        preview = preview_placement_slot_candidates(placement_id)
    except SectionPlacement.DoesNotExist:
        return _err("Placement not found", code="NOT_FOUND", status=404)
    return _ok(preview)


@login_required(login_url="login")
@require_http_methods(["GET", "POST"])
def tw_placement_student_outcome_candidates_view(
    request: HttpRequest, placement_id: int
) -> JsonResponse:
    """Return move candidates scored by full student reassignment outcome."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    candidate_moves = None
    if request.method == "POST":
        payload, err = _safe_json(request)
        if err:
            return err
        raw_candidates = payload.get("candidates")
        if isinstance(raw_candidates, list):
            candidate_moves = raw_candidates[:60]
    try:
        preview = preview_placement_student_outcome_candidates(
            placement_id,
            candidate_moves=candidate_moves,
        )
    except SectionPlacement.DoesNotExist:
        return _err("Placement not found", code="NOT_FOUND", status=404)
    return _ok(preview)


@login_required(login_url="login")
@require_POST
def tw_repair_analyse_view(request: HttpRequest) -> JsonResponse:
    """Create a read-only audited registration repair analysis run."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    payload, err = _safe_json(request)
    if err:
        return err

    placement_id = payload.get("placement_id")
    if not placement_id:
        return _err("placement_id is required", code="VALIDATION_PLACEMENT", status=400)
    blocked_student_ids = payload.get("blocked_student_ids") or []
    if not isinstance(blocked_student_ids, list):
        return _err(
            "blocked_student_ids must be an array",
            code="VALIDATION_BLOCKED_STUDENTS",
            status=400,
        )
    blocked_requests = payload.get("blocked_requests") or []
    if not isinstance(blocked_requests, list):
        return _err(
            "blocked_requests must be an array",
            code="VALIDATION_BLOCKED_REQUESTS",
            status=400,
        )
    limits = payload.get("limits") or {}
    if not isinstance(limits, dict):
        return _err("limits must be an object", code="VALIDATION_LIMITS", status=400)

    try:
        detail = section_move_optimisation_engine.analyse_section_move(
            placement_id=int(placement_id),
            blocked_student_ids=blocked_student_ids,
            blocked_requests=blocked_requests,
            mode=str(payload.get("mode") or "conservative"),
            move_scope=str(payload.get("move_scope") or "single_session"),
            requested_by=request.user,
            limits=limits,
            active_plan_filter=str(payload.get("active_plan_filter") or "ALL"),
        )
    except (TypeError, ValueError):
        return _err("placement_id must be an integer", code="VALIDATION_PLACEMENT", status=400)
    except SectionPlacement.DoesNotExist:
        return _err("Placement not found", code="NOT_FOUND", status=404)
    return _ok(detail, status=201)


@login_required(login_url="login")
@require_POST
def tw_repair_simulate_view(request: HttpRequest) -> JsonResponse:
    """Run a bounded analysis-only repair simulation across a scenario scope."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    payload, err = _safe_json(request)
    if err:
        return err

    scenario_id = payload.get("scenario_id")
    if not scenario_id:
        return _err("scenario_id is required", code="VALIDATION_SCENARIO", status=400)
    course_keys = payload.get("course_keys") or []
    if not isinstance(course_keys, list):
        return _err("course_keys must be an array", code="VALIDATION_COURSE_KEYS", status=400)
    limits = payload.get("limits") or {}
    if not isinstance(limits, dict):
        return _err("limits must be an object", code="VALIDATION_LIMITS", status=400)
    nominal_term = payload.get("nominal_term")
    try:
        result = simulate_timetable_repair_scope(
            scenario_id=int(scenario_id),
            program=str(payload.get("program") or ""),
            nominal_term=int(nominal_term) if nominal_term not in {None, ""} else None,
            course_keys=[str(course) for course in course_keys],
            requested_by=request.user,
            limits=limits,
            max_placements=int(payload.get("max_placements") or 8),
        )
    except (TypeError, ValueError):
        return _err(
            "scenario_id and max_placements must be integers",
            code="VALIDATION_SIMULATION",
            status=400,
        )
    except TimetableScenario.DoesNotExist:
        return _err("Scenario not found", code="NOT_FOUND", status=404)
    return _ok({"simulation": result}, status=201)


@login_required(login_url="login")
@require_POST
def tw_repair_global_plan_create_view(request: HttpRequest) -> JsonResponse:
    """Create a durable programme/level repair plan targeting unresolved students."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    payload, err = _safe_json(request)
    if err:
        return err

    scenario_id = payload.get("scenario_id")
    if not scenario_id:
        return _err("scenario_id is required", code="VALIDATION_SCENARIO", status=400)
    course_keys = payload.get("course_keys") or []
    if not isinstance(course_keys, list):
        return _err("course_keys must be an array", code="VALIDATION_COURSE_KEYS", status=400)
    limits = payload.get("limits") or {}
    if not isinstance(limits, dict):
        return _err("limits must be an object", code="VALIDATION_LIMITS", status=400)
    nominal_term = payload.get("nominal_term")
    try:
        plan = create_global_repair_plan(
            scenario_id=int(scenario_id),
            program=str(payload.get("program") or ""),
            nominal_term=int(nominal_term) if nominal_term not in {None, ""} else None,
            course_keys=[str(course) for course in course_keys],
            mode=str(payload.get("mode") or TimetableRepairRun.MODE_CONSERVATIVE),
            requested_by=request.user,
            limits=limits,
            max_placements=int(payload.get("max_placements") or 8),
            notes=str(payload.get("notes") or ""),
        )
    except (TypeError, ValueError):
        return _err(
            "Invalid global repair plan payload", code="VALIDATION_GLOBAL_REPAIR_PLAN", status=400
        )
    except TimetableScenario.DoesNotExist:
        return _err("Scenario not found", code="NOT_FOUND", status=404)
    except TimetableRepairOperationError as exc:
        return _err(exc.message, code=exc.code, status=exc.status, details=exc.details)
    log_audit_event(
        request,
        action="tw.repair.global_plan.create",
        status="success",
        details={"plan_id": plan["plan"]["id"], "scenario_id": int(scenario_id)},
    )
    return _ok(plan, status=201)


@login_required(login_url="login")
@require_GET
def tw_repair_global_plan_detail_view(request: HttpRequest, plan_id) -> JsonResponse:
    """Return one global repair plan with selected repair items."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        plan = global_repair_plan_detail(plan_id)
    except TimetableRepairGlobalPlan.DoesNotExist:
        return _err("Global repair plan not found", code="NOT_FOUND", status=404)
    return _ok(plan)


@login_required(login_url="login")
@require_POST
def tw_repair_global_plan_approve_view(request: HttpRequest, plan_id) -> JsonResponse:
    """Approve all ready candidates in a global repair plan."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    payload, err = _safe_json(request)
    if err:
        return err
    try:
        plan = approve_global_repair_plan(
            plan_id,
            decided_by=request.user,
            notes=str(payload.get("notes") or ""),
        )
    except TimetableRepairGlobalPlan.DoesNotExist:
        return _err("Global repair plan not found", code="NOT_FOUND", status=404)
    except TimetableRepairOperationError as exc:
        return _err(exc.message, code=exc.code, status=exc.status, details=exc.details)
    log_audit_event(
        request,
        action="tw.repair.global_plan.approve",
        status="success",
        details={"plan_id": str(plan_id)},
    )
    return _ok(plan)


@login_required(login_url="login")
@require_POST
def tw_repair_global_plan_apply_view(request: HttpRequest, plan_id) -> JsonResponse:
    """Apply an approved global repair plan."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        plan = apply_global_repair_plan(plan_id, decided_by=request.user)
    except TimetableRepairGlobalPlan.DoesNotExist:
        return _err("Global repair plan not found", code="NOT_FOUND", status=404)
    except TimetableRepairOperationError as exc:
        return _err(exc.message, code=exc.code, status=exc.status, details=exc.details)
    log_audit_event(
        request,
        action="tw.repair.global_plan.apply",
        status="success",
        details={"plan_id": str(plan_id)},
    )
    return _ok(plan)


@login_required(login_url="login")
@require_POST
def tw_repair_global_plan_rollback_view(request: HttpRequest, plan_id) -> JsonResponse:
    """Rollback all applied items in a global repair plan."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        plan = rollback_global_repair_plan(plan_id, decided_by=request.user)
    except TimetableRepairGlobalPlan.DoesNotExist:
        return _err("Global repair plan not found", code="NOT_FOUND", status=404)
    except TimetableRepairOperationError as exc:
        return _err(exc.message, code=exc.code, status=exc.status, details=exc.details)
    log_audit_event(
        request,
        action="tw.repair.global_plan.rollback",
        status="success",
        details={"plan_id": str(plan_id)},
    )
    return _ok(plan)


@login_required(login_url="login")
@require_POST
def tw_repair_job_submit_view(request: HttpRequest) -> JsonResponse:
    """Submit a durable repair analysis/simulation job."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    payload, err = _safe_json(request)
    if err:
        return err

    kind = str(payload.get("kind") or TimetableRepairJob.KIND_ANALYSIS)
    limits = payload.get("limits") or {}
    if not isinstance(limits, dict):
        return _err("limits must be an object", code="VALIDATION_LIMITS", status=400)
    try:
        if kind == TimetableRepairJob.KIND_ANALYSIS:
            placement_id = payload.get("placement_id")
            if not placement_id:
                return _err("placement_id is required", code="VALIDATION_PLACEMENT", status=400)
            blocked_student_ids = payload.get("blocked_student_ids") or []
            if not isinstance(blocked_student_ids, list):
                return _err(
                    "blocked_student_ids must be an array",
                    code="VALIDATION_BLOCKED_STUDENTS",
                    status=400,
                )
            blocked_requests = payload.get("blocked_requests") or []
            if not isinstance(blocked_requests, list):
                return _err(
                    "blocked_requests must be an array",
                    code="VALIDATION_BLOCKED_REQUESTS",
                    status=400,
                )
            job = submit_repair_analysis_job(
                placement_id=int(placement_id),
                blocked_student_ids=blocked_student_ids,
                blocked_requests=blocked_requests,
                mode=str(payload.get("mode") or "conservative"),
                requested_by=request.user,
                limits=limits,
                active_plan_filter=str(payload.get("active_plan_filter") or "ALL"),
            )
        elif kind == TimetableRepairJob.KIND_SIMULATION:
            scenario_id = payload.get("scenario_id")
            if not scenario_id:
                return _err("scenario_id is required", code="VALIDATION_SCENARIO", status=400)
            course_keys = payload.get("course_keys") or []
            if not isinstance(course_keys, list):
                return _err(
                    "course_keys must be an array", code="VALIDATION_COURSE_KEYS", status=400
                )
            nominal_term = payload.get("nominal_term")
            job = submit_repair_simulation_job(
                scenario_id=int(scenario_id),
                program=str(payload.get("program") or ""),
                nominal_term=int(nominal_term) if nominal_term not in {None, ""} else None,
                course_keys=course_keys,
                requested_by=request.user,
                limits=limits,
                max_placements=int(payload.get("max_placements") or 8),
            )
        else:
            return _err(
                "Unsupported repair job kind", code="VALIDATION_REPAIR_JOB_KIND", status=400
            )
    except (TypeError, ValueError):
        return _err("Invalid repair job payload", code="VALIDATION_REPAIR_JOB", status=400)
    except (SectionPlacement.DoesNotExist, TimetableScenario.DoesNotExist):
        return _err("Repair job target not found", code="NOT_FOUND", status=404)
    return _ok({"job": serialize_repair_job(job)}, status=201)


@login_required(login_url="login")
@require_GET
def tw_repair_job_list_view(request: HttpRequest) -> JsonResponse:
    """Return recent repair jobs for an operational monitor."""
    deny = _require_general_advisor(request)
    if deny:
        return deny

    allowed_kinds = {choice[0] for choice in TimetableRepairJob.KIND_CHOICES}
    allowed_statuses = {choice[0] for choice in TimetableRepairJob.STATUS_CHOICES}
    kind = str(request.GET.get("kind") or "")
    status = str(request.GET.get("status") or "")
    if kind and kind not in allowed_kinds:
        return _err("Unsupported repair job kind", code="VALIDATION_REPAIR_JOB_KIND", status=400)
    if status and status not in allowed_statuses:
        return _err(
            "Unsupported repair job status", code="VALIDATION_REPAIR_JOB_STATUS", status=400
        )

    scenario_id = None
    raw_scenario_id = request.GET.get("scenario_id")
    raw_limit = request.GET.get("limit") or 50
    try:
        if raw_scenario_id not in {None, ""}:
            scenario_id = int(raw_scenario_id)
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return _err(
            "scenario_id and limit must be integers", code="VALIDATION_REPAIR_JOB_LIST", status=400
        )

    mine = str(request.GET.get("mine") or "").lower() in {"1", "true", "yes"}
    submitted_by_id = request.user.id if mine else None
    jobs, meta = list_repair_jobs(
        scenario_id=scenario_id,
        kind=kind,
        status=status,
        submitted_by_id=submitted_by_id,
        limit=limit,
    )
    return _ok(
        {
            "api_contract": repair_job_collection_api_contract(),
            "filters": meta["filters"],
            "count": len(jobs),
            "has_more": bool(meta["has_more"]),
            "jobs": [serialize_repair_job(job) for job in jobs],
        }
    )


@login_required(login_url="login")
@require_POST
def tw_repair_job_recover_stale_view(request: HttpRequest) -> JsonResponse:
    """Recover stale running repair jobs for operational maintenance."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    payload, err = _safe_json(request)
    if err:
        return err
    try:
        stale_after_seconds = int(payload.get("stale_after_seconds") or 30 * 60)
        max_attempts = int(payload.get("max_attempts") or 3)
        limit = int(payload.get("limit") or 50)
    except (TypeError, ValueError):
        return _err(
            "Recovery options must be integers", code="VALIDATION_REPAIR_JOB_RECOVERY", status=400
        )
    recovered = recover_stale_repair_jobs(
        stale_after_seconds=stale_after_seconds,
        max_attempts=max_attempts,
        limit=limit,
        worker_id=f"api-recovery:{request.user.id}",
    )
    return _ok(
        {
            "count": len(recovered),
            "jobs": [serialize_repair_job(job) for job in recovered],
        }
    )


@login_required(login_url="login")
@require_GET
def tw_repair_job_poll_view(request: HttpRequest, job_id) -> JsonResponse:
    """Poll a repair job without returning the full result package."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    job = get_repair_job(job_id)
    if job is None:
        return _err("Repair job not found", code="NOT_FOUND", status=404)
    return _ok({"job": serialize_repair_job(job)})


@login_required(login_url="login")
@require_GET
def tw_repair_job_result_view(request: HttpRequest, job_id) -> JsonResponse:
    """Return the full repair job result after the job succeeds."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    job = get_repair_job(job_id)
    if job is None:
        return _err("Repair job not found", code="NOT_FOUND", status=404)
    if job.status != TimetableRepairJob.STATUS_SUCCEEDED:
        return _err(
            "Repair job result is not ready", code="REPAIR_JOB_RESULT_NOT_READY", status=404
        )
    return _ok({"job": serialize_repair_job(job, include_result=True)})


@login_required(login_url="login")
@require_POST
def tw_repair_job_cancel_view(request: HttpRequest, job_id) -> JsonResponse:
    """Cooperatively request cancellation of a queued/running repair job."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    ok = cancel_repair_job(job_id, requested_by=request.user)
    if not ok:
        return _err("Repair job cannot be cancelled", code="REPAIR_JOB_CANNOT_CANCEL", status=404)
    job = get_repair_job(job_id)
    return _ok({"job": serialize_repair_job(job) if job else {"job_id": str(job_id)}})


@login_required(login_url="login")
@require_POST
def tw_repair_job_retry_view(request: HttpRequest, job_id) -> JsonResponse:
    """Create a fresh queued retry for a failed/cancelled repair job."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    payload, err = _safe_json(request)
    if err:
        return err
    source = get_repair_job(job_id)
    if source is None:
        return _err("Repair job not found", code="NOT_FOUND", status=404)
    try:
        max_attempts = int(payload.get("max_attempts") or 3)
    except (TypeError, ValueError):
        return _err(
            "max_attempts must be an integer", code="VALIDATION_REPAIR_JOB_RETRY", status=400
        )
    retry = retry_repair_job(
        job_id,
        requested_by=request.user,
        max_attempts=max_attempts,
    )
    if retry is None:
        return _err(
            "Repair job cannot be retried",
            code="REPAIR_JOB_CANNOT_RETRY",
            status=409,
            details={"status": source.status, "attempt_count": int(source.attempt_count or 0)},
        )
    return _ok({"job": serialize_repair_job(retry)}, status=201)


@login_required(login_url="login")
@require_GET
def tw_repair_run_detail_view(request: HttpRequest, run_id) -> JsonResponse:
    """Return one audited repair run with candidates and snapshots."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        detail = section_move_optimisation_engine.run_detail(run_id)
    except TimetableRepairRun.DoesNotExist:
        return _err("Repair run not found", code="NOT_FOUND", status=404)
    return _ok(detail)


@login_required(login_url="login")
@require_GET
def tw_repair_run_report_view(request: HttpRequest, run_id) -> JsonResponse:
    """Return a stable admin evidence report for one repair run."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        report = section_move_optimisation_engine.report(
            run_id,
            candidate_id=(request.GET.get("candidate_id") or None),
        )
    except TimetableRepairRun.DoesNotExist:
        return _err("Repair run not found", code="NOT_FOUND", status=404)
    except TimetableRepairOperationError as exc:
        return _err(exc.message, code=exc.code, status=exc.status, details=exc.details)
    return _ok(report)


@login_required(login_url="login")
@require_GET
def tw_repair_candidate_detail_view(
    request: HttpRequest,
    run_id,
    candidate_id: str,
) -> JsonResponse:
    """Return one repair candidate with direct evidence and student-level changes."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        detail = section_move_optimisation_engine.candidate_detail(run_id, candidate_id)
    except TimetableRepairRun.DoesNotExist:
        return _err("Repair run not found", code="NOT_FOUND", status=404)
    except TimetableRepairOperationError as exc:
        return _err(exc.message, code=exc.code, status=exc.status, details=exc.details)
    return _ok(detail)


@login_required(login_url="login")
@require_POST
def tw_repair_candidate_approve_view(
    request: HttpRequest, run_id, candidate_id: str
) -> JsonResponse:
    """Approve a solved repair candidate without applying it."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    payload, err = _safe_json(request)
    if err:
        return err
    try:
        detail = section_move_optimisation_engine.approve_candidate(
            run_id,
            candidate_id,
            decided_by=request.user,
            notes=str(payload.get("notes") or ""),
        )
    except TimetableRepairRun.DoesNotExist:
        return _err("Repair run not found", code="NOT_FOUND", status=404)
    except TimetableRepairOperationError as exc:
        return _err(exc.message, code=exc.code, status=exc.status, details=exc.details)
    log_audit_event(
        request,
        action="tw.repair.approve",
        status="success",
        details={"run_id": str(run_id), "candidate_id": candidate_id},
    )
    return _ok(detail)


@login_required(login_url="login")
@require_POST
def tw_repair_candidate_apply_view(request: HttpRequest, run_id, candidate_id: str) -> JsonResponse:
    """Apply an approved repair candidate inside one transaction."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        detail = section_move_optimisation_engine.apply_candidate(
            run_id,
            candidate_id,
            decided_by=request.user,
        )
    except TimetableRepairRun.DoesNotExist:
        return _err("Repair run not found", code="NOT_FOUND", status=404)
    except TimetableRepairOperationError as exc:
        return _err(exc.message, code=exc.code, status=exc.status, details=exc.details)
    log_audit_event(
        request,
        action="tw.repair.apply",
        status="success",
        details={"run_id": str(run_id), "candidate_id": candidate_id},
    )
    return _ok(detail)


@login_required(login_url="login")
@require_POST
def tw_repair_run_rollback_view(request: HttpRequest, run_id) -> JsonResponse:
    """Rollback the applied repair candidate for a run."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        detail = section_move_optimisation_engine.rollback_run(run_id, decided_by=request.user)
    except TimetableRepairRun.DoesNotExist:
        return _err("Repair run not found", code="NOT_FOUND", status=404)
    except TimetableRepairOperationError as exc:
        return _err(exc.message, code=exc.code, status=exc.status, details=exc.details)
    log_audit_event(
        request,
        action="tw.repair.rollback",
        status="success",
        details={"run_id": str(run_id)},
    )
    return _ok(detail)


@login_required(login_url="login")
@require_GET
def tw_placement_room_candidates_view(request: HttpRequest, placement_id: int) -> JsonResponse:
    """Return room candidates for a placement at its current or previewed slot."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        preview = preview_placement_room_candidates(
            placement_id,
            day=request.GET.get("day") or None,
            start_time=request.GET.get("start_time") or None,
            end_time=request.GET.get("end_time") or None,
        )
    except SectionPlacement.DoesNotExist:
        return _err("Placement not found", code="NOT_FOUND", status=404)
    return _ok(preview)


@login_required(login_url="login")
@require_GET
def tw_placement_student_evidence_view(request: HttpRequest, placement_id: int) -> JsonResponse:
    """Return exact student evidence for conflicts involving one placement."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        limit = max(1, min(100, int(request.GET.get("limit", "40"))))
    except ValueError:
        limit = 40
    try:
        preview = preview_placement_student_evidence(placement_id, limit=limit)
    except SectionPlacement.DoesNotExist:
        return _err("Placement not found", code="NOT_FOUND", status=404)
    return _ok(preview)


@login_required(login_url="login")
@require_POST
def tw_placement_move_view(request: HttpRequest, placement_id: int) -> JsonResponse:
    """Move an existing placement to a new day/time/room.

    Locked placements require ``override: true`` in the payload.
    Returns updated placement data plus the new validation result.
    """
    deny = _require_general_advisor(request)
    if deny:
        return deny
    payload, err = _safe_json(request)
    if err:
        return err

    try:
        placement = SectionPlacement.objects.select_related("board__scenario", "term_section").get(
            id=placement_id
        )
    except SectionPlacement.DoesNotExist:
        return _err("Placement not found", code="NOT_FOUND", status=404)

    if placement.board.scenario.status == "published":
        return _err("Cannot modify published scenario", code="SCENARIO_PUBLISHED", status=400)

    if placement.is_locked and not payload.get("override"):
        return _err(
            "Placement is locked. Pass override: true to force.",
            code="PLACEMENT_LOCKED",
            status=400,
        )

    new_day = str(payload.get("day", placement.day)).strip().upper()
    new_start = str(payload.get("start_time", placement.start_time)).strip()
    new_end = str(payload.get("end_time", placement.end_time)).strip()
    new_room = str(payload.get("room", placement.room)).strip()
    if OnlineCourseLookup().is_online_course_for_board(
        placement.board, placement.term_section.course_code
    ):
        new_room = ""

    old_day, old_start, old_end = placement.day, placement.start_time, placement.end_time

    # Validate before persisting
    validation = validate_placement(
        board_id=placement.board_id,
        day=new_day,
        start_time=new_start,
        end_time=new_end,
        room=new_room,
        term_section_id=placement.term_section_id,
        exclude_placement_id=placement.id,
    )

    placement.day = new_day
    placement.start_time = new_start
    placement.end_time = new_end
    placement.room = new_room

    try:
        placement.save(update_fields=["day", "start_time", "end_time", "room", "updated_at"])
    except IntegrityError:
        return _err("Duplicate placement at new position", code="PLACEMENT_DUPLICATE", status=409)

    log_audit_event(
        request,
        action="tw.placement.move",
        status="success",
        details={
            "placement_id": placement_id,
            "from": f"{old_day} {old_start}-{old_end}",
            "to": f"{new_day} {new_start}-{new_end}",
        },
    )
    return _ok({"placement": _placement_to_dict(placement), "validation": validation})


@login_required(login_url="login")
@require_POST
def tw_placement_remove_view(request: HttpRequest, placement_id: int) -> JsonResponse:
    """Remove a placement from its board.

    Locked placements require ``override: true`` in the payload.
    """
    deny = _require_general_advisor(request)
    if deny:
        return deny
    payload, err = _safe_json(request)
    if err:
        return err

    try:
        placement = SectionPlacement.objects.select_related("board__scenario", "term_section").get(
            id=placement_id
        )
    except SectionPlacement.DoesNotExist:
        return _err("Placement not found", code="NOT_FOUND", status=404)

    if placement.board.scenario.status == "published":
        return _err("Cannot modify published scenario", code="SCENARIO_PUBLISHED", status=400)

    if placement.is_locked and not payload.get("override"):
        return _err(
            "Placement is locked. Pass override: true to force.",
            code="PLACEMENT_LOCKED",
            status=400,
        )

    pid = placement.id
    ts_id = placement.term_section_id
    board_id = placement.board_id
    placement.delete()

    log_audit_event(
        request,
        action="tw.placement.remove",
        status="success",
        details={"placement_id": pid, "board_id": board_id, "term_section_id": ts_id},
    )
    return _ok({"removed": pid})


@login_required(login_url="login")
@require_POST
def tw_placement_lock_view(request: HttpRequest, placement_id: int) -> JsonResponse:
    """Toggle the lock flag on a placement (locked placements resist move/remove)."""
    deny = _require_general_advisor(request)
    if deny:
        return deny

    try:
        placement = SectionPlacement.objects.select_related("board__scenario").get(id=placement_id)
    except SectionPlacement.DoesNotExist:
        return _err("Placement not found", code="NOT_FOUND", status=404)

    if placement.board.scenario.status == "published":
        return _err("Cannot modify published scenario", code="SCENARIO_PUBLISHED", status=400)

    placement.is_locked = not placement.is_locked
    placement.save(update_fields=["is_locked", "updated_at"])
    return _ok({"placement_id": placement_id, "is_locked": placement.is_locked})


@login_required(login_url="login")
@require_GET
def tw_instructors_list_view(request: HttpRequest) -> JsonResponse:
    """Active instructors for the workspace drawer typeahead. ``?q=`` filters."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    from django.db.models import Q

    from core.models import Instructor

    qs = Instructor.objects.filter(is_active=True)
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(full_name__icontains=q) | Q(full_name_ar__icontains=q))
    qs = qs.order_by("full_name")[:200]
    return _ok(
        {
            "instructors": [
                {"id": i.pk, "full_name": i.full_name, "full_name_ar": i.full_name_ar} for i in qs
            ]
        }
    )


@login_required(login_url="login")
@require_POST
def tw_section_instructors_set_view(request: HttpRequest, term_section_id: int) -> JsonResponse:
    """Replace a section's instructor set from the workspace drawer.

    Body: ``{instructor_ids: [...]}`` and/or ``{instructor_names: [...]}``. An
    empty set clears all instructors (reverts the meeting display cache to "").
    Blocked on published scenarios. Links are the source of truth; the primary
    instructor's name is written through to the meeting rows.
    """
    deny = _require_general_advisor(request)
    if deny:
        return deny

    try:
        section = TermSection.objects.select_related("scenario").get(pk=term_section_id)
    except TermSection.DoesNotExist:
        return _err("Section not found", code="NOT_FOUND", status=404)

    if section.scenario_id and section.scenario.status == "published":
        return _err("Cannot modify published scenario", code="SCENARIO_PUBLISHED", status=400)

    payload, err = _safe_json(request)
    if err:
        return err

    from core.services.instructor_assignment import set_section_instructors

    instructor_ids = payload.get("instructor_ids") or []
    instructor_names = payload.get("instructor_names") or []
    if not isinstance(instructor_ids, list) or not isinstance(instructor_names, list):
        return _err(
            "instructor_ids / instructor_names must be lists", code="VALIDATION", status=400
        )

    try:
        instructors = set_section_instructors(
            section, instructor_ids=instructor_ids, instructor_names=instructor_names
        )
    except ValueError as exc:
        return _err(str(exc), code="NOT_FOUND", status=404)

    log_audit_event(
        request,
        action="tw_section_instructors_set",
        status="success",
        details={"term_section_id": section.id, "count": len(instructors)},
    )
    return _ok({"term_section_id": section.id, "instructors": instructors})


@login_required(login_url="login")
@require_POST
def tw_blocked_slots_view(request: HttpRequest, scenario_id: int) -> JsonResponse:
    """Toggle a ``(day, start)`` cell in the scenario's blocked-slots list.

    Blocked slots are institutionally reserved cells the planner must never use
    — enforced by construction across every placement stage, and a publish
    blocker if a placement still occupies one. POST body: ``{day, start}``.
    Adds the cell if absent, removes it if present. Returns the updated
    ``blocked_slots`` list and whether the cell is now blocked.
    """
    deny = _require_general_advisor(request)
    if deny:
        return deny

    try:
        scenario = TimetableScenario.objects.get(pk=scenario_id)
    except TimetableScenario.DoesNotExist:
        return _err("Scenario not found", code="NOT_FOUND", status=404)

    if scenario.status == "published":
        return _err("Cannot modify published scenario", code="SCENARIO_PUBLISHED", status=400)

    payload, err = _safe_json(request)
    if err:
        return err

    day = str(payload.get("day", "")).strip()
    start = str(payload.get("start", "")).strip()
    if not day or not start:
        return _err("day and start are required", code="VALIDATION", status=400)

    blocked = list(scenario.blocked_slots or [])
    key = (day, start)
    already = any((bs.get("day"), bs.get("start")) == key for bs in blocked)
    if already:
        blocked = [bs for bs in blocked if (bs.get("day"), bs.get("start")) != key]
        now_blocked = False
    else:
        blocked.append({"day": day, "start": start})
        now_blocked = True

    scenario.blocked_slots = blocked
    scenario.save(update_fields=["blocked_slots", "updated_at"])
    return _ok(
        {
            "scenario_id": scenario_id,
            "day": day,
            "start": start,
            "blocked": now_blocked,
            "blocked_slots": blocked,
        }
    )


# ── Slot Template Endpoints ──────────────────────────────────────


@login_required(login_url="login")
@require_GET
def tw_slot_templates_list_view(request: HttpRequest) -> JsonResponse:
    """List all saved slot templates, ordered newest first."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    templates = TimeSlotTemplate.objects.all().order_by("-created_at")
    return _ok(
        {
            "templates": [
                {
                    "id": t.id,
                    "name": t.name,
                    "slots": t.slots,
                    "is_default": t.is_default,
                    "created_at": t.created_at.isoformat() if t.created_at else "",
                }
                for t in templates
            ]
        }
    )


@login_required(login_url="login")
@require_POST
def tw_slot_template_create_view(request: HttpRequest) -> JsonResponse:
    """Create a new slot template.

    Accepts ``name``, ``slots`` (array), and optional ``is_default``.
    If ``is_default`` is true, all other templates are un-defaulted first.
    """
    deny = _require_general_advisor(request)
    if deny:
        return deny
    payload, err = _safe_json(request)
    if err:
        return err

    name = str(payload.get("name", "")).strip()
    slots = payload.get("slots")
    is_default = bool(payload.get("is_default", False))

    if not name:
        return _err("name is required", code="VALIDATION_NAME", status=400)
    if not isinstance(slots, list):
        return _err("slots must be an array", code="VALIDATION_SLOTS", status=400)

    if is_default:
        TimeSlotTemplate.objects.filter(is_default=True).update(is_default=False)

    tpl = TimeSlotTemplate.objects.create(name=name, slots=slots, is_default=is_default)

    log_audit_event(
        request,
        action="tw.slot_template.create",
        status="success",
        details={"template_id": tpl.id, "name": name, "slot_count": len(slots)},
    )
    return _ok(
        {
            "template": {
                "id": tpl.id,
                "name": tpl.name,
                "slots": tpl.slots,
                "is_default": tpl.is_default,
            }
        },
        status=201,
    )


# ── V2 Optimiser ────────────────────────────────────────────────


@login_required(login_url="login")
@require_POST
def tw_optimise_v2_view(request: HttpRequest, scenario_id: int) -> JsonResponse:
    """Run the v2 optimisation pipeline on an existing scenario.

    POST body (all optional):
        mode: str              — "current" (improve existing board) or
                                 "full" (regenerate from scratch). Default "current".
        strategies: list[str]  — strategies for "full" mode. Default all.
        run_local_search: bool — run local search. Default true.
        max_iterations: int    — local search iterations. Default 50.
        run_chain_search: bool — run chain-2 search. Default true.
        run_cpsat_polish: bool — run CP-SAT polisher. Default true.
        cpsat_time_limit: int  — CP-SAT time budget (seconds). Default 60.
    """
    deny = _require_general_advisor(request)
    if deny:
        return deny

    try:
        TimetableScenario.objects.get(pk=scenario_id)
    except TimetableScenario.DoesNotExist:
        return _err("Scenario not found", code="NOT_FOUND", status=404)

    payload, err = _safe_json(request)
    if err:
        return err

    mode = payload.get("mode", "current")

    # WS-E — opt-in async dispatch. When ``async_run`` is set, the slow V2
    # pipeline is queued on the planner job runner (off the request thread, so
    # gunicorn's 120s timeout can't SIGKILL it mid-run) and a 202 + job id is
    # returned for the existing PR8 job-poll UI. Default stays SYNCHRONOUS so
    # the current frontend contract is unchanged.
    if payload.get("async_run"):
        from core.models import PlannerJob
        from core.services.planner_job_runner import dispatch_planner_job, submit_planner_job

        job_mode = (
            PlannerJob.MODE_OPTIMISE_V2_FULL
            if mode == "full"
            else PlannerJob.MODE_OPTIMISE_V2_CURRENT
        )
        # Carry the per-request tuning so the async job reproduces the same
        # rebuild the sync path would have (the runner replays these).
        job_params = {
            "strategies": payload.get("strategies") or None,
            "max_iterations": payload.get("max_iterations", 50),
            "run_chain_search": payload.get("run_chain_search", True),
            "run_cpsat_polish": payload.get("run_cpsat_polish", True),
            "cpsat_time_limit": payload.get("cpsat_time_limit", 60),
            "max_chain_iterations": payload.get("max_chain_iterations", 10),
        }
        job_id = submit_planner_job(
            scenario_id=scenario_id, mode=job_mode, user=request.user, params=job_params
        )
        dispatch_planner_job(job_id)
        return _ok({"job_id": str(job_id), "mode": mode, "async": True}, status=202)

    # Delegate to the shared, safety-gated runner (WS-E). The same function
    # runs from the async planner job, so the snapshot → run → regression →
    # rollback gate is identical on both paths.
    result = run_v2_optimisation_guarded(
        scenario_id,
        mode=mode,
        max_iterations=int(payload.get("max_iterations", 50)),
        run_chain=bool(payload.get("run_chain_search", True)),
        run_cpsat=bool(payload.get("run_cpsat_polish", True)),
        cpsat_limit=float(payload.get("cpsat_time_limit", 60)),
        strategies=payload.get("strategies") or None,
        max_chain_iterations=int(payload.get("max_chain_iterations", 10)),
    )

    if "error" in result:
        # Internal optimiser exception → 500; no-data (no candidates / no
        # profiles) → 400 — preserving the prior status-code contract.
        is_internal = str(result["error"]).startswith("Optimiser error")
        return _err(
            result["error"],
            code="OPTIMISER_ERROR" if is_internal else "OPTIMISER_NO_DATA",
            status=500 if is_internal else 400,
        )

    return _ok({"optimisation": result})

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
    POST tw_scenario_copy_view           -- copy the workspace into a new draft
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
    POST tw_placement_programme_preference_view -- mark a section mainly for a programme

Slot Templates
    GET  tw_slot_templates_list_view     -- list slot templates
    POST tw_slot_template_create_view    -- create a slot template
"""

from __future__ import annotations

import functools
import json
import logging
import math
import re
from dataclasses import dataclass

from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
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
from core.services.board_ownership import external_shared_section_ids
from core.services.job_runtime import (
    HOLDER_EXAM_JOB,
    HOLDER_PLANNER,
    HOLDER_REPAIR_ANALYSIS,
    SolverBusy,
    solver_slot,
)
from core.services.rbac import ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN, get_user_role
from core.services.request_lanes import preview_lane, server_busy_response
from core.services.section_move_optimisation import section_move_optimisation_engine
from core.services.section_programme_preference import allowed_programmes, normalise_programme
from core.services.section_programmes import (
    filter_sections_for_delivery_board,
    section_is_available_to_delivery_board,
)
from core.services.timetable_demand import compute_board_capacity
from core.services.timetable_flags import (
    is_one_teacher_active,
    is_section_programme_preference_enabled,
)
from core.services.timetable_generate import generate_workspace_scenario
from core.services.timetable_graph_twin import (
    TimetableGraphError,
    build_scenario_graph_summary,
    build_scenario_graph_view,
    neo4j_status,
    sync_scenario_graph_to_neo4j,
)
from core.services.timetable_move_outcome import preview_placement_student_outcome_candidates
from core.services.timetable_mutation_guard import (
    ScenarioPlannerBusy,
    ScenarioPublished,
    active_planner_job,
    guard_manual_scenario_write,
    reject_active_planner_job,
    scenario_busy_response,
)
from core.services.timetable_online import OnlineCourseLookup
from core.services.timetable_overlap import HARD_OVERLAP_THRESHOLD
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
from core.services.timetable_run_seed import parse_run_seed, resolve_run_seed
from core.services.timetable_scenario_copy import ScenarioCopyError, copy_timetable_scenario
from core.services.timetable_section_policy import (
    SectionPolicyError,
    create_policy_section_bundle,
    policy_payload,
    remove_policy_section_bundle,
    section_policy,
    switch_policy_section_bundle,
)
from core.services.timetable_slot_config import (
    carry_slot_flags,
    clean_slot_rows,
    confirmation_fingerprint,
    dropped_flagged_columns,
    effective_lab_slots,
    effective_lecture_slots,
    hidden_by_slot_change,
)
from core.services.timetable_student_blockers import build_scenario_student_blockers
from core.services.timetable_workspace import (
    apply_bulk_clean_room_assignments,
    apply_bulk_safe_time_moves,
    assign_physical_meeting_room,
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
        # The columns the scenario really uses (its own, or the defaults): the
        # pages draw and edit these, never defaults of their own.
        "effective_slot_config": effective_lecture_slots(s),
        "effective_lab_slot_config": effective_lab_slots(s),
        "blocked_slots": s.blocked_slots,
        # What the scenario was generated for, so a new generation (and the
        # elective mapping) can start from the open scenario's scope.
        "gender": s.gender or "",
        "programs": list(s.programs or []),
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


def _section_teachers(scenario_id: int) -> dict[int, dict] | None:
    """One teacher per section: each placed section's teacher, read once per
    request from the map the engine charges (``section_teacher_display``);
    None with the rule off, so no placement gets a ``teacher`` key."""
    if not is_one_teacher_active():
        return None
    from core.services.section_teacher_assignment import section_teacher_display

    return section_teacher_display(scenario_id)


def _one_teacher_request(view):
    """One teacher per section: every map read in the request (validation,
    backstop, display) shares one read of the stored rows. Off: the view as is."""

    @functools.wraps(view)
    def wrapped(request, *args, **kwargs):
        if not is_one_teacher_active():
            return view(request, *args, **kwargs)
        from core.services.section_teacher_assignment import request_scope

        with request_scope():
            return view(request, *args, **kwargs)

    return wrapped


def _programme_marks(scenario_id: int) -> dict | None:
    """The "mainly for programme" context for ``_placement_to_dict``, read once
    per request: None with the feature off (no placement gets the keys, as with
    ``teacher``), else the board's scenario and the sections another scenario
    also places — a mark, like a time, is set where the section is owned."""
    if not is_section_programme_preference_enabled():
        return None
    return {"scenario_id": scenario_id, "frozen": external_shared_section_ids(scenario_id)}


_MEETING_FIELDS = ("day", "start_time", "end_time", "building", "room", "instructor")


@dataclass
class PlacementPayloadContext:
    """What every placement payload of one request shares, read once.

    ``meetings`` maps a section to its meeting rows (one query for a list, not
    one per placement); ``shared`` is the set of sections another scenario
    also places (``external_shared_section_ids``); ``online`` answers whether a
    course is online for a board, as the publish check decides it.
    """

    meetings: dict[int, list[dict[str, object]]]
    shared: set[int]
    online: OnlineCourseLookup
    boards: dict[int, DeliveryBoard]

    @classmethod
    def build(
        cls, scenario_id: int, placements: list[SectionPlacement], *, shared: set[int] | None = None
    ) -> PlacementPayloadContext:
        section_ids = {p.term_section_id for p in placements}
        meetings: dict[int, list[dict[str, object]]] = {sid: [] for sid in section_ids}
        for row in (
            TermSectionMeeting.objects.filter(term_section_id__in=section_ids)
            .order_by("id")
            .values("term_section_id", *_MEETING_FIELDS)
        ):
            meetings[row.pop("term_section_id")].append(row)
        boards = {
            b.id: b for b in DeliveryBoard.objects.filter(id__in={p.board_id for p in placements})
        }
        return cls(
            meetings=meetings,
            # The programme marks already read this set (their "frozen").
            shared=shared if shared is not None else external_shared_section_ids(scenario_id),
            online=OnlineCourseLookup(),
            boards=boards,
        )


def _room_state(room: str | None) -> str:
    """``assigned``, ``unset`` (blank) or ``unassigned`` (the placer's marker).

    Exact equality with ``UNASSIGNED``, as the publish check filters it.
    """
    if room == "UNASSIGNED":
        return "unassigned"
    return "assigned" if (room or "").strip() else "unset"


def _placement_payloads(
    primary: SectionPlacement, rows, teachers: dict | None, marks: dict | None
) -> dict[str, object]:
    """``placement`` and the board's ``placements``, from one shared context."""
    rows = list(rows)
    ctx = PlacementPayloadContext.build(
        primary.board.scenario_id, [primary, *rows], shared=marks["frozen"] if marks else None
    )
    return {
        "placement": _placement_to_dict(primary, teachers, marks, ctx),
        "placements": [_placement_to_dict(row, teachers, marks, ctx) for row in rows],
    }


def _placement_to_dict(
    p: SectionPlacement,
    teachers: dict | None = None,
    marks: dict | None = None,
    ctx: PlacementPayloadContext | None = None,
) -> dict[str, object]:
    """Serialise a ``SectionPlacement`` with its meetings to a JSON-safe dict.

    Includes the related ``TermSection`` fields (course code, capacity, etc.)
    and all ``TermSectionMeeting`` records for that section. ``teachers``
    (``_section_teachers``, one teacher per section only) adds the section's
    ``teacher``: ``{"id", "name", "status", "source", "reason"}``, or None for
    a section the map leaves out. ``marks`` (``_programme_marks``) adds the
    section's ``preferred_programme`` and whether this board may change it
    (``preferred_programme_editable``: the board's scenario owns the section
    and no other scenario places it).

    Every payload also says, whatever the flags, what the publish check would
    say about its room: ``is_online`` (online for its board), ``needs_room``
    (not online, and no meeting policy or one that uses a shared room),
    ``room_state`` and ``shared_with_other_scenario``. A list passes one
    ``ctx`` (``PlacementPayloadContext.build``); a single payload builds its own.
    """
    ts = p.term_section
    if ctx is None:
        ctx = PlacementPayloadContext.build(p.board.scenario_id, [p])
    meetings = ctx.meetings.get(ts.id)
    if meetings is None:
        meetings = list(
            TermSectionMeeting.objects.filter(term_section=ts)
            .order_by("id")
            .values(*_MEETING_FIELDS)
        )
    policy = section_policy(ts)
    board = ctx.boards.get(p.board_id) or p.board
    is_online = ctx.online.is_online_course_for_board(board, ts.course_code)
    out: dict[str, object] = {
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
        **({"room_required": False, "no_room_required": True} if policy else {}),
        "meetings": meetings,
        "section_policy": policy_payload(policy, meetings) if policy else None,
        "is_online": is_online,
        "needs_room": not is_online and (policy is None or bool(policy.uses_shared_room)),
        "room_state": _room_state(p.room),
        "shared_with_other_scenario": ts.id in ctx.shared,
    }
    if teachers is not None:
        out["teacher"] = teachers.get(ts.id)
    if marks is not None:
        out["preferred_programme"] = ts.preferred_programme
        out["preferred_programme_editable"] = (
            ts.scenario_id == marks["scenario_id"] and ts.id not in marks["frozen"]
        )
    return out


# ── Page View ────────────────────────────────────────────────────


def _workspace_page_scope(request: HttpRequest) -> dict[str, str]:
    """Use an explicitly requested scenario's scope before filter lists load."""
    defaults = load_defaults()
    scope = {
        "default_year": str(defaults.get("academic_year", "") or ""),
        "default_term": str(defaults.get("term", "") or ""),
        "initial_scenario": "",
        "initial_board": request.GET.get("board") or "",
    }
    try:
        scenario_id = int(request.GET.get("scenario") or "0")
    except (ValueError, TypeError):
        return scope
    if not 0 < scenario_id <= 2**63 - 1:
        return scope
    scenario = (
        TimetableScenario.objects.filter(pk=scenario_id).values("academic_year", "term").first()
    )
    if scenario:
        scope.update(
            default_year=str(scenario["academic_year"] or ""),
            default_term=str(scenario["term"] or ""),
            initial_scenario=str(scenario_id),
        )
    return scope


@login_required(login_url="login")
@require_GET
def timetable_workspace_page(request: HttpRequest) -> HttpResponse:
    """Render the Timetable Workspace SPA shell page.

    Uses an explicit scenario's academic year and term, otherwise saved system
    defaults, so the frontend initializes its filters in the requested scope.
    ``programme_choices`` lists the programme codes that have a degree plan.
    """
    deny = _require_general_advisor(request)
    if deny:
        return deny
    import json as _json

    from django.conf import settings as _settings
    from django.middleware.csrf import get_token as _csrf

    from core.services.pr8_async_job_ui import (
        POLL_INTERVAL_MS,
        endpoint_map,
        is_async_job_ui_effective,
    )
    from core.services.section_limits import known_programmes

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
        # The workspace's card is a monitor: Optimise ▾ Full rebuild is the
        # rebuild here, so the card's legacy one is not offered.
        "offerRebuild": False,
    }
    context = {
        **get_sidebar_context(request),
        **_workspace_page_scope(request),
        # The programmes with a degree plan (ProgrammeRequirement), one
        # DISTINCT query: the Generate dialog's chips, not student data.
        "programme_choices": sorted(known_programmes()),
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
    context = {
        **get_sidebar_context(request),
        **_workspace_page_scope(request),
    }
    return render(request, "core/timetable_workspace_split.html", context)


@login_required(login_url="login")
@require_GET
def timetable_workspace_mri_page(request: HttpRequest) -> HttpResponse:
    """Render the full-screen Timetable MRI diagnostic page."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    context = {
        **get_sidebar_context(request),
        **_workspace_page_scope(request),
    }
    return render(request, "core/timetable_workspace_mri.html", context)


@login_required(login_url="login")
@require_GET
def timetable_workspace_graph_page(request: HttpRequest) -> HttpResponse:
    """Render the Neo4j timetable graph twin control page."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    context = {
        **get_sidebar_context(request),
        **_workspace_page_scope(request),
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
    try:
        seed = parse_run_seed(payload.get("seed"))
    except ValueError as exc:
        return _err(str(exc), code="VALIDATION_SEED", status=400)

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
    # Which placement engine fills the scaffold. Both build the SAME scenario
    # scaffold first — same students, same boards, same section budgets — so the
    # two differ in exactly one respect: where the classes go.
    engine = str(payload.get("engine") or "classic").strip().lower()
    if engine not in {"classic", "scheduler"}:
        return _err("Unknown timetable engine", code="VALIDATION_GENERATE", status=400)
    scheduler_params: dict | None = None
    if engine == "scheduler":
        if section not in {"M", "F"}:
            return _err(
                "Choose M or F for the scheduler's single-gender timetable.",
                code="VALIDATION_GENERATE",
                status=400,
            )
        # Validate before creating the scaffold: invalid tuning must not leave
        # an empty scenario behind. Existing `seconds` means per attempt;
        # `total_seconds` includes the shared seating-confirmation allowance.
        try:
            runs = int(str(payload.get("runs", 3)))
            confirmation_seconds = float(str(payload.get("confirmation_seconds", 20)))
            if "total_seconds" in payload:
                total_seconds = float(str(payload["total_seconds"]))
                seconds = (total_seconds - confirmation_seconds) / max(1, runs)
            else:
                seconds = float(str(payload.get("seconds", 120)))
            clash_tolerance = float(str(payload.get("clash_tolerance", 0.20)))
            if not (
                1 <= runs <= 8
                and math.isfinite(seconds)
                and seconds > 0
                and math.isfinite(confirmation_seconds)
                and confirmation_seconds > 0
                and math.isfinite(clash_tolerance)
                and 0 <= clash_tolerance <= 1
            ):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            return _err(
                "Scheduler requires 1-8 runs, positive finite search/confirmation "
                "budgets, and clash_tolerance between 0 and 1.",
                code="VALIDATION_GENERATE",
                status=400,
            )
        scheduler_params = {
            "academic_year": str(year_int),
            "term": semester_int,
            "programs": programs,
            "gender": section or "M",
            "seconds": seconds,
            "runs": runs,
            "confirmation_seconds": confirmation_seconds,
            "clash_tolerance": clash_tolerance,
            "default_capacity": max_local_4cr,
        }

    # WS-E follow-up — defer the heavy section placement off the request thread.
    # When ``defer_build`` is set and the async planner is available, build only
    # the scenario scaffold synchronously (fast) and run the placement + optimise
    # + compaction as an async planner job, so a multi-program build can't overrun
    # the request timeout (the failure mode that SIGKILLed sync generate). Falls
    # back to the synchronous build when async is unavailable.
    from core.services.planner_job_runner import is_async_planner_enabled

    use_async_build = bool(payload.get("defer_build")) and is_async_planner_enabled()

    seed = resolve_run_seed(seed)
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
            run_autoplace=False,
        )
    except Exception as exc:
        logger.exception("Generate workspace failed")
        return _err(str(exc), code="GENERATE_FAILED", details={"seed": seed}, status=500)

    result["seed"] = seed
    if engine == "scheduler":
        assert scheduler_params is not None
        build_params = {**scheduler_params, "seed": seed}
    else:
        build_params = {"cpsat_time_limit": 60, "seed": seed}

    if use_async_build:
        from core.models import PlannerJob
        from core.services.planner_job_runner import dispatch_planner_job, submit_planner_job

        try:
            if engine == "scheduler":
                job_mode = PlannerJob.MODE_SCHEDULER_BUILD
            else:
                job_mode = PlannerJob.MODE_OPTIMISE_V2_FULL
            build_job_id = submit_planner_job(
                scenario_id=result["scenario"]["id"],
                mode=job_mode,
                user=request.user,
                params=build_params,
            )
            dispatch_planner_job(build_job_id)
            result["build_job_id"] = str(build_job_id)
            result["engine"] = engine
        except Exception:
            logger.exception(
                "Deferred build dispatch failed for scenario %s", result["scenario"]["id"]
            )
            result["build_job_id"] = None  # frontend falls back to a manual rebuild
    else:
        # The same engine/policy runs when background execution is disabled.
        # Choosing scheduler must never silently build a classic timetable.
        try:
            from core.models import PlannerJob
            from core.services.planner_job_runner import run_planner_job_synchronously

            build_summary = run_planner_job_synchronously(
                scenario_id=result["scenario"]["id"],
                mode=PlannerJob.MODE_SCHEDULER_BUILD
                if engine == "scheduler"
                else PlannerJob.MODE_OPTIMISE_V2_FULL,
                user=request.user,
                params=build_params,
            )
            if "error" in build_summary:
                return _err(
                    str(build_summary["error"]),
                    code="GENERATE_FAILED",
                    details={"seed": seed},
                    status=500,
                )
            result["engine"] = engine
            result["build_summary"] = build_summary
            result["section_budget"] = compute_scenario_budget(result["scenario"]["id"])
        except Exception as exc:
            logger.exception("Synchronous build failed for scenario %s", result["scenario"]["id"])
            return _err(str(exc), code="GENERATE_FAILED", details={"seed": seed}, status=500)

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
@preview_lane
def tw_scenario_plan_lens_view(request: HttpRequest, scenario_id: int) -> JsonResponse:
    """Return read-only programme demand and section ownership metadata.

    It runs a full seating evaluation (the drawer's seated mix by programme and
    the per-plan counts), so it is a heavy preview and takes the preview lane.
    """
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
@require_POST
def tw_scenario_copy_view(request: HttpRequest, scenario_id: int) -> JsonResponse:
    """Create a separate draft without changing or publishing the source."""
    deny = _require_general_advisor(request)
    if deny is not None:
        return deny
    payload, error = _safe_json(request)
    if error is not None:
        return error
    try:
        result = copy_timetable_scenario(
            scenario_id, name=payload.get("name"), created_by=request.user.get_username()
        )
    except TimetableScenario.DoesNotExist:
        return _err("Scenario not found", code="NOT_FOUND", status=404)
    except ScenarioPlannerBusy as exc:
        return scenario_busy_response(exc.job)
    except ScenarioCopyError as exc:
        return _err(str(exc), code=exc.code, status=exc.status)
    log_audit_event(
        request,
        action="tw.scenario.copy",
        status="success",
        details={
            "source_scenario_id": scenario_id,
            "scenario_id": result.scenario.pk,
            **result.counts,
        },
    )
    return _ok(
        {
            "scenario": _scenario_to_dict(result.scenario),
            "boards": [_board_to_dict(board) for board in result.boards],
            "copied_from_scenario_id": scenario_id,
            "copy_summary": result.counts,
        },
        status=201,
    )


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
    from core.services.planner_job_runner import (
        is_async_planner_enabled,
        serialize_planner_job,
    )

    active_job = None
    if is_async_planner_enabled():
        job = active_planner_job(scenario_id)
        if job is not None:
            active_job = serialize_planner_job(job)
    return _ok({"scenario": _scenario_to_dict(scenario), "active_job": active_job})


@login_required(login_url="login")
@require_GET
def tw_scenario_quality_view(request: HttpRequest, scenario_id: int) -> JsonResponse:
    """Return current scenario-wide quality without modifying or solving rooms."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    if not TimetableScenario.objects.filter(id=scenario_id).exists():
        return _err("Scenario not found", code="NOT_FOUND", status=404)
    from core.services.timetable_scenario_quality import build_scenario_quality

    return _ok(build_scenario_quality(scenario_id))


@login_required(login_url="login")
@require_POST
@guard_manual_scenario_write
def tw_scenario_slots_update_view(request: HttpRequest, scenario_id: int) -> JsonResponse:
    """Replace the grid columns (time slots) of a draft scenario.

    Body: ``slot_config`` (lecture columns) and optionally ``lab_slot_config``
    (lab columns; left as it is when absent), each an array of
    ``{label, start, end}``; an empty array means the defaults.

    - Every column is validated (``clean_slot_rows``); a bad one is a 400
      ``VALIDATION_SLOTS`` naming its list, index and field.
    - A column keeps the flags of the column it replaces (``online_only``,
      ``policy_only``), matched by start and end, unless the request sets them:
      the editors send plain rows, and an online window that lost its flag
      would become a place an engine may book a room in the evening.
    - A change that would leave drawn meetings off the grid, on any board, is
      a 409 ``SLOT_EDIT_NEEDS_CONFIRMATION`` listing them, and so is one that
      removes a flagged column (an online-only window moved to another time
      becomes a plain column); it saves only when the request repeats the
      ``fingerprint`` of exactly that list as ``confirm``.
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

    if not isinstance(payload.get("slot_config"), list):
        return _err("slot_config must be an array", code="VALIDATION_SLOTS", status=400)
    slots, errors = clean_slot_rows(payload["slot_config"], "slot_config")
    lab_given = payload.get("lab_slot_config") is not None
    if lab_given:
        lab_slots, lab_errors = clean_slot_rows(payload["lab_slot_config"], "lab_slot_config")
        errors += lab_errors
    else:
        lab_slots = list(scenario.lab_slot_config or [])
    if errors:
        return _err(
            "Some time slots are not valid",
            code="VALIDATION_SLOTS",
            status=400,
            details={"errors": [error.as_dict() for error in errors]},
        )

    # The flags of the columns being replaced: the effective ones, so a
    # scenario still on the defaults keeps the online windows' flag too.
    slots = carry_slot_flags(slots, effective_lecture_slots(scenario))
    if lab_given:
        lab_slots = carry_slot_flags(lab_slots, effective_lab_slots(scenario))

    hidden = hidden_by_slot_change(scenario, slots, lab_slots)
    dropped = dropped_flagged_columns(effective_lecture_slots(scenario), slots, "slot_config")
    if lab_given:
        dropped += dropped_flagged_columns(
            effective_lab_slots(scenario), lab_slots, "lab_slot_config"
        )
    if hidden or dropped:
        fingerprint = confirmation_fingerprint(hidden, dropped)
        if payload.get("confirm") != fingerprint:
            return _err(
                f"This change would leave {len(hidden)} meeting(s) off the grid"
                f" and remove {len(dropped)} flagged column(s)",
                code="SLOT_EDIT_NEEDS_CONFIRMATION",
                status=409,
                details={
                    "hidden": hidden[:200],
                    "hidden_count": len(hidden),
                    "dropped_flags": dropped,
                    "fingerprint": fingerprint,
                },
            )

    scenario.slot_config = slots
    update_fields = ["slot_config", "updated_at"]
    if lab_given:
        scenario.lab_slot_config = lab_slots
        update_fields.append("lab_slot_config")
    scenario.save(update_fields=update_fields)

    log_audit_event(
        request,
        action="tw.scenario.slots_update",
        status="success",
        details={
            "scenario_id": scenario_id,
            "slot_count": len(slots),
            "lab_slot_count": len(lab_slots) if lab_given else None,
            "hidden_count": len(hidden),
            "dropped_flag_count": len(dropped),
        },
    )
    return _ok({"scenario": _scenario_to_dict(scenario), "hidden_count": len(hidden)})


@login_required(login_url="login")
@require_GET
@_one_teacher_request
def tw_scenario_readiness_view(request: HttpRequest, scenario_id: int) -> JsonResponse:
    """Return publish readiness without mutating the scenario.

    ``readiness`` is ``check_publish_readiness`` as is: the English
    ``blockers`` / ``warnings`` and the parallel structured ``blocker_items`` /
    ``warning_items``. One teacher map is read for the whole request (each
    board's conflict check would otherwise recompute it).
    """
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
@guard_manual_scenario_write
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
@guard_manual_scenario_write
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
@guard_manual_scenario_write
@_one_teacher_request
def tw_scenario_publish_view(request: HttpRequest, scenario_id: int) -> JsonResponse:
    """Publish (lock) a scenario, preventing further modifications.

    Runs a readiness check first; if any blocker exists the publish is
    rejected with ``PUBLISH_BLOCKED``, a message that counts the blockers, and
    ``details`` carrying the blockers/warnings (English strings) and the
    structured ``blocker_items`` / ``warning_items``.
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
        blocker_count = len(readiness["blockers"])
        return _err(
            f"Cannot publish: {blocker_count} {'blocker' if blocker_count == 1 else 'blockers'}",
            code="PUBLISH_BLOCKED",
            status=400,
            details={
                "blockers": readiness["blockers"],
                "warnings": readiness["warnings"],
                "blocker_items": readiness["blocker_items"],
                "warning_items": readiness["warning_items"],
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
@_one_teacher_request
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
@_one_teacher_request
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
@guard_manual_scenario_write
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

    placements = list(
        SectionPlacement.objects.filter(board=board).select_related("term_section", "board")
    )
    from core.models import BoardStudentLink

    primary_count = BoardStudentLink.objects.filter(board=board, link_type="primary").count()
    visitor_count = BoardStudentLink.objects.filter(board=board, link_type="visitor").count()
    teachers = _section_teachers(board.scenario_id)
    marks = _programme_marks(board.scenario_id)
    ctx = PlacementPayloadContext.build(
        board.scenario_id, placements, shared=marks["frozen"] if marks else None
    )
    return _ok(
        {
            "board": _board_to_dict(board),
            "slot_config": board.scenario.slot_config,
            "lab_slot_config": board.scenario.lab_slot_config,
            "effective_slot_config": effective_lecture_slots(board.scenario),
            "effective_lab_slot_config": effective_lab_slots(board.scenario),
            "placements": [_placement_to_dict(p, teachers, marks, ctx) for p in placements],
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
@_one_teacher_request
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
            # Publish readiness warns about a cross-board pair at this many
            # shared students; the page uses the same line (no copy of its own).
            "hard_overlap_threshold": HARD_OVERLAP_THRESHOLD,
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

    # Scenario-owned sections belong to this workspace. Global current-snapshot
    # sections must explicitly serve the board's programme (shared memberships
    # such as AI+DS naturally appear on both boards).
    all_sections = filter_sections_for_delivery_board(
        TermSection.objects.all(),
        scenario_id=board.scenario_id,
        program=board.program,
    ).prefetch_related("program_links", "meetings")

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

        meetings = [
            {
                "day": meeting.day,
                "start_time": meeting.start_time,
                "end_time": meeting.end_time,
                "room": meeting.room,
                "instructor": meeting.instructor,
            }
            for meeting in ts.meetings.all()
        ]
        unplaced.append(
            {
                "term_section_id": ts.id,
                "course_code": ts.course_code,
                "course_name": ts.course_name,
                "course_key": ts.course_key,
                "section": ts.section,
                "available_capacity": ts.available_capacity,
                "registered_count": ts.registered_count,
                "programs": sorted(link.program for link in ts.program_links.all()),
                "meetings": meetings,
            }
        )

    return _ok({"unplaced": unplaced, "count": len(unplaced)})


@login_required(login_url="login")
@require_GET
@preview_lane
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
@guard_manual_scenario_write
@_one_teacher_request
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
    teachers = _section_teachers(result["board"].scenario_id)
    marks = _programme_marks(result["board"].scenario_id)
    return _ok(
        {
            **_placement_payloads(placement, placements, teachers, marks),
            "validation": validation,
            "validations": validations,
            # Forwarded from the service, which already populated
            # involving_this_section. Without this key the JS
            # notifyInstructorClashBackstop call on this path is dead code and
            # the scenario-wide scan the service paid for is thrown away.
            "instructor_clash_backstop": result["instructor_clash_backstop"],
            "required_meetings": result["required_meetings"],
        },
        status=201,
    )


@login_required(login_url="login")
@require_POST
@guard_manual_scenario_write
@_one_teacher_request
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

    if not all([board_id, term_section_id]) or not (
        all([day, start_time, end_time]) or isinstance(payload.get("meetings"), list)
    ):
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

    if not section_is_available_to_delivery_board(
        ts,
        scenario_id=board.scenario_id,
        program=board.program,
    ):
        return _err(
            "TermSection is outside this board's scenario or programme",
            code="SECTION_NOT_AVAILABLE_TO_BOARD",
            status=409,
        )

    if OnlineCourseLookup().is_online_course_for_board(board, ts.course_code):
        room = ""

    if section_policy(ts):
        try:
            placements = create_policy_section_bundle(
                board,
                ts,
                meetings=payload.get("meetings"),
                day=day,
                start_time=start_time,
                end_time=end_time,
                room=room,
            )
        except (SectionPolicyError, ValueError, TypeError, DeliveryBoard.DoesNotExist) as exc:
            return _err(str(exc), code=SectionPolicyError.code, status=400)
        except IntegrityError:
            return _err(
                "Duplicate English section placement", code="PLACEMENT_DUPLICATE", status=409
            )
        if is_one_teacher_active():
            from core.services.section_teacher_assignment import name_new_section

            name_new_section(board.scenario_id, ts, board)
        placement = placements[0]
        log_audit_event(
            request,
            action="tw.placement.create",
            status="success",
            details={
                "placement_id": placement.id,
                "placement_ids": [row.id for row in placements],
                "board_id": board.id,
                "term_section_id": ts.id,
            },
        )
        teachers = _section_teachers(board.scenario_id)
        marks = _programme_marks(board.scenario_id)
        return _ok(
            {
                **_placement_payloads(placement, placements, teachers, marks),
                "section_policy": policy_payload(section_policy(ts), placements),
                **_policy_bundle_feedback(placements),
            },
            status=201,
        )

    if not all([day, start_time, end_time]):
        return _err(
            "day, start_time and end_time are required", code="VALIDATION_PLACEMENT", status=400
        )

    # Validate before persisting. NOTE: validate_placement is BOARD-scoped and
    # keys on the free-text meeting.instructor, so it cannot see an instructor
    # double-booked across two boards. The scenario-wide backstop below reports
    # that class of clash, which this validation structurally misses.
    validation = validate_placement(
        board_id=board.id,
        day=day,
        start_time=start_time,
        end_time=end_time,
        room=room,
        term_section_id=ts.id,
    )

    from core.services.timetable_instructor_backstop import (
        clashes_involving,
        section_id_for,
        verify_persisted_scenario,
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
    if is_one_teacher_active():
        # One teacher per section: the new section gets a teacher it does not
        # clash with, when there is one, and shows that teacher's name.
        from core.services.section_teacher_assignment import name_new_section

        name_new_section(board.scenario_id, ts, board)
    backstop = verify_persisted_scenario(board.scenario_id, context="manual_placement_create")
    backstop["involving_this_section"] = clashes_involving(backstop, section_id_for(ts))
    return _ok(
        {
            "placement": _placement_to_dict(
                placement,
                _section_teachers(board.scenario_id),
                _programme_marks(board.scenario_id),
            ),
            "validation": validation,
            "instructor_clash_backstop": backstop,
        },
        status=201,
    )


@login_required(login_url="login")
@require_GET
@preview_lane
@_one_teacher_request
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
@preview_lane
@_one_teacher_request
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
@preview_lane
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

    # A synchronous CP-SAT search of up to 45 s: it takes the process's one
    # solver slot like every other solve (job_runtime), without waiting - a
    # planner run or an exam job may hold it for minutes.
    try:
        with solver_slot(holder=HOLDER_REPAIR_ANALYSIS, wait=False):
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
    except SolverBusy as busy:
        kind = (busy.holder or {}).get("kind")
        return server_busy_response(
            "Another timetable action is using the solver. Retrying shortly.",
            retry_after={HOLDER_PLANNER: 30, HOLDER_EXAM_JOB: 15}.get(kind, 5),
            details={"holder": busy.holder},
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
@guard_manual_scenario_write
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
@guard_manual_scenario_write
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
@guard_manual_scenario_write
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
@guard_manual_scenario_write
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
@preview_lane
@_one_teacher_request
def tw_placement_room_candidates_view(request: HttpRequest, placement_id: int) -> JsonResponse:
    """Return room candidates for a placement at its current or previewed slot.

    ``?audit=1`` (exactly "1") adds the quality audit's answer for each room
    (``include_audit``); without it the answer is unchanged.
    """
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        preview = preview_placement_room_candidates(
            placement_id,
            day=request.GET.get("day") or None,
            start_time=request.GET.get("start_time") or None,
            end_time=request.GET.get("end_time") or None,
            include_audit=request.GET.get("audit") == "1",
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


def _policy_bundle_feedback(rows: list[SectionPlacement]) -> dict[str, object]:
    from core.services.timetable_instructor_backstop import (
        clashes_involving,
        section_id_for,
        verify_persisted_scenario,
    )

    validations = [
        validate_placement(
            board_id=row.board_id,
            day=row.day,
            start_time=row.start_time,
            end_time=row.end_time,
            room=row.room,
            term_section_id=row.term_section_id,
            exclude_placement_id=row.id,
        )
        for row in rows
    ]
    backstop = verify_persisted_scenario(rows[0].board.scenario_id, context="english_section_edit")
    backstop["involving_this_section"] = clashes_involving(
        backstop, section_id_for(rows[0].term_section)
    )
    return {
        "validation": {
            "valid": all(row["valid"] for row in validations),
            "items": validations,
            "critical_count": sum(row.get("critical_count", 0) for row in validations),
            "warning_count": sum(row.get("warning_count", 0) for row in validations),
            "overlaps": [item for row in validations for item in row.get("overlaps", [])],
            "instructor_clashes": [
                item for row in validations for item in row.get("instructor_clashes", [])
            ],
            "room_clashes": [item for row in validations for item in row.get("room_clashes", [])],
        },
        "validations": validations,
        "instructor_clash_backstop": backstop,
    }


def _switch_policy_response(
    request: HttpRequest, placement: SectionPlacement, payload: dict
) -> JsonResponse:
    try:
        result = switch_policy_section_bundle(
            placement,
            pattern_id=payload.get("pattern_id"),
            day=payload.get("day"),
            start_time=payload.get("start_time"),
            end_time=payload.get("end_time"),
            room=payload.get("room"),
            override=bool(payload.get("override")),
            placements=payload.get("placements"),
        )
    except (SectionPolicyError, ValueError, TypeError, KeyError) as exc:
        return _err(str(exc), code=SectionPolicyError.code, status=400)
    except IntegrityError:
        return _err(
            "Duplicate placement at English pattern destination",
            code="PLACEMENT_DUPLICATE",
            status=409,
        )
    rows = result["placements"]
    teachers = _section_teachers(placement.board.scenario_id)
    log_audit_event(
        request,
        action="tw.placement.switch_pattern",
        status="success",
        details={
            "placement_id": placement.id,
            "placement_ids": [row.id for row in rows],
            "before_pattern_id": result["before_section_policy"]["current_pattern_id"],
            "pattern_id": result["section_policy"]["current_pattern_id"],
        },
    )
    marks = _programme_marks(placement.board.scenario_id)
    return _ok(
        {
            **result,
            **_placement_payloads(result["placement"], rows, teachers, marks),
            **_policy_bundle_feedback(rows),
        }
    )


@login_required(login_url="login")
@require_POST
@guard_manual_scenario_write
@_one_teacher_request
def tw_placement_pattern_switch_view(request: HttpRequest, placement_id: int) -> JsonResponse:
    deny = _require_general_advisor(request)
    if deny:
        return deny
    payload, err = _safe_json(request)
    if err:
        return err
    try:
        placement = SectionPlacement.objects.select_related("board__scenario", "term_section").get(
            pk=placement_id
        )
    except SectionPlacement.DoesNotExist:
        return _err("Placement not found", code="NOT_FOUND", status=404)
    if not payload.get("pattern_id"):
        return _err("pattern_id is required", code=SectionPolicyError.code, status=400)
    # A pattern switch changes times; a section another scenario also places is
    # owned there, exactly as on the move endpoint.
    if placement.term_section_id in external_shared_section_ids(placement.board.scenario_id):
        return _err(
            "This section is also placed by another scenario, which owns its time and room.",
            code="PLACEMENT_EXTERNALLY_OWNED",
            status=409,
        )
    return _switch_policy_response(request, placement, payload)


@login_required(login_url="login")
@require_POST
@guard_manual_scenario_write
@_one_teacher_request
def tw_placement_move_view(request: HttpRequest, placement_id: int) -> JsonResponse:
    """Move an existing placement to a new day/time/room.

    A lock freezes days, start/end times and the contact pattern, not the room:
    changing day/start/end (or ``pattern_id``) on a locked placement requires
    ``override: true``, while a room-only change (same day/start/end) keeps the
    lock and needs none. A room-only change writes the same-scenario copies of
    that physical meeting that share its room, and their canonical meetings
    (``assign_physical_meeting_room``). Sections that
    another scenario also places are fully frozen (409
    ``PLACEMENT_EXTERNALLY_OWNED``), override or not.
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

    new_day = str(payload.get("day", placement.day)).strip().upper()
    new_start = str(payload.get("start_time", placement.start_time)).strip()
    new_end = str(payload.get("end_time", placement.end_time)).strip()
    new_room = str(payload.get("room", placement.room)).strip()
    if OnlineCourseLookup().is_online_course_for_board(
        placement.board, placement.term_section.course_code
    ):
        new_room = ""

    old_day, old_start, old_end = placement.day, placement.start_time, placement.end_time
    old_room = str(placement.room or "").strip()
    time_changed = bool(payload.get("pattern_id")) or (new_day, new_start, new_end) != (
        str(old_day).strip().upper(),
        str(old_start).strip(),
        str(old_end).strip(),
    )
    room_only = not time_changed and new_room != old_room
    scenario_id = placement.board.scenario_id

    # Another scenario also places this physical section and owns its time and
    # room; neither an override nor a room-only edit may change it from here.
    if placement.term_section_id in external_shared_section_ids(scenario_id) and (
        time_changed or room_only
    ):
        return _err(
            "This section is also placed by another scenario, which owns its time and room.",
            code="PLACEMENT_EXTERNALLY_OWNED",
            status=409,
        )

    # A lock freezes days, times and the contact pattern; the room stays assignable.
    if placement.is_locked and time_changed and not payload.get("override"):
        return _err(
            "Placement is locked: its days and times cannot change (its room can). "
            "Pass override: true to force.",
            code="PLACEMENT_LOCKED",
            status=400,
        )

    if section_policy(placement.term_section) and time_changed:
        return _switch_policy_response(request, placement, payload)

    # Validate before persisting. Board-scoped and free-text-instructor keyed —
    # see the note in tw_placement_create_view; the backstop covers what it misses.
    validation = validate_placement(
        board_id=placement.board_id,
        day=new_day,
        start_time=new_start,
        end_time=new_end,
        room=new_room,
        term_section_id=placement.term_section_id,
        exclude_placement_id=placement.id,
    )

    from core.services.timetable_instructor_backstop import (
        clashes_involving,
        section_id_for,
        verify_persisted_scenario,
    )

    if room_only:
        rows = assign_physical_meeting_room(placement, new_room)
    else:
        placement.day = new_day
        placement.start_time = new_start
        placement.end_time = new_end
        placement.room = new_room
        try:
            placement.save(update_fields=["day", "start_time", "end_time", "room", "updated_at"])
        except IntegrityError:
            return _err(
                "Duplicate placement at new position", code="PLACEMENT_DUPLICATE", status=409
            )
        rows = [placement]

    log_audit_event(
        request,
        action="tw.placement.move",
        status="success",
        details={
            "placement_id": placement_id,
            "placement_ids": [row.id for row in rows],
            "from": f"{old_day} {old_start}-{old_end}",
            "to": f"{new_day} {new_start}-{new_end}",
            "room_from": old_room,
            "room_to": new_room,
            "is_locked": placement.is_locked,
        },
    )
    backstop = verify_persisted_scenario(scenario_id, context="manual_placement_move")
    backstop["involving_this_section"] = clashes_involving(
        backstop, section_id_for(placement.term_section)
    )
    teachers = _section_teachers(scenario_id)
    marks = _programme_marks(scenario_id)
    return _ok(
        {
            **_placement_payloads(placement, rows, teachers, marks),
            "validation": validation,
            "instructor_clash_backstop": backstop,
        }
    )


@login_required(login_url="login")
@require_POST
@guard_manual_scenario_write
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
    if section_policy(placement.term_section):
        try:
            removed = remove_policy_section_bundle(
                placement, override=bool(payload.get("override"))
            )
        except SectionPolicyError as exc:
            return _err(str(exc), code=SectionPolicyError.code, status=400)
        log_audit_event(request, action="tw.placement.remove", status="success", details=removed)
        return _ok(removed)
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
@guard_manual_scenario_write
def tw_placement_lock_view(request: HttpRequest, placement_id: int) -> JsonResponse:
    """Toggle the lock flag on a placement.

    Locked placements resist time moves and removal; their room can still change.
    """
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
@require_POST
@guard_manual_scenario_write
def tw_placement_programme_preference_view(request: HttpRequest, placement_id: int) -> JsonResponse:
    """Mark the placement's section "mainly for programme" X, or for any ("").

    POST body ``{"preferred_programme": "AI2" | ""}``. The seating then puts
    X's students in this section first and others in the course's other
    sections, without ever costing a seat. A lock freezes only days and times,
    so a locked section may be marked; a section this scenario does not own (a
    global one, another scenario's) or that another scenario also places is
    marked where it is owned. Saving the same value is a no-op with no audit
    row. Returns ``{term_section_id, preferred_programme, changed}``.
    """
    deny = _require_general_advisor(request)
    if deny:
        return deny
    if not is_section_programme_preference_enabled():
        return _err(
            "Grouping sections by programme is not enabled.", code="FEATURE_DISABLED", status=409
        )
    payload, err = _safe_json(request)
    if err:
        return err
    try:
        placement = SectionPlacement.objects.select_related("board__scenario", "term_section").get(
            pk=placement_id
        )
    except SectionPlacement.DoesNotExist:
        return _err("Placement not found", code="NOT_FOUND", status=404)
    scenario = placement.board.scenario
    if scenario.status == "published":
        return _err("Cannot modify published scenario", code="SCENARIO_PUBLISHED", status=400)
    section = placement.term_section
    if section.scenario_id != scenario.id:
        return _err(
            "This section belongs to another scenario or to the registrar; mark it there.",
            code="SECTION_NOT_SCENARIO_OWNED",
            status=409,
        )
    if section.id in external_shared_section_ids(scenario.id):
        return _err(
            "This section is also placed by another scenario.",
            code="PLACEMENT_EXTERNALLY_OWNED",
            status=409,
        )
    raw = payload.get("preferred_programme", "")
    allowed = allowed_programmes(scenario)
    programme = normalise_programme(raw) if isinstance(raw, str) else None
    if programme is None or (programme and programme not in allowed):
        return _err(
            "Choose one of this scenario's programmes, or Any.",
            code="INVALID_PROGRAMME",
            status=400,
            details={"allowed": allowed},
        )
    with transaction.atomic():
        row = TermSection.objects.select_for_update().get(pk=section.pk)
        before = row.preferred_programme
        if before == programme:
            return _ok(
                {"term_section_id": row.id, "preferred_programme": programme, "changed": False}
            )
        row.preferred_programme = programme
        row.save(update_fields=["preferred_programme"])
    log_audit_event(
        request,
        action="tw.section.programme_preference",
        status="success",
        details={
            "scenario_id": scenario.id,
            "placement_id": placement.id,
            "term_section_id": row.id,
            "course_key": row.course_key,
            "section": row.section,
            "from": before,
            "to": programme,
        },
    )
    return _ok({"term_section_id": row.id, "preferred_programme": programme, "changed": True})


@login_required(login_url="login")
@require_POST
@guard_manual_scenario_write
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
        scenario = TimetableScenario.objects.get(pk=scenario_id)
    except TimetableScenario.DoesNotExist:
        return _err("Scenario not found", code="NOT_FOUND", status=404)

    if scenario.status == "published":
        return _err("Cannot modify published scenario", code="SCENARIO_PUBLISHED", status=400)
    busy = reject_active_planner_job(scenario_id)
    if busy is not None:
        return busy

    payload, err = _safe_json(request)
    if err:
        return err
    try:
        seed = parse_run_seed(payload.get("seed"))
    except ValueError as exc:
        return _err(str(exc), code="VALIDATION_SEED", status=400)

    mode = payload.get("mode", "current")
    seed = resolve_run_seed(seed)

    # WS-E — opt-in async dispatch. When ``async_run`` is set, the slow V2
    # pipeline is queued on the planner job runner (off the request thread, so
    # gunicorn's 120s timeout can't SIGKILL it mid-run) and a 202 + job id is
    # returned for the existing PR8 job-poll UI.
    #
    # The flag check is NOT belt-and-braces. Every /planner-jobs/ endpoint
    # (status, result, cancel) returns 404 when the flag is off, so accepting
    # async_run in that state would hand the client a job_id it can never poll
    # — while the job still runs and still PERSISTS. The client would report a
    # failure for a run that actually mutated the board. Falling through to the
    # sync path instead means no job_id comes back, which is exactly the signal
    # both front-ends already treat as "async unavailable → run synchronously".
    from core.services.planner_job_runner import is_async_planner_enabled

    if payload.get("async_run") and is_async_planner_enabled():
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
            "seed": seed,
            "strategies": payload.get("strategies") or None,
            "max_iterations": payload.get("max_iterations", 50),
            "run_chain_search": payload.get("run_chain_search", True),
            "run_cpsat_polish": payload.get("run_cpsat_polish", True),
            "cpsat_time_limit": payload.get("cpsat_time_limit", 60),
            "max_chain_iterations": payload.get("max_chain_iterations", 10),
        }
        try:
            job_id = submit_planner_job(
                scenario_id=scenario_id, mode=job_mode, user=request.user, params=job_params
            )
        except ScenarioPlannerBusy as exc:
            return scenario_busy_response(exc.job)
        except ScenarioPublished:
            return _err("Cannot modify published scenario", code="SCENARIO_PUBLISHED", status=400)
        dispatch_planner_job(job_id)
        return _ok({"job_id": str(job_id), "mode": mode, "async": True, "seed": seed}, status=202)

    # Synchronous clients still receive the existing result envelope, while
    # using the same durable ownership as background jobs. The async feature
    # flag controls dispatch/polling only, never this scenario write lease.
    from core.models import PlannerJob
    from core.services.planner_job_runner import run_planner_job_synchronously

    try:
        result = run_planner_job_synchronously(
            scenario_id=scenario_id,
            mode=PlannerJob.MODE_OPTIMISE_V2_FULL
            if mode == "full"
            else PlannerJob.MODE_OPTIMISE_V2_CURRENT,
            user=request.user,
            params={
                "seed": seed,
                "strategies": payload.get("strategies") or None,
                "max_iterations": payload.get("max_iterations", 50),
                "run_chain_search": payload.get("run_chain_search", True),
                "run_cpsat_polish": payload.get("run_cpsat_polish", True),
                "cpsat_time_limit": payload.get("cpsat_time_limit", 60),
                "max_chain_iterations": payload.get("max_chain_iterations", 10),
            },
        )
    except ScenarioPlannerBusy as exc:
        return scenario_busy_response(exc.job)
    except ScenarioPublished:
        return _err("Cannot modify published scenario", code="SCENARIO_PUBLISHED", status=400)

    if "error" in result:
        # Internal optimiser exception → 500; no-data (no candidates / no
        # profiles) → 400 — preserving the prior status-code contract.
        is_internal = str(result["error"]).startswith("Optimiser error")
        return _err(
            result["error"],
            details={"seed": result.get("seed", seed)},
            code="OPTIMISER_ERROR" if is_internal else "OPTIMISER_NO_DATA",
            status=500 if is_internal else 400,
        )

    return _ok({"optimisation": result})

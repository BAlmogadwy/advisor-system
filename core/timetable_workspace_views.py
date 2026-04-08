"""
core/timetable_workspace_views.py
Timetable Builder Workspace — page view + API endpoints.

Endpoints:
    GET  timetable_workspace_page       – render the workspace page
    GET  tw_scenarios_list_view         – list scenarios for year/term
    POST tw_scenario_create_view        – create a new scenario
    GET  tw_scenario_detail_view        – get scenario detail
    POST tw_scenario_slots_update_view  – edit scenario slot config
    POST tw_scenario_publish_view       – publish scenario
    GET  tw_boards_list_view            – list boards for scenario
    POST tw_board_create_view           – create a board
    GET  tw_board_detail_view           – board detail + placements
    GET  tw_board_summary_view          – board summary stats
    GET  tw_board_conflicts_view        – full conflict analysis
    GET  tw_board_capacity_view         – demand vs raw capacity
    GET  tw_board_unplaced_view         – unplaced sections for board
    POST tw_placement_create_view       – place section on board
    POST tw_placement_move_view         – move placement
    POST tw_placement_remove_view       – remove placement
    POST tw_placement_lock_view         – toggle lock
    GET  tw_slot_templates_list_view    – list slot templates
    POST tw_slot_template_create_view   – create slot template
"""

from __future__ import annotations

import json
import logging
import re

from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.http import FileResponse, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from core.models import (
    BoardSectionVisibility,
    DeliveryBoard,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TimeSlotTemplate,
    TimetableScenario,
)
from core.services.audit import log_audit_event
from core.services.rbac import ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN, get_user_role
from core.services.timetable_demand import compute_board_capacity
from core.services.timetable_generate import generate_workspace_scenario
from core.services.timetable_workspace import (
    check_publish_readiness,
    compute_affected_students,
    compute_board_summary,
    compute_scenario_budget,
    detect_board_conflicts,
    detect_cross_board_conflicts,
    get_scenario_boards_summary,
    validate_placement,
)
from core.settings_views import load_defaults
from core.sidebar_context import get_sidebar_context

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────


def _ok(data: dict[str, object], status: int = 200) -> JsonResponse:
    return JsonResponse({"ok": True, **data}, status=status)


def _err(
    message: str, *, code: str, status: int = 400, details: dict[str, object] | None = None
) -> JsonResponse:
    payload: dict[str, object] = {"ok": False, "error": {"code": code, "message": message}}
    if details:
        payload["error"]["details"] = details  # type: ignore[index]
    return JsonResponse(payload, status=status)


def _require_general_advisor(request: HttpRequest) -> JsonResponse | None:
    role = get_user_role(request.user)
    if role not in {ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN}:
        return _err("General Advisor access required", code="ROLE_REQUIRED", status=403)
    return None


def _safe_json(request: HttpRequest) -> tuple[dict[str, object], JsonResponse | None]:
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
    if not re.fullmatch(r"\d{4}", year):
        return _err("academic_year must be 4 digits", code="VALIDATION_YEAR", status=400)
    if term not in {"1", "2", "3"}:
        return _err("term must be 1, 2, or 3", code="VALIDATION_TERM", status=400)
    return None


def _scenario_to_dict(s: TimetableScenario) -> dict[str, object]:
    return {
        "id": s.id,
        "academic_year": s.academic_year,
        "term": s.term,
        "name": s.name,
        "status": s.status,
        "slot_config": s.slot_config,
        "created_by": s.created_by,
        "created_at": s.created_at.isoformat() if s.created_at else "",
        "updated_at": s.updated_at.isoformat() if s.updated_at else "",
        "notes": s.notes,
    }


def _board_to_dict(b: DeliveryBoard) -> dict[str, object]:
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
    }


# ── Page View ────────────────────────────────────────────────────


@login_required(login_url="login")
@require_GET
def timetable_workspace_page(request: HttpRequest) -> HttpResponse:
    deny = _require_general_advisor(request)
    if deny:
        return deny
    defaults = load_defaults()
    context = {
        **get_sidebar_context(request),
        "default_year": defaults.get("academic_year", ""),
        "default_term": defaults.get("term", ""),
    }
    return render(request, "core/timetable_workspace.html", context)


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
    # Accept comma-separated programs: "AI,DS" → ["AI", "DS"]
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
    """Section budget consumption for a scenario."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        TimetableScenario.objects.get(id=scenario_id)
    except TimetableScenario.DoesNotExist:
        return _err("Scenario not found", code="NOT_FOUND", status=404)

    budget = compute_scenario_budget(scenario_id)
    return _ok({"budget": budget})


# ── Scenario Endpoints ───────────────────────────────────────────


@login_required(login_url="login")
@require_GET
def tw_scenarios_list_view(request: HttpRequest) -> JsonResponse:
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

    scenario.slot_config = slots
    scenario.save(update_fields=["slot_config", "updated_at"])

    log_audit_event(
        request,
        action="tw.scenario.slots_update",
        status="success",
        details={"scenario_id": scenario_id, "slot_count": len(slots)},
    )
    return _ok({"scenario": _scenario_to_dict(scenario)})


@login_required(login_url="login")
@require_POST
def tw_scenario_publish_view(request: HttpRequest, scenario_id: int) -> JsonResponse:
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
    response = FileResponse(open(path, "rb"), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# ── Board Endpoints ──────────────────────────────────────────────


@login_required(login_url="login")
@require_GET
def tw_boards_list_view(request: HttpRequest) -> JsonResponse:
    deny = _require_general_advisor(request)
    if deny:
        return deny
    scenario_id = request.GET.get("scenario_id")
    if not scenario_id:
        return _err("scenario_id is required", code="VALIDATION_SCENARIO", status=400)
    boards_summary = get_scenario_boards_summary(int(scenario_id))
    return _ok({"boards": boards_summary})


@login_required(login_url="login")
@require_POST
def tw_board_create_view(request: HttpRequest) -> JsonResponse:
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
    return _ok({
        "board": _board_to_dict(board),
        "slot_config": board.scenario.slot_config,
        "placements": [_placement_to_dict(p) for p in placements],
        "primary_student_count": primary_count,
        "visitor_student_count": visitor_count,
    })


@login_required(login_url="login")
@require_GET
def tw_board_summary_view(request: HttpRequest, board_id: int) -> JsonResponse:
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
            c for c in cross_board
            if c["board_a_id"] == board_id or c["board_b_id"] == board_id
        ]
    except DeliveryBoard.DoesNotExist:
        cross_board = []

    return _ok({
        "board_id": board_id,
        **conflicts,
        "student_impact": student_impact,
        "cross_board_conflicts": cross_board,
    })


@login_required(login_url="login")
@require_GET
def tw_board_capacity_view(request: HttpRequest, board_id: int) -> JsonResponse:
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
    return _ok({
        "board_id": board_id,
        "courses": courses,
        "totals": {
            "demand": total_demand,
            "raw_capacity": total_raw,
            "deficit": total_deficit,
            "course_count": len(courses),
        },
    })


@login_required(login_url="login")
@require_GET
def tw_board_unplaced_view(request: HttpRequest, board_id: int) -> JsonResponse:
    deny = _require_general_advisor(request)
    if deny:
        return deny
    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return _err("Board not found", code="NOT_FOUND", status=404)

    scenario = board.scenario
    # Get all term sections (all available sections for this semester)
    all_sections = TermSection.objects.all()

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
        if search and search not in (ts.course_code or "").upper() and search not in (
            ts.course_name or ""
        ).upper() and search not in (ts.section or "").upper():
            continue

        meetings = list(
            TermSectionMeeting.objects.filter(term_section=ts).values(
                "day", "start_time", "end_time", "room", "instructor"
            )
        )
        unplaced.append({
            "term_section_id": ts.id,
            "course_code": ts.course_code,
            "course_name": ts.course_name,
            "course_key": ts.course_key,
            "section": ts.section,
            "available_capacity": ts.available_capacity,
            "registered_count": ts.registered_count,
            "meetings": meetings,
        })

    return _ok({"unplaced": unplaced, "count": len(unplaced)})


# ── Placement Endpoints ──────────────────────────────────────────


@login_required(login_url="login")
@require_POST
def tw_placement_create_planned_view(request: HttpRequest) -> JsonResponse:
    """Create a placement from generated plan data.

    Auto-creates a TermSection if one doesn't exist for the course/section combo,
    then places it on the board.
    """
    deny = _require_general_advisor(request)
    if deny:
        return deny
    payload, err = _safe_json(request)
    if err:
        return err

    board_id = payload.get("board_id")
    course_code = str(payload.get("course_code", "")).strip().upper()
    section_label = str(payload.get("section_label", "")).strip().upper()
    day = str(payload.get("day", "")).strip().upper()
    start_time = str(payload.get("start_time", "")).strip()
    end_time = str(payload.get("end_time", "")).strip()
    capacity = int(payload.get("capacity", 40))

    if not all([board_id, course_code, section_label, day, start_time, end_time]):
        return _err(
            "board_id, course_code, section_label, day, start_time, end_time are required",
            code="VALIDATION_PLACEMENT",
            status=400,
        )

    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=int(board_id))  # type: ignore[arg-type]
    except DeliveryBoard.DoesNotExist:
        return _err("Board not found", code="NOT_FOUND", status=404)

    if board.scenario.status == "published":
        return _err("Cannot modify published scenario", code="SCENARIO_PUBLISHED", status=400)

    # Find or create TermSection
    course_key = course_code
    ts, _created = TermSection.objects.get_or_create(
        course_key=course_key,
        section=section_label,
        defaults={
            "course_code": course_code,
            "course_number": course_code,
            "course_name": course_code,
            "available_capacity": capacity,
            "source_tag": "tw_planned",
        },
    )

    # Also create the meeting record
    TermSectionMeeting.objects.get_or_create(
        term_section=ts,
        day=day,
        start_time=start_time,
        end_time=end_time,
        defaults={"room": "", "instructor": ""},
    )

    # Validate
    validation = validate_placement(
        board_id=board.id,
        day=day,
        start_time=start_time,
        end_time=end_time,
        room="",
        term_section_id=ts.id,
    )

    try:
        placement = SectionPlacement.objects.create(
            board=board,
            term_section=ts,
            day=day,
            start_time=start_time,
            end_time=end_time,
        )
    except IntegrityError:
        return _err(
            "Duplicate placement for this section/day/time",
            code="PLACEMENT_DUPLICATE",
            status=409,
        )

    log_audit_event(
        request,
        action="tw.placement.create_planned",
        status="success",
        details={
            "placement_id": placement.id,
            "board_id": board.id,
            "course_code": course_code,
            "section_label": section_label,
        },
    )
    return _ok({"placement": _placement_to_dict(placement), "validation": validation}, status=201)


@login_required(login_url="login")
@require_POST
def tw_placement_create_view(request: HttpRequest) -> JsonResponse:
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
    return _ok({
        "placement": _placement_to_dict(placement),
        "validation": validation,
    }, status=201)


@login_required(login_url="login")
@require_POST
def tw_placement_move_view(request: HttpRequest, placement_id: int) -> JsonResponse:
    deny = _require_general_advisor(request)
    if deny:
        return deny
    payload, err = _safe_json(request)
    if err:
        return err

    try:
        placement = SectionPlacement.objects.select_related(
            "board__scenario", "term_section"
        ).get(id=placement_id)
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
    deny = _require_general_advisor(request)
    if deny:
        return deny
    payload, err = _safe_json(request)
    if err:
        return err

    try:
        placement = SectionPlacement.objects.select_related(
            "board__scenario", "term_section"
        ).get(id=placement_id)
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


# ── Slot Template Endpoints ──────────────────────────────────────


@login_required(login_url="login")
@require_GET
def tw_slot_templates_list_view(request: HttpRequest) -> JsonResponse:
    deny = _require_general_advisor(request)
    if deny:
        return deny
    templates = TimeSlotTemplate.objects.all().order_by("-created_at")
    return _ok({
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
    })


@login_required(login_url="login")
@require_POST
def tw_slot_template_create_view(request: HttpRequest) -> JsonResponse:
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

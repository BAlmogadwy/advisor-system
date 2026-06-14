"""Instructor management — page shell, roster CRUD, advisor-seed, course-level
assignment (program + section M/F → course → instructor), and a load report.

Assignment is scenario-INDEPENDENT: a ``CourseInstructor`` row ties an instructor
to ``(program, course_code, section M/F)``. RBAC mirrors section planning
(``ROLE_GENERAL_ADVISOR`` admits Super Admin); all writes are audited.
"""

from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from core.authz import role_required
from core.models import CourseInstructor, Instructor, ProgrammeRequirement
from core.services.audit import log_audit_event
from core.services.course_instructor_assignment import set_course_instructors
from core.services.rbac import ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN, get_user_role
from core.services.timetable_online import normalise_course_code
from core.services.timetable_pr4_instructor import normalise_instructor
from core.settings_views import load_defaults
from core.sidebar_context import get_sidebar_context

# The 12 real programmes (DBGPROG is a debug fixture, excluded from the picker).
KNOWN_PROGRAMS = ("AI", "AI2", "COE", "COE2", "CS", "CS2", "CYP", "CYP2", "DS", "DS2", "IS", "IS2")
VALID_SECTIONS = ("M", "F")


def _ok(data: dict[str, object], status: int = 200) -> JsonResponse:
    return JsonResponse({"ok": True, **data}, status=status)


def _err(message: str, *, code: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"ok": False, "error": {"code": code, "message": message}}, status=status)


def _require_general_advisor(request: HttpRequest) -> JsonResponse | None:
    if get_user_role(request.user) not in {ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN}:
        return _err("General Advisor access required", code="ROLE_REQUIRED", status=403)
    return None


def _safe_json(request: HttpRequest) -> tuple[dict[str, object], JsonResponse | None]:
    if not request.body:
        return {}, None
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return {}, _err("Invalid JSON payload", code="INVALID_JSON", status=400)
    if not isinstance(payload, dict):
        return {}, _err("Payload must be an object", code="INVALID_JSON", status=400)
    return payload, None


def _instructor_to_dict(i: Instructor) -> dict[str, object]:
    return {
        "id": i.pk,
        "full_name": i.full_name,
        "full_name_ar": i.full_name_ar,
        "email": i.email,
        "employee_no": i.employee_no,
        "department": i.department,
        "max_weekly_hours": i.max_weekly_hours,
        "is_active": i.is_active,
    }


def _parse_int_or_none(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# ── Page ─────────────────────────────────────────────────────────


@login_required(login_url="login")
@require_GET
def instructor_management_page(request: HttpRequest) -> HttpResponse:
    """Render the instructor management page (Assignments / Roster / Load Report)."""
    deny = _require_general_advisor(request)
    if deny:
        return deny
    defaults = load_defaults()
    ctx = {
        **get_sidebar_context(request),
        "default_year": defaults["academic_year"],
        "default_term": defaults["term"],
    }
    return render(request, "core/instructor_management.html", ctx)


# ── Roster CRUD ──────────────────────────────────────────────────


@role_required(ROLE_GENERAL_ADVISOR)
@require_GET
def instructors_list_view(request: HttpRequest) -> JsonResponse:
    """Roster + typeahead. ``?q=`` substring filter, ``?include_inactive=1``."""
    qs = Instructor.objects.all()
    if request.GET.get("include_inactive") not in ("1", "true", "yes"):
        qs = qs.filter(is_active=True)
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(
            Q(full_name__icontains=q) | Q(full_name_ar__icontains=q) | Q(email__icontains=q)
        )
    qs = qs.order_by("full_name")[:500]
    return _ok({"instructors": [_instructor_to_dict(i) for i in qs]})


@role_required(ROLE_GENERAL_ADVISOR)
@require_POST
def instructors_create_view(request: HttpRequest) -> JsonResponse:
    payload, err = _safe_json(request)
    if err:
        return err
    full_name = str(payload.get("full_name", "")).strip()
    norm = normalise_instructor(full_name)
    if not norm:
        return _err("full_name is required", code="VALIDATION", status=400)
    try:
        with transaction.atomic():
            instructor = Instructor.objects.create(
                full_name=full_name,
                normalised_name=norm,
                full_name_ar=str(payload.get("full_name_ar", "")).strip(),
                email=str(payload.get("email", "")).strip(),
                employee_no=str(payload.get("employee_no", "")).strip(),
                department=str(payload.get("department", "")).strip(),
                max_weekly_hours=_parse_int_or_none(payload.get("max_weekly_hours")),
            )
    except IntegrityError:
        return _err(
            "An instructor with this name or email already exists", code="DUPLICATE", status=409
        )
    log_audit_event(
        request, action="instructor_create", status="success", details={"id": instructor.pk}
    )
    return _ok({"instructor": _instructor_to_dict(instructor)}, status=201)


@role_required(ROLE_GENERAL_ADVISOR)
@require_POST
def instructors_update_view(request: HttpRequest) -> JsonResponse:
    payload, err = _safe_json(request)
    if err:
        return err
    instructor = Instructor.objects.filter(pk=payload.get("id")).first()
    if instructor is None:
        return _err("Instructor not found", code="NOT_FOUND", status=404)
    fields: list[str] = []
    if "full_name" in payload:
        full_name = str(payload.get("full_name", "")).strip()
        norm = normalise_instructor(full_name)
        if not norm:
            return _err("full_name cannot be empty", code="VALIDATION", status=400)
        instructor.full_name = full_name
        instructor.normalised_name = norm
        fields += ["full_name", "normalised_name"]
    for key in ("full_name_ar", "email", "employee_no", "department"):
        if key in payload:
            setattr(instructor, key, str(payload.get(key, "")).strip())
            fields.append(key)
    if "max_weekly_hours" in payload:
        instructor.max_weekly_hours = _parse_int_or_none(payload.get("max_weekly_hours"))
        fields.append("max_weekly_hours")
    if not fields:
        return _ok({"instructor": _instructor_to_dict(instructor)})
    try:
        with transaction.atomic():
            instructor.save(update_fields=[*fields, "updated_at"])
    except IntegrityError:
        return _err(
            "Another instructor already has this name or email", code="DUPLICATE", status=409
        )
    log_audit_event(
        request, action="instructor_update", status="success", details={"id": instructor.pk}
    )
    return _ok({"instructor": _instructor_to_dict(instructor)})


@role_required(ROLE_GENERAL_ADVISOR)
@require_POST
def instructors_set_active_view(request: HttpRequest) -> JsonResponse:
    payload, err = _safe_json(request)
    if err:
        return err
    instructor = Instructor.objects.filter(pk=payload.get("id")).first()
    if instructor is None:
        return _err("Instructor not found", code="NOT_FOUND", status=404)
    instructor.is_active = bool(payload.get("is_active", True))
    instructor.save(update_fields=["is_active", "updated_at"])
    log_audit_event(
        request,
        action="instructor_set_active",
        status="success",
        details={"id": instructor.pk, "is_active": instructor.is_active},
    )
    return _ok({"instructor": _instructor_to_dict(instructor)})


@role_required(ROLE_GENERAL_ADVISOR)
@require_GET
def instructor_advisors_view(request: HttpRequest) -> JsonResponse:
    """Existing ``AcademicAdvisor`` people, to seed a new instructor from."""
    from core.models import AcademicAdvisor

    qs = AcademicAdvisor.objects.all()
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(full_name__icontains=q) | Q(email__icontains=q))
    advisors = list(qs.order_by("full_name")[:500])
    existing = set(Instructor.objects.values_list("normalised_name", flat=True))
    return _ok(
        {
            "advisors": [
                {
                    "advisor_id": a.advisor_id,
                    "full_name": a.full_name,
                    "email": a.email,
                    "department": a.department,
                    "already_instructor": (normalise_instructor(a.full_name) or "") in existing,
                }
                for a in advisors
            ]
        }
    )


# ── Course assignment ────────────────────────────────────────────


def _validate_program_section(program: str, section: str) -> JsonResponse | None:
    if program not in KNOWN_PROGRAMS:
        return _err("Unknown program", code="VALIDATION", status=400)
    if section not in VALID_SECTIONS:
        return _err("section must be M or F", code="VALIDATION", status=400)
    return None


@role_required(ROLE_GENERAL_ADVISOR)
@require_GET
def course_assignments_view(request: HttpRequest) -> JsonResponse:
    """A program's courses (for one M/F section) with their assigned instructor.

    Every ``ProgrammeRequirement`` course for the program is returned — unassigned
    ones carry ``instructor: null`` — so the UI can show the full course list and
    the assignment state in one table.
    """
    program = (request.GET.get("program") or "").strip()
    section = (request.GET.get("section") or "").strip().upper()
    bad = _validate_program_section(program, section)
    if bad:
        return bad

    # course list for the program (dedupe by normalised code, keep first metadata)
    courses: dict[str, dict[str, object]] = {}
    for cc, name, term, cr, online in (
        ProgrammeRequirement.objects.filter(program=program)
        .order_by("programme_term", "course_code")
        .values_list("course_code", "course_name", "programme_term", "credit_hours", "is_online")
    ):
        key = normalise_course_code(cc)
        if key and key not in courses:
            courses[key] = {
                "course_code": cc,
                "course_name": name or "",
                "programme_term": term,
                "credit_hours": cr,
                "is_online": online,
                "instructor": None,
                "co_instructors": [],
            }

    # overlay current assignments
    for link in (
        CourseInstructor.objects.filter(program=program, section=section)
        .select_related("instructor")
        .order_by("id")
    ):
        row = courses.get(normalise_course_code(link.course_code))
        if row is None:
            continue
        entry = {
            "id": link.instructor_id,
            "full_name": link.instructor.full_name,
            "full_name_ar": link.instructor.full_name_ar,
            "role": link.role,
        }
        if link.role == "primary":
            row["instructor"] = entry
        else:
            row["co_instructors"].append(entry)

    return _ok({"program": program, "section": section, "courses": list(courses.values())})


@role_required(ROLE_GENERAL_ADVISOR)
@require_POST
def course_assignment_set_view(request: HttpRequest) -> JsonResponse:
    payload, err = _safe_json(request)
    if err:
        return err
    program = str(payload.get("program", "")).strip()
    section = str(payload.get("section", "")).strip().upper()
    course_code = str(payload.get("course_code", "")).strip()
    bad = _validate_program_section(program, section)
    if bad:
        return bad
    if not course_code:
        return _err("course_code is required", code="VALIDATION", status=400)
    ids = payload.get("instructor_ids")
    if ids is None and payload.get("instructor_id") is not None:
        ids = [payload.get("instructor_id")]
    if not isinstance(ids, list):
        return _err("instructor_ids must be a list", code="VALIDATION", status=400)
    try:
        instructors = set_course_instructors(program, course_code, section, ids)
    except ValueError as exc:
        return _err(str(exc), code="NOT_FOUND", status=404)
    log_audit_event(
        request,
        action="course_instructor_set",
        status="success",
        details={"program": program, "course_code": course_code, "section": section},
    )
    return _ok(
        {
            "program": program,
            "section": section,
            "course_code": course_code,
            "instructors": instructors,
        }
    )


@role_required(ROLE_GENERAL_ADVISOR)
@require_POST
def course_assignment_clear_view(request: HttpRequest) -> JsonResponse:
    payload, err = _safe_json(request)
    if err:
        return err
    program = str(payload.get("program", "")).strip()
    section = str(payload.get("section", "")).strip().upper()
    course_code = str(payload.get("course_code", "")).strip()
    bad = _validate_program_section(program, section)
    if bad:
        return bad
    set_course_instructors(program, course_code, section, [])
    log_audit_event(
        request,
        action="course_instructor_clear",
        status="success",
        details={"program": program, "course_code": course_code, "section": section},
    )
    return _ok({"program": program, "section": section, "course_code": course_code})


@role_required(ROLE_GENERAL_ADVISOR)
@require_POST
def course_assignment_bulk_view(request: HttpRequest) -> JsonResponse:
    """Assign one instructor as primary to many courses in (program, section)."""
    payload, err = _safe_json(request)
    if err:
        return err
    program = str(payload.get("program", "")).strip()
    section = str(payload.get("section", "")).strip().upper()
    bad = _validate_program_section(program, section)
    if bad:
        return bad
    instructor_id = payload.get("instructor_id")
    if not Instructor.objects.filter(pk=instructor_id).exists():
        return _err("Instructor not found", code="NOT_FOUND", status=404)
    course_codes = payload.get("course_codes") or []
    if not isinstance(course_codes, list):
        return _err("course_codes must be a list", code="VALIDATION", status=400)
    updated = 0
    for cc in course_codes:
        try:
            set_course_instructors(program, str(cc), section, [instructor_id])
            updated += 1
        except ValueError:
            continue
    log_audit_event(
        request,
        action="course_instructor_bulk",
        status="success",
        details={"program": program, "section": section, "updated": updated},
    )
    return _ok({"program": program, "section": section, "updated": updated})


@role_required(ROLE_GENERAL_ADVISOR)
@require_POST
def course_assignment_reconcile_view(request: HttpRequest) -> JsonResponse:
    """Re-fan current course-assignment names into an existing scenario's
    timetable (so edits reach an already-built scenario without a rebuild)."""
    from core.models import TimetableScenario
    from core.services.course_instructor_assignment import reconcile_scenario_instructors

    payload, err = _safe_json(request)
    if err:
        return err
    scenario = TimetableScenario.objects.filter(pk=payload.get("scenario_id")).first()
    if scenario is None:
        return _err("Scenario not found", code="NOT_FOUND", status=404)
    updated = reconcile_scenario_instructors(scenario)
    log_audit_event(
        request,
        action="course_instructor_reconcile",
        status="success",
        details={"scenario_id": scenario.id, "sections_updated": updated},
    )
    return _ok({"scenario_id": scenario.id, "sections_updated": updated})


# ── Load report ──────────────────────────────────────────────────


@role_required(ROLE_GENERAL_ADVISOR)
@require_GET
def instructor_load_report_view(request: HttpRequest) -> JsonResponse:
    """Per-instructor teaching-load report over course assignments.

    One row per instructor with at least one ``CourseInstructor`` assignment:
    courses taught (count + list), distinct courses, total credit hours (summed
    from ``ProgrammeRequirement``), programs, and a load-status vs
    ``max_weekly_hours``. Optional ``?program=``/``?section=`` filters.
    """
    program = (request.GET.get("program") or "").strip()
    section = (request.GET.get("section") or "").strip().upper()

    links = CourseInstructor.objects.select_related("instructor")
    if program:
        links = links.filter(program=program)
    if section in VALID_SECTIONS:
        links = links.filter(section=section)

    # credit-hours lookup keyed by (program, normalised course_code)
    credit: dict[tuple[str, str], int] = {}
    for prog, cc, cr in ProgrammeRequirement.objects.exclude(credit_hours__isnull=True).values_list(
        "program", "course_code", "credit_hours"
    ):
        credit[(prog, normalise_course_code(cc))] = cr

    per: dict[int, dict] = {}
    for link in links.order_by("instructor__full_name"):
        instr = link.instructor
        acc = per.setdefault(
            instr.pk,
            {
                "instructor": instr,
                "courses": [],
                "course_keys": set(),
                "programs": set(),
                "credit": 0,
            },
        )
        acc["courses"].append(
            {
                "program": link.program,
                "course_code": link.course_code,
                "section": link.section,
                "role": link.role,
            }
        )
        acc["course_keys"].add(normalise_course_code(link.course_code))
        acc["programs"].add(link.program)
        acc["credit"] += credit.get((link.program, normalise_course_code(link.course_code)), 0)

    rows: list[dict[str, object]] = []
    tot_courses = tot_credit = 0
    for acc in per.values():
        instr = acc["instructor"]
        max_h = instr.max_weekly_hours
        load = acc["credit"]
        if max_h is None:
            load_status = "na"
        elif load > max_h:
            load_status = "over"
        elif load == max_h:
            load_status = "at"
        else:
            load_status = "under"
        rows.append(
            {
                "instructor_id": instr.pk,
                "full_name": instr.full_name,
                "full_name_ar": instr.full_name_ar,
                "department": instr.department,
                "course_count": len(acc["courses"]),
                "courses": acc["courses"],
                "distinct_courses": len(acc["course_keys"]),
                "programs": sorted(acc["programs"]),
                "total_credit_hours": acc["credit"],
                "max_weekly_hours": max_h,
                "load_status": load_status,
            }
        )
        tot_courses += len(acc["courses"])
        tot_credit += acc["credit"]

    return _ok(
        {
            "rows": rows,
            "totals": {
                "instructors": len(rows),
                "course_count": tot_courses,
                "total_credit_hours": tot_credit,
            },
        }
    )

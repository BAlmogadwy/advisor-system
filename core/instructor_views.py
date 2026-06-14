"""Instructor management — page shell + roster/assignment ops endpoints.

RBAC mirrors section planning (``ROLE_GENERAL_ADVISOR`` admits Super Admin too).
All writes are audited and blocked on published scenarios. The single write path
for section ↔ instructor links is ``core.services.instructor_assignment``.
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
from core.models import Instructor, SectionInstructor, TermSection
from core.services.audit import log_audit_event
from core.services.instructor_assignment import (
    serialize_section_instructors,
    set_section_instructors,
)
from core.services.rbac import ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN, get_user_role
from core.services.timetable_pr4_instructor import normalise_instructor
from core.settings_views import load_defaults
from core.sidebar_context import get_sidebar_context


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


# ── Page ─────────────────────────────────────────────────────────


@login_required(login_url="login")
@require_GET
def instructor_management_page(request: HttpRequest) -> HttpResponse:
    """Render the instructor management SPA shell (roster + load report tabs)."""
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
@require_GET
def instructor_advisors_view(request: HttpRequest) -> JsonResponse:
    """Existing ``AcademicAdvisor`` people, to seed a new instructor from.

    Teaching staff overlap heavily with advisors, so the create form lets the
    registrar pick an advisor and pre-fill name/email/department instead of
    retyping. ``?q=`` filters by name/email. Advisors already promoted to an
    ``Instructor`` (matched on normalised name) are flagged ``already_instructor``.
    """
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
    # Rename propagates to the meeting display cache of every linked section.
    if "full_name" in payload:
        _propagate_rename(instructor)
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


# ── Assignment ───────────────────────────────────────────────────


@role_required(ROLE_GENERAL_ADVISOR)
@require_GET
def instructor_sections_view(request: HttpRequest) -> JsonResponse:
    """Assignable sections in a scenario with their current instructors.

    Powers the management page's assignment grid. ``?scenario_id=`` required,
    ``?q=`` filters by course code/name/section.
    """
    scenario_id = request.GET.get("scenario_id")
    if not scenario_id:
        return _err("scenario_id is required", code="VALIDATION", status=400)
    sections = TermSection.objects.filter(scenario_id=scenario_id)
    q = (request.GET.get("q") or "").strip()
    if q:
        sections = sections.filter(
            Q(course_code__icontains=q) | Q(course_name__icontains=q) | Q(section__icontains=q)
        )
    sections = sections.order_by("course_code", "section")[:1000]
    return _ok(
        {
            "scenario_id": int(scenario_id),
            "sections": [
                {
                    "term_section_id": s.id,
                    "course_code": s.course_code,
                    "course_name": s.course_name,
                    "section": s.section,
                    "instructors": serialize_section_instructors(s),
                }
                for s in sections
            ],
        }
    )


def _section_or_published_guard(
    term_section_id: object,
) -> tuple[TermSection | None, JsonResponse | None]:
    section = TermSection.objects.select_related("scenario").filter(pk=term_section_id).first()
    if section is None:
        return None, _err("Section not found", code="NOT_FOUND", status=404)
    if section.scenario_id and section.scenario.status == "published":
        return None, _err(
            "Cannot modify a published scenario", code="SCENARIO_PUBLISHED", status=400
        )
    return section, None


def _current_ids(term_section: TermSection) -> list[int]:
    return list(
        SectionInstructor.objects.filter(term_section=term_section)
        .order_by("id")
        .values_list("instructor_id", flat=True)
    )


@role_required(ROLE_GENERAL_ADVISOR)
@require_POST
def instructors_assign_view(request: HttpRequest) -> JsonResponse:
    """Add one instructor to one section (append to its set)."""
    payload, err = _safe_json(request)
    if err:
        return err
    section, guard = _section_or_published_guard(payload.get("term_section_id"))
    if guard:
        return guard
    assert section is not None
    instructor_id = payload.get("instructor_id")
    if not Instructor.objects.filter(pk=instructor_id).exists():
        return _err("Instructor not found", code="NOT_FOUND", status=404)
    ids = _current_ids(section)
    if instructor_id not in ids:
        ids.append(int(instructor_id))
    try:
        instructors = set_section_instructors(section, instructor_ids=ids)
    except ValueError as exc:
        return _err(str(exc), code="NOT_FOUND", status=404)
    log_audit_event(
        request,
        action="instructor_assign",
        status="success",
        details={"term_section_id": section.id, "instructor_id": instructor_id},
    )
    return _ok({"term_section_id": section.id, "instructors": instructors})


@role_required(ROLE_GENERAL_ADVISOR)
@require_POST
def instructors_unassign_view(request: HttpRequest) -> JsonResponse:
    """Remove one instructor from one section."""
    payload, err = _safe_json(request)
    if err:
        return err
    section, guard = _section_or_published_guard(payload.get("term_section_id"))
    if guard:
        return guard
    assert section is not None
    instructor_id = payload.get("instructor_id")
    ids = [i for i in _current_ids(section) if i != instructor_id]
    instructors = set_section_instructors(section, instructor_ids=ids)
    log_audit_event(
        request,
        action="instructor_unassign",
        status="success",
        details={"term_section_id": section.id, "instructor_id": instructor_id},
    )
    return _ok({"term_section_id": section.id, "instructors": instructors})


@role_required(ROLE_GENERAL_ADVISOR)
@require_POST
def instructors_assign_bulk_view(request: HttpRequest) -> JsonResponse:
    """Add one instructor to many sections. Published sections are skipped."""
    payload, err = _safe_json(request)
    if err:
        return err
    instructor_id = payload.get("instructor_id")
    if not Instructor.objects.filter(pk=instructor_id).exists():
        return _err("Instructor not found", code="NOT_FOUND", status=404)
    section_ids = payload.get("term_section_ids") or []
    if not isinstance(section_ids, list):
        return _err("term_section_ids must be a list", code="VALIDATION", status=400)
    assigned, skipped = 0, 0
    for sid in section_ids:
        section, guard = _section_or_published_guard(sid)
        if guard:
            skipped += 1
            continue
        assert section is not None
        ids = _current_ids(section)
        if instructor_id not in ids:
            ids.append(int(instructor_id))
        set_section_instructors(section, instructor_ids=ids)
        assigned += 1
    log_audit_event(
        request,
        action="instructor_assign_bulk",
        status="success",
        details={"instructor_id": instructor_id, "assigned": assigned, "skipped": skipped},
    )
    return _ok({"assigned": assigned, "skipped": skipped})


# ── Load report ──────────────────────────────────────────────────


def _parse_hhmm(value: object) -> int | None:
    try:
        hh, mm = str(value).split(":", 1)
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        return None


@role_required(ROLE_GENERAL_ADVISOR)
@require_GET
def instructor_load_report_view(request: HttpRequest) -> JsonResponse:
    """Per-instructor teaching-load report for one scenario.

    One row per instructor with at least one assignment in the scenario:
    sections taught, distinct courses, total credit hours, weekly contact
    hours (summed meeting durations), teaching days, time clashes (a day/start
    occupied by two different of the instructor's sections), and a load-status
    pill vs ``max_weekly_hours``. Aggregated in a handful of bulk queries.
    """
    scenario_id = request.GET.get("scenario_id")
    if not scenario_id:
        return _err("scenario_id is required", code="VALIDATION", status=400)

    from core.models import ProgrammeRequirement, ScenarioSectionBudget, TermSectionMeeting

    # credit-hours lookup: scenario budget (by key then code) → programme req (by code)
    credit_by_key: dict[str, int] = {}
    credit_by_code: dict[str, int] = {}
    for ck, cc, ch in ScenarioSectionBudget.objects.filter(scenario_id=scenario_id).values_list(
        "course_key", "course_code", "credit_hours"
    ):
        if ck:
            credit_by_key[ck] = ch
        if cc and cc not in credit_by_code:
            credit_by_code[cc] = ch
    prog_credit: dict[str, int] = {}
    for cc, ch in ProgrammeRequirement.objects.exclude(credit_hours__isnull=True).values_list(
        "course_code", "credit_hours"
    ):
        prog_credit.setdefault(cc, ch)

    def _credits(course_key: str, course_code: str) -> int:
        if course_key in credit_by_key:
            return credit_by_key[course_key]
        if course_code in credit_by_code:
            return credit_by_code[course_code]
        return prog_credit.get(course_code, 0)

    # section meetings (day, start_minute, duration) grouped by section
    meetings_by_section: dict[int, list[tuple[str, int, int]]] = {}
    unparseable = 0
    for tsid, day, st, en in TermSectionMeeting.objects.filter(
        term_section__scenario_id=scenario_id
    ).values_list("term_section_id", "day", "start_time", "end_time"):
        sm, em = _parse_hhmm(st), _parse_hhmm(en)
        if sm is None or em is None:
            unparseable += 1
            continue
        meetings_by_section.setdefault(tsid, []).append(((day or "").upper(), sm, max(0, em - sm)))

    # links: instructor → their sections in this scenario
    links = (
        SectionInstructor.objects.filter(term_section__scenario_id=scenario_id)
        .select_related("instructor", "term_section")
        .order_by("instructor__full_name")
    )
    per: dict[int, dict] = {}
    for link in links:
        instr = link.instructor
        ts = link.term_section
        acc = per.setdefault(
            instr.pk,
            {
                "instructor": instr,
                "sections": [],  # {course_code, section}
                "courses": set(),
                "credit": 0,
                "contact_min": 0,
                "days": set(),
                "slots": [],  # (day, start_min)
            },
        )
        acc["sections"].append({"course_code": ts.course_code, "section": ts.section})
        acc["courses"].add(ts.course_code)
        acc["credit"] += _credits(ts.course_key or "", ts.course_code)
        for day, sm, dur in meetings_by_section.get(ts.id, []):
            acc["contact_min"] += dur
            acc["days"].add(day)
            acc["slots"].append((day, sm))

    rows: list[dict[str, object]] = []
    tot_sections = tot_credits = tot_contact_min = 0
    for acc in per.values():
        instr = acc["instructor"]
        # a clash = a (day, start) occupied by 2+ of this instructor's meetings
        seen: dict[tuple[str, int], int] = {}
        clashes = 0
        for slot in acc["slots"]:
            seen[slot] = seen.get(slot, 0) + 1
            if seen[slot] == 2:
                clashes += 1
        contact_hours = round(acc["contact_min"] / 60, 1)
        max_h = instr.max_weekly_hours
        if max_h is None:
            load_status = "na"
        elif contact_hours > max_h:
            load_status = "over"
        elif contact_hours == max_h:
            load_status = "at"
        else:
            load_status = "under"
        rows.append(
            {
                "instructor_id": instr.pk,
                "full_name": instr.full_name,
                "full_name_ar": instr.full_name_ar,
                "department": instr.department,
                "section_count": len(acc["sections"]),
                "sections": acc["sections"],
                "distinct_courses": len(acc["courses"]),
                "total_credit_hours": acc["credit"],
                "weekly_contact_hours": contact_hours,
                "teaching_days": sorted(acc["days"]),
                "clash_count": clashes,
                "max_weekly_hours": max_h,
                "load_status": load_status,
            }
        )
        tot_sections += len(acc["sections"])
        tot_credits += acc["credit"]
        tot_contact_min += acc["contact_min"]

    return _ok(
        {
            "scenario_id": int(scenario_id),
            "rows": rows,
            "totals": {
                "instructors": len(rows),
                "section_count": tot_sections,
                "total_credit_hours": tot_credits,
                "weekly_contact_hours": round(tot_contact_min / 60, 1),
            },
            "unparseable_meetings": unparseable,
        }
    )


# ── helpers ──────────────────────────────────────────────────────


def _parse_int_or_none(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _propagate_rename(instructor: Instructor) -> None:
    """After a rename, refresh the display cache on every section where this
    instructor is primary."""
    from core.models import TermSectionMeeting

    primary_section_ids = SectionInstructor.objects.filter(
        instructor=instructor, role="primary"
    ).values_list("term_section_id", flat=True)
    TermSectionMeeting.objects.filter(term_section_id__in=list(primary_section_ids)).update(
        instructor=instructor.full_name
    )

"""Instructor management — page shell, roster CRUD, advisor-seed, course-level
assignment (program + section M/F → course → instructor), teaching-time rules,
and a load report.

Assignment is scenario-INDEPENDENT: a ``CourseInstructor`` row ties an instructor
to ``(program, course_code, section M/F)``. RBAC mirrors section planning
(``ROLE_GENERAL_ADVISOR`` admits Super Admin); all writes are audited.
"""

from __future__ import annotations

import json
from typing import Any

from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from core.authz import role_required
from core.models import CourseInstructor, Instructor, InstructorTimeRule, ProgrammeRequirement
from core.services.academic_settings import load_defaults
from core.services.audit import log_audit_event
from core.services.course_instructor_assignment import (
    build_assignable_course_list,
    set_course_instructors,
)
from core.services.instructor_availability import (
    KINDS,
    MAX_NOTE_LENGTH,
    MAX_RULES_PER_INSTRUCTOR,
    MINUTES_PER_DAY,
    TimeRule,
    compile_rules,
    days_from_mask,
    default_period_grid,
    load_time_rules,
    open_summary,
    rules_revision,
    to_hhmm,
    wire_day_index,
    wire_minutes,
)
from core.services.rbac import ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN, get_user_role
from core.services.timetable_online import normalise_course_code
from core.services.timetable_pr4_instructor import normalise_instructor
from core.sidebar_context import get_sidebar_context

# The 12 real programmes (DBGPROG is a debug fixture, excluded from the picker).
KNOWN_PROGRAMS = ("AI", "AI2", "COE", "COE2", "CS", "CS2", "CYP", "CYP2", "DS", "DS2", "IS", "IS2")
VALID_SECTIONS = ("M", "F")


def _ok(data: dict[str, object], status: int = 200) -> JsonResponse:
    return JsonResponse({"ok": True, **data}, status=status)


def _err(
    message: str, *, code: str, status: int = 400, details: dict[str, object] | None = None
) -> JsonResponse:
    error: dict[str, object] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return JsonResponse({"ok": False, "error": error}, status=status)


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


def _text(value: object) -> str:
    """A text field from a JSON payload: a missing or null value is empty,
    never the string "None" (``str(None)``)."""
    return "" if value is None else str(value).strip()


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
        "time_rules_enforced": _time_rules_enforced(),
    }
    return render(request, "core/instructor_management.html", ctx)


def _time_rules_enforced() -> bool:
    """Whether the timetable builder applies the teaching-time rules today.

    The rules editor tells staff the truth either way: the builder honours a
    rule only when its setting is on and one teacher per section is active (a
    rule binds the one teacher a section is charged to). This asks the engine's
    own switch, so the sentence on the page can never disagree with the builder.
    """
    from core.services.timetable_instructor_availability import rules_enabled

    return rules_enabled()


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
    instructors = list(qs.order_by("full_name")[:500])
    summaries = _time_rule_summaries([i.pk for i in instructors])
    return _ok(
        {
            "instructors": [
                {**_instructor_to_dict(i), "time_rules": summaries[i.pk]} for i in instructors
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
                full_name_ar=_text(payload.get("full_name_ar")),
                email=_text(payload.get("email")),
                employee_no=_text(payload.get("employee_no")),
                department=_text(payload.get("department")),
                max_weekly_hours=_parse_int_or_none(payload.get("max_weekly_hours")),
                is_active=bool(payload.get("is_active", True)),
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
            setattr(instructor, key, _text(payload.get(key)))
            fields.append(key)
    if "max_weekly_hours" in payload:
        instructor.max_weekly_hours = _parse_int_or_none(payload.get("max_weekly_hours"))
        fields.append("max_weekly_hours")
    if "is_active" in payload:
        instructor.is_active = bool(payload.get("is_active"))
        fields.append("is_active")
    if not fields:
        return _ok({"instructor": _instructor_to_dict(instructor)})
    try:
        with transaction.atomic():
            instructor.save(update_fields=[*fields, "updated_at"])
    except IntegrityError:
        return _err(
            "Another instructor already has this name or email", code="DUPLICATE", status=409
        )
    details: dict[str, object] = {"id": instructor.pk, "fields": sorted(fields)}
    if "is_active" in fields:
        details["is_active"] = instructor.is_active
    log_audit_event(request, action="instructor_update", status="success", details=details)
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
    """A program's teachable courses (for one M/F section) with their instructor.

    Elective placeholders (AI1, IS1, GSE1 …) are resolved to the real courses
    the planner actually schedules for the term — assigning to a placeholder can
    never reach a section (see ``build_assignable_course_list``). Every course is
    returned; unassigned ones carry ``instructor: null`` so the UI can show the
    full list and the assignment state in one table.
    """
    program = (request.GET.get("program") or "").strip()
    section = (request.GET.get("section") or "").strip().upper()
    bad = _validate_program_section(program, section)
    if bad:
        return bad

    # Term drives which electives are "offered this term"; default to the
    # configured planning term, with optional query overrides.
    defaults = load_defaults()
    year = (request.GET.get("year") or "").strip() or defaults["academic_year"]
    term = _parse_int_or_none(request.GET.get("term"))
    if term is None:
        term = defaults["term"]

    courses = build_assignable_course_list(program, year, term)
    by_code = {normalise_course_code(c["course_code"]): c for c in courses}

    # overlay current assignments (keyed by the real/resolved course code). An
    # assignment whose code isn't in the resolved list (e.g. a legacy row keyed
    # to a now-resolved placeholder) is surfaced as an ``is_orphan`` row so it
    # stays visible and clearable rather than being silently stranded in the DB.
    orphans: dict[str, dict[str, object]] = {}
    for link in (
        CourseInstructor.objects.filter(program=program, section=section)
        .select_related("instructor")
        .order_by("id")
    ):
        code = normalise_course_code(link.course_code)
        row = by_code.get(code) or orphans.get(code)
        if row is None:
            row = {
                "course_code": link.course_code,
                "course_name": "",
                "programme_term": None,
                "credit_hours": None,
                "is_online": False,
                "is_elective": False,
                "is_placeholder": False,
                "is_orphan": True,
                "instructor": None,
                "co_instructors": [],
            }
            orphans[code] = row
        entry = {
            "id": link.instructor_id,
            "full_name": link.instructor.full_name,
            "full_name_ar": link.instructor.full_name_ar,
            "role": link.role,
            "max_sections": link.max_sections,
        }
        if link.role == "primary":
            row["instructor"] = entry
        else:
            row["co_instructors"].append(entry)  # type: ignore[union-attr]

    courses.extend(orphans.values())

    return _ok(
        {
            "program": program,
            "section": section,
            "year": year,
            "term": term,
            "courses": courses,
        }
    )


MAX_SECTIONS_LIMIT = 99


def _parse_instructor_entries(
    raw: object,
) -> tuple[list[int], dict[int, int | None], JsonResponse | None]:
    """Validate the ``instructors: [{id, max_sections}]`` form of the set view."""
    if not isinstance(raw, list):
        return [], {}, _err("instructors must be a list", code="VALIDATION", status=400)
    ids: list[int] = []
    limits: dict[int, int | None] = {}
    for entry in raw:
        iid = entry.get("id") if isinstance(entry, dict) else None
        if isinstance(iid, bool) or not isinstance(iid, int):
            return (
                [],
                {},
                _err("each instructor needs an integer id", code="VALIDATION", status=400),
            )
        value = entry.get("max_sections")
        if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
            value = int(value.strip())  # "2" is a whole number; "2.5" / "x" stay rejected
        if value is not None:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= MAX_SECTIONS_LIMIT
            ):
                return (
                    [],
                    {},
                    _err(
                        f"max_sections for instructor {iid} must be a whole number "
                        f"from 1 to {MAX_SECTIONS_LIMIT}, or empty",
                        code="VALIDATION",
                        status=400,
                    ),
                )
        ids.append(iid)
        if iid not in limits:  # a repeated id keeps its first entry (first wins)
            limits[iid] = value
    return ids, limits, None


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
    limits: dict[int, int | None] | None = None
    if payload.get("instructors") is not None:
        ids, limits, bad = _parse_instructor_entries(payload.get("instructors"))
        if bad:
            return bad
    else:
        ids = payload.get("instructor_ids")
        if ids is None and payload.get("instructor_id") is not None:
            ids = [payload.get("instructor_id")]
        if not isinstance(ids, list):
            return _err("instructor_ids must be a list", code="VALIDATION", status=400)
    try:
        instructors = set_course_instructors(program, course_code, section, ids, limits)
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
    from core.services.timetable_flags import is_one_teacher_active

    if is_one_teacher_active():
        # One teacher per section: the names come from the map the engine
        # charges (no row is written here), so never under a running job, and
        # never into a published timetable.
        from core.services.timetable_mutation_guard import reject_active_planner_job

        busy = reject_active_planner_job(scenario.id)
        if busy is not None:
            return busy
        if scenario.status == "published":
            return _err("Cannot modify published scenario", code="SCENARIO_PUBLISHED", status=409)
    updated = reconcile_scenario_instructors(scenario)
    log_audit_event(
        request,
        action="course_instructor_reconcile",
        status="success",
        details={"scenario_id": scenario.id, "sections_updated": updated},
    )
    return _ok({"scenario_id": scenario.id, "sections_updated": updated})


# ── Teaching-time rules ──────────────────────────────────────────
#
# What the rules MEAN is defined once in core/services/instructor_availability.py.
# Here they are only read, validated and replaced as a set. A save carries the
# revision the editor loaded; a different stored revision means someone else
# saved in between, and the request is refused (409 STALE) with the fresh rules
# so nothing is overwritten unseen.

_RULE_FIELDS = ("kind", "day_mask", "start_minute", "end_minute", "note")


def _positive_id(value: int) -> int | None:
    """Within the database's integer range (a longer one would not query)."""
    return value if 0 < value < 2**63 else None


def _query_instructor_id(value: object) -> int | None:
    """An instructor id from the query string: ASCII digits only, nothing else."""
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return _positive_id(int(value))
    return None


def _body_instructor_id(value: object) -> int | None:
    """An instructor id from a JSON body: a positive int, never a bool or a string."""
    if isinstance(value, int) and not isinstance(value, bool):
        return _positive_id(value)
    return None


def _instructor_brief(instructor: Instructor) -> dict[str, object]:
    return {
        "id": instructor.pk,
        "full_name": instructor.full_name,
        "full_name_ar": instructor.full_name_ar,
        "is_active": instructor.is_active,
    }


def _rule_sort_key(rule: dict[str, Any]) -> tuple[str, int, int, int, int]:
    mask = rule["day_mask"]
    first_day = (mask & -mask).bit_length() - 1
    return (rule["kind"], first_day, rule["start_minute"], rule["end_minute"], mask)


def _stored_rules(instructor: Instructor) -> list[dict[str, Any]]:
    """The instructor's stored rules in display order: kind, first day, start, end."""
    rows = InstructorTimeRule.objects.filter(instructor=instructor).values(*_RULE_FIELDS)
    return sorted(rows, key=_rule_sort_key)


def _rule_to_wire(rule: dict[str, Any]) -> dict[str, object]:
    return {
        "kind": rule["kind"],
        "days": list(days_from_mask(rule["day_mask"])),
        "start": to_hhmm(rule["start_minute"]),
        "end": to_hhmm(rule["end_minute"]),
        "note": rule["note"],
        "all_day": rule["start_minute"] == 0 and rule["end_minute"] == MINUTES_PER_DAY,
    }


def _rule_to_audit(rule: dict[str, Any]) -> str:
    """``kind|DAYS|HH:MM-HH:MM`` — the rule without its note."""
    days = ",".join(days_from_mask(rule["day_mask"]))
    window = f"{to_hhmm(rule['start_minute'])}-{to_hhmm(rule['end_minute'])}"
    return f"{rule['kind']}|{days}|{window}"


def _open_for(instructor_id: int, rules: list[dict[str, Any]]) -> dict[str, Any]:
    availability = compile_rules(
        TimeRule(
            instructor_id, rule["kind"], rule["day_mask"], rule["start_minute"], rule["end_minute"]
        )
        for rule in rules
    )
    return open_summary(availability, instructor_id, default_period_grid())


def _rules_payload(instructor: Instructor, rules: list[dict[str, Any]]) -> dict[str, object]:
    return {
        "instructor": _instructor_brief(instructor),
        "rules": [_rule_to_wire(rule) for rule in rules],
        "revision": rules_revision(rules),
        "open": _open_for(instructor.pk, rules),
    }


def _time_rule_summaries(instructor_ids: list[int]) -> dict[int, dict[str, object]]:
    """Per instructor: rule count and the feasibility summary of the default
    periods (``instructor_availability.feasibility_summary``) — one query for all."""
    rules = load_time_rules(instructor_ids)
    counts: dict[int, int] = {}
    for rule in rules:
        counts[rule.instructor_id] = counts.get(rule.instructor_id, 0) + 1
    availability = compile_rules(rules)
    grid = default_period_grid()
    return {
        pk: {"count": counts.get(pk, 0), "open": open_summary(availability, pk, grid)}
        for pk in instructor_ids
    }


def _rule_error(index: int, field: str, code: str) -> dict[str, object]:
    return {"index": index, "field": field, "code": code}


def _clean_rule(index: int, item: object) -> tuple[dict[str, Any] | None, list[dict[str, object]]]:
    """One wire rule -> a storable row, or every problem it has.

    The wire is strings only and is never coerced: days are SUN..THU codes,
    times ASCII "HH:MM" ("24:00" only as an end), a note is a string.
    """
    if not isinstance(item, dict):
        return None, [_rule_error(index, "rule", "rule_not_object")]
    errors: list[dict[str, object]] = []
    kind = item.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        errors.append(_rule_error(index, "kind", "kind_invalid"))
    days = item.get("days")
    mask = 0
    if days is None or days == []:
        errors.append(_rule_error(index, "days", "days_empty"))
    elif not isinstance(days, list):
        errors.append(_rule_error(index, "days", "days_invalid"))
    else:
        indexes = [wire_day_index(day) for day in days]
        if None in indexes:
            errors.append(_rule_error(index, "days", "days_invalid"))
        else:
            # A repeated day is the same day: the mask dedupes it.
            mask = sum({1 << day for day in indexes if day is not None})
    start = wire_minutes(item.get("start"), end=False)
    if start is None:
        errors.append(_rule_error(index, "start", "start_invalid"))
    end = wire_minutes(item.get("end"), end=True)
    if end is None:
        errors.append(_rule_error(index, "end", "end_invalid"))
    elif start is not None and end <= start:
        errors.append(_rule_error(index, "end", "end_not_after_start"))
    raw_note = item.get("note", "")
    note = ""
    if not isinstance(raw_note, str):
        errors.append(_rule_error(index, "note", "note_invalid"))
    else:
        note = raw_note.strip()
        if len(note) > MAX_NOTE_LENGTH:
            errors.append(_rule_error(index, "note", "note_too_long"))
    if errors:
        return None, errors
    rule = {"kind": kind, "day_mask": mask, "start_minute": start, "end_minute": end, "note": note}
    return rule, []


def _clean_rules(raw: object) -> tuple[list[dict[str, Any]], list[dict[str, object]]]:
    """A replace-all rule list -> storable rows, and EVERY error in the list."""
    if not isinstance(raw, list):
        return [], [_rule_error(-1, "rules", "rules_not_list")]
    if len(raw) > MAX_RULES_PER_INSTRUCTOR:
        # One error and nothing else: per-item errors of an over-long list
        # would let one small request build an unbounded response.
        return [], [_rule_error(-1, "rules", "too_many")]
    errors: list[dict[str, object]] = []
    clean: list[dict[str, Any]] = []
    seen: set[tuple[object, ...]] = set()
    for index, item in enumerate(raw):
        rule, problems = _clean_rule(index, item)
        errors += problems
        if rule is None:
            continue
        identity = (rule["kind"], rule["day_mask"], rule["start_minute"], rule["end_minute"])
        if identity in seen:
            errors.append(_rule_error(index, "rule", "duplicate"))
        seen.add(identity)
        clean.append(rule)
    return clean, errors


def _rule_set(rules: list[dict[str, Any]]) -> set[tuple[object, ...]]:
    return {tuple(rule[field] for field in _RULE_FIELDS) for rule in rules}


@role_required(ROLE_GENERAL_ADVISOR)
@require_GET
def instructor_time_rules_view(request: HttpRequest) -> JsonResponse:
    """One instructor's teaching-time rules, their revision, and the default
    teaching periods they leave open (``grid`` lets the editor preview edits)."""
    instructor_id = _query_instructor_id(request.GET.get("instructor_id"))
    if instructor_id is None:
        return _err("instructor_id must be an integer", code="VALIDATION", status=400)
    instructor = Instructor.objects.filter(pk=instructor_id).first()
    if instructor is None:
        return _err("Instructor not found", code="NOT_FOUND", status=404)
    payload = _rules_payload(instructor, _stored_rules(instructor))
    return _ok({**payload, "grid": default_period_grid()})


@role_required(ROLE_GENERAL_ADVISOR)
@require_POST
def instructor_time_rules_set_view(request: HttpRequest) -> JsonResponse:
    """Replace one instructor's teaching-time rules as a whole set.

    Body ``{instructor_id, expected_revision, rules: [...]}``. Every validation
    problem is reported at once (``details.errors``: ``{index, field, code}``,
    index -1 for the request itself). A stale ``expected_revision`` is refused
    with 409 ``STALE`` and the stored rules. Saving the stored set again
    rewrites nothing and audits nothing.
    """
    payload, err = _safe_json(request)
    if err:
        return err
    errors: list[dict[str, object]] = []
    instructor_id = _body_instructor_id(payload.get("instructor_id"))
    if instructor_id is None:
        errors.append(_rule_error(-1, "instructor_id", "instructor_id_invalid"))
    expected = payload.get("expected_revision")
    if not isinstance(expected, str) or not expected:
        errors.append(_rule_error(-1, "expected_revision", "revision_required"))
    rules, rule_errors = _clean_rules(payload.get("rules"))
    errors += rule_errors
    if errors:
        return _err(
            "The time rules are not valid",
            code="VALIDATION",
            status=400,
            details={"errors": errors},
        )

    with transaction.atomic():
        # The row lock serialises two saves of the same instructor, so the
        # revision compared below is the one this save replaces.
        instructor = Instructor.objects.select_for_update().filter(pk=instructor_id).first()
        if instructor is None:
            return _err("Instructor not found", code="NOT_FOUND", status=404)
        before = _stored_rules(instructor)
        revision = rules_revision(before)
        if expected != revision:
            return _err(
                "The time rules changed since they were loaded",
                code="STALE",
                status=409,
                details={"rules": [_rule_to_wire(rule) for rule in before], "revision": revision},
            )
        if _rule_set(rules) == _rule_set(before):
            return _ok(_rules_payload(instructor, before))
        InstructorTimeRule.objects.filter(instructor=instructor).delete()
        InstructorTimeRule.objects.bulk_create(
            InstructorTimeRule(instructor=instructor, **rule) for rule in rules
        )
        after = _stored_rules(instructor)

    details: dict[str, object] = {
        "id": instructor.pk,
        "before": [_rule_to_audit(rule) for rule in before],
        "after": [_rule_to_audit(rule) for rule in after],
    }
    if _notes_changed(before, after):
        details["notes_changed"] = True
    log_audit_event(request, action="instructor_time_rules_set", status="success", details=details)
    return _ok(_rules_payload(instructor, after))


def _notes_changed(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> bool:
    """Whether a save changed any note (the audit records only THAT, never the text).

    True when the notes written differ (one added, removed or edited), or when
    the rules themselves are the same -- a save that went through and changed
    no rule changed a note (one moved to another rule, say).
    """

    def notes(rules: list[dict[str, Any]]) -> list[str]:
        return sorted(rule["note"] for rule in rules if rule["note"])

    def windows(rules: list[dict[str, Any]]) -> list[str]:
        return sorted(_rule_to_audit(rule) for rule in rules)

    return notes(before) != notes(after) or windows(before) == windows(after)


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
                "max_sections": link.max_sections,
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

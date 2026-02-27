from __future__ import annotations

import json
import logging
import re

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from core.models import (
    Course,
    Prerequisite,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    TermSection,
    TermSectionMeeting,
)
from core.services.planner_builder import build_plans
from core.services.policy import require_student_scope
from core.services.rbac import ROLE_ADVISOR, ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN, get_user_role
from core.services.recommender import recommend_next_courses
from core.services.student_helpers import get_student_passed_and_studying, normalize_code
from core.services.student_sections import (
    ensure_student_section_schema,
    get_student_term_baseline,
    replace_student_term_sections,
)
from core.settings_views import load_defaults
from core.sidebar_context import get_sidebar_context

logger = logging.getLogger(__name__)


def _ok(data: dict[str, object], status: int = 200) -> JsonResponse:
    return JsonResponse({"ok": True, **data}, status=status)


def _err(
    message: str, *, code: str, status: int = 400, details: dict[str, object] | None = None
) -> JsonResponse:
    payload: dict[str, object] = {"ok": False, "error": {"code": code, "message": message}}
    if details:
        payload["error"]["details"] = details  # type: ignore[index]
    return JsonResponse(payload, status=status)


def _internal_error(exc: Exception) -> JsonResponse:
    return _err(
        "Internal processing error",
        code="INTERNAL_ERROR",
        status=500,
        details={"type": type(exc).__name__},
    )


def _require_staff(request: HttpRequest) -> JsonResponse | None:
    role = get_user_role(request.user)
    if role not in {ROLE_SUPER_ADMIN, ROLE_GENERAL_ADVISOR, ROLE_ADVISOR}:
        return _err("Staff role required", code="STAFF_ROLE_REQUIRED", status=403)
    return None


def _validate_term_inputs(year: str, term: str) -> JsonResponse | None:
    if not re.fullmatch(r"\d{4}", year):
        return _err("academic_year must be 4 digits", code="VALIDATION_YEAR_FORMAT", status=400)
    if term not in {"1", "2", "3"}:
        return _err("term must be one of: 1, 2, 3", code="VALIDATION_TERM_FORMAT", status=400)
    return None


def _parse_student_id(student_id: str) -> tuple[int | None, JsonResponse | None]:
    if not student_id.isdigit():
        return None, _err("student_id must be numeric", code="VALIDATION_STUDENT_ID", status=400)
    return int(student_id), None


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


@login_required(login_url="login")
@require_GET
def planner_page(request: HttpRequest) -> HttpResponse:
    deny = _require_staff(request)
    if deny:
        return HttpResponse(deny.content, status=deny.status_code, content_type="application/json")
    _defs = load_defaults()
    ctx = {
        **get_sidebar_context(request),
        "default_year": _defs["currentYear"],
        "default_term": _defs["currentTerm"],
    }
    return render(request, "core/planner.html", ctx)


@login_required(login_url="login")
@require_POST
def planner_context_view(request: HttpRequest) -> JsonResponse:
    deny = _require_staff(request)
    if deny:
        return deny

    ensure_student_section_schema()

    payload, payload_err = _safe_json(request)
    if payload_err:
        return payload_err
    student_id = str(payload.get("student_id", "")).strip()
    year = str(payload.get("academic_year", "")).strip()
    term = str(payload.get("term", "")).strip()

    if not student_id or not year or not term:
        return _err(
            "student_id, academic_year, term are required",
            code="VALIDATION_REQUIRED_FIELDS",
            status=400,
        )

    term_err = _validate_term_inputs(year, term)
    if term_err:
        return term_err

    student_id_int, sid_err = _parse_student_id(student_id)
    if sid_err:
        return sid_err
    assert student_id_int is not None

    scope_err = require_student_scope(request, student_id_int)
    if scope_err:
        return scope_err

    student = (
        Student.objects.filter(student_id=student_id_int)
        .values(
            "student_id",
            "name",
            "program",
            "section",
            "advisor_id",
            "gpa",
            "total_registered_credits",
        )
        .first()
    )
    if not student:
        return _err(f"Student not found: {student_id}", code="STUDENT_NOT_FOUND", status=404)

    student_summary = {
        "student_id": student["student_id"],
        "name": student["name"] or "",
        "program": student["program"] or "",
        "cohort_section": student["section"] or "",
        "advisor_id": student["advisor_id"] or "",
        "gpa": student["gpa"],
        "registered_credits": student["total_registered_credits"] or 0,
        "credit_cap": 18,
    }

    baseline = get_student_term_baseline(student_id, year, term)
    if not baseline:
        # Auto-repair: build current snapshot mappings from studying courses when possible.
        studying_codes_qs = (
            StudentCourse.objects.filter(
                student_id=student_id,
                status__iexact="studying",
            )
            .select_related("course")
            .values_list("course__course_code", flat=True)
            .distinct()
        )
        wanted = [normalize_code(c) for c in studying_codes_qs if c]
        wanted = [w for w in wanted if w]
        if wanted:
            from django.db.models import Min

            mapped_qs = (
                TermSection.objects.filter(
                    course_key__in=wanted,
                )
                .values("course_key")
                .annotate(sid=Min("id"))
            )
            mapped_ids = [int(x["sid"]) for x in mapped_qs if x["sid"] is not None]
            if mapped_ids:
                try:
                    replace_student_term_sections(
                        student_id, year, term, mapped_ids, source="auto_from_studying"
                    )
                    baseline = get_student_term_baseline(student_id, year, term)
                except Exception:
                    logger.warning(
                        "Auto-map sections failed for student %s", student_id, exc_info=True
                    )

    if not baseline:
        # Fallback when no student->section mappings exist yet
        fb_program = (
            Student.objects.filter(student_id=student_id).values_list("program", flat=True).first()
        )
        fb_credit_map: dict[str, int] = {}
        if fb_program:
            for cc, ch in ProgrammeRequirement.objects.filter(
                program__iexact=fb_program
            ).values_list("course_code", "credit_hours"):
                fb_credit_map[normalize_code(cc)] = ch or 0

        fb_rows = (
            StudentCourse.objects.filter(
                student_id=student_id,
                status__iexact="studying",
            )
            .select_related("course")
            .order_by("course__course_code")
        )
        baseline = []
        for sc in fb_rows:
            c = sc.course
            code_norm = normalize_code(c.course_code)
            credits = fb_credit_map.get(code_norm, c.credit_hours or 0)
            baseline.append(
                {
                    "course_code": c.course_code or "",
                    "course_name": c.description or "",
                    "credits": int(credits or 0),
                    "section": "",
                    "day": "",
                    "start_time": "",
                    "end_time": "",
                    "room": "",
                    "source": "fallback_studying",
                }
            )

    recommendation_warning = None
    try:
        rec_codes = recommend_next_courses(student_id, int(year), int(term))
    except Exception as exc:
        rec_codes = []
        recommendation_warning = f"Recommendation engine fallback: {type(exc).__name__}"

    passed, studying = get_student_passed_and_studying(student_id)
    program = str(student_summary["program"] or "").strip()

    recommendations: list[dict[str, object]] = []
    # Build credit map for the program
    pr_credit_map: dict[str, int] = {}
    if program:
        for pr_cc, pr_ch in ProgrammeRequirement.objects.filter(
            program__iexact=program
        ).values_list("course_code", "credit_hours"):
            pr_credit_map[normalize_code(pr_cc)] = pr_ch or 0

    # Pre-build normalized-code → Course dict — only load courses matching rec_codes
    _needed_codes = {normalize_code(c) for c in rec_codes if c}
    _needed_codes.discard("")
    _course_lookup: dict[str, Course] = {}
    if _needed_codes:
        # Include raw rec_codes too in case DB stores un-normalized forms
        _filter_codes = _needed_codes | {str(c).strip() for c in rec_codes if str(c).strip()}
        for c in Course.objects.filter(course_code__in=_filter_codes):
            _course_lookup[normalize_code(c.course_code)] = c

    # Batch-load all prerequisites for the program to avoid N+1 queries
    _all_prereqs: dict[str, list[str]] = {}
    if program:
        for row in Prerequisite.objects.filter(program=program):
            key = normalize_code(row.course_code)
            prereq_code = row.prerequisite_course_code
            if prereq_code is None:
                continue
            for part in str(prereq_code).split(","):
                p = normalize_code(part)
                if p:
                    _all_prereqs.setdefault(key, []).append(p)

    for idx, code in enumerate(rec_codes, start=1):
        code_n = normalize_code(code)
        course_obj = _course_lookup.get(code_n)
        if course_obj:
            credits = pr_credit_map.get(code_n, course_obj.credit_hours or 0)
            info = (course_obj.course_code, course_obj.description, credits)
        else:
            info = (code, "", pr_credit_map.get(code_n, 0))

        prereqs = _all_prereqs.get(code_n, []) if program else []
        missing = [p for p in prereqs if p not in passed and p not in studying]
        status = "Eligible" if not missing else "Blocked"
        recommendations.append(
            {
                "course_code": info[0] or code,
                "course_name": info[1] or "",
                "credits": int(info[2] or 0),
                "priority": "High" if idx <= 3 else ("Med" if idx <= 7 else "Low"),
                "score": max(0, 100 - idx * 3),
                "status": status,
                "missing_prerequisites": missing,
                "reason_tags": ["recommended"],
            }
        )

    seen_keys: set[tuple[str, str, str]] = set()
    credits_total = 0
    for r in baseline:
        row_key = (
            str(r.get("course_code", "")),
            str(r.get("course_number", "")),
            str(r.get("section", "")),
        )
        if row_key not in seen_keys:
            seen_keys.add(row_key)
            credits_total += int(r.get("credits", 0) or 0)  # type: ignore[call-overload]

    return _ok(
        {
            "student": student_summary,
            "baseline": baseline,
            "baseline_totals": {
                "courses": len(seen_keys),
                "credits": credits_total,
            },
            "recommendations": recommendations,
            "year": year,
            "term": term,
            "warning": recommendation_warning,
        }
    )


@login_required(login_url="login")
@require_POST
def planner_save_student_sections_view(request: HttpRequest) -> JsonResponse:
    deny = _require_staff(request)
    if deny:
        return deny

    ensure_student_section_schema()
    payload, payload_err = _safe_json(request)
    if payload_err:
        return payload_err
    student_id = str(payload.get("student_id", "")).strip()
    year = str(payload.get("academic_year", "")).strip()
    term = str(payload.get("term", "")).strip()
    section_ids = payload.get("term_section_ids", [])
    confirm_replace = bool(payload.get("confirm_replace", False))

    if not student_id or not year or not term:
        return _err(
            "student_id, academic_year, term are required",
            code="VALIDATION_REQUIRED_FIELDS",
            status=400,
        )
    if not isinstance(section_ids, list):
        return _err("term_section_ids must be a list", code="VALIDATION_LIST_REQUIRED", status=400)

    term_err = _validate_term_inputs(year, term)
    if term_err:
        return term_err

    student_id_int, sid_err = _parse_student_id(student_id)
    if sid_err:
        return sid_err
    assert student_id_int is not None

    scope_err = require_student_scope(request, student_id_int)
    if scope_err:
        return scope_err

    if not confirm_replace:
        return _err(
            "confirm_replace=true is required for replace operation",
            code="CONFIRM_REPLACE_REQUIRED",
            status=400,
        )

    cleaned = [int(x) for x in section_ids if str(x).strip().isdigit()]

    try:
        if cleaned:
            valid_ids = set(TermSection.objects.filter(id__in=cleaned).values_list("id", flat=True))
            invalid = [sid for sid in cleaned if sid not in valid_ids]
            if invalid:
                return _err(
                    "Some section ids are invalid",
                    code="VALIDATION_SECTION_IDS",
                    status=400,
                    details={"invalid_section_ids": invalid},
                )

        result = replace_student_term_sections(student_id, year, term, cleaned, source="planner")
        return _ok(result)  # type: ignore[arg-type]
    except Exception as exc:
        return _internal_error(exc)


@login_required(login_url="login")
@require_POST
def planner_sections_catalog_view(request: HttpRequest) -> JsonResponse:
    deny = _require_staff(request)
    if deny:
        return deny

    payload, payload_err = _safe_json(request)
    if payload_err:
        return payload_err
    year = str(payload.get("academic_year", "")).strip()
    term = str(payload.get("term", "")).strip()
    course_codes = payload.get("course_codes", [])

    if not year or not term:
        return _err(
            "academic_year and term are required", code="VALIDATION_REQUIRED_FIELDS", status=400
        )

    term_err = _validate_term_inputs(year, term)
    if term_err:
        return term_err

    try:
        ts_qs = TermSection.objects.all()
        if isinstance(course_codes, list) and course_codes:
            normalized = [
                str(c).replace(" ", "").strip().upper() for c in course_codes if str(c).strip()
            ]
            if normalized:
                ts_qs = ts_qs.filter(course_key__in=normalized)

        ts_qs = ts_qs.order_by("course_code", "course_number", "section")

        grouped: dict[int, dict[str, object]] = {}
        for ts in ts_qs:
            sid = ts.id
            grouped[sid] = {
                "term_section_id": sid,
                "course_code": ts.course_code or "",
                "course_number": ts.course_number or "",
                "section": ts.section or "",
                "course_name": ts.course_name or "",
                "available_capacity": ts.available_capacity,
                "registered_count": ts.registered_count,
                "meetings": [],
            }

        if grouped:
            meetings_qs = TermSectionMeeting.objects.filter(
                term_section_id__in=grouped.keys(),
            ).order_by("day", "start_time")
            for m in meetings_qs:
                if m.day or m.start_time or m.end_time:
                    grouped[m.term_section_id]["meetings"].append(  # type: ignore[attr-defined]
                        {
                            "day": m.day or "",
                            "start_time": m.start_time or "",
                            "end_time": m.end_time or "",
                            "room": m.room or "",
                            "instructor": m.instructor or "",
                        }
                    )

        return _ok({"sections": list(grouped.values()), "count": len(grouped)})
    except Exception as exc:
        return _internal_error(exc)


@login_required(login_url="login")
@require_POST
def planner_build_view(request: HttpRequest) -> JsonResponse:
    deny = _require_staff(request)
    if deny:
        return deny

    payload, payload_err = _safe_json(request)
    if payload_err:
        return payload_err
    year = str(payload.get("academic_year", "")).strip()
    term = str(payload.get("term", "")).strip()
    mode = str(payload.get("mode", "keep")).strip().lower()
    strict_sections = bool(payload.get("strict_sections", False))
    shortlist = payload.get("shortlist", [])
    baseline = payload.get("baseline", [])

    if not year or not term:
        return _err(
            "academic_year and term are required", code="VALIDATION_REQUIRED_FIELDS", status=400
        )
    if not isinstance(shortlist, list):
        return _err("shortlist must be list", code="VALIDATION_LIST_REQUIRED", status=400)
    term_err = _validate_term_inputs(year, term)
    if term_err:
        return term_err
    if mode not in {"keep", "ignore"}:
        return _err("mode must be keep or ignore", code="VALIDATION_MODE", status=400)

    normalized_shortlist: list[dict[str, object]] = []
    for item in shortlist:
        if not isinstance(item, dict):
            return _err(
                "shortlist items must be objects", code="VALIDATION_SHORTLIST_ITEM", status=400
            )
        code = str(item.get("course_code", "")).strip().upper()
        if not code:
            return _err(
                "shortlist item missing course_code", code="VALIDATION_SHORTLIST_COURSE", status=400
            )
        pinned_raw = item.get("pinned_sections", [])
        pinned_sections: list[dict[str, object]] = []
        if isinstance(pinned_raw, list):
            for ps in pinned_raw:
                if isinstance(ps, dict) and ps.get("term_section_id"):
                    pinned_sections.append(
                        {
                            "term_section_id": int(ps["term_section_id"]),
                            "section": str(ps.get("section", "")),
                        }
                    )

        normalized_shortlist.append(
            {
                "course_code": code,
                "priority": str(item.get("priority", "Med")),
                "score": int(item.get("score", 0) or 0),
                "status": str(item.get("status", "Eligible")),
                "missing_prerequisites": item.get("missing_prerequisites", [])
                if isinstance(item.get("missing_prerequisites", []), list)
                else [],
                "must_take": bool(item.get("must_take", False)),
                "credits": int(item.get("credits", 0) or 0),
                "pinned_sections": pinned_sections,
            }
        )

    keep_registered = mode != "ignore"
    suggest_swaps = bool(payload.get("swap", False))
    strict_sections = bool(payload.get("strict_sections", False))
    consider_capacity = not bool(payload.get("ignore_capacity", False))
    max_credits = int(payload.get("max_credits", 0) or 0)  # type: ignore[call-overload]
    try:
        result = build_plans(
            year,
            term,
            normalized_shortlist,
            baseline if isinstance(baseline, list) else [],
            keep_registered,
            suggest_swaps=suggest_swaps,
            strict_per_course=strict_sections,
            consider_capacity=consider_capacity,
            max_credits=max_credits,
        )
        result["mode"] = mode
        return _ok(result)
    except Exception as exc:
        return _internal_error(exc)

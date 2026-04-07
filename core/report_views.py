import csv
from io import StringIO

from django.http import FileResponse, HttpRequest, HttpResponse, HttpResponseBase, JsonResponse
from django.views.decorators.http import require_GET

from core.authz import role_required
from core.models import Prerequisite, ProgrammeRequirement
from core.services.advisors import list_students_by_advisor
from core.services.conflict_matrix import build_conflict_matrix_report, export_conflict_matrix_xlsx
from core.services.debug_reporting import build_recommendation_debug_report
from core.services.eligibility import build_course_eligibility_report
from core.services.high_priority_missing import (
    export_missing_high_priority_xlsx,
    run_missing_high_priority_report,
)
from core.services.policy import require_program_scope, require_student_scope
from core.services.rbac import ROLE_ADVISOR, ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN, get_user_scope
from core.services.recommender import recommend_next_courses
from core.services.reporting import build_aggregate_counts
from core.services.student_helpers import (
    get_prerequisites,
    get_student_passed_and_studying,
    get_student_program,
    normalize_code,
)
from core.settings_views import load_defaults


def _safe_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value else default
    except (ValueError, TypeError):
        return default


def _safe_float(value: str | None, default: float) -> float:
    try:
        return float(value) if value else default
    except (ValueError, TypeError):
        return default


def _parse_int(value: str | None, field: str) -> tuple[int | None, JsonResponse | None]:
    if value is None:
        return None, JsonResponse(
            {"error": f"Missing required query parameter: {field}"}, status=400
        )
    try:
        return int(value), None
    except ValueError:
        return None, JsonResponse({"error": f"Invalid integer for {field}: {value}"}, status=400)


def _excel_csv_response(filename: str, csv_text: str) -> HttpResponse:
    body = "\ufeff" + csv_text
    response = HttpResponse(body, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _program_importance_scores(program: str) -> dict[str, float]:
    rows = Prerequisite.objects.filter(
        program=program,
    ).values_list("course_code", "prerequisite_course_code")

    graph: dict[str, set[str]] = {}

    def add_edge(prereq: str, course: str) -> None:
        p = normalize_code(prereq)
        c = normalize_code(course)
        if not p or not c:
            return
        graph.setdefault(p, set()).add(c)
        graph.setdefault(c, set())

    for course_raw, prereq_raw in rows:
        course = normalize_code(course_raw)
        if not course:
            continue
        prereq_cell = "" if prereq_raw is None else str(prereq_raw)
        parts = [x.strip() for x in prereq_cell.split(",") if x.strip()]
        if not parts:
            graph.setdefault(course, set())
            continue
        for p in parts:
            add_edge(p, course)

    scores: dict[str, float] = {}
    for node in graph:
        dist: dict[str, int] = {node: 0}
        queue: list[str] = [node]
        idx = 0
        while idx < len(queue):
            current = queue[idx]
            idx += 1
            for nxt in graph.get(current, set()):
                if nxt not in dist:
                    dist[nxt] = dist[current] + 1
                    queue.append(nxt)

        score = 0.0
        for target, d in dist.items():
            if target == node or d == 0:
                continue
            score += 1.0 / d
        scores[node] = round(score, 6)

    return scores


def _build_student_plan_payload(student_id: int) -> tuple[dict | None, JsonResponse | None]:
    program = get_student_program(student_id)
    if not program:
        return None, JsonResponse(
            {"error": f"Student not found or has no program: {student_id}"}, status=404
        )

    passed, studying = get_student_passed_and_studying(student_id)
    satisfied_pool = passed | studying
    importance_scores = _program_importance_scores(program)

    pr_rows = (
        ProgrammeRequirement.objects.filter(
            program=program,
        )
        .order_by("programme_term", "course_code")
        .values_list(
            "course_code",
            "type",
            "programme_term",
            "credit_hours",
        )
    )

    terms: dict[int, list[dict[str, object]]] = {t: [] for t in range(1, 11)}

    for code_raw, ctype, term_raw, credits_raw in pr_rows:
        code = normalize_code(code_raw)
        term = int(term_raw) if term_raw is not None else 0

        if code in passed:
            status = "passed"
        elif code in studying:
            status = "studying"
        else:
            status = "not_taken"

        prereqs = get_prerequisites(code, program)
        missing_prereqs = [p for p in prereqs if p not in satisfied_pool]
        prereqs_ok = len(missing_prereqs) == 0
        can_register = status == "not_taken" and prereqs_ok

        item = {
            "course_code": code,
            "type": str(ctype) if ctype is not None else "",
            "programme_term": term,
            "credit_hours": int(credits_raw) if credits_raw is not None else None,
            "status": status,
            "can_register": can_register,
            "prerequisites": prereqs,
            "missing_prereqs": missing_prereqs,
            "importance_score": float(importance_scores.get(code, 0.0)),
        }

        if 1 <= term <= 10:
            terms[term].append(item)

    blocker_stats: dict[str, dict[str, float | int]] = {}
    for courses in terms.values():
        for c in courses:
            status_val = str(c.get("status", ""))
            can_register_val = bool(c.get("can_register", False))
            if status_val != "not_taken" or can_register_val:
                continue

            missing_raw = c.get("missing_prereqs", [])
            missing_list = missing_raw if isinstance(missing_raw, list) else []
            for m in missing_list:
                key = str(m)
                if key not in blocker_stats:
                    blocker_stats[key] = {
                        "blocks": 0,
                        "unlock_score": float(importance_scores.get(key, 0.0)),
                    }
                blocker_stats[key]["blocks"] = int(blocker_stats[key]["blocks"]) + 1

    blocker_hints_unsorted: list[dict[str, str | int | float]] = [
        {
            "course_code": k,
            "blocks": int(v["blocks"]),
            "unlock_score": float(v["unlock_score"]),
        }
        for k, v in blocker_stats.items()
    ]

    blocker_hints = sorted(
        blocker_hints_unsorted,
        key=lambda x: (
            -int(x["blocks"]),
            -float(x["unlock_score"]),
            str(x["course_code"]),
        ),
    )[:10]

    payload = {
        "student_id": student_id,
        "program": program,
        "summary": {
            "passed": len(passed),
            "studying": len(studying),
            "not_taken_can_register": sum(
                1
                for t in terms.values()
                for c in t
                if c["status"] == "not_taken" and c["can_register"]
            ),
            "not_taken_locked": sum(
                1
                for t in terms.values()
                for c in t
                if c["status"] == "not_taken" and not c["can_register"]
            ),
        },
        "blocker_hints": blocker_hints,
        "terms": [{"term": t, "courses": terms[t]} for t in range(1, 11)],
    }
    return payload, None


@role_required(ROLE_GENERAL_ADVISOR)
@require_GET
def report_summary_view(request: HttpRequest) -> JsonResponse:
    year, err = _parse_int(request.GET.get("year"), "year")
    if err:
        return err
    semester, err = _parse_int(request.GET.get("semester"), "semester")
    if err:
        return err

    if year is None or semester is None:
        return JsonResponse({"error": "Invalid parameters"}, status=400)

    program = request.GET.get("program") or None
    section = request.GET.get("section") or None

    scope_err = require_program_scope(request, program)
    if scope_err:
        return scope_err

    student_count, aggregate = build_aggregate_counts(
        year=year,
        semester=semester,
        program=program,
        section=section,
    )

    return JsonResponse(
        {
            "year": year,
            "semester": semester,
            "program": program,
            "section": section,
            "student_count": student_count,
            "top_recommended_courses": [
                {"course_code": code, "count": count} for code, count in aggregate.most_common(20)
            ],
        }
    )


@role_required(ROLE_ADVISOR)
@require_GET
def export_student_csv_view(request: HttpRequest) -> HttpResponse:
    student_id, err = _parse_int(request.GET.get("student_id"), "student_id")
    if err:
        return err
    year, err = _parse_int(request.GET.get("year"), "year")
    if err:
        return err
    semester, err = _parse_int(request.GET.get("semester"), "semester")
    if err:
        return err

    if student_id is None or year is None or semester is None:
        return JsonResponse({"error": "Invalid parameters"}, status=400)

    scope_err = require_student_scope(request, student_id)
    if scope_err:
        return scope_err

    recommendations = recommend_next_courses(
        student_id=student_id,
        current_academic_year=year,
        current_semester=semester,
    )

    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(["student_id", "year", "semester", "course_code"])
    for code in recommendations:
        writer.writerow([student_id, year, semester, code])

    return _excel_csv_response(f"student_{student_id}_{year}_{semester}.csv", out.getvalue())


@role_required(ROLE_GENERAL_ADVISOR)
@require_GET
def export_aggregate_csv_view(request: HttpRequest) -> HttpResponse:
    year, err = _parse_int(request.GET.get("year"), "year")
    if err:
        return err
    semester, err = _parse_int(request.GET.get("semester"), "semester")
    if err:
        return err

    if year is None or semester is None:
        return JsonResponse({"error": "Invalid parameters"}, status=400)

    program = request.GET.get("program") or None
    section = request.GET.get("section") or None

    scope_err = require_program_scope(request, program)
    if scope_err:
        return scope_err

    student_count, aggregate = build_aggregate_counts(
        year=year,
        semester=semester,
        program=program,
        section=section,
    )

    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(
        ["year", "semester", "program", "section", "student_count", "course_code", "count"]
    )
    for code, count in aggregate.most_common():
        writer.writerow([year, semester, program or "", section or "", student_count, code, count])

    return _excel_csv_response(f"aggregate_{year}_{semester}.csv", out.getvalue())


@role_required(ROLE_ADVISOR)
@require_GET
def student_plan_view(request: HttpRequest) -> JsonResponse:
    student_id, err = _parse_int(request.GET.get("student_id"), "student_id")
    if err:
        return err
    if student_id is None:
        return JsonResponse({"error": "Invalid student_id"}, status=400)

    scope_err = require_student_scope(request, student_id)
    if scope_err:
        return scope_err

    payload, payload_err = _build_student_plan_payload(student_id)
    if payload_err:
        return payload_err
    if payload is None:
        return JsonResponse({"error": "Failed to build student plan"}, status=500)

    return JsonResponse(payload)


@role_required(ROLE_ADVISOR)
@require_GET
def export_student_plan_csv_view(request: HttpRequest) -> HttpResponse:
    student_id, err = _parse_int(request.GET.get("student_id"), "student_id")
    if err:
        return err
    if student_id is None:
        return JsonResponse({"error": "Invalid student_id"}, status=400)

    scope_err = require_student_scope(request, student_id)
    if scope_err:
        return scope_err

    payload, payload_err = _build_student_plan_payload(student_id)
    if payload_err:
        return payload_err
    if payload is None:
        return JsonResponse({"error": "Failed to build student plan"}, status=500)

    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "student_id",
            "program",
            "term",
            "course_code",
            "credit_hours",
            "status",
            "can_register",
            "prerequisites",
        ]
    )

    program = str(payload["program"])
    terms = payload["terms"]
    for term_obj in terms:
        term = term_obj["term"]
        for course in term_obj["courses"]:
            writer.writerow(
                [
                    student_id,
                    program,
                    term,
                    course["course_code"],
                    course["credit_hours"],
                    course["status"],
                    course["can_register"],
                    ",".join(course["prerequisites"]),
                ]
            )

    return _excel_csv_response(f"student_plan_{student_id}.csv", out.getvalue())


@role_required(ROLE_ADVISOR)
@require_GET
def prerequisites_view(request: HttpRequest) -> JsonResponse:
    program = (request.GET.get("program") or "").strip()
    course_code = (request.GET.get("course_code") or "").strip().upper().replace(" ", "")

    if not program:
        return JsonResponse({"error": "Missing required query parameter: program"}, status=400)

    scope_err = require_program_scope(request, program, require_program_for_scoped=False)
    if scope_err:
        return scope_err

    qs = Prerequisite.objects.filter(program=program)
    if course_code:
        # Filter by normalized course code
        matching_codes = [p.course_code for p in qs if normalize_code(p.course_code) == course_code]
        qs = qs.filter(course_code__in=matching_codes) if matching_codes else qs.none()

    rows = qs.order_by("course_code", "prerequisite_course_code").values_list(
        "course_code",
        "prerequisite_course_code",
    )
    data = [{"course_code": str(r[0]), "prerequisite_course_code": str(r[1])} for r in rows]
    return JsonResponse({"program": program, "count": len(data), "items": data})


@role_required(ROLE_ADVISOR)
@require_GET
def program_plan_view(request: HttpRequest) -> JsonResponse:
    program = (request.GET.get("program") or "").strip()
    if not program:
        return JsonResponse({"error": "Missing required query parameter: program"}, status=400)

    scope_err = require_program_scope(request, program, require_program_for_scoped=False)
    if scope_err:
        return scope_err

    pp_rows = (
        ProgrammeRequirement.objects.filter(
            program=program,
        )
        .order_by("programme_term", "course_code")
        .values_list(
            "course_code",
            "programme_term",
            "credit_hours",
        )
    )

    items = [
        {
            "course_code": str(r[0]),
            "programme_term": int(r[1]) if r[1] is not None else None,
            "credit_hours": int(r[2]) if r[2] is not None else None,
        }
        for r in pp_rows
    ]
    return JsonResponse({"program": program, "count": len(items), "items": items})


@role_required(ROLE_ADVISOR)
@require_GET
def recommendation_debug_view(request: HttpRequest) -> JsonResponse:
    _defaults = load_defaults()
    year_raw = request.GET.get("year", "").strip() or str(_defaults["academic_year"])
    semester_raw = request.GET.get("semester", "").strip() or str(_defaults["term"])

    year, err = _parse_int(year_raw, "year")
    if err:
        return err
    semester, err = _parse_int(semester_raw, "semester")
    if err:
        return err

    if year is None or semester is None:
        return JsonResponse({"error": "Invalid parameters"}, status=400)

    section = (request.GET.get("section") or "").strip().upper() or None
    program = (request.GET.get("program") or "").strip().upper() or None
    join_years_raw = (request.GET.get("join_years") or "").strip()
    join_years = (
        [x.strip() for x in join_years_raw.split(",") if x.strip()] if join_years_raw else None
    )
    limit = _safe_int(request.GET.get("limit"), 150)

    scope_err = require_program_scope(request, program)
    if scope_err:
        return scope_err

    payload = build_recommendation_debug_report(
        current_academic_year=year,
        current_semester=semester,
        section=section,
        program=program,
        join_year_prefixes=join_years,
        limit=limit,
    )
    return JsonResponse(payload)


@role_required(ROLE_ADVISOR)
@require_GET
def conflict_matrix_view(request: HttpRequest) -> JsonResponse:
    _defaults = load_defaults()
    year_raw = request.GET.get("year", "").strip() or str(_defaults["academic_year"])
    semester_raw = request.GET.get("semester", "").strip() or str(_defaults["term"])

    year, err = _parse_int(year_raw, "year")
    if err:
        return err
    semester, err = _parse_int(semester_raw, "semester")
    if err:
        return err

    if year is None or semester is None:
        return JsonResponse({"error": "Invalid parameters"}, status=400)

    section = (request.GET.get("section") or "").strip().upper() or None
    program = (request.GET.get("program") or "").strip().upper() or None
    join_years_raw = (request.GET.get("join_years") or "").strip()
    join_years = (
        [x.strip() for x in join_years_raw.split(",") if x.strip()] if join_years_raw else None
    )
    limit = _safe_int(request.GET.get("limit"), 150)

    scope_err = require_program_scope(request, program)
    if scope_err:
        return scope_err

    payload = build_conflict_matrix_report(
        current_academic_year=year,
        current_semester=semester,
        section=section,
        program=program,
        join_year_prefixes=join_years,
        limit=limit,
    )
    return JsonResponse(payload)


@role_required(ROLE_ADVISOR)
@require_GET
def export_conflict_matrix_xlsx_view(request: HttpRequest) -> HttpResponseBase:
    _defaults = load_defaults()
    year_raw = request.GET.get("year", "").strip() or str(_defaults["academic_year"])
    semester_raw = request.GET.get("semester", "").strip() or str(_defaults["term"])

    year, err = _parse_int(year_raw, "year")
    if err:
        return err
    semester, err = _parse_int(semester_raw, "semester")
    if err:
        return err

    if year is None or semester is None:
        return JsonResponse({"error": "Invalid parameters"}, status=400)

    section = (request.GET.get("section") or "").strip().upper() or None
    program = (request.GET.get("program") or "").strip().upper() or None
    join_years_raw = (request.GET.get("join_years") or "").strip()
    join_years = (
        [x.strip() for x in join_years_raw.split(",") if x.strip()] if join_years_raw else None
    )
    limit = _safe_int(request.GET.get("limit"), 150)

    scope_err = require_program_scope(request, program)
    if scope_err:
        return scope_err

    try:
        path = export_conflict_matrix_xlsx(
            current_academic_year=year,
            current_semester=semester,
            section=section,
            program=program,
            join_year_prefixes=join_years,
            limit=limit,
        )
        return FileResponse(path.open("rb"), as_attachment=True, filename="conflict_matrix.xlsx")
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=500)


@role_required(ROLE_ADVISOR)
@require_GET
def course_eligibility_view(request: HttpRequest) -> JsonResponse:
    course_code = (request.GET.get("course_code") or "").strip().upper()
    if not course_code:
        return JsonResponse({"error": "course_code is required"}, status=400)

    section = (request.GET.get("section") or "").strip().upper() or None
    program = (request.GET.get("program") or "").strip().upper() or None
    join_years_raw = (request.GET.get("join_years") or "").strip()
    join_years = (
        [x.strip() for x in join_years_raw.split(",") if x.strip()] if join_years_raw else None
    )
    strict_mode = (request.GET.get("mode") or "").strip().lower() == "strict"

    scope_err = require_program_scope(request, program)
    if scope_err:
        return scope_err

    payload = build_course_eligibility_report(
        course_code=course_code,
        section=section,
        program=program,
        join_year_prefixes=join_years,
        strict_passed_only=strict_mode,
    )
    return JsonResponse(payload)


@role_required(ROLE_ADVISOR)
@require_GET
def export_recommendation_debug_csv_view(request: HttpRequest) -> HttpResponse:
    _defaults = load_defaults()
    year_raw = request.GET.get("year", "").strip() or str(_defaults["academic_year"])
    semester_raw = request.GET.get("semester", "").strip() or str(_defaults["term"])

    year, err = _parse_int(year_raw, "year")
    if err:
        return err
    semester, err = _parse_int(semester_raw, "semester")
    if err:
        return err
    if year is None or semester is None:
        return JsonResponse({"error": "Invalid parameters"}, status=400)

    section = (request.GET.get("section") or "").strip().upper() or None
    program = (request.GET.get("program") or "").strip().upper() or None
    join_years_raw = (request.GET.get("join_years") or "").strip()
    join_years = (
        [x.strip() for x in join_years_raw.split(",") if x.strip()] if join_years_raw else None
    )
    limit = _safe_int(request.GET.get("limit"), 150)

    scope_err = require_program_scope(request, program)
    if scope_err:
        return scope_err

    payload = build_recommendation_debug_report(year, semester, section, program, join_years, limit)

    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "student_id",
            "program",
            "real_term",
            "next_term",
            "passed",
            "studying",
            "recommended_courses",
        ]
    )
    for item in payload.get("items", []):
        writer.writerow(
            [
                item.get("student_id"),
                item.get("program"),
                item.get("real_term"),
                item.get("next_term"),
                ",".join(item.get("passed", [])),
                ",".join(item.get("studying", [])),
                ",".join(item.get("recommended_courses", [])),
            ]
        )

    return _excel_csv_response("recommendation_debug.csv", out.getvalue())


@role_required(ROLE_ADVISOR)
@require_GET
def export_course_eligibility_csv_view(request: HttpRequest) -> HttpResponse:
    course_code = (request.GET.get("course_code") or "").strip().upper()
    if not course_code:
        return JsonResponse({"error": "course_code is required"}, status=400)

    section = (request.GET.get("section") or "").strip().upper() or None
    program = (request.GET.get("program") or "").strip().upper() or None
    join_years_raw = (request.GET.get("join_years") or "").strip()
    join_years = (
        [x.strip() for x in join_years_raw.split(",") if x.strip()] if join_years_raw else None
    )
    strict_mode = (request.GET.get("mode") or "").strip().lower() == "strict"

    scope_err = require_program_scope(request, program)
    if scope_err:
        return scope_err

    payload = build_course_eligibility_report(
        course_code, section, program, join_years, strict_mode
    )

    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "course_code",
            "mode",
            "program",
            "students",
            "eligible_count",
            "eligible_student_ids",
            "prerequisites",
        ]
    )
    mode = "strict" if strict_mode else "relaxed"
    for row in payload.get("per_program", []):
        writer.writerow(
            [
                payload.get("course_code"),
                mode,
                row.get("program"),
                row.get("students"),
                row.get("eligible_count"),
                ",".join(str(x) for x in row.get("eligible_student_ids", [])),
                ",".join(row.get("prerequisites", [])),
            ]
        )

    return _excel_csv_response("course_eligibility.csv", out.getvalue())


@role_required(ROLE_ADVISOR)
@require_GET
def missing_high_priority_view(request: HttpRequest) -> JsonResponse:
    year, err = _parse_int(request.GET.get("year"), "year")
    if err:
        return err
    semester, err = _parse_int(request.GET.get("semester"), "semester")
    if err:
        return err
    if year is None or semester is None:
        return JsonResponse({"error": "Invalid parameters"}, status=400)

    section = (request.GET.get("section") or "").strip().upper() or None
    program = (request.GET.get("program") or "").strip().upper() or None
    join_years_raw = (request.GET.get("join_years") or "").strip()
    join_years = (
        [x.strip() for x in join_years_raw.split(",") if x.strip()] if join_years_raw else None
    )

    scope_err = require_program_scope(request, program)
    if scope_err:
        return scope_err

    term_parity = _safe_int(request.GET.get("term_parity"), 0)
    discount = (request.GET.get("discount") or "1_over_d").strip()
    min_score = _safe_float(request.GET.get("min_score"), 2.0)
    top_k = _safe_int(request.GET.get("top_k"), 10)
    studying_counts = (request.GET.get("studying_counts_as_passed") or "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }

    payload = run_missing_high_priority_report(
        year=year,
        semester=semester,
        section=section,
        program=program,
        join_year_prefixes=join_years,
        term_parity=term_parity,
        discount=discount,
        min_score=min_score,
        top_k_per_student=top_k,
        studying_counts_as_passed=studying_counts,
    )
    return JsonResponse(payload)


@role_required(ROLE_GENERAL_ADVISOR)
@require_GET
def export_students_by_advisor_csv_view(request: HttpRequest) -> HttpResponse:
    advisor_id = (request.GET.get("advisor_id") or "").strip()
    if not advisor_id:
        return JsonResponse({"error": "advisor_id is required"}, status=400)

    search = (request.GET.get("search") or "").strip() or None
    focus = (request.GET.get("focus") or "").strip() or None
    program_filter = (request.GET.get("program_filter") or "").strip() or None

    scope = get_user_scope(request.user)
    role = str(scope.get("role", ""))
    forced_advisor_id = str(scope.get("advisor_id", "")).strip() if role != ROLE_SUPER_ADMIN else ""
    allowed_departments = (
        [str(x).upper() for x in scope.get("departments", [])]
        if role == ROLE_GENERAL_ADVISOR
        else None
    )

    payload = list_students_by_advisor(
        advisor_id,
        search=search,
        focus=focus,
        program_filter=program_filter,
        forced_advisor_id=forced_advisor_id,
        allowed_departments=allowed_departments,
    )
    if payload.get("mapping_ready") is False:
        return JsonResponse(
            {"error": payload.get("message", "students.advisor_id column is not added yet.")},
            status=400,
        )

    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "advisor_id",
            "student_id",
            "registration_no",
            "name",
            "program",
            "section",
            "status",
            "gpa",
            "total_earned_credits",
            "total_registered_credits",
            "current_term_registered_hours",
            "has_high_priority_missing",
            "needs_attention",
            "risk_score",
            "attention_reasons",
            "missing_courses_compact",
        ]
    )
    for row in payload.get("items", []):
        writer.writerow(
            [
                advisor_id,
                row.get("student_id"),
                row.get("registration_no", ""),
                row.get("name", ""),
                row.get("program", ""),
                row.get("section", ""),
                row.get("status", ""),
                row.get("gpa", ""),
                row.get("total_earned_credits", ""),
                row.get("total_registered_credits", ""),
                row.get("current_term_registered_hours", ""),
                row.get("has_high_priority_missing", ""),
                row.get("needs_attention", ""),
                row.get("risk_score", ""),
                ",".join(row.get("attention_reasons", [])),
                row.get("missing_courses_compact", ""),
            ]
        )

    return _excel_csv_response(f"students_by_advisor_{advisor_id}.csv", out.getvalue())


@role_required(ROLE_ADVISOR)
@require_GET
def export_missing_high_priority_xlsx_view(request: HttpRequest) -> HttpResponseBase:
    year, err = _parse_int(request.GET.get("year"), "year")
    if err:
        return err
    semester, err = _parse_int(request.GET.get("semester"), "semester")
    if err:
        return err
    if year is None or semester is None:
        return JsonResponse({"error": "Invalid parameters"}, status=400)

    section = (request.GET.get("section") or "").strip().upper() or None
    program = (request.GET.get("program") or "").strip().upper() or None
    join_years_raw = (request.GET.get("join_years") or "").strip()
    join_years = (
        [x.strip() for x in join_years_raw.split(",") if x.strip()] if join_years_raw else None
    )

    scope_err = require_program_scope(request, program)
    if scope_err:
        return scope_err

    term_parity = _safe_int(request.GET.get("term_parity"), 0)
    discount = (request.GET.get("discount") or "1_over_d").strip()
    min_score = _safe_float(request.GET.get("min_score"), 2.0)
    top_k = _safe_int(request.GET.get("top_k"), 10)
    studying_counts = (request.GET.get("studying_counts_as_passed") or "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }

    try:
        path = export_missing_high_priority_xlsx(
            year=year,
            semester=semester,
            section=section,
            program=program,
            join_year_prefixes=join_years,
            term_parity=term_parity,
            discount=discount,
            min_score=min_score,
            top_k_per_student=top_k,
            studying_counts_as_passed=studying_counts,
        )
    except RuntimeError as exc:
        return JsonResponse({"error": str(exc)}, status=500)

    return FileResponse(
        path.open("rb"), as_attachment=True, filename="flagged_students_missing_high_priority.xlsx"
    )

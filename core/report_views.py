import csv
from io import StringIO

from django.http import FileResponse, HttpRequest, HttpResponse, HttpResponseBase, JsonResponse
from django.views.decorators.http import require_GET

from core.authz import role_required
from core.models import Course, Prerequisite, ProgrammeRequirement, Student
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


def _parse_programs(program: str | None) -> list[str] | None:
    if not program:
        return None
    return [p.strip() for p in program.split(",") if p.strip()]


def _student_programs_for_filter(program: str | None, section: str | None) -> list[str]:
    parsed = _parse_programs(program)
    if parsed is not None:
        return parsed

    qs = Student.objects.exclude(program__isnull=True).exclude(program="")
    if section:
        qs = qs.filter(section=section)
    return sorted({str(p).strip() for p in qs.values_list("program", flat=True) if str(p).strip()})


def _course_name_fallbacks(course_codes: list[str]) -> dict[str, str]:
    return {
        normalize_code(code): description or ""
        for code, description in Course.objects.filter(course_code__in=course_codes).values_list(
            "course_code", "description"
        )
    }


def _programme_course_names(program: str, course_codes: list[str]) -> dict[str, str]:
    return {
        normalize_code(code): course_name or ""
        for code, course_name in ProgrammeRequirement.objects.filter(
            program=program,
            course_code__in=course_codes,
        ).values_list("course_code", "course_name")
    }


def _build_batch_course_rows(
    *,
    year: int,
    semester: int,
    program: str | None,
    section: str | None,
    limit: int | None = None,
) -> tuple[int, list[dict[str, object]]]:
    """Return batch recommender rows grouped by plan-specific course identity."""
    student_count, aggregate = build_aggregate_counts(
        year=year,
        semester=semester,
        program=program,
        section=section,
    )
    program_list = _student_programs_for_filter(program, section)
    show_programs = len(program_list) != 1

    course_codes = [normalize_code(code) for code in aggregate.keys()]
    fallback_names = _course_name_fallbacks(course_codes)
    merged: dict[tuple[str, str], dict[str, object]] = {}

    if program_list:
        for prog in program_list:
            _prog_student_count, prog_aggregate = build_aggregate_counts(
                year=year,
                semester=semester,
                program=prog,
                section=section,
            )
            prog_codes = [normalize_code(code) for code in prog_aggregate.keys()]
            prog_names = _programme_course_names(prog, prog_codes)
            for raw_code, count in prog_aggregate.items():
                code = normalize_code(raw_code)
                course_name = prog_names.get(code) or fallback_names.get(code, "")
                key = (code, course_name)
                if key not in merged:
                    merged[key] = {
                        "course_code": code,
                        "course_name": course_name,
                        "count": 0,
                        "programs": [],
                        "show_programs": show_programs,
                    }
                merged[key]["count"] = int(merged[key]["count"]) + int(count)
                programs = merged[key]["programs"]
                if isinstance(programs, list) and prog not in programs:
                    programs.append(prog)

    if not merged:
        for raw_code, count in aggregate.items():
            code = normalize_code(raw_code)
            course_name = fallback_names.get(code, "")
            merged[(code, course_name)] = {
                "course_code": code,
                "course_name": course_name,
                "count": int(count),
                "programs": program_list,
                "show_programs": show_programs,
            }

    rows = list(merged.values())
    for row in rows:
        programs = row.get("programs")
        if isinstance(programs, list):
            row["programs"] = sorted(programs)
    rows.sort(
        key=lambda row: (
            -int(row.get("count", 0)),
            str(row.get("course_code", "")),
            str(row.get("course_name", "")),
        )
    )
    if limit is not None:
        rows = rows[:limit]
    return student_count, rows


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

    student_count, rows = _build_batch_course_rows(
        year=year,
        semester=semester,
        program=program,
        section=section,
        limit=20,
    )

    return JsonResponse(
        {
            "year": year,
            "semester": semester,
            "program": program,
            "section": section,
            "student_count": student_count,
            "top_recommended_courses": rows,
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

    student_count, rows = _build_batch_course_rows(
        year=year,
        semester=semester,
        program=program,
        section=section,
    )

    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "year",
            "semester",
            "program",
            "section",
            "student_count",
            "programs",
            "course_code",
            "course_name",
            "count",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                year,
                semester,
                program or "",
                section or "",
                student_count,
                ",".join(row.get("programs", [])) if isinstance(row.get("programs"), list) else "",
                row.get("course_code", ""),
                row.get("course_name", ""),
                row.get("count", 0),
            ]
        )

    return _excel_csv_response(f"aggregate_{year}_{semester}.csv", out.getvalue())


@role_required(ROLE_ADVISOR)
@require_GET
def export_aggregate_xlsx_view(request: HttpRequest) -> HttpResponse:
    """Export batch recommender results as styled XLSX."""
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

    student_count, rows = _build_batch_course_rows(
        year=year,
        semester=semester,
        program=program,
        section=section,
    )

    from core.services.batch_export import export_batch_recommender_xlsx

    path = export_batch_recommender_xlsx(
        year,
        semester,
        program,
        section,
        student_count,
        {str(row.get("course_code", "")): int(row.get("count", 0)) for row in rows},
        course_rows=rows,
    )
    prog_label = program or "all"
    filename = f"batch_recommender_{prog_label}_{year}_T{semester}.xlsx"
    response = FileResponse(
        open(path, "rb"),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


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
def export_prerequisites_xlsx_view(request: HttpRequest) -> HttpResponse:
    """Export course prerequisites as a styled XLSX with dependency graph."""
    import tempfile
    from collections import defaultdict
    from pathlib import Path

    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XlImage
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    program = (request.GET.get("program") or "").strip()
    course_code = (request.GET.get("course_code") or "").strip().upper().replace(" ", "")

    if not program:
        return JsonResponse({"error": "Missing required query parameter: program"}, status=400)

    scope_err = require_program_scope(request, program, require_program_for_scoped=False)
    if scope_err:
        return scope_err

    qs = Prerequisite.objects.filter(program=program)
    if course_code:
        matching_codes = [p.course_code for p in qs if normalize_code(p.course_code) == course_code]
        qs = qs.filter(course_code__in=matching_codes) if matching_codes else qs.none()

    rows = list(
        qs.order_by("course_code", "prerequisite_course_code").values_list(
            "course_code",
            "prerequisite_course_code",
        )
    )

    # ── Shared styles ───────────────────────────────────────────
    thin = Side(style="thin", color="D5D8DC")
    border = Border(top=thin, bottom=thin, left=thin, right=thin)
    hdr_fill = PatternFill(start_color="0A8E6E", end_color="0A8E6E", fill_type="solid")
    hdr_font = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
    title_fill = PatternFill(start_color="1B2631", end_color="1B2631", fill_type="solid")
    title_font = Font(name="Calibri", bold=True, color="FFFFFF", size=13)
    row_alt = PatternFill(start_color="F4F6F7", end_color="F4F6F7", fill_type="solid")
    center = Alignment(horizontal="center", vertical="center")
    left_a = Alignment(horizontal="left", vertical="center")

    wb = Workbook()
    wb.remove(wb.active)

    # ── Sheet 1: Dependency Graph ───────────────────────────────
    graph_img = _render_prereq_graph(rows, program)
    ws_graph = wb.create_sheet(title="Dependency Graph")
    ws_graph.sheet_properties.tabColor = "0A8E6E"

    if graph_img is not None:
        img = XlImage(graph_img)
        # Scale to fit nicely — max ~1200px wide in the image
        # Excel column default ~64px, so ~18 columns ≈ 1150px
        ws_graph.add_image(img, "A1")
        # Set row heights and column widths so the image area is clean
        img_w, img_h = img.width, img.height
        # Each Excel column ≈ 8.43 chars ≈ 64px, each row ≈ 15px
        cols_needed = max(1, int(img_w / 64) + 2)
        rows_needed = max(1, int(img_h / 15) + 2)
        for c in range(1, cols_needed + 1):
            ws_graph.column_dimensions[get_column_letter(c)].width = 10
        for r in range(1, rows_needed + 1):
            ws_graph.row_dimensions[r].height = 15
    else:
        ws_graph.cell(row=1, column=1, value="No prerequisite data to graph.")

    # ── Sheet 2: Grouped by Course ──────────────────────────────
    ws2 = wb.create_sheet(title="Grouped by Course")
    ws2.sheet_properties.tabColor = "2E86C1"

    ws2.merge_cells("A1:C1")
    c = ws2.cell(row=1, column=1, value=f"Prerequisites Grouped — {program}")
    c.fill = title_fill
    c.font = title_font
    c.alignment = center
    for col in range(2, 4):
        ws2.cell(row=1, column=col).fill = title_fill

    for col, h in enumerate(["Course Code", "Prerequisites", "Count"], 1):
        c = ws2.cell(row=2, column=col, value=h)
        c.fill = hdr_fill
        c.font = hdr_font
        c.border = border
        c.alignment = center

    grouped: dict[str, list[str]] = defaultdict(list)
    for cc, pc in rows:
        grouped[cc].append(pc)

    r_idx = 3
    for cc in sorted(grouped):
        prereqs = grouped[cc]
        ws2.cell(row=r_idx, column=1, value=cc).alignment = left_a
        ws2.cell(row=r_idx, column=2, value=", ".join(sorted(prereqs))).alignment = left_a
        ws2.cell(row=r_idx, column=3, value=len(prereqs)).alignment = center
        if r_idx % 2 == 1:
            for col in range(1, 4):
                ws2.cell(row=r_idx, column=col).fill = row_alt
        for col in range(1, 4):
            ws2.cell(row=r_idx, column=col).border = border
        r_idx += 1

    ws2.column_dimensions["A"].width = 18
    ws2.column_dimensions["B"].width = 40
    ws2.column_dimensions["C"].width = 10
    ws2.freeze_panes = "A3"

    # ── Sheet 3: Summary ────────────────────────────────────────
    ws3 = wb.create_sheet(title="Summary")
    ws3.sheet_properties.tabColor = "F39C12"

    ws3.merge_cells("A1:B1")
    c = ws3.cell(row=1, column=1, value=f"Summary — {program}")
    c.fill = title_fill
    c.font = title_font
    c.alignment = center
    ws3.cell(row=1, column=2).fill = title_fill

    summary_data = [
        ("Program", program),
        ("Total Prerequisite Links", len(rows)),
        ("Courses with Prerequisites", len(grouped)),
        ("Unique Prerequisite Courses", len({pc for _, pc in rows})),
    ]
    for r_idx, (label, val) in enumerate(summary_data, 2):
        ws3.cell(row=r_idx, column=1, value=label).font = Font(bold=True)
        ws3.cell(row=r_idx, column=1).border = border
        ws3.cell(row=r_idx, column=2, value=val).border = border

    ws3.column_dimensions["A"].width = 30
    ws3.column_dimensions["B"].width = 20

    # ── Save and return ─────────────────────────────────────────
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    wb.save(tmp.name)
    tmp.close()

    course_label = f"_{course_code}" if course_code else ""
    filename = f"prerequisites_{program}{course_label}.xlsx"
    response = FileResponse(
        open(Path(tmp.name), "rb"),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _render_prereq_graph(rows: list[tuple[str, str]], program: str):
    """Render prerequisite dependency graph as a high-quality PNG.

    Mirrors the frontend ``renderPrereqGraph`` topological layout with
    polished rendering: anti-aliased bezier edges on a translucent layer,
    drop-shadow nodes, refined arrowheads, and a subtle gradient
    background.

    Returns a ``BytesIO`` ready for openpyxl ``Image()``, or ``None``.
    """
    from collections import defaultdict
    from io import BytesIO
    from math import atan2, cos, sin

    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    if not rows:
        return None

    # ── Build adjacency ─────────────────────────────────────────
    prereqs: dict[str, list[str]] = defaultdict(list)
    all_courses: set[str] = set()
    for cc, pc in rows:
        all_courses.add(cc)
        all_courses.add(pc)
        prereqs[cc].append(pc)

    # ── Topological layers ──────────────────────────────────────
    layers: dict[str, int] = {}

    def depth(c: str, vis: set | None = None) -> int:
        if c in layers:
            return layers[c]
        if vis is None:
            vis = set()
        if c in vis:
            return 0
        vis.add(c)
        ps = prereqs.get(c, [])
        if not ps:
            layers[c] = 0
            return 0
        d = max(depth(p, set(vis)) for p in ps) + 1
        layers[c] = d
        return d

    for c in all_courses:
        depth(c)

    groups: dict[int, list[str]] = defaultdict(list)
    max_layer = 0
    for c in all_courses:
        lyr = layers.get(c, 0)
        groups[lyr].append(c)
        if lyr > max_layer:
            max_layer = lyr
    for g in groups.values():
        g.sort()

    # ── Layout constants (render at 3x for HiDPI) ──────────────
    S = 3  # supersampling factor
    node_h = 34 * S
    node_w_base = 90 * S
    node_r = 8 * S
    gap_x = 18 * S
    gap_y = 56 * S
    pad_x = 50 * S
    pad_top = 60 * S
    pad_bot = 70 * S  # extra room for multi-row legend
    font_size = 11 * S

    # Dynamic node width based on longest label
    max_chars = max(len(c) for c in all_courses)
    node_w = max(node_w_base, (max_chars * 8 + 28) * S)
    max_row = max(len(g) for g in groups.values())

    img_w = max(600 * S, max_row * (node_w + gap_x) - gap_x + pad_x * 2)
    img_h = pad_top + (max_layer + 1) * (node_h + gap_y) - gap_y + pad_bot

    # ── Node positions ──────────────────────────────────────────
    pos: dict[str, tuple[int, int]] = {}
    for lyr in range(max_layer + 1):
        g = groups.get(lyr, [])
        total_w = len(g) * node_w + (len(g) - 1) * gap_x
        sx = (img_w - total_w) // 2
        for i, c in enumerate(g):
            cx = sx + i * (node_w + gap_x) + node_w // 2
            cy = pad_top + lyr * (node_h + gap_y) + node_h // 2
            pos[c] = (cx, cy)

    # ── Subtree colouring: each root gets a unique hue ──────────
    is_pre_of: set[str] = set()
    for _, pc in rows:
        is_pre_of.add(pc)

    # dependents: root → all courses reachable downward
    dependents: dict[str, list[str]] = defaultdict(list)
    for cc in all_courses:
        if prereqs.get(cc):
            for pc in prereqs[cc]:
                dependents[pc].append(cc)

    roots = sorted(c for c in all_courses if not prereqs.get(c))

    # Palette: 12 visually distinct, saturated colours
    _PALETTE = [
        (10, 142, 110),  # teal
        (64, 86, 227),  # indigo
        (220, 80, 60),  # coral red
        (180, 120, 20),  # amber
        (140, 60, 200),  # purple
        (20, 140, 200),  # sky blue
        (200, 60, 140),  # magenta
        (80, 160, 40),  # green
        (255, 130, 50),  # orange
        (100, 80, 180),  # violet
        (40, 180, 160),  # cyan
        (180, 60, 60),  # brick
    ]

    # Assign each root a colour, then BFS to find its full subtree
    root_color: dict[str, tuple[int, int, int]] = {}
    node_root: dict[str, str] = {}  # course → primary root

    for i, root in enumerate(roots):
        col = _PALETTE[i % len(_PALETTE)]
        root_color[root] = col
        # BFS downward through dependents
        queue = [root]
        visited = {root}
        while queue:
            cur = queue.pop(0)
            if cur not in node_root:
                node_root[cur] = root
            for child in dependents.get(cur, []):
                if child not in visited:
                    visited.add(child)
                    queue.append(child)

    # Edge colour: use the root colour of the prerequisite (source) node
    def edge_rgb(prereq_code: str) -> tuple[int, int, int]:
        root = node_root.get(prereq_code)
        if root:
            return root_color[root]
        return (160, 170, 180)  # fallback grey

    def node_style(c: str):
        """Return (fill, stroke, text_colour, shadow_colour) by role only."""
        has_p = bool(prereqs.get(c))
        is_p = c in is_pre_of
        if not has_p and is_p:
            # Foundation — teal tint
            return (228, 244, 239), (10, 142, 110), (6, 100, 80), (10, 142, 110, 30)
        elif has_p and not is_p:
            # Terminal — indigo tint
            return (232, 235, 252), (86, 104, 220), (48, 64, 180), (86, 104, 220, 30)
        else:
            # Intermediate — neutral white
            return (255, 255, 255), (195, 202, 212), (35, 45, 60), (0, 0, 0, 18)

    # ── Background: subtle vertical gradient ────────────────────
    img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    bg = Image.new("RGBA", (img_w, img_h))
    bg_draw = ImageDraw.Draw(bg)
    # Top: (240, 247, 250)  Bottom: (232, 240, 245)
    for y in range(img_h):
        t = y / max(1, img_h - 1)
        r = int(240 + (232 - 240) * t)
        g_c = int(247 + (240 - 247) * t)
        b = int(250 + (245 - 250) * t)
        bg_draw.line([(0, y), (img_w, y)], fill=(r, g_c, b, 255))
    img = Image.alpha_composite(img, bg)

    # ── Fonts ───────────────────────────────────────────────────
    def _try_font(names, size):
        for name in names:
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        return ImageFont.load_default()

    font_mono = _try_font(
        ["consolab.ttf", "consola.ttf", "cour.ttf", "DejaVuSansMono-Bold.ttf"], font_size
    )
    font_title = _try_font(
        ["calibrib.ttf", "calibri.ttf", "arialbd.ttf", "arial.ttf"], int(font_size * 1.5)
    )
    font_legend = _try_font(["calibri.ttf", "arial.ttf", "segoeui.ttf"], int(font_size * 0.9))

    # ── Pre-compute port offsets so edges fan out across node width ─
    # Count how many edges leave each node (bottom) and enter each node (top)
    out_edges: dict[str, list[str]] = defaultdict(list)  # src → [dst, ...]
    in_edges: dict[str, list[str]] = defaultdict(list)  # dst → [src, ...]
    for cc, pc in rows:
        out_edges[pc].append(cc)
        in_edges[cc].append(pc)

    # Sort each port list by target/source x-position so edges don't cross unnecessarily
    for _src, dsts in out_edges.items():
        dsts.sort(key=lambda d: pos.get(d, (0, 0))[0])
    for _dst, srcs in in_edges.items():
        srcs.sort(key=lambda s: pos.get(s, (0, 0))[0])

    def _port_x(node: str, index: int, count: int, is_out: bool) -> int:
        """Compute x offset for the i-th edge port on a node.
        Spreads ports evenly across 60% of node width."""
        cx = pos[node][0]
        if count <= 1:
            return cx
        usable = node_w * 0.6
        step = usable / (count - 1)
        return int(cx - usable / 2 + index * step)

    # ── Draw edges on a separate RGBA layer (crisp, no blur) ────
    edge_layer = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    edge_draw = ImageDraw.Draw(edge_layer)

    def _bezier(x1, y1, x2, y2, steps=60):
        """Cubic bezier with vertical control points."""
        cy = (y1 + y2) / 2
        pts = []
        for s in range(steps + 1):
            t = s / steps
            u = 1 - t
            bx = u**3 * x1 + 3 * u**2 * t * x1 + 3 * u * t**2 * x2 + t**3 * x2
            by = u**3 * y1 + 3 * u**2 * t * cy + 3 * u * t**2 * cy + t**3 * y2
            pts.append((bx, by))
        return pts

    line_w = max(3, S + S // 2)  # thick coloured edges

    for cc, pc in rows:
        if pc not in pos or cc not in pos:
            continue

        # Compute fanned port positions
        out_idx = out_edges[pc].index(cc)
        out_cnt = len(out_edges[pc])
        in_idx = in_edges[cc].index(pc)
        in_cnt = len(in_edges[cc])

        x1 = _port_x(pc, out_idx, out_cnt, is_out=True)
        y1 = pos[pc][1] + node_h // 2 + 2 * S
        x2 = _port_x(cc, in_idx, in_cnt, is_out=False)
        y2 = pos[cc][1] - node_h // 2 - 2 * S

        # Colour by the subtree of the prerequisite (source) node
        rgb = edge_rgb(pc)
        e_col = (*rgb, 160)
        a_col = (*rgb, 230)

        pts = _bezier(x1, y1, x2, y2, steps=60)
        for i in range(len(pts) - 1):
            edge_draw.line([pts[i], pts[i + 1]], fill=e_col, width=line_w)

        # Arrowhead — larger and more visible
        if len(pts) >= 4:
            px, py = pts[-4]
            ax, ay = pts[-1]
            angle = atan2(ay - py, ax - px)
            sz = 5.5 * S
            lx1 = ax - sz * cos(angle - 0.4)
            ly1 = ay - sz * sin(angle - 0.4)
            lx2 = ax - sz * cos(angle + 0.4)
            ly2 = ay - sz * sin(angle + 0.4)
            edge_draw.polygon([(ax, ay), (lx1, ly1), (lx2, ly2)], fill=a_col)

    # No blur — keep edges crisp
    img = Image.alpha_composite(img, edge_layer)

    # ── Draw nodes ──────────────────────────────────────────────
    draw = ImageDraw.Draw(img)

    # Shadow pass first (all nodes)
    shadow_layer = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)
    shadow_offset = 3 * S
    for c in all_courses:
        p = pos.get(c)
        if not p:
            continue
        cx, cy = p
        _, _, _, sh_col = node_style(c)
        x0 = cx - node_w // 2
        y0 = cy - node_h // 2 + shadow_offset
        x1 = cx + node_w // 2
        y1 = cy + node_h // 2 + shadow_offset
        shadow_draw.rounded_rectangle([x0, y0, x1, y1], radius=node_r, fill=sh_col)
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=4 * S))
    img = Image.alpha_composite(img, shadow_layer)

    # Node bodies
    draw = ImageDraw.Draw(img)
    for c in all_courses:
        p = pos.get(c)
        if not p:
            continue
        cx, cy = p
        fill_c, stroke_c, text_c, _ = node_style(c)
        x0 = cx - node_w // 2
        y0 = cy - node_h // 2
        x1 = cx + node_w // 2
        y1 = cy + node_h // 2

        # Node body with border
        draw.rounded_rectangle(
            [x0, y0, x1, y1],
            radius=node_r,
            fill=(*fill_c, 240),
            outline=(*stroke_c, 180),
            width=max(1, S),
        )

        # Centred text
        bbox = draw.textbbox((0, 0), c, font=font_mono)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text((cx - tw // 2, cy - th // 2), c, fill=(*text_c, 255), font=font_mono)

    # ── Title ───────────────────────────────────────────────────
    title = f"Dependency Graph — {program}"
    bbox = draw.textbbox((0, 0), title, font=font_title)
    tw = bbox[2] - bbox[0]
    draw.text(
        ((img_w - tw) // 2, (pad_top - int(font_size * 1.5)) // 2),
        title,
        fill=(27, 38, 49, 255),
        font=font_title,
    )

    # ── Legend: one entry per root subtree (centred at bottom) ───
    # Build legend items from root courses and their colours
    legend_items = [(root_color[r], r) for r in roots if r in root_color]
    dot_r = 5 * S
    item_gap = 18 * S

    # May need multiple rows if too many roots
    max_legend_w = img_w - pad_x * 2
    legend_rows: list[list[tuple]] = [[]]
    cur_row_w = 0
    for color, label in legend_items:
        bbox = draw.textbbox((0, 0), label, font=font_legend)
        item_w = dot_r * 2 + 6 * S + (bbox[2] - bbox[0]) + item_gap
        if cur_row_w + item_w > max_legend_w and legend_rows[-1]:
            legend_rows.append([])
            cur_row_w = 0
        legend_rows[-1].append((color, label))
        cur_row_w += item_w

    row_height = int(font_size * 1.2)
    ly_start = img_h - pad_bot + 10 * S
    for row_idx, row_items in enumerate(legend_rows):
        # Measure row width for centering
        row_w = 0
        for _color, label in row_items:
            bbox = draw.textbbox((0, 0), label, font=font_legend)
            row_w += dot_r * 2 + 6 * S + (bbox[2] - bbox[0]) + item_gap
        row_w -= item_gap  # no trailing gap

        lx = (img_w - row_w) // 2
        ly = ly_start + row_idx * row_height
        for color, label in row_items:
            draw.ellipse([lx, ly, lx + dot_r * 2, ly + dot_r * 2], fill=(*color, 255))
            draw.text(
                (lx + dot_r * 2 + 6 * S, ly - 2 * S),
                label,
                fill=(80, 90, 100, 255),
                font=font_legend,
            )
            bbox = draw.textbbox((0, 0), label, font=font_legend)
            lx += dot_r * 2 + 6 * S + (bbox[2] - bbox[0]) + item_gap

    # ── Finalise: flatten to RGB, downscale for crisp output ────
    flat = Image.new("RGB", img.size, (240, 247, 250))
    flat.paste(img, mask=img.split()[3])
    final = flat.resize((img_w // S, img_h // S), Image.LANCZOS)
    buf = BytesIO()
    final.save(buf, format="PNG", dpi=(150, 150))
    buf.seek(0)
    return buf


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
            "course_name",
            "programme_term",
            "credit_hours",
        )
    )

    items = [
        {
            "course_code": str(r[0]),
            "course_name": str(r[1] or ""),
            "programme_term": int(r[2]) if r[2] is not None else None,
            "credit_hours": int(r[3]) if r[3] is not None else None,
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
def export_recommendation_debug_xlsx_view(request: HttpRequest) -> HttpResponse:
    """Export recommendation debug report as styled XLSX workbook."""
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

    from core.services.debug_export import export_recommendation_debug_xlsx

    path = export_recommendation_debug_xlsx(payload)
    prog_label = program or "all"
    filename = f"recommendation_debug_{prog_label}_{year}_T{semester}.xlsx"
    response = FileResponse(
        open(path, "rb"),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@role_required(ROLE_ADVISOR)
@require_GET
def export_course_eligibility_csv_view(request: HttpRequest) -> HttpResponseBase:
    """Export course eligibility as a styled XLSX with multiple sheets."""
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

    from core.services.eligibility_export import export_eligibility_xlsx

    path = export_eligibility_xlsx(payload)
    filename = f"eligibility_{course_code}.xlsx"
    return FileResponse(path.open("rb"), as_attachment=True, filename=filename)


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

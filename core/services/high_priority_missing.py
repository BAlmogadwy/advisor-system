from collections import deque
from pathlib import Path
from typing import Any

from core.models import Prerequisite, Student
from core.services.recommender import get_all_department_courses
from core.services.student_helpers import (
    get_student_passed_and_studying,
    get_student_program,
    normalize_code,
)

BASE_DIR = Path(__file__).resolve().parents[2]
RUNTIME_DIR = BASE_DIR / "runtime"


def _get_all_filtered_students(section: str | None, program: str | None, join_year_prefixes: list[str] | None) -> list[int]:
    qs = Student.objects.all()
    if section:
        qs = qs.filter(section=section)
    if program:
        qs = qs.filter(program=program)
    if join_year_prefixes:
        from django.db.models import Q
        parts = Q()
        for p in join_year_prefixes:
            if p:
                low = int(p) * (10 ** (6 - len(p)))
                high = (int(p) + 1) * (10 ** (6 - len(p)))
                parts |= Q(student_id__gte=low, student_id__lt=high)
        if parts:
            qs = qs.filter(parts)
    return list(qs.values_list("student_id", flat=True))


def _build_unlock_graph_for_program(program: str) -> dict[str, set[str]]:
    program = program.strip().upper()
    plan_courses = [normalize_code(r["code"]) for r in get_all_department_courses(program)]
    g: dict[str, set[str]] = {c: set() for c in plan_courses}

    rows = Prerequisite.objects.filter(
        program=program,
    ).values_list("course_code", "prerequisite_course_code")
    for course_code, prereq_cell in rows:
        if not course_code or prereq_cell is None:
            continue
        course_norm = normalize_code(course_code)
        g.setdefault(course_norm, set())
        for p in str(prereq_cell).split(","):
            p_norm = normalize_code(p)
            if not p_norm:
                continue
            g.setdefault(p_norm, set())
            g[p_norm].add(course_norm)
    return g


def _single_source_dist(graph: dict[str, set[str]], source: str) -> dict[str, int]:
    dist = {source: 0}
    q: deque[str] = deque([source])
    while q:
        u = q.popleft()
        for v in graph.get(u, set()):
            if v in dist:
                continue
            dist[v] = dist[u] + 1
            q.append(v)
    return dist


def _compute_priority_scores(graph: dict[str, set[str]], discount: str = "1_over_d") -> dict[str, float]:
    scores: dict[str, float] = {}
    for node in graph.keys():
        dist_map = _single_source_dist(graph, node)
        score = 0.0
        for _, d in dist_map.items():
            if d == 0:
                continue
            if discount == "none":
                score += 1.0
            elif discount == "half_power_d":
                score += 0.5 ** (d - 1)
            else:
                score += 1.0 / d
        scores[node] = score
    return scores


def _build_course_term_map(program: str) -> dict[str, int | None]:
    rows = get_all_department_courses(program)
    out: dict[str, int | None] = {}
    for r in rows:
        c = normalize_code(r.get("code"))
        t = r.get("term")
        out[c] = int(t) if t is not None else None
    return out


def _matches_term_parity(course_term: int | None, term_parity: int) -> bool:
    if course_term is None:
        return False
    desired_remainder = 1 if term_parity == 0 else 0
    return (course_term % 2) == desired_remainder


def _prereqs_visual_style(course: str, program: str) -> set[str]:
    rows = Prerequisite.objects.filter(
        course_code=course,
        program=program,
    ).values_list("prerequisite_course_code", flat=True)
    out: set[str] = set()
    for cell in rows:
        if cell is None:
            continue
        for p in str(cell).split(","):
            n = normalize_code(p)
            if n:
                out.add(n)
    return out


def _is_eligible(course: str, program: str, passed: set[str], studying: set[str], studying_counts_as_passed: bool) -> bool:
    prereqs = _prereqs_visual_style(course, program)
    satisfied = set(passed) | (set(studying) if studying_counts_as_passed else set())
    return prereqs.issubset(satisfied)


def run_missing_high_priority_report(
    year: int,
    semester: int,
    section: str | None,
    program: str | None,
    join_year_prefixes: list[str] | None,
    term_parity: int,
    discount: str,
    min_score: float,
    top_k_per_student: int,
    studying_counts_as_passed: bool,
) -> dict[str, Any]:
    students = _get_all_filtered_students(section, program, join_year_prefixes)
    program_cache: dict[str, dict[str, Any]] = {}
    per_student_grouped: dict[int, dict[str, Any]] = {}

    for sid in students:
        student_program = get_student_program(sid)
        if not student_program:
            continue
        if program and student_program != program:
            continue

        if student_program not in program_cache:
            g = _build_unlock_graph_for_program(student_program)
            scores = _compute_priority_scores(g, discount=discount)
            term_map = _build_course_term_map(student_program)
            universe = set(term_map.keys())
            program_cache[student_program] = {"scores": scores, "term_map": term_map, "universe": universe}

        scores = program_cache[student_program]["scores"]
        term_map = program_cache[student_program]["term_map"]
        universe = program_cache[student_program]["universe"]

        passed, studying = get_student_passed_and_studying(sid)
        passed_n = {normalize_code(x) for x in passed}
        studying_n = {normalize_code(x) for x in studying}

        candidates: list[tuple[str, float]] = []
        for course in universe:
            if course in passed_n or course in studying_n:
                continue
            score = float(scores.get(course, 0.0))
            if score < min_score:
                continue
            if _is_eligible(course, student_program, passed_n, studying_n, studying_counts_as_passed):
                candidates.append((course, score))

        candidates.sort(key=lambda x: x[1], reverse=True)
        in_this: list[tuple[str, float]] = []
        other: list[tuple[str, float]] = []
        for c, s in candidates:
            if _matches_term_parity(term_map.get(c), term_parity):
                in_this.append((c, s))
            else:
                other.append((c, s))

        ordered = (in_this + other)[:top_k_per_student]
        in_this_ordered = [x for x in ordered if x in in_this]
        other_ordered = [x for x in ordered if x in other]

        if in_this_ordered or other_ordered:
            per_student_grouped[int(sid)] = {
                "program": student_program,
                "in_this": in_this_ordered,
                "other": other_ordered,
            }

    items = []
    for sid in sorted(per_student_grouped.keys()):
        row = per_student_grouped[sid]
        in_this_rows = [{"course_code": c, "score": float(s)} for c, s in row.get("in_this", [])]
        other_rows = [{"course_code": c, "score": float(s)} for c, s in row.get("other", [])]
        items.append(
            {
                "student_id": sid,
                "program": row.get("program", ""),
                "missing_this_parity": in_this_rows,
                "missing_other": other_rows,
                "missing_total": len(in_this_rows) + len(other_rows),
            }
        )

    return {
        "count": len(items),
        "params": {
            "year": year,
            "semester": semester,
            "section": section,
            "program": program,
            "join_year_prefixes": join_year_prefixes or [],
            "term_parity": term_parity,
            "discount": discount,
            "min_score": min_score,
            "top_k_per_student": top_k_per_student,
            "studying_counts_as_passed": studying_counts_as_passed,
        },
        "items": items,
    }


def export_missing_high_priority_xlsx(**kwargs: Any) -> Path:
    report = run_missing_high_priority_report(**kwargs)
    out = RUNTIME_DIR / "flagged_students_missing_high_priority.xlsx"
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    try:
        from openpyxl import Workbook  # type: ignore
    except Exception as exc:
        raise RuntimeError("openpyxl is required for XLSX export") from exc

    wb = Workbook()
    ws = wb.active
    ws.title = "Flagged Students"
    ws.append(["student_id", "program", "missing_this_parity", "missing_other", "missing_total"])
    for item in report.get("items", []):
        ws.append([
            item.get("student_id"),
            item.get("program"),
            ";".join(f"{x['course_code']}({x['score']:.2f})" for x in item.get("missing_this_parity", [])),
            ";".join(f"{x['course_code']}({x['score']:.2f})" for x in item.get("missing_other", [])),
            item.get("missing_total"),
        ])
    wb.save(str(out))
    return out

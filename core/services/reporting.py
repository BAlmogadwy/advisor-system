import time
from collections import Counter, defaultdict

from core.models import ElectiveTermMapping, Student, StudentCourse
from core.services.recommender_batch import batch_recommend, batch_recommend_multi_program
from core.services.student_helpers import normalize_code

_aggregate_cache: dict[tuple, tuple[float, tuple[int, "Counter[str]"]]] = {}
_AGGREGATE_CACHE_TTL = 300  # 5 minutes


def clear_aggregate_cache() -> None:
    """Clear cached aggregate recommendation counts."""
    _aggregate_cache.clear()


def get_student_ids(
    program: str | list[str] | None = None,
    section: str | None = None,
) -> list[int]:
    qs = Student.objects.all()
    if program:
        if isinstance(program, list):
            qs = qs.filter(program__in=program)
        else:
            qs = qs.filter(program=program)
    if section:
        qs = qs.filter(section=section)
    return list(qs.values_list("student_id", flat=True))


def build_aggregate_counts(
    year: int,
    semester: int,
    program: str | list[str] | None = None,
    section: str | None = None,
    *,
    resolve_electives: bool = False,
) -> tuple[int, Counter[str]]:
    # Normalize comma-separated program string into a list
    if isinstance(program, str) and "," in program:
        program = [p.strip() for p in program.split(",") if p.strip()]
    # Single-item list → unwrap to string for efficiency
    if isinstance(program, list) and len(program) == 1:
        program = program[0]

    # Normalize program for cache key (lists are not hashable)
    prog_key = tuple(program) if isinstance(program, list) else (str(program),)
    cache_key = (year, semester, prog_key, str(section), bool(resolve_electives))
    cached = _aggregate_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _AGGREGATE_CACHE_TTL:
        return cached[1]

    student_ids = get_student_ids(program=program, section=section)
    aggregate: Counter[str] = Counter()

    # Batch recommender — single program or multi-program
    if program and isinstance(program, str):
        all_recs = batch_recommend(student_ids, program, year, semester)
    else:
        all_recs = batch_recommend_multi_program(student_ids, year, semester)

    if resolve_electives:
        all_recs = resolve_elective_recommendations(
            all_recs,
            year=year,
            semester=semester,
            program=program,
        )

    for recs in all_recs.values():
        aggregate.update(recs)

    result = (len(student_ids), aggregate)
    _aggregate_cache[cache_key] = (time.time(), result)
    # Cap cache size to prevent unbounded memory growth
    if len(_aggregate_cache) > 20:
        oldest = min(_aggregate_cache, key=lambda k: _aggregate_cache[k][0])
        del _aggregate_cache[oldest]
    return result


def resolve_elective_recommendations(
    all_recs: dict[int, list[str]],
    *,
    year: int,
    semester: int,
    program: str | list[str] | None,
) -> dict[int, list[str]]:
    """Replace mapped elective placeholders with eligible real electives.

    The batch recommender intentionally emits plan placeholders such as DS2.
    Section planning needs the deliverable course demand instead, so it uses
    the term mapping table to expand DS2 into courses such as DS485.
    """
    if not all_recs:
        return all_recs

    student_ids = list(all_recs.keys())
    student_programs = {
        int(sid): str(prog)
        for sid, prog in Student.objects.filter(student_id__in=student_ids).values_list(
            "student_id", "program"
        )
        if prog
    }

    if isinstance(program, str):
        programmes = [program]
    elif isinstance(program, list):
        programmes = list(program)
    else:
        programmes = sorted(set(student_programs.values()))

    if not programmes:
        return all_recs

    mappings: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for mapping in (
        ElectiveTermMapping.objects.filter(
            academic_year=str(year),
            term=semester,
            programme__in=programmes,
        )
        .select_related("elective")
        .order_by("programme", "placeholder_code", "elective__course_code")
    ):
        mappings[str(mapping.programme)][normalize_code(mapping.placeholder_code)].append(
            mapping.elective
        )

    if not mappings:
        return all_recs

    sc_qs = StudentCourse.objects.filter(student_id__in=student_ids).select_related("course")
    passed: dict[int, set[str]] = defaultdict(set)
    studying: dict[int, set[str]] = defaultdict(set)
    for sc in sc_qs:
        code = normalize_code(sc.course.course_code)
        if sc.status == "passed":
            passed[int(sc.student_id)].add(code)
        elif sc.status == "studying":
            studying[int(sc.student_id)].add(code)

    resolved: dict[int, list[str]] = {}
    assignment_count: Counter[str] = Counter()

    for sid, recs in all_recs.items():
        student_programme = program if isinstance(program, str) else student_programs.get(int(sid))
        programme_mappings = mappings.get(str(student_programme), {})
        if not programme_mappings:
            resolved[sid] = recs
            continue

        student_passed = passed.get(int(sid), set())
        student_studying = studying.get(int(sid), set())
        student_resolved: list[str] = []

        for code in recs:
            norm = normalize_code(code)
            electives = programme_mappings.get(norm)
            if not electives:
                student_resolved.append(code)
                continue

            eligible = []
            for elective in electives:
                prereqs = [
                    normalize_code(part)
                    for part in str(elective.prerequisites_csv or "").split(",")
                    if part.strip()
                ]
                if all(req in student_passed or req in student_studying for req in prereqs):
                    eligible.append(normalize_code(elective.course_code))

            if eligible:
                pick = min(eligible, key=lambda c: (assignment_count[c], c))
                assignment_count[pick] += 1
                student_resolved.append(pick)

        resolved[sid] = student_resolved

    return resolved

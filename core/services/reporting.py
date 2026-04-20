import time
from collections import Counter

from core.models import Student
from core.services.recommender_batch import batch_recommend, batch_recommend_multi_program

_aggregate_cache: dict[tuple, tuple[float, tuple[int, "Counter[str]"]]] = {}
_AGGREGATE_CACHE_TTL = 300  # 5 minutes


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
) -> tuple[int, Counter[str]]:
    # Normalize comma-separated program string into a list
    if isinstance(program, str) and "," in program:
        program = [p.strip() for p in program.split(",") if p.strip()]
    # Single-item list → unwrap to string for efficiency
    if isinstance(program, list) and len(program) == 1:
        program = program[0]

    # Normalize program for cache key (lists are not hashable)
    prog_key = tuple(program) if isinstance(program, list) else (str(program),)
    cache_key = (year, semester, prog_key, str(section))
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

    for recs in all_recs.values():
        aggregate.update(recs)

    result = (len(student_ids), aggregate)
    _aggregate_cache[cache_key] = (time.time(), result)
    # Cap cache size to prevent unbounded memory growth
    if len(_aggregate_cache) > 20:
        oldest = min(_aggregate_cache, key=lambda k: _aggregate_cache[k][0])
        del _aggregate_cache[oldest]
    return result

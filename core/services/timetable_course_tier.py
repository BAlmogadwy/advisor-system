"""Course-tier classification for the tiered lexicographic objective.

The registrar's resolution priority is not flat: a student *must* get a
specialised major course, but a general-education requirement they can pick
up in another section university-wide should never wreck everyone's
timetable. This module maps each course to one of three tiers:

- ``T1`` — specialised major courses (AI/CS/COE/CYB/DS/IS core). Resolution
  is hard; never traded for schedule quality.
- ``T2`` — shared foundations (MATH/STAT by prefix, plus any course required
  by more than two programme plans, e.g. CS111/CS112 intro programming).
  A small unresolved count per course is tolerated before it turns near-hard.
- ``T3`` — general-education & free electives (ENGL/GS/GSE/FE). Soft: resolved
  only when it costs no schedule quality.

The classifier is split so it can run ~10^4 times per optimise click without a
DB hit: :func:`classify_course_tier` is pure, and :func:`program_count_by_code`
caches the one small global read of ``ProgrammeRequirement``. The per-run map
that the evaluator consumes is built once by
``build_course_tier_map_for_scenario`` in ``timetable_optimizer_v2``.
"""

from __future__ import annotations

from collections import defaultdict

from core.models import ProgrammeRequirement
from core.services.student_helpers import normalize_code

# ``str.startswith`` accepts a tuple. GS/GSE/FE/ENGL are all T3, so their
# overlap (GSE starts with GS) is harmless. No T1/T2-by-count prefix collides
# with these.
T3_PREFIXES = ("ENGL", "GS", "GSE", "FE")
T2_PREFIXES = ("MATH", "STAT")
DEFAULT_TIER = "T1"

# Number of distinct programme plans above which a course is considered a
# widely-shared "service" course (Tier-2) rather than a specialised one.
SHARED_PLAN_THRESHOLD = 2


def classify_course_tier(
    bare_code: str, distinct_program_count: int, default: str = DEFAULT_TIER
) -> str:
    """Return ``"T1"``/``"T2"``/``"T3"`` for a *bare* course code. Pure, no DB.

    Prefix rules win over the plan-count rule: ``MATH471`` sits in only two
    plans (count alone => T1) but its MATH prefix pins it to T2. ``default``
    (T1) applies to codes absent from ``ProgrammeRequirement`` (count <= 0) —
    the least-shared courses, which the low tier matches.
    """
    code = normalize_code(bare_code)
    if code.startswith(T3_PREFIXES):
        return "T3"
    if code.startswith(T2_PREFIXES):
        return "T2"
    if distinct_program_count <= 0:
        return default
    return "T2" if distinct_program_count > SHARED_PLAN_THRESHOLD else "T1"


def program_count_by_code() -> dict[str, int]:
    """Distinct programme plans requiring each course, keyed by normalised code.

    GLOBAL, not scenario-scoped — the "service course" signal counts across
    all programmes (CS111 is shared by 11 plans regardless of which scenario
    is being solved). Counting distinct programmes per *normalised* code in
    Python (rather than a SQL ``GROUP BY`` on the raw string) keeps dirty
    case/spacing imports from splitting a course into two under-counted
    buckets.

    **Deliberately NOT memoised.** This reads the small ``ProgrammeRequirement``
    table once per :func:`~core.services.timetable_optimizer_v2.build_course_tier_map_for_scenario`
    call — i.e. once per optimise run, NOT per evaluation (the hot loop reads
    the returned dict), so the query is negligible. A previous ``lru_cache``
    here was a correctness bug: the cache is process-local while production
    runs multiple gunicorn workers, so after a curriculum write only the
    worker that served the write invalidated. The other worker kept stale plan
    counts for its whole lifetime and therefore computed a DIFFERENT tier map
    — two workers disagreeing on whether a course is T1 or T2 makes the board
    reconstruct differently depending on which process answers the request,
    which is exactly the invariant the tiered objective depends on. Read it
    fresh; if this ever becomes hot, use a shared (cross-worker) cache keyed by
    a stamp the write paths bump, never a per-process one.
    """
    by: dict[str, set[str]] = defaultdict(set)
    for prog, code in ProgrammeRequirement.objects.values_list("program", "course_code"):
        by[normalize_code(code)].add(str(prog).strip())
    return {c: len(p) for c, p in by.items()}

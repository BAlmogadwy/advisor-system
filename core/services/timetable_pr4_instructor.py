"""PR4 — instructor realism plumbing.

Three public symbols:

- ``normalise_instructor(raw: str | None) -> str | None`` — strip + casefold
  normalisation. **Opaque-string discipline (DoR A6):** no delimiter
  parsing, no comma-splitting, no heuristics. The raw instructor text on
  ``TermSectionMeeting.instructor`` is treated as a single opaque id.
  A value like ``"Dr. Smith / Dr. Jones"`` normalises to one id, not two.
  Whitespace-only or empty input → ``None``.

- ``build_instructor_schedule(board_id: int) -> dict[str, set[tuple[str, int]]]``
  — per-run roll-up from the board's scenario. Walks ``TermSectionMeeting``
  rows for sections attached to this board's scenario, groups them by
  normalised instructor → set of ``(day, start_minute)`` tuples. The
  returned dict is transient (built per run, never persisted).

- ``is_instructor_clash_enabled() -> bool`` + ``INSTRUCTOR_CLASH_FLAG_SETTING``
  — flag helpers, mirroring the PR3 flag pattern. Default ``False`` through
  commits 2–7; flipped to ``True`` at the commit-8 promotion.

Commit 2 lands these helpers without wiring them into the planner. Commit 3
wires the flag-gated emission of the ``INSTRUCTOR_CLASH`` rejection code
inside ``auto_place_board``'s candidate-scoring loop.

A6 data scan (2026-04-20): 245 meeting rows, 46 unique instructor strings,
zero Latin multi-instructor delimiters observed. Data is clean → single-
string semantics is appropriate. If a future dataset contains delimiter
patterns, the normaliser stays as-is (per A6) and a follow-up commit is
scoped to extend it, not inline parsing changes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from django.conf import settings

from core.services import timetable_constraints as _constraints
from core.services.course_identity import course_display_code, programmes_serving_course

INSTRUCTOR_CLASH_FLAG_SETTING = "TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED"


def normalise_instructor(raw: str | None) -> str | None:
    """Strip + casefold the raw instructor string. Empty / whitespace-only
    / ``None`` all collapse to ``None`` so callers can check identity
    without branching on empty strings.

    Per A6: NO delimiter parsing. ``"Dr. Smith / Dr. Jones"`` normalises
    to ``"dr. smith / dr. jones"`` — one opaque id, not two. This is
    deliberate scope discipline; multi-instructor parsing is a data
    problem we have not scoped for PR4.
    """
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    return stripped.casefold()


def is_instructor_clash_enabled() -> bool:
    """Reads ``TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED`` from Django
    settings. Default ``False`` until commit 8's promotion."""
    return bool(getattr(settings, INSTRUCTOR_CLASH_FLAG_SETTING, False))


INSTRUCTOR_LINKS_FLAG_SETTING = "TIMETABLE_INSTRUCTOR_LINKS_ENABLED"


def is_instructor_links_enabled() -> bool:
    """Reads ``TIMETABLE_INSTRUCTOR_LINKS_ENABLED``. Default ``False``.

    Gates whether the planner clash sources instructor identity from structured
    ``CourseInstructor`` assignments (per-person, multi-instructor) instead of the
    legacy single opaque free-text string. Independent of the PR4 clash flag:
    PR4 gates *whether the filter runs*; this gates *where ids come from*.
    """
    return bool(getattr(settings, INSTRUCTOR_LINKS_FLAG_SETTING, False))


INSTRUCTOR_GAP_PENALTY_FLAG_SETTING = "TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED"


def is_instructor_gap_penalty_enabled() -> bool:
    """Reads ``TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED``. Default ``False``.

    Gates the soft objective that minimises idle gaps in each instructor's daily
    schedule. When True, the canonical evaluator appends a lowest-priority
    ``instructor_idle_minutes`` term (strictly below every student + reserve term,
    so it never trades away a student outcome) and the greedy scorer prefers
    placements that compact an instructor's day. Off → the score tuple is the
    unchanged 6-element shape (byte-identical to pre-feature behaviour).
    """
    return bool(getattr(settings, INSTRUCTOR_GAP_PENALTY_FLAG_SETTING, False))


INSTRUCTOR_DAILY_CAP_FLAG_SETTING = "TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED"
INSTRUCTOR_DAILY_CAP_SETTING = "TIMETABLE_INSTRUCTOR_DAILY_CAP"


def is_instructor_daily_cap_enabled() -> bool:
    """Reads ``TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED``. Default ``False``.

    Gates the HARD constraint capping the number of scheduled sessions (lectures
    AND labs both count) an instructor may teach on any single day. Unlike the
    gap penalty this is NOT a soft score term — it is enforced structurally at
    candidate generation in every solver stage (a 4th same-day session is never
    offered) and never touches the lexicographic tuple, so flag-off output is
    byte-identical to before.
    """
    return bool(getattr(settings, INSTRUCTOR_DAILY_CAP_FLAG_SETTING, False))


def get_instructor_daily_cap() -> int:
    """Reads ``TIMETABLE_INSTRUCTOR_DAILY_CAP``. Default ``3``."""
    return int(getattr(settings, INSTRUCTOR_DAILY_CAP_SETTING, 3))


INSTRUCTOR_COMPACTION_FLAG_SETTING = "TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED"


def is_instructor_compaction_enabled() -> bool:
    """Read ``TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED`` (configured default on).

    Gates the post-build instructor-day compaction pass (shrinks within-day idle
    gaps by relocating an instructor's sessions in time). Explicit false → no-op.
    """
    return bool(getattr(settings, INSTRUCTOR_COMPACTION_FLAG_SETTING, False))


def get_instructor_compaction_config() -> dict:
    """Tunables for the compaction pass (env-overridable, validated defaults)."""
    return {
        "gap_budget": float(getattr(settings, "TIMETABLE_INSTRUCTOR_COMPACTION_GAP_BUDGET", 0.03)),
        "per_student_cap": int(
            getattr(settings, "TIMETABLE_INSTRUCTOR_COMPACTION_PER_STUDENT_CAP", 75)
        ),
        "trade_ratio": float(getattr(settings, "TIMETABLE_INSTRUCTOR_COMPACTION_TRADE_RATIO", 2.0)),
        "max_rounds": int(getattr(settings, "TIMETABLE_INSTRUCTOR_COMPACTION_MAX_ROUNDS", 40)),
        "time_budget": float(
            getattr(settings, "TIMETABLE_INSTRUCTOR_COMPACTION_TIME_BUDGET_SECONDS", 20.0)
        ),
    }


def is_instructor_day_trade_active() -> bool:
    """True when the bounded instructor-day trade is switched on AND can act.

    The trade ranks campus working days, which only the instructor-first policy
    does, so ``TIMETABLE_INSTRUCTOR_DAY_TRADE_ENABLED`` alone is not enough. The
    compaction pass and the run-level idle gate must share this one condition,
    or the gate could excuse idle the pass never traded for.
    """
    from core.services.timetable_optimisation_policy import is_instructor_priority_enabled

    return bool(
        getattr(settings, "TIMETABLE_INSTRUCTOR_DAY_TRADE_ENABLED", False)
        and is_instructor_priority_enabled()
    )


def get_instructor_day_trade_config() -> dict:
    """Bounded student-gap-for-instructor-day trade (default OFF).

    ``total_gap`` is the cumulative net student gap-minutes (vs the pre-pass
    board) that day-saving moves may add; ``per_student`` / ``high_risk_per_student``
    cap what any one student / any tier-A or tier-B student may gain;
    ``idle_allowance`` is the idle minutes the merged day may cost per day saved.
    """
    return {
        "enabled": bool(getattr(settings, "TIMETABLE_INSTRUCTOR_DAY_TRADE_ENABLED", False)),
        "total_gap": int(getattr(settings, "TIMETABLE_INSTRUCTOR_DAY_TRADE_TOTAL_GAP", 300)),
        "per_student": int(getattr(settings, "TIMETABLE_INSTRUCTOR_DAY_TRADE_PER_STUDENT", 500)),
        "high_risk_per_student": int(
            getattr(settings, "TIMETABLE_INSTRUCTOR_DAY_TRADE_HIGH_RISK_PER_STUDENT", 500)
        ),
        "idle_allowance": int(
            getattr(settings, "TIMETABLE_INSTRUCTOR_DAY_TRADE_IDLE_ALLOWANCE", 150)
        ),
        "reserve_allowance": int(
            getattr(settings, "TIMETABLE_INSTRUCTOR_DAY_TRADE_RESERVE_ALLOWANCE", 0)
        ),
        "room_baseline_seconds": float(
            getattr(settings, "TIMETABLE_INSTRUCTOR_DAY_TRADE_ROOM_BASELINE_SECONDS", 5.0)
        ),
        "room_candidate_seconds": float(
            getattr(settings, "TIMETABLE_INSTRUCTOR_DAY_TRADE_ROOM_CANDIDATE_SECONDS", 0.5)
        ),
        "vacate_days": bool(getattr(settings, "TIMETABLE_INSTRUCTOR_DAY_TRADE_VACATE_DAYS", True)),
        "vacate_max_sessions": int(
            getattr(settings, "TIMETABLE_INSTRUCTOR_DAY_TRADE_VACATE_MAX_SESSIONS", 2)
        ),
        "vacate_top_n": int(getattr(settings, "TIMETABLE_INSTRUCTOR_DAY_TRADE_VACATE_TOP_N", 12)),
    }


def exceeds_instructor_daily_cap(sections_by_id, section_instructor_ids, cap: int) -> bool:
    """True if any (instructor, day) would hold more than ``cap`` sessions.

    Delegates to the constraint engine (single source for the rule). Duck-typed
    over the in-memory section states; a section taught by N instructors counts
    toward each. Re-exported here under its historical name for existing
    callers."""
    return _constraints.exceeds_instructor_daily_cap(sections_by_id, section_instructor_ids, cap)


def count_instructor_daily_overloads(sections_by_id, section_instructor_ids, cap: int) -> int:
    """Total over-cap sessions = Σ over (instructor, day) of ``max(0, count-cap)``.

    Delegates to the constraint engine. A side-band diagnostic (not part of the
    lexicographic score); 0 means every instructor-day is within the cap. Used
    for the evaluator's ``instructor_overload_count`` and the repair accept gate.
    """
    return _constraints.count_instructor_daily_overloads(
        sections_by_id, section_instructor_ids, cap
    )


def has_instructor_clash(sections_by_id, section_instructor_ids) -> bool:
    """True if any instructor is double-booked — two sessions of different
    sections whose times OVERLAP on the same day. Distinct from the daily cap
    (which limits sessions/day): a clash is two-at-the-same-TIME.

    Delegates to the interval-aware engine (``timetable_constraints``) — a
    10:30-11:45 lecture and a 10:45-12:25 lab are a clash even though their
    start minutes differ. Re-exported here under its historical name for the
    existing call sites."""
    return _constraints.has_instructor_clash(sections_by_id, section_instructor_ids)


def count_instructor_clashes(sections_by_id, section_instructor_ids) -> int:
    """Number of overlapping cross-section window pairs across all instructors —
    0 means clash-free. Interval-aware (see ``timetable_constraints``). A
    monotonic repair signal (fewer is better)."""
    return _constraints.count_instructor_clashes(sections_by_id, section_instructor_ids)


def section_instructor_resolver(programs, gender: str) -> Callable[[object, object], set[int]]:
    """Return ``resolve(course_key, display_code) -> {instructor_id, ...}``.

    The one place a section's instructors are read from the scenario-independent
    ``CourseInstructor`` assignments: active instructors only (a deactivated
    person drops out of the clash, matching the pickers), for ``gender``, and
    only from the programmes that teach ``course_key`` itself — a link is keyed
    by ``(programme, code)`` and a display code is not a course identity (see
    ``programmes_serving_course``). Empty when ``gender`` is empty: the
    assignment model is gender-scoped.
    """
    return _course_instructor_links(programs, gender)[0]


def _course_instructor_links(
    programs, gender: str
) -> tuple[Callable[[object, object], set[int]], dict[int, str]]:
    """:func:`section_instructor_resolver` and the linked instructors' names, by
    id, read in the same query (the active-instructor filter joins them
    already). Two queries with a gender, none without."""
    from core.models import CourseInstructor

    if not gender:
        return (lambda _course_key, _display_code: set()), {}
    programs = [p for p in programs if p]
    serving = programmes_serving_course(programs)

    # (program, normalised course_code) -> {instructor_id}
    by_course: dict[tuple[str, str], set[int]] = {}
    names: dict[int, str] = {}
    for prog, code, instructor_id, full_name in CourseInstructor.objects.filter(
        program__in=programs, section=gender, instructor__is_active=True
    ).values_list("program", "course_code", "instructor_id", "instructor__full_name"):
        by_course.setdefault((prog, (code or "").strip().upper()), set()).add(instructor_id)
        names[instructor_id] = full_name

    def resolve(course_key: object, display_code: object) -> set[int]:
        norm = str(display_code or "").strip().upper()
        ids: set[int] = set()
        for prog in serving(course_key):
            ids |= by_course.get((prog, norm), set())
        return ids

    return resolve, names


def build_section_instructor_ids(scenario, *, incomplete: str = "raise") -> dict[str, set[int]]:
    """``{"course_key|section" -> {instructor_id, ...}}`` resolved from the
    scenario-independent ``CourseInstructor`` assignments.

    Each of the scenario's ``TermSection``s is matched to its course's
    instructors for the scenario's gender, through
    ``section_instructor_resolver``. Sections with no course assignment are
    absent — the caller falls back to the opaque free-text string for those.

    With one teacher per section (``is_one_teacher_active``) each section maps
    to its ONE teacher instead (``section_teacher_assignment``): a section
    whose teacher is still to be decided is present with an EMPTY set, which
    charges nobody and suppresses the free-text fallback; a section with no
    links, or a frozen section with no known teacher, stays absent. The map
    also covers the teacher-frozen sections placed on the scenario's boards.
    ``incomplete`` is ``load_assignment``'s: an engine raises on an English
    section without one complete weekly pattern; the board's checks
    (``"collect"``) leave it absent, so its meeting name decides.
    """
    from core.services.timetable_flags import is_one_teacher_active

    if is_one_teacher_active():
        from core.services.section_teacher_assignment import load_assignment

        return load_assignment(scenario.id, incomplete=incomplete).pr4_map()

    return linked_section_instructor_ids(scenario).ids


class LinkedSectionInstructors(NamedTuple):
    """:func:`linked_section_instructor_ids`'s answer."""

    #: ``{"course_key|section" -> {instructor_id, ...}}``, every teacher linked
    #: to a section's course: :func:`build_section_instructor_ids`'s map with
    #: one teacher per section off.
    ids: dict[str, set[int]]
    #: The resolver it was read with (:func:`section_instructor_resolver`).
    resolve: Callable[[object, object], set[int]]
    #: Every linked instructor's name, by id.
    names: dict[int, str]


def linked_section_instructor_ids(scenario) -> LinkedSectionInstructors:
    """:func:`build_section_instructor_ids` with one teacher per section off --
    every teacher linked to a section's course -- with the resolver it read
    them with and their names.

    ``resolve(course_key, display_code)`` is who a section of a course not
    created yet will be charged with once it is (the map has only the
    scenario's ``TermSection`` rows): the slot verdict's planned section.
    Three queries with a gender (programme requirements, course links with
    their instructors' names, sections), none without.
    """
    from core.models import TermSection

    out: dict[str, set[int]] = {}
    gender = getattr(scenario, "gender", "")
    resolve, names = _course_instructor_links(list(getattr(scenario, "programs", []) or []), gender)
    if not gender:
        return LinkedSectionInstructors(out, resolve, names)

    for course_key, course_code, course_number, section in TermSection.objects.filter(
        scenario=scenario
    ).values_list("course_key", "course_code", "course_number", "section"):
        ids = resolve(course_key, course_display_code(course_key, course_code, course_number))
        if ids:
            out[f"{course_key}|{section}"] = ids
    return LinkedSectionInstructors(out, resolve, names)


def build_instructor_schedule(board_id: int) -> dict[str, set[tuple[str, int]]]:
    """Build the per-run instructor → slot-set roll-up.

    Walks ``TermSectionMeeting`` rows for sections in the given board's
    scenario. Meetings with empty or unparseable ``instructor`` /
    ``start_time`` are silently skipped. Returns a dict keyed by
    normalised instructor id → ``{(day_upper, start_minute)}``.

    The dict is transient (built per run, never persisted). Commit 3's
    emission path reads this to decide whether a candidate slot would
    double-book an instructor.
    """
    from core.models import DeliveryBoard, TermSectionMeeting

    board = DeliveryBoard.objects.get(id=board_id)
    scenario_id = board.scenario_id

    meetings = (
        TermSectionMeeting.objects.filter(term_section__scenario_id=scenario_id)
        .exclude(instructor="")
        .values_list("day", "start_time", "instructor")
    )

    schedule: dict[str, set[tuple[str, int]]] = {}
    for day, start_time, instructor in meetings:
        normalised = normalise_instructor(instructor)
        if normalised is None:
            continue
        try:
            hh_str, mm_str = start_time.split(":", 1)
            start_minute = int(hh_str) * 60 + int(mm_str)
        except (ValueError, AttributeError):
            continue
        key = ((day or "").upper(), start_minute)
        schedule.setdefault(normalised, set()).add(key)
    return schedule

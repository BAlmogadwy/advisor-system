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

from django.conf import settings

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
    """Reads ``TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED``. Default ``False``.

    Gates the post-build instructor-day compaction pass (shrinks within-day idle
    gaps by relocating an instructor's sessions in time). Default OFF → no-op.
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


def exceeds_instructor_daily_cap(sections_by_id, section_instructor_ids, cap: int) -> bool:
    """True if any (instructor, day) would hold more than ``cap`` sessions.

    Pure/duck-typed over the in-memory section states: ``sections_by_id`` maps
    ``section_id -> SectionState`` and ``section_instructor_ids`` maps the SAME
    ``section_id -> {instructor_id}``. Each section's ``meetings`` contribute one
    session per ``(instructor_id, meeting.day)`` (``day`` is the stage's native
    representation — int 0-4 for SectionState). Early-exits on the first breach,
    so it is cheap enough to call inside a move-evaluation loop. A section taught
    by N instructors counts toward each of those N.
    """
    if not section_instructor_ids:
        return False
    counts: dict[tuple[int, object], int] = {}
    for section_id, instr_ids in section_instructor_ids.items():
        sec = sections_by_id.get(section_id)
        if sec is None:
            continue
        for iid in instr_ids:
            for meeting in sec.meetings:
                key = (iid, meeting.day)
                nxt = counts.get(key, 0) + 1
                if nxt > cap:
                    return True
                counts[key] = nxt
    return False


def count_instructor_daily_overloads(sections_by_id, section_instructor_ids, cap: int) -> int:
    """Total over-cap sessions = Σ over (instructor, day) of ``max(0, count-cap)``.

    A side-band diagnostic (not part of the lexicographic score): 0 means every
    instructor-day is within the cap. Used for the evaluator's
    ``instructor_overload_count`` attribute and the repair pass's accept gate.
    """
    if not section_instructor_ids:
        return 0
    counts: dict[tuple[int, object], int] = {}
    for section_id, instr_ids in section_instructor_ids.items():
        sec = sections_by_id.get(section_id)
        if sec is None:
            continue
        for iid in instr_ids:
            for meeting in sec.meetings:
                key = (iid, meeting.day)
                counts[key] = counts.get(key, 0) + 1
    return sum(max(0, c - cap) for c in counts.values())


def has_instructor_clash(sections_by_id, section_instructor_ids) -> bool:
    """True if any instructor is double-booked — two sessions occupying the same
    ``(day, start_min)``. Distinct from the daily cap (which limits sessions/day):
    a clash is two-at-the-same-TIME, physically impossible. Early-exits on the
    first clash so it is cheap inside a move-evaluation loop. Cross-course (an
    instructor teaching two different courses at once) is exactly what this
    catches — the same-course rule does not, since the courses differ."""
    if not section_instructor_ids:
        return False
    seen: set[tuple[object, int, int]] = set()
    for section_id, instr_ids in section_instructor_ids.items():
        sec = sections_by_id.get(section_id)
        if sec is None:
            continue
        for iid in instr_ids:
            for meeting in sec.meetings:
                key = (iid, meeting.day, meeting.start_min)
                if key in seen:
                    return True
                seen.add(key)
    return False


def count_instructor_clashes(sections_by_id, section_instructor_ids) -> int:
    """Number of extra sessions stacked on an already-occupied (instructor, day,
    start) slot — 0 means clash-free. Side-band diagnostic / repair signal."""
    if not section_instructor_ids:
        return 0
    counts: dict[tuple[object, int, int], int] = {}
    for section_id, instr_ids in section_instructor_ids.items():
        sec = sections_by_id.get(section_id)
        if sec is None:
            continue
        for iid in instr_ids:
            for meeting in sec.meetings:
                key = (iid, meeting.day, meeting.start_min)
                counts[key] = counts.get(key, 0) + 1
    return sum(c - 1 for c in counts.values() if c > 1)


def build_section_instructor_ids(scenario) -> dict[str, set[int]]:
    """``{"course_key|section" -> {instructor_id, ...}}`` resolved from the
    scenario-independent ``CourseInstructor`` assignments.

    Each of the scenario's ``TermSection``s is matched to the course's
    instructors for the scenario's gender + program(s). Only active instructors
    are included (a deactivated person drops out of the clash, matching the
    pickers). Sections with no course assignment are absent — the caller falls
    back to the opaque free-text string for those.
    """
    from core.models import CourseInstructor, TermSection

    out: dict[str, set[int]] = {}
    gender = getattr(scenario, "gender", "")
    if not gender:
        return out
    programs = list(getattr(scenario, "programs", []) or [])

    # (program, normalised course_code) -> {instructor_id}
    by_course: dict[tuple[str, str], set[int]] = {}
    for prog, code, instructor_id in CourseInstructor.objects.filter(
        program__in=programs, section=gender, instructor__is_active=True
    ).values_list("program", "course_code", "instructor_id"):
        by_course.setdefault((prog, (code or "").strip().upper()), set()).add(instructor_id)

    for course_key, course_code, section in TermSection.objects.filter(
        scenario=scenario
    ).values_list("course_key", "course_code", "section"):
        norm = (course_code or "").strip().upper()
        ids: set[int] = set()
        for prog in programs:
            ids |= by_course.get((prog, norm), set())
        if ids:
            out[f"{course_key}|{section}"] = ids
    return out


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

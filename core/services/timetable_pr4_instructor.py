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

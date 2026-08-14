"""Timetable alternatives for a student to choose between.

The scheduling is not here. `planner_builder.build_plans` does that, and this module
calls it with the same arguments `build_my_timetable` already uses — identity clamped
to the caller, cohort resolved strictly, credits read from the programme rather than
accepted from a client. What changes is the ending.

`build_my_timetable` closes with `max(options, key=scheduled)`: it considers nine
alternatives and reports one. That is right for a chat answer, where a list of nine
timetables is unreadable. It is wrong for a screen whose entire purpose is the student
comparing alternatives and picking the one that suits them — the nine already exist and
are thrown away one line before the return.

So this module keeps them, removes the duplicates the builder cannot avoid producing,
and attaches the few facts a person needs to tell one from another. It computes nothing
academic: days, earliest and latest come from the meetings the builder already returned,
which is arithmetic over its output, not a second opinion about scheduling.

Deliberately absent, because the data cannot honestly support them: gaps between
classes, rooms and buildings, lecture-versus-laboratory labels, online-versus-in-person,
and any statement about seats. A screen that ranks timetables by a number the student
did not ask for is also absent — the ordering here is stable, not a recommendation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from core.models import ProgrammeRequirement, Student
from core.services.recommender import recommend_next_courses
from core.services.student_helpers import normalize_code
from core.services.student_sections import (
    UnknownStudentGender,
    get_student_term_baseline,
    section_is_available_to_student,
    student_gender_strict,
)
from core.services.virtual_advisor_capabilities import _translate_unplaced

#: A course with no credit hours recorded still has to be given a weight, or the
#: builder's credit cap becomes meaningless. Matches the value the chat capability
#: already uses, so the two paths cannot disagree about the same student.
DEFAULT_CREDITS = 3

#: The domain name for the retain-or-rebuild choice. Four names existed for it —
#: `keep_registered` in the solver, `mode: keep|ignore` at the staff HTTP boundary,
#: `keep_current_sections` in the tool schema, `keep_current` here — and a toggle
#: whose name changes at every layer is one refactor away from the UI and the
#: executor disagreeing about which way it points.
KEEP_CURRENT_SECTIONS = "keep_current_sections"

#: Sunday-first, which is how the week reads here.
DAY_ORDER = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")


class PlannerUnavailable(Exception):
    """The request cannot be answered, and guessing would be worse than refusing."""


@dataclass(frozen=True)
class PlannerRequest:
    """Everything the builder needs, all of it derived server-side.

    Frozen, and holding no client-supplied academic input on purpose: a caller may
    say WHICH courses to try, and nothing else. Credits, cohort, baseline and the
    recommendation come from the student's own record.
    """

    student_id: int
    year: int
    term: int
    # Soft candidates: the builder may omit these when they do not fit.  This
    # historical field used to be the only course list despite its hard-sounding
    # name, so changing its meaning would silently make every recommendation a
    # hard constraint for existing callers.
    must_include: tuple[str, ...] = ()
    # True hard requirements.  Every returned alternative must contain each of
    # these courses (or retain it from the baseline in keep-current mode).
    required_courses: tuple[str, ...] = ()
    keep_current_sections: bool = True
    max_credits: int = 0
    # Draft-backed screens already materialise the recommendation into
    # ``course_codes`` when the workspace is created.  They set this to False so
    # removing a recommended course on screen really removes it; callers that ask
    # the service to fill an otherwise partial request may keep the historical
    # behaviour by leaving it True.
    include_recommendations: bool = True
    #: {"AI221": 4213} — courses whose section the student pinned. A course absent
    #: from here is one the planner chooses freely, which is the default and the
    #: common case.
    fixed_sections: tuple[tuple[str, int], ...] = ()
    #: Server-only evaluation seam. ``None`` loads the student's stored term
    #: snapshot as usual; a tuple (including an empty tuple) is the exact
    #: read-only baseline the caller wants the canonical Planner adapter to
    #: evaluate.  This is used by course-replacement certification after removing
    #: one current course in memory.  No HTTP/client field maps to it.
    baseline_override: tuple[dict[str, Any], ...] | None = None
    #: Server-only credit evidence for a concrete course that is not itself a
    #: programme-requirement row (for example an approved elective choice). The
    #: ordinary planner continues to derive every credit value from the programme.
    course_credits_override: tuple[tuple[str, int], ...] = ()
    #: Server-only proof mode. When true, a section with missing, partial, or
    #: malformed meeting data is unavailable rather than appearing clash-free.
    require_complete_meetings: bool = False


def run_solver(
    *,
    year: str,
    term: str,
    shortlist: list[dict[str, Any]],
    baseline: list[dict[str, Any]],
    keep_current_sections: bool,
    max_credits: int,
    gender: str,
    program: str | None = None,
    require_complete_meetings: bool = False,
) -> dict[str, Any]:
    """THE one place student-facing code reaches the solver.

    Public, and called by the chat capability as well as by this module. It was
    private, which meant the capability could not use it and duplicated the whole
    call instead — two functions each commented as the sole translation point.

    Two vocabularies meet here and nowhere else. `keep_current_sections` is the
    domain name — what the student is choosing, and what the toggle, the tool
    schema and the draft all call it. `keep_registered` is the solver's own
    parameter, and it is deliberately not renamed there: read aloud it sounds like
    "keep it registered", which in a product that never registers anything is
    exactly the wrong thing to imply.

    The dead levers are pinned shut here too, so no caller has to remember them.
    """
    # Imported HERE, not at module scope. A module-level `from … import build_plans`
    # binds the name once, so patching `planner_builder.build_plans` — which is what
    # the existing tests do, at the real boundary — would silently miss this call.
    from core.services.planner_builder import build_plans

    return build_plans(
        year=year,
        term=term,
        shortlist=shortlist,
        baseline=baseline,
        keep_registered=keep_current_sections,
        suggest_swaps=False,  # the service emits placeholder strings, never real swaps
        strict_per_course=False,  # unusable on real data: returns scheduled=0
        # Seats are NOT promised. The reason used to read "available_capacity is
        # NULL on every row", which was true before the 50-section import and is
        # false now — all 50 live rows carry a number. The decision stands on a
        # current reason instead: capacity is a snapshot with no reservation behind
        # it, and a screen that shows "25 seats" is read as "a seat for you".
        consider_capacity=False,
        max_credits=int(max_credits or 0),
        gender=gender,
        program=program,
        require_complete_meetings=require_complete_meetings,
    )


def permitted_course_codes(program: str) -> set[str]:
    """Every course this programme's student may legitimately put in a plan.

    Plan membership alone is too narrow. A student filling an elective slot picks a
    CONCRETE course that is permitted through that slot and is not itself a plan
    row — rejecting it would refuse exactly the choice the elective screen exists to
    offer.

    The elective knowledge is not re-derived here. `_resolve_elective_slot` already
    knows that a placeholder is recognised by its requirement TYPE rather than by
    guessing at the code shape, and which concrete courses each slot maps to; this
    asks it, once per placeholder.
    """
    from core.models import ProgrammeRequirement
    from core.services.virtual_advisor_capabilities import _resolve_elective_slot, is_elective_slot

    # `iexact`, like every other programme lookup here. Exact matching made a
    # lowercase or oddly-cased `Student.program` yield an EMPTY permitted set —
    # which then rejected every course the student named, with a message blaming
    # the course.
    rows = list(
        ProgrammeRequirement.objects.filter(program__iexact=program).values("course_code", "type")
    )
    permitted = {normalize_code(r["course_code"]) for r in rows if r["course_code"]}

    for row in rows:
        # The shared predicate, not a second copy of the rule.
        if not is_elective_slot(row.get("type")):
            continue
        # `limit=None`: the cap inside is for chat readability, and inheriting it
        # here would turn a display decision into an authorisation one.
        for option in _resolve_elective_slot(row["course_code"], program, limit=None) or []:
            code = normalize_code(option.get("course_code") or "")
            if code:
                permitted.add(code)
    return permitted


def _course_credits(program: str) -> dict[str, int]:
    return {
        normalize_code(r["course_code"]): int(r["credit_hours"] or 0)
        for r in ProgrammeRequirement.objects.filter(program__iexact=program).values(
            "course_code", "credit_hours"
        )
        if normalize_code(r["course_code"])
    }


def _credit_for(code: str, credits: dict[str, int]) -> int:
    """Return the same non-zero course weight everywhere in the adapter."""
    recorded = int(credits.get(normalize_code(code), 0) or 0)
    return recorded if recorded > 0 else DEFAULT_CREDITS


def _baseline_mappings(baseline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the baseline's one-row-per-meeting shape into exact sections.

    ``planner_builder`` deliberately treats the baseline only as occupied time.  A
    student-facing alternative, however, must show the sections that make those
    times occupied.  Group by the real section id (with a defensive course/label
    fallback for old imported rows) and retain only timetable facts; room,
    instructor and registration metadata do not belong in an alternative.
    """
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    meeting_keys: dict[tuple[Any, ...], set[tuple[str, str, str]]] = {}

    for row in baseline:
        code = normalize_code(row.get("course_code") or row.get("course_key") or "")
        if not code:
            continue
        section = str(row.get("section") or "").strip()
        section_id = row.get("term_section_id")
        identity = (
            ("id", int(section_id))
            if section_id not in (None, "")
            else ("label", code, section.casefold())
        )
        if identity not in grouped:
            grouped[identity] = {
                "course_code": code,
                "course_key": code,
                "course_number": str(row.get("course_number") or ""),
                "section": section,
                "term_section_id": int(section_id) if section_id not in (None, "") else None,
                "meetings": [],
                "source": "current",
            }
            meeting_keys[identity] = set()

        day = str(row.get("day") or "").strip()
        start = str(row.get("start_time") or "").strip()
        end = str(row.get("end_time") or "").strip()
        # A baseline row with blank time fields represents a section for which no
        # meeting is recorded; it is not itself a blank meeting.
        if not (day and start and end):
            continue
        meeting_key = (day, start, end)
        if meeting_key in meeting_keys[identity]:
            continue
        meeting_keys[identity].add(meeting_key)
        grouped[identity]["meetings"].append({"day": day, "start_time": start, "end_time": end})

    return list(grouped.values())


def _distinct_course_count(mappings: list[dict[str, Any]]) -> int:
    return len(
        {
            normalize_code(mapping.get("course_code") or mapping.get("course_key") or "")
            for mapping in mappings
            if normalize_code(mapping.get("course_code") or mapping.get("course_key") or "")
        }
    )


def _distinct_credit_hours(mappings: list[dict[str, Any]], credits: dict[str, int]) -> int:
    codes = {
        normalize_code(mapping.get("course_code") or mapping.get("course_key") or "")
        for mapping in mappings
        if normalize_code(mapping.get("course_code") or mapping.get("course_key") or "")
    }
    return sum(_credit_for(code, credits) for code in codes)


def _option_signature(option: dict[str, Any]) -> tuple[int, ...]:
    """What makes two timetables the same timetable.

    The builder runs three methods and takes the top three from each, with the
    duplicate check scoped to a single method — so the same timetable can come back
    up to three times. The section ids it chose are the identity; everything else in
    the payload is derived from them.
    """
    ids = [
        int(m.get("term_section_id"))
        for m in (option.get("mappings") or [])
        if m.get("term_section_id") is not None
    ]
    return tuple(sorted(ids))


def _meeting_rows(option: dict[str, Any], credits: dict[str, int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for mapping in option.get("mappings") or []:
        code = str(mapping.get("course_code") or "")
        for meeting in mapping.get("meetings") or []:
            rows.append(
                {
                    "course_code": code,
                    "section": mapping.get("section"),
                    "credits": _credit_for(code, credits),
                    "day": str(meeting.get("day") or "").strip().upper()[:3],
                    "start": str(meeting.get("start_time") or ""),
                    "end": str(meeting.get("end_time") or ""),
                    "source": ("current" if mapping.get("source") == "current" else "proposed"),
                }
            )
    rows.sort(key=lambda r: (DAY_ORDER.index(r["day"]) if r["day"] in DAY_ORDER else 9, r["start"]))
    return rows


def _comparison(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The handful of facts that let a person tell two timetables apart.

    Arithmetic over meetings the builder already returned — not a judgement about
    which timetable is better. No gap analysis: the data supports counting days and
    reading the first and last class, and nothing more is offered as if it were.
    """
    if not rows:
        return {"days_on_campus": 0, "days": [], "earliest_start": None, "latest_end": None}
    days = sorted(
        {r["day"] for r in rows}, key=lambda d: DAY_ORDER.index(d) if d in DAY_ORDER else 9
    )
    return {
        "days_on_campus": len(days),
        "days": days,
        "earliest_start": min(r["start"] for r in rows if r["start"]),
        "latest_end": max(r["end"] for r in rows if r["end"]),
    }


def build_student_options(request: PlannerRequest) -> dict[str, Any]:
    """Every distinct timetable the builder could find, for the student to choose from.

    Raises `PlannerUnavailable` rather than degrading: a cohort that cannot be
    resolved must not fall through to an all-pass section filter, because every real
    section is gendered and the result would be the other cohort's timetable rather
    than an empty one.
    """
    student_id = int(request.student_id)
    try:
        gender = student_gender_strict(student_id)
    except UnknownStudentGender as exc:
        # NOT `str(exc)`. That message explains to an operator WHY the code refuses
        # to guess — "an unresolved cohort would return the other cohort's sections"
        # — which is internal reasoning about a database query, and it was going
        # straight to the student. The cause is chained for the log; the student
        # gets the thing they can act on.
        raise PlannerUnavailable(
            "تعذّر تحديد الشطر (طلاب/طالبات) في ملفك، ولا يصحّ تخمينه. راجع القسم لتحديث بياناتك."
        ) from exc

    program = str(
        Student.objects.filter(student_id=student_id).values_list("program", flat=True).first()
        or ""
    ).strip()
    if not program:
        raise PlannerUnavailable(
            "لا يوجد برنامج دراسي مسجَّل في ملفك، فلا توجد خطة يمكن البناء عليها. "
            "راجع القسم لتحديث بياناتك."
        )
    credits = _course_credits(program)
    for raw_code, raw_credits in request.course_credits_override:
        code = normalize_code(raw_code)
        value = int(raw_credits or 0)
        if code and value > 0:
            credits[code] = value

    # What the student asked for, then whatever their own plan recommends. Ordered so
    # a named course is never dropped in favour of a recommended one.
    wanted = [normalize_code(c) for c in request.must_include if str(c).strip()]
    required = [normalize_code(c) for c in request.required_courses if str(c).strip()]
    required_set = set(required)
    pinned = {
        normalize_code(code): int(section_id)
        for code, section_id in request.fixed_sections
        if normalize_code(code)
    }
    recommended = (
        [
            normalize_code(c)
            for c in (recommend_next_courses(student_id, request.year, request.term) or [])
        ]
        if request.include_recommendations
        else []
    )
    # A pin is a constraint on a candidate.  Include its course defensively even
    # when a caller forgot to repeat it in the soft candidate list; whether it is
    # REQUIRED remains a separate, explicit decision.
    codes = list(dict.fromkeys(wanted + required + list(pinned) + recommended))
    baseline = (
        [dict(row) for row in request.baseline_override]
        if request.baseline_override is not None
        else get_student_term_baseline(student_id, str(request.year), str(request.term))
    )
    current_mappings = _baseline_mappings(baseline) if request.keep_current_sections else []
    current_codes = {
        normalize_code(mapping.get("course_code") or "")
        for mapping in current_mappings
        if normalize_code(mapping.get("course_code") or "")
    }

    # In around-current mode the retained section is immutable for this build.
    # A pin to that same section is already satisfied; a pin to a different
    # section cannot be honoured and must not degrade into a baseline-only result
    # that looks as though the pin succeeded.
    baseline_pin_failures: list[dict[str, Any]] = []
    if request.keep_current_sections:
        current_ids_by_code: dict[str, set[int]] = {}
        for mapping in current_mappings:
            code = normalize_code(mapping.get("course_code") or "")
            section_id = mapping.get("term_section_id")
            if not code or section_id in (None, ""):
                continue
            current_ids_by_code.setdefault(code, set()).add(int(section_id))
        for code, pinned_id in pinned.items():
            current_ids = current_ids_by_code.get(code)
            if current_ids and current_ids != {pinned_id}:
                baseline_pin_failures.append(
                    {
                        "course_code": code,
                        "reason_code": "PIN_CONFLICTS_WITH_RETAINED_SECTION",
                        "reason": (
                            "The requested pinned section differs from the section retained "
                            "in around-current mode. Use a from-scratch proposal to compare "
                            "another section; nothing was changed."
                        ),
                    }
                )
    if baseline_pin_failures:
        return {
            "student_id": student_id,
            "term": f"{request.year}/{request.term}",
            "requested": codes,
            "alternatives": [],
            "unplaced": baseline_pin_failures,
            "constraint_failures": baseline_pin_failures,
            "generated": 0,
            "reason": "HARD_CONSTRAINTS_UNSATISFIED",
        }

    # The builder uses the baseline as an occupancy mask; it does not emit those
    # sections as selected mappings.  Asking it to schedule a held course therefore
    # makes the course collide with itself.  In keep mode the exact current section
    # is already the answer for that course, so only genuine additions go to the
    # solver.
    addition_codes = (
        [code for code in codes if code not in current_codes]
        if request.keep_current_sections
        else list(codes)
    )
    required_addition_codes = (
        required_set - current_codes if request.keep_current_sections else set(required_set)
    )
    if not addition_codes and not current_mappings:
        return {
            "student_id": student_id,
            "term": f"{request.year}/{request.term}",
            "requested": codes,
            "alternatives": [],
            "unplaced": [],
            "generated": 0,
            "reason": "NOTHING_TO_SCHEDULE",
        }

    # A pin is a filter on one course's options; every other course keeps its full
    # list. The builder honours this before any solver runs, so mixing pinned and
    # free courses in one request needs nothing special here.
    shortlist: list[dict[str, Any]] = []
    for code in addition_codes:
        item: dict[str, Any] = {
            "course_code": code,
            "credits": _credit_for(code, credits),
            "must_take": code in required_set,
        }
        if code in pinned:
            item["pinned_sections"] = [{"term_section_id": int(pinned[code])}]
        shortlist.append(item)

    current_credit_hours = _distinct_credit_hours(current_mappings, credits)
    requested_cap = int(request.max_credits or 0)
    solver_cap = requested_cap
    if request.keep_current_sections and requested_cap > 0:
        solver_cap = max(0, requested_cap - current_credit_hours)

    generated = 0
    solver_options: list[dict[str, Any]] = []
    solver_unscheduled: list[dict[str, Any]] = []
    solver_constraint_failures: list[dict[str, Any]] = []

    def _honours_hard_constraints(option: dict[str, Any]) -> bool:
        selected = {
            normalize_code(mapping.get("course_code") or ""): mapping
            for mapping in (option.get("mappings") or [])
            if normalize_code(mapping.get("course_code") or "")
        }
        if not required_addition_codes.issubset(selected):
            return False
        for code, pinned_id in pinned.items():
            mapping = selected.get(code)
            # A pin without must-take is only a filter: omission remains valid.
            if mapping is None:
                continue
            if int(mapping.get("term_section_id") or 0) != int(pinned_id):
                return False
        return True

    if shortlist and not (request.keep_current_sections and requested_cap > 0 and solver_cap <= 0):
        result = run_solver(
            year=str(request.year),
            term=str(request.term),
            shortlist=shortlist,
            baseline=baseline,
            keep_current_sections=request.keep_current_sections,
            max_credits=solver_cap,
            gender=gender,
            program=program,
            require_complete_meetings=request.require_complete_meetings,
        )
        raw_solver_options = list(result.get("options") or [])
        solver_options = [
            option for option in raw_solver_options if _honours_hard_constraints(option)
        ]
        solver_unscheduled = list(result.get("unscheduled") or [])
        solver_constraint_failures = list(
            (result.get("summary") or {}).get("hard_constraint_failures") or []
        )
        generated = len(solver_options)

    # A required addition that could not be placed invalidates the whole proposed
    # alternative.  Preserve an explanation, but never turn the retained baseline
    # into a successful option missing that course.
    if not solver_options and required_addition_codes:
        unscheduled_by_code = {
            normalize_code(row.get("course_code") or ""): row
            for row in solver_unscheduled
            if normalize_code(row.get("course_code") or "")
        }
        for code in sorted(required_addition_codes):
            source = unscheduled_by_code.get(code) or {}
            reason = str(source.get("reason") or "").strip()
            if not reason:
                reason = (
                    "Could not fit with chosen constraints/objective"
                    if requested_cap <= 0 or solver_cap > 0
                    else "Could not fit within the remaining credit limit"
                )
            if code not in unscheduled_by_code:
                solver_unscheduled.append({"course_code": code, "reason": reason})
            if not any(
                normalize_code(row.get("course_code") or "") == code
                for row in solver_constraint_failures
            ):
                solver_constraint_failures.append(
                    {
                        "course_code": code,
                        "reason": f"Required course could not be scheduled: {reason}",
                    }
                )

    # ``planner_builder`` no longer disguises an empty mapping as a valid option.
    # Retain its no-result diagnostics internally so this adapter can still
    # explain globally unplaced courses; ``raw_options`` below filters this row
    # out unless a real retained baseline makes it a meaningful alternative.
    if not solver_options and solver_unscheduled and not required_addition_codes:
        solver_options = [
            {
                "name": "",
                "scheduled": 0,
                "target": len(shortlist),
                "mappings": [],
                "unscheduled": solver_unscheduled,
                "diagnostic_only": True,
            }
        ]

    # Keeping a real baseline is itself a useful alternative.  This also covers a
    # solver that found no addition and a cap already consumed by current courses.
    # The synthetic row has no A/B/C label because no Planner method produced it.
    if current_mappings and not solver_options and not required_addition_codes:
        reason = (
            "Could not fit with chosen constraints/objective"
            if shortlist and requested_cap > 0 and solver_cap <= 0
            else "No plan could be built."
        )
        solver_options = [
            {
                "name": "",
                "scheduled": 0,
                "target": len(shortlist),
                "mappings": [],
                "unscheduled": [
                    {"course_code": item["course_code"], "reason": reason} for item in shortlist
                ],
            }
        ]

    # An empty solver mapping is not a timetable in rebuild mode.  Keep the raw
    # list separately because its unscheduled reasons still explain a global miss.
    raw_options = [
        option
        for option in solver_options
        if (option.get("mappings") or []) or bool(current_mappings)
    ]
    placed_in_any_option = {
        normalize_code(mapping.get("course_code") or "")
        for option in solver_options
        for mapping in (option.get("mappings") or [])
        if normalize_code(mapping.get("course_code") or "")
    } | current_codes

    alternatives: list[dict[str, Any]] = []
    seen: dict[tuple[int, ...], int] = {}
    for option in raw_options:
        proposed_mappings = [
            {**mapping, "source": "proposed"} for mapping in (option.get("mappings") or [])
        ]
        combined_mappings = [*current_mappings, *proposed_mappings]
        combined_option = {**option, "mappings": combined_mappings}
        signature = _option_signature(combined_option)
        planner_name = str(option.get("name") or "").strip().upper()
        if signature in seen:
            # A, B and C are genuinely different search methods, but they can
            # converge on the same section set. Keep that provenance instead of
            # showing an identical timetable as several fake alternatives.
            if planner_name:
                planner_options = alternatives[seen[signature]]["planner_options"]
                if planner_name not in planner_options:
                    planner_options.append(planner_name)
            continue
        seen[signature] = len(alternatives)
        rows = _meeting_rows(combined_option, credits)
        option_unplaced: list[dict[str, Any]] = []
        for entry in option.get("unscheduled") or []:
            course_code = normalize_code(entry.get("course_code") or "")
            if course_code in placed_in_any_option:
                # Top-k generation obtains A2/A3 (and B/C equivalents) by
                # temporarily excluding sections selected by a better variant.
                # The rerun can consequently say NO_SECTION even though A1 just
                # used that section. Translate that as a property of THIS variant,
                # not as a false claim about the catalogue.
                code = "OMITTED_IN_THIS_VARIANT"
                explanation = (
                    "This Planner variant did not place the course; another generated "
                    "variant did. Compare the other options."
                )
            else:
                code, explanation = _translate_unplaced(entry.get("reason"))
            option_unplaced.append(
                {
                    "course_code": course_code,
                    "reason_code": code,
                    "reason": explanation,
                }
            )
        alternatives.append(
            {
                # Stable, opaque, and the only thing a client sends back to say
                # which timetable it chose. It used to be the section ids joined by
                # dashes, under a comment claiming it was "not a database id" —
                # which was simply false, and false comments are what stop the next
                # reader looking. Hashed, so removing the ids from the payload
                # actually removes them.
                "key": hashlib.sha256(
                    "-".join(str(i) for i in signature).encode("ascii")
                ).hexdigest()[:16],
                "courses": [
                    {
                        "course_code": m.get("course_code"),
                        "section": m.get("section"),
                        "credits": _credit_for(str(m.get("course_code") or ""), credits),
                        "source": "current" if m.get("source") == "current" else "proposed",
                        "meeting_issue_codes": list(m.get("meeting_issue_codes") or []),
                        # Kept for the SERVER's own use — the draft stores this whole
                        # structure, and a pin is expressed as a section id. The view
                        # strips it before anything reaches the browser.
                        "term_section_id": m.get("term_section_id"),
                    }
                    for m in combined_mappings
                ],
                "meetings": rows,
                # These are the exact identities emitted by planner_builder:
                # A1-A3, B1-B3 and C1-C3. More than one name means independent
                # planner methods found the same section set.
                "planner_options": [planner_name] if planner_name else [],
                "scheduled_courses": int(
                    option.get("scheduled")
                    if option.get("scheduled") is not None
                    else _distinct_course_count(proposed_mappings)
                )
                + len(current_codes),
                "target_courses": int(
                    option.get("target") if option.get("target") is not None else len(shortlist)
                )
                + len(current_codes),
                # Unplaced courses belong to THIS generated option. A1 may place
                # more courses than A2, so one global list cannot describe both.
                "unplaced": option_unplaced,
                "course_count": _distinct_course_count(combined_mappings),
                # The baseline repeats once per meeting. Count a course's credits
                # once, not once per row (nor once per accidentally duplicated
                # section mapping).
                "credit_hours": _distinct_credit_hours(combined_mappings, credits),
                **_comparison(rows),
            }
        )

    # Global unplaced means no generated option could place the course. Courses
    # omitted only by A2/A3 belong to that alternative's `unplaced`, not here.
    unplaced: list[dict[str, Any]] = []
    first_unscheduled = (
        list(solver_options[0].get("unscheduled") or [])
        if solver_options
        else list(solver_unscheduled)
    )
    for entry in first_unscheduled:
        course_code = normalize_code(entry.get("course_code") or "")
        if course_code in placed_in_any_option:
            continue
        code, explanation = _translate_unplaced(entry.get("reason"))
        unplaced.append(
            {
                "course_code": course_code,
                "reason_code": code,
                "reason": explanation,
            }
        )

    return {
        "student_id": student_id,
        "term": f"{request.year}/{request.term}",
        "requested": codes,
        "alternatives": alternatives,
        "unplaced": unplaced,
        "constraint_failures": solver_constraint_failures if not alternatives else [],
        # How many the builder produced before duplicates were removed, so a caller
        # can see that "3 alternatives" came from nine attempts rather than three.
        "generated": generated,
        "reason": "" if alternatives else "NO_VALID_TIMETABLE",
    }


# ── drafts: carrying a selection across a navigation ─────────────


class DraftRejected(ValueError):
    """The draft names something this student may not take."""


def validate_draft_selection(
    student_id: int,
    course_codes: Any,
    fixed_sections: Any,
) -> tuple[list[str], dict[str, int]]:
    """Re-derive what the draft is allowed to contain, from the student's own data.

    A draft is a convenience for carrying a selection across a navigation, never a
    grant of authority over its contents — so nothing in it is trusted. Course codes
    must be in this student's programme plan; a pinned section must exist, belong to
    that course, and be one this student's cohort may take.

    The cohort check is the one that matters most. Sections are gender-segregated by
    their leading letter, so a pinned id from the other cohort would otherwise put a
    student in a section they cannot attend — and the planner would schedule around
    it happily, because the id is real.
    """
    from core.models import TermSection

    # Refuses rather than guesses, before anything else is checked: an unresolvable
    # cohort must not reach a section comparison at all.
    try:
        student_gender_strict(student_id)
    except UnknownStudentGender as exc:
        raise DraftRejected(
            "تعذّر تحديد الشطر (طلاب/طالبات) في ملفك، ولا يصحّ تخمينه. راجع القسم لتحديث بياناتك."
        ) from exc

    program = str(
        Student.objects.filter(student_id=student_id).values_list("program", flat=True).first()
        or ""
    ).strip()
    permitted = permitted_course_codes(program)

    codes: list[str] = []
    for raw in course_codes if isinstance(course_codes, list) else []:
        code = normalize_code(str(raw))
        if not code:
            continue
        if code not in permitted:
            raise DraftRejected(f"المقرر {code} ليس ضمن المقررات المتاحة لك.")
        if code not in codes:
            codes.append(code)

    pinned: dict[str, int] = {}
    for raw_code, raw_id in (
        (fixed_sections or {}).items() if isinstance(fixed_sections, dict) else []
    ):
        code = normalize_code(str(raw_code))
        if code not in codes:
            raise DraftRejected(f"حُدِّدت شعبة للمقرر {code} وهو غير مُدرَج ضمن اختيارك.")
        try:
            section_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise DraftRejected(f"الشعبة المحدَّدة للمقرر {code} غير صالحة.") from exc

        section = TermSection.objects.filter(id=section_id, scenario__isnull=True).first()
        if section is None:
            raise DraftRejected(f"الشعبة المحدَّدة للمقرر {code} غير موجودة.")
        actual = normalize_code(
            section.course_key or f"{section.course_code}{section.course_number}"
        )
        if actual != code:
            raise DraftRejected(f"الشعبة المحدَّدة للمقرر {code} تخصّ المقرر {actual}.")
        # THE canonical answer, shared with every other surface. Spelling the rule
        # out here — "the label starts with M" — would mean a change to section
        # coding splitting the planner and the chat apart without a failing test.
        if not section_is_available_to_student(section, student_id=student_id):
            raise DraftRejected(f"الشعبة {section.section} من {code} غير متاحة لك.")
        pinned[code] = section_id

    return codes, pinned


@dataclass(frozen=True)
class SectionLabelPin:
    """A public pin request: stable course code + human-visible section label."""

    course_code: str
    section_label: str


@dataclass(frozen=True)
class ResolvedSectionPin:
    """A validated current-snapshot pin, with its database id kept server-side."""

    course_code: str
    section_label: str
    term_section_id: int


class SectionPinResolutionError(ValueError):
    """A label pin cannot be resolved without guessing or crossing student scope."""

    def __init__(self, *, course_code: str, section_label: str, reason: str) -> None:
        self.course_code = normalize_code(course_code)
        self.section_label = str(section_label or "").strip().upper()
        self.reason = str(reason)
        super().__init__(self.reason)

    def as_failure(self) -> dict[str, str]:
        return {
            "course_code": self.course_code,
            "section_label": self.section_label,
            "reason": self.reason,
        }


def resolve_section_label_pins(
    student_id: int,
    course_codes: list[str],
    requested_pins: tuple[SectionLabelPin, ...],
) -> tuple[ResolvedSectionPin, ...]:
    """Resolve public section labels to private ids against the current snapshot.

    Chat must never accept a model-supplied ``TermSection`` id.  Labels are resolved
    afresh against global (``scenario IS NULL``) rows, then the existing draft
    validator re-checks course-plan membership plus the canonical programme/cohort
    rule.  There is deliberately no year/term predicate: ``TermSection`` is the
    current global snapshot and stores no historical term dimension.
    """
    from core.models import TermSection

    normalized_codes = [normalize_code(code) for code in course_codes if normalize_code(code)]
    try:
        validated_codes, _ = validate_draft_selection(student_id, normalized_codes, {})
    except DraftRejected as exc:
        raise SectionPinResolutionError(
            course_code="",
            section_label="",
            reason=str(exc),
        ) from exc

    resolved: list[ResolvedSectionPin] = []
    seen: dict[str, str] = {}
    for requested in requested_pins:
        code = normalize_code(requested.course_code)
        label = str(requested.section_label or "").strip().upper()
        if not code or not label:
            raise SectionPinResolutionError(
                course_code=code,
                section_label=label,
                reason="Each pinned section requires both a course code and a section label.",
            )
        previous = seen.get(code)
        if previous is not None:
            if previous == label:
                continue
            raise SectionPinResolutionError(
                course_code=code,
                section_label=label,
                reason=(f"More than one exact section was pinned for {code}; choose one section."),
            )
        seen[code] = label

        matches = list(
            TermSection.objects.filter(
                scenario__isnull=True,
                course_key__iexact=code,
                section__iexact=label,
            ).order_by("id")[:2]
        )
        if not matches:
            raise SectionPinResolutionError(
                course_code=code,
                section_label=label,
                reason=(
                    f"No current global section record matches {code} section {label}. "
                    "This does not mean the university does not offer it."
                ),
            )
        if len(matches) > 1:
            raise SectionPinResolutionError(
                course_code=code,
                section_label=label,
                reason=(
                    f"More than one current section record matches {code} section {label}; "
                    "the data must be reviewed before it can be pinned."
                ),
            )

        section = matches[0]
        try:
            # Reuse the existing authority boundary instead of duplicating the
            # plan, exact-course, programme and cohort rules here.
            validate_draft_selection(
                student_id,
                validated_codes,
                {code: int(section.id)},
            )
        except DraftRejected as exc:
            raise SectionPinResolutionError(
                course_code=code,
                section_label=label,
                reason=(
                    f"{code} section {label} is not valid for this student's programme or cohort."
                ),
            ) from exc

        resolved.append(
            ResolvedSectionPin(
                course_code=code,
                section_label=str(section.section or label).strip().upper(),
                term_section_id=int(section.id),
            )
        )

    return tuple(resolved)

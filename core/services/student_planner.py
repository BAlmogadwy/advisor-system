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
    must_include: tuple[str, ...] = ()
    keep_current_sections: bool = True
    max_credits: int = 0
    #: {"AI221": 4213} — courses whose section the student pinned. A course absent
    #: from here is one the planner chooses freely, which is the default and the
    #: common case.
    fixed_sections: tuple[tuple[str, int], ...] = ()


def run_solver(
    *,
    year: str,
    term: str,
    shortlist: list[dict[str, Any]],
    baseline: list[dict[str, Any]],
    keep_current_sections: bool,
    max_credits: int,
    gender: str,
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
        r["course_code"]: int(r["credit_hours"] or 0)
        for r in ProgrammeRequirement.objects.filter(program__iexact=program).values(
            "course_code", "credit_hours"
        )
    }


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
                    "credits": credits.get(code, DEFAULT_CREDITS),
                    "day": str(meeting.get("day") or "").strip().upper()[:3],
                    "start": str(meeting.get("start_time") or ""),
                    "end": str(meeting.get("end_time") or ""),
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

    # What the student asked for, then whatever their own plan recommends. Ordered so
    # a named course is never dropped in favour of a recommended one.
    wanted = [normalize_code(c) for c in request.must_include if str(c).strip()]
    recommended = [
        normalize_code(c)
        for c in (recommend_next_courses(student_id, request.year, request.term) or [])
    ]
    codes = list(dict.fromkeys(wanted + recommended))
    if not codes:
        return {
            "student_id": student_id,
            "term": f"{request.year}/{request.term}",
            "requested": [],
            "alternatives": [],
            "unplaced": [],
            "reason": "NOTHING_TO_SCHEDULE",
        }

    baseline = get_student_term_baseline(student_id, str(request.year), str(request.term))

    # A pin is a filter on one course's options; every other course keeps its full
    # list. The builder honours this before any solver runs, so mixing pinned and
    # free courses in one request needs nothing special here.
    pinned = dict(request.fixed_sections)
    shortlist: list[dict[str, Any]] = []
    for code in codes:
        item: dict[str, Any] = {"course_code": code, "credits": credits.get(code, DEFAULT_CREDITS)}
        if code in pinned:
            item["pinned_sections"] = [{"term_section_id": int(pinned[code])}]
        shortlist.append(item)

    result = run_solver(
        year=str(request.year),
        term=str(request.term),
        shortlist=shortlist,
        baseline=baseline,
        keep_current_sections=request.keep_current_sections,
        max_credits=request.max_credits,
        gender=gender,
    )

    alternatives: list[dict[str, Any]] = []
    seen: set[tuple[int, ...]] = set()
    for option in result.get("options") or []:
        signature = _option_signature(option)
        if signature in seen:
            continue
        seen.add(signature)
        rows = _meeting_rows(option, credits)
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
                        "credits": credits.get(str(m.get("course_code") or ""), DEFAULT_CREDITS),
                        # Kept for the SERVER's own use — the draft stores this whole
                        # structure, and a pin is expressed as a section id. The view
                        # strips it before anything reaches the browser.
                        "term_section_id": m.get("term_section_id"),
                    }
                    for m in (option.get("mappings") or [])
                ],
                "meetings": rows,
                "course_count": len(option.get("mappings") or []),
                "credit_hours": sum(
                    credits.get(str(m.get("course_code") or ""), DEFAULT_CREDITS)
                    for m in (option.get("mappings") or [])
                ),
                **_comparison(rows),
            }
        )

    # Reported from the first alternative: `unscheduled` is a property of the course
    # set against the catalogue, not of one arrangement of it.
    unplaced: list[dict[str, Any]] = []
    first = (result.get("options") or [{}])[0]
    for entry in first.get("unscheduled") or []:
        code, explanation = _translate_unplaced(entry.get("reason"))
        unplaced.append(
            {
                "course_code": entry.get("course_code"),
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
        # How many the builder produced before duplicates were removed, so a caller
        # can see that "3 alternatives" came from nine attempts rather than three.
        "generated": len(result.get("options") or []),
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

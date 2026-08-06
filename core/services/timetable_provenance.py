"""Where every course and every section in a built timetable came from.

WHAT THE OLD PAYLOAD COULD NOT SAY

``build_my_timetable`` answered with two fields that cannot carry the question.

``requested`` was ``must_include`` concatenated with whatever
``recommend_next_courses`` chose, under a name asserting the student asked for all
of it. So TT21 — «الجدول أضاف مقررًا أنا ما طلبته، من وين جاء؟», "the timetable added
a course I never asked for, where did it come from?" — had no answer anywhere in
the payload. Live, the model called no tool at all and told the student the system
keeps no record of prior requests. That is false: the record is the recommender,
and it had run on that very turn.

``placed`` was worse, because it looked complete. ``planner_builder`` uses the
baseline as occupied TIME — a mask that prunes colliding options (planner_builder
lines 444-461, 633-651, 842-861) — and never adds it to ``mappings``. So the
sections the student is already registered in appear in no field of the result,
while the tool description promises "It ALWAYS keeps the sections the student is
already registered in". The model was asked to assert a retention it was given no
evidence of, beside a ``placed`` list that reads as the whole week.

MEASURED, WITH NO MODEL IN THE LOOP

The controlled evaluation record, 1448/1, registered in AI1-M1, AI331-M1,
CS323-M1, CS372-M1::

    requested            ['AI331', 'CS323', 'CS372', 'GSE1']   # the student named none of them
    placed               [('CS323', 'M2', 4)]                  # a DIFFERENT section of a held course
    unplaced             [('GSE1', NOT_ON_FILE),
                          ('AI331', ALL_SECTIONS_CLASH),       # already registered in it
                          ('CS372', ALL_SECTIONS_CLASH)]       # already registered in it
    planned_credit_hours 4                                     # the student is carrying 15

``recommend_next_courses`` excludes courses that are PASSED or STUDYING, and a
``StudentTermSection`` registration is neither — so the term's own registrations
come back as recommendations, go to the solver, and are pruned by the student's
own baseline. ALL_SECTIONS_CLASH is then true and useless: the only thing they
clash with is themselves. Live, on TT10, that produced «تم الاحتفاظ بالشعب … CS323-M1
…» and «CS323: شعبة M2» in one answer, and «AI331: جميع الشعب المتاحة تتعارض مع جدولك
الحالي» about a course the student is sitting in.

WHY A SEPARATE MODULE

Every fact here is derived from the baseline and the solver's mappings by pure
functions, so it is testable without a solver, a database or a provider — and the
executor keeps one job. The old flattening was three lines inside a two-hundred
line function, which is precisely how it survived.

WHY THERE IS NO ``retained.isdisjoint(new)`` ASSERTION

Because that assertion would forbid the correct future behaviour instead of the
incorrect current one. A replace-section workflow legitimately holds one course in
both states at once; what must never happen is asserting BOTH without saying which
way the transition runs. So the transition is encoded — ``CHANGE_REPLACE_SECTION``
plus a row in ``section_replacements`` naming the section left and the section
taken — and the contradictory pair is the thing that cannot be expressed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The student named this course in ``must_include``.
SOURCE_STUDENT_REQUEST = "STUDENT_REQUEST"
#: ``recommend_next_courses`` chose it. This value is the answer to TT21, and it is
#: why the two course lists stay separate rather than merging into one list with a
#: ``source`` key: a merged list is one flattening away from being ``requested``
#: again, and the flattening is what this module exists to undo.
SOURCE_SYSTEM_RECOMMENDATION = "SYSTEM_RECOMMENDATION"
#: The student is already registered in this section. Appears on SECTION rows only:
#: the two course lists describe what was asked of the builder this turn, and a
#: standing registration was asked for by neither.
SOURCE_CURRENT_REGISTRATION = "CURRENT_REGISTRATION"
#: The solver returned a course that is in neither input list. Unreachable today —
#: the shortlist is built from those two lists — and named rather than defaulted,
#: because the fallback used to be SYSTEM_RECOMMENDATION and that is a provenance
#: CLAIM. Asserting "the system recommended this" about a course nothing recommended
#: is the same defect as the `requested` field this module replaced, one layer down:
#: an unattributable row saying it knows where it came from.
SOURCE_UNATTRIBUTED = "UNATTRIBUTED"

#: Kept from the student's current registration; the build did not touch it.
CHANGE_RETAIN = "RETAIN"
#: A section for a course the student holds none of.
CHANGE_ADD = "ADD"
#: Same course, different section — the held section is given up and another taken.
#: Unreachable from chat today, because a course the student already holds is never
#: sent to the solver. Computed rather than assumed empty: the planner's
#: replace-section workflow is exactly the caller that will produce it, and a field
#: that is only correct while it is empty is not a contract.
CHANGE_REPLACE_SECTION = "REPLACE_SECTION"

#: What became of a course the builder was asked to handle.
OUTCOME_ALREADY_REGISTERED = "ALREADY_REGISTERED"
OUTCOME_PLACED = "PLACED"
OUTCOME_NOT_PLACED = "NOT_PLACED"


#: Every key `TimetableAnswerFacts.as_payload` writes. Named as a set so callers
#: can ask "does this result carry a timetable at all" without listing the keys a
#: fifth time — the refusal path must carry none of them, and a test that checks
#: one key by hand goes quietly vacuous the moment that key is renamed.
TIMETABLE_FACT_KEYS = frozenset(
    {
        "student_id",
        "using_timetable_of_term",
        "student_requested_courses",
        "system_recommended_courses",
        "retained_sections",
        "new_sections",
        "section_replacements",
        "unplaced_courses",
        "credit_summary",
    }
)


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _meeting_text(day: Any, start: Any, end: Any) -> str:
    """One meeting as "DAY HH:MM-HH:MM", or "" when there is no meeting at all.

    The empty case has to be checked BEFORE formatting: a section with no meetings
    on file gives every part as "", and the template's own hyphen survives `strip()`
    as the string "-" — which is truthy, so it reads downstream as a real meeting
    and paints an empty slot in the student's week.
    """
    day, start, end = _text(day), _text(start), _text(end)
    if not (day or start or end):
        return ""
    return f"{day} {start}-{end}".strip()


def _same_section(held: dict[str, Any], placed: dict[str, Any]) -> bool:
    """Are these two rows the same section? Decided on the id, and only then the label.

    ``term_section_id`` is the primary key and the label is a display attribute, so
    the id decides — but ONLY when both rows carry one. A bare ``a != b`` reads a
    missing id as a difference, so a solver mapping with no id would report every
    section the student holds of that course as replaced: a student told their
    section was swapped, by a comparison against nothing.

    The label is the fallback rather than the rule because it is not normalised
    anywhere in this pipeline. Issue #54 records three classifiers in this repo
    disagreeing about whether a section is «M1» or « M1», so it is folded here — a
    difference of case or spacing between the registration row and the catalogue row
    is not a different section, and reporting it as one tells the student their
    section was replaced by itself.
    """
    held_id, placed_id = held.get("term_section_id"), placed.get("term_section_id")
    if held_id is not None and placed_id is not None:
        return held_id == placed_id
    return _text(held.get("section")).upper() == _text(placed.get("section")).upper()


def baseline_sections(baseline: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The student's current registration, one row per SECTION.

    ``get_student_term_baseline`` returns one row per MEETING — for student
    the controlled evaluation record that is 11 rows for 4 sections, each row
    repeating the section's full
    credit hours. Summing credits over it charges a 4-credit course 12 hours.

    Keyed on ``(course_code, section)`` rather than on the course alone. A student
    registered in two sections of one course is a registry state this module has no
    business silently resolving, and collapsing on the course would drop one of
    them under a rule nobody wrote down.

    ONLY the fields named below survive. The baseline rows carry ``instructor`` and
    ``room``, and ``_project_my_timetable`` drops instructor names on purpose —
    "who teaches it is a member of staff whose name does not need to leave the
    institution to answer 'when is my lecture'". Passing baseline rows through
    wholesale would undo that rule from a capability that never needed the names.
    """
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in baseline or []:
        code = _text(row.get("course_code")).upper()
        if not code:
            continue
        section = _text(row.get("section"))
        entry = out.setdefault(
            (code, section),
            {
                "course_code": code,
                "course_name": _text(row.get("course_name")),
                "section": section,
                "term_section_id": row.get("term_section_id"),
                "meetings": [],
            },
        )
        meeting = _meeting_text(row.get("day"), row.get("start_time"), row.get("end_time"))
        # A section with no meetings on file still emits a row, with empty day and
        # times. Keeping the blank would show the student an empty slot; dropping it
        # leaves `meetings: []`, which is the honest statement.
        if meeting and meeting not in entry["meetings"]:
            entry["meetings"].append(meeting)
    return list(out.values())


@dataclass(frozen=True)
class TimetableAnswerFacts:
    """Every fact a timetable answer may assert, already decided by the server.

    The model's job against this object is to say these things in the student's
    language. Working out which sections are new is not left to it — that
    reconstruction is what produced TT10's "I kept CS323-M1" beside "CS323: M2".

    Frozen, and ``as_payload`` hands back fresh containers, so a caller that mutates
    the payload cannot change what the facts say.
    """

    student_id: int
    using_timetable_of_term: str
    student_requested_courses: tuple[dict[str, Any], ...]
    system_recommended_courses: tuple[dict[str, Any], ...]
    retained_sections: tuple[dict[str, Any], ...]
    new_sections: tuple[dict[str, Any], ...]
    section_replacements: tuple[dict[str, Any], ...]
    unplaced_courses: tuple[dict[str, Any], ...]
    credit_summary: dict[str, Any]
    #: `None` means THIS CALLER CANNOT KNOW, and the key is then absent from the
    #: payload. An empty list would be a claim — "nothing was pinned" — and chat has
    #: no way to establish that: `must_include` names courses, never sections, so a
    #: hardcoded `[]` was evidence of an absence the code had never checked.
    fixed_sections: tuple[dict[str, Any], ...] | None = None

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "student_id": self.student_id,
            "using_timetable_of_term": self.using_timetable_of_term,
            "student_requested_courses": [dict(r) for r in self.student_requested_courses],
            "system_recommended_courses": [dict(r) for r in self.system_recommended_courses],
            "retained_sections": [dict(r) for r in self.retained_sections],
            "new_sections": [dict(r) for r in self.new_sections],
            "section_replacements": [dict(r) for r in self.section_replacements],
            "unplaced_courses": [dict(r) for r in self.unplaced_courses],
            "credit_summary": dict(self.credit_summary),
        }
        if self.fixed_sections is not None:
            payload["fixed_sections"] = [dict(r) for r in self.fixed_sections]
        return payload


def build_timetable_facts(
    *,
    student_id: int,
    using_timetable_of_term: str,
    requested_codes: list[str],
    recommended_codes: list[str],
    baseline: list[dict[str, Any]] | None,
    mappings: list[dict[str, Any]] | None,
    unscheduled: list[dict[str, Any]] | None,
    credit_hours: dict[str, int],
    default_credits: int,
    cap: int,
    fixed_sections: list[dict[str, Any]] | None = None,
) -> TimetableAnswerFacts:
    """Assemble the provenance from the four inputs that actually know it.

    ``credit_hours`` and ``default_credits`` are taken as arguments rather than
    looked up here so that ONE resolver decides every credit figure in the answer.
    The executor previously used two: ``credits.get(code, DEFAULT_CREDITS)`` when
    charging the solver's cap and ``credits.get(code, None)`` when reporting, so a
    course with no ``ProgrammeRequirement`` row for the student's programme was
    charged 3 hours against the cap and reported as contributing 0. Any row whose
    figure came from the fallback is marked ``credits_estimated``, so a summary
    built on a guess says so instead of reading as a record.
    """
    requested = [c for c in dict.fromkeys(requested_codes) if c]
    # Order-preserving de-duplication, and the student's own list wins the overlap:
    # a course named in `must_include` that the recommender also chose is the
    # student's request, not the system's suggestion.
    recommended = [c for c in dict.fromkeys(recommended_codes) if c and c not in set(requested)]
    source_of = {
        **{c: SOURCE_SYSTEM_RECOMMENDATION for c in recommended},
        **{c: SOURCE_STUDENT_REQUEST for c in requested},
    }

    def credits_for(code: str) -> tuple[int, bool]:
        value = credit_hours.get(code)
        if value in (None, ""):
            return int(default_credits), True
        return int(value), False

    def course_row(code: str) -> dict[str, Any]:
        hours, estimated = credits_for(code)
        row = {"course_code": code, "credit_hours": hours, "source": source_of[code]}
        if estimated:
            row["credits_estimated"] = True
        return row

    held = baseline_sections(baseline)
    held_by_course: dict[str, list[dict[str, Any]]] = {}
    for row in held:
        held_by_course.setdefault(row["course_code"], []).append(row)

    def section_row(base: dict[str, Any], *, source: str, change: str) -> dict[str, Any]:
        hours, estimated = credits_for(base["course_code"])
        row = {
            "course_code": base["course_code"],
            "course_name": base.get("course_name", ""),
            "section": base.get("section", ""),
            "meetings": list(base.get("meetings") or []),
            "credit_hours": hours,
            "source": source,
            "change": change,
        }
        if estimated:
            row["credits_estimated"] = True
        return row

    placed_rows = []
    for m in mappings or []:
        code = _text(m.get("course_code")).upper()
        placed_rows.append(
            {
                "course_code": code,
                "course_name": _text(m.get("course_name")),
                "section": _text(m.get("section")),
                "term_section_id": m.get("term_section_id"),
                "meetings": [
                    _meeting_text(mt.get("day"), mt.get("start_time"), mt.get("end_time"))
                    for mt in (m.get("meetings") or [])
                ],
            }
        )

    new_sections: list[dict[str, Any]] = []
    section_replacements: list[dict[str, Any]] = []
    replaced_keys: set[tuple[str, str]] = set()
    for row in placed_rows:
        code = row["course_code"]
        # The held section of the SAME course, if the solver picked a different one.
        superseded = next(
            (h for h in held_by_course.get(code, []) if not _same_section(h, row)),
            None,
        )
        source = source_of.get(code, SOURCE_UNATTRIBUTED)
        if superseded is not None:
            replaced_keys.add((code, superseded.get("section", "")))
            section_replacements.append(
                {
                    "course_code": code,
                    "from_section": superseded.get("section", ""),
                    "to_section": row["section"],
                    "source": source,
                }
            )
            new_sections.append(section_row(row, source=source, change=CHANGE_REPLACE_SECTION))
        else:
            new_sections.append(section_row(row, source=source, change=CHANGE_ADD))

    retained_sections = [
        section_row(h, source=SOURCE_CURRENT_REGISTRATION, change=CHANGE_RETAIN)
        for h in held
        if (h["course_code"], h.get("section", "")) not in replaced_keys
    ]

    unplaced_courses: list[dict[str, Any]] = []
    for u in unscheduled or []:
        code = _text(u.get("course_code")).upper()
        hours, estimated = credits_for(code)
        row = {
            "course_code": code,
            "credit_hours": hours,
            "source": source_of.get(code, SOURCE_UNATTRIBUTED),
            "outcome": OUTCOME_NOT_PLACED,
            "reason_code": u.get("reason_code"),
            "reason": u.get("reason"),
        }
        if estimated:
            row["credits_estimated"] = True
        unplaced_courses.append(row)

    # Every asked-for course the student already holds. Reported as its own outcome
    # rather than left to the solver, which prunes the student's own section against
    # the student's own baseline and calls the result ALL_SECTIONS_CLASH — measured
    # at 33 of 95 unplaced rows across 20 students, every one of them a course the
    # student was sitting in.
    for code in [c for c in (*requested, *recommended) if c in held_by_course]:
        hours, estimated = credits_for(code)
        row = {
            "course_code": code,
            "credit_hours": hours,
            "source": source_of[code],
            "outcome": OUTCOME_ALREADY_REGISTERED,
            "sections": [h.get("section", "") for h in held_by_course[code]],
        }
        if estimated:
            row["credits_estimated"] = True
        unplaced_courses.append(row)

    retained_hours = sum(int(r["credit_hours"]) for r in retained_sections)
    new_hours = sum(int(r["credit_hours"]) for r in new_sections)
    return TimetableAnswerFacts(
        student_id=int(student_id),
        using_timetable_of_term=using_timetable_of_term,
        student_requested_courses=tuple(course_row(c) for c in requested),
        system_recommended_courses=tuple(course_row(c) for c in recommended),
        retained_sections=tuple(retained_sections),
        new_sections=tuple(new_sections),
        # Passed through, and `None` when the caller could not know. See the field.
        fixed_sections=None if fixed_sections is None else tuple(fixed_sections),
        section_replacements=tuple(section_replacements),
        unplaced_courses=tuple(unplaced_courses),
        # NAMED FOR WHAT THEY MEASURE. `cap` sat beside `total` and bounded only
        # `new`, so {retained: 15, new: 6, total: 21, cap: 6} read as a plan breaching
        # its own ceiling. `run_solver` charges `max_credits` against the shortlist —
        # the courses this build ADDS — and the baseline never reaches it, so the
        # honest name says which half it governs.
        credit_summary={
            "retained_credit_hours": retained_hours,
            "new_credit_hours": new_hours,
            "total_plan_credit_hours": retained_hours + new_hours,
            "new_courses_credit_cap": int(cap) if cap else None,
        },
    )


class TimetableProvenanceError(Exception):
    """A contradiction the answer must not be built on.

    Raised rather than logged. `AdvisorCapabilityRegistry.execute` wraps every
    executor and turns an exception into `ok=False`, so a violation costs the student
    one refused tool call — against an answer that states they both keep and replace
    the same course, which is the defect this module exists to remove.
    """


def verify(facts: TimetableAnswerFacts, *, baseline_codes: set[str], keep_current: bool) -> None:
    """The four invariants, checked rather than commented.

    Each was previously a property the code happened to have. "Holds by
    construction" is only true until the construction changes, and three of these
    were exactly the kind of property a refactor removes without any test noticing.
    """
    retained = {(r["course_code"], r.get("section", "")) for r in facts.retained_sections}
    replaced = {(r["course_code"], r["from_section"]) for r in facts.section_replacements}
    # 1. retained sections match the baseline — as a SUBSET, because a replacement
    #    legitimately removes one. The union is the equality that actually holds.
    if not (retained | replaced) <= baseline_codes:
        raise TimetableProvenanceError(
            "retained and replaced sections are not drawn from the baseline"
        )
    # 2. every newly proposed course carries real provenance. UNATTRIBUTED is an
    #    internal contract failure, not a value a student may be shown: it means the
    #    solver returned a course that was in neither input list.
    for row in facts.new_sections:
        if row.get("source") not in (SOURCE_STUDENT_REQUEST, SOURCE_SYSTEM_RECOMMENDATION):
            raise TimetableProvenanceError(
                f"{row.get('course_code')} is proposed with source {row.get('source')!r}"
            )
    # 3. the totals reconcile with the rows they summarise.
    summary = facts.credit_summary
    retained_hours = sum(int(r["credit_hours"]) for r in facts.retained_sections)
    new_hours = sum(int(r["credit_hours"]) for r in facts.new_sections)
    if (
        summary["retained_credit_hours"] != retained_hours
        or summary["new_credit_hours"] != new_hours
    ):
        raise TimetableProvenanceError("credit summary does not match the section rows")
    if summary["total_plan_credit_hours"] != retained_hours + new_hours:
        raise TimetableProvenanceError("total is not retained plus new")
    cap = summary["new_courses_credit_cap"]
    if cap is not None and new_hours > cap:
        raise TimetableProvenanceError(f"new hours {new_hours} exceed the cap {cap}")
    # 4. keeping the current sections means none of them was swapped.
    if keep_current and facts.section_replacements:
        raise TimetableProvenanceError(
            "keep-current mode produced a section replacement, which it cannot"
        )


__all__ = [
    "CHANGE_ADD",
    "CHANGE_REPLACE_SECTION",
    "CHANGE_RETAIN",
    "OUTCOME_ALREADY_REGISTERED",
    "OUTCOME_NOT_PLACED",
    "OUTCOME_PLACED",
    "SOURCE_CURRENT_REGISTRATION",
    "SOURCE_STUDENT_REQUEST",
    "SOURCE_SYSTEM_RECOMMENDATION",
    "SOURCE_UNATTRIBUTED",
    "TIMETABLE_FACT_KEYS",
    "TimetableAnswerFacts",
    "TimetableProvenanceError",
    "baseline_sections",
    "build_timetable_facts",
    "verify",
]

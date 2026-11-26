"""Which elective row of the plan a registered course outside the plan counts for.

A FORECAST'S ASSUMPTION, NOT A RECORD. Nothing here is written anywhere, and
nothing here says what a student is registered in.

A plan lists its elective requirements as rows of its own: AI1 "Program Elective
I", FE1 "Free Elective", GSE1 "University Elective". The student registers a real
course instead (AI463, GS303), which is in no plan, and the registrar posts it
under the row's code once the grade lands. No student in the data has ever passed
a course that is not a plan row. Until then the row reads ``not_taken``, so a
forecast that takes it at face value schedules a requirement the student is
already meeting: one course too many, and often one term too many.

``elective_resolver`` settles this for the record, and only on proof: it marks a
row as studying when the department's mapping for the term names the registered
course for it. A forecast cannot wait for proof that mostly never comes (the
mapping of 1448/1 names 10 courses; 2,815 registrations outside a plan had none),
so it assumes, in this order, and says which it did:

    department_mapping   the term's mapping names the row for this course
    elective_catalogue   the course is one of the programme's electives: the
                         open Program Elective row of the same hours
    same_hours           otherwise an open Free or University Elective row of
                         the same hours, then any open elective row of them

always the row due earliest, never a Mandatory row, and one course for one row. A
course with no such row fills nothing and stays what it was: load.

CREDIT-NEUTRAL BY CONSTRUCTION. The caller counts a filled row at the PLAN's
hours, exactly as it counts a plan course in progress, and counts the course's
own hours toward nothing but the term's load. An earlier attempt (2026-08-31)
added the row's code to the passed set and not its hours; on the six plans whose
co-op gate is the plan total minus the co-op, 444 students lost their forecast.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.models import ElectiveCourse
from core.services.elective_validation import programme_variants
from core.services.student_helpers import is_elective_slot, normalize_code

BASIS_MAPPING = "department_mapping"
BASIS_CATALOGUE = "elective_catalogue"
BASIS_HOURS = "same_hours"


@dataclass(frozen=True)
class FilledRow:
    """One registered course and the plan row a forecast counts it for."""

    course_code: str
    row_code: str
    basis: str

    def as_dict(self) -> dict[str, str]:
        return {"course_code": self.course_code, "row_code": self.row_code, "basis": self.basis}


@dataclass(frozen=True)
class RowFilling:
    """What a term's courses outside the plan mean for the plan's elective rows."""

    #: The open rows those courses are counted for, in course order.
    filled: tuple[FilledRow, ...] = ()
    #: Elective rows listed in the term with no hours of their own that none of
    #: its courses carries. The resolver marks a row for a course in the
    #: timetable, and the row is listed beside that course with 0 hours; a
    #: what-if that drops the course leaves the row behind. It is then no
    #: longer in progress, whatever the stored mark says.
    unbacked: frozenset[str] = frozenset()


def is_elective_row(requirement_type: object) -> bool:
    """Any declared elective row: Program, Free, University or plain Elective.

    Wider than ``is_elective_slot`` on purpose. That one answers "is this row a
    placeholder that is never a course", which only a Program Elective is. This
    one answers "may a course outside the plan be posted under this row".
    """
    return "ELECTIVE" in str(requirement_type or "").upper()


def _term_mapping(
    program: str, academic_year: object, term: object, cache: dict[object, object] | None
) -> dict[str, set[str]]:
    key = ("elective_term_mapping", normalize_code(program), str(academic_year), str(term))
    cached = cache.get(key) if cache is not None else None
    if isinstance(cached, dict):
        return cached
    # Imported here: the resolver pulls in the reporting helpers, which this
    # module's callers are themselves imported by.
    from core.services.elective_resolver import elective_courses_by_placeholder

    mapping = {
        normalize_code(slot): {normalize_code(code) for code in codes}
        for slot, codes in elective_courses_by_placeholder(
            program, academic_year=str(academic_year), term=str(term)
        ).items()
    }
    if cache is not None:
        cache[key] = mapping
    return mapping


def _catalogue(program: str, cache: dict[object, object] | None) -> set[str]:
    key = ("elective_catalogue_codes", normalize_code(program))
    cached = cache.get(key) if cache is not None else None
    if isinstance(cached, set):
        return cached
    owners = set(programme_variants(program))
    codes = {
        normalize_code(code)
        for owner, code in ElectiveCourse.objects.values_list("programme", "course_code")
        if normalize_code(owner) in owners
    }
    if cache is not None:
        cache[key] = codes
    return codes


def elective_rows_filled(
    program: str,
    academic_year: object,
    term: object,
    *,
    current: Mapping[str, int],
    plan_rows: Mapping[str, Mapping[str, Any]],
    passed: set[str],
    query_cache: dict[object, object] | None = None,
) -> RowFilling:
    """The open elective rows this term's courses outside the plan are counted for.

    ``current`` is every course of the term with its own hours, plan rows
    included: a row already in progress (the resolver marked it, so the caller
    lists it) is spoken for by one of the courses and is not filled again.
    ``plan_rows`` gives each row's ``credits``, ``term`` and ``type``.
    """

    # A course is CODE::NAME where its section says so. The plan, the mapping and
    # the catalogue know it by the code alone.
    def bare(code: str) -> str:
        return code.partition("::")[0]

    def due(code: str) -> tuple[int, str]:
        return (int(plan_rows[code].get("term") or 0), code)

    def hours(code: str) -> int:
        return int(plan_rows[code].get("credits") or 0)

    elective = {code for code, row in plan_rows.items() if is_elective_row(row.get("type"))}
    # A row listed in the term with no hours of its own is there because one of
    # the term's courses carries it. (A row the plan itself gives no hours is
    # not such a row: 0 is then simply its figure.)
    carried_rows = sorted(
        (
            code
            for code in current
            if code in elective and code not in passed and current[code] <= 0 < hours(code)
        ),
        key=due,
    )
    outside = sorted(code for code in current if bare(code) not in plan_rows and code not in passed)
    if not outside:
        return RowFilling(unbacked=frozenset(carried_rows))
    mapped = _term_mapping(program, academic_year, term, query_cache)

    # EVERY STEP BELOW RUNS OVER ALL THE COURSES BEFORE THE NEXT ONE STARTS, so
    # weaker evidence for one course never takes what stronger evidence gives
    # another: the department's word, then the kind of row, then the hours.

    # Take each carried row's course out first, or it would go on to fill a
    # second row: the course the mapping names, else one of the row's hours (a
    # mapping is only published when the hours agree, and may be another term's).
    carrier_of: dict[str, str] = {}

    def carry(row: str, courses: list[str]) -> None:
        carrier = next((code for code in courses if code not in carrier_of.values()), None)
        if carrier is not None:
            carrier_of[row] = carrier

    for row in carried_rows:
        carry(row, [code for code in outside if bare(code) in mapped.get(row, ())])
    for row in carried_rows:
        if row not in carrier_of:
            carry(row, [code for code in outside if current[code] == hours(row)])
    spoken_for = set(carrier_of.values())
    unbacked = frozenset(row for row in carried_rows if row not in carrier_of)

    open_rows = sorted(
        (code for code in elective if code not in passed and code not in current), key=due
    )
    row_of: dict[str, tuple[str, str]] = {}

    def take(course: str, rows: list[str], basis: str) -> None:
        taken = {row for row, _basis in row_of.values()}
        row = next((code for code in rows if code not in taken), None)
        if row is not None:
            row_of[course] = (row, basis)

    free = [course for course in outside if course not in spoken_for]
    for course in free:
        take(
            course, [row for row in open_rows if bare(course) in mapped.get(row, ())], BASIS_MAPPING
        )
    # Past the mapping only the hours can match a course to a row, so a course
    # whose hours no source knows stops here.
    unplaced = [course for course in free if course not in row_of and current[course] > 0]
    if unplaced:
        catalogue = _catalogue(program, query_cache)
        # A programme elective to a Program Elective row; anything else to a row
        # the registrar resolves.
        for course in unplaced:
            listed = bare(course) in catalogue
            take(
                course,
                [
                    row
                    for row in open_rows
                    if hours(row) == current[course]
                    and is_elective_slot(plan_rows[row].get("type")) == listed
                ],
                BASIS_CATALOGUE if listed else BASIS_HOURS,
            )
        # Then whatever elective row of its hours is still open.
        for course in unplaced:
            if course not in row_of:
                take(
                    course,
                    [row for row in open_rows if hours(row) == current[course]],
                    BASIS_HOURS,
                )
    filled = tuple(FilledRow(course, *row_of[course]) for course in outside if course in row_of)
    return RowFilling(filled=filled, unbacked=unbacked)

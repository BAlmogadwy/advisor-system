"""Does every prerequisite name a course the programme's own plan contains?

A `Prerequisite` row is scoped to a programme, but nothing in the schema ties the
codes it names back to that programme's `ProgrammeRequirement` plan. A row may
therefore point at a course that does not exist anywhere in the plan it governs,
and **no layer will ever complain**: the recommender simply never satisfies it.

That is not a theoretical hole. `DS2/MATH471 -> MATH204` was one such row. The
registrar's DS2 plan document reproduced Calculus II under its first-cohort number
(`MATH204`) after the second-cohort plans had renumbered it to `MATH106`, and the
database faithfully imported the mistake. Nobody could take `MATH204` in DS2, so
`MATH471` was permanently blocked; its three credits pushed every DS2 student
below the zero-slack `147(HOURS)` gate on the co-op course `DS492`; and two
unresolved requirements set `estimated_additional_terms` to `None`. **All 191 DS2
students lost their graduation forecast to one wrong cell**, and the failure was
silent -- an absent forecast, never an error.

The class is cheap to detect and expensive to find by hand, so it gets a checker.
`find_orphan_prerequisites` is the single implementation; the management command
`curriculum_integrity_report` runs it against real data and the regression test
pins it.

Deliberately NOT a write-path guard. The Oracle plan importer inserts a course's
prerequisites immediately after its own requirement row, so a forward reference to
a course later in the same file is normal and momentary. Refusing those writes
would break legitimate imports; auditing after the fact would not.

Credit-hour gates such as ``147(HOURS)`` are pseudo-prerequisites, not course
codes, and are excluded here for the same reason `split_hour_prereqs` exists.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from core.models import ElectiveCourse, Prerequisite, ProgrammeRequirement
from core.services.eligibility import split_hour_prereqs
from core.services.student_helpers import normalize_code

#: The row names a course that the programme's plan does not contain.
PREREQUISITE_NOT_IN_PLAN = "PREREQUISITE_NOT_IN_PLAN"
#: The row governs a course that the programme's plan does not contain.
COURSE_NOT_IN_PLAN = "COURSE_NOT_IN_PLAN"
#: The programme has prerequisite rows but no plan at all to check them against.
PROGRAMME_HAS_NO_PLAN = "PROGRAMME_HAS_NO_PLAN"


@dataclass(frozen=True)
class OrphanPrerequisite:
    """One prerequisite cell that names a course outside its programme's plan."""

    prerequisite_id: int
    program: str
    course_code: str
    referenced_course_code: str
    reason: str

    def describe(self) -> str:
        return (
            f"{self.program}/{self.course_code} -> {self.referenced_course_code} "
            f"[{self.reason}] (prerequisites.id={self.prerequisite_id})"
        )


def _plans_by_program() -> dict[str, set[str]]:
    """Every course code a programme can legitimately name, by programme.

    The elective catalogue counts. `eligibility._course_exists_in_program` is the
    project's existing answer to "is this course in this programme", and it
    consults `ElectiveCourse` as well as the declared plan -- a plan carries
    placeholder slots (AI1, AI2) that the catalogue resolves. Checking only
    `ProgrammeRequirement` would call a prerequisite naming a real elective an
    orphan. No live row depends on this today; the two definitions simply must
    not disagree.
    """

    plans: dict[str, set[str]] = defaultdict(set)
    for program, course_code in ProgrammeRequirement.objects.values_list("program", "course_code"):
        plans[normalize_code(program)].add(normalize_code(course_code))
    for programme, course_code in ElectiveCourse.objects.values_list("programme", "course_code"):
        plans[normalize_code(programme)].add(normalize_code(course_code))
    return plans


def find_orphan_prerequisites(*, programs: list[str] | None = None) -> list[OrphanPrerequisite]:
    """Every prerequisite cell naming a course absent from its programme's plan.

    An empty list is the healthy state. Results are ordered by programme, then
    course, then referenced course, so a report diffs cleanly between runs.

    A programme carrying prerequisite rows with no plan rows at all is reported
    once as ``PROGRAMME_HAS_NO_PLAN`` rather than once per row: the actionable
    fault is the missing plan, and one finding per row would bury it.
    """

    plans = _plans_by_program()
    # Explicitly ordered. Without an ORDER BY, SQLite returns rowid order but
    # PostgreSQL returns heap order, which shifts after any UPDATE or VACUUM.  The
    # finding list is sorted before it is returned either way, but a
    # PROGRAMME_HAS_NO_PLAN finding reports whichever row it saw first, so an
    # unordered scan would make that row -- and the report -- unstable in
    # production while looking stable locally.
    rows = Prerequisite.objects.order_by(
        "program", "course_code", "prerequisite_course_code", "id"
    ).values_list("id", "program", "course_code", "prerequisite_course_code")
    # Programme identity is compared normalised everywhere else in this codebase,
    # so the filter normalises too; a database-side `program__in` would silently
    # miss a row stored as "ds2" or with a stray space.
    wanted = {normalize_code(program) for program in programs} if programs is not None else None

    findings: list[OrphanPrerequisite] = []
    planless_reported: set[str] = set()
    for row_id, raw_program, raw_course, raw_cell in rows:
        program = normalize_code(raw_program)
        if wanted is not None and program not in wanted:
            continue
        course_code = normalize_code(raw_course)
        plan = plans.get(program)
        if not plan:
            if program not in planless_reported:
                planless_reported.add(program)
                findings.append(
                    OrphanPrerequisite(
                        prerequisite_id=int(row_id),
                        program=program,
                        course_code=course_code,
                        referenced_course_code="",
                        reason=PROGRAMME_HAS_NO_PLAN,
                    )
                )
            continue

        if course_code and course_code not in plan:
            findings.append(
                OrphanPrerequisite(
                    prerequisite_id=int(row_id),
                    program=program,
                    course_code=course_code,
                    referenced_course_code=course_code,
                    reason=COURSE_NOT_IN_PLAN,
                )
            )

        # The storage format allows a comma-separated cell; `get_program_prerequisites`
        # splits it, so the audit must split it identically or it would miss a
        # bad code hiding beside a good one.
        cell_codes = [normalize_code(part) for part in str(raw_cell or "").split(",")]
        course_prereqs, _hours = split_hour_prereqs([code for code in cell_codes if code])
        for prerequisite in course_prereqs:
            if prerequisite not in plan:
                findings.append(
                    OrphanPrerequisite(
                        prerequisite_id=int(row_id),
                        program=program,
                        course_code=course_code,
                        referenced_course_code=prerequisite,
                        reason=PREREQUISITE_NOT_IN_PLAN,
                    )
                )

    findings.sort(key=lambda f: (f.program, f.course_code, f.referenced_course_code, f.reason))
    return findings

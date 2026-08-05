"""Seed registered timetables from an approved registration plan workbook.

**This module never writes.** It parses, resolves and validates, and returns a
deterministic plan. The command decides whether to apply it. That split is what
makes the dry run trustworthy: the run that reports and the run that writes
compute the same thing from the same code.

WHY THE SECTION LABELS CANNOT BE MATCHED DIRECTLY

The workbook labels sections by programme — `AI:S1`, `DS2:S2`, `AI:` — while
`TermSection.section` holds `M1`..`M4`, because the earlier `alllsections.xlsx`
import stripped the prefix. Zero of the 50 labels match as strings.

They are resolved on MEETING TIMES instead, which for this workbook is exact: all
50 file sections map to exactly one database section, with no ambiguity and no
orphans. A resolution that is not one-to-one is refused rather than guessed —
seating a student in the wrong section of the right course is worse than not
seating them at all.

WHAT THIS WRITES INTO, AND WHY IT REFUSES SO MUCH

`StudentTermSection` is the authoritative registered timetable. The student home
screen, the adviser chat and the expected-versus-registered comparison all read
it, so one wrong link propagates into three surfaces and contradicts nothing that
would notice.

A data-integrity review of the first version found four of its seven claimed
properties violated. Each is now enforced by the mechanism rather than asserted
in a docstring:

  * **A duplicate `(course, label)` in the roster sheet silently overwrote the
    map**, so the LAST row won for every student using that label — half a roster
    seated in another section, `plan.ok` True, no diagnostic anywhere. This
    module's own docstring cites a bare `AI:` as a real workbook value, which is
    exactly the shape that collides. Now `DUPLICATE_SECTION_LABEL`.
  * **Nothing checked the student's cohort.** Sections are gender-segregated and
    the gender is the leading letter of the LABEL (`M1`, `F3`), which time
    matching never sees — so a male student could be seated in the sole `F2`
    section. Latent today (all 50 sections are `M1..M4`, all students `M`) and
    live the moment F sections land, which the wider section data already holds
    415 of.
  * **A time MISMATCH with exactly one candidate linked anyway.** Written as a
    kindness for sections whose clock moved; it also seats a student in a section
    matching nothing they were told. Now refused unless the operator opts in with
    `accept_moved_times`, having read the disagreement report. Measured cost of
    tightening it: none — `FINAL2` produces zero disagreements, so the real import
    never used that path.
  * **Scenario-owned sections were eligible match targets**, while the reader
    (`get_student_term_baseline`) filters them out — so a draft timetable could
    win the match and the row would be written and then invisible on every screen.

WHAT IS DELIBERATELY NOT WRITTEN

  * `TermSectionMeeting` — owner decision: link only, change no times. Every
    disagreement is REPORTED rather than repaired, because a student would
    otherwise be shown a time that contradicts the plan that placed them. (An
    earlier version of this docstring named `DS332`, `MATH471` and `AI225` as
    sections whose clock had moved. Measured against `FINAL2`: **zero** sections
    disagree, so nothing here depends on that. The reporting path stays, because
    the next workbook is not this one.)
  * `StudentCourse.status` — sections are what the plan assigned; status is what
    the registrar recorded. One import, one meaning.
  * `Project` and `Foundation retake` rows — 156 of them, and not by preference:
    they carry section `—` and "no timeslot (graduation project)" or "first-year
    schedule (offered elsewhere)". There is no section to link to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Rows the workbook itself marks as having no timeslot.
UNPLACEABLE_KINDS = frozenset({"Project", "Foundation retake"})

#: `Mon 09:00-10:15` -> (`MON`, `09:00`). The workbook writes three-letter English
#: days in title case; `TermSectionMeeting.day` stores them upper.
_MEETING = re.compile(r"(\w{3})\s+(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})")


@dataclass
class Problem:
    where: str
    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.where}: [{self.code}] {self.detail}"


@dataclass
class Section:
    """A database section, carrying the label the cohort check needs."""

    id: int
    label: str
    times: set[tuple[str, str]]


@dataclass
class Plan:
    """What an apply would do. The same object the dry run prints."""

    links: list[dict[str, Any]] = field(default_factory=list)
    students: set[int] = field(default_factory=set)
    skipped_unplaceable: int = 0
    section_map: dict[tuple[str, str], Section] = field(default_factory=dict)
    #: course -> the registrations it could not carry, because NO section for
    #: that course exists at all. Not a validation failure: a reported gap.
    uncovered: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    time_disagreements: list[dict[str, Any]] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)

    #: Conservation. Every detail row is accounted for by exactly one of these and
    #: `check_conservation` proves it. Without a rows-read counter, openpyxl's
    #: `None` for the continuation cells of a MERGED student-id range — the normal
    #: shape of a hand-authored plan — dropped whole students with no diagnostic
    #: under a cheerful summary.
    detail_rows_read: int = 0
    blank_rows: int = 0
    duplicate_rows: int = 0

    #: How many existing rows an apply would DELETE. The first version reported
    #: only what it would write, so a dry run could say "1 link for 1 student, 0
    #: problems" while the apply removed two rows — and against the real database
    #: it would remove all 1081 with no number shown anywhere.
    replaces: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        return (
            f"{len(self.links)} section links for {len(self.students)} students, "
            f"REPLACING {self.replaces} existing row(s) for those students; "
            f"{self.detail_rows_read} detail rows read "
            f"({self.blank_rows} blank, {self.duplicate_rows} duplicate, "
            f"{self.skipped_unplaceable} with no timeslot); "
            f"{sum(len(v) for v in self.uncovered.values())} row(s) in "
            f"{len(self.uncovered)} course(s) with no section on file; "
            f"{len(self.time_disagreements)} section(s) whose times differ from the "
            f"database; {len(self.problems)} problem(s)"
        )

    def check_conservation(self) -> None:
        """Every detail row read is in exactly one bucket, or say so loudly."""
        accounted = (
            len(self.links)
            + self.blank_rows
            + self.duplicate_rows
            + self.skipped_unplaceable
            + sum(len(v) for v in self.uncovered.values())
            + sum(1 for p in self.problems if p.where.startswith("detail row"))
        )
        if accounted != self.detail_rows_read:
            self.problems.append(
                Problem(
                    "detail sheet",
                    "ROWS_UNACCOUNTED_FOR",
                    f"{self.detail_rows_read} rows read but {accounted} accounted for; "
                    f"{self.detail_rows_read - accounted} vanished without a reason",
                )
            )


def parse_meetings(*cells: object) -> set[tuple[str, str]]:
    """`{(DAY, HH:MM)}` from one or more workbook time cells."""
    found: set[tuple[str, str]] = set()
    for cell in cells:
        for match in _MEETING.finditer(str(cell or "")):
            found.add((match.group(1).upper()[:3], match.group(2).zfill(5)))
    return found


def _database_sections() -> dict[str, list[Section]]:
    """Real sections only.

    `scenario__isnull=True` matters: `get_student_term_baseline` — the reader every
    screen goes through — filters scenario-owned sections out. Leaving them in here
    lets a draft timetable win a time match, and the row is then written and
    invisible everywhere. There are none today; this project has held 47 scenarios
    at once.
    """
    from core.models import TermSection, TermSectionMeeting

    meetings: dict[int, set[tuple[str, str]]] = {}
    for section_id, day, start in TermSectionMeeting.objects.values_list(
        "term_section_id", "day", "start_time"
    ):
        # Zero-filled on both sides: `9:00` and `09:00` are one time, not two keys.
        meetings.setdefault(section_id, set()).add((str(day).upper()[:3], str(start)[:5].zfill(5)))

    by_course: dict[str, list[Section]] = {}
    for row in TermSection.objects.filter(scenario__isnull=True).values(
        "id", "course_key", "section"
    ):
        course = str(row["course_key"] or "").strip().upper()
        by_course.setdefault(course, []).append(
            Section(
                id=row["id"],
                label=str(row["section"] or "").strip(),
                times=meetings.get(row["id"], set()),
            )
        )
    return by_course


def _student_cohorts(detail: list[tuple]) -> dict[int, str]:
    """`student_id -> 'M' | 'F' | ''` for every id the detail sheet names that HAS a
    `Student` row.

    Absence from this mapping means "no such student", which is a different failure
    from "student exists with no cohort recorded" and gets its own problem code.
    Collapsing them produced the worse diagnostic: an id that simply is not in the
    database was reported as an unresolvable cohort.
    """
    from core.models import Student

    ids: set[int] = set()
    for row in detail:
        if not row or row[0] in (None, ""):
            continue
        try:
            ids.add(int(str(row[0]).strip()))
        except ValueError:
            continue
    return {
        int(sid): str(section or "").strip().upper()[:1]
        for sid, section in Student.objects.filter(student_id__in=sorted(ids)).values_list(
            "student_id", "section"
        )
    }


def build_plan(
    rosters: list[tuple],
    detail: list[tuple],
    academic_year: str,
    term: str,
    *,
    accept_moved_times: bool = False,
) -> Plan:
    """Resolve every registration row to a database section, or refuse.

    `rosters` and `detail` are the two workbook sheets as raw row tuples, header
    row already removed.

    `accept_moved_times` opts in to linking a section whose times match nothing on
    file, where the course has exactly one section. That is a real case — the
    workbook moved `DS332`, `MATH471` and `AI225` to seat more students — but it is
    also how a student ends up in a section matching nothing they were told, so it
    is off unless an operator asks for it having read the disagreement report.
    """
    from core.services.student_sections import section_gender

    plan = Plan()
    by_course = _database_sections()
    cohorts = _student_cohorts(detail)

    # ── resolve each workbook section to exactly one database section ──
    for index, row in enumerate(rosters):
        if not row or not row[0]:
            continue
        where = f"rosters row {index + 2}"
        course = str(row[0]).strip().upper()
        label = str(row[1]).strip()
        key = (course, label)

        # A duplicate label is the worst failure this file can have: both rows
        # resolve cleanly, the second silently replaces the first, and every
        # student matched to the first is seated in the second's section.
        if key in plan.section_map:
            plan.problems.append(
                Problem(
                    where,
                    "DUPLICATE_SECTION_LABEL",
                    f"{course} {label!r} appears twice in the roster sheet; the second "
                    "would silently reseat everyone matched to the first",
                )
            )
            continue

        wanted = parse_meetings(row[2], row[3])
        candidates = by_course.get(course, [])
        matches = [s for s in candidates if s.times == wanted]

        if len(matches) == 1:
            plan.section_map[key] = matches[0]
            continue
        if not candidates:
            plan.problems.append(Problem(where, "NO_SUCH_COURSE", f"{course} has no sections"))
        elif not matches:
            plan.time_disagreements.append(
                {
                    "course": course,
                    "file_section": label,
                    "file_times": sorted(wanted),
                    "database_times": {s.id: sorted(s.times) for s in candidates},
                }
            )
            if accept_moved_times and len(candidates) == 1:
                plan.section_map[key] = candidates[0]
            else:
                plan.problems.append(
                    Problem(
                        where,
                        "TIME_MISMATCH",
                        f"{course} {label}: the workbook's times match none of the "
                        f"{len(candidates)} section(s) on file"
                        + (
                            " — pass --accept-moved-times to link it anyway"
                            if len(candidates) == 1
                            else ""
                        ),
                    )
                )
        else:
            plan.problems.append(
                Problem(
                    where,
                    "AMBIGUOUS_SECTION",
                    f"{course} {label}: {len(matches)} database sections share these times "
                    f"({', '.join(sorted(s.label for s in matches))})",
                )
            )

    if plan.problems:
        # All-or-nothing: a half-resolved map would seat some students correctly
        # and others in the wrong section of the right course.
        return plan

    # ── every student registration row ──
    seen: set[tuple[int, int]] = set()
    for index, row in enumerate(detail):
        if not row:
            continue
        plan.detail_rows_read += 1
        line = index + 2
        if row[0] in (None, ""):
            # openpyxl returns None for the continuation cells of a MERGED range,
            # which is how a hand-authored plan usually writes one id across a
            # student's course rows. Counted, never silently dropped.
            plan.blank_rows += 1
            continue
        try:
            student_id = int(str(row[0]).strip())
        except ValueError:
            plan.problems.append(Problem(f"detail row {line}", "BAD_STUDENT_ID", str(row[0])))
            continue

        if student_id not in cohorts:
            # No `Student` row at all. Caught here rather than left to
            # `check_students_exist` so the operator is told the actual reason —
            # the cohort check below would otherwise report it as an unresolvable
            # cohort, which is true but useless.
            plan.problems.append(
                Problem(
                    f"detail row {line}",
                    "UNKNOWN_STUDENT",
                    f"{student_id} has no Student record; this import never creates one",
                )
            )
            continue

        kind = str(row[3] or "").strip()
        if kind in UNPLACEABLE_KINDS:
            plan.skipped_unplaceable += 1
            continue

        course = str(row[2] or "").strip().upper()
        label = str(row[4] or "").strip()
        section = plan.section_map.get((course, label))
        if section is None:
            # TWO different failures, and collapsing them would hide the one that
            # matters. If the course has NO sections at all, this is the known
            # coverage gap: the plan gave it a real day and time — `GSE1` Sunday
            # 15:50 online, `FE2` Monday 09:00 — and nothing in `TermSection`
            # represents it. Recorded and skipped, so the rest of the term can be
            # seeded honestly.
            #
            # But if the course HAS sections and this label still did not resolve,
            # something is wrong with the mapping, and seeding around it would put
            # a student in the wrong section of the right course. That still fails.
            if not by_course.get(course):
                plan.uncovered.setdefault(course, []).append(
                    {
                        "student_id": student_id,
                        "kind": kind,
                        "course": course,
                        "times": sorted(parse_meetings(row[5], row[6])),
                    }
                )
                continue
            plan.problems.append(
                Problem(f"detail row {line}", "UNRESOLVED_SECTION", f"{course} {label!r}")
            )
            continue

        # THE COHORT CHECK. Sections are gender-segregated and the gender is the
        # leading letter of the LABEL, which time matching never sees. Without this
        # a male student lands in the sole `F2` section and all three surfaces show
        # it as their registered timetable.
        required = section_gender(section.label)
        cohort = cohorts.get(student_id, "")
        if required and cohort and required != cohort:
            plan.problems.append(
                Problem(
                    f"detail row {line}",
                    "COHORT_MISMATCH",
                    f"student {student_id} is cohort {cohort} and {course} "
                    f"{section.label} is cohort {required}",
                )
            )
            continue
        if required and not cohort:
            # `student_gender_strict` refuses rather than guesses for exactly this
            # reason: every real section is gendered, so an unresolved cohort is a
            # total failure rather than a partial one.
            plan.problems.append(
                Problem(
                    f"detail row {line}",
                    "UNKNOWN_COHORT",
                    f"student {student_id} has no resolvable cohort and {course} "
                    f"{section.label} is gendered",
                )
            )
            continue

        pair = (student_id, section.id)
        if pair in seen:
            plan.duplicate_rows += 1  # a lecture row and a lab row naming one section
            continue
        seen.add(pair)
        plan.students.add(student_id)
        plan.links.append(
            {
                "student_id": student_id,
                "term_section_id": section.id,
                "academic_year": str(academic_year),
                "term": str(term),
                "course": course,
                "file_section": label,
                "database_section": section.label,
            }
        )

    plan.check_conservation()
    if plan.ok:
        plan.replaces = count_rows_to_replace(plan, academic_year, term)
        _check_cross_term_collisions(plan, academic_year, term)
    return plan


def count_rows_to_replace(plan: Plan, academic_year: str, term: str) -> int:
    """Existing rows an apply would DELETE. Computed by the same code that reports."""
    from core.models import StudentTermSection

    if not plan.students:
        return 0
    return StudentTermSection.objects.filter(
        student_id__in=sorted(plan.students),
        academic_year=str(academic_year),
        term=str(term),
    ).count()


def _check_cross_term_collisions(plan: Plan, academic_year: str, term: str) -> None:
    """Refuse rather than let a uniqueness constraint eat rows in silence.

    `StudentTermSection` is unique on `(student_id, term_section)` ONLY, and
    `TermSection` carries no year or term — the same 50 section rows are shared by
    every term. So seeding term 2 for a student already seeded in term 1 collides:
    the DELETE is year/term-scoped, the INSERT is not, and `ignore_conflicts` used
    to swallow the difference and report the row as written.
    """
    from core.models import StudentTermSection

    if not plan.links:
        return
    wanted = {(link["student_id"], link["term_section_id"]) for link in plan.links}
    clashing = StudentTermSection.objects.filter(student_id__in=sorted(plan.students)).exclude(
        academic_year=str(academic_year), term=str(term)
    )
    for student_id, section_id, year, other in clashing.values_list(
        "student_id", "term_section_id", "academic_year", "term"
    ):
        if (student_id, section_id) in wanted:
            plan.problems.append(
                Problem(
                    f"student {student_id}",
                    "CROSS_TERM_COLLISION",
                    f"section #{section_id} is already recorded for {year}/{other}, and the "
                    "uniqueness constraint spans terms — this row cannot be written without "
                    "removing that one",
                )
            )


def check_students_exist(plan: Plan) -> list[int]:
    """Student ids in the plan with no `Student` row. Never auto-created."""
    from core.models import Student

    known = set(
        Student.objects.filter(student_id__in=list(plan.students)).values_list(
            "student_id", flat=True
        )
    )
    return sorted(plan.students - known)


def apply_plan(plan: Plan, academic_year: str, term: str) -> dict[str, int]:
    """Write a validated plan atomically.

    Replaces the term for THE STUDENTS IN THE PLAN ONLY. A student absent from the
    workbook keeps whatever they have — the two the plan could not place are not
    silently emptied by an import that never considered them.

    Every guard the command performs is repeated here. This function is exported,
    and `StudentTermSection.student_id` is a plain integer with no foreign key, so
    a direct call could otherwise write a link for a student who does not exist —
    a class of orphan this database already holds 722 of.
    """
    from django.db import transaction

    from core.models import StudentTermSection

    if not plan.ok:
        raise ValueError("refusing to apply a plan that failed validation")
    missing = check_students_exist(plan)
    if missing:
        raise ValueError(
            f"refusing to write links for {len(missing)} unknown student(s): {missing[:10]}"
        )
    # The links carry the term `build_plan` was given; the delete uses the term
    # passed here. Nothing used to check they agree, so a mismatched pair emptied
    # one term and wrote into another.
    terms = {(link["academic_year"], link["term"]) for link in plan.links}
    if terms and terms != {(str(academic_year), str(term))}:
        raise ValueError(
            f"the plan was built for {sorted(terms)} but is being applied to "
            f"{(str(academic_year), str(term))}"
        )

    with transaction.atomic():
        removed = StudentTermSection.objects.filter(
            student_id__in=sorted(plan.students),
            academic_year=str(academic_year),
            term=str(term),
        ).delete()[0]
        # No `ignore_conflicts`: it turned a uniqueness violation into a silently
        # missing row while the caller was told the link had been written.
        # Cross-term collisions are detected in `build_plan`, so a violation here
        # is a genuine surprise and must raise.
        StudentTermSection.objects.bulk_create(
            [
                StudentTermSection(
                    student_id=link["student_id"],
                    academic_year=link["academic_year"],
                    term=link["term"],
                    term_section_id=link["term_section_id"],
                    # Carries the term it was actually built for. A hardcoded
                    # literal made a 1449/2 import claim to be `…_1448_t1`, and
                    # `source` is the only provenance a row has — the field an
                    # operator would use to find and undo this import.
                    source=f"registration_plan_{academic_year}_t{term}",
                )
                for link in plan.links
            ]
        )
        # MEASURED, not promised. The first version returned `len(plan.links)`
        # whatever the database actually did.
        written = StudentTermSection.objects.filter(
            student_id__in=sorted(plan.students),
            academic_year=str(academic_year),
            term=str(term),
        ).count()

    if written != len(plan.links):
        raise ValueError(
            f"planned {len(plan.links)} links but {written} rows exist after the write"
        )
    return {"removed": removed, "written": written, "students": len(plan.students)}


__all__ = [
    "UNPLACEABLE_KINDS",
    "Plan",
    "Problem",
    "Section",
    "apply_plan",
    "build_plan",
    "check_students_exist",
    "count_rows_to_replace",
    "parse_meetings",
]

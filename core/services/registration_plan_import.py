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

WHAT IS DELIBERATELY NOT WRITTEN

  * `TermSectionMeeting` — the workbook MOVED some section times to seat more
    students (`DS332`, `MATH471`, `AI225`) and applied fixes to others. Owner
    decision: link only, change no times. Every disagreement is REPORTED, because
    a student would otherwise be shown a time that contradicts the plan that
    placed them.
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
class Plan:
    """What an apply would do. The same object the dry run prints."""

    links: list[dict[str, Any]] = field(default_factory=list)
    students: set[int] = field(default_factory=set)
    skipped_unplaceable: int = 0
    section_map: dict[tuple[str, str], int] = field(default_factory=dict)
    #: course -> the registrations it could not carry, because NO section for
    #: that course exists at all. Not a validation failure: a reported gap.
    uncovered: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    time_disagreements: list[dict[str, Any]] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        return (
            f"{len(self.links)} section links for {len(self.students)} students; "
            f"{self.skipped_unplaceable} rows with no timeslot skipped; "
            f"{sum(len(v) for v in self.uncovered.values())} rows in "
            f"{len(self.uncovered)} course(s) with no section on file; "
            f"{len(self.time_disagreements)} section(s) whose times differ from the database; "
            f"{len(self.problems)} problem(s)"
        )


def parse_meetings(*cells: object) -> set[tuple[str, str]]:
    """`{(DAY, HH:MM)}` from one or more workbook time cells."""
    found: set[tuple[str, str]] = set()
    for cell in cells:
        for match in _MEETING.finditer(str(cell or "")):
            found.add((match.group(1).upper()[:3], match.group(2).zfill(5)))
    return found


def _database_sections() -> dict[str, dict[int, set[tuple[str, str]]]]:
    from core.models import TermSection, TermSectionMeeting

    meetings: dict[int, set[tuple[str, str]]] = {}
    for section_id, day, start in TermSectionMeeting.objects.values_list(
        "term_section_id", "day", "start_time"
    ):
        meetings.setdefault(section_id, set()).add((str(day).upper()[:3], str(start)[:5]))

    by_course: dict[str, dict[int, set[tuple[str, str]]]] = {}
    for row in TermSection.objects.values("id", "course_key"):
        course = str(row["course_key"] or "").strip().upper()
        by_course.setdefault(course, {})[row["id"]] = meetings.get(row["id"], set())
    return by_course


def build_plan(rosters: list[tuple], detail: list[tuple], academic_year: str, term: str) -> Plan:
    """Resolve every registration row to a database section, or refuse.

    `rosters` and `detail` are the two workbook sheets as raw row tuples, header
    row already removed.
    """
    plan = Plan()
    by_course = _database_sections()

    # ── resolve each workbook section to exactly one database section ──
    for index, row in enumerate(rosters):
        if not row or not row[0]:
            continue
        course = str(row[0]).strip().upper()
        label = str(row[1]).strip()
        wanted = parse_meetings(row[2], row[3])
        candidates = by_course.get(course, {})

        matches = [sid for sid, times in candidates.items() if times == wanted]
        if len(matches) == 1:
            plan.section_map[(course, label)] = matches[0]
            continue
        if not candidates:
            plan.problems.append(
                Problem(f"rosters row {index + 2}", "NO_SUCH_COURSE", f"{course} has no sections")
            )
        elif not matches:
            # Times differ. Report what the database holds so the disagreement is
            # legible, and fall back to an unambiguous single section if there is
            # exactly one — the course still exists, only its clock moved.
            plan.time_disagreements.append(
                {
                    "course": course,
                    "file_section": label,
                    "file_times": sorted(wanted),
                    "database_times": {sid: sorted(t) for sid, t in candidates.items()},
                }
            )
            if len(candidates) == 1:
                plan.section_map[(course, label)] = next(iter(candidates))
            else:
                plan.problems.append(
                    Problem(
                        f"rosters row {index + 2}",
                        "AMBIGUOUS_AFTER_TIME_MISMATCH",
                        f"{course} {label}: times match no section and {len(candidates)} "
                        "sections exist, so the right one cannot be determined",
                    )
                )
        else:
            plan.problems.append(
                Problem(
                    f"rosters row {index + 2}",
                    "AMBIGUOUS_SECTION",
                    f"{course} {label}: {len(matches)} database sections share these times",
                )
            )

    if plan.problems:
        # All-or-nothing: a half-resolved map would seat some students correctly
        # and others in the wrong section of the right course.
        return plan

    # ── every student registration row ──
    seen: set[tuple[int, int]] = set()
    for index, row in enumerate(detail):
        if not row or not row[0]:
            continue
        line = index + 2
        try:
            student_id = int(str(row[0]).strip())
        except ValueError:
            plan.problems.append(Problem(f"detail row {line}", "BAD_STUDENT_ID", str(row[0])))
            continue

        kind = str(row[3] or "").strip()
        if kind in UNPLACEABLE_KINDS:
            plan.skipped_unplaceable += 1
            continue

        course = str(row[2] or "").strip().upper()
        label = str(row[4] or "").strip()
        section_id = plan.section_map.get((course, label))
        if section_id is None:
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
                        "times": sorted(parse_meetings(row[5], row[6])),
                    }
                )
                continue
            plan.problems.append(
                Problem(f"detail row {line}", "UNRESOLVED_SECTION", f"{course} {label!r}")
            )
            continue

        key = (student_id, section_id)
        if key in seen:
            continue  # the same section listed twice for one student is not two seats
        seen.add(key)
        plan.students.add(student_id)
        plan.links.append(
            {
                "student_id": student_id,
                "term_section_id": section_id,
                "academic_year": str(academic_year),
                "term": str(term),
                "course": course,
                "file_section": label,
            }
        )

    return plan


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
    """
    from django.db import transaction

    from core.models import StudentTermSection

    if not plan.ok:
        raise ValueError("refusing to apply a plan that failed validation")

    with transaction.atomic():
        removed = StudentTermSection.objects.filter(
            student_id__in=sorted(plan.students),
            academic_year=str(academic_year),
            term=str(term),
        ).delete()[0]
        StudentTermSection.objects.bulk_create(
            [
                StudentTermSection(
                    student_id=link["student_id"],
                    academic_year=link["academic_year"],
                    term=link["term"],
                    term_section_id=link["term_section_id"],
                    source="registration_plan_1448_t1",
                )
                for link in plan.links
            ],
            ignore_conflicts=True,
        )
    return {"removed": removed, "written": len(plan.links), "students": len(plan.students)}


__all__ = [
    "UNPLACEABLE_KINDS",
    "Plan",
    "Problem",
    "apply_plan",
    "build_plan",
    "check_students_exist",
    "parse_meetings",
]

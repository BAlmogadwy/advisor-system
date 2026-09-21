"""Seed the female-cohort expected timetable from the two optimiser workbooks.

WHY THIS IS NOT ``registration_plan_import``

That importer resolves sections by MEETING TIMES, because its workbook labelled
sections by programme (`AI:S1`) while the database held `M1` and zero labels
matched as strings. These workbooks are the opposite case: they carry the section
label the database uses (`F1`, `F32`) AND explicit day/time columns. Matching on
times here would throw away the identity the file already states, and would fail
outright for the sections that have no timeslot at all.

What IS borrowed is every guard, because the failure modes are identical: a
duplicate label silently overwriting a map, a student seated in the other cohort's
section, a dry run that reports something different from what the apply does, and
a partial write. Planning is side-effect-free; :func:`apply_plan` is the only
writer and applies only a validated plan, atomically.

THE TWO FILES, AND WHY BOTH ARE NEEDED

* the TIMETABLE workbook defines the sections — code, number, credits, label,
  days, times, room — and is the only place the day lives. The portal's own HTML
  export draws days as coloured rectangles positioned by ``colspan``, which cannot
  be read back reliably; these columns can.
* the REGISTRATION workbook says which student sits in which section.

A section named by the roster and absent from the timetable is refused rather than
invented: seating a student in a section nobody scheduled is worse than not
seating them.

DAYS THAT ARE NOT DAYS

``Days (EN)`` carries values like ``Monday + Wednesday`` — one row describing two
meetings — and ``—`` for a course with no timeslot at all: a graduation project, a
Program Elective placeholder. The first is split; the second produces a section
with NO meetings, which the rest of this system already models (see
``get_student_term_baseline``, which emits a row with an empty day, and the
student portal's "Courses without a scheduled time" list).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Sheet names, in both languages as the workbooks write them.
ROSTER_SHEET = "قوائم الشعب Section Roster"
SCHEDULE_SHEET = "الجدول Schedule"

#: The programmes this import is scoped to. A roster row for anything else is a
#: refusal, not a silent skip: the file is supposed to contain only these.
ALLOWED_PROGRAMS = frozenset({"AI", "AI2", "DS", "DS2"})

#: `Days (EN)` values meaning "this section has no timeslot".
NO_TIMESLOT = frozenset({"—", "-", "", "None", "بدون موعد"})

_DAY_TO_CODE = {
    "SUNDAY": "SUN",
    "MONDAY": "MON",
    "TUESDAY": "TUE",
    "WEDNESDAY": "WED",
    "THURSDAY": "THU",
    "FRIDAY": "FRI",
    "SATURDAY": "SAT",
}


@dataclass
class Problem:
    where: str
    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.where}: [{self.code}] {self.detail}"


@dataclass
class Section:
    course_key: str
    course_code: str
    course_number: str
    course_name: str
    label: str
    credits: int
    #: ``(DAY, "HH:MM", "HH:MM", room)`` — empty for a section with no timeslot.
    meetings: list[tuple[str, str, str, str]] = field(default_factory=list)


@dataclass
class Plan:
    """What an apply would do. The same object the dry run prints."""

    sections: dict[str, Section] = field(default_factory=dict)
    #: ``(student_id, section_key)`` — one per roster row that will be written.
    links: list[tuple[int, str]] = field(default_factory=list)
    students: set[int] = field(default_factory=set)
    problems: list[Problem] = field(default_factory=list)
    notices: list[Problem] = field(default_factory=list)
    roster_rows_read: int = 0
    duplicate_rows: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        scheduled = sum(1 for s in self.sections.values() if s.meetings)
        meetings = sum(len(s.meetings) for s in self.sections.values())
        return (
            f"{len(self.links)} links for {len(self.students)} students across "
            f"{len(self.sections)} sections ({scheduled} scheduled, "
            f"{len(self.sections) - scheduled} with no timeslot, {meetings} meetings); "
            f"{self.roster_rows_read} roster rows read ({self.duplicate_rows} duplicate); "
            f"{len(self.notices)} notice(s); {len(self.problems)} problem(s)"
        )

    def check_conservation(self) -> None:
        """Every roster row is a link, a duplicate, or a problem. Or say so."""
        accounted = len(self.links) + self.duplicate_rows
        if self.problems:
            return
        if accounted != self.roster_rows_read:
            self.problems.append(
                Problem(
                    "conservation",
                    "ROWS_UNACCOUNTED",
                    f"{self.roster_rows_read} roster rows read but {accounted} accounted for",
                )
            )


def _text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def _clock(value: object) -> str:
    """`datetime.time`, `'18:00:00'` or `'18:00'` -> `'18:00'`. '' when unusable."""
    if value is None:
        return ""
    if hasattr(value, "hour") and hasattr(value, "minute"):
        return f"{value.hour:02d}:{value.minute:02d}"
    m = re.match(r"^\s*(\d{1,2}):(\d{2})", str(value))
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""


def parse_days(value: object) -> list[str]:
    """`'Monday + Wednesday'` -> `['MON', 'WED']`; `'—'` -> `[]`.

    Unknown day names return ``None`` so the caller can refuse rather than drop a
    meeting silently — a dropped meeting is a student told they are free when they
    are not.
    """
    raw = _text(value)
    if raw in NO_TIMESLOT:
        return []
    out: list[str] = []
    for part in re.split(r"[+,/&]| and ", raw):
        key = part.strip().upper()
        if not key:
            continue
        if key not in _DAY_TO_CODE:
            raise ValueError(f"unrecognised day {part.strip()!r}")
        out.append(_DAY_TO_CODE[key])
    return out


def section_key(course_key: str, label: str) -> str:
    return f"{course_key}|{label}"


def build_plan(schedule_rows: list[dict], roster_rows: list[dict]) -> Plan:
    """Validate and resolve. Touches no database and writes nothing."""
    from core.services.student_helpers import normalize_code

    plan = Plan()

    # ── the timetable defines the sections ──────────────────────────────────
    for i, row in enumerate(schedule_rows, start=2):
        course_key = normalize_code(_text(row.get("Course")))
        label = _text(row.get("Section"))
        if not course_key or not label:
            plan.problems.append(
                Problem(f"schedule row {i}", "INCOMPLETE_SECTION", "missing course or section")
            )
            continue
        if not label.upper().startswith("F"):
            plan.problems.append(
                Problem(f"schedule row {i}", "NOT_A_FEMALE_SECTION", f"section {label!r}")
            )
            continue
        try:
            days = parse_days(row.get("Days (EN)"))
        except ValueError as exc:
            plan.problems.append(Problem(f"schedule row {i}", "UNKNOWN_DAY", str(exc)))
            continue
        start, end = _clock(row.get("From")), _clock(row.get("To"))
        if days and not (start and end):
            plan.problems.append(
                Problem(
                    f"schedule row {i}",
                    "MISSING_TIME",
                    f"{course_key}/{label} has days but no time",
                )
            )
            continue
        if days and start >= end:
            plan.problems.append(
                Problem(f"schedule row {i}", "BAD_TIME", f"{course_key}/{label} {start}-{end}")
            )
            continue

        key = section_key(course_key, label)
        section = plan.sections.get(key)
        if section is None:
            try:
                credits = int(float(_text(row.get("Units")) or 0))
            except ValueError:
                credits = 0
            section = Section(
                course_key=course_key,
                course_code=_text(row.get("Code")),
                course_number=_text(row.get("No.")),
                course_name=_text(row.get("Course Name")),
                label=label,
                credits=credits,
            )
            plan.sections[key] = section
        room = _text(row.get("Room"))
        for day in days:
            meeting = (day, start, end, room)
            # One row per (day, time); the workbook repeats a section across rows
            # for multi-slot courses, and `Monday + Wednesday` expands here.
            if meeting not in section.meetings:
                section.meetings.append(meeting)

    # ── the roster says who sits in them ────────────────────────────────────
    seen: set[tuple[int, str]] = set()
    for i, row in enumerate(roster_rows, start=2):
        plan.roster_rows_read += 1
        raw_sid = _text(row.get("Student ID"))
        if not raw_sid.isdigit():
            plan.problems.append(
                Problem(f"roster row {i}", "BLANK_STUDENT_ID", f"student id {raw_sid!r}")
            )
            continue
        student_id = int(raw_sid)
        program = _text(row.get("Program")).upper()
        if program not in ALLOWED_PROGRAMS:
            plan.problems.append(
                Problem(f"roster row {i}", "PROGRAM_OUT_OF_SCOPE", f"programme {program!r}")
            )
            continue
        course_key = normalize_code(_text(row.get("Course")))
        label = _text(row.get("Section"))
        key = section_key(course_key, label)
        if key not in plan.sections:
            plan.problems.append(
                Problem(
                    f"roster row {i}",
                    "SECTION_NOT_SCHEDULED",
                    f"{course_key}/{label} is in the roster but not in the timetable",
                )
            )
            continue
        pair = (student_id, key)
        if pair in seen:
            plan.duplicate_rows += 1
            continue
        seen.add(pair)
        plan.links.append(pair)
        plan.students.add(student_id)

    # A student seated twice in ONE course is a registry state to refuse, not to
    # resolve: picking either section is a coin flip the student cannot see.
    per_course: dict[tuple[int, str], set[str]] = {}
    for student_id, key in plan.links:
        course_key, label = key.split("|", 1)
        per_course.setdefault((student_id, course_key), set()).add(label)
    for (student_id, course_key), labels in sorted(per_course.items()):
        if len(labels) > 1:
            plan.problems.append(
                Problem(
                    f"student {student_id}",
                    "TWO_SECTIONS_ONE_COURSE",
                    f"{course_key} in {sorted(labels)}",
                )
            )

    unused = sorted(k for k in plan.sections if k not in {key for _, key in plan.links})
    if unused:
        plan.notices.append(
            Problem(
                "timetable",
                "SECTION_WITH_NO_ROSTER",
                f"{len(unused)} scheduled section(s) nobody is assigned to (e.g. "
                f"{', '.join(u.replace('|', '/') for u in unused[:5])})",
            )
        )

    plan.check_conservation()
    return plan


def check_students(plan: Plan) -> tuple[list[int], list[int]]:
    """Return (missing from `Student`, present but not cohort F). Never created."""
    from core.models import Student

    ids = sorted(plan.students)
    known = {
        sid: (sec or "").strip().upper()
        for sid, sec in Student.objects.filter(student_id__in=ids).values_list(
            "student_id", "section"
        )
    }
    missing = [sid for sid in ids if sid not in known]
    wrong_cohort = [sid for sid in ids if sid in known and known[sid] != "F"]
    return missing, wrong_cohort


def check_time_disagreements(plan: Plan) -> list[dict[str, object]]:
    """Sections whose stored meetings differ from the workbook's. REPORTED, never repaired.

    This is the established owner decision for an expected-plan import, stated in
    ``registration_plan_import``: LINK ONLY, CHANGE NO TIMES. A student is placed
    by a plan; showing them a time that contradicts the plan that placed them is
    the harm, and silently rewriting whatever another source imported is how that
    happens.

    So a section that ALREADY carries meetings is left exactly as it is and any
    difference is surfaced here. A section this import must create, or one that
    carries no meetings at all, has nothing to contradict — the workbook is then
    the only authority for it, and writing its times is establishing them rather
    than overruling anyone.
    """
    from core.models import TermSection, TermSectionMeeting

    out: list[dict[str, object]] = []
    for key, section in sorted(plan.sections.items()):
        if not section.meetings:
            continue
        ts = TermSection.objects.filter(
            scenario__isnull=True, course_key=section.course_key, section=section.label
        ).first()
        if ts is None:
            continue
        stored = {
            (m.day, m.start_time, m.end_time, m.room)
            for m in TermSectionMeeting.objects.filter(term_section=ts)
        }
        if not stored:
            continue
        wanted = set(section.meetings)
        if stored != wanted:
            out.append(
                {
                    "section": key.replace("|", "/"),
                    "stored": sorted(stored),
                    "workbook": sorted(wanted),
                }
            )
    return out


def apply_plan(plan: Plan, academic_year: str, term: str, source: str) -> dict[str, int]:
    """Write a validated plan atomically. The only writer in this module.

    Every guard the command performs is repeated here, because this function is
    exported and ``StudentTermSection.student_id`` is a plain integer with no
    foreign key — a direct call could otherwise write links for students who do
    not exist, a class of orphan this database already holds many of.
    """
    from django.db import transaction

    from core.models import StudentTermSection, TermSection, TermSectionMeeting
    from core.services.section_programmes import reconcile_observed_section_programs
    from core.services.timetable_snapshots import SnapshotClass, classify_source

    if not plan.ok:
        raise ValueError("refusing to apply a plan that failed validation")
    if classify_source(source) is not SnapshotClass.EXPECTED:
        raise ValueError(
            f"{source!r} is not an expected-plan source; this importer seeds the "
            "EXPECTED timetable and must not write registrar evidence"
        )
    missing, wrong_cohort = check_students(plan)
    if missing:
        raise ValueError(f"refusing to write links for {len(missing)} unknown student(s)")
    if wrong_cohort:
        raise ValueError(
            f"refusing to seat {len(wrong_cohort)} non-F student(s) in female sections: "
            f"{wrong_cohort[:10]}"
        )

    created_sections = 0
    meetings_written = 0
    with transaction.atomic():
        section_ids: dict[str, int] = {}
        for key, section in plan.sections.items():
            ts = TermSection.objects.filter(
                scenario__isnull=True, course_key=section.course_key, section=section.label
            ).first()
            if ts is None:
                ts = TermSection.objects.create(
                    source_tag="f_optimised_plan",
                    course_name=section.course_name,
                    course_code=section.course_code,
                    course_number=section.course_number,
                    course_key=section.course_key,
                    section=section.label,
                    source_file="F_Course_Sections_Timetable_1448_S1.xlsx",
                )
                created_sections += 1
            section_ids[key] = int(ts.id)

            # LINK ONLY, CHANGE NO TIMES — the established rule for an
            # expected-plan import. Times are written ONLY where there are none to
            # contradict: a section this import just created, or one stored with no
            # meetings at all. A section that already carries meetings is left
            # untouched and any difference is reported by
            # `check_time_disagreements`, never repaired here.
            if not section.meetings:
                continue
            if TermSectionMeeting.objects.filter(term_section=ts).exists():
                continue
            for day, start, end, room in sorted(set(section.meetings)):
                TermSectionMeeting.objects.create(
                    term_section=ts,
                    day=day,
                    start_time=start,
                    end_time=end,
                    room=room,
                    instructor="",
                )
                meetings_written += 1

        target = StudentTermSection.objects.filter(
            student_id__in=sorted(plan.students),
            academic_year=str(academic_year),
            term=str(term),
            source=source,
        )
        affected = set(target.values_list("term_section_id", flat=True))
        removed = target.delete()[0]

        StudentTermSection.objects.bulk_create(
            [
                StudentTermSection(
                    student_id=student_id,
                    academic_year=str(academic_year),
                    term=str(term),
                    term_section_id=section_ids[key],
                    source=source,
                )
                for student_id, key in plan.links
            ]
        )
        written = StudentTermSection.objects.filter(
            student_id__in=sorted(plan.students),
            academic_year=str(academic_year),
            term=str(term),
            source=source,
        ).count()
        # MEASURED, not promised, and INSIDE the transaction so a short write
        # rolls back rather than being reported after it has committed.
        if written != len(plan.links):
            raise ValueError(f"planned {len(plan.links)} links but {written} rows exist after")

        affected.update(section_ids.values())
        reconcile_observed_section_programs(affected)

    return {
        "sections_created": created_sections,
        "meetings_written": meetings_written,
        "sections_total": len(plan.sections),
        "removed": removed,
        "written": written,
        "students": len(plan.students),
    }


__all__ = [
    "ALLOWED_PROGRAMS",
    "Plan",
    "Problem",
    "Section",
    "apply_plan",
    "build_plan",
    "check_students",
    "check_time_disagreements",
    "parse_days",
    "section_key",
]

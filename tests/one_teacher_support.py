"""Builders shared by the one-teacher-per-section tests (not collected)."""

from __future__ import annotations

from collections.abc import Iterable

from core.models import (
    CourseInstructor,
    DeliveryBoard,
    Instructor,
    ProgrammeRequirement,
    ScenarioSectionBudget,
    SectionInstructor,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.course_identity import planner_course_key
from core.services.section_teacher_assignment import D10_APPLIES_ABOVE, D10_MAX_SECTIONS, Slot
from core.services.timetable_pr4_instructor import normalise_instructor

DAYS = ("SUN", "MON", "TUE", "WED", "THU")
FLAG_ON = {
    "TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED": True,
    "TIMETABLE_INSTRUCTOR_LINKS_ENABLED": True,
    "TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED": True,
    "TIMETABLE_INSTRUCTOR_DAILY_CAP": 3,
    "TIMETABLE_ONE_TEACHER_PER_SECTION": True,
}


def hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def mins(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


# ── Pure slots ────────────────────────────────────────────────────────────


def course(
    course_key: str,
    sections: Iterable[dict],
    *,
    eligible: tuple[int, ...] = (1, 2),
    nominal: int = 3,
    planned: int | None = None,
    cap: object = "d10",
) -> list[Slot]:
    """One course's slots. Each section dict may set label, meetings
    [(day, start, end)], placed, fixed, kind, current, nominal, eligible, campus."""
    rows = list(sections)
    n_c = len(rows)
    n_course = planned if planned is not None else n_c
    course_cap = (
        (D10_MAX_SECTIONS if n_course > D10_APPLIES_ABOVE else None) if cap == "d10" else cap
    )
    out = []
    for rank, row in enumerate(rows, start=1):
        meetings = tuple(row.get("meetings", ()))
        out.append(
            Slot(
                course_key=course_key,
                label=row.get("label", f"S{rank}"),
                rank=rank,
                n_c=n_c,
                term_section_id=None,
                owner_scenario_id=None,
                placed=row.get("placed", bool(meetings)),
                campus=row.get("campus", True),
                meetings=meetings,
                nominal_sessions=row.get("nominal", nominal),
                eligible=tuple(row.get("eligible", eligible)),
                fixed=row.get("fixed"),
                fixed_kind=row.get("kind", "pin" if row.get("fixed") is not None else None),
                current=row.get("current"),
                course_sections=n_course,
                course_cap=course_cap,
            )
        )
    return out


def pinned_load(teacher: int, sessions: int, *, meetings=(), key: str = "FIXED") -> Slot:
    """A pinned section that gives ``teacher`` fixed load (and meetings)."""
    return course(
        f"{key}{teacher}",
        [{"fixed": teacher, "nominal": sessions, "meetings": meetings}],
        eligible=(teacher,),
    )[0]


def per_teacher(assignment) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for key, teacher in sorted(assignment.teacher.items()):
        if teacher is not None:
            out.setdefault(teacher, []).append(key)
    return out


# ── Database fixtures ─────────────────────────────────────────────────────


def instructor(name: str, *, active: bool = True) -> Instructor:
    return Instructor.objects.create(
        full_name=name, normalised_name=normalise_instructor(name), is_active=active
    )


def link(program: str, code: str, person: Instructor, role: str = "primary") -> None:
    CourseInstructor.objects.create(
        program=program, course_code=code, section="M", instructor=person, role=role
    )


def scenario(
    name: str = "one teacher",
    *,
    programs: Iterable[str] = ("AI",),
    board_program: str | None = None,
    gender: str = "M",
) -> tuple[TimetableScenario, DeliveryBoard]:
    programs = list(programs)
    sc = TimetableScenario.objects.create(
        academic_year="1448", term="2", name=name, gender=gender, programs=programs
    )
    board = DeliveryBoard.objects.create(
        scenario=sc,
        label="Term 2",
        nominal_term=2,
        program=board_program if board_program is not None else ",".join(programs),
    )
    return sc, board


def requirement(program: str, code: str, name: str, term: int = 2, credit: int = 3) -> str:
    ProgrammeRequirement.objects.create(
        program=program,
        course_code=code,
        course_name=name,
        programme_term=term,
        credit_hours=credit,
    )
    return planner_course_key(code, name)


def budget(sc, course_key: str, planned: int, *, code: str | None = None, credit: int = 3):
    return ScenarioSectionBudget.objects.create(
        scenario=sc,
        course_key=course_key,
        course_code=code or course_key.split("::", 1)[0],
        course_name=course_key,
        department="AI",
        credit_hours=credit,
        planned_sections=planned,
        max_per_section=40,
        total_demand=20 * planned,
        programme_term=2,
    )


def section(sc, course_key: str, label: str, *, owner=None, name: str = "") -> TermSection:
    code = course_key.split("::", 1)[0]
    return TermSection.objects.create(
        scenario=sc if owner is None else owner,
        course_key=course_key,
        course_code=code,
        course_number=code,
        course_name=course_key,
        section=label,
        source_tag="tw_auto",
    )


def place(
    board: DeliveryBoard,
    ts: TermSection,
    meetings: Iterable[tuple[str, str, str]],
    *,
    locked: bool = False,
    instructor_name: str = "",
    room: str = "R1",
) -> None:
    for day, start, end in meetings:
        SectionPlacement.objects.create(
            board=board,
            term_section=ts,
            day=day,
            start_time=start,
            end_time=end,
            room=room,
            is_locked=locked,
        )
        TermSectionMeeting.objects.get_or_create(
            term_section=ts,
            day=day,
            start_time=start,
            end_time=end,
            room=room,
            defaults={"instructor": instructor_name},
        )


def unplace(ts: TermSection, day: str | None = None) -> None:
    rows = SectionPlacement.objects.filter(term_section=ts)
    meetings = TermSectionMeeting.objects.filter(term_section=ts)
    if day is not None:
        rows = rows.filter(day=day)
        meetings = meetings.filter(day=day)
    rows.delete()
    meetings.delete()


def engine_rows(sc) -> dict[tuple[str, str], str]:
    return {
        (ck, label): name
        for ck, label, name in SectionInstructor.objects.filter(
            scenario=sc, source="engine"
        ).values_list("course_key", "section", "instructor__full_name")
    }


def names_by_key(sc, assignment) -> dict[str, str | None]:
    people = dict(Instructor.objects.values_list("id", "full_name"))
    return {
        key: (people.get(teacher) if teacher is not None else assignment.status[key])
        for key, teacher in sorted(assignment.teacher.items())
    }

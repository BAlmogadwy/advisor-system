"""Course-level instructor assignment — the scenario-independent source of truth.

An ``Instructor`` is assigned to a course keyed by ``(program, course_code,
section M/F)``. The planner resolves the *primary* at section-generation time
and writes its name into ``TermSectionMeeting.instructor`` (the legacy clash
key), so an assignment made here is independent of any scenario.
"""

from __future__ import annotations

from django.db import transaction

from core.models import CourseInstructor, Instructor
from core.services.timetable_online import normalise_course_code


def serialize_course_instructors(
    program: str, course_code: str, section: str
) -> list[dict[str, object]]:
    """The instructors assigned to one (program, course, section), primary first."""
    links = (
        CourseInstructor.objects.filter(
            program=program, course_code=normalise_course_code(course_code), section=section
        )
        .select_related("instructor")
        .order_by("-role", "id")  # 'primary' < 'co' alphabetically → -role puts primary first
    )
    # role values: primary | co | lab. Sort primary first explicitly.
    rows = [
        {
            "id": link.instructor_id,
            "full_name": link.instructor.full_name,
            "full_name_ar": link.instructor.full_name_ar,
            "role": link.role,
            "is_active": link.instructor.is_active,
        }
        for link in links
    ]
    rows.sort(key=lambda r: (r["role"] != "primary", r["id"]))
    return rows


@transaction.atomic
def set_course_instructors(
    program: str, course_code: str, section: str, instructor_ids: list[int]
) -> list[dict[str, object]]:
    """Replace the instructor set for one (program, course, section).

    The first id becomes the ``primary`` (its name is what the planner writes
    through); the rest are ``co``. An empty list clears the assignment. Returns
    the serialised links.
    """
    code = normalise_course_code(course_code)
    resolved: list[Instructor] = []
    seen: set[int] = set()
    for iid in instructor_ids or []:
        instructor = Instructor.objects.filter(pk=iid).first()
        if instructor is None:
            raise ValueError(f"Instructor {iid} not found")
        if instructor.pk not in seen:
            seen.add(instructor.pk)
            resolved.append(instructor)

    CourseInstructor.objects.filter(program=program, course_code=code, section=section).delete()
    for idx, instructor in enumerate(resolved):
        CourseInstructor.objects.create(
            program=program,
            course_code=code,
            section=section,
            instructor=instructor,
            role="primary" if idx == 0 else "co",
        )
    return serialize_course_instructors(program, code, section)


def apply_primary_instructor(ts, scenario, board, display_code: str) -> bool:
    """Fan the primary ``CourseInstructor`` name into a section's meeting rows.

    The single source of the meeting-level instructor write-through. Resolves
    by the scenario's gender + program (preferring the board's own programme
    order, then the scenario's), then writes the primary's name into every
    ``TermSectionMeeting`` of the section (the legacy display/clash cache).
    Returns ``True`` when a name is applied; ``False`` (no-op) when the
    scenario has no gender or the course has no active primary assignment.

    The greedy placer applies this as each section is placed. Every solver /
    local-search / load-balancer persist MUST re-apply it after recreating
    meeting rows: those recreate the meetings with a blank ``instructor``, so
    without this re-fan the name is lost — which silently suppresses the
    Instructors export sheet (it no-ops when no meeting carries a name) after
    a full rebuild, CP-SAT polish, or rebalance.
    """
    from core.models import TermSectionMeeting

    gender = getattr(scenario, "gender", "")
    if not gender:
        return False

    norm = (display_code or "").strip().upper()
    programs: list[str] = []
    for prog in str(getattr(board, "program", "") or "").split(","):
        prog = prog.strip()
        if prog and prog not in programs:
            programs.append(prog)
    for prog in getattr(scenario, "programs", []) or []:
        if prog not in programs:
            programs.append(prog)

    for prog in programs:
        primary = (
            CourseInstructor.objects.filter(
                program=prog,
                course_code=norm,
                section=gender,
                role="primary",
                instructor__is_active=True,
            )
            .select_related("instructor")
            .first()
        )
        if primary:
            TermSectionMeeting.objects.filter(term_section=ts).update(
                instructor=primary.instructor.full_name
            )
            return True
    return False


def reconcile_scenario_instructors(scenario) -> int:
    """Re-fan the current primary ``CourseInstructor`` names into an existing
    scenario's ``TermSectionMeeting.instructor`` rows (the display/clash cache).

    Lets a registrar's course-assignment edits reach an already-generated
    scenario without a full rebuild. Returns the number of sections updated.
    """
    from core.models import TermSection, TermSectionMeeting

    if not scenario.gender:
        return 0
    primaries: dict[tuple[str, str], str] = {}
    for prog, code, name in CourseInstructor.objects.filter(
        program__in=(scenario.programs or []),
        section=scenario.gender,
        role="primary",
        instructor__is_active=True,
    ).values_list("program", "course_code", "instructor__full_name"):
        primaries[(prog, normalise_course_code(code))] = name

    updated = 0
    for ts in TermSection.objects.filter(scenario=scenario):
        name: str | None = None
        for prog in scenario.programs or []:
            name = primaries.get((prog, normalise_course_code(ts.course_code)))
            if name:
                break
        if name is not None:
            if TermSectionMeeting.objects.filter(term_section=ts).update(instructor=name):
                updated += 1
    return updated

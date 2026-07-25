"""Course-level instructor assignment — the scenario-independent source of truth.

An ``Instructor`` is assigned to a course keyed by ``(program, course_code,
section M/F)``. The planner resolves the *primary* at section-generation time
and writes its name into ``TermSectionMeeting.instructor`` (the legacy clash
key), so an assignment made here is independent of any scenario.
"""

from __future__ import annotations

import re
from collections import defaultdict

from django.db import transaction

from core.models import CourseInstructor, Instructor
from core.services.timetable_online import normalise_course_code


def base_cohort(program: str) -> str:
    """The base cohort a programme code belongs to.

    Secondary cohorts (``AI2``, ``CS2``, ``IS2`` …) are re-runs of the base
    programme and share its ``ElectiveCourse`` catalogue and
    ``ElectiveTermMapping`` rows — those live only under the base code. Strip a
    trailing cohort digit so an elective lookup for ``AI2`` also finds ``AI``'s
    data (mirrors the planner, which resolves electives board-globally across all
    co-scheduled programmes). ``AI`` → ``AI``; ``COE2`` → ``COE``.
    """
    return re.sub(r"\d+$", "", program) or program


def build_assignable_course_list(
    program: str, year: str | int, term: str | int
) -> list[dict[str, object]]:
    """Ordered teachable-course list for a program, with elective placeholders
    resolved to the real courses that actually get scheduled.

    The degree plan (``ProgrammeRequirement``) stores each elective as a
    *placeholder* slot (AI1, IS1, GSE1 …) that the planner never schedules
    directly — at build time it replaces the placeholder with the real course(s)
    mapped for the term (``ElectiveTermMapping``), and it is those real courses
    that carry meetings and therefore need an instructor. Listing placeholders
    here would let a registrar assign to a code that can never fan onto a
    section, so this mirrors the planner's resolution:

    * **program electives** → the department's ``ElectiveCourse`` catalogue,
      unioned with its mappings across all terms (some programmes populate only
      one of the two — IS has no catalogue, its real electives live under a blank
      ``programme`` key reachable only via mappings; AI has 12 catalogue rows but
      one mapping). Assignment is term-independent so every mapped course is
      listed; ``offered_this_term`` flags just those mapped in the viewed term.
      The lookup spans the programme *and its base cohort* so secondary cohorts
      (AI2, CS2 …) that share the base's catalogue/mappings resolve too.
    * **free / university electives** → only their mappings, if any (these are
      other-faculty courses with no departmental catalogue); an unmapped slot is
      kept visible (``is_placeholder=True``) so the gap is obvious.

    A real (non-placeholder) ``ProgrammeRequirement`` course always wins over an
    elective sharing its code — enforced explicitly (not by iteration order), so
    a mandatory row keeps its own term/credits even if a same-code catalogue
    elective exists. Rows are otherwise deduped by normalised code and returned
    in plan order, with the program elective block landing at the first
    program-elective slot's term. Assignment is term-independent
    (``CourseInstructor`` has no term); ``offered_this_term`` is display-only.
    Instructor overlay is the caller's job.
    """
    from core.models import ElectiveCourse, ElectiveTermMapping, ProgrammeRequirement
    from core.services.elective_resolver import _classify_placeholder

    pr_rows = list(
        ProgrammeRequirement.objects.filter(program=program)
        .order_by("programme_term", "course_code")
        .values_list("course_code", "course_name", "programme_term", "credit_hours", "is_online")
    )

    # A real course always wins over a same-code elective — collect the mandatory
    # (non-placeholder) codes up front so the rule holds regardless of row order.
    mandatory_codes = {
        normalise_course_code(cc)
        for cc, _n, _t, _c, _o in pr_rows
        if _classify_placeholder(normalise_course_code(cc)) is None
    }

    # Elective catalogue + term mappings live under the base cohort; look both up.
    prog_variants = {program, base_cohort(program)}

    # Real electives this program offers via mappings, grouped by placeholder.
    # Assignment is term-independent, so gather EVERY mapped course (across all
    # terms) — that is the full set a registrar can assign — and use the viewed
    # term only to flag which are "offered this term". IS/IS2 depend on this: their
    # real elective catalogue is stored under a blank ``programme`` key, reachable
    # only through their mappings, so a term-scoped lookup would drop them (and
    # mislabel valid IS assignments as orphans) in any unmapped term.
    mapped_by_placeholder: dict[str, list] = defaultdict(list)
    for m in ElectiveTermMapping.objects.filter(programme__in=prog_variants).select_related(
        "elective"
    ):
        mapped_by_placeholder[normalise_course_code(m.placeholder_code)].append(m.elective)

    offered_codes = {
        normalise_course_code(code)
        for code in ElectiveTermMapping.objects.filter(
            programme__in=prog_variants, academic_year=str(year), term=int(term)
        ).values_list("elective__course_code", flat=True)
    }

    # Representative term for the (undifferentiated) program-elective block.
    prog_elective_terms = [
        t
        for cc, _n, t, _c, _o in pr_rows
        if _classify_placeholder(normalise_course_code(cc)) == "program_elective" and t is not None
    ]
    prog_elective_term = min(prog_elective_terms) if prog_elective_terms else None

    rows: list[dict[str, object]] = []
    seen: dict[str, dict[str, object]] = {}

    def _add(
        code: object,
        name: object,
        pterm: object,
        credit: object,
        is_online: object,
        *,
        is_elective: bool,
        offered: bool,
        is_placeholder: bool = False,
    ) -> None:
        key = normalise_course_code(code)
        if not key or key in seen:
            return
        row: dict[str, object] = {
            "course_code": str(code),
            "course_name": name or "",
            "programme_term": pterm,
            "credit_hours": credit,
            "is_online": bool(is_online),
            "is_elective": is_elective,
            "offered_this_term": offered,
            "is_placeholder": is_placeholder,
            "instructor": None,
            "co_instructors": [],
        }
        seen[key] = row
        rows.append(row)

    def _add_elective(ec: object, pterm: object, offered: bool) -> None:
        # A real/mandatory course of the same code always wins — never shadow it.
        if normalise_course_code(getattr(ec, "course_code", "")) in mandatory_codes:
            return
        _add(
            getattr(ec, "course_code", ""),
            getattr(ec, "course_name", ""),
            pterm,
            getattr(ec, "credit_hours", None),
            False,
            is_elective=True,
            offered=offered,
        )

    def _offered(ec: object) -> bool:
        return normalise_course_code(getattr(ec, "course_code", "")) in offered_codes

    def _emit_program_electives() -> int:
        # Mapped courses first (covers programmes whose real electives aren't in a
        # departmental catalogue, e.g. IS), then the rest of the catalogue.
        # Returns rows added.
        before = len(rows)
        for ph, electives in mapped_by_placeholder.items():
            if _classify_placeholder(ph) != "program_elective":
                continue
            for ec in electives:
                _add_elective(ec, prog_elective_term, _offered(ec))
        for ec in ElectiveCourse.objects.filter(programme__in=prog_variants).order_by(
            "course_code"
        ):
            _add_elective(ec, prog_elective_term, _offered(ec))
        return len(rows) - before

    program_block_emitted = False
    program_block_count = 0
    for cc, name, pterm, credit, online in pr_rows:
        cls = _classify_placeholder(normalise_course_code(cc))
        if cls is None:
            _add(cc, name, pterm, credit, online, is_elective=False, offered=True)
        elif cls == "program_elective":
            if not program_block_emitted:
                program_block_emitted = True
                program_block_count = _emit_program_electives()
            if program_block_count == 0:
                # No catalogue and no mapping resolved a real course — keep the
                # slot visible so the missing elective data is obvious.
                _add(
                    cc,
                    name,
                    pterm,
                    credit,
                    online,
                    is_elective=False,
                    offered=True,
                    is_placeholder=True,
                )
        else:  # free / university elective
            real = mapped_by_placeholder.get(normalise_course_code(cc), [])
            if real:
                for ec in real:
                    _add_elective(ec, pterm, _offered(ec))
            else:
                # No real course mapped this term — keep the slot visible.
                _add(
                    cc,
                    name,
                    pterm,
                    credit,
                    online,
                    is_elective=False,
                    offered=True,
                    is_placeholder=True,
                )

    # Defensive: emit program electives even if the plan has no program-elective
    # placeholder row (catalogue/mappings should still surface).
    if not program_block_emitted:
        _emit_program_electives()

    return rows


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

import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from core.models import ProgrammeRequirement, Student, StudentCourse
from core.services.course_identity import planner_course_key
from core.services.elective_validation import ElectiveMappingError, ElectiveSelection
from core.services.eligibility import evaluate_prerequisites
from core.services.recommender_batch import batch_recommend, batch_recommend_multi_program
from core.services.student_helpers import is_elective_slot, normalize_code
from core.services.student_sections import cohort_of_student_section

_aggregate_cache: dict[tuple, tuple[float, tuple[int, "Counter[str]"]]] = {}
_AGGREGATE_CACHE_TTL = 300  # 5 minutes


@dataclass(frozen=True)
class StudentDemand:
    """One student's next-term courses, with the programme they are planned in."""

    student_id: int
    program: str
    courses: tuple[str, ...]


@dataclass
class ElectiveTrace:
    """What elective resolution did with each placeholder slot it met.

    ``picks[(programme, slot, course)]`` counts the students whose slot became
    that real course; ``dropped[(programme, slot, reason)]`` counts the slot
    demands that became no course at all, and why (``not_published``,
    ``invalid_mapping``, ``no_eligible_course``, ``no_term_scope``).
    """

    picks: Counter[tuple[str, str, str]] = field(default_factory=Counter)
    dropped: Counter[tuple[str, str, str]] = field(default_factory=Counter)


@dataclass(frozen=True)
class PlanningDemand:
    """Everything Section Planning sizes ONE section from, per student (electives resolved).

    ``section`` is ``"M"`` or ``"F"``: every student in ``students`` is of it.
    ``no_section`` counts the students in scope with no recorded section: they
    are in neither section's plan, never assumed to be of one.
    """

    section: str
    students: tuple[StudentDemand, ...]
    no_section: int = 0
    #: (programme, slot, course, students): a resolved elective and the slot it fills.
    elective_picks: tuple[tuple[str, str, str, int], ...] = ()
    #: (programme, slot, reason, students): slot demand that became no course.
    dropped_slots: tuple[tuple[str, str, str, int], ...] = ()


_planning_cache: dict[tuple, tuple[float, PlanningDemand]] = {}


def clear_aggregate_cache() -> None:
    """Clear cached aggregate recommendation counts."""
    _aggregate_cache.clear()
    _planning_cache.clear()


#: The two sections (campuses) a plan is made for, as ``Student.section`` records them.
PLANNING_SECTIONS = ("M", "F")


def build_planning_demand(
    year: int, semester: int, program: str | None = None, *, section: str
) -> PlanningDemand:
    """Per-student demand of ONE section, for one programme or for every programme.

    The department plans the male section (``"M"``) and the female section
    (``"F"``) apart, never together. Only the chosen section's students are
    recommended and have their elective placeholders resolved (as
    ``build_aggregate_counts`` does), so a student of the other section never
    changes this demand, not even which elective a slot's students are spread
    across; the timetable builder plans a section from its students alone too.
    Students with no recorded section are only counted.
    """
    if section not in PLANNING_SECTIONS:
        raise ValueError(f"section must be one of {PLANNING_SECTIONS}, not {section!r}")
    program = str(program).strip() if program else None
    cache_key = (int(year), int(semester), program or "", section)
    cached = _planning_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _AGGREGATE_CACHE_TTL:
        return cached[1]

    rows = Student.objects.all()
    if program:
        rows = rows.filter(program=program)
    everyone = list(rows.values_list("student_id", "program", "section").order_by("student_id"))
    scope = [
        (sid, prog)
        for sid, prog, recorded in everyone
        if cohort_of_student_section(recorded) == section
    ]
    no_section = sum(
        1 for _sid, _prog, recorded in everyone if not cohort_of_student_section(recorded)
    )
    student_ids = [int(sid) for sid, _prog in scope]

    if program:
        all_recs = batch_recommend(student_ids, program, year, semester)
    else:
        all_recs = batch_recommend_multi_program(student_ids, year, semester)
    trace = ElectiveTrace()
    all_recs = resolve_elective_recommendations(
        all_recs, year=year, semester=semester, program=program, trace=trace
    )

    students = []
    for sid, prog in scope:
        courses = tuple(
            code for code in (normalize_code(c) for c in all_recs.get(int(sid), [])) if code
        )
        students.append(
            StudentDemand(student_id=int(sid), program=str(program or prog or ""), courses=courses)
        )
    demand = PlanningDemand(
        section=section,
        students=tuple(students),
        no_section=no_section,
        elective_picks=tuple(
            (prog, slot, course, n) for (prog, slot, course), n in sorted(trace.picks.items())
        ),
        dropped_slots=tuple(
            (prog, slot, reason, n) for (prog, slot, reason), n in sorted(trace.dropped.items())
        ),
    )
    _planning_cache[cache_key] = (time.time(), demand)
    if len(_planning_cache) > 20:
        oldest = min(_planning_cache, key=lambda k: _planning_cache[k][0])
        del _planning_cache[oldest]
    return demand


def get_student_ids(
    program: str | list[str] | None = None,
    section: str | None = None,
) -> list[int]:
    """The students of a programme (or several), of one section when one is given.

    A student's section is read as ``cohort_of_student_section`` reads it,
    trimmed and in either case (" f " is F), so the timetable builder plans a
    section from exactly the students Section Planning counts in it.
    """
    qs = Student.objects.all()
    if program:
        if isinstance(program, list):
            qs = qs.filter(program__in=program)
        else:
            qs = qs.filter(program=program)
    if section:
        # Read in Python: SQL TRIM strips spaces only, str.strip() every blank.
        wanted = str(section).strip().upper()
        return [
            sid
            for sid, recorded in qs.values_list("student_id", "section")
            if str(recorded or "").strip().upper() == wanted
        ]
    return list(qs.values_list("student_id", flat=True))


def build_aggregate_counts(
    year: int,
    semester: int,
    program: str | list[str] | None = None,
    section: str | None = None,
    *,
    resolve_electives: bool = False,
    strict_passed_only: bool = False,
) -> tuple[int, Counter[str]]:
    # Normalize comma-separated program string into a list
    if isinstance(program, str) and "," in program:
        program = [p.strip() for p in program.split(",") if p.strip()]
    # Single-item list → unwrap to string for efficiency
    if isinstance(program, list) and len(program) == 1:
        program = program[0]

    # Normalize program for cache key (lists are not hashable)
    prog_key = tuple(program) if isinstance(program, list) else (str(program),)
    cache_key = (
        year,
        semester,
        prog_key,
        str(section),
        bool(resolve_electives),
        bool(strict_passed_only),
    )
    cached = _aggregate_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _AGGREGATE_CACHE_TTL:
        return cached[1]

    student_ids = get_student_ids(program=program, section=section)
    aggregate: Counter[str] = Counter()

    # Batch recommender — single program or multi-program
    if program and isinstance(program, str):
        all_recs = batch_recommend(
            student_ids,
            program,
            year,
            semester,
            strict_passed_only=strict_passed_only,
        )
    else:
        all_recs = batch_recommend_multi_program(
            student_ids,
            year,
            semester,
            strict_passed_only=strict_passed_only,
        )

    if resolve_electives:
        all_recs = resolve_elective_recommendations(
            all_recs,
            year=year,
            semester=semester,
            program=program,
            strict_passed_only=strict_passed_only,
        )

    for recs in all_recs.values():
        aggregate.update(recs)

    result = (len(student_ids), aggregate)
    _aggregate_cache[cache_key] = (time.time(), result)
    # Cap cache size to prevent unbounded memory growth
    if len(_aggregate_cache) > 20:
        oldest = min(_aggregate_cache, key=lambda k: _aggregate_cache[k][0])
        del _aggregate_cache[oldest]
    return result


def build_course_identity_aggregate_counts(
    year: int,
    semester: int,
    program: str | list[str] | None = None,
    section: str | None = None,
    *,
    resolve_electives: bool = False,
) -> tuple[int, Counter[str], dict[str, dict[str, object]]]:
    """Build recommendation counts keyed by planner course identity.

    The public course code is not always unique across plans. Section/timetable
    planning needs to keep same-code courses with different plan names separate,
    while still displaying the registrar-facing code.
    """
    if isinstance(program, str) and "," in program:
        program = [p.strip() for p in program.split(",") if p.strip()]
    if isinstance(program, list) and len(program) == 1:
        program = program[0]

    student_ids = get_student_ids(program=program, section=section)
    if not student_ids:
        return 0, Counter(), {}

    student_programs = {
        int(sid): str(prog)
        for sid, prog in Student.objects.filter(student_id__in=student_ids).values_list(
            "student_id", "program"
        )
        if prog
    }

    if program and isinstance(program, str):
        all_recs = batch_recommend(student_ids, program, year, semester)
        programmes = [program]
    else:
        all_recs = batch_recommend_multi_program(student_ids, year, semester)
        if isinstance(program, list):
            programmes = list(program)
        else:
            programmes = sorted(set(student_programs.values()))

    if resolve_electives:
        all_recs = resolve_elective_recommendations(
            all_recs,
            year=year,
            semester=semester,
            program=program,
        )

    requirement_meta: dict[str, dict[str, dict[str, object]]] = {}
    if programmes:
        for row in ProgrammeRequirement.objects.filter(program__in=programmes).values(
            "program",
            "course_code",
            "course_name",
            "credit_hours",
        ):
            prog = str(row.get("program") or "")
            code = normalize_code(row.get("course_code"))
            if not prog or not code:
                continue
            requirement_meta.setdefault(prog, {})[code] = {
                "course_code": code,
                "course_name": str(row.get("course_name") or "").strip(),
                "credit_hours": row.get("credit_hours") or 3,
                "department": "".join(ch for ch in code if ch.isalpha()),
            }

    aggregate: Counter[str] = Counter()
    course_metadata: dict[str, dict[str, object]] = {}

    for sid, recs in all_recs.items():
        student_program = program if isinstance(program, str) else student_programs.get(int(sid))
        program_meta = requirement_meta.get(str(student_program), {})
        for code in recs:
            ncode = normalize_code(code)
            if not ncode:
                continue
            meta = dict(program_meta.get(ncode, {}))
            if not meta:
                meta = {
                    "course_code": ncode,
                    "course_name": "",
                    "credit_hours": 3,
                    "department": "".join(ch for ch in ncode if ch.isalpha()),
                }
            key = planner_course_key(ncode, meta.get("course_name"))
            meta["course_key"] = key
            course_metadata.setdefault(key, meta)
            aggregate[key] += 1

    return len(student_ids), aggregate, course_metadata


def resolve_elective_recommendations(
    all_recs: dict[int, list[str]],
    *,
    year: int,
    semester: int,
    program: str | list[str] | None,
    strict_passed_only: bool = False,
    trace: ElectiveTrace | None = None,
) -> dict[int, list[str]]:
    """Replace mapped elective placeholders with eligible real electives.

    The batch recommender intentionally emits plan placeholders such as DS2.
    Section planning needs the deliverable course demand instead, so it uses
    the term mapping table to expand DS2 into courses such as DS485.

    Pass ``trace`` to learn which slot each resolved elective fills and which
    slot demands were dropped (see ``ElectiveTrace``); the result is the same.
    """
    if not all_recs:
        return all_recs

    student_ids = list(all_recs.keys())
    student_programs: dict[int, str] = {}
    student_credits: dict[int, tuple[int, int]] = {}
    for sid, prog, earned, registered in Student.objects.filter(
        student_id__in=student_ids
    ).values_list(
        "student_id",
        "program",
        "total_earned_credits",
        "current_registered_credits",
    ):
        sid_int = int(sid)
        if prog:
            student_programs[sid_int] = str(prog)
        student_credits[sid_int] = (int(earned or 0), int(registered or 0))

    if isinstance(program, str):
        programmes = [program]
    elif isinstance(program, list):
        programmes = list(program)
    else:
        programmes = sorted(set(student_programs.values()))

    if not programmes:
        return all_recs

    selections = {}
    unscoped_ordinary = {}
    for programme in programmes:
        try:
            selections[normalize_code(programme)] = ElectiveSelection(
                programme, str(year), semester
            )
        except ElectiveMappingError:
            # Ordinary plan recommendations also run before a planning term has
            # been chosen (the recommender uses semester=0 for that case). Their
            # validity does not depend on an elective publication. Keep only
            # unambiguous, declared ordinary courses; a placeholder or standalone
            # catalogue option still needs a valid year/term to be actionable.
            requirements = defaultdict(list)
            for row in ProgrammeRequirement.objects.values("program", "course_code", "type"):
                if normalize_code(row["program"]) == normalize_code(programme):
                    requirements[normalize_code(row["course_code"])].append(row)
            unscoped_ordinary[normalize_code(programme)] = {
                code
                for code, rows in requirements.items()
                if code and len(rows) == 1 and not is_elective_slot(rows[0]["type"])
            }

    sc_qs = StudentCourse.objects.filter(student_id__in=student_ids).select_related("course")
    passed: dict[int, set[str]] = defaultdict(set)
    studying: dict[int, set[str]] = defaultdict(set)
    for sc in sc_qs:
        code = normalize_code(sc.course.course_code)
        if sc.status == "passed":
            passed[int(sc.student_id)].add(code)
        elif sc.status == "studying":
            studying[int(sc.student_id)].add(code)

    resolved: dict[int, list[str]] = {}
    assignment_count: Counter[str] = Counter()

    for sid, recs in all_recs.items():
        student_programme = program if isinstance(program, str) else student_programs.get(int(sid))
        selection = selections.get(normalize_code(student_programme or ""))
        programme_label = str(student_programme or "")
        if selection is None:
            ordinary = unscoped_ordinary.get(normalize_code(student_programme or ""), set())
            resolved[sid] = [code for code in recs if normalize_code(code) in ordinary]
            if trace is not None:
                for code in recs:
                    if normalize_code(code) not in ordinary:
                        trace.dropped[(programme_label, normalize_code(code), "no_term_scope")] += 1
            continue

        student_passed = passed.get(int(sid), set())
        student_studying = studying.get(int(sid), set())
        student_resolved: list[str] = []

        for code in recs:
            norm = normalize_code(code)
            requirement, _ = selection.requirement(norm)
            if (
                requirement is None
                and norm not in selection.mappings
                and len(selection.requirements.get(norm, [])) <= 1
            ):
                student_resolved.append(code)
                continue
            # A placeholder with no valid current publication cannot become a
            # concrete recommendation, even if an old mapping still exists.
            status, electives, _ = selection.resolve(norm)
            if status != "READY":
                if trace is not None:
                    trace.dropped[(programme_label, norm, status.lower())] += 1
                continue

            eligible = []
            for elective in electives:
                prereqs = [
                    normalize_code(part)
                    for part in str(elective["prerequisites_csv"] or "").split(",")
                    if part.strip()
                ]
                earned, registered = student_credits.get(int(sid), (0, 0))
                outcome = evaluate_prerequisites(
                    prereqs,
                    student_passed,
                    student_studying,
                    strict_passed_only=strict_passed_only,
                    earned_credits=earned,
                    registered_credits=registered,
                )
                if outcome.met:
                    eligible.append(elective["course_code"])

            if eligible:
                pick = min(eligible, key=lambda c: (assignment_count[c], c))
                assignment_count[pick] += 1
                student_resolved.append(pick)
                if trace is not None:
                    trace.picks[(programme_label, norm, normalize_code(pick))] += 1
            elif trace is not None:
                trace.dropped[(programme_label, norm, "no_eligible_course")] += 1

        resolved[sid] = student_resolved

    return resolved

"""Build a `Snapshot` from institutional data — the only Django-facing input code.

Boundaries (blueprint §0):

* reads **institutional facts** — students, programme requirements, rooms,
  instructor eligibility — and the upstream *advising* recommender;
* never reads a `TimetableScenario`, `DeliveryBoard`, `SectionPlacement` or
  `TermSectionMeeting`. No saved scenario is ever a baseline or a warm start;
* never imports `core.services.timetable_*`.

Demand comes from `recommender_batch.batch_recommend`, which already encodes
academic policy (real-term computation, parity, prerequisites, unlock ranking,
credit cap). `scheduler` **consumes** it and does not reimplement it — two
implementations of "what should this student take" would be a worse defect than
anything in the timetable builder.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime

from core.models import (
    CourseInstructor,
    ElectiveCourse,
    ElectiveTermMapping,
    ProgrammeRequirement,
    Student,
)
from core.models import Instructor as InstructorRow
from core.models import Room as RoomRow
from scheduler.domain import (
    CapacityPolicy,
    DeliveryMode,
    Grid,
    Instructor,
    MeetingKind,
    MeetingRequirement,
    Offering,
    Room,
    Section,
    Snapshot,
    StudentDemand,
)

# The declared slot grid (owner decision D2: the grid is the sole time authority).
DEFAULT_LECTURE_STARTS: dict[str, int] = {
    "09:00": 75,
    "10:30": 75,
    "10:50": 75,
    "13:00": 75,
    "14:30": 75,
    "14:45": 75,
    "16:00": 75,
}
DEFAULT_LAB_STARTS: dict[str, int] = {
    "09:00": 100,
    "10:45": 100,
    "13:00": 100,
    "14:45": 100,
    "16:30": 100,
}
# Online teaching runs at the SAME declared times as everything else of its
# length. It used to have a private late-day family (15:00, 16:45, 18:30) on the
# reasoning that a session after the campus day competes with nothing — which is
# what licensed the second half of D9, that online never clashes for a student.
#
# Owner decision, 2026-07-28: drop the private family. It was the one thing this
# engine scheduled at times the scenario's own grid does not declare, and the
# workspace has to widen that grid to draw them — a grid the EXISTING engine
# reads as its legal placement times, so a scheduler build could leave the old
# engine free to put a room-consuming lab at 18:30. Nothing else in the
# subsystem could regress the engine it was built beside.
#
# The exemption goes with it. A class at 13:00 occupies a student's 13:00
# whether they attend it in a room or at home, so online now clashes like
# anything else. What stays true is the physical part: online consumes NO ROOM,
# and creates no campus travel — so it is out of the room model and out of the
# instructor's campus span, but in the clash model and in the daily cap.
#
# Owner rule, 2026-07-28: GS and GSE — every online course — run in a family of
# THREE windows of their own and nowhere else, and no other course may use them.
# The exclusivity runs both ways, and both directions are enforced:
#
#   * online meetings only ever see these windows, because the grid families are
#     keyed on (duration, delivery);
#   * no other course reaches them, because the same windows are flagged
#     `online_only` in the shared slot config, which every automatic placer
#     filters through `placeable_slots()`.
#
# They are declared in that shared config rather than invented here, so a grid
# never has to be widened to draw an online class — widening it is what let a
# scheduler build leave the EXISTING engine free to book a room in the evening.
DEFAULT_ONLINE_STARTS: dict[str, int] = {
    "15:50": 100,
    "17:40": 100,
    "19:30": 100,
}


class IntakeError(Exception):
    """The input cannot produce a coherent snapshot (fail closed)."""


def default_grid() -> Grid:
    return Grid.from_spec(
        lecture_starts=DEFAULT_LECTURE_STARTS,
        lab_starts=DEFAULT_LAB_STARTS,
        online_starts=DEFAULT_ONLINE_STARTS,
    )


# ── Course-category policy (owner rules, 2026-07-26) ──────────────────────
#
# 1. Every GS / GSE course is delivered ONLINE: it needs no room and never
#    contributes to a student's on-campus gap.
# 2. Graduation projects and cooperative training are NOT timetabled at all —
#    no slot, no room. They carry credit but have no weekly meeting.
# 3. Everything else needs a room.

ONLINE_CODE_PREFIXES: tuple[str, ...] = ("GSE", "GS")

#: A real catalogue code carries a three-digit number (AI463, MATH105). Elective
#: placeholders never do (AI1, FE2, GSE1), which is what makes them detectable
#: without a hand-maintained list.
_REAL_COURSE_CODE = re.compile(r"^[A-Z]{2,4}\d{3}$")


def is_placeholder_code(course_code: str) -> bool:
    """Is this an elective *slot* rather than a teachable course?

    The single answer to that question. There used to be a second one — a
    hand-listed set in ``readiness.py`` — and it had already drifted: it knew
    about ``AI1`` and ``GSE1`` but not ``AI2``, ``AI3``, ``DS3``, ``GSE2`` or
    ``GSE3``, all of which exist in the live plan. Two sources of truth for one
    question means one of them is wrong and nothing says which.
    """
    return not _REAL_COURSE_CODE.match(str(course_code or "").strip().upper())


# Matched against the course NAME, anchored, because a substring match on
# "PROJECT" would wrongly catch real taught courses — IS357 / IS362
# "INFORMATION SYSTEMS PROJECT MANAGEMENT" are ordinary classroom courses.
#: Matched as prefixes on the plan's free-text course name.
#:
#: "GRADUATION" rather than "GRADUATION PROJECT", because the two are the same
#: thing spelled differently and the narrower rule cost a whole cohort: CS491 is
#: recorded as "GRADUATION I" while AI491 is "GRADUATION PROJECT I". The
#: narrower prefix missed CS491, so a graduation project was scheduled with 31
#: sections, and since sections of one course may never overlap while the week
#: holds 25 non-overlapping cells, the entire female CS build came back
#: INFEASIBLE with no explanation.
#:
#: Free-text names are a fragile thing to depend on, which is why the readiness
#: report now also proves this class of failure arithmetically rather than
#: trusting this list to be complete.
UNSCHEDULED_NAME_PREFIXES: tuple[str, ...] = (
    "GRADUATION",
    "COOPERATIVE TRAINING",
)
UNSCHEDULED_NAME_EXACT: frozenset[str] = frozenset({"TRAINING"})


def is_online_course(course_code: str) -> bool:
    """GS/GSE courses are online by policy — deliberately *not* read from the
    ``is_online`` column.

    The column disagrees with itself: GS112 is flagged online in one programme
    and not in another. A categorical rule corrects that inconsistency instead of
    propagating it into the timetable.
    """
    code = str(course_code or "").strip().upper()
    return any(code.startswith(p) for p in ONLINE_CODE_PREFIXES)


def is_unscheduled_course(course_name: str) -> bool:
    """Graduation projects and cooperative training are never timetabled."""
    name = " ".join(str(course_name or "").strip().upper().split())
    return name in UNSCHEDULED_NAME_EXACT or any(
        name.startswith(p) for p in UNSCHEDULED_NAME_PREFIXES
    )


def elective_placeholder_report(snapshot: Snapshot, academic_year: str, term: int) -> list[dict]:
    """Which scheduled offerings are elective *placeholders*, and can they be named?

    A placeholder such as ``AI1 "PROGRAM ELECTIVE I"`` is a slot, not a course.
    The timetable can schedule it perfectly and still be unpublishable, because
    no student can enrol in "PROGRAM ELECTIVE I". Three outcomes, kept distinct
    rather than collapsed into a pass/fail:

    * ``RESOLVED``   — exactly one catalogue course fills the slot, so the
      placement can be published under a real name with no assumption;
    * ``AMBIGUOUS``  — several courses fill it, so students split across them by
      a registration choice this stage cannot see. Naming one would be a guess;
    * ``UNMAPPED``   — nobody has said what fills it. An evidence gap, reported
      as such and never guessed at.

    Timetable *quality* is unaffected either way — measured on the live M cohort,
    resolution moves no metric — so this reports and does not rewrite.
    """
    from core.models import ElectiveTermMapping

    targets: dict[str, set[str]] = defaultdict(set)
    for mapping in ElectiveTermMapping.objects.filter(
        academic_year=str(academic_year),
        term=int(term),
        programme__in={p for o in snapshot.offerings for p in o.programs},
    ).select_related("elective"):
        targets[str(mapping.placeholder_code).strip().upper()].add(
            str(mapping.elective.course_code).strip().upper()
        )

    rows = []
    for offering in snapshot.offerings:
        code = offering.course_code.strip().upper()
        if _REAL_COURSE_CODE.match(code):
            continue
        sections = len(snapshot.sections_by_offering.get(offering.id, ()))
        if not sections:
            continue  # not planned this term — nothing to publish
        options = sorted(targets.get(code, ()))
        status = "UNMAPPED" if not options else ("RESOLVED" if len(options) == 1 else "AMBIGUOUS")
        rows.append(
            {
                "placeholder": code,
                "name": offering.course_name,
                "sections": sections,
                "status": status,
                "options": options,
            }
        )
    return sorted(rows, key=lambda r: (r["status"], r["placeholder"]))


def _one_elective_per_slot(
    recommended: dict[int, list[str]],
    academic_year: str,
    term: int,
    program_set: frozenset[str],
) -> dict[int, list[str]]:
    """Give each student ONE elective per placeholder slot, spread across the options.

    After placeholders are expanded, a student eligible for all three of CS403,
    CS468 and CS487 is recommended all three — but they will register for one.
    Counting all three triples that course's demand and therefore its section
    count, and sections are the scarce resource that decides whether a cohort can
    be scheduled at all.

    **This duplicates a rule that already exists** in the project, as
    `_limit_electives_per_placeholder`. It is reimplemented rather than imported
    because that function lives in `core.services.timetable_generate`, and this
    subsystem is forbidden — by a test, not merely by convention — from importing
    the old timetable engine. That isolation is the entire basis on which this
    subsystem was allowed to exist, so it is not worth spending to save twenty
    lines. The behaviour is pinned by tests; if the institution's rule changes,
    both places must change, and this comment is the pointer.
    """
    mappings: dict[str, set[str]] = defaultdict(set)
    for mapping in ElectiveTermMapping.objects.filter(
        academic_year=str(academic_year), term=int(term), programme__in=program_set
    ).select_related("elective"):
        mappings[str(mapping.elective.course_code).strip().upper()].add(
            str(mapping.placeholder_code).strip().upper()
        )
    if not mappings:
        return recommended

    taken: Counter[str] = Counter()
    out: dict[int, list[str]] = {}
    for student_id in sorted(recommended):
        codes = [str(c).strip().upper() for c in recommended[student_id]]
        regular = [c for c in codes if c not in mappings]
        electives = [c for c in codes if c in mappings]

        kept: list[str] = []
        filled: set[str] = set()
        for code in sorted(electives, key=lambda c: (taken[c], c)):
            slots = mappings[code] - filled
            if not slots:
                continue  # this student already has a course for every slot it fills
            filled |= slots
            kept.append(code)
            taken[code] += 1
        out[student_id] = regular + kept
    return out


def compile_requirements(credit_hours: int, *, is_online: bool) -> tuple[MeetingRequirement, ...]:
    """Compile an offering's exact weekly meeting multiset (N5).

    Credit hours are the *default input* to this compilation, not a runtime
    substitute for it — the old engine re-derived meeting shape at several call
    sites with three different lab-duration literals between them.

    The shape follows the institution's documented rule:
    4cr -> 2x75 lecture + 1x100 lab; 3cr -> 2x75; 2cr -> 1x100; 1cr -> 1x75.

    Online offerings need no room, so their meetings carry ``DeliveryMode.ONLINE``
    and are exempt from room supply entirely.
    """
    delivery = DeliveryMode.ONLINE if is_online else DeliveryMode.IN_PERSON
    credits = int(credit_hours or 0)
    if credits >= 4:
        return (
            MeetingRequirement(MeetingKind.LECTURE, delivery, 75, 2),
            MeetingRequirement(MeetingKind.LAB, delivery, 100, 1),
        )
    if credits == 3:
        return (MeetingRequirement(MeetingKind.LECTURE, delivery, 75, 2),)
    if credits == 2:
        return (MeetingRequirement(MeetingKind.LECTURE, delivery, 100, 1),)
    return (MeetingRequirement(MeetingKind.LECTURE, delivery, 75, 1),)


def _offering_id(course_code: str, programs: frozenset[str]) -> str:
    """Stable, opaque offering identity (N1).

    Keyed on the course plus the programme set that shares it, so one display
    code offered to two different cohorts yields two distinct offerings — the
    `FE1`/`CS111` ambiguity that silently merged demand in the old engine.
    """
    seed = f"{course_code.strip().upper()}|{'+'.join(sorted(programs))}"
    return f"off_{hashlib.sha256(seed.encode()).hexdigest()[:12]}"


def build_snapshot(
    *,
    academic_year: str,
    term: int,
    gender: str,
    programs: list[str],
    default_capacity: int,
    buffer: float = 1.0,
    grid: Grid | None = None,
) -> Snapshot:
    """Assemble an immutable, fingerprinted snapshot for one gender cohort."""
    gender = gender.strip().upper()
    if gender not in ("M", "F"):
        raise IntakeError("gender must be M or F — a snapshot is single-gender (D1)")
    program_set = {p.strip().upper() for p in programs if p.strip()}
    if not program_set:
        raise IntakeError("at least one programme is required")

    grid = grid or default_grid()
    policy = CapacityPolicy(default_capacity=default_capacity, buffer=buffer)

    # ── students (single gender, D1) ──
    #
    # Withdrawn students are excluded. They will not attend, so counting them
    # inflates demand, which inflates the section count, which is the pressure
    # that makes a cohort infeasible: sections of one course may never overlap,
    # so every extra section consumes one of the week's 25 non-overlapping cells.
    #
    # Nothing else in the project filters on status -- `recommender_batch` and
    # `timetable_workspace` both take whatever student set the caller hands them
    # -- so this is not an established rule being followed but an absent one
    # being written down. It is therefore narrow on purpose: only statuses that
    # explicitly say WITHDRAWN are dropped. "Visitor" is kept, because whether a
    # visiting student needs a seat is a policy question, not a technical one,
    # and the count is reported rather than silently applied.
    student_rows = list(
        Student.objects.filter(program__in=program_set, section__iexact=gender).values_list(
            "student_id", "program", "status"
        )
    )
    students = [
        (sid, prog)
        for sid, prog, status in student_rows
        if "WITHDRAWN" not in str(status or "").upper()
    ]
    excluded_withdrawn = len(student_rows) - len(students)
    if not students:
        raise IntakeError(f"no {gender} students in {sorted(program_set)} — nothing to schedule")
    program_by_student = {int(sid): str(prog).upper() for sid, prog in students}

    # ── demand, from the UPSTREAM recommender (never reimplemented) ──
    from core.services.recommender_batch import batch_recommend
    from core.services.reporting import resolve_elective_recommendations

    recommended: dict[int, list[str]] = {}
    by_program: dict[str, list[int]] = defaultdict(list)
    for sid, prog in program_by_student.items():
        by_program[prog].append(sid)
    for prog, sids in sorted(by_program.items()):
        recommended.update(batch_recommend(sids, prog, int(academic_year), int(term)) or {})

    # The recommender deliberately emits plan PLACEHOLDERS (CS1, DS2). Timetabling
    # a placeholder schedules a course nobody can enrol in, so they are expanded
    # into the real electives the term mapping assigns to them — using the
    # project's own resolver, not a second opinion about it.
    recommended = resolve_elective_recommendations(
        recommended, year=int(academic_year), semester=int(term), program=sorted(program_set)
    )
    recommended = _one_elective_per_slot(recommended, academic_year, term, program_set)

    # Which placeholder(s) each catalogue elective stands in for, and the
    # catalogue rows themselves. Built once: the demand side needs it to give a
    # resolved elective an offering, and the staffing side needs it to fold a
    # target's approvals back onto its placeholder.
    #
    # Keyed on **(programme, target)**, never on the target alone. An approval is
    # granted for one programme's elective slot, and both the mapping and the
    # staffing row carry a programme; dropping either would let an approval to
    # teach CS403 for CS count as staffing for an AI slot that happens to draw on
    # the same catalogue course.
    placeholder_of: dict[tuple[str, str], set[str]] = defaultdict(set)
    placeholders_of_target: dict[str, set[str]] = defaultdict(set)
    for mapping in ElectiveTermMapping.objects.filter(
        academic_year=str(academic_year), term=int(term), programme__in=program_set
    ).select_related("elective"):
        target = str(mapping.elective.course_code).strip().upper()
        programme = str(mapping.programme).strip().upper()
        placeholder = str(mapping.placeholder_code).strip().upper()
        placeholder_of[(programme, target)].add(placeholder)
        placeholders_of_target[target].add(placeholder)
    elective_by_code = {
        str(row.course_code).strip().upper(): row
        for row in ElectiveCourse.objects.filter(
            course_code__in=sorted(placeholders_of_target) or [""]
        )
    }
    #: Demand the snapshot could not place against any offering, by course code.
    #: Reported rather than discarded — dropping it silently is exactly how the
    #: resolved electives went missing.
    unmatched_demand: Counter[str] = Counter()

    # ── offerings, from the programme plan ──
    plan_rows = list(
        ProgrammeRequirement.objects.filter(program__in=program_set).values(
            "program",
            "course_code",
            "course_name",
            "credit_hours",
            "is_online",
            "max_capacity",
            "programme_term",
        )
    )
    # Group by course code: one offering per (code, sharing programme set).
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in plan_rows:
        grouped[str(row["course_code"]).strip().upper()].append(row)

    offerings: list[Offering] = []
    offering_by_code: dict[str, Offering] = {}
    for code, rows in sorted(grouped.items()):
        progs = frozenset(str(r["program"]).upper() for r in rows)
        credits = max(int(r["credit_hours"] or 0) for r in rows)
        name = str(rows[0].get("course_name") or code)
        declared = [int(r["max_capacity"]) for r in rows if r["max_capacity"]]
        capacity = min(declared) if declared else policy.default_capacity

        # Course-category policy: GS/GSE are online (no room, no campus gap);
        # graduation projects and cooperative training are not timetabled at all.
        unscheduled = any(is_unscheduled_course(r.get("course_name") or "") for r in rows)
        online = is_online_course(code)

        offering = Offering(
            id=_offering_id(code, progs),
            course_code=code,
            course_name=name,
            credit_hours=credits,
            programs=progs,
            terms=frozenset(int(r["programme_term"] or 0) for r in rows),
            requirements=() if unscheduled else compile_requirements(credits, is_online=online),
            capacity=capacity,
            capacity_is_declared=bool(declared),
            is_scheduled=not unscheduled,
        )
        offerings.append(offering)
        offering_by_code[code] = offering

    # ── the real electives the resolver named, which the plan does not hold ──
    #
    # `resolve_elective_recommendations` turns the plan's PLACEHOLDER (AI1,
    # "PROGRAM ELECTIVE I") into the catalogue course that fills it this term
    # (AI463, "Information Retrieval"). The plan table only ever holds the
    # placeholder — the real course lives in `ElectiveCourse` — so building
    # offerings from the plan alone and then dropping unrecognised codes threw
    # every resolved elective on the floor.
    #
    # Measured before this existed, male AI/DS cohort 1448 T1: **65 student
    # demands discarded** (AI463 50, DS487 15), AI1/AI2/AI3/DS1/DS2/DS3 all
    # reporting `demand=0, sections=0`, and readiness announcing them under
    # NO_DEMAND — true only because the demand had been thrown away. The
    # project's own scenario budget plans AI463 at 2 sections and DS487 at 1, so
    # the engine was three sections short and the students who need an elective
    # got none. The perverse part: the placeholders that CAN be resolved got
    # nothing, while the ones that cannot (FE1, FE2, GSE1) were the only ones
    # scheduled — under names that cannot be published.
    #
    # This is the mirror of the eligibility fold further down, which maps a
    # target BACK to its placeholder so a staffing approval is not lost. Here the
    # target gets an offering of its own, because it is the course a student
    # actually enrols in and the name the timetable has to print.
    wanted_codes = {str(c).strip().upper() for codes in recommended.values() for c in codes}
    for code in sorted(wanted_codes - set(offering_by_code)):
        elective = elective_by_code.get(code)
        hosts = [
            offering_by_code[ph]
            for ph in sorted(placeholders_of_target.get(code, ()))
            if ph in offering_by_code
        ]
        if elective is None or not hosts:
            # Either the catalogue does not know this code, or nothing in this
            # run's plan stands in for it. Reported rather than dropped in
            # silence — a filter nobody can see is indistinguishable from a
            # filter that is wrong, which is precisely how the resolved
            # electives went missing in the first place.
            #
            # DEFENSIVE, not a live path: both `placeholders_of_target` and the
            # resolver read the same term mappings filtered to the same
            # programmes, and a placeholder can only be recommended if the plan
            # holds it — so `hosts` is non-empty whenever the resolver fired.
            # It is deliberately not covered by a test, because every fixture
            # that appears to reach it actually reaches something else. It
            # exists so that a future change to either side surfaces the loss
            # instead of hiding it.
            unmatched_demand[code] += sum(
                1
                for codes in recommended.values()
                if code in {str(c).strip().upper() for c in codes}
            )
            continue
        # Programmes and plan terms come from the placeholder(s) this course
        # fills: that is where a student meets it, which decides both the room
        # pool it may use and the board it lands on.
        progs = frozenset().union(*(h.programs for h in hosts))
        terms = frozenset().union(*(h.terms for h in hosts))
        credits = int(elective.credit_hours or 0) or max(h.credit_hours for h in hosts)
        offering = Offering(
            id=_offering_id(code, progs),
            course_code=code,
            course_name=str(elective.course_name or code),
            credit_hours=credits,
            programs=progs,
            terms=terms,
            requirements=compile_requirements(credits, is_online=is_online_course(code)),
            capacity=policy.default_capacity,
            capacity_is_declared=False,
            is_scheduled=True,
        )
        offerings.append(offering)
        offering_by_code[code] = offering

    # ── student demand, mapped onto offerings ──
    demand: list[StudentDemand] = []
    for sid, codes in sorted(recommended.items()):
        ids = set()
        for c in codes:
            code = str(c).strip().upper()
            offering = offering_by_code.get(code)
            if offering is None:
                unmatched_demand[code] += 1
                continue
            ids.add(offering.id)
        if ids:
            demand.append(
                StudentDemand(
                    student_id=int(sid),
                    program=program_by_student.get(int(sid), ""),
                    offering_ids=frozenset(ids),
                )
            )

    # ── section plan: the PROJECT's planner, not a second opinion ──────────
    #
    # This used to be `ceil(demand / default_capacity)` with one capacity for
    # everything, and it was wrong in a way that mattered. The institution sizes
    # sections by course type — 25 seats for a local four-credit course, 40 for
    # other local courses, **50 for external/service courses** — with declared
    # per-programme capacities overriding both. A flat 25 split every service
    # course into twice as many sections as the institution would run: 88 against
    # 77 on the male AI/DS cohort, and 142 against 100 on female CS.
    #
    # That is not merely untidy. Sections of one course may never overlap, and the
    # week holds 25 non-overlapping slots, so every invented section consumes a
    # scarce resource — enough of them and the cohort becomes unschedulable
    # outright, which is exactly what happened to female CS.
    #
    # So `compute_section_plan` decides section counts and seat limits. It is
    # planning policy, in the same category as the recommender: an upstream
    # service this subsystem CONSUMES rather than rewrites. Two answers to "how
    # many sections should this course run" would be a worse defect than any
    # scheduling bug.
    from core.services.section_planning import compute_section_plan

    head_count: dict[str, int] = defaultdict(int)
    for d in demand:
        for oid in d.offering_ids:
            head_count[oid] += 1

    aggregate: Counter[str] = Counter()
    for offering in offerings:
        if offering.is_scheduled and head_count.get(offering.id):
            aggregate[offering.course_code] = head_count[offering.id]

    declared_caps = {
        offering.course_code: offering.capacity
        for offering in offerings
        if offering.capacity_is_declared
    }
    planned = {
        str(row["course_code"]).strip().upper(): row
        for row in compute_section_plan(aggregate, programme_capacities=declared_caps)
    }

    sections: list[Section] = []
    for offering in offerings:
        if not offering.is_scheduled:
            continue  # graduation project / training — carries credit, has no meeting
        if not head_count.get(offering.id):
            continue  # no demand this term -> no sections; not an error
        row = planned.get(offering.course_code)
        if row is None:
            # The planner had nothing to say; fall back rather than drop a course
            # somebody is waiting for.
            count = max(1, math.ceil(head_count[offering.id] / offering.capacity))
            seats = offering.capacity
        else:
            count = max(1, int(row["num_sections"]))
            seats = max(1, int(row["max_per_section"]))
        for index in range(1, count + 1):
            sections.append(
                Section(
                    id=f"{offering.id}#S{index}",
                    offering_id=offering.id,
                    index=index,
                    capacity=seats,
                    instructor_id=None,  # optional by design, permanently (D5)
                )
            )

    # ── rooms, filtered once to this gender and these programmes (D1) ──
    rooms: list[Room] = []
    for row in RoomRow.objects.filter(section__iexact=gender).order_by("id"):
        room_programs = frozenset(
            p.strip().upper() for p in str(row.department or "").split(",") if p.strip()
        )
        if not (room_programs & program_set):
            continue
        kind = (
            MeetingKind.LAB
            if str(row.room_type or "").strip().lower() == "lab"
            else MeetingKind.LECTURE
        )
        rooms.append(
            Room(
                id=str(row.room_code).strip().upper(),
                code=str(row.room_code).strip(),
                capacity=int(row.capacity or 0),
                kind=kind,
                programs=room_programs,
            )
        )

    # ── instructor ELIGIBILITY (not assignment — they are different things) ──
    # Filtered to this snapshot's gender like everything else (D1). CourseInstructor
    # carries the gender in ``section``; ignoring it would let a male-cohort
    # eligibility row appear as staffing for the female cohort.
    #
    # A staffing row and the timetable can name the elective differently: the row
    # may say AI463 ("Information Retrieval") while the plan says AI1 ("PROGRAM
    # ELECTIVE I"), or the other way round. Matching on the code alone throws one
    # of them away in silence, so an approval reaches BOTH the placeholder and
    # every catalogue course that fills it — a union, never a replacement.
    #
    # Both directions are needed, and which one does the work depends on the
    # data. Now that a resolved elective gets an offering of its own, AI463 is
    # where the sections are and AI1 usually has none; an approval to teach "the
    # AI1 slot" is an approval to teach whatever fills it, so it must reach
    # AI463. Where no mapping exists (FE1, FE2, GSE1) the placeholder is still
    # the only offering there is, and an approval naming a catalogue course must
    # fold back onto it.
    #
    # Both maps are keyed on **(programme, target)**, never on the target alone.
    # An approval is granted for one programme's elective slot, and both the
    # mapping and the staffing row carry a programme; dropping either would let
    # an approval to teach CS403 for CS count as staffing for an AI slot that
    # happens to draw on the same catalogue course. Widening who may teach what
    # is not a rounding error, so the two programmes must agree, and the offering
    # reached must actually serve that programme.
    targets_of_placeholder: dict[tuple[str, str], set[str]] = defaultdict(set)
    for (programme, target), placeholders in placeholder_of.items():
        for placeholder in placeholders:
            targets_of_placeholder[(programme, placeholder)].add(target)

    eligible: dict[int, set[str]] = defaultdict(set)
    for link in CourseInstructor.objects.filter(
        program__in=program_set, section__iexact=gender
    ).select_related("instructor"):
        code = str(link.course_code).strip().upper()
        if not link.instructor_id:
            continue
        programme = str(link.program).strip().upper()
        reached = {code} if code in offering_by_code else set()
        reached |= placeholder_of.get((programme, code), set())  # target -> its slot
        reached |= targets_of_placeholder.get((programme, code), set())  # slot -> its courses
        for resolved in sorted(reached):
            offering = offering_by_code.get(resolved)
            if offering is None:
                continue
            if programme not in offering.programs:
                # The offering does not belong to the programme that approved
                # them. Checked on EVERY match, not only on folded ones: a
                # direct code match used to skip this, so an approval to teach
                # AI463 for CS counted as staffing for an AI-only AI463 — the
                # very widening the fold below was written to prevent, walking
                # in through the front door.
                continue
            eligible[int(link.instructor_id)].add(offering.id)
    instructor_names = {
        row.id: row.full_name for row in InstructorRow.objects.filter(id__in=list(eligible) or [0])
    }
    instructors = tuple(
        Instructor(
            id=iid,
            name=instructor_names.get(iid, f"instructor:{iid}"),
            eligible_offerings=frozenset(oids),
        )
        for iid, oids in sorted(eligible.items())
    )

    source_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "students": sorted(program_by_student),
                "plan_rows": len(plan_rows),
                "rooms": sorted(r.id for r in rooms),
                "recommended": sorted((k, sorted(v)) for k, v in recommended.items()),
            },
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()

    return Snapshot(
        academic_year=str(academic_year),
        term=int(term),
        gender=gender,
        programs=tuple(sorted(program_set)),
        grid=grid,
        offerings=tuple(offerings),
        sections=tuple(sections),
        rooms=tuple(rooms),
        instructors=instructors,
        demand=tuple(demand),
        policy=policy,
        source_fingerprint=source_fingerprint,
        excluded_withdrawn=excluded_withdrawn,
        unmatched_demand=tuple(sorted(unmatched_demand.items())),
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )

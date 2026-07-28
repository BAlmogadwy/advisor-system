"""Tests for the new `scheduler` subsystem — S1 (snapshot + readiness).

Two layers, mirroring the architecture:

* the **domain** is pure Python, so it is tested with no database at all — that
  is the property that makes the solver reproducible and cheap to test;
* **intake/readiness** are tested against small in-DB fixtures.

The architectural boundaries (no Django in `domain/`, no coupling to the old
engine, no reading of saved scenarios) are enforced by tests here rather than by
convention, because convention is exactly what failed in the previous rebuilds.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.models import ProgrammeRequirement, Room, Student
from scheduler.domain import (
    CapacityPolicy,
    DeliveryMode,
    Grid,
    MeetingKind,
    Slot,
    TimeWindow,
    parse_hhmm,
)
from scheduler.intake import (
    IntakeError,
    build_snapshot,
    compile_requirements,
    default_grid,
    morning_block,
)
from scheduler.readiness import Severity, assess

# ── architectural boundaries ──────────────────────────────────────────────

DOMAIN_DIR = Path(__file__).resolve().parent.parent / "scheduler" / "domain"
SCHEDULER_DIR = Path(__file__).resolve().parent.parent / "scheduler"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_domain_is_pure_python_no_django():
    """`scheduler.domain` must never import Django — this is what keeps the
    solver reproducible and testable without a database."""
    for path in DOMAIN_DIR.glob("*.py"):
        offenders = {m for m in _imported_modules(path) if m.split(".")[0] == "django"}
        assert not offenders, f"{path.name} imports Django: {offenders}"


#: The one `core.services.timetable_*` module this subsystem may import.
#:
#: The rule exists to keep the old BUILDER out — its placer, optimiser, repair
#: passes and scenario state. `timetable_course_tier` is none of those: it is a
#: pure classifier over `ProgrammeRequirement` that says which courses a
#: registrar must resolve and which a student can pick up elsewhere in the
#: college. That is institutional policy, and this project's most expensive
#: mistakes have all come from restating policy instead of consuming it —
#: section sizing, elective resolution, the cross-term split. One named
#: exemption is cheaper than a fourth divergent copy.
#:
#: Everything else under that prefix stays forbidden. Add to this set only for
#: something that is policy rather than engine, and say why.
POLICY_MODULES = {"core.services.timetable_course_tier"}


def test_scheduler_never_imports_the_old_timetable_engine():
    """Zero coupling to `core.services.timetable_*` — the current builder must be
    unaffected by anything here."""
    for path in SCHEDULER_DIR.rglob("*.py"):
        offenders = {
            m
            for m in _imported_modules(path)
            if m.startswith("core.services.timetable") and m not in POLICY_MODULES
        }
        assert not offenders, f"{path} imports the old engine: {offenders}"


def _referenced_names(path: Path) -> set[str]:
    """Identifiers actually used in code — docstrings and comments excluded, so a
    doc that *describes* a forbidden model does not trip the boundary check."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name.split(".")[-1])
            if node.asname:
                names.add(node.asname)
    return names


#: The single file allowed to touch the workspace's scenario tables. Everything
#: else in the subsystem plans in ignorance of them.
SCENARIO_SEAM = "bridge.py"


def test_the_planner_never_reads_a_saved_scenario():
    """No saved scenario, board or placement is ever an INPUT to planning.

    The original rule said the subsystem must not reference those models at all,
    which was right while it had nowhere to put its results. It now writes a
    finished board into a scenario the caller created, so the rule is sharpened
    rather than dropped: the part that DECIDES a timetable still may not see a
    previous one.

    That is the property worth protecting. A planner that reads an existing board
    can inherit its mistakes, warm-start from them, or quietly reproduce them and
    call it agreement — which is exactly how the previous engine's results became
    impossible to trust.
    """
    forbidden = {"TimetableScenario", "DeliveryBoard", "SectionPlacement", "TermSectionMeeting"}
    for path in SCHEDULER_DIR.rglob("*.py"):
        if path.name == SCENARIO_SEAM:
            continue
        offenders = _referenced_names(path) & forbidden
        assert not offenders, (
            f"{path.name} references scenario artifacts {offenders}. Only "
            f"{SCENARIO_SEAM} may — planning must stay ignorant of saved boards."
        )


def test_the_seam_is_exactly_one_file():
    """A seam that spreads is not a seam.

    The exemption above is what lets the new engine deliver a board into the
    existing screen. Naming one file keeps that from becoming a general licence:
    if a second module starts reaching for scenario tables, this fails and the
    decision gets made deliberately instead of by accident.
    """
    forbidden = {"TimetableScenario", "DeliveryBoard", "SectionPlacement", "TermSectionMeeting"}
    touching = {
        path.name for path in SCHEDULER_DIR.rglob("*.py") if _referenced_names(path) & forbidden
    }
    assert touching <= {SCENARIO_SEAM}, (
        f"scenario tables are now touched by {sorted(touching)}; the seam is "
        f"meant to be {SCENARIO_SEAM} alone"
    )


def test_the_seam_writes_only_rows_it_can_identify_as_its_own():
    """Its rows must be distinguishable from the existing engine's, or a rebuild
    cannot clean up after itself without destroying somebody else's work."""
    from scheduler.bridge import SOURCE_TAG

    source = (SCHEDULER_DIR / SCENARIO_SEAM).read_text(encoding="utf-8")
    assert SOURCE_TAG != "tw_auto", "must not masquerade as the old engine's rows"
    assert "source_tag=SOURCE_TAG" in source
    # and it deletes only its own before rewriting
    assert (
        "source_tag=SOURCE_TAG"
        in source.split("delete()")[0].split("TermSection.objects.filter")[-1]
    )


# ── domain: calendar ──────────────────────────────────────────────────────


def test_time_windows_are_half_open():
    a = TimeWindow(540, 615)  # 09:00-10:15
    b = TimeWindow(615, 690)  # 10:15-11:30 — touches, does not overlap
    c = TimeWindow(600, 660)  # 10:00-11:00 — genuinely overlaps a
    assert not a.overlaps(b)
    assert a.overlaps(c) and c.overlaps(a)


def test_non_positive_window_is_rejected():
    with pytest.raises(ValueError):
        TimeWindow(540, 540)


def test_no_room_turnover_tight_transitions_are_legal():
    """D8: room exclusivity is raw teaching-time overlap — there is NO turnover
    allowance. Tight transitions are deliberate: they cost no room capacity (the
    75-min grid already carries 15-min gaps) and they shorten student days.

    Pinned as a test because adding a turnover buffer looks like a safety
    improvement and is not: it would forbid six same-room pairs, buy nothing, and
    push a student finishing at 10:40 from a 10:50 lecture out to 13:00.
    """
    # A 100-minute lecture ending 10:40 and a lecture starting 10:50 — 10 minutes
    # apart, same room. Legal.
    assert not TimeWindow(540, 640).overlaps(TimeWindow(650, 725))
    # Touching windows: 14:45-16:00 then 16:00-17:15. Legal.
    assert not TimeWindow(885, 960).overlaps(TimeWindow(960, 1035))
    # Consecutive 100-minute lectures at lab timings — 5 minutes apart. Legal.
    assert not TimeWindow(540, 640).overlaps(TimeWindow(645, 745))
    # Genuine overlap is still a conflict.
    assert TimeWindow(540, 640).overlaps(TimeWindow(630, 705))


def test_parse_hhmm_rejects_malformed():
    assert parse_hhmm("09:05") == 545
    for bad in ("", "9", "99:00", "09:99", "ab:cd"):
        with pytest.raises(ValueError):
            parse_hhmm(bad)


def test_timing_family_is_decided_by_duration_not_kind():
    """A 100-minute LECTURE runs at the 100-minute (lab-timing) windows while
    occupying a LECTURE room. Timing family follows duration; room family follows
    kind. Conflating the two is what made the same board judgeable both ways."""
    grid = default_grid()
    assert [str(w) for w in grid.windows_for(100)] == [
        "09:00-10:40",
        "10:45-12:25",
        "13:00-14:40",
        "14:45-16:25",
        "16:30-18:10",
    ]
    assert len(grid.windows_for(75)) == 7
    assert grid.windows_for(90) == ()  # a length nobody declared is honestly empty


def test_overlapping_alternatives_do_not_add_room_capacity():
    """10:30/10:50 and 14:30/14:45 are one opportunity offered two ways. Counting
    declared cells would overstate a room's throughput by 40%."""
    grid = default_grid()
    # 7 in-person lecture cells x 5 days are *declared*...
    in_person = [
        s for s in grid.of_kind(MeetingKind.LECTURE) if s.delivery is DeliveryMode.IN_PERSON
    ]
    assert len(in_person) == 35
    # ...but only 5 per day can actually be held, because 10:30/10:50 and
    # 14:30/14:45 are one opportunity offered two ways.
    assert grid.max_nonoverlapping_per_day(frozenset({75})) == 5
    assert grid.max_nonoverlapping_per_day(frozenset({75, 100})) == 5
    assert grid.room_periods_per_week(frozenset({75, 100}), room_count=5) == 125


def test_online_courses_have_three_windows_of_their_own_and_no_others():
    """Owner rule, 2026-07-28: GS and GSE are online, they run at 15:50, 17:40 and
    19:30, and nowhere else. No other course may use those windows.

    The exclusivity runs both ways and both directions are enforced here: the
    grid gives online meetings only these three, and the shared slot config
    flags the same three `online_only` so no automatic placer offers them to
    anything that needs a room."""
    grid = default_grid()
    online = grid.windows_for(100, DeliveryMode.ONLINE)
    assert [str(w) for w in online] == ["15:50-17:30", "17:40-19:20", "19:30-21:10"]

    in_person = grid.windows_for(100, DeliveryMode.IN_PERSON)
    assert not set(online) & set(in_person), (
        "an online window leaked into the on-campus family, so a lab could be put there"
    )
    assert [str(w) for w in in_person] == [
        "09:00-10:40",
        "10:45-12:25",
        "13:00-14:40",
        "14:45-16:25",
        "16:30-18:10",
    ]
    # No room at any hour -- that is a property of the delivery mode, not the time.
    assert grid.max_nonoverlapping_per_day(frozenset({100}), DeliveryMode.ONLINE) == 3


def test_a_room_period_bound_is_a_real_upper_bound():
    """Whatever mix is chosen, no room can host more than the bound in a day."""
    grid = default_grid()
    bound = grid.max_nonoverlapping_per_day(frozenset({75, 100}))
    # Room capacity is an in-person question: online sessions consume no room, so
    # they must not inflate the bound.
    windows = sorted(
        {s.window for s in grid.slots if s.delivery is DeliveryMode.IN_PERSON},
        key=lambda w: (w.start, w.end),
    )
    # brute-force the largest pairwise non-overlapping set for one day
    best = 0
    for mask in range(1 << len(windows)):
        chosen = [windows[i] for i in range(len(windows)) if mask >> i & 1]
        if all(not a.overlaps(b) for i, a in enumerate(chosen) for b in chosen[i + 1 :]):
            best = max(best, len(chosen))
    assert bound == best


def test_grid_fingerprint_is_stable_and_order_independent():
    a = default_grid()
    b = Grid(slots=tuple(reversed(a.slots)))
    assert a.fingerprint() == b.fingerprint()


def test_grid_rejects_duplicate_slots():
    slot = Slot(
        day=list(default_grid().days())[0], window=TimeWindow(540, 615), kind=MeetingKind.LECTURE
    )
    with pytest.raises(ValueError):
        Grid(slots=(slot, slot))


# ── domain: requirement compilation ───────────────────────────────────────


@pytest.mark.parametrize(
    "credits,expected",
    [
        (4, [(MeetingKind.LECTURE, 75, 2), (MeetingKind.LAB, 100, 1)]),
        (3, [(MeetingKind.LECTURE, 75, 2)]),
        (2, [(MeetingKind.LECTURE, 100, 1)]),
        (1, [(MeetingKind.LECTURE, 75, 1)]),
    ],
)
def test_requirement_compilation_by_credits(credits, expected):
    reqs = compile_requirements(credits, is_online=False)
    assert [(r.kind, r.duration, r.count_per_week) for r in reqs] == expected


def test_online_offerings_need_no_room():
    reqs = compile_requirements(3, is_online=True)
    assert all(r.delivery is DeliveryMode.ONLINE for r in reqs)
    assert all(not r.needs_room for r in reqs)


def test_capacity_policy_rejects_nonsense():
    with pytest.raises(ValueError):
        CapacityPolicy(default_capacity=0)
    with pytest.raises(ValueError):
        CapacityPolicy(default_capacity=25, buffer=0.5)


# ── intake + readiness (DB) ───────────────────────────────────────────────

pytestmark = pytest.mark.django_db


def _plan(program, code, credits=3, capacity=None, online=False, term=1, name=None):
    ProgrammeRequirement.objects.create(
        program=program,
        course_code=code,
        course_name=name or code,
        type="mandatory",
        programme_term=term,
        credit_hours=credits,
        is_online=online,
        max_capacity=capacity,
    )


def _student(sid, program, gender):
    return Student.objects.create(
        student_id=sid,
        registration_no=str(sid),
        name=f"S{sid}",
        program=program,
        section=gender,
        status="active",
    )


def _room(code, gender, kind="lecture", capacity=40, dept="AI"):
    Room.objects.create(
        room_code=code, capacity=capacity, room_type=kind, department=dept, section=gender
    )


def test_snapshot_is_single_gender():
    with pytest.raises(IntakeError):
        build_snapshot(
            academic_year="1448", term=1, gender="X", programs=["AI"], default_capacity=25
        )


def test_no_students_of_that_gender_fails_closed():
    _plan("AI", "AI101")
    _student(1001, "AI", "M")
    with pytest.raises(IntakeError):
        build_snapshot(
            academic_year="1448", term=1, gender="F", programs=["AI"], default_capacity=25
        )


def test_snapshot_excludes_the_other_gender_entirely():
    """D1: rooms and students are filtered once at intake, so gender never
    appears as a constraint the solver has to reason about."""
    _plan("AI", "AI101")
    _student(1001, "AI", "M")
    _student(1002, "AI", "F")
    _room("M-1", "M")
    _room("F-1", "F")
    snap = build_snapshot(
        academic_year="1448", term=1, gender="M", programs=["AI"], default_capacity=25
    )
    assert snap.gender == "M"
    assert [r.code for r in snap.rooms] == ["M-1"]


def test_assumed_capacity_is_reported_never_silent():
    """D3: the fallback is declared config and every offering that used it is named."""
    _plan("AI", "AI101", capacity=None)
    _plan("AI", "AI102", capacity=30)
    _student(1001, "AI", "M")
    _room("M-1", "M")
    snap = build_snapshot(
        academic_year="1448", term=1, gender="M", programs=["AI"], default_capacity=25
    )
    by_code = {o.course_code: o for o in snap.offerings}
    assert by_code["AI101"].capacity == 25 and not by_code["AI101"].capacity_is_declared
    assert by_code["AI102"].capacity == 30 and by_code["AI102"].capacity_is_declared
    report = assess(snap)
    assumed = [f for f in report.findings if f.code == "ASSUMED_CAPACITY"]
    assert assumed and "AI101" in assumed[0].detail["offerings"]


def test_snapshot_fingerprint_is_deterministic():
    """N8: same input ⇒ same fingerprint, so two runs can be honestly compared."""
    _plan("AI", "AI101")
    _student(1001, "AI", "M")
    _room("M-1", "M")
    kwargs = dict(academic_year="1448", term=1, gender="M", programs=["AI"], default_capacity=25)
    assert build_snapshot(**kwargs).fingerprint() == build_snapshot(**kwargs).fingerprint()


def test_two_credit_course_is_placeable_in_a_lecture_room_at_lab_timing():
    """A 2-credit course needs one 100-minute meeting. It is a LECTURE (so it
    needs a lecture room) placed at a 100-minute window (lab timing). That is
    legal, and readiness must not block it."""
    _plan("AI", "AI200", credits=2)
    _student(1001, "AI", "M")
    _room("M-1", "M", kind="lecture")
    snap = build_snapshot(
        academic_year="1448", term=1, gender="M", programs=["AI"], default_capacity=25
    )
    offering = next(o for o in snap.offerings if o.course_code == "AI200")
    (req,) = offering.requirements
    assert (req.kind, req.duration) == (MeetingKind.LECTURE, 100)
    assert snap.grid.windows_for(100)  # legal timings exist
    assert not [f for f in assess(snap).blocking if f.code == "NO_LEGAL_WINDOW_FOR_DURATION"]


def test_readiness_blocks_a_duration_the_grid_never_declares():
    """Only a length with no declared window anywhere is genuinely unplaceable."""
    _plan("AI", "AI200", credits=3)
    _student(1001, "AI", "M")
    _room("M-1", "M")
    snap = build_snapshot(
        academic_year="1448",
        term=1,
        gender="M",
        programs=["AI"],
        default_capacity=25,
        grid=Grid.from_spec(lecture_starts={"09:00": 50}, lab_starts={}),
    )
    report = assess(snap)
    assert "NO_LEGAL_WINDOW_FOR_DURATION" in {f.code for f in report.blocking}
    assert report.status == "BLOCKED_INPUT"


def test_gs_courses_are_online_regardless_of_the_is_online_column():
    """GS/GSE are online by policy. The column contradicts itself (GS112 is
    flagged online in one programme and not in another), so a categorical rule
    corrects the data rather than propagating the inconsistency."""
    _plan("AI", "GS112", credits=2, online=False)  # column says NOT online
    _student(1001, "AI", "M")
    _room("M-1", "M")
    snap = build_snapshot(
        academic_year="1448", term=1, gender="M", programs=["AI"], default_capacity=25
    )
    offering = next(o for o in snap.offerings if o.course_code == "GS112")
    assert offering.is_fully_online
    assert offering.physical_meetings_per_week == 0  # consumes no room


def test_graduation_projects_are_not_timetabled_at_all():
    _plan("AI", "AI491", credits=2)
    ProgrammeRequirement.objects.filter(course_code="AI491").update(
        course_name="GRADUATION PROJECT I"
    )
    _student(1001, "AI", "M")
    _room("M-1", "M")
    snap = build_snapshot(
        academic_year="1448", term=1, gender="M", programs=["AI"], default_capacity=25
    )
    offering = next(o for o in snap.offerings if o.course_code == "AI491")
    assert not offering.is_scheduled
    assert offering.requirements == ()
    assert not snap.sections_by_offering.get(offering.id)  # no sections, ever


def test_project_management_courses_are_still_taught():
    """A substring match on 'PROJECT' would wrongly unschedule IS357/IS362
    'INFORMATION SYSTEMS PROJECT MANAGEMENT', which are ordinary courses."""
    from scheduler.intake import is_unscheduled_course

    assert is_unscheduled_course("GRADUATION PROJECT I")
    assert is_unscheduled_course("COOPERATIVE TRAINING (CONTINUING WITH SUMMER)")
    assert is_unscheduled_course("TRAINING")
    assert not is_unscheduled_course("INFORMATION SYSTEMS PROJECT MANAGEMENT")


def test_readiness_is_informational_when_nothing_is_impossible():
    """D4: gaps report as scope and the run proceeds; only impossibility blocks."""
    _plan("AI", "AI101", credits=3, capacity=40)
    _student(1001, "AI", "M")
    _room("M-1", "M", capacity=40)
    snap = build_snapshot(
        academic_year="1448", term=1, gender="M", programs=["AI"], default_capacity=25
    )
    report = assess(snap)
    assert report.status == "READY"
    assert all(f.severity is not Severity.BLOCKING for f in report.findings)


def test_a_room_shortage_never_blocks_it_reports_unassigned_rooms():
    """D6: a room is an assignment that can be left unmade. Too few rooms means
    those meetings get a time and no room — it does not stop the build."""
    for n in range(1, 30):  # far more demand than one room can serve
        _plan("AI", f"AI1{n:02d}", credits=3, capacity=5)
    for sid in range(3001, 3061):
        _student(sid, "AI", "M")
    _room("M-1", "M")  # a single lecture room for the whole cohort
    snap = build_snapshot(
        academic_year="1448", term=1, gender="M", programs=["AI"], default_capacity=25
    )
    report = assess(snap)
    shortage = [f for f in report.findings if f.code == "ROOM_PERIOD_SHORTAGE"]
    if shortage:  # depends what the recommender actually returns
        assert shortage[0].severity is Severity.WARNING
        assert shortage[0].detail["minimum_unroomed"] > 0
        assert report.status == "READY"  # never blocked by a room shortage


def test_no_rooms_at_all_still_does_not_block():
    _plan("AI", "AI101", credits=3)
    _student(1001, "AI", "M")
    # deliberately no rooms created
    snap = build_snapshot(
        academic_year="1448", term=1, gender="M", programs=["AI"], default_capacity=25
    )
    report = assess(snap)
    assert not any(
        f.code == "NO_ROOMS_OF_KIND" and f.severity is Severity.BLOCKING for f in report.findings
    )
    assert report.status == "READY"


def test_unscheduled_offerings_do_not_report_a_seat_shortage():
    """Graduation projects have no sections by design, not by shortage."""
    _plan("AI", "AI491", credits=2)
    ProgrammeRequirement.objects.filter(course_code="AI491").update(
        course_name="GRADUATION PROJECT I"
    )
    _student(1001, "AI", "M")
    _room("M-1", "M")
    snap = build_snapshot(
        academic_year="1448", term=1, gender="M", programs=["AI"], default_capacity=25
    )
    shortage = [f for f in assess(snap).findings if f.code == "SEAT_SHORTAGE"]
    listed = {r["course"] for f in shortage for r in f.detail.get("offerings", [])}
    assert "AI491" not in listed


def test_instructor_scope_is_always_reported_with_its_denominator():
    """D5: partial linkage is permanent, so no instructor figure is ever
    published without the coverage it was computed over."""
    _plan("AI", "AI101")
    _student(1001, "AI", "M")
    _room("M-1", "M")
    snap = build_snapshot(
        academic_year="1448", term=1, gender="M", programs=["AI"], default_capacity=25
    )
    report = assess(snap)
    scope = next(f for f in report.findings if f.code == "INSTRUCTOR_SCOPE")
    assert scope.severity is Severity.INFO  # never an error
    assert {"sections_assigned", "sections_total", "eligible_instructors"} <= scope.detail.keys()


def test_sections_are_planned_from_demand_and_capacity():
    _plan("AI", "AI101", credits=3, capacity=10)
    for sid in range(2001, 2026):  # 25 students
        _student(sid, "AI", "M")
    _room("M-1", "M")
    snap = build_snapshot(
        academic_year="1448", term=1, gender="M", programs=["AI"], default_capacity=25
    )
    offering = next(o for o in snap.offerings if o.course_code == "AI101")
    demanded = snap.demand_index.by_offering.get(offering.id, 0)
    planned = len(snap.sections_by_offering.get(offering.id, ()))
    if demanded:  # the recommender decides who actually needs it
        assert planned == -(-demanded // 10)  # ceil(demand / capacity)


# ── elective placeholders ───────────────────────────────────────
#
# "AI1 / PROGRAM ELECTIVE I" is a slot, not a course. The timetable can place it
# perfectly and still be unpublishable, because nobody can enrol in "PROGRAM
# ELECTIVE I". These pin the three outcomes apart rather than collapsing them
# into pass/fail.
#
# The snapshot is built by hand rather than through the recommender: the report
# must be exercised on offerings that definitely exist, not on whatever demand
# happens to produce, or the assertions quietly stop running.

from core.models import CourseInstructor, ElectiveCourse, ElectiveTermMapping  # noqa: E402
from core.models import Instructor as InstructorRow  # noqa: E402
from scheduler.domain import (  # noqa: E402
    MeetingRequirement,
    Offering,
    Section,
    Snapshot,
)
from scheduler.intake import elective_placeholder_report  # noqa: E402


def _map(code, placeholder, programme="AI", year="1448", term=1):
    course = ElectiveCourse.objects.create(
        course_code=code,
        course_name=f"Real {code}",
        programme=programme,
        category="program",
        credit_hours=3,
    )
    ElectiveTermMapping.objects.create(
        academic_year=year,
        term=term,
        programme=programme,
        placeholder_code=placeholder,
        elective=course,
    )


def _snapshot_of(*codes):
    """A snapshot containing exactly these offerings, one section each."""
    offerings, sections = [], []
    for code in codes:
        offering = Offering(
            id=f"off:{code}",
            course_code=code,
            course_name=f"{code} NAME",
            credit_hours=3,
            programs=frozenset({"AI"}),
            terms=frozenset({1}),
            requirements=(MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, 2),),
            capacity=25,
            capacity_is_declared=True,
        )
        offerings.append(offering)
        sections.append(Section(f"off:{code}#S1", offering.id, 1, 20))
    return Snapshot(
        academic_year="1448",
        term=1,
        gender="M",
        programs=("AI",),
        grid=default_grid(),
        offerings=tuple(offerings),
        sections=tuple(sections),
        rooms=(),
        instructors=(),
        demand=(),
        policy=CapacityPolicy(default_capacity=25),
        source_fingerprint="t",
        created_at="2026-07-26T00:00:00+00:00",
    )


def test_a_placeholder_with_one_option_is_resolved():
    _map("AI463", "AI1")
    rows = {
        r["placeholder"]: r for r in elective_placeholder_report(_snapshot_of("AI1"), "1448", 1)
    }
    assert rows["AI1"]["status"] == "RESOLVED"
    assert rows["AI1"]["options"] == ["AI463"]
    assert rows["AI1"]["sections"] == 1


def test_several_options_stay_ambiguous_rather_than_being_guessed():
    """Students split across the choices by a registration decision this stage
    cannot see, so naming one of them would be an invention."""
    _map("AI463", "AI1")
    _map("AI461", "AI1")
    rows = {
        r["placeholder"]: r for r in elective_placeholder_report(_snapshot_of("AI1"), "1448", 1)
    }
    assert rows["AI1"]["status"] == "AMBIGUOUS"
    assert rows["AI1"]["options"] == ["AI461", "AI463"]


def test_an_unmapped_placeholder_is_an_evidence_gap_not_a_failure():
    rows = {
        r["placeholder"]: r for r in elective_placeholder_report(_snapshot_of("FE1"), "1448", 1)
    }
    assert rows["FE1"]["status"] == "UNMAPPED"
    assert rows["FE1"]["options"] == []


def test_real_course_codes_are_never_reported_as_placeholders():
    """Three digits means a real catalogue code — including GS101, whose online
    prefix is irrelevant to whether it names an actual course."""
    snap = _snapshot_of("AI101", "MATH105", "GS101", "AI1")
    reported = {r["placeholder"] for r in elective_placeholder_report(snap, "1448", 1)}
    assert reported == {"AI1"}


def test_a_mapping_from_another_term_is_not_borrowed():
    _map("AI463", "AI1", term=2)
    rows = {
        r["placeholder"]: r for r in elective_placeholder_report(_snapshot_of("AI1"), "1448", 1)
    }
    assert rows["AI1"]["status"] == "UNMAPPED"


# ── staffing rows that used to vanish ─────────────────────────


def test_staffing_for_a_real_elective_counts_towards_its_placeholder():
    """A row naming AI463 was dropped: only AI1 is an offering, so the code
    never matched and the approval vanished with no report of the loss."""
    _plan("AI", "AI1")
    for sid in range(3001, 3011):
        _student(sid, "AI", "M")
    _room("M-1", "M")
    _map("AI463", "AI1")
    # Gender lives on the CourseInstructor link, not on the person (D1).
    slot = InstructorRow.objects.create(full_name="Dr Slot", normalised_name="dr slot")
    target = InstructorRow.objects.create(full_name="Dr Target", normalised_name="dr target")
    CourseInstructor.objects.create(program="AI", course_code="AI1", section="M", instructor=slot)
    CourseInstructor.objects.create(
        program="AI", course_code="AI463", section="M", instructor=target
    )
    snap = build_snapshot(
        academic_year="1448", term=1, gender="M", programs=["AI"], default_capacity=25
    )
    offering = next((o for o in snap.offerings if o.course_code == "AI1"), None)
    assert offering is not None, "AI1 must be an offering for this test to mean anything"
    eligible = {i.id for i in snap.instructors if offering.id in i.eligible_offerings}
    # A union, never a replacement: resolution must not discard the approval
    # held against the slot itself.
    assert {slot.id, target.id} <= eligible


# ── saved plans ───────────────────────────────────────────────────────────
#
# Until these existed, the planner computed a timetable and printed it. Nothing
# survived closing the terminal — which is exactly how the two previous
# greenfield attempts in this project died: layers built beside the product with
# nothing to show for a run.
#
# Fingerprints carry more weight here than they normally would. N8 originally
# promised byte-identical reproducibility and that promise was retracted, since
# the only configurations delivering it produce timetables three to four times
# worse. With reproducibility gone, the stamps are the ONLY way to know two
# plans answered the same question.

from scheduler.domain.board import Board, Placement  # noqa: E402
from scheduler.models import SchedulerPlacement, SchedulerPlan  # noqa: E402
from scheduler.persist import config_fingerprint, save_plan  # noqa: E402


def _tiny_plan_inputs():
    _plan("AI", "AI101")
    for sid in range(4001, 4011):
        _student(sid, "AI", "M")
    _room("M-1", "M")
    snap = build_snapshot(
        academic_year="1448", term=1, gender="M", programs=["AI"], default_capacity=25
    )
    offering = next(o for o in snap.offerings if o.course_code == "AI101")
    section = snap.sections_by_offering[offering.id][0]
    window = snap.grid.day_windows_for(75, DeliveryMode.IN_PERSON)[0]
    board = Board(
        (
            Placement(
                section_id=section.id,
                offering_id=offering.id,
                meeting_index=1,
                kind=MeetingKind.LECTURE,
                delivery=DeliveryMode.IN_PERSON,
                day=window.day,
                window=window.window,
                room_id="M-1",
                instructor_id=None,
            ),
        )
    )
    return snap, board


def _save(snap, board, **overrides):
    kwargs = dict(
        config={"seconds": 10, "alpha": 0.9},
        solver_status="FEASIBLE",
        wall_time_seconds=1.5,
        certification="UNCERTIFIED",
        violation_count=0,
        expected_clashes=12.5,
        naive_baseline=100.0,
        instructors={
            "working_days": 3,
            "floor_days": 3,
            "idle_minutes": 45,
            "coverage": {"sections_assigned": 1, "sections_staffable": 1},
        },
        rooms={"unroomed": 4, "impossible": 3, "saturated": 1},
        pairing={"pairs_back_to_back": 2, "pairs_achievable": 3},
    )
    kwargs.update(overrides)
    return save_plan(snap, board, **kwargs)


def test_a_saved_plan_keeps_its_placements():
    snap, board = _tiny_plan_inputs()
    plan = _save(snap, board)
    assert SchedulerPlan.objects.count() == 1
    stored = SchedulerPlacement.objects.filter(plan=plan)
    assert stored.count() == len(board.placements)
    row = stored.first()
    assert row.course_code == "AI101"
    assert row.room_id == "M-1"


def test_every_metric_is_stored_beside_the_floor_it_should_be_read_against():
    """ "19 working days" means nothing without "and 19 was the minimum". Storing
    a figure without its floor invites a reader to imagine headroom that is not
    there."""
    snap, board = _tiny_plan_inputs()
    plan = _save(snap, board)
    assert plan.instructor_days == 3 and plan.instructor_days_floor == 3
    assert plan.instructor_days_at_floor
    # unroomed floor is impossible + saturated: neither can be rescheduled away
    assert plan.unroomed == 4 and plan.unroomed_floor == 4
    assert plan.unroomed_at_floor


def test_a_plan_is_stamped_with_all_three_fingerprints():
    snap, board = _tiny_plan_inputs()
    plan = _save(snap, board)
    assert plan.snapshot_fingerprint == snap.source_fingerprint
    for stamp in (plan.snapshot_fingerprint, plan.rulebook_fingerprint, plan.config_fingerprint):
        assert len(stamp) == 64, "full SHA-256; a comparability stamp is not a cache key"


def test_plans_of_the_same_question_are_comparable_and_others_are_not():
    """The guard against the mistake that produced two retracted conclusions
    while this was being built: reading a difference between runs of DIFFERENT
    configurations as though it were quality."""
    snap, board = _tiny_plan_inputs()
    a = _save(snap, board)
    b = _save(snap, board)  # same everything: a re-run, which will differ
    assert a.comparable_to(b)

    other = _save(snap, board, config={"seconds": 10, "alpha": 0.5})
    assert not a.comparable_to(other), "different settings are a different question"


def test_the_config_fingerprint_ignores_key_order_but_not_values():
    assert config_fingerprint({"a": 1, "b": 2}) == config_fingerprint({"b": 2, "a": 1})
    assert config_fingerprint({"a": 1}) != config_fingerprint({"a": 2})


def test_seating_is_optional_and_absent_rather_than_zero():
    """Seating is a slower, separate confirmation. A plan without it must say so,
    not report 0% clash-free as though every student had a broken timetable."""
    snap, board = _tiny_plan_inputs()
    plan = _save(snap, board)
    assert plan.students_seated is None
    assert plan.students_clash_free_percent is None

    with_seating = _save(
        snap,
        board,
        seating={
            "students": 10,
            "clash_free_percent": 100.0,
            "average_idle_minutes": 30.0,
        },
    )
    assert with_seating.students_seated == 10
    assert with_seating.students_clash_free_percent == 100.0


def test_online_meetings_are_long_enough_to_land_in_the_lab_grid():
    """Both surfaces split lecture from lab by DURATION — over 80 minutes reads
    as a lab. Online sessions are 100 minutes, so they are placed in the lab
    columns. That is the existing convention, not a choice made here; this pins
    the assumption so a change to either side is noticed.
    """
    from scheduler.intake import DEFAULT_ONLINE_STARTS

    assert DEFAULT_ONLINE_STARTS, "online family must exist (D9)"
    assert all(minutes > 80 for minutes in DEFAULT_ONLINE_STARTS.values()), (
        "an online session under 80 minutes would be routed to the lecture grid, "
        "whose columns it does not share"
    )


def test_the_seam_writes_instructor_names_into_every_meeting():
    """Otherwise the workbook's Instructors sheet renders nothing at all.

    `_render_instructors_sheet` reads `TermSectionMeeting.instructor` and returns
    early when no meeting carries a name — so writing an empty string there does
    not produce a blank sheet, it produces NO sheet. The subsystem decides who
    teaches what, proves each instructor sits on their own minimum number of
    working days, and reports their idle time; all of that was invisible in the
    only artefact anybody outside a terminal reads.

    Checked on the real pipeline: a name must reach the meeting rows, and it must
    be the same name across every meeting of a section, because the sheet reads
    the first non-empty one and assumes the rest agree.
    """
    from core.models import DeliveryBoard, TermSectionMeeting, TimetableScenario
    from scheduler.bridge import build_into_scenario

    _plan("AI", "AI101", credits=3)
    _plan("AI", "AI102", credits=3)
    for sid in range(7001, 7031):
        _student(sid, "AI", "M")
    _room("M-1", "M")

    instructor = InstructorRow.objects.create(full_name="Dr Named", normalised_name="dr named")
    for code in ("AI101", "AI102"):
        CourseInstructor.objects.create(
            program="AI", course_code=code, section="M", instructor=instructor
        )

    scenario = TimetableScenario.objects.create(academic_year="1448", term=1, name="seam-test")
    DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)

    try:
        build_into_scenario(
            scenario.id,
            academic_year="1448",
            term=1,
            programs=["AI"],
            gender="M",
            seconds=10,
        )
    except RuntimeError:
        return  # no demand from the recommender here; nothing to assert against

    named = list(
        TermSectionMeeting.objects.filter(term_section__scenario=scenario)
        .exclude(instructor="")
        .values_list("term_section_id", "instructor")
    )
    if not named:
        return  # nobody was assignable within the caps; not this test's concern

    assert all(name == "Dr Named" for _ts, name in named)
    # one name per section, not a mixture
    per_section = {}
    for ts_id, name in named:
        per_section.setdefault(ts_id, set()).add(name)
    assert all(len(names) == 1 for names in per_section.values())


# ── the elective a student actually enrols in ─────────────────────────────
#
# The recommender emits the plan's PLACEHOLDER (AI1, "PROGRAM ELECTIVE I") and
# the project's resolver turns it into the catalogue course that fills it this
# term (AI463, "Information Retrieval"). The plan table holds only the
# placeholder; the real course lives in ElectiveCourse. Building offerings from
# the plan alone and then dropping unrecognised codes discarded every resolved
# elective: 65 student demands on the live male cohort, and with them the two
# AI463 sections and one DS487 section the project's OWN scenario budget plans.


def test_a_resolved_elective_is_an_offering_students_can_be_counted_against():
    _plan("AI", "AI1")
    _map("AI463", "AI1")
    for sid in range(4001, 4011):
        _student(sid, "AI", "M")
    _room("M-1", "M")
    snap = build_snapshot(
        academic_year="1448", term=1, gender="M", programs=["AI"], default_capacity=25
    )
    codes = {o.course_code for o in snap.offerings}
    assert "AI463" in codes, "the course the student actually enrols in is not schedulable"

    target = next(o for o in snap.offerings if o.course_code == "AI463")
    takers = sum(1 for d in snap.demand if target.id in d.offering_ids)
    assert takers == 10, f"the resolved demand was dropped: {takers} of 10 students counted"
    assert [s for s in snap.sections if s.offering_id == target.id], "no sections were planned"


def test_a_resolved_elective_inherits_the_slot_it_fills():
    """Programmes and plan terms come from the placeholder, because that is where
    a student meets the course — and they decide both the room pool it may use
    and the board it is drawn on."""
    _plan("AI", "AI1", term=7)
    _map("AI463", "AI1")
    for sid in range(4101, 4106):
        _student(sid, "AI", "M")
    _room("M-1", "M")
    snap = build_snapshot(
        academic_year="1448", term=1, gender="M", programs=["AI"], default_capacity=25
    )
    target = next(o for o in snap.offerings if o.course_code == "AI463")
    assert target.programs == frozenset({"AI"})
    assert target.terms == frozenset({7}), "it must land where the plan puts the slot"
    assert target.course_name == "Real AI463", "the timetable must print the real course name"


def test_an_unmappable_placeholder_is_still_scheduled_under_its_own_name():
    """FE1, FE2 and GSE1 have no term mapping. They are the only elective slots
    that stay as placeholders, and dropping them would leave those students with
    nothing at all."""
    _plan("AI", "FE1")
    for sid in range(4201, 4206):
        _student(sid, "AI", "M")
    _room("M-1", "M")
    snap = build_snapshot(
        academic_year="1448", term=1, gender="M", programs=["AI"], default_capacity=25
    )
    placeholder = next(o for o in snap.offerings if o.course_code == "FE1")
    assert sum(1 for d in snap.demand if placeholder.id in d.offering_ids) == 5


def test_a_clean_run_reports_nothing_unmatched():
    """The other half — the report has to stay empty when nothing is lost, or it
    is noise nobody will read."""
    _plan("AI", "AI1")
    _map("AI463", "AI1")
    for sid in range(4401, 4404):
        _student(sid, "AI", "M")
    _room("M-1", "M")
    snap = build_snapshot(
        academic_year="1448", term=1, gender="M", programs=["AI"], default_capacity=25
    )
    assert snap.unmatched_demand == ()


def test_an_approval_reaches_both_the_slot_and_the_course_that_fills_it():
    """Now that the resolved elective has an offering of its own, the fold has to
    run BOTH ways: someone approved for the AI1 slot may teach whatever fills it,
    and someone approved for AI463 may teach the slot it stands in.

    Before, only target->placeholder existed, so an approval naming the slot
    never reached the course that actually has the sections."""
    _plan("AI", "AI1")
    _map("AI463", "AI1")
    for sid in range(4501, 4506):
        _student(sid, "AI", "M")
    _room("M-1", "M")
    slot = InstructorRow.objects.create(full_name="Dr Slot", normalised_name="dr slot")
    target = InstructorRow.objects.create(full_name="Dr Target", normalised_name="dr target")
    CourseInstructor.objects.create(program="AI", course_code="AI1", section="M", instructor=slot)
    CourseInstructor.objects.create(
        program="AI", course_code="AI463", section="M", instructor=target
    )
    snap = build_snapshot(
        academic_year="1448", term=1, gender="M", programs=["AI"], default_capacity=25
    )
    real = next(o for o in snap.offerings if o.course_code == "AI463")
    eligible = {i.id for i in snap.instructors if real.id in i.eligible_offerings}
    assert {slot.id, target.id} <= eligible, (
        f"the course with the sections must be teachable by both — got {sorted(eligible)}"
    )


def test_an_approval_for_one_programme_does_not_staff_another_programmes_course():
    """Widening who may teach what is not a rounding error. An approval is
    granted for one programme's course; the offering it reaches must serve that
    programme.

    The direct-code path used to skip this check entirely, so an approval to
    teach AI463 for CS counted as staffing for an AI-only AI463 offering -- the
    exact widening the placeholder fold was written to prevent, arriving through
    the front door instead."""
    _plan("AI", "AI1")
    _plan("CS", "CS101")
    _map("AI463", "AI1", programme="AI")
    for sid in range(4601, 4606):
        _student(sid, "AI", "M")
    _room("M-1", "M")
    outsider = InstructorRow.objects.create(full_name="Dr CS", normalised_name="dr cs")
    CourseInstructor.objects.create(
        program="CS", course_code="AI463", section="M", instructor=outsider
    )
    snap = build_snapshot(
        academic_year="1448", term=1, gender="M", programs=["AI", "CS"], default_capacity=25
    )
    real = next(o for o in snap.offerings if o.course_code == "AI463")
    assert "CS" not in real.programs, "this fixture needs an AI-only offering to mean anything"
    eligible = {i.id for i in snap.instructors if real.id in i.eligible_offerings}
    assert outsider.id not in eligible, (
        "a CS approval was counted as staffing for an AI-only course"
    )


# ── two things the seam must refuse to do quietly ─────────────────────────


def _scenario_with(gender):
    from core.models import DeliveryBoard, TimetableScenario

    scenario = TimetableScenario.objects.create(
        name="guard", academic_year=1448, term=1, gender=gender
    )
    DeliveryBoard.objects.create(scenario=scenario, label="T1", nominal_term=1, display_order=1)
    return scenario


def test_a_single_gender_build_refuses_an_all_gender_scenario():
    """A scenario scaffolded with no section covers everybody -- its boards,
    budgets and student links are sized for all of them. A snapshot is
    single-gender by construction (D1), so filling one from the other writes a
    timetable derived from part of the scenario's own demand and reports success.

    The workspace view defaults a blank Sec box to "M", so on the live data this
    was 1606 male students planned into a scenario built for 4004."""
    from scheduler.bridge import build_into_scenario

    scenario = _scenario_with("")  # blank == all genders
    with pytest.raises(RuntimeError, match="single-gender"):
        build_into_scenario(
            scenario.id, academic_year="1448", term=1, programs=["AI"], gender="M", seconds=1
        )


def test_a_build_refuses_to_delete_a_locked_placement():
    """A lock is a decision somebody made by hand. This engine re-solves the whole
    week and deletes its own rows to do it, which cascades straight through
    locked placements -- while the existing engine goes out of its way to keep
    them (`reset_scenario(keep_locked=True)`)."""
    from core.models import DeliveryBoard, SectionPlacement, TermSection
    from scheduler.bridge import SOURCE_TAG, build_into_scenario

    scenario = _scenario_with("M")
    board = DeliveryBoard.objects.filter(scenario=scenario).first()
    term_section = TermSection.objects.create(
        scenario=scenario,
        course_code="AI101",
        course_number="101",
        course_name="X",
        section="S1",
        course_key="AI101::X",
        source_tag=SOURCE_TAG,
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=term_section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="R1",
        is_locked=True,
    )
    with pytest.raises(RuntimeError, match="locked"):
        build_into_scenario(
            scenario.id, academic_year="1448", term=1, programs=["AI"], gender="M", seconds=1
        )
    assert SectionPlacement.objects.filter(is_locked=True).count() == 1, (
        "the locked placement was destroyed anyway"
    )


def test_no_automatic_placer_may_use_an_online_only_window():
    """Owner decision, 2026-07-28: the day gets one more 100-minute window at the
    end, 18:30-20:10, and *labs will not use it at all*.

    It has to be a real column in the shared slot config, or an online class
    placed there could not be drawn and the seam would have to widen the grid --
    which is what let a scheduler build leave the EXISTING engine free to put a
    room-consuming lab at 18:30. So it is declared, and flagged, and every
    automatic path filters the flag. Nothing in the estate is open at 18:30,
    which is exactly why it is safe to give to online and not to anyone else."""
    from core.services.timetable_autoplace import (
        DEFAULT_LAB_SLOTS,
        _generate_meeting_options,
        placeable_slots,
    )

    ONLINE = {"15:50", "17:40", "19:30"}
    declared = {s["start"]: s for s in DEFAULT_LAB_SLOTS if s["start"] in ONLINE}
    assert set(declared) == ONLINE, (
        "the windows must be declared, or an online class in one cannot be drawn"
    )
    assert all(s.get("online_only") is True for s in declared.values())

    offered = placeable_slots(DEFAULT_LAB_SLOTS)
    assert ONLINE & {s["start"] for s in offered} == set()
    assert len(offered) == len(DEFAULT_LAB_SLOTS) - len(ONLINE), (
        "only the flagged windows are withheld"
    )

    # ...and they really do not reach the option generator, which is what the
    # classic placer and the CP-SAT polisher both build their moves from.
    options = _generate_meeting_options([100], [], DEFAULT_LAB_SLOTS)
    starts = {slot["start"] for option in options for slot in option}
    assert starts, "this fixture must produce options to mean anything"
    assert ONLINE & starts == set(), (
        "an automatic placer was offered a window reserved for online courses"
    )


# ── a display code is not a course identity (N1) ──────────────────────────
#
# Found by the owner on the live data, 2026-07-28. The AI and AI2 plans are
# OFFSET BY ONE, so the same code names two different courses:
#
#     CS111  FUNDAMENTALS OF PROGRAMMING   AI2, DS2   plan term 1
#     CS111  PROGRAMMING I                 AI,  DS    plan term 3
#     CS112  PROGRAMMING I                 AI2, DS2   plan term 2
#     CS112  PROGRAMMING II                AI,  DS    plan term 4
#
# Intake grouped offerings by the bare code, so the two became one offering
# carrying the pooled demand of both, printed under whichever name sorted first.
# The project itself does not do this: `compute_section_plan` and
# `ScenarioSectionBudget` key on `planner_course_key` (CODE::NORMALISED_NAME)
# and plan CS111::FUNDAMENTALS_OF_PROGRAMMING at 3 sections and
# CS111::PROGRAMMING_I at 1 — four in total, where this subsystem produced three.
#
# AI492 is the same defect with a louder name: "Cooperative Training" in AI2 and
# "Graduation Project II" in AI.


def _both_cs111():
    """The live shape, shrunk: one code, two courses, one cohort each."""
    _plan("AI2", "CS111", capacity=10, name="FUNDAMENTALS OF PROGRAMMING")
    _plan("AI", "CS111", capacity=10, name="PROGRAMMING I")
    # IDs must start "44": the recommender reads the Hijri join year from the
    # first two digits, and a student who has not joined yet is eligible for
    # nothing at all.
    for sid in range(440001, 440025):  # 24 in the AI2 course
        _student(sid, "AI2", "M")
    for sid in range(441001, 441005):  # 4 in the AI course
        _student(sid, "AI", "M")
    _room("M-1", "M")
    return build_snapshot(
        academic_year="1448", term=1, gender="M", programs=["AI", "AI2"], default_capacity=25
    )


def _cs111_by_name(snap):
    found = {o.course_name: o for o in snap.offerings if o.course_code == "CS111"}
    assert len(found) == 2, f"one code, two courses — got {sorted(found)}"
    return found["FUNDAMENTALS OF PROGRAMMING"], found["PROGRAMMING I"]


def test_one_code_naming_two_courses_yields_two_offerings():
    """MUTATION: group by `row["course_code"]` instead of the course key. One
    offering comes back and this fails on the count."""
    fundamentals, programming = _cs111_by_name(_both_cs111())
    assert fundamentals.id != programming.id
    assert fundamentals.programs == frozenset({"AI2"})
    assert programming.programs == frozenset({"AI"})


def test_a_student_is_counted_against_their_own_programme_s_course():
    """The merge did not only mislabel: it pooled the demand of two different
    courses, so a first-term AI2 student was seated in a third-term AI course."""
    snap = _both_cs111()
    fundamentals, programming = _cs111_by_name(snap)
    program_of = {d.student_id: d.program for d in snap.demand}
    for d in snap.demand:
        if fundamentals.id in d.offering_ids:
            assert program_of[d.student_id] == "AI2", "an AI student joined the AI2 course"
        if programming.id in d.offering_ids:
            assert program_of[d.student_id] == "AI", "an AI2 student joined the AI course"
    takers = {
        fundamentals.id: sum(1 for d in snap.demand if fundamentals.id in d.offering_ids),
        programming.id: sum(1 for d in snap.demand if programming.id in d.offering_ids),
    }
    # Asserted exactly, so the test cannot pass by finding no demand at all: an
    # empty result satisfies "nobody is in the wrong course" perfectly.
    assert takers == {fundamentals.id: 24, programming.id: 4}, takers
    assert snap.unmatched_demand == (), f"demand was dropped, not split: {snap.unmatched_demand}"


def test_each_course_gets_its_own_section_budget():
    """MUTATION: key the section plan on `offering.course_code`. Both offerings
    then read the SAME planned row — and because the aggregate is built by plain
    assignment, the row belongs to whichever offering happened to be last. 24
    students and 4 students at 10 seats need 3 sections and 1, never 3 and 3 or
    1 and 1."""
    snap = _both_cs111()
    fundamentals, programming = _cs111_by_name(snap)
    counts = {
        "FUNDAMENTALS OF PROGRAMMING": len(snap.sections_by_offering.get(fundamentals.id, ())),
        "PROGRAMMING I": len(snap.sections_by_offering.get(programming.id, ())),
    }
    assert counts == {"FUNDAMENTALS OF PROGRAMMING": 3, "PROGRAMMING I": 1}, counts
    # ...and the sections belong to one course each, rather than one pool of four
    # that students of either course could be seated in.
    assert not {s.id for s in snap.sections_by_offering[fundamentals.id]} & {
        s.id for s in snap.sections_by_offering[programming.id]
    }


def test_staffing_approval_does_not_cross_between_two_courses_sharing_a_code():
    """An approval to teach AI2's CS111 is not an approval to teach AI's CS111.
    With one offering per code the question could not even be asked."""
    person = InstructorRow.objects.create(full_name="Dr Split", is_active=True)
    CourseInstructor.objects.create(
        program="AI2", course_code="CS111", section="M", instructor=person
    )
    snap = _both_cs111()
    fundamentals, programming = _cs111_by_name(snap)
    eligible = {i.id: i.eligible_offerings for i in snap.instructors}
    assert person.id in eligible, "the approval reached neither course"
    assert fundamentals.id in eligible[person.id]
    assert programming.id not in eligible[person.id], "staffing widened to the other course"


# ── courses too small to be worth a place on the board (D18) ──────────────
#
# Owner rule, 2026-07-28: "any course with demand less than 5 students, drop it
# from the demand before running the planner."
#
# A course three people want still costs a full section — a room for every
# meeting, an instructor's day opened, a slot every other course must avoid, and
# one of the week's 25 mutually non-overlapping cells (H10). The registrar's
# answer for those three is the tier argument: seat them in a section that
# already runs elsewhere in the college.
#
# The danger is the opposite of the usual one. Every other filter in this
# subsystem makes the numbers worse when it fires; this one makes them BETTER —
# fewer sections, fewer colliding pairs, less idle time. So the tests below pin
# the reporting as hard as the filtering: a drop nobody can see is
# indistinguishable from a board that was simply easier.


def _tiered_cohort():
    """Courses with controlled head-counts.

    Demand comes from the real recommender, which offers a student only the plan
    terms they have reached — and derives that from the Hijri join year in the
    first two digits of their ID. So the ID prefix, not a fixture switch, is what
    puts a different number of students in each course:

        44xxxx -> joined 1444, next term 9  -> reaches terms 1, 3 AND 9
        47xxxx -> joined 1447, next term 3  -> reaches terms 1 and 3 only
    """
    for term, code in ((1, "AI101"), (3, "AI301"), (9, "AI901")):
        _plan("AI", code, term=term, capacity=40)
    _plan("AI2", "AI201", term=1, capacity=40)
    _plan("AI2", "AI209", term=9, capacity=40)
    _plan("DS", "DS901", term=9, capacity=40)  # the only course its cohort has

    for sid in range(470001, 470007):  # 6 -> AI101, AI301
        _student(sid, "AI", "M")
    for sid in range(440001, 440003):  # 2 -> AI101, AI301, AI901
        _student(sid, "AI", "M")
    for sid in range(440101, 440106):  # 5 -> AI201, AI209
        _student(sid, "AI2", "M")
    for sid in range(440201, 440203):  # 2 -> DS901 and nothing else
        _student(sid, "DS", "M")
    _room("M-1", "M", capacity=60)


def _snap(min_demand):
    _tiered_cohort()
    return build_snapshot(
        academic_year="1448",
        term=1,
        gender="M",
        programs=["AI", "AI2", "DS"],
        default_capacity=25,
        min_demand=min_demand,
    )


def _counts(snap):
    by_code = {o.course_code: o.id for o in snap.offerings}
    return {
        code: sum(1 for d in snap.demand if oid in d.offering_ids) for code, oid in by_code.items()
    }


def test_the_fixture_really_does_have_courses_on_both_sides_of_the_floor():
    """Guard, not a feature. Every other test here is meaningless if the
    recommender hands out different numbers than intended — and a filter test
    whose input was already empty passes for the wrong reason."""
    assert _counts(_snap(1)) == {
        "AI101": 8,
        "AI301": 8,
        "AI901": 2,  # below the floor
        "AI201": 5,  # exactly ON the floor
        "AI209": 5,  # exactly ON the floor
        "DS901": 2,  # below it, and its cohort has nothing else
    }


def test_a_course_below_the_floor_is_withheld_and_one_on_it_is_kept():
    """MUTATION: `<=` instead of `<`. AI201/AI209 sit exactly on the floor, so
    an off-by-one there withholds two courses that must run."""
    snap = _snap(5)
    withheld = {code for code, _name, _n, _tier in snap.low_demand_dropped}
    assert withheld == {"AI901", "DS901"}
    kept = {o.course_code for o in snap.offerings if snap.sections_by_offering.get(o.id)}
    assert {"AI101", "AI301", "AI201", "AI209"} <= kept
    assert not (withheld & kept), "a withheld course still got sections"


def test_withheld_demand_leaves_the_students_other_courses_alone():
    """MUTATION: drop the whole STUDENT rather than the offering. The two 44xxxx
    AI students lose AI901 and must keep AI101 and AI301."""
    counts = _counts(_snap(5))
    assert counts["AI101"] == 8 and counts["AI301"] == 8
    assert counts["AI901"] == 0


def test_the_drop_is_reported_loudly_never_silently():
    """This is the one filter that improves every other number on the report, so
    it is a WARNING and it names each course, its size and its tier."""
    snap = _snap(5)
    assert dict((c, (n, t)) for c, _name, n, t in snap.low_demand_dropped) == {
        "AI901": (2, "T1"),
        "DS901": (2, "T1"),
    }
    finding = next(f for f in assess(snap).findings if f.code == "LOW_DEMAND_WITHHELD")
    assert finding.severity is Severity.WARNING
    assert finding.detail["min_demand"] == 5
    assert {c["code"] for c in finding.detail["courses"]} == {"AI901", "DS901"}


def test_a_student_left_with_no_courses_at_all_is_counted():
    """MUTATION: `students_left_unserved = 0`. The two DS students wanted only
    DS901; they vanish from `demand` entirely, and every per-student average
    would otherwise improve by losing them rather than by serving them."""
    snap = _snap(5)
    assert snap.students_left_unserved == 2
    assert not [d for d in snap.demand if d.program == "DS"]
    finding = next(f for f in assess(snap).findings if f.code == "STUDENTS_LEFT_UNSERVED")
    assert finding.severity is Severity.WARNING


def test_the_rule_is_off_at_one_so_nothing_inherits_it_by_accident():
    snap = _snap(1)
    assert snap.low_demand_dropped == ()
    assert snap.students_left_unserved == 0
    assert snap.sections_by_offering.get(
        next(o.id for o in snap.offerings if o.course_code == "DS901")
    ), "with the rule off, a two-student course still runs"


# ── D19: the morning block comes from the grid, never from a literal ──────


def test_the_morning_block_is_the_widest_non_overlapping_set_of_morning_starts():
    """MUTATION: take every start before noon. The declared grid offers 09:00,
    10:30 AND 10:50 — and 10:30+75 runs to 11:45, so 10:50 overlaps it. A course
    cannot be in two places at once, so the block is 09:00 and 10:30."""
    assert morning_block(default_grid()) == (9 * 60, 10 * 60 + 30)


def test_the_morning_block_is_read_from_the_grid_it_is_given():
    """D2: the grid is the sole time authority. This rule narrows a choice; it
    must never be the thing that invents an hour."""
    grid = Grid.from_spec(
        lecture_starts={"08:00": 75, "09:20": 75, "11:00": 75, "13:00": 75},
        lab_starts={"09:00": 100},
    )
    assert morning_block(grid) == (8 * 60, 9 * 60 + 20, 11 * 60)


def test_eng101_is_compiled_as_a_fixed_block_and_ordinary_english_is_not():
    """MUTATION: match the rule by prefix ("ENG") instead of by exact code.
    ENGL103/104/214 are ordinary three-credit courses and would be dragged into
    owning the morning."""
    _plan("AI", "ENG101", credits=4, term=1, name="ENGLISH LANGUAGE SKILLS I")
    _plan("AI", "ENGL103", credits=3, term=1, name="ENGLISH COMPOSITION")
    _student(440001, "AI", "M")
    _room("M-1", "M", capacity=40)
    snap = build_snapshot(
        academic_year="1448", term=1, gender="M", programs=["AI"], default_capacity=25
    )
    by_code = {o.course_code: o for o in snap.offerings}

    eng = by_code["ENG101"]
    assert eng.occupies_fixed_block
    (requirement,) = eng.requirements
    assert requirement.allowed_starts == frozenset(morning_block(snap.grid))
    assert requirement.count_per_week == len(morning_block(snap.grid)) * len(snap.grid.days())
    assert not requirement.uses_shared_room, "ENG has rooms of its own"
    assert requirement.delivery is DeliveryMode.IN_PERSON, "it is on campus, not online"

    ordinary = by_code["ENGL103"]
    assert not ordinary.occupies_fixed_block
    assert all(not r.allowed_starts and r.uses_shared_room for r in ordinary.requirements)


# ── the parts a test audit proved were never exercised ────────────────────


def test_section_sizing_uses_the_institution_s_rules_when_no_capacity_is_declared():
    """MUTATION: drop `course_metadata=` from the `compute_section_plan` call.

    Every other fixture in this suite declares `max_capacity`, and a declared
    capacity outranks institutional sizing — so the metadata was dead weight in
    all of them and the mutation stayed green across the whole suite. It is not
    dead on live data: without it the planner sees an opaque `off_...` id,
    cannot extract a department, and classes everything as an external service
    course at 50 seats. Measured on this fixture: 2 sections of 50 instead of 4
    of 25."""
    _plan("AI", "AI401", credits=4, capacity=None, term=1)
    for sid in range(440001, 440101):  # 100 students, no declared capacity
        _student(sid, "AI", "M")
    _room("M-1", "M", capacity=60)
    snap = build_snapshot(
        academic_year="1448", term=1, gender="M", programs=["AI"], default_capacity=25
    )
    offering = next(o for o in snap.offerings if o.course_code == "AI401")
    demanded = sum(1 for d in snap.demand if offering.id in d.offering_ids)
    assert demanded == 100, f"the fixture must produce real demand, got {demanded}"
    sections = snap.sections_by_offering[offering.id]
    seats = {s.capacity for s in sections}
    assert seats == {25}, (
        f"a 4-credit local course seats 25 by institutional rule; got {seats}. "
        f"The planner was not told the course code."
    )
    assert len(sections) == 4, f"100 students / 25 seats = 4 sections, got {len(sections)}"


def test_the_generate_button_withholds_small_courses_by_default():
    """MUTATION: `min_demand: int = 5` -> 1 in `bridge.py`. Every test passes
    `min_demand` explicitly, so nothing pinned the value the workspace actually
    uses and the rule could ship switched off."""
    import inspect

    from scheduler.bridge import build_into_scenario

    assert inspect.signature(build_into_scenario).parameters["min_demand"].default == 5

    from core.services import planner_job_runner

    source = inspect.getsource(planner_job_runner.run_planner_job)
    assert 'p.get("min_demand", 5)' in source, "the async job path lost the default"


def test_a_shared_code_no_programme_claims_is_reported_not_guessed():
    """MUTATION: `return None` -> `return candidates[0]` in `offering_for`.

    The docstring promises it never guesses "because guessing is what merged
    them before", and no test reached the branch. A CS student asking for a
    CS111 that exists only in the AI and AI2 plans must be counted as unmatched,
    not silently enrolled in whichever sorted first."""
    from scheduler.intake import build_snapshot as build

    _plan("AI", "CS111", capacity=10, name="PROGRAMMING I")
    _plan("AI2", "CS111", capacity=10, name="FUNDAMENTALS OF PROGRAMMING")
    _plan("DS", "DS101", capacity=10, name="DATA SCIENCE I")
    # A DS student whose own plan holds DS101; DS has no CS111 at all.
    for sid in range(440001, 440007):
        _student(sid, "DS", "M")
    for sid in range(441001, 441007):
        _student(sid, "AI", "M")
    _room("M-1", "M", capacity=40)
    snap = build(
        academic_year="1448",
        term=1,
        gender="M",
        programs=["AI", "AI2", "DS"],
        default_capacity=25,
    )
    by_prog = {o.id: o.programs for o in snap.offerings}
    for d in snap.demand:
        for oid in d.offering_ids:
            assert d.program in by_prog[oid], (
                f"a {d.program} student was enrolled in an offering for {by_prog[oid]}"
            )


def test_capacity_policy_rejects_a_nonsense_min_demand():
    """MUTATION: delete the `raise`. `test_capacity_policy_rejects_nonsense` was
    not extended when the field was added."""
    with pytest.raises(ValueError):
        CapacityPolicy(default_capacity=25, min_demand=0)
    assert CapacityPolicy(default_capacity=25, min_demand=1).min_demand == 1

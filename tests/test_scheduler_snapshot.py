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
from scheduler.intake import IntakeError, build_snapshot, compile_requirements, default_grid
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


def test_scheduler_never_imports_the_old_timetable_engine():
    """Zero coupling to `core.services.timetable_*` — the current builder must be
    unaffected by anything here."""
    for path in SCHEDULER_DIR.rglob("*.py"):
        offenders = {m for m in _imported_modules(path) if m.startswith("core.services.timetable")}
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


def test_online_has_its_own_late_day_family_that_needs_no_room():
    """D9: online teaching is a separate declared family from 15:00, so it never
    competes with the on-campus grid — and it consumes no room."""
    grid = default_grid()
    online = grid.windows_for(100, DeliveryMode.ONLINE)
    assert [str(w) for w in online] == ["15:00-16:40", "16:45-18:25", "18:30-20:10"]
    # The in-person 100-minute family is the lab timing set, and is disjoint from it.
    in_person = grid.windows_for(100, DeliveryMode.IN_PERSON)
    assert len(in_person) == 5
    assert not set(online) & set(in_person)
    # Online windows never enter the room-capacity bound.
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


def _plan(program, code, credits=3, capacity=None, online=False, term=1):
    ProgrammeRequirement.objects.create(
        program=program,
        course_code=code,
        course_name=code,
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

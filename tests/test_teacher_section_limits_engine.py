"""Per-teacher section limits per course: the engine side.

``CourseInstructor.max_sections`` is the most sections of one course a teacher
may hold in one timetable. A number REPLACES D10 for that teacher on that
course (any section count, possibly above 2); the meeting-policy cap stays a
minimum on top; a teacher without a number keeps the default rule; a pin is
never dropped. With no number stored the results are what they were before.
"""

from __future__ import annotations

from collections import Counter

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from core.models import CourseInstructor, SectionInstructor
from core.services import section_teacher_assignment as sta
from core.services.timetable_pr4_instructor import section_limit_resolver, section_resolvers
from tests.one_teacher_support import (
    FLAG_ON,
    budget,
    course,
    instructor,
    requirement,
    scenario,
)


@pytest.fixture
def flags(settings):
    for name, value in FLAG_ON.items():
        setattr(settings, name, value)
    return settings


def numbered(program: str, code: str, person, limit: int | None, role: str = "primary"):
    return CourseInstructor.objects.create(
        program=program,
        course_code=code,
        section="M",
        instructor=person,
        role=role,
        max_sections=limit,
    )


def held(assignment, key: str, person) -> int:
    return sum(
        1
        for k, teacher in assignment.teacher.items()
        if k.startswith(f"{key}_") and teacher == person.id
    )


def tbd_of(assignment, key: str) -> list[str]:
    return sorted(
        k
        for k, status in assignment.status.items()
        if k.startswith(f"{key}_") and status == sta.STATUS_TBD
    )


def owner_case(flags, sections: int = 8):
    """One course running ``sections`` sections: A may hold 2, C may hold 3."""
    key = requirement("AI", "CS112", "PROGRAMMING I")
    sc, _board = scenario()
    a, c = instructor("Dr A"), instructor("Dr C")
    numbered("AI", "CS112", a, 2)
    numbered("AI", "CS112", c, 3, role="co")
    budget(sc, key, sections, code="CS112")
    return sc, key, a, c


# ── The owner's case ──────────────────────────────────────────────────────


@pytest.mark.django_db
def test_owner_case_a_two_c_three_the_rest_are_tbd_with_d10(flags) -> None:
    sc, key, a, c = owner_case(flags)

    got = sta.load_assignment(sc.id)

    assert held(got, key, a) == 2
    assert held(got, key, c) == 3
    left = tbd_of(got, key)
    assert len(left) == 3
    assert {got.tbd_reason[k] for k in left} == {"d10"}
    coverage = sta.coverage_of(got)
    assert coverage["tbd_d10_floor"] == 3
    assert coverage["tbd_beyond_d10"] == 0


@pytest.mark.django_db
def test_a_limit_above_two_is_honoured_where_d10_alone_would_give_two(flags) -> None:
    key = requirement("AI", "CS112", "PROGRAMMING I")
    sc, _board = scenario()
    only = instructor("Dr Only")
    numbered("AI", "CS112", only, 4)
    budget(sc, key, 6, code="CS112")

    got = sta.load_assignment(sc.id)

    assert held(got, key, only) == 4
    assert {got.tbd_reason[k] for k in tbd_of(got, key)} == {"d10"}
    assert len(tbd_of(got, key)) == 2


@pytest.mark.django_db
def test_a_limit_of_one_binds_on_a_course_below_the_d10_threshold(flags) -> None:
    key = requirement("AI", "CS112", "PROGRAMMING I")
    sc, _board = scenario()
    only = instructor("Dr Only")
    numbered("AI", "CS112", only, 1)
    budget(sc, key, 3, code="CS112")

    got = sta.load_assignment(sc.id)

    assert held(got, key, only) == 1
    assert {got.tbd_reason[k] for k in tbd_of(got, key)} == {"d10"}
    assert len(tbd_of(got, key)) == 2
    # the layout-free lower bound sees it too (a true lower bound: 2 are forced)
    assert sta.coverage_of(got)["tbd_d10_floor"] == 2
    assert sta.coverage_of(got)["tbd_beyond_d10"] == 0


@pytest.mark.django_db
def test_a_teacher_without_a_number_still_follows_d10(flags) -> None:
    key = requirement("AI", "CS112", "PROGRAMMING I")
    sc, _board = scenario()
    limited, plain = instructor("Dr Limited"), instructor("Dr Plain")
    numbered("AI", "CS112", limited, 3)
    numbered("AI", "CS112", plain, None, role="co")
    budget(sc, key, 6, code="CS112")

    got = sta.load_assignment(sc.id)

    assert held(got, key, limited) == 3
    assert held(got, key, plain) == 2  # D10: one of six sections' worth is two
    assert len(tbd_of(got, key)) == 1
    # the plain teacher has no explicit cap, so a mixed course keeps D10 for him
    slot = got.slots[0]
    assert slot.cap_for(plain.id) == sta.D10_MAX_SECTIONS
    assert slot.cap_for(limited.id) == 3


# ── Several rows of one teacher ───────────────────────────────────────────


def _two_programme_course(flags):
    key = requirement("AI", "CS112", "PROGRAMMING I")
    requirement("AI2", "CS112", "PROGRAMMING I")
    sc, _board = scenario(programs=("AI", "AI2"))
    budget(sc, key, 8, code="CS112")
    return sc, key


@pytest.mark.django_db
def test_two_rows_of_one_teacher_use_the_smaller_number(flags) -> None:
    sc, key = _two_programme_course(flags)
    person = instructor("Dr Two Rows")
    numbered("AI", "CS112", person, 4)
    numbered("AI2", "CS112", person, 2)

    resolve, limit = section_resolvers(["AI", "AI2"], "M")
    assert limit(key, "CS112") == {person.id: 2}
    assert resolve(key, "CS112") == {person.id}
    got = sta.load_assignment(sc.id)
    assert held(got, key, person) == 2


@pytest.mark.django_db
def test_a_blank_row_next_to_a_numbered_row_gives_the_number(flags) -> None:
    sc, key = _two_programme_course(flags)
    person = instructor("Dr Mixed Rows")
    numbered("AI", "CS112", person, None)
    numbered("AI2", "CS112", person, 3)

    assert section_limit_resolver(["AI", "AI2"], "M")(key, "CS112") == {person.id: 3}
    got = sta.load_assignment(sc.id)
    assert held(got, key, person) == 3


@pytest.mark.django_db
def test_two_rows_of_one_teacher_that_differ_only_in_code_case_use_the_smaller(flags) -> None:
    key = requirement("AI", "CS112", "PROGRAMMING I")
    person = instructor("Dr Cases")
    numbered("AI", "CS112", person, 4)
    numbered("AI", "cs112", person, 2, role="co")

    assert section_limit_resolver(["AI"], "M")(key, "CS112") == {person.id: 2}


@pytest.mark.django_db
def test_the_limit_resolver_skips_inactive_teachers_and_the_other_gender() -> None:
    key = requirement("AI", "CS112", "PROGRAMMING I")
    gone = instructor("Dr Gone", active=False)
    here = instructor("Dr Here")
    numbered("AI", "CS112", gone, 1)
    numbered("AI", "CS112", here, 2, role="co")
    CourseInstructor.objects.create(
        program="AI", course_code="CS112", section="F", instructor=here, max_sections=1
    )

    assert section_limit_resolver(["AI"], "M")(key, "CS112") == {here.id: 2}
    assert section_limit_resolver(["AI"], "")(key, "CS112") == {}


# ── Pins and stored rows ──────────────────────────────────────────────────


@pytest.mark.django_db
def test_a_department_pin_above_the_limit_is_kept_and_nothing_more_is_added(flags) -> None:
    key = requirement("AI", "CS112", "PROGRAMMING I")
    sc, _board = scenario()
    a, b = instructor("Dr A"), instructor("Dr B")
    numbered("AI", "CS112", a, 2)
    numbered("AI", "CS112", b, 2, role="co")
    budget(sc, key, 6, code="CS112")
    for label in ("S1", "S2", "S3"):
        SectionInstructor.objects.create(
            scenario=sc, course_key=key, section=label, instructor=a, source="department"
        )

    got = sta.load_assignment(sc.id)

    assert [got.teacher[f"{key}_S{n}"] for n in (1, 2, 3)] == [a.id] * 3
    assert held(got, key, a) == 3  # his pins stay, the engine adds none
    assert held(got, key, b) == 2
    assert len(tbd_of(got, key)) == 1
    assert [list(row) for row in sta.coverage_of(got)["d10_violated_by_pins"]] == [[key, a.id]]


@pytest.mark.django_db
def test_pins_within_the_explicit_limit_are_not_reported_over_d10(flags) -> None:
    key = requirement("AI", "CS112", "PROGRAMMING I")
    sc, _board = scenario()
    person = instructor("Dr A")
    numbered("AI", "CS112", person, 3)
    budget(sc, key, 6, code="CS112")
    for label in ("S1", "S2", "S3"):
        SectionInstructor.objects.create(
            scenario=sc, course_key=key, section=label, instructor=person, source="department"
        )

    got = sta.load_assignment(sc.id)

    assert held(got, key, person) == 3
    assert sta.coverage_of(got)["d10_violated_by_pins"] == []  # 3 pins > D10's 2, but not > 3


@pytest.mark.django_db
def test_the_meeting_policy_cap_stays_a_minimum_over_an_explicit_limit(flags) -> None:
    key = requirement("AI", "ENG101", "ENGLISH I")
    sc, _board = scenario()
    person = instructor("Dr English")
    numbered("AI", "ENG101", person, 5)
    budget(sc, key, 6, code="ENG101")

    got = sta.load_assignment(sc.id)

    # The English policy has two disjoint weekly bundles: a third section would
    # make him overlap himself, so the number 5 cannot lift the cap above 2.
    assert held(got, key, person) == 2
    assert got.slots[0].cap_for(person.id) == 2


@pytest.mark.django_db
def test_a_persisted_map_reads_back_exactly_tbd_included(flags) -> None:
    sc, key, _a, _c = owner_case(flags)
    proposed = sta.propose_layout_assignment(sc.id)
    sta.persist_assignment(sc.id, proposed)

    back = sta.load_assignment(sc.id)

    assert back.teacher == proposed.teacher
    assert back.status == proposed.status
    assert back.tbd_reason == proposed.tbd_reason
    assert len(tbd_of(back, key)) == 3
    assert back.report["closure_keys"] == []
    assert back.report["stale_rows_ignored"] == 0


@pytest.mark.django_db
def test_lowering_a_limit_drops_the_stored_rows_over_it_lowest_rank_kept(flags) -> None:
    key = requirement("AI", "CS112", "PROGRAMMING I")
    sc, _board = scenario()
    person = instructor("Dr Only")
    row = numbered("AI", "CS112", person, 3)
    budget(sc, key, 5, code="CS112")
    sta.persist_assignment(sc.id, sta.propose_layout_assignment(sc.id))
    assert held(sta.load_assignment(sc.id), key, person) == 3

    CourseInstructor.objects.filter(pk=row.pk).update(max_sections=1)
    sta.invalidate_scope(sc.id)
    got = sta.load_assignment(sc.id)

    assert held(got, key, person) == 1
    assert got.teacher[f"{key}_S1"] == person.id
    assert got.report["ignored_row_keys"] == [[key.upper(), "S2"], [key.upper(), "S3"]]


@pytest.mark.django_db
def test_the_bridge_rejects_a_row_over_the_teachers_limit_with_d10(flags) -> None:
    key = requirement("AI", "CS112", "PROGRAMMING I")
    sc, _board = scenario()
    person = instructor("Dr Only")
    numbered("AI", "CS112", person, 2)
    budget(sc, key, 3, code="CS112")  # 3 sections: D10 alone would not bind

    result = sta.persist_bridge_assignment(sc.id, {(key, f"S{n}"): person.id for n in (1, 2, 3)})

    assert result["written"] == 2
    assert [r["reason"] for r in result["bridge_rejected"]] == ["d10"]


# ── No number anywhere: nothing changes ───────────────────────────────────


@pytest.mark.django_db
def test_no_numbers_leave_slots_and_queries_as_they_are(flags) -> None:
    key = requirement("AI", "CS112", "PROGRAMMING I")
    sc, _board = scenario()
    a, c = instructor("Dr A"), instructor("Dr C")
    numbered("AI", "CS112", a, None)
    numbered("AI", "CS112", c, None, role="co")
    budget(sc, key, 8, code="CS112")

    with CaptureQueriesContext(connection) as plain:
        got = sta.load_assignment(sc.id)

    assert all(slot.teacher_caps == () for slot in got.slots)
    assert {slot.cap_for(t) for slot in got.slots for t in (a.id, c.id)} == {sta.D10_MAX_SECTIONS}
    assert held(got, key, a) == 2 and held(got, key, c) == 2
    assert section_limit_resolver(["AI"], "M")(key, "CS112") == {}

    # the same shape with numbers runs the very same number of queries, and the
    # course links are read once either way
    CourseInstructor.objects.filter(instructor=a).update(max_sections=2)
    sta.invalidate_scope(sc.id)
    with CaptureQueriesContext(connection) as with_numbers:
        sta.load_assignment(sc.id)
    assert len(with_numbers) == len(plain)
    for captured in (plain, with_numbers):
        assert sum('FROM "course_instructors"' in q["sql"] for q in captured.captured_queries) == 1


def test_the_default_cap_is_what_a_slot_without_numbers_always_gave() -> None:
    four = course("AI201::ML", [{} for _ in range(5)], eligible=(1, 2))
    three = course("AI202::DL", [{} for _ in range(3)], eligible=(1, 2))

    assert {s.teacher_caps for s in [*four, *three]} == {()}
    assert {s.cap_for(1) for s in four} == {sta.D10_MAX_SECTIONS}
    assert {s.cap_for(1) for s in three} == {None}
    assert sta.tbd_d10_floor(four) == max(0, 5 - 2 * 2)
    assert sta.tbd_d10_floor(three) == 0


# ── The lower bound stays a lower bound ───────────────────────────────────


def _with_caps(slots, caps):
    import dataclasses

    return [dataclasses.replace(s, teacher_caps=tuple(sorted(caps.items()))) for s in slots]


def test_the_floor_with_mixed_explicit_and_default_limits() -> None:
    slots = course("AI201::ML", [{} for _ in range(8)], eligible=(1, 2, 3))
    # teacher 1 explicit 3, teachers 2 and 3 default (D10: 2 each) -> capacity 7
    assert sta.tbd_d10_floor(_with_caps(slots, {1: 3})) == 1


def test_the_floor_is_zero_when_a_teacher_has_no_cap_at_all() -> None:
    slots = course("AI202::DL", [{} for _ in range(3)], eligible=(1, 2))
    # 3 sections: teacher 2 is uncapped (D10 does not bind) -> nothing is forced
    assert sta.tbd_d10_floor(_with_caps(slots, {1: 1})) == 0
    # ... but with only the capped teacher eligible, 2 are forced
    only = course("AI203::OS", [{} for _ in range(3)], eligible=(1,))
    assert sta.tbd_d10_floor(_with_caps(only, {1: 1})) == 2


def test_the_floor_counts_a_pin_against_its_teachers_limit() -> None:
    rows = [{"fixed": 1}, {"fixed": 1}, {}, {}, {}]
    slots = course("AI204::DB", rows, eligible=(1, 2), cap=None)
    # teacher 1: limit 2 with 2 pins -> 0 left; teacher 2: limit 1 -> capacity 1;
    # 3 free sections -> 2 forced
    assert sta.tbd_d10_floor(_with_caps(slots, {1: 2, 2: 1})) == 2


# ── scheduler/: the same limit and combination rule (D7) ──────────────────


def _snapshot_with_sections(count: int, instructors):
    from scheduler.domain import DeliveryMode, MeetingKind, MeetingRequirement, Section
    from tests.test_scheduler_rules import _offering, _snapshot_with

    offering = _offering(
        "off1", reqs=(MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, 1),)
    )
    sections = [Section(f"off1#S{i}", "off1", i, 30) for i in range(1, count + 1)]
    return _snapshot_with([offering], sections, instructors)


def _scheduler_held(out) -> Counter:
    return Counter(s.instructor_id for s in out.sections if s.instructor_id is not None)


def test_scheduler_owner_case_a_two_c_three_the_rest_unassigned() -> None:
    from scheduler.domain import Instructor
    from scheduler.instructors import assign_instructors

    snap = _snapshot_with_sections(
        8,
        [
            Instructor(1, "A", frozenset({"off1"}), (("off1", 2),)),
            Instructor(3, "C", frozenset({"off1"}), (("off1", 3),)),
        ],
    )

    out = assign_instructors(snap)

    assert _scheduler_held(out) == Counter({1: 2, 3: 3})
    assert sum(1 for s in out.sections if s.instructor_id is None) == 3


def test_scheduler_limit_above_two_and_below_the_threshold() -> None:
    from scheduler.domain import Instructor
    from scheduler.instructors import assign_instructors

    above = assign_instructors(
        _snapshot_with_sections(6, [Instructor(1, "A", frozenset({"off1"}), (("off1", 4),))])
    )
    assert _scheduler_held(above) == Counter({1: 4})

    below = assign_instructors(
        _snapshot_with_sections(3, [Instructor(1, "A", frozenset({"off1"}), (("off1", 1),))])
    )
    assert _scheduler_held(below) == Counter({1: 1})


def test_scheduler_a_teacher_without_a_number_still_follows_d10() -> None:
    from scheduler.domain import Instructor
    from scheduler.instructors import MAX_SECTIONS_PER_COURSE, assign_instructors

    out = assign_instructors(
        _snapshot_with_sections(
            6,
            [
                Instructor(1, "A", frozenset({"off1"}), (("off1", 3),)),
                Instructor(2, "B", frozenset({"off1"})),
            ],
        )
    )

    assert _scheduler_held(out) == Counter({1: 3, 2: MAX_SECTIONS_PER_COURSE})


def test_scheduler_an_instructor_built_before_limits_existed_is_unchanged() -> None:
    from scheduler.domain import Instructor

    assert Instructor(1, "A", frozenset({"off1"})).section_limits == ()


@pytest.mark.django_db
def test_scheduler_intake_carries_the_smallest_number_of_the_rows_reaching_an_offering() -> None:
    from core.models import Instructor as InstructorRow
    from scheduler.intake import build_snapshot
    from tests.test_scheduler_snapshot import _map, _plan, _room, _student

    _plan("AI", "AI1")
    for sid in range(3001, 3011):
        _student(sid, "AI", "M")
    _room("M-1", "M")
    _map("AI463", "AI1")
    both = InstructorRow.objects.create(full_name="Dr Both", normalised_name="dr both")
    blank = InstructorRow.objects.create(full_name="Dr Blank", normalised_name="dr blank")
    # Dr Both: the slot row says 4, the elective's row says 2: the offering AI1 gets 2.
    numbered("AI", "AI1", both, 4)
    numbered("AI", "AI463", both, 2, role="co")
    numbered("AI", "AI1", blank, None, role="co")

    snap = build_snapshot(
        academic_year="1448", term=1, gender="M", programs=["AI"], default_capacity=25
    )

    offering = next(o for o in snap.offerings if o.course_code == "AI1")
    by_id = {i.id: i for i in snap.instructors}
    assert dict(by_id[both.id].section_limits)[offering.id] == 2
    assert by_id[blank.id].section_limits == ()

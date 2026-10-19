"""One teacher per section: the assignment itself (U1-U25 of the Phase 1 spec).

A ``CourseInstructor`` link means "may teach any section of the course". The
engine charges each section to exactly one eligible teacher, or to nobody
("teacher to be decided"); clash and the daily cap are hard, the weekly ceiling
and D10 too; the day-off target, days, idle, stability and the lowest id are
the staged objective. Pure tests build ``Slot``s directly; the database tests
go through ``collect_slots`` / ``load_assignment`` / ``persist_assignment``.
"""

from __future__ import annotations

import dataclasses
import itertools
import random
from collections import Counter

import pytest

from core.models import SectionInstructor, SectionPlacement
from core.services import section_teacher_assignment as sta
from core.services.timetable_optimisation_policy import instructor_schedule_metrics
from tests.one_teacher_support import (
    FLAG_ON,
    budget,
    course,
    engine_rows,
    instructor,
    link,
    mins,
    pinned_load,
    place,
    requirement,
    scenario,
    section,
    unplace,
)


@pytest.fixture
def flags(settings):
    for name, value in FLAG_ON.items():
        setattr(settings, name, value)
    return settings


def m(day: int, start: str, end: str) -> tuple[int, int, int]:
    return (day, mins(start), mins(end))


def taught(assignment) -> Counter:
    return Counter(t for t in assignment.teacher.values() if t is not None)


def tbd(assignment) -> list[str]:
    return sorted(k for k, s in assignment.status.items() if s == sta.STATUS_TBD)


# ── U1 / U24: eligibility by course identity, programmes from the boards ──


@pytest.mark.django_db
def test_u1_programming_ii_never_gets_a_programming_i_teacher(flags) -> None:
    pi = requirement("AI2", "CS112", "PROGRAMMING I")
    requirement("DS2", "CS112", "PROGRAMMING I")
    pii = requirement("AI", "CS112", "PROGRAMMING II", term=4)
    sc, _board = scenario(programs=("AI", "AI2", "DS2"))
    first, second = instructor("Dr First"), instructor("Dr Second")
    link("AI2", "CS112", first)
    link("DS2", "CS112", second)
    budget(sc, pi, 2, code="CS112")
    budget(sc, pii, 1, code="CS112")

    a = sta.load_assignment(sc.id)

    assert a.status[f"{pii}_S1"] == sta.STATUS_NO_LINKS
    assert a.teacher[f"{pii}_S1"] is None
    assert {a.teacher[f"{pi}_S1"], a.teacher[f"{pi}_S2"]} <= {first.id, second.id}
    assert a.slot(f"{pii}_S1").eligible == ()


@pytest.mark.django_db
def test_u24_empty_programmes_fall_back_to_the_boards(flags) -> None:
    key = requirement("AI", "AI201", "ML")
    sc, _board = scenario(programs=(), board_program="AI")
    person = instructor("Dr Board")
    link("AI", "AI201", person)
    budget(sc, key, 1)

    a = sta.load_assignment(sc.id)

    assert a.teacher[f"{key}_S1"] == person.id
    assert a.report["programmes_source"] == "boards"
    assert sta.coverage_of(a)["programmes_source"] == "boards"


# ── U2: one teacher per section, in both maps ─────────────────────────────


def test_u2_every_assigned_section_has_exactly_one_teacher_in_both_maps(flags) -> None:
    slots = course(
        "AI101",
        [{"meetings": [m(0, "08:00", "09:15")]}, {"meetings": [m(1, "08:00", "09:15")]}, {}],
        eligible=(1, 2, 3),
    )

    a = sta.solve_layout_aware(slots)

    assert len(a.v2_map()) == 3
    assert all(len(ids) == 1 for ids in a.v2_map().values())
    assert all(len(ids) == 1 for ids in a.pr4_map().values())
    assert {next(iter(v)) for v in a.v2_map().values()} <= {1, 2, 3}


# ── U3 / U4 / U4b: D10 ────────────────────────────────────────────────────


def test_u3_five_sections_two_teachers_leaves_one_to_be_decided(flags) -> None:
    slots = course("CS112::PI", [{} for _ in range(5)], eligible=(1, 2))

    a = sta.solve_layout_aware(slots)

    assert taught(a) == {1: 2, 2: 2}
    [left] = tbd(a)
    assert a.tbd_reason[left] == "d10"
    assert sta.coverage_of(a)["tbd_beyond_d10"] == 0


@pytest.mark.django_db
def test_u4_d10_applies_only_above_three_sections(flags) -> None:
    three = requirement("AI", "AI201", "ML")
    four = requirement("AI", "AI202", "DL")
    sc, _board = scenario()
    person = instructor("Dr Only")
    link("AI", "AI201", person)
    link("AI", "AI202", person)
    budget(sc, three, 3)
    budget(sc, four, 4)

    a = sta.load_assignment(sc.id)

    assert [a.teacher[f"{three}_S{n}"] for n in (1, 2, 3)] == [person.id] * 3
    four_taught = [a.teacher[f"{four}_S{n}"] for n in (1, 2, 3, 4)]
    assert four_taught.count(person.id) == 2
    assert {a.tbd_reason[f"{four}_S{n}"] for n in (1, 2, 3, 4) if four_taught[n - 1] is None} == {
        "d10"
    }


@pytest.mark.django_db
def test_u4b_a_placed_stale_label_does_not_switch_d10(flags) -> None:
    on = requirement("AI", "AI201", "ML")
    off = requirement("AI", "AI202", "DL")
    sc, board = scenario()
    person = instructor("Dr Stale")
    link("AI", "AI201", person)
    link("AI", "AI202", person)
    budget(sc, on, 5)
    budget(sc, off, 3)
    # A stale S7 is placed for both; placements never count toward N_c.
    place(board, section(sc, on, "S7"), [("SUN", "08:00", "09:15")])
    place(board, section(sc, off, "S7"), [("MON", "08:00", "09:15")])

    a = sta.load_assignment(sc.id)

    on_slots = [s for s in a.slots if s.course_key == on]
    off_slots = [s for s in a.slots if s.course_key == off]
    assert {s.course_sections for s in on_slots} == {5}
    assert {s.course_sections for s in off_slots} == {3}
    assert sum(1 for s in on_slots if a.teacher[s.key] == person.id) == 2
    assert sum(1 for s in off_slots if a.teacher[s.key] == person.id) == 4


# ── U5: where the TBD lands ───────────────────────────────────────────────


def test_u5_tbd_lands_on_the_unplaced_section_even_when_idle_prefers_a_placed_one(flags) -> None:
    slots = course(
        "CS114",
        [
            {"meetings": [m(0, "08:00", "09:15")]},
            {"meetings": [m(0, "14:00", "15:15")]},  # S2 makes idle for whoever holds it
            {"meetings": [m(0, "09:30", "10:45")]},
            {"meetings": [m(1, "08:00", "09:15")]},
            {},  # S5, unplaced
        ],
        eligible=(1, 2),
    )

    a = sta.solve_layout_aware(slots)

    assert tbd(a) == ["CS114_S5"]


def test_u5_a_placed_stale_label_never_takes_the_tbd_ahead_of_an_unplaced_one(flags) -> None:
    sections = [{"meetings": [m(d, "08:00", "09:15")]} for d in range(4)]
    sections += [{}, {"label": "S7", "meetings": [m(4, "08:00", "09:15")]}]
    slots = course("CS114", sections, eligible=(1, 2, 3), planned=5)
    # Teacher 3 already holds 12 sessions: room for one section only, so
    # exactly one of the six sections is left without a teacher.
    slots.append(pinned_load(3, 12))

    a = sta.solve_layout_aware(slots)

    assert tbd(a) == ["CS114_S5"]


# ── U6: the weekly ceiling, on nominal sessions ───────────────────────────


def test_u6_weekly_ceiling_counts_nominal_sessions_and_is_inclusive(flags) -> None:
    partly_placed = {"meetings": [m(0, "08:00", "09:15"), m(2, "08:00", "09:15")], "nominal": 3}

    at_ceiling = sta.solve_layout_aware(
        [pinned_load(1, 12), *course("X", [partly_placed], eligible=(1,))]
    )
    over = sta.solve_layout_aware(
        [pinned_load(1, 13), *course("X", [partly_placed], eligible=(1,))]
    )
    other = sta.solve_layout_aware(
        [pinned_load(1, 13), *course("X", [partly_placed], eligible=(1, 2))]
    )

    assert at_ceiling.teacher["X_S1"] == 1
    assert over.teacher["X_S1"] is None
    assert over.tbd_reason["X_S1"] == "weekly_ceiling"
    assert other.teacher["X_S1"] == 2
    for fixed, expected in ((12, 1), (13, None)):  # the greedy draws the same line
        greedy = sta.greedy_assign(
            [pinned_load(1, fixed), *course("X", [partly_placed], eligible=(1,))]
        )
        assert greedy.teacher["X_S1"] == expected

    # Sections compete for the room left, so the constraint itself binds.
    pair = [partly_placed, {"nominal": 3}]
    one = sta.solve_layout_aware([pinned_load(1, 10), *course("Y", pair, eligible=(1,))])
    assert taught(one) == {1: 2}  # 10 + 3 + 3 > 15, even though only 5 meetings are placed
    three = [partly_placed, {"nominal": 3}, {"nominal": 3}]
    two = sta.solve_layout_aware([pinned_load(1, 9), *course("Y", three, eligible=(1,))])
    assert taught(two) == {1: 3}  # the pin and two of three: 9 + 3 + 3 = 15 exactly


def test_u6_the_ceiling_is_fifteen_even_when_the_cap_allows_more(flags) -> None:
    flags.TIMETABLE_INSTRUCTOR_DAILY_CAP = 4
    a = sta.solve_layout_aware([pinned_load(1, 13), *course("X", [{}], eligible=(1,))])

    assert sta.weekly_ceiling() == 15
    assert a.tbd_reason == {"X_S1": "weekly_ceiling"}


@pytest.mark.django_db
def test_u6_nominal_sessions_come_from_the_budget_pattern_not_the_placements(flags) -> None:
    key = requirement("AI", "AI210", "LAB COURSE", credit=4)
    sc, board = scenario()
    person = instructor("Dr Nominal")
    link("AI", "AI210", person)
    budget(sc, key, 1, credit=4)
    # Two of the course's three meetings are placed.
    place(board, section(sc, key, "S1"), [("SUN", "08:00", "09:15"), ("TUE", "08:00", "09:15")])

    [slot] = sta.collect_slots(sc.id)

    assert slot.nominal_sessions == 3
    assert len(slot.meetings) == 2


# ── U7: the day-off target before days ────────────────────────────────────


def test_u7_the_lighter_teacher_gets_the_section_over_a_fewer_days_choice(flags) -> None:
    heavy = pinned_load(1, 10, meetings=(m(0, "08:00", "09:15"), m(2, "08:00", "09:15")))
    light = pinned_load(2, 4, meetings=(m(1, "08:00", "09:15"), m(3, "08:00", "09:15")))
    slots = [
        heavy,
        light,
        *course("X", [{"meetings": [m(0, "09:30", "10:45"), m(2, "09:30", "10:45")]}]),
    ]

    a = sta.solve_layout_aware(slots)

    assert a.teacher["X_S1"] == 2


# ── U8 / U9 / U10: clash and the daily cap are hard ───────────────────────


def test_u8_overlapping_sections_never_share_a_teacher(flags) -> None:
    slots = [
        *course("A", [{"meetings": [m(0, "08:00", "09:15")]}]),
        *course("B", [{"meetings": [m(0, "08:30", "09:45")]}]),
        *course(
            "C", [{"meetings": [m(1, "10:00", "11:15")]}, {"meetings": [m(1, "10:30", "11:45")]}]
        ),
    ]

    a = sta.solve_layout_aware(slots)

    assert a.teacher["A_S1"] != a.teacher["B_S1"]
    assert a.teacher["C_S1"] != a.teacher["C_S2"]
    assert sta.violations(a) == {"clash_pairs": 0, "over_cap_sessions": 0}


def _four_on_sunday():
    # One session each, so neither the weekly ceiling nor the day-off target
    # (both derived from the cap) can split them: only the cap does.
    return [
        course(code, [{"meetings": [m(0, start, end)]}], nominal=1)[0]
        for code, start, end in (
            ("A", "08:00", "09:15"),
            ("B", "09:30", "10:45"),
            ("C", "11:00", "12:15"),
            ("D", "13:00", "14:15"),
        )
    ]


def _max_per_day(a) -> int:
    per = Counter()
    for slot in a.slots:
        if a.teacher.get(slot.key) is not None:
            for day, _s, _e in slot.meetings:
                per[(a.teacher[slot.key], day)] += 1
    return max(per.values())


def test_u9_the_daily_cap_is_hard_and_follows_the_boards_setting(flags) -> None:
    assert _max_per_day(sta.solve_layout_aware(_four_on_sunday())) == 3

    flags.TIMETABLE_INSTRUCTOR_DAILY_CAP = 2
    assert _max_per_day(sta.solve_layout_aware(_four_on_sunday())) == 2

    flags.TIMETABLE_INSTRUCTOR_DAILY_CAP = 3
    flags.TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED = False
    assert _max_per_day(sta.solve_layout_aware(_four_on_sunday())) == 4


def test_u10_a_section_every_teacher_clashes_with_is_tbd_and_charges_nobody(flags) -> None:
    slots = [
        pinned_load(1, 3, meetings=(m(0, "08:00", "09:15"),)),
        *course("X", [{"meetings": [m(0, "08:30", "09:45")]}], eligible=(1,)),
    ]

    a = sta.solve_layout_aware(slots)

    assert a.status["X_S1"] == sta.STATUS_TBD
    assert a.tbd_reason["X_S1"] == "clash"
    assert "X_S1" not in a.v2_map()
    assert a.pr4_map()["X|S1"] == set()
    assert sta.violations(a)["clash_pairs"] == 0


# ── U11 / U12: days before idle, then stability ───────────────────────────


def test_u11_working_days_rank_above_idle(flags) -> None:
    slots = [
        pinned_load(1, 3, meetings=(m(0, "08:00", "09:15"),)),
        pinned_load(2, 3, meetings=(m(1, "08:00", "09:15"),)),
        # Teacher 1 already works Sunday (no new day, 285 idle minutes);
        # teacher 2 would add a day with no idle.
        *course("X", [{"meetings": [m(0, "14:00", "15:15")]}]),
    ]

    a = sta.solve_layout_aware(slots)

    assert a.teacher["X_S1"] == 1


def test_u12_equal_alternatives_keep_the_current_teacher(flags) -> None:
    slots = course("X", [{"current": 2}], eligible=(1, 2))

    a = sta.solve_layout_aware(slots)

    assert a.teacher["X_S1"] == 2
    assert sta.solve_layout_aware(course("X", [{}], eligible=(1, 2))).teacher["X_S1"] == 1


# ── U13: determinism ──────────────────────────────────────────────────────


def _random_instance(rng: random.Random, n_courses: int = 4, pools=((1, 2, 3, 4, 5),)) -> list:
    """Random courses; each draws its teachers from one of ``pools`` (disjoint
    pools give disconnected components)."""
    slots = []
    for c in range(n_courses):
        rows = []
        for _ in range(rng.randint(1, 5)):
            if rng.random() < 0.8:
                day = rng.randrange(5)
                start = rng.choice((480, 570, 660, 780, 870))
                rows.append({"meetings": [(day, start, start + 75)]})
            else:
                rows.append({})
        pool = pools[c % len(pools)]
        teachers = tuple(sorted(rng.sample(pool, rng.randint(1, min(3, len(pool))))))
        slots += course(f"C{c}", rows, eligible=teachers, nominal=rng.choice((2, 3, 4)))
    return slots


def test_u13_the_assignment_is_deterministic_and_order_independent(flags) -> None:
    rng = random.Random(13)
    for _ in range(5):
        slots = _random_instance(rng)
        first = sta.solve_layout_aware(slots)
        again = sta.solve_layout_aware(slots)
        shuffled = list(slots)
        rng.shuffle(shuffled)
        reordered = sta.solve_layout_aware(shuffled)
        greedy = sta.greedy_assign(slots)
        greedy_shuffled = sta.greedy_assign(shuffled)

        assert first.teacher == again.teacher == reordered.teacher
        assert greedy.teacher == greedy_shuffled.teacher


@pytest.mark.django_db
def test_u13_insertion_order_of_links_sections_and_placements_does_not_matter(flags) -> None:
    from django.db import transaction

    times = {"S1": "08:00", "S2": "09:30", "S3": "11:00", "S4": "13:00"}
    results = []
    for order in (0, 1):
        savepoint = transaction.savepoint()
        key = requirement("AI", "CS114", "ALGORITHMS")
        sc, board = scenario(name=f"order {order}")
        people = [instructor(name) for name in ("Dr A", "Dr B", "Dr C")]
        if order:
            people.reverse()
        for person in people:
            link("AI", "CS114", person, role="primary" if person is people[0] else "co")
        budget(sc, key, 5)
        for label in sorted(times, reverse=bool(order)):
            start = times[label]
            end = f"{int(start[:2]) + 1:02d}:{start[3:]}"
            place(board, section(sc, key, label), [("SUN", start, end), ("TUE", start, end)])
        by_id = {p.id: p.full_name for p in people}
        a = sta.load_assignment(sc.id)
        results.append({k: by_id.get(t) for k, t in a.teacher.items()})
        transaction.savepoint_rollback(savepoint)

    assert results[0] == results[1]
    assert sum(1 for name in results[0].values() if name) == 5  # three teachers, two each


# ── U14 / U14b: the exact optimum, against an oracle ──────────────────────


def _objective(slots, picks, current) -> tuple:
    a = sta._assemble(slots, picks, current, bounds=None, kept=frozenset(), report={})
    l1, l2, l3, l4, l5 = sta.assignment_key(a)
    l6 = sum(1 for k, t in current.items() if picks.get(k) != t)
    l7 = sum(
        s.eligible.index(picks[s.key])
        for s in slots
        if s.is_choice and picks.get(s.key) is not None
    )
    return (l1, l2, l3, l4, l5, l6, l7)


def _feasible(slots, picks, overlap) -> bool:
    """Hard rules checked independently of the solver's own code."""
    load, per_course, per_day = Counter(), Counter(), Counter()
    for slot in slots:
        teacher = picks.get(slot.key)
        if teacher is None:
            continue
        load[teacher] += slot.nominal_sessions
        per_course[(teacher, slot.course_key)] += 1
        if slot.course_cap is not None and per_course[(teacher, slot.course_key)] > slot.course_cap:
            return False
        for day, _s, _e in slot.meetings:
            per_day[(teacher, day)] += 1
    if any(v > sta.weekly_ceiling() for v in load.values()):
        return False
    if any(v > sta.daily_cap() for v in per_day.values()):
        return False
    return not any(picks.get(a) is not None and picks.get(a) == picks.get(b) for a, b in overlap)


def _oracle_instance(rng: random.Random) -> list:
    slots: list = []
    target = rng.randint(4, 7)
    while len(slots) < target:
        rows = []
        for _ in range(min(rng.randint(1, 4), target - len(slots))):
            current = rng.choice((None, 1, 2, 3))
            if rng.random() < 0.85:
                day = rng.randrange(2)
                start = rng.choice((480, 540, 600, 720, 840))
                meetings = [(day, start, start + 75)]
                if rng.random() < 0.5:
                    meetings.append((day + 2, start, start + 75))
                rows.append({"meetings": meetings, "current": current})
            else:
                rows.append({"current": current})
        teachers = tuple(sorted(rng.sample((1, 2, 3), rng.randint(1, 3))))
        rows = [dict(r, current=r["current"] if r["current"] in teachers else None) for r in rows]
        slots += course(
            f"C{len(slots)}",
            rows,
            eligible=teachers,
            nominal=rng.choice((3, 4, 5, 6)),
            planned=rng.choice((len(rows), 4, 5)),
        )
    return slots


def test_u14_the_staged_solve_is_the_lexicographic_optimum(flags) -> None:
    rng = random.Random(1914)
    instances = [(2, _four_on_sunday() + course("E", [{"meetings": [m(0, "15:00", "16:15")]}]))]
    for index in range(16):
        # Half the instances under a cap of 2, so the cap binds as well.
        instances.append((2 if index % 2 else 3, _oracle_instance(rng)))
    for cap, slots in instances:
        flags.TIMETABLE_INSTRUCTOR_DAILY_CAP = cap
        choice = [s for s in slots if s.is_choice]
        overlap = [
            (a.key, b.key)
            for a, b in itertools.combinations(slots, 2)
            if any(
                d1 == d2 and s1 < e2 and s2 < e1
                for d1, s1, e1 in a.meetings
                for d2, s2, e2 in b.meetings
            )
        ]
        current = {s.key: s.current for s in choice if s.current is not None}
        best = None
        for combo in itertools.product(*[(None, *s.eligible) for s in choice]):
            picks = {s.key: t for s, t in zip(choice, combo, strict=True)}
            if _feasible(slots, picks, overlap):
                value = _objective(slots, picks, current)
                best = value if best is None or value < best else best

        solved = sta.solve_layout_aware(slots)

        assert _feasible(slots, solved.teacher, overlap)
        assert _objective(slots, solved.teacher, current) == best


def _linear_idle(intervals) -> int:
    if not intervals:
        return 0
    first = min(s for s, _e in intervals)
    last = max(e for _s, e in intervals)
    return max(0, last - first - sum(e - s for s, e in intervals))


def test_u14b_the_linear_idle_is_the_merged_idle_without_clashes_and_a_bound_with_them() -> None:
    from core.services.timetable_assignment_models import SectionMeeting, SectionState

    rng = random.Random(14)
    for _ in range(300):
        intervals = []
        for _ in range(rng.randint(1, 5)):
            start = rng.randrange(480, 1000, 15)
            intervals.append((start, start + rng.choice((50, 75, 100))))
        states = [
            SectionState(
                section_id=f"S{i}",
                course_code="X",
                meetings=[SectionMeeting(day=0, start_min=s, end_min=e)],
                max_capacity=40,
                reserve_capacity=2,
            )
            for i, (s, e) in enumerate(intervals)
        ]
        merged = instructor_schedule_metrics(
            states, {f"S{i}": frozenset({1}) for i in range(len(intervals))}
        )
        clash = any(a[0] < b[1] and b[0] < a[1] for a, b in itertools.combinations(intervals, 2))
        if clash:
            assert _linear_idle(intervals) <= merged["idle_minutes"]
        else:
            assert _linear_idle(intervals) == merged["idle_minutes"]


def test_u14b_the_models_idle_stage_equals_the_measured_idle(flags) -> None:
    slots = [
        *course("A", [{"meetings": [m(0, "08:00", "09:15")]}], eligible=(1,)),
        *course("B", [{"meetings": [m(0, "13:00", "14:15")]}], eligible=(1,)),
        *course(
            "C",
            [{"meetings": [m(1, "08:00", "09:15")]}, {"meetings": [m(1, "11:00", "12:15")]}],
            eligible=(1, 2),
        ),
    ]

    a = sta.solve_layout_aware(slots)

    stage_l5 = sum(
        value
        for _i, stages in a.report["stages"]
        for name, _st, value, _dt in stages
        if name == "L5"
    )
    # 225 between A and B on Sunday, 105 between C's two Monday sections.
    assert stage_l5 == sta.schedule_of(a)["idle_minutes"] == 330


# ── U15: decomposition ────────────────────────────────────────────────────


def test_u15_components_solve_to_the_monolithic_optimum_with_bounds(flags) -> None:
    rng = random.Random(15)
    components_seen = 0
    for _ in range(6):
        slots = _random_instance(rng, n_courses=6, pools=((1, 2, 3), (4, 5), (6, 7)))
        saved = sta.greedy_assign(slots)
        week = sta.schedule_of(saved)
        bounds = {
            k: (days, week["idle_minutes_by_instructor"][k], 30)
            for k, days in week["working_days_by_instructor"].items()
        }
        choice = sorted((s for s in slots if s.is_choice), key=lambda s: s.key)
        components_seen += len(sta._components(choice))
        current = sta._current_map(slots, saved)
        overlap = [
            (a.key, b.key)
            for a, b in itertools.combinations(slots, 2)
            if any(
                d1 == d2 and s1 < e2 and s2 < e1
                for d1, s1, e1 in a.meetings
                for d2, s2, e2 in b.meetings
            )
        ]
        for limits in (None, bounds):
            split = sta.solve_layout_aware(slots, current=saved, bounds=limits)
            # The same model over every section at once.
            whole, _meta = sta._solve_component(
                choice,
                sta._fixed_ledger(slots, {}),
                current,
                limits,
                max(s.n_c for s in slots) + 1,
                {s.key: saved.teacher.get(s.key) for s in choice},
                float("inf"),
                [],
            )
            # Optima can tie at every stage (two sections swapping teachers),
            # so the stage values are what must agree, and both must be valid.
            assert _feasible(slots, split.teacher, overlap)
            assert _objective(slots, split.teacher, current) == _objective(slots, whole, current)
            if limits is not None:
                week_after = sta.schedule_of(split)
                for teacher, days in week_after["working_days_by_instructor"].items():
                    limit_days, limit_idle, allowance = limits.get(teacher, (0, 0, 0))
                    assert days <= limit_days
                    assert week_after["idle_minutes_by_instructor"][
                        teacher
                    ] <= limit_idle + allowance * (limit_days - days)
    assert components_seen > 6  # the instances really decompose


# ── U16: every failure falls back to the greedy ───────────────────────────


def _assert_greedy_fallback(a, slots) -> None:
    assert a.report["engine"] == "greedy"
    assert a.teacher == sta.greedy_assign(slots).teacher
    assert sta.violations(a) == {"clash_pairs": 0, "over_cap_sessions": 0}
    assert all(len(v) == 1 for v in a.v2_map().values())


def test_u16_a_model_over_the_element_budget_uses_the_greedy(flags) -> None:
    flags.TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS = 5
    slots = _four_on_sunday() + course("E", [{}, {}], eligible=(1, 2))

    a = sta.solve_layout_aware(slots)

    _assert_greedy_fallback(a, slots)
    assert a.report["fallback_reason"] == "model_too_large"


def test_u16_a_raising_solver_or_a_failing_import_uses_the_greedy(flags, monkeypatch) -> None:
    from ortools.sat.python import cp_model

    slots = _four_on_sunday()

    def boom(self, *args, **kwargs):
        raise RuntimeError("solver crashed")

    monkeypatch.setattr(cp_model.CpSolver, "solve", boom)
    _assert_greedy_fallback(sta.solve_layout_aware(slots), slots)
    monkeypatch.undo()

    import builtins

    real_import = builtins.__import__

    def no_ortools(name, *args, **kwargs):
        if name.startswith("ortools"):
            raise ImportError("no ortools here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_ortools)
    a = sta.solve_layout_aware(slots)
    monkeypatch.undo()
    _assert_greedy_fallback(a, slots)
    assert a.report["fallback_reason"] == "solver_unavailable"


def test_u16_a_stale_current_map_only_breaks_the_fallbacks_ties(flags) -> None:
    """Every shared course is Dr 1's in the current map, as the union era's
    meeting names say. Seeded first, that map fills Dr 1's week to the ceiling
    and leaves the course only Dr 1 can teach without a teacher; the greedy
    gives it to Dr 1 first and spreads the rest."""
    flags.TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS = 5
    slots = course("ONLY1", [{}], eligible=(1,)) + [
        slot for n in range(5) for slot in course(f"SHARED{n}", [{"current": 1}], eligible=(1, 2))
    ]

    a = sta.solve_layout_aware(slots)

    assert a.report["fallback_reason"] == "model_too_large"
    _assert_greedy_fallback(a, slots)
    assert tbd(a) == []
    assert a.teacher["ONLY1_S1"] == 1


# ── U17: storage — read-back, layout edits, pins, forced FEASIBLE ─────────


def _two_course_board(flags):
    alg = requirement("AI", "CS114", "ALGORITHMS")
    ml = requirement("AI", "AI201", "ML")
    sc, board = scenario()
    a, b = instructor("Dr A"), instructor("Dr B")
    for person in (a, b):
        link("AI", "CS114", person, role="primary" if person is a else "co")
    link("AI", "AI201", a)
    budget(sc, alg, 5)
    budget(sc, ml, 2)
    sections = {}
    for n, (day, start) in enumerate(
        (("SUN", "08:00"), ("SUN", "09:30"), ("MON", "08:00"), ("MON", "09:30")), start=1
    ):
        ts = section(sc, alg, f"S{n}")
        end = f"{int(start[:2]) + 1:02d}:{start[3:5]}"
        place(board, ts, [(day, start, end), ("WED" if day == "SUN" else "THU", start, end)])
        sections[f"S{n}"] = ts
    ml1 = section(sc, ml, "S1")
    place(board, ml1, [("TUE", "08:00", "09:00")])
    sections["ML1"] = ml1
    return sc, board, alg, ml, sections


@pytest.mark.django_db
def test_u17_a_persisted_map_reads_back_exactly_including_tbd(flags) -> None:
    sc, _board, alg, _ml, _sections = _two_course_board(flags)
    proposed = sta.propose_layout_assignment(sc.id)
    sta.persist_assignment(sc.id, proposed)

    back = sta.load_assignment(sc.id)

    assert back.teacher == proposed.teacher
    assert back.status == proposed.status
    assert tbd(back) == [f"{alg}_S5"]
    assert back.report["closure_keys"] == []
    assert back.report["stale_rows_ignored"] == 0


@pytest.mark.django_db
def test_u17_layout_edits_never_change_or_drop_a_stored_row(flags) -> None:
    sc, board, alg, _ml, sections = _two_course_board(flags)
    sta.persist_assignment(sc.id, sta.propose_layout_assignment(sc.id))
    stored = engine_rows(sc)
    baseline = sta.load_assignment(sc.id)

    unplace(sections["S1"])  # unplace one
    unplace(sections["S2"], day="WED")  # partly unplace another
    SectionPlacement.objects.filter(term_section=sections["S3"], day="MON").update(
        start_time="09:30",
        end_time="10:30",  # move one onto S4's time
    )
    after = sta.load_assignment(sc.id)

    assert engine_rows(sc) == stored
    for key, teacher in baseline.teacher.items():
        if baseline.status[key] == sta.STATUS_ENGINE:
            assert after.teacher[key] == teacher, key
    # ... because every stored row is still read as valid, not re-derived.
    assert after.report["stale_rows_ignored"] == 0
    assert not set(after.report["closure_keys"]) & {
        key for key, s in baseline.status.items() if s == sta.STATUS_ENGINE
    }
    # Derived changes only for sections without a row, and only towards assigned.
    for key in after.teacher:
        if baseline.status[key] == sta.STATUS_TBD and after.status[key] != sta.STATUS_TBD:
            assert after.status[key] == sta.STATUS_ENGINE
    with sta.pinned_assignment(sc.id, baseline):
        place(board, section(sc, alg, "S5"), [("THU", "13:00", "14:00")])
        assert sta.load_assignment(sc.id) is baseline


@pytest.mark.django_db
def test_u17_stored_rows_over_the_weekly_ceiling_drop_by_rank_whatever_the_layout(flags) -> None:
    first = requirement("AI", "AI301", "FIRST", credit=4)
    second = requirement("AI", "AI302", "SECOND", credit=4)
    sc, board = scenario()
    person = instructor("Dr Busy")
    for code in ("AI301", "AI302"):
        link("AI", code, person)
    for key in (first, second):
        budget(sc, key, 3, credit=4)
        for n in (1, 2, 3):  # 6 x 3 nominal sessions = 18 > 15, stored by an older link set
            SectionInstructor.objects.create(
                scenario=sc, course_key=key, section=f"S{n}", instructor=person, source="engine"
            )

    unplaced = sta.load_assignment(sc.id)
    ts = section(sc, first, "S1")
    place(board, ts, [("SUN", "08:00", "09:15")])  # only one of its three meetings placed
    placed = sta.load_assignment(sc.id)

    for a in (unplaced, placed):
        assert tbd(a) == [f"{first}_S3"]  # the highest rank goes first
        assert a.tbd_reason[f"{first}_S3"] == "weekly_ceiling"
        assert a.report["stale_rows_ignored"] == 1


@pytest.mark.django_db
def test_u17_a_solve_cut_short_is_closure_completed_before_it_is_stored(flags) -> None:
    sc, _board, alg, _ml, _sections = _two_course_board(flags)
    flags.TIMETABLE_ONE_TEACHER_STAGE_DETERMINISTIC_TIME = 1e-9
    cut = sta.propose_layout_assignment(sc.id)
    sta.persist_assignment(sc.id, cut)
    assert sta.load_assignment(sc.id).report["closure_keys"] == []

    # The extreme cut: nothing assigned at all. Persist must complete it.
    nothing = sta.TeacherAssignment(
        teacher=dict.fromkeys(cut.teacher),
        status={s.key: sta.STATUS_TBD if s.is_choice else cut.status[s.key] for s in cut.slots},
        tbd_reason={},
        kept=frozenset(),
        universe=cut.universe,
        slots=cut.slots,
    )
    sta.persist_assignment(sc.id, nothing)
    back = sta.load_assignment(sc.id)

    assert len(engine_rows(sc)) == 6  # four CS114 sections (two each) + AI201 S1 and S2
    assert back.report["closure_keys"] == []
    assert tbd(back) == [f"{alg}_S5"]


# ── U18 / U19 / U20 ───────────────────────────────────────────────────────


def test_u18_core_constants_equal_the_schedulers(flags) -> None:
    instructors = pytest.importorskip("scheduler.instructors")
    assert sta.D10_MAX_SECTIONS == instructors.MAX_SECTIONS_PER_COURSE
    assert sta.D10_APPLIES_ABOVE == instructors._CAP_APPLIES_ABOVE
    assert sta.WEEKLY_SESSION_CEILING == instructors.DERIVED_WEEKLY_SESSION_CAP
    assert sta.weekly_ceiling() == 15
    assert sta.soft_weekly() == 12


def test_u19_the_rule_needs_both_flags(settings) -> None:
    from core.services.timetable_flags import is_one_teacher_active

    settings.TIMETABLE_ONE_TEACHER_PER_SECTION = True
    settings.TIMETABLE_INSTRUCTOR_LINKS_ENABLED = False
    assert not is_one_teacher_active()
    settings.TIMETABLE_INSTRUCTOR_LINKS_ENABLED = True
    assert is_one_teacher_active()
    settings.TIMETABLE_ONE_TEACHER_PER_SECTION = False
    assert not is_one_teacher_active()


def test_u20_padded_metrics_stay_consistent_evidence(flags) -> None:
    from core.services.timetable_optimisation_policy import instructor_schedule_regressions

    before = {
        "working_days": 3,
        "idle_minutes": 30,
        "working_days_by_instructor": {"1": 2, "2": 1},
        "idle_minutes_by_instructor": {"1": 30, "2": 0},
        "affected_instructors": 2,
    }
    after = {  # teacher 2 lost every section; teacher 3 is new
        "working_days": 3,
        "idle_minutes": 30,
        "working_days_by_instructor": {"1": 2, "3": 1},
        "idle_minutes_by_instructor": {"1": 30, "3": 0},
        "affected_instructors": 2,
    }
    universe = {1, 2, 3}

    padded_before = sta.pad_schedule_metrics(before, universe)
    padded_after = sta.pad_schedule_metrics(after, universe)

    assert padded_after["working_days_by_instructor"]["2"] == 0
    assert padded_before["affected_instructors"] == padded_after["affected_instructors"] == 3
    assert padded_before["working_days"] == 3 and padded_after["idle_minutes"] == 30
    regressions = instructor_schedule_regressions(padded_before, padded_after)
    assert not [r for r in regressions if r["metric"].endswith("_evidence")]
    assert [r["instructor_id"] for r in regressions] == ["3"]


# ── U21 / U22: the kept rule and the run-start bounds ─────────────────────


def test_u21_a_section_in_a_clash_under_the_current_map_stays_with_its_teacher(flags) -> None:
    slots = [
        *course("A", [{"meetings": [m(0, "08:00", "09:15")], "current": 1}], eligible=(1,)),
        *course("B", [{"meetings": [m(0, "08:30", "09:45")], "current": 1}], eligible=(1,)),
    ]
    current = {"A_S1": 1, "B_S1": 1}

    kept = sta.solve_layout_aware(slots, current=current, kept=sta._violating_keys(slots, current))

    assert kept.teacher == {"A_S1": 1, "B_S1": 1}
    assert kept.kept == frozenset({"A_S1", "B_S1"})
    assert sta.coverage_of(kept)["kept_in_violation"] == ["A_S1", "B_S1"]


def test_u22_the_bounded_run_start_never_regresses_a_teachers_week(flags) -> None:
    slots = [
        pinned_load(1, 3, meetings=(m(0, "08:00", "09:15"),)),
        pinned_load(2, 3, meetings=(m(1, "08:00", "09:15"),)),
        *course("X", [{"meetings": [m(0, "14:00", "15:15")]}]),
    ]
    saved = sta.greedy_assign(slots, seed={"X_S1": 2})
    before = sta.schedule_of(saved)
    bounds = {
        k: (d, before["idle_minutes_by_instructor"][k], 0)
        for k, d in before["working_days_by_instructor"].items()
    }

    unbounded = sta.solve_layout_aware(slots, current=saved)
    bounded = sta.solve_layout_aware(slots, current=saved, bounds=bounds)
    after = sta.schedule_of(bounded)

    assert unbounded.teacher["X_S1"] == 1  # the global optimum costs teacher 1 idle
    assert bounded.teacher["X_S1"] == 2
    for teacher, days in after["working_days_by_instructor"].items():
        assert days <= before["working_days_by_instructor"][teacher]
        assert (
            after["idle_minutes_by_instructor"][teacher]
            <= before["idle_minutes_by_instructor"][teacher]
        )


# ── U23: a co/lab holder is never made primary ────────────────────────────


@pytest.mark.django_db
def test_u23_a_teacher_with_a_co_row_on_the_section_is_not_its_primary(flags) -> None:
    key = requirement("AI", "AI201", "ML")
    sc, _board = scenario()
    co, other = instructor("Dr Co"), instructor("Dr Other")
    link("AI", "AI201", co)
    link("AI", "AI201", other, role="co")
    budget(sc, key, 1)
    SectionInstructor.objects.create(
        scenario=sc, course_key=key, section="S1", instructor=co, role="co", source="department"
    )

    a = sta.load_assignment(sc.id)
    sta.persist_assignment(sc.id, a)

    assert a.teacher[f"{key}_S1"] == other.id
    assert sta.coverage_of(a)["non_primary_conflicts"] == [
        {"section": f"{key}_S1", "instructor_id": co.id}
    ]


# ── U25: the TBD reasons ──────────────────────────────────────────────────


def test_u25_each_tbd_reason_is_attributed(flags) -> None:
    cases = {
        "d10": course("X", [{} for _ in range(5)], eligible=(1, 2)),
        "weekly_ceiling": [pinned_load(1, 13), *course("X", [{}], eligible=(1,))],
        "clash": [
            pinned_load(1, 3, meetings=(m(0, "08:00", "09:15"),)),
            *course("X", [{"meetings": [m(0, "09:00", "10:15")]}], eligible=(1,)),
        ],
        "daily_cap": [
            pinned_load(
                1,
                3,
                meetings=(m(0, "08:00", "09:00"), m(0, "10:00", "11:00"), m(0, "12:00", "13:00")),
            ),
            *course("X", [{"meetings": [m(0, "14:00", "15:00")]}], eligible=(1,)),
        ],
    }
    for reason, slots in cases.items():
        a = sta.solve_layout_aware(slots)
        assert {a.tbd_reason[k] for k in tbd(a)} == {reason}, reason

    bound = sta.solve_layout_aware(
        course("X", [{"meetings": [m(0, "08:00", "09:00")]}], eligible=(1,)),
        bounds={"1": (0, 0, 0)},
    )
    assert bound.tbd_reason == {"X_S1": "bound"}
    # A clash outranks the cap when teachers differ in what stops them.
    mixed = sta.solve_layout_aware(
        [
            pinned_load(1, 3, meetings=(m(0, "08:00", "09:15"),)),
            pinned_load(
                2,
                3,
                meetings=(m(0, "10:00", "11:00"), m(0, "12:00", "13:00"), m(0, "14:00", "15:00")),
            ),
            *course("X", [{"meetings": [m(0, "08:30", "09:30")]}], eligible=(1, 2)),
        ]
    )
    assert mixed.tbd_reason == {"X_S1": "clash"}


# ── U26: a section not placed yet keeps an option free for its teacher ────

SUN_TUE = (m(0, "08:00", "09:15"), m(2, "08:00", "09:15"))
MON_WED = (m(1, "08:00", "09:15"), m(3, "08:00", "09:15"))


def _boxing_board(options=(SUN_TUE, MON_WED)):
    """U is not placed and only Dr 1 teaches it; the placer would put it on
    Sunday + Tuesday or Monday + Wednesday at 08:00. A1 (Sunday 08:00) and A2
    (Monday 08:00) are for Dr 1 or Dr 2. Dr 1 already works Sunday and Monday,
    Dr 2 only Tuesday, so the fewest days give both A sections to Dr 1 — and
    then U has no option left."""
    return [
        pinned_load(1, 3, meetings=(m(0, "10:00", "11:15"), m(1, "10:00", "11:15"))),
        pinned_load(2, 3, meetings=(m(2, "10:00", "11:15"),)),
        *course("U", [{"placed": False, "options": options}], eligible=(1,)),
        *course(
            "A",
            [{"meetings": [m(0, "08:00", "09:15")]}, {"meetings": [m(1, "08:00", "09:15")]}],
            eligible=(1, 2),
        ),
    ]


def test_u26_the_exact_solve_leaves_a_missing_section_an_option(flags) -> None:
    control = sta.solve_layout_aware(_boxing_board(options=()))
    assert (control.teacher["A_S1"], control.teacher["A_S2"]) == (1, 1)  # fewest days

    a = sta.solve_layout_aware(_boxing_board())

    assert a.teacher["U_S1"] == 1
    assert sorted((a.teacher["A_S1"], a.teacher["A_S2"])) == [1, 2]
    assert sta.coverage_of(a)["unplaced_teacher_busy"] == []
    assert sta.violations(a) == {"clash_pairs": 0, "over_cap_sessions": 0}


def test_u26_a_pinned_missing_section_keeps_an_option_too(flags) -> None:
    slots = [
        s if s.key != "U_S1" else dataclasses.replace(s, fixed=1, fixed_kind="pin")
        for s in _boxing_board()
    ]

    for a in (sta.solve_layout_aware(slots), sta.greedy_assign(slots)):
        assert a.status["U_S1"] == sta.STATUS_PINNED
        assert sorted((a.teacher["A_S1"], a.teacher["A_S2"])) == [1, 2]
        assert sta.coverage_of(a)["unplaced_teacher_busy"] == []


def test_u26_the_greedy_never_takes_a_missing_sections_last_option(flags) -> None:
    g = sta.greedy_assign(_boxing_board())

    # U first (one teacher); A1 to Dr 1 (no new day, Monday + Wednesday left);
    # A2 would take U's last option, so it goes to Dr 2.
    assert (g.teacher["U_S1"], g.teacher["A_S1"], g.teacher["A_S2"]) == (1, 1, 2)
    assert sta.coverage_of(g)["unplaced_teacher_busy"] == []


def test_u26_a_section_the_map_in_force_left_no_option_is_kept_not_rescued(flags) -> None:
    """The kept rule: the map in force had already used U's last option, so U
    keeps its teacher and nobody is moved (and no bound broken) to free one;
    the coverage reports it."""
    slots = _boxing_board()
    current = {"U_S1": 1, "A_S1": 1, "A_S2": 1}
    kept = sta._violating_keys(slots, current)
    assert kept == {"U_S1"}

    a = sta.solve_layout_aware(slots, current=current, kept=kept)

    assert {k: a.teacher[k] for k in current} == current
    coverage = sta.coverage_of(a)
    assert coverage["kept_in_violation"] == ["U_S1"]
    assert coverage["unplaced_teacher_busy"] == ["U_S1"]


def test_u26_a_missing_section_no_teacher_can_still_place_is_tbd_with_its_reason(flags) -> None:
    cases = {
        "clash": [
            pinned_load(1, 3, meetings=SUN_TUE),
            *course("U", [{"placed": False, "options": (SUN_TUE,)}], eligible=(1,)),
        ],
        "daily_cap": [
            pinned_load(
                1,
                3,
                meetings=(m(0, "10:00", "11:00"), m(0, "12:00", "13:00"), m(0, "14:00", "15:00")),
            ),
            *course("U", [{"placed": False, "options": (SUN_TUE,)}], eligible=(1,)),
        ],
    }
    for reason, slots in cases.items():
        for a in (sta.solve_layout_aware(slots), sta.greedy_assign(slots)):
            assert a.status["U_S1"] == sta.STATUS_TBD, reason
            assert a.tbd_reason["U_S1"] == reason

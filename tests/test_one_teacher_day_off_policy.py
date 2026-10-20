"""One teacher per section under the day-off policy (W1-W7).

A re-pick of who teaches what on a fixed layout is judged by
``DayOffPolicy.swap_ok`` against the run's start: coverage first, then no new
violation, no breach growing, no instructor's days rising (with the student
score fixed, a spare day given up buys nothing), and a no-worse
(fives, idle net of allowances, days, idle). The bounded solve keeps a day
given up earlier for students, and the exact solve's L3 counts teachers with
no day off. Some tests hand ``_final_teacher_recompute`` an unbounded
proposal: the bounds steer the search, the judgement is what they pin.
"""

from __future__ import annotations

import dataclasses

import pytest

from core.models import SectionInstructor, TermSectionMeeting
from core.services import section_teacher_assignment as sta
from core.services.timetable_optimisation_policy import (
    DayOffAnchor,
    DayOffPolicy,
    current_day_off_policy,
    instructor_schedule_metrics,
)
from tests.one_teacher_support import (
    FLAG_ON,
    budget,
    course,
    engine_rows,
    instructor,
    link,
    pinned_load,
    place,
    requirement,
    scenario,
    section,
)

RUN = {"max_iterations": 3, "run_chain": False, "run_cpsat": False, "max_chain_iterations": 2}
SCORE = (0, 0, 0, 0, 1000, 0, 0, 0, 0)
GAIN = (0, 0, 0, 0, 990, 0, 0, 0, 0)


@pytest.fixture
def flags(settings):
    for name, value in FLAG_ON.items():
        setattr(settings, name, value)
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY = True
    settings.TIMETABLE_SECONDARY_RECOVERY_ENABLED = False
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_TIME_BUDGET_SECONDS = 5
    return settings


def _metrics(scenario_id: int, assignment) -> dict:
    from core.services.timetable_optimizer_v2 import build_section_states_for_scenario

    return instructor_schedule_metrics(
        build_section_states_for_scenario(scenario_id), assignment.v2_map()
    )


def _policy_on_saved(scenario_id: int, score=SCORE) -> tuple:
    """The map in force and a policy whose run start is that map on this board."""
    in_force = sta.load_assignment(scenario_id)
    anchor = DayOffAnchor.from_metrics(
        _metrics(scenario_id, in_force), padded=True, universe=in_force.universe
    )
    return in_force, DayOffPolicy(anchor, score, "saved_board")


def _unbounded_proposals(monkeypatch, *, change=None) -> None:
    """``_final_teacher_recompute``'s exact re-pick without its bounds (and
    then ``change``d): the proposal the policy has to judge."""
    real = sta.propose_layout_assignment

    def propose(scenario_id, **kwargs):
        proposed = real(scenario_id, **{**kwargs, "bounds": None})
        return change(proposed) if change is not None else proposed

    monkeypatch.setattr(sta, "propose_layout_assignment", propose)


def _pin(sc, board, key, person, day, start, end, *, locked=False) -> None:
    """A section whose teacher is a department pin (its time is free unless locked)."""
    place(
        board,
        section(sc, key, "S1"),
        [(day, start, end)],
        instructor_name=person.full_name,
        locked=locked,
    )
    SectionInstructor.objects.create(
        scenario=sc, course_key=key, section="S1", instructor=person, source="department"
    )


def _engine(sc, board, key, person, day, start, end, *, locked=False) -> None:
    """A section the engine stored with ``person``."""
    place(
        board,
        section(sc, key, "S1"),
        [(day, start, end)],
        instructor_name=person.full_name,
        locked=locked,
    )
    SectionInstructor.objects.create(
        scenario=sc, course_key=key, section="S1", instructor=person, source="engine"
    )


def _handover(*, total_falls: bool):
    """X (Dr A or Dr B) on Sunday 14:00, stored with Dr B. Dr A is pinned on
    Sunday; Dr B on Monday, and — when ``total_falls`` — on Sunday 08:00 too.

    total_falls=False: X to Dr A saves Dr B a SPARE day (2 -> 1) and gives Dr A
    285 idle minutes; the total rises from 0 to 285.
    total_falls=True: X to Dr A moves 285 idle minutes of Dr B's to 165 of Dr
    A's; nobody's days change and the total falls.
    """
    x = requirement("AI", "AI301", "X COURSE")
    p = requirement("AI", "AI401", "PIN A")
    q = requirement("AI", "AI402", "PIN B")
    keys = [x, p, q]
    if total_falls:
        keys.append(requirement("AI", "AI403", "PIN B SUNDAY"))
    sc, board = scenario()
    a, b = instructor("Dr A"), instructor("Dr B")
    link("AI", "AI301", a)
    link("AI", "AI301", b, role="co")
    for key in keys:
        budget(sc, key, 1)
    _pin(sc, board, p, a, "SUN", *(("10:00", "11:15") if total_falls else ("08:00", "09:15")))
    _pin(sc, board, q, b, "MON", "08:00", "09:15")
    if total_falls:
        _pin(sc, board, keys[3], b, "SUN", "08:00", "09:15")
    _engine(sc, board, x, b, "SUN", "14:00", "15:15")
    return sc, x


# ── W1: how a swap is judged (R4) ─────────────────────────────────────────


@pytest.mark.django_db
def test_w1_a_swap_that_raises_total_idle_is_kept_out(flags, monkeypatch) -> None:
    from core.services.timetable_optimizer_v2 import _final_teacher_recompute

    sc, x = _handover(total_falls=False)
    in_force, policy = _policy_on_saved(sc.id)
    _unbounded_proposals(monkeypatch)

    kept, changed, report = _final_teacher_recompute(sc.id, in_force, policy=policy, score=SCORE)

    assert (changed, report["adopted"], report["reason"], report["day_off_policy"]) == (
        False,
        False,
        "day_off_policy",
        True,
    )
    assert engine_rows(sc) == {(x, "S1"): "Dr B"}


@pytest.mark.django_db
def test_w1_a_swap_that_lowers_total_idle_is_adopted_though_one_teacher_rises(
    flags, monkeypatch
) -> None:
    from core.services.timetable_optimizer_v2 import _final_teacher_recompute

    sc, x = _handover(total_falls=True)
    in_force, policy = _policy_on_saved(sc.id)
    _unbounded_proposals(monkeypatch)

    adopted, changed, report = _final_teacher_recompute(sc.id, in_force, policy=policy, score=SCORE)

    assert (changed, report["adopted"], report["reason"]) == (True, True, "adopted")
    assert engine_rows(sc) == {(x, "S1"): "Dr A"}
    after = _metrics(sc.id, adopted)["idle_minutes_by_instructor"]
    assert sorted(after.values()) == [0, 165]  # Dr A +165, Dr B -285
    assert set(
        TermSectionMeeting.objects.filter(term_section__course_key=x).values_list(
            "instructor", flat=True
        )
    ) == {"Dr A"}


# ── W2: no day-neutral swap ───────────────────────────────────────────────


def _five_day_week():
    """Dr Y is pinned Monday-Thursday and stored on Z (Sunday): five days.
    Dr X, pinned Monday-Wednesday (three days), may also teach Z."""
    sc, board = scenario()
    y, xx = instructor("Dr Y"), instructor("Dr X")
    z = requirement("AI", "AI301", "Z COURSE")
    link("AI", "AI301", y)
    link("AI", "AI301", xx, role="co")
    budget(sc, z, 1)
    for n, day in enumerate(("MON", "TUE", "WED", "THU")):
        key = requirement("AI", f"AI40{n}", f"PIN Y {day}")
        budget(sc, key, 1)
        _pin(sc, board, key, y, day, "08:00", "09:15")
    for n, day in enumerate(("MON", "TUE", "WED")):
        key = requirement("AI", f"AI50{n}", f"PIN X {day}")
        budget(sc, key, 1)
        _pin(sc, board, key, xx, day, "10:00", "11:15")
    _engine(sc, board, z, y, "SUN", "08:00", "09:15")
    return sc, f"{z}_S1", y, xx


@pytest.mark.django_db
def test_w2_a_spare_day_is_never_given_up_for_another_teachers_first_day_off(
    flags, monkeypatch
) -> None:
    """Handing Z to Dr X gives Dr Y a first day off and costs Dr X a spare one.
    Students gain nothing from a swap, so it is refused — even though the run
    has a student gain in hand that could otherwise justify a spare day."""
    from core.services.timetable_optimizer_v2 import _final_teacher_recompute

    sc, z, y, xx = _five_day_week()
    in_force, policy = _policy_on_saved(sc.id)
    _unbounded_proposals(monkeypatch)
    assert sta.propose_layout_assignment(sc.id, current=in_force).teacher[z] == xx.id

    kept, changed, report = _final_teacher_recompute(sc.id, in_force, policy=policy, score=GAIN)

    assert (changed, report["reason"]) == (False, "day_off_policy")
    assert kept.teacher[z] == y.id


# ── W3: the exact solve's L3 counts teachers with no day off ──────────────


def _w3_slots():
    """Y (1) is pinned Monday-Thursday; P (Sunday) may go to Y or W (3). Z (2)
    is pinned Monday and Tuesday; Q (Thursday) may go to Z or W. W, pinned on
    Sunday and Thursday, can take only one of them (the weekly ceiling: 5 + 6
    + 6 > 15). P to W gives Y a first day off at 225 idle minutes of W's; Q to
    W saves Z a spare day at 15. Both save one day; nobody passes 12 sessions."""
    hours = lambda day, start, end: ((day, start, end),)  # noqa: E731
    slots = [
        pinned_load(1, 1, meetings=hours(day, 480, 555), key=f"Y{day}") for day in (1, 2, 3, 4)
    ]
    slots += [pinned_load(2, 1, meetings=hours(day, 600, 675), key=f"Z{day}") for day in (1, 2)]
    slots += [
        pinned_load(3, 1, meetings=hours(0, 480, 555), key="WSUN"),
        pinned_load(3, 1, meetings=hours(4, 480, 555), key="WTHU"),
        pinned_load(3, 3, key="WLOAD"),
    ]
    slots += course(
        "P", [{"meetings": hours(0, 780, 855), "current": 1}], eligible=(1, 3), nominal=6
    )
    slots += course(
        "Q", [{"meetings": hours(4, 570, 645), "current": 2}], eligible=(2, 3), nominal=6
    )
    return slots


@pytest.mark.parametrize(("policy_on", "p_to", "q_to"), [(True, 3, 2), (False, 1, 3)])
def test_w3_the_exact_solve_gives_a_first_day_off_before_saving_idle(
    settings, policy_on, p_to, q_to
) -> None:
    for name, value in FLAG_ON.items():
        setattr(settings, name, value)
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY = policy_on

    solved = sta.solve_layout_aware(_w3_slots(), current={"P_S1": 1, "Q_S1": 2})

    assert solved.report["engine"] == "cpsat" and solved.report["proven"]
    assert (solved.teacher["P_S1"], solved.teacher["Q_S1"]) == (p_to, q_to)


# ── W4: the swap bounds keep a day given up for students ──────────────────


def _justified_fourth_day():
    """Dr X teaches X4 on Thursday (only Dr X may) on top of three pinned days;
    the run started with Dr X on three days, so the fourth was given up for
    students earlier in the run. Separately U (Sunday 13:00) is stored with Dr
    M (pinned Sunday 08:00, 225 idle minutes) though Dr N (pinned Sunday 14:15)
    could take it at no idle."""
    sc, board = scenario()
    xx, m, n = instructor("Dr X"), instructor("Dr M"), instructor("Dr N")
    x4 = requirement("AI", "AI301", "X FOURTH")
    u = requirement("AI", "AI302", "U COURSE")
    link("AI", "AI301", xx)
    link("AI", "AI302", m)
    link("AI", "AI302", n, role="co")
    for key in (x4, u):
        budget(sc, key, 1)
    for k, day in enumerate(("MON", "TUE", "WED")):
        key = requirement("AI", f"AI40{k}", f"PIN X {day}")
        budget(sc, key, 1)
        _pin(sc, board, key, xx, day, "08:00", "09:15")
    for key, person, start, end in (("AI501", m, "08:00", "09:15"), ("AI502", n, "14:15", "15:30")):
        planner = requirement("AI", key, f"PIN {person.full_name}")
        budget(sc, planner, 1)
        _pin(sc, board, planner, person, "SUN", start, end)
    _engine(sc, board, x4, xx, "THU", "08:00", "09:15")
    _engine(sc, board, u, m, "SUN", "13:00", "14:15")
    return sc, f"{x4}_S1", f"{u}_S1", xx, n


@pytest.mark.django_db
def test_w4_a_day_given_up_for_students_is_kept_by_the_swap_bounds(flags) -> None:
    from core.services.timetable_optimizer_v2 import _final_teacher_recompute

    sc, x4, u, xx, n = _justified_fourth_day()
    in_force = sta.load_assignment(sc.id)
    now = _metrics(sc.id, in_force)
    start_days = {**now["working_days_by_instructor"], str(xx.id): 3}  # before the gain
    anchor = DayOffAnchor(
        days={str(i): start_days.get(str(i), 0) for i in in_force.universe},
        idle={str(i): now["idle_minutes_by_instructor"].get(str(i), 0) for i in in_force.universe},
        total_idle=now["idle_minutes"],
        padded=True,
        saved_idle=0,
    )
    policy = DayOffPolicy(anchor, SCORE, "saved_board")

    chosen, changed, report = _final_teacher_recompute(sc.id, in_force, policy=policy, score=GAIN)

    assert (changed, report["reason"]) == (True, "adopted")
    assert report["changed_sections"] == [u]
    assert chosen.teacher[x4] == xx.id and chosen.teacher[u] == n.id
    assert report["coverage"]["tbd"] == 0


# ── W5: coverage first ────────────────────────────────────────────────────


@pytest.mark.django_db
def test_w5_a_swap_that_leaves_a_section_without_a_teacher_is_refused(flags, monkeypatch) -> None:
    """A proposal that gives Dr Y a first day off by leaving Z to be decided
    is worse coverage, whatever it does for the week."""
    from core.services.timetable_optimizer_v2 import _final_teacher_recompute

    sc, z, y, _xx = _five_day_week()
    in_force, policy = _policy_on_saved(sc.id)

    def without_z(proposed):
        return dataclasses.replace(
            proposed,
            teacher={**proposed.teacher, z: None},
            status={**proposed.status, z: sta.STATUS_TBD},
        )

    _unbounded_proposals(monkeypatch, change=without_z)

    kept, changed, report = _final_teacher_recompute(sc.id, in_force, policy=policy, score=GAIN)

    assert (changed, report["reason"]) == (False, "worse_than_kept_map")
    assert kept.teacher[z] == y.id


# ── W6 / W7: Optimise Current hands the map on; flag off is untouched ─────


def _enrol(sc, course_key: str, count: int = 3) -> None:
    from core.models import ScenarioStudentCourseRequest, Student

    for k in range(count):
        Student.objects.create(
            student_id=8_200_000 + k, program="AI", section="M", gpa=3.0, total_earned_credits=60
        )
        ScenarioStudentCourseRequest.objects.create(
            scenario=sc,
            student_id=8_200_000 + k,
            course_key=course_key,
            course_code=course_key.split("::", 1)[0],
            course_name=course_key,
        )


def _idle_swap_board():
    """U (Sunday 13:00) stored with Dr M, pinned Sunday 08:00 (225 idle
    minutes); Dr N, pinned Sunday 14:15, could take it at none. Every time is
    locked (a lock never fixes the teacher), so only who teaches U can change."""
    sc, board = scenario()
    m, n = instructor("Dr M"), instructor("Dr N")
    u = requirement("AI", "AI302", "U COURSE")
    link("AI", "AI302", m)
    link("AI", "AI302", n, role="co")
    budget(sc, u, 1)
    for key, person, start, end in (("AI501", m, "08:00", "09:15"), ("AI502", n, "14:15", "15:30")):
        planner = requirement("AI", key, f"PIN {person.full_name}")
        budget(sc, planner, 1)
        _pin(sc, board, planner, person, "SUN", start, end, locked=True)
    _engine(sc, board, u, m, "SUN", "13:00", "14:15", locked=True)
    _enrol(sc, u)
    return sc, f"{u}_S1", m, n


class _Spy:
    """What each stage of an Optimise Current run saw."""

    def __init__(self, monkeypatch, scenario_id: int) -> None:
        from core.services import (
            timetable_instructor_compaction,
            timetable_local_search_v2,
            timetable_optimizer_v2,
            timetable_secondary_recovery_runner,
        )

        self.scenario_id = scenario_id
        self.optimise_kwargs: dict = {}
        self.recomputes: list[tuple] = []
        self.compaction: list = []
        self.recovery: list = []
        self.search: list = []
        real_optimise = timetable_optimizer_v2.optimise_current_timetable
        real_recompute = timetable_optimizer_v2._final_teacher_recompute
        real_compact = timetable_instructor_compaction.compact_instructor_schedules
        real_recovery = timetable_secondary_recovery_runner.run_secondary_recovery_for_scenario
        real_search = timetable_local_search_v2.diagnostic_driven_local_search

        def optimise(*args, **kwargs):
            self.optimise_kwargs = kwargs
            return real_optimise(*args, **kwargs)

        def recompute(scenario_id, in_force, **kwargs):
            out = real_recompute(scenario_id, in_force, **kwargs)
            self.recomputes.append((in_force, kwargs, out[0]))
            return out

        def compact(scenario_id):
            self.compaction.append((sta.current_pin(scenario_id), current_day_off_policy()))
            return real_compact(scenario_id)

        def recovery(scenario_id, result, profiles, rigidity, section_instructor_ids, tiers):
            self.recovery.append((sta.current_pin(scenario_id), section_instructor_ids))
            return real_recovery(
                scenario_id, result, profiles, rigidity, section_instructor_ids, tiers
            )

        def search(**kwargs):
            self.search.append(current_day_off_policy())
            return real_search(**kwargs)

        monkeypatch.setattr(timetable_optimizer_v2, "optimise_current_timetable", optimise)
        monkeypatch.setattr(timetable_optimizer_v2, "_final_teacher_recompute", recompute)
        monkeypatch.setattr(
            timetable_instructor_compaction, "compact_instructor_schedules", compact
        )
        monkeypatch.setattr(
            timetable_secondary_recovery_runner, "run_secondary_recovery_for_scenario", recovery
        )
        monkeypatch.setattr(timetable_local_search_v2, "diagnostic_driven_local_search", search)


def _start_on(monkeypatch, key: str, teacher: int) -> None:
    """The run starts with ``key`` charged to ``teacher`` (C5 would already
    have moved it): the improvement is left for S1."""
    from core.services.timetable_v2_runner import _RunTeachers

    real_start = _RunTeachers.start_run

    def start_run(self, bounds):
        real_start(self, bounds)
        self.start = dataclasses.replace(self.start, teacher={**self.start.teacher, key: teacher})
        return self.pin(self.start)

    monkeypatch.setattr(_RunTeachers, "start_run", start_run)


@pytest.mark.django_db
def test_w6_the_map_s1_stores_is_the_one_every_later_stage_charges(flags, monkeypatch) -> None:
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded
    from tests.test_one_teacher_pipeline import _score_of_saved_board

    sc, u, m, n = _idle_swap_board()
    _start_on(monkeypatch, u, m.id)
    spy = _Spy(monkeypatch, sc.id)

    result = run_v2_optimisation_guarded(sc.id, mode="current", **RUN)

    assert result["safety_blocked"] is False, result.get("safety_regression")
    (s1_in, s1_kwargs, s1_map), (c7_in, c7_kwargs, c7_map) = spy.recomputes
    assert s1_in.teacher[u] == m.id and s1_map.teacher[u] == n.id
    assert result["section_teachers"]["after_search"]["changed_sections"] == [u]
    assert result["section_teachers"]["after_search"]["day_off_policy"] is True
    # Compaction, recovery and C7 all work under the map S1 stored, not the start's.
    assert spy.compaction and all(pin is s1_map for pin, _policy in spy.compaction)
    assert spy.recovery == [(s1_map, s1_map.v2_map())]
    assert c7_in is s1_map and c7_kwargs["policy"] is not None
    assert engine_rows(sc)[tuple(u.rsplit("_", 1))] == "Dr N"
    assert result["final_score"] == _score_of_saved_board(sc)


@pytest.mark.django_db
@pytest.mark.parametrize("policy_on", [False, True])
def test_w7_with_the_flag_off_the_run_is_todays(flags, monkeypatch, policy_on) -> None:
    flags.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY = policy_on
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded

    sc, u, _m, _n = _idle_swap_board()
    bounds_seen: list = []
    real_bounds = sta.teacher_bounds

    def teacher_bounds(metrics):
        bounds_seen.append(real_bounds(metrics))
        return bounds_seen[-1]

    monkeypatch.setattr(sta, "teacher_bounds", teacher_bounds)
    spy = _Spy(monkeypatch, sc.id)

    result = run_v2_optimisation_guarded(sc.id, mode="current", **RUN)

    assert result["safety_blocked"] is False
    assert len(bounds_seen) == 1
    if not policy_on:
        assert spy.optimise_kwargs.keys().isdisjoint({"day_off_anchor", "teacher_swap"})
        assert [kwargs for _in, kwargs, _out in spy.recomputes] == [
            {"bounds": bounds_seen[0], "gate": spy.recomputes[0][1]["gate"]}
        ]
        assert spy.search == [None] and all(p is None for _pin, p in spy.compaction)
        assert "day_off_policy" not in result and "day_off_policy_fallback" not in result
        assert "day_off_policy" not in result["section_teachers"]["final"]
        assert "day_off_policy" not in result["instructor_schedule_metric"]
        assert "after_search" not in result["section_teachers"]
    else:
        policy = DayOffPolicy.from_dict(result["day_off_policy"])
        assert spy.optimise_kwargs["day_off_anchor"] == policy.anchor
        assert spy.search == [policy] and all(p == policy for _pin, p in spy.compaction)
        assert result["instructor_schedule_metric"]["day_off_policy"]["evidence_complete"]
        assert result["section_teachers"]["final"]["day_off_policy"] is True


# ── W8: Generate judges from its winner; the placers keep their own rules ─


@pytest.mark.django_db
def test_w8_generate_is_judged_from_its_winner_and_its_placers_are_left_alone(
    flags, monkeypatch
) -> None:
    from core.services import (
        timetable_autoplace,
        timetable_local_search_v2,
        timetable_optimizer_v2,
        timetable_required_sections,
    )
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded
    from tests.test_one_teacher_flag_parity import build_corpus

    sc = build_corpus()
    seen: dict = {"place": [], "search": [], "reinstate": [], "final": []}
    real_place = timetable_autoplace.auto_place_scenario
    real_search = timetable_local_search_v2.diagnostic_driven_local_search
    real_reinstate = timetable_required_sections.maybe_reinstate_missing_required_sections
    real_recompute = timetable_optimizer_v2._final_teacher_recompute

    def place(*args, **kwargs):
        seen["place"].append(current_day_off_policy())
        return real_place(*args, **kwargs)

    def search(**kwargs):
        seen["search"].append(current_day_off_policy())
        return real_search(**kwargs)

    def reinstate(*args, **kwargs):
        seen["reinstate"].append(current_day_off_policy())
        return real_reinstate(*args, **kwargs)

    def recompute(scenario_id, in_force, **kwargs):
        seen["final"].append(kwargs)
        return real_recompute(scenario_id, in_force, **kwargs)

    monkeypatch.setattr(timetable_autoplace, "auto_place_scenario", place)
    monkeypatch.setattr(timetable_local_search_v2, "diagnostic_driven_local_search", search)
    monkeypatch.setattr(
        timetable_required_sections, "maybe_reinstate_missing_required_sections", reinstate
    )
    monkeypatch.setattr(timetable_optimizer_v2, "_final_teacher_recompute", recompute)

    result = run_v2_optimisation_guarded(sc.id, mode="full", strategies=["compact"], **RUN)

    assert result["safety_blocked"] is False
    policy = DayOffPolicy.from_dict(result["day_off_policy"])
    assert policy is not None and policy.source == "generate_winner"
    assert policy.score == tuple(result["best_score"])
    assert seen["search"] == [policy]
    assert seen["place"] and all(p is None for p in seen["place"])
    assert seen["reinstate"] == [None]
    assert [kwargs["policy"] for kwargs in seen["final"]] == [policy]
    assert result["section_teachers"]["final"]["day_off_policy"] is True
    assert result["day_off_policy"]["audit"]["blocking"] is False
    assert current_day_off_policy() is None  # the scope ended with the run

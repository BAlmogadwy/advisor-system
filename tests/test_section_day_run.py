"""``TIMETABLE_SECTION_DAY_SPREAD``, the stronger rule: no three-meeting section on
three consecutive days (owner, 2026-10-25).

A section with exactly three meetings (no meeting policy) must not sit on SUN-MON-TUE,
MON-TUE-WED or TUE-WED-THU. It is a guard, not a soft cost:

- the greedy placer ranks such an option behind every other (chosen only when NO other
  option exists, so a section is never left unplaced);
- every later stage that moves meetings (local search v2, chains, simulated annealing,
  the load balancer, the CP-SAT polisher, secondary recovery, instructor compaction)
  rejects a move or pass that CREATES a new run, while a run that already exists may
  stay or improve;
- the instructor cap / clash repairs fix HARD violations, so they prefer a run-free
  relocation and fall back to one that makes a run when nothing else repairs it;
- both CP-SAT builders add a prohibitive (not infinite) penalty per three-day window.

Manual moves and validation never read the flag (see ``test_section_day_spread``).
Each stage test has a flag-OFF control proving the move it rejects is otherwise taken.
"""

from __future__ import annotations

import pytest

from core.models import DeliveryBoard, SectionPlacement
from core.services import timetable_constraints as constraints
from core.services import timetable_local_search_chains as chains
from core.services import timetable_local_search_v2 as local_search
from core.services.timetable_assignment_models import (
    CanonicalPattern,
    RiskTier,
    SectionMeeting,
    SectionState,
    StudentProfile,
    TimetableMove,
)
from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
from tests.test_one_teacher_flag_parity import build_corpus
from tests.test_section_day_spread import (
    PRODUCTION_FLAGS,
    _days_by_section,
    _tiny_board,
    make_capture_models,
)


@pytest.fixture
def flag_off(settings):
    for name, value in PRODUCTION_FLAGS.items():
        setattr(settings, name, value)
    settings.TIMETABLE_SECTION_DAY_SPREAD = False
    return settings


@pytest.fixture
def flag_on(flag_off):
    flag_off.TIMETABLE_SECTION_DAY_SPREAD = True
    flag_off.TIMETABLE_SECTION_DAY_SPREAD_WEIGHT = 40
    return flag_off


@pytest.fixture
def capture_models(monkeypatch):
    return make_capture_models(monkeypatch)


SUN, MON, TUE, WED, THU = range(5)
DAY_NAMES = ["SUN", "MON", "TUE", "WED", "THU"]


def _is_run(days) -> bool:
    return constraints.days_form_day_run(days)


# ── the pure helpers ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "days",
    [(0, 1, 2), (1, 2, 3), (2, 3, 4), (4, 2, 3), ("SUN", "MON", "TUE"), ("tue", "wed", "thu")],
)
def test_three_consecutive_weekdays_are_a_run(days) -> None:
    assert constraints.days_form_day_run(days) is True


@pytest.mark.parametrize(
    "days",
    [
        (0, 2, 4),  # fully spread
        (0, 1, 3),  # two and one
        (0, 1, 4),
        (0, 2, 3),
        (0, 1),  # two meetings
        (0, 1, 2, 3),  # four meetings
        (1, 1, 2),  # a repeated day is not three days
        (),
        ("SUN", "MON", "FUNDAY"),
    ],
)
def test_anything_else_is_not_a_run(days) -> None:
    assert constraints.days_form_day_run(days) is False


def _state(code: str, days, section_id: str | None = None) -> SectionState:
    return SectionState(
        section_id=section_id or f"{code}_S1",
        course_code=code,
        meetings=[SectionMeeting(d, 540, 615) for d in days],
        max_capacity=30,
        reserve_capacity=0,
    )


def test_move_introduces_day_run_only_for_a_new_run() -> None:
    board = {"AI101_S1": _state("AI101", (0, 1, 2))}
    spread_before = {"AI101_S1": [SectionMeeting(d, 540, 615) for d in (0, 2, 4)]}
    run_before = {"AI101_S1": [SectionMeeting(d, 540, 615) for d in (2, 3, 4)]}

    assert constraints.move_introduces_day_run(board, spread_before) is True
    # A run the section already had may stay or move to another run.
    assert constraints.move_introduces_day_run(board, run_before) is False
    # Unknown section ids are ignored.
    assert constraints.move_introduces_day_run(board, {"GHOST": spread_before["AI101_S1"]}) is False


def test_move_introduces_day_run_ignores_meeting_policy_and_other_shapes() -> None:
    eng = {"ENG101_S1": _state("ENG101", (0, 1, 2), "ENG101_S1")}
    before = {"ENG101_S1": [SectionMeeting(d, 540, 615) for d in (0, 2, 4)]}
    assert constraints.move_introduces_day_run(eng, before) is False  # ENG fixes its days

    two = {"AI101_S1": _state("AI101", (0, 1))}
    two_before = {"AI101_S1": [SectionMeeting(d, 540, 615) for d in (0, 2)]}
    assert constraints.move_introduces_day_run(two, two_before) is False


def test_before_meetings_keep_the_first_snapshot_of_a_section() -> None:
    from types import SimpleNamespace

    first = SimpleNamespace(section_id="A", old_meetings=[SectionMeeting(0, 540, 615)])
    second = SimpleNamespace(section_id="A", old_meetings=[SectionMeeting(3, 540, 615)])
    other = SimpleNamespace(section_id="B", old_meetings=[SectionMeeting(1, 540, 615)])

    before = constraints.before_meetings_of([first, second, other])

    assert before["A"][0].day == 0
    assert set(before) == {"A", "B"}


# ── greedy placer ─────────────────────────────────────────────────────


def _place_with_pressure(monkeypatch, seed: int, *, blocked_days=(), guard: bool = True) -> dict:
    """Place a 4-credit course while the soft term REWARDS a run, so only the guard
    can keep the section off three consecutive days."""
    from core.services import timetable_autoplace as autoplace
    from core.services.timetable_run_seed import seeded_timetable_run

    def reward_runs(option, weight):
        days = [m["day"] for m in option]
        return -1000 if len(option) == 3 and _is_run(days) else 0

    monkeypatch.setattr(autoplace, "_section_day_spread_penalty", reward_runs)
    if not guard:
        monkeypatch.setattr(autoplace, "DAY_RUN_GUARD_RANK", 0)

    _place_with_pressure.calls += 1
    scenario = _tiny_board(4, name=f"day run #{_place_with_pressure.calls}")
    if blocked_days:
        slots = autoplace.placeable_slots(autoplace.DEFAULT_SLOTS) + autoplace.placeable_slots(
            autoplace.DEFAULT_LAB_SLOTS
        )
        scenario.blocked_slots = [
            {"day": day, "start": slot["start"]} for day in blocked_days for slot in slots
        ]
        scenario.save(update_fields=["blocked_slots"])
    with seeded_timetable_run(seed):
        autoplace.auto_place_scenario(scenario.id, strategy="compact")
    return _days_by_section(scenario)


_place_with_pressure.calls = 0


@pytest.mark.django_db
def test_flag_on_the_greedy_placer_never_picks_a_run_when_another_option_exists(
    flag_on, monkeypatch
) -> None:
    for seed in range(6):
        days = _place_with_pressure(monkeypatch, seed)
        assert days and all(len(v) == 3 and not _is_run(v) for v in days.values()), (seed, days)


@pytest.mark.django_db
def test_flag_on_without_the_guard_the_same_pressure_does_pick_a_run(flag_on, monkeypatch) -> None:
    """The control: the rewarded run wins once the guard rank is removed, so the test
    above passes because of the guard and not because of the board."""
    placed = [_place_with_pressure(monkeypatch, seed, guard=False) for seed in range(3)]

    assert all(_is_run(v) for days in placed for v in days.values())


@pytest.mark.django_db
def test_flag_off_the_same_pressure_picks_a_run(flag_off, monkeypatch) -> None:
    placed = [_place_with_pressure(monkeypatch, seed) for seed in range(3)]

    assert all(_is_run(v) for days in placed for v in days.values())


@pytest.mark.django_db
def test_flag_on_the_greedy_placer_still_places_a_section_that_only_fits_in_a_run(
    flag_on, monkeypatch
) -> None:
    """With WED and THU blocked the only three-day option is SUN-MON-TUE: the guard
    ranks it last, it does not forbid it, so the section is placed, not dropped."""
    days = _place_with_pressure(monkeypatch, 3, blocked_days=("WED", "THU"))

    assert days == {"S1": ["MON", "SUN", "TUE"]}


@pytest.mark.django_db
def test_flag_on_two_and_four_meeting_sections_are_not_guarded(flag_on, monkeypatch) -> None:
    from core.services import timetable_autoplace as autoplace

    assert autoplace._option_is_day_run([{"day": d} for d in ("SUN", "MON", "TUE")]) is True
    assert autoplace._option_is_day_run([{"day": d} for d in ("SUN", "MON")]) is False
    assert autoplace._option_is_day_run([{"day": d} for d in ("SUN", "MON", "TUE", "WED")]) is False


# ── in-memory stages: local search v2, chains, secondary recovery ──────


def _pattern(name: str, days, start: int = 540, family: str = "normal") -> CanonicalPattern:
    return CanonicalPattern(
        pattern_id=name,
        signature=name,
        meetings=[SectionMeeting(d, start, start + 75) for d in days],
        pattern_family=family,
        duration_permutation="75",
        is_lab_mixed=False,
        meeting_count=len(days),
        days_used=frozenset(days),
        slot_fingerprint=name,
    )


def _section(code: str, pattern: CanonicalPattern) -> SectionState:
    return SectionState(
        section_id=code,
        course_code=code,
        meetings=list(pattern.meetings),
        max_capacity=30,
        reserve_capacity=0,
        pattern_id=pattern.pattern_id,
        pattern_family=pattern.pattern_family,
    )


def _profile(sid: str, courses: list[str]) -> StudentProfile:
    return StudentProfile(sid, "AI", courses, RiskTier.C, 0)


A_SPREAD = _pattern("a_spread", (SUN, TUE, THU))
A_TWO_ONE = _pattern("a_two_one", (SUN, MON, WED))
A_RUN = _pattern("a_run", (SUN, MON, TUE))
A_RUN_LATE = _pattern("a_run_late", (TUE, WED, THU))
B_BLOCK = _pattern("b_block", (WED, THU), family="b")
B_SPREAD_CLASH = _pattern("b_clash", (SUN, TUE), family="b")


def _clash_board(start: CanonicalPattern, blocker: CanonicalPattern, catalog_a):
    """Course A (three meetings) clashes with the locked course B at every pattern
    except the ones that avoid B's two days; ``a_run`` / ``a_run_late`` avoid it."""
    sections = {"A": _section("A", start), "B": _section("B", blocker)}
    catalog = {"normal": list(catalog_a), "b": [blocker]}
    profiles = {"s": _profile("s", ["A", "B"])}
    baseline = evaluate_generated_timetable_candidate(
        "baseline", list(sections.values()), profiles, {}
    )
    return sections, catalog, profiles, baseline


def _a_days(sections) -> tuple[int, ...]:
    return tuple(sorted(m.day for m in sections["A"].meetings))


def test_local_search_v2_rejects_a_move_that_creates_a_run(settings) -> None:
    settings.TIMETABLE_SECTION_DAY_SPREAD = True
    sections, catalog, profiles, baseline = _clash_board(
        A_SPREAD, B_BLOCK, [A_SPREAD, A_TWO_ONE, A_RUN]
    )
    assert baseline.lexicographic_score  # A clashes with B on THU

    result = local_search.diagnostic_driven_local_search(
        baseline, sections, catalog, profiles, {}, locked_section_ids={"B"}, max_iterations=1
    )

    assert result is baseline
    assert sections["A"].pattern_id == "a_spread"


def test_local_search_v2_flag_off_control_takes_the_run(settings) -> None:
    settings.TIMETABLE_SECTION_DAY_SPREAD = False
    sections, catalog, profiles, baseline = _clash_board(
        A_SPREAD, B_BLOCK, [A_SPREAD, A_TWO_ONE, A_RUN]
    )

    result = local_search.diagnostic_driven_local_search(
        baseline, sections, catalog, profiles, {}, locked_section_ids={"B"}, max_iterations=1
    )

    assert result is not baseline
    assert sections["A"].pattern_id == "a_run"
    assert _is_run(_a_days(sections))


def test_local_search_v2_lets_an_existing_run_move_to_another_run(settings) -> None:
    settings.TIMETABLE_SECTION_DAY_SPREAD = True
    sections, catalog, profiles, baseline = _clash_board(
        A_RUN_LATE, B_BLOCK, [A_SPREAD, A_TWO_ONE, A_RUN, A_RUN_LATE]
    )

    result = local_search.diagnostic_driven_local_search(
        baseline, sections, catalog, profiles, {}, locked_section_ids={"B"}, max_iterations=1
    )

    assert result is not baseline
    assert sections["A"].pattern_id == "a_run"  # it was a run, so a run may stay a run


def _chain_board():
    """A is spread and clashes with the locked B; C is an unrelated mover. The one
    chain under test is (A -> run, C -> elsewhere): it resolves the clash."""
    c_now, c_alt = _pattern("c_now", (MON,), 900, "c"), _pattern("c_alt", (MON,), 960, "c")
    sections = {
        "A": _section("A", A_SPREAD),
        "B": _section("B", B_BLOCK),
        "C": _section("C", c_now),
    }
    catalog = {"normal": [A_SPREAD, A_TWO_ONE, A_RUN], "b": [B_BLOCK], "c": [c_now, c_alt]}
    profiles = {"s": _profile("s", ["A", "B", "C"])}
    baseline = evaluate_generated_timetable_candidate(
        "baseline", list(sections.values()), profiles, {}
    )
    return sections, catalog, profiles, baseline


A_TO_RUN = TimetableMove("repattern", "A", "a_spread", "a_run")
C_AWAY = TimetableMove("repattern", "C", "c_now", "c_alt")


def _run_chains(monkeypatch, sections, catalog, profiles, baseline, chain_list):
    """Run chain search on exactly ``chain_list`` (the generator is replaced, so the
    test controls which chain is on offer; the loop under test is the real one)."""
    monkeypatch.setattr(chains, "generate_chain_2_moves", lambda *a, **k: list(chain_list))
    return chains.chain_local_search(
        baseline, sections, catalog, profiles, {}, locked_section_ids={"B"}, max_iterations=1
    )


def test_chain_search_rejects_a_chain_that_creates_a_run(settings, monkeypatch) -> None:
    settings.TIMETABLE_SECTION_DAY_SPREAD = True
    sections, catalog, profiles, baseline = _chain_board()

    result = _run_chains(
        monkeypatch, sections, catalog, profiles, baseline, [chains.ChainMove(A_TO_RUN, C_AWAY)]
    )

    assert result is baseline
    assert sections["A"].pattern_id == "a_spread"
    assert sections["C"].pattern_id == "c_now"  # rolled back, not half applied


def test_chain_search_flag_off_control_takes_the_run(settings, monkeypatch) -> None:
    settings.TIMETABLE_SECTION_DAY_SPREAD = False
    sections, catalog, profiles, baseline = _chain_board()

    result = _run_chains(
        monkeypatch, sections, catalog, profiles, baseline, [chains.ChainMove(A_TO_RUN, C_AWAY)]
    )

    assert result is not baseline
    assert sections["A"].pattern_id == "a_run"


def test_chain_rejection_uses_the_first_snapshot_when_both_legs_move_one_section(
    settings, monkeypatch
) -> None:
    """A chain whose two legs both move A starts from A's ORIGINAL days: spread ->
    two-and-one -> run is still a new run."""
    settings.TIMETABLE_SECTION_DAY_SPREAD = True
    sections, catalog, profiles, baseline = _chain_board()
    leg_a = TimetableMove("repattern", "A", "a_spread", "a_two_one")
    leg_b = TimetableMove("repattern", "A", "a_two_one", "a_run")

    result = _run_chains(
        monkeypatch, sections, catalog, profiles, baseline, [chains.ChainMove(leg_a, leg_b)]
    )

    assert result is baseline
    assert sections["A"].pattern_id == "a_spread"


def _recovery(sections, patterns):
    from core.services.timetable_secondary_recovery import recover_secondary_quality

    profiles = {"s": StudentProfile("s", "CS", ["CORE", "SOFT"], RiskTier.C, 0)}
    tiers = {"CORE": "T1", "SOFT": "T3"}
    baseline = evaluate_generated_timetable_candidate(
        "before", list(sections.values()), profiles, {}, None, tiers
    )
    return recover_secondary_quality(
        current_eval=baseline,
        sections_by_id=sections,
        pattern_catalog={"normal": patterns},
        student_profiles=profiles,
        course_rigidity={},
        room_evaluator=lambda states: {
            "unassigned_count": 0,
            "unassigned_section_ids": [],
            "proven_optimal": True,
        },
        course_tiers=tiers,
        locked_section_ids={"CORE"},
        time_limit_seconds=10,
        max_iterations=3,
    )["report"]


def _recovery_board():
    """SOFT clashes with the locked CORE on SUN 09:00; the only pattern that frees it
    is a run (SUN 10:30, MON, TUE)."""
    core = _pattern("core_p", (SUN,), 540, "normal")
    soft_clash = _pattern("soft_clash", (SUN, TUE, THU), 540)
    soft_run = _pattern("soft_run", (SUN, MON, TUE), 660)
    sections = {"CORE": _section("CORE", core), "SOFT": _section("SOFT", soft_clash)}
    return sections, [core, soft_clash, soft_run]


@pytest.mark.parametrize(("flag", "applied"), [(True, False), (False, True)])
def test_secondary_recovery_rejects_a_move_that_creates_a_run(settings, flag, applied) -> None:
    settings.TIMETABLE_TIERED_OBJECTIVE_ENABLED = True
    settings.TIMETABLE_SECTION_DAY_SPREAD = flag
    sections, patterns = _recovery_board()

    report = _recovery(sections, patterns)

    assert bool(report["applied"]) is applied
    if flag:
        assert report["structural_rejections"] > 0


# ── simulated annealing and the load balancer ──────────────────────────


def _placed_four_credit_board(name: str):
    """A 4-credit course placed SUN-TUE-THU by the greedy placer (flag on), so the
    stage under test starts from a board with no run."""
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_run_seed import seeded_timetable_run

    scenario = _tiny_board(4, name=name)
    with seeded_timetable_run(3):
        auto_place_scenario(scenario.id, strategy="compact")
    board = DeliveryBoard.objects.get(scenario=scenario)
    return scenario, board


def _reward_runs(schedule, sections, *args, **kwargs) -> float:
    """-1000 when any section sits on three consecutive days, else 0: the stage's
    cost function itself prefers the run, so only the reject can keep it away."""
    for meetings in schedule.values():
        if _is_run([m["day"] for m in meetings]):
            return -1000.0
    return 0.0


@pytest.mark.django_db
@pytest.mark.parametrize("flag", [True, False], ids=["flag-on", "flag-off-control"])
def test_simulated_annealing_never_creates_a_run(settings, monkeypatch, flag) -> None:
    from core.services import timetable_local_search as sa

    settings.TIMETABLE_SECTION_DAY_SPREAD = False
    _scenario, board = _placed_four_credit_board(f"sa {flag}")
    settings.TIMETABLE_SECTION_DAY_SPREAD = flag
    monkeypatch.setattr(sa, "_compute_cost", _reward_runs)

    result = sa.optimize_board(board.id, max_seconds=1.5)

    assert result["status"] == "optimized"
    final_days = [[m["day"] for m in meetings] for meetings in result["schedule"].values()]
    if flag:
        assert not any(_is_run(days) for days in final_days), final_days
    else:
        assert any(_is_run(days) for days in final_days), final_days


@pytest.mark.django_db
@pytest.mark.parametrize("flag", [True, False], ids=["flag-on", "flag-off-control"])
def test_load_balancer_never_creates_a_run(settings, monkeypatch, flag) -> None:
    from core.services import timetable_load_balanced as balancer

    settings.TIMETABLE_SECTION_DAY_SPREAD = False
    _scenario, board = _placed_four_credit_board(f"balance {flag}")
    settings.TIMETABLE_SECTION_DAY_SPREAD = flag
    monkeypatch.setattr(balancer, "_compute_balance_score", _reward_runs)

    result = balancer.rebalance_board(board.id, max_seconds=1.5)

    assert result["status"] == "optimized"
    final_days = [[m["day"] for m in meetings] for meetings in result["schedule"].values()]
    if flag:
        assert not any(_is_run(days) for days in final_days), final_days
    else:
        assert any(_is_run(days) for days in final_days), final_days


@pytest.mark.django_db
@pytest.mark.parametrize("stage", ["annealing", "balancer"])
def test_a_section_that_is_already_a_run_may_move_to_another_run(
    settings, monkeypatch, stage
) -> None:
    """Only a move INTO a run from a non-run is refused. The section starts on
    SUN-MON-TUE and the cost rewards MON-TUE-WED while making any non-run
    prohibitive: one relocation (SUN -> WED) gets there, run to run, and must be
    allowed."""
    from core.services import timetable_load_balanced as balancer
    from core.services import timetable_local_search as sa

    settings.TIMETABLE_SECTION_DAY_SPREAD = False
    _scenario, board = _placed_four_credit_board(f"existing run {stage}")
    for placement, day in zip(
        SectionPlacement.objects.filter(board=board).order_by("id"),
        ("SUN", "MON", "TUE"),
        strict=True,
    ):
        placement.day = day
        placement.save(update_fields=["day"])
    settings.TIMETABLE_SECTION_DAY_SPREAD = True

    def reward_mon_tue_wed(schedule, sections, *args, **kwargs) -> float:
        """MON-TUE-WED is best, any other run is neutral, leaving the runs is
        prohibitive: the walk stays among runs, so only run-to-run moves count."""
        days = {m["day"] for ms in schedule.values() for m in ms}
        if days == {"MON", "TUE", "WED"}:
            return -1000.0
        return 0.0 if _is_run(sorted(days, key=DAY_NAMES.index)) else 100000.0

    if stage == "annealing":
        monkeypatch.setattr(sa, "_compute_cost", reward_mon_tue_wed)
        result = sa.optimize_board(board.id, max_seconds=1.5)
    else:
        monkeypatch.setattr(balancer, "_compute_balance_score", reward_mon_tue_wed)
        result = balancer.rebalance_board(board.id, max_seconds=1.5)

    final_days = [{m["day"] for m in meetings} for meetings in result["schedule"].values()]
    assert {"MON", "TUE", "WED"} in final_days, final_days


# ── CP-SAT polisher ────────────────────────────────────────────────────


def _polish(a_days=(SUN, TUE, THU), b_days=(WED, THU)):
    """A clashes with the locked B; WED and THU are blocked in the polisher's slot
    domain, so EVERY polished A is on SUN/MON/TUE: a run."""
    from core.models import TimetableScenario
    from core.services.timetable_autoplace import DEFAULT_LAB_SLOTS, DEFAULT_SLOTS, placeable_slots
    from core.services.timetable_cpsat_polisher import polish_scenario_with_cpsat

    scenario = TimetableScenario.objects.create(academic_year="1448", term="2", name="polish run")
    DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)
    slots = placeable_slots(DEFAULT_SLOTS) + placeable_slots(DEFAULT_LAB_SLOTS)
    scenario.blocked_slots = [
        {"day": DAY_NAMES[day], "start": slot["start"]} for day in (WED, THU) for slot in slots
    ]
    scenario.save(update_fields=["blocked_slots"])
    a = SectionState(
        "A_S1",
        "A",
        [SectionMeeting(d, 540, 615) for d in a_days],
        30,
        0,
        pattern_family="ONCAMPUS_LEC_75",
        pattern_id="a_before",
    )
    b = SectionState(
        "B_S1",
        "B",
        [SectionMeeting(d, 540, 615) for d in b_days],
        30,
        0,
        pattern_family="ONCAMPUS_LEC_75",
        pattern_id="b_block",
    )
    profiles = {"s": _profile("s", ["A", "B"])}
    baseline = evaluate_generated_timetable_candidate("current", [a, b], profiles, {})
    return polish_scenario_with_cpsat(
        scenario.id,
        [a, b],
        profiles,
        {},
        baseline,
        time_limit_seconds=5,
        locked_section_ids={"B_S1"},
    )


@pytest.mark.django_db
def test_polisher_flag_off_control_accepts_the_run(settings) -> None:
    settings.TIMETABLE_SECTION_DAY_SPREAD = False

    result = _polish()

    assert result is not None
    polished = next(s for s in result["improved_sections"] if s.course_code == "A")
    assert _is_run([m.day for m in polished.meetings])


@pytest.mark.django_db
def test_polisher_rejects_a_pass_that_creates_a_run(settings) -> None:
    settings.TIMETABLE_SECTION_DAY_SPREAD = True

    assert _polish() is None


@pytest.mark.django_db
def test_polisher_keeps_a_pass_for_a_section_that_was_already_a_run(settings) -> None:
    """A starts on SUN-MON-TUE (clashing with B on MON) and every polished A is a run
    too (WED and THU are blocked): a run that already existed may stay, so the pass
    is kept."""
    settings.TIMETABLE_SECTION_DAY_SPREAD = True

    result = _polish(a_days=(SUN, MON, TUE), b_days=(MON,))

    assert result is not None
    polished = next(s for s in result["improved_sections"] if s.course_code == "A")
    assert _is_run([m.day for m in polished.meetings])


# ── CP-SAT strategies ──────────────────────────────────────────────────


@pytest.mark.django_db
def test_flag_on_cpsat_models_penalise_each_three_day_window_of_a_three_meeting_section(
    flag_on, capture_models
) -> None:
    import re

    for name, model in capture_models(build_corpus(11)).items():
        proto = model.proto
        names = {i: v.name for i, v in enumerate(proto.variables)}
        penalised: dict[int, set[int]] = {}
        for var, coeff in zip(proto.objective.vars, proto.objective.coeffs, strict=True):
            match = re.fullmatch(r"day_run_(\d+)_(\d)", names[var])
            if match:
                assert coeff == 10**6, name
                penalised.setdefault(int(match.group(1)), set()).add(int(match.group(2)))
        assert penalised, name
        # Exactly the three windows SUN-TUE, MON-WED, TUE-THU, per guarded section.
        assert all(windows == {0, 1, 2} for windows in penalised.values()), name


@pytest.mark.django_db
def test_flag_off_cpsat_models_have_no_day_run_variables(flag_off, capture_models) -> None:
    for name, model in capture_models(build_corpus(11)).items():
        assert not any(v.name.startswith("day_run_") for v in model.proto.variables), name


def _solve(builder: str, scenario, *, blocked_days=()) -> dict:
    from core.services import timetable_solver
    from core.services.timetable_autoplace import DEFAULT_LAB_SLOTS, DEFAULT_SLOTS, placeable_slots

    if blocked_days:
        slots = placeable_slots(DEFAULT_SLOTS) + placeable_slots(DEFAULT_LAB_SLOTS)
        scenario.blocked_slots = [
            {"day": DAY_NAMES[day], "start": slot["start"]}
            for day in blocked_days
            for slot in slots
        ]
        scenario.save(update_fields=["blocked_slots"])
    board = DeliveryBoard.objects.get(scenario=scenario)
    if builder == "solve_board":
        return timetable_solver.solve_board(board.id, time_limit_seconds=10)
    return timetable_solver.solve_board_with_hints(board.id, [], time_limit_seconds=10)


def _solved_days(result: dict) -> list[list[str]]:
    return [[m["day"] for m in p["meetings"]] for p in result["placements"]]


@pytest.fixture
def consecutive_days_rewarded(monkeypatch):
    """Make the per-pair term REWARD consecutive days, so the run is the cheapest
    arrangement and only the day-run penalty can keep the solver off it."""
    from core.services import timetable_solver

    monkeypatch.setattr(timetable_solver, "_consecutive_day_weight", lambda sec, weight: -50)


@pytest.mark.django_db
@pytest.mark.parametrize("builder", ["solve_board", "solve_board_with_hints"])
def test_flag_on_cpsat_keeps_a_three_meeting_section_off_a_run(
    flag_on, consecutive_days_rewarded, builder
) -> None:
    result = _solve(builder, _tiny_board(4, name=f"cpsat run {builder}"))

    assert result["placed"] == 1
    assert not any(_is_run(days) for days in _solved_days(result))


@pytest.mark.django_db
@pytest.mark.parametrize("builder", ["solve_board", "solve_board_with_hints"])
def test_flag_off_cpsat_control_takes_the_rewarded_run(
    flag_off, consecutive_days_rewarded, builder
) -> None:
    result = _solve(builder, _tiny_board(4, name=f"cpsat control {builder}"))

    assert result["placed"] == 1
    assert all(_is_run(days) for days in _solved_days(result))


@pytest.mark.django_db
@pytest.mark.parametrize("builder", ["solve_board", "solve_board_with_hints"])
def test_flag_on_cpsat_still_places_a_section_that_only_fits_in_a_run(flag_on, builder) -> None:
    """A penalty, not a hard constraint: with WED and THU blocked the section is
    placed on SUN-MON-TUE instead of the board coming back infeasible."""
    result = _solve(
        builder, _tiny_board(4, name=f"cpsat forced {builder}"), blocked_days=(WED, THU)
    )

    assert result["status"] in ("optimal", "feasible")
    assert result["placed"] == 1
    assert sorted(_solved_days(result)[0]) == ["MON", "SUN", "TUE"]


# ── instructor cap / clash repairs and compaction (DB boards) ───────────

_SLOTS = [
    {"start": "09:00", "end": "10:15"},
    {"start": "10:30", "end": "11:45"},
    {"start": "13:00", "end": "14:15"},
    {"start": "14:30", "end": "15:45"},
]


def _instructor_board(name: str, sessions, *, blocked=(), slots=None, instructor="Dr Run"):
    """One instructor, one scenario. ``sessions`` is ``[(course, section_label,
    [(day, start, end), ...], locked?)]``; each course gets its own TermSection
    (created in the order given, so an earlier course has the lower placement id) and
    the primary instructor is fanned onto it."""
    from core.models import (
        CourseInstructor,
        Instructor,
        Room,
        TermSection,
        TermSectionMeeting,
        TimetableScenario,
    )
    from core.services.course_instructor_assignment import apply_primary_instructor
    from core.services.timetable_pr4_instructor import normalise_instructor

    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name=name,
        gender="M",
        programs=["AI"],
        slot_config=slots or _SLOTS,
        lab_slot_config=[],
        blocked_slots=[{"day": day, "start": s["start"]} for day, s in blocked],
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="T1", nominal_term=1, program="AI"
    )
    Room.objects.get_or_create(
        room_code="R1", defaults={"department": "AI", "capacity": 50, "room_type": "lecture"}
    )
    instr = Instructor.objects.create(
        full_name=instructor, normalised_name=normalise_instructor(instructor)
    )
    for code, section, meetings, *rest in sessions:
        CourseInstructor.objects.create(
            program="AI", course_code=code, section="M", instructor=instr, role="primary"
        )
        ts = TermSection.objects.create(
            scenario=scenario,
            course_key=code,
            section=section,
            course_code=code,
            course_number=code,
            course_name=code,
            available_capacity=30,
            source_tag="day_run_test",
        )
        for day, start, end in meetings:
            TermSectionMeeting.objects.create(
                term_section=ts, day=day, start_time=start, end_time=end, room="", instructor=""
            )
            SectionPlacement.objects.create(
                board=board,
                term_section=ts,
                day=day,
                start_time=start,
                end_time=end,
                room="R1",
                is_locked=bool(rest and rest[0]),
            )
        apply_primary_instructor(ts, scenario, board, ts.course_code)
    return scenario


def _course_days(scenario, course: str) -> list[str]:
    order = {d: i for i, d in enumerate(DAY_NAMES)}
    return sorted(
        SectionPlacement.objects.filter(
            board__scenario=scenario, term_section__course_code=course
        ).values_list("day", flat=True),
        key=order.get,
    )


def _clash_scenario(name: str, *, blocked=()):
    """C2 (one meeting) and C1 (three meetings, SUN-TUE-THU) both want THU 09:00 for
    the same instructor. C2 is created first so C1's THU session is the one moved.
    From THU, MON gives SUN-MON-TUE (a run) and WED gives SUN-TUE-WED (not)."""
    nine = ("09:00", "10:15")
    return _instructor_board(
        name,
        [
            ("C2", "S1", [("THU", *nine)]),
            ("C1", "S1", [("SUN", *nine), ("TUE", *nine), ("THU", *nine)]),
        ],
        blocked=blocked,
    )


CLASH_FLAGS = {
    "TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED": True,
    "TIMETABLE_INSTRUCTOR_LINKS_ENABLED": True,
    "TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED": False,
}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("flag", "expected"),
    [(True, ["SUN", "TUE", "WED"]), (False, ["SUN", "MON", "TUE"])],
    ids=["flag-on", "flag-off-control"],
)
def test_clash_repair_prefers_a_relocation_that_makes_no_run(settings, flag, expected) -> None:
    from core.services.timetable_instructor_cap_repair import repair_instructor_clashes

    for key, value in CLASH_FLAGS.items():
        setattr(settings, key, value)
    settings.TIMETABLE_SECTION_DAY_SPREAD = flag
    scenario = _clash_scenario(f"clash prefers {flag}")

    report = repair_instructor_clashes(scenario.id)

    assert report["remaining_clashes"] == 0
    assert [r["section"] for r in report["repaired"]] == ["C1 S1"]
    assert _course_days(scenario, "C1") == expected


@pytest.mark.django_db(transaction=True)
def test_clash_repair_still_repairs_when_only_a_run_remains(settings) -> None:
    """A hard repair must never be undone by the soft rule: with WED blocked the only
    clash-free day is MON (a run), and the session is relocated there, not left."""
    from core.services.timetable_instructor_cap_repair import repair_instructor_clashes

    for key, value in CLASH_FLAGS.items():
        setattr(settings, key, value)
    settings.TIMETABLE_SECTION_DAY_SPREAD = True
    scenario = _clash_scenario("clash forced", blocked=[("WED", s) for s in _SLOTS])

    report = repair_instructor_clashes(scenario.id)

    assert report["remaining_clashes"] == 0
    assert report["unplaced"] == []
    assert _course_days(scenario, "C1") == ["SUN", "MON", "TUE"]


# Back-to-back slots: a session placed next to another leaves the instructor no idle
# minute, so the instructor-first safe search accepts the move on its own merits.
_ADJACENT_SLOTS = [
    {"start": "09:00", "end": "10:15"},
    {"start": "10:15", "end": "11:30"},
    {"start": "11:30", "end": "12:45"},
]


def _safe_search_scenario(name: str):
    """The instructor already works MON and WED (C3, back to back with a moved C1) and
    THU is closed except for 09:00, so the coordinated safe search has exactly two
    zero-cost moves for C1's clashing THU session: MON (a run) and WED (not)."""
    nine, ten = ("09:00", "10:15"), ("10:15", "11:30")
    return _instructor_board(
        name,
        [
            ("C3", "S1", [("MON", *ten), ("WED", *ten)]),
            ("C2", "S1", [("THU", *nine)]),
            ("C1", "S1", [("SUN", *nine), ("TUE", *nine), ("THU", *nine)]),
        ],
        slots=_ADJACENT_SLOTS,
        blocked=[("THU", s) for s in _ADJACENT_SLOTS[1:]],
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("flag", "expected"),
    [(True, ["SUN", "TUE", "WED"]), (False, ["SUN", "MON", "TUE"])],
    ids=["flag-on", "flag-off-control"],
)
def test_instructor_first_safe_search_never_creates_a_run(settings, flag, expected) -> None:
    from core.services.timetable_instructor_cap_repair import repair_instructor_clashes

    for key, value in CLASH_FLAGS.items():
        setattr(settings, key, value)
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_SECTION_DAY_SPREAD = flag
    scenario = _safe_search_scenario(f"safe search {flag}")

    report = repair_instructor_clashes(scenario.id)

    assert [r["instructor_safe"] for r in report["repaired"]] == [True]  # via the safe search
    assert _course_days(scenario, "C1") == expected


def _overload_scenario(name: str, *, blocked=()):
    """THU carries four sessions of one instructor (cap 3): three single-meeting
    courses and C1's THU meeting, the latest, which is the one moved. C1 is on
    SUN-TUE-THU; from THU, MON gives SUN-MON-TUE (a run), WED gives SUN-TUE-WED (not)."""
    nine, mid, one, late = (
        ("09:00", "10:15"),
        ("10:30", "11:45"),
        ("13:00", "14:15"),
        ("14:30", "15:45"),
    )
    return _instructor_board(
        name,
        [
            ("C2", "S1", [("THU", *nine)]),
            ("C3", "S1", [("THU", *mid)]),
            ("C4", "S1", [("THU", *one)]),
            ("C1", "S1", [("SUN", *nine), ("TUE", *nine), ("THU", *late)]),
        ],
        blocked=blocked,
    )


CAP_FLAGS = {
    "TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED": True,
    "TIMETABLE_INSTRUCTOR_DAILY_CAP": 3,
    "TIMETABLE_INSTRUCTOR_LINKS_ENABLED": True,
    "TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED": True,
}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("flag", "expected"),
    [(True, ["SUN", "TUE", "WED"]), (False, ["SUN", "MON", "TUE"])],
    ids=["flag-on", "flag-off-control"],
)
def test_cap_repair_prefers_a_relocation_that_makes_no_run(settings, flag, expected) -> None:
    from core.services.timetable_instructor_cap_repair import repair_instructor_daily_overloads

    for key, value in CAP_FLAGS.items():
        setattr(settings, key, value)
    settings.TIMETABLE_SECTION_DAY_SPREAD = flag
    scenario = _overload_scenario(f"cap prefers {flag}")

    report = repair_instructor_daily_overloads(scenario.id)

    assert report["remaining_violations"] == 0
    assert [r["section"] for r in report["repaired"]] == ["C1 S1"]
    assert _course_days(scenario, "C1") == expected


@pytest.mark.django_db(transaction=True)
def test_cap_repair_relocates_into_a_run_rather_than_unplacing(settings) -> None:
    """The cap is hard: with WED blocked only MON is left, and the session is moved
    there (a run) instead of being dropped from the board."""
    from core.services.timetable_instructor_cap_repair import repair_instructor_daily_overloads

    for key, value in CAP_FLAGS.items():
        setattr(settings, key, value)
    settings.TIMETABLE_SECTION_DAY_SPREAD = True
    scenario = _overload_scenario("cap forced", blocked=[("WED", s) for s in _SLOTS])

    report = repair_instructor_daily_overloads(scenario.id)

    assert report["remaining_violations"] == 0
    assert report["unplaced"] == []
    assert _course_days(scenario, "C1") == ["SUN", "MON", "TUE"]


@pytest.fixture
def compaction_on(settings):
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_LINKS_ENABLED = True
    settings.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = False
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_MAX_ROUNDS = 4
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_TIME_BUDGET_SECONDS = 0
    return settings


def _compaction_scenario(name: str, *, blocked=()):
    """C2 (locked) sits on THU 13:00, 2h45 after C1's THU 09:00 session: a hole that
    vanishes if C1's THU session moves to a free day. From THU, MON gives SUN-MON-TUE
    (a run) and WED gives SUN-TUE-WED (not)."""
    nine, one = ("09:00", "10:15"), ("13:00", "14:15")
    return _instructor_board(
        name,
        [
            ("C2", "S1", [("THU", *one)], True),
            ("C1", "S1", [("SUN", *nine), ("TUE", *nine), ("THU", *nine)]),
        ],
        blocked=blocked,
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("flag", "moved_to"),
    [(True, "WED"), (False, "MON")],
    ids=["flag-on", "flag-off-control"],
)
def test_compaction_never_moves_a_section_into_a_run(compaction_on, flag, moved_to) -> None:
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    compaction_on.TIMETABLE_SECTION_DAY_SPREAD = flag
    scenario = _compaction_scenario(f"compaction {flag}")

    report = compact_instructor_schedules(scenario.id)

    assert report["search"]["moves_accepted"] >= 1
    days = _course_days(scenario, "C1")
    assert ("THU" not in days) and (moved_to in days), days
    assert _is_run([DAY_NAMES.index(d) for d in days]) is (not flag)


@pytest.mark.django_db(transaction=True)
def test_compaction_leaves_the_board_alone_when_only_a_run_would_help(compaction_on) -> None:
    """Compaction is optional, so unlike the hard repairs it just declines: with WED
    blocked the only way to close the hole by moving C1 is MON (a run)."""
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    compaction_on.TIMETABLE_SECTION_DAY_SPREAD = True
    scenario = _compaction_scenario("compaction forced", blocked=[("WED", s) for s in _SLOTS])

    compact_instructor_schedules(scenario.id)

    assert not _is_run([DAY_NAMES.index(d) for d in _course_days(scenario, "C1")])


# ── manual moves never see the rule ────────────────────────────────────


@pytest.mark.django_db
@pytest.mark.parametrize("flag", [True, False])
def test_a_manual_move_into_a_run_is_saved_with_or_without_the_flag(settings, flag) -> None:
    """The owner's rule: a manual move is never refused for a clash. The move below
    turns SUN-TUE-THU into SUN-MON-TUE; the endpoint saves it either way."""
    import json

    from django.contrib.auth.models import Group, User
    from django.test import Client

    from core.services.rbac import ROLE_SUPER_ADMIN

    settings.TIMETABLE_SECTION_DAY_SPREAD = True  # placed spread, whatever the flag below
    scenario, _board = _placed_four_credit_board(f"manual move {flag}")
    settings.TIMETABLE_SECTION_DAY_SPREAD = flag
    thursday = SectionPlacement.objects.get(board__scenario=scenario, day="THU")
    user = User.objects.create_user(username=f"day-run-admin-{flag}")
    user.groups.add(Group.objects.get_or_create(name=ROLE_SUPER_ADMIN)[0])
    client = Client()
    client.force_login(user)

    response = client.post(
        f"/ops/tw/placements/{thursday.id}/move/",
        json.dumps(
            {"day": "MON", "start_time": thursday.start_time, "end_time": thursday.end_time}
        ),
        content_type="application/json",
    )

    assert response.status_code == 200, response.content
    assert _course_days(scenario, "AI101") == ["SUN", "MON", "TUE"]


@pytest.mark.django_db(transaction=True)
def test_compaction_lets_a_section_that_is_already_a_run_move_to_another_run(
    compaction_on,
) -> None:
    """C1 starts on TUE-WED-THU (a run) and SUN is closed, so the only day that
    closes the THU hole is MON: MON-TUE-WED, run to run. A run that already exists
    may stay a run, so compaction makes the move."""
    from core.services.timetable_instructor_compaction import compact_instructor_schedules

    compaction_on.TIMETABLE_SECTION_DAY_SPREAD = True
    nine, one = ("09:00", "10:15"), ("13:00", "14:15")
    scenario = _instructor_board(
        "compaction existing run",
        [
            ("C2", "S1", [("THU", *one)], True),
            ("C1", "S1", [("TUE", *nine), ("WED", *nine), ("THU", *nine)]),
        ],
        blocked=[("SUN", s) for s in _SLOTS],
    )

    report = compact_instructor_schedules(scenario.id)

    assert report["search"]["moves_accepted"] >= 1
    assert _course_days(scenario, "C1") == ["MON", "TUE", "WED"]

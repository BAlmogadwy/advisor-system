"""``TIMETABLE_INSTRUCTOR_AVAILABILITY``: the stages that IMPROVE or REPAIR a board
(B1 review, fix plan decisions D-D, D-E, D-F, D-I, D-J, D-K and the D-P holes of
those stages).

- D-D: every stage that moves an already-placed section (LS-v2 moves and swaps,
  chain legs, the recovery kernel, the English bundle search) is STRICT: no
  meeting moves INTO a forbidden time, even when the section's count would fall;
  a forbidden meeting the section already had may stay where it is;
- D-E: the cap repair never unplaces a session because a rule kept another one;
- D-F: the polisher holds a ruled section fixed when a meeting has no legal
  option other than its current slot, so the rest of the board is polished;
- D-I: a rule on an eligible teacher the scenario never charges keeps the board
  identical and adds only report keys carrying zeros;
- D-J: a whole-board rollback restores (or drops) the side-band count;
- D-K: the registration-repair read and apply paths read the rules once.

Helpers come from ``tests/test_instructor_availability_engine.py`` (the slice's
main module). Every behavioural test has a control without the rule.
"""

from __future__ import annotations

import pytest

from core.models import SectionPlacement
from core.services import timetable_constraints as constraints
from core.services.instructor_availability import (
    EMPTY_AVAILABILITY,
    KIND_AVAILABLE_ONLY,
    KIND_UNAVAILABLE,
)
from core.services.timetable_assignment_models import (
    RiskTier,
    SectionMeeting,
    StudentProfile,
    TimetableMove,
)
from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
from tests._instructor_availability_fixture import AVAILABILITY_FLAGS
from tests.test_instructor_availability_engine import (
    AM,
    MON,
    NO_MORNINGS,
    PM,
    SUN,
    THU,
    TUE,
    WED,
    _av,
    _chain_search,
    _dr,
    _inject,
    _instructor_board,
    _local_search,
    _pat,
    _search_board,
    _sec,
    _state,
    chains,
)


@pytest.fixture
def av_on(settings):
    for name, value in AVAILABILITY_FLAGS.items():
        setattr(settings, name, value)
    return settings


RULES_OR_EMPTY = pytest.mark.parametrize(
    "rules", [True, False], ids=["rules", "empty-availability-control"]
)


# ── D-D: the strict move rule ────────────────────────────────────────────


def _two_forbidden():
    """X (teacher 7, no mornings) holds two forbidden meetings: SUN and TUE 09:00."""
    board = {"X_S1": _state("X_S1", [(SUN, *AM), (TUE, *AM)])}
    before = {"X_S1": list(board["X_S1"].meetings)}
    return board, {"X_S1": frozenset({7})}, before


def _moved_to(board, cells) -> None:
    board["X_S1"].meetings[:] = [SectionMeeting(day, start, end) for day, (start, end) in cells]


@RULES_OR_EMPTY
def test_a_move_to_one_different_forbidden_meeting_is_refused(rules) -> None:
    """D-D: two forbidden meetings -> MON 09:00 + WED 13:00 holds ONE forbidden
    meeting the section did not have: refused although the count falls 2 -> 1
    (control: an empty availability never objects)."""
    board, charged, before = _two_forbidden()
    _moved_to(board, [(MON, AM), (WED, PM)])

    assert (
        constraints.move_adds_availability_violation(
            board, charged, NO_MORNINGS if rules else EMPTY_AVAILABILITY, before
        )
        is rules
    )


def test_a_forbidden_meeting_may_stay_where_it_is_while_the_other_leaves() -> None:
    """D-D: SUN 09:00 stays (a violation the section already had), TUE 09:00 moves
    to TUE 13:00 (legal): accepted."""
    board, charged, before = _two_forbidden()
    _moved_to(board, [(SUN, AM), (TUE, PM)])

    assert not constraints.move_adds_availability_violation(board, charged, NO_MORNINGS, before)


def test_a_move_to_a_fully_legal_pattern_is_accepted() -> None:
    board, charged, before = _two_forbidden()
    _moved_to(board, [(SUN, PM), (TUE, PM)])

    assert not constraints.move_adds_availability_violation(board, charged, NO_MORNINGS, before)


def test_a_kept_forbidden_meeting_must_keep_its_day_start_and_end() -> None:
    """D-D: 'already had' means the same day, start AND end: SUN 09:00-10:15 ->
    SUN 09:00-10:40 is a new forbidden meeting."""
    board, charged, before = _two_forbidden()
    _moved_to(board, [(SUN, (AM[0], AM[0] + 100)), (TUE, PM)])

    assert constraints.move_adds_availability_violation(board, charged, NO_MORNINGS, before)


#: A pattern with ONE forbidden meeting the section never had (MON 09:00).
A_MON_AM_WED_PM = _pat("a_mon_am_wed_pm", [(MON, AM), (WED, PM)])
#: SUN 09:00 kept, TUE moved to the afternoon.
A_SUN_AM_TUE_PM = _pat("a_sun_am_tue_pm", [(SUN, AM), (TUE, PM)])
A_AM = _pat("a_am", [(SUN, AM), (TUE, AM)])
B_TUE_AM = _pat("b_tue_am", [(TUE, AM)], family="b")


@RULES_OR_EMPTY
def test_local_search_v2_never_trades_a_forbidden_week_for_another(rules) -> None:
    """D-D through LS-v2: A (two forbidden mornings) clashes with the locked B on
    TUE 09:00; the only alternative holds one forbidden meeting A never had, so A
    stays (control: an empty availability takes it)."""
    sections, catalog, profiles, baseline = _search_board(A_AM, B_TUE_AM, [A_AM, A_MON_AM_WED_PM])

    result = _local_search(
        NO_MORNINGS if rules else EMPTY_AVAILABILITY, sections, catalog, profiles, baseline
    )

    if rules:
        assert result is baseline
        assert sections["A"].pattern_id == "a_am"
    else:
        assert sections["A"].pattern_id == "a_mon_am_wed_pm"


def test_local_search_v2_lets_a_forbidden_meeting_stay_while_the_other_leaves() -> None:
    """D-D through LS-v2: the alternative keeps SUN 09:00 and moves TUE 09:00 to
    the afternoon: accepted under the rule."""
    sections, catalog, profiles, baseline = _search_board(A_AM, B_TUE_AM, [A_AM, A_SUN_AM_TUE_PM])

    result = _local_search(NO_MORNINGS, sections, catalog, profiles, baseline)

    assert result is not baseline
    assert sections["A"].pattern_id == "a_sun_am_tue_pm"


C_NOW = _pat("c_now", [(MON, (900, 975))], family="c")
C_ALT = _pat("c_alt", [(MON, (960, 1035))], family="c")


@RULES_OR_EMPTY
def test_a_chain_never_trades_a_forbidden_week_for_another(monkeypatch, rules) -> None:
    """D-D through a chain: leg A takes A from two forbidden mornings to one
    different forbidden meeting; the chain is rolled back whole (control: an empty
    availability takes it)."""
    board = _search_board(
        A_AM, B_TUE_AM, [A_AM, A_MON_AM_WED_PM], extra=[("C", C_NOW, [C_NOW, C_ALT])]
    )
    chain = chains.ChainMove(
        TimetableMove("repattern", "A", "a_am", "a_mon_am_wed_pm"),
        TimetableMove("repattern", "C", "c_now", "c_alt"),
    )

    result = _chain_search(
        monkeypatch, NO_MORNINGS if rules else EMPTY_AVAILABILITY, board, [chain]
    )

    sections, _catalog, _profiles, baseline = board
    if rules:
        assert result is baseline
        assert sections["A"].pattern_id == "a_am"
    else:
        assert sections["A"].pattern_id == "a_mon_am_wed_pm"


CORE_AM = _pat("core_am", [(SUN, AM)])
SOFT_SUN_AM = _pat("soft_sun_am", [(SUN, AM)])
SOFT_MON_AM = _pat("soft_mon_am", [(MON, AM)])
SOFT_MON_PM = _pat("soft_mon_pm", [(MON, PM)])


def _recover(alternative, availability) -> dict:
    """SOFT (teacher 7) sits at SUN 09:00, forbidden, clashing with the locked
    CORE; its one alternative is ``alternative``."""
    from core.services.timetable_secondary_recovery import recover_secondary_quality

    sections = {"CORE": _sec("CORE", CORE_AM), "SOFT": _sec("SOFT", SOFT_SUN_AM)}
    profiles = {"s": StudentProfile("s", "CS", ["CORE", "SOFT"], RiskTier.C, 0)}
    tiers = {"CORE": "T1", "SOFT": "T3"}
    ids = {"SOFT": frozenset({7})}
    baseline = evaluate_generated_timetable_candidate(
        "before", list(sections.values()), profiles, {}, ids, tiers
    )
    report = recover_secondary_quality(
        current_eval=baseline,
        sections_by_id=sections,
        pattern_catalog={"fam": [SOFT_SUN_AM, alternative]},
        student_profiles=profiles,
        course_rigidity={},
        room_evaluator=lambda states: {
            "unassigned_count": 0,
            "unassigned_section_ids": [],
            "proven_optimal": True,
        },
        section_instructor_ids=ids,
        course_tiers=tiers,
        locked_section_ids={"CORE"},
        time_limit_seconds=10,
        max_iterations=3,
        instructor_availability=availability,
    )["report"]
    return report


@RULES_OR_EMPTY
def test_the_recovery_kernel_never_trades_a_forbidden_meeting_for_another(settings, rules) -> None:
    """D-D through the recovery kernel: SUN 09:00 -> MON 09:00 keeps the count at
    one but moves the meeting INTO a forbidden time: refused (control: an empty
    availability applies it)."""
    settings.TIMETABLE_TIERED_OBJECTIVE_ENABLED = True

    report = _recover(SOFT_MON_AM, NO_MORNINGS if rules else EMPTY_AVAILABILITY)

    assert bool(report["applied"]) is not rules
    if rules:
        assert report["structural_rejections"] > 0


def test_the_recovery_kernel_moves_a_forbidden_meeting_to_a_legal_time(settings) -> None:
    """D-D: forbidden -> legal is a move the strict rule accepts."""
    settings.TIMETABLE_TIERED_OBJECTIVE_ENABLED = True

    report = _recover(SOFT_MON_PM, NO_MORNINGS)

    assert report["applied"]


REPAIR_FLAGS = {
    "TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED": True,
    "TIMETABLE_INSTRUCTOR_LINKS_ENABLED": True,
    "TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED": True,
    "TIMETABLE_INSTRUCTOR_DAILY_CAP": 3,
    "TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED": False,
    "TIMETABLE_SECTION_DAY_SPREAD": False,
}


@pytest.fixture
def repair_flags(settings):
    for name, value in REPAIR_FLAGS.items():
        setattr(settings, name, value)
    return settings


ENG_DAYS = ("SUN", "MON", "WED", "THU")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "afternoon_thu", [True, False], ids=["afternoon-breaks-thu", "afternoon-legal-control"]
)
def test_the_english_bundle_search_never_trades_a_forbidden_bundle_for_another(
    repair_flags, monkeypatch, afternoon_thu
) -> None:
    """D-D through the English bundle search: ENG101's morning bundle breaks Dr
    Run's rule twice (SUN and MON mornings) and clashes with C2 on SUN 11:45-13:00. The
    afternoon bundle repairs the clash but its THU afternoon is forbidden too: a
    meeting the section never had, so the switch is refused although the count
    falls 2 -> 1. Control: with THU afternoon allowed the switch is taken."""
    from core.services.timetable_fixed_pattern_search import improve_fixed_course_patterns

    scenario = _instructor_board(
        f"d-d eng {afternoon_thu}",
        [
            ("C2", "S1", [("SUN", "11:45", "13:00")]),
            ("ENG101", "S1", [(day, "09:00", "12:00") for day in ENG_DAYS]),
        ],
    )
    dr = _dr()
    rows = [(dr, KIND_UNAVAILABLE, ("SUN", "MON"), 9 * 60, 12 * 60)]
    if afternoon_thu:
        rows.append((dr, KIND_UNAVAILABLE, ("THU",), 13 * 60, 16 * 60))
    _inject(monkeypatch, _av(*rows))

    report = improve_fixed_course_patterns(scenario.id, repair_clashes=True)

    starts = set(
        SectionPlacement.objects.filter(
            board__scenario=scenario, term_section__course_code="ENG101"
        ).values_list("start_time", flat=True)
    )
    assert report["evaluated"] >= 1
    assert starts == ({"09:00"} if afternoon_thu else {"13:00"})
    assert report["accepted"] == (0 if afternoon_thu else 1)


# ── D-E: the cap repair never unplaces because of a rule ─────────────────


def _cap_board(name: str):
    """repairs:R1's fixture. Cap 3, one teacher (Dr Run). THU holds C2 09:00,
    C3 10:30, C4 13:00-13:50 (a 50-minute meeting no slot fits: it has no
    relocation at all) and C1 14:30 (C1 also meets SUN and TUE, so it may only go
    to MON or WED). Sessions are tried latest first: C1, then C4."""
    return _instructor_board(
        name,
        [
            ("C2", "S1", [("THU", "09:00", "10:15")]),
            ("C3", "S1", [("THU", "10:30", "11:45")]),
            ("C4", "S1", [("THU", "13:00", "13:50")]),
            (
                "C1",
                "S1",
                [("SUN", "09:00", "10:15"), ("TUE", "09:00", "10:15"), ("THU", "14:30", "15:45")],
            ),
        ],
    )


@pytest.mark.django_db(transaction=True)
@RULES_OR_EMPTY
def test_cap_repair_never_unplaces_a_session_because_a_rule_kept_another(
    repair_flags, monkeypatch, rules
) -> None:
    """D-E: Dr Run may not teach on MON or WED, so C1 is kept on THU (reported).
    C4 has no relocation at all; without the rule C1 would have relieved the day
    and C4 would never have been tried, so C4 is kept and reported too, never
    unplaced. Control: an empty availability moves C1 to MON and unplaces nothing."""
    from core.services.timetable_instructor_cap_repair import repair_instructor_daily_overloads
    from tests.test_instructor_availability_engine import _course_days, _unavailable

    scenario = _cap_board(f"d-e {rules}")
    _inject(monkeypatch, _unavailable(_dr(), "MON", "WED") if rules else EMPTY_AVAILABILITY)

    report = repair_instructor_daily_overloads(scenario.id)

    assert report["unplaced"] == []
    assert _course_days(scenario, "C4") == ["THU"]
    if rules:
        assert [row["section"] for row in report["availability_blocked"]] == ["C1 S1", "C4 S1"]
        assert "THU" in _course_days(scenario, "C1")
    else:
        assert "availability_blocked" not in report
        assert _course_days(scenario, "C1") == ["SUN", "MON", "TUE"]


# ── D-F: the polisher's H6 as designed ───────────────────────────────────

#: Teacher 7 may teach only SUN 13:00-14:15.
ONLY_SUN_ONE = _av((7, KIND_AVAILABLE_ONLY, ("SUN",), 13 * 60, 14 * 60 + 15))


@pytest.mark.django_db
@RULES_OR_EMPTY
def test_a_pre_existing_clash_of_a_narrow_window_teacher_leaves_the_rest_polishable(
    settings, monkeypatch, rules
) -> None:
    """D-F probe 1 (improvement:F1): A1 and A2 (teacher 7, only SUN 13:00) both
    sit at SUN 13:00, a clash a manual move may leave. Each one's only legal cell
    is its current one, so both are held fixed; X still leaves the locked B's SUN
    09:00 (with the old H6 the clash made the whole model infeasible). Control: an
    empty availability polishes them all."""
    from tests.test_instructor_availability_engine import (
        MON_PM_CELL,
        SUN_AM_CELL,
        SUN_PM_CELL,
        _polish_case,
    )

    settings.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = True
    # Instructor priority off: splitting the clash costs teacher 7 a day or idle
    # time, which would veto the control's polish for a reason unrelated to H6.
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = False
    result, polished = _polish_case(
        monkeypatch,
        {"A1": [SUN_PM_CELL], "A2": [SUN_PM_CELL], "X": [SUN_AM_CELL]},
        {"B": [SUN_AM_CELL]},
        overlap={("B", "X"): 2},
        takes=("B", "X"),
        section_ids={"A1": frozenset({7}), "A2": frozenset({7})},
        availability=ONLY_SUN_ONE if rules else EMPTY_AVAILABILITY,
    )

    assert result is not None
    assert polished["X"] != [SUN_AM_CELL]
    if rules:
        assert "A1" not in polished and "A2" not in polished  # held fixed
    else:
        assert sorted([polished["A1"], polished["A2"]]) == [[SUN_PM_CELL], [MON_PM_CELL]]


@pytest.mark.django_db
@RULES_OR_EMPTY
def test_an_off_grid_meeting_under_all_different_days_leaves_the_rest_polishable(
    monkeypatch, rules
) -> None:
    """D-F probe 2 (improvement:F1): R (teacher 7, only SUN 13:00) meets SUN 13:00
    (legal) and MON 08:00 (off the grid, forbidden). Each meeting alone has the
    legal SUN 13:00, but both may not be on SUN: meeting 0 has no legal option
    other than its current slot, so R is held fixed and X is still polished
    (control: an empty availability polishes X too)."""
    from tests.test_instructor_availability_engine import SUN_AM_CELL, SUN_PM_CELL, _polish_case

    off_grid = (MON, 8 * 60, 9 * 60 + 15)
    result, polished = _polish_case(
        monkeypatch,
        {"R": [SUN_PM_CELL, off_grid], "X": [SUN_AM_CELL]},
        {"B": [SUN_AM_CELL]},
        overlap={("B", "X"): 2},
        takes=("B", "X"),
        section_ids={"R": frozenset({7})},
        availability=ONLY_SUN_ONE if rules else EMPTY_AVAILABILITY,
    )

    assert result is not None
    assert polished["X"] != [SUN_AM_CELL]
    if rules:
        assert "R" not in polished  # held fixed


@pytest.mark.django_db
@RULES_OR_EMPTY
def test_an_english_section_whose_only_legal_bundle_is_its_own_leaves_the_rest_polishable(
    settings, monkeypatch, rules
) -> None:
    """D-F, the bundle branch: ENG101 (teacher 7, no mornings) teaches the
    afternoon bundle, which already clashes with teacher 7's locked B on SUN 13:00.
    Its only legal bundle is its current one, so it is held fixed and X is still
    polished (with the old H6 the clash closed the one legal bundle and the model
    was infeasible). Control: an empty availability moves ENG101 to the mornings."""
    from tests.test_instructor_availability_engine import SUN_PM_CELL, _polish_case

    settings.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = False
    afternoon = [(day, 13 * 60, 16 * 60) for day in (SUN, MON, WED, THU)]
    result, polished = _polish_case(
        monkeypatch,
        {"ENG101": afternoon, "X": [(TUE, 9 * 60, 10 * 60 + 15)]},
        {"B": [SUN_PM_CELL], "C": [(TUE, 9 * 60, 10 * 60 + 15)]},
        open_cells=None,
        overlap={("C", "X"): 2},
        takes=("C", "X"),
        section_ids={"ENG101": frozenset({7}), "B": frozenset({7})},
        availability=NO_MORNINGS if rules else EMPTY_AVAILABILITY,
    )

    assert result is not None
    assert polished["X"] != [(TUE, 9 * 60, 10 * 60 + 15)]
    if rules:
        assert "ENG101" not in polished  # held fixed
    else:
        assert polished["ENG101"] == [(day, 9 * 60, 12 * 60) for day in (SUN, MON, WED, THU)]


# ── D-J: a whole-board rollback restores the side-band count ─────────────


@pytest.mark.django_db
@pytest.mark.parametrize("baseline", [2, None], ids=["baseline-count", "no-baseline-count"])
def test_a_whole_board_rollback_reports_the_restored_boards_rule_count(
    monkeypatch, settings, baseline
) -> None:
    """D-J (improvement:F4): the run lowered the count to 0, then a regression
    rolled the whole board back. The reported count is the baseline board's (2),
    or absent when the run pinned none, never the discarded board's 0."""
    from core.services import timetable_optimizer_v2 as optimiser
    from core.services.timetable_board_persistence import snapshot_scenario
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded
    from tests.test_timetable_runner_rollback import _persisted_board

    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = False
    scenario, _board, _section = _persisted_board("d-j rollback")
    original = snapshot_scenario(scenario.id)

    def worse(**_kwargs):
        SectionPlacement.objects.filter(board__scenario_id=scenario.id).update(day="WED")
        return {
            "baseline_score": [0, 0, 0, 0, 0, 0],
            "final_score": [1, 0, 0, 0, 0, 0],
            "quality_score": {"instructor_availability": {"violations": 0}},
            **(
                {"baseline_instructor_availability": {"violations": baseline}}
                if baseline is not None
                else {}
            ),
        }

    monkeypatch.setattr(optimiser, "optimise_current_timetable", worse)
    result = run_v2_optimisation_guarded(scenario.id, mode="current")

    assert result["safety_blocked"] is True
    assert snapshot_scenario(scenario.id) == original
    if baseline is None:
        assert "instructor_availability" not in result["quality_score"]
    else:
        assert result["quality_score"]["instructor_availability"] == {"violations": 2}


@pytest.mark.django_db
def test_a_kept_run_reports_its_own_rule_count(monkeypatch, settings) -> None:
    """D-J control: without a rollback the run's own count stands."""
    from core.services import timetable_optimizer_v2 as optimiser
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded
    from tests.test_timetable_runner_rollback import _persisted_board

    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = False
    scenario, _board, _section = _persisted_board("d-j kept")

    def same(**_kwargs):
        return {
            "baseline_score": [0, 0, 0, 0, 0, 0],
            "final_score": [0, 0, 0, 0, 0, 0],
            "quality_score": {"instructor_availability": {"violations": 0}},
            "baseline_instructor_availability": {"violations": 2},
        }

    monkeypatch.setattr(optimiser, "optimise_current_timetable", same)
    result = run_v2_optimisation_guarded(scenario.id, mode="current")

    assert not result.get("safety_blocked")
    assert result["quality_score"]["instructor_availability"] == {"violations": 0}


def _no_afternoons_for_the_corpus_teachers() -> None:
    from core.models import Instructor
    from tests._instructor_availability_fixture import rule
    from tests.test_one_teacher_flag_parity import LINKS

    for person in Instructor.objects.filter(full_name__in=set(LINKS.values())):
        rule(person, KIND_UNAVAILABLE, ("SUN", "MON", "TUE", "WED", "THU"), "16:00", "24:00")


@pytest.mark.django_db
@pytest.mark.parametrize("flag", [True, False], ids=["rules", "flag-off-control"])
def test_optimise_current_pins_the_baseline_boards_rule_count(av_on, flag) -> None:
    """D-J: Optimise Current pins the baseline board's side band next to it, as
    it pins the student gap split (flag off: neither key exists)."""
    from django.db import transaction

    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded
    from tests.test_instructor_availability_engine import _Rollback
    from tests.test_one_teacher_flag_parity import build_corpus

    av_on.TIMETABLE_INSTRUCTOR_AVAILABILITY = flag
    out: dict = {}
    try:
        with transaction.atomic():
            scenario = build_corpus(11)
            auto_place_scenario(scenario.id, strategy="compact")
            _no_afternoons_for_the_corpus_teachers()
            out["result"] = run_v2_optimisation_guarded(
                scenario.id, mode="current", max_iterations=1, run_chain=False, run_cpsat=False
            )
            raise _Rollback
    except _Rollback:
        pass
    result = out["result"]

    if flag:
        assert set(result["baseline_instructor_availability"]) == {"violations"}
        assert isinstance(result["baseline_instructor_availability"]["violations"], int)
    else:
        assert "baseline_instructor_availability" not in result
        assert "instructor_availability" not in result["quality_score"]

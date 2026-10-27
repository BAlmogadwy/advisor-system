"""``TIMETABLE_SECTION_DAY_SPREAD``: a soft wish to space a section's own meetings.

Owner wish 2026-10-25: a section's own meetings should not sit on consecutive
days (SUN+MON) when SUN+TUE / SUN+WED would do; a 3-meeting (4-credit) section
should not sit on three days in a row, and "two and one" (SUN+MON+WED) is allowed
at a penalty. It ships behind a flag that defaults OFF, and OFF must be exactly
today's engine:

- **Flag OFF = baseline.** The greedy placer reproduces a golden recorded on the
  UNCHANGED engine (three strategies x seeds 3 / 11 / 29 plus one unseeded legacy
  run), and the two CP-SAT builders (``solve_board`` and
  ``solve_board_with_hints``) emit the same objective (SHA-256 of the serialised
  objective) and the same variable / constraint counts. Only the model is
  compared, never the solution: CP-SAT solutions differ between Windows and
  Linux (see ``test_one_teacher_flag_parity``), the proto is pure Python. The
  load balancer runs on a step clock (``LOAD_BALANCER_STEP_SECONDS``), so its
  search is the same length on every machine.
- **Flag ON.** Two- and three-meeting sections prefer non-adjacent days (greedy:
  0 / w / 3w for spread / two-and-one / three in a row; CP-SAT: 0 / w / 2w, per
  pair, no extra variable); sections with one or four meetings, and
  ENG-intensive options, are untouched; a roomless meeting (100) still costs
  more than a consecutive pair (default 40); the CP-SAT model keeps its size.
- **Nothing else reads the flag.** Manual moves and validation never see it.

The golden lives in ``tests/fixtures/section_day_spread_flag_off_golden.json``.
To re-check it, check out the commit BEFORE the flag was added and run::

    SECTION_DAY_SPREAD_GOLDEN_RECORD=<file> python -m pytest -p no:cacheprovider \\
        tests/test_section_day_spread.py -k record

then diff ``<file>`` against the fixture. Never re-record it from a branch that
changes the planner: a golden that follows the code proves nothing. That commit's
harness reads the real clock; it records the same golden, which holds from 10,000
load-balancer moves a board on.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from unittest import mock

import pytest
from ortools.sat.python import cp_model

from core.models import (
    DeliveryBoard,
    Room,
    ScenarioSectionBudget,
    SectionPlacement,
    TimetableScenario,
)
from core.services.course_identity import planner_course_key
from tests.test_one_teacher_flag_parity import build_corpus, capture_board

GOLDEN = Path(__file__).parent / "fixtures" / "section_day_spread_flag_off_golden.json"
RECORD_ENV = "SECTION_DAY_SPREAD_GOLDEN_RECORD"

REPO = Path(__file__).resolve().parent.parent

# The production instructor flags, with the new preference explicitly OFF.
PRODUCTION_FLAGS = {
    "TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED": True,
    "TIMETABLE_INSTRUCTOR_LINKS_ENABLED": True,
    "TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED": True,
    "TIMETABLE_INSTRUCTOR_DAILY_CAP": 3,
    "TIMETABLE_ONE_TEACHER_PER_SECTION": True,
    "TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY": True,
    "TIMETABLE_CPSAT_MAX_MODEL_ELEMENTS": 0,
}

STRATEGIES = ("compact", "morning", "load_balanced")
RUN_SEEDS = (3, 11, 29, None)  # None = the legacy unseeded call, ties by option order
GREEDY_CASES = [(strategy, seed) for strategy in STRATEGIES for seed in RUN_SEEDS]
CPSAT_CASES = ("solve_board", "solve_board_with_hints")

# The load balancer stops on the wall clock (5 s a board), so how far its search
# gets depends on the machine: on 2026-10-26 a slow CI runner stopped seed 11
# short and roomed one meeting differently. The golden builds give it a clock that
# moves one fixed step per reading instead: 30,000 moves a board on every
# machine. The golden (real clock, 30k-860k moves a board) holds from 10,000 on.
LOAD_BALANCER_STEP_SECONDS = 5.0 / 30_000


class _StepClock:
    """A stand-in for the ``time`` module: each reading is one step later."""

    def __init__(self, step: float) -> None:
        self._step = step
        self._readings = 0

    def time(self) -> float:
        self._readings += 1
        return self._readings * self._step


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


# ── the recorded workloads ────────────────────────────────────────────


def _case_key(strategy: str, seed: int | None) -> str:
    return f"{strategy}|{'unseeded' if seed is None else seed}"


def run_greedy_case(strategy: str, seed: int | None) -> dict:
    """A build, a lock, a rebuild: the rebuild exercises every instructor filter."""
    from core.services import timetable_load_balanced
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_board_persistence import reset_scenario
    from core.services.timetable_run_seed import seeded_timetable_run

    scenario = build_corpus(seed or 11)
    step_clock = _StepClock(LOAD_BALANCER_STEP_SECONDS)
    with (
        seeded_timetable_run(seed),
        mock.patch.object(timetable_load_balanced, "time", step_clock),
    ):
        first = auto_place_scenario(scenario.id, strategy=strategy)
        SectionPlacement.objects.filter(
            board__scenario=scenario,
            term_section__course_key=planner_course_key("CS114", "ALGORITHMS & DATA STRUCTURES"),
            term_section__section="S1",
        ).update(is_locked=True)
        reset_scenario(scenario.id, keep_locked=True)
        placed = auto_place_scenario(scenario.id, strategy=strategy)
    board = capture_board(scenario)
    return json.loads(
        json.dumps(
            {
                "first_placed": first.get("total_placed"),
                "total_placed": placed.get("total_placed"),
                "placements": board["placements"],
                "meetings": board["meetings"],
            }
        )
    )


class _Captured(Exception):
    """Raised by the spy to stop a solve once its model has been fingerprinted."""


def _model_fingerprint(model) -> dict:
    proto = model.proto
    objective = proto.objective
    return {
        "objective_sha256": hashlib.sha256(
            repr(
                (
                    list(objective.vars),
                    list(objective.coeffs),
                    objective.offset,
                    objective.scaling_factor,
                    [variable.name for variable in proto.variables],
                )
            ).encode()
        ).hexdigest(),
        "objective_terms": len(proto.objective.vars),
        "variables": len(proto.variables),
        "constraints": len(proto.constraints),
    }


def make_capture_models(monkeypatch):
    """``capture(scenario)`` runs both CP-SAT builders on the scenario's Term 4
    board and returns ``{builder: model}``.

    The spy stops each solve before it starts, so nothing is solved or saved and
    the same scenario can be captured again after a setting changes.
    """
    from core.services import timetable_solver

    seen: list = []

    def spy(self, model, *args, **kwargs):
        seen.append(model)
        raise _Captured

    monkeypatch.setattr(cp_model.CpSolver, "solve", spy)

    def capture(scenario) -> dict:
        board = DeliveryBoard.objects.get(scenario=scenario, label="Term 4")
        models = {}
        for name in CPSAT_CASES:
            seen.clear()
            with pytest.raises(_Captured):
                if name == "solve_board":
                    timetable_solver.solve_board(board.id, time_limit_seconds=1)
                else:
                    timetable_solver.solve_board_with_hints(board.id, [], time_limit_seconds=1)
            models[name] = seen[-1]
        return models

    return capture


@pytest.fixture
def capture_models(monkeypatch):
    return make_capture_models(monkeypatch)


# ── recorder (skipped unless asked) ───────────────────────────────────


def _merge_into_record(key: str, value: dict) -> None:
    target = Path(os.environ[RECORD_ENV])
    data = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    data[key] = value
    target.write_text(
        json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )


@pytest.mark.skipif(not os.environ.get(RECORD_ENV), reason=f"set {RECORD_ENV} to record")
@pytest.mark.django_db
@pytest.mark.parametrize(("strategy", "seed"), GREEDY_CASES)
def test_record_greedy_golden(flag_off, strategy, seed) -> None:
    _merge_into_record(f"greedy:{_case_key(strategy, seed)}", run_greedy_case(strategy, seed))


@pytest.mark.skipif(not os.environ.get(RECORD_ENV), reason=f"set {RECORD_ENV} to record")
@pytest.mark.django_db
def test_record_cpsat_golden(flag_off, capture_models) -> None:
    for name, model in capture_models(build_corpus(11)).items():
        _merge_into_record(f"cpsat:{name}", _model_fingerprint(model))


# ── flag OFF = the baseline ───────────────────────────────────────────


def _golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.mark.django_db
@pytest.mark.parametrize(("strategy", "seed"), GREEDY_CASES)
def test_flag_off_greedy_build_matches_the_recorded_golden(flag_off, strategy, seed) -> None:
    assert run_greedy_case(strategy, seed) == _golden()[f"greedy:{_case_key(strategy, seed)}"]


@pytest.mark.django_db
def test_flag_off_cpsat_models_match_the_recorded_golden(flag_off, capture_models) -> None:
    produced = {
        f"cpsat:{name}": _model_fingerprint(model)
        for name, model in capture_models(build_corpus(11)).items()
    }

    golden = _golden()
    assert produced == {key: golden[key] for key in produced}


# ── flag ON: the greedy placer ────────────────────────────────────────


def _meeting(day: str) -> dict:
    return {"day": day, "start": "08:00", "end": "09:15", "slot_idx": 0}


def test_helper_charges_adjacent_two_meeting_options() -> None:
    from core.services.timetable_autoplace import _section_day_spread_penalty as penalty

    assert penalty([_meeting("SUN"), _meeting("MON")], 40) == 40
    assert penalty([_meeting("MON"), _meeting("SUN")], 40) == 40  # order never matters
    assert penalty([_meeting("WED"), _meeting("THU")], 40) == 40
    assert penalty([_meeting("SUN"), _meeting("TUE")], 40) == 0
    assert penalty([_meeting("SUN"), _meeting("WED")], 40) == 0
    # THU -> SUN wraps the week, it is not "consecutive".
    assert penalty([_meeting("SUN"), _meeting("THU")], 40) == 0
    # One meeting is never charged.
    assert penalty([_meeting("SUN")], 40) == 0
    # Weight 0 is the flag-off state.
    assert penalty([_meeting("SUN"), _meeting("MON")], 0) == 0
    assert penalty([_meeting("SUN"), _meeting("MON"), _meeting("TUE")], 0) == 0


def test_helper_three_meetings_rank_spread_then_two_and_one_then_three_in_a_row() -> None:
    from core.services.timetable_autoplace import _section_day_spread_penalty as penalty

    def cost(*days: str, weight: int = 40) -> int:
        return penalty([_meeting(d) for d in days], weight)

    assert cost("SUN", "TUE", "THU") == 0
    assert cost("SUN", "MON", "WED") == 40  # two and one: one adjacent pair
    assert cost("SUN", "WED", "THU") == 40
    assert cost("MON", "WED", "THU") == 40
    assert cost("SUN", "MON", "THU") == 40
    assert cost("SUN", "MON", "TUE") == 120  # three in a row: 2 pairs + 1 extra = 3w
    assert cost("TUE", "WED", "THU") == 120
    assert cost("THU", "TUE", "WED") == 120  # order never matters
    assert cost("SUN", "TUE", "WED", weight=25) == 25
    assert cost("SUN", "MON", "TUE", weight=25) == 75
    # THU -> SUN never wraps: THU, SUN, WED has exactly one adjacent pair.
    assert cost("THU", "SUN", "WED") == 40
    assert 0 == cost("SUN", "TUE", "THU") < cost("SUN", "MON", "WED") < cost("SUN", "MON", "TUE")


def test_helper_never_charges_four_or_more_meetings() -> None:
    from core.services.timetable_autoplace import _section_day_spread_penalty as penalty

    assert penalty([_meeting(d) for d in ("SUN", "MON", "TUE", "WED")], 40) == 0
    assert penalty([_meeting(d) for d in ("SUN", "MON", "TUE", "WED", "THU")], 40) == 0
    assert penalty([], 40) == 0


def _tiny_board(
    credit_hours: int, planned: int = 1, name: str = "day spread", course_code: str = "AI101"
) -> TimetableScenario:
    scenario = TimetableScenario.objects.create(academic_year="1448", term="2", name=name)
    DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_code=course_code,
        course_key=course_code,
        department="AI",
        programme_term=1,
        credit_hours=credit_hours,
        planned_sections=planned,
        max_per_section=30,
        total_demand=20,
    )
    for index in range(3):
        Room.objects.get_or_create(
            room_code=f"L{index + 1}",
            defaults={"capacity": 40, "room_type": "lecture", "department": "AI"},
        )
        Room.objects.get_or_create(
            room_code=f"LAB{index + 1}",
            defaults={"capacity": 40, "room_type": "lab", "department": "AI"},
        )
    return scenario


def _days_by_section(scenario: TimetableScenario) -> dict[str, list[str]]:
    days: dict[str, list[str]] = {}
    for label, day in SectionPlacement.objects.filter(board__scenario=scenario).values_list(
        "term_section__section", "day"
    ):
        days.setdefault(label, []).append(day)
    return {label: sorted(values) for label, values in days.items()}


def _consecutive(days: list[str]) -> bool:
    order = ["SUN", "MON", "TUE", "WED", "THU"]
    idx = sorted(order.index(d) for d in days)
    return any(b - a == 1 for a, b in zip(idx, idx[1:], strict=False))


def _place(
    strategy: str, seed: int, *, credit_hours: int, planned: int = 1, course_code: str = "AI101"
) -> dict:
    """Place a fresh one-course board; every call gets its own scenario (unique name)."""
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_run_seed import seeded_timetable_run

    _place.calls += 1
    scenario = _tiny_board(
        credit_hours, planned, name=f"day spread #{_place.calls}", course_code=course_code
    )
    with seeded_timetable_run(seed):
        auto_place_scenario(scenario.id, strategy=strategy)
    return _days_by_section(scenario)


_place.calls = 0


@pytest.mark.django_db
def test_flag_on_two_meeting_section_avoids_consecutive_days_for_every_seed(flag_on) -> None:
    for seed in range(12):
        days = _place("compact", seed, credit_hours=3)
        assert days and all(len(v) == 2 for v in days.values()), (seed, days)
        assert not any(_consecutive(v) for v in days.values()), (seed, days)


@pytest.mark.django_db
def test_flag_off_the_same_board_does_land_on_consecutive_days_for_some_seed(flag_off) -> None:
    """The control: without the flag the seeded tie-break DOES pick adjacent days,
    so the ON test above is not passing for free."""
    placed = [_place("compact", seed, credit_hours=3) for seed in range(12)]

    assert any(_consecutive(v) for days in placed for v in days.values())


@pytest.mark.django_db
def test_flag_on_three_meeting_section_lands_on_sun_tue_thu_for_every_seed(flag_on) -> None:
    """SUN+TUE+THU is the only fully spread triple in five days, so with everything
    else equal the placer must pick it over two-and-one and three in a row."""
    for seed in range(12):
        days = _place("compact", seed, credit_hours=4)
        assert days and all(v == ["SUN", "THU", "TUE"] for v in days.values()), (seed, days)


@pytest.mark.django_db
def test_flag_off_the_same_three_meeting_board_does_not_always_spread(flag_off) -> None:
    """The control: without the flag some seed lands on adjacent days, so the ON
    test above is not passing for free."""
    placed = [_place("compact", seed, credit_hours=4) for seed in range(12)]

    assert all(len(v) == 3 for days in placed for v in days.values())
    assert any(_consecutive(v) for days in placed for v in days.values())


@pytest.mark.django_db
def test_flag_on_the_placer_scores_three_meeting_options_at_every_cost_level(
    flag_on, monkeypatch
) -> None:
    """Through the real scoring loop: the helper is called for 3-meeting options and
    sees the spread (0), two-and-one (w) and three-in-a-row (3w) costs."""
    from core.services import timetable_autoplace as autoplace

    seen: list[tuple[int, int]] = []
    real = autoplace._section_day_spread_penalty

    def spy(option, weight):
        value = real(option, weight)
        seen.append((len(option), value))
        return value

    monkeypatch.setattr(autoplace, "_section_day_spread_penalty", spy)
    _place("compact", 1, credit_hours=4)

    assert {value for count, value in seen if count == 3} >= {0, 40, 120}


@pytest.mark.django_db
def test_flag_on_the_eng_intensive_policy_is_never_scored_for_spacing(flag_on, monkeypatch) -> None:
    """ENG101 meets SUN+MON+WED+THU by policy; the placer must not even ask for a
    spacing cost, and the result is identical with the flag off."""
    from core.services import timetable_autoplace as autoplace

    calls: list[int] = []
    real = autoplace._section_day_spread_penalty

    def spy(option, weight):
        calls.append(len(option))
        return real(option, weight)

    monkeypatch.setattr(autoplace, "_section_day_spread_penalty", spy)
    on = _place("compact", 3, credit_hours=3, course_code="ENG101")
    flag_on.TIMETABLE_SECTION_DAY_SPREAD = False
    off = _place("compact", 3, credit_hours=3, course_code="ENG101")

    assert on == off
    assert on and all(v == ["MON", "SUN", "THU", "WED"] for v in on.values())
    assert calls == []


def test_default_weight_is_cheaper_than_a_roomless_meeting() -> None:
    """A roomless meeting costs 100 in the soft bucket; spacing must never buy a
    room away, so the default weight stays below it."""
    source = (REPO / "core" / "services" / "timetable_autoplace.py").read_text(encoding="utf-8")
    roomless = int(re.search(r"room_penalty \+= (\d+)", source).group(1))
    default = int(
        re.search(
            r'"TIMETABLE_SECTION_DAY_SPREAD_WEIGHT",\s*"(\d+)"',
            (REPO / "config" / "settings.py").read_text(encoding="utf-8"),
        ).group(1)
    )

    assert default == 40
    assert default < roomless


# ── flag ON: CP-SAT ───────────────────────────────────────────────────


def _consec_coefficients(model) -> dict[int, set[int]]:
    """Section index -> the coefficients its ``consec_*`` variables carry."""
    proto = model.proto
    names = {i: v.name for i, v in enumerate(proto.variables)}
    out: dict[int, set[int]] = {}
    for var, coeff in zip(proto.objective.vars, proto.objective.coeffs, strict=True):
        match = re.fullmatch(r"consec_(\d+)_\d+_\d+_\d+_\d+", names[var])
        if match:
            out.setdefault(int(match.group(1)), set()).add(coeff)
    return out


def _meetings_per_section(model) -> dict[int, int]:
    """Section index -> its meeting count, read from the ``consec_*`` variable names."""
    count: dict[int, int] = {}
    for variable in model.proto.variables:
        match = re.fullmatch(r"consec_(\d+)_(\d+)_\d+_(\d+)_\d+", variable.name)
        if match:
            i, m_a, m_b = (int(g) for g in match.groups())
            count[i] = max(count.get(i, 0), m_a + 1, m_b + 1)
    return count


def test_cpsat_weight_helper_changes_only_policy_free_two_and_three_meeting_sections() -> None:
    from core.services.timetable_course_policy import ENGLISH_INTENSIVE_POLICY
    from core.services.timetable_solver import _consecutive_day_weight as weight

    two, three = {"pattern": [75, 75]}, {"pattern": [75, 75, 100]}
    assert weight(two, 40) == 40
    assert weight(two, 0) == 5  # the flag-off state: legacy weight
    assert weight(three, 40) == 40
    assert weight(three, 0) == 5
    assert weight({"pattern": [100]}, 40) == 5
    assert weight({"pattern": [75, 75, 75, 75]}, 40) == 5  # four meetings: legacy
    assert weight({**two, "meeting_policy": None}, 40) == 40
    assert weight({**three, "meeting_policy": None}, 40) == 40
    assert weight({**two, "meeting_policy": ENGLISH_INTENSIVE_POLICY}, 40) == 5
    assert weight({**three, "meeting_policy": ENGLISH_INTENSIVE_POLICY}, 40) == 5


@pytest.mark.django_db
def test_flag_on_cpsat_weights_two_and_three_meeting_sections(flag_on, capture_models) -> None:
    for name, model in capture_models(build_corpus(11)).items():
        coefficients = _consec_coefficients(model)
        meetings = _meetings_per_section(model)
        assert {2, 3} <= set(meetings.values()), name  # the corpus has both shapes
        for i, count in meetings.items():
            assert coefficients[i] == ({40} if count in (2, 3) else {5}), (name, i, count)


@pytest.mark.django_db
def test_flag_on_cpsat_model_grows_only_by_the_three_meeting_day_run_guard(
    flag_off, capture_models
) -> None:
    """The weights change the objective; the stronger rule (no three meetings on
    three consecutive days) adds exactly 3 booleans, 3 linear constraints and 3
    objective terms per policy-free three-meeting section, nothing else."""
    scenario = build_corpus(11)
    off = {name: _model_fingerprint(m) for name, m in capture_models(scenario).items()}
    flag_off.TIMETABLE_SECTION_DAY_SPREAD = True
    flag_off.TIMETABLE_SECTION_DAY_SPREAD_WEIGHT = 40
    size = ("objective_terms", "variables", "constraints")
    for name, model in capture_models(scenario).items():
        on = _model_fingerprint(model)
        three_meeting_sections = sum(1 for n in _meetings_per_section(model).values() if n == 3)
        assert three_meeting_sections > 0, name
        grown = 3 * three_meeting_sections
        assert {k: on[k] - off[name][k] for k in size} == dict.fromkeys(size, grown), name
        assert on["objective_sha256"] != off[name]["objective_sha256"], name


@pytest.mark.django_db
def test_flag_on_cpsat_uses_the_configured_weight(flag_on, capture_models) -> None:
    flag_on.TIMETABLE_SECTION_DAY_SPREAD_WEIGHT = 80
    for model in capture_models(build_corpus(11)).values():
        coefficients = _consec_coefficients(model)
        meetings = _meetings_per_section(model)
        assert all(
            coefficients[i] == ({80} if count in (2, 3) else {5}) for i, count in meetings.items()
        )


@pytest.mark.django_db
def test_a_negative_weight_is_read_as_zero_and_a_zero_weight_changes_no_coefficient(
    flag_on, capture_models
) -> None:
    """Weight 0 is the legacy 5 on every ``consec_*`` variable. The three-in-a-row
    rule is the flag's, not the weight's, so the guard variables are still added."""
    from core.services.timetable_flags import get_section_day_spread_weight

    scenario = build_corpus(11)
    flag_on.TIMETABLE_SECTION_DAY_SPREAD_WEIGHT = -7
    assert get_section_day_spread_weight() == 0
    for name, model in capture_models(scenario).items():
        assert all(coeffs == {5} for coeffs in _consec_coefficients(model).values()), name


@pytest.mark.django_db
def test_flag_helpers_read_the_settings(settings) -> None:
    from core.services.timetable_flags import (
        get_section_day_spread_weight,
        is_section_day_spread_enabled,
    )

    settings.TIMETABLE_SECTION_DAY_SPREAD = False
    assert is_section_day_spread_enabled() is False
    settings.TIMETABLE_SECTION_DAY_SPREAD = True
    assert is_section_day_spread_enabled() is True
    settings.TIMETABLE_SECTION_DAY_SPREAD_WEIGHT = 25
    assert get_section_day_spread_weight() == 25


def test_the_flag_is_off_by_default_and_production_turns_it_on() -> None:
    # Default OFF in settings; the owner turned it on for production (2026-10-26)
    # explicitly in render.yaml, so removing that line restores the old placement.
    settings_source = (REPO / "config" / "settings.py").read_text(encoding="utf-8")
    assert re.search(
        r'TIMETABLE_SECTION_DAY_SPREAD = os\.getenv\(\s*"TIMETABLE_SECTION_DAY_SPREAD",\s*"false"',
        settings_source,
    )
    render = (REPO / "render.yaml").read_text(encoding="utf-8")
    assert re.search(r'key: TIMETABLE_SECTION_DAY_SPREAD\s*\n\s*value: "true"', render)


# ── nothing else reads the flag ───────────────────────────────────────

# The placers, plus the improvement stages that move a section's meetings (the
# three-in-a-row rule). Manual moves, validation, the evaluator, the day-off policy,
# the scheduler package and every view are NOT in this set, and must never be.
ALLOWED_READERS = {
    "config/settings.py",
    "core/services/timetable_flags.py",
    "core/services/timetable_autoplace.py",
    "core/services/timetable_solver.py",
    "core/services/timetable_local_search.py",
    "core/services/timetable_local_search_v2.py",
    "core/services/timetable_local_search_chains.py",
    "core/services/timetable_load_balanced.py",
    "core/services/timetable_cpsat_polisher.py",
    "core/services/timetable_secondary_recovery.py",
    "core/services/timetable_instructor_cap_repair.py",
    "core/services/timetable_instructor_compaction.py",
}
FLAG_NAMES = re.compile(
    r"is_section_day_spread_enabled|get_section_day_spread_weight"
    r"|TIMETABLE_SECTION_DAY_SPREAD|SECTION_DAY_SPREAD_(?:WEIGHT_)?SETTING"
)


def test_only_the_placers_and_the_meeting_moving_stages_read_the_flag() -> None:
    """Manual moves, validation, the evaluator, the day-off policy, the scheduler
    package and every view never see it."""
    readers = set()
    for root in ("config", "core", "scheduler"):
        for path in (REPO / root).rglob("*.py"):
            if "migrations" in path.parts:
                continue
            if FLAG_NAMES.search(path.read_text(encoding="utf-8", errors="replace")):
                readers.add(path.relative_to(REPO).as_posix())

    assert readers == ALLOWED_READERS


@pytest.mark.django_db
def test_the_flag_never_changes_manual_move_validation(settings) -> None:
    """A manual move is never refused for a clash, with or without the flag, and
    both answers (verdict and candidate previews) are identical."""
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_workspace import (
        preview_planned_section_slot_candidates,
        validate_placement,
    )

    for name, value in PRODUCTION_FLAGS.items():
        setattr(settings, name, value)
    settings.TIMETABLE_SECTION_DAY_SPREAD = False
    scenario = build_corpus(11)
    auto_place_scenario(scenario.id, strategy="compact")
    board = DeliveryBoard.objects.get(scenario=scenario, label="Term 2")
    placement = (
        SectionPlacement.objects.filter(board=board)
        .select_related("term_section")
        .order_by("id")
        .first()
    )
    sibling_days = list(
        SectionPlacement.objects.filter(term_section=placement.term_section)
        .exclude(id=placement.id)
        .values_list("day", flat=True)
    )
    order = ["SUN", "MON", "TUE", "WED", "THU"]
    # Move one meeting onto a day adjacent to its sibling: exactly the shape the
    # preference dislikes.
    adjacent = order[min(order.index(sibling_days[0]) + 1, 4)] if sibling_days else "MON"

    def answers():
        verdict = validate_placement(
            board.id,
            adjacent,
            "08:00",
            "09:15",
            "",
            placement.term_section_id,
            exclude_placement_id=placement.id,
        )
        previews = preview_planned_section_slot_candidates(
            board.id,
            course_code="CS112",
            course_key=planner_course_key("CS112", "PROGRAMMING I"),
            section_label="S9",
        )
        return json.loads(json.dumps({"verdict": verdict, "previews": previews}, default=str))

    off = answers()
    settings.TIMETABLE_SECTION_DAY_SPREAD = True
    settings.TIMETABLE_SECTION_DAY_SPREAD_WEIGHT = 40
    on = answers()

    assert on == off

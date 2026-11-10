"""Every automatic engine places a "2 x 100 min" course as two 100-minute lectures.

Owner, 2026-11-10: a 3-credit course may be taught, per scenario, as two meetings a
week on two different days, each in a 100-minute column, in a LECTURE room
(``ScenarioSectionBudget.meeting_pattern = "2x100"``). The foundation gave every
door the budget's pattern; these tests hold every automatic door to the shape on
one board (``tests/meeting_pattern_support.py``) where X = CS112 PROGRAMMING I is
configured, its same-code twin CS112 PROGRAMMING II is not, and AI201 is an
ordinary 4-credit course with a lab meeting:

- the greedy placer under each strategy, both CP-SAT builders, the V2 catalog,
  the one-teacher guard's copy of the placer options and a small V2 full run;
- the movers keep the shape (simulated annealing / load balancer relocate, LS-v2
  repattern), and CP-SAT's objective wants both 100-minute meetings in one window
  (term 3b), as the greedy's options already put them;
- the greedy scores a 2 x 100 option against every booking it overlaps, the
  repair panel calls its meetings lectures, the export names the shape and the V2
  section state asks for a lecture room.

An unconfigured course must behave exactly as before (the recorded goldens hold
that); a few tests here pin the configured-only conditions directly.
"""

from __future__ import annotations

import os
import re
import sys

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from ortools.sat.python import cp_model

from core.models import (
    DeliveryBoard,
    Room,
    ScenarioSectionBudget,
    SectionPlacement,
    TermSection,
    TimetableScenario,
)
from core.services.course_identity import planner_course_key
from core.services.timetable_meeting_shape import TWO_BY_100
from tests._online_windows_fixture import capture_guard_options, capture_relocate_domains
from tests.meeting_pattern_support import (
    DAYS,
    FLAGS,
    LAB_KEY,
    LONG_WINDOWS,
    SMALL_LAB_SLOT_CONFIG,
    TWIN_KEY,
    X_KEY,
    assert_scenario_shapes,
    assert_two_by_100,
    board_of,
    build_scenario,
    minutes,
    placed_sections,
    planned_sections,
    solver_shapes,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def flags(settings):
    for name, value in FLAGS.items():
        setattr(settings, name, value)
    return settings


def _cells(options) -> set[tuple[tuple[int, int, int], ...]]:
    """Placer options as the one-teacher guard keeps them: sorted (day, start, end)."""
    return {
        tuple(sorted((DAYS.index(m["day"]), minutes(m["start"]), minutes(m["end"])) for m in o))
        for o in options
    }


# ── the greedy placer, every strategy ────────────────────────────────────


@pytest.mark.parametrize(
    "strategy",
    ("compact", "morning", "balanced", "load_balanced", "hybrid", "adaptive", "optimal"),
)
def test_every_greedy_strategy_places_x_as_two_100_minute_lectures(flags, strategy) -> None:
    """Greedy, its annealing / load-balancing polish and adaptive's CP-SAT step:
    X is 2 x 100 in lecture rooms, its twin 2 x 75, AI201's lab in a lab room."""
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_run_seed import seeded_timetable_run

    scenario = build_scenario()
    with seeded_timetable_run(11):
        result = auto_place_scenario(scenario.id, strategy=strategy, candidate_gen_budget=1.0)

    assert result["total_placed"] == sum(planned_sections(scenario).values()), result
    assert_scenario_shapes(scenario)


# ── CP-SAT: both builders ────────────────────────────────────────────────


@pytest.mark.parametrize("builder", ("solve_board", "solve_board_with_hints"))
def test_both_cpsat_builders_place_x_as_two_100_minute_lectures(flags, builder) -> None:
    """One section a course on the small grid, so the solve is optimal within its
    budget on any machine; the 3b test below reads the full board's model."""
    from core.services.timetable_rooming import assign_rooms_to_board
    from core.services.timetable_solver import (
        persist_solver_result,
        solve_board,
        solve_board_with_hints,
    )

    scenario = build_scenario(planned={X_KEY: 1, TWIN_KEY: 1}, small_grid=True)
    board = board_of(scenario)
    if builder == "solve_board":
        result = solve_board(board.id, time_limit_seconds=3)
    else:
        result = solve_board_with_hints(board.id, [], time_limit_seconds=3)

    assert result["status"] in ("optimal", "feasible"), result["status"]
    shapes = solver_shapes(result)
    assert len(shapes[X_KEY]) == 1
    assert_two_by_100(shapes[X_KEY][0], builder)
    assert {(s, e) for _d, s, e in shapes[X_KEY][0]} <= {
        (s["start"], s["end"]) for s in SMALL_LAB_SLOT_CONFIG
    }
    assert [minutes(e) - minutes(s) for _d, s, e in shapes[TWIN_KEY][0]] == [75, 75]
    persist_solver_result(board.id, result)
    assign_rooms_to_board(board.id)
    assert_scenario_shapes(scenario)


def test_the_optimal_strategy_persists_cpsat_sections_of_x_as_two_100_minute_lectures(
    flags,
) -> None:
    """The "optimal" strategy's own CP-SAT solve, persist and rooming (not its
    compact fallback) on the small grid."""
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_run_seed import seeded_timetable_run

    scenario = build_scenario(planned={X_KEY: 1, TWIN_KEY: 1}, small_grid=True)
    with seeded_timetable_run(11):
        result = auto_place_scenario(scenario.id, strategy="optimal", candidate_gen_budget=3.0)

    assert result["boards"]["Term 1"].get("status") in ("optimal", "feasible"), result
    assert_scenario_shapes(scenario)


class _Captured(Exception):
    """Raised by the solve spy once a builder's model is complete."""


def _capture_model(monkeypatch, builder: str, board: DeliveryBoard):
    """The builder's CP-SAT model and, per section index, (course_key, options per meeting)."""
    from core.services import timetable_solver

    models: list = []
    calls: list[tuple[str, int, list]] = []
    real_options = timetable_solver._scoped_meeting_options

    def options_spy(section, meeting_index, ordinary):
        options = real_options(section, meeting_index, ordinary)
        calls.append((section["code"], meeting_index, options))
        return options

    def solve_spy(self, model, *args, **kwargs):
        models.append(model)
        raise _Captured

    monkeypatch.setattr(timetable_solver, "_scoped_meeting_options", options_spy)
    monkeypatch.setattr(cp_model.CpSolver, "solve", solve_spy)
    with pytest.raises(_Captured):
        if builder == "solve_board":
            timetable_solver.solve_board(board.id, time_limit_seconds=1)
        else:
            timetable_solver.solve_board_with_hints(board.id, [], time_limit_seconds=1)
    sections: list[tuple[str, list]] = []
    for code, meeting_index, options in calls:
        if meeting_index == 0:
            sections.append((code, []))
        sections[-1][1].append(options)
    return models[-1], sections


def _tdiff_terms(model) -> dict[int, dict[tuple[int, int, int, int], int]]:
    """Section index -> {(m_a, o_a, m_b, o_b): objective coefficient} of its 3b terms."""
    proto = model.proto
    coefficients = dict(zip(proto.objective.vars, proto.objective.coeffs, strict=True))
    out: dict[int, dict] = {}
    for index, variable in enumerate(proto.variables):
        match = re.fullmatch(r"tdiff_(\d+)_(\d+)_(\d+)_(\d+)_(\d+)", variable.name)
        if match:
            i, m_a, o_a, m_b, o_b = (int(g) for g in match.groups())
            out.setdefault(i, {})[(m_a, o_a, m_b, o_b)] = coefficients[index]
    return out


@pytest.mark.parametrize("builder", ("solve_board", "solve_board_with_hints"))
def test_cpsat_objective_wants_both_100_minute_meetings_of_x_in_one_window(
    flags, monkeypatch, builder
) -> None:
    """Objective 3b compares X's two 100-minute meetings, as it compares a 2 x 75
    section's lectures: one term per pair of options in different windows, 10 per
    window apart. AI201 still compares only its two lectures, never its lab."""
    model, sections = _capture_model(monkeypatch, builder, board_of(build_scenario()))
    terms = _tdiff_terms(model)
    long_cells = {(day, start, end) for day in DAYS for start, end in LONG_WINDOWS}

    seen = set()
    for i, (key, options) in enumerate(sections):
        seen.add(key)
        expected = {
            (0, o_a, 1, o_b): 10 * abs(a[1] - b[1])
            for o_a, a in enumerate(options[0])
            for o_b, b in enumerate(options[1])
            if a[1] != b[1]
        }
        assert terms.get(i) == expected, (key, i)
        if key == X_KEY:
            # Every placeable 100-minute cell, never an online-only window.
            assert [{opt[2:5] for opt in meeting} for meeting in options] == [long_cells] * 2
    assert seen == {X_KEY, TWIN_KEY, LAB_KEY}


@pytest.mark.parametrize("builder", ("solve_board", "solve_board_with_hints"))
def test_an_english_policy_wins_over_a_stored_shape_in_cpsat(flags, monkeypatch, builder) -> None:
    """ENG101 keeps its four-day bundles and gains no 3b term, even with "2x100"
    stored on a 3-credit budget row: the course meeting policy decides first."""
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="ENG")
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="Term 1", nominal_term=1, program="AI"
    )
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key="ENG101::ENGLISH",
        course_code="ENG101",
        course_name="English",
        department="AI",
        programme_term=1,
        credit_hours=3,
        planned_sections=2,
        max_per_section=25,
        total_demand=50,
        meeting_pattern=TWO_BY_100,
    )
    model, sections = _capture_model(monkeypatch, builder, board)

    assert [len(options) for _key, options in sections] == [4, 4]
    assert _tdiff_terms(model) == {}


# ── the V2 catalog and the one-teacher guard ─────────────────────────────


def test_the_v2_catalog_builds_the_100_100_family_for_x_and_75_75_for_its_twin(
    flags, monkeypatch
) -> None:
    from core.services import timetable_optimizer_v2, timetable_pattern_catalog

    scenario = build_scenario()
    requirements: list[dict] = []
    real = timetable_pattern_catalog.build_canonical_pattern_catalog

    def spy(course_requirements, **kwargs):
        requirements.extend(course_requirements)
        return real(course_requirements=course_requirements, **kwargs)

    monkeypatch.setattr(timetable_pattern_catalog, "build_canonical_pattern_catalog", spy)
    catalog = timetable_optimizer_v2._build_pattern_catalog_for_scenario(scenario.id)

    durations = {req["course_key"]: req["durations"] for req in requirements}
    assert durations == {X_KEY: [100, 100], TWIN_KEY: [75, 75], LAB_KEY: [75, 75, 100]}
    family = catalog["ONCAMPUS_LEC_100_100"]
    assert len(family) == len(LONG_WINDOWS) * 10  # every window x every pair of days
    for pattern in family:
        assert len({m.day for m in pattern.meetings}) == 2
        assert {(m.start_min, m.end_min) for m in pattern.meetings} <= {
            (minutes(s), minutes(e)) for s, e in LONG_WINDOWS
        }
    assert all(
        m.end_min - m.start_min == 75 for p in catalog["ONCAMPUS_LEC_75_75"] for m in p.meetings
    )


def test_the_one_teacher_guard_offers_x_exactly_the_greedy_options(flags, monkeypatch) -> None:
    from core.services import timetable_autoplace

    scenario = build_scenario()
    greedy: dict[str, list] = {}
    real = timetable_autoplace.generate_course_meeting_options

    def spy(*args, **kwargs):
        options = real(*args, **kwargs)
        if sys._getframe(1).f_code.co_name == "auto_place_board":
            greedy[kwargs["course_key"]] = options
        return options

    monkeypatch.setattr(timetable_autoplace, "generate_course_meeting_options", spy)
    timetable_autoplace.auto_place_board(board_of(scenario).id, strategy="compact")
    guard = {
        key: {tuple(tuple(cell) for cell in option) for option in options}
        for key, options in capture_guard_options(scenario).items()
    }

    assert set(greedy) == {X_KEY, TWIN_KEY, LAB_KEY}
    for key in greedy:
        assert guard[key] == _cells(greedy[key]), key
    assert len(guard[X_KEY]) == len(LONG_WINDOWS) * 10
    for option in guard[X_KEY]:
        assert len({day for day, _s, _e in option}) == 2
        assert all((end - start) == 100 for _d, start, end in option), option
    assert all(end - start == 75 for option in guard[TWIN_KEY] for _d, start, end in option)


# ── the movers ───────────────────────────────────────────────────────────


def test_annealing_and_the_load_balancer_move_x_only_between_100_minute_windows(
    flags, monkeypatch
) -> None:
    from core.services.timetable_autoplace import auto_place_scenario

    scenario = build_scenario()
    auto_place_scenario(scenario.id, strategy="compact")
    windows = [
        [day, start, end, index] for day in DAYS for index, (start, end) in enumerate(LONG_WINDOWS)
    ]

    stages = capture_relocate_domains(monkeypatch, board_of(scenario))
    assert set(stages) == {"sa", "lb"}
    for stage, rows in stages.items():
        x_rows = {key: row for key, row in rows.items() if key.split("|")[0] == X_KEY}
        assert len(x_rows) == planned_sections(scenario)[X_KEY], stage
        for key, row in x_rows.items():
            assert row["domain"] == [windows, windows], (stage, key)
            assert_two_by_100([tuple(m[:3]) for m in row["current"]], (stage, key))


def test_ls_v2_repatterns_x_only_within_its_100_100_family(flags) -> None:
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_local_search_v2 import generate_all_repattern_moves
    from core.services.timetable_optimizer_v2 import (
        _build_pattern_catalog_for_scenario,
        build_section_states_for_scenario,
    )

    scenario = build_scenario()
    auto_place_scenario(scenario.id, strategy="compact")
    sections = {s.section_id: s for s in build_section_states_for_scenario(scenario.id)}
    catalog = _build_pattern_catalog_for_scenario(scenario.id)
    patterns = {p.pattern_id: p for family in catalog.values() for p in family}

    moves = generate_all_repattern_moves(sections, catalog)
    x_moves = [m for m in moves if sections[m.section_id_a].course_code == X_KEY]
    assert {m.section_id_a for m in x_moves} == {
        sid for sid, s in sections.items() if s.course_code == X_KEY
    }  # neither section is frozen
    for move in x_moves:
        target = patterns[move.to_pattern_id_a]
        assert target.pattern_family == "ONCAMPUS_LEC_100_100"
        assert_two_by_100(
            [(DAYS[m.day], _hm(m.start_min), _hm(m.end_min)) for m in target.meetings], move
        )


def _hm(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def test_a_small_v2_full_run_keeps_x_two_by_100_in_lecture_rooms(flags) -> None:
    from core.services.timetable_optimizer_v2 import optimise_scenario_timetable_v2

    scenario = build_scenario()
    result = optimise_scenario_timetable_v2(
        scenario.id,
        strategies=["compact", "optimal"],
        max_search_iterations=5,
        max_chain_iterations=2,
        cpsat_time_limit=2.0,
        cpsat_hotspot_only=True,
        chain_time_limit_seconds=2.0,
    )

    assert result.get("status") != "error", result
    assert_scenario_shapes(scenario)


def test_the_v2_section_state_asks_a_lecture_room_only_for_a_configured_shape(flags) -> None:
    """A configured 2 x 100 section is a lecture section. The same placed meetings
    of an unconfigured course keep today's duration-majority label ("lab")."""
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_optimizer_v2 import build_section_states_for_scenario

    scenario = build_scenario()
    auto_place_scenario(scenario.id, strategy="compact")

    def room_types() -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for state in build_section_states_for_scenario(scenario.id):
            out.setdefault(state.course_code, set()).add(state.room_type_required)
        return out

    assert room_types() == {X_KEY: {"lecture"}, TWIN_KEY: {"lecture"}, LAB_KEY: {"lecture"}}
    ScenarioSectionBudget.objects.filter(scenario=scenario, course_key=X_KEY).update(
        meeting_pattern=""
    )
    assert room_types()[X_KEY] == {"lab"}


# ── greedy room scoring: the whole 100 minutes ───────────────────────────


def _one_lecture_room_board(*, credit_hours: int, meeting_pattern: str) -> TimetableScenario:
    """One lecture room, held 10:30-11:45 every day by another board's section.

    A 100-minute column from 09:00 or 10:45 overlaps that booking although no
    booking starts at either time.
    """
    scenario = TimetableScenario.objects.create(academic_year="1448", term="2", name="one room")
    DeliveryBoard.objects.create(
        scenario=scenario, label="Term 1", nominal_term=1, program="AI", display_order=1
    )
    other = DeliveryBoard.objects.create(
        scenario=scenario, label="Term 3", nominal_term=3, program="AI", display_order=2
    )
    Room.objects.create(room_code="L1", capacity=40, room_type="lecture", department="AI")
    held = TermSection.objects.create(
        scenario=scenario,
        course_code="AI300",
        course_number="AI300",
        course_key="AI300",
        course_name="Held",
        section="S1",
        available_capacity=30,
        source_tag="test",
    )
    for day in DAYS:
        SectionPlacement.objects.create(
            board=other,
            term_section=held,
            day=day,
            start_time="10:30",
            end_time="11:45",
            room="L1",
        )
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key="STAT103",
        course_code="STAT103",
        course_name="Statistics",
        department="AI",
        programme_term=1,
        credit_hours=credit_hours,
        planned_sections=1,
        max_per_section=30,
        total_demand=20,
        meeting_pattern=meeting_pattern,
    )
    return scenario


def _stat103(scenario: TimetableScenario) -> list[tuple]:
    return placed_sections(scenario)["STAT103"]["S1"]


def test_greedy_scores_a_2x100_option_against_every_booking_it_overlaps(flags) -> None:
    """09:00-10:40 and 10:45-12:25 both overlap the room's 10:30 lecture: an
    exact-start check thinks the room free there, places X at 09:00 and leaves it
    UNASSIGNED. Scored over the whole meeting, X takes 13:00 and gets the room."""
    from core.services.timetable_autoplace import auto_place_board

    scenario = _one_lecture_room_board(credit_hours=3, meeting_pattern=TWO_BY_100)
    auto_place_board(board_of(scenario).id, strategy="compact")

    meetings = _stat103(scenario)
    assert_two_by_100(meetings)
    assert {(start, room) for _day, start, _end, room in meetings} == {("13:00", "L1")}


def test_an_unconfigured_course_keeps_the_exact_start_room_check(flags) -> None:
    """The byte-identical contract: a 2-credit 100-minute course is scored exactly
    as before, at the exact start only, so it still takes 09:00 (where the room is
    in fact held from 10:30 and the meeting is left UNASSIGNED; a deferred gap)."""
    from core.services.timetable_autoplace import auto_place_board

    scenario = _one_lecture_room_board(credit_hours=2, meeting_pattern="")
    auto_place_board(board_of(scenario).id, strategy="compact")

    ((_day, start, end, room),) = _stat103(scenario)
    assert (start, end, room) == ("09:00", "10:40", "UNASSIGNED")


# ── the repair panel's move scope ────────────────────────────────────────


def _repair_scenario() -> tuple[TimetableScenario, DeliveryBoard]:
    """X and its twin, each placed SUN + TUE 09:00-10:40 (two 100-minute meetings)."""
    scenario = build_scenario()
    board = board_of(scenario)
    for key, name, room in ((X_KEY, "PROGRAMMING I", "L1"), (TWIN_KEY, "PROGRAMMING II", "L2")):
        section = TermSection.objects.create(
            scenario=scenario,
            course_code="CS112",
            course_number="CS112",
            course_key=key,
            course_name=name,
            section="S1",
            available_capacity=30,
            source_tag="test",
        )
        for day in ("SUN", "TUE"):
            SectionPlacement.objects.create(
                board=board,
                term_section=section,
                day=day,
                start_time="09:00",
                end_time="10:40",
                room=room,
            )
    return scenario, board


def _anchor(board: DeliveryBoard, key: str) -> SectionPlacement:
    return (
        SectionPlacement.objects.select_related("board__scenario", "term_section")
        .filter(board=board, term_section__course_key=key, day="SUN")
        .get()
    )


def test_lectures_only_moves_both_meetings_of_x_but_not_of_its_twin(flags) -> None:
    from core.services.timetable_repair import _placements_for_move_scope

    _scenario, board = _repair_scenario()
    x, twin = _anchor(board, X_KEY), _anchor(board, TWIN_KEY)

    included, excluded = _placements_for_move_scope(x, "lectures_only")
    assert sorted(p.day for p in included) == ["SUN", "TUE"] and excluded == []
    # The twin is not configured: two 100-minute meetings read as labs, as before,
    # so "Lectures only" falls back to the chosen meeting.
    included, excluded = _placements_for_move_scope(twin, "lectures_only")
    assert [p.day for p in included] == ["SUN"] and [p.day for p in excluded] == ["TUE"]


def test_the_repair_scope_payload_counts_x_meetings_as_lectures_from_one_budget_read(
    flags, monkeypatch
) -> None:
    from core.services import timetable_repair as repair_service

    _scenario, board = _repair_scenario()
    x = _anchor(board, X_KEY)
    # SUN -> MON / TUE: the TUE companion follows to WED / THU, inside the week.
    rows = [
        {"rank": 1, "kind": "lab", "day": "MON", "start": "13:00", "end": "14:40"},
        {"rank": 2, "kind": "lab", "day": "TUE", "start": "10:45", "end": "12:25"},
    ]
    monkeypatch.setattr(
        repair_service, "preview_placement_slot_candidates", lambda _pid: {"candidates": rows}
    )
    monkeypatch.setattr(
        repair_service,
        "_select_room_for_candidate",
        lambda *_args, **_kwargs: ("L3", [], {"selected_room": "L3", "policy_clean": True}),
    )
    monkeypatch.setattr(repair_service, "hard_feasibility_rejections", lambda *_a, **_k: [])

    with CaptureQueriesContext(connection) as queries:
        prepared = repair_service._prepare_repair_candidate_rows(
            x, limits={"max_candidates": 2}, move_scope="lectures_only"
        )

    budget_reads = [q for q in queries.captured_queries if "scenario_section_budgets" in q["sql"]]
    assert len(budget_reads) == 1, budget_reads
    assert len(prepared) == 2
    for row in prepared:
        payload = row["move_scope_payload"]
        assert len(payload["moves"]) == 2 and payload["excluded_placement_ids"] == []
        assert {move["kind"] for move in payload["moves"]} == {"lect"}
        assert payload["summary"] == {"lecture_sessions": 2, "lab_sessions": 0}


def test_the_repair_shape_lookup_never_guesses_between_two_named_courses(flags) -> None:
    """By CODE::NAME; a bare code only on a bare side and only for the scenario's one
    budget row of that code."""
    from core.services.timetable_repair import _placement_meeting_shape

    scenario, board = _repair_scenario()

    def shape_of(key: str, code: str = "CS112"):
        section = TermSection.objects.create(
            scenario=scenario,
            course_code=code,
            course_number=code,
            course_key=key,
            course_name="",
            section="S9",
            available_capacity=30,
            source_tag="test",
        )
        placement = SectionPlacement.objects.create(
            board=board, term_section=section, day="THU", start_time="13:00", end_time="14:40"
        )
        placement = SectionPlacement.objects.select_related("board", "term_section").get(
            pk=placement.pk
        )
        return _placement_meeting_shape(placement)

    assert shape_of(X_KEY).shape_id == TWO_BY_100
    assert shape_of(TWIN_KEY) is None
    assert shape_of("CS112") is None  # two CS112 identities: not guessed
    stat = planner_course_key("STAT103", "PRINCIPLES OF STATISTICS")
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key=stat,
        course_code="STAT103",
        course_name="PRINCIPLES OF STATISTICS",
        programme_term=1,
        credit_hours=3,
        meeting_pattern=TWO_BY_100,
    )
    assert shape_of("STAT103", "STAT103").shape_id == TWO_BY_100  # the one row of the code
    # Another name of the code is another course, even with one budget row.
    assert shape_of(planner_course_key("STAT103", "APPLIED STATISTICS"), "STAT103") is None


@pytest.mark.parametrize("move_scope", ("single_session", "all_sessions", "lectures_only"))
def test_a_section_with_no_meeting_over_80_minutes_reads_no_budget_for_repair(
    flags, monkeypatch, move_scope
) -> None:
    """Only a meeting over 80 minutes needs the course's shape (lecture or lab), so a
    2 x 75 section - configured or not - costs the repair panel no budget read."""
    from core.services import timetable_repair as repair_service

    _scenario, board = _repair_scenario()
    SectionPlacement.objects.filter(board=board).update(end_time="10:15")  # X and twin: 2 x 75
    rows = [{"rank": 1, "kind": "lect", "day": "MON", "start": "13:00", "end": "14:15"}]
    monkeypatch.setattr(
        repair_service, "preview_placement_slot_candidates", lambda _pid: {"candidates": rows}
    )
    monkeypatch.setattr(
        repair_service,
        "_select_room_for_candidate",
        lambda *_args, **_kwargs: ("L3", [], {"selected_room": "L3", "policy_clean": True}),
    )
    monkeypatch.setattr(repair_service, "hard_feasibility_rejections", lambda *_a, **_k: [])

    for key in (X_KEY, TWIN_KEY):
        anchor = _anchor(board, key)
        with CaptureQueriesContext(connection) as queries:
            prepared = repair_service._prepare_repair_candidate_rows(
                anchor, limits={"max_candidates": 1}, move_scope=move_scope
            )

        budget_reads = [
            q for q in queries.captured_queries if "scenario_section_budgets" in q["sql"]
        ]
        assert budget_reads == [], (key, budget_reads)
        moves = [move for row in prepared for move in row["move_scope_payload"]["moves"]]
        assert moves and {move["kind"] for move in moves} == {"lect"}, (key, moves)


def _move_scopes(detail: dict) -> tuple[set[tuple[int, ...]], set[str]]:
    """What a repair analysis's candidates move together, and what they call each move."""
    scopes = [candidate["metrics"]["move_scope"] for candidate in detail["candidates"]]
    return (
        {tuple(scope["included_placement_ids"]) for scope in scopes},
        {move["kind"] for scope in scopes for move in scope["moves"]},
    )


def test_a_changed_weekly_shape_is_analysed_again_never_served_from_the_repair_cache(
    flags,
) -> None:
    """A shape change moves no placement, so the state fingerprint holds the shapes:
    the "Lectures only" run made under the old shape is neither served again nor
    approvable (stale), and going back to it serves that run again."""
    from core.services.timetable_repair import analyse_timetable_repair, repair_run_detail

    scenario, board = _repair_scenario()
    twin = _anchor(board, TWIN_KEY)
    companion = SectionPlacement.objects.get(board=board, term_section=twin.term_section, day="TUE")
    budget = ScenarioSectionBudget.objects.get(scenario=scenario, course_key=TWIN_KEY)

    def analyse() -> dict:
        return analyse_timetable_repair(placement_id=twin.id, move_scope="lectures_only")

    def set_pattern(value: str) -> None:
        budget.meeting_pattern = value
        budget.save(update_fields=["meeting_pattern"])

    def freshness(detail: dict) -> str:
        return repair_run_detail(detail["run"]["id"])["run_freshness"]["status"]

    standard = analyse()
    assert standard["cache"]["hit"] is False and standard["candidates"]
    # Unconfigured, two 100-minute meetings read as labs: only the anchor moves.
    assert _move_scopes(standard) == ({(twin.id,)}, {"lab"})

    set_pattern(TWO_BY_100)
    shaped = analyse()
    assert shaped["cache"]["hit"] is False
    assert shaped["cache"]["fingerprint"] != standard["cache"]["fingerprint"]
    # Now both meetings are lectures and move together.
    assert _move_scopes(shaped) == ({(twin.id, companion.id)}, {"lect"})
    assert freshness(standard) == "stale"
    assert freshness(shaped) == "fresh"

    # Back to the credit default: the state is the first run's again.
    set_pattern("")
    back = analyse()
    assert back["cache"] == {**standard["cache"], "hit": True}
    assert _move_scopes(back) == ({(twin.id,)}, {"lab"})
    assert freshness(shaped) == "stale"


def test_the_repair_fingerprint_names_the_shapes_only_when_a_course_has_one(
    flags, monkeypatch
) -> None:
    """Every scenario without a shape keeps the fingerprint it always had: its payload
    holds exactly the keys it held before shapes existed."""
    from core.services import timetable_repair as repair_service

    scenario, _board = _repair_scenario()
    real_dumps = repair_service.json.dumps
    payloads: list[dict] = []

    def spy(value, *args, **kwargs):
        if isinstance(value, dict) and {"rooms", "term_sections"} <= set(value):
            payloads.append(value)
        return real_dumps(value, *args, **kwargs)

    monkeypatch.setattr(repair_service.json, "dumps", spy)
    configured = repair_service._repair_state_fingerprint(scenario.id, [])
    ScenarioSectionBudget.objects.filter(scenario=scenario).update(meeting_pattern="")
    unconfigured = repair_service._repair_state_fingerprint(scenario.id, [])

    assert configured != unconfigured
    # (course_key, course_code, meeting_pattern, credit_hours) of each configured row.
    assert payloads[0]["meeting_patterns"] == [(X_KEY, "CS112", TWO_BY_100, 3)]
    assert set(payloads[1]) == {
        "version",
        "scenario_id",
        "blocked_ids",
        "placements",
        "term_sections",
        "assignments",
        "scenario_requests",
        "students",
        "prerequisites",
        "rooms",
    }


# ── the export's Summary sheet ───────────────────────────────────────────


def test_the_export_summary_names_the_configured_shape(flags) -> None:
    from openpyxl import load_workbook

    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_export import export_scenario_xlsx

    scenario = build_scenario()
    auto_place_scenario(scenario.id, strategy="compact")
    sheet = load_workbook(export_scenario_xlsx(scenario.id))["Summary"]

    rows = [[cell.value for cell in row] for row in sheet.iter_rows()]
    header = next(
        i for i, row in enumerate(rows) if row[:4] == ["Course", "Term", "Credits", "Meetings/wk"]
    )
    meetings = sorted((row[0], row[3]) for row in rows[header + 1 :] if row[0])
    # One CS112 is X (configured), the other its twin (the credit default's count).
    assert meetings == [("AI201", "3x"), ("CS112", "2 × 100 min"), ("CS112", "2x")]


# ── the Rooms sheets of both exports ─────────────────────────────────────

ROOM_NOTE = "Also in this room, outside these time slots: "


def _rooms_scenario(x_pattern: str = TWO_BY_100) -> TimetableScenario:
    """X S1 meets MON + WED 10:45-12:25 in lecture room L1, where no lecture column
    of the default grid starts; X S2 meets SUN + TUE 09:00-10:40 in L1, at a lecture
    column's start; the twin (no shape) S1 meets MON + WED 10:45-12:25 in L2."""
    scenario = build_scenario(x_pattern=x_pattern)
    board = board_of(scenario)
    for key, name, label, days, start, end, room in (
        (X_KEY, "PROGRAMMING I", "S1", ("MON", "WED"), "10:45", "12:25", "L1"),
        (X_KEY, "PROGRAMMING I", "S2", ("SUN", "TUE"), "09:00", "10:40", "L1"),
        (TWIN_KEY, "PROGRAMMING II", "S1", ("MON", "WED"), "10:45", "12:25", "L2"),
    ):
        section = TermSection.objects.create(
            scenario=scenario,
            course_code="CS112",
            course_number="CS112",
            course_key=key,
            course_name=name,
            section=label,
            available_capacity=30,
            source_tag="test",
        )
        for day in days:
            SectionPlacement.objects.create(
                board=board,
                term_section=section,
                day=day,
                start_time=start,
                end_time=end,
                room=room,
            )
    return scenario


def _note_under(ws, room: str) -> str | None:
    """The note row under ``room``'s table (header, column row, five days), if any."""
    header = next(
        row[0].row for row in ws.iter_rows(min_col=1, max_col=1) if row[0].value == f"{room} (40)"
    )
    value = ws.cell(row=header + 7, column=1).value
    if not (isinstance(value, str) and value.startswith(ROOM_NOTE)):
        return None
    assert any(  # one merged row across the room's table
        rng.min_row == rng.max_row == header + 7 and rng.min_col == 1 and rng.max_col > 1
        for rng in ws.merged_cells.ranges
    )
    return value


def _notes(ws) -> list[str]:
    return [
        cell.value
        for row in ws.iter_rows(min_col=1, max_col=1)
        for cell in row
        if isinstance(cell.value, str) and cell.value.startswith(ROOM_NOTE)
    ]


def _cells_of(ws, room: str) -> list[str]:
    """Every filled day cell of ``room``'s table."""
    header = next(
        row[0].row for row in ws.iter_rows(min_col=1, max_col=1) if row[0].value == f"{room} (40)"
    )
    return [
        str(cell.value).replace("\r\n", "\n")
        for row in ws.iter_rows(min_row=header + 2, max_row=header + 6, min_col=2)
        for cell in row
        if cell.value
    ]


def _scenario_rooms_sheet(scenario: TimetableScenario):
    from openpyxl import load_workbook

    from core.services.timetable_export import export_scenario_xlsx

    path = export_scenario_xlsx(scenario.id)
    try:
        return load_workbook(path)["Rooms"]
    finally:
        os.unlink(path)


def _per_plan_rooms_sheet(scenario: TimetableScenario):
    from tests.test_per_plan_export_grouping import _export

    return _export(scenario)["AI"]["Rooms"]


@pytest.mark.parametrize("rooms_sheet", (_scenario_rooms_sheet, _per_plan_rooms_sheet))
def test_a_rooms_sheet_lists_the_100_minute_lectures_no_lecture_column_shows(
    flags, rooms_sheet
) -> None:
    """A lecture room's table draws the lecture columns only, and no lecture column
    starts at 10:45: X's 2 x 100 lectures there are listed under L1's table rather
    than dropped (L1 would read as free). X S2, at a lecture column's start, is drawn
    as before; the twin, which has no shape, is exactly as it was."""
    ws = rooms_sheet(_rooms_scenario())

    assert _note_under(ws, "L1") == (
        ROOM_NOTE + "CS112 S1 Monday 10:45-12:25; CS112 S1 Wednesday 10:45-12:25"
    )
    assert sorted(_cells_of(ws, "L1")) == ["CS112 S2", "CS112 S2"]
    assert _note_under(ws, "L2") is None
    assert _notes(ws) == [_note_under(ws, "L1")]


@pytest.mark.parametrize("rooms_sheet", (_scenario_rooms_sheet, _per_plan_rooms_sheet))
def test_a_rooms_sheet_without_a_shape_is_what_it_was(flags, rooms_sheet) -> None:
    """The same meetings with X at the credit default: no note (the byte-identical
    contract; the gap for courses with no shape is an older, recorded one)."""
    ws = rooms_sheet(_rooms_scenario(x_pattern=""))

    assert _notes(ws) == []
    assert _note_under(ws, "L1") is None

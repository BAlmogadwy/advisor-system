"""The timetable benchmark: a run is measured, then undone; the database copy rule holds.

``bench_timetable`` / ``core.services.timetable_benchmark`` measure Generate and Optimise
Current with the project's own evaluators and report keys. These tests pin the parts a
benchmark can silently get wrong: the guard that keeps it off the repository's database, the
rollback that makes repeats start from the same board, the per-stage recorder (time, "did it
change the board", no double counting, always restored), and the summary over repeats.

The engine itself is the one the week-closure tests use (a small board with one teacher).
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
import types
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from core.management.commands import bench_timetable
from core.models import SectionPlacement
from core.services import timetable_benchmark as bench
from tests.one_teacher_support import FLAG_ON
from tests.test_one_teacher_day_off_policy import RUN, _enrol
from tests.test_week_closure_guard import _board, _locked_but_x, _x_day

WEDNESDAY, WED_AFTER_P4 = 3, (555, 630)  # the optimiser's day index; 09:15-10:30
TUNING = {
    "max_iterations": RUN["max_iterations"],
    "run_chain": RUN["run_chain"],
    "run_cpsat": RUN["run_cpsat"],
    "max_chain_iterations": RUN["max_chain_iterations"],
}


@pytest.fixture
def flags(settings):
    for name, value in FLAG_ON.items():
        setattr(settings, name, value)
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY = True
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_TIME_BUDGET_SECONDS = 5
    settings.TIMETABLE_SECONDARY_RECOVERY_ENABLED = False
    return settings


def _fixture_board():
    sc, _teacher, x = _board()
    _enrol(sc, x)
    _locked_but_x(sc, x)
    return sc, x


def _search_that_moves_x(monkeypatch, x: str) -> None:
    """The optimiser's search, with a stand-in that moves X to Wednesday after P4 and is
    accepted, so the run really writes to the board."""
    from core.services import timetable_local_search_v2, timetable_optimizer_v2

    real = timetable_local_search_v2.diagnostic_driven_local_search

    def search(**kwargs):
        out = real(**kwargs)
        moved = kwargs["sections_by_id"][f"{x}_S1"]
        meeting = dataclasses.replace(
            moved.meetings[0], day=WEDNESDAY, start_min=WED_AFTER_P4[0], end_min=WED_AFTER_P4[1]
        )
        kwargs["sections_by_id"][f"{x}_S1"] = dataclasses.replace(moved, meetings=(meeting,))
        return dataclasses.replace(out)

    monkeypatch.setattr(timetable_local_search_v2, "diagnostic_driven_local_search", search)
    monkeypatch.setattr(timetable_optimizer_v2, "stage_is_better", lambda out, inp: out is not inp)


# ── the database guard ─────────────────────────────────────────────────────


def test_a_the_repository_database_is_refused_even_through_a_hard_link(tmp_path, settings):
    repo = tmp_path / "repo"
    repo.mkdir()
    real = repo / "db.sqlite3"
    real.write_bytes(b"")
    settings.BASE_DIR = repo
    with pytest.raises(CommandError, match="repository's db.sqlite3"):
        bench_timetable.point_default_database_at(str(real))
    try:
        os.link(real, tmp_path / "alias.sqlite3")
    except OSError:  # a filesystem without hard links
        pytest.skip("no hard links here")
    with pytest.raises(CommandError, match="repository's db.sqlite3"):
        bench_timetable.point_default_database_at(str(tmp_path / "alias.sqlite3"))


def test_a2_a_main_checkout_database_is_refused_from_a_worktree(tmp_path, settings):
    """A worktree has no db.sqlite3 of its own; the real one sits in the main checkout."""
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    real = main / "db.sqlite3"
    real.write_bytes(b"")
    work = tmp_path / "work"
    work.mkdir()
    (work / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'wt'}\n", encoding="utf-8")
    settings.BASE_DIR = work
    with pytest.raises(CommandError, match="repository's db.sqlite3"):
        bench_timetable.point_default_database_at(str(real))
    # and a db.sqlite3 next to a manage.py is a repository database whichever checkout it is
    other = tmp_path / "other"
    other.mkdir()
    (other / "manage.py").write_text("", encoding="utf-8")
    (other / "db.sqlite3").write_bytes(b"")
    with pytest.raises(CommandError, match="next to a manage.py"):
        bench_timetable.point_default_database_at(str(other / "db.sqlite3"))


def test_a2b_a_relative_gitdir_is_read_against_the_worktree(tmp_path, settings):
    """Newer git writes the worktree's gitdir relative to the worktree."""
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    real = main / "db.sqlite3"
    real.write_bytes(b"")
    work = tmp_path / "work"
    work.mkdir()
    (work / ".git").write_text("gitdir: ../main/.git/worktrees/wt" + chr(10), encoding="utf-8")
    settings.BASE_DIR = work
    assert real.resolve() in [p.resolve() for p in bench_timetable._repository_databases()]
    with pytest.raises(CommandError, match="repository's db.sqlite3"):
        bench_timetable.point_default_database_at(str(real))


def test_a3_a_missing_file_is_refused_and_the_command_stops_before_running(tmp_path):
    with pytest.raises(CommandError, match="not an existing file"):
        call_command("bench_timetable", database=str(tmp_path / "nope.sqlite3"), scenarios=[1])


# ── flags ──────────────────────────────────────────────────────────────────


def test_b_production_flags_are_the_timetable_values_render_yaml_sets():
    flags = bench.production_flags()
    assert flags["TIMETABLE_ONE_TEACHER_PER_SECTION"] is True
    assert flags["TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY"] is True
    assert flags["TIMETABLE_INSTRUCTOR_AVAILABILITY"] is True
    assert isinstance(flags["TIMETABLE_CPSAT_WORKERS"], int)
    assert flags["TIMETABLE_WORKSPACE_JSON_GZIP"] is False
    assert all(key.startswith("TIMETABLE_") for key in flags)
    assert "DJANGO_SECRET_KEY" not in flags


def test_b2_overrides_are_typed_and_a_malformed_one_is_refused():
    assert bench.parse_overrides(["A=true", "B=3", "C=2.5", "D=x"]) == {
        "A": True,
        "B": 3,
        "C": 2.5,
        "D": "x",
    }
    with pytest.raises(ValueError):
        bench.parse_overrides(["no-equals-sign"])


# ── the stage recorder ─────────────────────────────────────────────────────


@pytest.fixture
def fake_stages(monkeypatch):
    module = types.ModuleType("bench_fake_stages")
    monkeypatch.setitem(sys.modules, "bench_fake_stages", module)

    def improving(*, best_candidate):
        return types.SimpleNamespace(lexicographic_score=(0, 1))

    def idle(*, best_candidate):
        return best_candidate

    def outer():
        return sys.modules["bench_fake_stages"].inner()  # through the module, as the engine does

    def inner():
        return "inner"

    def boom():
        raise RuntimeError("stage failed")

    def slow():
        time.sleep(0.05)

    module.improving, module.idle, module.outer, module.inner, module.boom, module.slow = (
        improving,
        idle,
        outer,
        inner,
        boom,
        slow,
    )
    monkeypatch.setattr(
        bench,
        "STAGE_FUNCTIONS",
        (
            ("bench_fake_stages", "improving", "search", "score"),
            ("bench_fake_stages", "idle", "chain", "score"),
            ("bench_fake_stages", "outer", "outer", "time"),
            ("bench_fake_stages", "inner", "inner", "time"),
            ("bench_fake_stages", "boom", "boom", "time"),
            ("bench_fake_stages", "slow", "slow", "time"),
        ),
    )
    return module


@pytest.mark.django_db
def test_c_the_recorder_times_each_stage_and_says_whether_it_changed_the_board(fake_stages):
    candidate = types.SimpleNamespace(lexicographic_score=(0, 5))
    with bench.StageRecorder(1) as recorder:
        assert fake_stages.improving(best_candidate=candidate).lexicographic_score == (0, 1)
        assert fake_stages.idle(best_candidate=candidate) is candidate
    totals = recorder.totals()
    assert totals["search"]["changed"] is True
    assert totals["chain"]["changed"] is False  # ran, changed nothing
    assert totals["search"]["calls"] == totals["chain"]["calls"] == 1
    with bench.StageRecorder(1) as slow:
        fake_stages.slow()
    assert slow.totals()["slow"]["seconds"] >= 0.04  # the stage's own time is what is reported


@pytest.mark.django_db
def test_c2_a_stage_inside_another_is_not_counted_twice_and_a_timed_only_stage_is_not_judged(
    fake_stages,
):
    with bench.StageRecorder(1) as recorder:
        assert fake_stages.outer() == "inner"
    totals = recorder.totals()
    assert set(totals) == {"outer"}
    assert totals["outer"]["changed"] is None  # timed only: neither changed nor not


@pytest.mark.django_db
def test_c3_every_function_is_restored_even_when_a_stage_raises(fake_stages):
    originals = {name: getattr(fake_stages, name) for name in ("improving", "idle", "boom")}
    from core.services import timetable_v2_runner

    swap = timetable_v2_runner._RunTeachers.swap
    with pytest.raises(RuntimeError, match="stage failed"):
        with bench.StageRecorder(1) as recorder:
            assert fake_stages.boom is not originals["boom"]  # patched inside
            fake_stages.boom()
    assert recorder.calls[-1]["stage"] == "boom" and recorder.calls[-1]["changed"] is None
    for name, original in originals.items():
        assert getattr(fake_stages, name) is original
    assert timetable_v2_runner._RunTeachers.swap is swap


@pytest.mark.django_db
def test_c4_a_probe_that_fails_never_reaches_the_engine_or_blinds_later_stages(fake_stages):
    """A stage whose input the probe cannot read still runs, returns what it returns, and is
    timed (not judged); the recorder is not left 'inside' that stage."""
    with bench.StageRecorder(1) as recorder:
        # no `lexicographic_score` on the input: the "score" probe raises before the call
        assert fake_stages.idle(best_candidate=object()) is not None
        assert fake_stages.outer() == "inner"  # a later stage is still recorded
    totals = recorder.totals()
    assert totals["chain"]["calls"] == 1 and totals["chain"]["changed"] is None
    assert "outer" in totals


def test_c5_every_instrumented_stage_exists_on_the_real_modules():
    """A renamed engine stage must fail here, not silently drop out of the timings."""
    import importlib

    from core.services import timetable_v2_runner

    for module_name, attribute, stage, _probe in bench.STAGE_FUNCTIONS:
        assert hasattr(importlib.import_module(module_name), attribute), (stage, attribute)
    for method, stage, _mode in bench.TEACHER_STAGES:
        assert hasattr(timetable_v2_runner._RunTeachers, method), (stage, method)


# ── one run on a real board ────────────────────────────────────────────────


@pytest.mark.django_db
def test_d_a_run_is_measured_and_the_database_is_left_as_it_was(flags, monkeypatch):
    sc, x = _fixture_board()
    _search_that_moves_x(monkeypatch, x)
    day_before = _x_day(sc, x)
    placements_before = sorted(
        SectionPlacement.objects.filter(board__scenario=sc).values_list("id", "day", "start_time")
    )

    row = bench.run_once(sc.id, "optimise_current", seed=7, tuning=TUNING, repeat=2)

    assert row["scenario_id"] == sc.id and row["action"] == "optimise_current"
    assert row["repeat"] == 2 and row["seed"] == 7
    assert row["error"] is None and row["outcome"] in ("kept", "rolled_back")
    assert row["flags"]["one_teacher_per_section"] is True
    # the run really wrote (a meeting moved) ... and the benchmark undid it
    assert row["board_changed"] is True and row["meetings_moved"] >= 1
    assert _x_day(sc, x) == day_before
    assert (
        sorted(
            SectionPlacement.objects.filter(board__scenario=sc).values_list(
                "id", "day", "start_time"
            )
        )
        == placements_before
    )
    assert row["rolled_back_to_saved_board"] is True
    # the stage columns are filled from the run itself
    assert row["stage_calls"]["search"] == 1
    assert row["stage_seconds"]["search"] >= 0
    # a board stage that wrote says so, one that found nothing to do says that too
    assert row["stage_changed"]["persist"] is True
    assert row["stage_changed"]["clash_repair"] is False
    assert row["stage_applied"]["local_search_ran"] is True
    assert row["wall_seconds"] >= sum(row["stage_seconds"].values()) - 0.01
    # before/after are the project's own evaluator on the stored board
    before = row["before"]
    assert before["measurable"] is True and before["students"] == 3
    assert before["placed_meetings"] == 5 and before["teachers"] == 1
    assert before["required_sections_missing"] == 0
    assert row["delta"]["teacher_working_days"] == (
        row["after"]["teacher_working_days"] - before["teacher_working_days"]
    )


@pytest.mark.django_db
def test_d2_teacher_numbers_are_the_projects_instructor_schedule_metrics(flags):
    from core.services import section_teacher_assignment as sta
    from core.services.timetable_optimisation_policy import instructor_schedule_metrics
    from core.services.timetable_optimizer_v2 import build_section_states_for_scenario

    sc, _x = _fixture_board()
    measured = bench.measure_board(sc.id)
    expected = instructor_schedule_metrics(
        build_section_states_for_scenario(sc.id), sta.load_assignment(sc.id).v2_map()
    )
    assert measured["teacher_working_days"] == expected["working_days"]
    assert measured["teacher_idle_minutes"] == expected["idle_minutes"]
    assert measured["teachers_on_five_days"] == sum(
        1 for days in expected["working_days_by_instructor"].values() if days >= 5
    )


@pytest.mark.django_db
def test_d3_a_rolled_back_run_is_reported_with_its_blocking_reasons(flags, monkeypatch):
    from core.services import timetable_v2_runner

    sc, x = _fixture_board()
    _search_that_moves_x(monkeypatch, x)
    real = timetable_v2_runner.optimiser_student_outcome_regression

    def blocked(result):
        out = real(result)
        return {
            "blocked": True,
            "regressions": [*out["regressions"], {"metric": "bench_forced", "label": "forced"}],
        }

    monkeypatch.setattr(timetable_v2_runner, "optimiser_student_outcome_regression", blocked)
    row = bench.run_once(sc.id, "optimise_current", seed=1, tuning=TUNING)
    assert row["outcome"] == "rolled_back"
    assert "bench_forced" in row["blocking_reasons"]
    assert row["persist_action"] == "rolled_back_safety_regression"
    assert "rolled_back [" in bench.render_table([row])


@pytest.mark.django_db
def test_d4_an_engine_exception_is_a_row_not_a_crash_and_still_rolls_back(flags, monkeypatch):
    from core.services import timetable_v2_runner

    sc, x = _fixture_board()
    placements_before = SectionPlacement.objects.filter(board__scenario=sc).count()

    def explode(*args, **kwargs):
        SectionPlacement.objects.filter(board__scenario=sc).delete()  # written, THEN fails
        raise RuntimeError("engine blew up")

    monkeypatch.setattr(timetable_v2_runner, "run_v2_optimisation_guarded", explode)
    row = bench.run_once(sc.id, "optimise_current", seed=1, tuning=TUNING)
    assert row["outcome"] == "error" and "engine blew up" in row["error"]
    assert row["rolled_back_to_saved_board"] is None  # an error row makes no such claim
    assert SectionPlacement.objects.filter(board__scenario=sc).count() == placements_before


@pytest.mark.django_db
def test_d3b_hard_clashes_do_not_count_an_instructor_clash_twice(flags, monkeypatch):
    """The scenario-wide instructor count already includes the per-board ones."""
    from core.services import timetable_workspace

    sc, _x = _fixture_board()
    real = timetable_workspace.compute_scenario_safety_summary

    def summary(scenario_id, **kwargs):
        return {
            **real(scenario_id, **kwargs),
            "same_board_conflicts": {
                "overlaps": 9,
                "same_course_overlaps": 1,
                "instructors": 3,
                "rooms": 2,
            },
            "instructor_clashes_scenario": 4,
        }

    monkeypatch.setattr(timetable_workspace, "compute_scenario_safety_summary", summary)
    measured = bench.measure_board(sc.id)
    assert measured["hard_clashes"] == 1 + 2 + 4  # not 1 + 2 + 3 + 4
    assert measured["hard_clash_parts"]["instructor_clashes"] == 3


@pytest.mark.django_db
def test_d4b_a_board_the_evaluators_cannot_read_still_gets_its_engine_run(flags, monkeypatch):
    """A scenario that raises while being measured (IncompletePolicyPattern) is a measured-nothing
    row with the reason, and the engine's own outcome is still reported."""
    sc, x = _fixture_board()
    _search_that_moves_x(monkeypatch, x)

    def unreadable(scenario_id):
        raise ValueError("English board copies must each use the same complete weekly pattern")

    monkeypatch.setattr(bench, "measure_board", unreadable)
    row = bench.run_once(sc.id, "optimise_current", seed=1, tuning=TUNING)
    assert (
        row["before"]["measurable"] is False
        and "English board copies" in row["before"]["errors"][0]
    )
    assert row["after"]["measurable"] is False
    assert row["outcome"] in ("kept", "rolled_back")  # the engine ran
    assert row["stage_calls"]["search"] == 1
    assert row["delta"]["unresolved_students"] is None


@pytest.mark.django_db
def test_d5_generate_starts_from_an_empty_board_and_the_copy_is_unchanged(flags, monkeypatch):
    from core.services import timetable_v2_runner

    sc, _x = _fixture_board()
    placements_before = sorted(
        SectionPlacement.objects.filter(board__scenario=sc).values_list("id", "day", "start_time")
    )
    seen: list[tuple[str, int]] = []
    real = timetable_v2_runner.run_v2_optimisation_guarded

    def spy(scenario_id, **kwargs):
        seen.append((kwargs["mode"], SectionPlacement.objects.filter(board__scenario=sc).count()))
        return real(scenario_id, **kwargs)

    monkeypatch.setattr(timetable_v2_runner, "run_v2_optimisation_guarded", spy)
    row = bench.run_once(sc.id, "generate", seed=3, tuning=TUNING)
    assert row["outcome"] in ("kept", "rolled_back"), row.get("error")  # the engine really ran
    bench.run_once(sc.id, "optimise_current", seed=3, tuning=TUNING)
    # Generate hands the engine an empty board in "full" mode; Optimise Current the stored one
    assert seen == [("full", 0), ("current", 5)]
    assert row["action"] == "generate"
    assert row["before"]["placed_meetings"] == 5  # the stored board, measured before clearing
    assert (
        sorted(
            SectionPlacement.objects.filter(board__scenario=sc).values_list(
                "id", "day", "start_time"
            )
        )
        == placements_before
    )
    with pytest.raises(ValueError):
        bench.run_once(sc.id, "bogus", seed=1)


@pytest.mark.django_db
def test_d6_the_scheduler_comparison_row_goes_through_the_bridge_and_is_undone(flags, monkeypatch):
    import scheduler.bridge as bridge

    sc, _x = _fixture_board()
    calls: list[dict] = []

    def fake_build(scenario_id, **kwargs):
        from core.models import TermSection

        calls.append(
            {
                **kwargs,
                "scenario_id": scenario_id,
                "placed": SectionPlacement.objects.count(),
                "sections": TermSection.objects.filter(scenario_id=scenario_id).count(),
            }
        )
        SectionPlacement.objects.filter(board__scenario=sc).delete()  # it rebuilds the week
        return {"engine": "scheduler"}

    monkeypatch.setattr(bridge, "build_into_scenario", fake_build)
    row = bench.run_once(
        sc.id,
        "scheduler_generate",
        seed=5,
        tuning={"scheduler_seconds": 2.0, "scheduler_runs": 1, **TUNING},
    )
    (call,) = calls
    assert call["placed"] == 0 and call["seed"] == 5  # a fresh board; the run's seed
    assert call["sections"] == 0  # a new scaffold has no sections for the bridge to collide with
    assert isinstance(call["meeting_patterns"], dict) and call["meeting_patterns"]  # the budget's
    assert call["seconds"] == 2.0 and call["runs"] == 1
    assert call["gender"] == "M" and call["programs"]  # the scenario's own cohort
    assert row["action"] == "scheduler_generate" and row["outcome"] == "kept"
    assert row["after"]["placed_meetings"] == 0 and row["before"]["placed_meetings"] == 5
    assert row["rolled_back_to_saved_board"] is True
    assert "scheduler_generate" in bench.ALL_ACTIONS and "scheduler_generate" not in bench.ACTIONS


# ── the summary over repeats ───────────────────────────────────────────────


def _row(repeat, outcome, unresolved, wall, score, search_changed, *, dropped=()):
    return {
        "scenario_id": 5,
        "action": "optimise_current",
        "repeat": repeat,
        "outcome": outcome,
        "blocking_reasons": ["instructor_working_days"] if outcome == "rolled_back" else [],
        "stages_dropped": list(dropped),
        "second_try": {"kept": "first", "second_outcome": "not_better"} if repeat == 3 else None,
        "wall_seconds": wall,
        "meetings_moved": repeat,
        "before": {"score": [9], "unresolved_students": 10},
        "after": {"score": score, "unresolved_students": unresolved, "student_gap_minutes": 100},
        "stage_seconds": {"search": wall / 2, "chain": 4.0},
        "stage_changed": {"search": search_changed, "chain": False},
    }


def test_e_the_summary_reports_the_spread_not_one_number():
    rows = [
        _row(1, "kept", 2, 10.0, [1], True),
        _row(2, "kept", 4, 30.0, [2], True),
        _row(3, "rolled_back", 10, 20.0, [9], False, dropped=["search"]),
        {**_row(4, "error", 999, 999.0, [], False), "second_try": None},  # restored board
    ]
    (entry,) = bench.summarise(rows)
    assert entry["runs"] == 4 and entry["outcomes"] == {"kept": 2, "rolled_back": 1, "error": 1}
    assert entry["spread"]["unresolved_students"] == {"min": 2, "median": 4, "max": 10, "n": 3}
    assert entry["spread"]["wall_seconds"]["median"] == 20.0
    assert entry["runs_with_stage_dropped"] == 1 and entry["runs_with_second_try"] == 1
    assert entry["blocking_reasons"] == ["instructor_working_days"]
    assert entry["distinct_final_scores"] == 3  # no two of the three runs ended alike
    assert entry["stages"]["search"]["runs_changed"] == 2
    assert entry["stages"]["chain"]["runs_changed"] == 0  # ran in every run, changed nothing
    assert entry["stages"]["chain"]["runs_judged"] == 4  # stage tallies keep the errored run
    assert "chain" in bench.render_stage_table([entry])


# ── the command ────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_f_the_command_runs_the_repeats_and_writes_json(flags, monkeypatch, tmp_path, capsys):
    sc, x = _fixture_board()
    _search_that_moves_x(monkeypatch, x)
    # the real command repoints the connection at a copy; here the test database stays put
    monkeypatch.setattr(bench_timetable, "point_default_database_at", lambda path: Path(path))
    out = tmp_path / "out.json"
    call_command(
        "bench_timetable",
        database=str(tmp_path / "copy.sqlite3"),
        scenarios=[sc.id],
        action="optimise_current",
        flags="local",
        repeats=2,
        vary_seed=True,
        seed=10,
        max_iterations=3,
        max_chain_iterations=2,
        no_chain=True,
        no_cpsat=True,
        json_path=str(out),
    )
    shown = capsys.readouterr().out
    assert "optimise_current" in shown and "search" in shown
    data = json.loads(out.read_text(encoding="utf-8"))
    assert [row["seed"] for row in data["rows"]] == [10, 11]  # --vary-seed: seed + repeat
    assert [row["repeat"] for row in data["rows"]] == [1, 2]
    assert data["summary"][0]["runs"] == 2
    assert data["meta"]["tuning"]["run_chain"] is False and data["meta"]["flags_mode"] == "local"
    assert data["meta"]["effective_flags"]["one_teacher_per_section"] is True
    # every TIMETABLE_* setting in force is recorded, not only the ones render.yaml names
    settings_in_force = data["meta"]["effective_settings"]
    assert settings_in_force["TIMETABLE_ONE_TEACHER_PER_SECTION"] is True
    assert "TIMETABLE_INSTRUCTOR_DAILY_CAP" in settings_in_force
    assert _x_day(sc, x) == "SUN"  # both repeats were undone
    # production flags are render.yaml's, applied to the run; --set wins over them
    call_command(
        "bench_timetable",
        database=str(tmp_path / "copy.sqlite3"),
        scenarios=[sc.id],
        flags="production",
        overrides=["TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY=false"],
        max_iterations=3,
        no_chain=True,
        no_cpsat=True,
        json_path=str(out),
    )
    meta = json.loads(out.read_text(encoding="utf-8"))["meta"]
    assert meta["flags"]["TIMETABLE_ONE_TEACHER_PER_SECTION"] is True  # from render.yaml
    assert meta["flags"]["TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY"] is False  # the override
    assert meta["effective_flags"]["day_off_policy"] is False
    with pytest.raises(CommandError, match="--repeats"):
        call_command(
            "bench_timetable", database=str(tmp_path / "c.sqlite3"), scenarios=[1], repeats=0
        )
    with pytest.raises(CommandError, match="--scenarios"):
        call_command("bench_timetable", database=str(tmp_path / "c.sqlite3"))

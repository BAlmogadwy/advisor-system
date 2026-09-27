"""The CP-SAT replay behind the rooms parity tests answers only master's questions.

``exam_cpsat_replay`` stands in for the native solver, so a parity test can
hold our code to master's outputs on any platform. That is only sound if a
replayed answer really is the recorded one, if every input that can change an
answer is part of the question, and if a question master never asked - or
master's questions asked differently - fails the test instead of being
answered some other way.
"""

import json

import pytest
from ortools.sat.python import cp_model

from tests import exam_cpsat_replay
from tests.test_exam_linked_rooms_parity import MASTER_CPSAT

Failed = pytest.fail.Exception


def _model(bound: int = 4, hint: int = 1):
    model = cp_model.CpModel()
    x = model.new_int_var(0, 5, "x")
    b = model.new_bool_var("b")
    model.add(x + 2 * b <= bound)
    model.maximize(x + b)
    model.add_hint(b, hint)
    return model, x, b


def _solve(model, callback=None, **parameters):
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    solver.parameters.max_deterministic_time = 0.06
    solver.parameters.max_time_in_seconds = 5.0
    for name, value in parameters.items():
        setattr(solver.parameters, name, value)
    status = solver.solve(model, callback)
    return solver, status


@pytest.fixture
def recorded(tmp_path):
    """Two questions, recorded from the real solver as one workload."""
    path = tmp_path / "answers.json"
    with pytest.MonkeyPatch.context() as patch:
        recording = exam_cpsat_replay.Recording()
        recording.install(patch)
        with recording.workload("w"):
            _solve(_model()[0])
            _solve(_model(bound=3)[0])
        recording.save(path)
    return path


@pytest.fixture
def replay(recorded, monkeypatch):
    replay = exam_cpsat_replay.Replay(recorded)
    replay.install(monkeypatch)
    return replay


def test_a_replayed_solve_returns_the_recorded_answer_not_a_new_search(recorded, monkeypatch):
    saved = json.loads(recorded.read_text(encoding="utf-8"))
    first = saved["workloads"]["w"][0]
    assert saved["answers"][first]["solution"] == [4, 0]
    # A different answer than the solver would give: the replay must return it.
    saved["answers"][first].update(solution=[1, 0], status="FEASIBLE", dtime=0.06, objective=1.0)
    recorded.write_text(json.dumps(saved), encoding="utf-8")
    replay = exam_cpsat_replay.Replay(recorded)
    replay.install(monkeypatch)
    model, x, b = _model()
    with replay.workload("w"):
        solver, status = _solve(model)
        _solve(_model(bound=3)[0])
    assert status == cp_model.FEASIBLE
    assert (solver.value(x), solver.value(b), solver.value(x + b)) == (1, 0, 1)
    assert solver.objective_value == 1.0
    assert solver.response_proto.deterministic_time == 0.06


def test_a_question_master_never_asked_fails_through_any_except(replay):
    with pytest.raises(Failed, match="never asked"):
        try:
            _solve(_model(bound=2)[0])
        except Exception:  # a fallback in the code under test must not absorb it
            pass


def test_every_input_but_the_wall_clock_is_part_of_the_question(replay):
    with replay.workload("w"):
        _solve(_model()[0], max_time_in_seconds=123.0)
        _solve(_model(bound=3)[0], max_time_in_seconds=0.5)
    for change in ({"random_seed": 1}, {"max_deterministic_time": 0.07}, {"num_search_workers": 2}):
        with pytest.raises(Failed, match="never asked"):
            _solve(_model()[0], **change)
    with pytest.raises(Failed, match="never asked"):
        _solve(_model(hint=0)[0])


@pytest.mark.parametrize(
    "order", [(3, 4), (4,), (4, 3, 4), ()], ids=["swapped", "fewer", "more", "none"]
)
def test_a_workload_asks_master_questions_in_master_order(replay, order):
    with pytest.raises(AssertionError, match="questions where master asked"):
        with replay.workload("w"):
            for bound in order:
                _solve(_model(bound=bound)[0])


def test_an_unknown_question_absorbed_on_the_way_still_fails_the_workload(replay):
    with pytest.raises(AssertionError, match="never asked: question 3 of workload 'w'"):
        with replay.workload("w"):
            _solve(_model()[0])
            _solve(_model(bound=3)[0])
            try:
                _solve(_model(bound=2)[0])
            except BaseException:
                pass


def test_callbacks_are_not_replayed(replay):
    with pytest.raises(Failed, match="not recorded"):
        _solve(_model()[0], cp_model.CpSolverSolutionCallback())


def test_master_solver_must_answer_one_question_one_way():
    recording = exam_cpsat_replay.Recording()
    answer = {"status": "OPTIMAL", "objective": 4.0, "dtime": 0.01, "solution": [4]}
    recording.record("q", exam_cpsat_replay._response(answer))
    recording.record("q", exam_cpsat_replay._response(answer))
    with pytest.raises(AssertionError, match="two answers"):
        recording.record("q", exam_cpsat_replay._response({**answer, "solution": [3]}))


def test_master_answers_cover_exactly_the_questions_master_asked():
    saved = json.loads(MASTER_CPSAT.read_text(encoding="utf-8"))
    assert saved["recorded_from"] == exam_cpsat_replay.RECORDED_FROM
    assert sorted(saved["workloads"]) == ["exports", "periods", "room_reports"]
    asked = {question for questions in saved["workloads"].values() for question in questions}
    assert asked == set(saved["answers"])

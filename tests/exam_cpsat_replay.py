"""Record CP-SAT's answers on one machine; replay them on any other.

The rooms parity tests (``test_exam_linked_rooms_parity.py``) hold rooming
without links to what origin/master (84704ad) returned, byte for byte. Rooming
calls CP-SAT (``exam_room_allocation._repair``), and OR-Tools' Windows and
Linux builds do not return the same solution for the same model: where optima
tie, or where a search stops at its deterministic work limit, each build keeps
what its own search found. The goldens were recorded on Windows. On CI (Linux)
25 of the 60 period boards came back different, every one a board that
reaches CP-SAT (12 stopped by the work limit, 13 proven optimal), while on
Windows the branch and master send CP-SAT byte-identical models and parameters
and get identical answers. The difference is the solver build's, not ours.

So those tests replay master's solver. The seam is the one place Python hands
a model to the native solver: ``cp_model_helper.SolveWrapper``, which
``CpSolver.solve`` creates for every solve. Everything above it runs for real -
building the model, its hints and parameters, reading values out of the
response through ``CpSolver`` itself, and every decision taken on them - so
the outputs are still compared with master's byte for byte. Only the native
search is replaced: a question is known by the digest of its input (the model
and the parameters that decide the answer, each serialised deterministically;
the wall-clock safety valve ``max_time_in_seconds`` is left out) and answered
with the response master's solver gave to that very question.

A question master never asked fails the test at once: the code now asks the
solver something master did not. A workload must also ask master's questions
in master's order - one fewer, one more or two swapped fails too.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import ortools
import pytest
from google.protobuf import text_format
from ortools.sat import cp_model_pb2, sat_parameters_pb2
from ortools.sat.python import cp_model_helper

#: The parameters that cannot change an answer: the wall-clock safety valve.
#: Every other one (workers, seed, the deterministic work limit) is part of
#: the question.
WALL_CLOCK_PARAMETERS = ("max_time_in_seconds",)
#: The only code whose questions may be recorded.
RECORDED_FROM = "origin/master 84704ad"
#: A response's deterministic time is kept to 1e-12: its run-to-run noise is
#: far below that, and the allocator reads it only against its work limit.
DTIME_DECIMALS = 12


def _canonical(text: str, message_type, clear: tuple[str, ...] = ()) -> bytes:
    """A proto's deterministic serialisation, read from its text form.

    OR-Tools' Python layer holds its protos in C++ and offers no binary form;
    parsing the text into the generated message and serialising that is
    platform-independent, and a double reads back as the same double whatever
    digits a platform printed for it.
    """
    message = message_type()
    text_format.Parse(text, message)
    for name in clear:
        message.ClearField(name)
    return message.SerializeToString(deterministic=True)


def question_digest(model_text: str, parameters_text: str) -> str:
    """The digest of one solve's input: the model and the parameters that count."""
    digest = hashlib.sha256()
    for part in (
        _canonical(model_text, cp_model_pb2.CpModelProto),
        _canonical(parameters_text, sat_parameters_pb2.SatParameters, WALL_CLOCK_PARAMETERS),
    ):
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def _answer(response) -> dict:
    """What the code under test reads from a response, through ``CpSolver``."""
    return {
        "status": response.status.name,
        "objective": response.objective_value,
        # Its last bits vary from run to run (a floating-point sum); the code
        # compares it with its work limit to within 1e-9.
        "dtime": round(response.deterministic_time, DTIME_DECIMALS),
        "solution": list(response.solution),
    }


def _response(answer: dict):
    """A native response carrying a recorded answer; every other field is empty."""
    response = cp_model_helper.CpSolverResponse()
    response.status = getattr(cp_model_helper.CpSolverStatus, answer["status"])
    response.objective_value = answer["objective"]
    response.deterministic_time = answer["dtime"]
    response.solution.extend(answer["solution"])
    return response


class _Questions:
    """The questions one workload asks, in order."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.current: str | None = None


# ── recording (on master only) ──────────────────────────────────────────────


class Recording(_Questions):
    """Real CP-SAT, with every question and its answer written down."""

    def __init__(self) -> None:
        super().__init__()
        self.answers: dict[str, dict] = {}
        self.workloads: dict[str, list[str]] = {}
        self._native = cp_model_helper.SolveWrapper

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(cp_model_helper, "SolveWrapper", self._wrapper)

    def _wrapper(self):
        return _RecordingWrapper(self, self._native())

    def record(self, question: str, response) -> None:
        answer = _answer(response)
        known = self.answers.setdefault(question, answer)
        # Master's solver must itself be deterministic, or there is nothing to replay.
        assert known == answer, f"one question, two answers from master's solver: {question}"
        self.asked.append(question)

    @contextlib.contextmanager
    def workload(self, name: str) -> Iterator[None]:
        self.asked, self.current = [], name
        yield
        assert name not in self.workloads, f"workload {name} recorded twice"
        self.workloads[name] = self.asked

    def save(self, path: Path) -> None:
        """One JSON document, a line per workload and per answer."""

        def line(value) -> str:
            return json.dumps(value, separators=(",", ":"))

        used = sorted({question for asked in self.workloads.values() for question in asked})
        workloads = [
            f"{line(name)}:{line(self.workloads[name])}" for name in sorted(self.workloads)
        ]
        answers = [f"{line(question)}:{line(self.answers[question])}" for question in used]
        path.write_text(
            "{\n"
            f'"recorded_from":{line(RECORDED_FROM)},\n'
            f'"ortools":{line(ortools.__version__)},\n'
            '"workloads":{\n' + ",\n".join(workloads) + "\n},\n"
            '"answers":{\n' + ",\n".join(answers) + "\n}\n}\n",
            encoding="utf-8",
        )


class _RecordingWrapper:
    def __init__(self, recording: Recording, native) -> None:
        self._recording = recording
        self._native = native
        self._parameters = ""

    def set_parameters(self, parameters) -> None:
        self._parameters = str(parameters)
        self._native.set_parameters(parameters)

    def solve(self, model_proto):
        question = question_digest(str(model_proto), self._parameters)
        response = self._native.solve(model_proto)
        self._recording.record(question, response)
        return response

    def __getattr__(self, name: str):
        return getattr(self._native, name)


# ── replay ──────────────────────────────────────────────────────────────────


class Replay(_Questions):
    """CP-SAT answering from a recording: known questions only, in order."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        recorded = json.loads(path.read_text(encoding="utf-8"))
        assert recorded["recorded_from"] == RECORDED_FROM, recorded["recorded_from"]
        self.version: str = recorded["ortools"]
        self.answers: dict[str, dict] = recorded["answers"]
        self.workloads: dict[str, list[str]] = recorded["workloads"]
        #: (position in the workload, digest) of each question master never asked.
        self.unknown: list[tuple[int, str]] = []

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(cp_model_helper, "SolveWrapper", lambda: _ReplayWrapper(self))

    def answer(self, question: str):
        self.asked.append(question)
        known = self.answers.get(question)
        if known is None:
            self.unknown.append((len(self.asked), question))
            # Failed is a BaseException: no ``except Exception`` on the way out
            # can turn this into a fallback answer.
            pytest.fail(self._unknown_message(*self.unknown[-1]), pytrace=False)
        return _response(known)

    def _unknown_message(self, number: int, question: str) -> str:
        return (
            f"CP-SAT was asked a question master never asked: question "
            f"{number} of workload {self.current!r}, input digest {question}. "
            "The model or the parameters differ from master's. If master itself "
            f"changed (or OR-Tools, recorded with {self.version}), re-record: see "
            "test_exam_linked_rooms_parity.py."
        )

    @contextlib.contextmanager
    def workload(self, name: str) -> Iterator[None]:
        """Replay one workload; it must ask exactly master's questions, in order."""
        expected = self.workloads[name]
        self.asked, self.current, self.unknown = [], name, []
        yield
        assert not self.unknown, self._unknown_message(*self.unknown[0])
        if self.asked != expected:
            first = next(
                (
                    number
                    for number, (asked, recorded) in enumerate(
                        zip(self.asked, expected, strict=False)
                    )
                    if asked != recorded
                ),
                min(len(self.asked), len(expected)),
            )
            raise AssertionError(
                f"workload {name!r} asked CP-SAT {len(self.asked)} questions where master "
                f"asked {len(expected)}; they part at question {first + 1}"
            )


class _ReplayWrapper:
    def __init__(self, replay: Replay) -> None:
        self._replay = replay
        self._parameters = ""

    def set_parameters(self, parameters) -> None:
        self._parameters = str(parameters)

    def solve(self, model_proto):
        return self._replay.answer(question_digest(str(model_proto), self._parameters))

    def _unsupported(self, *_args) -> None:
        # A recorded response replays no callbacks and cannot be interrupted.
        pytest.fail("CP-SAT replay: callbacks and stop_search are not recorded", pytrace=False)

    add_solution_callback = add_log_callback = add_best_bound_callback = _unsupported
    clear_solution_callback = stop_search = _unsupported

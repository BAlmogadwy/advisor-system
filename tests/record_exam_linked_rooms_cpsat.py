"""Re-record, FROM MASTER, the CP-SAT answers the rooms parity tests replay.

Never run this on a branch: it would record the branch's own answers, and a
parity test that replays them proves nothing. It runs the three solver
workloads of ``test_exam_linked_rooms_parity.py`` through the checkout's own
functions, as master has them, on the real solver, and fails unless every
output is master's golden - so the answers it saves are the ones behind those
goldens. The steps are in that module's docstring. Not collected by default
(no ``test_`` prefix); pytest runs it when named.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core import models
from core.services import (
    exam_input_fingerprint,
    exam_room_allocation,
    exam_run_schema,
    exam_timetable,
)
from tests import exam_cpsat_replay
from tests import exam_linked_parity_corpus as base
from tests import exam_linked_rooms_corpus as corpus
from tests.test_exam_linked_rooms_parity import (
    MASTER_EXPORTS,
    MASTER_ROOM_POLICY,
    _api,
    _assert_master,
)


@pytest.fixture(scope="module")
def recording():
    target = os.environ.get("EXAM_CPSAT_RECORD")
    if not target:
        pytest.fail("Set EXAM_CPSAT_RECORD to the file to write.", pytrace=False)
    # The questions must be master's: a checkout whose allocator already has
    # shared rooms would record its own.
    if exam_room_allocation.ROOM_ALLOCATION_POLICY_VERSION != MASTER_ROOM_POLICY:
        pytest.fail("This checkout is not master 84704ad; record from master.", pytrace=False)
    recording = exam_cpsat_replay.Recording()
    yield recording
    assert sorted(recording.workloads) == ["exports", "periods", "room_reports"]
    recording.save(Path(target))


@pytest.fixture(autouse=True)
def _record_every_solve(recording, monkeypatch):
    recording.install(monkeypatch)
    with exam_room_allocation._CACHE_LOCK:
        exam_room_allocation._CACHE.clear()


def test_record_periods(recording):
    with recording.workload("periods"):
        digests = [
            corpus.run_period(
                board,
                exam_room_allocation.allocate_period,
                exam_room_allocation.RoomAllocationContext,
            )
            for board in corpus.period_boards()
        ]
    _assert_master("periods", digests)


def test_record_room_reports(recording):
    with recording.workload("room_reports"):
        digests = []
        for board, rooms in corpus.room_boards():
            _conflicts, adj, _thin = base.graph(
                board,
                exam_timetable.build_conflict_graph,
                exam_timetable.apply_thin_conflict_policy,
            )
            digests.append(
                corpus.run_room_reports(
                    board,
                    rooms,
                    exam_timetable.assign_rooms_to_schedule,
                    exam_timetable._rebalance_invigilators_pass,
                    exam_room_allocation.RoomAllocationContext.for_periods,
                    exam_timetable._build_room_qa,
                    exam_run_schema.derive_building_footprint,
                    adj,
                )
            )
    _assert_master("room_reports", digests)


@pytest.mark.django_db
def test_record_exports(recording, monkeypatch):
    monkeypatch.setattr(
        exam_input_fingerprint, "ROOM_ALLOCATION_POLICY_VERSION", MASTER_ROOM_POLICY
    )
    corpus.create_rooms_population(models)
    api = _api()
    with recording.workload("exports"):
        result, run = corpus.build_population_run(api, models, None)
        digests = corpus.export_digests(run, api)
    digests["build"] = base.digest(base.comparable(result))
    assert digests == MASTER_EXPORTS

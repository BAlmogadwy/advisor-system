"""The preview lane: heavy workspace previews never take every request thread.

A full lane answers 503 SERVER_BUSY at once without running the preview;
writes and /health/ never take it; a token is returned even when the preview
raises; and the lane leaves two of gunicorn's threads free (one for /health/,
one for a write) in every start command that deploys the app.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core import timetable_workspace_views as views
from core.models import DeliveryBoard, SectionPlacement, TermSection, TimetableScenario
from core.services import request_lanes
from core.services.job_runtime import HOLDER_PLANNER, solver_slot
from core.services.rbac import ROLE_SUPER_ADMIN
from core.views import health

pytestmark = pytest.mark.django_db
ROOT = Path(__file__).resolve().parents[1]

#: Every heavy read-only preview the workspace pages call while a user clicks.
HEAVY_PREVIEW_VIEWS = [
    views.tw_placement_room_candidates_view,
    views.tw_placement_slot_candidates_view,
    views.tw_placement_student_outcome_candidates_view,
    views.tw_board_planned_slot_candidates_view,
    views.tw_repair_analyse_view,
    # A full seating evaluation (the drawer's seated mix by programme).
    views.tw_scenario_plan_lens_view,
]
#: Never gated: the health check and the manual writes.
UNGATED_VIEWS = [
    health,
    views.tw_placement_move_view,
    views.tw_placement_create_view,
    views.tw_placement_create_planned_view,
    views.tw_placement_pattern_switch_view,
    views.tw_placement_remove_view,
    views.tw_placement_lock_view,
    views.tw_placement_student_evidence_view,
    views.tw_boards_list_view,
    views.tw_board_conflicts_view,
]


@pytest.fixture
def http():
    user = User.objects.create_user(username="preview-lane-admin")
    user.groups.add(Group.objects.get_or_create(name=ROLE_SUPER_ADMIN)[0])
    client = Client(raise_request_exception=False)
    client.force_login(user)
    return client


@pytest.fixture
def placement():
    scenario = TimetableScenario.objects.create(name="Lane")
    board = DeliveryBoard.objects.create(scenario=scenario, label="T1", program="AI")
    section = TermSection.objects.create(
        scenario=scenario, course_key="AI111", course_code="AI111", section="S1"
    )
    return SectionPlacement.objects.create(
        board=board,
        term_section=section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="R1",
    )


@contextmanager
def lane_full():
    """Hold every lane token, as concurrent previews would."""
    held = 0
    try:
        for _ in range(request_lanes.PREVIEW_LANE_SIZE):
            assert request_lanes._PREVIEW_LANE.acquire(blocking=False)
            held += 1
        yield
    finally:
        for _ in range(held):
            request_lanes._PREVIEW_LANE.release()


def lane_free() -> bool:
    """Every token can be taken (and is given back)."""
    taken = 0
    try:
        while taken < request_lanes.PREVIEW_LANE_SIZE and request_lanes._PREVIEW_LANE.acquire(
            blocking=False
        ):
            taken += 1
        return taken == request_lanes.PREVIEW_LANE_SIZE
    finally:
        for _ in range(taken):
            request_lanes._PREVIEW_LANE.release()


def _previews(placement):
    base = f"/ops/tw/placements/{placement.id}"
    body = json.dumps({"candidates": [{"day": "MON", "start": "09:00", "end": "10:15"}]})
    return {
        "preview_placement_room_candidates": lambda c: c.get(
            f"{base}/room-candidates/?day=MON&start_time=09:00&end_time=10:15"
        ),
        "preview_placement_slot_candidates": lambda c: c.get(f"{base}/slot-candidates/"),
        "preview_placement_student_outcome_candidates": lambda c: c.post(
            f"{base}/student-outcome-candidates/", body, content_type="application/json"
        ),
        "preview_planned_section_slot_candidates": lambda c: c.get(
            f"/ops/tw/boards/{placement.board_id}/planned-slot-candidates/?course_code=AI111"
        ),
        "build_scenario_plan_lens": lambda c: c.get(
            f"/ops/tw/scenarios/{placement.board.scenario_id}/plan-lens/"
        ),
    }


def _engine(analyse):
    """A stand-in for the (frozen) move-optimisation engine."""
    return SimpleNamespace(analyse_section_move=analyse)


def _refuse(*_args, **_kwargs):
    raise AssertionError("a refused preview must not run")


@pytest.mark.parametrize(
    "service",
    [
        "preview_placement_room_candidates",
        "preview_placement_slot_candidates",
        "preview_placement_student_outcome_candidates",
        "preview_planned_section_slot_candidates",
        "build_scenario_plan_lens",
    ],
)
def test_a_full_lane_answers_busy_at_once_without_running_the_preview(
    service, http, placement, monkeypatch
) -> None:
    monkeypatch.setattr(views, service, _refuse)
    with lane_full():
        response = _previews(placement)[service](http)
    assert response.status_code == 503
    assert response["Retry-After"] == str(request_lanes.PREVIEW_RETRY_AFTER_SECONDS)
    body = response.json()
    assert body["ok"] is False
    assert body["code"] == body["error"]["code"] == "SERVER_BUSY"
    assert lane_free()


def test_repair_analysis_is_refused_while_the_lane_is_full(http, placement, monkeypatch) -> None:
    monkeypatch.setattr(views, "section_move_optimisation_engine", _engine(_refuse))
    with lane_full():
        response = http.post(
            "/ops/tw/repair/analyse/",
            json.dumps({"placement_id": placement.id}),
            content_type="application/json",
        )
    assert response.status_code == 503
    assert response.json()["code"] == "SERVER_BUSY"


def test_repair_analysis_waits_for_no_other_solve(http, placement, monkeypatch) -> None:
    """The analysis is a CP-SAT search: it shares the one solver slot, and a
    planner run holding it turns the analysis away at once (Retry-After 30)."""
    monkeypatch.setattr(views, "section_move_optimisation_engine", _engine(_refuse))
    with solver_slot(holder=HOLDER_PLANNER):
        response = http.post(
            "/ops/tw/repair/analyse/",
            json.dumps({"placement_id": placement.id}),
            content_type="application/json",
        )
    assert response.status_code == 503
    assert response["Retry-After"] == "30"
    body = response.json()
    assert body["code"] == "SERVER_BUSY"
    assert body["error"]["details"]["holder"]["kind"] == HOLDER_PLANNER
    assert lane_free()


def test_repair_analysis_holds_the_solver_while_it_runs(http, placement, monkeypatch) -> None:
    from core.services.job_runtime import solver_holder

    seen = []

    def analyse(**_kwargs):
        seen.append(solver_holder())
        return {"run": {"id": 1}, "candidates": []}

    monkeypatch.setattr(views, "section_move_optimisation_engine", _engine(analyse))
    response = http.post(
        "/ops/tw/repair/analyse/",
        json.dumps({"placement_id": placement.id}),
        content_type="application/json",
    )
    assert response.status_code == 201
    assert seen and seen[0]["kind"] == "repair_analysis"
    assert solver_holder() is None


def test_the_lane_is_returned_when_the_preview_raises(http, placement, monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("preview failed")

    monkeypatch.setattr(views, "preview_placement_room_candidates", boom)
    for _ in range(request_lanes.PREVIEW_LANE_SIZE + 1):
        response = _previews(placement)["preview_placement_room_candidates"](http)
        assert response.status_code == 500
    assert lane_free()


def test_a_move_and_the_health_check_run_while_the_lane_is_full(http, placement) -> None:
    with lane_full():
        moved = http.post(
            f"/ops/tw/placements/{placement.id}/move/",
            json.dumps({"day": "MON", "start_time": "09:00", "end_time": "10:15"}),
            content_type="application/json",
        )
        healthy = Client().get("/health/")
        evidence = http.get(f"/ops/tw/placements/{placement.id}/student-evidence/?limit=5")
    assert moved.status_code == 200, moved.content
    assert healthy.status_code == 200
    assert evidence.status_code == 200


def test_every_heavy_preview_carries_the_lane_and_no_write_does() -> None:
    for view in HEAVY_PREVIEW_VIEWS:
        assert getattr(view, request_lanes.LANE_MARKER, False), view.__name__
    for view in UNGATED_VIEWS:
        assert not getattr(view, request_lanes.LANE_MARKER, False), view.__name__


@pytest.mark.parametrize("source", ["render.yaml", "Procfile"])
def test_the_lane_leaves_a_thread_for_health_and_one_for_a_write(source) -> None:
    text = (ROOT / source).read_text(encoding="utf-8")
    commands = [line for line in text.splitlines() if "gunicorn" in line and "--threads" in line]
    assert commands, f"no gunicorn start command in {source}"
    for command in commands:
        threads = int(re.search(r"--threads\s+(\d+)", command).group(1))
        workers = int(re.search(r"--workers\s+(\d+)", command).group(1))
        # The lane is per process: it guards the threads only with one worker.
        assert workers == 1, command
        assert 1 <= request_lanes.PREVIEW_LANE_SIZE <= threads - 2, command

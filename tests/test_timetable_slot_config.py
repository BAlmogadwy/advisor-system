"""A scenario's grid columns: saving them never loses data or hides meetings silently.

- A save keeps each column's flags (``online_only``, ``policy_only``) when the
  request names the same start and end: the editors send plain rows, and an
  online window that loses its flag is a place an engine may book a room at
  18:30.
- Every column is validated, and a bad one is named by list, line and field.
- A change that would leave drawn meetings off the grid, on any board, is
  refused with their list until the request confirms exactly that set.
- The pages draw the server's effective columns (their own defaults
  disagreed with the scheduler's), and the per-plan export lists an off-grid
  lecture instead of failing.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import DeliveryBoard, SectionPlacement, TermSection, TimetableScenario
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups
from core.services.timetable_autoplace import DEFAULT_LAB_SLOTS, DEFAULT_SLOTS, placeable_slots
from core.services.timetable_slot_config import (
    carry_slot_flags,
    clean_slot_rows,
    hidden_fingerprint,
    is_on_grid,
)

LECTURES = [
    {"label": "09:00-10:15", "start": "09:00", "end": "10:15"},
    {"label": "10:50-12:05", "start": "10:50", "end": "12:05"},
]
LABS = [
    {"label": "Lab 1", "start": "09:00", "end": "10:40"},
    {"label": "Lab 5", "start": "16:30", "end": "18:10"},
    {"label": "Online 1", "start": "15:50", "end": "17:30", "online_only": True},
]


def _plain(rows):
    """What the textarea editors send: label, start and end only."""
    return [{"label": r["label"], "start": r["start"], "end": r["end"]} for r in rows]


# ── validation and flags (pure) ─────────────────────────────────────────────


def test_clean_rows_normalise_times_and_default_the_label() -> None:
    rows, errors = clean_slot_rows([{"start": "9:00", "end": "10:15"}], "slot_config")
    assert errors == []
    assert rows == [{"label": "09:00-10:15", "start": "09:00", "end": "10:15"}]


def test_each_bad_column_is_named_by_list_line_and_field() -> None:
    rows, errors = clean_slot_rows(
        [
            {"start": "09:00", "end": "10:15"},
            {"start": "25:00", "end": "10:15"},
            {"start": "11:00", "end": "10:00"},
            {"start": "09:00", "end": "10:15"},
            "09:00",
        ],
        "lab_slot_config",
    )
    assert [r["start"] for r in rows] == ["09:00"]
    assert [e.as_dict() for e in errors] == [
        {
            "list": "lab_slot_config",
            "index": 1,
            "field": "start",
            "message": "must be a time as HH:MM",
        },
        {
            "list": "lab_slot_config",
            "index": 2,
            "field": "end",
            "message": "must be after the start",
        },
        {
            "list": "lab_slot_config",
            "index": 3,
            "field": "start",
            "message": "repeats another column",
        },
        {"list": "lab_slot_config", "index": 4, "field": "row", "message": "must be an object"},
    ]


def test_a_plain_row_keeps_the_flag_of_the_column_it_replaces() -> None:
    rows, _ = clean_slot_rows(_plain(LABS), "lab_slot_config")
    carried = carry_slot_flags(rows, LABS)
    assert [bool(r.get("online_only")) for r in carried] == [False, False, True]
    # ...and stays out of every automatic placer's options.
    assert [r["start"] for r in placeable_slots(carried)] == ["09:00", "16:30"]


def test_a_flag_the_request_sets_wins_and_a_moved_column_starts_plain() -> None:
    rows, _ = clean_slot_rows(
        [
            {"label": "Online 1", "start": "15:50", "end": "17:30", "online_only": False},
            {"label": "Moved", "start": "17:40", "end": "19:20"},
            {"label": "New online", "start": "19:30", "end": "21:10", "online_only": True},
        ],
        "lab_slot_config",
    )
    carried = carry_slot_flags(rows, LABS)
    assert [r.get("online_only", False) for r in carried] == [False, False, True]
    assert "online_only" not in carried[0], "a false flag is not stored"


def test_the_grid_rule_is_a_teaching_day_and_a_column_start_of_the_meetings_kind() -> None:
    assert is_on_grid("SUN", "10:50", "12:05", LECTURES, LABS)
    assert not is_on_grid("SUN", "10:30", "11:45", LECTURES, LABS), (
        "no lecture column starts at 10:30"
    )
    assert is_on_grid("MON", "16:30", "18:10", LECTURES, LABS), "100 minutes: a lab column"
    assert not is_on_grid("MON", "09:00", "10:40", LECTURES, []), (
        "a lab is not drawn in a lecture column"
    )
    assert not is_on_grid("SAT", "09:00", "10:15", LECTURES, LABS), "not a teaching day"
    assert is_on_grid("SUN", "09:00:00", "10:15:00", LECTURES, LABS), (
        "stored times may carry seconds"
    )


def test_the_fingerprint_names_the_set_not_its_size() -> None:
    a = hidden_fingerprint([{"placement_id": 1}, {"placement_id": 2}])
    assert a == hidden_fingerprint([{"placement_id": 2}, {"placement_id": 1}])
    assert a != hidden_fingerprint([{"placement_id": 1}, {"placement_id": 3}])


# ── the endpoint ────────────────────────────────────────────────────────────


@pytest.fixture
def http():
    ensure_role_groups()
    user = User.objects.create_user(username="slots-admin")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client()
    client.force_login(user)
    return client


def _scenario(slot_config=None, lab_slot_config=None) -> TimetableScenario:
    return TimetableScenario.objects.create(
        academic_year="1448",
        term="2",
        name="Slots",
        slot_config=slot_config if slot_config is not None else [dict(r) for r in LECTURES],
        lab_slot_config=lab_slot_config if lab_slot_config is not None else [dict(r) for r in LABS],
    )


def _board(scenario, label="Term 2", order=1) -> DeliveryBoard:
    return DeliveryBoard.objects.create(
        scenario=scenario, label=label, nominal_term=2, program="AI", display_order=order
    )


def _place(board, code="CS112", section="S1", day="SUN", start="10:50", end="12:05", locked=False):
    ts = TermSection.objects.create(
        scenario=board.scenario,
        course_code=code,
        course_number=code,
        course_key=code,
        section=section,
    )
    return SectionPlacement.objects.create(
        board=board, term_section=ts, day=day, start_time=start, end_time=end, is_locked=locked
    )


def _save(http, scenario, slots, labs=None, **extra):
    body = {"slot_config": slots, **extra}
    if labs is not None:
        body["lab_slot_config"] = labs
    return http.post(
        f"/ops/tw/scenarios/{scenario.id}/slots/update/", body, content_type="application/json"
    )


@pytest.mark.django_db
def test_saving_the_editor_text_unchanged_keeps_the_online_windows_online_only(http) -> None:
    scenario = _scenario()
    response = _save(http, scenario, _plain(LECTURES), _plain(LABS))
    assert response.status_code == 200, response.content
    scenario.refresh_from_db()
    assert [bool(r.get("online_only")) for r in scenario.lab_slot_config] == [False, False, True]
    assert scenario.slot_config == LECTURES


@pytest.mark.django_db
def test_a_scenario_on_the_default_columns_keeps_their_flags_too(http) -> None:
    scenario = _scenario(slot_config=[], lab_slot_config=[])
    response = _save(http, scenario, _plain(DEFAULT_SLOTS), _plain(DEFAULT_LAB_SLOTS))
    assert response.status_code == 200, response.content
    scenario.refresh_from_db()
    online = [r["start"] for r in scenario.lab_slot_config if r.get("online_only")]
    assert online == [r["start"] for r in DEFAULT_LAB_SLOTS if r.get("online_only")] != []


@pytest.mark.django_db
def test_an_invalid_column_is_refused_and_named(http) -> None:
    scenario = _scenario()
    response = _save(http, scenario, [{"start": "09:00", "end": "08:00"}], _plain(LABS))
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_SLOTS"
    assert error["details"]["errors"] == [
        {"list": "slot_config", "index": 0, "field": "end", "message": "must be after the start"}
    ]
    scenario.refresh_from_db()
    assert scenario.slot_config == LECTURES, "nothing saved"


@pytest.mark.django_db
def test_a_change_that_hides_meetings_lists_them_and_saves_only_when_confirmed(http) -> None:
    scenario = _scenario()
    board = _board(scenario)
    other = _board(scenario, label="Term 4", order=2)
    hidden = _place(board, "CS112", "S1", "SUN", "10:50", "12:05", locked=True)
    hidden_elsewhere = _place(other, "AI201", "S1", "MON", "10:50", "12:05")
    _place(board, "CS113", "S1", "SUN", "09:00", "10:15")  # stays on the grid
    _place(board, "CS114", "S1", "SUN", "11:00", "12:15")  # already off the grid
    without_1050 = _plain(LECTURES[:1])

    response = _save(http, scenario, without_1050)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "SLOT_EDIT_HIDES_PLACEMENTS"
    assert error["details"]["hidden_count"] == 2
    assert [
        (h["placement_id"], h["board_label"], h["is_locked"]) for h in error["details"]["hidden"]
    ] == [
        (hidden.id, "Term 2", True),
        (hidden_elsewhere.id, "Term 4", False),
    ]
    scenario.refresh_from_db()
    assert scenario.slot_config == LECTURES, "nothing saved"

    stale = _save(http, scenario, without_1050, confirm_hidden="0" * 16)
    assert stale.status_code == 409, "a confirmation of another set does not count"

    confirmed = _save(http, scenario, without_1050, confirm_hidden=error["details"]["fingerprint"])
    assert confirmed.status_code == 200, confirmed.content
    assert confirmed.json()["hidden_count"] == 2
    scenario.refresh_from_db()
    assert [r["start"] for r in scenario.slot_config] == ["09:00"]


@pytest.mark.django_db
def test_lab_columns_hide_labs_and_lecture_columns_hide_lectures(http) -> None:
    scenario = _scenario()
    board = _board(scenario)
    lab = _place(board, "CS112", "S1", "TUE", "16:30", "18:10")
    # The lecture columns change, the lab columns are left as they are: no lab hides.
    assert _save(http, scenario, _plain(LECTURES)).status_code == 200
    response = _save(http, scenario, _plain(LECTURES), _plain([LABS[0], LABS[2]]))
    assert response.status_code == 409
    assert [h["placement_id"] for h in response.json()["error"]["details"]["hidden"]] == [lab.id]


@pytest.mark.django_db
def test_without_lab_columns_in_the_request_the_lab_columns_stay(http) -> None:
    scenario = _scenario()
    assert _save(http, scenario, _plain(LECTURES)).status_code == 200
    scenario.refresh_from_db()
    assert scenario.lab_slot_config == LABS


@pytest.mark.django_db
def test_the_pages_get_the_effective_columns(http) -> None:
    scenario = _scenario(slot_config=[], lab_slot_config=[])
    board = _board(scenario)
    detail = http.get(f"/ops/tw/boards/{board.id}/").json()
    assert detail["effective_slot_config"] == DEFAULT_SLOTS
    assert detail["effective_lab_slot_config"] == DEFAULT_LAB_SLOTS
    meta = http.get(f"/ops/tw/scenarios/{scenario.id}/").json()["scenario"]
    assert meta["effective_lab_slot_config"] == DEFAULT_LAB_SLOTS
    assert meta["lab_slot_config"] == [], "the stored list is still reported as it is"


# ── the per-plan export ─────────────────────────────────────────────────────


@pytest.mark.django_db
def test_an_off_grid_lecture_is_listed_under_the_grid_not_a_failed_export(monkeypatch) -> None:
    from tests.test_per_plan_export_grouping import _course, _export, _row, _seat
    from tests.test_per_plan_export_grouping import _scenario as export_scenario

    scenario, [board] = export_scenario()
    sections = _course(board, "CS112", ("S1", "SUN", "09:00"))
    off = TermSection.objects.create(
        scenario=scenario,
        course_code="CS112",
        course_number="CS112",
        course_key="CS112",
        course_name="CS112",
        section="S2",
        source_tag="tw_auto",
    )
    SectionPlacement.objects.create(
        board=board, term_section=off, day="MON", start_time="18:30", end_time="19:45"
    )
    _seat(monkeypatch, {sections["S1"]: _row(AI2=20), off: _row(AI2=20)})

    sheet = _export(scenario)["AI2"]["Term 2"]
    notes = [
        cell.value
        for row in sheet.iter_rows(min_col=1, max_col=1)
        for cell in row
        if isinstance(cell.value, str)
        and cell.value.startswith("Outside this timetable's time slots")
    ]
    assert notes and "CS112 S2" in notes[0] and "Monday 18:30-19:45" in notes[0]

"""The other times for one exam, through the real request: ``exam_timetable_move_options``.

The ranking claims what a board would count with the exam moved. These tests
move the exam there and run Check (``exam_timetable_draft_impact``), which
counts the board as the cards show it, and hold the claim to that count. They
also hold the request to what it promises: it moves nothing and saves nothing,
it answers while a background job holds the solver, and only the exam
timetable's own roles may ask.
"""

import json

import pytest
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.urls import reverse

from core.middleware import ExamCommitteeAccessMiddleware
from core.models import ExamTimetableJob, ExamTimetableRun, Room
from core.services import exam_jobs
from core.services.job_runtime import solver_slot
from core.services.rbac import ROLE_ADVISOR, ROLE_EXAM_COMMITTEE, ensure_role_groups
from tests.test_exam_optimise_http import (
    CROWD,
    CROWD_GROUPS,
    CROWDED,
    DAYS,
    LINKED,
    LINKED_BOARD,
    LINKED_GROUPS,
    PERIODS,
    _board,
    _build,
    _check,
    _link,
    _locked_run,
    _numbers,
    _payload,
    _populate,
    _post,
    _rooms,
    _where,
)

pytestmark = pytest.mark.django_db

URL = "exam_timetable_move_options"


@pytest.fixture
def client_(client, django_user_model, monkeypatch):
    monkeypatch.setattr("core.authz._rate_buckets", {})
    cache.clear()
    client.force_login(django_user_model.objects.create_superuser(username="move-options-admin"))
    return client


@pytest.fixture
def crowd(client_):
    """Five exams crowded onto two days: the worst board, so better times exist."""
    _populate(CROWD, CROWD_GROUPS)
    built = _build(client_)
    return built, _board(built, CROWDED)


def _options(client, built, board, code, *, status=200, **changes):
    return _post(
        client, {**_payload(built, board, **changes), "course_code": code}, status, url=URL
    )


def _cell(option):
    return (option["day"], option["period"])


def test_every_offered_time_counts_what_check_counts_with_the_exam_moved_there(client_, crowd):
    built, board = crowd
    here = _numbers(_check(client_, built, board)["qa"])
    answer = _options(client_, built, board, "CR3")

    assert answer["course_code"] == "CR3"
    assert answer["members"] == ["CR3"]
    # Built without rooms: no seats to count, no rooms to estimate staff from.
    assert answer["seats_counted"] is False
    assert answer["invigilators_estimated"] is False
    assert (answer["current"]["day"], answer["current"]["period"]) == CROWDED["CR3"]
    assert {key: answer["current"]["levels"][key] for key in here} == here
    offered = [_cell(option) for option in answer["options"]]
    assert sorted(offered) == sorted(
        (day, period) for day in DAYS for period in PERIODS if (day, period) != CROWDED["CR3"]
    ), "every other time, once"
    for option in answer["options"]:
        moved = _check(client_, built, _board(built, {**CROWDED, "CR3": _cell(option)}))
        assert {key: option["levels"][key] for key in here} == _numbers(moved["qa"]), option
        assert option["changes"] == {
            level: option["levels"][level] - answer["current"]["levels"][level]
            for level in option["levels"]
        }
        # A rule break here is a student's two exams at once: Check sees it too.
        assert (option["levels"]["rule_breaks"] > 0) == (moved["qa"]["conflict_count"] > 0)
        assert bool(option["clashes_with"]) == (option["levels"]["rule_breaks"] > 0)
    # CR3 off the crowded Sunday spares its four students a third exam that day.
    best = answer["options"][0]
    assert best["verdict"] == "better"
    assert best["day"] != "Sun"
    assert best["changes"]["over_limit"] == -4
    # Into CR1's period: four students would sit two exams at once.
    clash = next(o for o in answer["options"] if _cell(o) == CROWDED["CR1"])
    assert clash["verdict"] == "worse"
    assert clash["clashes_with"] == [{"courses": ["CR1"], "students": 4}]


def test_asking_moves_nothing_and_saves_nothing(client_, crowd):
    built, board = crowd
    run = ExamTimetableRun.objects.get(pk=built["run_id"])
    saved, runs = run.result_json, ExamTimetableRun.objects.count()

    _options(client_, built, board, "CR3")

    assert ExamTimetableRun.objects.count() == runs
    assert ExamTimetableRun.objects.get(pk=built["run_id"]).result_json == saved
    assert not ExamTimetableJob.objects.exists()


def test_a_pinned_exam_is_refused_with_its_reason(client_, crowd):
    built, board = crowd
    pin = {"course_code": "CR3", "day": CROWDED["CR3"][0], "period": CROWDED["CR3"][1]}
    refused = _options(client_, built, board, "CR3", status=409, pinned=[pin])
    assert refused["error_code"] == "exam_not_movable"
    assert "pinned" in refused["error"]
    # Its neighbours may still move; the pin stays where it is for them.
    assert _options(client_, built, board, "CR2", pinned=[pin])["options"]


def test_an_exam_in_overflow_is_offered_every_time_and_seating_it_is_better(client_, crowd):
    built, _ = crowd
    board = _board(built, {**CROWDED, "CR5": None})
    # An OVERFLOW row is numbered after every real slot.
    for entry in board:
        if entry["course_code"] == "CR5":
            entry.update(slot_index=len(DAYS) * len(PERIODS), period="Extra-1")
    answer = _options(client_, built, board, "CR5")
    assert answer["current"]["day"] == "OVERFLOW"
    assert answer["current"]["levels"]["unseated"] == 1
    assert len(answer["options"]) == len(DAYS) * len(PERIODS)
    assert {option["changes"]["unseated"] for option in answer["options"]} == {-1}
    # CR4 and CR5 share three students: seating CR5 beside CR4 is a clash.
    assert {o["verdict"] for o in answer["options"] if _cell(o) != CROWDED["CR4"]} == {"better"}


def test_a_linked_exam_is_ranked_as_one_with_every_course_that_moves_with_it(client_):
    _populate(LINKED, LINKED_GROUPS)
    link = _link("LP1", "LQ1")
    built = _build(client_, linked_exams=[link])
    board = _board(built, LINKED_BOARD)
    answer = _options(client_, built, board, "LQ1", linked_exams=[link])
    assert answer["members"] == ["LP1", "LQ1"]
    sunday_late = next(o for o in answer["options"] if _cell(o) == LINKED_BOARD["LR1"])
    # LR1 shares three students with LP1: a clash for the whole link.
    assert sunday_late["clashes_with"] == [{"courses": ["LR1"], "students": 3}]
    # And from LR1's side, the link it would clash with is named by its courses.
    beside = next(
        o
        for o in _options(client_, built, board, "LR1", linked_exams=[link])["options"]
        if _cell(o) == LINKED_BOARD["LP1"]
    )
    assert beside["clashes_with"] == [{"courses": ["LP1", "LQ1"], "students": 3}]
    # Moving the link where the ranking says, as one exam, is what Check counts.
    best = answer["options"][0]
    moved = _board(built, {**LINKED_BOARD, "LP1": _cell(best), "LQ1": _cell(best)})
    checked = _check(client_, built, moved, linked_exams=[link])
    assert {key: best["levels"][key] for key in _numbers(checked["qa"])} == _numbers(checked["qa"])


def test_locked_cells_are_never_offered_and_an_exam_in_one_is_refused(client_):
    _populate(CROWD, CROWD_GROUPS)
    _rooms()
    built = _build(client_, assign_rooms=True)
    locks = [{"day": "Tue"}, {"day": "Sun", "period": PERIODS[0]}]
    saved = _locked_run(client_, built, _board(built, CROWDED), locks)
    cells = {("Tue", period) for period in PERIODS} | {("Sun", PERIODS[0])}

    answer = _options(client_, saved, saved["schedule"], "CR3", assign_rooms=True)

    assert answer["options"]
    assert answer["seats_counted"] is True
    assert answer["invigilators_estimated"] is True, "the saved rooms say what each exam needs"
    assert not cells & {_cell(option) for option in answer["options"]}
    assert len(answer["options"]) == len(DAYS) * len(PERIODS) - len(cells) - 1
    refused = _options(client_, saved, saved["schedule"], "CR1", status=409, assign_rooms=True)
    assert refused["error_code"] == "exam_not_movable"


def test_seats_are_counted_by_cohort_when_rooms_are_assigned(client_):
    """Women have one room of three seats; each of CR1-CR3 seats four women."""
    _populate(CROWD, CROWD_GROUPS)
    Room.objects.create(room_code="RF", capacity=3, exam_capacity=3, section="F")
    Room.objects.create(room_code="RM", capacity=30, exam_capacity=30, section="M")
    built = _build(client_, assign_rooms=True)
    board = _board(built, CROWDED)

    answer = _options(client_, built, board, "CR4", assign_rooms=True)
    checked = _check(client_, built, board, assign_rooms=True)

    assert answer["seats_counted"] is True
    # A woman short in each of CR1-CR3's periods: three in all, a floor on what
    # the rooming leaves unseated.
    assert answer["current"]["levels"]["seats_short"] == 3
    unseated = sum(
        row["student_count"] for row in checked["qa"]["rooms"]["unassigned_room_sections"]
    )
    assert unseated >= 3
    # CR4 seats only men: no period is shorter for it anywhere.
    assert {option["changes"]["seats_short"] for option in answer["options"]} == {0}
    # Without rooms the seats are not counted at all.
    unroomed = _options(client_, built, board, "CR4", assign_rooms=False)
    assert unroomed["seats_counted"] is False
    assert unroomed["current"]["levels"]["seats_short"] == 0


def test_a_linked_exam_needs_the_seats_of_every_course_in_it(client_):
    """LP1 seats four women, LQ1 one woman and two men: the link, five women."""
    _populate(LINKED, LINKED_GROUPS)
    Room.objects.create(room_code="RF", capacity=4, exam_capacity=4, section="F")
    Room.objects.create(room_code="RM", capacity=30, exam_capacity=30, section="M")
    link = _link("LP1", "LQ1")
    built = _build(client_, linked_exams=[link], assign_rooms=True)
    board = _board(built, LINKED_BOARD)

    answer = _options(client_, built, board, "LP1", linked_exams=[link], assign_rooms=True)

    assert answer["current"]["levels"]["seats_short"] == 1, "five women, four seats"
    # Beside LR1's three women: eight women, four seats - and its old period is empty.
    later = next(o for o in answer["options"] if _cell(o) == LINKED_BOARD["LR1"])
    assert later["levels"]["seats_short"] == 4
    assert later["changes"]["seats_short"] == 3


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"course_code": ""}, "Choose the exam"),
        ({"course_code": "NOPE1"}, "not on this timetable"),
        (
            {"course_code": "CR3", "course_identity": "CR3::Some other course"},
            "not on this timetable",
        ),
    ],
)
def test_a_request_for_no_exam_of_this_board_is_refused(client_, crowd, body, error):
    built, board = crowd
    refused = _post(client_, {**_payload(built, board), **body}, 400, url=URL)
    assert error in refused["error"]


def test_a_malformed_request_is_refused(client_):
    assert client_.post(reverse(URL), "{", content_type="application/json").status_code == 400
    assert client_.post(reverse(URL), "[]", content_type="application/json").status_code == 400
    assert client_.get(reverse(URL)).status_code == 405
    stale = _post(client_, {"course_code": "CR3", "base_schedule": []}, 400, url=URL)
    assert "Reload the saved timetable" in stale["error"]


def test_it_answers_while_a_background_job_holds_the_solver(client_, crowd, settings):
    """Counting needs no solver: the ranking does not wait for, or slow, a job's."""
    built, board = crowd
    settings.EXAM_JOBS_ENABLED = True
    assert exam_jobs.jobs_enabled()
    with solver_slot(holder="exam_job"):
        assert _options(client_, built, board, "CR3")["options"]
        busy = client_.post(
            reverse("exam_timetable_draft_impact"),
            json.dumps(_payload(built, board)),
            content_type="application/json",
        )
        assert busy.status_code == 503, "the control: a Check waits for the solver"


def test_the_exam_committee_may_ask_and_other_roles_may_not(client, django_user_model, crowd):
    built, board = crowd
    assert URL in ExamCommitteeAccessMiddleware.allowed_views
    ensure_role_groups()
    body = json.dumps({**_payload(built, board), "course_code": "CR3"})
    for role, status in ((ROLE_EXAM_COMMITTEE, 200), (ROLE_ADVISOR, 403)):
        user = django_user_model.objects.create_user(username=f"move-{role.lower()}")
        user.groups.add(Group.objects.get(name=role))
        client.force_login(user)
        answer = client.post(reverse(URL), body, content_type="application/json")
        assert answer.status_code == status, (role, answer.content)
    client.logout()
    assert client.post(reverse(URL), body, content_type="application/json").status_code == 302


def test_the_answer_names_the_board_it_was_asked_about(client_, crowd):
    built, board = crowd
    answer = _post(
        client_,
        {**_payload(built, board), "course_code": "CR3", "editor_revision": 7},
        url=URL,
    )
    assert answer["editor_revision"] == 7
    assert answer["course_identity"].startswith("CR3")
    assert _where({"schedule": board}) == CROWDED

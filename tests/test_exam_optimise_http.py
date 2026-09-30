"""Optimize current timetable on the solver path, end to end through the HTTP view.

``mode: "optimize_loaded"`` used to re-place every exam with the greedy and then
flatten the invigilators. It now improves the submitted board with a search
over small CP-SAT solves (``core/services/exam_optimise.py``). These drive the
real request on boards the optimiser really improves - on a board of one or
two courses nothing can improve, so the action answers ``saved: False`` and
every "the saved run keeps ..." assertion would pass by saving nothing.

The greedy path, still there as the rollback (``EXAM_OPTIMISE_EXACT = False``),
is pinned by the older Optimize tests through the ``greedy_optimise`` fixture.
"""

import json
from copy import deepcopy

import pytest
from django.core.cache import cache
from django.urls import reverse

from core import exam_views
from core.models import (
    Course,
    ExamTimetableJob,
    ExamTimetableRun,
    ProgrammeRequirement,
    Room,
    Student,
    StudentCourse,
)
from core.services import exam_jobs
from core.services.exam_run_schema import load_normalised_run
from core.services.exam_timetable import build_enrolled_sets_with_meta
from tests.exam_source_factory import scraped_exam_registration
from tests.test_exam_jobs import _poll, _result, _states

pytestmark = pytest.mark.django_db

SCOPE = {"programs": ["AI"], "sections": ["F", "M"]}
DAYS = ["Sun", "Mon", "Tue"]
PERIODS = ["08:00-10:00", "11:00-13:00", "14:00-16:00"]
NO_ROOMS = {"assign_rooms": False}


@pytest.fixture
def client_(client, django_user_model, monkeypatch):
    monkeypatch.setattr("core.authz._rate_buckets", {})
    cache.clear()
    client.force_login(django_user_model.objects.create_superuser(username="optimise-admin"))
    return client


def _populate(courses, groups):
    """``courses``: (code, credits); each is its own plan term, so no two share a day rule.

    ``groups``: (how many students, their section, the courses each sits).
    """
    for term, (code, credits) in enumerate(courses, start=1):
        Course.objects.create(course_code=code, description=code, credit_hours=credits)
        ProgrammeRequirement.objects.create(
            program="AI", course_code=code, course_name=f"{code} name", programme_term=term
        )
    student_id = 7000
    for count, section, sat in groups:
        for _ in range(count):
            student_id += 1
            student = Student.objects.create(student_id=student_id, program="AI", section=section)
            for code in sat:
                StudentCourse.objects.create(
                    student=student, course=Course.objects.get(course_code=code), status="studying"
                )
                scraped_exam_registration(student, Course.objects.get(course_code=code))


def _post(client, payload, status=200, url="exam_timetable_build", **headers):
    response = client.post(reverse(url), payload, content_type="application/json", **headers)
    assert response.status_code == status, response.content
    return response.json()


def _build(client, *, days=DAYS, periods=PERIODS, **changes):
    return _post(
        client,
        {
            "label": "Optimise",
            "days": days,
            "periods": periods,
            "max_per_day": 2,
            **SCOPE,
            **NO_ROOMS,
            "pinned": [],
            **changes,
        },
    )


def _board(built, where):
    """``built``'s board with the exams in ``where`` set to (day, period), or None for OVERFLOW."""
    board = deepcopy(built["schedule"])
    overflow = max(entry["slot_index"] for entry in board) + 1
    for entry in board:
        if entry["course_code"] in where:
            target = where[entry["course_code"]]
            if target is None:
                entry.update(day="OVERFLOW", period=f"Extra-{overflow}", slot_index=overflow)
                overflow += 1
            else:
                entry.update(day=target[0], period=target[1])
    return board


def _payload(built, board, *, days=DAYS, periods=PERIODS, **changes):
    return {
        "label": "Optimise",
        "days": days,
        "periods": periods,
        "max_per_day": 2,
        **SCOPE,
        "previous_run_id": built["run_id"],
        "base_schedule": board,
        "pinned": built.get("pinned", []),
        **changes,
    }


def _optimise(client, built, board, *, status=200, **changes):
    return _post(
        client, {**_payload(built, board, **changes), "mode": "optimize_loaded"}, status=status
    )


def _check(client, built, board, **changes):
    return _post(client, _payload(built, board, **changes), url="exam_timetable_draft_impact")


def _save(client, built, board, **changes):
    """Check, then Save what was checked: how a hand-made board becomes a saved run."""
    checked = _check(client, built, board, **changes)
    return _post(
        client,
        {
            **_payload(built, board, **changes),
            "mode": "save_loaded_changes",
            "expected_input_fingerprint": checked["input_fingerprint"],
        },
    )


def _where(result):
    return {entry["course_code"]: (entry["day"], entry["period"]) for entry in result["schedule"]}


def _stable(value):
    """qa as the page compares it (reportSignature): timestamps and how an
    earlier pass reached its board are not part of the comparison."""
    if isinstance(value, list):
        return [_stable(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _stable(item)
            for key, item in sorted(value.items())
            if key not in {"snapshot_timestamp", "created_at", "generated_at", "rebalance_moves"}
        }
    return value


def _numbers(qa):
    return {
        "over_limit": qa["students_over_limit_per_day"],
        "heavy_day": qa["heavy_day_students"],
        "multi_exam_day": qa["multi_exam_day_students"],
    }


def _scored(report):
    return {key: report[key] for key in ("over_limit", "heavy_day", "multi_exam_day")}


# Five exams: one student group sits three of them (a heavy pair among them),
# another sits two. Three days of three periods leave room for every exam.
CROWD = [("CR1", 4), ("CR2", 3), ("CR3", 3), ("CR4", 3), ("CR5", 3)]
CROWD_GROUPS = [(4, "F", ["CR1", "CR2", "CR3"]), (3, "M", ["CR4", "CR5"])]
#: Every group's exams crowd one day: the worst board.
CROWDED = {
    "CR1": ("Sun", PERIODS[0]),
    "CR2": ("Sun", PERIODS[1]),
    "CR3": ("Sun", PERIODS[2]),
    "CR4": ("Mon", PERIODS[0]),
    "CR5": ("Mon", PERIODS[1]),
}


@pytest.fixture
def crowd(client_):
    _populate(CROWD, CROWD_GROUPS)
    built = _build(client_)
    return built, _board(built, CROWDED)


# ── an improvable board ──────────────────────────────────────────────────────


def test_an_improvable_board_is_saved_better_and_the_engine_counts_as_the_qa_does(client_, crowd):
    built, board = crowd
    submitted = _check(client_, built, board)
    assert _numbers(submitted["qa"]) == {"over_limit": 4, "heavy_day": 4, "multi_exam_day": 7}

    result = _optimise(client_, built, board)

    report = result["optimisation"]
    assert result["run_id"] and result.get("saved", True) is not False
    assert report["improved"] is True
    assert report["proven"] is True, "five exams are solved whole"
    # The engine's exact score and the QA report count the same students.
    assert (
        _scored(report["after"])
        == _numbers(result["qa"])
        == {
            "over_limit": 0,
            "heavy_day": 0,
            "multi_exam_day": 0,
        }
    )
    # And what it started from is what a Check of the submitted board reports.
    assert _scored(report["before"]) == _numbers(submitted["qa"])
    assert report["before"]["unseated"] == report["after"]["unseated"] == 0
    assert report["moved"] >= 1 and report["movable"] == 5 and report["fixed"] == 0
    assert result["rebuild_mode"] == "optimized_from_loaded"


def test_the_saved_run_carries_the_report_when_reloaded_from_history(client_, crowd):
    built, board = crowd
    result = _optimise(client_, built, board)

    reloaded = client_.get(reverse("exam_timetable_detail", args=[result["run_id"]])).json()
    assert reloaded["optimisation"] == result["optimisation"]
    stored = json.loads(ExamTimetableRun.objects.get(pk=result["run_id"]).result_json)
    assert stored["optimisation"] == result["optimisation"]
    assert load_normalised_run(ExamTimetableRun.objects.get(pk=result["run_id"]))["optimisation"]


def test_the_report_is_beside_qa_never_in_it_and_a_check_reproduces_qa(client_, crowd):
    """The page compares a saved run's qa with a Check of the same board; a
    difference marks the run changed and blocks the XLSX export."""
    built, board = crowd
    result = _optimise(client_, built, board)
    assert "optimisation" not in result["qa"]

    checked = _check(client_, result, result["schedule"])

    assert _stable(result["qa"]) == _stable(checked["qa"])
    assert "optimisation" not in checked and "optimisation" not in checked["qa"]


# ── a board nothing can improve ──────────────────────────────────────────────


def test_a_board_that_cannot_be_improved_is_not_saved_again(client_, crowd):
    built, board = crowd
    better = _optimise(client_, built, board)
    runs = ExamTimetableRun.objects.count()

    again = _optimise(client_, better, better["schedule"])

    assert again["saved"] is False and again["ok"] is True
    assert "run_id" not in again and "schedule" not in again
    report = again["optimisation"]
    assert report["improved"] is False
    assert report["moved"] == 0
    assert report["before"] == report["after"]
    assert ExamTimetableRun.objects.count() == runs


# ── pins ─────────────────────────────────────────────────────────────────────

# SA and SB share three students, so their day is the one thing to improve, and
# SB cannot help: SE and SF (pinned on Monday) each share a student with it, so
# it has no seat but its own day. Only SA can move - to Monday, beside them.
STUCK = [("ST1", 3), ("ST2", 3), ("ST3", 3), ("ST4", 3)]
STUCK_GROUPS = [(3, "F", ["ST1", "ST2"]), (1, "M", ["ST2", "ST3"]), (1, "M", ["ST2", "ST4"])]
STUCK_BOARD = {
    "ST1": ("Sun", PERIODS[0]),
    "ST2": ("Sun", PERIODS[1]),
    "ST3": ("Mon", PERIODS[0]),
    "ST4": ("Mon", PERIODS[1]),
}
TWO = PERIODS[:2]


def _pin(code):
    day, period = STUCK_BOARD[code]
    return {"course_code": code, "day": day, "period": period}


@pytest.fixture
def stuck(client_):
    _populate(STUCK, STUCK_GROUPS)
    built = _build(client_, days=DAYS[:2], periods=TWO)
    return built, _board(built, STUCK_BOARD)


def _stuck_optimise(client, built, board, **changes):
    return _optimise(client, built, board, days=DAYS[:2], periods=TWO, **changes)


def test_the_board_is_improvable_only_by_moving_the_exam_a_pin_will_hold(client_, stuck):
    """The control: with SA free it is the one exam that moves, and SB stays."""
    built, board = stuck
    result = _stuck_optimise(client_, built, board, pinned=[_pin("ST3"), _pin("ST4")])
    assert result["optimisation"]["improved"] is True
    where = _where(result)
    assert where["ST1"][0] == "Mon"
    assert {code: where[code] for code in ("ST2", "ST3", "ST4")} == {
        code: STUCK_BOARD[code] for code in ("ST2", "ST3", "ST4")
    }


def test_a_pinned_exam_never_moves_even_when_moving_it_is_the_only_improvement(client_, stuck):
    built, board = stuck
    pins = [_pin("ST1"), _pin("ST3"), _pin("ST4")]
    saved = _save(client_, built, board, days=DAYS[:2], periods=TWO, pinned=pins)
    runs = ExamTimetableRun.objects.count()

    result = _stuck_optimise(client_, saved, saved["schedule"], pinned=pins)

    assert result["saved"] is False, "the only way to improve the board was to move SA"
    assert result["optimisation"]["improved"] is False
    assert result["optimisation"]["fixed"] == 3 and result["optimisation"]["movable"] == 1
    assert ExamTimetableRun.objects.count() == runs


# ── what else the request asks for is never dropped ──────────────────────────


def test_a_new_pin_is_saved_although_nothing_moves_and_nothing_is_better(client_, stuck):
    """The pin lives only in the request: answering "nothing saved" would lose it."""
    built, board = stuck
    saved = _save(client_, built, board, days=DAYS[:2], periods=TWO, pinned=[_pin("ST3")])
    pins = [_pin("ST1"), _pin("ST3"), _pin("ST4")]

    result = _stuck_optimise(client_, saved, saved["schedule"], pinned=pins)

    assert result["run_id"] != saved["run_id"]
    report = result["optimisation"]
    assert report["improved"] is False and report["moved"] == 0
    assert _where(result) == _where(saved)
    reloaded = load_normalised_run(ExamTimetableRun.objects.get(pk=result["run_id"]))
    assert sorted(pin["course_code"] for pin in reloaded["pinned"]) == ["ST1", "ST3", "ST4"]


def test_a_removed_pin_is_saved_too(client_, stuck):
    built, board = stuck
    pins = [_pin("ST1"), _pin("ST2"), _pin("ST3"), _pin("ST4")]
    saved = _save(client_, built, board, days=DAYS[:2], periods=TWO, pinned=pins)

    # ST2 freed: it still has nowhere better to go, and the pin list changed.
    result = _stuck_optimise(client_, saved, saved["schedule"], pinned=pins[:1] + pins[2:])

    assert result["run_id"] != saved["run_id"]
    assert sorted(pin["course_code"] for pin in result["pinned"]) == ["ST1", "ST3", "ST4"]


@pytest.mark.parametrize(
    "change",
    [
        {"max_per_day": 1},
        {"thin_conflict_threshold": 3},
        {"days": DAYS},
        {"periods": PERIODS},
        {"assign_rooms": True},
    ],
    ids=["daily-limit", "thin-threshold", "a-day-added", "a-period-added", "rooms-switched-on"],
)
def test_a_changed_setting_is_saved_although_nothing_is_better(client_, stuck, change):
    built, board = stuck
    pins = [_pin("ST1"), _pin("ST2"), _pin("ST3"), _pin("ST4")]
    header = {"days": DAYS[:2], "periods": TWO}
    saved = _save(client_, built, board, **header, pinned=pins)
    runs = ExamTimetableRun.objects.count()

    same = _optimise(client_, saved, saved["schedule"], **header, pinned=pins)
    assert same["saved"] is False and ExamTimetableRun.objects.count() == runs

    result = _optimise(client_, saved, saved["schedule"], **{**header, **change}, pinned=pins)

    assert result["run_id"] != saved["run_id"], "every exam is pinned: only the setting changed"
    assert ExamTimetableRun.objects.count() == runs + 1
    if "max_per_day" in change:
        assert result["qa"]["max_per_day"] == 1
    if "thin_conflict_threshold" in change:
        assert result["qa"]["thin_threshold"] == 3
    if "days" in change:
        assert len(result["slots"]) == len(DAYS) * len(TWO)
    if "periods" in change:
        assert len(result["slots"]) == 2 * len(PERIODS)
    if "assign_rooms" in change:
        assert result["assign_rooms"] is True


@pytest.mark.parametrize(
    ("value", "rounds"),
    [
        (None, 500),
        ("", 500),
        ("40", 40),
        ("40.9", 40),
        ("abc", 500),
        ("nan", 500),
        ("inf", 500),
        ("1e400", 500),
        ("0", 1),
        ("-5", 1),
        ("999999", 5000),
    ],
)
def test_a_mistyped_rounds_setting_never_stops_the_site_and_never_means_no_search(
    monkeypatch, value, rounds
):
    from config.settings import _rounds_env

    if value is None:
        monkeypatch.delenv("EXAM_OPTIMISE_ROUNDS", raising=False)
    else:
        monkeypatch.setenv("EXAM_OPTIMISE_ROUNDS", value)
    assert _rounds_env("EXAM_OPTIMISE_ROUNDS", 500) == rounds


def test_a_pin_that_moves_an_exam_is_saved_although_the_search_finds_nothing_better(client_, crowd):
    """Not improved is not the same as unchanged: the registrar's pin relocates CR4."""
    built, board = crowd
    better = _optimise(client_, built, board)
    where = _where(better)
    day = next(day for day in DAYS if day not in {where["CR4"][0], where["CR5"][0]})
    pin = {"course_code": "CR4", "day": day, "period": PERIODS[0]}
    assert where["CR4"] != (day, PERIODS[0])

    result = _optimise(client_, better, better["schedule"], pinned=[pin])

    assert result["optimisation"]["improved"] is False
    assert result["optimisation"]["moved"] == 1, "the pinned exam is the one move"
    assert result["run_id"], "the board differs from the submitted one, so it is saved"
    assert _where(result)["CR4"] == (day, PERIODS[0])
    assert {code: cell for code, cell in _where(result).items() if code != "CR4"} == {
        code: cell for code, cell in where.items() if code != "CR4"
    }


# ── locks ────────────────────────────────────────────────────────────────────


def _rooms():
    for gender in ("F", "M"):
        for index in range(6):
            Room.objects.create(room_code=f"R{gender}{index}", capacity=30, section=gender)


def _locked_run(client, built, board, locks, **changes):
    """``board`` saved with its rooms, then saved again with ``locks``: the only way to lock."""
    roomed = _save(client, built, board, **changes)
    return _save(client, roomed, roomed["schedule"], exam_locks=locks, **changes)


def _cells(locks, periods):
    return {
        (lock["day"], period)
        for lock in locks
        for period in ([lock["period"]] if "period" in lock else periods)
    }


def _held(result, cells):
    return {
        entry["course_code"]: (entry["day"], entry["period"], json.dumps(entry["rooms"]))
        for entry in result["schedule"]
        if (entry["day"], entry["period"]) in cells
    }


def test_no_exam_moves_out_of_or_into_a_locked_cell(client_):
    """Tuesday is locked and empty, and SA's cell is locked. Without the locks SA or SB
    could leave the crowded Sunday for Tuesday; with them nothing can move at all."""
    _populate(STUCK, STUCK_GROUPS)
    _rooms()
    built = _build(client_, days=DAYS, periods=TWO, assign_rooms=True)
    locks = [{"day": "Tue"}, {"day": "Sun", "period": TWO[0]}]
    pins = [_pin("ST3"), _pin("ST4")]
    saved = _locked_run(
        client_,
        built,
        _board(built, STUCK_BOARD),
        locks,
        days=DAYS,
        periods=TWO,
        pinned=pins,
    )
    assert sorted(saved["exam_locks"], key=json.dumps) == sorted(locks, key=json.dumps)
    runs = ExamTimetableRun.objects.count()

    result = _optimise(client_, saved, saved["schedule"], days=DAYS, periods=TWO)

    assert result["saved"] is False
    assert result["optimisation"]["improved"] is False
    assert ExamTimetableRun.objects.count() == runs
    # The control: the same run with its locks lifted is improvable.
    lifted = _optimise(client_, saved, saved["schedule"], days=DAYS, periods=TWO, exam_locks=[])
    assert lifted["optimisation"]["improved"] is True
    assert lifted["run_id"]


def test_a_locked_cell_keeps_its_exams_and_rooms_while_the_rest_improves(client_):
    _populate(CROWD, CROWD_GROUPS)
    _rooms()
    built = _build(client_, assign_rooms=True)
    # CR1 and CR2 sit together on Sunday by the registrar's decision, and stay.
    locks = [{"day": "Sun", "period": PERIODS[0]}, {"day": "Sun", "period": PERIODS[1]}]
    saved = _locked_run(client_, built, _board(built, CROWDED), locks)
    cells = _cells(locks, PERIODS)
    held = _held(saved, cells)
    assert set(held) == {"CR1", "CR2"}

    result = _optimise(client_, saved, saved["schedule"])

    assert result["optimisation"]["improved"] is True
    assert _held(result, cells) == held, "the locked exams keep their cells and their rooms"
    assert result["optimisation"]["fixed"] == 2
    assert sorted(result["exam_locks"], key=json.dumps) == sorted(locks, key=json.dumps)
    # CR3 left the crowded day: students over the daily limit are spared.
    assert result["qa"]["students_over_limit_per_day"] == 0
    assert _where(result)["CR3"][0] != "Sun"
    assert _scored(result["optimisation"]["after"]) == _numbers(result["qa"])


# ── links ────────────────────────────────────────────────────────────────────

# LP and LQ are one exam. One student sits both (two papers at one time, always
# on one day); LR and LS are pinned beside the link on Sunday, each sharing three
# and two students with it. Only the link can move, and it must move whole.
LINKED = [("LP1", 3), ("LQ1", 3), ("LR1", 3), ("LS1", 3)]
LINKED_GROUPS = [
    (3, "F", ["LP1", "LR1"]),
    (2, "M", ["LQ1", "LS1"]),
    (1, "F", ["LP1", "LQ1"]),
]
LINKED_BOARD = {
    "LP1": ("Sun", PERIODS[0]),
    "LQ1": ("Sun", PERIODS[0]),
    "LR1": ("Sun", PERIODS[1]),
    "LS1": ("Sun", PERIODS[2]),
}


def test_a_link_moves_as_one_exam_and_its_student_in_both_is_counted_as_the_qa_counts_it(
    client_,
):
    _populate(LINKED, LINKED_GROUPS)
    _, meta = build_enrolled_sets_with_meta(**SCOPE)
    link = {
        "members": [
            {"course_identity": meta[code]["course_identity"], "course_code": code}
            for code in ("LP1", "LQ1")
        ]
    }
    built = _build(client_, linked_exams=[link])
    assert _where(built)["LP1"] == _where(built)["LQ1"]
    board = _board(built, LINKED_BOARD)
    pins = [
        {"course_code": code, "day": "Sun", "period": LINKED_BOARD[code][1]}
        for code in ("LR1", "LS1")
    ]
    submitted = _check(client_, built, board, pinned=pins)
    assert submitted["qa"]["multi_exam_day_students"] == 6

    result = _optimise(client_, built, board, pinned=pins)

    report = result["optimisation"]
    where = _where(result)
    assert report["improved"] is True
    assert where["LP1"] == where["LQ1"], "a link is never split"
    assert where["LP1"][0] != "Sun"
    assert (where["LR1"], where["LS1"]) == (LINKED_BOARD["LR1"], LINKED_BOARD["LS1"])
    assert result["linked_exams"] == built["linked_exams"]
    assert (report["moved"], report["movable"], report["fixed"]) == (2, 2, 2)
    # The student sitting both members of the link still has two papers on one
    # day, and the engine counts him exactly as the QA report does.
    assert result["qa"]["multi_exam_day_students"] == 1
    assert _scored(report["after"]) == _numbers(result["qa"])
    assert _scored(report["before"]) == _numbers(submitted["qa"])


# ── the invigilator limit ────────────────────────────────────────────────────

# SF1 and SF2 share three students and sit on one day, as do the exams of the
# other days. Moving SF2 alone to any other day would put three exams (three
# invigilators) on it while every day of the submitted board has two, so the
# only board within the limit swaps SF2 with an exam of another day.
STAFFED = [("SF1", 3), ("SF2", 3), ("SF3", 3), ("SF4", 3), ("SF5", 3), ("SF6", 3)]
STAFFED_GROUPS = [
    (3, "M", ["SF1", "SF2"]),
    (2, "M", ["SF3"]),
    (2, "M", ["SF4"]),
    (2, "M", ["SF5"]),
    (2, "M", ["SF6"]),
]
STAFFED_BOARD = {
    "SF1": ("Sun", TWO[0]),
    "SF2": ("Sun", TWO[1]),
    "SF3": ("Mon", TWO[0]),
    "SF4": ("Mon", TWO[1]),
    "SF5": ("Tue", TWO[0]),
    "SF6": ("Tue", TWO[1]),
}


def _exams_a_day(result):
    days: dict[str, int] = {}
    for entry in result["schedule"]:
        days[entry["day"]] = days.get(entry["day"], 0) + 1
    return days


def _staffed(client, *, assign_rooms):
    _populate(STAFFED, STAFFED_GROUPS)
    _rooms()
    built = _build(client, periods=TWO, assign_rooms=True)
    board = _board(built, STAFFED_BOARD)
    return built, board, _optimise(client, built, board, periods=TWO, assign_rooms=assign_rooms)


def test_the_optimiser_never_puts_more_invigilators_on_a_day_than_the_submitted_boards_peak(
    client_,
):
    built, board, result = _staffed(client_, assign_rooms=True)

    report = result["optimisation"]
    assert report["invigilator_day_limits"] == {"M": 2, "total": 2}
    assert report["improved"] is True
    assert report["after"]["staff_excess"] == 0
    per_day = result["qa"]["rooms"]["invigilators_per_day"]
    assert max(counts["total"] for counts in per_day.values()) <= 2
    assert max(_exams_a_day(result).values()) == 2
    assert _scored(report["after"]) == _numbers(result["qa"])
    assert report["after"]["multi_exam_day"] == 0


def test_without_rooms_no_invigilator_limit_is_applied(client_):
    """The control that shows the limit bites: with no rooms there is nothing to
    count, the cheapest fix moves SF2 alone, and a day holds three exams."""
    _, _, result = _staffed(client_, assign_rooms=False)

    report = result["optimisation"]
    assert report["invigilator_day_limits"] == {}
    assert report["improved"] is True
    assert report["after"]["staff_excess"] == 0
    assert max(_exams_a_day(result).values()) == 3


# ── OVERFLOW ─────────────────────────────────────────────────────────────────

# One day, two periods. OX and OY are pinned in them and each shares a student
# with one member of the link OL, so the link has no legal seat at all; OW
# shares nothing and has both. All three start in OVERFLOW.
OVER = [("OV1", 3), ("OV2", 3), ("OL1", 3), ("OL2", 3), ("OW1", 3)]
OVER_GROUPS = [(1, "F", ["OV1", "OL1"]), (1, "M", ["OV2", "OL2"]), (2, "F", ["OW1"])]


def test_an_exam_in_overflow_with_a_legal_seat_is_seated_and_one_with_none_stays(client_):
    _populate(OVER, OVER_GROUPS)
    _, meta = build_enrolled_sets_with_meta(**SCOPE)
    link = {
        "members": [
            {"course_identity": meta[code]["course_identity"], "course_code": code}
            for code in ("OL1", "OL2")
        ]
    }
    built = _build(client_, days=["Sun"], periods=TWO, linked_exams=[link])
    board = _board(
        built,
        {"OV1": ("Sun", TWO[0]), "OV2": ("Sun", TWO[1]), "OL1": None, "OL2": None, "OW1": None},
    )
    first = next(entry for entry in board if entry["course_code"] == "OL1")
    for entry in board:
        if entry["course_code"] == "OL2":  # a link leaves the board as one exam
            entry.update(day="OVERFLOW", period=first["period"], slot_index=first["slot_index"])
    pins = [
        {"course_code": "OV1", "day": "Sun", "period": TWO[0]},
        {"course_code": "OV2", "day": "Sun", "period": TWO[1]},
    ]

    result = _optimise(client_, built, board, days=["Sun"], periods=TWO, pinned=pins)

    where = _where(result)
    report = result["optimisation"]
    assert where["OW1"][0] == "Sun", "it had a legal seat"
    assert where["OL1"][0] == where["OL2"][0] == "OVERFLOW", "the link had none"
    assert where["OL1"] == where["OL2"], "one shared Extra-n for the link"
    assert where["OL1"] == ("OVERFLOW", first["period"]), "an exam left in OVERFLOW is untouched"
    assert (report["before"]["unseated"], report["after"]["unseated"]) == (3, 2)
    assert report["sent_to_overflow"] == 0, "the link was already there"
    assert report["improved"] is True


CLASH = [("CL1", 3), ("CL2", 3), ("CL3", 3)]
CLASH_GROUPS = [(3, "F", ["CL1", "CL2"]), (2, "M", ["CL1", "CL3"])]


def test_a_clash_between_movable_exams_is_ended_first(client_):
    """A hand-made clash used to be re-coloured away by the greedy; it still goes."""
    _populate(CLASH, CLASH_GROUPS)
    built = _build(client_)
    board = _board(
        built,
        {"CL1": ("Sun", PERIODS[0]), "CL2": ("Sun", PERIODS[0]), "CL3": ("Tue", PERIODS[0])},
    )
    assert _check(client_, built, board)["qa"]["conflict_count"] == 3

    result = _optimise(client_, built, board)

    report = result["optimisation"]
    assert (report["before"]["rule_breaks"], report["after"]["rule_breaks"]) == (1, 0)
    assert report["improved"] is True
    assert result["qa"]["conflict_count"] == 0
    where = _where(result)
    assert where["CL1"] != where["CL2"]


def test_a_movable_exam_clashing_with_a_pin_and_with_no_other_seat_goes_to_overflow(client_):
    """The one period is the pinned exam's: the exam clashing with it leaves the board."""
    _populate(CLASH[:2], CLASH_GROUPS[:1])
    one = PERIODS[:1]
    built = _build(client_, days=["Sun"], periods=one)
    board = _board(built, {"CL1": ("Sun", one[0]), "CL2": ("Sun", one[0])})
    pins = [{"course_code": "CL1", "day": "Sun", "period": one[0]}]
    runs = ExamTimetableRun.objects.count()

    result = _optimise(client_, built, board, days=["Sun"], periods=one, pinned=pins)

    report = result["optimisation"]
    assert report["improved"] is True
    assert (report["before"]["rule_breaks"], report["after"]["rule_breaks"]) == (1, 0)
    assert (report["before"]["unseated"], report["after"]["unseated"]) == (0, 1)
    assert report["sent_to_overflow"] == 1
    where = _where(result)
    assert where["CL1"] == ("Sun", one[0]), "the pinned exam stays"
    assert where["CL2"][0] == "OVERFLOW" and where["CL2"][1].startswith("Extra-")
    assert result["qa"]["conflict_count"] == 0
    assert ExamTimetableRun.objects.count() == runs + 1


def test_a_pin_that_seats_an_overflow_exam_is_reported_as_the_improvement_it_is(client_):
    """Before is the board as submitted - the exam in OVERFLOW - not after the pin."""
    _populate(CLASH[:2], CLASH_GROUPS[:1])
    built = _build(client_, days=DAYS[:2], periods=TWO)
    board = _board(built, {"CL1": ("Sun", TWO[0])})
    for entry in board:
        if entry["course_code"] == "CL2":
            entry.update(day="OVERFLOW", period="Extra-4", slot_index=4)
    pins = [
        {"course_code": "CL1", "day": "Sun", "period": TWO[0]},
        {"course_code": "CL2", "day": "Mon", "period": TWO[0]},
    ]

    result = _optimise(client_, built, board, days=DAYS[:2], periods=TWO, pinned=pins)

    report = result["optimisation"]
    assert (report["before"]["unseated"], report["after"]["unseated"]) == (1, 0)
    assert report["improved"] is True and report["moved"] == 1
    assert _where(result)["CL2"] == ("Mon", TWO[0])


# ── jobs, requests and the greedy rollback ───────────────────────────────────


@pytest.fixture
def jobs(settings):
    settings.EXAM_JOBS_ENABLED = True
    settings.EXAM_JOBS_RUN_INLINE = True
    settings.EXAM_OPTIMISE_ROUNDS = 123
    settings.EXAM_OPTIMISE_SYNC_ROUNDS = 17
    return settings


def _spy_on_the_engine(monkeypatch):
    calls = []
    real = exam_views.optimise_board

    def spy(board, **kwargs):
        calls.append(kwargs)
        return real(board, **kwargs)

    monkeypatch.setattr(exam_views, "optimise_board", spy)
    return calls


def _as_job(client, built, board, **changes):
    payload = {**_payload(built, board, **changes), "mode": "optimize_loaded"}
    response = client.post(
        reverse("exam_timetable_build"),
        payload,
        content_type="application/json",
        HTTP_X_EXAM_JOBS="1",
    )
    assert response.status_code == 202, response.content
    return response.json()["job"]["id"]


def test_a_background_job_searches_with_the_jobs_rounds_and_a_request_with_the_sync_rounds(
    client_, crowd, jobs, monkeypatch
):
    built, board = crowd
    calls = _spy_on_the_engine(monkeypatch)

    jobs.EXAM_JOBS_ENABLED = False
    _optimise(client_, built, board)
    jobs.EXAM_JOBS_ENABLED = True
    job_id = _as_job(client_, built, board)
    status, body = _result(client_, job_id)

    assert status == 200 and body["optimisation"]["improved"] is True
    assert [call["rounds"] for call in calls] == [17, 123]


def test_a_cancelled_optimise_job_saves_nothing_whether_or_not_it_would_have_improved(
    client_, crowd, jobs, monkeypatch
):
    from core.services.exam_progress import current

    built, board = crowd
    better = _optimise(client_, built, board)
    real = exam_views.optimise_board

    def cancelled_mid_search(board, **kwargs):
        current().cancelled.set()  # what the flusher does when the person presses Cancel
        return real(board, **kwargs)

    monkeypatch.setattr(exam_views, "optimise_board", cancelled_mid_search)
    runs = ExamTimetableRun.objects.count()
    for source, submitted in ((built, board), (better, better["schedule"])):
        job_id = _as_job(client_, source, submitted)
        job = _poll(client_, job_id)
        assert (job["status"], job["error_code"]) == ("cancelled", "cancelled")
        assert ExamTimetableRun.objects.count() == runs


# Fourteen exams, more than one solve frees: the search runs its rounds.
MANY = [(f"MN{index:02d}", 3) for index in range(14)]
MANY_GROUPS = [(2, "F", [f"MN{index:02d}", f"MN{index + 1:02d}"]) for index in range(0, 14, 2)]


@pytest.fixture
def many(client_):
    """Seven pairs of exams, each pair sharing two students and one day."""
    _populate(MANY, MANY_GROUPS)
    days = ["Sun", "Mon", "Tue", "Wed", "Thu"]
    built = _build(client_, days=days, periods=PERIODS)
    cells = [(day, period) for day in days for period in PERIODS]
    board = _board(built, {code: cells[index] for index, (code, _) in enumerate(MANY)})
    return built, board, {"days": days, "periods": PERIODS}


def test_a_board_larger_than_one_solve_is_searched_in_rounds_that_report_progress(
    client_, many, jobs, monkeypatch
):
    built, board, header = many
    ticks = []
    real = exam_jobs.JobProgress.counter

    def counter(self, key):
        tick = real(self, key)

        def recorded(done, total):
            ticks.append((key, done, total))
            tick(done, total)

        return recorded

    monkeypatch.setattr(exam_jobs.JobProgress, "counter", counter)
    jobs.EXAM_OPTIMISE_ROUNDS = 9

    job_id = _as_job(client_, built, board, **header)
    status, body = _result(client_, job_id)

    assert status == 200
    report = body["optimisation"]
    assert report["improved"] is True and report["proven"] is False
    assert 0 < report["after"]["multi_exam_day"] + 1 <= report["before"]["multi_exam_day"]
    placed = [tick for tick in ticks if tick[0] == "place_exams"]
    assert placed[0] == ("place_exams", 0, 9)
    assert [done for _key, done, _total in placed] == list(range(len(placed)))
    assert 1 < len(placed) <= 9


def test_a_job_cancelled_between_rounds_stops_there_and_saves_nothing(
    client_, many, jobs, monkeypatch
):
    from core.services import exam_optimise
    from core.services.exam_progress import current

    built, board, header = many
    rounds = []
    real = exam_optimise._Search.improve

    def improve(self, free):
        rounds.append(len(rounds))
        if len(rounds) == 2:
            current().cancelled.set()  # the person pressed Stop during the second solve
        return real(self, free)

    monkeypatch.setattr(exam_optimise._Search, "improve", improve)
    jobs.EXAM_OPTIMISE_ROUNDS = 9
    runs = ExamTimetableRun.objects.count()

    job = _poll(client_, _as_job(client_, built, board, **header))

    assert (job["status"], job["error_code"]) == ("cancelled", "cancelled")
    assert len(rounds) == 2, "the third round never started"
    assert ExamTimetableRun.objects.count() == runs


def _peak(result):
    per_day = result["qa"]["rooms"]["invigilators_per_day"]
    return max(counts["total"] for counts in per_day.values())


def test_the_report_states_the_busiest_invigilator_day_as_the_rooms_really_came_out(client_):
    built, board, _dragged = _staffed(client_, assign_rooms=True)
    saved = _save(client_, built, board, periods=TWO, assign_rooms=True)

    result = _optimise(client_, saved, saved["schedule"], periods=TWO, assign_rooms=True)

    assert result["optimisation"]["invigilator_peak"] == {
        "before": _peak(saved),
        "after": _peak(result),
    }
    assert exam_views._invigilator_peak({"qa": {"rooms": {}}}) is None
    assert exam_views._invigilator_peak(None) is None


def test_after_a_drag_the_peak_before_is_not_known_and_is_not_guessed(client_):
    """The saved run's peak is not the dragged board's: the report leaves it out."""
    built, _board_, result = _staffed(client_, assign_rooms=True)
    assert result["optimisation"]["invigilator_peak"] == {"before": None, "after": _peak(result)}


def test_the_peak_before_is_the_saved_runs_and_the_peak_after_is_the_new_boards(
    client_, monkeypatch
):
    built, board, _result = _staffed(client_, assign_rooms=True)
    saved = _save(client_, built, board, periods=TWO, assign_rooms=True)
    monkeypatch.setattr(
        exam_views,
        "_invigilator_peak",
        lambda run: 222 if run.get("rebuild_mode") == "optimized_from_loaded" else 111,
    )
    result = _optimise(client_, saved, saved["schedule"], periods=TWO, assign_rooms=True)
    assert result["optimisation"]["invigilator_peak"] == {"before": 111, "after": 222}


def test_an_unticked_course_is_saved_although_nothing_is_better(client_, stuck):
    """The course list lives only in the request, like a pin."""
    built, board = stuck
    pins = [_pin("ST1"), _pin("ST2"), _pin("ST3"), _pin("ST4")]
    header = {"days": DAYS[:2], "periods": TWO}
    saved = _save(client_, built, board, **header, pinned=pins)
    fewer = [entry for entry in saved["schedule"] if entry["course_code"] != "ST4"]

    result = _optimise(
        client_,
        saved,
        fewer,
        **header,
        pinned=pins[:3],
        selected_courses=[entry["course_code"] for entry in fewer],
    )

    assert result["run_id"] != saved["run_id"]
    assert sorted(_where(result)) == ["ST1", "ST2", "ST3"]


def test_the_rollback_flag_takes_the_greedy_path_and_the_default_the_solver(
    client_, crowd, settings, monkeypatch
):
    built, board = crowd
    solver = _spy_on_the_engine(monkeypatch)
    greedy = []
    real = exam_views.schedule_linked

    def spy(*args, **kwargs):
        greedy.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(exam_views, "schedule_linked", spy)

    _optimise(client_, built, board)
    assert len(solver) == 1 and not greedy

    settings.EXAM_OPTIMISE_EXACT = False
    result = _optimise(client_, built, board)
    assert len(solver) == 1 and greedy
    assert "optimisation" not in result, "the greedy answer is what it always was"


# ── the stage plan ───────────────────────────────────────────────────────────


def test_the_invigilator_stage_is_reported_skipped_never_left_pending_in_a_finished_job(
    client_, jobs
):
    """The plan still lists ``balance_invigilators`` (the greedy path enters it);
    the solver path never does, so a finished job must say it was skipped."""
    assert "balance_invigilators" in exam_jobs.STAGES[ExamTimetableJob.KIND_OPTIMIZE]
    _populate(CROWD, CROWD_GROUPS)
    _rooms()
    jobs.EXAM_JOBS_ENABLED = False
    built = _build(client_, assign_rooms=True)
    board = _board(built, CROWDED)
    jobs.EXAM_JOBS_ENABLED = True

    def finished(source, submitted):
        job_id = _as_job(client_, source, submitted)
        status, body = _result(client_, job_id)
        job = _poll(client_, job_id)
        assert (status, job["status"]) == (200, "succeeded")
        return body, _states(job)

    saved, states = finished(built, board)
    assert saved["run_id"]
    assert states["balance_invigilators"] == "skipped", states
    assert states["place_exams"] == states["assign_rooms"] == states["save"] == "done", states
    assert set(states.values()) <= {"done", "skipped"}, states

    # A second press finds nothing better and saves nothing: still no stage left open.
    again, states = finished(saved, saved["schedule"])
    assert again["saved"] is False
    assert states["balance_invigilators"] == "skipped", states
    assert states["place_exams"] == "done" and states["save"] == "skipped", states
    assert set(states.values()) <= {"done", "skipped"}, states

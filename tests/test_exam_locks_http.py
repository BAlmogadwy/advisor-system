"""Exam locks through the API: Build, Check, Save, Optimise, Fix, multistart, jobs and Copy.

In this release locks are reachable only through the request's ``exam_locks``
(the page gains its lock buttons next). A lock is saved with its timetable,
like a pin or a link: Check, then Save, with the lock. From then on every
action keeps the locked cells as that run saved them - their exams, their
rooms, their invigilators - and never places anything else there.
"""

import json
from copy import deepcopy

import pytest
from django.core.cache import cache
from django.urls import reverse

from core import models
from core.models import ExamTimetableJob, ExamTimetableRun, Student, StudentTermSection, TermSection
from core.services import exam_jobs, exam_timetable
from core.services.exam_rosters import CHANGED, build_roster_model
from core.services.exam_run_schema import load_normalised_run
from core.services.exam_timetable import _invigilators_per_day, _physical_exam_rooms
from tests import exam_linked_parity_corpus as corpus

pytestmark = pytest.mark.django_db

DAYS = corpus.POPULATION_DAYS
PERIODS = corpus.POPULATION_PERIODS
SCOPE = {"programs": ["AI", "CS"], "sections": ["F", "M"]}


@pytest.fixture
def population():
    corpus.create_population(models)


@pytest.fixture
def client_(client, django_user_model, monkeypatch):
    monkeypatch.setattr("core.authz._rate_buckets", {})
    cache.clear()
    client.force_login(django_user_model.objects.create_superuser(username="locks-admin"))
    return client


def _post(client, payload, status=200, url="exam_timetable_build", **headers):
    response = client.post(reverse(url), payload, content_type="application/json", **headers)
    assert response.status_code == status, response.content
    return response.json()


def _build(client, *, status=200, **changes):
    return _post(
        client,
        {
            "label": "Locks",
            "days": DAYS,
            "periods": PERIODS,
            "max_per_day": 2,
            **SCOPE,
            "assign_rooms": True,
            "pinned": [],
            **changes,
        },
        status=status,
    )


def _loaded(client, source, board=None, *, mode=None, status=200, **changes):
    payload = {
        "label": "Locks",
        "days": DAYS,
        "periods": PERIODS,
        "max_per_day": 2,
        **SCOPE,
        "previous_run_id": source["run_id"],
        "base_schedule": deepcopy(source["schedule"] if board is None else board),
        "pinned": source["pinned"],
        **changes,
    }
    if mode is None:
        return _post(client, payload, status=status, url="exam_timetable_draft_impact")
    return _post(client, {**payload, "mode": mode}, status=status)


def _save(client, source, board=None, **changes):
    """Check, then Save what was checked: how a lock (or any edit) is saved."""
    checked = _loaded(client, source, board, **changes)
    return _loaded(
        client,
        source,
        board,
        mode="save_loaded_changes",
        expected_input_fingerprint=checked["input_fingerprint"],
        **changes,
    )


def _cells(result) -> dict[tuple[str, str], list[str]]:
    cells: dict[tuple[str, str], list[str]] = {}
    for entry in result["schedule"]:
        if entry["day"] != "OVERFLOW":
            cells.setdefault((entry["day"], entry["period"]), []).append(entry["course_code"])
    return {cell: sorted(codes) for cell, codes in cells.items()}


def _choose_locks(result) -> list[dict]:
    """A whole day with exams, and one period with exams on a later day."""
    cells = _cells(result)
    busy = [day for day in DAYS if any(cells.get((day, period)) for period in PERIODS)]
    day = busy[0]
    other = next((d, p) for d in busy[1:] for p in PERIODS if cells.get((d, p)))
    return [{"day": day}, {"day": other[0], "period": other[1]}]


def _locked_cells(locks) -> set[tuple[str, str]]:
    return {
        (item["day"], period)
        for item in locks
        for period in ([item["period"]] if "period" in item else PERIODS)
    }


def _held(result, locks) -> dict[str, tuple]:
    """Every exam in a locked cell: its cell and its saved room rows, as bytes."""
    cells = _locked_cells(locks)
    return {
        entry["course_code"]: (
            entry["day"],
            entry["period"],
            json.dumps(entry["rooms"], ensure_ascii=False),
        )
        for entry in result["schedule"]
        if (entry["day"], entry["period"]) in cells
    }


def _staff(result, locks) -> dict:
    """The invigilators of the locked cells alone, counted once per physical room."""
    cells = _locked_cells(locks)
    return _invigilators_per_day(
        _physical_exam_rooms(
            (entry, room)
            for entry in result["schedule"]
            if (entry["day"], entry["period"]) in cells
            for room in entry["rooms"]
        )
    )


def _assert_kept(before, after, locks):
    assert _held(after, locks) == _held(before, locks)
    assert _staff(after, locks) == _staff(before, locks)


def _qa(result) -> dict:
    """The QA a fixed-time Check reproduces: all of it but the time it was read."""
    qa = deepcopy(result["qa"])
    qa["enrolment_snapshot"].pop("snapshot_timestamp")
    return qa


def _saved(result) -> dict:
    return load_normalised_run(ExamTimetableRun.objects.get(pk=result["run_id"]))


@pytest.fixture
def locked(client_, population):
    """A build, and the same timetable saved with a day and a period locked."""
    built = _build(client_)
    locks = _choose_locks(built)
    saved = _save(client_, built, exam_locks=locks)
    assert saved["exam_locks"] == locks
    return built, saved, locks


# ── Check and Save ───────────────────────────────────────────────────────────


def test_a_lock_is_saved_and_its_cells_keep_their_exams_rooms_and_staff(client_, locked):
    built, saved, locks = locked
    assert _held(built, locks), "the lock holds exams"
    _assert_kept(built, saved, locks)
    stored = json.loads(ExamTimetableRun.objects.get(pk=saved["run_id"]).result_json)
    assert stored["exam_locks"] == locks
    report = stored["qa"]["exam_locks"]
    assert report["locked_courses"] == len(_held(built, locks))
    assert [(cell["day"], cell["period"]) for cell in report["cells"]] == sorted(
        _locked_cells(locks), key=lambda cell: (DAYS.index(cell[0]), PERIODS.index(cell[1]))
    )
    # Checking the saved run again with its own locks reproduces it exactly.
    again = _loaded(client_, saved)
    assert again["source_inputs_changed"] is False
    assert _qa(again) == _qa(saved)
    _assert_kept(built, again, locks)


def test_a_run_without_locks_carries_no_lock_key(client_, population):
    built = _build(client_)
    assert "exam_locks" not in built
    assert "exam_locks" not in built["qa"]
    stored = json.loads(ExamTimetableRun.objects.get(pk=built["run_id"]).result_json)
    assert "exam_locks" not in stored and "exam_locks" not in stored["qa"]
    checked = _loaded(client_, built)
    assert "exam_locks" not in checked
    assert checked["source_inputs_changed"] is False


def test_a_loaded_action_without_the_key_keeps_the_saved_locks(client_, locked):
    built, saved, locks = locked
    checked = _loaded(client_, saved)
    assert checked["exam_locks"] == locks
    unlocked = _loaded(client_, saved, exam_locks=[])
    assert "exam_locks" not in unlocked
    # Unlocking is an input change the Check must be reviewed for.
    assert unlocked["source_inputs_changed"] is True


def test_a_save_reviewed_under_other_locks_is_refused(client_, locked):
    built, saved, locks = locked
    checked = _loaded(client_, saved, exam_locks=locks[:1])
    body = _loaded(
        client_,
        saved,
        mode="save_loaded_changes",
        status=409,
        exam_locks=locks,
        expected_input_fingerprint=checked["input_fingerprint"],
    )
    assert body["error_code"] == "inputs_changed"


# ── Optimise and Fix ─────────────────────────────────────────────────────────


def test_optimise_keeps_the_locked_cells_and_places_nothing_in_them(client_, locked):
    built, saved, locks = locked
    optimised = _loaded(client_, saved, mode="optimize_loaded", randomize=True)
    assert optimised["exam_locks"] == locks
    _assert_kept(built, optimised, locks)
    cells = _locked_cells(locks)
    held = set(_held(built, locks))
    assert (
        not {
            entry["course_code"]
            for entry in optimised["schedule"]
            if (entry["day"], entry["period"]) in cells
        }
        - held
    )


def _unlocked_clash(result, locks):
    """An unlocked exam dragged onto an unlocked clashing neighbour's cell."""
    cells = _locked_cells(locks)
    where = {entry["course_code"]: entry for entry in result["schedule"]}
    free = {
        code
        for code, entry in where.items()
        if entry["day"] != "OVERFLOW" and (entry["day"], entry["period"]) not in cells
    }
    for row in result["conflicts"]:
        if {row["course_a"], row["course_b"]} <= free:
            anchor = where[row["course_a"]]
            board = deepcopy(result["schedule"])
            for entry in board:
                if entry["course_code"] == row["course_b"]:
                    entry.update(day=anchor["day"], period=anchor["period"])
            return board, row["course_b"]
    pytest.skip("no two unlocked exams share a student")


def test_fix_repairs_around_the_locked_cells_and_counts_them(client_, locked):
    built, saved, locks = locked
    board, dragged = _unlocked_clash(saved, locks)
    fixed = _loaded(client_, saved, board, mode="minimum_change_repair")
    report = fixed["minimum_change"]
    assert report["locked_count"] == len(_held(built, locks))
    assert report["locked_violations"] == 0
    assert report["moves"] or report["unseated"], "the drag made a clash to repair"
    assert fixed["run_id"]
    _assert_kept(built, fixed, locks)
    cells = _locked_cells(locks)
    for move in report["moves"]:
        assert (move["to"]["day"], move["to"]["period"]) not in cells
    assert fixed["exam_locks"] == locks


# ── Build (the owner's scenario) and multistart ─────────────────────────────


def test_a_build_with_an_added_course_rebuilds_only_around_the_locked_cells(client_, population):
    everything = sorted(_build(client_)["courses"])
    added = "LX108"
    first = _build(client_, selected_courses=[code for code in everything if code != added])
    locks = _choose_locks(first)
    saved = _save(client_, first, exam_locks=locks)
    rebuilt = _build(client_, selected_courses=everything, previous_run_id=saved["run_id"])
    assert rebuilt["exam_locks"] == locks  # inherited: never dropped by omission
    _assert_kept(first, rebuilt, locks)
    assert added in rebuilt["courses"]
    where = {entry["course_code"]: (entry["day"], entry["period"]) for entry in rebuilt["schedule"]}
    assert where[added] not in _locked_cells(locks)
    stored = _saved(rebuilt)
    assert stored["exam_locks"] == locks


def test_a_build_sent_no_locks_is_unlocked(client_, locked):
    built, saved, locks = locked
    rebuilt = _build(client_, previous_run_id=saved["run_id"], exam_locks=[])
    assert "exam_locks" not in rebuilt


@pytest.mark.parametrize(
    "previous", ["deleted", "garbage", True, None, "unreadable", "not-ok"], ids=str
)
def test_a_build_whose_previous_run_is_not_readable_is_masters_build(client_, locked, previous):
    """Master's Build never read ``previous_run_id``: a run that cannot be read
    - gone, not a number, not JSON, not an ``ok`` run (even one naming locks) -
    gives exactly the Build a request without it gives."""
    built, saved, locks = locked
    plain = _build(client_)
    runs = ExamTimetableRun.objects.filter(pk=saved["run_id"])
    if previous == "deleted":
        runs.delete()
        previous = saved["run_id"]
    elif previous == "unreadable":
        runs.update(result_json="{not json")
        previous = saved["run_id"]
    elif previous == "not-ok":
        runs.update(
            result_json=json.dumps(
                {"schema_version": 6, "status": "feasibility_error", "exam_locks": locks}
            )
        )
        previous = saved["run_id"]
    rebuilt = _build(client_, previous_run_id=previous)
    assert "exam_locks" not in rebuilt
    assert corpus.comparable(rebuilt) == corpus.comparable(plain)


def test_a_build_from_a_saved_run_without_locks_keeps_masters_links_default(client_, population):
    """Links are inherited only WITH the locks they were checked with. A Build
    from a run that has links but no locks, sending no ``linked_exams``, links
    nothing - master's default - and is exactly the Build without the run."""
    plain = _build(client_)
    clashing = {frozenset((row["course_a"], row["course_b"])) for row in plain["conflicts"]}
    codes = sorted(entry["course_code"] for entry in plain["schedule"])
    pair = next((a, b) for a in codes for b in codes if a < b and frozenset((a, b)) not in clashing)
    by_code = {entry["course_code"]: entry for entry in plain["schedule"]}
    link = {
        "members": [
            {"course_identity": by_code[code]["course_identity"], "course_code": code}
            for code in pair
        ]
    }
    linked = _build(client_, linked_exams=[link])
    assert linked["linked_exams"], "the saved run has a link"
    rebuilt = _build(client_, previous_run_id=linked["run_id"])
    assert not rebuilt.get("linked_exams")
    assert "exam_locks" not in rebuilt
    assert corpus.comparable(rebuilt) == corpus.comparable(plain)


def test_a_build_with_locks_and_no_saved_run_is_refused(client_, locked):
    built, saved, locks = locked
    ExamTimetableRun.objects.filter(pk=saved["run_id"]).delete()
    body = _build(client_, status=400, previous_run_id=saved["run_id"], exam_locks=locks)
    assert (body["code"], body["field"]) == ("exam_locks_source_required", "previous_run_id")
    assert ExamTimetableRun.objects.filter(label="Locks").count() == 1  # the first build only


def test_a_build_for_another_scope_is_refused_while_locked(client_, locked):
    built, saved, locks = locked
    body = _build(client_, status=400, previous_run_id=saved["run_id"], sections=["M"])
    assert body["code"] == "exam_locks_scope_changed"


def test_every_multistart_candidate_keeps_the_locked_cells(client_, locked, settings):
    settings.TIMETABLE_EXAM_MULTISTART_ENABLED = True
    built, saved, locks = locked
    body = _build(
        client_,
        previous_run_id=saved["run_id"],
        multistart=True,
        n_runs=3,
        time_budget_s=60,
        randomize=True,
    )
    candidates = body["multistart"]["candidates"]
    assert candidates
    for candidate in candidates.values():
        payload = candidate["payload"]
        assert payload["exam_locks"] == locks
        _assert_kept(built, payload, locks)
        assert not set(candidate["courses_moved"]) & set(_held(built, locks))


# ── refusals (rule 6, rule 4) ───────────────────────────────────────────────


def _code(body) -> tuple:
    return body["code"], body["field"]


def _move(board, code, day, period):
    moved = deepcopy(board)
    for entry in moved:
        if entry["course_code"] == code:
            entry.update(day=day, period=period)
    return moved


def _free_cell(result, locks) -> tuple[str, str]:
    """An unlocked cell: an empty one when there is one."""
    used = set(_cells(result))
    cells = _locked_cells(locks)
    unlocked = [(d, p) for d in DAYS for p in PERIODS if (d, p) not in cells]
    return next((cell for cell in unlocked if cell not in used), unlocked[-1])


@pytest.mark.parametrize(
    "mode", [None, "save_loaded_changes", "optimize_loaded", "minimum_change_repair"]
)
def test_moving_an_exam_out_of_a_locked_cell_is_refused(client_, locked, mode):
    built, saved, locks = locked
    code = sorted(_held(built, locks))[0]
    board = _move(saved["schedule"], code, *_free_cell(saved, locks))
    extra = (
        {"expected_input_fingerprint": saved["input_fingerprint"]}
        if mode == "save_loaded_changes"
        else {}
    )
    body = _loaded(client_, saved, board, mode=mode, status=400, **extra)
    assert body["code"] == "exam_locks_moved_out"
    assert body["courses"] == [code]


def test_moving_an_exam_into_a_locked_cell_is_refused(client_, locked):
    built, saved, locks = locked
    cells = _locked_cells(locks)
    outsider = next(
        entry["course_code"]
        for entry in saved["schedule"]
        if (entry["day"], entry["period"]) not in cells
    )
    target = sorted(cells, key=lambda cell: (DAYS.index(cell[0]), PERIODS.index(cell[1])))[0]
    body = _loaded(client_, saved, _move(saved["schedule"], outsider, *target), status=400)
    assert body["code"] == "exam_locks_moved_in"
    assert body["cell"] == {"day": target[0], "period": target[1]}


def test_pins_into_and_out_of_a_locked_cell_are_refused(client_, locked):
    built, saved, locks = locked
    cells = _locked_cells(locks)
    code = sorted(_held(built, locks))[0]
    day, period = _held(built, locks)[code][:2]
    free = _free_cell(saved, locks)
    body = _loaded(
        client_,
        saved,
        mode="optimize_loaded",
        status=400,
        pinned=[{"course_code": code, "day": free[0], "period": free[1]}],
    )
    assert _code(body) == ("exam_locks_pinned_elsewhere", "pinned")
    outsider = next(
        entry
        for entry in saved["schedule"]
        if (entry["day"], entry["period"]) not in cells and entry["day"] != "OVERFLOW"
    )
    body = _loaded(
        client_,
        saved,
        mode="optimize_loaded",
        status=400,
        pinned=[{"course_code": outsider["course_code"], "day": day, "period": period}],
    )
    assert _code(body) == ("exam_locks_pin_in_locked_cell", "pinned")
    # A pin of a locked exam at its own cell is fine.
    ok = _loaded(client_, saved, pinned=[{"course_code": code, "day": day, "period": period}])
    assert ok["exam_locks"] == locks


def test_a_link_reaching_out_of_a_locked_cell_is_refused(client_, locked):
    built, saved, locks = locked
    cells = _locked_cells(locks)
    inside = sorted(_held(built, locks))[0]
    outsider = next(
        entry
        for entry in saved["schedule"]
        if (entry["day"], entry["period"]) not in cells and entry["day"] != "OVERFLOW"
    )
    by_code = {entry["course_code"]: entry for entry in saved["schedule"]}
    link = {
        "members": [
            {"course_identity": by_code[code]["course_identity"], "course_code": code}
            for code in (inside, outsider["course_code"])
        ]
    }
    body = _loaded(client_, saved, status=400, linked_exams=[link])
    assert _code(body) == ("exam_locks_link_outside", "linked_exams[0]")
    assert body["courses"] == sorted([inside, outsider["course_code"]])


def test_locking_over_unsaved_changes_is_refused(client_, population):
    built = _build(client_)
    locks = _choose_locks(built)
    code = sorted(_held(built, locks))[0]
    board = _move(built["schedule"], code, *_free_cell(built, locks))
    body = _loaded(client_, built, board, status=400, exam_locks=locks)
    assert body["code"] == "exam_locks_cell_unsaved"


@pytest.mark.parametrize("mode", [None, "optimize_loaded"])
def test_a_null_lock_list_is_refused(client_, locked, mode):
    built, saved, locks = locked
    body = _loaded(client_, saved, mode=mode, status=400, exam_locks=None)
    assert _code(body) == ("exam_locks_invalid", "exam_locks")
    body = _build(client_, status=400, previous_run_id=saved["run_id"], exam_locks=None)
    assert _code(body) == ("exam_locks_invalid", "exam_locks")


def test_locks_of_another_term_are_refused(client_, locked):
    built, saved, locks = locked
    # A newer registrar term appears: the saved rows are from the old one.
    student = Student.objects.order_by("student_id").first()
    section = TermSection.objects.filter(section=f"{student.section}1").first()
    StudentTermSection.objects.create(
        student_id=student.student_id,
        academic_year="1448",
        term="2",
        term_section=section,
        source="scraper_timetable",
    )
    body = _loaded(client_, saved, status=400)
    assert body["code"] == "exam_locks_term_changed"


# ── rooms (rule 2) ──────────────────────────────────────────────────────────


def test_no_locked_exam_ever_reaches_the_room_allocator(client_, locked, monkeypatch):
    built, saved, locks = locked
    held = set(_held(built, locks))
    asked: set[str] = set()
    real = exam_timetable.allocate_period

    def spy(demands, rooms, context, **kwargs):
        asked.update(demand["course_code"] for demand in demands)
        return real(demands, rooms, context, **kwargs)

    monkeypatch.setattr(exam_timetable, "allocate_period", spy)
    _loaded(client_, saved)
    _loaded(client_, saved, mode="optimize_loaded")
    board, _dragged = _unlocked_clash(saved, locks)
    _loaded(client_, saved, board, mode="minimum_change_repair")
    _build(client_, previous_run_id=saved["run_id"])
    assert asked, "the spy saw the allocator"
    assert not asked & held


def test_a_saved_room_the_allocator_would_never_choose_is_kept_byte_for_byte(client_, population):
    built = _build(client_)
    locks = _choose_locks(built)
    code = sorted(_held(built, locks))[0]
    # Seat the exam's first group in the largest free room of its cohort, as a
    # registrar's own choice: re-solving would put it back in a small one.
    run = ExamTimetableRun.objects.get(pk=built["run_id"])
    data = json.loads(run.result_json)
    entry = next(entry for entry in data["schedule"] if entry["course_code"] == code)
    room = entry["rooms"][0]
    taken = {
        row["room_code"]
        for other in data["schedule"]
        if other["slot_index"] == entry["slot_index"]
        for row in other["rooms"]
    }
    big = next(
        candidate
        for candidate in models.Room.objects.filter(section=room["gender"]).order_by(
            "-exam_capacity", "room_code"
        )
        if candidate.room_code not in taken
    )
    room.update(room_code=big.room_code, room_capacity=big.exam_capacity)
    # A row saved without a floor, for a room the inventory knows: filling it in
    # on Check, Save or Build would change the saved bytes.
    room.pop("floor")
    run.result_json = json.dumps(data, ensure_ascii=False)
    run.save(update_fields=["result_json"])
    source = {**built, "schedule": data["schedule"]}
    saved = _save(client_, source, exam_locks=locks)
    expected = json.dumps(entry["rooms"], ensure_ascii=False)
    kept = next(row for row in saved["schedule"] if row["course_code"] == code)
    assert json.dumps(kept["rooms"], ensure_ascii=False) == expected
    for result in (
        _loaded(client_, saved, mode="optimize_loaded"),
        _build(client_, previous_run_id=saved["run_id"]),
    ):
        kept = next(row for row in result["schedule"] if row["course_code"] == code)
        assert json.dumps(kept["rooms"], ensure_ascii=False) == expected


# ── registrations changed since the save (rule 5, H1) ───────────────────────


def test_a_locked_course_whose_registrations_changed_stays_readable_and_is_reported(
    client_, locked
):
    built, saved, locks = locked
    code = sorted(_held(built, locks))[0]
    entry = next(entry for entry in saved["schedule"] if entry["course_code"] == code)
    source_code = entry["source_course_code"]
    registered = set(
        StudentTermSection.objects.filter(term_section__course_key=source_code).values_list(
            "student_id", flat=True
        )
    )
    newcomer = next(
        student
        for student in Student.objects.filter(program__in=SCOPE["programs"]).order_by("student_id")
        if student.student_id not in registered
    )
    StudentTermSection.objects.create(
        student_id=newcomer.student_id,
        academic_year="1448",
        term="1",
        term_section=TermSection.objects.get(
            course_key=source_code, section=f"{newcomer.section}1"
        ),
        source="scraper_timetable",
    )
    again = _save(client_, saved)
    _assert_kept(built, again, locks)
    issues = [
        issue
        for issue in again["qa"]["exam_locks"]["issues"]
        if issue["kind"] == "registrations_changed"
    ]
    assert [(issue["course_code"], issue["change"]) for issue in issues] == [(code, "grew")]
    assert again["primary_status"] in {"requires_room_action", "contains_overflow"}
    assert "room_action_required" in again["status_flags"]
    # The Student lists read the new run, and say the group changed.
    model = build_roster_model(ExamTimetableRun.objects.get(pk=again["run_id"]))
    verdicts = {group.membership for group in model.groups if group.exam == code}
    assert CHANGED in verdicts
    # A Build from it keeps the saved rows too, and reads the same way.
    rebuilt = _build(client_, previous_run_id=again["run_id"])
    _assert_kept(built, rebuilt, locks)
    model = build_roster_model(ExamTimetableRun.objects.get(pk=rebuilt["run_id"]))
    assert CHANGED in {group.membership for group in model.groups if group.exam == code}


# ── jobs and Copy ────────────────────────────────────────────────────────────


def test_a_build_job_settles_the_inherited_locks_when_it_is_submitted(client_, locked, settings):
    settings.EXAM_JOBS_ENABLED = True
    settings.EXAM_JOBS_RUN_INLINE = True
    built, saved, locks = locked
    submitted = _post(
        client_,
        {
            "label": "Locks",
            "days": DAYS,
            "periods": PERIODS,
            "max_per_day": 2,
            **SCOPE,
            "assign_rooms": True,
            "pinned": [],
            "previous_run_id": saved["run_id"],
        },
        status=202,
        HTTP_X_EXAM_JOBS="1",
    )
    job_id = submitted["job"]["id"]
    response = client_.get(reverse("exam_timetable_job_result", args=[job_id]))
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["exam_locks"] == locks
    _assert_kept(built, body, locks)
    assert ExamTimetableJob.objects.filter(pk=job_id).exists()


def test_a_build_job_whose_saved_run_is_deleted_while_it_waits_is_refused(
    client_, locked, settings, monkeypatch
):
    """The inherited locks were written into the job when it was submitted: a
    run deleted before the job starts refuses the Build, never drops the locks."""
    settings.EXAM_JOBS_ENABLED = True
    settings.EXAM_JOBS_RUN_INLINE = True
    built, saved, locks = locked
    monkeypatch.setattr(exam_jobs, "_dispatch", lambda job_id: None)  # it waits
    submitted = _post(
        client_,
        {
            "label": "Locks",
            "days": DAYS,
            "periods": PERIODS,
            "max_per_day": 2,
            **SCOPE,
            "assign_rooms": True,
            "pinned": [],
            "previous_run_id": saved["run_id"],
        },
        status=202,
        HTTP_X_EXAM_JOBS="1",
    )
    job_id = submitted["job"]["id"]
    stored = ExamTimetableJob.objects.get(pk=job_id).request_payload
    assert stored["exam_locks"] == locks
    assert stored["linked_exams"] == []
    ExamTimetableRun.objects.filter(pk=saved["run_id"]).delete()
    exam_jobs.run_job(job_id, threaded=False)
    response = client_.get(reverse("exam_timetable_job_result", args=[job_id]))
    assert response.status_code == 400, response.content
    assert response.json()["code"] == "exam_locks_source_required"


def test_a_build_job_without_locks_is_stored_as_sent(client_, population, settings, monkeypatch):
    settings.EXAM_JOBS_ENABLED = True
    settings.EXAM_JOBS_RUN_INLINE = True
    built = _build(client_)
    monkeypatch.setattr(exam_jobs, "_dispatch", lambda job_id: None)
    payload = {
        "label": "Locks",
        "days": DAYS,
        "periods": PERIODS,
        "max_per_day": 2,
        **SCOPE,
        "assign_rooms": True,
        "pinned": [],
        "previous_run_id": built["run_id"],
    }
    submitted = _post(client_, payload, status=202, HTTP_X_EXAM_JOBS="1")
    stored = ExamTimetableJob.objects.get(pk=submitted["job"]["id"]).request_payload
    assert stored == payload


def test_a_loaded_job_keeps_the_locks(client_, locked, settings):
    settings.EXAM_JOBS_ENABLED = True
    settings.EXAM_JOBS_RUN_INLINE = True
    built, saved, locks = locked
    submitted = _post(
        client_,
        {
            "label": "Locks",
            "days": DAYS,
            "periods": PERIODS,
            "max_per_day": 2,
            **SCOPE,
            "mode": "optimize_loaded",
            "previous_run_id": saved["run_id"],
            "base_schedule": saved["schedule"],
            "pinned": [],
        },
        status=202,
        HTTP_X_EXAM_JOBS="1",
    )
    response = client_.get(reverse("exam_timetable_job_result", args=[submitted["job"]["id"]]))
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["exam_locks"] == locks
    _assert_kept(built, body, locks)


def test_a_copy_keeps_its_locks(client_, locked):
    built, saved, locks = locked
    response = client_.post(
        reverse("exam_timetable_copy", args=[saved["run_id"]]), {}, content_type="application/json"
    )
    assert response.status_code == 201, response.content
    copied = response.json()
    assert copied["exam_locks"] == locks
    checked = _loaded(client_, copied)
    assert checked["exam_locks"] == locks
    _assert_kept(built, checked, locks)


# ── a problem inside a locked cell (rule 5) ─────────────────────────────────


def test_a_clash_inside_a_locked_cell_is_reported_never_moved(client_, population):
    built = _build(client_)
    board, dragged = _unlocked_clash(built, [])
    clashing = _save(client_, built, board)
    where = {entry["course_code"]: entry for entry in clashing["schedule"]}
    cell = {"day": where[dragged]["day"], "period": where[dragged]["period"]}
    saved = _save(client_, clashing, exam_locks=[cell])
    report = saved["qa"]["exam_locks"]
    clashes = [issue for issue in report["issues"] if issue["kind"] == "clash"]
    assert clashes and all(
        (issue["day"], issue["period"]) == (cell["day"], cell["period"]) for issue in clashes
    )
    assert all(issue["student_count"] >= 1 for issue in clashes)
    locked_rows = [row for row in saved["qa"]["manual_override_details"] if row.get("locked")]
    assert locked_rows and all(
        dragged in row["courses"] for row in locked_rows if row["kind"] == "same_slot"
    )
    fixed = _loaded(client_, saved, mode="minimum_change_repair")
    report = fixed["minimum_change"]
    assert report["locked_violations"] >= 1
    assert all(move["course_code"] not in _held(saved, [cell]) for move in report["moves"])
    if fixed.get("saved") is not False:
        _assert_kept(saved, fixed, [cell])


# ── closed cells reach every solver ─────────────────────────────────────────


def test_an_empty_locked_day_stays_empty_through_every_action(client_, population):
    """Two new days are empty, so a Build that could use them would. One of
    them is locked: Build, Optimise and Fix leave it empty."""
    wider = [*DAYS, "Thu", "Fri"]
    built = _build(client_)
    # As the page sends it after adding days: OVERFLOW exams are renumbered.
    board = [
        {key: value for key, value in entry.items() if key != "slot_index"}
        if entry["day"] == "OVERFLOW"
        else entry
        for entry in built["schedule"]
    ]
    widened = _save(client_, built, board, days=wider)
    locks = [{"day": "Fri"}]
    locked = _save(client_, widened, days=wider, exam_locks=locks)
    assert locked["exam_locks"] == locks

    def on_friday(result):
        return sorted(entry["course_code"] for entry in result["schedule"] if entry["day"] == "Fri")

    rebuilt = _build(client_, days=wider, previous_run_id=locked["run_id"])
    assert on_friday(rebuilt) == []
    assert any(entry["day"] == "Thu" for entry in rebuilt["schedule"]), "the open new day is used"
    assert on_friday(_loaded(client_, locked, mode="optimize_loaded", days=wider)) == []
    unlocked = _build(client_, days=wider, previous_run_id=locked["run_id"], exam_locks=[])
    assert on_friday(unlocked), "without the lock the Build uses Friday"


@pytest.mark.parametrize(
    "mode, target",
    [
        ("optimize_loaded", "core.exam_views.schedule_linked"),
        ("minimum_change_repair", "core.exam_views.repair_minimum_change"),
        (None, "core.services.exam_timetable.schedule_linked"),
    ],
    ids=["optimise", "fix", "build"],
)
def test_every_solver_is_handed_the_locked_slots(client_, locked, monkeypatch, mode, target):
    built, saved, locks = locked
    module, name = target.rsplit(".", 1)
    real = getattr(__import__(module, fromlist=[name]), name)
    seen: list = []

    def spy(*args, **kwargs):
        seen.append(kwargs.get("closed_slots"))
        return real(*args, **kwargs)

    monkeypatch.setattr(target, spy)
    slots = [(day, period) for day in DAYS for period in PERIODS]
    expected = frozenset(index for index, cell in enumerate(slots) if cell in _locked_cells(locks))
    if mode is None:
        _build(client_, previous_run_id=saved["run_id"])
    elif mode == "minimum_change_repair":
        board, _dragged = _unlocked_clash(saved, locks)
        _loaded(client_, saved, board, mode=mode)
    else:
        _loaded(client_, saved, mode=mode)
    assert seen and seen[0] == expected


# ── the defence behind every post-pass ──────────────────────────────────────


def _lock_breaking_pass(held_code):
    """A post-pass gone wrong: it moves a locked exam to another cell."""

    def breaking(entries, *_args, **_kwargs):
        moving = next(entry for entry in entries if entry["course_code"] == held_code)
        target = next(
            (day, period)
            for day in DAYS
            for period in PERIODS
            if (day, period) != (moving["day"], moving["period"]) and day != moving["day"]
        )
        moving.update(
            day=target[0],
            period=target[1],
            slot_index=DAYS.index(target[0]) * len(PERIODS) + PERIODS.index(target[1]),
        )
        return 1

    return breaking


@pytest.mark.parametrize("where", ["build", "optimise"])
def test_a_post_pass_that_breaks_a_lock_is_a_server_error_that_saves_nothing(
    client_, locked, monkeypatch, where
):
    built, saved, locks = locked
    code = sorted(_held(built, locks))[0]
    runs = ExamTimetableRun.objects.count()
    if where == "build":
        monkeypatch.setattr(
            "core.services.exam_timetable._rebalance_invigilators_pass", _lock_breaking_pass(code)
        )
        body = _build(client_, status=500, previous_run_id=saved["run_id"])
    else:
        monkeypatch.setattr(
            "core.services.exam_evaluation._rebalance_invigilators_pass", _lock_breaking_pass(code)
        )
        body = _loaded(client_, saved, mode="optimize_loaded", status=500)
    assert body["ok"] is False
    assert ExamTimetableRun.objects.count() == runs

"""Adding courses to a saved exam timetable, end to end through the HTTP views.

The committee builds from part of the courses, perfects the board, then adds
the rest to THAT board: every existing exam stays where it is unless moving it
is the only way to seat a new one, locked cells and their rooms never change,
and the result is a new run beside the untouched source. Refusals name their
code, field and courses, so the page can say which course and why.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core import exam_views, models
from core.models import (
    ExamTimetableJob,
    ExamTimetableRun,
    ProgrammeRequirement,
    Student,
    StudentTermSection,
    TermSection,
)
from core.services import exam_add_courses as adding
from core.services import exam_room_allocation
from core.services.exam_run_schema import load_normalised_run
from core.services.exam_timetable import build_credit_map, build_enrolled_sets_with_meta
from core.services.rbac import ROLE_EXAM_COMMITTEE
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
    client.force_login(django_user_model.objects.create_superuser(username="add-admin"))
    return client


def _post(client, payload, status=200, url="exam_timetable_build", **headers):
    response = client.post(reverse(url), payload, content_type="application/json", **headers)
    assert response.status_code == status, response.content
    return response.json()


def _live(**scope):
    _, metadata = build_enrolled_sets_with_meta(**(scope or SCOPE))
    return metadata


def _build(client, codes=None, *, scope=None, **changes):
    metadata = _live(**(scope or SCOPE))
    chosen = sorted(metadata) if codes is None else list(codes)
    return _post(
        client,
        {
            "label": "Part of the courses",
            "days": DAYS,
            "periods": PERIODS,
            "max_per_day": 2,
            **(scope or SCOPE),
            "assign_rooms": True,
            "selected_courses": chosen,
            "selected_course_entries": [{"course_code": c, **metadata[c]} for c in chosen],
            "pinned": [],
            **changes,
        },
    )


def _board(run):
    return [
        {
            key: entry[key]
            for key in (
                "course_code",
                "source_course_code",
                "course_name",
                "course_identity",
                "day",
                "period",
                "slot_index",
            )
        }
        for entry in run["schedule"]
    ]


def _add_payload(run, identities, *, board=None, **changes):
    board = _board(run) if board is None else board
    return {
        "label": "With the rest",
        "days": list(dict.fromkeys(slot["day"] for slot in run["slots"])),
        "periods": list(dict.fromkeys(slot["period"] for slot in run["slots"])),
        "max_per_day": run["qa"].get("max_per_day", 2),
        **SCOPE,
        "mode": "add_courses",
        "previous_run_id": run["run_id"],
        "base_schedule": board,
        "selected_courses": [entry["course_code"] for entry in board],
        "selected_course_entries": [
            {key: entry[key] for key in ("course_code", "course_name", "course_identity")}
            for entry in board
        ],
        "pinned": run.get("pinned", []),
        "linked_exams": run.get("linked_exams", []),
        "editor_revision": 3,
        "added_courses": [{"course_identity": identity} for identity in identities],
        **changes,
    }


def _add(client, run, identities, *, status=200, board=None, **changes):
    return _post(client, _add_payload(run, identities, board=board, **changes), status=status)


def _check(client, run):
    """A fixed-time Check of a saved run's own board, as the page runs it."""
    return _post(
        client,
        {
            "days": list(dict.fromkeys(slot["day"] for slot in run["slots"])),
            "periods": list(dict.fromkeys(slot["period"] for slot in run["slots"])),
            "max_per_day": run["qa"].get("max_per_day", 2),
            **SCOPE,
            "previous_run_id": run["run_id"],
            "base_schedule": _board(run),
            "pinned": run["pinned"],
            "linked_exams": run.get("linked_exams", []),
        },
        url="exam_timetable_draft_impact",
    )


def _sha(run_id):
    return hashlib.sha256(
        ExamTimetableRun.objects.get(pk=run_id).result_json.encode("utf-8")
    ).hexdigest()


def _identities(codes):
    metadata = _live()
    return [metadata[code]["course_identity"] for code in codes]


def _stable(value):
    if isinstance(value, dict):
        return {
            key: _stable(item)
            for key, item in sorted(value.items())
            if key not in {"snapshot_timestamp", "created_at", "generated_at"}
        }
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


def _placements(run):
    return {entry["course_code"]: (entry["day"], entry["period"]) for entry in run["schedule"]}


# ── the owner's workflow ─────────────────────────────────────────────────────


def test_the_rest_of_the_courses_are_added_to_the_saved_board_as_a_new_run(population, client_):
    codes = sorted(_live())
    part, rest = codes[:6], codes[6:]
    built = _build(client_, part)
    before = _sha(built["run_id"])
    runs = ExamTimetableRun.objects.count()

    added = _add(client_, built, _identities(rest))

    assert ExamTimetableRun.objects.count() == runs + 1
    assert added["run_id"] != built["run_id"]
    # The source run is never written.
    assert _sha(built["run_id"]) == before
    assert sorted(added["courses"]) == codes
    report = added["add_courses"]
    assert "add_courses" not in added["qa"]
    assert report["source_run_id"] == built["run_id"]
    assert report["requested_count"] == len(rest)
    assert report["placed_count"] + len(report["not_placed"]) == len(rest)
    assert added["rebuild_mode"] == adding.REBUILD_MODE
    # Nothing that was placed moved unless the report says so.
    moved = {move["course_code"] for move in report["moves"]}
    for code, place in _placements(built).items():
        if code not in moved:
            assert _placements(added)[code] == place, code
    assert report["violations_after"] == report["untouched_violations"]
    # Persisted with the run: a reload from history still says what happened.
    saved = load_normalised_run(ExamTimetableRun.objects.get(pk=added["run_id"]))
    assert saved["add_courses"] == report
    assert added["editor_revision"] == 3


def test_the_new_run_reads_as_unchanged_to_a_check_of_its_own_board(population, client_):
    codes = sorted(_live())
    built = _build(client_, codes[:7])
    added = _add(client_, built, _identities(codes[7:]))
    checked = _check(client_, added)
    # The page compares these (reportSignature); a mismatch would mark the new
    # run as unsaved the moment it opened, and gate its export.
    assert _stable(checked["qa"]) == _stable(added["qa"])
    assert checked["section_enrollment"] == added["section_enrollment"]
    assert [(e["course_code"], e["day"], e["period"], e["rooms"]) for e in checked["schedule"]] == [
        (e["course_code"], e["day"], e["period"], e["rooms"]) for e in added["schedule"]
    ]
    assert checked["input_fingerprint"] == added["input_fingerprint"]
    # The added courses are part of what the fingerprint covers.
    assert added["input_fingerprint"] != built["input_fingerprint"]


def test_with_room_on_the_board_no_existing_exam_moves_and_that_is_proven(population, client_):
    codes = sorted(_live())
    built = _build(client_, codes[:5])
    added = _add(client_, built, _identities(codes[5:7]))
    report = added["add_courses"]
    assert report["moves"] == []
    assert report["not_placed"] == []
    assert report["proven_minimal"] is True and report["status"] == "OPTIMAL"
    for entry in report["added"]:
        assert entry["placed"] is not None
        assert _placements(added)[entry["course_code"]] == (
            entry["placed"]["day"],
            entry["placed"]["period"],
        )


def test_boards_nothing_changed_keep_their_saved_rooms_byte_for_byte(population, client_):
    codes = sorted(_live())
    built = _build(client_, codes[:6])
    # A room added since the save: a board allocated afresh could take it.
    models.Room.objects.create(
        room_code="RF-NEW", **corpus.room_seats(models, 60), section="F", building="B9", floor=1
    )
    models.Room.objects.create(
        room_code="RM-NEW", **corpus.room_seats(models, 60), section="M", building="B9", floor=1
    )
    exam_room_allocation._CACHE.clear()
    added = _add(client_, built, _identities(codes[6:7]))
    changed = {
        (row["day"], row["period"], row["gender"]) for row in added["add_courses"]["rooms_changed"]
    }
    new_code = added["add_courses"]["added"][0]["course_code"]
    new_at = _placements(added)[new_code]

    def packs(run):
        out = {}
        for entry in run["schedule"]:
            for room in entry["rooms"]:
                key = (entry["day"], entry["period"], room["gender"])
                out.setdefault(key, {}).setdefault(entry["course_code"], []).append(room)
        return {key: json.dumps(value, ensure_ascii=False) for key, value in out.items()}

    before, after = packs(built), packs(added)
    kept = [key for key in before if key not in changed]
    assert kept, "nothing was left to keep"
    for key in kept:
        assert after[key] == before[key], key
    # Only the new exam's own boards were allocated again.
    assert {(day, period) for day, period, _ in changed} <= {new_at}
    assert not any(
        room["room_code"] in {"RF-NEW", "RM-NEW"}
        for entry in added["schedule"]
        if entry["course_code"] != new_code and (entry["day"], entry["period"]) != new_at
        for room in entry["rooms"]
    )


def test_boards_nothing_changed_keep_the_rooms_they_were_saved_with(population, client_):
    """Kept, not merely allocated again the same way.

    The saved run seats every women's group in a room of its own that no
    allocator would choose today (99 seats). A board the Add did not touch
    must still hold exactly those rooms.
    """
    codes = sorted(_live())
    built = _build(client_, codes[:6])
    run = ExamTimetableRun.objects.get(pk=built["run_id"])
    stored = json.loads(run.result_json)
    number = 0
    for entry in stored["schedule"]:
        for room in entry["rooms"]:
            if room["gender"] == "F" and room["room_code"] != "UNASSIGNED":
                number += 1
                room["room_code"] = f"RFZ-{number}"
                models.Room.objects.create(
                    room_code=room["room_code"],
                    **corpus.room_seats(models, 99),
                    section="F",
                    building="BZ",
                    floor=1,
                )
    assert number
    run.result_json = json.dumps(stored, ensure_ascii=False)
    run.save(update_fields=["result_json"])
    exam_room_allocation._CACHE.clear()
    added = _add(client_, built, _identities(codes[6:7]))
    changed = {
        (row["day"], row["period"], row["gender"]) for row in added["add_courses"]["rooms_changed"]
    }
    saved_rows = {
        (entry["day"], entry["period"], entry["course_code"]): [
            room for room in entry["rooms"] if room["gender"] == "F"
        ]
        for entry in stored["schedule"]
    }
    kept = 0
    for entry in added["schedule"]:
        key = (entry["day"], entry["period"], "F")
        if key in changed or entry["day"] == "OVERFLOW":
            continue
        rows = [room for room in entry["rooms"] if room["gender"] == "F"]
        if not rows:
            continue
        assert rows == saved_rows[(entry["day"], entry["period"], entry["course_code"])]
        assert all(room["room_code"].startswith("RFZ-") for room in rows)
        kept += 1
    assert kept


def test_a_full_board_leaves_what_cannot_fit_in_overflow_with_a_reason(population, client_):
    codes = sorted(_live())
    # Two days of two periods: far too few slots for every course.
    days = DAYS[:2]
    built = _build(client_, codes[:3], days=days)
    pin = built["schedule"][0]
    pinned = [{"course_code": pin["course_code"], "day": pin["day"], "period": pin["period"]}]
    checked = _post(
        client_,
        {
            "days": days,
            "periods": PERIODS,
            "max_per_day": 2,
            **SCOPE,
            "previous_run_id": built["run_id"],
            "base_schedule": _board(built),
            "pinned": pinned,
        },
        url="exam_timetable_draft_impact",
    )
    saved = _post(
        client_,
        {
            "label": "Pinned",
            "days": days,
            "periods": PERIODS,
            "max_per_day": 2,
            **SCOPE,
            "mode": "save_loaded_changes",
            "previous_run_id": built["run_id"],
            "base_schedule": _board(built),
            "pinned": pinned,
            "expected_input_fingerprint": checked["input_fingerprint"],
        },
    )
    added = _add(client_, saved, _identities(codes[3:]))
    report = added["add_courses"]
    assert _placements(added)[pin["course_code"]] == (pin["day"], pin["period"])
    assert pin["course_code"] not in {move["course_code"] for move in report["moves"]}
    for move in report["moves"]:
        assert move["from"] == {
            "day": _placements(saved)[move["course_code"]][0],
            "period": _placements(saved)[move["course_code"]][1],
        }
    for row in report["not_placed"]:
        assert row["reason"] in {
            "bucket_days_full",
            "blocked_by_fixed_exams",
            "blocked_by_clashes",
            "only_locked_periods_free",
            "search_limit",
        }
        entry = next(e for e in added["schedule"] if e["course_code"] == row["course_code"])
        assert entry["day"] == "OVERFLOW"
    # Overflow numbering stays above every slot.
    overflow = [e["slot_index"] for e in added["schedule"] if e["day"] == "OVERFLOW"]
    assert all(index >= len(added["slots"]) for index in overflow)
    assert len(set(overflow)) == len(overflow)


MOVE_SCOPE = {"programs": ["MV"], "sections": ["M"]}
MOVE_DAYS = ["Sun", "Mon"]


@pytest.fixture
def four_exams_one_newcomer():
    """Four exams in four slots and a fifth course clashing with each of them.

    Student i sits MV10i and MV200, so MV200 fits nowhere while the four sit
    apart. The four share no student with one another: any one of them can
    join another's slot, and that single move frees a slot for MV200.
    """
    for code in ["MV101", "MV102", "MV103", "MV104", "MV200"]:
        models.Course.objects.create(course_code=code, description=code, credit_hours=3)
    sections = {
        code: TermSection.objects.create(
            id=98000 + number,
            course_key=code,
            course_code=code,
            course_number="",
            course_name=code,
            section="M1",
            source_tag="scraper_timetable",
        )
        for number, code in enumerate(["MV101", "MV102", "MV103", "MV104", "MV200"])
    }
    for number in range(1, 5):
        student = Student.objects.create(student_id=880000 + number, program="MV", section="M")
        for code in (f"MV10{number}", "MV200"):
            StudentTermSection.objects.create(
                student_id=student.student_id,
                academic_year="1448",
                term="1",
                term_section=sections[code],
                source="scraper_timetable",
            )
    for size in (10, 20):
        models.Room.objects.create(
            room_code=f"MV-R{size}",
            **corpus.room_seats(models, size),
            section="M",
            building="B1",
            floor=1,
        )


def _save_with_pins(client, built, codes):
    pinned = [
        {"course_code": entry["course_code"], "day": entry["day"], "period": entry["period"]}
        for entry in built["schedule"]
        if entry["course_code"] in codes
    ]
    common = {
        "days": MOVE_DAYS,
        "periods": PERIODS,
        "max_per_day": 2,
        **MOVE_SCOPE,
        "previous_run_id": built["run_id"],
        "base_schedule": _board(built),
        "pinned": pinned,
    }
    checked = _post(client, common, url="exam_timetable_draft_impact")
    return _post(
        client,
        {
            **common,
            "label": "Pinned",
            "mode": "save_loaded_changes",
            "expected_input_fingerprint": checked["input_fingerprint"],
        },
    )


def _add_newcomer(client, run):
    identity = _live(**MOVE_SCOPE)["MV200"]["course_identity"]
    return _add(client, run, [identity], **MOVE_SCOPE)


def test_one_existing_exam_steps_aside_and_that_is_proven_the_fewest(
    four_exams_one_newcomer, client_
):
    built = _build(client_, ["MV101", "MV102", "MV103", "MV104"], scope=MOVE_SCOPE, days=MOVE_DAYS)
    assert len({(e["day"], e["period"]) for e in built["schedule"]}) == 4
    added = _add_newcomer(client_, built)
    report = added["add_courses"]
    assert report["not_placed"] == []
    assert len(report["moves"]) == 1
    move = report["moves"][0]
    newcomer = report["added"][0]["placed"]
    # It left the very slot the newcomer took, and says for whom.
    assert move["from"] == newcomer
    assert move["made_room_for"] == ["MV200"]
    assert report["proven_minimal"] is True and report["status"] == "OPTIMAL"
    assert report["widened"] is True and report["moved_weight"] == 1
    others = {code for code in ["MV101", "MV102", "MV103", "MV104"]} - {move["course_code"]}
    for code in others:
        assert _placements(added)[code] == _placements(built)[code]


def test_pinned_exams_never_step_aside_whatever_their_names(four_exams_one_newcomer, client_):
    built = _build(client_, ["MV101", "MV102", "MV103", "MV104"], scope=MOVE_SCOPE, days=MOVE_DAYS)
    # The one left free sorts last: protection that held only by code order
    # would pass with a first-named exam.
    saved = _save_with_pins(client_, built, {"MV101", "MV102", "MV103"})
    added = _add_newcomer(client_, saved)
    assert [move["course_code"] for move in added["add_courses"]["moves"]] == ["MV104"]
    for code in ["MV101", "MV102", "MV103"]:
        assert _placements(added)[code] == _placements(saved)[code]


def test_with_every_blocker_pinned_the_newcomer_waits_in_overflow_and_says_why(
    four_exams_one_newcomer, client_
):
    built = _build(client_, ["MV101", "MV102", "MV103", "MV104"], scope=MOVE_SCOPE, days=MOVE_DAYS)
    saved = _save_with_pins(client_, built, {"MV101", "MV102", "MV103", "MV104"})
    added = _add_newcomer(client_, saved)
    report = added["add_courses"]
    assert report["moves"] == []
    assert report["placed_count"] == 0
    (row,) = report["not_placed"]
    assert row["course_code"] == "MV200"
    assert row["reason"] == "blocked_by_fixed_exams"
    assert row["blocked_by"]["fixed"] == ["MV101", "MV102", "MV103", "MV104"]
    entry = next(e for e in added["schedule"] if e["course_code"] == "MV200")
    assert entry["day"] == "OVERFLOW" and entry["slot_index"] >= len(added["slots"])
    # Saved all the same: adding the course was the committee's decision.
    assert added["run_id"]


@pytest.fixture
def five_exams_one_newcomer(four_exams_one_newcomer):
    """The four, a fifth that also clashes with the newcomer, and two more clashes.

    MV101 and MV102 share a student, and so do MV102 and MV103.
    """
    section = TermSection.objects.create(
        id=98010,
        course_key="MV105",
        course_code="MV105",
        course_number="",
        course_name="MV105",
        section="M1",
        source_tag="scraper_timetable",
    )
    models.Course.objects.create(course_code="MV105", description="MV105", credit_hours=3)
    by_code = {
        row.course_code: row for row in TermSection.objects.filter(course_code__startswith="MV")
    }
    for student_id, codes in [
        (880005, ["MV105", "MV200"]),
        (880010, ["MV101", "MV102"]),
        (880011, ["MV102", "MV103"]),
    ]:
        Student.objects.create(student_id=student_id, program="MV", section="M")
        for code in codes:
            StudentTermSection.objects.create(
                student_id=student_id,
                academic_year="1448",
                term="1",
                term_section=section if code == "MV105" else by_code[code],
                source="scraper_timetable",
            )


FIVE = ["MV101", "MV102", "MV103", "MV104", "MV105"]


def _save_board(client, built, places, *, pinned=(), links=()):
    """Check, then Save, the board ``places`` gives, with pins and links."""
    slot_of = {(slot["day"], slot["period"]): slot["index"] for slot in built["slots"]}
    board = []
    for entry in _board(built):
        day, period = places[entry["course_code"]]
        board.append({**entry, "day": day, "period": period, "slot_index": slot_of[(day, period)]})
    identity = {entry["course_code"]: entry["course_identity"] for entry in board}
    common = {
        "days": MOVE_DAYS,
        "periods": PERIODS,
        "max_per_day": 2,
        **MOVE_SCOPE,
        "previous_run_id": built["run_id"],
        "base_schedule": board,
        "pinned": [
            {"course_code": code, "day": places[code][0], "period": places[code][1]}
            for code in pinned
        ],
        "linked_exams": [
            {"members": [{"course_identity": identity[code]} for code in link]} for link in links
        ],
    }
    checked = _post(client, common, url="exam_timetable_draft_impact")
    return _post(
        client,
        {
            **common,
            "label": "Saved board",
            "mode": "save_loaded_changes",
            "expected_input_fingerprint": checked["input_fingerprint"],
        },
    )


def _five_places(first_cell_holds):
    cells = [(day, period) for day in MOVE_DAYS for period in PERIODS]
    places = dict.fromkeys(first_cell_holds, cells[0])
    rest = [code for code in FIVE if code not in first_cell_holds]
    places.update(zip(rest, cells[1:], strict=True))
    return places


def test_a_rule_break_the_saved_board_has_is_left_alone_not_fixed(five_exams_one_newcomer, client_):
    built = _build(client_, FIVE, scope=MOVE_SCOPE, days=MOVE_DAYS)
    places = _five_places(["MV101", "MV102"])
    saved = _save_board(client_, built, places, pinned=["MV103", "MV104", "MV105"])
    added = _add_newcomer(client_, saved)
    report = added["add_courses"]
    # Freeing a slot would mean moving both exams of the saved clash: "fixing"
    # it. That is not what was asked, so the newcomer waits in OVERFLOW.
    assert report["untouched_violations"] == 1 and report["violations_after"] == 1
    assert report["moves"] == []
    assert [row["reason"] for row in report["not_placed"]] == ["blocked_by_fixed_exams"]
    for code in FIVE:
        assert _placements(added)[code] == places[code]


def test_a_linked_exam_steps_aside_whole(five_exams_one_newcomer, client_):
    built = _build(client_, FIVE, scope=MOVE_SCOPE, days=MOVE_DAYS)
    places = _five_places(["MV101", "MV102"])
    saved = _save_board(
        client_, built, places, pinned=["MV103", "MV104", "MV105"], links=[["MV101", "MV102"]]
    )
    added = _add_newcomer(client_, saved)
    report = added["add_courses"]
    assert report["not_placed"] == []
    assert sorted(move["course_code"] for move in report["moves"]) == ["MV101", "MV102"]
    at = _placements(added)
    assert at["MV101"] == at["MV102"] != places["MV101"]
    assert at["MV200"] == places["MV101"]
    assert report["moved_weight"] == 2 and report["proven_minimal"] is True


def test_exams_an_earlier_fix_protected_stay_protected_and_are_carried(
    four_exams_one_newcomer, client_
):
    built = _build(client_, ["MV101", "MV102", "MV103", "MV104"], scope=MOVE_SCOPE, days=MOVE_DAYS)
    run = ExamTimetableRun.objects.get(pk=built["run_id"])
    stored = json.loads(run.result_json)
    stored["minimum_change_protected"] = ["MV101", "MV102", "MV103"]
    run.result_json = json.dumps(stored, ensure_ascii=False)
    run.save(update_fields=["result_json"])
    added = _add_newcomer(client_, built)
    assert [move["course_code"] for move in added["add_courses"]["moves"]] == ["MV104"]
    assert added["minimum_change_protected"] == ["MV101", "MV102", "MV103"]


def test_locked_cells_keep_their_exams_rooms_and_sections_verbatim(population, client_):
    codes = sorted(_live())
    built = _build(client_, codes[:6])
    busy = next(e["day"] for e in built["schedule"] if e["day"] != "OVERFLOW")
    locks = [{"day": busy}]
    board = _board(built)
    common = {
        "days": DAYS,
        "periods": PERIODS,
        "max_per_day": 2,
        **SCOPE,
        "previous_run_id": built["run_id"],
        "base_schedule": board,
        "pinned": [],
        "exam_locks": locks,
    }
    checked = _post(client_, common, url="exam_timetable_draft_impact")
    locked = _post(
        client_,
        {
            **common,
            "label": "Locked",
            "mode": "save_loaded_changes",
            "expected_input_fingerprint": checked["input_fingerprint"],
        },
    )
    assert locked["exam_locks"] == locks
    added = _add(client_, locked, _identities(codes[6:]))
    assert added["exam_locks"] == locks
    held = [entry for entry in locked["schedule"] if entry["day"] == busy]
    assert held
    after = {entry["course_code"]: entry for entry in added["schedule"]}
    for entry in held:
        code = entry["course_code"]
        assert (after[code]["day"], after[code]["period"]) == (entry["day"], entry["period"])
        assert json.dumps(after[code]["rooms"], ensure_ascii=False) == json.dumps(
            entry["rooms"], ensure_ascii=False
        )
        assert json.dumps(added["section_enrollment"][code], ensure_ascii=False) == json.dumps(
            locked["section_enrollment"][code], ensure_ascii=False
        )
        assert json.dumps(
            added["operations_snapshot"]["courses"][code]["sections"], ensure_ascii=False
        ) == json.dumps(
            locked["operations_snapshot"]["courses"][code]["sections"], ensure_ascii=False
        )
    new_codes = {row["course_code"] for row in added["add_courses"]["added"]}
    assert not [e for e in added["schedule"] if e["course_code"] in new_codes and e["day"] == busy]
    assert added["add_courses"]["locked_count"] == len(held)


def test_linked_exams_are_kept_whole_and_saved_with_the_new_run(population, client_):
    codes = sorted(_live())
    built = _build(client_, codes[:6])
    first, second = built["schedule"][0], built["schedule"][1]
    board = _board(built)
    for entry in board:
        if entry["course_code"] == second["course_code"]:
            entry.update(day=first["day"], period=first["period"], slot_index=first["slot_index"])
    links = [
        {
            "members": [
                {"course_identity": first["course_identity"]},
                {"course_identity": second["course_identity"]},
            ]
        }
    ]
    common = {
        "days": DAYS,
        "periods": PERIODS,
        "max_per_day": 2,
        **SCOPE,
        "previous_run_id": built["run_id"],
        "base_schedule": board,
        "pinned": [],
        "linked_exams": links,
    }
    checked = _post(client_, common, url="exam_timetable_draft_impact")
    linked = _post(
        client_,
        {
            **common,
            "label": "Linked",
            "mode": "save_loaded_changes",
            "expected_input_fingerprint": checked["input_fingerprint"],
        },
    )
    added = _add(client_, linked, _identities(codes[6:]))
    at = _placements(added)
    assert at[first["course_code"]] == at[second["course_code"]]
    assert added["linked_exams"] == linked["linked_exams"]


# ── refusals ─────────────────────────────────────────────────────────────────


@pytest.fixture
def saved_part(population, client_):
    codes = sorted(_live())
    return _build(client_, codes[:6]), codes


@pytest.mark.parametrize(
    ("added", "field"),
    [
        ("not a list", "added_courses"),
        ([["CS"]], "added_courses[0]"),
        ([{"course_code": "LX101"}], "added_courses[0]"),
        ([{"course_identity": "  "}], "added_courses[0]"),
        ([{"course_identity": "x" * 301}], "added_courses[0]"),
        ([{"course_identity": "a"}, {"course_identity": "a"}], "added_courses[1]"),
    ],
)
def test_a_malformed_list_of_courses_is_refused(saved_part, client_, added, field):
    built, _ = saved_part
    payload = _add_payload(built, [])
    payload["added_courses"] = added
    body = _post(client_, payload, status=400)
    assert body["code"] == adding.INVALID and body["field"] == field


def test_an_empty_list_is_refused_and_nothing_is_saved(saved_part, client_):
    built, _ = saved_part
    runs = ExamTimetableRun.objects.count()
    body = _add(client_, built, [], status=400)
    assert body["code"] == adding.NONE
    assert ExamTimetableRun.objects.count() == runs


def _moved(board):
    board = deepcopy(board)
    entry = board[0]
    entry.update(day=DAYS[-1], period=PERIODS[-1])
    return board


@pytest.mark.parametrize(
    ("change", "field"),
    [
        (lambda built, p: p.update(base_schedule=_moved(p["base_schedule"])), "base_schedule"),
        (
            lambda built, p: p.update(
                base_schedule=p["base_schedule"][1:],
                selected_courses=p["selected_courses"][1:],
                selected_course_entries=p["selected_course_entries"][1:],
            ),
            "base_schedule",
        ),
        (
            lambda built, p: p.update(
                pinned=[
                    {
                        "course_code": built["schedule"][0]["course_code"],
                        "day": built["schedule"][0]["day"],
                        "period": built["schedule"][0]["period"],
                    }
                ]
            ),
            "pinned",
        ),
        (lambda built, p: p.update(max_per_day=3), "header"),
        (lambda built, p: p.update(thin_conflict_threshold=4), "header"),
        (lambda built, p: p.update(assign_rooms=False), "header"),
        (
            lambda built, p: p.update(
                exam_locks=[{"day": built["schedule"][0]["day"]}],
            ),
            "exam_locks",
        ),
    ],
)
def test_a_board_with_unsaved_changes_is_refused(saved_part, client_, change, field):
    built, codes = saved_part
    payload = _add_payload(built, _identities(codes[6:7]))
    change(built, payload)
    body = _post(client_, payload, status=400)
    assert body["code"] == adding.UNSAVED_CHANGES and body["field"] == field


def test_a_changed_link_is_an_unsaved_change(saved_part, client_):
    built, codes = saved_part
    first, second = built["schedule"][0], built["schedule"][1]
    board = _board(built)
    for entry in board:
        if entry["course_code"] == second["course_code"]:
            entry.update(day=first["day"], period=first["period"], slot_index=first["slot_index"])
    body = _add(
        client_,
        built,
        _identities(codes[6:7]),
        board=board,
        status=400,
    )
    assert body["code"] == adding.UNSAVED_CHANGES and body["field"] == "base_schedule"
    payload = _add_payload(built, _identities(codes[6:7]))
    payload["linked_exams"] = [
        {
            "members": [
                {"course_identity": first["course_identity"]},
                {"course_identity": second["course_identity"]},
            ]
        }
    ]
    body = _post(client_, payload, status=400)
    assert body["code"] == adding.UNSAVED_CHANGES and body["field"] == "linked_exams"


def test_the_name_is_not_a_change(saved_part, client_):
    built, codes = saved_part
    added = _add(client_, built, _identities(codes[6:7]), label="Another name")
    assert added["ok"] is True


def test_a_course_already_in_the_timetable_is_refused_by_name(saved_part, client_):
    built, codes = saved_part
    body = _add(client_, built, _identities([codes[0]]), status=400)
    assert body["code"] == adding.ALREADY_IN_TIMETABLE
    assert body["courses"] == [codes[0]]
    assert body["field"] == "added_courses[0]"


def test_a_course_of_another_scope_is_refused_as_outside_it(population, client_):
    only_cs = Student.objects.filter(program="CS").order_by("student_id")[:3]
    section = TermSection.objects.create(
        id=99001,
        course_key="LX400",
        course_code="LX400",
        course_number="",
        course_name="LX400",
        section="F1",
        source_tag="scraper_timetable",
    )
    for student in only_cs:
        StudentTermSection.objects.create(
            student_id=student.student_id,
            academic_year="1448",
            term="1",
            term_section=section,
            source="scraper_timetable",
        )
    ProgrammeRequirement.objects.create(
        program="CS", course_code="LX400", course_name="LX400", programme_term=6
    )
    ai = {"programs": ["AI"], "sections": ["F", "M"]}
    built = _build(client_, None, scope=ai)
    identity = _live(programs=None, sections=None)["LX400"]["course_identity"]
    payload = _add_payload(built, [identity])
    payload.update(ai)
    body = _post(client_, payload, status=400)
    assert body["code"] == adding.OUTSIDE_SCOPE
    assert body["courses"] == ["LX400"]


def test_a_course_with_no_registrations_is_refused_as_unavailable(saved_part, client_):
    built, _ = saved_part
    body = _add(client_, built, ["LX999::NOWHERE"], status=400)
    assert body["code"] == adding.UNAVAILABLE and body["courses"] == []


def test_an_existing_course_that_lost_its_registrations_is_the_standard_refusal(
    saved_part, client_
):
    built, codes = saved_part
    gone = built["schedule"][0]["source_course_code"]
    StudentTermSection.objects.filter(term_section__course_code=gone).delete()
    body = _add(client_, built, _identities(codes[6:7]), status=400)
    assert body["code"] == "courses_unavailable"


def test_a_timetable_of_another_term_is_refused(saved_part, client_, monkeypatch):
    built, codes = saved_part
    monkeypatch.setattr(exam_views, "current_exam_term", lambda: ("1449", "2"))
    body = _add(client_, built, _identities(codes[6:7]), status=400)
    assert body["code"] == adding.TERM_CHANGED


def test_the_term_is_read_from_the_section_rows_not_a_stray_schedule_term(saved_part, client_):
    """Every run saved before 2026-09-26 carries a study-plan term in schedule[].term."""
    built, codes = saved_part
    run = ExamTimetableRun.objects.get(pk=built["run_id"])
    stored = json.loads(run.result_json)
    for entry in stored["schedule"]:
        entry["term"] = "5"
    run.result_json = json.dumps(stored, ensure_ascii=False)
    run.save(update_fields=["result_json"])
    added = _add(client_, built, _identities(codes[6:7]))
    assert added["ok"] is True


def test_without_a_saved_timetable_nothing_is_built_or_saved(saved_part, client_):
    built, codes = saved_part
    runs = ExamTimetableRun.objects.count()
    payload = _add_payload(built, _identities(codes[6:7]))
    del payload["base_schedule"]
    body = _post(client_, payload, status=400)
    assert body["code"] == adding.SOURCE_REQUIRED
    payload = _add_payload(built, _identities(codes[6:7]), previous_run_id=987654)
    body = _post(client_, payload, status=400)
    assert body["code"] == adding.SOURCE_REQUIRED
    assert ExamTimetableRun.objects.count() == runs


def test_only_the_exam_committee_and_super_admins_may_add(saved_part, client, django_user_model):
    built, codes = saved_part
    member = django_user_model.objects.create_user(username="committee", password="x")
    member.groups.add(Group.objects.get_or_create(name=ROLE_EXAM_COMMITTEE)[0])
    client.force_login(member)
    added = _add(client, built, _identities(codes[6:7]))
    assert added["ok"] is True
    # The list the page opens first: a committee account may read it.
    listed = client.get(reverse("exam_timetable_scope_courses", args=[built["run_id"]]))
    assert listed.status_code == 200, listed.content
    other = django_user_model.objects.create_user(username="other", password="x")
    client.force_login(other)
    response = client.post(
        reverse("exam_timetable_build"),
        _add_payload(built, _identities(codes[6:7])),
        content_type="application/json",
    )
    assert response.status_code == 403
    response = client.get(reverse("exam_timetable_scope_courses", args=[built["run_id"]]))
    assert response.status_code == 403


# ── as a background job ──────────────────────────────────────────────────────


def test_as_a_job_it_reports_its_stages_and_answers_as_the_synchronous_view(
    saved_part, client_, settings
):
    built, codes = saved_part
    settings.EXAM_JOBS_ENABLED = True
    settings.EXAM_JOBS_RUN_INLINE = True
    submitted = _post(
        client_,
        _add_payload(built, _identities(codes[6:])),
        status=202,
        HTTP_X_EXAM_JOBS="1",
    )
    job = submitted["job"]
    assert job["kind"] == ExamTimetableJob.KIND_ADD
    polled = client_.get(reverse("exam_timetable_job", args=[job["id"]])).json()["job"]
    stages = [stage["key"] for stage in polled["stages"]]
    assert stages == [
        "read_board",
        "place_exams",
        "fewest_moves",
        "check_rules",
        "assign_rooms",
        "save",
    ]
    assert polled["status"] == "succeeded" and polled["has_run"]
    body = client_.get(reverse("exam_timetable_job_result", args=[job["id"]])).json()
    assert body["ok"] is True and body["editor_revision"] == 3
    assert body["add_courses"]["requested_count"] == len(codes) - 6


def test_a_job_without_a_board_is_refused_before_it_is_stored(saved_part, client_, settings):
    built, codes = saved_part
    settings.EXAM_JOBS_ENABLED = True
    settings.EXAM_JOBS_RUN_INLINE = True
    payload = _add_payload(built, _identities(codes[6:7]))
    del payload["base_schedule"]
    body = _post(client_, payload, status=400, HTTP_X_EXAM_JOBS="1")
    assert body["code"] == adding.SOURCE_REQUIRED
    assert not ExamTimetableJob.objects.exists()


# ── the list of courses for a saved run ──────────────────────────────────────


def _scope(client, run_id, status=200, **query):
    response = client.get(reverse("exam_timetable_scope_courses", args=[run_id]), query)
    assert response.status_code == status, response.content
    return response.json()


def test_the_list_holds_every_course_of_the_saved_scope_with_the_runs_own_marked(
    saved_part, client_
):
    built, codes = saved_part
    body = _scope(client_, built["run_id"], programs="CS")
    assert body["scope"] == SCOPE
    listed = {row["course_code"]: row for row in body["courses"]}
    assert sorted(listed) == codes
    for code in codes[:6]:
        row = listed[code]
        assert row["in_timetable"] is True and row["timetable_course_code"] == code
        assert set(row["placement"]) == {"day", "period"}
        assert "add_code" not in row
    for code in codes[6:]:
        assert listed[code]["in_timetable"] is False
        assert listed[code]["add_code"] == code
    assert body["missing"] == [] and body["term_changed"] is False


def test_the_list_marks_an_old_entry_without_an_identity_and_names_missing_courses(
    saved_part, client_
):
    built, codes = saved_part
    run = ExamTimetableRun.objects.get(pk=built["run_id"])
    stored = json.loads(run.result_json)
    stored["schedule"][0].pop("course_identity", None)
    stored["schedule"].append(
        {
            "course_code": "GONE1",
            "source_course_code": "GONE1",
            "course_name": "Gone",
            "course_identity": "GONE1::GONE",
            "day": "OVERFLOW",
            "period": "Extra-99",
            "slot_index": 99,
            "rooms": [],
        }
    )
    run.result_json = json.dumps(stored, ensure_ascii=False)
    run.save(update_fields=["result_json"])
    body = _scope(client_, built["run_id"])
    listed = {row["course_code"]: row for row in body["courses"]}
    assert listed[stored["schedule"][0]["course_code"]]["in_timetable"] is True
    assert body["missing"] == [{"course_code": "GONE1", "course_identity": "GONE1::GONE"}]


def test_the_list_writes_nothing_and_refuses_what_it_cannot_read(saved_part, client_):
    built, _ = saved_part
    before = _sha(built["run_id"])
    with CaptureQueriesContext(connection) as queries:
        _scope(client_, built["run_id"])
    writes = [
        query["sql"]
        for query in queries.captured_queries
        if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        and "django_session" not in query["sql"]
    ]
    assert writes == []
    assert _sha(built["run_id"]) == before
    assert _scope(client_, 987654, status=404)["code"] == "run_not_found"


def test_the_list_says_when_the_timetable_belongs_to_another_term(saved_part, client_, monkeypatch):
    built, _ = saved_part
    monkeypatch.setattr(exam_views, "current_exam_term", lambda: ("1449", "2"))
    assert _scope(client_, built["run_id"])["term_changed"] is True


def test_an_old_run_with_an_empty_scope_lists_every_course_of_the_term(population, client_):
    codes = sorted(_live())
    built = _build(client_, codes[:6])
    run = ExamTimetableRun.objects.get(pk=built["run_id"])
    stored = json.loads(run.result_json)
    stored["enrollment_scope"] = {"programs": [], "sections": []}
    run.result_json = json.dumps(stored, ensure_ascii=False)
    run.save(update_fields=["result_json"])
    body = _scope(client_, built["run_id"])
    assert sorted(row["course_code"] for row in body["courses"]) == sorted(
        _live(programs=None, sections=None)
    )


def test_load_courses_lists_exactly_what_it_listed_before(population, client_):
    response = client_.post(
        reverse("exam_timetable_preview_courses"), SCOPE, content_type="application/json"
    )
    enrolled, meta = build_enrolled_sets_with_meta(**SCOPE)
    credit = build_credit_map(list(enrolled))
    expected = sorted(
        [
            {
                "course_code": code,
                **meta.get(code, {}),
                "enrolled_count": len(students),
                "credit_hours": credit.get(code, 3),
            }
            for code, students in enrolled.items()
        ],
        key=lambda row: str(row["course_code"]),
    )
    assert response.status_code == 200
    assert response.content == JsonResponse({"ok": True, "courses": expected}).content


def test_registrations_changed_since_the_save_are_counted_and_their_boards_reallocated(
    population, client_
):
    codes = sorted(_live())
    built = _build(client_, codes[:6])
    target = built["schedule"][0]
    link = (
        StudentTermSection.objects.filter(term_section__course_code=target["source_course_code"])
        .order_by("student_id")
        .first()
    )
    link.delete()
    added = _add(client_, built, _identities(codes[6:7]))
    report = added["add_courses"]
    assert report["sections_changed"] >= 1
    gender = next(
        row["gender"]
        for row in built["section_enrollment"][target["course_code"]]
        if row["section_key"]
        not in {
            later["section_key"]
            for later in added["section_enrollment"][target["course_code"]]
            if later["membership_fingerprint"] == row["membership_fingerprint"]
        }
    )
    assert {"day": target["day"], "period": target["period"], "gender": gender} in report[
        "rooms_changed"
    ]

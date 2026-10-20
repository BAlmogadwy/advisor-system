"""Room candidates: one room-independent check per request, one lookup per room.

The 2026-10-20 outage: GET room-candidates ran ``validate_placement`` once for
each of ~80 ``Room`` rows, and with one teacher per section on, each run
rebuilt the one-teacher map. ``_PlacementTimeCheck`` now computes the
room-independent half once and indexes the room clashes once.

These tests pin that split to the behaviour it replaced: a verbatim copy of
``validate_placement`` as it stood at 18ba3f8 is the oracle, per room, with the
rule on and off; and the request's cost must not grow with the room count.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from core.models import (
    DeliveryBoard,
    ProgrammeRequirement,
    Room,
    ScenarioStudentCourseRequest,
    ScenarioStudentMap,
    SectionPlacement,
    Student,
    TermSection,
    TermSectionMeeting,
)
from core.services import section_teacher_assignment as sta
from core.services import timetable_workspace as tws
from core.services.rbac import ROLE_SUPER_ADMIN
from core.services.timetable_flags import is_one_teacher_active
from core.services.timetable_online import OnlineCourseLookup
from core.services.timetable_section_policy import section_policy
from core.services.timetable_workspace import (
    _load_board_placements,
    _parallel_policy_sections,
    _room_key,
    _same_course_code,
    _section_teacher_matcher,
    _time_mask,
    preview_placement_room_candidates,
    validate_placement,
)
from tests.one_teacher_support import (
    FLAG_ON,
    budget,
    instructor,
    link,
    place,
    requirement,
    scenario,
    section,
)

pytestmark = pytest.mark.django_db

TARGET = ("SUN", "09:00", "10:15")


@pytest.fixture(params=[True, False], ids=["one_teacher_on", "one_teacher_off"])
def flags(request, settings):
    for name, value in FLAG_ON.items():
        setattr(settings, name, value)
    settings.TIMETABLE_ONE_TEACHER_PER_SECTION = request.param
    return settings


# ── The oracle: validate_placement verbatim at 18ba3f8 (renamed) ──────────


def _legacy_validate_placement(
    board_id: int,
    day: str,
    start_time: str,
    end_time: str,
    room: str,
    term_section_id: int,
    exclude_placement_id: int | None = None,
    ignore_overlap_term_section_ids: set[int] | None = None,
) -> dict:
    """Pre-flight validation for a new or moved placement.

    Builds a bitmask for the *candidate* placement and checks it against
    every existing placement on the board.  For a move operation, pass
    ``exclude_placement_id`` so the section's current position is not
    compared against itself.

    Parameters:
        board_id:             Target board PK.
        day:                  Candidate day (``"SUN"`` etc.).
        start_time:           Candidate start ``"HH:MM"``.
        end_time:             Candidate end ``"HH:MM"``.
        room:                 Candidate room code (may be empty).
        term_section_id:      PK of the ``TermSection`` being placed.
        exclude_placement_id: If moving an existing placement, pass its
                              PK here to skip self-comparison.

    Returns:
        A dict with the shape::

            {
                "valid":              bool,   # True when zero conflicts
                "overlaps":           [...],
                "instructor_clashes": [...],
                "room_clashes":       [...],
                "critical_count":     int,
                "warning_count":      int,
            }
    """
    items = _load_board_placements(board_id)
    new_mask = _time_mask(day, start_time, end_time)
    ignored_overlap_term_sections = {
        int(value) for value in (ignore_overlap_term_section_ids or set())
    }

    # Get instructor for the section being placed
    meeting = TermSectionMeeting.objects.filter(term_section_id=term_section_id).first()
    new_instructor = meeting.instructor if meeting else ""

    ts = TermSection.objects.filter(id=term_section_id).first()
    new_requires_room = not bool(ts and section_policy(ts))
    online_lookup = OnlineCourseLookup()
    new_is_online = False

    # Build real overlap matrix for this board

    board_obj: DeliveryBoard | None = None
    try:
        board_obj = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
        if ts:
            new_is_online = online_lookup.is_online_course_for_board(board_obj, ts.course_code)
        board_courses = {item.course_key for item in items}
        if ts:
            board_courses.add(ts.course_key or ts.course_code)
        from core.services.timetable_overlap import build_overlap_matrix as _bom

        _overlap_mat = _bom(board_obj.scenario_id, board_courses)
    except DeliveryBoard.DoesNotExist:
        _overlap_mat = {}

    # One teacher per section: the section's own teacher, by id (as the engine
    # and the backstop charge it), not the free-text name of its meetings.
    teacher_of = same_teacher = new_teacher = None
    if board_obj is not None and is_one_teacher_active():
        teacher_of, same_teacher = _section_teacher_matcher(board_obj.scenario_id)
        new_teacher = teacher_of(
            term_section_id, f"{ts.course_key or ts.course_code}_{ts.section}" if ts else None
        )
        if new_teacher is not None:
            new_instructor = new_teacher[1]

    overlaps: list[dict] = []
    instructor_clashes: list[dict] = []
    room_clashes: list[dict] = []

    for item in items:
        if item.id == exclude_placement_id:
            continue

        # Time overlap — graduated severity matching the placement engines
        if new_mask & item.mask:
            from core.services.timetable_overlap import (
                HARD_OVERLAP_THRESHOLD,
                SAME_COURSE_SENTINEL,
            )
            from core.services.timetable_overlap import (
                shared_student_count as _ssc_val,
            )

            ts_key = (ts.course_key or ts.course_code) if ts else ""
            same_course = bool(ts and _same_course_code(item.course_key, ts_key))
            ignore_student_overlap = (
                item.term_section_id in ignored_overlap_term_sections
                or _parallel_policy_sections(item.course_key, ts_key)
            )
            if not ignore_student_overlap:
                shared = (
                    _ssc_val(_overlap_mat, ts_key, item.course_key)
                    if ts and not same_course
                    else (SAME_COURSE_SENTINEL if same_course else 0)
                )

                if same_course or shared > 0:
                    is_critical = same_course or shared >= HARD_OVERLAP_THRESHOLD
                    overlaps.append(
                        {
                            "id": item.id,
                            "section": f"{item.course_code}-{item.section}",
                            "detail": f"{item.day} {item.start_time}-{item.end_time}",
                            "severity": "critical" if is_critical else "warning",
                            "shared_students": shared if not same_course else None,
                        }
                    )

            # Instructor clash
            if (
                same_teacher(
                    new_teacher,
                    new_instructor,
                    teacher_of(item.term_section_id, f"{item.course_key}_{item.section}"),
                    item.instructor,
                )
                if same_teacher is not None
                else (
                    new_instructor
                    and item.instructor
                    and new_instructor.strip().upper() == item.instructor.strip().upper()
                )
            ):
                instructor_clashes.append(
                    {
                        "id": item.id,
                        "instructor": new_instructor,
                        "section": f"{item.course_code}-{item.section}",
                    }
                )

            # Room clash on the same board (exclude blank/UNASSIGNED).
            room_key = "" if new_is_online or not new_requires_room else _room_key(room)
            if room_key and not item.is_online and room_key == _room_key(item.room):
                room_clashes.append(
                    {
                        "id": item.id,
                        "room": room,
                        "section": f"{item.course_code}-{item.section}",
                        "scope": "same_board",
                    }
                )

    room_key = "" if new_is_online or not new_requires_room else _room_key(room)
    if board_obj and room_key:
        cross_board_placements = (
            SectionPlacement.objects.filter(board__scenario=board_obj.scenario)
            .exclude(board_id=board_id)
            .exclude(room="")
            .exclude(room="UNASSIGNED")
            .select_related("board", "term_section")
        )
        if exclude_placement_id:
            cross_board_placements = cross_board_placements.exclude(id=exclude_placement_id)

        for other in cross_board_placements:
            if section_policy(other.term_section):
                continue
            if online_lookup.is_online_course_for_board(
                other.board, other.term_section.course_code
            ):
                continue
            if room_key != _room_key(other.room):
                continue
            other_mask = _time_mask(other.day, other.start_time, other.end_time)
            if not (new_mask & other_mask):
                continue
            room_clashes.append(
                {
                    "id": other.id,
                    "room": room,
                    "section": f"{other.term_section.course_code}-{other.term_section.section}",
                    "scope": "cross_board",
                    "board_id": other.board_id,
                    "board_label": other.board.label,
                    "detail": (
                        f"{other.board.label}: {other.day} {other.start_time}-{other.end_time}"
                    ),
                }
            )

    critical_overlaps = sum(1 for o in overlaps if o.get("severity") == "critical")
    warning_overlaps = sum(1 for o in overlaps if o.get("severity") == "warning")
    critical = critical_overlaps + len(instructor_clashes)
    warning = warning_overlaps + len(room_clashes)

    return {
        "valid": critical == 0,
        "overlaps": overlaps,
        "instructor_clashes": instructor_clashes,
        "room_clashes": room_clashes,
        "critical_count": critical,
        "warning_count": warning,
    }


# ── The fixture ───────────────────────────────────────────────────────────


def _requests(sc, student_id: int, course_keys: list[str]) -> None:
    Student.objects.get_or_create(
        student_id=student_id, defaults={"name": f"S{student_id}", "program": "AI"}
    )
    ScenarioStudentMap.objects.get_or_create(
        scenario=sc,
        student_id=student_id,
        defaults={"primary_term": 2, "recommended_courses": course_keys},
    )
    for key in course_keys:
        ScenarioStudentCourseRequest.objects.create(
            scenario=sc,
            student_id=student_id,
            course_key=key,
            course_code=key.split("::", 1)[0],
            primary_term=2,
            status=ScenarioStudentCourseRequest.STATUS_REQUESTED,
            source="test",
        )


def _rooms(count: int) -> None:
    """R1-R7 (R3 twice: an M and an F row) plus filler rooms up to ``count``."""
    named = [
        ("R1", "M"),
        ("R2", "M"),
        ("R3", "M"),
        ("R3", "F"),
        ("R4", "M"),
        ("R5", "M"),
        ("R6", "M"),
        ("R7", "M"),
    ]
    rows = named[:count] + [(f"F{n}", "M") for n in range(max(0, count - len(named)))]
    for code, gender in rows:
        Room.objects.create(
            room_code=code, capacity=40, room_type="lecture", section=gender, department="AI"
        )


def _board(room_count: int = 8):
    """Y (Dr A only) moves to SUN 09:00-10:15. On its board: X (Dr A or Dr B,
    meeting rows still name Dr A) in R1 at 08:00, Y's sibling S2 at 09:30 with
    no room, Z (shared students with Y) with a blank room, and an online course
    in R4. On the other board: W in R3 at the target time, W2 in R3 later, an
    online course in R4, an English section in R5, blank and UNASSIGNED rooms,
    and a lower-case ``r6 `` that is room R6."""
    x = requirement("AI", "AI301", "X COURSE")
    y = requirement("AI", "AI302", "Y COURSE")
    z = requirement("AI", "AI303", "Z COURSE")
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="AI390",
        course_name="ONLINE",
        programme_term=2,
        credit_hours=3,
        is_online=True,
    )
    sc, board = scenario()
    other = DeliveryBoard.objects.create(scenario=sc, label="Term 4", nominal_term=4, program="AI")
    a, b = instructor("Dr A"), instructor("Dr B")
    link("AI", "AI301", a)
    link("AI", "AI301", b, role="co")
    link("AI", "AI302", a)
    for key in (x, y, z):
        budget(sc, key, 2)
    xs, ys, ys2, zs = (
        section(sc, x, "S1"),
        section(sc, y, "S1"),
        section(sc, y, "S2"),
        section(sc, z, "S1"),
    )
    place(board, xs, [("SUN", "08:00", "09:15")], instructor_name="Dr A", room="R1")
    place(board, ys, [("SUN", "08:30", "09:45")], instructor_name="Dr A", room="R2")
    place(board, ys2, [("SUN", "09:30", "10:45")], room="UNASSIGNED")
    place(board, zs, [("SUN", "09:00", "10:15")], room="")
    place(board, section(sc, "AI390", "S1"), [("SUN", "09:00", "10:15")], room="R4")
    place(other, section(sc, "AI304", "S1"), [("SUN", "09:00", "10:15")], room="R3")
    place(other, section(sc, "AI305", "S1"), [("SUN", "11:00", "12:15")], room="R3")
    place(other, section(sc, "AI390", "S2"), [("SUN", "09:00", "10:15")], room="R4")
    place(
        other,
        section(sc, "ENG101", "S1"),
        [(day, "09:00", "12:00") for day in ("SUN", "MON", "WED", "THU")],
        room="R5",
    )
    place(other, section(sc, "AI306", "S1"), [("SUN", "09:00", "10:15")], room="")
    place(other, section(sc, "AI307", "S1"), [("SUN", "09:00", "10:15")], room="UNASSIGNED")
    place(other, section(sc, "AI308", "S1"), [("SUN", "09:30", "10:45")], room="r6 ")
    for student_id in (1, 2, 3):
        _requests(sc, student_id, [y, z])
    _rooms(room_count)
    moving = SectionPlacement.objects.get(term_section=ys)
    return sc, board, moving


def _validation(result: dict) -> dict:
    return {
        "critical_count": result.get("critical_count", 0),
        "warning_count": result.get("warning_count", 0),
        "room_clashes": result.get("room_clashes", []),
    }


# ── Parity ────────────────────────────────────────────────────────────────


def test_every_room_candidate_matches_the_legacy_validation(flags) -> None:
    _sc, board, moving = _board()
    preview = preview_placement_room_candidates(
        moving.id, day=TARGET[0], start_time=TARGET[1], end_time=TARGET[2]
    )
    candidates = preview["candidates"]
    assert {c["room_code"] for c in candidates} == {"R1", "R2", "R3", "R4", "R5", "R6", "R7"}
    assert len(candidates) == 8  # R3 has an M and an F row

    seen_scopes: set[str] = set()
    for candidate in candidates:
        legacy = _legacy_validate_placement(
            board.id, *TARGET, candidate["room_code"], moving.term_section_id, moving.id
        )
        assert candidate["validation"] == _validation(legacy), candidate["room_code"]
        seen_scopes |= {clash["scope"] for clash in legacy["room_clashes"]}
        expected_score = (
            (0 if candidate["resource_available"] else 100000)
            + (0 if legacy["critical_count"] == 0 else 20000)
            + (0 if legacy["warning_count"] == 0 else 1000)
            + (0 if candidate["department_fit"] else 800)
            + (0 if candidate["fits_type"] else 500)
            + (0 if candidate["fits_gender"] else 300)
            + max(0, candidate["capacity_slack"])
        )
        assert candidate["score"] == expected_score, candidate["room_code"]

    by_room = {c["room_code"]: c["validation"] for c in candidates}
    # The fixture exercises both halves of the index and every exclusion.
    assert seen_scopes == {"same_board", "cross_board"}
    assert [c["scope"] for c in by_room["R1"]["room_clashes"]] == ["same_board"]
    assert [c["scope"] for c in by_room["R3"]["room_clashes"]] == ["cross_board"]
    assert [c["room"] for c in by_room["R6"]["room_clashes"]] == ["R6"]
    for clean in ("R2", "R4", "R5", "R7"):
        assert by_room[clean]["room_clashes"] == [], clean


def test_validate_placement_is_unchanged_for_any_room(flags) -> None:
    sc, board, moving = _board()
    rooms = ["R1", "r1", " R3 ", "R4", "R5", "r6", "R7", "", "UNASSIGNED"]
    for room in rooms:
        for exclude in (moving.id, None):
            legacy = _legacy_validate_placement(
                board.id, *TARGET, room, moving.term_section_id, exclude
            )
            assert (
                validate_placement(
                    board.id, *TARGET, room, moving.term_section_id, exclude_placement_id=exclude
                )
                == legacy
            ), (room, exclude)
    legacy = _legacy_validate_placement(board.id, *TARGET, "R1", moving.term_section_id, moving.id)
    # Same-course and shared-student overlaps are in play; the instructor clash
    # with X exists only under the union (one teacher gives X to Dr B).
    assert any(o["severity"] == "critical" for o in legacy["overlaps"])
    assert any(o["shared_students"] for o in legacy["overlaps"])
    assert bool(legacy["instructor_clashes"]) is not is_one_teacher_active()
    # A missing board: no overlap matrix, no cross-board half.
    missing = DeliveryBoard.objects.order_by("-pk").first().pk + 100
    assert validate_placement(
        missing, *TARGET, "R1", moving.term_section_id
    ) == _legacy_validate_placement(missing, *TARGET, "R1", moving.term_section_id)
    # An English section needs no room of its own: no room clash, ever.
    english = TermSection.objects.get(scenario=sc, course_code="ENG101")
    assert validate_placement(board.id, *TARGET, "R3", english.id) == _legacy_validate_placement(
        board.id, *TARGET, "R3", english.id
    )


def test_ignored_overlaps_keep_parity(flags) -> None:
    _sc, board, moving = _board()
    zs = TermSection.objects.get(course_code="AI303")
    preview = preview_placement_room_candidates(
        moving.id,
        day=TARGET[0],
        start_time=TARGET[1],
        end_time=TARGET[2],
        ignore_overlap_term_section_ids={zs.id},
    )
    for candidate in preview["candidates"]:
        legacy = _legacy_validate_placement(
            board.id,
            *TARGET,
            candidate["room_code"],
            moving.term_section_id,
            moving.id,
            ignore_overlap_term_section_ids={zs.id},
        )
        assert candidate["validation"] == _validation(legacy), candidate["room_code"]


# ── Cost: independent of the room count ───────────────────────────────────


@pytest.fixture
def http():
    user = User.objects.create_user(username="room-candidates-admin")
    user.groups.add(Group.objects.get_or_create(name=ROLE_SUPER_ADMIN)[0])
    client = Client()
    client.force_login(user)
    return client


def _room_candidates(http, moving) -> tuple[int, dict]:
    url = (
        f"/ops/tw/placements/{moving.id}/room-candidates/"
        f"?day={TARGET[0]}&start_time={TARGET[1]}&end_time={TARGET[2]}"
    )
    with CaptureQueriesContext(connection) as ctx:
        response = http.get(url)
    assert response.status_code == 200, response.content
    return len(ctx.captured_queries), json.loads(response.content)


def test_room_candidates_query_count_does_not_grow_with_rooms(flags, http, monkeypatch) -> None:
    calls = {"load_assignment": 0, "matcher": 0}
    real_load, real_matcher = sta.load_assignment, tws._section_teacher_matcher

    def counted_load(*args, **kwargs):
        calls["load_assignment"] += 1
        return real_load(*args, **kwargs)

    def counted_matcher(*args, **kwargs):
        calls["matcher"] += 1
        return real_matcher(*args, **kwargs)

    monkeypatch.setattr(sta, "load_assignment", counted_load)
    monkeypatch.setattr(tws, "_section_teacher_matcher", counted_matcher)

    _sc, _board_obj, moving = _board(room_count=5)
    few_queries, few = _room_candidates(http, moving)
    expected = 1 if is_one_teacher_active() else 0
    assert calls == {"load_assignment": expected, "matcher": expected}

    for n in range(55):
        Room.objects.create(room_code=f"X{n}", capacity=30, room_type="lecture", section="M")
    many_queries, many = _room_candidates(http, moving)
    assert len(few["candidates"]) == 5 and many["summary"]["total"] == 60
    assert many_queries == few_queries
    assert calls == {"load_assignment": 2 * expected, "matcher": 2 * expected}


# ── Student outcome: the baseline is summarised once ──────────────────────


def test_student_outcome_baseline_is_summarised_once_with_identical_output(
    flags, monkeypatch
) -> None:
    from core.services import timetable_move_outcome as tmo

    _sc, _board_obj, moving = _board()
    moves = [
        {"kind": "lect", "day": day, "start": start, "end": end}
        for day, start, end in (
            ("SUN", "09:00", "10:15"),
            ("MON", "09:00", "10:15"),
            ("TUE", "13:00", "14:15"),
        )
    ]
    calls = {"statuses": 0}
    real_statuses = tmo.student_statuses

    def counted_statuses(*args, **kwargs):
        calls["statuses"] += 1
        return real_statuses(*args, **kwargs)

    monkeypatch.setattr(tmo, "student_statuses", counted_statuses)
    fast = tmo.preview_placement_student_outcome_candidates(moving.id, candidate_moves=moves)
    assert fast["student_outcome_available"] is True
    assert len(fast["candidates"]) == 3
    # One for the baseline, one per candidate (it was 1 + 4 per candidate).
    assert calls["statuses"] == 1 + 3

    real_compare = tmo.compare_evaluations

    def recomputing_compare(**kwargs):
        kwargs.pop("before", None)
        kwargs.pop("before_status", None)
        return real_compare(**kwargs)

    monkeypatch.setattr(tmo, "compare_evaluations", recomputing_compare)
    slow = tmo.preview_placement_student_outcome_candidates(moving.id, candidate_moves=moves)
    assert json.dumps(fast, sort_keys=True, default=str) == json.dumps(
        slow, sort_keys=True, default=str
    )
    assert any(row["student_outcome"]["before"]["score"] for row in fast["candidates"]), (
        "the fixture must give the baseline a score"
    )


# ── Request scope: one read of the one-teacher map per preview request ────


@pytest.mark.parametrize(
    "request_for",
    ["room_candidates", "slot_candidates", "outcome_candidates", "boards_list", "board_conflicts"],
)
def test_preview_views_read_the_one_teacher_map_once(
    request_for, http, settings, monkeypatch
) -> None:
    for name, value in FLAG_ON.items():
        setattr(settings, name, value)
    sc, board, moving = _board()
    collects = []
    real_collect = sta._collect

    def counted_collect(scenario_id, *args, **kwargs):
        collects.append(scenario_id)
        return real_collect(scenario_id, *args, **kwargs)

    monkeypatch.setattr(sta, "_collect", counted_collect)
    base = f"/ops/tw/placements/{moving.id}"
    outcome_body = {
        "candidates": [{"kind": "lect", "day": "MON", "start": "09:00", "end": "10:15"}]
    }
    send = {
        "room_candidates": lambda: http.get(
            f"{base}/room-candidates/?day=SUN&start_time=09:00&end_time=10:15"
        ),
        "slot_candidates": lambda: http.get(f"{base}/slot-candidates/"),
        "outcome_candidates": lambda: http.post(
            f"{base}/student-outcome-candidates/",
            json.dumps(outcome_body),
            content_type="application/json",
        ),
        "boards_list": lambda: http.get(f"/ops/tw/boards/?scenario_id={sc.id}"),
        "board_conflicts": lambda: http.get(f"/ops/tw/boards/{board.id}/conflicts/"),
    }[request_for]
    response = send()
    assert response.status_code == 200, response.content
    assert collects == [sc.id]

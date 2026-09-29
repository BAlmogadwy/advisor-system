"""Exam locks, resolved: every refusal names its field, courses and cell.

A lock closes a day, or one period of it, of a SAVED timetable. What it holds
is what the saved run (the source) placed there, with the rooms and section
rows the source saved. These tests need no database: the saved runs are made
by hand (``exam_locks_fixtures``) and are readable by the Student lists.
"""

import json
from copy import deepcopy

import pytest

from core.services import exam_locks
from core.services.exam_input_fingerprint import fingerprint_exam_inputs
from core.services.exam_locks import (
    NO_LOCKS,
    ExamLocksError,
    exam_locks_qa,
    mark_locked_rows,
    require_lock_list,
    resolve_exam_locks,
)
from core.services.exam_run_schema import _lock_room_action, derive_status_surface
from core.services.linked_exams import NO_LINKS, resolve_linked_exams
from tests.exam_locks_fixtures import (
    DAYS,
    PERIODS,
    TERM,
    board_of,
    courses_of,
    move,
    saved_run,
)

P1, P2 = PERIODS
SUN, MON, TUE = DAYS

#: A on Sunday morning, B Sunday midday, C Monday morning, D Tuesday midday,
#: E in OVERFLOW; Monday midday and Tuesday morning are empty.
PLACEMENTS = {"A": (SUN, P1), "B": (SUN, P2), "C": (MON, P1), "D": (TUE, P2), "E": None}


def _run(**changes):
    return saved_run(PLACEMENTS, **changes)


def resolve(raw, run=None, **overrides):
    run = _run() if run is None else run
    kwargs = {
        "days": DAYS,
        "periods": PERIODS,
        "courses": courses_of(run),
        "source": run,
        "pinned": [],
        "links": NO_LINKS,
        "board": board_of(run),
        "assign_rooms": True,
        "current_term": TERM,
    }
    kwargs.update(overrides)
    return resolve_exam_locks(raw, **kwargs)


def refused(raw, run=None, **overrides) -> ExamLocksError:
    with pytest.raises(ExamLocksError) as caught:
        resolve(raw, run, **overrides)
    return caught.value


def _shape(error: ExamLocksError) -> tuple:
    return error.code, error.field, error.courses, error.cell


# ── no locks ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", [None, []], ids=["none", "empty"])
def test_no_locks_is_no_locks_and_reads_no_source(raw):
    assert resolve(raw, source=None) is NO_LOCKS
    assert not NO_LOCKS
    assert NO_LOCKS.closed_slots([{"index": 0, "day": SUN, "period": P1}]) == frozenset()
    assert NO_LOCKS.pins() == []
    assert NO_LOCKS.saved() == []


# ── the request's shape ──────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", [None, {}, "W1-Sun", 0])
def test_a_lock_list_that_is_not_a_list_is_refused(raw):
    with pytest.raises(ExamLocksError) as caught:
        require_lock_list(raw)
    assert (caught.value.code, caught.value.field) == ("exam_locks_invalid", "exam_locks")


def test_the_resolver_refuses_a_non_list_too():
    assert _shape(refused({"day": SUN})) == ("exam_locks_invalid", "exam_locks", None, None)


@pytest.mark.parametrize(
    "item",
    [
        5,
        {},
        {"day": ""},
        {"day": "   "},
        {"day": 3},
        {"period": P1},
        {"day": SUN, "period": None},
        {"day": SUN, "period": ""},
        {"day": SUN, "room": "R1"},
    ],
    ids=[
        "not-an-object",
        "empty",
        "blank-day",
        "spaces-day",
        "number-day",
        "no-day",
        "null-period",
        "blank-period",
        "unknown-key",
    ],
)
def test_a_malformed_lock_is_refused_by_its_place(item):
    assert _shape(refused([{"day": TUE}, item])) == (
        "exam_locks_invalid",
        "exam_locks[1]",
        None,
        None,
    )


@pytest.mark.parametrize(
    "item, cell",
    [
        ({"day": "W9-Sun"}, {"day": "W9-Sun"}),
        ({"day": SUN, "period": "07:00-08:00"}, {"day": SUN, "period": "07:00-08:00"}),
    ],
    ids=["day", "period"],
)
def test_a_lock_outside_the_header_is_refused(item, cell):
    assert _shape(refused([item])) == ("exam_locks_outside_timetable", "exam_locks[0]", None, cell)


@pytest.mark.parametrize(
    "items",
    [
        [{"day": SUN}, {"day": SUN}],
        [{"day": SUN, "period": P1}, {"day": SUN, "period": P1}],
        [{"day": SUN}, {"day": SUN, "period": P2}],
        [{"day": SUN, "period": P2}, {"day": SUN}],
    ],
    ids=["day-twice", "cell-twice", "cell-in-locked-day", "day-over-locked-cell"],
)
def test_a_cell_locked_twice_is_refused(items):
    error = refused(items)
    assert (error.code, error.field) == ("exam_locks_repeated", "exam_locks[1]")


def test_locks_are_saved_in_header_order_and_trimmed():
    locks = resolve(
        [
            {"day": TUE},
            {"day": f" {MON}", "period": P2},
            {"day": MON, "period": f"{P1} "},
        ]
    )
    assert locks.saved() == [
        {"day": MON, "period": P1},
        {"day": MON, "period": P2},
        {"day": TUE},
    ]
    assert locks.cells == {(MON, P1), (MON, P2), (TUE, P1), (TUE, P2)}
    assert locks.positions[(MON, P1)] == 2
    assert locks.positions[(TUE, P2)] == 0


# ── what a lock holds ────────────────────────────────────────────────────────


def test_a_day_lock_holds_every_exam_of_the_day_and_closes_its_slots():
    locks = resolve([{"day": SUN}, {"day": MON, "period": P2}])
    assert dict(locks.placements) == {"A": (SUN, P1), "B": (SUN, P2)}
    assert locks.pins() == [
        {"course_code": "A", "day": SUN, "period": P1},
        {"course_code": "B", "day": SUN, "period": P2},
    ]
    slots = _run()["slots"]
    assert locks.closed_slots(slots) == frozenset({0, 1, 3})
    assert locks.open_days(DAYS, PERIODS) == {MON, TUE}
    assert locks.holds({"course_code": "A", "day": SUN, "period": P1})
    assert not locks.holds({"course_code": "A", "day": MON, "period": P1})
    assert not locks.holds({"course_code": "C", "day": MON, "period": P1})


def test_an_empty_cell_may_be_locked_and_simply_stays_empty():
    locks = resolve([{"day": MON, "period": P2}])
    assert locks
    assert dict(locks.placements) == {}
    assert locks.closed_slots(_run()["slots"]) == frozenset({3})


def test_a_day_lock_covers_a_period_the_header_gained():
    wider = [*PERIODS, "14:00-16:00"]
    locks = resolve([{"day": SUN}], periods=wider)
    assert locks.cells == {(SUN, period) for period in wider}
    assert dict(locks.placements) == {"A": (SUN, P1), "B": (SUN, P2)}


def test_a_day_lock_never_lets_an_exam_of_a_removed_period_go():
    """The saved run had a third period; the header dropped it. The exam there
    is in no cell of the current header, but it is still locked: refused."""
    run = saved_run(
        {**PLACEMENTS, "X": (SUN, "14:00-16:00")},
        periods=[*PERIODS, "14:00-16:00"],
    )
    error = refused([{"day": SUN}], run)
    assert _shape(error) == (
        "exam_locks_outside_timetable",
        "exam_locks[0]",
        ["X"],
        {"day": SUN, "period": "14:00-16:00"},
    )


# ── the source ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("source", [None, {"status": "feasibility_error"}], ids=["none", "not-ok"])
def test_locks_need_a_readable_saved_run(source):
    assert _shape(refused([{"day": SUN}], source=source)) == (
        "exam_locks_source_required",
        "previous_run_id",
        None,
        None,
    )


def test_locked_rooms_need_room_assignment_on():
    assert _shape(refused([{"day": SUN}], assign_rooms=False))[:2] == (
        "exam_locks_invalid",
        "assign_rooms",
    )


def test_a_build_for_other_programs_or_sections_is_refused():
    error = refused([{"day": SUN}], board=None, scope=(["AI"], ["F", "M"]))
    assert (error.code, error.field) == ("exam_locks_scope_changed", "exam_locks")
    # The same programs and sections in another order are the same scope.
    assert resolve(
        [{"day": SUN}], _run(exam_locks=[{"day": SUN}]), board=None, scope=(["CS"], ["M", "F"])
    )


def test_a_locked_exam_that_is_not_selected_is_refused():
    run = _run()
    courses = courses_of(run)
    del courses["B"]
    assert _shape(refused([{"day": SUN}], run, courses=courses, board=None)) == (
        "exam_locks_course_not_selected",
        "exam_locks[0]",
        ["B"],
        {"day": SUN, "period": P2},
    )


def test_a_locked_exam_whose_code_was_renumbered_keeps_its_lock_under_the_new_code():
    """The code "A" became "A (1)" when another study plan named it differently: the
    same exam (identity), so the lock holds it, with its saved rows, under "A (1)"."""
    run = _run(exam_locks=[{"day": SUN}])
    courses = courses_of(run)
    courses["A (1)"] = courses.pop("A")
    locks = resolve([{"day": SUN}], run, courses=courses, board=None)
    assert dict(locks.placements) == {"A (1)": (SUN, P1), "B": (SUN, P2)}
    assert locks.identities["A (1)"] == _entry(run, "A")["course_identity"]
    saved = _entry(run, "A")
    assert locks.rooms_text["A (1)"] == json.dumps(saved["rooms"], ensure_ascii=False)
    assert locks.sections_text["A (1)"] == json.dumps(
        run["section_enrollment"]["A"], ensure_ascii=False
    )
    assert locks.operations_text["A (1)"] == json.dumps(
        run["operations_snapshot"]["courses"]["A"]["sections"], ensure_ascii=False
    )
    assert locks.pins() == [
        {"course_code": "A (1)", "day": SUN, "period": P1},
        {"course_code": "B", "day": SUN, "period": P2},
    ]


def test_codes_swapped_between_two_locked_exams_follow_their_identities():
    run = _run(exam_locks=[{"day": SUN}])
    courses = courses_of(run)
    courses["A"], courses["B"] = courses["B"], courses["A"]
    locks = resolve([{"day": SUN}], run, courses=courses, board=None)
    # B's exam is shown as "A" now: it keeps B's cell and B's rooms.
    assert dict(locks.placements) == {"A": (SUN, P2), "B": (SUN, P1)}
    assert locks.rooms_text["A"] == json.dumps(_entry(run, "B")["rooms"], ensure_ascii=False)


def test_a_renumbered_partner_of_a_shared_locked_room_is_named_by_its_new_code():
    """The saved rows name the other course in a shared room by its saved code;
    restored under the new code, as the department files check them. A room
    no renumbering touches keeps its saved bytes."""
    run = _sharing()
    run["exam_locks"] = [{"day": SUN}]
    courses = courses_of(run)
    courses["B (2)"] = courses.pop("B")
    links = resolve_linked_exams(
        [
            {
                "members": [
                    {"course_identity": courses[code]["course_identity"]} for code in ("A", "B (2)")
                ]
            }
        ],
        courses,
    )
    assert links.unit("A") == links.unit("B (2)") is not None
    locks = resolve([{"day": SUN}], run, courses=courses, links=links, board=None)
    rooms_a = json.loads(locks.rooms_text["A"])
    assert {tuple(room["room_shared_with"]) for room in rooms_a} == {("B (2)",)}
    assert locks.rooms_text["B (2)"] == json.dumps(_entry(run, "B")["rooms"], ensure_ascii=False)
    # The source run is never changed.
    assert {tuple(room["room_shared_with"]) for room in _entry(run, "A")["rooms"]} == {("B",)}
    # Unlinked, the renumbered partner is named by the code it has now.
    error = refused([{"day": SUN}], run, courses=courses, links=NO_LINKS, board=None)
    assert (error.code, error.courses) == ("exam_locks_link_room_shared", ["A", "B (2)"])


def test_only_a_renumbered_partner_list_is_rewritten_and_sorted_again():
    rooms = [
        {"room_code": "R1", "room_shared_with": ["B", "C"]},
        {"room_code": "R2", "room_shared_with": ["C", "B"]},
        {"room_code": "R3"},
    ]
    renumbered = exam_locks._renumbered_partners(rooms, {"B": "Z (1)", "C": "C"})
    assert renumbered == [
        {"room_code": "R1", "room_shared_with": ["C", "Z (1)"]},
        {"room_code": "R2", "room_shared_with": ["C", "Z (1)"]},
        {"room_code": "R3"},
    ]
    # Nothing renumbered: every row as saved, even a list saved out of order.
    assert exam_locks._renumbered_partners(rooms, {"B": "B", "C": "C"}) == rooms
    assert rooms[1]["room_shared_with"] == ["C", "B"], "the saved rows are never changed"


def test_a_locked_exam_whose_identity_is_gone_is_refused():
    run = _run(exam_locks=[{"day": SUN}])
    courses = courses_of(run)
    courses["A"] = {**courses["A"], "course_identity": "A::another plan name"}
    assert _shape(refused([{"day": SUN}], run, courses=courses, board=None)) == (
        "exam_locks_course_not_selected",
        "exam_locks[0]",
        ["A"],
        {"day": SUN, "period": P1},
    )


def _broken(change) -> dict:
    run = _run()
    change(run)
    return run


def _entry(run, code):
    return next(entry for entry in run["schedule"] if entry["course_code"] == code)


@pytest.mark.parametrize(
    "change",
    [
        lambda run: run.update(assign_rooms=False),
        lambda run: _entry(run, "A").update(rooms=[]),
        lambda run: _entry(run, "A")["rooms"][0]["section_parts"][0].update(student_count=11),
        lambda run: run["section_enrollment"].pop("A"),
        lambda run: run["operations_snapshot"]["courses"]["A"].pop("sections"),
        lambda run: run["section_enrollment"]["A"][0].update(membership_fingerprint="x"),
        lambda run: _entry(run, "A")["rooms"].pop(),
    ],
    ids=[
        "saved-without-rooms",
        "no-rooms",
        "parts-do-not-add-up",
        "no-section-rows",
        "no-operations-rows",
        "not-readable-by-student-lists",
        "a-group-with-no-room",
    ],
)
def test_a_locked_exam_needs_complete_saved_rooms_and_sections(change):
    error = refused([{"day": SUN}], _broken(change))
    assert (error.code, error.field) == ("exam_locks_source_incomplete", "exam_locks[0]")
    assert error.cell["day"] == SUN
    # A fixed-time Check and Save repairs it; a Build would move every exam.
    assert "Check and save" in str(error)
    assert "uild" not in str(error)


def test_an_exam_of_an_unlocked_cell_needs_nothing_complete():
    run = _broken(lambda run: _entry(run, "C").update(rooms=[]))
    run["section_enrollment"]["C"][0]["membership_fingerprint"] = "x"
    assert resolve([{"day": MON, "period": P2}], run)


@pytest.mark.parametrize(
    "current", [("1448", "2"), ("1449", "1"), None], ids=["term", "year", "none"]
)
def test_locked_exams_of_another_term_are_refused(current):
    error = refused([{"day": SUN}], current_term=current)
    assert (error.code, error.courses) == ("exam_locks_term_changed", ["A", "B"])


# ── links ────────────────────────────────────────────────────────────────────


def _links(run, *groups):
    courses = courses_of(run)
    return resolve_linked_exams(
        [
            {"members": [{"course_identity": courses[code]["course_identity"]} for code in group]}
            for group in groups
        ],
        courses,
    )


def test_a_link_reaching_out_of_a_locked_cell_is_refused():
    run = _run()
    links = _links(run, ("D", "E"), ("A", "C"))
    error = refused([{"day": SUN}], run, links=links, board=None)
    assert _shape(error) == (
        "exam_locks_link_outside",
        "linked_exams[1]",
        ["A", "C"],
        {"day": SUN, "period": P1},
    )


def test_a_link_split_across_two_locked_cells_is_refused():
    run = _run()
    error = refused([{"day": SUN}], run, links=_links(run, ("A", "B")), board=None)
    assert (error.code, error.field) == ("exam_locks_link_outside", "linked_exams[0]")


def test_courses_linked_inside_one_locked_cell_are_allowed():
    run = saved_run({**PLACEMENTS, "B": (SUN, P1)})
    locks = resolve([{"day": SUN}], run, links=_links(run, ("A", "B")))
    assert dict(locks.placements) == {"A": (SUN, P1), "B": (SUN, P1)}


def _sharing() -> dict:
    run = saved_run({**PLACEMENTS, "B": (SUN, P1)})
    for code, partner in (("A", "B"), ("B", "A")):
        for room in _entry(run, code)["rooms"]:
            room["room_shared_with"] = [partner]
    return run


def test_courses_sharing_a_room_in_a_locked_cell_cannot_be_unlinked():
    run = _sharing()
    error = refused([{"day": SUN}], run, links=NO_LINKS)
    assert _shape(error) == (
        "exam_locks_link_room_shared",
        "linked_exams",
        ["A", "B"],
        {"day": SUN, "period": P1},
    )
    assert resolve([{"day": SUN}], run, links=_links(run, ("A", "B")))


# ── pins (rule 6) ────────────────────────────────────────────────────────────


def test_a_locked_exam_pinned_elsewhere_is_refused():
    error = refused([{"day": SUN}], pinned=[{"course_code": "B", "day": MON, "period": P2}])
    assert _shape(error) == (
        "exam_locks_pinned_elsewhere",
        "pinned",
        ["B"],
        {"day": SUN, "period": P2},
    )


def test_a_locked_exam_pinned_to_its_own_cell_is_fine():
    assert resolve([{"day": SUN}], pinned=[{"course_code": "A", "day": SUN, "period": P1}])


def test_an_exam_pinned_into_a_locked_cell_is_refused():
    error = refused(
        [{"day": MON, "period": P2}], pinned=[{"course_code": "C", "day": MON, "period": P2}]
    )
    assert _shape(error) == (
        "exam_locks_pin_in_locked_cell",
        "pinned",
        ["C"],
        {"day": MON, "period": P2},
    )


# ── the board (rule 6) ───────────────────────────────────────────────────────


def _locked_sunday() -> dict:
    return _run(exam_locks=[{"day": SUN}])


def test_a_locked_exam_moved_out_is_refused():
    run = _locked_sunday()
    error = refused([{"day": SUN}], run, board=move(board_of(run), "B", MON, P2))
    assert _shape(error) == (
        "exam_locks_moved_out",
        "exam_locks[0]",
        ["B"],
        {"day": SUN, "period": P2},
    )


def test_an_exam_moved_into_a_locked_cell_is_refused():
    run = _locked_sunday()
    error = refused([{"day": SUN}], run, board=move(board_of(run), "C", SUN, P2))
    assert _shape(error) == (
        "exam_locks_moved_in",
        "exam_locks[0]",
        ["C"],
        {"day": SUN, "period": P2},
    )


def test_an_exam_moved_from_overflow_into_a_locked_cell_is_refused():
    run = _locked_sunday()
    error = refused([{"day": SUN}], run, board=move(board_of(run), "E", SUN, P1))
    assert (error.code, error.courses) == ("exam_locks_moved_in", ["E"])


def test_a_locked_exam_missing_from_the_board_is_refused():
    run = _locked_sunday()
    board = [entry for entry in board_of(run) if entry["course_code"] != "A"]
    error = refused([{"day": SUN}], run, board=board)
    assert (error.code, error.courses) == ("exam_locks_course_not_selected", ["A"])


@pytest.mark.parametrize(
    "board_change, period",
    [
        (lambda board: move(board, "A", MON, P2), P1),
        (lambda board: move(board, "C", SUN, P2), P2),
    ],
    ids=["moved-out", "moved-in"],
)
def test_locking_a_cell_whose_changes_are_not_saved_is_refused(board_change, period):
    run = _run()
    error = refused([{"day": SUN}], run, board=board_change(board_of(run)))
    assert _shape(error) == (
        "exam_locks_cell_unsaved",
        "exam_locks[0]",
        None,
        {"day": SUN, "period": period},
    )


def test_a_cell_matching_the_saved_run_may_be_locked_now():
    run = _run()
    board = move(board_of(run), "C", MON, P2)  # an edit elsewhere is not the lock's
    locks = resolve([{"day": SUN}], run, board=board)
    assert dict(locks.placements) == {"A": (SUN, P1), "B": (SUN, P2)}


def test_a_build_honours_only_locks_the_saved_run_already_has():
    run = _locked_sunday()
    assert resolve([{"day": SUN}], run, board=None)
    # A part of a saved day lock is an unlock of the rest.
    assert dict(resolve([{"day": SUN, "period": P2}], run, board=None).placements) == {
        "B": (SUN, P2)
    }
    error = refused([{"day": SUN}, {"day": MON, "period": P1}], run, board=None)
    assert _shape(error) == (
        "exam_locks_cell_unsaved",
        "exam_locks[1]",
        None,
        {"day": MON, "period": P1},
    )


# ── the frozen parts ─────────────────────────────────────────────────────────


def test_restored_rooms_are_fresh_objects_with_the_saved_bytes():
    run = _run()
    locks = resolve([{"day": SUN}], run)
    entry = {"course_code": "A", "day": SUN, "period": P1, "rooms": []}
    locks.restore_rooms(entry)
    saved = _entry(run, "A")["rooms"]
    assert json.dumps(entry["rooms"], ensure_ascii=False) == json.dumps(saved, ensure_ascii=False)
    entry["rooms"][0]["room_code"] = "changed"
    again = {"course_code": "A"}
    locks.restore_rooms(again)
    assert again["rooms"] == saved


def test_frozen_sections_come_back_and_every_change_since_the_save_is_reported():
    run = saved_run(PLACEMENTS, sizes={"A": {"M": 12, "F": 9, "U": 2}, "B": {"M": 5}})
    locks = resolve([{"day": SUN}], run)
    live_a = deepcopy(run["section_enrollment"]["A"])
    frozen_a = deepcopy(live_a)
    by_gender = {row["gender"]: row for row in live_a}
    by_gender["M"].update(student_count=14, membership_fingerprint="1" * 64)  # grew
    by_gender["F"].update(membership_fingerprint="2" * 64)  # swapped, same size
    live_a.remove(by_gender["U"])  # gone
    live_b = deepcopy(run["section_enrollment"]["B"])
    live_b[0].update(student_count=4, membership_fingerprint="3" * 64)  # shrank
    live_b.append({**live_b[0], "section_key": "term-section:new", "section": "M9"})  # new
    enrollment = {"A": live_a, "B": live_b, "C": [{"live": True}]}
    operations = {
        "A": [{**row, "instructors": ["Dr Live"]} for row in live_a],
        "C": [{"live": True}],
    }
    drift = locks.freeze_sections(enrollment, operations)
    assert enrollment["A"] == frozen_a
    assert enrollment["B"] == run["section_enrollment"]["B"]
    assert enrollment["C"] == [{"live": True}]
    assert [(row["course_code"], row["gender"], row["change"]) for row in drift] == [
        ("A", "F", "swapped"),
        ("A", "M", "grew"),
        ("A", "U", "gone"),
        ("B", "M", "shrank"),
        ("B", "M", "new"),
    ]
    grew = drift[1]
    assert (grew["saved_count"], grew["live_count"], grew["day"], grew["period"]) == (
        12,
        14,
        SUN,
        P1,
    )
    assert (drift[4]["saved_count"], drift[4]["live_count"]) == (0, 4)
    # Operations rows are the saved ones, but who teaches a section is live.
    frozen_ops = run["operations_snapshot"]["courses"]["A"]["sections"]
    assert [row["student_count"] for row in operations["A"]] == [
        row["student_count"] for row in frozen_ops
    ]
    assert {tuple(row["instructors"]) for row in operations["A"] if row["gender"] != "U"} == {
        ("Dr Live",)
    }
    assert next(row for row in operations["A"] if row["gender"] == "U")["instructors"] == [
        "Dr Saved"
    ]
    assert "B" not in operations and operations["C"] == [{"live": True}]


def test_nothing_changed_is_no_drift():
    run = _run()
    locks = resolve([{"day": SUN}], run)
    enrollment = deepcopy(run["section_enrollment"])
    assert locks.freeze_sections(enrollment, {}) == []
    assert enrollment == run["section_enrollment"]


def test_a_solved_board_that_breaks_a_lock_is_a_bug_not_a_refusal():
    run = _run()
    locks = resolve([{"day": SUN}], run)
    board = board_of(run)
    locks.require_board(board)
    for broken in (
        move(board, "A", MON, P2),
        move(board, "C", SUN, P1),
        [entry for entry in board if entry["course_code"] != "B"],
    ):
        with pytest.raises(RuntimeError):
            locks.require_board(broken)
    NO_LOCKS.require_board(move(board, "A", MON, P2))


# ── the input fingerprint ────────────────────────────────────────────────────


def _fingerprint(**extra) -> str:
    result = {
        "enrollment_source": "scraper_timetable",
        "credit_map": {"A": 3},
        "section_enrollment": {"A": []},
        "operations_snapshot": None,
        "buckets_summary": [],
        "enrollment_scope": {"programs": ["CS"], "sections": ["M"]},
        "assign_rooms": True,
        "seed": None,
    }
    return fingerprint_exam_inputs(
        result=result,
        enrolled_sets={"A": {1, 2}},
        course_meta={"A": {"programs": ["CS"]}},
        student_attribution=[{"student_id": 1}],
        rooms=[{"room_code": "R1"}],
        days=DAYS,
        periods=PERIODS,
        max_per_day=2,
        thin_conflict_threshold=0,
        **extra,
    )


#: ``fingerprint_exam_inputs`` of the inputs above, recorded on origin/master
#: (ae056bd) before locks existed: no locks must leave it exactly as it was.
MASTER_FINGERPRINT = "606cb237263d27feeb959db6b1248882276a7ed4fc4fde91c560257edfad5b29"


def test_no_locks_leave_the_fingerprint_as_it_was():
    assert _fingerprint() == _fingerprint(exam_locks=None) == _fingerprint(exam_locks={})
    assert _fingerprint() == MASTER_FINGERPRINT


def test_the_fingerprint_changes_with_the_locks_and_their_rooms():
    run = _run()
    sunday = resolve([{"day": SUN}], run).fingerprint_block()
    monday = resolve([{"day": MON, "period": P1}], run).fingerprint_block()
    moved = _run()
    _entry(moved, "A")["rooms"][0]["room_code"] = "elsewhere"
    sunday_elsewhere = resolve([{"day": SUN}], moved).fingerprint_block()
    prints = {
        _fingerprint(),
        _fingerprint(exam_locks=sunday),
        _fingerprint(exam_locks=monday),
        _fingerprint(exam_locks=sunday_elsewhere),
    }
    assert len(prints) == 4


# ── reports (rule 5) ─────────────────────────────────────────────────────────


def _placed(run, locks) -> list[dict]:
    board = deepcopy(run["schedule"])
    for entry in board:
        if locks.holds(entry):
            locks.restore_rooms(entry)
    return board


def test_the_lock_report_lists_each_cell_and_every_problem_inside_it():
    run = saved_run({**PLACEMENTS, "B": (SUN, P1)})
    locks = resolve([{"day": SUN}, {"day": MON, "period": P2}], run)
    board = _placed(run, locks)
    a = next(entry for entry in board if entry["course_code"] == "A")
    a["rooms"].append(
        {"room_code": "UNASSIGNED", "section": "M1", "student_count": 3, "gender": "M"}
    )
    qa = {
        "same_slot_conflicts": [
            {"student_id": 1, "slot_index": 0, "courses": ["A", "B"]},
            {"student_id": 2, "slot_index": 0, "courses": ["A", "B"]},
            {"student_id": 3, "slot_index": 2, "courses": ["C", "X"]},
        ],
        "bucket_day_violations": [
            {"program": "CS", "programme_term": 1, "day": SUN, "courses": ["A", "B"]},
            {"program": "CS", "programme_term": 2, "day": MON, "courses": ["A", "C"]},
        ],
        "rooms": {
            "room_double_bookings": [
                {"slot_index": 0, "room_code": "RM-A", "courses": ["A", "B"]},
                {"slot_index": 2, "room_code": "RM-C", "courses": ["C", "D"]},
            ]
        },
    }
    inventory = [
        {"room_code": "RM-A", "capacity": 10, "section": "M"},  # 12 seated
        {"room_code": "RF-A", "capacity": 40, "section": "M"},  # cohort changed
        {"room_code": "RF-B", "capacity": 40, "section": "F"},
        # RM-B is gone from the inventory.
    ]
    drift = [{"kind": "registrations_changed", "course_code": "A"}]
    report = exam_locks_qa(locks, board, qa, inventory, drift)
    assert report["cells"] == [
        {
            "day": SUN,
            "period": P1,
            "courses": [
                {"course_code": "A", "course_identity": "A::A name"},
                {"course_code": "B", "course_identity": "B::B name"},
            ],
        },
        {"day": SUN, "period": P2, "courses": []},
        {"day": MON, "period": P2, "courses": []},
    ]
    assert report["locked_courses"] == 2
    kinds = [
        (issue["kind"], issue.get("room_code") or issue.get("course_code"))
        for issue in report["issues"]
    ]
    assert kinds == [
        ("clash", None),
        ("bucket_day", None),
        ("registrations_changed", "A"),
        ("unassigned", "A"),
        ("room_cohort_changed", "RF-A"),
        ("room_over_capacity", "RM-A"),
        ("room_unavailable", "RM-B"),
        ("double_booking", "RM-A"),
    ]
    assert report["issues"][0] == {
        "kind": "clash",
        "day": SUN,
        "period": P1,
        "courses": ["A", "B"],
        "student_count": 2,
    }
    assert report["issue_count"] == 8


def test_rows_inside_a_locked_cell_are_marked_locked_and_nothing_else_is():
    run = _run()
    locks = resolve([{"day": SUN}], run)
    details = [
        {"kind": "same_slot", "courses": ["A", "B"]},
        {"kind": "bucket_day", "courses": ["A", "C"]},
    ]
    qa = {
        "manual_override_details": details,
        "schedule_violation_details": details,
        "rooms": {
            "unassigned_room_sections": [
                {"course_code": "A", "day": SUN, "period": P1},
                {"course_code": "C", "day": MON, "period": P1},
            ],
            "room_double_bookings": [{"courses": ["A", "B"]}, {"courses": ["B", "C"]}],
        },
        "multi_sitting_details": [{"course_code": "B"}, {"course_code": "D"}],
        "room_feasibility_violations": [{"course_code": "A"}, {"course_code": "E"}],
    }
    mark_locked_rows(qa, locks)
    assert [row.get("locked") for row in details] == [True, None]
    assert [row.get("locked") for row in qa["rooms"]["unassigned_room_sections"]] == [True, None]
    assert [row.get("locked") for row in qa["rooms"]["room_double_bookings"]] == [True, None]
    assert [row.get("locked") for row in qa["multi_sitting_details"]] == [True, None]
    assert [row.get("locked") for row in qa["room_feasibility_violations"]] == [True, None]
    untouched = deepcopy(qa)
    mark_locked_rows(untouched, NO_LOCKS)
    assert untouched == qa


# ── status ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "issue, action",
    [
        ({"kind": "room_unavailable"}, True),
        ({"kind": "room_over_capacity"}, True),
        ({"kind": "room_cohort_changed"}, True),
        ({"kind": "registrations_changed", "saved_count": 30, "live_count": 32}, True),
        ({"kind": "registrations_changed", "saved_count": 0, "live_count": 4}, True),
        ({"kind": "registrations_changed", "saved_count": 30, "live_count": 30}, False),
        ({"kind": "registrations_changed", "saved_count": 30, "live_count": 29}, False),
        ({"kind": "clash"}, False),
        ({"kind": "double_booking"}, False),
    ],
)
def test_a_locked_room_that_no_longer_seats_its_students_requires_room_action(issue, action):
    qa = {"manual_override_count": 0, "exam_locks": {"issues": [issue]}}
    assert _lock_room_action(qa) is action
    primary, flags = derive_status_surface({"status": "ok", "schedule": [], "qa": qa})
    assert (primary == "requires_room_action") is action
    assert ("room_action_required" in flags) is action


def test_a_qa_without_a_lock_report_never_requires_room_action_for_locks():
    assert _lock_room_action({}) is False
    assert _lock_room_action({"exam_locks": None}) is False
    assert (
        _lock_room_action(
            {"exam_locks": {"issues": [None, {"kind": "registrations_changed", "live_count": "x"}]}}
        )
        is False
    )


def test_the_module_reports_every_code_it_defines():
    codes = {
        value
        for name, value in vars(exam_locks).items()
        if name.isupper() and isinstance(value, str) and value.startswith("exam_locks_")
    }
    assert codes == {
        "exam_locks_invalid",
        "exam_locks_outside_timetable",
        "exam_locks_repeated",
        "exam_locks_source_required",
        "exam_locks_source_incomplete",
        "exam_locks_scope_changed",
        "exam_locks_term_changed",
        "exam_locks_course_not_selected",
        "exam_locks_link_outside",
        "exam_locks_link_room_shared",
        "exam_locks_pinned_elsewhere",
        "exam_locks_pin_in_locked_cell",
        "exam_locks_moved_out",
        "exam_locks_moved_in",
        "exam_locks_cell_unsaved",
    }

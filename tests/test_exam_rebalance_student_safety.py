"""Invigilator balancing may not be bought with student exam days.

The pass already refuses a move that loses seats or splits a section. It said
nothing about students, so it was free to flatten the staff curve by
concentrating exams onto somebody's day - measured on a real 167-course board,
it drove heavy_day_students from 0 to 63 and max exams/day from 2 to 3 while
doing exactly what it had been asked to do.
"""

import pytest

from core.services import exam_timetable
from core.services.exam_room_allocation import RoomAllocationContext

# Two days, two periods. The second period matters: the harmful move lands on
# the SAME DAY as a shared-cohort exam but in a DIFFERENT SLOT, so the existing
# clash rule cannot catch it and only a student-load guard can.
SLOTS = [
    {"index": 0, "day": "Sun", "period": "08:00-10:00"},
    {"index": 1, "day": "Sun", "period": "13:00-15:00"},
    {"index": 2, "day": "Mon", "period": "08:00-10:00"},
    {"index": 3, "day": "Mon", "period": "13:00-15:00"},
]
# Sun carries three 100-student exams (2 invigilators each) against Mon's single
# 10-student exam (1): 6 versus 1, comfortably past the "already flat" cutoff.
SIZES = {"CS101": 100, "CS102": 100, "CS103": 100, "CS104": 10}
PLACEMENT = [
    ("CS101", "Sun", "08:00-10:00", 0),
    ("CS102", "Sun", "08:00-10:00", 0),
    ("CS103", "Sun", "13:00-15:00", 1),
    ("CS104", "Mon", "08:00-10:00", 2),
]


def _entries():
    return [
        {
            "course_code": code,
            "course_identity": f"{code}:name",
            "day": day,
            "period": period,
            "slot_index": index,
        }
        for code, day, period, index in PLACEMENT
    ]


def _enrollment(entries):
    return {
        entry["course_code"]: [
            {
                "section": "F01",
                "section_key": f"term-section:{index}",
                "term_section_id": index,
                "gender": "F",
                "mapping_status": "mapped",
                "student_count": SIZES[entry["course_code"]],
            }
        ]
        for index, entry in enumerate(entries, 1)
    }


def _pack(schedule, sections, rooms, seed=None, **kwargs):
    """Deterministic rooming: one room per exam, sized to its section."""
    for entry in schedule:
        section = sections[entry["course_code"]][0]
        entry["rooms"] = [
            {
                "room_code": f"{entry['course_code']}-R0",
                "room_capacity": section["student_count"],
                "student_count": section["student_count"],
                "gender": "F",
                "section": section["section"],
                "section_parts": [dict(section)],
            }
        ]
    return schedule


def _run(monkeypatch, enrolled_sets, adj=None):
    """Only CS101 may move, so the result isolates one accept/refuse decision."""
    monkeypatch.setattr(exam_timetable, "assign_rooms_to_schedule", _pack)
    entries = _entries()
    moves = exam_timetable._rebalance_invigilators_pass(
        entries,
        _enrollment(entries),
        [{"room_code": "inventory-present"}],
        SLOTS,
        adj or {},
        {},
        {},
        max_iterations=4,
        pinned_courses={"CS102", "CS103", "CS104"},
        allocation_context=RoomAllocationContext(),
        enrolled_sets=enrolled_sets,
        credit_map=dict.fromkeys(SIZES, 3),
        max_per_day=1,
    )
    return moves, {entry["course_code"]: entry["day"] for entry in entries}


SHARED = {
    "CS101": set(range(100)),
    "CS102": set(range(100, 200)),
    "CS103": set(range(200, 300)),
    "CS104": set(range(10)),  # every one of them also sits CS101
}
DISJOINT = {**SHARED, "CS104": set(range(300, 310))}
# The shared cohort makes CS101 and CS104 genuine conflict neighbours, so the
# clash rule blocks Mon morning and the guard alone decides Mon afternoon.
SHARED_ADJ = {"CS101": {"CS104": 10}, "CS104": {"CS101": 10}}


def test_a_move_that_costs_a_student_an_extra_exam_day_is_refused(monkeypatch):
    """Moving CS101 to Monday afternoon flattens staff and breaks ten students.

    Those ten already sit CS104 on Monday morning. A different slot, so no
    clash - just a second exam in a day against a limit of one.
    """
    moves, days = _run(monkeypatch, SHARED, adj=SHARED_ADJ)
    assert moves == 0, "Staff balance was bought with a student's exam day."
    assert days["CS101"] == "Sun"


def test_the_same_move_is_accepted_when_no_student_pays_for_it(monkeypatch):
    """The control: identical board and identical staff gain, disjoint cohorts.

    Without this, the refusal above could just mean the pass never found a move.
    """
    moves, days = _run(monkeypatch, DISJOINT)
    assert moves == 1, "A move that harms nobody and flattens the load must be taken."
    assert days["CS101"] == "Mon"


def test_the_guard_is_inert_when_a_caller_supplies_no_enrolments(monkeypatch):
    """Callers outside the two production paths keep their old behaviour."""
    moves, days = _run(monkeypatch, None, adj=SHARED_ADJ)
    assert moves == 1
    assert days["CS101"] == "Mon"


def _pass_call_source(func) -> str:
    """The text of func's call to the invigilator pass, to its closing paren."""
    import inspect

    source = inspect.getsource(func)
    start = source.index("_rebalance_invigilators_pass(")
    depth = 0
    for end in range(start + len("_rebalance_invigilators_pass(") - 1, len(source)):
        if source[end] == "(":
            depth += 1
        elif source[end] == ")":
            depth -= 1
            if depth == 0:
                return source[start : end + 1]
    raise AssertionError("unbalanced call source")


@pytest.mark.parametrize("which", ["build", "evaluate"])
def test_every_production_caller_arms_the_student_guard(which):
    """A guard nobody switches on is not a guard.

    The pass still works without ``enrolled_sets`` so non-production callers are
    unaffected - which means a call site that quietly stops passing it degrades
    in silence. Pin both.
    """
    from core.services.exam_evaluation import evaluate_exam_schedule

    call = _pass_call_source(
        exam_timetable.build_exam_timetable if which == "build" else evaluate_exam_schedule
    )
    for argument in ("enrolled_sets=", "credit_map=", "max_per_day="):
        assert argument in call, f"{which} no longer arms the student guard ({argument})"


# ── which caller may move exams ─────────────────────────────────────


def test_save_never_enables_the_post_pass(monkeypatch):
    """Save persists the exact board the registrar is looking at."""
    from core import exam_views

    seen = {}

    def recorder(**kwargs):
        seen.update(kwargs)
        return {"input_fingerprint": "x" * 64, "qa": {}}

    monkeypatch.setattr(exam_views, "evaluate_exam_schedule", recorder)
    monkeypatch.setattr(
        exam_views.ExamTimetableRun.objects,
        "create",
        lambda **kwargs: type("R", (), {"id": 1})(),
    )
    exam_views._rebuild_loaded_schedule(label="Save", days=["Sun"], periods=["08:00-10:00"])
    assert seen["rebalance_invigilators"] is False


def test_optimise_enables_the_post_pass():
    """Optimise re-solves every placement, so it owes them the build post-pass.

    Without this it silently discards the invigilator balancing the original
    build paid for: measured on a real board, day totals went from a spread of
    2 to a spread of 45.
    """
    import inspect

    from core import exam_views

    source = inspect.getsource(exam_views._optimise_loaded_schedule)
    assert "rebalance_invigilators=True" in source


def test_check_never_enables_the_post_pass():
    """A fixed-time Check must report on the board it was handed, not change it."""
    import inspect

    from core import exam_views

    source = inspect.getsource(exam_views.exam_timetable_draft_impact_view)
    assert "rebalance_invigilators" not in source


# ── the metric itself, one dimension at a time ──────────────────────
#
# The accept/refuse tests above exercise the guard end to end, but a harmful
# move trips several metrics at once, so dropping any single one from the tuple
# still leaves the others to catch it. These pin each dimension on its own.


def _board(placement: dict[str, tuple[str, int]]) -> list[dict]:
    return [
        {
            "course_code": code,
            "course_identity": code,
            "day": day,
            "period": "08:00-10:00",
            "slot_index": index,
        }
        for code, (day, index) in placement.items()
    ]


def test_the_signature_reports_every_metric_build_qa_exposes():
    from core.services.exam_timetable import STUDENT_LOAD_METRICS, student_load_signature

    board = _board({"A": ("Sun", 0), "B": ("Sun", 1)})
    signature = student_load_signature(
        {"A": {1}, "B": {1}}, board, max_per_day=2, credit_map={"A": 3, "B": 3}
    )
    assert len(signature) == len(STUDENT_LOAD_METRICS) == 4


@pytest.mark.parametrize(
    "metric,enrolled,before,after,max_per_day,credits",
    [
        # A second student tips over the one-a-day cap. The first was already
        # over, so the worst day is unchanged; credits stay mild throughout.
        (
            "students_over_limit_per_day",
            {"A": {1}, "B": {1}, "C": {2}, "D": {2}},
            {"A": ("Sun", 0), "B": ("Sun", 1), "C": ("Mon", 2), "D": ("Tue", 3)},
            {"A": ("Sun", 0), "B": ("Sun", 1), "C": ("Mon", 2), "D": ("Mon", 3)},
            1,
            {"A": 3, "B": 3, "C": 3, "D": 3},
        ),
        # The already-over student goes from two exams to three. The count of
        # over-limit students cannot rise - it is the same student - and the
        # other student already holds the worst credit day.
        (
            "max_exams_per_day_per_student",
            {"A": {1}, "B": {1}, "E": {1}, "C": {2}, "D": {2}},
            {
                "A": ("Sun", 0),
                "B": ("Sun", 1),
                "E": ("Tue", 3),
                "C": ("Mon", 2),
                "D": ("Mon", 4),
            },
            {
                "A": ("Sun", 0),
                "B": ("Sun", 1),
                "E": ("Sun", 3),
                "C": ("Mon", 2),
                "D": ("Mon", 4),
            },
            1,
            {"A": 3, "B": 3, "E": 3, "C": 5, "D": 4},
        ),
        # A (5,4) day outweighs the existing (4,4) one without being heavy:
        # the pair falls through to the fallback weight, below the threshold.
        (
            "max_credit_load_per_day",
            {"A": {1}, "B": {1}, "C": {2}, "D": {2}},
            {"A": ("Sun", 0), "B": ("Sun", 1), "C": ("Mon", 2), "D": ("Tue", 3)},
            {"A": ("Sun", 0), "B": ("Sun", 1), "C": ("Mon", 2), "D": ("Mon", 3)},
            2,
            {"A": 4, "B": 4, "C": 5, "D": 4},
        ),
        # A second student acquires a (4,4) day. One already had one, so the
        # worst-day credit total and the exam counts are untouched.
        (
            "heavy_day_students",
            {"A": {1}, "B": {1}, "C": {2}, "D": {2}},
            {"A": ("Sun", 0), "B": ("Sun", 1), "C": ("Mon", 2), "D": ("Tue", 3)},
            {"A": ("Sun", 0), "B": ("Sun", 1), "C": ("Mon", 2), "D": ("Mon", 3)},
            2,
            {"A": 4, "B": 4, "C": 4, "D": 4},
        ),
    ],
)
def test_each_metric_moves_on_its_own(metric, enrolled, before, after, max_per_day, credits):
    """Exactly one dimension of the tuple reacts, so dropping it hides the harm."""
    from core.services.exam_timetable import STUDENT_LOAD_METRICS, student_load_signature

    kwargs = {"max_per_day": max_per_day, "credit_map": credits}
    old = student_load_signature(enrolled, _board(before), **kwargs)
    new = student_load_signature(enrolled, _board(after), **kwargs)
    index = STUDENT_LOAD_METRICS.index(metric)
    assert new[index] > old[index], f"{metric} should have worsened: {old} -> {new}"
    others = [i for i in range(len(old)) if i != index]
    assert [new[i] for i in others] == [old[i] for i in others], (
        f"{metric} was not isolated: {old} -> {new}"
    )


# ── the guard's baseline must track the board, not the starting board ──

# Six exams on Sunday (2 invigilators each) against one small Monday exam and
# two on Tuesday, so the pass wants two separate moves onto Monday.
TWO_MOVE_SLOTS = [
    {"index": 0, "day": "Sun", "period": "08:00-10:00"},
    {"index": 1, "day": "Sun", "period": "13:00-15:00"},
    {"index": 2, "day": "Mon", "period": "08:00-10:00"},
    {"index": 3, "day": "Mon", "period": "13:00-15:00"},
    {"index": 4, "day": "Tue", "period": "08:00-10:00"},
    {"index": 5, "day": "Tue", "period": "13:00-15:00"},
]
TWO_MOVE_BOARD = [
    ("CS101", 0),
    ("CS102", 0),
    ("CS108", 0),  # Sunday morning
    ("CS103", 1),
    ("CS106", 1),
    ("CS109", 1),  # Sunday afternoon
    ("CS104", 2),  # Monday morning, small
    ("CS105", 4),
    ("CS107", 5),  # Tuesday
]
TWO_MOVE_SIZES = {code: (10 if code == "CS104" else 100) for code, _ in TWO_MOVE_BOARD}
# X sits CS101+CS103, both heavy, both on Sunday: moving CS101 away IMPROVES
# the student signature. Y sits CS102+CS104, and landing CS102 on Monday
# recreates exactly the harm the first move removed.
TWO_MOVE_ENROLLED = {
    "CS101": {1},
    "CS103": {1},
    "CS102": {2},
    "CS104": {2},
    "CS106": {3},
    "CS108": {4},
    "CS109": {5},
    "CS105": {6},
    "CS107": {7},
}
TWO_MOVE_CREDITS = {
    code: (4 if code in {"CS101", "CS102", "CS103", "CS104"} else 1) for code in TWO_MOVE_SIZES
}
TWO_MOVE_ADJ = {
    "CS101": {"CS103": 1},
    "CS103": {"CS101": 1},
    "CS102": {"CS104": 1},
    "CS104": {"CS102": 1},
}


def _two_move_entries():
    slot_of = {slot["index"]: slot for slot in TWO_MOVE_SLOTS}
    return [
        {
            "course_code": code,
            "course_identity": f"{code}:name",
            "day": slot_of[index]["day"],
            "period": slot_of[index]["period"],
            "slot_index": index,
        }
        for code, index in TWO_MOVE_BOARD
    ]


def _run_two_move_board(monkeypatch, enrolled_sets):
    monkeypatch.setattr(exam_timetable, "assign_rooms_to_schedule", _pack)
    entries = _two_move_entries()
    enrollment = {
        entry["course_code"]: [
            {
                "section": "F01",
                "section_key": f"term-section:{index}",
                "term_section_id": index,
                "gender": "F",
                "mapping_status": "mapped",
                "student_count": TWO_MOVE_SIZES[entry["course_code"]],
            }
        ]
        for index, entry in enumerate(entries, 1)
    }
    exam_timetable._rebalance_invigilators_pass(
        entries,
        enrollment,
        [{"room_code": "inventory-present"}],
        TWO_MOVE_SLOTS,
        TWO_MOVE_ADJ,
        {},
        {},
        max_iterations=20,
        pinned_courses=set(TWO_MOVE_SIZES) - {"CS101", "CS102"},
        allocation_context=RoomAllocationContext(),
        enrolled_sets=enrolled_sets,
        credit_map=TWO_MOVE_CREDITS,
        max_per_day=2,
    )
    return {entry["course_code"]: entry["day"] for entry in entries}


def test_the_guard_compares_against_the_current_board_not_the_starting_one(monkeypatch):
    """A move that gives back an earlier move's gain must be refused.

    The first accepted move takes a heavy exam day away from one student.
    Sending CS102 to Monday would hand an identical heavy day to another.
    Against the board as it now stands that is a regression; against the board
    the pass STARTED from it merely breaks even - so a baseline that never
    advances lets the optimisation walk straight back to where it began.

    Refusing it does not end the search: the pass moves on to the next day
    pair and places CS102 somewhere that costs nobody anything.
    """
    unguarded = _run_two_move_board(monkeypatch, None)
    guarded = _run_two_move_board(monkeypatch, TWO_MOVE_ENROLLED)

    assert unguarded["CS102"] == "Mon", (
        "Without the guard the harmful move is both available and attractive; "
        "if it is not taken this test proves nothing about the guard."
    )
    assert guarded["CS101"] == "Mon", "The improving first move should have been taken."
    assert guarded["CS102"] != "Mon", "The guard must refuse the move that undoes it."
    # And refusing it must not end the optimisation. Sunday/Monday is exhausted,
    # so the search moves to the next-widest pair and places CS102 on Tuesday,
    # which flattens the load and costs no student anything. Before the search
    # escalated, one refusal abandoned the whole pass and CS102 stayed put.
    assert guarded["CS102"] == "Tue", "A refused pair must not end the search."


def test_the_search_is_bounded_by_work_not_by_the_clock(monkeypatch):
    """A wall-bounded search would publish a different board on a slower host.

    That is the failure PR #110 was written about, so the escalating pair
    search carries its own deterministic trial budget and the wall deadline
    stays a backstop.
    """
    monkeypatch.setattr(exam_timetable, "assign_rooms_to_schedule", _pack)
    entries = _two_move_entries()
    enrollment = {
        entry["course_code"]: [
            {
                "section": "F01",
                "section_key": f"term-section:{index}",
                "term_section_id": index,
                "gender": "F",
                "mapping_status": "mapped",
                "student_count": TWO_MOVE_SIZES[entry["course_code"]],
            }
        ]
        for index, entry in enumerate(entries, 1)
    }
    moves = exam_timetable._rebalance_invigilators_pass(
        entries,
        enrollment,
        [{"room_code": "inventory-present"}],
        TWO_MOVE_SLOTS,
        TWO_MOVE_ADJ,
        {},
        {},
        max_iterations=20,
        max_trials=1,
        pinned_courses=set(TWO_MOVE_SIZES) - {"CS101", "CS102"},
        allocation_context=RoomAllocationContext(),
        enrolled_sets=TWO_MOVE_ENROLLED,
        credit_map=TWO_MOVE_CREDITS,
        max_per_day=2,
    )
    assert moves <= 1, "One trial cannot produce two accepted moves."


# ── the trial budget must track the deadline it has to finish inside ──


@pytest.mark.parametrize(
    "budget,expected",
    [
        (55.0, 77),  # 15 days x 3 periods: converges at 67, so nothing is lost
        (46.0, 64),  # 12 days: a flat cap of 120 ran this until the wall stopped it
        (34.0, 47),  # 8 days: the densest board gets the smallest search
        (0.0, 30),  # no deadline reported -> the floor, never unbounded
        (10_000.0, 120),
    ],
)
def test_the_trial_budget_scales_with_the_rooming_deadline(budget, expected):
    """A flat cap is tuned on the easiest board and lets denser ones truncate.

    search_budget_seconds sizes on period/cohorts, which shrinks as the exam
    period shortens while the work grows, so the shortest boards get both the
    smallest deadline and the most to do.
    """
    assert exam_timetable.derive_trial_budget(budget) == expected


def test_a_pass_with_no_context_still_bounds_itself(monkeypatch):
    monkeypatch.setattr(exam_timetable, "assign_rooms_to_schedule", _pack)
    entries = _entries()
    exam_timetable._rebalance_invigilators_pass(
        entries,
        _enrollment(entries),
        [{"room_code": "inventory-present"}],
        SLOTS,
        {},
        {},
        {},
        pinned_courses={"CS102", "CS103", "CS104"},
        enrolled_sets=SHARED,
        credit_map=dict.fromkeys(SIZES, 3),
        max_per_day=1,
    )  # must not raise: max_trials is derived, not required


def test_the_trial_count_it_reports_is_the_trial_count_it_spent(monkeypatch, caplog):
    """A job shows "trial N of at most M" from this counter. It must end on the
    number the pass itself logs, and count up one trial at a time."""
    import logging

    ticks = []
    monkeypatch.setattr(exam_timetable, "assign_rooms_to_schedule", _pack)
    entries = _two_move_entries()
    enrollment = {
        entry["course_code"]: [
            {
                "section": "F01",
                "section_key": f"term-section:{index}",
                "term_section_id": index,
                "gender": "F",
                "mapping_status": "mapped",
                "student_count": TWO_MOVE_SIZES[entry["course_code"]],
            }
        ]
        for index, entry in enumerate(entries, 1)
    }
    with caplog.at_level(logging.INFO, logger="core.services.exam_timetable"):
        exam_timetable._rebalance_invigilators_pass(
            entries,
            enrollment,
            [{"room_code": "inventory-present"}],
            TWO_MOVE_SLOTS,
            TWO_MOVE_ADJ,
            {},
            {},
            max_iterations=20,
            pinned_courses=set(TWO_MOVE_SIZES) - {"CS101", "CS102"},
            allocation_context=RoomAllocationContext(),
            enrolled_sets=TWO_MOVE_ENROLLED,
            credit_map=TWO_MOVE_CREDITS,
            max_per_day=2,
            on_trial=lambda done, total: ticks.append((done, total)),
        )
    summary = next(r for r in caplog.records if "invigilator rebalance" in r.getMessage())
    spent, ceiling = summary.args[3], summary.args[4]
    assert spent > 0, "The board must make the pass try at least one move"
    assert ticks == [(trial, ceiling) for trial in range(1, spent + 1)]

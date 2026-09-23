"""The minimum-change repair, end to end through the HTTP view.

Every other test of this mode stubs the solver inputs and the save. Three
mutations survived that: keying the saved placements by identity, emptying
them, and the view never recognising the mode at all - the last of which saved
the clashing board and reported it as repaired. These drive the real request.
"""

from copy import deepcopy

import pytest
from django.urls import reverse

from core.models import Course, ExamTimetableRun, ProgrammeRequirement, Student, StudentCourse
from core.services import exam_timetable
from tests.exam_source_factory import scraped_exam_registration
from tests.test_exam_export_source import PROGRAMMING
from tests.test_exam_export_source import export_client as export_client
from tests.test_exam_export_source import export_courses as export_courses

pytestmark = pytest.mark.django_db
DAYS = ["Sun", "Mon", "Tue"]
PERIODS = ["08:00-10:00", "13:00-15:00"]
SCOPE = {"programs": ["AI", "AI2"], "sections": ["F", "M"]}


def _post(client, payload, status=200):
    response = client.post(
        reverse("exam_timetable_build"), payload, content_type="application/json"
    )
    assert response.status_code == status, response.content
    return response.json()


@pytest.fixture
def clashing_pair(export_courses):  # noqa: F811 - the shared fixture seeds CS111.
    """A companion sharing both students and a plan term with Programming I.

    Dragging it onto Programming I breaks both hard rules at once: the same
    students sit two exams in one slot, and one (AI, term 1) bucket gets two
    exams on one day.
    """
    companion = Course.objects.create(course_code="EX222", credit_hours=3)
    ProgrammeRequirement.objects.create(
        program="AI", course_code="EX222", course_name="Second exam", programme_term=1
    )
    for student in Student.objects.filter(program="AI"):
        StudentCourse.objects.create(student=student, course=companion, status="studying")
        scraped_exam_registration(student, companion)
    _, metadata = exam_timetable.build_enrolled_sets_with_meta(**SCOPE)
    codes = {entry["course_name"]: code for code, entry in metadata.items()}
    return metadata, codes[PROGRAMMING], codes["Second exam"]


def _build(client, metadata, periods=PERIODS):
    return _post(
        client,
        {
            "label": "Repair end to end",
            "days": DAYS,
            "periods": periods,
            "max_per_day": 2,
            **SCOPE,
            "assign_rooms": False,
            "selected_courses": sorted(metadata),
            "selected_course_entries": [{"course_code": c, **e} for c, e in metadata.items()],
            "pinned": [],
        },
    )


def _drag(board, code, onto):
    moved = deepcopy(board)
    target = next(entry for entry in moved if entry["course_code"] == onto)
    for entry in moved:
        if entry["course_code"] == code:
            entry.update(day=target["day"], period=target["period"])
    return moved


def _repair(
    client, built, board, *, periods=PERIODS, mode="minimum_change_repair", status=200, pinned=None
):
    return _post(
        client,
        {
            "label": built["label"] if "label" in built else "Repair end to end",
            "days": DAYS,
            "periods": periods,
            "max_per_day": 2,
            **SCOPE,
            "mode": mode,
            "previous_run_id": built["run_id"],
            "base_schedule": board,
            "pinned": pinned or [],
        },
        status=status,
    )


def _check(client, built, board, *, periods=PERIODS):
    response = client.post(
        reverse("exam_timetable_draft_impact"),
        {
            "days": DAYS,
            "periods": periods,
            "max_per_day": 2,
            **SCOPE,
            "previous_run_id": built["run_id"],
            "base_schedule": board,
            "pinned": [],
        },
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response.json()


def _comparable(qa):
    """qa as the page compares it (reportSignature): how an earlier optimiser
    reached its board, and timestamps, are not part of the comparison."""

    def stable(value):
        if isinstance(value, list):
            return [stable(item) for item in value]
        if isinstance(value, dict):
            return {
                key: stable(item)
                for key, item in sorted(value.items())
                if key not in {"snapshot_timestamp", "created_at", "generated_at"}
            }
        return value

    return stable({key: item for key, item in qa.items() if key != "rebalance_moves"})


def _at(result, code):
    entry = next(e for e in result["schedule"] if e["course_code"] == code)
    return entry["day"], entry["period"]


def test_a_drag_that_breaks_both_rules_is_repaired_by_moving_the_other_exam(
    export_client,  # noqa: F811 - the imported shared fixture
    clashing_pair,
):
    metadata, programming, companion = clashing_pair
    built = _build(export_client, metadata)
    dragged = _drag(built["schedule"], companion, onto=programming)
    target = _at({"schedule": dragged}, companion)

    repaired = _repair(export_client, built, dragged)

    assert _at(repaired, companion) == target, "The registrar's drag must survive"
    assert _at(repaired, programming)[0] != target[0], "Bucket-mates may not share a day"
    report = repaired["minimum_change"]
    assert [move["course_code"] for move in report["moves"]] == [programming]
    assert report["violations_before"] >= 1
    assert report["violations_after"] == 0
    assert repaired["rebuild_mode"] == "minimum_change_from_loaded"
    assert "minimum_change" not in repaired["qa"], "qa is compared between Build and Check"


def test_the_repair_report_survives_a_reload_from_history(export_client, clashing_pair):  # noqa: F811
    metadata, programming, companion = clashing_pair
    built = _build(export_client, metadata)
    repaired = _repair(export_client, built, _drag(built["schedule"], companion, onto=programming))

    reloaded = export_client.get(reverse("exam_timetable_detail", args=[repaired["run_id"]])).json()
    assert reloaded["minimum_change"]["moves"][0]["course_code"] == programming
    assert companion in reloaded["minimum_change_protected"]


def test_an_action_the_server_does_not_recognise_is_refused_not_saved(
    export_client,  # noqa: F811 - the imported shared fixture
    clashing_pair,
):
    """Save used to be the fallthrough, so an unknown action persisted the
    clashing board and the page called it repaired."""
    metadata, programming, companion = clashing_pair
    built = _build(export_client, metadata)
    before = ExamTimetableRun.objects.count()
    _repair(
        export_client,
        built,
        _drag(built["schedule"], companion, onto=programming),
        mode="minimum_change_repiar",
        status=400,
    )
    assert ExamTimetableRun.objects.count() == before


def test_adding_a_period_does_not_turn_every_exam_into_the_registrars(
    export_client,  # noqa: F811 - the imported shared fixture
    clashing_pair,
):
    """Slot numbers shift when a period is added before them. Comparing by slot
    number made nearly the whole board look hand-moved, froze it, and left the
    clash unrepaired."""
    metadata, programming, companion = clashing_pair
    built = _build(export_client, metadata)
    widened = ["07:00-08:00", *PERIODS]
    dragged = _drag(built["schedule"], companion, onto=programming)

    repaired = _repair(export_client, built, dragged, periods=widened)

    report = repaired["minimum_change"]
    assert report["protected_count"] == 1, "Only the dragged exam is the registrar's"
    assert report["violations_after"] == 0


def test_a_second_repair_keeps_the_first_drag_where_the_registrar_put_it(
    export_client,  # noqa: F811 - the imported shared fixture
    clashing_pair,
):
    """Each repair saves a run that becomes the next baseline. Without carrying
    the protection forward, drag, Fix, drag, Fix moved the first drag."""
    metadata, programming, companion = clashing_pair
    built = _build(export_client, metadata)
    first = _repair(export_client, built, _drag(built["schedule"], companion, onto=programming))
    kept = _at(first, companion)

    assert companion in first["minimum_change_protected"]
    before = ExamTimetableRun.objects.count()

    # Drag Programming I back onto the companion. Both are now the registrar's:
    # the companion by the carried protection, Programming I by this drag. Only
    # the carried protection stops the repair from moving the companion.
    second = _repair(export_client, first, _drag(first["schedule"], programming, onto=companion))

    report = second["minimum_change"]
    assert report["moves"] == [], "The first drag was undone by the second repair"
    assert report["protected_count"] == 2
    assert report["violations_after"] >= 1, "The clash is between the registrar's own exams"
    assert second["saved"] is False
    assert ExamTimetableRun.objects.count() == before
    assert _at(first, companion) == kept


def test_a_malformed_pin_is_a_validation_error_not_a_server_error(
    export_client,  # noqa: F811 - the imported shared fixture
    clashing_pair,
):
    metadata, programming, companion = clashing_pair
    built = _build(export_client, metadata)
    _repair(
        export_client,
        built,
        _drag(built["schedule"], companion, onto=programming),
        pinned=[{"day": "Sun"}],
        status=400,
    )


def test_a_repaired_run_reads_as_unchanged_to_a_check_of_the_same_board(
    export_client,  # noqa: F811 - the imported shared fixture
    clashing_pair,
):
    """The page compares a saved run's qa with a Check of the same board, and a
    difference marks the run as changed and blocks the XLSX export. The repair
    report lives beside qa, never in it. A Check here also runs the loaded
    context through the other consumer that must strip its provenance - a key
    left in once made every Check a server error."""
    metadata, programming, companion = clashing_pair
    built = _build(export_client, metadata)
    repaired = _repair(export_client, built, _drag(built["schedule"], companion, onto=programming))

    checked = _check(export_client, repaired, repaired["schedule"])

    assert _comparable(repaired["qa"]) == _comparable(checked["qa"])
    assert "minimum_change" not in checked


def test_a_board_with_nothing_to_repair_is_not_saved_again(
    export_client,  # noqa: F811 - the imported shared fixture
    clashing_pair,
):
    metadata, _, _ = clashing_pair
    built = _build(export_client, metadata)
    before = ExamTimetableRun.objects.count()

    result = _repair(export_client, built, built["schedule"])

    assert result["saved"] is False
    assert result["minimum_change"]["moves"] == []
    assert result["minimum_change"]["violations_before"] == 0
    assert ExamTimetableRun.objects.count() == before


def test_an_exam_brought_back_from_overflow_is_the_registrars(
    export_client,  # noqa: F811 - the imported shared fixture
    clashing_pair,
):
    """The exam a registrar most often places by hand is one parked in OVERFLOW.
    Overflow origins were once dropped from the saved placements, so dragging
    one onto the board read as "not moved" and the repair moved it straight
    back off. The companion's code sorts after Programming I's, so the
    tie-break alone would move the companion."""
    metadata, programming, companion = clashing_pair
    assert companion > programming
    built = _build(export_client, metadata)
    parked = deepcopy(built["schedule"])
    for entry in parked:
        if entry["course_code"] == companion:
            entry.update(day="OVERFLOW", period="Extra-1", slot_index=len(DAYS) * len(PERIODS))
    saved = _post(
        export_client,
        {
            "label": "Companion parked",
            "days": DAYS,
            "periods": PERIODS,
            "max_per_day": 2,
            **SCOPE,
            "mode": "save_loaded_changes",
            "previous_run_id": built["run_id"],
            "base_schedule": parked,
            "expected_input_fingerprint": built["input_fingerprint"],
            "pinned": [],
        },
    )
    assert _at(saved, companion)[0] == "OVERFLOW"
    target = _at(saved, programming)

    repaired = _repair(export_client, saved, _drag(saved["schedule"], companion, onto=programming))

    assert _at(repaired, companion) == target, "The exam placed by hand was moved"
    assert [move["course_code"] for move in repaired["minimum_change"]["moves"]] == [programming]

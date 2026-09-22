"""The rooming wall budget must be sized by the work, not by a flat constant.

A wall stop never yields a partial allocation — it raises — so the budget
decides only whether a rooming phase may finish, never which allocation it
produces. A flat six seconds was smaller than the measured cost of a real
fifteen-day build, so production rejected valid timetables outright and the
optional invigilator pass was silently truncated even on a fast host.
"""

import os
import re
import time
from pathlib import Path

import pytest
from django.test import override_settings

from core.services import exam_room_allocation as allocation
from core.services import exam_timetable
from core.services.exam_timetable import assign_rooms_to_schedule, period_cohort_count

# Cold-cache wall cost of the full 15-day / 3-period / 12-programme build
# (172 courses, 747 sections, 71 rooms, ~90 period/cohort allocations),
# measured on a developer workstation. Production runs a 0.5-CPU instance.
MEASURED_ROOMING_SECONDS = 11.0
SLOWEST_SUPPORTED_HOST_FACTOR = 3


@pytest.fixture(autouse=True)
def isolated_cache():
    """Load-bearing, not hygiene.

    ``allocate_period`` counts a period only on a cache miss, and the fixtures
    below deliberately share course codes, so a leaked cache turns the
    ``periods_solved`` assertions into zeroes depending on test order.
    """
    with allocation._CACHE_LOCK:
        allocation._CACHE.clear()
    yield
    with allocation._CACHE_LOCK:
        allocation._CACHE.clear()


# ── the budget itself ───────────────────────────────────────────────


@override_settings(
    EXAM_ROOM_BASE_SEARCH_SECONDS=10.0,
    EXAM_ROOM_PERIOD_SEARCH_SECONDS=0.5,
    EXAM_ROOM_MAX_SEARCH_SECONDS=600.0,
)
def test_budget_grows_linearly_with_the_periods_a_pack_must_solve():
    assert allocation.search_budget_seconds(2) == pytest.approx(11.0)
    assert allocation.search_budget_seconds(60) == pytest.approx(40.0)


def test_budget_covers_a_full_fifteen_day_build_on_the_slowest_supported_host():
    """90 period/cohort allocations is a real 15-day, 3-period, 2-cohort build."""
    budget = allocation.search_budget_seconds(90)
    assert budget > 6.0, "The flat budget that production rejected this build with."
    assert budget >= MEASURED_ROOMING_SECONDS * SLOWEST_SUPPORTED_HOST_FACTOR


def test_budget_stays_inside_the_deployed_worker_timeout():
    """The ceiling exists to keep one rooming phase inside the worker timeout."""
    procfile = Path(__file__).resolve().parent.parent / "Procfile"
    timeout = re.search(r"--timeout (\d+)", procfile.read_text(encoding="utf-8"))
    assert timeout, "Procfile no longer declares a worker timeout to size against."
    assert allocation.search_budget_seconds(10_000) < int(timeout.group(1))


@override_settings(
    EXAM_ROOM_BASE_SEARCH_SECONDS=4.0,
    EXAM_ROOM_PERIOD_SEARCH_SECONDS=0.25,
    EXAM_ROOM_MAX_SEARCH_SECONDS=90.0,
)
def test_budget_is_tunable_per_deployment_without_a_code_change():
    assert allocation.search_budget_seconds(8) == pytest.approx(6.0)
    assert allocation.search_budget_seconds(1000) == pytest.approx(90.0)


@pytest.mark.parametrize("junk", ["not-a-number", None, True, False, [], object()])
def test_an_unusable_override_falls_back_to_the_shipped_default(junk):
    with override_settings(EXAM_ROOM_BASE_SEARCH_SECONDS=junk):
        assert allocation.search_budget_seconds(0) == allocation._BASE_SEARCH_SECONDS


@override_settings(EXAM_ROOM_MAX_SEARCH_SECONDS=0.0)
def test_budget_never_collapses_to_an_already_spent_deadline():
    assert allocation.search_budget_seconds(50) >= 1.0


@pytest.mark.parametrize(
    "base,per_period,expected",
    [(10.0, -5.0, 10.0), (-50.0, 0.5, 50.0)],
    ids=["negative-rate", "negative-base"],
)
def test_a_negative_override_subtracts_nothing_from_the_budget(base, per_period, expected):
    """Unclamped, a negative term makes the budget SHRINK as the problem grows."""
    with override_settings(
        EXAM_ROOM_BASE_SEARCH_SECONDS=base,
        EXAM_ROOM_PERIOD_SEARCH_SECONDS=per_period,
        EXAM_ROOM_MAX_SEARCH_SECONDS=600.0,
    ):
        assert allocation.search_budget_seconds(100) == pytest.approx(expected)


def test_for_periods_arms_a_deadline_matching_the_sized_budget():
    before = time.monotonic()
    context = allocation.RoomAllocationContext.for_periods(40)
    after = time.monotonic()
    budget = allocation.search_budget_seconds(40)
    assert before + budget <= context.deadline <= after + budget
    assert context.budget_seconds == budget, "A timeout must be able to report its budget."


# ── counting the work ───────────────────────────────────────────────


def _schedule(slot_count: int) -> tuple[list[dict], dict, list[dict]]:
    schedule = [
        {
            "course_code": f"C{index}",
            "course_identity": f"C{index}",
            "slot_index": index,
            "day": f"D{index}",
            "period": "08:00-10:00",
        }
        for index in range(slot_count)
    ]
    sections = {
        f"C{index}": [
            {"section": "M1", "section_key": f"C{index}M", "student_count": 10, "gender": "M"},
            {"section": "F1", "section_key": f"C{index}F", "student_count": 10, "gender": "F"},
        ]
        for index in range(slot_count)
    }
    rooms = [
        {"room_code": "M1", "capacity": 40, "section": "M"},
        {"room_code": "F1", "capacity": 40, "section": "F"},
    ]
    return schedule, sections, rooms


def test_period_cohort_count_is_slots_times_cohorts_and_ignores_overflow():
    schedule, sections, _ = _schedule(4)
    assert period_cohort_count(schedule, sections) == 8
    schedule.append(
        {"course_code": "X", "course_identity": "X", "slot_index": 99, "day": "OVERFLOW"}
    )
    assert period_cohort_count(schedule, sections) == 8, "An unscheduled course costs no search."


def test_period_cohort_count_survives_an_empty_enrollment():
    schedule, _, _ = _schedule(3)
    assert period_cohort_count(schedule, {}) == 3


@pytest.mark.parametrize(
    "genders,expected",
    [
        (["M", "m"], 3),
        (["M", None], 6),
        (["M", "X"], 6),
    ],
    ids=["case-folds-like-the-pack", "missing-is-its-own-cohort", "a-third-cohort"],
)
def test_period_cohort_count_normalises_cohorts_the_way_the_pack_groups_them(genders, expected):
    """The pack groups by ``str(gender or "U").upper()``; the count must agree."""
    schedule, sections, _ = _schedule(3)
    for code in sections:
        for section, gender in zip(sections[code], genders, strict=True):
            section["gender"] = gender
    assert period_cohort_count(schedule, sections) == expected


# ── who arms the deadline ───────────────────────────────────────────


def test_a_pack_without_a_caller_context_sizes_its_own_deadline(monkeypatch):
    """The Check path builds its own context; it must not inherit a flat budget."""
    seen: list[int] = []
    original = allocation.RoomAllocationContext.for_periods

    def recording(period_cohorts):
        seen.append(period_cohorts)
        return original(period_cohorts)

    monkeypatch.setattr(allocation.RoomAllocationContext, "for_periods", recording)
    schedule, sections, rooms = _schedule(12)
    assign_rooms_to_schedule(schedule, sections, rooms)
    assert seen == [24], "The pack must size its budget from the periods it will solve."


def test_a_caller_supplied_context_is_the_one_the_pack_spends():
    """A build shares one deadline across its pack and every rebalance repack.

    Substituting a freshly armed context would hand each repack a whole new
    budget and leave the request itself unbounded.
    """
    schedule, sections, rooms = _schedule(3)
    context = allocation.RoomAllocationContext(deadline=time.monotonic() + 30)
    deadline = context.deadline
    assign_rooms_to_schedule(schedule, sections, rooms, allocation_context=context)
    assert context.deadline == deadline, "A shared build deadline must stay shared."
    assert context.periods_solved == 6, "The caller context must record the work spent on it."


# ── the optional invigilator pass ───────────────────────────────────


def _rebalance_fixture():
    entries = [
        {
            "course_code": code,
            "course_identity": f"{code}:name",
            "day": day,
            "period": "08:00-10:00",
            "slot_index": index,
        }
        for code, day, index in [("CS101", "Sun", 0), ("CS102", "Sun", 0), ("CS104", "Mon", 1)]
    ]
    enrollment = {
        entry["course_code"]: [
            {
                "section": "F01",
                "section_key": f"term-section:{index}",
                "term_section_id": index,
                "gender": "F",
                "mapping_status": "mapped",
                "student_count": 10 if entry["course_code"] == "CS104" else 40,
            }
        ]
        for index, entry in enumerate(entries, 1)
    }
    slots = [
        {"index": index, "day": day, "period": "08:00-10:00"}
        for index, day in enumerate(["Sun", "Mon"])
    ]
    return entries, enrollment, slots


def _seated_pack(schedule, sections, rooms, seed=None, **kwargs):
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


def _run_rebalance(entries, enrollment, slots, context=None):
    return exam_timetable._rebalance_invigilators_pass(
        entries,
        enrollment,
        [{"room_code": "inventory-present"}],
        slots,
        {},
        {},
        {},
        max_iterations=1,
        pinned_courses={"CS102", "CS104"},
        allocation_context=context,
    )


@pytest.mark.parametrize("exhaust", [True, False], ids=["deadline-elapsed", "deadline-intact"])
def test_a_truncated_invigilator_pass_records_it_for_the_log(monkeypatch, exhaust):
    """The pass is optional, so it absorbs the timeout — but it must not stay silent."""
    entries, enrollment, slots = _rebalance_fixture()

    def pack(schedule, sections, rooms, seed=None, **kwargs):
        moving = next(entry for entry in schedule if entry["course_code"] == "CS101")
        if exhaust and moving["day"] == "Mon":
            raise allocation.RoomAllocationTimeout("The shared room allocation deadline elapsed.")
        return _seated_pack(schedule, sections, rooms, seed=seed, **kwargs)

    monkeypatch.setattr(exam_timetable, "assign_rooms_to_schedule", pack)
    context = allocation.RoomAllocationContext(deadline=time.monotonic() + 60)
    assert context.truncated is False
    _run_rebalance(entries, enrollment, slots, context)
    assert context.truncated is exhaust


def test_an_optional_pass_never_destroys_the_rooming_the_build_already_earned(monkeypatch):
    """The opening repack can exhaust a shared deadline on its own.

    A concurrent build evicting this one cache entries is enough. That must
    not fail a build whose mandatory pack already succeeded, and it must not
    leave the schedule holding the cleared rows the failed repack wrote.
    """
    entries, enrollment, slots = _rebalance_fixture()
    _seated_pack(entries, enrollment, None)
    earned = [list(entry["rooms"]) for entry in entries]

    def pack(schedule, sections, rooms, seed=None, **kwargs):
        for entry in schedule:
            entry["rooms"] = [{"room_code": "partial-result", "student_count": 999}]
        raise allocation.RoomAllocationTimeout("The shared room allocation deadline elapsed.")

    monkeypatch.setattr(exam_timetable, "assign_rooms_to_schedule", pack)
    context = allocation.RoomAllocationContext(deadline=time.monotonic() + 60)
    assert _run_rebalance(entries, enrollment, slots, context) == 0
    assert [entry["rooms"] for entry in entries] == earned
    assert context.truncated is True


def test_the_optional_pass_sizes_its_own_fallback_deadline_too(monkeypatch):
    """Called without a context it must size one, not fall back to the base budget."""
    seen: list[int] = []
    original = allocation.RoomAllocationContext.for_periods

    def recording(period_cohorts):
        seen.append(period_cohorts)
        return original(period_cohorts)

    monkeypatch.setattr(allocation.RoomAllocationContext, "for_periods", recording)
    monkeypatch.setattr(exam_timetable, "assign_rooms_to_schedule", _seated_pack)
    entries, enrollment, slots = _rebalance_fixture()
    _run_rebalance(entries, enrollment, slots)
    assert seen == [2], "Two occupied slots, one cohort."


def test_every_repack_in_one_pass_spends_the_same_shared_deadline(monkeypatch):
    seen: list[tuple[int, float]] = []
    entries, enrollment, slots = _rebalance_fixture()

    def pack(schedule, sections, rooms, seed=None, **kwargs):
        context = kwargs["allocation_context"]
        seen.append((id(context), context.deadline))
        return _seated_pack(schedule, sections, rooms, seed=seed)

    monkeypatch.setattr(exam_timetable, "assign_rooms_to_schedule", pack)
    context = allocation.RoomAllocationContext(deadline=time.monotonic() + 60)
    _run_rebalance(entries, enrollment, slots, context)
    assert len(seen) > 1, "The pass must actually have repacked more than once."
    assert len(set(seen)) == 1, "A fresh budget per repack leaves the request unbounded."


@pytest.mark.parametrize("raw", ["", "   ", "45s", "none"])
def test_a_malformed_env_budget_cannot_stop_the_site_booting(monkeypatch, raw):
    """A dashboard field cleared mid-incident must degrade, not crash at import."""
    from config.settings import _float_env

    monkeypatch.setitem(os.environ, "EXAM_ROOM_MAX_SEARCH_SECONDS", raw)
    assert _float_env("EXAM_ROOM_MAX_SEARCH_SECONDS", "60") == 60.0

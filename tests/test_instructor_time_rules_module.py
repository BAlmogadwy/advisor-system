"""The instructor teaching-time rule semantics (core/services/instructor_availability.py).

The heart of the feature is the interval algebra: union of ``unavailable``
windows, the complement of ``available_only`` windows, half-open overlap. It is
proven three ways: hand-written edge cases, a property test against an
independent minute-by-minute oracle over many seeded random rule sets, and the
parity fixture the staff editor's JavaScript mirror also runs.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from core.models import Instructor, InstructorTimeRule
from core.services.instructor_availability import (
    DAY_CODES,
    EMPTY_AVAILABILITY,
    KIND_AVAILABLE_ONLY,
    KIND_UNAVAILABLE,
    MINUTES_PER_DAY,
    TimeRule,
    compile_rules,
    day_index,
    days_from_mask,
    default_period_grid,
    load_instructor_availability,
    load_time_rules,
    mask_from_days,
    open_periods,
    open_summary,
    rules_revision,
    to_hhmm,
    to_minutes,
)

CASES = Path(__file__).resolve().parent / "fixtures" / "instructor_availability_cases.json"
ALL = 31


def _rule(kind: str, mask: int, start: int, end: int, instructor_id: int = 1) -> TimeRule:
    return TimeRule(instructor_id, kind, mask, start, end)


def _off(mask: int, start: int, end: int, instructor_id: int = 1) -> TimeRule:
    return _rule(KIND_UNAVAILABLE, mask, start, end, instructor_id)


def _only(mask: int, start: int, end: int, instructor_id: int = 1) -> TimeRule:
    return _rule(KIND_AVAILABLE_ONLY, mask, start, end, instructor_id)


# ── Values ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("SUN", 0),
        ("sun ", 0),
        (" Thu", 4),
        ("Sunday", 0),
        ("wednesday", 3),
        (0, 0),
        (4, 4),
        (5, None),
        (-1, None),
        (True, None),
        ("FRI", None),
        ("Sunflower", None),
        ("", None),
        (None, None),
        (1.0, None),
    ],
)
def test_day_index_reads_codes_names_and_indexes(value, expected):
    assert day_index(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("00:00", 0),
        ("09:00", 540),
        ("9:05", 545),
        (" 13:30 ", 810),
        ("23:59", 1439),
        ("10:15:00", 615),
        ("24:00", 1440),
        ("24:00:00", 1440),
        (0, 0),
        (1440, 1440),
        (1441, None),
        (-1, None),
        (True, None),
        ("24:01", None),
        ("25:00", None),
        ("12:60", None),
        ("1200", None),
        ("", None),
        (None, None),
        # Arabic-Indic digits are the page's job to normalise; the server reads ASCII only.
        (chr(0x660) + chr(0x669) + ":30", None),
    ],
)
def test_to_minutes_reads_ascii_times_and_the_end_of_day(value, expected):
    assert to_minutes(value) == expected


def test_to_hhmm_formats_both_ends_of_the_day():
    assert [to_hhmm(m) for m in (0, 545, 1439, 1440)] == ["00:00", "09:05", "23:59", "24:00"]


def test_day_mask_round_trips_and_refuses_unknown_days():
    assert mask_from_days(["SUN"]) == 1
    assert mask_from_days(["THU"]) == 16
    assert mask_from_days(["MON", "WED"]) == 2 | 8
    assert mask_from_days([4, "sun", "Sunday"]) == 1 | 16
    assert mask_from_days(DAY_CODES) == ALL
    for mask in range(1, ALL + 1):
        assert mask_from_days(days_from_mask(mask)) == mask
    assert days_from_mask(1 | 4 | 16) == ("SUN", "TUE", "THU")
    with pytest.raises(ValueError):
        mask_from_days(["SUN", "FRI"])


# ── Compile semantics ────────────────────────────────────────────


def test_no_rules_compile_to_the_empty_availability():
    availability = compile_rules([])
    assert availability is EMPTY_AVAILABILITY
    assert not availability
    assert availability.instructor_ids == frozenset()
    assert availability.fingerprint() == ""
    assert not availability.blocks(1, 0, 0, MINUTES_PER_DAY)


def test_unavailable_windows_merge_when_overlapping_or_touching():
    availability = compile_rules(
        [_off(1, 600, 660), _off(1, 540, 610), _off(1, 660, 700), _off(1, 800, 900)]
    )
    assert availability.forbidden(1, 0) == ((540, 700), (800, 900))
    assert availability.forbidden(1, 1) == ()
    assert availability.instructor_ids == frozenset({1})


def test_overlap_is_half_open_touching_is_not_a_violation():
    availability = compile_rules([_off(1, 600, 660)])
    assert not availability.blocks(1, 0, 540, 600)  # ends where the window starts
    assert not availability.blocks(1, 0, 660, 720)  # starts where the window ends
    assert availability.blocks(1, 0, 599, 601)
    assert availability.blocks(1, 0, 659, 661)
    assert availability.blocks(1, 0, 620, 630)  # inside
    assert availability.blocks(1, 0, 500, 800)  # around


def test_available_only_forbids_its_complement_and_every_day_it_does_not_name():
    availability = compile_rules([_only(1 | 4, 540, 720)])
    assert availability.forbidden(1, 0) == ((0, 540), (720, MINUTES_PER_DAY))
    assert availability.forbidden(1, 2) == ((0, 540), (720, MINUTES_PER_DAY))
    for day in (1, 3, 4):
        assert availability.forbidden(1, day) == ((0, MINUTES_PER_DAY),)
    assert not availability.blocks(1, 0, 540, 720)  # the window contains the meeting
    assert availability.blocks(1, 0, 539, 600)
    assert availability.blocks(1, 0, 700, 721)


def test_available_only_windows_union_and_unavailable_carves_out_of_them():
    availability = compile_rules(
        [_only(ALL, 540, 660), _only(ALL, 650, 780), _only(2, 900, 960), _off(2, 600, 610)]
    )
    assert availability.forbidden(1, 0) == ((0, 540), (780, MINUTES_PER_DAY))
    assert availability.forbidden(1, 1) == (
        (0, 540),
        (600, 610),
        (780, 900),
        (960, MINUTES_PER_DAY),
    )
    assert not availability.blocks(1, 1, 610, 780)
    assert not availability.blocks(1, 1, 900, 960)
    assert availability.blocks(1, 1, 605, 606)


def test_available_only_on_every_day_all_day_forbids_nothing():
    availability = compile_rules([_only(ALL, 0, MINUTES_PER_DAY)])
    assert not availability
    assert availability.instructor_ids == frozenset()


def test_blocks_is_false_off_the_week_for_strangers_and_for_empty_meetings():
    availability = compile_rules([_off(ALL, 0, MINUTES_PER_DAY)])
    assert availability.blocks(1, 4, 0, 1)
    assert not availability.blocks(1, 5, 0, 1)
    assert not availability.blocks(1, -1, 0, 1)
    assert not availability.blocks(2, 0, 0, 1)
    assert not availability.blocks(1, 0, 600, 600)
    assert not availability.blocks(1, 0, 700, 600)


def test_blocked_instructors_are_unique_and_sorted():
    availability = compile_rules([_off(1, 540, 600, 7), _off(1, 540, 600, 3), _only(2, 0, 60, 5)])
    assert availability.blocked_instructors([7, 5, 3, 9, 7], 0, 550, 560) == (3, 5, 7)
    assert availability.blocked_instructors([7, 3], 1, 550, 560) == ()


def test_rules_of_different_instructors_never_mix():
    availability = compile_rules([_only(1, 540, 600, 1), _off(2, 0, 60, 2)])
    # Instructor 2 has no available_only rule, so only its own window is forbidden.
    assert availability.forbidden(2, 0) == ()
    assert availability.forbidden(2, 1) == ((0, 60),)
    assert availability.forbidden(1, 1) == ((0, MINUTES_PER_DAY),)


@pytest.mark.parametrize(
    "bad",
    [
        _rule("sometimes", 1, 0, 60),
        _rule(KIND_UNAVAILABLE, 0, 0, 60),
        _rule(KIND_UNAVAILABLE, 32, 0, 60),
        _rule(KIND_UNAVAILABLE, 1, 60, 60),
        _rule(KIND_UNAVAILABLE, 1, 0, 1441),
        _rule(KIND_UNAVAILABLE, 1, -1, 60),
    ],
)
def test_compile_refuses_a_rule_the_database_would_refuse(bad):
    with pytest.raises(ValueError):
        compile_rules([bad])


# ── Property test against an independent minute-by-minute oracle ─


def _oracle_forbidden(rules: list[TimeRule], day: int) -> list[bool]:
    """Minute-by-minute truth for one instructor and day, straight from the
    definition — no intervals, no merging, no complement."""
    bit = 1 << day
    any_available_only = any(r.kind == KIND_AVAILABLE_ONLY for r in rules)
    minutes = []
    for minute in range(MINUTES_PER_DAY):

        def covers(rule: TimeRule, minute: int = minute) -> bool:
            return bool(rule.day_mask & bit) and rule.start_minute <= minute < rule.end_minute

        allowed = not any_available_only or any(
            covers(r) for r in rules if r.kind == KIND_AVAILABLE_ONLY
        )
        unavailable = any(covers(r) for r in rules if r.kind == KIND_UNAVAILABLE)
        minutes.append(not allowed or unavailable)
    return minutes


def _runs(flags: list[bool]) -> tuple[tuple[int, int], ...]:
    runs, start = [], None
    for minute, flag in enumerate([*flags, False]):
        if flag and start is None:
            start = minute
        elif not flag and start is not None:
            runs.append((start, minute))
            start = None
    return tuple(runs)


def _random_time(rng: random.Random) -> int:
    # Mostly a 5-minute grid, so touching and shared edges are frequent.
    if rng.random() < 0.8:
        return rng.randrange(0, MINUTES_PER_DAY // 5 + 1) * 5
    return rng.randrange(0, MINUTES_PER_DAY + 1)


def _random_rules(rng: random.Random, instructor_id: int) -> list[TimeRule]:
    rules = []
    for _ in range(rng.randrange(0, 7)):
        a, b = sorted(rng.sample(range(0, MINUTES_PER_DAY + 1, 5), 2))
        if rng.random() < 0.15:
            a, b = 0, MINUTES_PER_DAY
        kind = KIND_AVAILABLE_ONLY if rng.random() < 0.35 else KIND_UNAVAILABLE
        rules.append(_rule(kind, rng.randrange(1, ALL + 1), a, b, instructor_id))
    return rules


@pytest.mark.parametrize("seed", range(6))
def test_blocks_and_forbidden_match_the_minute_oracle_on_random_rule_sets(seed):
    rng = random.Random(20261121 + seed)
    for _ in range(25):
        rules = {iid: _random_rules(rng, iid) for iid in (1, 2, 3)}
        availability = compile_rules([rule for own in rules.values() for rule in own])
        for iid, own in rules.items():
            for day in range(len(DAY_CODES)):
                truth = _oracle_forbidden(own, day)
                assert availability.forbidden(iid, day) == _runs(truth), (seed, own, day)
                prefix = [0]
                for flag in truth:
                    prefix.append(prefix[-1] + flag)
                for _ in range(40):
                    s, e = _random_time(rng), _random_time(rng)
                    expected = s < e and prefix[e] - prefix[s] > 0
                    assert availability.blocks(iid, day, s, e) == expected, (own, day, s, e)
        assert bool(availability) == any(
            any(_oracle_forbidden(own, d)) for own in rules.values() for d in range(5)
        )


# ── Fingerprint and revision ─────────────────────────────────────


def test_fingerprint_is_stable_order_independent_and_semantic():
    rules = [_off(1, 540, 600, 2), _only(4, 600, 900, 1), _off(16, 0, 60, 1)]
    first = compile_rules(rules).fingerprint()
    assert len(first) == 64
    assert compile_rules(list(reversed(rules))).fingerprint() == first
    # Two rule sets that forbid the same time are the same availability.
    split = [_off(1, 540, 570, 2), _off(1, 570, 600, 2), _only(4, 600, 900, 1), _off(16, 0, 60, 1)]
    assert compile_rules(split).fingerprint() == first
    assert compile_rules([*rules, _off(8, 0, 1, 2)]).fingerprint() != first
    assert (
        compile_rules([_off(1, 540, 600, 3)]).fingerprint()
        != compile_rules([_off(1, 540, 600, 2)]).fingerprint()
    )


def _row(kind=KIND_UNAVAILABLE, mask=1, start=540, end=600, note=""):
    return {"kind": kind, "day_mask": mask, "start_minute": start, "end_minute": end, "note": note}


def test_rules_revision_is_short_stable_order_independent_and_sees_every_field():
    rows = [_row(), _row(KIND_AVAILABLE_ONLY, 31, 0, 780, "x")]
    token = rules_revision(rows)
    assert len(token) == 16
    assert rules_revision(list(reversed(rows))) == token
    assert rules_revision([]) == rules_revision([])
    assert rules_revision([]) != token
    for changed in (
        _row(kind=KIND_AVAILABLE_ONLY),
        _row(mask=2),
        _row(start=541),
        _row(end=601),
        _row(note="moved"),
    ):
        assert rules_revision([changed, rows[1]]) != token, changed


# ── Open periods and the default grid ────────────────────────────


def test_default_period_grid_is_the_placeable_default_columns():
    grid = default_period_grid()
    assert grid["days"] == list(DAY_CODES)
    assert [(p["start"], p["end"]) for p in grid["lecture"]] == [
        ("09:00", "10:15"),
        ("10:30", "11:45"),
        ("10:50", "12:05"),
        ("13:00", "14:15"),
        ("14:30", "15:45"),
        ("14:45", "16:00"),
        ("16:00", "17:15"),
    ]
    # The three online-only evening windows are not teaching periods.
    assert [p["label"] for p in grid["lab"]] == ["Lab 1", "Lab 2", "Lab 3", "Lab 4", "Lab 5"]
    json.dumps(grid)  # served as JSON as is


def test_open_periods_marks_each_day_and_slot():
    availability = compile_rules([_off(1 | 16, 0, 780)])
    slots = [{"start": "09:00", "end": "10:15"}, {"start": "13:00", "end": "14:15"}]
    assert open_periods(availability, 1, slots) == [
        [False, True],
        [True, True],
        [True, True],
        [True, True],
        [False, True],
    ]
    assert open_periods(availability, 99, slots) == [[True, True]] * 5
    with pytest.raises(ValueError):
        open_periods(availability, 1, [{"start": "9h", "end": "10:15"}])


def _wire_to_rules(wire: list[dict], instructor_id: int = 1) -> list[TimeRule]:
    return [
        TimeRule(
            instructor_id,
            rule["kind"],
            mask_from_days(rule["days"]),
            to_minutes(rule["start"]),
            to_minutes(rule["end"]),
        )
        for rule in wire
    ]


def test_every_shared_parity_case_holds():
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    assert len(cases) >= 14
    assert len({case["name"] for case in cases}) == len(cases)
    grid = default_period_grid()
    for case in cases:
        availability = compile_rules(_wire_to_rules(case["rules"]))
        for probe in case["probes"]:
            blocked = availability.blocks(
                1, day_index(probe["day"]), to_minutes(probe["start"]), to_minutes(probe["end"])
            )
            assert blocked is probe["blocked"], (case["name"], probe)
        summary = open_summary(availability, 1, grid)
        assert {kind: summary[kind]["open"] for kind in ("lecture", "lab")} == case["open"], case[
            "name"
        ]
        assert (summary["lecture"]["total"], summary["lab"]["total"]) == (35, 25)


# ── Loader ───────────────────────────────────────────────────────


def _instructor(name: str) -> Instructor:
    return Instructor.objects.create(full_name=name, normalised_name=name.lower())


@pytest.mark.django_db
def test_loader_is_one_query_and_empty_tables_load_falsy():
    a, b = _instructor("Dr Loader A"), _instructor("Dr Loader B")
    with CaptureQueriesContext(connection) as ctx:
        availability = load_instructor_availability()
    assert len(ctx.captured_queries) == 1
    assert not availability

    InstructorTimeRule.objects.create(
        instructor=a, kind=KIND_UNAVAILABLE, day_mask=1, start_minute=540, end_minute=600
    )
    InstructorTimeRule.objects.create(
        instructor=b, kind=KIND_AVAILABLE_ONLY, day_mask=2, start_minute=600, end_minute=700
    )
    with CaptureQueriesContext(connection) as ctx:
        both = load_instructor_availability()
        only_a = load_instructor_availability([a.pk], scenario=object())
    assert len(ctx.captured_queries) == 2
    assert both.instructor_ids == frozenset({a.pk, b.pk})
    assert only_a.instructor_ids == frozenset({a.pk})
    assert only_a.blocks(a.pk, 0, 550, 560)
    assert not only_a.blocks(b.pk, 0, 550, 560)

    with CaptureQueriesContext(connection) as ctx:
        assert load_time_rules([]) == []
    assert len(ctx.captured_queries) == 0

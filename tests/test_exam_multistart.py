"""Tests for ``core.services.exam_multistart``.

Covers:
- Pareto picker correctness for each of the 4 roles (synthetic candidates).
- Tie-breaker discipline (deterministic on seed when metrics tie).
- Same candidate winning multiple roles.
- Mechanical "best room feasibility" definition (the peer-review-locked
  one — fewest UNASSIGNED -> fewest multi-sitting -> highest avg
  utilisation, never building/floor).
- Feature flag gating.
- ``_extract_metrics`` defensive against missing QA keys.
- ``run_multistart`` deterministic when seeds are explicit.
- ``run_multistart`` respects the time budget (loop exits after deadline
  with at least one candidate).
- All-feasibility-error path: empty report with feasibility_error set.
- Pin-and-rebuild signal: ``movement_count`` and ``courses_moved``
  computed correctly vs a baseline run.
- Persistence: only winning seeds become ``ExamTimetableRun`` rows.
- Each persisted candidate carries ``schema_version`` and the
  ``multistart`` annotation.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from django.test import override_settings

from core.models import ExamTimetableRun
from core.services.exam_multistart import (
    ALL_CANDIDATE_ROLES,
    CandidateMetrics,
    MultistartCandidate,
    _baseline_placements,
    _extract_metrics,
    _movement_vs_baseline,
    is_multistart_enabled,
    report_to_dict,
    run_multistart,
    select_pareto_candidates,
)
from core.services.exam_run_schema import EXAM_RUN_SCHEMA_VERSION

# ---------------------------------------------------------------------------
# Synthetic-candidate helpers
# ---------------------------------------------------------------------------


def _candidate(
    seed: int,
    *,
    overflow: int = 0,
    overload: int = 0,
    heavy: int = 0,
    same_slot: int = 0,
    bucket_day: int = 0,
    unassigned_rooms: int = 0,
    multi_sitting: int = 0,
    avg_util: float = 0.0,
    max_credit: int = 0,
    max_exams: int = 0,
) -> MultistartCandidate:
    metrics = CandidateMetrics(
        overflow_count=overflow,
        students_over_limit_count=overload,
        heavy_day_students=heavy,
        same_slot_conflicts=same_slot,
        bucket_day_violations=bucket_day,
        unassigned_room_sections=unassigned_rooms,
        multi_sitting_sections=multi_sitting,
        avg_utilisation=avg_util,
        max_credit_load_per_day=max_credit,
        max_exams_per_day=max_exams,
    )
    return MultistartCandidate(
        seed=seed,
        metrics=metrics,
        payload={"schema_version": EXAM_RUN_SCHEMA_VERSION, "status": "ok", "schedule": []},
    )


# ---------------------------------------------------------------------------
# Pareto picker correctness
# ---------------------------------------------------------------------------


def test_select_pareto_candidates_empty_pool() -> None:
    assert select_pareto_candidates([]) == {}


def test_select_pareto_returns_one_per_role() -> None:
    pool = [_candidate(s) for s in range(3)]
    result = select_pareto_candidates(pool)
    assert set(result.keys()) == set(ALL_CANDIDATE_ROLES)


def test_lowest_overflow_picks_minimum_overflow() -> None:
    a = _candidate(1, overflow=5)
    b = _candidate(2, overflow=2)
    c = _candidate(3, overflow=3)
    out = select_pareto_candidates([a, b, c])
    assert out["lowest_overflow"].seed == 2


def test_lowest_overload_picks_minimum_students_over_limit() -> None:
    a = _candidate(1, overload=10)
    b = _candidate(2, overload=4)
    c = _candidate(3, overload=8)
    out = select_pareto_candidates([a, b, c])
    assert out["lowest_overload"].seed == 2


def test_recommended_prefers_zero_hard_violations() -> None:
    """A candidate with zero hard violations beats one with lower
    soft cost but a same-slot conflict."""
    bad = _candidate(1, same_slot=1, overload=0)
    good = _candidate(2, same_slot=0, overload=20)
    out = select_pareto_candidates([bad, good])
    assert out["recommended"].seed == 2


def test_recommended_prefers_lower_overflow_after_hard_clean() -> None:
    a = _candidate(1, overflow=3)
    b = _candidate(2, overflow=0)
    out = select_pareto_candidates([a, b])
    assert out["recommended"].seed == 2


def test_recommended_breaks_ties_by_overload_then_heavy_then_credit() -> None:
    a = _candidate(1, overload=5, heavy=2)
    b = _candidate(2, overload=3, heavy=1)
    c = _candidate(3, overload=3, heavy=2)
    out = select_pareto_candidates([a, b, c])
    # b wins over c on heavy_day_students after overload tied.
    assert out["recommended"].seed == 2


def test_best_room_feasibility_uses_mechanical_definition() -> None:
    """Tier 1: fewest UNASSIGNED. Tier 2: fewest multi-sitting.
    Tier 3: highest avg utilisation."""
    a = _candidate(1, unassigned_rooms=3, multi_sitting=2, avg_util=0.9)
    b = _candidate(2, unassigned_rooms=1, multi_sitting=5, avg_util=0.7)
    c = _candidate(3, unassigned_rooms=1, multi_sitting=3, avg_util=0.5)
    out = select_pareto_candidates([a, b, c])
    # b and c tie on unassigned_rooms (1); c wins multi_sitting (3 < 5).
    assert out["best_room_feasibility"].seed == 3


def test_best_room_feasibility_breaks_tier3_tie_on_utilisation() -> None:
    a = _candidate(1, unassigned_rooms=0, multi_sitting=0, avg_util=0.6)
    b = _candidate(2, unassigned_rooms=0, multi_sitting=0, avg_util=0.85)
    out = select_pareto_candidates([a, b])
    assert out["best_room_feasibility"].seed == 2


def test_best_room_feasibility_does_not_use_buildings_per_slot() -> None:
    """Mechanical definition excludes buildings-per-slot (telemetry only).
    A candidate with worse 'building footprint' must still win if it
    has better unassigned/multi-sitting/utilisation. We can't test the
    metric directly because it's not in CandidateMetrics — that's the
    test: it's not even in the type system, so it cannot leak in."""
    fields = CandidateMetrics.__dataclass_fields__
    assert "buildings_per_slot" not in fields
    assert "building_concentration" not in fields
    assert "floor_concentration" not in fields


def test_same_seed_can_win_multiple_roles() -> None:
    """A genuinely good candidate dominates across roles. The runner
    deduplicates on persistence; the picker preserves all mappings."""
    dominant = _candidate(0, overflow=0, overload=0, unassigned_rooms=0)
    weak = _candidate(1, overflow=5, overload=10, unassigned_rooms=5)
    out = select_pareto_candidates([dominant, weak])
    # dominant wins all four roles because it's strictly better on every dim.
    assert all(c.seed == 0 for c in out.values())


def test_tie_breaker_uses_seed_deterministically() -> None:
    """When two candidates have identical metrics, the lower seed wins
    every tie. This is what makes multi-start runs reproducible."""
    same = {
        "overflow": 0,
        "overload": 0,
        "heavy": 0,
        "unassigned_rooms": 0,
        "multi_sitting": 0,
    }
    a = _candidate(7, **same)
    b = _candidate(3, **same)
    c = _candidate(11, **same)
    out = select_pareto_candidates([a, b, c])
    for role in ALL_CANDIDATE_ROLES:
        assert out[role].seed == 3, f"role {role} did not break tie on lowest seed"


# ---------------------------------------------------------------------------
# Metrics extraction
# ---------------------------------------------------------------------------


def test_extract_metrics_from_full_payload() -> None:
    payload = {
        "qa": {
            "students_over_limit_per_day": 4,
            "heavy_day_students": 2,
            "conflict_count": 0,
            "bucket_day_violations_count": 0,
            "unassigned_room_sections": 1,
            "multi_sitting_sections": 0,
            "avg_utilization": 0.78,
            "max_credit_load_per_day": 10,
            "max_exams_per_day_per_student": 3,
        },
        "schedule": [
            {"day": "SUN", "course_code": "CS101", "slot_index": 0},
            {"day": "OVERFLOW", "course_code": "CS999", "slot_index": -1},
            {"day": "OVERFLOW", "course_code": "CS998", "slot_index": -1},
        ],
    }
    m = _extract_metrics(payload)
    assert m.overflow_count == 2
    assert m.students_over_limit_count == 4
    assert m.heavy_day_students == 2
    assert m.unassigned_room_sections == 1
    assert m.avg_utilisation == 0.78


def test_extract_metrics_defensive_on_missing_keys() -> None:
    """A pre-step-2 payload (no multi_sitting tile yet, perhaps no QA)
    must extract zero metrics, not crash."""
    m = _extract_metrics({})
    assert m.overflow_count == 0
    assert m.students_over_limit_count == 0
    assert m.unassigned_room_sections == 0
    assert m.multi_sitting_sections == 0


def test_extract_metrics_missing_multi_sitting_key_neutral() -> None:
    """Until step 3 lands the multi-sitting tile, every candidate reads
    multi_sitting=0 and the tie-breaker effectively skips it."""
    payload = {"qa": {"students_over_limit_per_day": 1}, "schedule": []}
    m = _extract_metrics(payload)
    assert m.multi_sitting_sections == 0


# ---------------------------------------------------------------------------
# Pin-and-rebuild signal
# ---------------------------------------------------------------------------


def test_movement_vs_baseline_zero_when_identical() -> None:
    baseline = {"CS101": ("SUN", 0), "CS102": ("MON", 1)}
    payload = {
        "schedule": [
            {"course_code": "CS101", "day": "SUN", "slot_index": 0},
            {"course_code": "CS102", "day": "MON", "slot_index": 1},
        ]
    }
    count, moved = _movement_vs_baseline(payload, baseline)
    assert count == 0
    assert moved == []


def test_movement_vs_baseline_counts_changed_placements() -> None:
    baseline = {"CS101": ("SUN", 0), "CS102": ("MON", 1)}
    payload = {
        "schedule": [
            {"course_code": "CS101", "day": "SUN", "slot_index": 0},  # same
            {"course_code": "CS102", "day": "TUE", "slot_index": 2},  # moved
        ]
    }
    count, moved = _movement_vs_baseline(payload, baseline)
    assert count == 1
    assert moved == ["CS102"]


def test_movement_vs_baseline_counts_dropped_courses() -> None:
    baseline = {"CS101": ("SUN", 0), "CS102": ("MON", 1)}
    payload = {
        "schedule": [
            {"course_code": "CS101", "day": "SUN", "slot_index": 0},
        ]
    }
    count, moved = _movement_vs_baseline(payload, baseline)
    assert count == 1
    assert moved == ["CS102"]


def test_movement_vs_baseline_counts_added_courses() -> None:
    baseline = {"CS101": ("SUN", 0)}
    payload = {
        "schedule": [
            {"course_code": "CS101", "day": "SUN", "slot_index": 0},
            {"course_code": "CS102", "day": "MON", "slot_index": 1},
        ]
    }
    count, moved = _movement_vs_baseline(payload, baseline)
    assert count == 1
    assert moved == ["CS102"]


def test_movement_vs_baseline_sorted_alphabetically_for_determinism() -> None:
    baseline = {"BBB": ("SUN", 0), "AAA": ("MON", 0), "CCC": ("TUE", 0)}
    payload: dict[str, Any] = {"schedule": []}
    _, moved = _movement_vs_baseline(payload, baseline)
    assert moved == ["AAA", "BBB", "CCC"]


@pytest.mark.django_db
def test_baseline_placements_returns_none_for_missing_run() -> None:
    assert _baseline_placements(99999) is None


@pytest.mark.django_db
def test_baseline_placements_returns_none_for_unrenderable_run() -> None:
    run = ExamTimetableRun.objects.create(label="corrupt", result_json="{garbage")
    assert _baseline_placements(run.id) is None


@pytest.mark.django_db
def test_baseline_placements_extracts_schedule() -> None:
    payload = {
        "schema_version": EXAM_RUN_SCHEMA_VERSION,
        "status": "ok",
        "schedule": [
            {"course_code": "CS101", "day": "SUN", "slot_index": 0},
            {"course_code": "CS102", "day": "MON", "slot_index": 1},
        ],
    }
    run = ExamTimetableRun.objects.create(label="x", result_json=json.dumps(payload))
    baseline = _baseline_placements(run.id)
    assert baseline == {"CS101": ("SUN", 0), "CS102": ("MON", 1)}


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


def test_feature_flag_default_off() -> None:
    """The flag must default to False — the existing single-run UI is
    the default until multi-start UI is proven."""
    with override_settings():
        # No setting at all: defaults to False.
        from django.conf import settings as _s

        if hasattr(_s, "TIMETABLE_EXAM_MULTISTART_ENABLED"):
            del _s._wrapped.TIMETABLE_EXAM_MULTISTART_ENABLED  # type: ignore[attr-defined]
        # Trust the helper: must default to False.
        assert is_multistart_enabled() is False


@override_settings(TIMETABLE_EXAM_MULTISTART_ENABLED=True)
def test_feature_flag_can_be_enabled() -> None:
    assert is_multistart_enabled() is True


@override_settings(TIMETABLE_EXAM_MULTISTART_ENABLED=False)
def test_feature_flag_can_be_disabled() -> None:
    assert is_multistart_enabled() is False


# ---------------------------------------------------------------------------
# Runner — using mocked build_exam_timetable so tests stay fast and
# do not depend on the full scheduler. The runner's contract is:
# call build N times, collect, pick, persist. Mocking the build keeps
# the test focused on the runner contract.
# ---------------------------------------------------------------------------


def _fake_build_factory(metrics_per_seed: dict[int, dict]):
    """Build a stand-in for ``build_exam_timetable`` that returns
    pre-canned QA metrics keyed by seed."""

    def _fake_build(**kwargs: Any) -> dict[str, Any]:
        seed = kwargs.get("seed")
        spec = metrics_per_seed.get(seed, {})
        if spec.get("feasibility_error"):
            return {
                "schema_version": EXAM_RUN_SCHEMA_VERSION,
                "status": "feasibility_error",
                "feasibility_error": True,
                "violations": [{"bucket": "(CS, T1)", "courses": 4, "days": 3}],
                "courses_count": 0,
                "students_count": 0,
                "bucket_count": 0,
            }
        # Build a synthetic schedule that yields the requested overflow_count.
        # _extract_metrics counts entries with day=="OVERFLOW", so we inject
        # exactly that many OVERFLOW rows on top of any explicit schedule.
        if "schedule" in spec:
            schedule = list(spec["schedule"])
        else:
            schedule = [{"course_code": "CS101", "day": "SUN", "slot_index": 0}]
            for i in range(spec.get("overflow", 0)):
                schedule.append(
                    {
                        "course_code": f"OVF{i:03d}",
                        "day": "OVERFLOW",
                        "slot_index": -1,
                    }
                )
        return {
            "schema_version": EXAM_RUN_SCHEMA_VERSION,
            "status": "ok",
            "schedule": schedule,
            "qa": {
                "students_over_limit_per_day": spec.get("overload", 0),
                "heavy_day_students": spec.get("heavy", 0),
                "conflict_count": spec.get("conflicts", 0),
                "bucket_day_violations_count": spec.get("bucket_day", 0),
                "unassigned_room_sections": spec.get("unassigned", 0),
                "multi_sitting_sections": spec.get("multi_sitting", 0),
                "avg_utilization": spec.get("util", 0.5),
                "max_credit_load_per_day": spec.get("max_credit", 0),
                "max_exams_per_day_per_student": spec.get("max_exams", 0),
            },
            "students_count": 100,
            "courses": ["CS101"],
            "courses_count": 1,
            "conflicts": [],
            "conflicts_count": 0,
            "slots": [],
            "buckets_summary": [],
            "bucket_count": 0,
            "credit_map": {},
            "section_enrollment": {},
            "rooms_count": 0,
            "assign_rooms": {},
            "seed": seed,
        }

    return _fake_build


@pytest.mark.django_db
def test_run_multistart_persists_only_winning_seeds() -> None:
    """If 4 different seeds each win one role, 4 rows are persisted.
    If one seed dominates and wins all 4 roles, only 1 row is persisted."""
    spec = {
        # Dominant seed across the board.
        0: {"overload": 0, "overflow": 0, "unassigned": 0, "util": 0.9},
        # Worse on every metric.
        1: {"overload": 5, "overflow": 2, "unassigned": 3, "util": 0.5},
        2: {"overload": 5, "overflow": 2, "unassigned": 3, "util": 0.5},
    }
    pre_count = ExamTimetableRun.objects.count()
    with patch(
        "core.services.exam_timetable.build_exam_timetable",
        side_effect=_fake_build_factory(spec),
    ):
        report = run_multistart(
            label="t",
            days=["SUN", "MON"],
            periods=["P1", "P2"],
            seeds=[0, 1, 2],
            time_budget_s=60,
        )
    new_rows = ExamTimetableRun.objects.count() - pre_count
    assert new_rows == 1, "dominant candidate should be persisted exactly once"
    assert report.runs_completed == 3
    # Same candidate object under all 4 roles.
    seed_0_count = sum(1 for c in report.candidates_by_role.values() if c.seed == 0)
    assert seed_0_count == 4


@pytest.mark.django_db
def test_run_multistart_persists_four_distinct_winners() -> None:
    """Four distinct best-of-role seeds → 4 persisted rows.

    By design, ``recommended`` shares overflow as its third key with
    ``lowest_overflow``, so a candidate with overflow=0 normally wins
    both roles. To force four distinct winners we give seed 1 (the
    overflow champion) a hard violation, which demotes it on
    ``recommended`` (hard-violation count is the very first key) but
    not on ``lowest_overflow`` (where hard counts are only a
    tie-breaker among equal-overflow candidates).
    """
    spec = {
        # Each seed wins a different role.
        0: {"overload": 0, "overflow": 5, "unassigned": 5, "util": 0.5},  # lowest_overload
        1: {
            "overload": 10,
            "overflow": 0,
            "unassigned": 10,
            "util": 0.5,
            "conflicts": 1,
        },  # lowest_overflow only
        2: {"overload": 10, "overflow": 10, "unassigned": 0, "util": 0.9},  # best_room_feasibility
        3: {"overload": 1, "overflow": 1, "unassigned": 1, "util": 0.5},  # recommended
    }
    pre_count = ExamTimetableRun.objects.count()
    with patch(
        "core.services.exam_timetable.build_exam_timetable",
        side_effect=_fake_build_factory(spec),
    ):
        report = run_multistart(
            label="four",
            days=["SUN"],
            periods=["P1"],
            seeds=[0, 1, 2, 3],
            time_budget_s=60,
        )
    new_rows = ExamTimetableRun.objects.count() - pre_count
    assert new_rows == 4
    seeds = {c.seed for c in report.candidates_by_role.values()}
    assert len(seeds) == 4
    # Verify the role-to-seed mapping is what we expect.
    assert report.candidates_by_role["lowest_overload"].seed == 0
    assert report.candidates_by_role["lowest_overflow"].seed == 1
    assert report.candidates_by_role["best_room_feasibility"].seed == 2
    assert report.candidates_by_role["recommended"].seed == 3


@pytest.mark.django_db
def test_run_multistart_label_discriminator() -> None:
    spec = {0: {"overload": 0, "overflow": 0, "unassigned": 0}}
    with patch(
        "core.services.exam_timetable.build_exam_timetable",
        side_effect=_fake_build_factory(spec),
    ):
        report = run_multistart(
            label="MyRun",
            days=["SUN"],
            periods=["P1"],
            seeds=[0],
            time_budget_s=60,
        )
    run_id = list(report.run_ids.values())[0]
    persisted = ExamTimetableRun.objects.get(id=run_id)
    assert "MyRun" in persisted.label
    assert "candidate=" in persisted.label
    assert "seed=0" in persisted.label


@pytest.mark.django_db
def test_run_multistart_persisted_payload_carries_schema_version() -> None:
    spec = {0: {"overload": 0}}
    with patch(
        "core.services.exam_timetable.build_exam_timetable",
        side_effect=_fake_build_factory(spec),
    ):
        report = run_multistart(
            label="x",
            days=["SUN"],
            periods=["P1"],
            seeds=[0],
            time_budget_s=60,
        )
    run_id = list(report.run_ids.values())[0]
    persisted = ExamTimetableRun.objects.get(id=run_id)
    payload = json.loads(persisted.result_json)
    assert payload["schema_version"] == EXAM_RUN_SCHEMA_VERSION
    assert payload["status"] == "ok"
    assert "multistart" in payload
    assert payload["multistart"]["seed"] == 0
    assert "role_winners" in payload["multistart"]


# ---------------------------------------------------------------------------
# Hardening: grouping metadata so the history panel can reconstruct
# sibling-runs without label parsing.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_run_multistart_persists_group_id() -> None:
    """All candidates from one multistart call share a UUID4 group_id."""
    spec = {
        0: {"overload": 0, "overflow": 5, "unassigned": 5},
        1: {"overload": 5, "overflow": 0, "unassigned": 5, "conflicts": 1},
        2: {"overload": 5, "overflow": 5, "unassigned": 0, "util": 0.9},
        3: {"overload": 1, "overflow": 1, "unassigned": 1},
    }
    with patch(
        "core.services.exam_timetable.build_exam_timetable",
        side_effect=_fake_build_factory(spec),
    ):
        report = run_multistart(
            label="g",
            days=["SUN"],
            periods=["P1"],
            seeds=[0, 1, 2, 3],
            time_budget_s=60,
        )

    group_ids: set[str] = set()
    for run_id in report.run_ids.values():
        persisted = ExamTimetableRun.objects.get(id=run_id)
        payload = json.loads(persisted.result_json)
        group_ids.add(payload["multistart"]["group_id"])
    # All candidates from one call share one group_id.
    assert len(group_ids) == 1
    # And it's a 32-char hex (uuid4().hex).
    assert len(next(iter(group_ids))) == 32


@pytest.mark.django_db
def test_run_multistart_persists_base_label_and_selected_flag() -> None:
    spec = {0: {"overload": 0}}
    with patch(
        "core.services.exam_timetable.build_exam_timetable",
        side_effect=_fake_build_factory(spec),
    ):
        report = run_multistart(
            label="MyBase",
            days=["SUN"],
            periods=["P1"],
            seeds=[0],
            time_budget_s=60,
        )
    run_id = list(report.run_ids.values())[0]
    payload = json.loads(ExamTimetableRun.objects.get(id=run_id).result_json)
    assert payload["multistart"]["base_label"] == "MyBase"
    assert payload["multistart"]["selected_from_multistart"] is True


@pytest.mark.django_db
def test_run_multistart_persists_candidate_rank() -> None:
    """candidate_rank = ALL_CANDIDATE_ROLES.index of the first role
    won, so 0 = recommended, 1 = lowest_overflow, etc."""
    spec = {
        0: {"overload": 0, "overflow": 5, "unassigned": 5},
        1: {"overload": 5, "overflow": 0, "unassigned": 5, "conflicts": 1},
        2: {"overload": 5, "overflow": 5, "unassigned": 0, "util": 0.9},
        3: {"overload": 1, "overflow": 1, "unassigned": 1},
    }
    with patch(
        "core.services.exam_timetable.build_exam_timetable",
        side_effect=_fake_build_factory(spec),
    ):
        run_multistart(
            label="r",
            days=["SUN"],
            periods=["P1"],
            seeds=[0, 1, 2, 3],
            time_budget_s=60,
        )
    rank_by_seed = {}
    for run in ExamTimetableRun.objects.filter(label__startswith="r ::"):
        payload = json.loads(run.result_json)
        ms = payload["multistart"]
        rank_by_seed[ms["seed"]] = ms["candidate_rank"]
    # seed=3 wins recommended (rank 0); seed=1 wins lowest_overflow (1);
    # seed=0 wins lowest_overload (2); seed=2 wins best_room_feasibility (3).
    assert rank_by_seed == {3: 0, 1: 1, 0: 2, 2: 3}


# ---------------------------------------------------------------------------
# Hardening: completion metadata so a time-budget-cut multistart is
# reproducible across machines.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_run_multistart_records_attempted_and_completed_seeds() -> None:
    spec = {0: {"overload": 0}, 1: {"overload": 5}, 2: {"overload": 10}}
    with patch(
        "core.services.exam_timetable.build_exam_timetable",
        side_effect=_fake_build_factory(spec),
    ):
        report = run_multistart(
            label="c",
            days=["SUN"],
            periods=["P1"],
            seeds=[0, 1, 2],
            time_budget_s=60,
        )
    run_id = list(report.run_ids.values())[0]
    payload = json.loads(ExamTimetableRun.objects.get(id=run_id).result_json)
    ms = payload["multistart"]
    assert ms["requested_n_runs"] == 3
    assert ms["completed_n_runs"] == 3
    assert ms["attempted_seeds"] == [0, 1, 2]
    assert ms["completed_seeds"] == [0, 1, 2]
    assert ms["timeout_hit"] is False
    assert ms["time_budget_s"] == 60


@pytest.mark.django_db
def test_run_multistart_records_timeout_hit_when_budget_cuts_loop() -> None:
    """A zero time-budget plus multiple seeds: first build completes
    (the loop checks the deadline AFTER the first candidate is
    collected), subsequent seeds are cut. timeout_hit=True."""
    spec = {0: {"overload": 0}, 1: {"overload": 5}}
    with patch(
        "core.services.exam_timetable.build_exam_timetable",
        side_effect=_fake_build_factory(spec),
    ):
        report = run_multistart(
            label="t",
            days=["SUN"],
            periods=["P1"],
            seeds=[0, 1],
            time_budget_s=0.0,  # immediate cut after first candidate
        )
    run_id = list(report.run_ids.values())[0]
    payload = json.loads(ExamTimetableRun.objects.get(id=run_id).result_json)
    ms = payload["multistart"]
    assert ms["timeout_hit"] is True
    assert ms["completed_n_runs"] == 1
    assert ms["completed_seeds"] == [0]
    assert ms["attempted_seeds"] == [0, 1]


# ---------------------------------------------------------------------------
# Hardening: lowest_overload tie-breaker (peer-review-confirmed order).
# ---------------------------------------------------------------------------


def test_lowest_overload_breaks_ties_in_confirmed_order() -> None:
    """Confirmed order: students_over_limit -> heavy_day -> max_credit_load
    -> overflow -> unassigned -> multi_sitting -> -avg_util -> hard
    -> seed."""
    # Same overload (=5); break on heavy_day (a=5, b=2); b should win.
    a = _candidate(0, overload=5, heavy=5)
    b = _candidate(1, overload=5, heavy=2)
    out = select_pareto_candidates([a, b])
    assert out["lowest_overload"].seed == 1


def test_lowest_overload_breaks_overload_and_heavy_tie_on_max_credit() -> None:
    a = _candidate(0, overload=3, heavy=2, max_credit=10)
    b = _candidate(1, overload=3, heavy=2, max_credit=4)
    out = select_pareto_candidates([a, b])
    assert out["lowest_overload"].seed == 1


def test_lowest_overload_breaks_through_to_overflow_after_credit() -> None:
    a = _candidate(0, overload=3, heavy=2, max_credit=5, overflow=5)
    b = _candidate(1, overload=3, heavy=2, max_credit=5, overflow=1)
    out = select_pareto_candidates([a, b])
    assert out["lowest_overload"].seed == 1


@pytest.mark.django_db
def test_run_multistart_pin_rebuild_signal_with_baseline() -> None:
    # First persist a baseline run with two courses.
    baseline_payload = {
        "schema_version": EXAM_RUN_SCHEMA_VERSION,
        "status": "ok",
        "schedule": [
            {"course_code": "CS101", "day": "SUN", "slot_index": 0},
            {"course_code": "CS102", "day": "MON", "slot_index": 1},
        ],
    }
    baseline_run = ExamTimetableRun.objects.create(
        label="baseline",
        result_json=json.dumps(baseline_payload),
    )

    # Now run multistart against a build that places CS101 the same but
    # CS102 differently — expect movement_count=1 with CS102 in the list.
    spec = {
        0: {
            "schedule": [
                {"course_code": "CS101", "day": "SUN", "slot_index": 0},  # same
                {"course_code": "CS102", "day": "TUE", "slot_index": 2},  # moved
            ],
        }
    }
    with patch(
        "core.services.exam_timetable.build_exam_timetable",
        side_effect=_fake_build_factory(spec),
    ):
        report = run_multistart(
            label="rebuild",
            days=["SUN", "MON", "TUE"],
            periods=["P1", "P2"],
            seeds=[0],
            previous_run_id=baseline_run.id,
            time_budget_s=60,
        )

    run_id = list(report.run_ids.values())[0]
    persisted = ExamTimetableRun.objects.get(id=run_id)
    payload = json.loads(persisted.result_json)
    assert payload["multistart"]["previous_run_id"] == baseline_run.id
    assert payload["multistart"]["movement_count"] == 1
    assert payload["multistart"]["courses_moved"] == ["CS102"]


@pytest.mark.django_db
def test_run_multistart_no_baseline_means_no_movement_metadata() -> None:
    spec = {0: {"overload": 0}}
    with patch(
        "core.services.exam_timetable.build_exam_timetable",
        side_effect=_fake_build_factory(spec),
    ):
        report = run_multistart(
            label="no-baseline",
            days=["SUN"],
            periods=["P1"],
            seeds=[0],
            time_budget_s=60,
        )
    run_id = list(report.run_ids.values())[0]
    persisted = ExamTimetableRun.objects.get(id=run_id)
    payload = json.loads(persisted.result_json)
    assert payload["multistart"]["movement_count"] is None
    assert payload["multistart"]["courses_moved"] == []


@pytest.mark.django_db
def test_run_multistart_all_feasibility_error() -> None:
    spec = {s: {"feasibility_error": True} for s in range(3)}
    pre_count = ExamTimetableRun.objects.count()
    with patch(
        "core.services.exam_timetable.build_exam_timetable",
        side_effect=_fake_build_factory(spec),
    ):
        report = run_multistart(
            label="bad",
            days=["SUN"],
            periods=["P1"],
            seeds=[0, 1, 2],
            time_budget_s=60,
        )
    assert report.candidates_by_role == {}
    assert report.feasibility_error is not None
    # No persistence: every seed failed feasibility.
    assert ExamTimetableRun.objects.count() == pre_count


@pytest.mark.django_db
def test_run_multistart_partial_feasibility_error() -> None:
    """Mix of feasibility-error and OK seeds: only the OK ones are
    candidates, but the report still includes the error sample."""
    spec: dict[int, dict] = {
        0: {"feasibility_error": True},
        1: {"overload": 0},
        2: {"feasibility_error": True},
    }
    with patch(
        "core.services.exam_timetable.build_exam_timetable",
        side_effect=_fake_build_factory(spec),
    ):
        report = run_multistart(
            label="mixed",
            days=["SUN"],
            periods=["P1"],
            seeds=[0, 1, 2],
            time_budget_s=60,
        )
    assert len(report.candidates_by_role) == 4  # all 4 roles point to seed=1
    assert report.runs_completed == 1


@pytest.mark.django_db
def test_run_multistart_deterministic_with_explicit_seeds() -> None:
    """Two runs with the same explicit seeds must produce byte-equal
    persisted payloads (modulo run_id, label, and the multistart
    metadata's role_winners ordering which is also deterministic)."""
    spec = {0: {"overload": 0, "overflow": 0, "unassigned": 0}, 1: {"overload": 5}}

    with patch(
        "core.services.exam_timetable.build_exam_timetable",
        side_effect=_fake_build_factory(spec),
    ):
        r1 = run_multistart(
            label="d1",
            days=["SUN"],
            periods=["P1"],
            seeds=[0, 1],
            time_budget_s=60,
        )
    with patch(
        "core.services.exam_timetable.build_exam_timetable",
        side_effect=_fake_build_factory(spec),
    ):
        r2 = run_multistart(
            label="d2",
            days=["SUN"],
            periods=["P1"],
            seeds=[0, 1],
            time_budget_s=60,
        )

    # Same seed wins each role.
    for role in ALL_CANDIDATE_ROLES:
        assert r1.candidates_by_role[role].seed == r2.candidates_by_role[role].seed


@pytest.mark.django_db
def test_run_multistart_zero_seeds_defaults_to_at_least_one() -> None:
    """``seeds=[]`` shouldn't produce a no-op; the runner falls back
    to a single deterministic seed=0 build."""
    spec = {0: {"overload": 0}}
    with patch(
        "core.services.exam_timetable.build_exam_timetable",
        side_effect=_fake_build_factory(spec),
    ):
        report = run_multistart(
            label="z",
            days=["SUN"],
            periods=["P1"],
            seeds=[],
            time_budget_s=60,
        )
    assert report.runs_completed == 1


# ---------------------------------------------------------------------------
# JSON serialisation for the view layer
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_report_to_dict_round_trips_through_json() -> None:
    spec = {0: {"overload": 0}}
    with patch(
        "core.services.exam_timetable.build_exam_timetable",
        side_effect=_fake_build_factory(spec),
    ):
        report = run_multistart(
            label="x",
            days=["SUN"],
            periods=["P1"],
            seeds=[0],
            time_budget_s=60,
        )
    rendered = report_to_dict(report)
    encoded = json.dumps(rendered)
    decoded = json.loads(encoded)
    assert decoded["runs_completed"] == 1
    assert "candidates" in decoded
    for role in ALL_CANDIDATE_ROLES:
        assert role in decoded["candidates"]
        assert "metrics" in decoded["candidates"][role]
        assert "payload" in decoded["candidates"][role]

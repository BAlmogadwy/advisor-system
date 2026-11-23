"""A repeatable measurement of the timetable builder (Generate) and optimiser (Optimise Current).

Measurement only: nothing here changes what an engine does. One *run* is one call of the
production entry point, ``run_v2_optimisation_guarded`` (``mode="full"`` is Generate,
``mode="current"`` is Optimise Current), inside a database transaction that is ROLLED BACK
afterwards, so every repeat starts from the same stored board and the database is left as it
was found. Each run becomes one flat row (:func:`run_once`); :func:`summarise` reports the
spread over repeats, because the engines are not deterministic.

The numbers come from the project's own evaluators, not a score of this module's own:

* students: ``evaluate_generated_timetable_candidate`` (unresolved students, real gap minutes
  and assigned clashes decoded with ``decode_score``);
* teachers: its ``instructor_schedule`` side band, i.e. ``instructor_schedule_metrics``;
* hard clashes and cross-board conflicts: ``compute_scenario_safety_summary``;
* required sections missing: ``find_missing_required_sections``;
* outcome, rollback reasons, dropped stages, second try: the keys of the run result dict.

Per-stage wall time and "did this stage change the board" come from :class:`StageRecorder`,
which wraps the stage functions for the duration of one run (the engine calls them through
lazy imports, so patching the defining module reaches every call). A stage nested inside
another one (rooming inside the placer) is not counted twice.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import importlib
import statistics
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from django.db import transaction

ACTIONS = ("generate", "optimise_current")
# The newer engine (``scheduler/``), run through the bridge Generate uses when the user picks it.
# A comparison row only: its stages are not instrumented, and it withholds courses fewer than
# ``min_demand`` students want, which the board measure then counts as unresolved students.
COMPARISON_ACTIONS = ("scheduler_generate",)
ALL_ACTIONS = (*ACTIONS, *COMPARISON_ACTIONS)
# Generate is the engine's "full" mode, Optimise Current its "current" mode.
_MODE = {"generate": "full", "optimise_current": "current"}

# The values the job runner and the workspace views send when the user gives no tuning.
DEFAULT_TUNING = {
    "max_iterations": 50,
    "run_chain": True,
    "run_cpsat": True,
    "cpsat_limit": 60.0,
    "max_chain_iterations": 10,
}
# The scheduler bridge's own tuning (the values the workspace sends by default).
SCHEDULER_TUNING = {"scheduler_seconds": 120.0, "scheduler_runs": 3, "scheduler_confirmation": 20.0}

# ── flags ──────────────────────────────────────────────────────────────────

RENDER_YAML = Path(__file__).resolve().parents[2] / "render.yaml"


def _coerce(value: str) -> bool | int | float | str:
    text = str(value).strip()
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    for kind in (int, float):
        try:
            return kind(text)
        except ValueError:
            continue
    return text


def production_flags(path: Path | None = None) -> dict[str, Any]:
    """The ``TIMETABLE_*`` settings the production web service sets (``render.yaml``)."""
    import yaml

    document = yaml.safe_load((path or RENDER_YAML).read_text(encoding="utf-8"))
    web = next(s for s in document["services"] if s.get("type") == "web")
    return {
        item["key"]: _coerce(item["value"])
        for item in web.get("envVars", [])
        if str(item.get("key", "")).startswith("TIMETABLE_") and "value" in item
    }


def parse_overrides(items: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--set expects KEY=VALUE, not {item!r}")
        out[key.strip()] = _coerce(value)
    return out


def effective_settings() -> dict[str, Any]:
    """Every ``TIMETABLE_*`` setting in force, so a run can be reproduced: the engine also reads
    ones this machine's environment (or ``.env``) set that ``render.yaml`` does not."""
    from django.conf import settings

    return {
        name: getattr(settings, name)
        for name in sorted(dir(settings))
        if name.startswith("TIMETABLE_")
    }


def effective_flags() -> dict[str, bool]:
    """What the engine will actually read, through the project's own helpers."""
    from core.services.timetable_flags import is_one_teacher_active, is_tiered_objective_enabled
    from core.services.timetable_optimisation_policy import (
        is_day_off_policy_active,
        is_instructor_priority_enabled,
    )

    # The teaching-time rules flag is deliberately not read here: only the flags module and the
    # engine's own helper may ask for it (tests/test_instructor_availability_engine.py). Its
    # TIMETABLE_* setting is in ``effective_settings()``, which every JSON result records.
    return {
        "one_teacher_per_section": is_one_teacher_active(),
        "day_off_policy": is_day_off_policy_active(),
        "instructor_priority": is_instructor_priority_enabled(),
        "tiered_objective": is_tiered_objective_enabled(),
    }


# ── the board ──────────────────────────────────────────────────────────────


def board_slots(scenario_id: int) -> frozenset[tuple]:
    """Every placed meeting as (board, section, day, start, room)."""
    from core.models import SectionPlacement

    return frozenset(
        SectionPlacement.objects.filter(board__scenario_id=scenario_id).values_list(
            "board_id", "term_section_id", "day", "start_time", "room"
        )
    )


def digest_of(value: object) -> str:
    """A short stable fingerprint of a board (a set of slots) or a score tuple.

    Two runs ended on the same board exactly when their digests are equal; the digest is what
    "repeatable" is judged on, so it covers every column of a slot, room included.
    """
    if isinstance(value, set | frozenset):
        value = sorted(value, key=repr)
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()[:12]


def _stable(value: object) -> object:
    """A value with every set and mapping put in sorted order, so equal maps digest equally
    whatever the hash order of their members."""
    if isinstance(value, dict):
        return tuple(sorted((repr(k), _stable(v)) for k, v in value.items()))
    if isinstance(value, set | frozenset):
        return tuple(sorted(repr(_stable(v)) for v in value))
    if isinstance(value, list | tuple):
        return tuple(_stable(v) for v in value)
    return repr(value)


def _times(slots: frozenset[tuple]) -> frozenset[tuple]:
    return frozenset(slot[:4] for slot in slots)


def measure_board(scenario_id: int) -> dict[str, Any]:
    """Measure the stored board with the project's own evaluators (None where unreadable)."""
    from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
    from core.services.timetable_flags import is_tiered_objective_enabled
    from core.services.timetable_optimizer_v2 import (
        INCOMPLETE_COLLECT,
        build_course_rigidity_for_scenario,
        build_course_tier_map_for_scenario,
        build_section_instructor_map_for_scenario,
        build_section_states_for_scenario,
        build_student_profiles_for_scenario,
    )
    from core.services.timetable_required_sections import (
        find_missing_required_sections,
        sections_short_by_course,
    )
    from core.services.timetable_student_assignment import decode_score
    from core.services.timetable_workspace import compute_scenario_safety_summary

    measured: dict[str, Any] = {"measurable": False, "errors": []}
    profiles = build_student_profiles_for_scenario(scenario_id)
    sections = build_section_states_for_scenario(scenario_id)
    slots = board_slots(scenario_id)
    measured.update(placed_meetings=len(slots), sections=len(sections), students=len(profiles))
    if not profiles or not sections:
        return measured
    try:
        teachers = build_section_instructor_map_for_scenario(
            scenario_id, incomplete=INCOMPLETE_COLLECT, release_unteachable=False
        )
    except Exception as exc:  # a board whose teacher map cannot be read still has students
        teachers = None
        measured["errors"].append(f"teacher map: {type(exc).__name__}: {exc}")
    evaluation = evaluate_generated_timetable_candidate(
        candidate_id="bench",
        generated_sections=sections,
        student_profiles=profiles,
        course_rigidity=build_course_rigidity_for_scenario(scenario_id),
        section_instructor_ids=teachers,
        course_tiers=build_course_tier_map_for_scenario(scenario_id)
        if is_tiered_objective_enabled()
        else None,
    )
    score = tuple(int(v) for v in evaluation.lexicographic_score)
    decoded = decode_score(score)
    schedule = (evaluation.quality_score or {}).get("instructor_schedule") or {}
    by_days = schedule.get("working_days_by_instructor") or {}
    safety = compute_scenario_safety_summary(scenario_id)
    same_board = safety.get("same_board_conflicts") or {}
    missing = sections_short_by_course(find_missing_required_sections(scenario_id))
    measured.update(
        measurable=True,
        score=list(score),
        unresolved_students=len(evaluation.unresolved_student_ids),
        student_gap_minutes=decoded.get("gap_minutes"),
        student_assigned_clashes=decoded.get("actual_assigned_clashes"),
        cross_board_conflicts=safety.get("cross_board_conflicts"),
        # The scenario-wide instructor count already includes every same-board instructor
        # clash (the runner gates the two separately); counting both would double them.
        hard_clashes=(
            int(same_board.get("same_course_overlaps", 0))
            + int(same_board.get("rooms", 0))
            + max(
                int(same_board.get("instructors", 0)),
                int(safety.get("instructor_clashes_scenario") or 0),
            )
        ),
        hard_clash_parts={
            "same_course_overlaps": same_board.get("same_course_overlaps", 0),
            "instructor_clashes": same_board.get("instructors", 0),
            "room_clashes": same_board.get("rooms", 0),
            "instructor_clashes_scenario": safety.get("instructor_clashes_scenario", 0),
        },
        required_sections_missing=sum(missing.values()),
        teachers=len(schedule.get("working_days_by_instructor") or {}),
        teacher_working_days=schedule.get("working_days"),
        teacher_idle_minutes=schedule.get("idle_minutes"),
        teachers_on_five_days=sum(1 for days in by_days.values() if days >= 5),
    )
    return measured


def safe_measure(scenario_id: int) -> dict[str, Any]:
    """:func:`measure_board`, but a board the evaluators cannot read (for example a scenario that
    raises ``IncompletePolicyPattern``) is a row saying so, not a missing row: the engine is
    still run and its own outcome reported."""
    try:
        return measure_board(scenario_id)
    except Exception as exc:
        return {"measurable": False, "errors": [f"{type(exc).__name__}: {exc}"]}


# ── stages ─────────────────────────────────────────────────────────────────

# (module, attribute, stage, probe). A probe says how to tell whether the stage changed
# something: "score" compares the lexicographic score of its input and output candidate (the
# in-memory search stages; the engine adopts a candidate only if ``stage_is_better`` says so, see
# ``stage_applied`` in the row), "cpsat" does the same for the polisher's result dict, "board"
# compares the stored placements, "returns"/"truthy" read a teacher-map stage's return value
# (the TEACHER map changed, not the board), "time" does not judge (the stage is only timed).
STAGE_FUNCTIONS: tuple[tuple[str, str, str, str], ...] = (
    ("core.services.timetable_autoplace", "auto_place_scenario", "place", "time"),
    (
        "core.services.timetable_local_search_v2",
        "diagnostic_driven_local_search",
        "search",
        "score",
    ),
    ("core.services.timetable_local_search_chains", "chain_local_search", "chain", "score"),
    ("core.services.timetable_cpsat_polisher", "polish_scenario_with_cpsat", "cpsat", "cpsat"),
    (
        "core.services.timetable_optimizer_v2",
        "persist_section_states_to_scenario",
        "persist",
        "board",
    ),
    ("core.services.timetable_rooming", "assign_rooms_to_board", "rooming", "board"),
    (
        "core.services.timetable_required_sections",
        "reinstate_missing_required_sections",
        "reinstate_required",
        "board",
    ),
    (
        "core.services.timetable_required_sections",
        "maybe_reinstate_missing_required_sections",
        "reinstate_required",
        "board",
    ),
    (
        "core.services.timetable_instructor_cap_repair",
        "repair_instructor_daily_overloads",
        "cap_repair",
        "board",
    ),
    (
        "core.services.timetable_instructor_cap_repair",
        "repair_instructor_clashes",
        "clash_repair",
        "board",
    ),
    (
        "core.services.timetable_instructor_cap_repair",
        "repair_instructor_availability",
        "availability_repair",
        "board",
    ),
    (
        "core.services.timetable_instructor_compaction",
        "compact_instructor_schedules",
        "compaction",
        "board",
    ),
    (
        "core.services.timetable_secondary_recovery_runner",
        "run_secondary_recovery_for_scenario",
        "recovery",
        "board",
    ),
)
# Methods of the runner's teacher state: who teaches what, re-picked mid-run and at the end.
TEACHER_STAGES: tuple[tuple[str, str, str], ...] = (
    ("swap", "teacher_swap", "returns"),  # None = kept the map in force
    ("finish", "teacher_final", "truthy"),
)


_UNJUDGED = object()


class StageRecorder:
    """Time and judge each stage call while active; restore every patched function on exit."""

    def __init__(self, scenario_id: int) -> None:
        self.scenario_id = scenario_id
        self.calls: list[dict[str, Any]] = []
        self._active: str | None = None
        self._undo: list[Callable[[], None]] = []

    def __enter__(self) -> StageRecorder:
        try:
            for module_name, attribute, stage, probe in STAGE_FUNCTIONS:
                module = importlib.import_module(module_name)
                self._patch(module, attribute, stage, probe)
            from core.services import timetable_v2_runner

            for method, stage, mode in TEACHER_STAGES:
                self._patch(timetable_v2_runner._RunTeachers, method, stage, mode)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc_info: object) -> None:
        while self._undo:
            self._undo.pop()()

    def _patch(self, owner: object, attribute: str, stage: str, probe: str) -> None:
        original = getattr(owner, attribute, None)
        if original is None:  # a renamed stage must show up as missing, not crash the run
            return
        wrapper = self._wrap(original, stage, probe)
        setattr(owner, attribute, wrapper)
        self._undo.append(lambda: setattr(owner, attribute, original))

    def _wrap(self, original: Callable, stage: str, probe: str) -> Callable:
        recorder = self

        @functools.wraps(original)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if recorder._active is not None:  # nested in a stage already being timed
                return original(*args, **kwargs)
            recorder._active = stage
            changed: bool | None = None
            seconds = 0.0
            digest: str | None = None
            try:
                before = recorder._probe(recorder._before, probe, args, kwargs)
                started = time.perf_counter()
                try:
                    out = original(*args, **kwargs)
                finally:
                    seconds = time.perf_counter() - started  # the stage itself, not the probes
                if before is not _UNJUDGED:
                    changed = recorder._probe(recorder._changed, probe, before, out)
                    changed = None if changed is _UNJUDGED else changed
                digest = recorder._probe(recorder._digest, probe, out)
                digest = None if digest is _UNJUDGED else digest
                return out
            finally:
                recorder._active = None
                recorder.calls.append(
                    {"stage": stage, "seconds": seconds, "changed": changed, "digest": digest}
                )

        return wrapper

    @staticmethod
    def _probe(function: Callable, *args: Any) -> Any:
        """A probe that fails is "not judged"; it never reaches the engine."""
        try:
            return function(*args)
        except Exception:
            return _UNJUDGED

    def _before(self, probe: str, args: tuple, kwargs: dict) -> Any:
        if probe == "board":
            return board_slots(self.scenario_id)
        if probe == "score":
            candidate = kwargs.get("best_candidate", args[0] if args else None)
            return tuple(candidate.lexicographic_score) if candidate is not None else None
        if probe == "cpsat":
            current = kwargs.get("current_eval")
            return tuple(current.lexicographic_score) if current is not None else None
        return None

    def _digest(self, probe: str, out: Any) -> str | None:
        """What the stage left behind: the stored board, or the score it ended on."""
        if probe in ("board", "time"):
            return digest_of(board_slots(self.scenario_id))
        if probe == "score":
            return digest_of(tuple(out.lexicographic_score))
        if probe == "cpsat":
            return None if out is None else digest_of(tuple(out["eval"].lexicographic_score))
        if probe == "returns":
            return None if out is None else digest_of(_stable(out))
        return None

    def _changed(self, probe: str, before: Any, out: Any) -> bool | None:
        if probe == "board":
            return board_slots(self.scenario_id) != before
        if probe == "score":
            return before is not None and tuple(out.lexicographic_score) != before
        if probe == "cpsat":
            return (
                out is not None
                and before is not None
                and tuple(out["eval"].lexicographic_score) != before
            )
        if probe == "returns":
            return out is not None
        if probe == "truthy":
            return bool(out)
        return None

    def totals(self) -> dict[str, dict[str, Any]]:
        by_stage: dict[str, dict[str, Any]] = {}
        for call in self.calls:
            row = by_stage.setdefault(
                call["stage"], {"calls": 0, "seconds": 0.0, "changed_calls": 0, "judged": 0}
            )
            row["calls"] += 1
            row["seconds"] += call["seconds"]
            if call["changed"] is not None:
                row["judged"] += 1
                row["changed_calls"] += int(call["changed"])
        for row in by_stage.values():
            row["seconds"] = round(row["seconds"], 3)
            # None: the stage is only timed (or never judged), not "changed nothing".
            row["changed"] = bool(row["changed_calls"]) if row["judged"] else None
        return by_stage


# ── one run ────────────────────────────────────────────────────────────────


def _outcome(result: dict) -> str:
    if "error" in result:
        return "error"
    action = (result.get("persist_result") or {}).get("action")
    return "rolled_back" if action == "rolled_back_safety_regression" else "kept"


def _result_fields(result: dict) -> dict[str, Any]:
    """What the run result dict says about the run, under the project's own keys."""
    regressions = (result.get("safety_regression") or {}).get("regressions") or []
    dropped = result.get("week_closure_dropped") or []
    second = result.get("second_try")
    recovery = result.get("secondary_recovery") or {}
    compaction = result.get("instructor_compaction") or {}
    return {
        "outcome": _outcome(result),
        "error": str(result["error"])[:300] if "error" in result else None,
        "blocking_reasons": [r.get("metric") for r in regressions],
        "stages_dropped": [d.get("stage") for d in dropped],
        # How each search ended (work budget / completed / wall-clock ceiling): a run a wall-clock
        # ceiling ended is one another run would not reproduce.
        "repeatable": (result.get("repeatability") or {}).get("repeatable"),
        "search_stops": (result.get("repeatability") or {}).get("searches"),
        "second_try": (
            {"kept": second.get("kept"), "second_outcome": second.get("second_outcome")}
            if isinstance(second, dict)
            else None
        ),
        "persist_action": (result.get("persist_result") or {}).get("action"),
        "engine_seconds": result.get("elapsed_seconds"),
        "reported_baseline_score": result.get("baseline_score"),
        "reported_final_score": result.get("final_score"),
        "candidate_final_score": result.get("candidate_final_score"),
        # The engine's own words. ``local_search_ran`` is True whenever the search ran, not when
        # it improved; chain and cpsat are True only when their result was adopted.
        "stage_applied": {
            "local_search_ran": result.get("local_search_applied"),
            "chain_adopted": result.get("chain_search_applied"),
            "cpsat_adopted": result.get("cpsat_polish_applied"),
            "recovery_applied": recovery.get("applied"),
            "compaction_relocations": len(compaction.get("relocations") or []),
            "search_undone_by_week_closure": "search_reverted_by_week_closure" in result,
        },
    }


def _delta(before: dict, after: dict, key: str) -> int | None:
    if before.get(key) is None or after.get(key) is None:
        return None
    return after[key] - before[key]


def _start_from_a_new_scaffold(scenario_id: int, action: str, options: dict[str, Any]) -> None:
    """Generate always begins on a freshly scaffolded scenario: budgets, no sections, no board.

    The classic engine starts from the cleared board; the newer engine builds its own sections,
    so the ones a saved scenario carries are removed (and with them their placements), and it
    is handed each course's weekly meetings from the budget, as the workspace hands them.
    """
    if action == "generate":
        from core.services.timetable_board_persistence import reset_scenario

        reset_scenario(scenario_id, keep_locked=False)
    elif action == "scheduler_generate":
        from core.models import TermSection
        from core.services.timetable_workspace import compute_scenario_budget

        options["meeting_patterns"] = {
            str(row["course_key"]): str(row.get("meeting_pattern") or "")
            for row in compute_scenario_budget(scenario_id)
            if row.get("course_key")
        }
        TermSection.objects.filter(scenario_id=scenario_id).delete()


def _run_engine(scenario_id: int, action: str, seed: int, options: dict[str, Any]) -> dict:
    if action in COMPARISON_ACTIONS:
        from core.models import TimetableScenario
        from scheduler.bridge import build_into_scenario

        scenario = TimetableScenario.objects.get(pk=scenario_id)
        return build_into_scenario(
            scenario_id,
            academic_year=str(scenario.academic_year),
            term=int(scenario.term),
            programs=list(scenario.programs or []),
            gender=str(scenario.gender),
            seconds=float(options["scheduler_seconds"]),
            runs=int(options["scheduler_runs"]),
            confirmation_seconds=float(options["scheduler_confirmation"]),
            seed=seed,
            meeting_patterns=options.get("meeting_patterns"),
        )
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded

    engine_options = {k: v for k, v in options.items() if k in DEFAULT_TUNING}
    return run_v2_optimisation_guarded(scenario_id, mode=_MODE[action], **engine_options)


def run_once(
    scenario_id: int,
    action: str,
    *,
    seed: int,
    tuning: dict[str, Any] | None = None,
    repeat: int = 1,
) -> dict[str, Any]:
    """Run one action on one scenario, measure it, and roll everything back.

    ``generate`` clears the board first (a Generate always starts from an empty scenario); the
    ``before`` measurement is the board as it was stored.
    """
    from core.services.timetable_run_seed import seeded_timetable_run

    if action not in ALL_ACTIONS:
        raise ValueError(f"action must be one of {ALL_ACTIONS}, not {action!r}")
    options = {**DEFAULT_TUNING, **SCHEDULER_TUNING, **(tuning or {})}
    row: dict[str, Any] = {
        "scenario_id": scenario_id,
        "action": action,
        "repeat": repeat,
        "seed": seed,
        "flags": effective_flags(),
    }
    slots_before: frozenset[tuple] = frozenset()
    with contextlib.suppress(Exception):
        slots_before = board_slots(scenario_id)
    try:
        with transaction.atomic():
            try:
                before = safe_measure(scenario_id)
                _start_from_a_new_scaffold(scenario_id, action, options)
                recorder = StageRecorder(scenario_id)
                started = time.perf_counter()
                with recorder, seeded_timetable_run(seed):
                    try:
                        result = _run_engine(scenario_id, action, seed, options)
                    except Exception as exc:  # the runner reports engine errors; this is the rest
                        result = {"error": f"{type(exc).__name__}: {exc}"}
                wall = time.perf_counter() - started
                after = safe_measure(scenario_id)
                slots_after = board_slots(scenario_id)
            finally:
                transaction.set_rollback(True)  # a benchmark never keeps what it did
    except Exception as exc:  # measurement failed outside the engine
        row.update(outcome="error", error=f"benchmark: {type(exc).__name__}: {exc}")
        return row
    totals = recorder.totals()
    staged = sum(t["seconds"] for t in totals.values())
    after_moves = _times(slots_after) - _times(slots_before)
    row.update(_result_fields(result))
    row.update(
        wall_seconds=round(wall, 3),
        stage_seconds={k: v["seconds"] for k, v in totals.items()},
        stage_changed={k: v["changed"] for k, v in totals.items()},
        stage_calls={k: v["calls"] for k, v in totals.items()},
        # Every stage call in order with what it left behind: the first call whose digest differs
        # between two runs is where they diverged.
        stage_trace=[
            {"stage": c["stage"], "seconds": round(c["seconds"], 3), "digest": c["digest"]}
            for c in recorder.calls
        ],
        board_digest=digest_of(slots_after),
        other_seconds=round(max(0.0, wall - staged), 3),
        before=before,
        after=after,
        delta={
            key: _delta(before, after, key)
            for key in (
                "unresolved_students",
                "student_gap_minutes",
                "cross_board_conflicts",
                "hard_clashes",
                "required_sections_missing",
                "teacher_working_days",
                "teacher_idle_minutes",
                "teachers_on_five_days",
            )
        },
        # Meetings that stand at a time they did not stand at before the run.
        meetings_moved=len(after_moves),
        board_changed=_times(slots_after) != _times(slots_before),
        rolled_back_to_saved_board=(
            None if row.get("error") else slots_before == board_slots(scenario_id)
        ),
    )
    return row


# ── summary ────────────────────────────────────────────────────────────────

_SPREAD_KEYS = (
    "wall_seconds",
    "unresolved_students",
    "student_gap_minutes",
    "cross_board_conflicts",
    "hard_clashes",
    "required_sections_missing",
    "teacher_working_days",
    "teacher_idle_minutes",
    "teachers_on_five_days",
    "meetings_moved",
)


def _flat(row: dict) -> dict[str, Any]:
    after = row.get("after") or {}
    return {
        **after,
        "wall_seconds": row.get("wall_seconds"),
        "meetings_moved": row.get("meetings_moved"),
    }


def _spread(values: list[float | int | None]) -> dict[str, Any] | None:
    numbers = [v for v in values if v is not None]
    if not numbers:
        return None
    return {
        "min": min(numbers),
        "median": statistics.median(numbers),
        "max": max(numbers),
        "n": len(numbers),
    }


def summarise(rows: list[dict]) -> list[dict[str, Any]]:
    """One entry per (scenario, action): outcome counts and the min/median/max of each number."""
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        groups.setdefault((row["scenario_id"], row["action"]), []).append(row)

    # An errored run restored the board it started from: its numbers say nothing about the engine.
    def _ok(group: list[dict]) -> list[dict]:
        return [r for r in group if r.get("outcome") != "error"]

    summary = []
    for (scenario_id, action), group in groups.items():
        outcomes: dict[str, int] = {}
        for row in group:
            outcomes[row["outcome"]] = outcomes.get(row["outcome"], 0) + 1
        stage_names = sorted({s for row in group for s in row.get("stage_seconds", {})})
        stages = {}
        for name in stage_names:
            present = [row for row in group if name in row.get("stage_seconds", {})]
            judged = [
                row["stage_changed"][name]
                for row in present
                if row["stage_changed"].get(name) is not None
            ]
            stages[name] = {
                "seconds": _spread([row["stage_seconds"][name] for row in present]),
                "runs_present": len(present),
                "runs_changed": sum(1 for v in judged if v),
                "runs_judged": len(judged),
            }
        summary.append(
            {
                "scenario_id": scenario_id,
                "action": action,
                "runs": len(group),
                "outcomes": outcomes,
                "runs_with_stage_dropped": sum(1 for r in group if r.get("stages_dropped")),
                "runs_with_second_try": sum(1 for r in group if r.get("second_try")),
                "blocking_reasons": sorted(
                    {reason for r in group for reason in r.get("blocking_reasons") or [] if reason}
                ),
                "spread": {
                    key: _spread([_flat(r).get(key) for r in _ok(group)]) for key in _SPREAD_KEYS
                },
                "before": group[0].get("before"),
                "distinct_final_scores": len(
                    {
                        tuple((r.get("after") or {}).get("score") or ())
                        for r in _ok(group)
                        if (r.get("after") or {}).get("score")
                    }
                ),
                "stages": stages,
            }
        )
    return summary


# ── plain table ────────────────────────────────────────────────────────────


def _cell(value: object) -> str:
    return "-" if value is None else str(value)


def _arrow(row: dict, key: str) -> str:
    before = (row.get("before") or {}).get(key)
    after = (row.get("after") or {}).get(key)
    return f"{_cell(before)}>{_cell(after)}"


def render_table(rows: list[dict]) -> str:
    """One line per run; ``a>b`` is the stored board before the run > after it."""
    header = (
        "scn",
        "action",
        "rep",
        "outcome",
        "unres",
        "gap_min",
        "xboard",
        "clash",
        "missing",
        "t_days",
        "t_idle",
        "t_5day",
        "moved",
        "dropped",
        "2nd",
        "wall_s",
    )
    lines = [header]
    for row in rows:
        second = row.get("second_try")
        lines.append(
            (
                str(row["scenario_id"]),
                row["action"],
                str(row["repeat"]),
                row["outcome"]
                + (
                    f" [{','.join(row['blocking_reasons'])}]" if row.get("blocking_reasons") else ""
                ),
                _arrow(row, "unresolved_students"),
                _arrow(row, "student_gap_minutes"),
                _arrow(row, "cross_board_conflicts"),
                _arrow(row, "hard_clashes"),
                _arrow(row, "required_sections_missing"),
                _arrow(row, "teacher_working_days"),
                _arrow(row, "teacher_idle_minutes"),
                _arrow(row, "teachers_on_five_days"),
                _cell(row.get("meetings_moved")),
                ",".join(row.get("stages_dropped") or []) or "-",
                (second["kept"] + "/" + str(second["second_outcome"])) if second else "-",
                _cell(row.get("wall_seconds")),
            )
        )
    return _columns(lines)


def render_stage_table(summary: list[dict]) -> str:
    lines = [("scn", "action", "stage", "runs", "sec_median", "sec_max", "changed")]
    for entry in summary:
        for name, stage in entry["stages"].items():
            seconds = stage["seconds"] or {}
            judged = stage["runs_judged"]
            lines.append(
                (
                    str(entry["scenario_id"]),
                    entry["action"],
                    name,
                    str(stage["runs_present"]),
                    _cell(seconds.get("median")),
                    _cell(seconds.get("max")),
                    f"{stage['runs_changed']}/{judged}" if judged else "(timed only)",
                )
            )
    return _columns(lines)


def _columns(lines: list[tuple[str, ...]]) -> str:
    widths = [max(len(line[i]) for line in lines) for i in range(len(lines[0]))]
    return "\n".join(
        "  ".join(cell.ljust(w) for cell, w in zip(line, widths, strict=False)).rstrip()
        for line in lines
    )


@contextlib.contextmanager
def flag_scope(flags: dict[str, Any]) -> Iterator[None]:
    """Apply settings for the duration of the benchmark (the engine reads them live)."""
    from django.test.utils import override_settings

    with override_settings(**flags):
        yield

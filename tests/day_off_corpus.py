"""A seeded corpus for the strict (legacy) acceptance relation (not collected).

``tests/test_instructor_day_off_policy.py`` replays it against the golden in
``tests/fixtures/day_off_legacy_policy_golden.json``, recorded at ``efad91a``
(the base the day-off policy starts from) by ``scratchpad``-style replay of this
module against that commit's ``timetable_optimisation_policy``::

    python record.py <efad91a checkout> tests/day_off_corpus.py <out.json>

where ``record.py`` configures Django settings, imports the base's policy
module and writes ``legacy_outputs(policy, legacy_corpus())`` for priority on
and off. Never re-record it from a branch that changes the policy: a golden that
follows the code proves nothing. This module imports nothing the day-off policy
added, so it runs unchanged against the base.
"""

from __future__ import annotations

import random

from core.services.timetable_assignment_models import TimetableEvaluationResult


def _metrics(rng: random.Random, teachers: list[str]) -> dict:
    days = {t: rng.randint(0, 5) for t in teachers}
    idle = {t: rng.choice((0, 15, 30, 60, 90, 120, 255)) for t in teachers}
    metrics = {
        "working_days": sum(days.values()),
        "idle_minutes": sum(idle.values()),
        "working_days_by_instructor": days,
        "idle_minutes_by_instructor": idle,
        "affected_instructors": len(teachers),
    }
    if teachers and rng.random() < 0.08:  # incomplete evidence, now and then
        broken = rng.choice(teachers)
        defect = rng.choice(("missing", "negative", "total"))
        if defect == "missing":
            del metrics["idle_minutes_by_instructor"][broken]
        elif defect == "negative":
            metrics["idle_minutes_by_instructor"][broken] = -1
        else:
            metrics["idle_minutes"] += 1
    return metrics


def _evaluation(rng: random.Random, teachers: list[str]) -> TimetableEvaluationResult:
    score = tuple(rng.randint(0, 2) for _ in range(4)) + tuple(rng.randint(0, 40) for _ in range(5))
    return TimetableEvaluationResult(
        "corpus",
        score,
        {},
        [],
        [],
        [],
        [],
        quality_score={"instructor_schedule": _metrics(rng, teachers)},
        instructor_overload_count=rng.randint(0, 1),
    )


def legacy_corpus(seed: int = 20261020, size: int = 600) -> list[tuple]:
    """``(candidate, incumbent, ceilings, mode)`` cases over 0-3 instructors."""
    rng = random.Random(seed)
    cases = []
    for _ in range(size):
        teachers = [str(i) for i in range(1, rng.randint(0, 3) + 1)]
        candidate, incumbent, ceilings = (_evaluation(rng, teachers) for _ in range(3))
        cases.append((candidate, incumbent, ceilings, rng.choice(("plain", "pinned", "idle"))))
    return cases


def legacy_outputs(policy, cases: list[tuple]) -> list[list]:
    """``[evaluation_is_better, evaluation_order_key(candidate)]`` per case, with
    the ceilings each mode passes (``policy`` is the module under test)."""
    out = []
    for candidate, incumbent, ceilings, mode in cases:
        kwargs: dict = {}
        if mode in ("pinned", "idle"):
            kwargs["feasibility_ceilings"] = ceilings
            kwargs["day_ceilings"] = ceilings.quality_score["instructor_schedule"]
        if mode == "idle":
            kwargs["idle_ceilings"] = incumbent.quality_score["instructor_schedule"]
        out.append(
            [
                bool(policy.evaluation_is_better(candidate, incumbent, **kwargs)),
                list(policy.evaluation_order_key(candidate)),
            ]
        )
    return out

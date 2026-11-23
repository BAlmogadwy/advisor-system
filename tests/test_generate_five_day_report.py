"""Generate under the day-off policy: the five-day pass runs, and the run says who is left.

The pass itself is pinned in ``test_five_day_repair``; here is where the run calls it (only with
the policy active and one teacher per section, once, on the evaluated winner's board) and how the
run reports the teachers still on five days.
"""

from __future__ import annotations

import pytest

from core.services.timetable_v2_runner import day_off_audit
from tests.one_teacher_support import FLAG_ON
from tests.test_one_teacher_flag_parity import build_corpus

RUN = {"max_iterations": 3, "run_chain": False, "run_cpsat": False, "max_chain_iterations": 2}


@pytest.fixture
def flags(settings):
    for name, value in FLAG_ON.items():
        setattr(settings, name, value)
    settings.TIMETABLE_SECONDARY_RECOVERY_ENABLED = False
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_TIME_BUDGET_SECONDS = 5
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY = True
    return settings


def _generate(monkeypatch, report):
    """Run Generate on the corpus with the pass replaced by a recorder; returns (result, calls)."""
    from core.services import timetable_five_day_repair as module
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded

    calls: list[dict] = []

    def recorder(**kwargs):
        calls.append(kwargs)
        return None, report

    monkeypatch.setattr(module, "repair_five_day_teachers", recorder)
    sc = build_corpus()
    result = run_v2_optimisation_guarded(
        sc.id, mode="full", strategies=["compact"], checkpoint=_checkpoint, **RUN
    )
    return result, calls


def _checkpoint() -> None:
    """The job's cancellation check; the pass must be handed this very function."""


@pytest.mark.django_db
def test_generate_runs_the_pass_once_on_the_winner_under_the_policy(flags, monkeypatch) -> None:
    result, calls = _generate(monkeypatch, None)  # nothing to do: no key in the result
    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["sections_by_id"] and kwargs["section_instructor_ids"]
    assert kwargs["checkpoint"] is _checkpoint
    assert "five_day_repair" not in result


@pytest.mark.django_db
def test_generate_reports_the_pass_when_it_evaluated_something(flags, monkeypatch) -> None:
    from core.services import timetable_five_day_repair as module
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded

    report = {"before": ["1"], "after": ["1"], "teachers": [], "evaluations": 4}
    monkeypatch.setattr(module, "repair_five_day_teachers", lambda **_kw: (_Evaluated(), report))
    sc = build_corpus()

    result = run_v2_optimisation_guarded(sc.id, mode="full", strategies=["compact"], **RUN)

    assert result["five_day_repair"] == report


class _Evaluated:
    """An evaluation stand-in the run never reads from when the pass kept no move."""


@pytest.mark.django_db
def test_generate_does_not_run_the_pass_with_the_policy_off(flags, monkeypatch) -> None:
    flags.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY = False

    result, calls = _generate(monkeypatch, {})

    assert calls == []
    assert "five_day_repair" not in result


@pytest.mark.django_db
def test_generate_does_not_run_the_pass_without_one_teacher_per_section(flags, monkeypatch) -> None:
    flags.TIMETABLE_ONE_TEACHER_PER_SECTION = False

    result, calls = _generate(monkeypatch, {})

    assert calls == []
    assert "five_day_repair" not in result


def test_the_audit_lists_every_teacher_still_on_five_days() -> None:
    before = {"working_days_by_instructor": {}}  # Generate starts from an empty board
    after = {"working_days_by_instructor": {"1": 5, "2": 4, "3": 5, "4": 3}}

    audit = day_off_audit(before, after)

    assert audit["five_day_teachers"] == [
        {"instructor_id": "1", "days": 5},
        {"instructor_id": "3", "days": 5},
    ]
    # The older view of the same board is unchanged, and still blind to "before" (0 days).
    assert audit["five_day_after"] == 2 and audit["blocking"] is False
    assert [row["days_before"] for row in audit["fifth_day_added"]] == [0, 0]


def test_the_audit_lists_nobody_when_nobody_is_on_five_days() -> None:
    audit = day_off_audit({}, {"working_days_by_instructor": {"1": 4, "2": 2}})

    assert audit["five_day_teachers"] == []


def _placer_contexts(monkeypatch) -> list[bool]:
    """Run Generate and return the "align shared days" setting each placer call ran under."""
    from core.services import timetable_autoplace as placer
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded

    seen: list[bool] = []
    real = placer.auto_place_scenario

    def spy(*args, **kwargs):
        seen.append(placer._align_shared_section_days.get())
        return real(*args, **kwargs)

    monkeypatch.setattr(placer, "auto_place_scenario", spy)
    sc = build_corpus()
    run_v2_optimisation_guarded(sc.id, mode="full", strategies=["compact"], **RUN)
    return seen


@pytest.mark.django_db
def test_every_generate_placement_aligns_shared_sections_under_the_policy(
    flags, monkeypatch
) -> None:
    seen = _placer_contexts(monkeypatch)

    # The candidate build and the winner's re-placement (the skeleton must be built as it was).
    assert len(seen) >= 2 and all(seen)


@pytest.mark.django_db
def test_no_generate_placement_aligns_anything_with_the_policy_off(flags, monkeypatch) -> None:
    flags.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY = False

    seen = _placer_contexts(monkeypatch)

    assert len(seen) >= 2 and not any(seen)

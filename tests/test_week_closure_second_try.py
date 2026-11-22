"""The second try: the guard dropped the search and the run-start re-pick had changed the map.

When the week-closure guard drops the SEARCH stage and C5 (the run-start re-pick) had changed
who teaches what, the run is tried once more under the SAVED map, so the search's gains are
not lost to a re-pick that was only a preference. The better outcome is kept by the project's
own relation (``stage_is_better``); otherwise the first attempt stands.

A small fixture cannot make the real C5 swap (that is scenario 663 itself), so C5 is a
controlled stand-in: it leaves P4 (Dr A's Wednesday section) without a teacher. The search is
a stand-in too: under that map it moves X to Thursday (the closure then gives Dr A a fifth
day, which the guard drops); under the saved map it moves X to Wednesday right after P4 (a
harmless change). See tests/test_week_closure_guard.py for the guard itself.
"""

from __future__ import annotations

import dataclasses

import pytest

from core.models import SectionInstructor
from core.services import section_teacher_assignment as sta
from tests.one_teacher_support import FLAG_ON
from tests.test_one_teacher_day_off_policy import RUN, _enrol
from tests.test_week_closure_guard import _board, _locked_but_x, _x_day

THURSDAY, WEDNESDAY, WED_AFTER_P4 = 4, 3, (555, 630)  # the optimiser's day index; 09:15-10:30


@pytest.fixture
def flags(settings):
    for name, value in FLAG_ON.items():
        setattr(settings, name, value)
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY = True
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_TIME_BUDGET_SECONDS = 5
    settings.TIMETABLE_SECONDARY_RECOVERY_ENABLED = False
    return settings


class _Board:
    """The board, the C5 stand-in and the search stand-in, patched in."""

    def __init__(
        self, monkeypatch, *, search_fails_on_saved_map: bool = False, harmless_search: bool = False
    ) -> None:
        from core.services import timetable_local_search_v2, timetable_optimizer_v2
        from core.services.timetable_v2_runner import _RunTeachers

        self.monkeypatch = monkeypatch
        self.sc, self.a, self.x = _board()
        _enrol(self.sc, self.x)
        _locked_but_x(self.sc, self.x)
        p4 = SectionInstructor.objects.get(scenario=self.sc, course_key__contains="AI404")
        self.p4_key = f"{p4.course_key}_S1"
        self.searches: list[str] = []
        real_start = _RunTeachers.start_run
        real_search = timetable_local_search_v2.diagnostic_driven_local_search

        def start_run(this, bounds):
            real_start(this, bounds)
            this.start = dataclasses.replace(
                this.start,
                teacher={**this.start.teacher, self.p4_key: None},
                status={**this.start.status, self.p4_key: sta.STATUS_TBD},
            )
            return this.pin(this.start)

        def search(**kwargs):
            out = real_search(**kwargs)
            on_c5_map = sta.current_pin(self.sc.id).teacher.get(self.p4_key) is None
            self.searches.append("c5_map" if on_c5_map else "saved_map")
            if not on_c5_map and search_fails_on_saved_map:
                raise RuntimeError("the second try's search fails")
            moved = kwargs["sections_by_id"][f"{self.x}_S1"]
            if on_c5_map and not harmless_search:
                meeting = dataclasses.replace(moved.meetings[0], day=THURSDAY)
            else:
                meeting = dataclasses.replace(
                    moved.meetings[0],
                    day=WEDNESDAY,
                    start_min=WED_AFTER_P4[0],
                    end_min=WED_AFTER_P4[1],
                )
            kwargs["sections_by_id"][f"{self.x}_S1"] = dataclasses.replace(
                moved, meetings=(meeting,)
            )
            return dataclasses.replace(out)

        monkeypatch.setattr(_RunTeachers, "start_run", start_run)
        monkeypatch.setattr(timetable_local_search_v2, "diagnostic_driven_local_search", search)
        monkeypatch.setattr(
            timetable_optimizer_v2, "stage_is_better", lambda out, inp: out is not inp
        )

    def run(self, **kwargs):
        from core.services import timetable_v2_runner
        from core.services.timetable_v2_runner import run_v2_optimisation_guarded

        self.restores: list = []
        real_restore = timetable_v2_runner.restore_scenario

        def restore(scenario_id, snapshot):
            self.restores.append(snapshot)
            return real_restore(scenario_id, snapshot)

        self.monkeypatch.setattr(timetable_v2_runner, "restore_scenario", restore)

        return run_v2_optimisation_guarded(self.sc.id, mode="current", **RUN, **kwargs)


@pytest.mark.django_db
def test_s1_the_second_try_under_the_saved_map_keeps_the_search_the_better_outcome(
    flags, monkeypatch
) -> None:
    from core.services import timetable_v2_runner

    board = _Board(monkeypatch)
    monkeypatch.setattr(timetable_v2_runner, "_second_is_better", lambda *args: True)

    result = board.run()

    assert board.searches == ["c5_map", "saved_map"]
    assert result["safety_blocked"] is False, result.get("safety_regression")
    assert result["persist_result"]["action"] == "updated_in_place"
    assert _x_day(board.sc, board.x) == "WED"  # the second try's search was kept
    second_try = result["second_try"]
    assert second_try["kept"] == "second" and second_try["second_outcome"] == "kept"
    assert second_try["reason"] == "search_dropped_by_week_closure_after_run_start_repick"
    assert second_try["first_score"] and second_try["second_score"]
    assert "week_closure_dropped" not in result  # the kept attempt dropped nothing
    assert result["section_teachers"]["run_start"]["changed_sections"] == []
    # The second run began from the board the first began with: one restore, nothing after.
    assert len(board.restores) == 1


@pytest.mark.django_db
def test_s2_a_second_try_that_is_not_better_leaves_the_first_attempt_standing(
    flags, monkeypatch
) -> None:
    board = _Board(monkeypatch)  # the real relation: equal boards, so not better

    result = board.run()

    assert board.searches == ["c5_map", "saved_map"]
    assert result["second_try"]["kept"] == "first"
    assert result["second_try"]["second_outcome"] == "not_better"
    assert [d["stage"] for d in result["week_closure_dropped"]] == ["search"]  # the first's
    assert _x_day(board.sc, board.x) == "SUN"  # the first attempt's board, restored
    assert len(board.restores) == 2  # the start board for the second run, the first's after
    assert result["safety_blocked"] is False


@pytest.mark.django_db
def test_s3_a_second_try_that_fails_leaves_the_first_attempt_standing(flags, monkeypatch) -> None:
    board = _Board(monkeypatch, search_fails_on_saved_map=True)

    result = board.run()

    assert result["second_try"]["kept"] == "first"
    assert result["second_try"]["second_outcome"] == "error"
    assert result["second_try"]["second_score"] is None
    assert _x_day(board.sc, board.x) == "SUN"
    assert [d["stage"] for d in result["week_closure_dropped"]] == ["search"]


@pytest.mark.django_db
def test_s4_a_second_try_the_final_gate_blocks_leaves_the_first_attempt_standing(
    flags, monkeypatch
) -> None:
    from core.services import timetable_v2_runner

    board = _Board(monkeypatch)
    real_kept = timetable_v2_runner._attempt_kept
    verdicts = iter([True, False])  # first attempt kept, second one blocked
    monkeypatch.setattr(timetable_v2_runner, "_attempt_kept", lambda r: next(verdicts))
    assert real_kept({"error": "x"}) is False  # the helper itself

    result = board.run()

    assert result["second_try"]["kept"] == "first"
    assert result["second_try"]["second_outcome"] == "blocked"
    assert _x_day(board.sc, board.x) == "SUN"


@pytest.mark.django_db
def test_s5_a_first_attempt_the_final_gate_blocked_gives_way_to_a_kept_second_without_a_comparison(
    flags, monkeypatch
) -> None:
    from core.services import timetable_v2_runner

    board = _Board(monkeypatch)
    verdicts = iter([False, True])  # first attempt blocked, second one kept
    monkeypatch.setattr(timetable_v2_runner, "_attempt_kept", lambda r: next(verdicts))

    def never(*args):
        raise AssertionError("a blocked first attempt is not compared")

    monkeypatch.setattr(timetable_v2_runner, "_second_is_better", never)

    result = board.run()

    assert result["second_try"]["kept"] == "second"
    assert _x_day(board.sc, board.x) == "WED"


@pytest.mark.django_db
def test_s6_no_second_try_when_the_run_start_repick_changed_nothing(flags, monkeypatch) -> None:
    """The search is dropped by the guard but C5 left the map as it was: a second run would
    repeat the first, so none is made and the result carries no extra key."""
    from core.services import timetable_local_search_v2, timetable_optimizer_v2
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded

    sc, _a, x = _board()
    _enrol(sc, x)
    _locked_but_x(sc, x)
    calls: list[int] = []
    real = timetable_local_search_v2.diagnostic_driven_local_search

    def search(**kwargs):
        calls.append(1)
        out = real(**kwargs)
        moved = kwargs["sections_by_id"][f"{x}_S1"]
        kwargs["sections_by_id"][f"{x}_S1"] = dataclasses.replace(
            moved, meetings=(dataclasses.replace(moved.meetings[0], day=THURSDAY),)
        )
        return dataclasses.replace(out)

    monkeypatch.setattr(timetable_local_search_v2, "diagnostic_driven_local_search", search)
    monkeypatch.setattr(timetable_optimizer_v2, "stage_is_better", lambda out, inp: out is not inp)

    result = run_v2_optimisation_guarded(sc.id, mode="current", **RUN)

    assert [d["stage"] for d in result["week_closure_dropped"]] == ["search"]
    assert calls == [1]
    assert "second_try" not in result
    assert _x_day(sc, x) == "SUN"


@pytest.mark.django_db
def test_s7_no_second_try_when_the_guard_dropped_nothing(flags, monkeypatch) -> None:
    """C5 changed the map but the search is harmless: the guard drops nothing, so the
    first run stands as it is, with no second run and no extra key."""
    board = _Board(monkeypatch, harmless_search=True)

    result = board.run()

    assert board.searches == ["c5_map"]
    assert "second_try" not in result and "week_closure_dropped" not in result
    assert _x_day(board.sc, board.x) == "WED"
    assert result["safety_blocked"] is False


@pytest.mark.django_db
def test_s8_the_cancel_hook_is_honoured_before_the_second_try_starts(flags, monkeypatch) -> None:
    import sys

    class Cancelled(Exception):
        pass

    def checkpoint():
        if sys._getframe(1).f_code.co_name == "_second_try":
            raise Cancelled

    board = _Board(monkeypatch)

    with pytest.raises(Cancelled):
        board.run(checkpoint=checkpoint)

    assert board.searches == ["c5_map"]  # the second run never began


def test_s9_an_attempts_score_is_the_one_it_reached() -> None:
    from core.services.timetable_v2_runner import _attempt_score

    blocked = {
        "persist_result": {"action": "rolled_back_safety_regression"},
        "candidate_final_score": (1, 2),
        "final_score": [9, 9],
    }
    kept = {"persist_result": {"action": "updated_in_place"}, "final_score": (3, 4)}
    assert _attempt_score(blocked) == [1, 2]
    assert _attempt_score(kept) == [3, 4]
    assert _attempt_score({"error": "x"}) is None


@pytest.mark.django_db
def test_s10_an_exception_the_second_run_lets_escape_leaves_the_first_attempt_standing(
    flags, monkeypatch
) -> None:
    from core.services import timetable_v2_runner

    board = _Board(monkeypatch)
    real = timetable_v2_runner._attempt_evaluation
    calls: list[int] = []

    def evaluation(scenario_id, teachers):
        calls.append(1)
        if len(calls) == 2:  # the second attempt's board, after it kept its changes
            raise RuntimeError("evaluating the second attempt failed")
        return real(scenario_id, teachers)

    monkeypatch.setattr(timetable_v2_runner, "_attempt_evaluation", evaluation)

    result = board.run()

    assert result["second_try"]["kept"] == "first"
    assert result["second_try"]["second_outcome"] == "error"
    assert _x_day(board.sc, board.x) == "SUN"  # the first attempt's board, not the second's
    assert len(board.restores) == 2  # once for the second run, once after it failed
    assert board.restores[1] is not board.restores[0]  # the first attempt's, not the start's
    assert result["safety_blocked"] is False


@pytest.mark.django_db
def test_s11_a_cancellation_during_a_failed_second_run_is_not_swallowed(flags, monkeypatch) -> None:
    import sys

    from core.services import timetable_v2_runner

    class Cancelled(Exception):
        pass

    seen: list[int] = []

    def checkpoint():
        if sys._getframe(1).f_code.co_name == "_second_try":
            seen.append(1)
            if len(seen) == 2:  # the call made after the second run raised
                raise Cancelled

    def evaluation(scenario_id, teachers):
        raise RuntimeError("the second attempt failed")

    board = _Board(monkeypatch)
    real = timetable_v2_runner._attempt_evaluation
    calls: list[int] = []

    def flaky(scenario_id, teachers):
        calls.append(1)
        return real(scenario_id, teachers) if len(calls) == 1 else evaluation(scenario_id, teachers)

    monkeypatch.setattr(timetable_v2_runner, "_attempt_evaluation", flaky)

    with pytest.raises(Cancelled):
        board.run(checkpoint=checkpoint)

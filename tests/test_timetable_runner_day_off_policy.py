"""The runner's final gate under the day-off policy (R1-R6).

Optimise Current: the final board is judged per instructor against the saved
board, with the policy the optimiser carried in its result — only when that
policy starts from the runner's own anchor; otherwise the strict gates decide.
Every breach blocks. R2 reads the optimiser's own (like-for-like) score.
Generate only reports a 5th day added against the saved board.

The optimiser is replaced by a stand-in that moves placements and reports
scores, as in ``test_timetable_instructor_priority``; teachers come from a
fixed map (the union charging).
"""

from __future__ import annotations

import dataclasses

import pytest

from core.models import DeliveryBoard, SectionPlacement, TermSection, TimetableScenario
from core.services.timetable_optimisation_policy import DayOffPolicy

BASE = (0, 0, 0, 0, 1000, 0, 0, 0, 0)
GAIN = (0, 0, 0, 0, 990, 0, 0, 0, 0)
WORSE = (0, 0, 0, 0, 1010, 0, 0, 0, 0)


@pytest.fixture
def policy_on(settings):
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY = True
    return settings


def _board(placements):
    """Sections ``[(code, day, start, end)]`` on one board (no students)."""
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="Day off")
    board = DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)
    for code, day, start, end in placements:
        ts = TermSection.objects.create(
            scenario=scenario,
            course_key=code,
            course_code=code,
            course_number=code,
            course_name=code,
            section="S1",
            available_capacity=30,
        )
        SectionPlacement.objects.create(
            board=board, term_section=ts, day=day, start_time=start, end_time=end
        )
    return scenario, board


def _run(
    monkeypatch,
    placements,
    teachers: dict[str, int],
    moves,
    *,
    final=BASE,
    before_passes=None,
    carry="own",
    mode="current",
):
    """Run the guarded runner over a stand-in optimiser that applies ``moves``
    and reports ``final`` (and ``before_passes``, the like-for-like score).
    ``carry``: the policy the result carries — the runner's own ("own"), none
    ("missing") or one from another start ("other")."""
    from core.services import timetable_optimizer_v2 as optimizer
    from core.services import timetable_v2_runner as runner
    from core.services.timetable_board_persistence import snapshot_scenario

    scenario, board = _board(placements)
    teacher_map = {f"{code}_S1": frozenset({teachers[code]}) for code, *_rest in placements}
    monkeypatch.setattr(
        optimizer, "build_section_instructor_map_for_scenario", lambda _sid: teacher_map
    )
    monkeypatch.setattr(runner, "compute_scenario_safety_summary", lambda _sid: {})
    seen: dict = {}

    def apply_moves():
        for code, day, start, end in moves:
            SectionPlacement.objects.filter(board=board, term_section__course_code=code).update(
                day=day, start_time=start, end_time=end
            )

    def optimise(**kwargs):
        seen.update(kwargs)
        apply_moves()
        result = {"baseline_score": list(BASE), "final_score": list(final)}
        if before_passes is not None:
            result["score_before_instructor_passes"] = list(before_passes)
        anchor = kwargs.get("day_off_anchor")
        if carry == "own":
            result["day_off_policy"] = DayOffPolicy(anchor, BASE, "saved_board").to_dict()
        elif carry == "other":
            first = next(iter(anchor.idle))
            other = dataclasses.replace(
                anchor,
                idle={**anchor.idle, first: anchor.idle[first] + 1},
                total_idle=anchor.total_idle + 1,
            )
            result["day_off_policy"] = DayOffPolicy(other, BASE, "saved_board").to_dict()
        return result

    def generate(**kwargs):
        seen.update(kwargs)
        apply_moves()
        return {"final_score": list(final), "day_off_policy": {"version": 1, "source": "test"}}

    monkeypatch.setattr(optimizer, "optimise_current_timetable", optimise)
    monkeypatch.setattr(optimizer, "optimise_scenario_timetable_v2", generate)
    original = snapshot_scenario(scenario.id)
    result = runner.run_v2_optimisation_guarded(scenario.id, mode=mode)
    return result, seen, snapshot_scenario(scenario.id) == original


def _metrics(result) -> list[str]:
    return sorted(r["metric"] for r in result["safety_regression"]["regressions"])


# The strict gates' own fixture (test_timetable_instructor_priority): teacher 2
# gains 30 idle minutes while teacher 1 loses 120 — the total falls by 90.
IDLE_TRADE = [
    ("A", "SUN", "09:00", "10:00"),
    ("B", "SUN", "12:00", "13:00"),
    ("C", "SUN", "09:00", "10:00"),
    ("D", "SUN", "10:00", "11:00"),
]
IDLE_TEACHERS = {"A": 1, "B": 1, "C": 2, "D": 2}
IDLE_MOVES = [("B", "SUN", "10:00", "11:00"), ("D", "SUN", "10:30", "11:30")]


@pytest.mark.django_db
def test_r1_one_teachers_idle_may_rise_when_the_total_falls(policy_on, monkeypatch) -> None:
    result, seen, restored = _run(monkeypatch, IDLE_TRADE, IDLE_TEACHERS, IDLE_MOVES)

    assert result["safety_blocked"] is False, result["safety_regression"]
    assert not restored
    assert seen["day_off_anchor"].total_idle == 120
    report = result["instructor_schedule_metric"]["day_off_policy"]
    assert (report["total_idle_before"], report["total_idle_after"]) == (120, 30)
    assert report["unsanctioned_idle_rise"] == 30
    assert report["teachers"]["2"]["idle_change"] == 30
    assert "day_off_policy_fallback" not in result


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("carry", "fallback"), [("missing", "missing"), ("other", "anchor_mismatch")]
)
def test_r5_without_the_runs_own_policy_the_strict_gates_decide(
    policy_on, monkeypatch, carry, fallback
) -> None:
    result, _seen, restored = _run(monkeypatch, IDLE_TRADE, IDLE_TEACHERS, IDLE_MOVES, carry=carry)

    assert result["safety_blocked"] is True and restored
    assert result["day_off_policy_fallback"] == fallback
    assert any(
        r.get("instructor_id") == "2" and r["metric"] == "instructor_idle_minutes"
        for r in result["safety_regression"]["regressions"]
    )
    assert "day_off_policy" not in result["instructor_schedule_metric"]


FOUR_DAYS = [
    ("A", "SUN", "09:00", "10:00"),
    ("B", "MON", "09:00", "10:00"),
    ("C", "TUE", "09:00", "10:00"),
    ("D", "WED", "09:00", "10:00"),
    ("E", "WED", "10:00", "11:00"),
]


@pytest.mark.django_db
def test_r2_a_fifth_working_day_blocks_even_with_a_student_gain(policy_on, monkeypatch) -> None:
    result, _seen, restored = _run(
        monkeypatch,
        FOUR_DAYS,
        dict.fromkeys("ABCDE", 1),
        [("E", "THU", "09:00", "10:00")],
        final=GAIN,
    )

    assert result["safety_blocked"] is True and restored
    assert _metrics(result) == ["instructor_fifth_working_day"]
    assert result["safety_regression"]["regressions"][0]["instructor_ids"] == ["1"]


THREE_DAYS = [
    ("A", "SUN", "09:00", "10:00"),
    ("F", "SUN", "10:00", "11:00"),
    ("B", "MON", "09:00", "10:00"),
    ("C", "TUE", "09:00", "10:00"),
    ("G", "TUE", "10:00", "11:00"),
]
SPARE_DAY = [("G", "WED", "09:00", "10:00")]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("final", "before_passes", "blocked"),
    [
        (GAIN, None, False),  # the optimiser's board is better for students
        (BASE, None, True),  # level: nothing paid for the day
        (WORSE, GAIN, False),  # only a mandatory repair cost students afterwards
        (GAIN, BASE, True),  # the optimiser's own board gained nothing
    ],
)
def test_r3_a_spare_day_given_up_needs_the_optimisers_own_student_gain(
    policy_on, monkeypatch, final, before_passes, blocked
) -> None:
    result, _seen, restored = _run(
        monkeypatch,
        THREE_DAYS,
        dict.fromkeys("ABCFG", 1),
        SPARE_DAY,
        final=final,
        before_passes=before_passes,
    )

    assert result["safety_blocked"] is blocked and restored is blocked
    if blocked:
        assert _metrics(result) == ["instructor_spare_day_without_student_gain"]
    else:
        teacher = result["instructor_schedule_metric"]["day_off_policy"]["teachers"]["1"]
        assert (teacher["days_before"], teacher["days_after"], teacher["days_given_up"]) == (
            3,
            4,
            1,
        )


@pytest.mark.django_db
@pytest.mark.parametrize(("start", "blocked"), [("11:00", False), ("11:01", True)])
def test_r4_sixty_idle_minutes_per_spare_day_and_not_one_more(
    policy_on, monkeypatch, start, blocked
) -> None:
    end = f"12:{start[3:]}"
    result, _seen, restored = _run(
        monkeypatch,
        THREE_DAYS,
        dict.fromkeys("ABCFG", 1),
        [*SPARE_DAY, ("F", "SUN", start, end)],
        final=GAIN,
    )

    assert result["safety_blocked"] is blocked and restored is blocked
    if blocked:
        assert _metrics(result) == ["instructor_spare_day_idle_over_allowance"]
        assert result["safety_regression"]["regressions"][0]["after"] == 1


@pytest.mark.django_db
@pytest.mark.parametrize(("flag", "action"), [(True, "updated_in_place"), (False, "no_change")])
def test_optimise_current_keeps_an_r4_trade_only_under_the_policy(
    settings, monkeypatch, flag, action
) -> None:
    """Called directly (no runner, so no anchor handed in), Optimise Current
    starts from its own baseline. The search moves teacher 1's idle 120 -> 0
    and teacher 2's 0 -> 30; the strict veto refuses it, the policy keeps it."""
    from core.models import CourseInstructor, Instructor
    from core.services import timetable_optimizer_v2 as optimizer
    from core.services.timetable_assignment_models import RiskTier, SectionMeeting, StudentProfile
    from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate

    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY = flag
    settings.TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED = False
    settings.TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED = False
    settings.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = False
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED = False
    settings.TIMETABLE_SECONDARY_RECOVERY_ENABLED = False
    scenario, _board_obj = _board(
        [(code, "SUN", start, end) for code, _day, start, end in IDLE_TRADE]
    )
    scenario.gender, scenario.programs = "M", ["CS"]
    scenario.save(update_fields=["gender", "programs"])
    people = {
        n: Instructor.objects.create(full_name=f"T{n}", normalised_name=f"t{n}") for n in (1, 2)
    }
    for code, n in IDLE_TEACHERS.items():
        CourseInstructor.objects.create(
            program="CS", course_code=code, section="M", instructor=people[n], role="primary"
        )
    monkeypatch.setattr(
        optimizer,
        "build_student_profiles_for_scenario",
        lambda _sid: {"s": StudentProfile("s", "CS", ["A"], RiskTier.C, 0)},
    )
    monkeypatch.setattr(optimizer, "build_course_rigidity_for_scenario", lambda _sid: {})
    monkeypatch.setattr(optimizer, "_build_pattern_catalog_for_scenario", lambda _sid: {})

    def improve(**kwargs):
        sections = kwargs["sections_by_id"]
        sections["B_S1"].meetings = [SectionMeeting(0, 600, 660)]
        sections["D_S1"].meetings = [SectionMeeting(0, 630, 690)]
        return evaluate_generated_timetable_candidate(
            "trade",
            list(sections.values()),
            kwargs["student_profiles"],
            {},
            kwargs["section_instructor_ids"],
            kwargs["course_tiers"],
        )

    monkeypatch.setattr(
        "core.services.timetable_local_search_v2.diagnostic_driven_local_search", improve
    )

    result = optimizer.optimise_current_timetable(
        scenario.id, run_chain_search=False, run_cpsat_polish=False
    )

    assert result["persist_result"]["action"] == action
    assert ("day_off_policy" in result) is flag
    if flag:
        assert result["day_off_policy"]["anchor"]["total_idle"] == 120
        assert result["quality_score"]["instructor_schedule"]["idle_minutes"] == 30
        assert SectionPlacement.objects.get(term_section__course_code="D").start_time == "10:30"


@pytest.mark.django_db
def test_r6_generate_reports_a_fifth_day_and_does_not_block(policy_on, monkeypatch) -> None:
    result, seen, restored = _run(
        monkeypatch,
        FOUR_DAYS,
        dict.fromkeys("ABCDE", 1),
        [("E", "THU", "09:00", "10:00")],
        mode="full",
    )

    assert result["safety_blocked"] is False and not restored
    assert "day_off_anchor" not in seen  # Generate is judged against its own winner
    audit = result["day_off_policy"]["audit"]
    assert audit["blocking"] is False
    assert audit["fifth_day_added"] == [{"instructor_id": "1", "days_before": 4, "days_after": 5}]
    assert (audit["five_day_before"], audit["five_day_after"]) == (0, 1)

"""Generate: the pass that brings a teacher on five working days down to four.

``repair_five_day_teachers`` works on a small board of sections with a real pattern catalog and
the real day-off policy; only the student evaluation is a stand-in (each test says what the
students' score does), so the pass's own decisions are what is under test: which moves it
enumerates, what it keeps, what it never touches.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.services import timetable_five_day_repair as repair
from core.services.timetable_assignment_models import (
    CanonicalPattern,
    SectionMeeting,
    SectionState,
    TimetableEvaluationResult,
)
from core.services.timetable_optimisation_policy import (
    DayOffAnchor,
    DayOffPolicy,
    day_off_scope,
    instructor_schedule_metrics,
)

SUN, MON, TUE, WED, THU = range(5)
# Back-to-back 75-minute slots: a day with sessions in neighbouring slots has no idle time.
S1, S2, S3 = 9 * 60, 9 * 60 + 75, 9 * 60 + 150


@pytest.fixture(autouse=True)
def policy_flags(settings):
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY = True
    settings.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED = False
    settings.TIMETABLE_SECTION_DAY_SPREAD = False
    return settings


def _pattern(family: str, days: tuple[int, ...], start: int) -> CanonicalPattern:
    return CanonicalPattern(
        pattern_id=f"{family}_{''.join(map(str, days))}_{start}",
        signature=f"{family}|{days}|{start}",
        meetings=[SectionMeeting(day=d, start_min=start, end_min=start + 75) for d in days],
        pattern_family=family,
        duration_permutation="75-75",
        is_lab_mixed=False,
        meeting_count=len(days),
        days_used=frozenset(days),
        slot_fingerprint="",
    )


def _section(code: str, family: str, days: tuple[int, ...], start: int) -> SectionState:
    pattern = _pattern(family, days, start)
    return SectionState(
        section_id=code,
        course_code=code,
        meetings=list(pattern.meetings),
        max_capacity=30,
        reserve_capacity=0,
        pattern_id=pattern.pattern_id,
        pattern_family=family,
    )


def _world(spec: dict[str, tuple[tuple[int, ...], int, list[tuple[tuple[int, ...], int]]]]):
    """``{code: (days, start, [(alt days, alt start), ...])}`` -> (sections, catalog); every
    section is its own pattern family, so only the alternatives listed are available to it."""
    sections: dict[str, SectionState] = {}
    catalog: dict[str, list[CanonicalPattern]] = {}
    for code, (days, start, alternatives) in spec.items():
        family = f"F{code}"
        sections[code] = _section(code, family, days, start)
        catalog[family] = [_pattern(family, days, start)] + [
            _pattern(family, d, s) for d, s in alternatives
        ]
    return sections, catalog


class Students:
    """Stand-in student evaluation: ``score(sections_by_id)`` -> the lexicographic score."""

    def __init__(self, monkeypatch, score, tamper=None) -> None:
        self.calls = 0
        self.score = score
        self.tamper = (
            tamper  # (sections, metrics) -> metrics, to report days the board does not hold
        )
        monkeypatch.setattr(repair, "evaluate_generated_timetable_candidate", self.evaluate)

    def evaluate(
        self,
        candidate_id,
        generated_sections,
        student_profiles,
        course_rigidity,
        section_instructor_ids=None,
        course_tiers=None,
    ) -> TimetableEvaluationResult:
        self.calls += 1
        sections = list(generated_sections)
        metrics = instructor_schedule_metrics(sections, section_instructor_ids)
        if self.tamper is not None:
            metrics = self.tamper({s.section_id: s for s in sections}, metrics)
        return TimetableEvaluationResult(
            candidate_id=candidate_id,
            lexicographic_score=tuple(self.score({s.section_id: s for s in sections})),
            assignment_states={},
            unresolved_student_ids=[],
            hotspot_courses=[],
            capacity_pressure_courses=[],
            reserve_heavy_sections=[],
            quality_score={"instructor_schedule": metrics},
        )


def _flat(_sections) -> tuple[int, ...]:
    return (0, 0, 0, 0, 1000, 0, 0, 0, 0)


def _run(sections, catalog, teachers, monkeypatch, score=_flat, tamper=None, **extra):
    """Run the pass inside a day-off policy anchored at the board as it stands; returns
    (evaluation, report, students)."""
    students = Students(monkeypatch, score, tamper)
    maps = {sid: frozenset(ids) for sid, ids in teachers.items()}
    metrics = instructor_schedule_metrics(sections.values(), maps)
    anchor = DayOffAnchor.from_metrics(metrics, padded=True, universe=[1, 2, 3])
    policy = DayOffPolicy(anchor, tuple(score(sections)), "generate_winner")
    with day_off_scope(policy):
        evaluation, report = repair.repair_five_day_teachers(
            sections_by_id=sections,
            pattern_catalog=catalog,
            student_profiles={"S": SimpleNamespace()},
            course_rigidity={},
            section_instructor_ids=maps,
            **extra,
        )
    return evaluation, report, students


def _days(sections, teachers, who) -> set[int]:
    return {
        m.day
        for sid, section in sections.items()
        if who in teachers.get(sid, ())
        for m in section.meetings
    }


# Teacher 1 works five days and only a PAIR gets him to four: A and C both sit on Sunday, and
# neither of them has a way off Sunday that leaves him on four days alone.
PAIR_TEACHERS = {"A": {1}, "B": {1}, "C": {1}}
PAIR_SPEC = {
    "A": ((SUN, TUE), S1, [((TUE, THU), S1)]),
    "B": ((MON, WED), S3, []),
    "C": ((THU, SUN), S2, [((THU, MON), S2)]),
}


def test_a_teacher_on_five_days_is_brought_to_four_by_a_pair_move(monkeypatch) -> None:
    sections, catalog = _world(PAIR_SPEC)
    original = sections["A"].pattern_id

    def score(
        board,
    ):  # the pair costs the students 20 gap minutes, the hard positions are as before
        return (0, 0, 0, 0, 1000 + (20 if board["A"].pattern_id != original else 0), 0, 0, 0, 0)

    evaluation, report, _ = _run(sections, catalog, PAIR_TEACHERS, monkeypatch, score)

    assert _days(sections, PAIR_TEACHERS, 1) == {MON, TUE, WED, THU}
    assert report["before"] == ["1"] and report["after"] == []
    (entry,) = report["teachers"]
    assert (entry["candidates"]["single"], entry["candidates"]["pair"]) == (0, 1)
    assert {m["section_id"] for m in entry["kept"]["moves"]} == {"A", "C"}
    assert entry["kept"]["days_after"] == 4
    assert entry["kept"]["score_after"][4] - entry["kept"]["score_before"][4] == 20
    assert evaluation is not None and evaluation.lexicographic_score[4] == 1020


def test_a_single_move_is_taken_when_one_is_enough(monkeypatch) -> None:
    spec = {
        "A": ((SUN, TUE), S1, [((TUE, WED), S2)]),
        "B": ((WED, THU), S1, []),
        "D": ((MON, TUE), S2, [((WED, THU), S2)]),  # D onto B's days frees Monday
    }
    teachers = {"A": {1}, "B": {1}, "D": {1}}
    sections, catalog = _world(spec)

    _, report, _ = _run(sections, catalog, teachers, monkeypatch)

    (entry,) = report["teachers"]
    # Two single moves reach four days (D alone, A alone) and a pair of them as well; the single
    # tier is evaluated first and, with one kept, the pair tier is never reached.
    assert entry["candidates"]["single"] == 2 and entry["candidates"]["pair"] >= 1
    assert len(entry["kept"]["moves"]) == 1
    assert entry["evaluated"] >= 1
    assert len(_days(sections, teachers, 1)) == 4


def test_the_smallest_change_wins_over_a_bigger_one_that_also_works(monkeypatch) -> None:
    # E alone moving to Tuesday frees Thursday; A and E together would free Sunday as well.
    # Both bring the teacher under five days, so the single move is the one kept.
    spec = {
        "A": ((SUN, MON), S1, [((MON, TUE), S1)]),
        "B": ((TUE, WED), S3, []),
        "E": ((THU,), S1, [((TUE,), S2)]),
    }
    teachers = {"A": {1}, "B": {1}, "E": {1}}
    sections, catalog = _world(spec)

    _, report, _ = _run(sections, catalog, teachers, monkeypatch)

    (entry,) = report["teachers"]
    assert entry["candidates"]["single"] >= 1 and entry["candidates"]["pair"] >= 1
    assert [m["section_id"] for m in entry["kept"]["moves"]] == ["E"]


def test_a_move_that_worsens_a_hard_student_position_is_never_kept(monkeypatch) -> None:
    sections, catalog = _world(PAIR_SPEC)
    before = {sid: list(s.meetings) for sid, s in sections.items()}
    original = sections["A"].pattern_id

    def score(board):  # moving A leaves a student without a section
        changed = board["A"].pattern_id != original
        return (1 if changed else 0, 0, 0, 0, 1000, 0, 0, 0, 0)

    _, report, students = _run(sections, catalog, PAIR_TEACHERS, monkeypatch, score)

    assert report["teachers"][0]["kept"] is None
    assert report["after"] == ["1"]
    assert {sid: list(s.meetings) for sid, s in sections.items()} == before
    assert report["evaluations"] == 1  # the pair was evaluated, and refused


def test_no_other_teachers_days_rise(monkeypatch) -> None:
    # Teacher 1's only lever is A (B and C are locked). A is shared with teacher 2, who also
    # teaches E on Sunday/Monday: A moving to Tuesday/Thursday would give teacher 2 two more days.
    spec = {
        "A": ((SUN, MON), S1, [((TUE, THU), S2)]),
        "B": ((TUE, WED), S1, []),
        "C": ((THU,), S1, []),
        "E": ((SUN, MON), S2, []),
    }
    teachers = {"A": {1, 2}, "B": {1}, "C": {1}, "E": {2}}
    sections, catalog = _world(spec)

    _, report, students = _run(
        sections, catalog, teachers, monkeypatch, locked_section_ids={"B", "C"}
    )

    entry = report["teachers"][0]
    assert entry["candidates"] == {"single": 0, "pair": 0}  # not even evaluated
    assert entry["kept"] is None
    assert _days(sections, teachers, 2) == {SUN, MON}
    assert students.calls == 1  # only the opening evaluation


def test_a_board_without_a_five_day_teacher_is_not_touched_or_evaluated(monkeypatch) -> None:
    spec = {"A": ((SUN, TUE), S1, []), "B": ((MON, WED), S1, [])}
    sections, catalog = _world(spec)

    evaluation, report, students = _run(sections, catalog, {"A": {1}, "B": {1}}, monkeypatch)

    assert evaluation is None
    assert report["before"] == [] and report["teachers"] == []
    assert students.calls == 0


def test_no_teachers_means_nothing_is_done(monkeypatch) -> None:
    sections, catalog = _world(PAIR_SPEC)
    students = Students(monkeypatch, _flat)

    evaluation, report = repair.repair_five_day_teachers(
        sections_by_id=sections,
        pattern_catalog=catalog,
        student_profiles={},
        course_rigidity={},
        section_instructor_ids=None,
    )

    assert evaluation is None and report["teachers"] == [] and students.calls == 0


def test_the_pass_is_bounded_by_a_count_of_evaluations(monkeypatch) -> None:
    # Three pair moves are valid; every one is refused, so the pass keeps looking until its
    # budget ends: a budget of 1 evaluation stops after one, a larger one tries all three.
    spec = {
        **PAIR_SPEC,
        "A": ((SUN, TUE), S1, [((TUE, THU), S1), ((TUE, THU), S3)]),
        "C": ((THU, SUN), S2, [((THU, MON), S2), ((THU, MON), S1)]),
    }

    def run(budget):
        sections, catalog = _world(spec)
        original = sections["A"].pattern_id

        def score(board):
            return (1 if board["A"].pattern_id != original else 0, 0, 0, 0, 1000, 0, 0, 0, 0)

        return _run(sections, catalog, PAIR_TEACHERS, monkeypatch, score, max_evaluations=budget)

    _, report, students = run(1)
    assert report["evaluations"] == 1
    assert students.calls == 2  # the opening evaluation and the one candidate

    _, report, _ = run(50)
    assert report["evaluations"] == 3


def test_a_locked_section_is_never_moved(monkeypatch) -> None:
    sections, catalog = _world(PAIR_SPEC)
    before = sections["A"].pattern_id

    _, report, _ = _run(sections, catalog, PAIR_TEACHERS, monkeypatch, locked_section_ids={"A"})

    assert report["teachers"][0]["kept"] is None
    assert sections["A"].pattern_id == before


def test_a_move_that_double_books_the_teacher_is_skipped(monkeypatch) -> None:
    # The same pair, but C's way off Sunday lands on Thursday at A's time: teacher 1 would be
    # in two places at once. The hard rule rejects it before any student evaluation.
    spec = {**PAIR_SPEC, "C": ((THU, SUN), S2, [((THU, MON), S1)])}
    sections, catalog = _world(spec)

    _, report, students = _run(sections, catalog, PAIR_TEACHERS, monkeypatch)

    entry = report["teachers"][0]
    assert entry["kept"] is None
    assert entry["skipped_hard_rule"] == 1
    assert students.calls == 1


def test_a_move_that_raises_another_teachers_evaluated_days_is_refused(monkeypatch) -> None:
    # The candidate gate looks at the board; the evaluation is the second opinion. Teacher 2 is
    # on three days. The pair passes the first gate, but is measured with a fourth day for teacher
    # 2 AND a better student score: the policy alone would allow that trade (a spare day for a
    # student gain), the owner's rule for this pass is that no other teacher's days rise.
    spec = {**PAIR_SPEC, "E": ((SUN, MON), S1, []), "F": ((TUE,), S1, [])}
    teachers = {**PAIR_TEACHERS, "E": {2}, "F": {2}}
    sections, catalog = _world(spec)
    original = sections["A"].pattern_id

    def score(board):
        better = board["A"].pattern_id != original
        return (0, 0, 0, 0, 990 if better else 1000, 0, 0, 0, 0)

    def tamper(board, metrics):
        if board["A"].pattern_id == original:
            return metrics
        days = {**metrics["working_days_by_instructor"], "2": 4}
        return {**metrics, "working_days_by_instructor": days, "working_days": sum(days.values())}

    _, report, _ = _run(sections, catalog, teachers, monkeypatch, score, tamper=tamper)

    assert report["teachers"][0]["evaluated"] == 1
    assert report["teachers"][0]["kept"] is None


def test_the_same_board_in_another_order_keeps_the_same_move(monkeypatch) -> None:
    spec = {
        **PAIR_SPEC,
        "A": ((SUN, TUE), S1, [((TUE, THU), S1), ((TUE, THU), S3)]),
        "C": ((THU, SUN), S2, [((THU, MON), S2), ((THU, MON), S1)]),
    }

    def kept(reverse: bool):
        sections, catalog = _world(spec)
        if reverse:
            sections = dict(reversed(list(sections.items())))
            catalog = {family: list(reversed(patterns)) for family, patterns in catalog.items()}
        _, report, _ = _run(sections, catalog, PAIR_TEACHERS, monkeypatch)
        return report["teachers"][0]["kept"]["moves"]

    assert sorted(kept(False), key=repr) == sorted(kept(True), key=repr)


def test_the_evaluations_are_shared_between_the_teachers_on_five_days(monkeypatch) -> None:
    # Two teachers, each with three valid pair moves that are all refused. With four evaluations
    # to spend each gets two: the first does not use up the budget the second needs.
    one = {
        "A": ((SUN, TUE), S1, [((TUE, THU), S1), ((TUE, THU), S3)]),
        "B": ((MON, WED), S3, []),
        "C": ((THU, SUN), S2, [((THU, MON), S2), ((THU, MON), S1)]),
    }
    spec = {**one, **{f"{code}2": value for code, value in one.items()}}
    teachers = {**{code: {1} for code in one}, **{f"{code}2": {2} for code in one}}
    sections, catalog = _world(spec)
    originals = {code: sections[code].pattern_id for code in ("A", "A2")}

    def score(board):  # every pair is refused: a student loses a section
        moved = any(board[code].pattern_id != original for code, original in originals.items())
        return (1 if moved else 0, 0, 0, 0, 1000, 0, 0, 0, 0)

    _, report, _ = _run(sections, catalog, teachers, monkeypatch, score, max_evaluations=4)

    assert [t["evaluated"] for t in report["teachers"]] == [2, 2]
    assert report["evaluations"] == 4


def test_the_report_says_why_the_pass_stopped(monkeypatch) -> None:
    from core.services.timetable_work_budget import capture_search_stops

    many = {  # three valid pairs, so a cap of one candidate really cuts the search short
        **PAIR_SPEC,
        "A": ((SUN, TUE), S1, [((TUE, THU), S1), ((TUE, THU), S3)]),
        "C": ((THU, SUN), S2, [((THU, MON), S2), ((THU, MON), S1)]),
    }

    def stops(**extra):
        sections, catalog = _world(many)
        with capture_search_stops() as recorded:
            _run(sections, catalog, PAIR_TEACHERS, monkeypatch, **extra)
        return [row["reason"] for row in recorded if row["stage"] == "five_day_repair"]

    assert stops() == ["completed"]
    assert stops(max_evaluations=1) == ["work_limit"]
    assert stops(max_considered=1) == ["work_limit"]
    assert stops(ceiling_seconds=-1.0) == [
        "wall_ceiling"
    ]  # the clock only ever ends a slow machine


def test_the_checkpoint_is_called_and_a_cancellation_leaves_the_board_alone(monkeypatch) -> None:
    sections, catalog = _world(PAIR_SPEC)
    before = {sid: s.pattern_id for sid, s in sections.items()}
    calls = []

    def cancel():
        calls.append(1)
        raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        _run(sections, catalog, PAIR_TEACHERS, monkeypatch, checkpoint=cancel)

    assert calls == [1]
    assert {sid: s.pattern_id for sid, s in sections.items()} == before


def test_the_report_is_plain_json(monkeypatch) -> None:
    import json

    sections, catalog = _world(PAIR_SPEC)

    _, report, _ = _run(sections, catalog, PAIR_TEACHERS, monkeypatch)

    assert json.loads(json.dumps(report)) == report

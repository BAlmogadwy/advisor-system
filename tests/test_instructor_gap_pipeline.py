"""WS-4: uniform tuple-length invariant across the live pipeline.

The #1 correctness risk is a single optimise run mixing tuple lengths: Python
compares a 6- and a 9-tuple without error (shared prefix, then longer wins),
silently corrupting accept/reject. These tests run the real
``optimise_current_timetable`` pipeline and assert every emitted stage score has
the SAME length.

Current contract — the **tiered objective is the default**, so a run emits
9-tuples with instructor idle absorbed at position 8 (hence 9 whether or not
``TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED`` is set). The legacy path is still
reachable by pinning ``TIMETABLE_TIERED_OBJECTIVE_ENABLED=False``, and keeps its
original contract: 6 elements with the gap flag off (byte parity), 7 with it on.

Coverage caveat: ``_stage_score_lengths`` can only observe the stage scores the
result dict actually carries, so these tests assert uniformity over the stages
that ran, not proof that every stage was threaded.
"""

from __future__ import annotations

from django.test import TransactionTestCase, override_settings

from core.services.timetable_optimizer_v2 import optimise_current_timetable

# Include baseline_score/best_score: baseline_score is the tuple the safety-gate
# rollback compares final_score against, so a length mismatch THERE is exactly the
# mixed-comparison bug this module exists to catch.
_SCORE_KEYS = (
    "baseline_score",
    "best_score",
    "final_score",
    "score_before_local_search",
    "score_before_chain",
)


def _stage_score_lengths(result: dict) -> list[int]:
    return [len(result[k]) for k in _SCORE_KEYS if isinstance(result.get(k), list)]


def _seed_placements(scenario, board) -> None:
    """Add two unlocked SectionPlacement rows (CS101|S1, CS102|S1) so
    ``optimise_current_timetable`` has a starting state to read. Mirrors the
    proven pr5 cpsat-test seeding."""
    from core.models import SectionPlacement, TermSection

    for course in ("CS101", "CS102"):
        ts, _ = TermSection.objects.get_or_create(
            scenario=scenario,
            course_key=course,
            section="S1",
            defaults={
                "course_code": course,
                "course_number": course,
                "course_name": course,
                "available_capacity": 30,
                "source_tag": "gap_seed",
            },
        )
        SectionPlacement.objects.create(
            board=board,
            term_section=ts,
            day="MON",
            start_time="08:00",
            end_time="09:15",
            room="R1",
            is_locked=False,
        )


class TestUniformTupleLength(TransactionTestCase):
    def _run(self) -> dict:
        from pr5_fixture_loader import load_pr5_fixture

        scenario, board, _ = load_pr5_fixture("pr5_cpsat_improve.json")
        _seed_placements(scenario, board)
        return optimise_current_timetable(
            scenario.id,
            run_local_search=True,
            run_chain_search=True,
            run_cpsat_polish=False,
        )

    # The tiered objective is now ON by default and independently sets the tuple
    # length (9), so these two pin it OFF to test the instructor-gap flag's own
    # legacy contract in isolation. The property under test is *uniformity*
    # across stages — a run must never mix tuple lengths, or a longer tuple
    # would win a prefix-tie comparison against a shorter one.
    @override_settings(
        TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED=False,
        TIMETABLE_TIERED_OBJECTIVE_ENABLED=False,
    )
    def test_flag_off_every_stage_is_six_tuple(self) -> None:
        lengths = _stage_score_lengths(self._run())
        assert lengths, "expected at least one stage score in the result"
        assert all(n == 6 for n in lengths), f"flag OFF must keep 6-tuples: {lengths}"

    @override_settings(
        TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED=True,
        TIMETABLE_TIERED_OBJECTIVE_ENABLED=False,
    )
    def test_flag_on_every_stage_is_seven_tuple(self) -> None:
        lengths = _stage_score_lengths(self._run())
        assert lengths, "expected at least one stage score in the result"
        assert all(n == 7 for n in lengths), f"flag ON must make all stages 7-tuples: {lengths}"

    # Same uniformity contract under the tiered objective (now the default). The
    # instructor-gap term is absorbed as the tiered tuple's last position, so the
    # length is 9 whichever way that flag is set — one test each, because _run()
    # loads a fixture scenario and cannot be called twice in a single test.
    @override_settings(
        TIMETABLE_TIERED_OBJECTIVE_ENABLED=True,
        TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED=False,
    )
    def test_tiered_every_stage_is_nine_tuple_gap_off(self) -> None:
        lengths = _stage_score_lengths(self._run())
        assert lengths, "expected at least one stage score in the result"
        assert all(n == 9 for n in lengths), f"tiered must make all stages 9-tuples: {lengths}"

    @override_settings(
        TIMETABLE_TIERED_OBJECTIVE_ENABLED=True,
        TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED=True,
    )
    def test_tiered_every_stage_is_nine_tuple_gap_on(self) -> None:
        lengths = _stage_score_lengths(self._run())
        assert lengths, "expected at least one stage score in the result"
        assert all(n == 9 for n in lengths), f"tiered must make all stages 9-tuples: {lengths}"


class TestInstructorGapTelemetry(TransactionTestCase):
    """End-to-end: a real instructor teaching two same-day sections with a gap →
    the result payload's instructor_gap_metric reflects it (and is zeroed when
    the flag is OFF)."""

    def _setup(self):
        from pr5_fixture_loader import load_pr5_fixture

        from core.models import CourseInstructor, Instructor, SectionPlacement, TermSection
        from core.services.timetable_pr4_instructor import normalise_instructor

        scenario, board, _ = load_pr5_fixture("pr5_cpsat_improve.json")
        scenario.gender = "M"
        scenario.programs = ["CS"]
        scenario.save(update_fields=["gender", "programs"])

        instr = Instructor.objects.create(
            full_name="Dr G", normalised_name=normalise_instructor("Dr G")
        )
        # CS101 SUN 09:00-10:15 then CS102 SUN 13:00-14:15 → 165-minute instructor gap.
        for course, (start, end) in (("CS101", ("09:00", "10:15")), ("CS102", ("13:00", "14:15"))):
            ts, _ = TermSection.objects.get_or_create(
                scenario=scenario,
                course_key=course,
                section="S1",
                defaults={
                    "course_code": course,
                    "course_number": course,
                    "course_name": course,
                    "available_capacity": 30,
                    "source_tag": "gap_seed",
                },
            )
            SectionPlacement.objects.create(
                board=board, term_section=ts, day="SUN", start_time=start, end_time=end, room="R1"
            )
            CourseInstructor.objects.create(
                program="CS", course_code=course, section="M", instructor=instr, role="primary"
            )
        return scenario

    @override_settings(TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED=True)
    def test_metric_reflects_instructor_gap_when_on(self) -> None:
        scenario = self._setup()
        result = optimise_current_timetable(
            scenario.id, run_local_search=False, run_chain_search=False, run_cpsat_polish=False
        )
        metric = result["instructor_gap_metric"]
        assert metric["affected_instructors"] == 1
        assert metric["idle_minutes_before"] == 165
        assert metric["idle_delta"] >= 0  # never a regression (students-first gate)

    @override_settings(TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED=False)
    def test_metric_zeroed_when_off(self) -> None:
        scenario = self._setup()
        result = optimise_current_timetable(
            scenario.id, run_local_search=False, run_chain_search=False, run_cpsat_polish=False
        )
        assert result["instructor_gap_metric"] == {
            "idle_minutes_before": 0,
            "idle_minutes_after": 0,
            "idle_delta": 0,
            "affected_instructors": 0,
        }

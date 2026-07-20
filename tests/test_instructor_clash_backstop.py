"""Persist-time instructor-clash backstop.

Covers the two properties the backstop exists for: the detail form can never
disagree with the count, and the check is SCENARIO-wide (the pre-existing
per-board detector is structurally blind to an instructor spanning two boards,
which is the shape of clash that actually shipped).
"""

from __future__ import annotations

import pytest

from core.services.timetable_assignment_models import SectionMeeting, SectionState
from core.services.timetable_constraints import (
    count_instructor_clashes,
    has_instructor_clash,
    list_instructor_clashes,
)


def _state(section_id: str, meetings: list[tuple[int, int, int]]) -> SectionState:
    return SectionState(
        section_id=section_id,
        course_code=section_id.rsplit("_", 1)[0],
        meetings=[SectionMeeting(day=d, start_min=s, end_min=e) for d, s, e in meetings],
        max_capacity=30,
        reserve_capacity=0,
    )


class TestListMatchesCount:
    """len(list) == count, always — they share ``_overlapping_pair_list``."""

    def test_clean_board_reports_nothing(self):
        sections = {
            "A_1": _state("A_1", [(0, 600, 675)]),
            "B_1": _state("B_1", [(0, 700, 775)]),
        }
        links = {"A_1": frozenset({7}), "B_1": frozenset({7})}
        assert count_instructor_clashes(sections, links) == 0
        assert list_instructor_clashes(sections, links) == []
        assert has_instructor_clash(sections, links) is False

    def test_overlap_is_reported_with_actionable_detail(self):
        sections = {
            "A_1": _state("A_1", [(0, 630, 745)]),  # 10:30-12:25
            "B_1": _state("B_1", [(0, 645, 805)]),  # 10:45-13:25 — overlaps
        }
        links = {"A_1": frozenset({7}), "B_1": frozenset({7})}
        rows = list_instructor_clashes(sections, links)
        assert count_instructor_clashes(sections, links) == len(rows) == 1
        row = rows[0]
        assert row["instructor_id"] == 7
        assert row["day"] == 0
        assert sorted(row["sections"]) == ["A_1", "B_1"]

    def test_touching_end_to_start_is_not_a_clash(self):
        """Half-open intervals: 10:00-11:00 then 11:00-12:00 is back-to-back."""
        sections = {
            "A_1": _state("A_1", [(0, 600, 660)]),
            "B_1": _state("B_1", [(0, 660, 720)]),
        }
        links = {"A_1": frozenset({7}), "B_1": frozenset({7})}
        assert list_instructor_clashes(sections, links) == []

    def test_a_sections_own_meetings_are_not_self_clashes(self):
        sections = {"A_1": _state("A_1", [(0, 600, 675), (0, 600, 675)])}
        assert list_instructor_clashes(sections, {"A_1": frozenset({7})}) == []

    def test_different_instructors_same_slot_is_fine(self):
        sections = {
            "A_1": _state("A_1", [(0, 600, 675)]),
            "B_1": _state("B_1", [(0, 600, 675)]),
        }
        links = {"A_1": frozenset({7}), "B_1": frozenset({8})}
        assert list_instructor_clashes(sections, links) == []

    def test_no_links_means_nothing_to_check(self):
        sections = {"A_1": _state("A_1", [(0, 600, 675)])}
        assert list_instructor_clashes(sections, {}) == []
        assert list_instructor_clashes(sections, None) == []

    def test_count_and_list_agree_on_multiple_clashes(self):
        sections = {
            "A_1": _state("A_1", [(0, 600, 700)]),
            "B_1": _state("B_1", [(0, 620, 700)]),
            "C_1": _state("C_1", [(0, 640, 700)]),
        }
        links = {sid: frozenset({7}) for sid in sections}
        # 3 mutually overlapping windows → 3 pairs.
        assert count_instructor_clashes(sections, links) == 3
        assert len(list_instructor_clashes(sections, links)) == 3


class TestBackstopIsScenarioWide:
    """The gap the backstop exists to close."""

    def test_cross_board_clash_is_caught(self):
        """Two sections an instructor teaches at once, on DIFFERENT boards.

        The per-board detector groups by instructor within one board, so this
        shape is invisible to it. The backstop's predicate is board-agnostic —
        it only sees section ids and times — so it catches it.
        """
        sections = {
            "AI101_1": _state("AI101_1", [(1, 540, 615)]),  # board: AI
            "DS201_1": _state("DS201_1", [(1, 540, 615)]),  # board: DS, same slot
        }
        links = {"AI101_1": frozenset({12}), "DS201_1": frozenset({12})}
        assert count_instructor_clashes(sections, links) == 1


@pytest.mark.django_db
class TestEndToEndAgainstTheDatabase:
    """Pins the happy path.

    ``verify_persisted_scenario`` swallows every exception so it can never break
    a persist — which means a bug inside the check (a bad import, a renamed
    helper) would leave the backstop permanently returning ``checked: False``
    and looking healthy. This test is what turns that into a suite failure. It
    already earned its keep: it caught the flag helper being imported from the
    wrong module.
    """

    @staticmethod
    def _scenario_with_shared_instructor(*, same_slot: bool):
        from core.models import (
            CourseInstructor,
            DeliveryBoard,
            Instructor,
            ScenarioSectionBudget,
            SectionPlacement,
            TermSection,
            TimetableScenario,
        )
        from core.services.timetable_pr4_instructor import normalise_instructor

        scenario = TimetableScenario.objects.create(
            academic_year="1448", term="1", name="AI+DS M", gender="M", programs=["AI", "DS"]
        )
        instr = Instructor.objects.create(
            full_name="Dr Shared", normalised_name=normalise_instructor("Dr Shared")
        )
        # Two boards — the cross-board shape the per-board detector cannot see.
        for program, code, start, end in (
            ("AI", "AI101", "09:00", "10:15"),
            ("DS", "DS201", "09:00" if same_slot else "11:00", "10:15" if same_slot else "12:15"),
        ):
            board = DeliveryBoard.objects.create(
                scenario=scenario, label=f"{program}-T1", nominal_term=1, program=program
            )
            ScenarioSectionBudget.objects.create(
                scenario=scenario,
                course_code=code,
                department=program,
                credit_hours=3,
                planned_sections=1,
                max_per_section=30,
                total_demand=20,
            )
            ts = TermSection.objects.create(
                scenario=scenario,
                course_code=code,
                course_number=code,
                course_key=code,
                course_name=code,
                section="S1",
                source_tag="test",
            )
            SectionPlacement.objects.create(
                board=board,
                term_section=ts,
                day="SUN",
                start_time=start,
                end_time=end,
                room="UNASSIGNED",
            )
            CourseInstructor.objects.create(
                program=program, course_code=code, section="M", instructor=instr, role="primary"
            )
        return scenario

    def test_report_runs_end_to_end_on_a_real_scenario(self, monkeypatch):
        from core.services import timetable_instructor_backstop as backstop

        monkeypatch.setattr(
            "core.services.timetable_pr4_instructor.is_instructor_links_enabled", lambda: True
        )
        scenario = self._scenario_with_shared_instructor(same_slot=True)
        report = backstop.scenario_instructor_clash_report(scenario.id)

        assert report["checked"] is True, "backstop silently checked nothing"
        assert report["count"] == 1
        assert report["clashes"][0]["instructor_id"] is not None

    def test_clean_scenario_is_reported_clean_and_checked(self, monkeypatch):
        from core.services import timetable_instructor_backstop as backstop

        monkeypatch.setattr(
            "core.services.timetable_pr4_instructor.is_instructor_links_enabled", lambda: True
        )
        scenario = self._scenario_with_shared_instructor(same_slot=False)
        report = backstop.scenario_instructor_clash_report(scenario.id)

        assert report["checked"] is True
        assert report["count"] == 0


@pytest.mark.django_db
class TestReportContract:
    def test_checked_is_false_when_links_are_disabled(self, settings, monkeypatch):
        """A scenario we could not check must never be reported as clean."""
        from core.services import timetable_instructor_backstop as backstop

        monkeypatch.setattr(
            "core.services.timetable_pr4_instructor.is_instructor_links_enabled", lambda: False
        )
        report = backstop.scenario_instructor_clash_report(999999)
        assert report["checked"] is False
        assert report["reason"] == "links_disabled"
        assert report["count"] == 0

    def test_verify_never_raises(self, monkeypatch):
        """A backstop that can break the persist it guards is worse than the hole."""
        from core.services import timetable_instructor_backstop as backstop

        def _boom(_sid):
            raise RuntimeError("db exploded")

        monkeypatch.setattr(backstop, "scenario_instructor_clash_report", _boom)
        report = backstop.verify_persisted_scenario(1, context="test")
        assert report["checked"] is False
        assert report["reason"] == "backstop_error"

    def test_introduced_is_the_delta_not_the_absolute(self, monkeypatch):
        """A pre-existing clash must not be blamed on this write."""
        from core.services import timetable_instructor_backstop as backstop

        monkeypatch.setattr(
            backstop,
            "scenario_instructor_clash_report",
            lambda _sid: {"count": 3, "clashes": [], "checked": True},
        )
        report = backstop.verify_persisted_scenario(1, context="test", before=3)
        assert report["introduced"] == 0

        report = backstop.verify_persisted_scenario(1, context="test", before=1)
        assert report["introduced"] == 2

    def test_a_repair_that_removes_clashes_reports_zero_introduced(self, monkeypatch):
        from core.services import timetable_instructor_backstop as backstop

        monkeypatch.setattr(
            backstop,
            "scenario_instructor_clash_report",
            lambda _sid: {"count": 0, "clashes": [], "checked": True},
        )
        report = backstop.verify_persisted_scenario(1, context="test", before=5)
        assert report["introduced"] == 0


class TestRollbackGateCoversScenarioWide:
    def test_new_cross_board_clash_blocks_the_run(self):
        from core.services.timetable_v2_runner import optimiser_safety_regression

        before = {"same_board_conflicts": {}, "instructor_clashes_scenario": 0}
        after = {"same_board_conflicts": {}, "instructor_clashes_scenario": 1}
        verdict = optimiser_safety_regression(before, after)
        assert verdict["blocked"] is True
        assert verdict["regressions"][0]["metric"] == "instructor_clashes_scenario"

    def test_clearing_clashes_does_not_block(self):
        from core.services.timetable_v2_runner import optimiser_safety_regression

        before = {"same_board_conflicts": {}, "instructor_clashes_scenario": 4}
        after = {"same_board_conflicts": {}, "instructor_clashes_scenario": 0}
        assert optimiser_safety_regression(before, after)["blocked"] is False

    def test_a_pre_existing_clash_does_not_paralyse_the_persist(self):
        """Delta, not absolute — otherwise no one could ever fix a broken board."""
        from core.services.timetable_v2_runner import optimiser_safety_regression

        before = {"same_board_conflicts": {}, "instructor_clashes_scenario": 2}
        after = {"same_board_conflicts": {}, "instructor_clashes_scenario": 2}
        assert optimiser_safety_regression(before, after)["blocked"] is False

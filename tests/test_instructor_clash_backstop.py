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


class TestSiblingSectionsAreNotPhantomClashes:
    """build_section_instructor_map_for_scenario attributes instructors at COURSE
    granularity (a set-union handed to every section of the course), so two
    instructors on two overlapping sections of one course report two clashes
    where the real assignment has none. That count now vetoes an optimise run,
    so a phantom would discard an entire good run. Sibling overlap is already
    forbidden by the same-course rule, so excluding these pairs costs no
    enforcement.
    """

    @staticmethod
    def _report_with(sections, links, monkeypatch):
        from core.services import timetable_instructor_backstop as backstop

        monkeypatch.setattr(
            "core.services.timetable_pr4_instructor.is_instructor_links_enabled", lambda: True
        )
        monkeypatch.setattr(
            "core.services.timetable_optimizer_v2.build_section_instructor_map_for_scenario",
            lambda _sid: links,
        )
        monkeypatch.setattr(
            "core.services.timetable_optimizer_v2.build_section_states_for_scenario",
            lambda _sid: list(sections.values()),
        )
        return backstop.scenario_instructor_clash_report(1)

    def test_two_overlapping_siblings_report_no_instructor_clash(self, monkeypatch):
        s1 = _state("STAT305_S1", [(0, 600, 675)])
        s2 = _state("STAT305_S2", [(0, 600, 675)])
        s1.course_code = s2.course_code = "STAT305"
        sections = {s1.section_id: s1, s2.section_id: s2}
        # Course-granular attribution: BOTH instructors on BOTH sections.
        links = {sid: frozenset({1, 2}) for sid in sections}

        report = self._report_with(sections, links, monkeypatch)
        assert report["checked"] is True
        assert report["count"] == 0, "phantom clash would roll back a whole optimise run"

    def test_a_genuine_cross_course_clash_still_reports(self, monkeypatch):
        a = _state("AI101_S1", [(0, 600, 675)])
        b = _state("DS201_S1", [(0, 600, 675)])
        a.course_code, b.course_code = "AI101", "DS201"
        sections = {a.section_id: a, b.section_id: b}
        links = {a.section_id: frozenset({1}), b.section_id: frozenset({1})}

        report = self._report_with(sections, links, monkeypatch)
        assert report["count"] == 1, "the clash the backstop exists to catch"


class TestClashesInvolving:
    """The manual-placement signal: 'is the section I just touched clashing',
    not the scenario-wide count (which would warn on every drag while any
    unrelated legacy clash existed) and not a delta (a second full scan)."""

    REPORT = {
        "checked": True,
        "count": 2,
        "clashes": [
            {"instructor_id": 1, "day": 0, "sections": ["AI101_S1", "DS201_S1"]},
            {"instructor_id": 2, "day": 1, "sections": ["CS300_S1", "IS400_S1"]},
        ],
    }

    def test_returns_only_rows_touching_the_section(self):
        from core.services.timetable_instructor_backstop import clashes_involving

        rows = clashes_involving(self.REPORT, "DS201_S1")
        assert len(rows) == 1
        assert rows[0]["instructor_id"] == 1

    def test_unrelated_legacy_clash_does_not_warn_on_this_placement(self):
        from core.services.timetable_instructor_backstop import clashes_involving

        assert clashes_involving(self.REPORT, "ENGL101_S1") == []

    def test_an_unchecked_report_yields_nothing(self):
        from core.services.timetable_instructor_backstop import clashes_involving

        assert clashes_involving({"checked": False, "clashes": []}, "AI101_S1") == []


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

    @pytest.mark.parametrize(
        "entrypoint",
        [
            "scenario_instructor_clash_report",
            "scenario_instructor_clash_count",
            "verify_persisted_scenario",
        ],
    )
    def test_no_entrypoint_can_break_the_persist_it_guards(self, entrypoint, monkeypatch):
        """EVERY entrypoint must swallow, not just verify_persisted_scenario.

        The count-only helper runs at write sites too; an unguarded twin ahead of
        the guarded one would 500 a drag-drop that worked fine before this module
        existed — turning a safety net into an outage. A reviewer caught exactly
        that asymmetry in the first cut of this change.
        """
        from core.services import timetable_instructor_backstop as backstop

        def _boom(_sid):
            raise RuntimeError("db exploded")

        monkeypatch.setattr(
            "core.services.timetable_pr4_instructor.is_instructor_links_enabled", lambda: True
        )
        monkeypatch.setattr(
            "core.services.timetable_optimizer_v2.build_section_instructor_map_for_scenario",
            _boom,
        )
        fn = getattr(backstop, entrypoint)
        result = fn(1, context="test") if entrypoint == "verify_persisted_scenario" else fn(1)
        if isinstance(result, dict):
            assert result["checked"] is False
            assert result["reason"] == "backstop_error"
        else:
            assert result == 0

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

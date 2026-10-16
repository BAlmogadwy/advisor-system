"""Tests for core.services.timetable_required_sections.

Covers the 2026-10-15 DS331 incident: a course budgeted for a section that
never got a single SectionPlacement/TermSectionMeeting row, and stayed
missing across every later Optimise because nothing ever retried it.
"""

from __future__ import annotations

import pytest

from core.models import (
    DeliveryBoard,
    ScenarioSectionBudget,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services import timetable_optimizer_v2 as optimiser
from core.services import timetable_required_sections as rs
from core.services.timetable_board_persistence import snapshot_scenario
from core.services.timetable_v2_runner import run_v2_optimisation_guarded


def _scenario_with_budget(
    name: str, *, planned_sections: int = 1, with_anchor: bool = True
) -> tuple:
    """A scenario budgeted for DS331, plus (by default) one OTHER course that
    is already fully placed — the real 2026-10-15 incident dropped one course
    out of ~65 correctly-placed ones, not an empty board. ``with_anchor=False``
    gives the genuinely-empty-scenario shape for the dedicated test of that
    edge case."""
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name=name)
    board = DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_code="DS331",
        course_key="DS331",
        course_name="DATA MINING",
        department="DS",
        credit_hours=4,
        planned_sections=planned_sections,
        max_per_section=25,
        total_demand=11,
        programme_term=6,
    )
    if with_anchor:
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_code="AI124",
            course_key="AI124",
            course_name="AI PROGRAMMING",
            department="AI",
            credit_hours=3,
            planned_sections=1,
            max_per_section=34,
            total_demand=9,
            programme_term=4,
        )
        anchor = TermSection.objects.create(
            scenario=scenario,
            course_code="AI124",
            course_number="AI124",
            course_key="AI124",
            course_name="AI124",
            section="S1",
            source_tag="tw_auto",
        )
        SectionPlacement.objects.create(
            board=board,
            term_section=anchor,
            day="SUN",
            start_time="09:00",
            end_time="10:15",
            room="R2",
        )
        TermSectionMeeting.objects.create(
            term_section=anchor,
            day="SUN",
            start_time="09:00",
            end_time="10:15",
            room="R2",
            instructor="Anchor instructor",
        )
    return scenario, board


def _place_section(scenario, board, label="S1"):
    section = TermSection.objects.create(
        scenario=scenario,
        course_code="DS331",
        course_number="DS331",
        course_key="DS331",
        course_name="DS331",
        section=label,
        source_tag="tw_auto",
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=section,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="R1",
    )
    TermSectionMeeting.objects.create(
        term_section=section,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="R1",
        instructor="Some instructor",
    )
    return section


@pytest.mark.django_db
def test_find_missing_required_sections_reports_fully_dropped_course():
    scenario, _board = _scenario_with_budget("Missing DS331")
    missing = rs.find_missing_required_sections(scenario.id)
    assert [row["course_code"] for row in missing] == ["DS331"]
    assert missing[0]["remaining_sections"] == 1
    assert missing[0]["used_sections"] == 0


@pytest.mark.django_db
def test_find_missing_required_sections_empty_when_fully_placed():
    scenario, board = _scenario_with_budget("Fully placed DS331")
    _place_section(scenario, board)
    assert rs.find_missing_required_sections(scenario.id) == []


@pytest.mark.django_db
def test_reinstate_is_a_noop_when_nothing_missing(monkeypatch):
    """Mutation guard: the repair must not call the placer at all when
    nothing is missing — a mutant that always calls it turns this red.

    Tracks the call directly (a plain counter) rather than raising from the
    mock: ``reinstate_missing_required_sections`` deliberately swallows any
    exception the placer raises (so a failed repair can't discard an
    otherwise-successful optimise run), which would silently hide an
    assertion raised from inside the mock itself.
    """
    scenario, board = _scenario_with_budget("Nothing missing")
    _place_section(scenario, board)

    calls = []
    monkeypatch.setattr(
        "core.services.timetable_autoplace.auto_place_scenario",
        lambda scenario_id: calls.append(scenario_id),
    )

    report = rs.reinstate_missing_required_sections(scenario.id)
    assert calls == []
    assert report == {
        "enabled": True,
        "attempted": 0,
        "before_missing": [],
        "after_missing": [],
        "newly_placed": [],
    }


@pytest.mark.django_db
def test_reinstate_reports_success_when_placer_fills_the_gap(monkeypatch):
    scenario, board = _scenario_with_budget("Placer succeeds")

    def fake_place(_scenario_id):
        _place_section(scenario, board)
        return {"placed": 1, "skipped": 0}

    monkeypatch.setattr("core.services.timetable_autoplace.auto_place_scenario", fake_place)

    report = rs.reinstate_missing_required_sections(scenario.id)
    assert report["attempted"] == 1
    assert report["newly_placed"] == ["DS331"]
    assert report["after_missing"] == []
    assert rs.find_missing_required_sections(scenario.id) == []


@pytest.mark.django_db
def test_reinstate_reports_failure_without_raising_when_placer_cannot_help(monkeypatch):
    scenario, _board = _scenario_with_budget("Placer cannot help")

    monkeypatch.setattr(
        "core.services.timetable_autoplace.auto_place_scenario",
        lambda _scenario_id: {"placed": 0, "skipped": 1},
    )

    report = rs.reinstate_missing_required_sections(scenario.id)
    assert report["attempted"] == 1
    assert report["newly_placed"] == []
    assert [row["course_code"] for row in report["after_missing"]] == ["DS331"]


@pytest.mark.django_db
def test_reinstate_survives_an_exception_from_the_placer(monkeypatch):
    """Mutation guard: removing the try/except turns this red (it would raise)."""
    scenario, _board = _scenario_with_budget("Placer raises")

    def exploding(_scenario_id):
        raise RuntimeError("boom")

    monkeypatch.setattr("core.services.timetable_autoplace.auto_place_scenario", exploding)

    report = rs.reinstate_missing_required_sections(scenario.id)
    assert report["newly_placed"] == []
    assert [row["course_code"] for row in report["after_missing"]] == ["DS331"]


@pytest.mark.django_db
def test_summarise_required_sections_status():
    scenario, board = _scenario_with_budget("Summarise")
    assert rs.summarise_required_sections(scenario.id)["status"] == "incomplete"
    _place_section(scenario, board)
    assert rs.summarise_required_sections(scenario.id)["status"] == "complete"


# ── Runner integration: pre-step, rollback gate, always-on reporting ──────


@pytest.mark.django_db
def test_current_mode_pre_repair_fixes_a_pre_existing_gap_before_the_baseline(
    monkeypatch, settings
):
    """A section missing BEFORE Optimise Current starts is patched up first,
    so the snapshot/rollback baseline and the optimiser both see a complete
    board — the exact scenario-23-to-24 shape of the live incident."""
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = False
    scenario, board = _scenario_with_budget("Current mode pre-repair")

    def fake_place(_scenario_id):
        _place_section(scenario, board)
        return {"placed": 1, "skipped": 0}

    monkeypatch.setattr("core.services.timetable_autoplace.auto_place_scenario", fake_place)

    def noop_optimise(**kwargs):
        return {"final_score": [0, 0, 0, 0, 0, 0], "baseline_score": [0, 0, 0, 0, 0, 0]}

    monkeypatch.setattr(optimiser, "optimise_current_timetable", noop_optimise)

    result = run_v2_optimisation_guarded(scenario.id, mode="current")

    assert result["safety_blocked"] is False
    assert result["required_sections"]["status"] == "complete"
    assert result["required_sections"]["pre_repair"]["newly_placed"] == ["DS331"]
    assert rs.find_missing_required_sections(scenario.id) == []


@pytest.mark.django_db
@pytest.mark.parametrize("mode", ["current", "full"])
def test_run_that_drops_a_required_section_is_rolled_back(monkeypatch, settings, mode):
    """The run itself (not a pre-existing gap) dropping a required section is
    a blocking regression: the whole board is restored, exactly like the
    existing student/instructor/safety regressions this gate already guards."""
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = False
    scenario, board = _scenario_with_budget("Run drops a section")
    section = _place_section(scenario, board)
    original = snapshot_scenario(scenario.id)

    def drop_the_section(**kwargs):
        SectionPlacement.objects.filter(term_section=section).delete()
        TermSectionMeeting.objects.filter(term_section=section).delete()
        return {"final_score": [0, 0, 0, 0, 0, 0], "baseline_score": [0, 0, 0, 0, 0, 0]}

    target = "optimise_scenario_timetable_v2" if mode == "full" else "optimise_current_timetable"
    monkeypatch.setattr(optimiser, target, drop_the_section)

    result = run_v2_optimisation_guarded(scenario.id, mode=mode)

    assert result["safety_blocked"] is True
    regression = next(
        r
        for r in result["safety_regression"]["regressions"]
        if r.get("metric") == "required_sections_missing"
    )
    assert regression["sections"] == ["DS331"]
    restored = snapshot_scenario(scenario.id)
    assert restored.placements == original.placements
    assert restored.meetings == original.meetings
    # Final reporting reflects the POST-ROLLBACK board, not the dropped one.
    assert result["required_sections"]["status"] == "complete"


@pytest.mark.django_db
def test_a_pre_existing_gap_that_cannot_be_repaired_does_not_block(monkeypatch, settings):
    """A section missing before the run, and still missing after, is reported
    — it must not block an otherwise-valid optimise (nothing regressed; the
    board simply started incomplete and stayed incomplete)."""
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = False
    scenario, _board = _scenario_with_budget("Unrepairable gap")

    monkeypatch.setattr(
        "core.services.timetable_autoplace.auto_place_scenario",
        lambda _scenario_id: {"placed": 0, "skipped": 1},
    )
    monkeypatch.setattr(
        optimiser,
        "optimise_current_timetable",
        lambda **_: {"final_score": [0, 0, 0, 0, 0, 0], "baseline_score": [0, 0, 0, 0, 0, 0]},
    )

    result = run_v2_optimisation_guarded(scenario.id, mode="current")

    assert result["safety_blocked"] is False
    assert result["required_sections"]["status"] == "incomplete"
    assert [row["course_code"] for row in result["required_sections"]["missing"]] == ["DS331"]


@pytest.mark.django_db
def test_current_mode_on_an_empty_board_is_not_turned_into_a_full_build(monkeypatch, settings):
    """A scenario with a budget but zero placements at all is "nothing built
    yet", not "something got dropped" — every budgeted course looks missing
    before the first build. The pre-repair must not fire here: doing so
    would silently perform a full build disguised as Optimise Current,
    overriding ``optimise_current_timetable``'s own intentional
    "No placements found — nothing to optimise" response."""
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = False
    scenario, _board = _scenario_with_budget("Empty board, current mode", with_anchor=False)

    # A counter, not a raise: reinstate_missing_required_sections deliberately
    # swallows any exception the placer raises, which would hide an
    # assertion raised from inside the mock itself (see the "nothing
    # missing" no-op test above for the same reasoning).
    calls = []
    monkeypatch.setattr(
        "core.services.timetable_autoplace.auto_place_scenario",
        lambda scenario_id: calls.append(scenario_id),
    )

    result = run_v2_optimisation_guarded(scenario.id, mode="current")

    assert calls == []
    # No students/placements were set up either, so the exact early-return
    # message depends on which of optimise_current_timetable's own guards
    # fires first — what matters here is that the placer never ran and no
    # section got built, not which graceful error message came back.
    assert "error" in result
    assert rs.find_missing_required_sections(scenario.id)[0]["course_code"] == "DS331"


@pytest.mark.django_db
def test_flag_off_disables_repair_and_the_gate_but_not_reporting(monkeypatch, settings):
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = False
    settings.TIMETABLE_REQUIRED_SECTION_GUARANTEE_ENABLED = False
    scenario, board = _scenario_with_budget("Flag off")
    section = _place_section(scenario, board)

    def placer_should_not_run(_scenario_id):
        raise AssertionError("placer must not run with the guarantee flag off")

    monkeypatch.setattr(
        "core.services.timetable_autoplace.auto_place_scenario", placer_should_not_run
    )

    def drop_the_section(**kwargs):
        SectionPlacement.objects.filter(term_section=section).delete()
        TermSectionMeeting.objects.filter(term_section=section).delete()
        return {"final_score": [0, 0, 0, 0, 0, 0], "baseline_score": [0, 0, 0, 0, 0, 0]}

    monkeypatch.setattr(optimiser, "optimise_current_timetable", drop_the_section)

    result = run_v2_optimisation_guarded(scenario.id, mode="current")

    # The gate did not fire (flag off), so the run is NOT rolled back...
    assert not any(
        r.get("metric") == "required_sections_missing"
        for r in result["safety_regression"]["regressions"]
    )
    # ...but detection/reporting still runs and shows the truth.
    assert result["required_sections"]["status"] == "incomplete"
    assert result["required_sections"]["guarantee_enabled"] is False
    assert result["required_sections"]["pre_repair"] is None

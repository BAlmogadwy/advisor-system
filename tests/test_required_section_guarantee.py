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
    # Totals of sections short, like every other row the result chip renders
    # as "before -> after" — a {course_key: short} map there read "NaN -> NaN".
    assert (regression["before"], regression["after"]) == (0, 1)
    assert regression["before_by_course"] == {}
    assert regression["after_by_course"] == {"DS331": 1}
    assert regression["course_labels"] == ["DS331 - DATA MINING"]
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


# ── Same display code, two courses (CS112 PROGRAMMING I / II) ─────────────

PI, PII = "CS112::PROGRAMMING_I", "CS112::PROGRAMMING_II"


def _two_cs112_courses(
    name: str, *, planned_i: int, placed_i: int, planned_ii: int, placed_ii: int
):
    """CS112 PROGRAMMING I and CS112 PROGRAMMING II: one display code, two
    budgets, two identities — the lab scenario 667 shape."""
    scenario = TimetableScenario.objects.create(academic_year="1448", term="2", name=name)
    board = DeliveryBoard.objects.create(scenario=scenario, label="Term 2", nominal_term=2)
    sections: dict[str, list[TermSection]] = {}
    for key, planned, placed in ((PI, planned_i, placed_i), (PII, planned_ii, placed_ii)):
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_code="CS112",
            course_key=key,
            course_name=key.split("::", 1)[1].replace("_", " "),
            department="CS",
            credit_hours=3,
            planned_sections=planned,
            max_per_section=30,
            total_demand=20 * planned,
            programme_term=2,
        )
        sections[key] = [_place(scenario, board, key, f"S{i}") for i in range(1, placed + 1)]
    return scenario, board, sections


def _place(scenario, board, key: str, label: str) -> TermSection:
    section = TermSection.objects.create(
        scenario=scenario,
        course_code="CS112",
        course_number="CS112",
        course_key=key,
        course_name=key,
        section=label,
        source_tag="tw_auto",
    )
    SectionPlacement.objects.create(
        board=board, term_section=section, day="SUN", start_time="09:00", end_time="10:15"
    )
    TermSectionMeeting.objects.create(
        term_section=section, day="SUN", start_time="09:00", end_time="10:15"
    )
    return section


def _drop(section: TermSection) -> None:
    SectionPlacement.objects.filter(term_section=section).delete()
    TermSectionMeeting.objects.filter(term_section=section).delete()


@pytest.mark.django_db
def test_reinstating_one_cs112_is_reported_while_the_other_stays_short(monkeypatch):
    """Keyed on the display code, "CS112" stayed "missing" because PROGRAMMING I
    was still short, so PROGRAMMING II's reinstatement never showed — and full
    mode then skipped the re-score of the board it changed."""
    scenario, board, _ = _two_cs112_courses(
        "PII reinstated", planned_i=2, placed_i=1, planned_ii=1, placed_ii=0
    )

    def fake_place(_scenario_id):
        _place(scenario, board, PII, "S1")

    monkeypatch.setattr("core.services.timetable_autoplace.auto_place_scenario", fake_place)

    report = rs.reinstate_missing_required_sections(scenario.id)

    assert report["newly_placed"] == [PII]
    assert [row["course_key"] for row in report["after_missing"]] == [PI]


@pytest.mark.django_db
def test_partial_reinstatement_counts_as_newly_placed(monkeypatch):
    """3 of 5 -> 4 of 5 changed the board as much as 0 of 1 -> 1 of 1."""
    scenario, board, _ = _two_cs112_courses(
        "PI partly reinstated", planned_i=3, placed_i=1, planned_ii=1, placed_ii=1
    )
    monkeypatch.setattr(
        "core.services.timetable_autoplace.auto_place_scenario",
        lambda _scenario_id: _place(scenario, board, PI, "S2"),
    )

    report = rs.reinstate_missing_required_sections(scenario.id)

    assert report["newly_placed"] == [PI]


@pytest.mark.django_db
def test_dropping_one_cs112_is_rolled_back_while_the_other_is_already_short(monkeypatch, settings):
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = False
    scenario, _board, sections = _two_cs112_courses(
        "PII dropped", planned_i=2, placed_i=1, planned_ii=1, placed_ii=1
    )
    original = snapshot_scenario(scenario.id)
    monkeypatch.setattr(
        "core.services.timetable_autoplace.auto_place_scenario", lambda _scenario_id: None
    )

    def drop_programming_ii(**kwargs):
        _drop(sections[PII][0])
        return {"final_score": [0, 0, 0, 0, 0, 0], "baseline_score": [0, 0, 0, 0, 0, 0]}

    monkeypatch.setattr(optimiser, "optimise_current_timetable", drop_programming_ii)

    result = run_v2_optimisation_guarded(scenario.id, mode="current")

    assert result["safety_blocked"] is True
    regression = next(
        r
        for r in result["safety_regression"]["regressions"]
        if r.get("metric") == "required_sections_missing"
    )
    assert regression["sections"] == [PII]
    # The totals count every short section; the label names the identity
    # dropped, which the bare code "CS112" could not tell apart.
    assert (regression["before"], regression["after"]) == (1, 2)
    assert regression["after_by_course"] == {PI: 1, PII: 1}
    assert regression["course_labels"] == ["CS112 - PROGRAMMING II"]
    assert snapshot_scenario(scenario.id).placements == original.placements


@pytest.mark.django_db
def test_an_already_short_course_losing_another_section_is_rolled_back(monkeypatch, settings):
    """Short by one before the run, short by two after it: the run dropped a
    section, even though the course was "missing" both times."""
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = False
    scenario, _board, sections = _two_cs112_courses(
        "PI shorter", planned_i=3, placed_i=2, planned_ii=1, placed_ii=1
    )
    monkeypatch.setattr(
        "core.services.timetable_autoplace.auto_place_scenario", lambda _scenario_id: None
    )

    def drop_one_more(**kwargs):
        _drop(sections[PI][1])
        return {"final_score": [0, 0, 0, 0, 0, 0], "baseline_score": [0, 0, 0, 0, 0, 0]}

    monkeypatch.setattr(optimiser, "optimise_current_timetable", drop_one_more)

    result = run_v2_optimisation_guarded(scenario.id, mode="current")

    assert result["safety_blocked"] is True
    regression = next(
        r
        for r in result["safety_regression"]["regressions"]
        if r.get("metric") == "required_sections_missing"
    )
    assert regression["sections"] == [PI]
    # Sections short, not courses short: one course both times, 1 -> 2 sections.
    assert (regression["before"], regression["after"]) == (1, 2)
    assert regression["before_by_course"] == {PI: 1}
    assert regression["after_by_course"] == {PI: 2}
    assert regression["course_labels"] == ["CS112 - PROGRAMMING I"]


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

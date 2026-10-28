"""A hand-made timetable CSV becomes a scenario's sections and placements.

The case that drove this: on a freshly scaffolded scenario nothing is placed, so
``infer_required_meeting_count`` falls back to one meeting for every course under
4 credits - and an ordinary 3-credit course meets twice. Every section of a real
timetable would have been refused. The importer computes the pattern from the
course's credit hours and passes it in, which is why MT1 is the first test here.
"""

from __future__ import annotations

import uuid
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from core.models import (
    DeliveryBoard,
    Instructor,
    PlannerJob,
    Room,
    ScenarioSectionBudget,
    SectionPlacement,
    TermSection,
    TimetableScenario,
)
from core.services.course_identity import planner_course_key
from core.services.manual_timetable_import import ImportFailed, apply_plan, build_plan
from core.services.timetable_workspace import create_planned_section_placements

HEADER = "board,course_code,section,day,start_time,end_time,room"


@pytest.fixture
def scenario() -> TimetableScenario:
    """A scaffold: two boards, two budgeted courses, nothing placed."""

    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="2", name="manual import"
    )
    DeliveryBoard.objects.create(scenario=scenario, label="Term 1", nominal_term=1)
    DeliveryBoard.objects.create(scenario=scenario, label="Term 3", nominal_term=3)
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key="CS211",
        course_code="CS211",
        course_name="Data Structures",
        credit_hours=3,
        planned_sections=2,
        max_per_section=35,
        total_demand=60,
    )
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key="AI301",
        course_code="AI301",
        course_name="Machine Learning",
        credit_hours=4,
        planned_sections=1,
        max_per_section=25,
        total_demand=20,
    )
    Room.objects.create(room_code="227FC001", capacity=42, section="F", department="AI,DS")
    return scenario


def _csv(tmp_path, body: str, header: str = HEADER) -> str:
    path = tmp_path / "timetable.csv"
    path.write_text(f"{header}\n{body}\n", encoding="utf-8")
    return str(path)


def _placements(scenario: TimetableScenario) -> list[tuple[str, str, str, str]]:
    return sorted(
        (p.term_section.course_code, p.term_section.section, p.day, p.start_time)
        for p in SectionPlacement.objects.filter(board__scenario=scenario).select_related(
            "term_section"
        )
    )


@pytest.mark.django_db
def test_mt1_a_three_credit_course_keeps_its_two_meetings(tmp_path, scenario):
    """The blocker: two meetings for a 3-credit course, on an empty scenario."""

    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,09:00,10:15,227FC001\nTerm 1,CS211,F1,TUE,09:00,10:15,227FC001",
    )
    plan = build_plan(path, scenario.id)
    assert not plan.problems, plan.problems
    assert [s.required_meetings for s in plan.sections] == [2]

    result = apply_plan(plan)
    assert result["sections_created"] == 1
    assert result["placements"] == 2
    assert _placements(scenario) == [
        ("CS211", "F1", "SUN", "09:00"),
        ("CS211", "F1", "TUE", "09:00"),
    ]
    section = TermSection.objects.get(scenario=scenario, course_key="CS211", section="F1")
    assert section.available_capacity == 35  # the budget's max_per_section
    assert section.course_name == "Data Structures"


@pytest.mark.django_db
def test_mt2_a_four_credit_course_needs_three_meetings(tmp_path, scenario):
    path = _csv(
        tmp_path,
        "Term 3,AI301,F1,SUN,09:00,10:15,\n"
        "Term 3,AI301,F1,MON,13:00,14:15,\n"
        "Term 3,AI301,F1,WED,09:00,10:40,",
    )
    plan = build_plan(path, scenario.id)
    assert not plan.problems, plan.problems
    assert plan.sections[0].required_meetings == 3
    assert apply_plan(plan)["placements"] == 3


@pytest.mark.django_db
def test_mt3_a_wrong_meeting_count_is_refused_and_nothing_is_written(tmp_path, scenario):
    path = _csv(tmp_path, "Term 1,CS211,F1,SUN,09:00,10:15,227FC001")
    plan = build_plan(path, scenario.id)
    assert [str(p) for p in plan.problems] == [
        "line 2: CS211 F1 has 1 meeting(s); a 3-credit course needs 2. "
        "Fix the sheet, or set credits for this course."
    ]
    assert not plan.sections
    with pytest.raises(ValueError, match="problems"):
        apply_plan(plan)
    assert not SectionPlacement.objects.filter(board__scenario=scenario).exists()


@pytest.mark.django_db
def test_mt4_one_bad_row_refuses_the_whole_sheet(tmp_path, scenario):
    """A half-imported timetable is the worst outcome, so a problem stops all of it."""

    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,09:00,10:15,\n"
        "Term 1,CS211,F1,TUE,09:00,10:15,\n"
        "Term 3,AI301,F1,FRI,09:00,10:15,",
    )
    plan = build_plan(path, scenario.id)
    assert len(plan.sections) == 1  # CS211 is fine on its own
    assert any("not one of SUN, MON, TUE, WED, THU" in str(p) for p in plan.problems)
    with pytest.raises(ValueError, match="problems"):
        apply_plan(plan)
    assert not SectionPlacement.objects.filter(board__scenario=scenario).exists()


@pytest.mark.django_db
def test_mt5_a_missing_column_is_named(tmp_path, scenario):
    path = _csv(
        tmp_path,
        "Term 1,CS211,SUN,09:00,10:15",
        header="board,course_code,day,start_time,end_time",
    )
    plan = build_plan(path, scenario.id)
    assert [str(p) for p in plan.problems] == ["The sheet needs these columns: section."]


@pytest.mark.django_db
def test_mt6_an_unknown_board_lists_the_real_ones(tmp_path, scenario):
    path = _csv(tmp_path, "Term 9,CS211,F1,SUN,09:00,10:15,")
    plan = build_plan(path, scenario.id)
    assert str(plan.problems[0]) == (
        "line 2: No board named 'Term 9' on this scenario. It has: Term 1, Term 3."
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ("9:00", "10:15", None),  # H:MM is read; the grid accepts it
        ("٠٩:٠٠", "10:15", "is not HH:MM"),
        ("25:00", "10:15", "is not HH:MM"),
        ("10:15", "09:00", "does not end after it starts"),
        ("09:00", "09:00", "does not end after it starts"),
    ],
)
def test_mt7_times_are_read_strictly(tmp_path, scenario, start, end, expected):
    path = _csv(
        tmp_path,
        f"Term 1,CS211,F1,SUN,{start},{end},\nTerm 1,CS211,F1,TUE,09:00,10:15,",
    )
    plan = build_plan(path, scenario.id)
    if expected is None:
        assert not plan.problems, plan.problems
    else:
        assert any(expected in str(p) for p in plan.problems), plan.problems


@pytest.mark.django_db
def test_mt8_a_section_meeting_twice_on_one_day_is_refused(tmp_path, scenario):
    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,09:00,10:15,\nTerm 1,CS211,F1,SUN,13:00,14:15,",
    )
    plan = build_plan(path, scenario.id)
    assert any("already meets on SUN" in str(p) for p in plan.problems)


@pytest.mark.django_db
def test_mt9_a_time_on_no_column_is_refused_until_allowed(tmp_path, scenario):
    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,08:45,10:00,\nTerm 1,CS211,F1,TUE,08:45,10:00,",
    )
    refused = build_plan(path, scenario.id)
    assert any("starts at no lecture column" in str(p) for p in refused.problems)

    allowed = build_plan(path, scenario.id, allow_offgrid=True)
    assert not allowed.problems, allowed.problems
    assert apply_plan(allowed)["placements"] == 2


@pytest.mark.django_db
def test_mt10_a_course_outside_the_plan_needs_its_credits(tmp_path, scenario):
    rows = "Term 1,MATH101,F1,SUN,09:00,10:15,\nTerm 1,MATH101,F1,TUE,09:00,10:15,"
    plan = build_plan(_csv(tmp_path, rows), scenario.id)
    assert any("is not in this scenario's plan" in str(p) for p in plan.problems)

    with_credits = build_plan(
        _csv(tmp_path, rows.replace(",\n", ",,3\n") + ",3", header=f"{HEADER},credits"),
        scenario.id,
    )
    assert not with_credits.problems, with_credits.problems
    assert with_credits.sections[0].capacity == 40  # no budget to size it


@pytest.mark.django_db
def test_mt11_one_label_cannot_sit_on_two_boards(tmp_path, scenario):
    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,09:00,10:15,\n"
        "Term 1,CS211,F1,TUE,09:00,10:15,\n"
        "Term 3,CS211,F1,MON,13:00,14:15,\n"
        "Term 3,CS211,F1,WED,13:00,14:15,",
    )
    plan = build_plan(path, scenario.id)
    assert any("appears on 2 boards" in str(p) for p in plan.problems)


@pytest.mark.django_db
def test_mt12_an_unknown_room_is_a_warning_not_a_refusal(tmp_path, scenario):
    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,09:00,10:15,NOWHERE9\nTerm 1,CS211,F1,TUE,09:00,10:15,227FC001",
    )
    plan = build_plan(path, scenario.id)
    assert not plan.problems, plan.problems
    rooms = [w for w in plan.warnings if w.startswith("Rooms not in the room list")]
    assert rooms == [
        "Rooms not in the room list (saved as typed, left out of the room reports): NOWHERE9 x1"
    ]
    assert apply_plan(plan)["placements"] == 2


@pytest.mark.django_db
def test_mt13_instructors_are_checked_and_not_written(tmp_path, scenario):
    Instructor.objects.create(full_name="Sara Ali", normalised_name="sara ali")
    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,09:00,10:15,,Sara Ali\nTerm 1,CS211,F1,TUE,09:00,10:15,,Nobody Here",
        header=f"{HEADER},instructor",
    )
    plan = build_plan(path, scenario.id)
    assert not plan.problems, plan.problems
    assert any("Nobody Here x1" in w for w in plan.warnings)
    assert not any("Sara Ali" in w for w in plan.warnings)
    assert any("NOT written" in w for w in plan.warnings)

    apply_plan(plan)
    section = TermSection.objects.get(scenario=scenario, course_key="CS211", section="F1")
    assert {m.instructor for m in section.meetings.all()} == {""}


@pytest.mark.django_db
def test_mt14_the_sheet_sizes_the_section_when_it_says_so(tmp_path, scenario):
    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,09:00,10:15,,28\nTerm 1,CS211,F1,TUE,09:00,10:15,,28",
        header=f"{HEADER},capacity",
    )
    plan = build_plan(path, scenario.id)
    assert plan.sections[0].capacity == 28
    apply_plan(plan)
    assert TermSection.objects.get(scenario=scenario, section="F1").available_capacity == 28


@pytest.mark.django_db
def test_mt15_a_running_job_stops_the_write(tmp_path, scenario):
    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,09:00,10:15,\nTerm 1,CS211,F1,TUE,09:00,10:15,",
    )
    plan = build_plan(path, scenario.id)
    PlannerJob.objects.create(
        id=uuid.uuid4(),
        scenario=scenario,
        mode=PlannerJob.MODE_OPTIMISE_V2_FULL,
        status=PlannerJob.STATUS_RUNNING,
    )
    with pytest.raises(ImportFailed, match="running"):
        apply_plan(plan)
    assert not SectionPlacement.objects.filter(board__scenario=scenario).exists()


@pytest.mark.django_db
def test_mt16_a_published_scenario_is_read_only(tmp_path, scenario):
    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,09:00,10:15,\nTerm 1,CS211,F1,TUE,09:00,10:15,",
    )
    good = build_plan(path, scenario.id)
    assert not good.problems, good.problems

    scenario.status = "published"
    scenario.save(update_fields=["status"])
    assert any("published" in str(p) for p in build_plan(path, scenario.id).problems)
    # And again at the write, under the lock: the scenario may be published
    # between the two. Its own words, not the create service's later refusal.
    with pytest.raises(ImportFailed, match="the scenario is published and is read-only"):
        apply_plan(good)
    assert not SectionPlacement.objects.filter(board__scenario=scenario).exists()


@pytest.mark.django_db
def test_mt17_a_scenario_without_boards_says_to_generate_first(tmp_path, scenario):
    DeliveryBoard.objects.filter(scenario=scenario).delete()
    plan = build_plan(_csv(tmp_path, "Term 1,CS211,F1,SUN,09:00,10:15,"), scenario.id)
    assert any("generate the scaffold first" in str(p) for p in plan.problems)


@pytest.mark.django_db
def test_mt18_a_label_already_on_the_board_is_caught_before_anything_is_written(tmp_path, scenario):
    """A re-run is refused at plan time, not discovered half way through."""

    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,09:00,10:15,\nTerm 1,CS211,F1,TUE,09:00,10:15,",
    )
    apply_plan(build_plan(path, scenario.id))
    # allow_existing gets past the "this scenario already has a timetable"
    # refusal, so the label check is the one under test here.
    again = build_plan(path, scenario.id, allow_existing=True)
    assert any("is already placed in this scenario" in str(p) for p in again.problems)
    assert not again.sections
    assert SectionPlacement.objects.filter(board__scenario=scenario).count() == 2


@pytest.fixture
def same_code(scenario) -> TimetableScenario:
    """CS112 as the real plans have it: one code, two courses, two budgets."""

    for name, term in (("PROGRAMMING I", 2), ("PROGRAMMING II", 4)):
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_key=planner_course_key("CS112", name),
            course_code="CS112",
            course_name=name,
            credit_hours=4,
            planned_sections=1,
            max_per_section=27,
            total_demand=30,
            programme_term=term,
        )
    return scenario


@pytest.mark.django_db
def test_mt22_a_section_is_written_under_the_scenarios_own_course_key(tmp_path, scenario):
    """The bare code would create a section no student request can join."""

    ScenarioSectionBudget.objects.filter(scenario=scenario, course_key="CS211").update(
        course_key="CS211::DATA_STRUCTURES"
    )
    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,09:00,10:15,\nTerm 1,CS211,F1,TUE,09:00,10:15,",
    )
    plan = build_plan(path, scenario.id)
    assert not plan.problems, plan.problems
    assert plan.sections[0].course_key == "CS211::DATA_STRUCTURES"
    apply_plan(plan)
    assert TermSection.objects.get(scenario=scenario, section="F1").course_key == (
        "CS211::DATA_STRUCTURES"
    )


@pytest.mark.django_db
def test_mt23_a_code_that_is_two_courses_is_refused_with_both_names(tmp_path, same_code):
    path = _csv(
        tmp_path,
        "Term 1,CS112,F1,SUN,09:00,10:15,\n"
        "Term 1,CS112,F1,TUE,09:00,10:15,\n"
        "Term 1,CS112,F1,WED,09:00,10:40,",
    )
    plan = build_plan(path, same_code.id)
    assert [str(p) for p in plan.problems] == [
        "line 2: CS112 is 2 different courses in this scenario "
        "(PROGRAMMING I, PROGRAMMING II). Put the course_name of the one you mean in the sheet."
    ]
    assert not plan.sections


@pytest.mark.django_db
def test_mt24_the_course_name_picks_which_of_the_two_it_is(tmp_path, same_code):
    path = _csv(
        tmp_path,
        "Term 1,CS112,F1,SUN,09:00,10:15,,PROGRAMMING I\n"
        "Term 1,CS112,F1,TUE,09:00,10:15,,PROGRAMMING I\n"
        "Term 1,CS112,F1,WED,09:00,10:40,,PROGRAMMING I",
        header=f"{HEADER},course_name",
    )
    plan = build_plan(path, same_code.id)
    assert not plan.problems, plan.problems
    assert plan.sections[0].course_key == "CS112::PROGRAMMING_I"
    assert plan.sections[0].required_meetings == 3  # 4 credits: 75 + 75 + 100
    apply_plan(plan)
    assert TermSection.objects.get(scenario=same_code, section="F1").course_key == (
        "CS112::PROGRAMMING_I"
    )


@pytest.mark.django_db
def test_mt25_times_are_stored_zero_padded(tmp_path, scenario):
    """Excel writes 9:00; the exports compare stored times as text."""

    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,9:00,10:15,\nTerm 1,CS211,F1,TUE,9:00,10:15,",
    )
    plan = build_plan(path, scenario.id)
    assert not plan.problems, plan.problems
    assert [m["start_time"] for m in plan.sections[0].meetings] == ["09:00", "09:00"]
    apply_plan(plan)
    assert {p.start_time for p in SectionPlacement.objects.filter(board__scenario=scenario)} == {
        "09:00"
    }


@pytest.mark.django_db
def test_mt26_a_sheet_excel_saved_in_the_system_codepage_still_reads(tmp_path, scenario):
    path = tmp_path / "arabic.csv"
    path.write_text(
        f"{HEADER},course_name\n"
        "Term 1,CS211,F1,SUN,09:00,10:15,,برمجة\n"
        "Term 1,CS211,F1,TUE,09:00,10:15,,برمجة\n",
        encoding="cp1256",
    )
    plan = build_plan(str(path), scenario.id)
    assert not plan.problems, plan.problems
    assert plan.sections[0].course_name == "برمجة"


@pytest.mark.django_db
def test_mt27_trailing_empty_rows_and_odd_header_case_are_tolerated(tmp_path, scenario):
    path = tmp_path / "messy.csv"
    path.write_text(
        "Board,Course_Code,Section,Day,Start_Time,End_Time,Room\n"
        "Term 1,CS211,F1,SUN,09:00,10:15,\n"
        "Term 1,CS211,F1,TUE,09:00,10:15,\n"
        ",,,,,,\n,,,,,,\n",
        encoding="utf-8",
    )
    plan = build_plan(str(path), scenario.id)
    assert not plan.problems, plan.problems
    assert len(plan.sections[0].meetings) == 2


@pytest.mark.django_db
def test_mt28_a_three_credit_course_may_meet_in_longer_blocks(tmp_path, scenario):
    """STAT103 is 3 credits taught as two 100-minute blocks: the count is what is checked."""

    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key="STAT103",
        course_code="STAT103",
        course_name="Statistics",
        credit_hours=3,
        planned_sections=1,
        max_per_section=50,
        total_demand=60,
    )
    path = _csv(
        tmp_path,
        "Term 1,STAT103,F1,MON,13:00,14:40,\nTerm 1,STAT103,F1,WED,13:00,14:40,",
    )
    plan = build_plan(path, scenario.id)
    assert not plan.problems, plan.problems
    assert plan.sections[0].required_meetings == 2
    assert apply_plan(plan)["placements"] == 2


@pytest.mark.django_db
def test_mt29_a_refusal_half_way_rolls_the_whole_import_back(tmp_path, scenario, monkeypatch):
    from core.services import timetable_workspace

    original = timetable_workspace.create_planned_section_placements
    calls = {"n": 0}

    def explode(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return original(*args, **kwargs)

    monkeypatch.setattr(timetable_workspace, "create_planned_section_placements", explode)
    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,09:00,10:15,\n"
        "Term 1,CS211,F1,TUE,09:00,10:15,\n"
        "Term 3,AI301,F1,SUN,13:00,14:15,\n"
        "Term 3,AI301,F1,MON,14:30,15:45,\n"
        "Term 3,AI301,F1,WED,09:00,10:40,",
    )
    plan = build_plan(path, scenario.id)
    assert len(plan.sections) == 2
    with pytest.raises(ImportFailed, match="boom"):
        apply_plan(plan)
    assert calls["n"] == 2, "it stopped on the second section"
    assert not SectionPlacement.objects.filter(board__scenario=scenario).exists()
    assert not TermSection.objects.filter(scenario=scenario).exists()


@pytest.mark.django_db
def test_mt31_every_section_of_the_sheet_is_marked_mainly_for_one_programme(tmp_path, scenario):
    scenario.programs = ["AI2", "DS2"]
    scenario.save(update_fields=["programs"])
    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,09:00,10:15,\nTerm 1,CS211,F1,TUE,09:00,10:15,",
    )
    plan = build_plan(path, scenario.id, prefer_programme="ai2")
    assert not plan.problems, plan.problems
    assert plan.sections[0].preferred_programme == "AI2"
    apply_plan(plan)
    assert TermSection.objects.get(scenario=scenario, section="F1").preferred_programme == "AI2"


@pytest.mark.django_db
def test_mt32_a_column_overrides_the_sheets_programme_for_one_section(tmp_path, scenario):
    scenario.programs = ["AI2", "DS2"]
    scenario.save(update_fields=["programs"])
    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,09:00,10:15,,DS2\n"
        "Term 1,CS211,F1,TUE,09:00,10:15,,DS2\n"
        "Term 3,AI301,F1,SUN,13:00,14:15,,\n"
        "Term 3,AI301,F1,MON,14:30,15:45,,\n"
        "Term 3,AI301,F1,WED,09:00,10:40,,",
        header=f"{HEADER},preferred_programme",
    )
    plan = build_plan(path, scenario.id, prefer_programme="AI2")
    assert not plan.problems, plan.problems
    assert {s.course_code: s.preferred_programme for s in plan.sections} == {
        "CS211": "DS2",
        "AI301": "AI2",
    }


@pytest.mark.django_db
def test_mt33_a_programme_the_scenario_does_not_have_is_refused(tmp_path, scenario):
    scenario.programs = ["AI2"]
    scenario.save(update_fields=["programs"])
    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,09:00,10:15,\nTerm 1,CS211,F1,TUE,09:00,10:15,",
    )
    # Refused for the file, before any row is read: one problem, not one per section.
    whole = build_plan(path, scenario.id, prefer_programme="CS9")
    assert [str(p) for p in whole.problems] == [
        "CS9 is not a programme of this scenario. It has: AI2"
    ]
    assert not whole.sections

    one = build_plan(
        _csv(
            tmp_path,
            "Term 1,CS211,F1,SUN,09:00,10:15,,CS9\nTerm 1,CS211,F1,TUE,09:00,10:15,,CS9",
            header=f"{HEADER},preferred_programme",
        ),
        scenario.id,
    )
    assert any("CS211 F1: CS9 is not a programme" in str(p) for p in one.problems)
    assert not one.sections


@pytest.mark.django_db
def test_mt35_a_locked_section_keeps_its_times_through_a_rebuild(tmp_path, scenario):
    """STAT103 runs two 100-minute blocks the engine would not choose: pin them."""

    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key="STAT103",
        course_code="STAT103",
        course_name="Statistics",
        credit_hours=3,
        planned_sections=1,
        max_per_section=50,
        total_demand=60,
    )
    path = _csv(
        tmp_path,
        "Term 1,STAT103,F1,MON,13:00,14:40,,yes\n"
        "Term 1,STAT103,F1,WED,13:00,14:40,,yes\n"
        "Term 1,CS211,F2,SUN,09:00,10:15,,\n"
        "Term 1,CS211,F2,TUE,09:00,10:15,,",
        header=f"{HEADER},locked",
    )
    plan = build_plan(path, scenario.id)
    assert not plan.problems, plan.problems
    assert {s.course_code: s.locked for s in plan.sections} == {"STAT103": True, "CS211": False}

    apply_plan(plan)
    locked = {
        (p.term_section.course_code, p.is_locked)
        for p in SectionPlacement.objects.filter(board__scenario=scenario).select_related(
            "term_section"
        )
    }
    assert locked == {("STAT103", True), ("CS211", False)}


@pytest.mark.django_db
def test_mt36_a_locked_value_that_is_not_yes_or_no_is_refused(tmp_path, scenario):
    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,09:00,10:15,,maybe\nTerm 1,CS211,F1,TUE,09:00,10:15,,maybe",
        header=f"{HEADER},locked",
    )
    plan = build_plan(path, scenario.id)
    assert [str(p) for p in plan.problems] == ["line 2: locked 'maybe' is not yes or no."]
    assert not plan.sections


@pytest.mark.django_db
def test_mt34_an_unmarked_sheet_leaves_the_mark_empty(tmp_path, scenario):
    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,09:00,10:15,\nTerm 1,CS211,F1,TUE,09:00,10:15,",
    )
    plan = build_plan(path, scenario.id)
    assert plan.sections[0].preferred_programme == ""
    apply_plan(plan)
    assert TermSection.objects.get(scenario=scenario, section="F1").preferred_programme == ""


@pytest.mark.django_db
def test_mt37_rows_of_one_section_must_agree_on_what_describes_the_section(tmp_path, scenario):
    """A lock on the second row alone was read as no lock, and the rebuild ate it."""

    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,09:00,10:15,,\nTerm 1,CS211,F1,TUE,09:00,10:15,,yes",
        header=f"{HEADER},locked",
    )
    plan = build_plan(path, scenario.id)
    assert [str(p) for p in plan.problems] == [
        "line 2: CS211 F1: every row of one section must agree on locked; "
        "these rows do not (lines 2, 3)."
    ]
    assert not plan.sections


@pytest.mark.django_db
def test_mt38_a_room_is_stored_as_the_room_list_spells_it(tmp_path, scenario):
    """The exports match a room's code as text, so the sheet's casing cannot win."""

    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,09:00,10:15,227fc001\nTerm 1,CS211,F1,TUE,09:00,10:15,227FC001",
    )
    plan = build_plan(path, scenario.id)
    assert not plan.problems, plan.problems
    assert [m["room"] for m in plan.sections[0].meetings] == ["227FC001", "227FC001"]
    apply_plan(plan)
    assert {p.room for p in SectionPlacement.objects.filter(board__scenario=scenario)} == {
        "227FC001"
    }


@pytest.mark.django_db
def test_mt39_a_scenario_that_already_has_a_timetable_is_refused(tmp_path, scenario):
    """Generate builds one by default; importing beside it doubles every course."""

    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,09:00,10:15,\nTerm 1,CS211,F1,TUE,09:00,10:15,",
    )
    apply_plan(build_plan(path, scenario.id))

    second = _csv(
        tmp_path,
        "Term 3,AI301,F1,SUN,13:00,14:15,\n"
        "Term 3,AI301,F1,MON,14:30,15:45,\n"
        "Term 3,AI301,F1,WED,09:00,10:40,",
    )
    refused = build_plan(second, scenario.id)
    assert any("already holds 2 placement(s)" in str(p) for p in refused.problems)
    assert not refused.sections

    allowed = build_plan(second, scenario.id, allow_existing=True)
    assert not allowed.problems, allowed.problems
    assert apply_plan(allowed)["placements"] == 3


@pytest.mark.django_db
def test_mt40_a_course_key_naming_another_course_is_refused(tmp_path, scenario):
    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,09:00,10:15,,CS112::PROGRAMMING_I\n"
        "Term 1,CS211,F1,TUE,09:00,10:15,,CS112::PROGRAMMING_I",
        header=f"{HEADER},course_key",
    )
    plan = build_plan(path, scenario.id)
    assert any("is not a key of CS211" in str(p) for p in plan.problems)
    assert not plan.sections


@pytest.mark.django_db
def test_mt41_a_problem_names_the_sheets_own_line_number(tmp_path, scenario):
    """Blank rows are skipped, but the line number still counts them."""

    path = tmp_path / "gappy.csv"
    path.write_text(f"{HEADER}\n,,,,,,\nTerm 1,CS211,F1,FRI,09:00,10:15,\n", encoding="utf-8")
    plan = build_plan(str(path), scenario.id)
    assert [str(p) for p in plan.problems] == [
        "line 3: day 'FRI' is not one of SUN, MON, TUE, WED, THU."
    ]


@pytest.mark.django_db
def test_mt30_english_is_refused_at_plan_time_not_half_way_through_the_write(tmp_path, scenario):
    """ENG101/ENG102 run four fixed 180-minute blocks; the count alone would pass."""

    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key="ENG102",
        course_code="ENG102",
        course_name="English II",
        credit_hours=4,
        planned_sections=1,
        max_per_section=25,
        total_demand=30,
    )
    wrong = _csv(
        tmp_path,
        "Term 1,ENG102,F1,SUN,08:00,11:30,\n"
        "Term 1,ENG102,F1,MON,08:00,11:30,\n"
        "Term 1,ENG102,F1,WED,08:00,11:30,\n"
        "Term 1,ENG102,F1,THU,08:00,11:30,",
    )
    plan = build_plan(wrong, scenario.id)
    assert any("ENG102 F1" in str(p) and "180-minute" in str(p) for p in plan.problems), (
        plan.problems
    )
    assert not plan.sections

    right = _csv(
        tmp_path,
        "Term 1,ENG102,F1,SUN,09:00,12:00,\n"
        "Term 1,ENG102,F1,MON,09:00,12:00,\n"
        "Term 1,ENG102,F1,WED,09:00,12:00,\n"
        "Term 1,ENG102,F1,THU,09:00,12:00,",
    )
    # The default lab columns happen to start at 09:00 and 13:00, so the English
    # pattern would land on one by luck. Take them away: a policy course draws in
    # its own table and must be accepted with no column of its own.
    scenario.lab_slot_config = [{"label": "Lab", "start": "10:25", "end": "12:05"}]
    scenario.save(update_fields=["lab_slot_config"])
    good = build_plan(right, scenario.id)
    assert not good.problems, good.problems
    assert apply_plan(good)["placements"] == 4


@pytest.mark.django_db
def test_mt19_the_required_meetings_override_moves_the_check_it_does_not_remove_it(scenario):
    """The caller states the pattern; a section that does not match is still refused."""

    board = DeliveryBoard.objects.get(scenario=scenario, label="Term 1")
    two = [
        {"day": "SUN", "start_time": "09:00", "end_time": "10:15", "room": ""},
        {"day": "TUE", "start_time": "09:00", "end_time": "10:15", "room": ""},
    ]
    common = {
        "course_code": "CS211",
        "course_key": "CS211",
        "course_name": "Data Structures",
        "capacity": 30,
    }
    # Left out, the page's own inference reads 1 for a 3-credit course, so the
    # pair is refused - the behaviour this override exists to work around.
    with pytest.raises(ValueError, match="only needs one planned meeting target"):
        create_planned_section_placements(board.id, section_label="F9", meetings=two, **common)
    with pytest.raises(ValueError, match="needs a complete 3-meeting section pattern"):
        create_planned_section_placements(
            board.id, section_label="F9", meetings=two, required_meetings=3, **common
        )
    with pytest.raises(ValueError, match="at least 1"):
        create_planned_section_placements(
            board.id, section_label="F9", meetings=two, required_meetings=0, **common
        )
    result = create_planned_section_placements(
        board.id, section_label="F9", meetings=two, required_meetings=2, **common
    )
    assert len(result["placements"]) == 2
    assert result["required_meetings"] == 2


@pytest.mark.django_db
def test_mt20_the_command_writes_only_with_apply(tmp_path, scenario):
    path = _csv(
        tmp_path,
        "Term 1,CS211,F1,SUN,09:00,10:15,\nTerm 1,CS211,F1,TUE,09:00,10:15,",
    )
    out = StringIO()
    call_command("import_manual_timetable", path, "--scenario-id", str(scenario.id), stdout=out)
    assert "Dry run: nothing written" in out.getvalue()
    assert not SectionPlacement.objects.filter(board__scenario=scenario).exists()

    out = StringIO()
    call_command(
        "import_manual_timetable", path, "--scenario-id", str(scenario.id), "--apply", stdout=out
    )
    assert "Written: 1 section(s), 2 placement(s)." in out.getvalue()
    assert SectionPlacement.objects.filter(board__scenario=scenario).count() == 2


@pytest.mark.django_db
def test_mt21_the_command_names_the_problems_and_writes_nothing(tmp_path, scenario):
    path = _csv(tmp_path, "Term 1,CS211,F1,SUN,09:00,10:15,")
    out = StringIO()
    with pytest.raises(CommandError, match="Fix the sheet"):
        call_command(
            "import_manual_timetable",
            path,
            "--scenario-id",
            str(scenario.id),
            "--apply",
            stdout=out,
        )
    assert "a 3-credit course needs 2" in out.getvalue()
    assert not SectionPlacement.objects.filter(board__scenario=scenario).exists()

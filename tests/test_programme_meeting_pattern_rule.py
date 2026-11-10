"""A programme's vote on a course's weekly meetings, and the scenario's decision.

Owner, 2026-11-10: the department default for "2 x 100 min" is set PER PROGRAMME
(both campuses alike). ``ProgrammeRequirement.meeting_pattern`` holds the
programme's vote: "" not set (it abstains), "standard" (chosen on purpose) or
"2x100". A scenario pools a course over its programmes; the programmes that
voted must agree, an unset programme does not count, and a disagreement keeps
the credit default. These tests pin the pure rule both engines run
(``timetable_meeting_shape``), its database guard (migration 0078), and the read
side the pages use (``programme_meeting_patterns``).
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import IntegrityError, transaction

from core.models import DeliveryBoard, ProgrammeRequirement, TimetableScenario
from core.services import programme_meeting_patterns as pmp
from core.services.timetable_course_exclusion import is_generate_excluded_course
from core.services.timetable_meeting_shape import (
    AGREE,
    AMBIGUOUS,
    MIXED,
    NO_VOTE,
    PROGRAMME_STANDARD,
    TWO_BY_100,
    ProgrammeRow,
    ProgrammeShape,
    budget_form,
    department_budget_value,
    merge_programme_patterns,
    programme_shape_for,
    validate_programme_meeting_pattern,
)

STAT = "STAT103::PRINCIPLES_OF_PROBABILITY_AND_STATISTICS"
LA = "MATH243::243_LINEAR_ALGEBRA_I"
LA_CYP2 = "MATH243::LINEAR_ALGEBRA"


def row(programme, identity=STAT, vote="", credits=3, named=True):
    return ProgrammeRow(programme, identity, vote, credits, named)


# ── the merge ────────────────────────────────────────────────────────────────


def test_an_unset_programme_abstains_so_the_programmes_that_chose_decide():
    decision = merge_programme_patterns([row("AI2", vote=TWO_BY_100), row("DS2")])
    assert decision == ProgrammeShape(AGREE, TWO_BY_100, (("AI2", TWO_BY_100), ("DS2", "")))


def test_an_explicit_standard_vetoes_two_by_100_and_nothing_is_chosen():
    decision = merge_programme_patterns(
        [row("AI2", vote=TWO_BY_100), row("CS2", vote=PROGRAMME_STANDARD), row("DS2")]
    )
    assert decision.status == MIXED
    assert decision.meeting_pattern == ""
    assert decision.programmes == (("AI2", TWO_BY_100), ("CS2", PROGRAMME_STANDARD), ("DS2", ""))


def test_nobody_voting_decides_nothing_and_still_lists_the_programmes():
    decision = merge_programme_patterns([row("DS2"), row("AI2")])
    assert decision == ProgrammeShape(NO_VOTE, "", (("AI2", ""), ("DS2", "")))


def test_programmes_agreeing_on_standard_agree_on_standard():
    decision = merge_programme_patterns(
        [row("AI2", vote=PROGRAMME_STANDARD), row("DS2", vote=PROGRAMME_STANDARD)]
    )
    assert (decision.status, decision.meeting_pattern) == (AGREE, PROGRAMME_STANDARD)


def test_a_row_without_a_course_name_never_votes():
    # It cannot say which course it means (classic Generate would name it from
    # the catalogue, the scheduler by its bare code), so its value is ignored.
    decision = merge_programme_patterns(
        [row("AI2", vote=TWO_BY_100), row("CS2", vote=PROGRAMME_STANDARD, named=False)]
    )
    assert (decision.status, decision.meeting_pattern) == (AGREE, TWO_BY_100)
    assert ("CS2", "") in decision.programmes


def test_a_shape_on_a_row_its_credit_hours_do_not_fit_votes_standard():
    decision = merge_programme_patterns([row("AI2", vote=TWO_BY_100, credits=4)])
    assert (decision.status, decision.meeting_pattern) == (AGREE, PROGRAMME_STANDARD)


def test_an_unknown_stored_value_abstains_and_is_never_raised():
    decision = merge_programme_patterns([row("AI2", vote="3x60"), row("DS2", vote=TWO_BY_100)])
    assert (decision.status, decision.meeting_pattern) == (AGREE, TWO_BY_100)


@pytest.mark.parametrize("vote", [TWO_BY_100, PROGRAMME_STANDARD])
def test_rows_teaching_a_course_at_different_credit_hours_decide_nothing(vote):
    """AI teaches the course at 3 credits and voted; DS2 teaches the same
    identity at 4, where no vote is possible. One scenario budget cannot be
    both, and the engines size it differently (classic Generate from the first
    student it meets, the scheduler from the largest), so a shape would depend
    on student order: the default stands and the course is reported."""
    ai, ds2 = row("AI", vote=vote), row("DS2", credits=4)
    for rows in ([ai, ds2], [ds2, ai]):
        assert merge_programme_patterns(rows) == ProgrammeShape(
            MIXED, "", (("AI", vote), ("DS2", ""))
        )
        assert programme_shape_for(STAT, rows) == merge_programme_patterns(rows)
        assert programme_shape_for("STAT103", rows).status == MIXED


def test_different_credit_hours_with_nobody_voting_decide_nothing_either():
    decision = merge_programme_patterns([row("AI"), row("DS2", credits=4)])
    assert decision == ProgrammeShape(NO_VOTE, "", (("AI", ""), ("DS2", "")))


def test_credit_hours_are_compared_as_the_engines_read_them():
    # "3" and a missing value (read as 3) are three credit hours, like 3.
    rows = [row("AI", vote=TWO_BY_100), row("AI2", credits="3"), row("DS2", credits=None)]
    assert merge_programme_patterns(rows).status == AGREE
    assert merge_programme_patterns(rows).meeting_pattern == TWO_BY_100


def test_a_nameless_row_at_other_credit_hours_counts_too():
    """An Arabic plan name keys as the bare code, and so does a nameless row:
    the scheduler groups both into one offering sized at the larger credit
    hours. Ignoring the nameless row would let classic Generate give a 3-credit
    budget 2 x 100 that the scheduler builds at 4 credits."""
    rows = [row("AI2", "AR201", TWO_BY_100), row("DS2", "AR201", credits=4, named=False)]
    assert programme_shape_for("AR201", rows).status == MIXED


def test_row_order_and_programme_case_never_matter():
    rows = [row("ds2", vote=TWO_BY_100), row("AI2", vote=PROGRAMME_STANDARD), row(" cs2 ")]
    assert merge_programme_patterns(rows) == merge_programme_patterns(list(reversed(rows)))
    assert [p for p, _ in merge_programme_patterns(rows).programmes] == ["AI2", "CS2", "DS2"]


# ── which rows decide a key ──────────────────────────────────────────────────


def test_rows_of_the_exact_identity_decide_and_a_same_code_twin_does_not():
    rows = [row("AI", LA, TWO_BY_100), row("CYP2", LA_CYP2, PROGRAMME_STANDARD)]
    assert programme_shape_for(LA, rows).meeting_pattern == TWO_BY_100
    assert programme_shape_for(LA_CYP2, rows).meeting_pattern == PROGRAMME_STANDARD


def test_a_bare_code_with_two_courses_behind_it_is_never_guessed():
    rows = [row("AI", LA, TWO_BY_100), row("CYP2", LA_CYP2, TWO_BY_100)]
    assert programme_shape_for("MATH243", rows) == ProgrammeShape(AMBIGUOUS, "", ())


def test_a_bare_code_with_one_course_behind_it_is_that_course():
    rows = [row("AI2", STAT, TWO_BY_100), row("DS2", STAT)]
    assert programme_shape_for("STAT103", rows).meeting_pattern == TWO_BY_100


def test_a_row_whose_identity_is_the_bare_code_wins_over_the_code_rule():
    # An Arabic plan name normalises to nothing: that row's identity IS the code.
    rows = [row("IS", "MGT201", TWO_BY_100), row("IS2", "MGT201::MANAGEMENT", PROGRAMME_STANDARD)]
    assert programme_shape_for("MGT201", rows).meeting_pattern == TWO_BY_100


def test_a_typed_code_is_read_as_a_whole_code_never_as_a_prefix():
    rows = [
        row("AI2", STAT, TWO_BY_100),
        row("AI", LA, TWO_BY_100),
        row("CYP2", LA_CYP2, PROGRAMME_STANDARD),
    ]
    # STAT10 is not STAT103, and MATH24 is neither MATH243 course.
    assert programme_shape_for("STAT10", rows) == ProgrammeShape(NO_VOTE, "", ())
    assert programme_shape_for("MATH24", rows) == ProgrammeShape(NO_VOTE, "", ())


def test_a_named_key_no_row_has_is_undecided():
    assert programme_shape_for("STAT103::OTHER", [row("AI2", vote=TWO_BY_100)]).status == NO_VOTE


# ── what a scenario budget takes ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("decision", "credits", "policy", "expected"),
    [
        (ProgrammeShape(AGREE, TWO_BY_100, ()), 3, False, TWO_BY_100),
        (ProgrammeShape(AGREE, PROGRAMME_STANDARD, ()), 3, False, ""),
        (ProgrammeShape(MIXED, "", ()), 3, False, ""),
        # Only an agreement yields a shape, whatever else a decision holds.
        (ProgrammeShape(MIXED, TWO_BY_100, ()), 3, False, ""),
        (ProgrammeShape(NO_VOTE, "", ()), 3, False, ""),
        (ProgrammeShape(AMBIGUOUS, "", ()), 3, False, ""),
        (ProgrammeShape(AGREE, TWO_BY_100, ()), 4, False, ""),
        (ProgrammeShape(AGREE, TWO_BY_100, ()), "3", False, TWO_BY_100),
        (ProgrammeShape(AGREE, TWO_BY_100, ()), 3, True, ""),
    ],
)
def test_a_budget_takes_two_by_100_only_from_an_agreement_that_fits_it(
    decision, credits, policy, expected
):
    assert department_budget_value(decision, credits, has_policy=policy) == expected


def test_a_programme_value_as_a_budget_stores_it():
    assert budget_form(TWO_BY_100) == TWO_BY_100
    assert budget_form(PROGRAMME_STANDARD) == ""
    assert budget_form("") == ""


# ── what may be written ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "credits", "expected"),
    [("", 4, ""), (None, None, ""), (" Standard ", 3, "standard"), ("2X100", "3", "2x100")],
)
def test_a_vote_is_stored_normalised(value, credits, expected):
    assert validate_programme_meeting_pattern(value, credits) == expected


@pytest.mark.parametrize(
    ("value", "credits"), [("2x100", 4), ("standard", 2), ("2x100", None), ("weekly", 3)]
)
def test_a_vote_that_does_not_fit_is_refused(value, credits):
    with pytest.raises(ValueError):
        validate_programme_meeting_pattern(value, credits, course_code="STAT103")


# ── the rule stays pure: the scheduler imports it ────────────────────────────


def test_the_shape_module_imports_nothing_but_the_standard_library():
    path = Path(__file__).resolve().parents[1] / "core/services/timetable_meeting_shape.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "dataclasses", "collections", "typing"}, imported


@pytest.mark.parametrize(
    "name",
    [
        "TRAINING",
        "GRADUATION I",
        "GRADUATION  PROJECT II",
        "COOPERATIVE TRAINING (CONTINUING WITH SUMMER)",
    ],
)
def test_every_course_the_scheduler_leaves_unscheduled_classic_generate_excludes(name):
    # core never imports scheduler code, so the editor and importers use the
    # classic rule; it must cover the scheduler's (scheduler/intake.py).
    from scheduler.intake import is_unscheduled_course

    assert is_unscheduled_course(name)
    assert is_generate_excluded_course("XYZ201", name)


# ── the database refuses a vote where there is no choice (migration 0078) ────


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("value", "credits", "allowed"),
    [
        ("", None, True),
        ("", 4, True),
        ("2x100", 3, True),
        ("standard", 3, True),
        ("2x100", 4, False),
        ("2x100", None, False),
        ("standard", 4, False),
        ("2X100", 3, False),
    ],
)
def test_the_database_keeps_a_vote_to_three_credit_rows(value, credits, allowed):
    def create():
        with transaction.atomic():
            ProgrammeRequirement.objects.create(
                program="AI2",
                course_code="STAT103",
                course_name="Principles of Probability and Statistics",
                credit_hours=credits,
                meeting_pattern=value,
            )

    if allowed:
        create()
        assert ProgrammeRequirement.objects.get(program="AI2").meeting_pattern == value
    else:
        with pytest.raises(IntegrityError):
            create()


@pytest.mark.django_db
def test_models_and_migrations_agree():
    out = io.StringIO()
    call_command("makemigrations", "core", "--check", "--dry-run", stdout=out)
    assert "No changes detected" in out.getvalue()


# ── the read side ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({}, None),
        ({"credit_hours": 4}, pmp.NOT_THREE_CREDITS),
        ({"credit_hours": None}, pmp.NOT_THREE_CREDITS),
        ({"course_name": "  "}, pmp.NOT_NAMED),
        ({"type": "Program Elective"}, pmp.PLACEHOLDER),
        ({"course_code": "MATH101"}, pmp.EXCLUDED),
        ({"course_name": "Graduation Project II"}, pmp.EXCLUDED),
        ({"course_code": "ENG102"}, pmp.POLICY),
    ],
)
def test_which_rows_may_vote(fields, reason):
    base = {
        "program": "AI2",
        "course_code": "STAT103",
        "course_name": "Principles of Probability and Statistics",
        "credit_hours": 3,
        "type": "Mandatory",
        "meeting_pattern": "",
    }
    assert pmp.ineligibility({**base, **fields}) == reason


@pytest.mark.django_db
def test_a_scenarios_programmes_come_from_the_scenario_else_from_its_boards():
    named = TimetableScenario.objects.create(
        academic_year="1448", term="2", name="named", programs=["ds2", "AI2"]
    )
    older = TimetableScenario.objects.create(
        academic_year="1448", term="2", name="older", programs=[]
    )
    DeliveryBoard.objects.create(scenario=older, label="T3", program="DS,AI2", nominal_term=3)
    DeliveryBoard.objects.create(scenario=older, label="T4", program="AI2", nominal_term=4)
    empty = TimetableScenario.objects.create(
        academic_year="1448", term="2", name="empty", programs=[]
    )
    assert pmp.scenario_programmes(named) == ["AI2", "DS2"]
    assert pmp.scenario_programmes(older) == ["AI2", "DS"]
    assert pmp.scenario_programmes(empty) == []


@pytest.mark.django_db
def test_a_scenarios_stored_programmes_win_over_its_boards_labels():
    """Generate decided an AI2 scenario from AI2's rows. A board labelled CS2,
    added later by hand, must not bring CS2's standard in: the workspace would
    then call the course mixed while Generate agreed on 2 x 100."""
    for program, vote in (("AI2", TWO_BY_100), ("CS2", PROGRAMME_STANDARD)):
        ProgrammeRequirement.objects.create(
            program=program,
            course_code="STAT103",
            course_name="Principles of Probability and Statistics",
            credit_hours=3,
            meeting_pattern=vote,
        )
    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="2", name="AI2 only", programs=["AI2"]
    )
    DeliveryBoard.objects.create(scenario=scenario, label="T3", program="CS2", nominal_term=3)
    assert pmp.scenario_programmes(scenario) == ["AI2"]
    decision = pmp.resolve_for_keys([STAT], pmp.scenario_programmes(scenario))[STAT]
    assert (decision.status, decision.meeting_pattern) == (AGREE, TWO_BY_100)


def test_a_course_name_of_only_spaces_is_no_name_and_never_votes():
    base = {"course_code": "IS233", "credit_hours": 3, "type": "Mandatory"}
    rows = pmp.pre_key(
        [
            {**base, "program": "AI2", "course_name": "  ", "meeting_pattern": TWO_BY_100},
            {**base, "program": "DS2", "course_name": "", "meeting_pattern": ""},
        ]
    )
    assert [(r.identity, r.named, r.vote) for r in rows] == [
        ("IS233", False, ""),
        ("IS233", False, ""),
    ]
    assert programme_shape_for("IS233", rows).status == NO_VOTE


@pytest.mark.django_db
def test_an_agreed_standard_is_put_in_the_budgets_own_credit_hours():
    """Add course may store a course at other credit hours than its programmes'
    rows (here 4, from 3-credit rows that chose standard). The budget is taught
    at its own credit default, so the words say that one, not 2 x 75."""
    from core.models import ScenarioSectionBudget
    from core.services.timetable_workspace import compute_scenario_budget

    ProgrammeRequirement.objects.create(
        program="DS2",
        course_code="DS201",
        course_name="Data Wrangling",
        credit_hours=3,
        meeting_pattern=PROGRAMME_STANDARD,
    )
    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="2", name="Added at 4 credits", programs=["DS2"]
    )
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key="DS201::DATA_WRANGLING",
        course_code="DS201",
        course_name="Data Wrangling",
        credit_hours=4,
        planned_sections=1,
    )
    [budget] = compute_scenario_budget(scenario.id, department_for=scenario)
    block = budget["programme_meeting_pattern"]
    assert (block["status"], block["meeting_pattern"]) == (AGREE, PROGRAMME_STANDARD)
    assert (block["label"], block["label_ar"]) == (
        "Standard (75 + 75 + 100 min)",
        "قياسي (٧٥ + ٧٥ + ١٠٠ دقيقة)",
    )
    assert budget["meeting_pattern_drift"] is False


@pytest.mark.django_db
def test_a_scenarios_courses_are_decided_from_its_own_programmes_rows_in_one_query(
    django_assert_num_queries,
):
    for program, vote in (("AI2", "2x100"), ("DS2", ""), ("CS2", "standard")):
        ProgrammeRequirement.objects.create(
            program=program,
            course_code="STAT103",
            course_name="Principles of Probability and Statistics",
            credit_hours=3,
            meeting_pattern=vote,
        )
    with django_assert_num_queries(1):
        decided = pmp.resolve_for_keys([STAT, "STAT103", "stat 103"], ["AI2", "DS2"])
    assert {k: (d.status, d.meeting_pattern) for k, d in decided.items()} == {
        STAT: (AGREE, TWO_BY_100),
        "STAT103": (AGREE, TWO_BY_100),
        "stat 103": (AGREE, TWO_BY_100),
    }
    with_cs2 = pmp.resolve_for_keys([STAT], ["AI2", "DS2", "CS2"])[STAT]
    assert (with_cs2.status, with_cs2.meeting_pattern) == (MIXED, "")
    with django_assert_num_queries(0):
        assert pmp.resolve_for_keys([STAT], [])[STAT].status == NO_VOTE


@pytest.mark.parametrize(
    ("decision", "row_pattern", "choices", "drift"),
    [
        (ProgrammeShape(AGREE, TWO_BY_100, ()), "", ["", "2x100"], True),
        (ProgrammeShape(AGREE, TWO_BY_100, ()), "2x100", ["", "2x100"], False),
        (ProgrammeShape(AGREE, PROGRAMME_STANDARD, ()), "2x100", ["", "2x100"], True),
        (ProgrammeShape(AGREE, PROGRAMME_STANDARD, ()), "", ["", "2x100"], False),
        # A scenario's own 2 x 100 over programmes that set nothing is not drift.
        (ProgrammeShape(NO_VOTE, "", ()), "2x100", ["", "2x100"], False),
        (ProgrammeShape(MIXED, "", ()), "2x100", ["", "2x100"], False),
        (ProgrammeShape(AGREE, "", ()), "2x100", ["", "2x100"], False),
        # An English bundle or another credit hours: 2 x 100 is not a choice.
        (ProgrammeShape(AGREE, TWO_BY_100, ()), "", [""], False),
    ],
)
def test_drift_is_only_an_agreed_set_value_the_row_could_take_and_does_not(
    decision, row_pattern, choices, drift
):
    budget_row = {"meeting_pattern": row_pattern, "meeting_pattern_choices": choices}
    assert pmp.is_drift(decision, budget_row) is drift


def test_the_pages_get_the_votes_in_words_with_the_engines_minutes():
    assert pmp.pattern_choice_options() == [
        {"value": "", "label": "Not set", "label_ar": "غير محدد"},
        {"value": "standard", "label": "Standard (2 × 75 min)", "label_ar": "قياسي (٢ × ٧٥ دقيقة)"},
        {"value": "2x100", "label": "2 × 100 min", "label_ar": "٢ × ١٠٠ دقيقة"},
    ]
    block = pmp.decision_block(ProgrammeShape(AGREE, TWO_BY_100, (("AI2", "2x100"), ("DS2", ""))))
    assert block == {
        "status": "agree",
        "meeting_pattern": "2x100",
        "label": "2 × 100 min",
        "label_ar": "٢ × ١٠٠ دقيقة",
        "programmes": [
            {"program": "AI2", "meeting_pattern": "2x100"},
            {"program": "DS2", "meeting_pattern": ""},
        ],
    }
    assert pmp.decision_block(ProgrammeShape(MIXED, "", ()))["label"] == ""

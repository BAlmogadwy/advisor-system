"""Both engines take a course's weekly meetings from its programmes' votes.

Owner, 2026-11-10: each programme sets its own 3-credit courses to two 100-minute
meetings a week, or to the standard 2 x 75 on purpose, or leaves them unset
(``ProgrammeRequirement.meeting_pattern``). A scenario pools a course identity
(CODE::NAME) over its programmes: the programmes that voted must agree, an unset
programme does not count, and a disagreement keeps the credit default.

* Classic Generate decides it once, before it writes anything, copies the value
  into each ``ScenarioSectionBudget`` and reports what it decided
  (``department_meeting_patterns``).
* The new scheduler is handed Generate's values by the planner job, so the board
  follows its own budget even when a vote changed while the job was queued.
  Without them (``sch_plan``, the benchmarks) intake decides by the same rule
  over the same plan rows, and on one shared fixture it decides every course
  both engines build exactly as Generate does.

With no programme value anywhere, nothing changes: budgets, the response (one
extra empty list), offerings and snapshot fingerprints.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from core.models import (
    Course,
    PlannerJob,
    ProgrammeRequirement,
    ScenarioSectionBudget,
    Student,
    TimetableScenario,
)
from core.services import programme_meeting_patterns as pmp
from core.services import timetable_meeting_shape
from core.services.course_identity import planner_course_key
from core.services.timetable_generate import generate_workspace_scenario
from core.services.timetable_meeting_shape import (
    AGREE,
    MIXED,
    PROGRAMME_STANDARD,
    TWO_BY_100,
    MeetingShape,
    shape_for,
)
from scheduler.domain import DeliveryMode, MeetingKind, MeetingRequirement
from scheduler.intake import (
    build_snapshot,
    compile_requirements,
    is_online_course,
    programme_votes,
)

pytestmark = pytest.mark.django_db

PROGRAMMES = ["AI", "AI2", "DS2"]
YEAR, TERM = 1448, 1

STAT = ("STAT103", "PRINCIPLES OF PROBABILITY AND STATISTICS")
LA_I = ("MATH243", "243 LINEAR ALGEBRA I")
LA = ("MATH243", "LINEAR ALGEBRA")
PROG_I = ("CS112", "PROGRAMMING I")
PROG_II = ("CS112", "PROGRAMMING II")
WRANGLING = ("DS201", "DATA WRANGLING")
ENGLISH = ("ENG102", "ENGLISH LANGUAGE II")
ONLINE = ("GS150", "ENTREPRENEURSHIP")
NAMELESS = ("IS231", "")
ARABIC = ("AR201", "مهارات الاتصال")
FOUR_CREDITS = ("AI201", "MACHINE LEARNING")
PROJECT = ("AI492", "GRADUATION PROJECT II")


def key(course: tuple[str, str]) -> str:
    return planner_course_key(*course)


# (programme, course, credit hours, vote). IS2 is not one of the scenario's
# programmes: its rows must never count.
CURRICULUM = (
    # AI2 chose 2 x 100 and DS2 has not set it: an unset programme abstains.
    ("AI2", STAT, 3, TWO_BY_100),
    ("DS2", STAT, 3, ""),
    ("IS2", STAT, 3, PROGRAMME_STANDARD),
    # MATH243 names two courses. LINEAR ALGEBRA I: AI chose 2 x 100, AI2 chose
    # standard on purpose, so they differ. LINEAR ALGEBRA (DS2): 2 x 100.
    ("AI", LA_I, 3, TWO_BY_100),
    ("AI2", LA_I, 3, PROGRAMME_STANDARD),
    ("DS2", LA, 3, TWO_BY_100),
    # CS112 names two courses too: PROGRAMMING I agreed on 2 x 100, PROGRAMMING
    # II on standard. Neither may take the other's vote.
    ("AI2", PROG_I, 3, TWO_BY_100),
    ("DS2", PROG_I, 3, TWO_BY_100),
    ("AI", PROG_II, 3, PROGRAMME_STANDARD),
    # Standard chosen on purpose: decided, and the budget keeps the default.
    ("DS2", WRANGLING, 3, PROGRAMME_STANDARD),
    # A 3-credit English course: its fixed weekly bundle wins over any vote.
    ("AI", ENGLISH, 3, TWO_BY_100),
    # An online course the programmes agree on.
    ("AI2", ONLINE, 3, TWO_BY_100),
    ("DS2", ONLINE, 3, ""),
    # No course name: the row cannot say which course it means, so it abstains.
    ("AI2", NAMELESS, 3, TWO_BY_100),
    # An Arabic name keys as the bare code in both engines, and it votes.
    ("AI2", ARABIC, 3, TWO_BY_100),
    # 4 credits: no vote is possible (the database keeps votes to 3 credits).
    ("AI", FOUR_CREDITS, 4, ""),
    # Never timetabled (classic excludes it; the scheduler leaves it unscheduled).
    ("AI", PROJECT, 3, TWO_BY_100),
)

#: What a budget of the scenario's programmes takes, per course key.
EXPECTED = {
    key(STAT): TWO_BY_100,
    key(LA_I): "",
    key(LA): TWO_BY_100,
    key(PROG_I): TWO_BY_100,
    key(PROG_II): "",
    key(WRANGLING): "",
    key(ENGLISH): "",
    key(ONLINE): TWO_BY_100,
    key(NAMELESS): "",
    key(ARABIC): TWO_BY_100,
    key(FOUR_CREDITS): "",
}

STUDENTS = {"AI": (440101, 440102), "AI2": (440201, 440202), "DS2": (440301, 440302)}


def make_curriculum(rows=CURRICULUM, *, votes: bool = True) -> None:
    for programme, (code, name), credits, vote in rows:
        ProgrammeRequirement.objects.create(
            program=programme,
            course_code=code,
            course_name=name,
            type="Mandatory",
            programme_term=1,
            credit_hours=credits,
            meeting_pattern=vote if votes else "",
        )


def make_students() -> list[int]:
    ids = []
    for programme, sids in STUDENTS.items():
        for sid in sids:
            Student.objects.create(
                student_id=sid,
                registration_no=str(sid),
                name=f"Student {sid}",
                program=programme,
                section="M",
                status="active",
            )
            ids.append(sid)
    return ids


def plan_codes(programme: str) -> list[str]:
    return list(
        ProgrammeRequirement.objects.filter(program=programme)
        .order_by("course_code")
        .values_list("course_code", flat=True)
    )


def recommend_every_plan_course(monkeypatch, student_ids: list[int]) -> None:
    """These students (only) are recommended every course of their own plan."""
    programme_of = dict(Student.objects.values_list("student_id", "program"))
    monkeypatch.setattr(
        "core.services.timetable_generate.get_student_ids", lambda **_kw: list(student_ids)
    )
    monkeypatch.setattr(
        "core.services.recommender_batch.batch_recommend_multi_program",
        lambda sids, _year, _term: {sid: plan_codes(programme_of[sid]) for sid in sids},
    )
    monkeypatch.setattr(
        "core.services.recommender_batch.batch_recommend",
        lambda sids, programme, _year, _term: {
            sid: plan_codes(programme) for sid in sids if sid in student_ids
        },
    )


@pytest.fixture
def department(monkeypatch):
    make_curriculum()
    students = make_students()
    recommend_every_plan_course(monkeypatch, students)
    return students


def generate(name: str = "Programme weekly meetings", **kwargs) -> dict:
    return generate_workspace_scenario(
        YEAR, TERM, PROGRAMMES, section="M", scenario_name=name, run_autoplace=False, **kwargs
    )


def budget_patterns(result: dict) -> dict[str, str]:
    return dict(
        ScenarioSectionBudget.objects.filter(scenario_id=result["scenario"]["id"]).values_list(
            "course_key", "meeting_pattern"
        )
    )


def snapshot(**kwargs):
    return build_snapshot(
        academic_year=str(YEAR),
        term=TERM,
        gender="M",
        programs=PROGRAMMES,
        default_capacity=25,
        **kwargs,
    )


def offerings_by_key(snap) -> dict:
    return {planner_course_key(o.course_code, o.course_name): o for o in snap.offerings}


def lectures(duration: int, count: int, delivery=DeliveryMode.IN_PERSON):
    return (MeetingRequirement(MeetingKind.LECTURE, delivery, duration, count),)


# ── classic Generate ─────────────────────────────────────────────────────────


def test_generate_copies_what_the_scenarios_programmes_decided_into_each_budget(department):
    patterns = budget_patterns(generate())
    assert {k: patterns[k] for k in EXPECTED} == EXPECTED
    # Never budgeted: classic Generate leaves graduation projects off the board.
    assert key(PROJECT) not in patterns


def test_generate_reports_every_course_a_programme_voted_on_and_what_its_budget_got(department):
    report = generate()["department_meeting_patterns"]

    def entry(course, status, programme_pattern, meeting_pattern, programmes):
        return {
            "course_key": key(course),
            "course_code": course[0],
            "course_name": course[1],
            "status": status,
            "programme_pattern": programme_pattern,
            "meeting_pattern": meeting_pattern,
            "programmes": [{"program": p, "meeting_pattern": v} for p, v in programmes],
            "no_window": False,
        }

    assert report == [
        entry(ARABIC, AGREE, TWO_BY_100, TWO_BY_100, [("AI2", TWO_BY_100)]),
        entry(PROG_I, AGREE, TWO_BY_100, TWO_BY_100, [("AI2", TWO_BY_100), ("DS2", TWO_BY_100)]),
        entry(PROG_II, AGREE, PROGRAMME_STANDARD, "", [("AI", PROGRAMME_STANDARD)]),
        entry(WRANGLING, AGREE, PROGRAMME_STANDARD, "", [("DS2", PROGRAMME_STANDARD)]),
        # The English bundle decides the budget, whatever the programme chose.
        entry(ENGLISH, AGREE, TWO_BY_100, "", [("AI", TWO_BY_100)]),
        entry(ONLINE, AGREE, TWO_BY_100, TWO_BY_100, [("AI2", TWO_BY_100), ("DS2", "")]),
        # An explicit standard is a veto: the votes differ, the default stands.
        entry(LA_I, MIXED, "", "", [("AI", TWO_BY_100), ("AI2", PROGRAMME_STANDARD)]),
        entry(LA, AGREE, TWO_BY_100, TWO_BY_100, [("DS2", TWO_BY_100)]),
        # IS2 chose standard, but IS2 is not one of this scenario's programmes.
        entry(STAT, AGREE, TWO_BY_100, TWO_BY_100, [("AI2", TWO_BY_100), ("DS2", "")]),
    ]


@pytest.mark.parametrize("first", ["AI", "AI2"])
def test_generates_report_is_in_course_code_then_course_key_order(monkeypatch, first):
    """Whichever programme's students come first. CS112 names two courses (the
    plan keeps them in the order its students met them), and CS112 is a prefix
    of CS1121, where course-key order alone would put CS1121 first (":" sorts
    after the digits)."""
    longer = ("CS1121", "DATA PROGRAMMING")
    make_curriculum(
        [
            ("AI", PROG_II, 3, PROGRAMME_STANDARD),
            ("AI2", PROG_I, 3, TWO_BY_100),
            ("AI2", longer, 3, TWO_BY_100),
        ]
    )
    make_students()
    second = "AI2" if first == "AI" else "AI"
    recommend_every_plan_course(monkeypatch, [*STUDENTS[first], *STUDENTS[second]])
    report = generate()["department_meeting_patterns"]
    assert [row["course_key"] for row in report] == [key(PROG_I), key(PROG_II), key(longer)]


def test_generate_keeps_two_courses_behind_one_code_apart(department):
    patterns = budget_patterns(generate())
    # CS112 PROGRAMMING I is agreed 2 x 100; PROGRAMMING II, same code, standard.
    assert (patterns[key(PROG_I)], patterns[key(PROG_II)]) == (TWO_BY_100, "")
    # MATH243 LINEAR ALGEBRA (DS2) is 2 x 100; the other MATH243 is mixed.
    assert (patterns[key(LA)], patterns[key(LA_I)]) == (TWO_BY_100, "")


@pytest.mark.parametrize("first", ["AI", "DS2"])
def test_one_identity_at_two_credit_hours_keeps_the_default_in_both_engines(monkeypatch, first):
    """One identity, 3 credits in AI (2 x 100) and 4 in DS2 (no vote possible),
    both programmes' students recommended it, in either order. Classic Generate
    sizes the budget from the first student it meets, the scheduler from the
    largest row, so a 2 x 100 here would follow student order. The rows'
    credit hours differ, so the programmes decide nothing: both engines keep
    the default whoever comes first, and Generate reports the course as mixed.
    (The budget's credit hours still follow the first student: that credit
    divergence predates the weekly meetings and is not this rule's.)"""
    make_curriculum([("AI", FOUR_CREDITS, 3, TWO_BY_100), ("DS2", FOUR_CREDITS, 4, "")])
    make_students()
    second = "DS2" if first == "AI" else "AI"
    recommend_every_plan_course(monkeypatch, [*STUDENTS[first], *STUDENTS[second]])
    result = generate()
    budget = ScenarioSectionBudget.objects.get(scenario_id=result["scenario"]["id"])
    assert (budget.course_key, budget.total_demand, budget.meeting_pattern) == (
        key(FOUR_CREDITS),
        4,
        "",
    )
    assert result["department_meeting_patterns"] == [
        {
            "course_key": key(FOUR_CREDITS),
            "course_code": FOUR_CREDITS[0],
            "course_name": FOUR_CREDITS[1],
            "status": MIXED,
            "programme_pattern": "",
            "meeting_pattern": "",
            "programmes": [
                {"program": "AI", "meeting_pattern": TWO_BY_100},
                {"program": "DS2", "meeting_pattern": ""},
            ],
            "no_window": False,
        }
    ]
    handed = {budget.course_key: budget.meeting_pattern}
    for given in (handed, None):
        offering = offerings_by_key(snapshot(meeting_patterns=given))[key(FOUR_CREDITS)]
        assert (offering.credit_hours, offering.meeting_pattern) == (4, "")
        assert offering.requirements == compile_requirements(4, is_online=False)
        assert shape_for(budget.meeting_pattern, budget.credit_hours) is None


@pytest.mark.parametrize("first", ["AI2", "DS2"])
def test_a_nameless_row_pooled_through_the_catalogue_at_other_credit_hours_keeps_the_default(
    monkeypatch, first
):
    """AI2's IS232 row has no name and 4 credits; DS2's names SYSTEMS ANALYSIS
    at 3 credits and voted 2 x 100. Generate names AI2's row after the
    catalogue, so both programmes' students share one budget, sized from
    whichever student comes first. AI2's row never matches that identity (its
    own is the bare code), so the rows of the identity alone agree on 2 x 100:
    the credit hours of the students pooled into the budget differ, so it is
    mixed and keeps the default in either order, and the scheduler is handed
    the default for it."""
    analysis = ("IS232", "SYSTEMS ANALYSIS")
    make_curriculum([("AI2", ("IS232", ""), 4, ""), ("DS2", analysis, 3, TWO_BY_100)])
    Course.objects.create(course_code="IS232", description="SYSTEMS ANALYSIS", credit_hours=3)
    make_students()
    second = "DS2" if first == "AI2" else "AI2"
    recommend_every_plan_course(monkeypatch, [*STUDENTS[first], *STUDENTS[second]])

    result = generate()

    budget = ScenarioSectionBudget.objects.get(scenario_id=result["scenario"]["id"])
    assert (budget.course_key, budget.total_demand, budget.meeting_pattern) == (
        key(analysis),
        4,
        "",
    )
    # The budget's credit hours still follow the first student (that predates
    # the weekly meetings); its meetings no longer do.
    assert budget.credit_hours == (4 if first == "AI2" else 3)
    assert result["department_meeting_patterns"] == [
        {
            "course_key": key(analysis),
            "course_code": "IS232",
            "course_name": "SYSTEMS ANALYSIS",
            "status": MIXED,
            "programme_pattern": "",
            "meeting_pattern": "",
            "programmes": [{"program": "DS2", "meeting_pattern": TWO_BY_100}],
            "no_window": False,
        }
    ]
    handed = dict(
        ScenarioSectionBudget.objects.filter(scenario_id=result["scenario"]["id"]).values_list(
            "course_key", "meeting_pattern"
        )
    )
    assert handed == {key(analysis): ""}
    offerings = offerings_by_key(snapshot(meeting_patterns=handed))
    named, bare = offerings[key(analysis)], offerings["IS232"]
    assert (named.programs, named.credit_hours, named.meeting_pattern) == ({"DS2"}, 3, "")
    assert named.requirements == lectures(75, 2)
    assert (bare.programs, bare.credit_hours, bare.meeting_pattern) == ({"AI2"}, 4, "")
    assert bare.requirements == compile_requirements(4, is_online=False)


def test_a_programme_with_no_students_in_the_course_still_has_its_say(monkeypatch):
    """Plan ownership decides, not this term's demand (the engines' demand
    differs; ownership is what they share). No AI student takes DS301 this term,
    but AI chose standard for it on purpose, so DS2's 2 x 100 does not stand -
    in either engine."""
    course = ("DS301", "BIG DATA ANALYTICS")
    make_curriculum([("AI", course, 3, PROGRAMME_STANDARD), ("DS2", course, 3, TWO_BY_100)])
    make_students()
    recommend_every_plan_course(monkeypatch, list(STUDENTS["DS2"]))
    result = generate()
    assert budget_patterns(result) == {key(course): ""}
    [entry] = result["department_meeting_patterns"]
    assert (entry["status"], entry["programmes"]) == (
        MIXED,
        [
            {"program": "AI", "meeting_pattern": PROGRAMME_STANDARD},
            {"program": "DS2", "meeting_pattern": TWO_BY_100},
        ],
    )
    snap = snapshot()
    assert {d.program for d in snap.demand} == {"DS2"}, "AI must have no demand here"
    offering = offerings_by_key(snap)[key(course)]
    assert (offering.meeting_pattern, offering.requirements) == ("", lectures(75, 2))


def test_with_nothing_set_generate_writes_and_answers_exactly_as_before(monkeypatch):
    """A department that has set nothing gets today's budgets and response, plus
    one empty list; a department that has voted changes the weekly meetings and
    nothing else (keys, demand, sizing, terms, boards, students)."""
    make_curriculum(votes=False)
    students = make_students()
    recommend_every_plan_course(monkeypatch, students)
    before = generate()
    assert before["department_meeting_patterns"] == []
    assert set(budget_patterns(before).values()) == {""}
    assert {row["meeting_pattern"] for row in before["section_budget"]} == {""}

    for programme, (code, _name), _credits, vote in CURRICULUM:
        ProgrammeRequirement.objects.filter(program=programme, course_code=code).update(
            meeting_pattern=vote
        )
    after = generate("Programme weekly meetings, voted")
    assert after["department_meeting_patterns"], "the votes must have been read"

    def budgets(result):
        rows = ScenarioSectionBudget.objects.filter(scenario_id=result["scenario"]["id"])
        return sorted(rows.values_list(*NON_PATTERN_BUDGET_FIELDS))

    assert budgets(after) == budgets(before)
    for part in ("section_plan", "plan_summary", "student_summary", "unserved_elective_slots"):
        assert after[part] == before[part], part
    assert [_board(b) for b in after["boards"]] == [_board(b) for b in before["boards"]]
    assert set(after) == set(before)

    def unpatterned(rows):
        return [{k: v for k, v in row.items() if k not in PATTERN_FIELDS} for row in rows]

    assert unpatterned(after["section_budget"]) == unpatterned(before["section_budget"])
    changed = {
        row["course_key"]: row["meeting_durations"]
        for row in after["section_budget"]
        if row["meeting_pattern"]
    }
    assert changed == {k: [100, 100] for k, v in EXPECTED.items() if v}


NON_PATTERN_BUDGET_FIELDS = (
    "course_key",
    "course_code",
    "course_name",
    "department",
    "credit_hours",
    "planned_sections",
    "max_per_section",
    "total_demand",
    "programme_term",
)
#: The section_budget fields the weekly shape decides (compute_scenario_budget).
PATTERN_FIELDS = {
    "meeting_pattern",
    "meeting_pattern_label",
    "meeting_pattern_label_ar",
    "meeting_durations",
    "meetings_per_week",
    "required_meetings_per_section",
    "requires_full_section_pattern",
}


def _board(board: dict) -> dict:
    return {k: v for k, v in board.items() if k != "id"}


ONLINE_ONLY_LAB_COLUMNS = [
    {"label": "Online 1", "start": "15:50", "end": "17:30", "online_only": True},
    {"label": "Online 2", "start": "17:40", "end": "19:20", "online_only": True},
]


@pytest.mark.parametrize(
    ("lab_columns", "no_window"),
    [
        (ONLINE_ONLY_LAB_COLUMNS, True),
        ([{"label": "Lab A", "start": "08:00", "end": "09:40"}], False),
    ],
)
def test_generate_says_when_a_two_by_100_course_has_no_in_person_window(
    department, lab_columns, no_window
):
    """Every classic placer takes an in-person 100-minute meeting from the
    placeable 100-minute columns only. With none, a 2 x 100 course could never be
    placed in person, so Generate says so at once (the readiness check would say
    it later). A course that kept the default needs no such window."""
    report = generate(lab_slot_config=lab_columns)["department_meeting_patterns"]
    flags = {row["course_key"]: row["no_window"] for row in report}
    assert flags == {
        row["course_key"]: no_window and row["meeting_pattern"] == TWO_BY_100 for row in report
    }
    assert flags[key(STAT)] is no_window
    assert flags[key(LA_I)] is False  # mixed: the budget kept the default


# ── the new scheduler ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("credits", "online", "pattern", "expected"),
    [
        (3, False, TWO_BY_100, lectures(100, 2)),
        (3, True, TWO_BY_100, lectures(100, 2, DeliveryMode.ONLINE)),
        (3, False, " 2X100 ", lectures(100, 2)),
        (3, False, "", lectures(75, 2)),
        (3, False, PROGRAMME_STANDARD, lectures(75, 2)),
        (3, False, "3x50", lectures(75, 2)),
        (2, False, TWO_BY_100, lectures(100, 1)),
        (
            4,
            False,
            TWO_BY_100,
            (
                MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, 2),
                MeetingRequirement(MeetingKind.LAB, DeliveryMode.IN_PERSON, 100, 1),
            ),
        ),
    ],
)
def test_a_weekly_shape_compiles_to_its_own_lectures_only_where_it_fits(
    credits, online, pattern, expected
):
    assert compile_requirements(credits, is_online=online, meeting_pattern=pattern) == expected


def test_the_compiled_meetings_are_read_from_the_shape(monkeypatch):
    """Not a copy of 2 x 100: another shape compiles to its own meetings."""
    monkeypatch.setitem(
        timetable_meeting_shape.SHAPES,
        "75+100",
        MeetingShape("75+100", (100, 75), 3, "75 + 100 min", "٧٥ + ١٠٠ دقيقة"),
    )
    assert compile_requirements(3, is_online=False, meeting_pattern="75+100") == (
        MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 75, 1),
        MeetingRequirement(MeetingKind.LECTURE, DeliveryMode.IN_PERSON, 100, 1),
    )


def test_intake_reads_each_plan_row_as_classic_generate_does():
    """Intake may not import the module Generate pre-keys with (it carries the
    old engine's exclusion rule), so it builds the same tuples itself."""
    rows = [
        {
            "program": " ai2 ",
            "course_code": "stat103",
            "course_name": "Principles of Probability and Statistics",
            "credit_hours": 3,
            "meeting_pattern": "2x100",
        },
        {
            "program": "DS2",
            "course_code": "CS 112",
            "course_name": "PROGRAMMING\u00a0I",
            "credit_hours": 3,
            "meeting_pattern": "standard",
        },
        {
            "program": "AI",
            "course_code": "IS231",
            "course_name": "",
            "credit_hours": 3,
            "meeting_pattern": "2x100",
        },
        {
            "program": "AI",
            "course_code": "IS232",
            "course_name": None,
            "credit_hours": None,
            "meeting_pattern": None,
        },
        {
            "program": "AI2",
            "course_code": "AR201",
            "course_name": "مهارات الاتصال",
            "credit_hours": "3",
            "meeting_pattern": "2X100",
        },
        {
            "program": "AI2",
            "course_code": "AI201",
            "course_name": "AI201",
            "credit_hours": 4,
            "meeting_pattern": "",
        },
        {
            # A name of only spaces is no name: the row never votes, in
            # either engine.
            "program": "DS2",
            "course_code": "IS233",
            "course_name": "  ",
            "credit_hours": 3,
            "meeting_pattern": "2x100",
        },
    ]
    assert programme_votes(rows) == pmp.pre_key(rows)
    assert [v.identity for v in programme_votes(rows)] == [
        "STAT103::PRINCIPLES_OF_PROBABILITY_AND_STATISTICS",
        "CS112::PROGRAMMING_I",
        "IS231",
        "IS232",
        "AR201",
        "AI201",
        "IS233",
    ]
    assert [v.named for v in programme_votes(rows)] == [True, True, False, False, True, True, False]
    assert programme_votes(rows)[-1].vote == ""


def test_intake_refuses_a_plan_row_read_without_its_vote():
    """A query that forgot the column must fail loudly, not read every course
    as "not set" and quietly drop the department's choices."""
    row = {
        "program": "AI2",
        "course_code": "STAT103",
        "course_name": "Principles of Probability and Statistics",
        "credit_hours": 3,
    }
    with pytest.raises(KeyError, match="meeting_pattern"):
        programme_votes([row])


def test_without_generates_values_intake_decides_each_course_as_generate_does(department):
    """One fixture, both engines, every course both of them build.

    Known divergences sit outside that set and are named, not hidden. They come
    from how the engines key a nameless plan row the catalogue names: classic
    Generate keys its students by the catalogue description, the scheduler by
    the bare code.

    * Alone (IS232), the row never votes and both engines keep the default.
    * Beside another programme's row that names the course as the catalogue
      does and votes 2 x 100 (IS234), classic pools the nameless programme's
      students into that course's budget, which takes 2 x 100, while the
      scheduler builds them a bare-code offering of their own at the default:
      the nameless programme's students meet 2 x 75 there and 2 x 100 in
      classic. That offering's key has no budget, so Check says nothing.
      (At other credit hours the pooled budget is mixed and keeps the default:
      ``test_a_nameless_row_pooled_through_the_catalogue_at_other_credit_hours_keeps_the_default``.)
    * A 101 course with a stray vote: classic never budgets a 101 course; the
      scheduler schedules it, and applies the vote.

    Section Planning never lets a nameless or 101 row hold a vote
    (``programme_meeting_patterns.ineligibility``); the keying itself predates
    the weekly meetings.
    """
    nameless_pair = ("IS234", "DATABASE SYSTEMS")
    make_curriculum(
        [
            ("AI2", ("IS232", ""), 3, TWO_BY_100),
            ("AI", ("MATH101", "CALCULUS I"), 3, TWO_BY_100),
            ("AI2", ("IS234", ""), 3, ""),
            ("DS2", nameless_pair, 3, TWO_BY_100),
        ]
    )
    Course.objects.create(course_code="IS232", description="SYSTEMS ANALYSIS", credit_hours=3)
    Course.objects.create(course_code="IS234", description="DATABASE SYSTEMS", credit_hours=3)

    budgets = {
        row["course_key"]: row
        for row in ScenarioSectionBudget.objects.filter(
            scenario_id=generate()["scenario"]["id"]
        ).values("course_key", "course_code", "credit_hours", "meeting_pattern", "total_demand")
    }
    offerings = offerings_by_key(snapshot())
    shared = sorted(set(budgets) & set(offerings))
    assert set(EXPECTED) <= set(shared), "the fixture must reach every case in both engines"

    for course_key in shared:
        budget, offering = budgets[course_key], offerings[course_key]
        assert shape_for(budget["meeting_pattern"], budget["credit_hours"]) == shape_for(
            offering.meeting_pattern, offering.credit_hours
        ), course_key
        if offering.meeting_policy is None:
            assert offering.requirements == compile_requirements(
                offering.credit_hours,
                is_online=is_online_course(offering.course_code),
                meeting_pattern=budget["meeting_pattern"],
            ), course_key
    assert {k: offerings[k].meeting_pattern for k in EXPECTED} == EXPECTED
    assert offerings[key(ONLINE)].requirements == lectures(100, 2, DeliveryMode.ONLINE)
    assert offerings[key(ENGLISH)].meeting_policy is not None

    # The named divergences.
    assert budgets["IS232::SYSTEMS_ANALYSIS"]["meeting_pattern"] == ""
    assert "IS232" not in budgets and offerings["IS232"].meeting_pattern == ""
    assert "MATH101::CALCULUS_I" not in budgets
    assert offerings["MATH101::CALCULUS_I"].meeting_pattern == TWO_BY_100
    # IS234: one classic budget for both programmes' students, at 2 x 100 ...
    pooled = budgets[key(nameless_pair)]
    assert (pooled["meeting_pattern"], pooled["total_demand"]) == (TWO_BY_100, 4)
    assert "IS234" not in budgets
    # ... but two scheduler offerings, the nameless programme's at the default,
    # whether or not intake is handed Generate's values.
    handed = {k: row["meeting_pattern"] for k, row in budgets.items()}
    for snap_offerings in (offerings, offerings_by_key(snapshot(meeting_patterns=handed))):
        named, bare = snap_offerings[key(nameless_pair)], snap_offerings["IS234"]
        assert (named.programs, named.meeting_pattern) == ({"DS2"}, TWO_BY_100)
        assert named.requirements == lectures(100, 2)
        assert (bare.programs, bare.meeting_pattern) == ({"AI2"}, "")
        assert bare.requirements == lectures(75, 2)


def test_generates_values_reach_intake_and_win_over_the_live_curriculum(department):
    """The race is closed both ways: a vote saved while the job was queued
    changes nothing, because the board follows the budget Generate wrote."""
    offerings = offerings_by_key(
        snapshot(meeting_patterns={key(STAT): "", key(LA_I): TWO_BY_100, key(PROG_II): ""})
    )
    # The curriculum now agrees on 2 x 100 for STAT103; the budget said "".
    assert (offerings[key(STAT)].meeting_pattern, offerings[key(STAT)].requirements) == (
        "",
        lectures(75, 2),
    )
    # The curriculum is now mixed for LINEAR ALGEBRA I; the budget said 2 x 100.
    assert (offerings[key(LA_I)].meeting_pattern, offerings[key(LA_I)].requirements) == (
        TWO_BY_100,
        lectures(100, 2),
    )
    # A course the map does not hold is decided from the plan rows, as Generate would.
    assert offerings[key(PROG_I)].meeting_pattern == TWO_BY_100
    assert offerings[key(PROG_I)].requirements == lectures(100, 2)


def test_generates_values_never_override_a_bundle_an_unscheduled_course_or_the_credits(
    department,
):
    offerings = offerings_by_key(
        snapshot(
            meeting_patterns={
                key(ENGLISH): TWO_BY_100,
                key(PROJECT): TWO_BY_100,
                key(FOUR_CREDITS): TWO_BY_100,
            }
        )
    )
    english, project, four = (
        offerings[key(ENGLISH)],
        offerings[key(PROJECT)],
        offerings[key(FOUR_CREDITS)],
    )
    assert english.meeting_policy is not None and english.meeting_pattern == ""
    assert english.requirements != lectures(100, 2)
    assert (project.is_scheduled, project.requirements, project.meeting_pattern) == (
        False,
        (),
        "",
    )
    assert four.meeting_pattern == ""
    assert four.requirements == compile_requirements(4, is_online=False)


def test_an_unconfigured_snapshot_is_exactly_what_it_was(monkeypatch):
    """No vote anywhere: every offering keeps its credit default and records no
    shape. The recorded shape is not part of the fingerprint (the requirements
    it produced are), so votes that change no requirement change no
    fingerprint, and a shape that does apply does."""
    make_curriculum(votes=False)
    recommend_every_plan_course(monkeypatch, make_students())
    plain = snapshot()
    for offering in plain.offerings:
        assert offering.meeting_pattern == ""
        if offering.is_scheduled and offering.meeting_policy is None:
            assert offering.requirements == compile_requirements(
                offering.credit_hours, is_online=is_online_course(offering.course_code)
            ), offering.course_code
    fingerprint = plain.fingerprint()
    assert snapshot(meeting_patterns={}).fingerprint() == fingerprint
    assert (
        snapshot(meeting_patterns={k: "" for k in offerings_by_key(plain)}).fingerprint()
        == fingerprint
    )

    relabelled = replace(
        plain,
        offerings=tuple(replace(o, meeting_pattern=TWO_BY_100) for o in plain.offerings),
    )
    assert relabelled.fingerprint() == fingerprint

    for programme, (code, _name), _credits, vote in CURRICULUM:
        ProgrammeRequirement.objects.filter(program=programme, course_code=code).update(
            meeting_pattern=vote
        )
    assert snapshot(meeting_patterns={k: "" for k in offerings_by_key(plain)}).fingerprint() == (
        fingerprint
    )
    assert snapshot().fingerprint() != fingerprint


# ── the hand-over: view -> planner job -> bridge -> intake ───────────────────


def test_the_bridge_hands_generates_values_to_intake(monkeypatch):
    from scheduler import bridge

    scenario = TimetableScenario.objects.create(
        name="Weekly meetings hand-over", academic_year="1448", term="1", gender="M"
    )
    received = []

    class SnapshotReached(Exception):
        pass

    def capture(**kwargs):
        received.append(kwargs.get("meeting_patterns", "missing"))
        raise SnapshotReached

    monkeypatch.setattr(bridge, "build_snapshot", capture)
    values = {key(STAT): TWO_BY_100, key(LA_I): ""}
    for given in (values, None):
        extra = {} if given is None else {"meeting_patterns": given}
        with pytest.raises(SnapshotReached):
            bridge.build_into_scenario(
                scenario.pk, academic_year="1448", term=1, programs=["AI"], gender="M", **extra
            )
    assert received == [values, None]


def test_the_planner_job_hands_generates_values_to_the_bridge(monkeypatch):
    from core.services import planner_job_runner

    received = []

    def build(*_args, **kwargs):
        received.append(kwargs.get("meeting_patterns", "missing"))
        return {}

    monkeypatch.setattr("scheduler.bridge.build_into_scenario", build)
    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="Weekly meetings job"
    )
    values = {key(STAT): TWO_BY_100, key(LA_I): ""}
    for params in ({"meeting_patterns": values}, {}):
        job_id = planner_job_runner.submit_planner_job(
            scenario_id=scenario.id, mode=PlannerJob.MODE_SCHEDULER_BUILD, params=params
        )
        planner_job_runner.run_planner_job(job_id)
        assert PlannerJob.objects.get(pk=job_id).status == PlannerJob.STATUS_SUCCEEDED
    assert received == [values, None]


def test_generates_values_travel_from_the_generate_view_to_the_scheduler_build(
    department, monkeypatch
):
    """End to end: the scheduler build a Generate starts is handed exactly the
    weekly meetings Generate stored for every course, the default ("") too."""
    import json

    from django.contrib.auth.models import Group, User
    from django.test import Client

    from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups

    ensure_role_groups()
    user = User.objects.create_user(username="weekly-meetings-admin")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    http = Client()
    http.force_login(user)
    received = []

    def build(scenario_id, **kwargs):
        received.append((scenario_id, kwargs.get("meeting_patterns", "missing")))
        return {"engine": "scheduler"}

    monkeypatch.setattr("scheduler.bridge.build_into_scenario", build)
    monkeypatch.setattr("core.services.planner_job_runner.is_async_planner_enabled", lambda: False)
    response = http.post(
        "/ops/tw/generate-workspace/",
        content_type="application/json",
        data=json.dumps(
            {
                "year": YEAR,
                "semester": TERM,
                "program": ",".join(PROGRAMMES),
                "section": "M",
                "engine": "scheduler",
            }
        ),
    )
    assert response.status_code == 201, response.content
    [(scenario_id, handed)] = received
    stored = ScenarioSectionBudget.objects.filter(scenario_id=scenario_id)
    assert handed == dict(stored.values_list("course_key", "meeting_pattern"))
    assert {k: handed[k] for k in EXPECTED} == EXPECTED

"""A registered course outside the plan fills the elective row it is taken against.

The graduation forecast used to take a plan's elective rows at face value. A student
registered in AI463 for the row AI1, or in a general-studies course for a Free
Elective row, still had the row ``not_taken``, so the forecast scheduled it again:
one course too many, and on the local data of 2026-11-25 a longer estimate for 798
of 3,926 students.

The first half pins the rule (``elective_row_filling``): which row, on what
evidence. The second half pins what the forecast does with it, on a zero-slack plan
like the six real second-cohort ones, where one hour too many or too few in the
ledger moves the co-op.
"""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from core.models import (
    Course,
    ElectiveCourse,
    ElectiveTermMapping,
    Prerequisite,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
)
from core.services.elective_row_filling import (
    BASIS_CATALOGUE,
    BASIS_HOURS,
    BASIS_MAPPING,
    elective_rows_filled,
    is_elective_row,
)
from core.services.planner_graduation import compare_planner_graduation
from core.services.student_graduation import (
    RECOMMENDED_CURRENT_TERM,
    REGISTERED_TIMETABLE,
    build_graduation_report,
    build_graduation_what_if,
)

pytestmark = pytest.mark.django_db

PROG = "ERF"
YEAR, TERM = 1448, 1
# The id's first two digits are the join year (1446), so 1448/1 is plan term 5.
SID = 4_690_201
PE, FREE, UNIVERSITY, MANDATORY = (
    "Program Elective",
    "Free Elective",
    "University Elective",
    "Mandatory",
)


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def _rows(*rows: tuple[str, int, int, str]) -> dict[str, dict]:
    """Plan rows as the forecast holds them: code -> credits, term, type."""
    return {
        code: {"credits": hours, "term": term, "type": kind} for code, hours, term, kind in rows
    }


PLAN = _rows(
    ("ER201", 3, 5, MANDATORY),
    ("ER301", 3, 6, MANDATORY),
    ("ERFE1", 2, 6, FREE),
    ("ERGSE1", 2, 7, UNIVERSITY),
    ("ER1", 3, 8, PE),
    ("ER2", 3, 9, PE),
)


def _catalogue(*codes: str, programme: str = PROG) -> dict[str, ElectiveCourse]:
    return {
        code: ElectiveCourse.objects.create(
            programme=programme, course_code=code, course_name=f"Elective {code}", credit_hours=3
        )
        for code in codes
    }


def _map(slot: str, elective: ElectiveCourse, *, programme: str = PROG) -> None:
    ElectiveTermMapping.objects.create(
        academic_year=str(YEAR),
        term=TERM,
        programme=programme,
        placeholder_code=slot,
        elective=elective,
    )


def _filled(current: dict[str, int], *, passed: set[str] | None = None, program: str = PROG):
    filling = elective_rows_filled(
        program, YEAR, TERM, current=current, plan_rows=PLAN, passed=passed or set()
    )
    return [(row.course_code, row.row_code, row.basis) for row in filling.filled]


def test_a_term_all_in_the_plan_fills_nothing_and_reads_nothing() -> None:
    with CaptureQueriesContext(connection) as queries:
        assert _filled({"ER201": 3, "ER301": 3}) == []

    assert len(queries) == 0


def test_the_departments_mapping_names_the_row() -> None:
    """Proof, as the resolver takes it: even past an earlier row, even with no hours."""
    catalogue = _catalogue("ER463")
    _map("ER2", catalogue["ER463"])

    assert _filled({"ER201": 3, "ER463": 3}) == [("ER463", "ER2", BASIS_MAPPING)]
    assert _filled({"ER201": 3, "ER463": 0}) == [("ER463", "ER2", BASIS_MAPPING)]


def test_the_mapping_is_honoured_before_any_weaker_evidence_takes_its_row() -> None:
    """AAA300 comes first in every list and has only its hours; ER1 is still ER463's."""
    catalogue = _catalogue("ER463")
    _map("ER1", catalogue["ER463"])

    assert _filled({"AAA300": 3, "ER463": 3}) == [
        ("AAA300", "ER2", BASIS_HOURS),
        ("ER463", "ER1", BASIS_MAPPING),
    ]


def test_a_programme_elective_gets_its_row_before_a_course_with_only_its_hours() -> None:
    """One Program Elective row left: the catalogue elective takes it, not AAA300."""
    _catalogue("ER463")

    assert _filled({"AAA300": 3, "ER463": 3}, passed={"ER2"}) == [("ER463", "ER1", BASIS_CATALOGUE)]


def test_two_courses_mapped_to_one_row_take_one_row_each() -> None:
    catalogue = _catalogue("ER463", "ER481")
    _map("ER1", catalogue["ER463"])
    _map("ER1", catalogue["ER481"])

    assert _filled({"ER463": 3, "ER481": 3}) == [
        ("ER463", "ER1", BASIS_MAPPING),
        ("ER481", "ER2", BASIS_CATALOGUE),
    ]


def test_a_programme_elective_takes_the_earliest_program_elective_row() -> None:
    _catalogue("ER463")

    assert _filled({"ER201": 3, "ER463": 3}) == [("ER463", "ER1", BASIS_CATALOGUE)]


def test_a_mapped_course_whose_row_is_done_takes_the_next_program_elective_row() -> None:
    """The mapping names ER1 only, and ER1 is passed: the course is still an elective."""
    catalogue = _catalogue("ER463")
    _map("ER1", catalogue["ER463"])

    assert _filled({"ER463": 3}, passed={"ER1"}) == [("ER463", "ER2", BASIS_CATALOGUE)]


def test_any_other_course_takes_the_earliest_row_the_registrar_resolves() -> None:
    """A general-studies course: a Free or University Elective row of its hours."""
    assert _filled({"ER201": 3, "GS303": 2}) == [("GS303", "ERFE1", BASIS_HOURS)]
    assert _filled({"GS303": 2}, passed={"ERFE1"}) == [("GS303", "ERGSE1", BASIS_HOURS)]


def test_the_kind_of_row_comes_before_its_due_term() -> None:
    """Same hours everywhere: the catalogue elective skips the earlier free row."""
    plan = _rows(("FREE3", 3, 5, FREE), ("ER1", 3, 8, PE), ("FREE3B", 3, 9, FREE))
    _catalogue("ER463")

    def filled(course: str) -> list[tuple[str, str]]:
        filling = elective_rows_filled(
            PROG, YEAR, TERM, current={course: 3}, plan_rows=plan, passed=set()
        )
        return [(row.row_code, row.basis) for row in filling.filled]

    assert filled("ER463") == [("ER1", BASIS_CATALOGUE)]
    assert filled("ED315") == [("FREE3", BASIS_HOURS)]


def test_a_course_with_no_row_of_its_own_kind_falls_back_to_any_elective_row() -> None:
    """IS and COE electives are in no catalogue today: 3 hours, only a Program row open."""
    assert _filled({"ER485": 3}) == [("ER485", "ER1", BASIS_HOURS)]

    _catalogue("ER463")
    assert _filled({"ER463": 3}, passed={"ER1", "ER2"}) == []
    plan = _rows(("FREE3", 3, 6, FREE))
    filling = elective_rows_filled(
        PROG, YEAR, TERM, current={"ER463": 3}, plan_rows=plan, passed=set()
    )
    assert [(row.row_code, row.basis) for row in filling.filled] == [("FREE3", BASIS_HOURS)]


def test_what_is_never_filled() -> None:
    # A Mandatory row, whatever its hours.
    assert _filled({"XX900": 3}, passed={"ER1", "ER2"}) == []
    # A row of other hours.
    assert _filled({"XX901": 4}) == []
    # A course whose hours no source knows, with no mapping to prove its row.
    # Not even a row that records no hours either: unknown is not "the same".
    assert _filled({"GS303": 0}) == []
    unknown = elective_rows_filled(
        PROG,
        YEAR,
        TERM,
        current={"GS303": 0},
        plan_rows=_rows(("ERZ1", 0, 6, FREE)),
        passed=set(),
    )
    assert unknown.filled == ()
    # A row that is passed, or that is itself among this term's courses.
    assert _filled({"GS303": 2}, passed={"ERFE1", "ERGSE1"}) == []
    assert _filled({"GS303": 2, "ERFE1": 2, "ERGSE1": 2}) == []


def test_one_course_for_one_row() -> None:
    """Three 2-hour courses, two 2-hour rows: the third fills nothing."""
    assert _filled({"GS303": 2, "GS243": 2, "GS311": 2}) == [
        ("GS243", "ERFE1", BASIS_HOURS),
        ("GS303", "ERGSE1", BASIS_HOURS),
    ]


def test_a_row_the_resolver_marked_is_spoken_for_by_its_course() -> None:
    """ER1 is listed with 0 hours: ER463 carries them. ER463 must not fill ER2 as well."""
    catalogue = _catalogue("ER463", "ER481")
    _map("ER1", catalogue["ER463"])

    assert _filled({"ER1": 0, "ER463": 3}) == []
    # A second elective the same term goes to the next row.
    assert _filled({"ER1": 0, "ER463": 3, "ER481": 3}) == [("ER481", "ER2", BASIS_CATALOGUE)]
    # With no mapping left to say which course it was, the hours decide.
    ElectiveTermMapping.objects.all().delete()
    assert _filled({"ER1": 0, "ER463": 3}) == []


def test_a_row_registered_under_its_own_code_speaks_for_no_course() -> None:
    """ERFE1 with hours of its own is a registration; GS303 is another one."""
    assert _filled({"ERFE1": 2, "GS303": 2}) == [("GS303", "ERGSE1", BASIS_HOURS)]


def test_a_marked_row_that_no_course_carries_any_longer_is_unbacked() -> None:
    """Listed with 0 hours because a course carried it; a what-if dropped the course."""
    catalogue = _catalogue("ER463")
    _map("ER1", catalogue["ER463"])

    def unbacked(current: dict[str, int]) -> frozenset[str]:
        return elective_rows_filled(
            PROG, YEAR, TERM, current=current, plan_rows=PLAN, passed=set()
        ).unbacked

    assert unbacked({"ER201": 3, "ER1": 0, "ER463": 3}) == frozenset()
    assert unbacked({"ER201": 3, "ER1": 0}) == {"ER1"}
    # A course of other hours that the mapping does not name is not its carrier,
    # and goes on to fill a row of its own.
    assert unbacked({"ER1": 0, "GS303": 2}) == {"ER1"}
    assert _filled({"ER1": 0, "GS303": 2}) == [("GS303", "ERFE1", BASIS_HOURS)]
    # A row with hours of its own is a registration, whatever else is listed.
    assert unbacked({"ERFE1": 2}) == frozenset()
    # A named course key is still the course the mapping names.
    assert unbacked({"ER1": 0, "ER463::DATA_MINING": 3}) == frozenset()


def test_the_mapping_decides_which_of_two_marked_rows_a_course_carries() -> None:
    """ER1 is due first, but the mapping names ER463 for ER2: it is ER1 that is left."""
    catalogue = _catalogue("ER463", "ER481")
    _map("ER2", catalogue["ER463"])

    def filling(current: dict[str, int]):
        return elective_rows_filled(PROG, YEAR, TERM, current=current, plan_rows=PLAN, passed=set())

    both = filling({"ER1": 0, "ER2": 0, "ER463": 3, "ER481": 3})
    assert (both.filled, both.unbacked) == ((), frozenset())
    one = filling({"ER1": 0, "ER2": 0, "ER463": 3, "GS303": 2})
    assert one.unbacked == {"ER1"}
    assert [(row.course_code, row.row_code) for row in one.filled] == [("GS303", "ERFE1")]


def test_a_plan_row_that_carries_no_hours_is_not_a_marked_slot() -> None:
    """0 is that row's own figure. It is held, not left behind."""
    filling = elective_rows_filled(
        PROG,
        YEAR,
        TERM,
        current={"ERZ1": 0, "ER201": 3},
        plan_rows={**PLAN, **_rows(("ERZ1", 0, 6, FREE))},
        passed=set(),
    )

    assert filling.unbacked == frozenset()


def test_a_second_cohort_plan_reads_its_base_programmes_mapping_and_catalogue() -> None:
    catalogue = _catalogue("ER463", "ER481")
    _map("ER2", catalogue["ER463"])

    assert _filled({"ER463": 3, "ER481": 3}, program="ERF2") == [
        ("ER463", "ER2", BASIS_MAPPING),
        ("ER481", "ER1", BASIS_CATALOGUE),
    ]


def test_a_named_course_key_is_read_by_its_code() -> None:
    """CODE::NAME is the course; the plan, mapping and catalogue know the code."""
    _catalogue("ER463")

    assert _filled({"ER463::DATA_MINING": 3}) == [("ER463::DATA_MINING", "ER1", BASIS_CATALOGUE)]
    # A plan course under a named key is not a course outside the plan.
    assert _filled({"ER201::DATA_STRUCTURES": 3}) == []


def test_which_rows_are_elective_rows() -> None:
    assert all(map(is_elective_row, (PE, FREE, UNIVERSITY, "Elective", "programme elective")))
    assert not any(map(is_elective_row, (MANDATORY, "", None)))


# ---------------------------------------------------------------------------
# The forecast
# ---------------------------------------------------------------------------

PLAN_TOTAL = 24


def _student() -> None:
    Student.objects.create(
        student_id=SID,
        registration_no=str(SID),
        name="Sample Student",
        program=PROG,
        section="M",
        status="ACTIVE",
    )


def _plan(code: str, name: str, hours: int, term: int, kind: str = MANDATORY) -> None:
    ProgrammeRequirement.objects.create(
        program=PROG,
        course_code=code,
        course_name=name,
        programme_term=term,
        credit_hours=hours,
        type=kind,
    )


def _register(code: str, name: str) -> None:
    section = TermSection.objects.create(
        course_key=code, course_code=code, course_number="", course_name=name, section="M1"
    )
    TermSectionMeeting.objects.create(
        term_section=section, day="SUN", start_time="09:00", end_time="10:15"
    )
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year=str(YEAR),
        term=str(TERM),
        term_section=section,
        source="scraper_timetable",
    )


def _zero_slack_term() -> None:
    """Five plan courses (13 hours) and two courses outside the plan (3 and 2).

    Left in the plan: the two elective rows (3 and 2) and the co-op (6), which
    needs 18 hours, every hour that comes before it. The plan totals 24. All
    three sit at plan term 6, so only the hour gate decides where the co-op goes.
    """
    _student()
    for code, hours in [("ER201", 3), ("ER202", 3), ("ER203", 3), ("ER204", 2), ("ER205", 2)]:
        _plan(code, f"Course {code}", hours, 5)
        _register(code, f"Course {code}")
    _plan("ER1", "Program Elective I", 3, 6, PE)
    _plan("ERFE1", "Free Elective I", 2, 6, FREE)
    _plan("ER490", "Cooperative Training", 6, 6)
    Prerequisite.objects.create(
        program=PROG, course_code="ER490", prerequisite_course_code="18(HOURS)"
    )
    ElectiveCourse.objects.create(
        programme=PROG, course_code="ER463", course_name="Data Mining", credit_hours=3
    )
    Course.objects.create(course_code="GS303", description="Communication Skills", credit_hours=2)
    _register("ER463", "Data Mining")
    _register("GS303", "Communication Skills")


def _report(**kwargs) -> dict:
    return build_graduation_report(
        SID, YEAR, TERM, planning_baseline_kind=REGISTERED_TIMETABLE, **kwargs
    )


def _planned(report: dict) -> dict[str, int]:
    return {code: term["sequence"] for term in report["term_plan"] for code in term["course_codes"]}


def _end_total(report: dict) -> int:
    return report["scenario_credit_seed"] + sum(term["credits"] for term in report["term_plan"])


def test_the_forecast_counts_the_rows_this_term_fills() -> None:
    """Both elective rows are in progress, so the co-op is next term.

    The ledger takes them at the plan's hours (13 + 3 + 2), which is what meets
    the co-op's gate, and it still closes on the plan total. Before, both rows were
    scheduled again and the co-op came in the third term.
    """
    _zero_slack_term()

    report = _report()

    assert report["baseline_elective_rows_filled"] == [
        {"course_code": "ER463", "row_code": "ER1", "basis": BASIS_CATALOGUE},
        {"course_code": "GS303", "row_code": "ERFE1", "basis": BASIS_HOURS},
    ]
    assert report["registered_credits_at_planning_baseline"] == 18
    assert report["scenario_credit_seed_registrar"] == 18
    assert report["simulation_completed"] is True
    assert _planned(report) == {"ER490": 1}
    assert report["estimated_additional_terms"] == 1
    assert _end_total(report) == PLAN_TOTAL


def test_a_filled_row_is_marked_as_this_forecasts_assumption() -> None:
    """In progress on the forecast's word, not the registrar's: every reader can tell."""
    _zero_slack_term()

    report = _report()

    marks = {
        row["code"]: (row.get("filled_by"), row.get("fill_basis")) for row in report["in_progress"]
    }
    assert marks["ER1"] == ("ER463", BASIS_CATALOGUE)
    assert marks["ERFE1"] == ("GS303", BASIS_HOURS)
    # A plan course of the term is in progress on the registrar's word.
    assert marks["ER201"] == (None, None)
    assert report["counts"]["studying"] == len(report["in_progress"]) == 7
    stated = [line for line in report["simulation_assumptions"] if "outside the plan" in line]
    assert len(stated) == 1
    assert "ER463 for ER1" in stated[0] and "GS303 for ERFE1" in stated[0]


def test_the_real_courses_stay_in_the_list_and_the_rows_stay_requirements() -> None:
    """Nothing is renamed: the list shows what is registered, the plan what it needs."""
    _zero_slack_term()

    report = _report()

    listed = {course["code"] for course in report["planning_baseline_courses_assumed_passed"]}
    assert {"ER463", "GS303"} <= listed
    assert not {"ER1", "ERFE1"} & listed
    # In progress, not passed: the plan's own counts do not move.
    assert report["plan_courses_passed"] == 0
    assert report["plan_courses_total"] == 8


def test_a_course_whose_hours_nothing_knows_fills_no_row_and_the_ledger_still_closes() -> None:
    """Then the rows are scheduled, as before, and bring the hours themselves."""
    _zero_slack_term()
    ElectiveCourse.objects.update(credit_hours=0)
    Course.objects.update(credit_hours=0)

    report = _report()

    assert report["baseline_elective_rows_filled"] == []
    assert report["scenario_credit_seed_registrar"] == 13
    assert _planned(report) == {"ER1": 1, "ERFE1": 1, "ER490": 3}
    assert _end_total(report) == PLAN_TOTAL
    assert not any("outside the plan" in line for line in report["simulation_assumptions"])


def test_a_course_no_row_can_take_is_load_and_never_ledger() -> None:
    """A 4-hour course outside the plan, and no 4-hour elective row: load 22, ledger 18.

    Its own hours go nowhere but the load. In the ledger they would be 22 against
    a plan of 24 with 6 still to come.
    """
    _zero_slack_term()
    Course.objects.create(course_code="XX444", description="Elsewhere", credit_hours=4)
    _register("XX444", "Elsewhere")

    report = _report()

    assert [row["course_code"] for row in report["baseline_elective_rows_filled"]] == [
        "ER463",
        "GS303",
    ]
    assert report["registered_credits_at_planning_baseline"] == 22
    assert report["scenario_credit_seed_registrar"] == 18
    assert _end_total(report) == PLAN_TOTAL


def test_the_term_that_fills_the_last_rows_completes_the_plan() -> None:
    """The same student without the co-op: nothing is left after this term."""
    _zero_slack_term()
    Prerequisite.objects.all().delete()
    ProgrammeRequirement.objects.filter(course_code="ER490").delete()

    report = _report()

    assert report["term_plan"] == []
    assert report["estimated_additional_terms"] == 0
    assert report["plan_completion_in_planning_baseline_possible"] is True
    # The floor counts what is left AFTER this term, and the rows are this term.
    assert report["lower_bound_additional_terms"] == 0


def test_the_planner_forecast_fills_the_same_rows() -> None:
    _zero_slack_term()

    planner = compare_planner_graduation(student_id=SID, year=YEAR, term=TERM, options=[])

    assert planner["baseline"]["status"] == "complete"
    assert (
        planner["baseline"]["terms_including_planning_term"]
        == _report()["estimated_terms_including_planning_baseline"]
        == 2
    )


def _resolved_slot_term() -> None:
    """ER463 is registered, the mapping names it for ER1, and the resolver marked ER1."""
    _student()
    _plan("ER201", "Data Structures", 3, 5)
    _plan("ER1", "Program Elective I", 3, 5, PE)
    elective = ElectiveCourse.objects.create(
        programme=PROG, course_code="ER463", course_name="Data Mining", credit_hours=3
    )
    _map("ER1", elective)
    slot = Course.objects.create(
        course_code="ER1", description="Program Elective I", credit_hours=3
    )
    StudentCourse.objects.create(student_id=SID, course=slot, status="studying")
    _register("ER201", "Data Structures")
    _register("ER463", "Data Mining")


def test_the_planner_forecast_fills_a_row_the_resolver_already_marked() -> None:
    """The planner leaves persisted studying rows out; the mapping still proves the row.

    The adviser's report knows ER1 is in progress because the resolver marked it.
    The planner forecast is built from the timetable alone, so it used to schedule
    ER1 again and say one term more.
    """
    _resolved_slot_term()

    adviser = _report()
    planner = compare_planner_graduation(student_id=SID, year=YEAR, term=TERM, options=[])

    # The resolver's mark is the adviser's evidence: nothing is filled a second time.
    assert adviser["baseline_elective_rows_filled"] == []
    assert adviser["scenario_credit_seed_registrar"] == 6
    assert adviser["term_plan"] == []
    assert planner["baseline"]["terms_including_planning_term"] == 1


def test_dropping_the_course_reopens_its_row() -> None:
    """A what-if that removes GS303 has ERFE1 to take again, and the co-op waits for it."""
    _zero_slack_term()

    result = build_graduation_what_if(
        SID,
        YEAR,
        TERM,
        planning_baseline_kind=REGISTERED_TIMETABLE,
        remove_current_courses=["GS303"],
    )

    what_if = result["what_if"]
    assert what_if["valid"] is True
    assert what_if["baseline"]["estimated_additional_terms"] == 1
    # ERFE1 in the first term, then the co-op in the next term of its parity.
    assert what_if["scenario"]["estimated_additional_terms"] == 3
    assert result["baseline_elective_rows_filled"] == [
        {"course_code": "ER463", "row_code": "ER1", "basis": BASIS_CATALOGUE}
    ]


def test_dropping_the_course_behind_a_resolved_slot_reopens_the_slot() -> None:
    """The stored mark says ER1 is studying; without ER463 nothing is studying it."""
    _resolved_slot_term()

    result = build_graduation_what_if(
        SID,
        YEAR,
        TERM,
        planning_baseline_kind=REGISTERED_TIMETABLE,
        remove_current_courses=["ER463"],
    )

    what_if = result["what_if"]
    assert what_if["valid"] is True
    assert what_if["baseline"]["estimated_additional_terms"] == 0
    assert what_if["scenario"]["estimated_additional_terms"] > 0
    assert "ER1" in {code for term in result["term_plan"] for code in term["course_codes"]}
    listed = {course["code"] for course in result["planning_baseline_courses_assumed_passed"]}
    assert listed == {"ER201"}
    # Not scheduled for later and in progress now at once.
    assert "ER1" not in {row["code"] for row in result["in_progress"]}


def test_a_filled_row_is_in_progress_even_when_its_stored_mark_is_set_aside() -> None:
    """The planner forecast passes the timetable and sets the stored studying rows aside.

    ER1 is among them. ER463 still fills it, so it is in progress in every list of
    the report, not only in the scenario.
    """
    _resolved_slot_term()

    report = _report(
        _current_courses_override=[
            {"code": "ER201", "name": "Data Structures", "credits": 3, "section": ""},
            {"code": "ER463", "name": "Data Mining", "credits": 3, "section": ""},
        ],
        _excluded_studying_codes={"ER1"},
    )

    assert report["baseline_elective_rows_filled"] == [
        {"course_code": "ER463", "row_code": "ER1", "basis": BASIS_MAPPING}
    ]
    assert "ER1" in {row["code"] for row in report["in_progress"]}
    assert report["term_plan"] == []


def test_the_replacement_search_does_not_offer_a_row_that_is_being_filled() -> None:
    """ERFE1 is a course on file and, without GS303, an open one. With GS303 it is taken."""
    _zero_slack_term()
    Course.objects.create(course_code="ERFE1", description="Free Elective I", credit_hours=2)

    result = build_graduation_what_if(
        SID,
        YEAR,
        TERM,
        planning_baseline_kind=REGISTERED_TIMETABLE,
        search_better_replacements=True,
    )

    assert result["what_if"]["mode"] == "replacement_search"
    assert "ERFE1" not in result["what_if"]["candidate_courses_considered"]


def test_the_recommended_baseline_fills_nothing() -> None:
    """It is made of plan rows only; the timetable is not part of it."""
    _zero_slack_term()

    report = build_graduation_report(
        SID, YEAR, TERM, planning_baseline_kind=RECOMMENDED_CURRENT_TERM
    )

    assert report["baseline_elective_rows_filled"] == []

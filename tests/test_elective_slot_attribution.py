"""An elective fills the slot the student was given it for.

The department maps real courses onto a plan's elective slots, and one course
may be offered for two of them: in 1448 term 2 DS482 fills both DS1 (term 8)
and DS3 (term 10). The generator used to expand placeholders first and work out
which slot each course filled afterwards, which lost two things — the slot a
student still owed, and the fact that one course can belong to two terms. A
student who had passed DS1 and owed only DS3 had their course booked against
DS1, and DS482 was budgeted at one arbitrary term while the other term's board
got nothing.

These tests pin the resolver's own behaviour, then prove the term attribution
end to end through ``generate_workspace_scenario``.
"""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

import pytest

from core.models import (
    BoardStudentLink,
    Course,
    ElectiveCourse,
    ElectiveTermMapping,
    ProgrammeRequirement,
    ScenarioSectionBudget,
    Student,
    StudentCourse,
)
from core.services.timetable_generate import (
    _resolve_elective_slots,
    _summarise_unserved_slots,
    generate_workspace_scenario,
)


def ec(code: str, prerequisites: str = "") -> SimpleNamespace:
    """A stand-in for an ElectiveCourse row; the resolver reads two fields."""
    return SimpleNamespace(course_code=code, prerequisites_csv=prerequisites)


DS_ONLY = {("DS", "DS1"): [ec("DS482")], ("DS", "DS3"): [ec("DS482")]}


def ds(_sid: int) -> str:
    return "DS"


# ── the resolver ────────────────────────────────────────────────────────


def test_slot_the_student_owes_is_the_slot_recorded() -> None:
    """ES1: a student owing only DS3 is recorded against DS3, not DS1.

    The live defect: DS482 fills DS1 and DS3, and the old code credited it to
    whichever slot sorted first, so a term-10 student's elective counted at
    term 8 and sized itself from DS1's declared capacity.
    """
    recs, picks, unserved = _resolve_elective_slots({2: ["DS451", "DS3"]}, DS_ONLY, ds, {}, {})

    assert picks == [(2, "DS3", "DS482")]
    assert unserved == []
    assert recs[2] == ["DS451", "DS482"]


def test_one_course_cannot_fill_two_slots_for_one_student() -> None:
    """ES2: owing DS1 and DS3 with only DS482 on offer fills one and reports one.

    Nobody clears two requirements by sitting one class, so the second slot
    cannot be served. It must be reported, not dropped.
    """
    recs, picks, unserved = _resolve_elective_slots(
        {1: ["DS451", "DS1", "DS3"]}, DS_ONLY, ds, {}, {}
    )

    assert picks == [(1, "DS1", "DS482")]
    assert unserved == [(1, "DS3", "courses_exhausted")]
    assert recs[1].count("DS482") == 1


def test_a_second_mapped_course_serves_the_second_slot() -> None:
    """ES3: with another course on DS3, both slots are filled by distinct courses."""
    elective_map = {
        ("DS", "DS1"): [ec("DS482")],
        ("DS", "DS3"): [ec("DS482"), ec("DS471")],
    }

    recs, picks, unserved = _resolve_elective_slots(
        {3: ["DS451", "DS1", "DS3"]}, elective_map, ds, {}, {}
    )

    assert unserved == []
    assert sorted(picks) == [(3, "DS1", "DS482"), (3, "DS3", "DS471")]
    # Two owed slots means two courses, so demand counts twice.
    assert sorted(recs[3]) == ["DS451", "DS471", "DS482"]


def test_demand_is_one_course_per_owed_slot() -> None:
    """ES4: the recs a student ends with carry one course per filled slot."""
    elective_map = {
        ("DS", "DS1"): [ec("A1"), ec("A2")],
        ("DS", "DS2"): [ec("A1"), ec("A2")],
        ("DS", "DS3"): [ec("A1"), ec("A2")],
    }

    recs, picks, unserved = _resolve_elective_slots(
        {4: ["DS1", "DS2", "DS3"]}, elective_map, ds, {}, {}
    )

    # Three slots, two courses: two slots are served with a course each and the
    # third is reported. Which slot is left is pinned — serving DS3 and dropping
    # DS1 would fail — but which course landed on which of the two served slots
    # is an artefact of the matching and deliberately not a contract.
    assert sorted(slot for _sid, slot, _course in picks) == ["DS1", "DS2"]
    assert sorted(course for _sid, _slot, course in picks) == ["A1", "A2"]
    assert unserved == [(4, "DS3", "courses_exhausted")]
    assert sorted(recs[4]) == ["A1", "A2"]


def test_a_slot_is_not_stranded_when_another_assignment_would_serve_it() -> None:
    """ES4b: DS1 offers A and B, DS3 only A. Both slots must be filled.

    Taking each slot's best course in turn hands A to DS1 and leaves DS3 with
    nothing, which would report a mapping gap the department cannot act on —
    DS1->B, DS3->A serves both.
    """
    elective_map = {("DS", "DS1"): [ec("A"), ec("B")], ("DS", "DS3"): [ec("A")]}

    recs, picks, unserved = _resolve_elective_slots({41: ["DS1", "DS3"]}, elective_map, ds, {}, {})

    assert unserved == [], "a valid assignment exists, so nothing is unserved"
    assert sorted(picks) == [(41, "DS1", "B"), (41, "DS3", "A")]
    assert sorted(recs[41]) == ["A", "B"]


def test_a_course_already_passed_cannot_fill_a_slot() -> None:
    """ES4c: a course the student has passed is not offered again.

    They would be sitting the same class twice, and the slot is genuinely
    unfillable for them — which is what the report must say.
    """
    elective_map = {("DS", "DS3"): [ec("DS482")]}

    recs, picks, unserved = _resolve_elective_slots(
        {42: ["DS3"]}, elective_map, ds, {42: {"DS482"}}, {}
    )

    assert picks == []
    assert unserved == [(42, "DS3", "no_eligible_course")]
    assert recs[42] == []


def test_a_course_being_studied_now_cannot_fill_a_slot() -> None:
    """ES4d: the same holds for a course they are sitting this term.

    The course being studied is the one that would otherwise be picked (equal
    use, and first alphabetically), so skipping the exclusion changes the
    answer rather than landing on it by luck.
    """
    elective_map = {("DS", "DS3"): [ec("DS410"), ec("DS471")]}

    _recs, picks, unserved = _resolve_elective_slots(
        {43: ["DS3"]}, elective_map, ds, {}, {43: {"DS410"}}
    )

    assert picks == [(43, "DS3", "DS471")]
    assert unserved == []


def test_the_two_unserved_reasons_are_told_apart() -> None:
    """ES4e: "not ready" and "no course left" are different rows.

    They ask different things of the reader: only one of them is the
    department's to fix.
    """
    elective_map = {
        ("DS", "DS1"): [ec("A")],
        ("DS", "DS2"): [ec("A")],
        ("DS", "DS3"): [ec("Z", "CS999")],
    }

    _recs, _picks, unserved = _resolve_elective_slots(
        {44: ["DS1", "DS2", "DS3"]}, elective_map, ds, {}, {}
    )

    assert sorted(unserved) == [
        (44, "DS2", "courses_exhausted"),
        (44, "DS3", "no_eligible_course"),
    ]


def test_the_same_slot_code_in_two_plans_stays_separate() -> None:
    """ES5: AI3 in the AI plan and AI3 in AI2 are different slots.

    The map used to be keyed by the bare placeholder code, so whichever plan
    loaded first decided the course list — and the term — for every other plan.
    """
    elective_map = {("AI", "AI3"): [ec("AI462")], ("AI2", "AI3"): [ec("AI455")]}

    def programme_of(sid: int) -> str:
        return "AI" if sid == 4 else "AI2"

    recs, picks, _unserved = _resolve_elective_slots(
        {4: ["AI3"], 5: ["AI3"]}, elective_map, programme_of, {}, {}
    )

    assert recs[4] == ["AI462"]
    assert recs[5] == ["AI455"]
    assert sorted(picks) == [(4, "AI3", "AI462"), (5, "AI3", "AI455")]


def test_a_programme_with_no_mapped_slots_is_untouched() -> None:
    """ES6: a student whose plan maps nothing keeps their recommendations."""
    recs, picks, unserved = _resolve_elective_slots(
        {6: ["DS451", "DS3"]}, {("AI", "AI3"): [ec("AI462")]}, ds, {}, {}
    )

    assert recs[6] == ["DS451", "DS3"]
    assert picks == []
    assert unserved == []


def test_prerequisites_still_gate_a_course() -> None:
    """ES7: a course whose prerequisites are unmet is not offered for the slot."""
    elective_map = {("DS", "DS3"): [ec("DS482", "CS372")]}

    recs, picks, unserved = _resolve_elective_slots({7: ["DS3"]}, elective_map, ds, {}, {})
    assert picks == []
    assert unserved == [(7, "DS3", "no_eligible_course")]
    assert recs[7] == []

    recs, picks, unserved = _resolve_elective_slots(
        {8: ["DS3"]}, elective_map, ds, {8: {"CS372"}}, {}
    )
    assert picks == [(8, "DS3", "DS482")]
    assert recs[8] == ["DS482"]


def test_a_course_being_studied_now_satisfies_a_prerequisite() -> None:
    """ES8: ``studying`` counts as well as ``passed``, as it did before."""
    elective_map = {("DS", "DS3"): [ec("DS482", "CS372")]}

    _recs, picks, _unserved = _resolve_elective_slots(
        {9: ["DS3"]}, elective_map, ds, {}, {9: {"CS372"}}
    )

    assert picks == [(9, "DS3", "DS482")]


def test_students_are_spread_across_a_slots_courses() -> None:
    """ES9: demand does not pile onto whichever course sorts first."""
    elective_map = {("DS", "DS3"): [ec("DSA"), ec("DSB")]}

    _recs, picks, _unserved = _resolve_elective_slots(
        {sid: ["DS3"] for sid in range(10, 16)}, elective_map, ds, {}, {}
    )

    assert Counter(course for _sid, _slot, course in picks) == {"DSA": 3, "DSB": 3}


def test_resolution_is_stable_across_runs() -> None:
    """ES10: the same data resolves to the same courses, so a rebuild matches."""
    elective_map = {("DS", "DS1"): [ec("DSA"), ec("DSB")], ("DS", "DS3"): [ec("DSA"), ec("DSB")]}
    recs = {sid: ["DS1", "DS3"] for sid in range(20, 25)}

    first = _resolve_elective_slots({k: list(v) for k, v in recs.items()}, elective_map, ds, {}, {})
    second = _resolve_elective_slots(
        {k: list(v) for k, v in recs.items()}, elective_map, ds, {}, {}
    )

    assert sorted(first[1]) == sorted(second[1])
    assert first[2] == second[2]


def test_an_empty_map_returns_the_recommendations_unchanged() -> None:
    """ES11: no mappings for the term means nothing is resolved or reported."""
    recs, picks, unserved = _resolve_elective_slots({30: ["DS451", "DS3"]}, {}, ds, {}, {})

    assert recs == {30: ["DS451", "DS3"]}
    assert picks == []
    assert unserved == []


def test_another_plans_slot_code_is_not_a_slot_here() -> None:
    """ES11b: a code that is a slot in the AI plan only is left alone for a DS
    student, who may have a real course of that name.

    The slot list has to be scoped per programme, not pooled across every
    programme in the scenario.
    """
    # Both plans are in the scenario, each with its own slot. The DS student
    # must own DS3 and not AI3, so pooling the two plans' slots is visible.
    elective_map = {("AI", "AI3"): [ec("AI462")], ("DS", "DS3"): [ec("DS482")]}

    recs, picks, unserved = _resolve_elective_slots({40: ["AI3", "DS3"]}, elective_map, ds, {}, {})

    assert recs[40] == ["AI3", "DS482"], "AI3 is not this student's slot and must be left alone"
    assert picks == [(40, "DS3", "DS482")]
    assert unserved == []


def test_a_slot_listed_twice_in_the_recs_is_owed_once() -> None:
    """ES12: a duplicated placeholder does not earn a second course."""
    elective_map = {("DS", "DS3"): [ec("DSA"), ec("DSB")]}

    _recs, picks, _unserved = _resolve_elective_slots(
        {31: ["DS3", "DS3"]}, elective_map, ds, {}, {}
    )

    assert picks == [(31, "DS3", "DSA")]


def test_one_course_mapped_twice_onto_one_slot_is_offered_once() -> None:
    """ES13: a duplicate mapping row does not make a slot look servable twice."""
    elective_map = {("DS", "DS1"): [ec("DS482"), ec("DS482")], ("DS", "DS3"): [ec("DS482")]}

    _recs, picks, unserved = _resolve_elective_slots({32: ["DS1", "DS3"]}, elective_map, ds, {}, {})

    assert picks == [(32, "DS1", "DS482")]
    assert unserved == [(32, "DS3", "courses_exhausted")]


# ── the report ──────────────────────────────────────────────────────────


def test_unserved_slots_are_grouped_with_their_term() -> None:
    """ES14: the report counts students per (programme, slot, reason) and names
    the term, most-affected first."""
    rows = _summarise_unserved_slots(
        [
            (1, "DS3", "courses_exhausted"),
            (2, "DS3", "courses_exhausted"),
            (3, "AI3", "courses_exhausted"),
            (4, "DS3", "no_eligible_course"),
        ],
        lambda sid: "AI" if sid == 3 else "DS",
        {("DS", "DS3"): 10, ("AI", "AI3"): 10},
    )

    assert rows == [
        {
            "programme": "DS",
            "slot": "DS3",
            "programme_term": 10,
            "reason": "courses_exhausted",
            "students": 2,
        },
        {
            "programme": "AI",
            "slot": "AI3",
            "programme_term": 10,
            "reason": "courses_exhausted",
            "students": 1,
        },
        {
            "programme": "DS",
            "slot": "DS3",
            "programme_term": 10,
            "reason": "no_eligible_course",
            "students": 1,
        },
    ]


def test_an_unserved_slot_with_no_declared_term_still_reports() -> None:
    """ES15: a slot absent from the plan reports with no term rather than vanishing."""
    rows = _summarise_unserved_slots([(1, "DS9", "courses_exhausted")], ds, {})

    assert rows == [
        {
            "programme": "DS",
            "slot": "DS9",
            "programme_term": None,
            "reason": "courses_exhausted",
            "students": 1,
        }
    ]


def test_nothing_unserved_reports_nothing() -> None:
    """ES16: a clean run carries an empty report, not a missing key."""
    assert _summarise_unserved_slots([], ds, {}) == []


# ── end to end ──────────────────────────────────────────────────────────


def _ds_plan() -> None:
    """The DS rows the generator reads: a real course plus two elective slots."""
    Course.objects.create(course_code="DS451", description="INFORMATION RETRIEVAL", credit_hours=3)
    for code, name, term in [
        ("DS451", "INFORMATION RETRIEVAL", 10),
        ("DS1", "PROGRAM ELECTIVE COURSE I", 8),
        ("DS3", "PROGRAM ELECTIVE COURSE III", 10),
    ]:
        ProgrammeRequirement.objects.create(
            program="DS",
            course_code=code,
            course_name=name,
            programme_term=term,
            credit_hours=3,
            type="Mandatory" if code == "DS451" else "Program Elective",
        )


def _map_elective(code: str, slots: list[str]) -> None:
    elective = ElectiveCourse.objects.create(
        course_code=code, course_name=f"{code} NAME", programme="DS", credit_hours=3
    )
    for slot in slots:
        ElectiveTermMapping.objects.create(
            academic_year="1448", term=2, programme="DS", placeholder_code=slot, elective=elective
        )


@pytest.mark.django_db
def test_a_term_10_slot_is_budgeted_at_term_10(monkeypatch) -> None:
    """ES17: the live case. DS482 fills DS1 and DS3; the term-10 cohort's
    elective must be budgeted at term 10, not folded into term 8.

    Before the fix every DS482 pick was credited to DS1, so the budget carried
    programme_term 8 and the Term 10 board had no elective at all.
    """
    _ds_plan()
    _map_elective("DS482", ["DS1", "DS3"])
    _map_elective("DS471", ["DS3"])

    students = [770001, 770002, 770003]
    for sid in students:
        Student.objects.create(
            student_id=sid,
            registration_no=str(sid),
            name=f"Student {sid}",
            program="DS",
            section="M",
            status="active",
        )

    monkeypatch.setattr("core.services.timetable_generate.get_student_ids", lambda **_kw: students)
    monkeypatch.setattr(
        "core.services.recommender_batch.batch_recommend",
        # Every one of them owes DS3 and the term-10 course, and nothing else:
        # their elective belongs to term 10.
        lambda student_ids, _program, _year, _term: {sid: ["DS451", "DS3"] for sid in student_ids},
    )

    result = generate_workspace_scenario(
        1448,
        2,
        "DS",
        scenario_name="Elective slot term test",
        run_autoplace=False,
    )

    budgets = {
        b.course_code: b
        for b in ScenarioSectionBudget.objects.filter(scenario_id=result["scenario"]["id"])
    }
    assert "DS482" in budgets or "DS471" in budgets, f"no elective budgeted: {sorted(budgets)}"
    elective = budgets.get("DS482") or budgets["DS471"]
    assert elective.programme_term == 10, (
        f"{elective.course_code} budgeted at term {elective.programme_term}, expected 10"
    )
    assert result["unserved_elective_slots"] == []


@pytest.mark.django_db
def test_a_students_term_follows_the_slot_their_elective_filled(monkeypatch) -> None:
    """ES19: when the elective is the only course with a term, it decides the
    student's own term level — so they land on the term-10 board.

    Without the slot's term the course has no term at all, the student cannot be
    classified, and they appear on no board.
    """
    _ds_plan()
    _map_elective("DS482", ["DS1", "DS3"])

    students = [772001, 772002]
    for sid in students:
        Student.objects.create(
            student_id=sid,
            registration_no=str(sid),
            name=f"Student {sid}",
            program="DS",
            section="M",
            status="active",
        )

    monkeypatch.setattr("core.services.timetable_generate.get_student_ids", lambda **_kw: students)
    monkeypatch.setattr(
        "core.services.recommender_batch.batch_recommend",
        # Only the elective, so nothing else can decide their term.
        lambda student_ids, _program, _year, _term: {sid: ["DS3"] for sid in student_ids},
    )

    result = generate_workspace_scenario(
        1448, 2, "DS", scenario_name="Elective decides the term", run_autoplace=False
    )

    assert result["student_summary"]["by_term"] == {10: 2}


@pytest.mark.django_db
def test_a_cross_term_student_is_linked_to_their_electives_board(monkeypatch) -> None:
    """ES19b: a student counted as cross-term BECAUSE of their elective gets a
    visitor link to the board that elective sits on.

    The resolved elective has no entry in the course->term maps, so the visitor
    loop has to read the slot's term as well. Without it the student is marked
    cross-term and then linked to no second board at all.
    """
    _ds_plan()
    ProgrammeRequirement.objects.create(
        program="DS",
        course_code="DS352",
        course_name="DATA EXPLORATION",
        programme_term=8,
        credit_hours=3,
        type="Mandatory",
    )
    _map_elective("DS482", ["DS1", "DS3"])

    # The two we care about sit at term 8 and owe the term-10 elective. A board
    # exists only for a term some student calls their own, so a third student
    # anchored at term 10 is what brings the Term 10 board into being.
    cross_term = [773001, 773002]
    anchor = 773003
    for sid in [*cross_term, anchor]:
        Student.objects.create(
            student_id=sid,
            registration_no=str(sid),
            name=f"Student {sid}",
            program="DS",
            section="M",
            status="active",
        )

    monkeypatch.setattr(
        "core.services.timetable_generate.get_student_ids", lambda **_kw: [*cross_term, anchor]
    )
    monkeypatch.setattr(
        "core.services.recommender_batch.batch_recommend",
        # A term-8 course plus the DS3 elective, which sits at term 10: the
        # elective is the only reason they reach outside term 8 at all.
        lambda student_ids, _program, _year, _term: {
            sid: (["DS451"] if sid == anchor else ["DS352", "DS3"]) for sid in student_ids
        },
    )

    result = generate_workspace_scenario(
        1448, 2, "DS", scenario_name="Visitor link for an elective", run_autoplace=False
    )

    boards = {b["label"]: b["id"] for b in result["boards"] if isinstance(b, dict) and "label" in b}
    assert "Term 10" in boards, f"no term-10 board: {sorted(boards)}"
    visitors = BoardStudentLink.objects.filter(
        board_id=boards["Term 10"], link_type="visitor"
    ).values_list("student_id", flat=True)
    assert sorted(visitors) == cross_term


@pytest.mark.django_db
def test_the_same_slot_code_keeps_each_plans_own_term(monkeypatch) -> None:
    """ES20: AI3 is term 10 in the AI plan and term 9 in AI2, and each plan's
    elective is budgeted at its own plan's term.

    The slot term used to be looked up by the bare code and cached on first
    sight, so whichever plan loaded first set the term for both.
    """
    Course.objects.create(course_code="AI300", description="CORE", credit_hours=3)
    for programme, slot_term in [("AI", 10), ("AI2", 9)]:
        ProgrammeRequirement.objects.create(
            program=programme,
            course_code="AI300",
            course_name="CORE",
            programme_term=slot_term,
            credit_hours=3,
            type="Mandatory",
        )
        ProgrammeRequirement.objects.create(
            program=programme,
            course_code="AI3",
            course_name="PROGRAM ELECTIVE COURSE III",
            programme_term=slot_term,
            credit_hours=3,
            type="Program Elective",
        )
    for programme, code in [("AI", "AI462"), ("AI2", "AI455")]:
        elective = ElectiveCourse.objects.create(
            course_code=code, course_name=f"{code} NAME", programme=programme, credit_hours=3
        )
        ElectiveTermMapping.objects.create(
            academic_year="1448",
            term=2,
            programme=programme,
            placeholder_code="AI3",
            elective=elective,
        )

    students = {780001: "AI", 780002: "AI2"}
    for sid, programme in students.items():
        Student.objects.create(
            student_id=sid,
            registration_no=str(sid),
            name=f"Student {sid}",
            program=programme,
            section="M",
            status="active",
        )

    monkeypatch.setattr(
        "core.services.timetable_generate.get_student_ids", lambda **_kw: list(students)
    )
    monkeypatch.setattr(
        "core.services.recommender_batch.batch_recommend_multi_program",
        lambda student_ids, _year, _term: {sid: ["AI3"] for sid in student_ids},
    )

    result = generate_workspace_scenario(
        1448,
        2,
        ["AI", "AI2"],
        scenario_name="Slot term per plan",
        run_autoplace=False,
    )

    terms = {
        b.course_code: b.programme_term
        for b in ScenarioSectionBudget.objects.filter(scenario_id=result["scenario"]["id"])
    }
    # The AI plan's course at the AI plan's term, AI2's at AI2's.
    assert terms.get("AI462") == 10, terms
    assert terms.get("AI455") == 9, terms


@pytest.mark.django_db
def test_a_slot_term_of_zero_is_not_a_term(monkeypatch) -> None:
    """ES21: 0 is not a curriculum term, so it is not used as one.

    The old lookup required a truthy term and skipped a 0; keeping that means
    bad plan data cannot quietly place an elective on a term-0 board.
    """
    Course.objects.create(course_code="DS451", description="INFORMATION RETRIEVAL", credit_hours=3)
    ProgrammeRequirement.objects.create(
        program="DS",
        course_code="DS451",
        course_name="INFORMATION RETRIEVAL",
        programme_term=10,
        credit_hours=3,
        type="Mandatory",
    )
    ProgrammeRequirement.objects.create(
        program="DS",
        course_code="DS3",
        course_name="PROGRAM ELECTIVE COURSE III",
        programme_term=0,
        credit_hours=3,
        type="Program Elective",
    )
    _map_elective("DS482", ["DS3"])

    students = [774001]
    Student.objects.create(
        student_id=students[0],
        registration_no=str(students[0]),
        name="Student",
        program="DS",
        section="M",
        status="active",
    )

    monkeypatch.setattr("core.services.timetable_generate.get_student_ids", lambda **_kw: students)
    monkeypatch.setattr(
        "core.services.recommender_batch.batch_recommend",
        lambda student_ids, _program, _year, _term: {sid: ["DS451", "DS3"] for sid in student_ids},
    )

    result = generate_workspace_scenario(
        1448, 2, "DS", scenario_name="Term zero slot", run_autoplace=False
    )

    budget = ScenarioSectionBudget.objects.filter(
        scenario_id=result["scenario"]["id"], course_code="DS482"
    ).first()
    assert budget is not None
    assert budget.programme_term is None, "term 0 must not be adopted as the slot's term"


@pytest.mark.django_db
def test_an_unfillable_slot_is_reported_by_generate(monkeypatch) -> None:
    """ES18: when a slot has no course left for a student, generate says so."""
    _ds_plan()
    _map_elective("DS482", ["DS1", "DS3"])

    students = [771001, 771002]
    for sid in students:
        Student.objects.create(
            student_id=sid,
            registration_no=str(sid),
            name=f"Student {sid}",
            program="DS",
            section="M",
            status="active",
        )
        StudentCourse.objects.filter(student_id=sid).delete()

    monkeypatch.setattr("core.services.timetable_generate.get_student_ids", lambda **_kw: students)
    monkeypatch.setattr(
        "core.services.recommender_batch.batch_recommend",
        # Each owes BOTH slots, and DS482 is the only course mapped to either.
        lambda student_ids, _program, _year, _term: {
            sid: ["DS451", "DS1", "DS3"] for sid in student_ids
        },
    )

    result = generate_workspace_scenario(
        1448,
        2,
        "DS",
        scenario_name="Unservable slot test",
        run_autoplace=False,
    )

    assert result["unserved_elective_slots"] == [
        {
            "programme": "DS",
            "slot": "DS3",
            "programme_term": 10,
            "reason": "courses_exhausted",
            "students": 2,
        }
    ]

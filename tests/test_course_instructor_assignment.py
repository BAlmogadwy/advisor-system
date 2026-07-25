"""Course-level instructor assignment — model, service, endpoints, load report,
and the planner write-through.

Assignment is scenario-independent: a ``CourseInstructor`` ties an instructor to
``(program, course_code, section M/F)``. The planner resolves the primary at
section-generation and writes its name into ``TermSectionMeeting.instructor``.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import Group, User
from django.db import IntegrityError
from django.db.models import ProtectedError
from django.test import Client

from core.models import (
    CourseInstructor,
    DeliveryBoard,
    ElectiveCourse,
    ElectiveTermMapping,
    Instructor,
    ProgrammeRequirement,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.course_instructor_assignment import (
    build_assignable_course_list,
    reconcile_scenario_instructors,
    set_course_instructors,
)
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups
from core.services.timetable_pr4_instructor import build_section_instructor_ids


def _instructor(name: str, **kw) -> Instructor:
    from core.services.timetable_pr4_instructor import normalise_instructor

    return Instructor.objects.create(
        full_name=name, normalised_name=normalise_instructor(name), **kw
    )


def _req(program: str, code: str, term: int = 1, credit: int = 3) -> None:
    ProgrammeRequirement.objects.create(
        program=program,
        course_code=code,
        course_name=f"{code} name",
        programme_term=term,
        credit_hours=credit,
    )


def _elective(programme: str, code: str, credit: int = 3) -> ElectiveCourse:
    return ElectiveCourse.objects.create(
        programme=programme, course_code=code, course_name=f"{code} name", credit_hours=credit
    )


def _map(
    programme: str, placeholder: str, elective: ElectiveCourse, year: str = "1448", term: int = 1
) -> None:
    ElectiveTermMapping.objects.create(
        academic_year=year,
        term=term,
        programme=programme,
        placeholder_code=placeholder,
        elective=elective,
    )


def _admin_client() -> Client:
    ensure_role_groups()
    user, _ = User.objects.get_or_create(username="ci-admin")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    http = Client()
    http.force_login(user)
    return http


# ── Model ────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_one_primary_constraint() -> None:
    a, b = _instructor("Dr A"), _instructor("Dr B")
    CourseInstructor.objects.create(
        program="AI", course_code="AI113", section="M", instructor=a, role="primary"
    )
    # a second primary for the same (program, course, section) is rejected
    with pytest.raises(IntegrityError):
        CourseInstructor.objects.create(
            program="AI", course_code="AI113", section="M", instructor=b, role="primary"
        )


@pytest.mark.django_db
def test_unique_person_per_course_section() -> None:
    a = _instructor("Dr A")
    CourseInstructor.objects.create(
        program="AI", course_code="AI113", section="M", instructor=a, role="primary"
    )
    with pytest.raises(IntegrityError):
        CourseInstructor.objects.create(
            program="AI", course_code="AI113", section="M", instructor=a, role="co"
        )


@pytest.mark.django_db
def test_protect_assigned_instructor() -> None:
    a = _instructor("Dr A")
    CourseInstructor.objects.create(program="AI", course_code="AI113", section="M", instructor=a)
    with pytest.raises(ProtectedError):
        a.delete()


# ── Service ──────────────────────────────────────────────────────


@pytest.mark.django_db
def test_set_course_instructors_primary_first_and_replace() -> None:
    a, b, c = _instructor("Dr A"), _instructor("Dr B"), _instructor("Dr C")
    res = set_course_instructors("AI", "ai113", "M", [a.pk, b.pk])  # lowercase code normalises
    assert [(r["role"]) for r in res] == ["primary", "co"]
    assert CourseInstructor.objects.filter(course_code="AI113").count() == 2
    # re-set replaces cleanly
    res2 = set_course_instructors("AI", "AI113", "M", [c.pk])
    assert [r["id"] for r in res2] == [c.pk]
    assert (
        CourseInstructor.objects.filter(program="AI", course_code="AI113", section="M").count() == 1
    )
    # empty clears
    set_course_instructors("AI", "AI113", "M", [])
    assert CourseInstructor.objects.filter(course_code="AI113").count() == 0


# ── Endpoints ────────────────────────────────────────────────────


@pytest.mark.django_db
def test_course_assignments_lists_all_courses_with_state() -> None:
    http = _admin_client()
    _req("AI", "AI113")
    _req("AI", "AI212")
    a = _instructor("Dr A")
    set_course_instructors("AI", "AI113", "M", [a.pk])

    r = http.get("/ops/instructors/course-assignments/", {"program": "AI", "section": "M"})
    assert r.status_code == 200
    courses = {c["course_code"]: c for c in r.json()["courses"]}
    assert courses["AI113"]["instructor"]["full_name"] == "Dr A"
    assert courses["AI212"]["instructor"] is None  # unassigned still listed


# ── Elective placeholder resolution ──────────────────────────────


@pytest.mark.django_db
def test_program_electives_expand_from_catalogue() -> None:
    """A program-elective placeholder (AI1) is replaced by the department's real
    ElectiveCourse catalogue; the abstract placeholder is never listed."""
    _req("AI", "AI201", term=5)  # mandatory
    _req("AI", "AI1", term=9)  # program-elective placeholder
    _elective("AI", "AI461")
    _elective("AI", "AI462")

    rows = build_assignable_course_list("AI", "1448", 1)
    by_code = {r["course_code"]: r for r in rows}
    assert "AI201" in by_code  # mandatory kept
    assert "AI1" not in by_code  # placeholder replaced, never assignable
    assert by_code["AI461"]["is_elective"] is True
    assert by_code["AI461"]["offered_this_term"] is False  # in catalogue, not mapped
    assert by_code["AI461"]["programme_term"] == 9  # inherits the slot's term


@pytest.mark.django_db
def test_program_electives_from_mapping_when_catalogue_empty() -> None:
    """IS has no departmental catalogue — its electives live only in the term
    mapping (elective.programme is blank). They must still surface, flagged as
    offered this term."""
    _req("IS", "IS1", term=8)
    ec = _elective("", "IS481")  # blank programme, mirrors real IS data
    _map("IS", "IS1", ec, term=1)

    by_code = {r["course_code"]: r for r in build_assignable_course_list("IS", "1448", 1)}
    assert "IS1" not in by_code
    assert by_code["IS481"]["is_elective"] is True
    assert by_code["IS481"]["offered_this_term"] is True


@pytest.mark.django_db
def test_offered_flag_is_term_scoped() -> None:
    """offered_this_term reflects the requested term, not any term."""
    _req("AI", "AI1", term=9)
    ec = _elective("AI", "AI463")
    _map("AI", "AI1", ec, term=1)

    t1 = {r["course_code"]: r for r in build_assignable_course_list("AI", "1448", 1)}
    t2 = {r["course_code"]: r for r in build_assignable_course_list("AI", "1448", 2)}
    assert t1["AI463"]["offered_this_term"] is True
    assert t2["AI463"]["offered_this_term"] is False  # same catalogue course, not mapped in T2


@pytest.mark.django_db
def test_unresolvable_program_elective_kept_visible() -> None:
    """A program-elective slot with neither catalogue nor mapping stays visible
    (a data gap the registrar should see) rather than silently vanishing."""
    _req("COE", "COE1", term=9)
    by_code = {r["course_code"]: r for r in build_assignable_course_list("COE", "1448", 1)}
    assert "COE1" in by_code
    assert by_code["COE1"]["is_elective"] is False


@pytest.mark.django_db
def test_free_and_university_placeholders_kept_when_unmapped() -> None:
    """Free/university electives are other-faculty courses with no departmental
    catalogue; their slots remain visible until a term mapping fills them."""
    _req("AI", "FE1", term=6)
    _req("AI", "GSE1", term=7)
    by_code = {r["course_code"]: r for r in build_assignable_course_list("AI", "1448", 1)}
    assert "FE1" in by_code
    assert "GSE1" in by_code


@pytest.mark.django_db
def test_assignment_overlays_on_resolved_elective_code() -> None:
    """An instructor assigned to the real elective code shows against the
    resolved row — the whole point of resolving placeholders here."""
    http = _admin_client()
    _req("AI", "AI1", term=9)
    _elective("AI", "AI461")
    a = _instructor("Dr Elec")
    set_course_instructors("AI", "AI461", "M", [a.pk])

    r = http.get("/ops/instructors/course-assignments/", {"program": "AI", "section": "M"})
    courses = {c["course_code"]: c for c in r.json()["courses"]}
    assert courses["AI461"]["instructor"]["full_name"] == "Dr Elec"


@pytest.mark.django_db
def test_secondary_cohort_shares_base_catalogue_and_mappings() -> None:
    """Secondary cohorts (AI2) reuse the base cohort's catalogue + mappings, which
    are keyed only under the base code 'AI'. They must still resolve, not fall
    back to listing the un-schedulable placeholder."""
    _req("AI2", "AI1", term=9)  # AI2 plan uses the same placeholder codes as AI
    _elective("AI", "AI461")  # catalogue lives under the base cohort 'AI'
    ec = _elective("AI", "AI463")
    _map("AI", "AI1", ec, term=1)  # mapping also under base 'AI'

    by_code = {r["course_code"]: r for r in build_assignable_course_list("AI2", "1448", 1)}
    assert "AI1" not in by_code  # placeholder resolved, not re-listed
    assert by_code["AI461"]["is_elective"] is True
    assert by_code["AI463"]["offered_this_term"] is True


@pytest.mark.django_db
def test_mandatory_course_wins_over_same_code_elective_regardless_of_order() -> None:
    """A real ProgrammeRequirement course keeps its own term/credits/flags even
    when a same-code catalogue elective exists and the program-elective slot sorts
    before it (the 'mandatory wins' invariant must be order-independent)."""
    _req("AI", "AI1", term=3)  # program-elective placeholder — sorts first
    _req("AI", "AI461", term=9, credit=4)  # mandatory, higher term
    _elective("AI", "AI461")  # same code also in the catalogue

    by_code = {r["course_code"]: r for r in build_assignable_course_list("AI", "1448", 1)}
    row = by_code["AI461"]
    assert row["is_elective"] is False  # mandatory wins
    assert row["programme_term"] == 9  # its own term, not the block's min
    assert row["credit_hours"] == 4


@pytest.mark.django_db
def test_unmapped_slots_are_flagged_placeholder() -> None:
    """Slots with no resolvable real course stay visible but flagged so the UI can
    disable assignment (assigning to them would create a dead, unschedulable row)."""
    coe = {r["course_code"]: r for r in build_assignable_course_list("COE", "1448", 1)}
    # COE has no catalogue/mapping under COE or its base — placeholder kept.
    _req("COE", "COE1", term=9)
    coe = {r["course_code"]: r for r in build_assignable_course_list("COE", "1448", 1)}
    assert coe["COE1"]["is_placeholder"] is True

    _req("AI", "FE1", term=6)  # free-elective slot, unmapped
    ai = {r["course_code"]: r for r in build_assignable_course_list("AI", "1448", 1)}
    assert ai["FE1"]["is_placeholder"] is True


@pytest.mark.django_db
def test_orphan_placeholder_assignment_surfaced_and_clearable() -> None:
    """A legacy CourseInstructor keyed to a now-resolved placeholder code is
    surfaced as an is_orphan row so it stays visible and clearable rather than
    stranded invisibly in the DB."""
    http = _admin_client()
    _req("AI", "AI1", term=9)
    _elective("AI", "AI463")  # AI1 resolves -> 'AI1' is not in the resolved list
    a = _instructor("Dr Legacy")
    CourseInstructor.objects.create(
        program="AI", course_code="AI1", section="M", instructor=a, role="primary"
    )

    r = http.get("/ops/instructors/course-assignments/", {"program": "AI", "section": "M"})
    courses = {c["course_code"]: c for c in r.json()["courses"]}
    assert courses["AI1"]["is_orphan"] is True
    assert courses["AI1"]["instructor"]["full_name"] == "Dr Legacy"


@pytest.mark.django_db
def test_mapped_electives_visible_across_terms_not_falsely_orphaned() -> None:
    """A real elective is listed (assignment is term-independent) whatever term is
    viewed; ``offered_this_term`` tracks the viewed term. Guards against IS-style
    electives (mapped in T1, catalogue under a blank programme) being mislabeled
    as clearable orphans when a later term is viewed."""
    _req("IS", "IS1", term=8)
    ec = _elective("", "IS481")  # blank-programme catalogue, reachable only via mapping
    _map("IS", "IS1", ec, term=1)

    t1 = {r["course_code"]: r for r in build_assignable_course_list("IS", "1448", 1)}
    t2 = {r["course_code"]: r for r in build_assignable_course_list("IS", "1448", 2)}
    assert t1["IS481"]["is_elective"] is True and t1["IS481"]["offered_this_term"] is True
    # Still a real elective in T2 (assignable), just not offered — NOT an orphan.
    assert t2["IS481"]["is_elective"] is True
    assert t2["IS481"]["offered_this_term"] is False
    assert t2["IS481"].get("is_orphan", False) is False


@pytest.mark.django_db
def test_assignment_to_mapped_elective_not_orphaned_in_other_term() -> None:
    """End-to-end: an instructor assigned to a mapped elective shows against the
    real row (never an orphan) even when the viewed term isn't the mapped term."""
    http = _admin_client()
    _req("IS", "IS1", term=8)
    ec = _elective("", "IS481")
    _map("IS", "IS1", ec, term=1)
    a = _instructor("Dr IS")
    set_course_instructors("IS", "IS481", "M", [a.pk])

    r = http.get(
        "/ops/instructors/course-assignments/", {"program": "IS", "section": "M", "term": 2}
    )
    courses = {c["course_code"]: c for c in r.json()["courses"]}
    assert courses["IS481"]["instructor"]["full_name"] == "Dr IS"
    assert courses["IS481"].get("is_orphan", False) is False


@pytest.mark.django_db
def test_set_clear_endpoints_and_validation() -> None:
    http = _admin_client()
    _req("AI", "AI113")
    a = _instructor("Dr A")
    ok = http.post(
        "/ops/instructors/course-assignments/set/",
        data=json.dumps(
            {"program": "AI", "section": "M", "course_code": "AI113", "instructor_ids": [a.pk]}
        ),
        content_type="application/json",
    )
    assert ok.status_code == 200
    assert (
        CourseInstructor.objects.filter(program="AI", course_code="AI113", section="M").count() == 1
    )
    # bad section
    bad = http.post(
        "/ops/instructors/course-assignments/set/",
        data=json.dumps(
            {"program": "AI", "section": "X", "course_code": "AI113", "instructor_ids": [a.pk]}
        ),
        content_type="application/json",
    )
    assert bad.status_code == 400
    # clear
    http.post(
        "/ops/instructors/course-assignments/clear/",
        data=json.dumps({"program": "AI", "section": "M", "course_code": "AI113"}),
        content_type="application/json",
    )
    assert CourseInstructor.objects.filter(course_code="AI113").count() == 0


@pytest.mark.django_db
def test_rbac_denies_non_advisor() -> None:
    ensure_role_groups()
    user, _ = User.objects.get_or_create(username="ci-nobody")
    user.groups.clear()
    http = Client()
    http.force_login(user)
    r = http.get("/ops/instructors/course-assignments/", {"program": "AI", "section": "M"})
    assert r.status_code in (403, 302)


@pytest.mark.django_db
def test_load_report_course_level() -> None:
    http = _admin_client()
    _req("AI", "AI113", credit=3)
    _req("AI", "AI212", credit=4)
    a = _instructor("Dr Load", max_weekly_hours=5)
    set_course_instructors("AI", "AI113", "M", [a.pk])
    set_course_instructors("AI", "AI212", "M", [a.pk])
    r = http.get("/ops/instructors/load-report/")
    assert r.status_code == 200
    row = next(x for x in r.json()["rows"] if x["instructor_id"] == a.pk)
    assert row["course_count"] == 2
    assert row["total_credit_hours"] == 7
    assert row["load_status"] == "over"  # 7 > 5


# ── Planner integration ──────────────────────────────────────────


@pytest.mark.django_db
def test_autoplace_write_through_and_links(settings) -> None:
    """generate populates scenario.gender/programs; autoplace fans the primary
    course-instructor name into meetings; build_section_instructor_ids resolves
    section→instructor from CourseInstructor for the scenario."""
    sc = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="AI M T1", gender="M", programs=["AI"]
    )
    instr = _instructor("Dr Course")
    set_course_instructors("AI", "AI113", "M", [instr.pk])
    ts = TermSection.objects.create(
        scenario=sc, course_key="AI113", course_code="AI113", course_number="113", section="S1"
    )
    TermSectionMeeting.objects.create(
        term_section=ts, day="SUN", start_time="09:00", end_time="10:15"
    )

    # links-keyed resolution
    settings.TIMETABLE_INSTRUCTOR_LINKS_ENABLED = True
    mapping = build_section_instructor_ids(sc)
    assert mapping == {"AI113|S1": {instr.pk}}

    # reconcile fans the name into the meeting display cache
    updated = reconcile_scenario_instructors(sc)
    assert updated == 1
    assert ts.meetings.first().instructor == "Dr Course"


@pytest.mark.django_db
def test_build_section_instructor_ids_empty_without_gender() -> None:
    sc = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="x", gender="", programs=["AI"]
    )
    assert build_section_instructor_ids(sc) == {}


@pytest.mark.django_db
def test_solver_persist_refans_instructor() -> None:
    """A solver persist deletes + recreates meeting rows with a blank instructor.
    It MUST re-fan the primary CourseInstructor name, or a CP-SAT-backed build
    (full rebuild / optimal / V2 polish) silently drops the greedy write-through
    and the Instructors export sheet goes blank — the bug this guards against.
    """
    from core.services.timetable_solver import persist_solver_result

    sc = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="AI M T1", gender="M", programs=["AI"]
    )
    board = DeliveryBoard.objects.create(scenario=sc, label="T1", nominal_term=1, program="AI")
    instr = _instructor("Dr Solver")
    set_course_instructors("AI", "AI113", "M", [instr.pk])

    # Greedy already placed the section and wrote the primary's name on it.
    ts = TermSection.objects.create(
        scenario=sc,
        course_key="AI113",
        course_code="AI113",
        course_number="113",
        section="S1",
        source_tag="tw_auto",
    )
    TermSectionMeeting.objects.create(
        term_section=ts, day="SUN", start_time="09:00", end_time="10:15", instructor="Dr Solver"
    )
    SectionPlacement.objects.create(
        board=board, term_section=ts, day="SUN", start_time="09:00", end_time="10:15", room="R1"
    )

    # CP-SAT relocates the section; persist wipes the old meeting and recreates it.
    persist_solver_result(
        board.id,
        {
            "status": "feasible",
            "placements": [
                {
                    "course_code": "AI113",
                    "display_code": "AI113",
                    "section": "S1",
                    "course_name": "AI113",
                    "meetings": [{"day": "MON", "start": "11:00", "end": "12:15"}],
                }
            ],
        },
    )

    meeting = TermSectionMeeting.objects.get(term_section=ts)
    assert meeting.day == "MON"  # the relocation persisted...
    assert meeting.instructor == "Dr Solver"  # ...and the write-through survived it


@pytest.mark.django_db
def test_solver_persist_leaves_unassigned_course_blank() -> None:
    """Re-fan is a no-op for a course with no active primary link — the meeting
    stays blank rather than borrowing some other course's instructor.
    """
    from core.services.timetable_solver import persist_solver_result

    sc = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="AI M T1", gender="M", programs=["AI"]
    )
    board = DeliveryBoard.objects.create(scenario=sc, label="T1", nominal_term=1, program="AI")
    # AI113 has an instructor; AI999 (the one we persist) does not.
    set_course_instructors("AI", "AI113", "M", [_instructor("Dr Solver").pk])

    persist_solver_result(
        board.id,
        {
            "status": "feasible",
            "placements": [
                {
                    "course_code": "AI999",
                    "display_code": "AI999",
                    "section": "S1",
                    "course_name": "AI999",
                    "meetings": [{"day": "MON", "start": "11:00", "end": "12:15"}],
                }
            ],
        },
    )

    meeting = TermSectionMeeting.objects.get(term_section__course_key="AI999")
    assert meeting.instructor == ""

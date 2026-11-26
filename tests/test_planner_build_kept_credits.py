"""The staff build counts the kept registered courses against the credit cap.

In "keep registered" mode the build checked the cap against the shortlisted courses
only: ``build_plans`` took its credits from the shortlist and used the kept timetable
as busy time and nothing else. A student holding 15 registered hours with the cap at
18 was given 18 more. The cap is the student's whole load, as the board's own credits
header already counts it: what is kept plus what is added.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import (
    Course,
    ElectiveCourse,
    ElectiveTermMapping,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
)
from core.services.planner_builder import build_plans
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups

pytestmark = pytest.mark.django_db

PROG = "KCP"
YEAR, TERM = "1448", "1"
SID = 4_690_301
DAYS = ["SUN", "MON", "TUE", "WED", "THU"]


def _student() -> None:
    Student.objects.create(
        student_id=SID,
        registration_no=str(SID),
        name="Sample Student",
        program=PROG,
        section="M",
        status="ACTIVE",
    )


def _section(code: str, slot: int, *, hours: int = 3, in_plan: bool = True) -> TermSection:
    """One section of ``code`` at its own hour of the week, so nothing ever clashes."""
    if in_plan:
        ProgrammeRequirement.objects.create(
            program=PROG,
            course_code=code,
            course_name=f"Course {code}",
            programme_term=1,
            credit_hours=hours,
            type="Mandatory",
        )
    section = TermSection.objects.create(
        course_key=code,
        course_code=code,
        course_number="",
        course_name=f"Course {code}",
        section="M1",
        available_capacity=30,
        registered_count=1,
    )
    TermSectionProgram.objects.create(term_section=section, program=PROG)
    start = 8 + 2 * (slot // 5)
    # Two meetings, so the baseline has two rows for the course: its hours are
    # the course's, once.
    for room in ("A1", "A2"):
        TermSectionMeeting.objects.create(
            term_section=section,
            day=DAYS[slot % 5],
            start_time=f"{start:02d}:00",
            end_time=f"{start + 1:02d}:15",
            room=room,
        )
    return section


def _register(section: TermSection) -> None:
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year=YEAR,
        term=TERM,
        term_section=section,
        source="scraper_timetable",
    )


def _kept(count: int, *, hours: int = 3) -> None:
    """Registered courses, as the registrar's records hold them: a section and a status."""
    for index in range(count):
        code = f"KP1{index:02d}"
        _register(_section(code, index, hours=hours))
        course = Course.objects.create(
            course_code=code, description=f"Course {code}", credit_hours=hours
        )
        StudentCourse.objects.create(student_id=SID, course=course, status="studying")


def _wanted(count: int, *, hours: int = 3) -> list[dict]:
    items = []
    for index in range(count):
        code = f"NW2{index:02d}"
        _section(code, 10 + index, hours=hours)
        items.append(
            {
                "course_code": code,
                "credits": hours,
                "priority": "Med",
                "score": 50,
                "status": "Eligible",
                "missing_prerequisites": [],
                "must_take": False,
            }
        )
    return items


def _client() -> Client:
    ensure_role_groups()
    user = User.objects.create(username="kept-credits")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client(SERVER_NAME="localhost")
    client.force_login(user)
    return client


def _build(client: Client, shortlist: list[dict], *, cap: int = 18, mode: str = "keep") -> dict:
    response = client.post(
        "/ops/planner/build/",
        data={
            "student_id": str(SID),
            "academic_year": YEAR,
            "term": TERM,
            "mode": mode,
            "eligibility_mode": "relaxed",
            "swap": False,
            "program_sections_only": True,
            "allow_full_sections": False,
            "max_credits": cap,
            "shortlist": shortlist,
        },
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response.json()


def _context(client: Client) -> dict:
    return client.post(
        "/ops/planner/context/",
        data={"student_id": str(SID), "academic_year": YEAR, "term": TERM},
        content_type="application/json",
    ).json()


def _added(option: dict, hours: int = 3) -> int:
    return len(option["mappings"]) * hours


def test_keep_mode_leaves_only_the_room_the_registered_courses_do_not_use() -> None:
    """15 registered, cap 18: one 3-hour course fits. It used to add six."""
    _student()
    _kept(5)
    result = _build(_client(), _wanted(6))

    assert result["options"], result
    assert {_added(option) for option in result["options"]} == {3}
    # Each of the three solvers is given the room, and so each still has an
    # option to offer. One given the cap as set would fill it and be refused.
    assert {option["method"] for option in result["options"]} == {"A", "B", "C"}
    assert result["constraints"]["registered_credits"] == 15
    assert result["constraints"]["credit_room"] == 3
    # The options can still be forecast: the build hands out its token as before.
    assert result["graduation_projection_token"]


def test_a_must_take_course_is_the_one_that_gets_the_room() -> None:
    """Room for one course of two: the must is placed, though the other ranks higher."""
    _student()
    _kept(5)
    wanted = _wanted(2)
    wanted[0].update(priority="High", score=90)
    wanted[1]["must_take"] = True
    result = _build(_client(), wanted)

    assert result["options"], result
    assert result["summary"]["best_feasible"] is True
    for option in result["options"]:
        assert [mapping["course_code"] for mapping in option["mappings"]] == ["NW201"]


def test_a_must_take_course_bigger_than_the_room_fails_the_build_and_says_why() -> None:
    """A must of 4 hours against 3 of room: no option, and the reason names the load."""
    _student()
    _kept(5)
    wanted = _wanted(1, hours=4)
    wanted[0]["must_take"] = True
    result = _build(_client(), wanted)

    assert result["options"] == []
    assert result["summary"]["best_feasible"] is False
    reasons = {
        failure["kind"]: failure["reason"]
        for failure in result["summary"]["hard_constraint_failures"]
    }
    assert "must_take" in reasons, reasons
    assert "15 of it registered" in reasons["must_take"]
    assert "graduation_projection_token" not in result


def test_ignore_mode_gives_the_whole_cap_to_the_shortlist() -> None:
    """Nothing is kept, so nothing is counted: the plan replaces the registration."""
    _student()
    _kept(5)
    result = _build(_client(), _wanted(6), mode="ignore")

    assert max(_added(option) for option in result["options"]) == 18
    assert result["constraints"]["registered_credits"] == 0


def test_no_cap_is_still_no_cap() -> None:
    _student()
    _kept(5)
    result = _build(_client(), _wanted(6), cap=0)

    assert max(_added(option) for option in result["options"]) == 18
    assert result["constraints"]["credit_room"] is None


def test_a_full_load_leaves_no_room_and_says_why() -> None:
    """18 registered against 18: no option, and each course says what stopped it."""
    _student()
    _kept(6)
    result = _build(_client(), _wanted(2))

    assert result["options"] == []
    assert result["summary"]["best_feasible"] is False
    assert [failure["kind"] for failure in result["summary"]["hard_constraint_failures"]] == [
        "max_credits"
    ]
    assert sorted(row["course_code"] for row in result["unscheduled"]) == ["NW200", "NW201"]
    for row in result["unscheduled"]:
        assert row["reason_code"] == "DID_NOT_FIT"
        assert row["details"] == [
            {"course_credits": 3, "credit_limit": 18, "registered_credits": 18}
        ]
    assert result["constraints"]["credit_room"] == 0
    # No option, so nothing to forecast: no token, as for any build that found none.
    assert "graduation_projection_token" not in result


def test_a_course_bigger_than_the_room_is_told_the_limit_it_was_set() -> None:
    """15 registered, cap 18, one 4-hour course: the page is given 18, not 3."""
    _student()
    _kept(5)
    result = _build(_client(), _wanted(1, hours=4))

    assert result["options"] == []
    (row,) = result["unscheduled"]
    assert row["reason_code"] == "DID_NOT_FIT"
    # The course alone is over the room: said as that, not as a clash.
    assert row["details"] == [{"course_credits": 4, "credit_limit": 18, "registered_credits": 15}]
    assert "4 credits" in row["reason"] and "15 of it registered" in row["reason"]


def test_a_course_that_only_misses_the_room_beside_the_others_says_how_much_is_registered() -> None:
    """Two 2-hour courses for 3 hours of room: one goes in, the other is told why not."""
    _student()
    _kept(5)
    result = _build(_client(), _wanted(2, hours=2))

    assert {_added(option, hours=2) for option in result["options"]} == {2}
    left = [row for option in result["options"] for row in option["unscheduled"]]
    assert left
    for row in left:
        assert row["reason_code"] == "DID_NOT_FIT"
        assert row["details"][0]["credit_limit"] == 18
        assert row["details"][0]["registered_credits"] == 15
        assert "sections_considered" in row["details"][0]


def test_a_load_already_over_the_cap_leaves_no_room() -> None:
    """18 registered against a cap of 15: the room is none, not minus three."""
    _student()
    _kept(6)
    result = _build(_client(), _wanted(1), cap=15)

    assert result["options"] == []
    assert result["constraints"]["registered_credits"] == 18
    assert result["constraints"]["credit_room"] == 0
    (failure,) = result["summary"]["hard_constraint_failures"]
    assert failure["reason"] == "No room left under the 15-credit limit: 18 credits are registered"
    assert [row["details"] for row in result["unscheduled"]] == [
        [{"course_credits": 3, "credit_limit": 15, "registered_credits": 18}]
    ]


def test_an_option_over_the_room_is_refused_whichever_solver_made_it(monkeypatch) -> None:
    """The last check counts against the room too, not against the cap as it was set."""
    from core.services import planner_builder

    _student()
    shortlist = _wanted(3)
    catalogue = planner_builder._catalog_for_courses(
        YEAR, TERM, [item["course_code"] for item in shortlist], "M", PROG
    )
    everything = [sections[0] for sections in catalogue.values()]
    assert len(everything) == 3

    monkeypatch.setattr(planner_builder, "_cp_build_option", lambda *a, **k: (everything, []))
    monkeypatch.setattr(planner_builder, "_bitmask_build_option_b", lambda *a, **k: ([], []))
    monkeypatch.setattr(planner_builder, "_bitmask_build_option_c", lambda *a, **k: ([], []))

    result = build_plans(
        YEAR,
        TERM,
        shortlist,
        [],
        keep_registered=True,
        max_credits=18,
        kept_credits=12,
        gender="M",
        program=PROG,
    )

    # 9 hours fit a cap of 18, and do not fit the 6 that 12 kept hours leave.
    assert result["options"] == []
    assert [failure["kind"] for failure in result["summary"]["hard_constraint_failures"]] == [
        "max_credits"
    ]


def test_the_build_counts_the_load_the_page_shows() -> None:
    """One figure on both sides: a course outside the plan, one with no section, a slot.

    Registered: a plan course (3), a programme elective outside the plan (3), whose
    slot the resolver has marked (counted once), and a general-studies course (2). A
    fifth course is studied with no section on file (4). The page shows 12.
    """
    _student()
    _register(_section("KP100", 0))
    ProgrammeRequirement.objects.create(
        program=PROG,
        course_code="KP1",
        course_name="Program Elective I",
        programme_term=1,
        credit_hours=3,
        type="Program Elective",
    )
    elective = ElectiveCourse.objects.create(
        programme=PROG, course_code="KP463", course_name="Data Mining", credit_hours=3
    )
    ElectiveTermMapping.objects.create(
        academic_year=YEAR,
        term=int(TERM),
        programme=PROG,
        placeholder_code="KP1",
        elective=elective,
    )
    slot = Course.objects.create(
        course_code="KP1", description="Program Elective I", credit_hours=3
    )
    StudentCourse.objects.create(student_id=SID, course=slot, status="studying")
    _register(_section("KP463", 1, in_plan=False))
    Course.objects.create(course_code="GS303", description="Communication Skills", credit_hours=2)
    _register(_section("GS303", 2, in_plan=False))
    ProgrammeRequirement.objects.create(
        program=PROG,
        course_code="KP140",
        course_name="Course KP140",
        programme_term=1,
        credit_hours=4,
        type="Mandatory",
    )
    unmapped = Course.objects.create(
        course_code="KP140", description="Course KP140", credit_hours=4
    )
    StudentCourse.objects.create(student_id=SID, course=unmapped, status="studying")
    client = _client()

    shown = _context(client)["baseline_totals"]["credits"]
    result = _build(client, _wanted(3))

    assert shown == 12
    assert result["constraints"]["registered_credits"] == shown
    assert result["constraints"]["credit_room"] == 6
    assert {_added(option) for option in result["options"]} == {6}


def test_the_solver_itself_takes_the_kept_hours_only_in_keep_mode() -> None:
    """``build_plans`` for a caller that has already made room, or keeps nothing."""
    _student()
    shortlist = _wanted(3)

    def placed(**kwargs) -> int:
        result = build_plans(YEAR, TERM, shortlist, [], gender="M", program=PROG, **kwargs)
        return max((len(option["mappings"]) for option in result["options"]), default=0)

    assert placed(keep_registered=True, max_credits=9, kept_credits=6) == 1
    assert placed(keep_registered=False, max_credits=9, kept_credits=6) == 3
    assert placed(keep_registered=True, max_credits=9) == 3
    assert placed(keep_registered=True, max_credits=0, kept_credits=6) == 3
    assert placed(keep_registered=True, max_credits=6, kept_credits=6) == 0

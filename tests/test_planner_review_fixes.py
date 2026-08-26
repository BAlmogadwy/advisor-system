"""Regressions for the sixteen findings of the Timetable Builder review.

Each test pins a defect that shipped, not a happy path. The theme running
through most of them is a single one: the prerequisite rule had grown five
inline copies, and the four that were not the shared helper all silently
treated the curriculum's ``90(HOURS)`` credit gate as a course code that can
never be satisfied.
"""

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import (
    AcademicAdvisor,
    Course,
    Prerequisite,
    ProgrammeRequirement,
    Student,
    StudentCourse,
)
from core.services.eligibility import evaluate_prerequisites
from core.services.rbac import ROLE_ADVISOR, ROLE_SUPER_ADMIN, ensure_role_groups

pytestmark = pytest.mark.django_db

PROG = "TSTP"
YEAR, TERM = "1446", "1"


# ── the shared helper ────────────────────────────────────────────────────────


def test_hour_gate_is_not_treated_as_a_course_code() -> None:
    """The defect in one line: 90(HOURS) is a gate, not a course."""
    out = evaluate_prerequisites(["90(HOURS)"], set(), set(), earned_credits=98)
    assert out.met is True
    assert out.missing == []
    assert out.required_hours == 90


def test_unmet_hour_gate_is_reported_in_curriculum_form() -> None:
    out = evaluate_prerequisites(["90(HOURS)"], set(), set(), earned_credits=40)
    assert out.met is False
    assert out.missing == ["90(HOURS)"]


def test_course_and_hour_prerequisites_are_evaluated_together() -> None:
    out = evaluate_prerequisites(["CS101", "90(HOURS)"], {"CS101"}, set(), earned_credits=98)
    assert out.met is True
    out2 = evaluate_prerequisites(["CS101", "90(HOURS)"], set(), set(), earned_credits=98)
    assert out2.met is False
    assert out2.missing == ["CS101"], "an met hour gate must not appear as missing"


def test_registered_credits_count_only_in_relaxed_mode() -> None:
    relaxed = evaluate_prerequisites(
        ["90(HOURS)"], set(), set(), earned_credits=80, registered_credits=15
    )
    strict = evaluate_prerequisites(
        ["90(HOURS)"],
        set(),
        set(),
        earned_credits=80,
        registered_credits=15,
        strict_passed_only=True,
    )
    assert relaxed.met is True
    assert strict.met is False


# ── the planner recommendation panel (F1) ────────────────────────────────────


@pytest.fixture()
def capstone_student() -> int:
    # ID encodes the join year: 44 -> 1444, so against YEAR=1446 the real term
    # is 4 and the next term 5. CAP490 sits at term 5 so it clears the
    # recommender's parity and not-beyond-next-term filters and actually
    # reaches the panel under test.
    sid = 4490001
    Student.objects.create(
        student_id=sid,
        name="Capstone Student",
        program=PROG,
        section="M",
        status="ACTIVE",
        total_earned_credits=98,
        current_registered_credits=0,
    )
    for code, term in [("BASE101", 1), ("CAP490", 5)]:
        ProgrammeRequirement.objects.create(
            program=PROG, course_code=code, programme_term=term, credit_hours=3
        )
    # The curriculum's own encoding: an hour gate stored as a prerequisite row.
    Prerequisite.objects.create(
        program=PROG, course_code="CAP490", prerequisite_course_code="90(HOURS)"
    )
    course = Course.objects.create(course_code="BASE101", credit_hours=3)
    StudentCourse.objects.create(student_id=sid, course=course, status="passed")
    return sid


def _staff_client(username: str) -> Client:
    ensure_role_groups()
    user, _ = User.objects.get_or_create(username=username)
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client(SERVER_NAME="localhost")
    client.force_login(user)
    return client


def test_planner_marks_an_hour_gated_course_eligible_once_the_gate_is_met(
    capstone_student: int,
) -> None:
    """This shipped as Blocked for 68 live students, all of whom had met the gate.

    The Add button is rendered ``disabled`` whenever status is not Eligible, so
    the recommend-then-add path was closed for exactly the courses it mattered
    most for.
    """
    client = _staff_client("planner-hourgate")
    res = client.post(
        "/ops/planner/context/",
        data={"student_id": str(capstone_student), "academic_year": YEAR, "term": TERM},
        content_type="application/json",
    )
    assert res.status_code == 200
    recs = {r["course_code"].upper(): r for r in res.json().get("recommendations", [])}
    assert "CAP490" in recs, "the capstone should be recommended at all"
    assert recs["CAP490"]["status"] == "Eligible"
    assert recs["CAP490"]["missing_prerequisites"] == []


def test_planner_still_blocks_an_unmet_hour_gate(capstone_student: int) -> None:
    """The converse: the fix must not turn the gate off, only evaluate it."""
    Student.objects.filter(student_id=capstone_student).update(total_earned_credits=40)
    client = _staff_client("planner-hourgate-short")
    res = client.post(
        "/ops/planner/context/",
        data={"student_id": str(capstone_student), "academic_year": YEAR, "term": TERM},
        content_type="application/json",
    )
    recs = {r["course_code"].upper(): r for r in res.json().get("recommendations", [])}
    if "CAP490" in recs:
        assert recs["CAP490"]["status"] == "Blocked"
        assert "90(HOURS)" in recs["CAP490"]["missing_prerequisites"]


def test_planner_context_sends_the_regulatory_ceiling(capstone_student: int) -> None:
    """F5: the UI can only show the legal limit if the payload carries it."""
    client = _staff_client("planner-caps")
    res = client.post(
        "/ops/planner/context/",
        data={"student_id": str(capstone_student), "academic_year": YEAR, "term": TERM},
        content_type="application/json",
    )
    student = res.json()["student"]
    assert student["regulatory_max_credits"] > student["credit_cap"], (
        "the advisory cap and the regulatory ceiling must be distinguishable"
    )


# ── the catalogue authorization gap (F3) ─────────────────────────────────────


@pytest.fixture()
def two_advisers_and_a_student() -> int:
    # email is unique=True, so two blank ones collide.
    AcademicAdvisor.objects.create(
        advisor_id="ADV_MINE", full_name="Mine", email="mine@example.test"
    )
    AcademicAdvisor.objects.create(
        advisor_id="ADV_THEIRS", full_name="Theirs", email="theirs@example.test"
    )
    sid = 4490202
    Student.objects.create(
        student_id=sid,
        name="Someone Else's Student",
        program=PROG,
        section="M",
        status="ACTIVE",
        advisor_id="ADV_THEIRS",
    )
    return sid


def test_catalogue_refuses_a_student_outside_the_callers_scope(
    two_advisers_and_a_student: int,
) -> None:
    """F3: this endpoint was the one sibling that never checked scope.

    Naming a student here reveals their programme and cohort, so it must be
    gated exactly as planner_context_view already is.
    """
    from core.services.rbac import set_user_scope

    ensure_role_groups()
    user, _ = User.objects.get_or_create(username="scoped-adviser")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_ADVISOR))
    set_user_scope(user.id, advisor_id="ADV_MINE")
    client = Client(SERVER_NAME="localhost")
    client.force_login(user)

    body = {
        "student_id": str(two_advisers_and_a_student),
        "academic_year": YEAR,
        "term": TERM,
        "course_codes": [],
    }
    catalog = client.post(
        "/ops/planner/sections-catalog/", data=body, content_type="application/json"
    )
    context = client.post("/ops/planner/context/", data=body, content_type="application/json")
    assert context.status_code == catalog.status_code, (
        "the catalogue must answer an out-of-scope student the same way its "
        f"sibling does (context={context.status_code}, catalog={catalog.status_code})"
    )
    assert catalog.status_code in {403, 404}


def test_catalogue_without_a_student_stays_open_to_staff() -> None:
    """The no-student path is staff browsing the shared catalogue, by design."""
    client = _staff_client("catalog-browse")
    res = client.post(
        "/ops/planner/sections-catalog/",
        data={"academic_year": YEAR, "term": TERM, "course_codes": []},
        content_type="application/json",
    )
    assert res.status_code == 200

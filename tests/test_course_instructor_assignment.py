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
    Instructor,
    ProgrammeRequirement,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.course_instructor_assignment import (
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

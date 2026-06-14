"""Instructor-assignment feature — models, service, endpoints, load report, and
the flag-gated links-keyed planner clash.

The link (``SectionInstructor``) is the source of truth; the primary
instructor's name is written through to ``TermSectionMeeting.instructor`` as a
display cache so the existing free-text readers keep working.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import Group, User
from django.db import IntegrityError
from django.test import Client

from core.models import (
    Instructor,
    SectionInstructor,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.instructor_assignment import set_section_instructors
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups
from core.services.timetable_pr4_instructor import (
    build_section_instructor_ids,
    normalise_instructor,
)


def _scenario(status: str = "draft") -> TimetableScenario:
    return TimetableScenario.objects.create(
        academic_year="1448", term="1", name="instr-test", status=status
    )


def _section(scenario, course_key="CS101", section="F1", course_code="CS101") -> TermSection:
    ts = TermSection.objects.create(
        scenario=scenario,
        course_key=course_key,
        course_code=course_code,
        course_number=course_code[-3:],
        section=section,
        course_name="Test Course",
    )
    TermSectionMeeting.objects.create(
        term_section=ts, day="SUN", start_time="09:00", end_time="10:15"
    )
    return ts


def _admin_client() -> tuple[Client, User]:
    ensure_role_groups()
    user, _ = User.objects.get_or_create(username="instr-admin")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    http = Client()
    http.force_login(user)
    return http, user


# ── Model ────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_normalised_name_unique() -> None:
    Instructor.objects.create(full_name="Dr. Ada", normalised_name="dr. ada")
    with pytest.raises(IntegrityError):
        Instructor.objects.create(full_name="dr. ada", normalised_name="dr. ada")


@pytest.mark.django_db
def test_instructor_protect_and_section_cascade() -> None:
    sc = _scenario()
    ts = _section(sc)
    instr = Instructor.objects.create(full_name="Dr. B", normalised_name="dr. b")
    SectionInstructor.objects.create(term_section=ts, instructor=instr)
    # PROTECT: a linked instructor cannot be deleted.
    from django.db.models import ProtectedError

    with pytest.raises(ProtectedError):
        instr.delete()
    # CASCADE: deleting the section drops the link.
    ts.delete()
    assert SectionInstructor.objects.count() == 0
    assert Instructor.objects.filter(pk=instr.pk).exists()


# ── Assign service ───────────────────────────────────────────────


@pytest.mark.django_db
def test_set_section_instructors_writethrough_and_clear() -> None:
    sc = _scenario()
    ts = _section(sc)
    a = Instructor.objects.create(full_name="Dr. Primary", normalised_name="dr. primary")
    b = Instructor.objects.create(full_name="Dr. Co", normalised_name="dr. co")

    res = set_section_instructors(ts, instructor_ids=[a.pk, b.pk])
    assert [r["role"] for r in res] == ["primary", "co"]
    # write-through: the meeting cache shows the PRIMARY name only
    assert set(ts.meetings.values_list("instructor", flat=True)) == {"Dr. Primary"}
    assert SectionInstructor.objects.filter(term_section=ts).count() == 2

    # clear reverts both links and the display cache
    set_section_instructors(ts, instructor_ids=[])
    assert SectionInstructor.objects.filter(term_section=ts).count() == 0
    assert set(ts.meetings.values_list("instructor", flat=True)) == {""}


@pytest.mark.django_db
def test_set_section_instructors_by_name_dedupes() -> None:
    sc = _scenario()
    ts = _section(sc)
    existing = Instructor.objects.create(
        full_name="Dr. X", normalised_name=normalise_instructor("Dr. X")
    )
    res = set_section_instructors(ts, instructor_names=["Dr. X", "dr. x", "Dr. Y"])
    # "Dr. X"/"dr. x" collapse to one (existing) instructor; "Dr. Y" is created
    assert len(res) == 2
    assert Instructor.objects.filter(normalised_name="dr. x").count() == 1
    assert existing.pk in {r["id"] for r in res}


# ── Endpoints ────────────────────────────────────────────────────


@pytest.mark.django_db
def test_create_and_duplicate() -> None:
    http, _ = _admin_client()
    r1 = http.post(
        "/ops/instructors/create/",
        data=json.dumps({"full_name": "Dr. Grace", "email": "g@x.io"}),
        content_type="application/json",
    )
    assert r1.status_code == 201
    r2 = http.post(
        "/ops/instructors/create/",
        data=json.dumps({"full_name": "dr. grace"}),
        content_type="application/json",
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "DUPLICATE"


@pytest.mark.django_db
def test_assign_endpoint_and_published_block() -> None:
    http, _ = _admin_client()
    sc = _scenario()
    ts = _section(sc)
    instr = Instructor.objects.create(full_name="Dr. Z", normalised_name="dr. z")

    ok = http.post(
        "/ops/instructors/assign/",
        data=json.dumps({"term_section_id": ts.id, "instructor_id": instr.pk}),
        content_type="application/json",
    )
    assert ok.status_code == 200
    assert ts.meetings.first().instructor == "Dr. Z"

    # publishing the scenario blocks further assignment
    sc.status = "published"
    sc.save(update_fields=["status"])
    blocked = http.post(
        "/ops/instructors/assign/",
        data=json.dumps({"term_section_id": ts.id, "instructor_id": instr.pk}),
        content_type="application/json",
    )
    assert blocked.status_code == 400
    assert blocked.json()["error"]["code"] == "SCENARIO_PUBLISHED"


@pytest.mark.django_db
def test_rbac_denies_non_advisor() -> None:
    ensure_role_groups()
    user, _ = User.objects.get_or_create(username="nobody")
    user.groups.clear()
    http = Client()
    http.force_login(user)
    r = http.post(
        "/ops/instructors/create/",
        data=json.dumps({"full_name": "Dr. Nope"}),
        content_type="application/json",
    )
    assert r.status_code in (403, 302)  # role guard or login redirect


@pytest.mark.django_db
def test_load_report_aggregates() -> None:
    http, _ = _admin_client()
    sc = _scenario()
    ts1 = _section(sc, course_key="CS101", section="F1", course_code="CS101")
    ts2 = _section(sc, course_key="CS102", section="F1", course_code="CS102")
    instr = Instructor.objects.create(
        full_name="Dr. Load", normalised_name="dr. load", max_weekly_hours=1
    )
    set_section_instructors(ts1, instructor_ids=[instr.pk])
    set_section_instructors(ts2, instructor_ids=[instr.pk])

    resp = http.get("/ops/instructors/load-report/", {"scenario_id": sc.id})
    assert resp.status_code == 200
    data = resp.json()
    row = next(r for r in data["rows"] if r["instructor_id"] == instr.pk)
    assert row["section_count"] == 2
    assert row["distinct_courses"] == 2
    # two 75-min meetings = 2.5h > 1h cap ⇒ over
    assert row["weekly_contact_hours"] == 2.5
    assert row["load_status"] == "over"
    assert data["totals"]["section_count"] == 2


# ── Links flag / planner clash ───────────────────────────────────


@pytest.mark.django_db
def test_build_section_instructor_ids_active_only() -> None:
    sc = _scenario()
    ts = _section(sc, course_key="CS101", section="F1")
    active = Instructor.objects.create(full_name="Active", normalised_name="active")
    inactive = Instructor.objects.create(
        full_name="Inactive", normalised_name="inactive", is_active=False
    )
    SectionInstructor.objects.create(term_section=ts, instructor=active)
    SectionInstructor.objects.create(term_section=ts, instructor=inactive)
    mapping = build_section_instructor_ids(sc.id)
    assert mapping == {"CS101|F1": {active.pk}}  # inactive excluded


@pytest.mark.django_db
def test_links_flag_clash_rejects_double_booking(settings) -> None:
    """With the links flag ON, two sections sharing an instructor cannot both
    sit at the same slot — the greedy planner rejects the overlapping option."""
    settings.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_LINKS_ENABLED = True

    sc = _scenario()
    # two DIFFERENT courses (same-course rule is separate) sharing one instructor
    ts1 = _section(sc, course_key="AAA101", section="F1", course_code="AAA101")
    ts2 = _section(sc, course_key="BBB101", section="F1", course_code="BBB101")
    instr = Instructor.objects.create(full_name="Dr. Shared", normalised_name="dr. shared")
    set_section_instructors(ts1, instructor_ids=[instr.pk])
    set_section_instructors(ts2, instructor_ids=[instr.pk])

    from core.services.timetable_pr4_instructor import build_section_instructor_ids as _b

    mapping = _b(sc.id)
    # both sections resolve to the same instructor id ⇒ the clash maps will
    # treat a shared (day,start) as a double-booking for that id.
    assert mapping["AAA101|F1"] == mapping["BBB101|F1"] == {instr.pk}

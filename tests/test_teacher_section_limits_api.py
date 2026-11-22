"""Per-teacher section limits (``CourseInstructor.max_sections``): the service,
the course-assignment API, the load report, the DB constraint and the seed."""

from __future__ import annotations

import json
from io import StringIO

import pytest
from django.contrib.auth.models import Group, User
from django.core import serializers
from django.db import IntegrityError, transaction
from django.test import Client

from core.models import CourseInstructor, Instructor, ProgrammeRequirement
from core.services.course_instructor_assignment import (
    serialize_course_instructors,
    set_course_instructors,
)
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups

SET_URL = "/ops/instructors/course-assignments/set/"
LIST_URL = "/ops/instructors/course-assignments/"
REPORT_URL = "/ops/instructors/load-report/"


def _instructor(name: str) -> Instructor:
    return Instructor.objects.create(full_name=name, normalised_name=name.lower())


def _client() -> Client:
    ensure_role_groups()
    user, _ = User.objects.get_or_create(username="limits-admin")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    http = Client()
    http.force_login(user)
    return http


def _post(http: Client, **body):
    payload = {"program": "CS", "section": "M", "course_code": "CS112", **body}
    return http.post(SET_URL, data=json.dumps(payload), content_type="application/json")


def _rows() -> list[tuple[int, str, int | None]]:
    return [
        (r.instructor_id, r.role, r.max_sections)
        for r in CourseInstructor.objects.filter(course_code="CS112").order_by("id")
    ]


@pytest.fixture
def two() -> tuple[Instructor, Instructor]:
    return _instructor("Teacher A"), _instructor("Teacher C")


# ── Service ──────────────────────────────────────────────────────


@pytest.mark.django_db
def test_service_stores_limits_in_order_with_roles(two) -> None:
    a, c = two
    out = set_course_instructors("CS", "CS112", "M", [c.pk, a.pk], {a.pk: 2, c.pk: None})
    assert _rows() == [(c.pk, "primary", None), (a.pk, "co", 2)]
    assert [(r["id"], r["role"], r["max_sections"]) for r in out] == [
        (c.pk, "primary", None),
        (a.pk, "co", 2),
    ]
    assert serialize_course_instructors("CS", "CS112", "M") == out


@pytest.mark.django_db
def test_service_without_limits_stores_none(two) -> None:
    a, c = two
    set_course_instructors("CS", "CS112", "M", [a.pk, c.pk])
    assert [m for _, _, m in _rows()] == [None, None]


# ── Set view: the new form ───────────────────────────────────────


@pytest.mark.django_db
def test_new_form_round_trip_and_list_view(two) -> None:
    a, c = two
    http = _client()
    resp = _post(
        http, instructors=[{"id": a.pk, "max_sections": 2}, {"id": c.pk, "max_sections": None}]
    )
    assert resp.status_code == 200, resp.content
    assert _rows() == [(a.pk, "primary", 2), (c.pk, "co", None)]
    body = resp.json()
    assert [(i["id"], i["max_sections"]) for i in body["instructors"]] == [(a.pk, 2), (c.pk, None)]

    # an entry without the key means "no limit"; a whole-number string is accepted
    resp = _post(http, instructors=[{"id": c.pk}, {"id": a.pk, "max_sections": "3"}])
    assert resp.status_code == 200, resp.content
    assert _rows() == [(c.pk, "primary", None), (a.pk, "co", 3)]

    ProgrammeRequirement.objects.create(
        program="CS", course_code="CS112", course_name="n", programme_term=1, credit_hours=3
    )
    listing = http.get(LIST_URL, {"program": "CS", "section": "M"}).json()
    row = next(r for r in listing["courses"] if r["course_code"] == "CS112")
    assert row["instructor"]["id"] == c.pk and row["instructor"]["max_sections"] is None
    assert [(x["id"], x["max_sections"]) for x in row["co_instructors"]] == [(a.pk, 3)]


@pytest.mark.django_db
def test_legacy_forms_store_no_limit(two) -> None:
    a, c = two
    http = _client()
    # keep a stale limit on the books: a legacy save must not carry it forward
    set_course_instructors("CS", "CS112", "M", [a.pk], {a.pk: 4})
    assert _post(http, instructor_ids=[a.pk, c.pk]).status_code == 200
    assert _rows() == [(a.pk, "primary", None), (c.pk, "co", None)]
    set_course_instructors("CS", "CS112", "M", [a.pk], {a.pk: 4})
    assert _post(http, instructor_id=c.pk).status_code == 200
    assert _rows() == [(c.pk, "primary", None)]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "bad",
    [0, -1, 100, 1000, True, False, "x", "2.5", "-3", "", 2.5, [2], {"a": 1}],
)
def test_bad_limit_is_a_400_naming_the_teacher_and_writes_nothing(two, bad) -> None:
    a, c = two
    set_course_instructors("CS", "CS112", "M", [a.pk], {a.pk: 5})
    before = _rows()
    resp = _post(
        _client(),
        instructors=[{"id": c.pk, "max_sections": None}, {"id": a.pk, "max_sections": bad}],
    )
    assert resp.status_code == 400, bad
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION"
    assert f"instructor {a.pk}" in err["message"]
    assert _rows() == before


@pytest.mark.django_db
@pytest.mark.parametrize("bad_entry", ["7", None, {"max_sections": 2}, {"id": "7"}, {"id": True}])
def test_bad_entry_is_a_400_and_writes_nothing(two, bad_entry) -> None:
    a, _ = two
    set_course_instructors("CS", "CS112", "M", [a.pk], {a.pk: 5})
    before = _rows()
    resp = _post(_client(), instructors=[bad_entry])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION"
    assert _rows() == before
    assert _post(_client(), instructors="nope").status_code == 400
    assert _rows() == before


@pytest.mark.django_db
def test_limits_at_the_edges_are_accepted(two) -> None:
    a, c = two
    resp = _post(
        _client(), instructors=[{"id": a.pk, "max_sections": 1}, {"id": c.pk, "max_sections": 99}]
    )
    assert resp.status_code == 200
    assert _rows() == [(a.pk, "primary", 1), (c.pk, "co", 99)]


@pytest.mark.django_db
def test_unknown_teacher_is_404_and_writes_nothing(two) -> None:
    a, _ = two
    set_course_instructors("CS", "CS112", "M", [a.pk], {a.pk: 5})
    before = _rows()
    resp = _post(
        _client(), instructors=[{"id": a.pk, "max_sections": 2}, {"id": 999999, "max_sections": 1}]
    )
    assert resp.status_code == 404
    assert _rows() == before


@pytest.mark.django_db
def test_empty_new_form_clears_the_course(two) -> None:
    a, _ = two
    set_course_instructors("CS", "CS112", "M", [a.pk], {a.pk: 5})
    assert _post(_client(), instructors=[]).status_code == 200
    assert _rows() == []


@pytest.mark.django_db
def test_bulk_assign_keeps_its_meaning_and_stores_no_limit(two) -> None:
    a, c = two
    set_course_instructors("CS", "CS112", "M", [a.pk, c.pk], {a.pk: 2, c.pk: 3})
    resp = _client().post(
        "/ops/instructors/course-assignments/assign-bulk/",
        data=json.dumps(
            {"program": "CS", "section": "M", "course_codes": ["CS112"], "instructor_id": c.pk}
        ),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert _rows() == [(c.pk, "primary", None)]


# ── Load report ──────────────────────────────────────────────────


@pytest.mark.django_db
def test_load_report_links_carry_max_sections(two) -> None:
    a, c = two
    set_course_instructors("CS", "CS112", "M", [a.pk, c.pk], {a.pk: 2})
    rows = _client().get(REPORT_URL).json()["rows"]
    by_id = {r["instructor_id"]: r for r in rows}
    assert by_id[a.pk]["courses"][0]["max_sections"] == 2
    assert by_id[c.pk]["courses"][0]["max_sections"] is None


# ── Database constraint and release seed ─────────────────────────


@pytest.mark.django_db
@pytest.mark.parametrize("bad", [0, 100])
def test_check_constraint_rejects_out_of_range_on_create_and_update(two, bad) -> None:
    a, _ = two
    with pytest.raises(IntegrityError), transaction.atomic():
        CourseInstructor.objects.bulk_create(
            [
                CourseInstructor(
                    program="CS",
                    course_code="CS112",
                    section="M",
                    instructor=a,
                    role="primary",
                    max_sections=bad,
                )
            ]
        )
    link = CourseInstructor.objects.create(
        program="CS", course_code="CS112", section="M", instructor=a, role="primary", max_sections=1
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        CourseInstructor.objects.filter(pk=link.pk).update(max_sections=bad)
    link.refresh_from_db()
    assert link.max_sections == 1


@pytest.mark.django_db
def test_release_seed_carries_max_sections(two) -> None:
    """The seed uses Django's serializer on every field, so the column travels."""
    from core.management.commands.export_release_seed import ALLOWED_MODELS

    assert "core.courseinstructor" in ALLOWED_MODELS
    a, _ = two
    CourseInstructor.objects.create(
        program="CS", course_code="CS112", section="M", instructor=a, role="primary", max_sections=3
    )
    stream = StringIO()
    serializers.serialize(
        "json",
        CourseInstructor.objects.all(),
        stream=stream,
        use_natural_foreign_keys=True,
        use_natural_primary_keys=False,
    )
    records = json.loads(stream.getvalue())
    assert records[0]["fields"]["max_sections"] == 3
    restored = next(serializers.deserialize("json", stream.getvalue())).object
    assert restored.max_sections == 3

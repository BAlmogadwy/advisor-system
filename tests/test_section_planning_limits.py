"""Saving per-course seat limits from Section Planning is data-safe.

A limit belongs to a programme: a save writes only the programmes on screen,
unless ONE course is explicitly widened to every programme that teaches it.
Nothing is written without a preview listing course · programme · old -> new,
and the commit must carry that preview's token. Every changed row is audited
in the same transaction, and a failed audit write saves nothing.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import NoReverseMatch, reverse

from core.authz import _rate_buckets
from core.models import AuditLog, ProgrammeRequirement
from core.services import audit as audit_service
from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_GENERAL_ADVISOR,
    ensure_role_groups,
    ensure_scope_schema,
    set_user_scope,
)
from core.services.section_limits import (
    AUDIT_ACTION,
    LimitChange,
    LimitConflict,
    apply_limit_writes,
    plan_limit_writes,
)

pytestmark = pytest.mark.django_db

URL = "/ops/section-planning/limits/"


@pytest.fixture(autouse=True)
def _fresh_throttle():
    _rate_buckets.clear()
    yield
    _rate_buckets.clear()


def _login(client: Client, role: str = ROLE_GENERAL_ADVISOR, username: str = "sp-limits") -> User:
    ensure_role_groups()
    ensure_scope_schema()
    user, _ = User.objects.get_or_create(username=username)
    user.groups.clear()
    user.groups.add(Group.objects.get(name=role))
    set_user_scope(user.id, advisor_id="", departments="")
    client.force_login(user)
    return user


def _req(program: str, code: str, cap: int | None) -> ProgrammeRequirement:
    return ProgrammeRequirement.objects.create(
        program=program, course_code=code, credit_hours=3, max_capacity=cap
    )


def _limits() -> dict[tuple[str, str], int | None]:
    return {(r.program, r.course_code): r.max_capacity for r in ProgrammeRequirement.objects.all()}


def _post(client: Client, body: dict):
    return client.post(URL, json.dumps(body), content_type="application/json")


def _save(client: Client, programs: list[str], changes: list[dict]):
    """Preview, then commit with the preview's token (what the page does)."""
    preview = _post(client, {"programs": programs, "changes": changes, "dry_run": True})
    assert preview.status_code == 200, preview.content
    token = preview.json()["preview_token"]
    return preview, _post(
        client,
        {"programs": programs, "changes": changes, "dry_run": False, "preview_token": token},
    )


@pytest.fixture
def shared_courses():
    """CS211 and CS323 are taught by AI, DS and CS, each with its own saved limit."""
    for program, cs211, cs323 in (("AI", 30, None), ("DS", 35, 25), ("CS", None, 40)):
        _req(program, "CS211", cs211)
        _req(program, "CS323", cs323)
    _req("AI", "AI491", 5)


# ── scope ──────────────────────────────────────────────────────────


def test_save_writes_only_the_programmes_on_screen(client: Client, shared_courses) -> None:
    _login(client)
    before = _limits()

    _, response = _save(client, ["AI"], [{"course_code": "CS211", "max_capacity": 28}])

    assert response.status_code == 200, response.content
    after = _limits()
    assert after[("AI", "CS211")] == 28
    # Every other programme's limit, and every other course, is untouched.
    assert {k: v for k, v in after.items() if k != ("AI", "CS211")} == {
        k: v for k, v in before.items() if k != ("AI", "CS211")
    }
    assert response.json()["changed"] == [
        {"course_code": "CS211", "program": "AI", "old": 30, "new": 28, "scope": "programmes"}
    ]


def test_all_programmes_flag_widens_only_that_course(client: Client, shared_courses) -> None:
    _login(client)

    _, response = _save(
        client,
        ["AI"],
        [
            {"course_code": "CS211", "max_capacity": 32, "all_programmes": True},
            {"course_code": "CS323", "max_capacity": 20},
        ],
    )

    assert response.status_code == 200, response.content
    after = _limits()
    assert [after[(p, "CS211")] for p in ("AI", "CS", "DS")] == [32, 32, 32]
    # CS323 was not flagged: only the programme on screen changes.
    assert after[("AI", "CS323")] == 20
    assert after[("DS", "CS323")] == 25
    assert after[("CS", "CS323")] == 40
    scopes = {(c["course_code"], c["program"]): c["scope"] for c in response.json()["changed"]}
    assert scopes == {
        ("CS211", "AI"): "all_programmes",
        ("CS211", "CS"): "all_programmes",
        ("CS211", "DS"): "all_programmes",
        ("CS323", "AI"): "programmes",
    }


def test_several_programmes_on_screen_are_each_written(client: Client, shared_courses) -> None:
    _login(client)

    _, response = _save(client, ["AI", "DS"], [{"course_code": "CS323", "max_capacity": 22}])

    assert response.status_code == 200
    after = _limits()
    assert (after[("AI", "CS323")], after[("DS", "CS323")], after[("CS", "CS323")]) == (22, 22, 40)


def test_a_course_the_programmes_on_screen_do_not_teach_is_refused(
    client: Client, shared_courses
) -> None:
    _login(client)
    before = _limits()

    response = _post(
        client,
        {
            "programs": ["DS"],
            "changes": [{"course_code": "AI491", "max_capacity": 9, "all_programmes": True}],
            "dry_run": True,
        },
    )

    assert response.status_code == 400
    assert response.json()["code"] == "course_not_in_programmes"
    assert response.json()["course_code"] == "AI491"
    assert _limits() == before


# ── the preview is what gets written ───────────────────────────────


def test_preview_lists_course_programme_old_and_new_and_writes_nothing(
    client: Client, shared_courses
) -> None:
    _login(client)
    before = _limits()

    response = _post(
        client,
        {
            "programs": ["AI", "DS"],
            "changes": [
                {"course_code": "CS211", "max_capacity": 35},
                {"course_code": "CS323", "max_capacity": None},
            ],
            "dry_run": True,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["dry_run"] is True
    # Rows that CHANGE, not rows matched: DS CS211 already holds 35, AI CS323 is empty.
    assert data["changes"] == [
        {"course_code": "CS211", "program": "AI", "old": 30, "new": 35, "scope": "programmes"},
        {"course_code": "CS323", "program": "DS", "old": 25, "new": None, "scope": "programmes"},
    ]
    assert data["unchanged"] == 2
    assert len(data["preview_token"]) == 64
    assert _limits() == before
    assert not AuditLog.objects.filter(action=AUDIT_ACTION).exists()


def test_commit_without_the_preview_token_writes_nothing(client: Client, shared_courses) -> None:
    _login(client)
    before = _limits()

    response = _post(
        client, {"programs": ["AI"], "changes": [{"course_code": "CS211", "max_capacity": 20}]}
    )

    assert response.status_code == 409
    assert response.json()["code"] == "preview_stale"
    assert _limits() == before


def test_commit_after_the_rows_moved_is_refused(client: Client, shared_courses) -> None:
    _login(client)
    changes = [{"course_code": "CS211", "max_capacity": 20}]
    preview = _post(client, {"programs": ["AI"], "changes": changes, "dry_run": True}).json()
    # Someone else saves AI CS211 between the preview and the commit.
    ProgrammeRequirement.objects.filter(program="AI", course_code="CS211").update(max_capacity=33)

    response = _post(
        client,
        {
            "programs": ["AI"],
            "changes": changes,
            "dry_run": False,
            "preview_token": preview["preview_token"],
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "preview_stale"
    # The fresh preview is returned so the page can show what WOULD change now.
    assert response.json()["changes"][0]["old"] == 33
    assert _limits()[("AI", "CS211")] == 33


def test_removing_a_saved_limit_is_an_explicit_change(client: Client, shared_courses) -> None:
    _login(client)

    _, response = _save(client, ["DS"], [{"course_code": "CS323", "max_capacity": None}])

    assert response.status_code == 200
    assert _limits()[("DS", "CS323")] is None
    assert response.json()["changed"] == [
        {"course_code": "CS323", "program": "DS", "old": 25, "new": None, "scope": "programmes"}
    ]


# ── audit, fail-closed ─────────────────────────────────────────────


def test_every_changed_row_is_audited(client: Client, shared_courses) -> None:
    _login(client, username="limit-auditor")

    _, response = _save(
        client, ["AI"], [{"course_code": "CS211", "max_capacity": 31, "all_programmes": True}]
    )

    assert response.status_code == 200
    rows = list(AuditLog.objects.filter(action=AUDIT_ACTION).order_by("id"))
    assert len(rows) == 3
    details = [json.loads(row.details_json) for row in rows]
    assert [(d["program"], d["old"], d["new"]) for d in details] == [
        ("AI", 30, 31),
        ("CS", None, 31),
        ("DS", 35, 31),
    ]
    assert {d["course_code"] for d in details} == {"CS211"}
    assert {d["scope"] for d in details} == {"all_programmes"}
    assert {d["programs_on_screen"][0] for d in details} == {"AI"}
    assert [d["position"] for d in details] == [1, 2, 3]
    assert {row.actor_username for row in rows} == {"limit-auditor"}
    assert {row.endpoint for row in rows} == {URL}
    assert {row.status for row in rows} == {"success"}


def test_a_failed_audit_write_saves_nothing(
    client: Client, shared_courses, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(client)
    before = _limits()
    calls = []
    real_append = audit_service._append_audit_row

    def failing_second(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise RuntimeError("audit store down")
        return real_append(**kwargs)

    monkeypatch.setattr(audit_service, "_append_audit_row", failing_second)

    _, response = _save(
        client, ["AI"], [{"course_code": "CS211", "max_capacity": 29, "all_programmes": True}]
    )

    assert response.status_code == 503
    assert response.json()["code"] == "audit_unavailable"
    # The first row was written and audited before the second audit failed:
    # both are rolled back together.
    assert len(calls) == 2
    assert _limits() == before
    assert not AuditLog.objects.filter(action=AUDIT_ACTION).exists()


def test_apply_refuses_a_row_that_no_longer_holds_the_previewed_value(shared_courses) -> None:
    writes, _ = plan_limit_writes(
        ["AI", "DS"], [LimitChange(course_code="CS211", max_capacity=21, all_programmes=False)]
    )
    ProgrammeRequirement.objects.filter(program="DS", course_code="CS211").update(max_capacity=99)
    audited = []

    with pytest.raises(LimitConflict):
        apply_limit_writes(writes, audit=lambda w, i, n: audited.append(w))

    # AI was updated first, then DS failed its condition: all of it is rolled back.
    assert _limits()[("AI", "CS211")] == 30
    assert _limits()[("DS", "CS211")] == 99
    assert len(audited) == 1


# ── validation ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"changes": [{"course_code": "CS211", "max_capacity": 20}]}, "programs_required"),
        (
            {"programs": [], "changes": [{"course_code": "CS211", "max_capacity": 20}]},
            "programs_required",
        ),
        (
            {"programs": ["XYZ"], "changes": [{"course_code": "CS211", "max_capacity": 20}]},
            "unknown_program",
        ),
        ({"programs": ["AI"], "changes": []}, "changes_required"),
        ({"programs": ["AI"], "changes": [{"max_capacity": 20}]}, "invalid_course"),
        (
            {"programs": ["AI"], "changes": [{"course_code": "CS211", "max_capacity": 0}]},
            "invalid_limit",
        ),
        (
            {"programs": ["AI"], "changes": [{"course_code": "CS211", "max_capacity": 501}]},
            "invalid_limit",
        ),
        (
            {"programs": ["AI"], "changes": [{"course_code": "CS211", "max_capacity": "30"}]},
            "invalid_limit",
        ),
        (
            {"programs": ["AI"], "changes": [{"course_code": "CS211", "max_capacity": True}]},
            "invalid_limit",
        ),
        (
            {"programs": ["AI"], "changes": [{"course_code": "CS211", "max_capacity": 2.5}]},
            "invalid_limit",
        ),
        ({"programs": ["AI"], "changes": [{"course_code": "CS211"}]}, "invalid_limit"),
        (
            {
                "programs": ["AI"],
                "changes": [{"course_code": "CS211", "max_capacity": 20, "all_programmes": "yes"}],
            },
            "invalid_scope",
        ),
        (
            {
                "programs": ["AI"],
                "changes": [
                    {"course_code": "CS211", "max_capacity": 20},
                    {"course_code": " cs211", "max_capacity": 21},
                ],
            },
            "duplicate_course",
        ),
    ],
)
def test_invalid_requests_are_refused_with_a_code(
    client: Client, shared_courses, body: dict, code: str
) -> None:
    _login(client)
    before = _limits()

    response = _post(client, {**body, "dry_run": True})

    assert response.status_code == 400
    assert response.json()["code"] == code
    assert _limits() == before


def test_limits_bounds_are_inclusive(client: Client, shared_courses) -> None:
    _login(client)

    for value in (1, 500):
        _, response = _save(client, ["AI"], [{"course_code": "AI491", "max_capacity": value}])
        assert response.status_code == 200
        assert _limits()[("AI", "AI491")] == value


def test_saving_needs_a_general_advisor(client: Client, shared_courses) -> None:
    body = json.dumps(
        {"programs": ["AI"], "changes": [{"course_code": "CS211", "max_capacity": 20}]}
    )
    assert client.post(URL, body, content_type="application/json").status_code == 401
    _login(client, role=ROLE_ADVISOR, username="plain-advisor")
    assert client.post(URL, body, content_type="application/json").status_code == 403
    assert _limits()[("AI", "CS211")] == 30


def test_the_unscoped_write_endpoints_are_gone(client: Client) -> None:
    """The per-field save and the every-programme bulk save no longer exist."""
    for name in ("section_plan_save_capacity", "section_plan_save_overrides_bulk"):
        with pytest.raises(NoReverseMatch):
            reverse(name)
    _login(client)
    for url in (
        "/ops/section-planning/save-capacity/",
        "/ops/section-planning/save-overrides-bulk/",
    ):
        assert client.post(url, "{}", content_type="application/json").status_code == 404

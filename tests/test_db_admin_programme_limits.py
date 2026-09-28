"""DB Admin's "Programme Capacities" panel saves seat limits through Section Planning's path.

The panel used to POST every row of a programme to its own endpoint, which
wrote each code's value (an emptied field removed a limit), unaudited per row
and without a preview. It now calls ``section_limits.save_limits`` through a
super-admin view: one loaded programme, never widened, previewed, confirmed,
committed with the preview's token, and every changed row audited exactly like
a Section Planning save, marked ``"source": "db_admin"``.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import NoReverseMatch, reverse

from core.authz import _rate_buckets
from core.models import AuditLog, ProgrammeRequirement
from core.services import audit as audit_service
from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_EXAM_COMMITTEE,
    ROLE_GENERAL_ADVISOR,
    ROLE_SUPER_ADMIN,
)
from core.services.section_limits import AUDIT_ACTION
from tests.test_section_planning_limits import _limits, _login

pytestmark = pytest.mark.django_db

URL = "/ops/db/programme-capacities/limits/"
LIST_URL = "/ops/db/programme-capacities/"
SECTION_PLANNING_URL = "/ops/section-planning/limits/"


@pytest.fixture(autouse=True)
def _fresh_throttle():
    _rate_buckets.clear()
    yield
    _rate_buckets.clear()


@pytest.fixture
def programmes():
    """AI492 is a graduation project in AI and DS, and co-op training in AI2.

    CS211 is taught by AI and DS, each with its own limit.
    """
    for program, name, cap in (
        ("AI", "GRADUATION PROJECT II", 5),
        ("AI2", "COOPERATIVE TRAINING (CONTINUING WITH SUMMER)", 5),
        ("DS", "Graduation  project II", None),
    ):
        ProgrammeRequirement.objects.create(
            program=program, course_code="AI492", course_name=name, credit_hours=3, max_capacity=cap
        )
    for program, cap in (("AI", 30), ("DS", 35)):
        ProgrammeRequirement.objects.create(
            program=program,
            course_code="CS211",
            course_name="DATA STRUCTURES",
            credit_hours=3,
            max_capacity=cap,
        )


def _admin(client: Client, username: str = "db-admin") -> User:
    return _login(client, role=ROLE_SUPER_ADMIN, username=username)


def _post(client: Client, body: dict, url: str = URL):
    return client.post(url, json.dumps(body), content_type="application/json")


def _save(client: Client, program: str, changes: list[dict], url: str = URL):
    """Preview, then commit with the preview's token (what the panel does)."""
    preview = _post(client, {"programs": [program], "changes": changes, "dry_run": True}, url)
    assert preview.status_code == 200, preview.content
    token = preview.json()["preview_token"]
    commit = _post(
        client,
        {"programs": [program], "changes": changes, "dry_run": False, "preview_token": token},
        url,
    )
    return preview, commit


# ── the old write path is gone ─────────────────────────────────────


def test_the_old_save_all_endpoint_is_gone(client: Client, programmes) -> None:
    with pytest.raises(NoReverseMatch):
        reverse("db_update_programme_capacities")
    _admin(client)
    before = _limits()

    response = client.post(
        "/ops/db/update-programme-capacities/",
        json.dumps({"program": "AI", "capacities": {"AI492": 9, "CS211": None}}),
        content_type="application/json",
    )

    assert response.status_code == 404
    assert _limits() == before


# ── the list the panel loads ───────────────────────────────────────


def test_the_list_names_each_course_and_answers_the_stored_programme(
    client: Client, programmes
) -> None:
    _admin(client)

    response = client.get(LIST_URL, {"program": " ai "})

    assert response.status_code == 200
    data = response.json()
    assert data["program"] == "AI"
    assert data["rows"] == [
        {
            "course_code": "AI492",
            "course_name": "GRADUATION PROJECT II",
            "credit_hours": 3,
            "max_capacity": 5,
        },
        {
            "course_code": "CS211",
            "course_name": "DATA STRUCTURES",
            "credit_hours": 3,
            "max_capacity": 30,
        },
    ]


def test_an_unknown_programme_lists_nothing(client: Client, programmes) -> None:
    _admin(client)

    response = client.get(LIST_URL, {"program": "XYZ"})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "program": "XYZ", "rows": []}


# ── only the edited course, of the loaded programme ────────────────


def test_a_save_writes_only_the_edited_course_of_the_loaded_programme(
    client: Client, programmes
) -> None:
    """AI492 is also in AI2 (another course) and DS (the same course): neither moves."""
    _admin(client)
    before = _limits()

    preview, response = _save(client, "AI", [{"course_code": "AI492", "max_capacity": 8}])

    assert response.status_code == 200, response.content
    assert preview.json()["changes"] == [
        {
            "course_code": "AI492",
            "course_name": "GRADUATION PROJECT II",
            "program": "AI",
            "old": 5,
            "new": 8,
            "scope": "programmes",
        }
    ]
    after = _limits()
    assert after[("AI", "AI492")] == 8
    assert {k: v for k, v in after.items() if k != ("AI", "AI492")} == {
        k: v for k, v in before.items() if k != ("AI", "AI492")
    }


def test_the_panel_cannot_widen_a_change_to_every_programme(client: Client, programmes) -> None:
    _admin(client)
    before = _limits()

    response = _post(
        client,
        {
            "programs": ["AI"],
            "changes": [{"course_code": "AI492", "max_capacity": 8, "all_programmes": True}],
            "dry_run": True,
        },
    )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_scope"
    assert response.json()["course_code"] == "AI492"
    assert _limits() == before


def test_the_panel_saves_one_programme_at_a_time(client: Client, programmes) -> None:
    _admin(client)
    before = _limits()

    response = _post(
        client,
        {
            "programs": ["AI", "DS"],
            "changes": [{"course_code": "CS211", "max_capacity": 20}],
            "dry_run": True,
        },
    )

    assert response.status_code == 400
    assert response.json()["code"] == "programs_required"
    assert _limits() == before


def test_a_course_the_loaded_programme_does_not_teach_is_refused(
    client: Client, programmes
) -> None:
    """Rows loaded for AI, box changed to AI2 by a stale page: AI's courses are not AI2's."""
    ProgrammeRequirement.objects.create(program="AI2", course_code="AI101", credit_hours=3)
    _admin(client)
    before = _limits()

    response = _post(
        client,
        {
            "programs": ["AI2"],
            "changes": [{"course_code": "CS211", "max_capacity": 20}],
            "dry_run": True,
        },
    )

    assert response.status_code == 400
    assert response.json()["code"] == "course_not_in_programmes"
    assert _limits() == before


def test_removing_a_limit_is_previewed_as_a_removal(client: Client, programmes) -> None:
    _admin(client)

    preview, response = _save(client, "DS", [{"course_code": "CS211", "max_capacity": None}])

    assert [(c["course_code"], c["old"], c["new"]) for c in preview.json()["changes"]] == [
        ("CS211", 35, None)
    ]
    assert response.status_code == 200
    assert _limits()[("DS", "CS211")] is None
    assert _limits()[("AI", "CS211")] == 30


def test_a_commit_needs_the_preview_token(client: Client, programmes) -> None:
    _admin(client)
    before = _limits()

    response = _post(
        client, {"programs": ["AI"], "changes": [{"course_code": "CS211", "max_capacity": 20}]}
    )

    assert response.status_code == 409
    assert response.json()["code"] == "preview_stale"
    assert _limits() == before


@pytest.mark.parametrize(
    ("value", "code"),
    [("30", "invalid_limit"), (0, "invalid_limit"), (501, "invalid_limit"), (2.5, "invalid_limit")],
)
def test_bad_values_are_refused_with_a_code_never_a_server_error(
    client: Client, programmes, value: object, code: str
) -> None:
    _admin(client)
    before = _limits()

    response = _post(
        client,
        {"programs": ["AI"], "changes": [{"course_code": "CS211", "max_capacity": value}]},
    )

    assert response.status_code == 400
    assert response.json()["code"] == code
    assert _limits() == before


def test_a_body_that_is_not_json_is_refused(client: Client, programmes) -> None:
    _admin(client)

    response = client.post(URL, "{not json", content_type="application/json")

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_json"


# ── audit: the same rows as Section Planning, marked db_admin ──────


def test_every_change_is_audited_like_a_section_planning_save(client: Client, programmes) -> None:
    _admin(client, username="db-limit-auditor")

    _, response = _save(
        client,
        "AI",
        [
            {"course_code": "AI492", "max_capacity": 7},
            {"course_code": "CS211", "max_capacity": None},
        ],
    )

    assert response.status_code == 200, response.content
    rows = list(AuditLog.objects.filter(action=AUDIT_ACTION).order_by("id"))
    details = [json.loads(row.details_json) for row in rows]
    assert [
        (d["course_code"], d["course_name"], d["program"], d["old"], d["new"]) for d in details
    ] == [
        ("AI492", "GRADUATION PROJECT II", "AI", 5, 7),
        ("CS211", "DATA STRUCTURES", "AI", 30, None),
    ]
    assert {d["source"] for d in details} == {"db_admin"}
    assert [(d["position"], d["of"]) for d in details] == [(1, 2), (2, 2)]
    assert {row.actor_username for row in rows} == {"db-limit-auditor"}
    assert {row.actor_role for row in rows} == {ROLE_SUPER_ADMIN}
    assert {row.endpoint for row in rows} == {URL}
    assert {row.status for row in rows} == {"success"}

    # A Section Planning save of the same kind writes rows with the same shape.
    _save(client, "DS", [{"course_code": "CS211", "max_capacity": 36}], url=SECTION_PLANNING_URL)
    planning = json.loads(AuditLog.objects.filter(action=AUDIT_ACTION).latest("id").details_json)
    assert planning["source"] == "section_planning"
    assert set(planning) == set(details[0])


def test_a_failed_audit_write_saves_nothing(
    client: Client, programmes, monkeypatch: pytest.MonkeyPatch
) -> None:
    _admin(client)
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
        client,
        "AI",
        [{"course_code": "AI492", "max_capacity": 9}, {"course_code": "CS211", "max_capacity": 31}],
    )

    assert response.status_code == 503
    assert response.json()["code"] == "audit_unavailable"
    assert len(calls) == 2
    assert _limits() == before
    assert not AuditLog.objects.filter(action=AUDIT_ACTION).exists()


# ── super admins only ──────────────────────────────────────────────


def test_only_a_super_admin_reaches_the_panel(client: Client, programmes) -> None:
    body = json.dumps(
        {
            "programs": ["AI"],
            "changes": [{"course_code": "CS211", "max_capacity": 20}],
            "dry_run": True,
        }
    )
    assert client.post(URL, body, content_type="application/json").status_code == 401
    assert client.get(LIST_URL, {"program": "AI"}).status_code == 401
    for role in (ROLE_GENERAL_ADVISOR, ROLE_ADVISOR, ROLE_EXAM_COMMITTEE):
        _login(client, role=role, username=f"not-admin-{role.lower()}")
        assert client.post(URL, body, content_type="application/json").status_code == 403, role
        assert client.get(LIST_URL, {"program": "AI"}).status_code == 403, role
        assert client.get("/db-admin/").status_code in (302, 403), role
    assert _limits()[("AI", "CS211")] == 30
    assert not AuditLog.objects.filter(action=AUDIT_ACTION).exists()


def test_the_page_offers_the_reviewed_save_in_arabic(client: Client) -> None:
    _admin(client)

    html = client.get("/db-admin/", HTTP_ACCEPT_LANGUAGE="ar").content.decode("utf-8")

    assert 'lang="ar"' in html
    assert "حفظ التغييرات" in html
    assert "حفظ الكل" not in html


def test_the_page_offers_the_reviewed_save_in_english(client: Client) -> None:
    _admin(client)

    html = client.get("/db-admin/").content.decode("utf-8")

    assert "Save changes" in html
    assert "Save All" not in html

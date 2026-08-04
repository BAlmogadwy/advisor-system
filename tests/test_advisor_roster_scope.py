"""Who may read an adviser's roster. Fail closed.

Both views that serve a roster computed the caller's scope inline, and both wrote
the same guard::

    forced_advisor_id = str(scope.get("advisor_id", "")).strip() if role != SUPER else ""
    if forced_advisor_id and advisor_id != forced_advisor_id:   # <- falsy
        return 403

`rbac.get_user_role` returns `ROLE_ADVISOR` as the DEFAULT for any authenticated
non-superuser outside the STUDENT and GENERAL_ACADEMIC_ADVISOR groups, and a user
with no `UserScope` row gets `advisor_id = ""`. Blank is falsy, so the comparison
was skipped and the request was served.

Measured before the fix: an account created with `User.objects.create_user(...)`
and nothing else — no groups, no scope row, `is_staff=False` — read another
adviser's full roster by naming them in the query string. `role_required` checks
only `is_authenticated`, so `is_staff` never entered into it.

The same shape sat in `allowed_departments`: `if allowed_departments:` treated
"scoped to no departments" as "not scoped at all".
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import AcademicAdvisor, Student
from core.services.rbac import (
    ROLE_GENERAL_ADVISOR,
    ROLE_SUPER_ADMIN,
    ensure_role_groups,
    set_user_scope,
)

pytestmark = pytest.mark.django_db

MINE = "ADV_MINE"
THEIRS = "ADV_THEIRS"
JSON_URL = "/report/students-by-advisor/"
CSV_URL = "/export/students-by-advisor.csv"


@pytest.fixture
def rosters():
    ensure_role_groups()
    for advisor, sids, program in ((MINE, (770001, 770002), "AI"), (THEIRS, (770003,), "DS")):
        AcademicAdvisor.objects.update_or_create(
            advisor_id=advisor,
            defaults={
                "full_name": advisor,
                "email": f"{advisor}@example.edu",
                "department": program,
            },
        )
        for sid in sids:
            Student.objects.update_or_create(
                student_id=sid,
                defaults={
                    "name": f"S{sid}",
                    "program": program,
                    "section": "M",
                    "advisor_id": advisor,
                    "status": "ACTIVE",
                },
            )
    yield


def _client(username, *, group=None, advisor_id=None, departments=None):
    user = User.objects.create_user(username=username, password="x")
    if group:
        user.groups.add(Group.objects.get(name=group))
    if advisor_id is not None or departments is not None:
        set_user_scope(user.id, advisor_id=advisor_id or "", departments=departments or "")
    client = Client()
    client.force_login(user)
    return client


def _ids(response):
    return [i.get("student_id") for i in (json.loads(response.content).get("items") or [])]


# ── the bypass ───────────────────────────────────────────────────


def test_an_account_with_no_groups_and_no_scope_reads_nothing(rosters):
    """The measured bypass. No groups, no UserScope, not staff — just logged in.

    `get_user_role` defaults it to ADVISOR and `get_user_scope` gives it a blank
    advisor_id, which the old falsy guard read as "impose no restriction".
    """
    client = _client("nobody")
    response = client.get(JSON_URL, {"advisor_id": THEIRS})
    assert response.status_code == 403, f"read {_ids(response)}"


def test_the_same_account_cannot_take_the_csv_either(rosters):
    """The export answered a scope mismatch with 200 and a header-only file, because
    the view read `mapping_ready` and discarded `payload["error"]`. A refusal that
    downloads looks exactly like an adviser with no students."""
    client = _client("nobody_csv")
    response = client.get(CSV_URL, {"advisor_id": THEIRS})
    assert response.status_code == 403, response.content[:200]


def test_an_adviser_with_a_blank_advisor_id_is_refused_by_name(rosters):
    """Explicitly scoped to nothing, rather than merely unscoped. Same answer, and
    the message has to say what is wrong — this is a provisioning fault, and the
    person hitting it can do nothing about it without being told."""
    client = _client("blank", advisor_id="")
    response = client.get(JSON_URL, {"advisor_id": THEIRS})
    assert response.status_code == 403
    assert "advisor id" in json.loads(response.content)["error"].lower()


# ── the scoping that must still work ─────────────────────────────


def test_an_adviser_reads_their_own_roster(rosters):
    client = _client("mine", advisor_id=MINE)
    response = client.get(JSON_URL, {"advisor_id": MINE})
    assert response.status_code == 200
    assert sorted(_ids(response)) == [770001, 770002]


def test_an_adviser_is_refused_someone_elses(rosters):
    client = _client("mine2", advisor_id=MINE)
    assert client.get(JSON_URL, {"advisor_id": THEIRS}).status_code == 403


def test_a_super_admin_reads_any_roster(rosters):
    client = _client("boss", group=ROLE_SUPER_ADMIN)
    response = client.get(JSON_URL, {"advisor_id": THEIRS})
    assert response.status_code == 200
    assert _ids(response) == [770003]


# ── departments: scoped to none is not scoped to all ─────────────


def test_a_general_adviser_sees_only_their_departments(rosters):
    client = _client("gen", group=ROLE_GENERAL_ADVISOR, departments="AI")
    response = client.get(JSON_URL, {"advisor_id": MINE})
    assert response.status_code == 200
    assert sorted(_ids(response)) == [770001, 770002]

    # THEIRS is a DS roster; the department filter must empty it, not pass it through.
    other = client.get(JSON_URL, {"advisor_id": THEIRS})
    assert other.status_code == 200
    assert _ids(other) == [], "a DS roster reached an AI-only general adviser"


def test_a_general_adviser_with_no_departments_is_refused(rosters):
    """`if allowed_departments:` treated `[]` as "no filter", so a general adviser
    with an empty scope row saw every department instead of none."""
    client = _client("gen_blank", group=ROLE_GENERAL_ADVISOR, departments="")
    response = client.get(JSON_URL, {"advisor_id": THEIRS})
    assert response.status_code == 403
    assert "department" in json.loads(response.content)["error"].lower()


# ── the two views cannot drift apart again ───────────────────────


def test_both_views_resolve_scope_through_the_one_helper():
    """They each had their own copy, and both copies carried the same falsy guard.

    Asserted against the source because that is where the duplication lived: a
    behavioural test passes just as well with the logic pasted twice, which is the
    state that produced this defect.
    """
    import pathlib

    for path in ("core/advisor_views.py", "core/report_views.py"):
        source = pathlib.Path(path).read_text(encoding="utf-8")
        assert "resolve_roster_scope(" in source, path
        assert "if forced_advisor_id and advisor_id != forced_advisor_id" not in source, (
            f"{path} has re-grown its own copy of the scope check"
        )


# ── the service's own guard, reached directly ────────────────────


def test_the_service_treats_an_empty_allow_list_as_matching_nothing(rosters):
    """Defence in depth, tested at its own level.

    The view refuses a departmentless general adviser before the service is
    reached, so `allowed_departments=[]` never arrives through HTTP — and a
    service-level mutant restoring `if allowed_departments:` survived every
    endpoint test. This calls the service directly, which is the only place that
    distinction is observable.

    `None` means "not department scoped". `[]` means "scoped to no department".
    Collapsing them is what served everything to a caller entitled to nothing.
    """
    from core.services.advisors import list_students_by_advisor

    unscoped = list_students_by_advisor(MINE, allowed_departments=None)
    assert [i["student_id"] for i in unscoped["items"]] == [770001, 770002]

    scoped_to_nothing = list_students_by_advisor(MINE, allowed_departments=[])
    assert scoped_to_nothing["items"] == [], "an empty allow-list passed rows through"

    scoped_elsewhere = list_students_by_advisor(MINE, allowed_departments=["DS"])
    assert scoped_elsewhere["items"] == []

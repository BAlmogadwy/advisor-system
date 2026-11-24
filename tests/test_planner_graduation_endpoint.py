"""Staff Planner graduation projections are bound to the exact built alternatives."""

from unittest.mock import Mock

import pytest
from django.contrib.auth.models import Group, User
from django.http import JsonResponse
from django.test import Client
from django.urls import reverse

from core.models import Student, StudentTermSection, TermSection, TermSectionMeeting
from core.services.rbac import ROLE_STUDENT, set_user_scope

pytestmark = pytest.mark.django_db

YEAR, TERM = "1448", "1"
SID = 4498410
ADVISOR_ID = "planner-projection-advisor"


@pytest.fixture
def advisor_client(monkeypatch: pytest.MonkeyPatch) -> Client:
    monkeypatch.setattr("core.authz._rate_buckets", {})
    Student.objects.create(
        student_id=SID, name="Planner Student", program="CS", advisor_id=ADVISOR_ID, section="M"
    )
    user = User.objects.create_user(username=ADVISOR_ID)
    set_user_scope(user.pk, advisor_id=ADVISOR_ID)
    client = Client(SERVER_NAME="localhost")
    client.force_login(user)
    return client


@pytest.fixture
def retained_section() -> StudentTermSection:
    section = TermSection.objects.create(
        course_code="CS", course_number="100", course_key="CS100", section="M1"
    )
    TermSectionMeeting.objects.create(
        term_section=section, day="MON", start_time="08:00", end_time="08:50"
    )
    return StudentTermSection.objects.create(
        student_id=SID, academic_year=YEAR, term=TERM, term_section=section, source="planner"
    )


@pytest.fixture
def built_options(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    options = [
        {"name": name, "mappings": [{"term_section_id": pk, "course_key": code}]}
        for name, pk, code in [("A1", 11, "CS101"), ("A2", 12, "CS101"), ("B1", 23, "CS102")]
    ]
    monkeypatch.setattr("core.planner_views.build_plans", lambda *a, **kw: {"options": options})
    return options


@pytest.fixture
def projection(monkeypatch: pytest.MonkeyPatch, built_options: list[dict]) -> Mock:
    mock = Mock(return_value={"options": {option["name"]: {} for option in built_options}})
    monkeypatch.setattr("core.services.planner_graduation.compare_planner_graduation", mock)
    return mock


def _build_token(client: Client, mode: str = "keep") -> str:
    response = client.post(
        reverse("planner_build"),
        {
            "student_id": SID,
            "academic_year": YEAR,
            "term": TERM,
            "mode": mode,
            "baseline": [{"course_code": "UNTRUSTED"}],
        },
        content_type="application/json",
    )
    assert response.status_code == 200
    return response.json()["graduation_projection_token"]


def _project(client: Client, token: str, **other_fields: object) -> JsonResponse:
    return client.post(
        reverse("planner_graduation_projection"),
        {"token": token, **other_fields},
        content_type="application/json",
    )


@pytest.mark.parametrize("mode", ["keep", "ignore"])
def test_projects_all_server_built_variants_and_only_keeps_the_effective_baseline(
    advisor_client: Client,
    built_options: list[dict],
    retained_section: StudentTermSection,
    projection: Mock,
    mode: str,
) -> None:
    token = _build_token(advisor_client, mode)
    projection.assert_not_called()
    before = list(StudentTermSection.objects.values())

    response = _project(advisor_client, token, student_id=1, options=[], term=2)

    assert response.status_code == 200
    assert "no-store" in response.headers["Cache-Control"]
    assert set(response.json()["options"]) == {"A1", "A2", "B1"}
    projection.assert_called_once()
    args = projection.call_args.kwargs
    kept = args.pop("retained_baseline")
    assert args == {
        "student_id": SID,
        "year": int(YEAR),
        "term": int(TERM),
        "options": built_options,
    }
    if mode == "keep":
        assert [row["term_section_id"] for row in kept] == [retained_section.term_section_id]
    else:
        assert kept is None
    assert list(StudentTermSection.objects.values()) == before


def test_rejects_tampered_and_expired_tokens(
    advisor_client: Client, projection: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = _build_token(advisor_client)
    response = _project(advisor_client, token + ".tampered")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PROJECTION_TOKEN_INVALID"

    monkeypatch.setattr("core.planner_views._PROJECTION_TOKEN_MAX_AGE", -1)
    response = _project(advisor_client, token)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROJECTION_BUILD_EXPIRED"
    projection.assert_not_called()


def test_requires_the_building_advisor_and_current_student_scope(
    advisor_client: Client, projection: Mock
) -> None:
    token = _build_token(advisor_client)
    other_client = Client(SERVER_NAME="localhost")
    assert _project(other_client, token).status_code == 302

    other_user = User.objects.create_user(username="other-planner-projection-advisor")
    other_client.force_login(other_user)
    set_user_scope(other_user.pk, advisor_id=ADVISOR_ID)
    response = _project(other_client, token)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PROJECTION_SCOPE_MISMATCH"

    group, _ = Group.objects.get_or_create(name=ROLE_STUDENT)
    other_user.groups.add(group)
    response = _project(other_client, token)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "STAFF_ROLE_REQUIRED"

    Student.objects.filter(student_id=SID).update(advisor_id="new-advisor")
    response = _project(advisor_client, token)
    assert response.status_code == 403
    assert response.json()["reason_code"] == "STUDENT_SCOPE_ADVISOR_MISMATCH"
    projection.assert_not_called()


def test_refuses_to_forecast_from_a_changed_kept_timetable(
    advisor_client: Client, retained_section: StudentTermSection, projection: Mock
) -> None:
    token = _build_token(advisor_client)
    TermSectionMeeting.objects.filter(term_section=retained_section.term_section).update(
        start_time="08:20"
    )
    response = _project(advisor_client, token)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROJECTION_BUILD_STALE"
    projection.assert_not_called()


def test_infeasible_builds_issue_no_projection_token(
    advisor_client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("core.planner_views.build_plans", lambda *a, **kw: {"options": []})
    response = advisor_client.post(
        reverse("planner_build"),
        {"student_id": SID, "academic_year": YEAR, "term": TERM},
        content_type="application/json",
    )
    assert response.status_code == 200
    assert "graduation_projection_token" not in response.json()

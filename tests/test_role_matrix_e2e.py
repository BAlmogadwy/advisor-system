from collections import Counter

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from pytest import MonkeyPatch

from core.models import Student
from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_GENERAL_ADVISOR,
    ROLE_SUPER_ADMIN,
    ensure_role_groups,
    ensure_scope_schema,
    set_user_scope,
)

pytestmark = pytest.mark.django_db


client = Client()


def _login_as(username: str, role: str, *, advisor_id: str = "", departments: str = "") -> User:
    ensure_role_groups()
    ensure_scope_schema()
    user, _ = User.objects.get_or_create(username=username)
    user.groups.clear()
    user.groups.add(Group.objects.get(name=role))
    set_user_scope(user.id, advisor_id=advisor_id, departments=departments)
    client.force_login(user)
    return user


def test_dashboard_visibility_matrix() -> None:
    _login_as("role-super", ROLE_SUPER_ADMIN)
    r = client.get("/")
    html = r.content.decode("utf-8")
    assert r.status_code == 200
    # Super admin sees sidebar links for advisor admin, portfolio, and db admin
    assert 'data-target="advisoradmin"' in html
    assert "Advisor Portfolio" in html
    assert "DB Admin Panel" in html
    assert "Access scope:" in html

    _login_as("role-general", ROLE_GENERAL_ADVISOR, departments="AI,CS")
    r = client.get("/")
    html = r.content.decode("utf-8")
    assert r.status_code == 200
    # General advisor does NOT see advisor admin or db admin sidebar links
    assert 'data-target="advisoradmin"' not in html
    assert "Advisor Portfolio" in html
    assert "DB Admin Panel" not in html

    _login_as("role-advisor", ROLE_ADVISOR, advisor_id="A001")
    r = client.get("/")
    html = r.content.decode("utf-8")
    assert r.status_code == 200
    assert 'data-target="advisoradmin"' not in html
    assert "Advisor Portfolio" in html
    assert "DB Admin Panel" not in html


def test_super_admin_full_access_summary(monkeypatch: MonkeyPatch) -> None:
    _login_as("api-super", ROLE_SUPER_ADMIN)

    monkeypatch.setattr(
        "core.report_views.build_aggregate_counts",
        lambda year, semester, program=None, section=None: (2, Counter({"CS101": 2})),
    )

    response = client.get("/report/summary/?year=1448&semester=0")
    assert response.status_code == 200


def test_general_advisor_department_scope_summary(monkeypatch: MonkeyPatch) -> None:
    _login_as("api-general", ROLE_GENERAL_ADVISOR, departments="AI,CS")

    monkeypatch.setattr(
        "core.report_views.build_aggregate_counts",
        lambda year, semester, program=None, section=None: (1, Counter({"AI201": 1})),
    )

    ok_resp = client.get("/report/summary/?year=1448&semester=0&program=AI")
    assert ok_resp.status_code == 200

    missing_program = client.get("/report/summary/?year=1448&semester=0")
    assert missing_program.status_code == 400

    out_of_scope = client.get("/report/summary/?year=1448&semester=0&program=EE")
    assert out_of_scope.status_code == 403


def test_advisor_own_students_scope_student_plan(monkeypatch: MonkeyPatch) -> None:
    _login_as("api-advisor", ROLE_ADVISOR, advisor_id="A001")

    # Create real Student objects instead of monkeypatching fetch_all
    Student.objects.get_or_create(
        student_id=1001,
        defaults={"program": "AI", "advisor_id": "A001"},
    )
    Student.objects.get_or_create(
        student_id=1002,
        defaults={"program": "AI", "advisor_id": "A002"},
    )

    monkeypatch.setattr(
        "core.report_views._build_student_plan_payload",
        lambda student_id: ({"student_id": student_id, "program": "AI", "terms": []}, None),
    )

    in_scope = client.get("/report/student-plan/?student_id=1001")
    assert in_scope.status_code == 200

    out_scope = client.get("/report/student-plan/?student_id=1002")
    assert out_scope.status_code == 403


def test_general_advisor_department_scope_student_plan(monkeypatch: MonkeyPatch) -> None:
    _login_as("api-general-plan", ROLE_GENERAL_ADVISOR, departments="AI")

    # Create real Student object instead of monkeypatching fetch_all
    Student.objects.get_or_create(
        student_id=1003,
        defaults={"program": "CS", "advisor_id": "A777"},
    )

    monkeypatch.setattr(
        "core.report_views._build_student_plan_payload",
        lambda student_id: ({"student_id": student_id, "program": "CS", "terms": []}, None),
    )

    response = client.get("/report/student-plan/?student_id=1003")
    assert response.status_code == 403

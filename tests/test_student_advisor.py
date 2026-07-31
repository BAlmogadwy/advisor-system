"""Student-facing AI advisor: identity forcing, tool scoping, page gating."""

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client, override_settings

from core.models import Student
from core.services import student_otp
from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_STUDENT,
    ROLE_SUPER_ADMIN,
    ensure_role_groups,
    set_user_scope,
)
from core.services.virtual_advisor_capabilities import (
    _resolve_scoped_student_id,
    build_default_registry,
)

SID = 4901234
OTHER = 4905678
pytestmark = pytest.mark.django_db


@pytest.fixture
def students():
    ensure_role_groups()
    Student.objects.get_or_create(
        student_id=SID, defaults={"name": "Test One", "program": "DS2", "section": "M"}
    )
    Student.objects.get_or_create(
        student_id=OTHER, defaults={"name": "Test Two", "program": "AI2", "section": "M"}
    )


def _tools(role):
    reg = build_default_registry()
    return sorted(c.name for c in reg.capabilities_for_scope({"role": role, "student_id": SID}))


def test_students_get_only_self_scoped_tools():
    student = _tools(ROLE_STUDENT)
    assert "find_students" not in student  # cohort search is staff-only
    assert "portfolio_triage" not in student
    assert "aggregate_demand" not in student
    assert "graduation_shortfall" not in student
    assert set(student) == {
        "course_prerequisites",
        "get_student_context",
        "lookup_course",
        "recommend_courses",
    }
    assert "find_students" in _tools(ROLE_ADVISOR)  # staff keep it
    assert "aggregate_demand" in _tools(ROLE_SUPER_ADMIN)


def test_tool_layer_forces_own_identity():
    scope = {"role": ROLE_STUDENT, "student_id": SID}
    resolved, err = _resolve_scoped_student_id({"student_id": OTHER}, scope)
    assert resolved is None and "own records" in err
    resolved, err = _resolve_scoped_student_id({}, scope)
    assert resolved == SID and err is None


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_advisor_page_renders_without_student_id_control(students):
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)
    r = c.get("/student/advisor/")
    assert r.status_code == 200
    body = r.content.decode()
    assert "page-student-advisor.js" in body
    assert "vaStudentId" not in body  # no free-text student-ID box
    assert 'name="student_id"' not in body


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_staff_are_redirected_off_the_student_advisor(students):
    staff = User.objects.create_user(username="adv9", password="x", is_staff=True)
    set_user_scope(staff.id, advisor_id="A1")
    c = Client()
    c.force_login(staff)
    r = c.get("/student/advisor/")
    assert r.status_code == 302 and "virtual-advisor" in r.headers["Location"]


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_chat_ignores_a_student_id_in_the_payload(students, monkeypatch):
    """A student may pass any id; the view must overwrite it with their own."""
    seen = {}

    def fake_answer(**kwargs):
        seen.update(kwargs)
        return {"answer": "ok", "model": "test"}

    monkeypatch.setattr("core.virtual_advisor_views.answer_virtual_advisor", fake_answer)
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)
    r = c.post(
        "/ops/virtual-advisor/chat/",
        data=json.dumps({"message": "hi", "student_id": OTHER}),
        content_type="application/json",
    )
    assert r.status_code == 200
    assert seen["student_id"] == SID  # forced, not OTHER
    assert seen["scope"]["role"] == ROLE_STUDENT
    assert seen["scope"]["student_id"] == SID


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_chat_refuses_a_student_with_no_linked_record(students, monkeypatch):
    from core.models import UserScope

    monkeypatch.setattr(
        "core.virtual_advisor_views.answer_virtual_advisor",
        lambda **k: {"answer": "should not run"},
    )
    u = student_otp.provision_student_user(SID)
    UserScope.objects.filter(user_id=u.id).update(student_id=None)
    c = Client()
    c.force_login(u)
    r = c.post(
        "/ops/virtual-advisor/chat/",
        data=json.dumps({"message": "hi"}),
        content_type="application/json",
    )
    assert r.status_code == 403

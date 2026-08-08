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
        # course / plan reference
        "course_prerequisites",
        "get_student_context",
        "lookup_course",
        "recommend_courses",
        # the portal's own answers, each self-scoped to the caller
        "my_progress",
        "why_course_locked",
        "graduation_progress",
        "my_timetable",
        "my_plan_by_term",
        "my_advisor",
        "my_clash_free_sections",
        "build_my_timetable",
        "build_timetable_proposal",
        # The written regulations. Carries no student data at all — the rules are the
        # same for everyone — so it widens what a student can READ, not what they can
        # reach ABOUT THEMSELVES or anyone else. Added deliberately when the policy
        # store was wired into the runtime.
        "policy_lookup",
    }
    # This set is pinned on purpose. Every addition widens what a student can reach,
    # so it must be a deliberate edit here rather than a silent consequence of
    # registering a tool. If this assertion fails, check the new tool is genuinely
    # self-scoped before updating the set.
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
def test_a_student_cannot_reach_the_operator_console_at_all(students, monkeypatch):
    """Stronger than the clamp this replaces.

    The console reaches the same generator as the student adviser on a separate,
    looser budget, so a student who could call it had a route around their own
    rate limit — and the only thing standing between them and another student's
    record was a payload-overwrite deep inside the view. Now they never arrive.
    """
    called = []
    monkeypatch.setattr(
        "core.virtual_advisor_views.answer_virtual_advisor",
        lambda **k: called.append(k) or {"answer": "should not run", "model": "t"},
    )
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)
    r = c.post(
        "/ops/virtual-advisor/chat/",
        data=json.dumps({"message": "hi", "student_id": OTHER}),
        content_type="application/json",
    )
    assert r.status_code == 403
    assert called == [], "the generator ran for a student on the operator console"


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_a_student_cannot_probe_the_model_host_through_the_health_endpoint(students):
    """It makes an outbound call to the LLM host.

    Ungated, any signed-in student can aim the deployment's own model connection,
    at 30 per minute per worker.
    """
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)
    assert c.get("/ops/virtual-advisor/health/").status_code == 403


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_the_console_still_clamps_a_student_if_the_role_gate_is_removed(students, monkeypatch):
    """The second lock. The role gate above is the first.

    Called with the decorator stripped, so this pins the view's OWN behaviour: a
    student's principal forces their own id whatever the payload claims. Without
    this the clamp reads as dead code the moment the gate lands.
    """
    seen = {}

    def fake_answer(**kwargs):
        seen.update(kwargs)
        return {"answer": "ok", "model": "test"}

    monkeypatch.setattr("core.virtual_advisor_views.answer_virtual_advisor", fake_answer)
    u = student_otp.provision_student_user(SID)

    from django.test import RequestFactory

    from core.virtual_advisor_views import virtual_advisor_chat_view

    undecorated = virtual_advisor_chat_view
    while hasattr(undecorated, "__wrapped__"):
        undecorated = undecorated.__wrapped__

    request = RequestFactory().post(
        "/ops/virtual-advisor/chat/",
        data=json.dumps({"message": "hi", "student_id": OTHER}),
        content_type="application/json",
    )
    request.user = u
    assert undecorated(request).status_code == 200
    assert seen["principal"].student_id == SID  # forced, not OTHER
    assert seen["principal"].as_scope()["role"] == ROLE_STUDENT
    assert seen["principal"].as_scope()["student_id"] == SID


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


# ── the four capabilities added so the advisor can answer from the portal's own data ──

NEW_TOOLS = {"my_progress", "why_course_locked", "graduation_progress", "my_timetable"}


def test_new_tools_are_available_to_students_and_staff():
    student = set(_tools(ROLE_STUDENT))
    assert NEW_TOOLS <= student, f"missing for students: {NEW_TOOLS - student}"
    assert NEW_TOOLS <= set(_tools(ROLE_ADVISOR))
    assert NEW_TOOLS <= set(_tools(ROLE_SUPER_ADMIN))


@pytest.mark.parametrize("tool", sorted(NEW_TOOLS))
def test_new_tools_refuse_another_students_record(students, tool):
    reg = build_default_registry()
    scope = {"role": ROLE_STUDENT, "student_id": SID}
    args = {"student_id": OTHER}
    if tool == "why_course_locked":
        args["course_code"] = "CS101"
    r = reg.execute(tool, args, scope=scope, ctx={"academic_year": 1448, "term": 1})
    assert r["ok"] is False
    assert "own records" in r["error"]


@pytest.mark.parametrize("tool", sorted(NEW_TOOLS))
def test_new_tools_refuse_a_session_with_no_student(students, tool):
    reg = build_default_registry()
    scope = {"role": ROLE_STUDENT, "student_id": None}
    args = {"course_code": "CS101"} if tool == "why_course_locked" else {}
    r = reg.execute(tool, args, scope=scope, ctx={"academic_year": 1448, "term": 1})
    assert r["ok"] is False


def test_why_course_locked_needs_a_course_code(students):
    reg = build_default_registry()
    r = reg.execute(
        "why_course_locked",
        {},
        scope={"role": ROLE_STUDENT, "student_id": SID},
        ctx={"academic_year": 1448, "term": 1},
    )
    assert r["ok"] is False and "course_code" in r["error"]

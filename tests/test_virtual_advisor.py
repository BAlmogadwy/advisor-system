import json

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import Course, ProgrammeRequirement, Student, StudentCourse
from core.services.local_llm import ChatResult
from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_SUPER_ADMIN,
    ensure_role_groups,
    ensure_scope_schema,
    set_user_scope,
)
from core.services.virtual_advisor import (
    answer_virtual_advisor,
    plan_verified_tools,
    run_planned_tools,
)

pytestmark = pytest.mark.django_db


def _login_as(client: Client, username: str, role: str, *, advisor_id: str = "") -> User:
    ensure_role_groups()
    ensure_scope_schema()
    user, _ = User.objects.get_or_create(username=username)
    user.groups.clear()
    user.groups.add(Group.objects.get(name=role))
    set_user_scope(user.id, advisor_id=advisor_id, departments="")
    client.force_login(user)
    return user


def test_virtual_advisor_page_requires_auth(client: Client) -> None:
    response = client.get("/virtual-advisor/")
    assert response.status_code == 302


def test_virtual_advisor_chat_uses_verified_student_context(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _login_as(client, "va-admin", ROLE_SUPER_ADMIN)

    student = Student.objects.create(
        student_id=501001,
        name="Local Advisor Student",
        program="AI",
        section="AI M",
        advisor_id="A001",
        gpa=3.2,
        total_earned_credits=83,
    )
    course = Course.objects.create(course_code="AI331", description="Artificial Intelligence")
    StudentCourse.objects.create(student=student, course=course, status="passed")
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="AI331",
        course_name="Artificial Intelligence",
        type="Core",
        programme_term=5,
        credit_hours=3,
    )
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="AI431",
        course_name="Graduation Project",
        type="Core",
        programme_term=8,
        credit_hours=3,
    )

    captured: dict[str, object] = {}

    class FakeClient:
        def resolve_model(self, requested_model=None):
            return requested_model or "fake-qwen"

        def chat(
            self,
            messages,
            *,
            model=None,
            temperature=0.2,
            max_tokens=None,
            assistant_prefill=None,
        ):
            captured["messages"] = messages
            captured["model"] = model
            captured["assistant_prefill"] = assistant_prefill
            return ChatResult(
                content="The student has verified AI context and passed AI331.",
                model="fake-qwen",
                usage={},
                raw={},
            )

    monkeypatch.setattr("core.services.virtual_advisor.LocalLLMClient", lambda: FakeClient())

    response = client.post(
        "/ops/virtual-advisor/chat/",
        data=json.dumps(
            {
                "message": "Can this student proceed safely?",
                "student_id": 501001,
                "academic_year": 1448,
                "term": 1,
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "The student has verified AI context and passed AI331."
    assert body["model"] == "fake-qwen"
    assert body["context_summary"]["student_id"] == 501001
    assert body["context_summary"]["total_earned_credits"] == 83

    prompt = captured["messages"][-1]["content"]  # type: ignore[index]
    assert "verified_context" in prompt
    assert "AI331" in prompt
    assert "total_earned_credits" in prompt


def test_virtual_advisor_chat_respects_student_scope(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _login_as(client, "va-advisor", ROLE_ADVISOR, advisor_id="A001")
    Student.objects.create(student_id=501002, program="AI", advisor_id="A999")

    monkeypatch.setattr(
        "core.services.virtual_advisor.LocalLLMClient",
        lambda: object(),
    )

    response = client.post(
        "/ops/virtual-advisor/chat/",
        data=json.dumps({"message": "Summarize", "student_id": 501002}),
        content_type="application/json",
    )

    assert response.status_code == 403
    assert response.json()["reason_code"] == "STUDENT_SCOPE_ADVISOR_MISMATCH"


def test_virtual_advisor_agent_finds_students_by_credits_and_passed_course(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cs323 = Course.objects.create(course_code="CS323", description="Operating Systems")
    cs111 = Course.objects.create(course_code="CS111", description="Programming")

    match = Student.objects.create(
        student_id=700001,
        name="Matched Student",
        program="CS",
        section="M",
        advisor_id="A001",
        total_earned_credits=96,
        gpa=3.1,
    )
    too_low = Student.objects.create(
        student_id=700002,
        name="Too Low Credits",
        program="CS",
        section="M",
        advisor_id="A001",
        total_earned_credits=89,
    )
    studying = Student.objects.create(
        student_id=700003,
        name="Studying Not Passed",
        program="CS",
        section="F",
        advisor_id="A001",
        total_earned_credits=120,
    )
    wrong_course = Student.objects.create(
        student_id=700004,
        name="Wrong Course",
        program="CS",
        section="F",
        advisor_id="A001",
        total_earned_credits=110,
    )
    StudentCourse.objects.create(student=match, course=cs323, status="passed")
    StudentCourse.objects.create(student=too_low, course=cs323, status="passed")
    StudentCourse.objects.create(student=studying, course=cs323, status="studying")
    StudentCourse.objects.create(student=wrong_course, course=cs111, status="passed")

    captured: dict[str, object] = {}

    class FakeClient:
        def resolve_model(self, requested_model=None):
            return requested_model or "fake-local"

        def chat(
            self, messages, *, model=None, temperature=0.2, max_tokens=None, assistant_prefill=None
        ):
            captured["messages"] = messages
            return ChatResult(
                content="Verified one matching student.", model="fake-local", usage={}, raw={}
            )

    monkeypatch.setattr("core.services.virtual_advisor.LocalLLMClient", lambda: FakeClient())

    result = answer_virtual_advisor(
        question="Find the students who completed 90 hours or more and have CS323 passed. Show top 5.",
        scope={"role": ROLE_SUPER_ADMIN},
    )

    tool_result = result["tool_results"][0]
    assert tool_result["tool"] == "find_students"
    assert tool_result["count"] == 1
    assert tool_result["students"][0]["student_id"] == 700001

    prompt = captured["messages"][-1]["content"]  # type: ignore[index]
    assert "tool_results" in prompt
    assert "CS323" in prompt
    assert "700001" in prompt


def test_virtual_advisor_dataset_query_respects_advisor_scope(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _login_as(client, "va-agent-advisor", ROLE_ADVISOR, advisor_id="A001")
    cs323 = Course.objects.create(course_code="CS323", description="Operating Systems")
    own = Student.objects.create(
        student_id=800001,
        name="Own Student",
        program="CS",
        advisor_id="A001",
        total_earned_credits=100,
    )
    other = Student.objects.create(
        student_id=800002,
        name="Other Advisor Student",
        program="CS",
        advisor_id="A999",
        total_earned_credits=100,
    )
    StudentCourse.objects.create(student=own, course=cs323, status="passed")
    StudentCourse.objects.create(student=other, course=cs323, status="passed")

    preview = client.post(
        "/ops/virtual-advisor/tools/preview/",
        data=json.dumps(
            {
                "message": "Find students who completed 90 hours or more and have CS323 passed.",
            }
        ),
        content_type="application/json",
    )
    assert preview.status_code == 200
    preview_tool = preview.json()["tool_results"][0]
    assert preview_tool["count"] == 1
    assert preview_tool["students"][0]["student_id"] == 800001

    class FakeClient:
        def resolve_model(self, requested_model=None):
            return requested_model or "fake-local"

        def chat(
            self, messages, *, model=None, temperature=0.2, max_tokens=None, assistant_prefill=None
        ):
            return ChatResult(
                content="Scoped verified result.", model="fake-local", usage={}, raw={}
            )

    monkeypatch.setattr("core.services.virtual_advisor.LocalLLMClient", lambda: FakeClient())

    response = client.post(
        "/ops/virtual-advisor/chat/",
        data=json.dumps(
            {
                "message": "Find students who completed 90 hours or more and have CS323 passed.",
                "academic_year": 1448,
                "term": 1,
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 200
    body = response.json()
    tool_result = body["tool_results"][0]
    assert tool_result["count"] == 1
    assert tool_result["scope_applied"]["advisor_id"] == "A001"
    assert tool_result["students"][0]["student_id"] == 800001


def test_virtual_advisor_plans_compound_passed_or_studying_query() -> None:
    ai331 = Course.objects.create(course_code="AI331", description="Artificial Intelligence")
    ds331 = Course.objects.create(course_code="DS331", description="Data Science")

    ai_passed = Student.objects.create(
        student_id=810001,
        name="AI Passed",
        program="AI",
        section="M",
        total_earned_credits=90,
        gpa=4.1,
    )
    ai_studying = Student.objects.create(
        student_id=810002,
        name="AI Studying",
        program="AI",
        section="F",
        total_earned_credits=100,
        gpa=4.3,
    )
    ai_low = Student.objects.create(
        student_id=810003,
        name="AI Low",
        program="AI",
        section="F",
        total_earned_credits=84,
        gpa=4.5,
    )
    ds_passed = Student.objects.create(
        student_id=810004,
        name="DS Passed",
        program="DS",
        section="M",
        total_earned_credits=95,
        gpa=4.0,
    )
    ds_studying = Student.objects.create(
        student_id=810005,
        name="DS Studying",
        program="DS",
        section="F",
        total_earned_credits=110,
        gpa=4.6,
    )
    StudentCourse.objects.create(student=ai_passed, course=ai331, status="passed")
    StudentCourse.objects.create(student=ai_studying, course=ai331, status="studying")
    StudentCourse.objects.create(student=ai_low, course=ai331, status="passed")
    StudentCourse.objects.create(student=ds_passed, course=ds331, status="passed")
    StudentCourse.objects.create(student=ds_studying, course=ds331, status="studying")

    query = """i want you to find me a list against our local DB

all AI M,F students passed or studying AI331 and earned credit 85 or more
all DS M,F students passed or studying DS331 and earned credit 85 or more

i want the student name,ID, GPA earned credit , AI331 or DS331 status"""

    plan = plan_verified_tools(query)
    assert plan == [
        {
            "tool": "find_students",
            "args": {
                "min_earned_credits": 85,
                "sections": ["M", "F"],
                "limit": 500,
                "program": "AI",
                "course_status_any": [{"course_code": "AI331", "statuses": ["passed", "studying"]}],
            },
        },
        {
            "tool": "find_students",
            "args": {
                "min_earned_credits": 85,
                "sections": ["M", "F"],
                "limit": 500,
                "program": "DS",
                "course_status_any": [{"course_code": "DS331", "statuses": ["passed", "studying"]}],
            },
        },
    ]

    results = run_planned_tools(query, scope={"role": ROLE_SUPER_ADMIN})

    assert [result["count"] for result in results] == [2, 2]
    assert {row["student_id"] for row in results[0]["students"]} == {810001, 810002}
    assert results[0]["students"][0]["course_statuses"]["AI331"] in {"passed", "studying"}
    assert {row["student_id"] for row in results[1]["students"]} == {810004, 810005}
    assert results[1]["students"][0]["course_statuses"]["DS331"] in {"passed", "studying"}


def test_course_status_any_respects_student_scope() -> None:
    ai331 = Course.objects.create(course_code="AI331", description="Artificial Intelligence")
    linked = Student.objects.create(
        student_id=820001,
        name="Linked Student",
        program="AI",
        section="F",
        total_earned_credits=90,
    )
    other = Student.objects.create(
        student_id=820002,
        name="Other Student",
        program="AI",
        section="F",
        total_earned_credits=120,
    )
    StudentCourse.objects.create(student=linked, course=ai331, status="passed")
    StudentCourse.objects.create(student=other, course=ai331, status="passed")

    result = run_planned_tools(
        "Show all AI students passed or studying AI331 and earned credit 85 or more",
        scope={"role": "STUDENT", "student_id": linked.student_id},
    )[0]

    assert result["count"] == 1
    assert result["students"][0]["student_id"] == linked.student_id


def test_virtual_advisor_keeps_same_program_multi_course_query_as_intersection() -> None:
    cs323 = Course.objects.create(course_code="CS323", description="Operating Systems")
    cs111 = Course.objects.create(course_code="CS111", description="Programming")
    both = Student.objects.create(
        student_id=830001,
        name="Both Courses",
        program="CS",
        total_earned_credits=100,
    )
    one = Student.objects.create(
        student_id=830002,
        name="One Course",
        program="CS",
        total_earned_credits=100,
    )
    StudentCourse.objects.create(student=both, course=cs323, status="passed")
    StudentCourse.objects.create(student=both, course=cs111, status="passed")
    StudentCourse.objects.create(student=one, course=cs323, status="passed")

    query = "Find students who earned 85 credits or more and passed CS323 and CS111."

    plan = plan_verified_tools(query)
    assert len(plan) == 1
    assert plan[0]["args"]["passed_courses"] == ["CS323", "CS111"]

    result = run_planned_tools(query, scope={"role": ROLE_SUPER_ADMIN})[0]
    assert result["count"] == 1
    assert result["students"][0]["student_id"] == both.student_id


def test_virtual_advisor_understands_section_and_gpa_filters() -> None:
    ai331 = Course.objects.create(course_code="AI331", description="Artificial Intelligence")
    match = Student.objects.create(
        student_id=840001,
        name="High GPA Female",
        program="AI",
        section="F",
        total_earned_credits=100,
        gpa=4.7,
    )
    low_gpa = Student.objects.create(
        student_id=840002,
        name="Low GPA Female",
        program="AI",
        section="F",
        total_earned_credits=100,
        gpa=4.2,
    )
    male = Student.objects.create(
        student_id=840003,
        name="High GPA Male",
        program="AI",
        section="M",
        total_earned_credits=100,
        gpa=4.8,
    )
    for student in (match, low_gpa, male):
        StudentCourse.objects.create(student=student, course=ai331, status="passed")

    query = (
        "Show female AI students with GPA 4.5 or more and earned credits above 90 who passed AI331."
    )

    plan = plan_verified_tools(query)
    assert plan[0]["args"]["program"] == "AI"
    assert plan[0]["args"]["sections"] == ["F"]
    assert plan[0]["args"]["min_gpa"] == 4.5
    assert plan[0]["args"]["min_earned_credits"] == 90

    result = run_planned_tools(query, scope={"role": ROLE_SUPER_ADMIN})[0]
    assert result["count"] == 1
    assert result["students"][0]["student_id"] == match.student_id


def test_virtual_advisor_lists_scoped_students_without_extra_filters() -> None:
    Student.objects.create(
        student_id=850001,
        name="Advisor Student One",
        program="AI",
        advisor_id="A001",
        total_earned_credits=50,
    )
    Student.objects.create(
        student_id=850002,
        name="Advisor Student Two",
        program="DS",
        advisor_id="A001",
        total_earned_credits=80,
    )
    Student.objects.create(
        student_id=850003,
        name="Other Advisor",
        program="AI",
        advisor_id="A999",
        total_earned_credits=120,
    )

    query = "Show my students."
    plan = plan_verified_tools(query)
    assert plan == [{"tool": "find_students", "args": {"limit": 100}}]

    result = run_planned_tools(query, scope={"role": ROLE_ADVISOR, "advisor_id": "A001"})[0]
    assert result["count"] == 2
    assert {row["student_id"] for row in result["students"]} == {850001, 850002}


def test_virtual_advisor_understands_missing_course_and_program() -> None:
    ai331 = Course.objects.create(course_code="AI331", description="Artificial Intelligence")
    missing = Student.objects.create(
        student_id=860001,
        name="Missing Course",
        program="AI",
        total_earned_credits=100,
    )
    passed = Student.objects.create(
        student_id=860002,
        name="Passed Course",
        program="AI",
        total_earned_credits=100,
    )
    studying = Student.objects.create(
        student_id=860003,
        name="Studying Course",
        program="AI",
        total_earned_credits=100,
    )
    StudentCourse.objects.create(student=passed, course=ai331, status="passed")
    StudentCourse.objects.create(student=studying, course=ai331, status="studying")

    query = "Which AI students with at least 85 credits have not passed AI331?"

    result = run_planned_tools(query, scope={"role": ROLE_SUPER_ADMIN})[0]
    assert result["filters"]["program"] == "AI"
    assert result["filters"]["missing_courses"] == ["AI331"]
    assert result["count"] == 1
    assert result["students"][0]["student_id"] == missing.student_id


def test_virtual_advisor_understands_messy_advisor_wording() -> None:
    ai331 = Course.objects.create(course_code="AI331", description="Artificial Intelligence")
    match = Student.objects.create(
        student_id=870001,
        name="Natural Wording Match",
        program="AI",
        section="F",
        total_earned_credits=92,
        gpa=4.0,
    )
    wrong_section = Student.objects.create(
        student_id=870002,
        name="Male Match",
        program="AI",
        section="M",
        total_earned_credits=92,
        gpa=4.0,
    )
    low_hours = Student.objects.create(
        student_id=870003,
        name="Low Hours",
        program="AI",
        section="F",
        total_earned_credits=70,
        gpa=4.0,
    )
    for student in (match, wrong_section, low_hours):
        StudentCourse.objects.create(student=student, course=ai331, status="passed")

    query = "Need the girls in AI who already did AI331 and have 90+ hours."

    plan = plan_verified_tools(query)
    assert plan[0]["args"]["program"] == "AI"
    assert plan[0]["args"]["sections"] == ["F"]
    assert plan[0]["args"]["min_earned_credits"] == 90
    assert plan[0]["args"]["passed_courses"] == ["AI331"]

    result = run_planned_tools(query, scope={"role": ROLE_SUPER_ADMIN})[0]
    assert result["count"] == 1
    assert result["students"][0]["student_id"] == match.student_id


def test_student_like_vague_question_uses_verified_context_without_dataset_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    student = Student.objects.create(
        student_id=880001,
        name="Vague Student",
        program="AI",
        section="F",
        total_earned_credits=90,
        gpa=4.2,
    )
    ai331 = Course.objects.create(course_code="AI331", description="Artificial Intelligence")
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="AI331",
        course_name="Artificial Intelligence",
        type="Core",
        programme_term=5,
        credit_hours=3,
    )
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="AI431",
        course_name="Advanced AI",
        type="Core",
        programme_term=7,
        credit_hours=3,
    )
    StudentCourse.objects.create(student=student, course=ai331, status="passed")
    captured: dict[str, object] = {}

    class FakeClient:
        def resolve_model(self, requested_model=None):
            return requested_model or "fake-local"

        def chat(
            self, messages, *, model=None, temperature=0.2, max_tokens=None, assistant_prefill=None
        ):
            captured["messages"] = messages
            return ChatResult(
                content="Use verified context, not a canned answer.",
                model="fake-local",
                usage={},
                raw={},
            )

    monkeypatch.setattr("core.services.virtual_advisor.LocalLLMClient", lambda: FakeClient())

    result = answer_virtual_advisor(
        question="I finished the AI thing, what should I do next?",
        student_id=student.student_id,
        scope={"role": "STUDENT", "student_id": student.student_id},
    )

    assert result["tool_results"] == []
    prompt = captured["messages"][-1]["content"]  # type: ignore[index]
    assert "verified_context" in prompt
    assert "AI331" in prompt
    assert "AI431" in prompt

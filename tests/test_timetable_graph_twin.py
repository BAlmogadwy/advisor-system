import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import (
    BoardStudentLink,
    Course,
    DeliveryBoard,
    ProgrammeRequirement,
    ScenarioStudentCourseRequest,
    ScenarioStudentMap,
    SectionPlacement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.rbac import (
    ROLE_SUPER_ADMIN,
    ensure_role_groups,
    ensure_scope_schema,
    set_user_scope,
)
from core.services.timetable_graph_twin import (
    build_scenario_graph_summary,
    build_scenario_graph_view,
)

pytestmark = pytest.mark.django_db


def _login_as_admin(client: Client) -> User:
    ensure_role_groups()
    ensure_scope_schema()
    user, _ = User.objects.get_or_create(username="graph-admin")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    set_user_scope(user.id, advisor_id="", departments="")
    client.force_login(user)
    return user


def _graph_fixture() -> TimetableScenario:
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="Graph Twin Test",
        status="draft",
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario,
        label="AI M Term 5",
        nominal_term=5,
        program="AI",
        target_size=1,
    )
    student = Student.objects.create(
        student_id=80001,
        name="Graph Student",
        program="AI",
        section="M",
        total_earned_credits=83,
        current_registered_credits=15,
    )
    course = Course.objects.create(
        course_code="AI331",
        description="Artificial Intelligence",
        credit_hours=3,
    )
    StudentCourse.objects.create(student=student, course=course, status="studying")
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="AI331",
        course_name="Artificial Intelligence",
        type="Core",
        programme_term=5,
        credit_hours=3,
    )
    term_section = TermSection.objects.create(
        scenario=scenario,
        source_tag="test",
        course_name="Artificial Intelligence",
        available_capacity=40,
        registered_count=1,
        course_code="AI331",
        course_number="AI331",
        course_key="AI331",
        section="S1",
    )
    TermSectionMeeting.objects.create(
        term_section=term_section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="AI101",
        instructor="Dr Graph",
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=term_section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="AI101",
    )
    StudentTermSection.objects.create(
        student_id=student.student_id,
        academic_year="1448",
        term="1",
        term_section=term_section,
        source="test",
    )
    ScenarioStudentMap.objects.create(
        scenario=scenario,
        student_id=student.student_id,
        primary_term=5,
        recommended_courses=["AI331"],
    )
    ScenarioStudentCourseRequest.objects.create(
        scenario=scenario,
        student_id=student.student_id,
        course_key="AI331",
        course_code="AI331",
        primary_term=5,
        status=ScenarioStudentCourseRequest.STATUS_REQUESTED,
        priority=ScenarioStudentCourseRequest.PRIORITY_NORMAL,
        source="test",
    )
    BoardStudentLink.objects.create(board=board, student_id=student.student_id, link_type="primary")
    return scenario


def test_scenario_graph_summary_builds_student_centered_contract() -> None:
    scenario = _graph_fixture()

    data = build_scenario_graph_summary(scenario.id)

    node_counts = data["summary"]["node_counts"]
    rel_counts = data["summary"]["relationship_counts"]
    assert node_counts["TTStudent"] == 1
    assert node_counts["TTProgram"] == 1
    assert node_counts["TTPlanTerm"] == 1
    assert node_counts["TTCourse"] == 1
    assert node_counts["TTSection"] == 1
    assert node_counts["TTSlot"] == 1
    assert node_counts["TTBoard"] == 1
    assert rel_counts["ENROLLED_IN"] == 1
    assert rel_counts["HAS_PLAN_TERM"] == 1
    assert rel_counts["HAS_GROUP"] == 1
    assert rel_counts["TERM_REQUIRES"] == 1
    assert rel_counts["SCHEDULES_COURSE"] == 1
    assert rel_counts["STUDYING_NOW"] == 1
    assert rel_counts["CURRENTLY_REGISTERED_IN"] == 1
    assert rel_counts["NEEDS_IN_SCENARIO"] == 1
    assert rel_counts["PLACED_IN"] == 1
    assert rel_counts["OF_COURSE"] == 1


def test_scenario_graph_view_returns_embedded_view_contract() -> None:
    scenario = _graph_fixture()

    data = build_scenario_graph_view(scenario.id, mode="placements", limit=30)

    assert data["mode"] == "placements"
    assert data["nodes"]
    assert data["edges"]
    assert {"id", "label", "type"}.issubset(data["nodes"][0])
    assert {"source", "target", "type"}.issubset(data["edges"][0])


def test_scenario_graph_view_returns_plan_hierarchy() -> None:
    scenario = _graph_fixture()

    data = build_scenario_graph_view(scenario.id, mode="plan", limit=40, include_students=True)

    node_types = {node["type"] for node in data["nodes"]}
    edge_types = {edge["type"] for edge in data["edges"]}
    assert data["mode"] == "plan"
    assert {"TTProgram", "TTPlanTerm", "TTBoard", "TTCourse", "TTSection", "TTStudent"}.issubset(
        node_types
    )
    assert {
        "HAS_PLAN_TERM",
        "HAS_GROUP",
        "SCHEDULES_COURSE",
        "HAS_SECTION",
        "HAS_ENROLLED_STUDENT",
    }.issubset(edge_types)


def test_scenario_graph_view_filters_plan_tree() -> None:
    scenario = _graph_fixture()

    data = build_scenario_graph_view(
        scenario.id,
        mode="plan",
        limit=40,
        program="AI",
        plan_term="5",
        include_students=False,
    )

    node_types = {node["type"] for node in data["nodes"]}
    edge_types = {edge["type"] for edge in data["edges"]}
    assert data["tree"]["program"] == "AI"
    assert data["tree"]["plan_term"] == "5"
    assert "TTStudent" not in node_types
    assert "HAS_ENROLLED_STUDENT" not in edge_types
    assert "AI" in data["filters"]["programs"]


def test_scenario_graph_view_splits_combined_board_programs() -> None:
    scenario = _graph_fixture()
    board = DeliveryBoard.objects.get(scenario=scenario)
    board.program = "AI,DS"
    board.save(update_fields=["program"])

    data = build_scenario_graph_view(scenario.id, mode="plan", limit=40, program="DS")

    program_labels = {node["label"] for node in data["nodes"] if node["type"] == "TTProgram"}
    assert "DS" in program_labels
    assert "AI,DS" not in data["filters"]["programs"]
    assert any(edge["type"] == "HAS_GROUP" for edge in data["edges"])


def test_scenario_graph_view_defaults_to_readable_plan_overview() -> None:
    scenario = _graph_fixture()

    data = build_scenario_graph_view(scenario.id, mode="plan", limit=40)

    node_types = {node["type"] for node in data["nodes"]}
    edge_types = {edge["type"] for edge in data["edges"]}
    assert {"TTProgram", "TTPlanTerm", "TTBoard"}.issubset(node_types)
    assert "TTCourse" not in node_types
    assert "TTSection" not in node_types
    assert {"HAS_PLAN_TERM", "HAS_GROUP"}.issubset(edge_types)


def test_scenario_graph_view_can_return_progressive_plan_root() -> None:
    scenario = _graph_fixture()

    data = build_scenario_graph_view(
        scenario.id,
        mode="plan",
        limit=120,
        include_students=True,
        progressive=True,
    )

    node_types = {node["type"] for node in data["nodes"]}
    edge_types = {edge["type"] for edge in data["edges"]}
    assert data["tree"]["progressive"] is True
    assert data["tree"]["levels"][0] == "Scenario"
    assert {
        "TTScenario",
        "TTProgram",
        "TTPlanTerm",
        "TTBoard",
        "TTCourse",
        "TTSection",
        "TTStudent",
    }.issubset(node_types)
    assert {
        "HAS_PROGRAM",
        "HAS_PLAN_TERM",
        "HAS_GROUP",
        "SCHEDULES_COURSE",
        "HAS_SECTION",
        "HAS_ENROLLED_STUDENT",
    }.issubset(edge_types)


def test_graph_status_and_summary_endpoints(client: Client) -> None:
    _login_as_admin(client)
    scenario = _graph_fixture()

    status_response = client.get("/ops/tw/graph/status/")
    assert status_response.status_code == 200
    assert "neo4j" in status_response.json()

    summary_response = client.get(f"/ops/tw/scenarios/{scenario.id}/graph/summary/")
    assert summary_response.status_code == 200
    body = summary_response.json()
    assert body["summary"]["students"] == 1
    assert body["summary"]["placements"] == 1

    view_response = client.get(f"/ops/tw/scenarios/{scenario.id}/graph/view/?mode=placements")
    assert view_response.status_code == 200
    assert view_response.json()["mode"] == "placements"


def test_graph_sync_endpoint_requires_neo4j_password(
    client: Client,
    settings,
) -> None:
    _login_as_admin(client)
    scenario = _graph_fixture()
    settings.NEO4J_PASSWORD = ""

    response = client.post(f"/ops/tw/scenarios/{scenario.id}/graph/sync/")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "NEO4J_SYNC_FAILED"

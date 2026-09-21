import json
from typing import Any, Protocol

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from pytest import MonkeyPatch

from core.models import (
    AuditLog,
    DeliveryBoard,
    SectionPlacement,
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
    TimetableScenario,
)
from core.services.db_admin_ops import preview_clear_section_snapshot
from core.services.rbac import ROLE_ADVISOR, ROLE_SUPER_ADMIN, ensure_role_groups

pytestmark = pytest.mark.django_db


class _TestResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


def _section(
    code: str,
    section: str,
    *,
    scenario: TimetableScenario | None = None,
    source: str = "scraper_timetable",
) -> TermSection:
    letters = "".join(character for character in code if character.isalpha())
    numbers = "".join(character for character in code if character.isdigit())
    return TermSection.objects.create(
        scenario=scenario,
        source_tag=source,
        course_name=f"Course {code}",
        course_code=letters,
        course_number=numbers,
        course_key=code,
        section=section,
    )


def _meeting(section: TermSection, day: str = "SUN") -> TermSectionMeeting:
    return TermSectionMeeting.objects.create(
        term_section=section,
        day=day,
        start_time="09:00",
        end_time="10:00",
        room="101",
    )


def _student(student_id: int, program: str) -> Student:
    return Student.objects.create(
        student_id=student_id, program=program, name=f"Student {student_id}"
    )


def _student_link(student: Student, section: TermSection) -> StudentTermSection:
    return StudentTermSection.objects.create(
        student_id=student.student_id,
        academic_year="1448",
        term="1",
        term_section=section,
        source="scraper_timetable",
    )


def _login(client: Client, username: str, role: str = ROLE_SUPER_ADMIN) -> User:
    ensure_role_groups()
    user = User.objects.create_user(username=username, password="test-password")
    user.groups.add(Group.objects.get(name=role))
    client.force_login(user)
    return user


def _post_json(client: Client, path: str, payload: dict[str, object]) -> _TestResponse:
    return client.post(path, data=json.dumps(payload), content_type="application/json")


def test_preview_scopes_global_sections_by_program_and_gender() -> None:
    ai_m = _section("CS111", "M1")
    TermSectionProgram.objects.create(term_section=ai_m, program="AI")
    ai_f = _section("CS112", "F1")
    TermSectionProgram.objects.create(term_section=ai_f, program="AI")
    unassigned_m = _section("GS101", "M7")
    not_m_prefix = _section("GS102", "XM1")

    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="scenario")
    scenario_section = _section("AI221", "M2", scenario=scenario)

    preview = preview_clear_section_snapshot(
        program="ai",
        gender="m",
        all_programs=False,
        user_id=9,
    )

    assert preview["scope"] == {"program": "AI", "gender": "M", "all_programs": False}
    assert preview["sections_count"] == 1
    assert preview["physical_sections_count"] == 1
    assert preview["unassigned"]["count"] == 1
    assert preview["unassigned"]["included"] is False
    assert preview["confirmation_phrase"] == "CLEAR 1"
    assert preview["samples"][0]["id"] == ai_m.id
    assert unassigned_m.id not in {row["id"] for row in preview["samples"]}
    assert not_m_prefix.id not in {row["id"] for row in preview["samples"]}
    assert scenario_section.id not in {row["id"] for row in preview["samples"]}

    all_m = preview_clear_section_snapshot(program="", gender="M", all_programs=True, user_id=9)
    assert all_m["sections_count"] == 2
    assert all_m["unassigned"] == {
        "count": 1,
        "included": True,
        "samples": [
            {
                "id": unassigned_m.id,
                "course_key": "GS101",
                "section": "M7",
                "source_tag": "scraper_timetable",
            }
        ],
    }


def test_programme_clear_normalizes_student_program_before_scoping_links() -> None:
    section = _section("AI114", "M1")
    TermSectionProgram.objects.create(term_section=section, program="AI")
    student = _student(4610991, " ai ")
    _student_link(student, section)

    preview = preview_clear_section_snapshot(
        program="AI", gender="M", all_programs=False, user_id=9
    )

    assert preview["sections_count"] == 1
    assert preview["physical_sections_count"] == 1
    assert preview["student_links_count"] == 1
    assert preview["students_count"] == 1
    assert preview["retained_sections_count"] == 0


def test_program_clear_removes_its_membership_and_preserves_shared_section(
    monkeypatch: MonkeyPatch,
) -> None:
    private = _section("AI201", "M1")
    shared = _section("CS211", "M2")
    female = _section("AI202", "F1")
    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="protected scenario"
    )
    scenario_section = _section("AI399", "M9", scenario=scenario)

    TermSectionProgram.objects.create(term_section=private, program="AI")
    TermSectionProgram.objects.create(term_section=shared, program="AI")
    TermSectionProgram.objects.create(term_section=shared, program="DS")
    TermSectionProgram.objects.create(term_section=female, program="AI")
    ai_student = _student(1001, "AI")
    ds_student = _student(2001, "DS")
    _student_link(ai_student, private)
    _student_link(ai_student, shared)
    _student_link(ds_student, shared)
    private_meeting = _meeting(private)
    shared_meeting = _meeting(shared, "MON")

    monkeypatch.setattr(
        "core.services.db_admin_ops.create_backup_snapshot",
        lambda: {"ok": True, "backup_file": "test.sqlite3", "size_bytes": 1},
    )
    monkeypatch.setattr("core.services.scrape_ops.get_scrape_status", lambda: {"running": False})

    preview = preview_clear_section_snapshot(
        program="AI", gender="M", all_programs=False, user_id=11
    )
    assert preview["sections_count"] == 2
    assert preview["physical_sections_count"] == 1
    assert preview["shared_retained_count"] == 1
    assert preview["memberships_count"] == 2
    assert preview["meetings_count"] == 1
    assert preview["student_links_count"] == 2
    assert preview["students_count"] == 1

    from core.services.db_admin_ops import clear_section_snapshot

    result = clear_section_snapshot(
        preview_token=preview["preview_token"],
        confirm=preview["confirmation_phrase"],
        user_id=11,
    )

    assert result["deleted"] == {
        "sections": 1,
        "meetings": 1,
        "program_memberships": 2,
        "student_links": 2,
    }
    assert not TermSection.objects.filter(id=private.id).exists()
    assert not TermSectionMeeting.objects.filter(id=private_meeting.id).exists()
    assert TermSection.objects.filter(id=shared.id).exists()
    assert TermSectionMeeting.objects.filter(id=shared_meeting.id).exists()
    assert list(shared.program_links.values_list("program", flat=True)) == ["DS"]
    assert list(shared.student_sections.values_list("student_id", flat=True)) == [2001]
    assert TermSection.objects.filter(id=female.id).exists()
    assert TermSection.objects.filter(id=scenario_section.id).exists()


def test_clear_retains_global_section_referenced_by_planner(
    monkeypatch: MonkeyPatch,
) -> None:
    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="working planner"
    )
    board = DeliveryBoard.objects.create(scenario=scenario, label="AI level 4")
    section = _section("AI311", "M1")
    TermSectionProgram.objects.create(term_section=section, program="AI")
    meeting = _meeting(section)
    SectionPlacement.objects.create(
        board=board,
        term_section=section,
        day="SUN",
        start_time="09:00",
        end_time="10:00",
    )
    monkeypatch.setattr(
        "core.services.db_admin_ops.create_backup_snapshot",
        lambda: {"ok": True, "backup_file": "test.sqlite3", "size_bytes": 1},
    )
    monkeypatch.setattr("core.services.scrape_ops.get_scrape_status", lambda: {"running": False})

    preview = preview_clear_section_snapshot(
        program="", gender="ALL", all_programs=True, user_id=12
    )
    assert preview["sections_count"] == 1
    assert preview["physical_sections_count"] == 0
    assert preview["protected_sections_count"] == 1
    assert preview["meetings_count"] == 0

    from core.services.db_admin_ops import clear_section_snapshot

    result = clear_section_snapshot(
        preview_token=preview["preview_token"],
        confirm="CLEAR 1",
        user_id=12,
    )

    assert result["deleted"]["sections"] == 0
    assert TermSection.objects.filter(id=section.id).exists()
    assert TermSectionMeeting.objects.filter(id=meeting.id).exists()
    assert SectionPlacement.objects.filter(term_section=section).exists()


def test_endpoint_rejects_stale_preview_without_deleting(monkeypatch: MonkeyPatch) -> None:
    client = Client()
    _login(client, "section-admin")
    section = _section("DS221", "M1")
    TermSectionProgram.objects.create(term_section=section, program="DS")

    preview_response = _post_json(
        client,
        "/ops/db/section-snapshot/preview/",
        {"program": "DS", "gender": "M", "all_programs": False},
    )
    assert preview_response.status_code == 200
    preview = preview_response.json()
    _meeting(section)

    monkeypatch.setattr("core.services.scrape_ops.get_scrape_status", lambda: {"running": False})
    clear_response = _post_json(
        client,
        "/ops/db/section-snapshot/clear/",
        {
            "preview_token": preview["preview_token"],
            "confirm": preview["confirmation_phrase"],
        },
    )

    assert clear_response.status_code == 409
    assert clear_response.json()["code"] == "section_snapshot_preview_stale"
    assert TermSection.objects.filter(id=section.id).exists()
    assert AuditLog.objects.filter(action="db.section_snapshot.clear", status="error").exists()


def test_endpoint_refuses_clear_while_scraper_is_running(monkeypatch: MonkeyPatch) -> None:
    client = Client()
    _login(client, "busy-admin")
    section = _section("DS222", "M1")
    TermSectionProgram.objects.create(term_section=section, program="DS")
    preview = _post_json(
        client,
        "/ops/db/section-snapshot/preview/",
        {"program": "DS", "gender": "ALL", "all_programs": False},
    ).json()

    monkeypatch.setattr(
        "core.services.scrape_ops.get_scrape_status",
        lambda: {"running": True, "pid": 123, "started_at": "now"},
    )
    response = _post_json(
        client,
        "/ops/db/section-snapshot/clear/",
        {
            "preview_token": preview["preview_token"],
            "confirm": preview["confirmation_phrase"],
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "section_snapshot_busy"
    assert TermSection.objects.filter(id=section.id).exists()


def test_endpoint_refuses_preview_while_scraper_is_running(monkeypatch: MonkeyPatch) -> None:
    client = Client()
    _login(client, "busy-preview-admin")
    section = _section("DS223", "M1")
    TermSectionProgram.objects.create(term_section=section, program="DS")

    monkeypatch.setattr(
        "core.services.scrape_ops.get_scrape_status",
        lambda: {"running": True, "pid": 456, "started_at": "now"},
    )
    response = _post_json(
        client,
        "/ops/db/section-snapshot/preview/",
        {"program": "DS", "gender": "M", "all_programs": False},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "section_snapshot_busy"
    assert response.json()["pid"] == 456
    assert TermSection.objects.filter(id=section.id).exists()


def test_section_snapshot_endpoints_are_post_only_and_super_admin_only() -> None:
    anonymous = Client()
    assert (
        _post_json(
            anonymous,
            "/ops/db/section-snapshot/preview/",
            {"program": "", "gender": "ALL", "all_programs": True},
        ).status_code
        == 401
    )

    advisor = Client()
    _login(advisor, "ordinary-advisor", ROLE_ADVISOR)
    assert (
        _post_json(
            advisor,
            "/ops/db/section-snapshot/preview/",
            {"program": "", "gender": "ALL", "all_programs": True},
        ).status_code
        == 403
    )

    admin = Client()
    _login(admin, "method-admin")
    assert admin.get("/ops/db/section-snapshot/preview/").status_code == 405
    assert admin.get("/ops/db/section-snapshot/clear/").status_code == 405


def test_section_snapshot_endpoints_require_csrf() -> None:
    client = Client(enforce_csrf_checks=True)
    _login(client, "csrf-admin")

    response = _post_json(
        client,
        "/ops/db/section-snapshot/preview/",
        {"program": "", "gender": "ALL", "all_programs": True},
    )

    assert response.status_code == 403


def test_preview_token_is_bound_to_the_admin_user(monkeypatch: MonkeyPatch) -> None:
    first_client = Client()
    _login(first_client, "first-admin")
    section = _section("CS285", "M3")
    TermSectionProgram.objects.create(term_section=section, program="AI")
    preview = _post_json(
        first_client,
        "/ops/db/section-snapshot/preview/",
        {"program": "AI", "gender": "M", "all_programs": False},
    ).json()

    second_client = Client()
    _login(second_client, "second-admin")
    monkeypatch.setattr("core.services.scrape_ops.get_scrape_status", lambda: {"running": False})
    response = _post_json(
        second_client,
        "/ops/db/section-snapshot/clear/",
        {
            "preview_token": preview["preview_token"],
            "confirm": preview["confirmation_phrase"],
        },
    )

    assert response.status_code == 400
    assert "different administrator" in response.json()["error"]
    assert TermSection.objects.filter(id=section.id).exists()

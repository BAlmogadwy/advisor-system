from __future__ import annotations

from typing import Any

from django.contrib.auth.models import User
from django.db.models import Count, Q

from core.models import (
    AcademicAdvisor,
    AuditLog,
    Course,
    DeliveryBoard,
    PlannerJob,
    Prerequisite,
    ProgrammeRequirement,
    ScenarioStudentMap,
    SectionPlacement,
    Student,
    TimetableScenario,
)
from core.services.audit import validate_hash_chain
from core.services.rbac import ROLE_ADVISOR, ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN


def _student_scope_q(scope: dict[str, Any]) -> Q:
    role = str(scope.get("role") or ROLE_ADVISOR)
    advisor_id = str(scope.get("advisor_id") or "").strip()
    departments = [
        str(item).strip().upper() for item in scope.get("departments", []) if str(item).strip()
    ]

    if role == ROLE_SUPER_ADMIN:
        return Q()
    if role == ROLE_GENERAL_ADVISOR and departments:
        return Q(program__in=departments)
    if role == ROLE_ADVISOR and advisor_id:
        return Q(advisor_id=advisor_id)
    return Q(pk__isnull=True)


def _program_breakdown(students_qs) -> list[dict[str, Any]]:
    rows = (
        students_qs.exclude(program__isnull=True)
        .exclude(program="")
        .values("program")
        .annotate(count=Count("student_id"))
        .order_by("-count", "program")[:6]
    )
    return [{"program": str(row["program"]), "count": int(row["count"])} for row in rows]


def _latest_timetable_snapshot() -> dict[str, Any]:
    scenario = TimetableScenario.objects.order_by("-updated_at", "-id").first()
    if scenario is None:
        return {
            "has_scenario": False,
            "name": "No scenario",
            "scenario_id": "",
            "status": "",
            "boards": 0,
            "students": 0,
            "placements": 0,
            "unassigned_rooms": 0,
            "cross_board_clashes": 0,
            "split_url": "/timetable-workspace/",
            "mri_url": "/timetable-workspace/",
            "graph_url": "/timetable-workspace/graph/",
        }

    boards_qs = DeliveryBoard.objects.filter(scenario=scenario)
    placements_qs = SectionPlacement.objects.filter(board__scenario=scenario)
    try:
        from core.services.timetable_workspace import detect_cross_board_conflicts

        cross_board_clashes = len(detect_cross_board_conflicts(int(scenario.id)))
    except Exception:
        cross_board_clashes = 0

    return {
        "has_scenario": True,
        "name": scenario.name,
        "scenario_id": scenario.id,
        "status": scenario.status,
        "year": scenario.academic_year,
        "term": scenario.term,
        "boards": boards_qs.count(),
        "students": ScenarioStudentMap.objects.filter(scenario=scenario).count(),
        "placements": placements_qs.count(),
        "unassigned_rooms": placements_qs.filter(Q(room="") | Q(room__iexact="UNASSIGNED")).count(),
        "cross_board_clashes": cross_board_clashes,
        "split_url": f"/timetable-workspace/split/?scenario={scenario.id}",
        "mri_url": f"/timetable-workspace/mri/?scenario={scenario.id}",
        "graph_url": f"/timetable-workspace/graph/?scenario={scenario.id}",
    }


def _recent_audit_activity() -> list[dict[str, str]]:
    rows = AuditLog.objects.order_by("-id")[:6]
    return [
        {
            "id": str(row.id),
            "ts": str(row.ts_utc or "")[:19].replace("T", " "),
            "actor": str(row.actor_username or "system"),
            "action": str(row.action or ""),
            "status": str(row.status or ""),
        }
        for row in rows
    ]


def build_dashboard_command_center(scope: dict[str, Any]) -> dict[str, Any]:
    student_scope = _student_scope_q(scope)
    students_qs = Student.objects.filter(student_scope)
    total_students = students_qs.count()
    mapped_students = students_qs.exclude(advisor_id__isnull=True).exclude(advisor_id="").count()
    low_gpa_students = students_qs.filter(gpa__isnull=False, gpa__lt=2.0).count()
    zero_load_students = students_qs.filter(
        Q(current_registered_credits__isnull=True) | Q(current_registered_credits__lte=0)
    ).count()
    near_graduation_students = students_qs.filter(total_earned_credits__gte=85).count()

    audit_chain = validate_hash_chain(limit=2000)
    latest_timetable = _latest_timetable_snapshot()
    running_jobs = PlannerJob.objects.filter(
        status__in=[PlannerJob.STATUS_QUEUED, PlannerJob.STATUS_RUNNING]
    ).count()
    failed_jobs = PlannerJob.objects.filter(status=PlannerJob.STATUS_FAILED).count()

    urgent_count = (
        low_gpa_students
        + zero_load_students
        + int(latest_timetable["cross_board_clashes"])
        + (0 if audit_chain.get("ok") else 1)
    )

    return {
        "scope": {
            "role": str(scope.get("role") or ""),
            "advisor_id": str(scope.get("advisor_id") or ""),
            "departments": list(scope.get("departments") or []),
        },
        "kpis": {
            "students": total_students,
            "mapped_students": mapped_students,
            "low_gpa": low_gpa_students,
            "zero_load": zero_load_students,
            "near_graduation": near_graduation_students,
            "urgent": urgent_count,
        },
        "academic": {
            "programs": _program_breakdown(students_qs),
            "courses": Course.objects.count(),
            "requirements": ProgrammeRequirement.objects.count(),
            "prerequisites": Prerequisite.objects.count(),
        },
        "timetable": latest_timetable,
        "operations": {
            "advisors": AcademicAdvisor.objects.count(),
            "users": User.objects.count(),
            "scenarios": TimetableScenario.objects.count(),
            "running_jobs": running_jobs,
            "failed_jobs": failed_jobs,
        },
        "audit": {
            "ok": bool(audit_chain.get("ok")),
            "checked": int(audit_chain.get("checked") or 0),
            "legacy_count": int(audit_chain.get("legacy_count") or 0),
            "hmac_count": int(audit_chain.get("hmac_count") or 0),
            "invalid_ids": audit_chain.get("invalid_ids") or [],
        },
        "recent_activity": _recent_audit_activity(),
    }

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from django.conf import settings

from core.models import (
    BoardStudentLink,
    Course,
    DeliveryBoard,
    Prerequisite,
    ProgrammeRequirement,
    SectionPlacement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.student_helpers import normalize_code
from core.services.timetable_demand import StudentCourseDemand, load_scenario_course_demands
from core.services.timetable_workspace import detect_board_conflicts, detect_cross_board_conflicts

try:
    from neo4j import GraphDatabase
except ImportError:  # pragma: no cover - depends on optional local dependency
    GraphDatabase = None  # type: ignore[assignment]


class TimetableGraphError(RuntimeError):
    """Raised when the Neo4j graph twin cannot be built or synced."""


@dataclass(frozen=True)
class Neo4jConfig:
    uri: str
    username: str
    password: str
    database: str

    @property
    def configured(self) -> bool:
        return bool(self.uri and self.username and self.password)


def get_neo4j_config() -> Neo4jConfig:
    return Neo4jConfig(
        uri=str(getattr(settings, "NEO4J_URI", "")).strip(),
        username=str(getattr(settings, "NEO4J_USERNAME", "")).strip(),
        password=str(getattr(settings, "NEO4J_PASSWORD", "")).strip(),
        database=str(getattr(settings, "NEO4J_DATABASE", "neo4j")).strip() or "neo4j",
    )


def neo4j_status() -> dict[str, Any]:
    config = get_neo4j_config()
    if GraphDatabase is None:
        return {
            "configured": config.configured,
            "driver_installed": False,
            "connected": False,
            "uri": config.uri,
            "database": config.database,
            "message": "Python neo4j driver is not installed.",
        }
    if not config.configured:
        return {
            "configured": False,
            "driver_installed": True,
            "connected": False,
            "uri": config.uri,
            "database": config.database,
            "message": "Set NEO4J_PASSWORD to enable graph sync.",
        }
    try:
        with GraphDatabase.driver(config.uri, auth=(config.username, config.password)) as driver:
            driver.verify_connectivity()
        return {
            "configured": True,
            "driver_installed": True,
            "connected": True,
            "uri": config.uri,
            "database": config.database,
            "message": "Neo4j connection is ready.",
        }
    except Exception as exc:  # pragma: no cover - depends on local Neo4j state
        return {
            "configured": True,
            "driver_installed": True,
            "connected": False,
            "uri": config.uri,
            "database": config.database,
            "message": str(exc),
        }


def _node(label: str, key: str, props: dict[str, Any]) -> dict[str, Any]:
    return {"label": label, "key": key, "props": {"key": key, **props}}


def _rel_key(
    rel_type: str,
    start_key: str,
    end_key: str,
    scenario_id: int,
    props: dict[str, Any],
) -> str:
    kind = str(props.get("kind", ""))
    board = str(props.get("board_id", props.get("board_a_id", "")))
    return f"{scenario_id}:{rel_type}:{start_key}:{end_key}:{kind}:{board}"


def _rel(
    rel_type: str,
    start_label: str,
    start_key: str,
    end_label: str,
    end_key: str,
    scenario_id: int,
    props: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rel_props = {"scenario_id": scenario_id, **(props or {})}
    rel_key = _rel_key(rel_type, start_key, end_key, scenario_id, rel_props)
    return {
        "type": rel_type,
        "rel_key": rel_key,
        "start_label": start_label,
        "start_key": start_key,
        "end_label": end_label,
        "end_key": end_key,
        "scenario_id": scenario_id,
        "props": {"rel_key": rel_key, **rel_props},
    }


def _course_key(code: object) -> str:
    return f"course:{normalize_code(code)}"


def _student_key(student_id: object) -> str:
    return f"student:{student_id}"


def _program_key(program: object) -> str:
    return f"program:{str(program or '').strip().upper()}"


def _program_codes(program: object) -> list[str]:
    codes = [
        str(part or "").strip().upper()
        for part in str(program or "").replace("/", ",").split(",")
        if str(part or "").strip()
    ]
    return sorted(set(codes))


def _plan_term_key(program: object, programme_term: object) -> str:
    program_code = str(program or "").strip().upper()
    term_value = str(programme_term or "").strip() or "unknown"
    return f"plan_term:{program_code}:{term_value}"


def _section_key(scenario_id: int, term_section_id: object) -> str:
    return f"section:{scenario_id}:{term_section_id}"


def _slot_key(scenario_id: int, day: object, start: object, end: object) -> str:
    return f"slot:{scenario_id}:{day}:{start}:{end}"


def _room_key(room: object) -> str:
    return f"room:{str(room or '').strip().upper()}"


def _instructor_key(instructor: object) -> str:
    return f"instructor:{str(instructor or '').strip().upper()}"


def _group_key(program: object, section: object) -> str:
    return f"group:{str(program or '').strip().upper()}:{str(section or '').strip().upper()}"


def _dedupe_rows(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in rows:
        key = json.dumps([row.get(field) for field in fields], sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _scenario_or_error(scenario_id: int) -> TimetableScenario:
    try:
        return TimetableScenario.objects.get(id=scenario_id)
    except TimetableScenario.DoesNotExist as exc:
        raise TimetableGraphError(f"Scenario not found: {scenario_id}") from exc


def build_scenario_graph_payload(scenario_id: int) -> dict[str, Any]:
    scenario = _scenario_or_error(scenario_id)
    nodes: list[dict[str, Any]] = []
    rels: list[dict[str, Any]] = []

    nodes.append(
        _node(
            "TTScenario",
            f"scenario:{scenario.id}",
            {
                "scenario_id": scenario.id,
                "name": scenario.name,
                "academic_year": scenario.academic_year,
                "term": scenario.term,
                "status": scenario.status,
            },
        )
    )

    boards = list(DeliveryBoard.objects.filter(scenario=scenario).order_by("display_order", "id"))
    placements = list(
        SectionPlacement.objects.filter(board__scenario=scenario)
        .select_related("board", "term_section")
        .order_by("board__display_order", "day", "start_time", "id")
    )
    scenario_demands = load_scenario_course_demands(scenario.id)
    board_links = list(
        BoardStudentLink.objects.filter(board__scenario=scenario).select_related("board")
    )

    student_ids = {demand.student_id for demand in scenario_demands}
    student_ids.update(link.student_id for link in board_links)
    student_rows = {
        row["student_id"]: row
        for row in Student.objects.filter(student_id__in=student_ids).values(
            "student_id",
            "name",
            "program",
            "section",
            "gpa",
            "total_earned_credits",
            "current_registered_credits",
            "advisor_id",
        )
    }

    programs: set[str] = set()
    for board in boards:
        programs.update(_program_codes(board.program))
    programs.update(
        str(row.get("program") or "").strip().upper()
        for row in student_rows.values()
        if str(row.get("program") or "").strip()
    )

    course_codes: set[str] = set()
    for demand in scenario_demands:
        code = normalize_code(demand.course_key)
        if code:
            course_codes.add(code)
    for placement in placements:
        course_codes.add(normalize_code(placement.term_section.course_code))

    studying_rows = list(
        StudentCourse.objects.filter(student_id__in=student_ids, status="studying")
        .select_related("course")
        .values("student_id", "course__course_code")
    )
    for row in studying_rows:
        code = normalize_code(row.get("course__course_code"))
        if code:
            course_codes.add(code)

    req_rows = list(
        ProgrammeRequirement.objects.filter(program__in=programs).values(
            "program", "course_code", "course_name", "programme_term", "credit_hours", "type"
        )
    )
    for row in req_rows:
        code = normalize_code(row.get("course_code"))
        if code:
            course_codes.add(code)

    prereq_rows = list(
        Prerequisite.objects.filter(program__in=programs).values(
            "program", "course_code", "prerequisite_course_code"
        )
    )
    for row in prereq_rows:
        code = normalize_code(row.get("course_code"))
        if code:
            course_codes.add(code)
        for part in str(row.get("prerequisite_course_code") or "").split(","):
            prereq = normalize_code(part)
            if prereq:
                course_codes.add(prereq)

    course_names = {
        normalize_code(row["course_code"]): str(row.get("description") or "")
        for row in Course.objects.filter(course_code__in=course_codes).values(
            "course_code", "description"
        )
    }
    for row in req_rows:
        code = normalize_code(row.get("course_code"))
        if code and not course_names.get(code):
            course_names[code] = str(row.get("course_name") or "")

    _add_program_nodes(nodes, rels, scenario.id, programs)
    _add_course_nodes(nodes, course_codes, course_names)
    _add_student_nodes(nodes, rels, scenario.id, student_ids, student_rows)
    _add_board_nodes(nodes, rels, scenario.id, boards)
    _add_student_course_rels(rels, scenario.id, scenario_demands, studying_rows)
    _add_curriculum_rels(nodes, rels, scenario.id, req_rows, prereq_rows)
    _add_section_nodes_and_rels(nodes, rels, scenario.id, placements)
    _add_current_section_rels(rels, scenario.id, student_ids, placements)
    _add_clash_rels(rels, scenario.id, boards, placements)

    nodes = _dedupe_rows(nodes, ("label", "key"))
    rels = _dedupe_rows(rels, ("rel_key",))
    return _payload_from_rows(scenario, nodes, rels, boards, placements, student_ids, course_codes)


def _add_program_nodes(
    nodes: list[dict[str, Any]],
    rels: list[dict[str, Any]],
    scenario_id: int,
    programs: set[str],
) -> None:
    for program in sorted(programs):
        nodes.append(_node("TTProgram", _program_key(program), {"code": program}))


def _add_course_nodes(
    nodes: list[dict[str, Any]], course_codes: set[str], course_names: dict[str, str]
) -> None:
    for code in sorted(c for c in course_codes if c):
        nodes.append(
            _node("TTCourse", _course_key(code), {"code": code, "name": course_names.get(code, "")})
        )


def _add_student_nodes(
    nodes: list[dict[str, Any]],
    rels: list[dict[str, Any]],
    scenario_id: int,
    student_ids: set[int],
    student_rows: dict[int, dict[str, Any]],
) -> None:
    for sid in sorted(student_ids):
        row = student_rows.get(sid, {})
        program = str(row.get("program") or "").strip().upper()
        section = str(row.get("section") or "").strip()
        nodes.append(
            _node(
                "TTStudent",
                _student_key(sid),
                {
                    "student_id": sid,
                    "name": str(row.get("name") or ""),
                    "program": program,
                    "section": section,
                    "gpa": row.get("gpa"),
                    "total_earned_credits": row.get("total_earned_credits"),
                    "current_registered_credits": row.get("current_registered_credits"),
                    "advisor_id": str(row.get("advisor_id") or ""),
                },
            )
        )
        if program:
            rels.append(
                _rel(
                    "ENROLLED_IN",
                    "TTStudent",
                    _student_key(sid),
                    "TTProgram",
                    _program_key(program),
                    scenario_id,
                )
            )
        if section:
            group_key = _group_key(program, section)
            nodes.append(_node("TTGroup", group_key, {"program": program, "section": section}))
            rels.append(
                _rel(
                    "BELONGS_TO", "TTStudent", _student_key(sid), "TTGroup", group_key, scenario_id
                )
            )


def _add_board_nodes(
    nodes: list[dict[str, Any]],
    rels: list[dict[str, Any]],
    scenario_id: int,
    boards: list[DeliveryBoard],
) -> None:
    for board in boards:
        board_key = f"board:{board.id}"
        nodes.append(
            _node(
                "TTBoard",
                board_key,
                {
                    "scenario_id": scenario_id,
                    "board_id": board.id,
                    "label": board.label,
                    "program": str(board.program or "").strip().upper(),
                    "programs": _program_codes(board.program),
                    "nominal_term": board.nominal_term,
                    "board_type": board.board_type,
                    "target_size": board.target_size,
                    "display_order": board.display_order,
                },
            )
        )
        rels.append(
            _rel(
                "IN_SCENARIO",
                "TTBoard",
                board_key,
                "TTScenario",
                f"scenario:{scenario_id}",
                scenario_id,
            )
        )
        for program in _program_codes(board.program):
            if not board.nominal_term:
                continue
            plan_term_key = _plan_term_key(program, board.nominal_term)
            nodes.append(
                _node(
                    "TTPlanTerm",
                    plan_term_key,
                    {
                        "program": program,
                        "programme_term": board.nominal_term,
                        "label": f"{program} Term {board.nominal_term}",
                    },
                )
            )
            rels.append(
                _rel(
                    "HAS_PLAN_TERM",
                    "TTProgram",
                    _program_key(program),
                    "TTPlanTerm",
                    plan_term_key,
                    scenario_id,
                )
            )
            rels.append(
                _rel(
                    "HAS_GROUP",
                    "TTPlanTerm",
                    plan_term_key,
                    "TTBoard",
                    board_key,
                    scenario_id,
                    {"board_id": board.id},
                )
            )
        for link in BoardStudentLink.objects.filter(board=board):
            rels.append(
                _rel(
                    "ON_BOARD",
                    "TTStudent",
                    _student_key(link.student_id),
                    "TTBoard",
                    board_key,
                    scenario_id,
                    {"link_type": link.link_type},
                )
            )


def _add_student_course_rels(
    rels: list[dict[str, Any]],
    scenario_id: int,
    scenario_demands: list[StudentCourseDemand],
    studying_rows: list[dict[str, Any]],
) -> None:
    for demand in scenario_demands:
        code = normalize_code(demand.course_key)
        if code:
            rels.append(
                _rel(
                    "NEEDS_IN_SCENARIO",
                    "TTStudent",
                    _student_key(demand.student_id),
                    "TTCourse",
                    _course_key(code),
                    scenario_id,
                    {
                        "primary_term": demand.primary_term,
                        "is_cross_term": demand.is_cross_term,
                        "status": demand.status,
                        "priority": demand.priority,
                    },
                )
            )
    for row in studying_rows:
        code = normalize_code(row.get("course__course_code"))
        if code:
            rels.append(
                _rel(
                    "STUDYING_NOW",
                    "TTStudent",
                    _student_key(row["student_id"]),
                    "TTCourse",
                    _course_key(code),
                    scenario_id,
                )
            )


def _add_curriculum_rels(
    nodes: list[dict[str, Any]],
    rels: list[dict[str, Any]],
    scenario_id: int,
    req_rows: list[dict[str, Any]],
    prereq_rows: list[dict[str, Any]],
) -> None:
    for row in req_rows:
        program = str(row.get("program") or "").strip().upper()
        code = normalize_code(row.get("course_code"))
        programme_term = row.get("programme_term")
        if program and programme_term:
            plan_term_key = _plan_term_key(program, programme_term)
            nodes.append(
                _node(
                    "TTPlanTerm",
                    plan_term_key,
                    {
                        "program": program,
                        "programme_term": programme_term,
                        "label": f"{program} Term {programme_term}",
                    },
                )
            )
            rels.append(
                _rel(
                    "HAS_PLAN_TERM",
                    "TTProgram",
                    _program_key(program),
                    "TTPlanTerm",
                    plan_term_key,
                    scenario_id,
                )
            )
            if code:
                rels.append(
                    _rel(
                        "TERM_REQUIRES",
                        "TTPlanTerm",
                        plan_term_key,
                        "TTCourse",
                        _course_key(code),
                        scenario_id,
                        {
                            "programme_term": programme_term,
                            "credit_hours": row.get("credit_hours"),
                            "type": str(row.get("type") or ""),
                        },
                    )
                )
        if program and code:
            rels.append(
                _rel(
                    "REQUIRES",
                    "TTProgram",
                    _program_key(program),
                    "TTCourse",
                    _course_key(code),
                    scenario_id,
                    {
                        "programme_term": row.get("programme_term"),
                        "credit_hours": row.get("credit_hours"),
                        "type": str(row.get("type") or ""),
                    },
                )
            )
    for row in prereq_rows:
        code = normalize_code(row.get("course_code"))
        for part in str(row.get("prerequisite_course_code") or "").split(","):
            prereq = normalize_code(part)
            if code and prereq:
                rels.append(
                    _rel(
                        "REQUIRES_PREREQUISITE",
                        "TTCourse",
                        _course_key(code),
                        "TTCourse",
                        _course_key(prereq),
                        scenario_id,
                        {"program": str(row.get("program") or "").strip().upper()},
                    )
                )


def _add_section_nodes_and_rels(
    nodes: list[dict[str, Any]],
    rels: list[dict[str, Any]],
    scenario_id: int,
    placements: list[SectionPlacement],
) -> None:
    section_ids = {p.term_section_id for p in placements}
    meetings_by_section: dict[int, list[TermSectionMeeting]] = {}
    for meeting in TermSectionMeeting.objects.filter(term_section_id__in=section_ids):
        meetings_by_section.setdefault(meeting.term_section_id, []).append(meeting)

    for placement in placements:
        ts = placement.term_section
        section_key = _section_key(scenario_id, ts.id)
        course_code = normalize_code(ts.course_code)
        instructors = sorted(
            {
                str(m.instructor).strip()
                for m in meetings_by_section.get(ts.id, [])
                if str(m.instructor).strip()
            }
        )
        nodes.append(
            _node(
                "TTSection",
                section_key,
                {
                    "scenario_id": scenario_id,
                    "term_section_id": ts.id,
                    "course_code": course_code,
                    "course_name": ts.course_name,
                    "course_key": ts.course_key,
                    "section": ts.section,
                    "available_capacity": ts.available_capacity,
                    "registered_count": ts.registered_count,
                    "placement_id": placement.id,
                    "board_id": placement.board_id,
                    "board_label": placement.board.label,
                    "day": placement.day,
                    "start_time": placement.start_time,
                    "end_time": placement.end_time,
                    "room": placement.room,
                    "is_locked": placement.is_locked,
                    "instructors": ", ".join(instructors),
                },
            )
        )
        rels.append(
            _rel(
                "OF_COURSE",
                "TTSection",
                section_key,
                "TTCourse",
                _course_key(course_code),
                scenario_id,
            )
        )
        rels.append(
            _rel(
                "ON_BOARD",
                "TTSection",
                section_key,
                "TTBoard",
                f"board:{placement.board_id}",
                scenario_id,
            )
        )
        rels.append(
            _rel(
                "SCHEDULES_COURSE",
                "TTBoard",
                f"board:{placement.board_id}",
                "TTCourse",
                _course_key(course_code),
                scenario_id,
                {"board_id": placement.board_id},
            )
        )

        slot_key = _slot_key(scenario_id, placement.day, placement.start_time, placement.end_time)
        nodes.append(
            _node(
                "TTSlot",
                slot_key,
                {
                    "scenario_id": scenario_id,
                    "day": placement.day,
                    "start_time": placement.start_time,
                    "end_time": placement.end_time,
                },
            )
        )
        rels.append(
            _rel(
                "PLACED_IN",
                "TTSection",
                section_key,
                "TTSlot",
                slot_key,
                scenario_id,
                {"placement_id": placement.id, "is_locked": placement.is_locked},
            )
        )

        if placement.room and placement.room.strip().upper() != "UNASSIGNED":
            room_key = _room_key(placement.room)
            nodes.append(_node("TTRoom", room_key, {"room": placement.room.strip()}))
            rels.append(
                _rel("USES_ROOM", "TTSection", section_key, "TTRoom", room_key, scenario_id)
            )

        for instructor in instructors:
            instructor_key = _instructor_key(instructor)
            nodes.append(_node("TTInstructor", instructor_key, {"name": instructor}))
            rels.append(
                _rel(
                    "TAUGHT_BY",
                    "TTSection",
                    section_key,
                    "TTInstructor",
                    instructor_key,
                    scenario_id,
                )
            )


def _add_current_section_rels(
    rels: list[dict[str, Any]],
    scenario_id: int,
    student_ids: set[int],
    placements: list[SectionPlacement],
) -> None:
    section_ids = {p.term_section_id for p in placements}
    rows = StudentTermSection.objects.filter(
        student_id__in=student_ids,
        term_section_id__in=section_ids,
    ).values("student_id", "term_section_id", "academic_year", "term", "source")
    for row in rows:
        rels.append(
            _rel(
                "CURRENTLY_REGISTERED_IN",
                "TTStudent",
                _student_key(row["student_id"]),
                "TTSection",
                _section_key(scenario_id, row["term_section_id"]),
                scenario_id,
                {
                    "academic_year": row.get("academic_year"),
                    "term": row.get("term"),
                    "source": row.get("source"),
                },
            )
        )


def _add_clash_rels(
    rels: list[dict[str, Any]],
    scenario_id: int,
    boards: list[DeliveryBoard],
    placements: list[SectionPlacement],
) -> None:
    placements_by_id = {p.id: p for p in placements}
    for board in boards:
        conflicts = detect_board_conflicts(board.id)
        for conflict_kind in ("overlaps", "instructor_clashes", "room_clashes"):
            for conflict in conflicts.get(conflict_kind, []):
                _append_conflict_rel(
                    rels, scenario_id, placements_by_id, conflict, conflict_kind, board.id
                )

    for conflict in detect_cross_board_conflicts(scenario_id):
        _append_conflict_rel(
            rels,
            scenario_id,
            placements_by_id,
            {
                "ids": [conflict.get("placement_a_id"), conflict.get("placement_b_id")],
                "kind": "cross_board_student_clash",
                "severity": "critical",
                "detail": conflict.get("time", ""),
                "shared_students": conflict.get("overlap_count", 0),
                "board_a_id": conflict.get("board_a_id"),
                "board_b_id": conflict.get("board_b_id"),
            },
            "cross_board_student_clash",
            int(conflict.get("board_a_id") or 0),
        )


def _append_conflict_rel(
    rels: list[dict[str, Any]],
    scenario_id: int,
    placements_by_id: dict[int, SectionPlacement],
    conflict: dict[str, Any],
    conflict_kind: str,
    board_id: int,
) -> None:
    ids = conflict.get("ids", [])
    if not isinstance(ids, list) or len(ids) < 2:
        return
    pa = placements_by_id.get(ids[0])
    pb = placements_by_id.get(ids[1])
    if not pa or not pb:
        return
    rels.append(
        _rel(
            "CLASHES_WITH",
            "TTSection",
            _section_key(scenario_id, pa.term_section_id),
            "TTSection",
            _section_key(scenario_id, pb.term_section_id),
            scenario_id,
            {
                "kind": str(conflict.get("kind") or conflict_kind),
                "board_id": board_id,
                "severity": conflict.get(
                    "severity", "warning" if conflict_kind == "room_clashes" else "critical"
                ),
                "detail": conflict.get("detail", ""),
                "shared_students": conflict.get("shared_students"),
                "board_a_id": conflict.get("board_a_id"),
                "board_b_id": conflict.get("board_b_id"),
            },
        )
    )


def _payload_from_rows(
    scenario: TimetableScenario,
    nodes: list[dict[str, Any]],
    rels: list[dict[str, Any]],
    boards: list[DeliveryBoard],
    placements: list[SectionPlacement],
    student_ids: set[int],
    course_codes: set[str],
) -> dict[str, Any]:
    node_counts = dict(Counter(row["label"] for row in nodes))
    rel_counts = dict(Counter(row["type"] for row in rels))
    return {
        "scenario": {
            "id": scenario.id,
            "name": scenario.name,
            "academic_year": scenario.academic_year,
            "term": scenario.term,
            "status": scenario.status,
        },
        "nodes": nodes,
        "relationships": rels,
        "summary": {
            "node_count": len(nodes),
            "relationship_count": len(rels),
            "node_counts": node_counts,
            "relationship_counts": rel_counts,
            "boards": len(boards),
            "placements": len(placements),
            "students": len(student_ids),
            "courses": len(course_codes),
        },
    }


def build_scenario_graph_summary(scenario_id: int) -> dict[str, Any]:
    payload = build_scenario_graph_payload(scenario_id)
    return {
        "scenario": payload["scenario"],
        "summary": payload["summary"],
        "samples": {
            "nodes": payload["nodes"][:12],
            "relationships": payload["relationships"][:16],
        },
        "design": {
            "source_of_truth": "Django timetable scenario tables",
            "neo4j_role": "Disposable graph twin for relationship analysis and explanation",
            "student_edges": [
                "ENROLLED_IN",
                "BELONGS_TO",
                "STUDYING_NOW",
                "CURRENTLY_REGISTERED_IN",
                "NEEDS_IN_SCENARIO",
            ],
        },
    }


def build_scenario_graph_view(
    scenario_id: int,
    *,
    mode: str = "clashes",
    limit: int = 80,
    program: str = "",
    plan_term: str = "",
    include_students: bool = False,
    progressive: bool = False,
) -> dict[str, Any]:
    """Return a focused graph slice suitable for the embedded UI viewer."""
    payload = build_scenario_graph_payload(scenario_id)
    mode = mode if mode in {"plan", "clashes", "students", "placements"} else "clashes"
    max_limit = 1200 if mode == "plan" and progressive else 260
    limit = max(20, min(max_limit, int(limit or 80)))
    program_filter = str(program or "").strip().upper()
    term_filter = str(plan_term or "").strip()
    nodes_by_key = {row["key"]: row for row in payload["nodes"]}
    selected_nodes: dict[str, dict[str, Any]] = {}
    selected_edges: dict[str, dict[str, Any]] = {}

    def add_node(key: str) -> None:
        row = nodes_by_key.get(key)
        if row:
            selected_nodes[key] = _viewer_node(row)

    def add_edge(rel: dict[str, Any]) -> None:
        add_node(rel["start_key"])
        add_node(rel["end_key"])
        selected_edges[rel["rel_key"]] = _viewer_edge(rel)

    rels = payload["relationships"]
    if mode == "plan":
        _select_plan_view(
            rels,
            add_edge,
            limit,
            program_filter=program_filter,
            term_filter=term_filter,
            include_students=include_students or progressive,
        )
        if progressive:
            _add_progressive_plan_root(payload["scenario"], selected_nodes, add_node, add_edge)
    elif mode == "clashes":
        _select_clash_view(rels, add_edge, limit)
    elif mode == "students":
        _select_student_view(payload["nodes"], rels, add_edge, limit)
    else:
        _select_placement_view(rels, add_edge, limit)

    return {
        "scenario": payload["scenario"],
        "mode": mode,
        "nodes": list(selected_nodes.values()),
        "edges": list(selected_edges.values()),
        "summary": {
            "nodes": len(selected_nodes),
            "edges": len(selected_edges),
            "source_nodes": payload["summary"]["node_count"],
            "source_edges": payload["summary"]["relationship_count"],
        },
        "tree": {
            "program": program_filter,
            "plan_term": term_filter,
            "include_students": bool(include_students or progressive),
            "progressive": bool(progressive),
            "levels": [
                "Scenario",
                "Program",
                "Plan Term",
                "Term Group",
                "Course",
                "Section",
                "Student",
            ],
            "node_counts": dict(Counter(node["type"] for node in selected_nodes.values())),
        },
        "filters": _plan_filter_options(payload["nodes"]),
        "legend": {
            "TTStudent": "Student",
            "TTProgram": "Program",
            "TTPlanTerm": "Plan term",
            "TTCourse": "Course",
            "TTSection": "Section",
            "TTSlot": "Time slot",
            "TTBoard": "Term group / board",
            "HAS_PROGRAM": "Scenario contains program",
            "CLASHES_WITH": "Verified clash relationship",
        },
    }


def _add_progressive_plan_root(
    scenario: dict[str, Any],
    selected_nodes: dict[str, dict[str, Any]],
    add_node: Any,
    add_edge: Any,
) -> None:
    """Add a scenario root so the UI can expand the plan like Neo4j Browser."""
    scenario_key = f"scenario:{scenario['id']}"
    add_node(scenario_key)
    program_keys = sorted(
        key for key, node in selected_nodes.items() if node.get("type") == "TTProgram"
    )
    for program_key in program_keys:
        add_edge(
            {
                "type": "HAS_PROGRAM",
                "rel_key": f"{scenario['id']}:HAS_PROGRAM:{scenario_key}:{program_key}",
                "start_label": "TTScenario",
                "start_key": scenario_key,
                "end_label": "TTProgram",
                "end_key": program_key,
                "scenario_id": scenario["id"],
                "props": {
                    "rel_key": f"{scenario['id']}:HAS_PROGRAM:{scenario_key}:{program_key}",
                    "scenario_id": scenario["id"],
                    "kind": "plan_navigation",
                },
            }
        )


def _plan_filter_options(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    programs = sorted(
        str(row.get("props", {}).get("code") or "").strip().upper()
        for row in nodes
        if row["label"] == "TTProgram" and str(row.get("props", {}).get("code") or "").strip()
    )
    terms_by_program: dict[str, set[str]] = {}
    for row in nodes:
        if row["label"] != "TTPlanTerm":
            continue
        props = row.get("props", {})
        program = str(props.get("program") or "").strip().upper()
        programme_term = str(props.get("programme_term") or "").strip()
        if program and programme_term:
            terms_by_program.setdefault(program, set()).add(programme_term)
    return {
        "programs": programs,
        "terms_by_program": {
            program: sorted(terms, key=lambda value: (not value.isdigit(), value))
            for program, terms in sorted(terms_by_program.items())
        },
    }


def _select_plan_view(
    rels: list[dict[str, Any]],
    add_edge: Any,
    limit: int,
    *,
    program_filter: str = "",
    term_filter: str = "",
    include_students: bool = False,
) -> None:
    """Select the top-down plan path: program, term, group, course, section, students."""
    budget = limit * 3
    edge_count = 0
    board_keys: set[str] = set()
    course_keys: set[str] = set()
    section_keys: set[str] = set()
    section_board: dict[str, str] = {}
    section_course: dict[str, str] = {}
    board_students: dict[str, set[str]] = {}
    course_students: dict[str, set[str]] = {}
    scenario_id = (
        int(rels[0].get("scenario_id") or rels[0].get("props", {}).get("scenario_id") or 0)
        if rels
        else 0
    )

    for rel in rels:
        if rel["type"] == "ON_BOARD" and rel["start_label"] == "TTSection":
            section_board[rel["start_key"]] = rel["end_key"]
        elif rel["type"] == "OF_COURSE" and rel["start_label"] == "TTSection":
            section_course[rel["start_key"]] = rel["end_key"]
        elif rel["type"] == "ON_BOARD" and rel["start_label"] == "TTStudent":
            board_students.setdefault(rel["end_key"], set()).add(rel["start_key"])
        elif (
            rel["type"] in {"NEEDS_IN_SCENARIO", "STUDYING_NOW"}
            and rel["start_label"] == "TTStudent"
        ):
            course_students.setdefault(rel["end_key"], set()).add(rel["start_key"])

    def program_matches(key: str) -> bool:
        return not program_filter or key == _program_key(program_filter)

    def plan_term_matches(key: str) -> bool:
        return not term_filter or key.rsplit(":", 1)[-1] == term_filter

    def add_plan_edge(rel: dict[str, Any], rel_type: str, source: str, target: str) -> None:
        add_edge(
            {
                **rel,
                "type": rel_type,
                "rel_key": f"{rel['rel_key']}:plan:{rel_type}",
                "start_key": source,
                "end_key": target,
                "props": {**rel.get("props", {}), "kind": "plan_navigation"},
            }
        )

    group_rels = [
        rel
        for rel in rels
        if rel["type"] == "HAS_GROUP"
        and (not program_filter or rel["start_key"].split(":")[1] == program_filter)
        and plan_term_matches(rel["start_key"])
    ]
    plan_term_keys = {rel["start_key"] for rel in group_rels}

    for rel in rels:
        if (
            rel["type"] == "HAS_PLAN_TERM"
            and rel["end_key"] in plan_term_keys
            and program_matches(rel["start_key"])
            and plan_term_matches(rel["end_key"])
        ):
            add_edge(rel)
            edge_count += 1

    for rel in group_rels:
        if edge_count >= budget:
            return
        if rel["start_key"] in plan_term_keys:
            add_edge(rel)
            board_keys.add(rel["end_key"])
            edge_count += 1
        if edge_count >= budget:
            return

    overview_only = not program_filter and not term_filter and not include_students
    if overview_only:
        return

    for rel in rels:
        if rel["type"] == "SCHEDULES_COURSE" and rel["start_key"] in board_keys:
            add_edge(rel)
            course_keys.add(rel["end_key"])
            edge_count += 1
        if edge_count >= budget:
            return

    for rel in rels:
        if (
            rel["type"] == "OF_COURSE"
            and section_board.get(rel["start_key"]) in board_keys
            and rel["end_key"] in course_keys
        ):
            add_plan_edge(rel, "HAS_SECTION", rel["end_key"], rel["start_key"])
            section_keys.add(rel["start_key"])
            edge_count += 1
        if edge_count >= budget:
            return

    direct_section_students: set[tuple[str, str]] = set()
    for rel in rels:
        if not include_students:
            break
        if rel["type"] == "CURRENTLY_REGISTERED_IN" and rel["end_key"] in section_keys:
            add_plan_edge(rel, "HAS_ENROLLED_STUDENT", rel["end_key"], rel["start_key"])
            direct_section_students.add((rel["end_key"], rel["start_key"]))
            edge_count += 1
        if edge_count >= budget:
            return

    if include_students:
        for section_key in sorted(section_keys):
            if any(item[0] == section_key for item in direct_section_students):
                continue
            board_key = section_board.get(section_key)
            course_key = section_course.get(section_key)
            if not board_key or not course_key:
                continue
            inferred_students = sorted(
                board_students.get(board_key, set()) & course_students.get(course_key, set())
            )
            for student_key in inferred_students:
                add_edge(
                    {
                        "type": "HAS_ENROLLED_STUDENT",
                        "rel_key": f"{scenario_id}:plan:{section_key}:{student_key}:inferred_student",
                        "start_label": "TTSection",
                        "start_key": section_key,
                        "end_label": "TTStudent",
                        "end_key": student_key,
                        "scenario_id": scenario_id,
                        "props": {
                            "kind": "board_roster_course_match",
                            "source": "board roster + student course evidence",
                        },
                    }
                )
                edge_count += 1
                if edge_count >= budget:
                    return


def _select_clash_view(rels: list[dict[str, Any]], add_edge: Any, limit: int) -> None:
    clash_rels = sorted(
        [rel for rel in rels if rel["type"] == "CLASHES_WITH"],
        key=lambda rel: (
            str(rel.get("props", {}).get("severity") or "") != "critical",
            -int(rel.get("props", {}).get("shared_students") or 0),
        ),
    )[:limit]
    section_keys: set[str] = set()
    for rel in clash_rels:
        add_edge(rel)
        section_keys.add(rel["start_key"])
        section_keys.add(rel["end_key"])

    context_types = {"OF_COURSE", "PLACED_IN", "ON_BOARD", "TAUGHT_BY", "USES_ROOM"}
    for rel in rels:
        if rel["type"] in context_types and rel["start_key"] in section_keys:
            add_edge(rel)


def _select_student_view(
    nodes: list[dict[str, Any]],
    rels: list[dict[str, Any]],
    add_edge: Any,
    limit: int,
) -> None:
    student_keys = [row["key"] for row in nodes if row["label"] == "TTStudent"][
        : max(8, limit // 4)
    ]
    student_key_set = set(student_keys)
    student_rel_types = {
        "ENROLLED_IN",
        "BELONGS_TO",
        "STUDYING_NOW",
        "CURRENTLY_REGISTERED_IN",
        "NEEDS_IN_SCENARIO",
    }
    count = 0
    for rel in rels:
        if rel["start_key"] in student_key_set and rel["type"] in student_rel_types:
            add_edge(rel)
            count += 1
        if count >= limit * 2:
            break


def _select_placement_view(rels: list[dict[str, Any]], add_edge: Any, limit: int) -> None:
    placement_types = {"IN_SCENARIO", "ON_BOARD", "OF_COURSE", "PLACED_IN"}
    count = 0
    for rel in rels:
        if rel["type"] in placement_types:
            add_edge(rel)
            count += 1
        if count >= limit * 2:
            break


def _compact_meta(items: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in items.items()
        if value is not None and value != "" and value != []
    }


def _viewer_node(row: dict[str, Any]) -> dict[str, Any]:
    props = row.get("props", {})
    label = row["label"]
    display = {
        "TTScenario": props.get("name"),
        "TTProgram": props.get("code"),
        "TTPlanTerm": props.get("label"),
        "TTCourse": props.get("code"),
        "TTStudent": props.get("student_id"),
        "TTGroup": f"{props.get('program', '')} {props.get('section', '')}".strip(),
        "TTBoard": props.get("label"),
        "TTSection": f"{props.get('course_code', '')} {props.get('section', '')}".strip(),
        "TTSlot": f"{props.get('day', '')} {props.get('start_time', '')}",
        "TTRoom": props.get("room"),
        "TTInstructor": props.get("name"),
    }.get(label)
    detail = {
        "TTStudent": f"{props.get('program', '')} {props.get('section', '')}".strip(),
        "TTSection": (
            f"{props.get('course_name', '')} | "
            f"{props.get('day', '')} {props.get('start_time', '')}-{props.get('end_time', '')}"
        ).strip(" |"),
        "TTSlot": f"{props.get('start_time', '')}-{props.get('end_time', '')}",
        "TTBoard": f"Term {props.get('nominal_term') or '-'} | {props.get('program') or ''}",
        "TTPlanTerm": f"Program {props.get('program') or ''}",
        "TTCourse": props.get("name", ""),
    }.get(label, "")
    meta = {
        "TTStudent": _compact_meta(
            {
                "Program": props.get("program"),
                "Group": props.get("section"),
                "Credits earned": props.get("total_earned_credits"),
                "Current credits": props.get("current_registered_credits"),
                "GPA": props.get("gpa"),
            }
        ),
        "TTProgram": _compact_meta({"Program": props.get("code")}),
        "TTPlanTerm": _compact_meta(
            {"Program": props.get("program"), "Plan term": props.get("programme_term")}
        ),
        "TTBoard": _compact_meta(
            {
                "Program": props.get("program"),
                "Term": props.get("nominal_term"),
                "Target students": props.get("target_size"),
                "Board type": props.get("board_type"),
            }
        ),
        "TTCourse": _compact_meta({"Course": props.get("code"), "Name": props.get("name")}),
        "TTSection": _compact_meta(
            {
                "Course": props.get("course_code"),
                "Section": props.get("section"),
                "Placement": props.get("placement_id"),
                "Board": props.get("board_label"),
                "Day": props.get("day"),
                "Time": (
                    f"{props.get('start_time', '')}-{props.get('end_time', '')}"
                    if props.get("start_time") or props.get("end_time")
                    else None
                ),
                "Room": props.get("room"),
                "Instructor": props.get("instructors"),
                "Capacity": props.get("available_capacity"),
                "Registered": props.get("registered_count"),
                "Locked": "Yes" if props.get("is_locked") else "No",
            }
        ),
        "TTSlot": _compact_meta(
            {
                "Day": props.get("day"),
                "Start": props.get("start_time"),
                "End": props.get("end_time"),
            }
        ),
    }.get(label, {})
    return {
        "id": row["key"],
        "label": str(display or row["key"]),
        "type": label,
        "group": label.replace("TT", ""),
        "detail": str(detail or ""),
        "meta": meta,
        "size": _viewer_node_size(label),
    }


def _viewer_edge(rel: dict[str, Any]) -> dict[str, Any]:
    props = rel.get("props", {})
    tone = "neutral"
    if rel["type"] == "CLASHES_WITH":
        tone = "critical" if props.get("severity") == "critical" else "warning"
    elif rel["type"] in {"CURRENTLY_REGISTERED_IN", "PLACED_IN", "STUDYING_NOW"}:
        tone = "active"
    return {
        "id": rel["rel_key"],
        "source": rel["start_key"],
        "target": rel["end_key"],
        "type": rel["type"],
        "label": rel["type"],
        "tone": tone,
        "detail": str(props.get("kind") or props.get("detail") or props.get("link_type") or ""),
        "shared_students": props.get("shared_students"),
        "meta": _compact_meta(
            {
                "Kind": props.get("kind"),
                "Severity": props.get("severity"),
                "Shared students": props.get("shared_students"),
                "Board": props.get("board_id"),
                "Placement": props.get("placement_id"),
                "Locked": props.get("is_locked"),
            }
        ),
    }


def _viewer_node_size(label: str) -> int:
    return {
        "TTStudent": 9,
        "TTCourse": 12,
        "TTSection": 15,
        "TTSlot": 11,
        "TTBoard": 16,
        "TTPlanTerm": 15,
        "TTProgram": 13,
        "TTGroup": 12,
    }.get(label, 10)


_CONSTRAINTS = {
    "TTScenario": "tt_scenario_key",
    "TTProgram": "tt_program_key",
    "TTPlanTerm": "tt_plan_term_key",
    "TTCourse": "tt_course_key",
    "TTStudent": "tt_student_key",
    "TTGroup": "tt_group_key",
    "TTBoard": "tt_board_key",
    "TTSection": "tt_section_key",
    "TTSlot": "tt_slot_key",
    "TTRoom": "tt_room_key",
    "TTInstructor": "tt_instructor_key",
}


def _driver_or_error() -> Any:
    if GraphDatabase is None:
        raise TimetableGraphError("Install the neo4j Python driver to enable graph sync.")
    config = get_neo4j_config()
    if not config.configured:
        raise TimetableGraphError("Set NEO4J_PASSWORD to enable graph sync.")
    return GraphDatabase.driver(config.uri, auth=(config.username, config.password))


def _session_kwargs(config: Neo4jConfig) -> dict[str, str]:
    return {"database": config.database} if config.database else {}


def _create_constraints(session: Any) -> None:
    for label, name in _CONSTRAINTS.items():
        session.run(
            f"CREATE CONSTRAINT {name} IF NOT EXISTS FOR (n:{label}) REQUIRE n.key IS UNIQUE"
        )


def _clear_scenario(session: Any, scenario_id: int) -> None:
    session.run("MATCH ()-[r {scenario_id: $scenario_id}]-() DELETE r", scenario_id=scenario_id)
    for label in ("TTScenario", "TTBoard", "TTSection", "TTSlot"):
        session.run(
            f"MATCH (n:{label} {{scenario_id: $scenario_id}}) DETACH DELETE n",
            scenario_id=scenario_id,
        )
    for label in (
        "TTStudent",
        "TTProgram",
        "TTPlanTerm",
        "TTCourse",
        "TTGroup",
        "TTRoom",
        "TTInstructor",
    ):
        session.run(f"MATCH (n:{label}) WHERE NOT (n)--() DELETE n")


def _merge_nodes(session: Any, nodes: list[dict[str, Any]]) -> None:
    for label in _CONSTRAINTS:
        rows = [{"key": n["key"], "props": n["props"]} for n in nodes if n["label"] == label]
        if rows:
            session.run(
                f"""
                UNWIND $rows AS row
                MERGE (n:{label} {{key: row.key}})
                SET n += row.props
                """,
                rows=rows,
            )


def _merge_relationships(session: Any, relationships: list[dict[str, Any]]) -> None:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for rel in relationships:
        grouped.setdefault((rel["type"], rel["start_label"], rel["end_label"]), []).append(rel)

    for (rel_type, start_label, end_label), rows in grouped.items():
        session.run(
            f"""
            UNWIND $rows AS row
            MATCH (a:{start_label} {{key: row.start_key}})
            MATCH (b:{end_label} {{key: row.end_key}})
            MERGE (a)-[r:{rel_type} {{rel_key: row.rel_key}}]->(b)
            SET r += row.props
            """,
            rows=rows,
        )


def sync_scenario_graph_to_neo4j(scenario_id: int) -> dict[str, Any]:
    payload = build_scenario_graph_payload(scenario_id)
    config = get_neo4j_config()
    synced_at = datetime.now(UTC).isoformat()

    try:
        with _driver_or_error() as driver:
            driver.verify_connectivity()
            with driver.session(**_session_kwargs(config)) as session:
                _create_constraints(session)
                _clear_scenario(session, scenario_id)
                _merge_nodes(session, payload["nodes"])
                _merge_relationships(session, payload["relationships"])
                session.run(
                    "MATCH (s:TTScenario {key: $key}) SET s.last_synced_at = $synced_at",
                    key=f"scenario:{scenario_id}",
                    synced_at=synced_at,
                )
    except TimetableGraphError:
        raise
    except Exception as exc:  # pragma: no cover - depends on local Neo4j state
        raise TimetableGraphError(str(exc)) from exc

    return {
        "ok": True,
        "synced_at": synced_at,
        "neo4j": {"uri": config.uri, "database": config.database},
        "scenario": payload["scenario"],
        "summary": payload["summary"],
    }

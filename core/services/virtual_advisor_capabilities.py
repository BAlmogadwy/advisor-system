"""
core/services/virtual_advisor_capabilities.py
Read-only capability registry for the Virtual Advisor agent loop.

Each capability wraps an EXISTING verified service function as an
LLM-callable tool. The model never touches the ORM or SQL: it can only
name a registered capability and supply JSON arguments; the executor
validates the arguments, enforces the caller's scope server-side, calls
the underlying service, and returns a token-compact evidence dict.

Design rules (see docs/VIRTUAL_ADVISOR_CAPABILITY_MAP.md):

- Identity and scope come from the authenticated request scope dict,
  NEVER from model-supplied arguments. A student can only read their own
  records; an advisor only their portfolio; a general advisor only their
  departments.
- Every capability is read-only. Mutating tools are intentionally not
  registered.
- Executors must not raise: failures return ``{"ok": False, "error": …}``
  so the model can recover or rephrase instead of crashing the chat turn.
- Outputs are compacted (row caps, dropped heavy fields) because they are
  re-serialised into the model context on every loop iteration.

The registry deliberately imports services lazily inside executors —
``core.services.virtual_advisor`` imports this module, and several
services import models that would otherwise create import cycles.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_GENERAL_ADVISOR,
    ROLE_STUDENT,
    ROLE_SUPER_ADMIN,
)
from core.services.student_helpers import normalize_code

logger = logging.getLogger(__name__)

_STAFF_ROLES = frozenset({ROLE_SUPER_ADMIN, ROLE_GENERAL_ADVISOR, ROLE_ADVISOR})
_PROGRAM_ROLES = frozenset({ROLE_SUPER_ADMIN, ROLE_GENERAL_ADVISOR})
_ALL_ROLES = _STAFF_ROLES | frozenset({ROLE_STUDENT})

_MAX_LIST_ROWS = 20
_MAX_COURSE_MATCHES = 10


# ── Scope helpers ────────────────────────────────────────────────


def _scope_role(scope: dict[str, Any] | None) -> str:
    return str((scope or {}).get("role") or ROLE_SUPER_ADMIN)


def _scope_departments(scope: dict[str, Any] | None) -> list[str]:
    return [
        str(item).strip().upper()
        for item in (scope or {}).get("departments", [])
        if str(item).strip()
    ]


def _resolve_scoped_student_id(
    args: dict[str, Any], scope: dict[str, Any] | None
) -> tuple[int | None, str | None]:
    """Resolve the student a tool call may read, enforcing scope.

    Returns ``(student_id, error)``. The model's ``student_id`` argument
    is honoured only when the caller's scope allows reading that student.
    """
    from core.models import Student

    scope = scope or {}
    role = _scope_role(scope)

    requested: int | None = None
    raw = args.get("student_id")
    if raw not in (None, ""):
        try:
            requested = int(raw)
        except (TypeError, ValueError):
            return None, "student_id must be an integer."

    if role == ROLE_STUDENT:
        own = scope.get("student_id")
        try:
            own_id = int(own) if own not in (None, "") else None
        except (TypeError, ValueError):
            own_id = None
        if own_id is None:
            return None, "No student identity is linked to this session."
        if requested is not None and requested != own_id:
            return None, "Students can only access their own records."
        return own_id, None

    if requested is None:
        return None, "student_id is required."

    row = (
        Student.objects.filter(student_id=requested)
        .values("student_id", "program", "advisor_id")
        .first()
    )
    if not row:
        return None, f"Student not found: {requested}"

    if role == ROLE_ADVISOR:
        advisor_id = str(scope.get("advisor_id") or "").strip()
        if not advisor_id or str(row.get("advisor_id") or "").strip() != advisor_id:
            return None, "This student is outside your advisor portfolio."
        return requested, None

    if role == ROLE_GENERAL_ADVISOR:
        departments = _scope_departments(scope)
        if str(row.get("program") or "").strip().upper() not in departments:
            return None, "This student is outside your department scope."
        return requested, None

    return requested, None


def _resolve_scoped_programs(
    args: dict[str, Any], scope: dict[str, Any] | None
) -> tuple[list[str], str | None]:
    """Resolve the program list a tool call may aggregate over.

    General advisors are restricted (and defaulted) to their departments;
    super admins must name programs explicitly or pass none for "all".
    """
    raw = args.get("programs") if args.get("programs") not in (None, "") else args.get("program")
    requested: list[str] = []
    if isinstance(raw, list):
        requested = [str(item).strip().upper() for item in raw if str(item).strip()]
    elif raw not in (None, ""):
        requested = [part.strip().upper() for part in str(raw).split(",") if part.strip()]

    role = _scope_role(scope)
    if role == ROLE_GENERAL_ADVISOR:
        departments = _scope_departments(scope)
        if not departments:
            return [], "No departments are configured for your scope."
        if not requested:
            return departments, None
        outside = [p for p in requested if p not in departments]
        if outside:
            return [], f"Programs outside your department scope: {', '.join(outside)}"
        return requested, None
    return requested, None


def _clean_section(value: Any) -> str | None:
    section = str(value or "").strip().upper()
    return section if section in {"M", "F"} else None


# ── Capability + registry ────────────────────────────────────────


@dataclass(frozen=True)
class AdvisorCapability:
    """One read-only, scope-guarded tool the agent loop may call."""

    name: str
    description: str
    parameters: dict[str, Any]
    allowed_roles: frozenset[str]
    executor: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]]
    read_only: bool = True

    def tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class AdvisorCapabilityRegistry:
    """Scope-aware lookup + execution surface for advisor capabilities."""

    capabilities: dict[str, AdvisorCapability] = field(default_factory=dict)

    def register(self, capability: AdvisorCapability) -> None:
        if not capability.read_only:
            raise ValueError(
                f"Capability {capability.name!r} is not read-only; "
                "mutating advisor tools are not allowed."
            )
        self.capabilities[capability.name] = capability

    def capabilities_for_scope(self, scope: dict[str, Any] | None) -> list[AdvisorCapability]:
        role = _scope_role(scope)
        return [cap for cap in self.capabilities.values() if role in cap.allowed_roles]

    def tool_schemas_for_scope(self, scope: dict[str, Any] | None) -> list[dict[str, Any]]:
        return [cap.tool_schema() for cap in self.capabilities_for_scope(scope)]

    def execute(
        self,
        name: str,
        args: dict[str, Any],
        *,
        scope: dict[str, Any] | None = None,
        ctx: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        capability = self.capabilities.get(name)
        if capability is None:
            return {"tool": name, "ok": False, "error": "Unknown advisor tool."}
        if _scope_role(scope) not in capability.allowed_roles:
            return {"tool": name, "ok": False, "error": "This tool is not allowed for your role."}
        if not isinstance(args, dict):
            return {"tool": name, "ok": False, "error": "Tool arguments must be an object."}
        try:
            result = capability.executor(args, scope or {}, ctx or {})
        except Exception:
            logger.exception("Advisor capability %s failed", name)
            return {
                "tool": name,
                "ok": False,
                "error": "The tool failed while querying verified records.",
            }
        result.setdefault("tool", name)
        result.setdefault("ok", True)
        return result


# ── Executors (lazy service imports; compact outputs) ────────────


def _ctx_year_term(
    args: dict[str, Any], ctx: dict[str, Any]
) -> tuple[int | None, int | None, str | None]:
    def _coerce(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    year = _coerce(args.get("academic_year")) or _coerce(ctx.get("academic_year"))
    term = _coerce(args.get("term")) or _coerce(ctx.get("term"))
    if year is None or term is None:
        return None, None, "academic_year and term are required (none configured for this chat)."
    return year, term, None


_FIND_STUDENTS_MESSAGE_ROWS = 30


def _exec_find_students(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    from core.services.virtual_advisor import find_students_tool

    result = find_students_tool(args, scope=scope)

    # Token diet for the agent loop: a 500-row dump is ~20k prompt tokens
    # and slows or times out the next turn. Keep a representative page and
    # attach summary statistics over the returned rows so overview
    # questions ("how are they doing?") need no raw dump at all.
    students = result.get("students") or []
    if students:
        gpas = [s.get("gpa") for s in students if isinstance(s.get("gpa"), int | float)]
        credits = [
            s.get("total_earned_credits")
            for s in students
            if isinstance(s.get("total_earned_credits"), int | float)
        ]
        stats: dict[str, Any] = {"rows_in_stats": len(students)}
        if gpas:
            stats.update(
                {
                    "gpa_min": round(min(gpas), 2),
                    "gpa_avg": round(sum(gpas) / len(gpas), 2),
                    "gpa_max": round(max(gpas), 2),
                    "gpa_below_2_count": sum(1 for g in gpas if g < 2.0),
                }
            )
        if credits:
            stats["avg_earned_credits"] = round(sum(credits) / len(credits), 1)
        result["summary_stats"] = stats
    if len(students) > _FIND_STUDENTS_MESSAGE_ROWS:
        result["students"] = students[:_FIND_STUDENTS_MESSAGE_ROWS]
        result["students_omitted"] = len(students) - _FIND_STUDENTS_MESSAGE_ROWS
    return result


def _exec_get_student_context(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    from core.services.virtual_advisor import build_verified_student_context

    student_id, error = _resolve_scoped_student_id(args, scope)
    if error:
        return {"ok": False, "error": error}

    def _coerce(value: Any) -> int | None:
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    year = _coerce(args.get("academic_year")) or _coerce(ctx.get("academic_year"))
    term = _coerce(args.get("term")) or _coerce(ctx.get("term"))
    try:
        context = build_verified_student_context(
            student_id=student_id, academic_year=year, term=term
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "student_context": context}


def _exec_lookup_course(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    from django.db.models import Q

    from core.models import Course, ProgrammeRequirement

    query = str(args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "query is required."}
    program = str(args.get("program") or "").strip().upper()

    matches: dict[str, dict[str, Any]] = {}

    exact = normalize_code(query)
    if exact:
        req_qs = ProgrammeRequirement.objects.filter(course_code__iexact=exact)
        if program:
            req_qs = req_qs.filter(program=program)
        for row in req_qs.values(
            "course_code", "course_name", "program", "programme_term", "credit_hours"
        )[:_MAX_COURSE_MATCHES]:
            code = normalize_code(row["course_code"])
            entry = matches.setdefault(
                code,
                {
                    "course_code": code,
                    "course_name": str(row.get("course_name") or "").strip(),
                    "credit_hours": row.get("credit_hours"),
                    "programs": [],
                },
            )
            prog = str(row.get("program") or "").strip().upper()
            if prog and prog not in entry["programs"]:
                entry["programs"].append(prog)

    name_query = Q(course_name__icontains=query)
    req_qs = ProgrammeRequirement.objects.filter(name_query)
    if program:
        req_qs = req_qs.filter(program=program)
    for row in req_qs.values(
        "course_code", "course_name", "program", "programme_term", "credit_hours"
    )[: _MAX_COURSE_MATCHES * 3]:
        code = normalize_code(row["course_code"])
        if not code:
            continue
        entry = matches.setdefault(
            code,
            {
                "course_code": code,
                "course_name": str(row.get("course_name") or "").strip(),
                "credit_hours": row.get("credit_hours"),
                "programs": [],
            },
        )
        prog = str(row.get("program") or "").strip().upper()
        if prog and prog not in entry["programs"]:
            entry["programs"].append(prog)
        if len(matches) >= _MAX_COURSE_MATCHES:
            break

    if len(matches) < _MAX_COURSE_MATCHES:
        for row in Course.objects.filter(description__icontains=query).values(
            "course_code", "description"
        )[:_MAX_COURSE_MATCHES]:
            code = normalize_code(row["course_code"])
            if not code or code in matches:
                continue
            matches[code] = {
                "course_code": code,
                "course_name": str(row.get("description") or "").strip(),
                "credit_hours": None,
                "programs": [],
            }
            if len(matches) >= _MAX_COURSE_MATCHES:
                break

    return {
        "ok": True,
        "query": query,
        "match_count": len(matches),
        "courses": list(matches.values()),
    }


def _exec_course_prerequisites(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    from core.models import Prerequisite, ProgrammeRequirement, Student
    from core.services.student_helpers import get_prerequisites

    course_code = normalize_code(args.get("course_code"))
    if not course_code:
        return {"ok": False, "error": "course_code is required."}

    program = str(args.get("program") or "").strip().upper()
    if not program and _scope_role(scope) == ROLE_STUDENT:
        own = scope.get("student_id")
        row = Student.objects.filter(student_id=own).values_list("program", flat=True).first()
        program = str(row or "").strip().upper()

    if program:
        programs = [program]
    else:
        programs = sorted(
            set(
                Prerequisite.objects.filter(course_code__iexact=course_code).values_list(
                    "program", flat=True
                )
            )
            | set(
                ProgrammeRequirement.objects.filter(course_code__iexact=course_code).values_list(
                    "program", flat=True
                )
            )
        )
    if not programs:
        return {
            "ok": True,
            "course_code": course_code,
            "per_program": [],
            "note": "Course not found in any programme plan.",
        }

    per_program: list[dict[str, Any]] = []
    for prog in programs[:12]:
        prereqs = get_prerequisites(course_code, prog)
        plan_row = (
            ProgrammeRequirement.objects.filter(program=prog, course_code__iexact=course_code)
            .values("course_name", "programme_term", "credit_hours")
            .first()
        )
        per_program.append(
            {
                "program": prog,
                "prerequisites": prereqs,
                "course_name": str((plan_row or {}).get("course_name") or "").strip(),
                "programme_term": (plan_row or {}).get("programme_term"),
                "credit_hours": (plan_row or {}).get("credit_hours"),
            }
        )

    return {"ok": True, "course_code": course_code, "per_program": per_program}


def _exec_course_eligibility(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    from core.services.eligibility import build_course_eligibility_report

    course_code = normalize_code(args.get("course_code"))
    if not course_code:
        return {"ok": False, "error": "course_code is required."}
    section = _clean_section(args.get("section"))
    programs, error = _resolve_scoped_programs(args, scope)
    if error:
        return {"ok": False, "error": error}

    # The underlying service accepts one program (or None for all).
    role = _scope_role(scope)
    program_args: list[str | None]
    if programs:
        program_args = list(programs)
    elif role == ROLE_SUPER_ADMIN:
        program_args = [None]
    else:
        return {"ok": False, "error": "No programs resolved for your scope."}

    total_students = 0
    total_eligible = 0
    per_program: list[dict[str, Any]] = []
    for prog in program_args:
        report = build_course_eligibility_report(
            course_code, section=section, program=prog if prog else None
        )
        total_students += int(report.get("total_students") or 0)
        total_eligible += int(report.get("total_eligible") or 0)
        for row in report.get("per_program", []):
            per_program.append(
                {
                    "program": row.get("program"),
                    "students": row.get("students"),
                    "eligible_count": row.get("eligible_count"),
                    "blocked_count": row.get("blocked_count"),
                    "prerequisites": row.get("prerequisites"),
                    "top_missing_prerequisites": row.get("top_missing_prerequisites"),
                    "eligible_student_ids_sample": (row.get("eligible_student_ids") or [])[:15],
                }
            )

    return {
        "ok": True,
        "course_code": course_code,
        "section": section,
        "total_students": total_students,
        "total_eligible": total_eligible,
        "per_program": per_program,
    }


def _exec_recommend_courses(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    from core.services.recommender import recommend_next_courses
    from core.services.virtual_advisor import _course_names

    student_id, error = _resolve_scoped_student_id(args, scope)
    if error:
        return {"ok": False, "error": error}
    year, term, error = _ctx_year_term(args, ctx)
    if error:
        return {"ok": False, "error": error}

    codes = recommend_next_courses(int(student_id), int(year), int(term))
    names = _course_names(set(codes))
    return {
        "ok": True,
        "student_id": student_id,
        "academic_year": year,
        "term": term,
        "recommendation_count": len(codes),
        "recommendations": [
            {"course_code": code, "course_name": names.get(code, "")} for code in codes
        ],
    }


def _exec_graduation_shortfall(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    from core.services.credit_shortfall_analysis import run_shortfall_analysis

    programs, error = _resolve_scoped_programs(args, scope)
    if error:
        return {"ok": False, "error": error}
    if not programs:
        return {"ok": False, "error": 'programs is required (e.g. ["IS", "IS2"]).'}
    year, term, error = _ctx_year_term(args, ctx)
    if error:
        return {"ok": False, "error": error}
    section = _clean_section(args.get("section"))

    def _coerce_min(value: Any) -> int | None:
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    min_credits = _coerce_min(args.get("min_credits"))
    kwargs: dict[str, Any] = {"section": section}
    if min_credits is not None:
        kwargs["min_credits"] = min_credits

    report = run_shortfall_analysis(int(year), int(term), list(programs), **kwargs)
    shortfall_rows = report.get("shortfall_students") or []
    return {
        "ok": True,
        "programs": list(programs),
        "section": section,
        "total_students": report.get("total_students"),
        "shortfall_count": report.get("shortfall_count"),
        "ok_count": report.get("ok_count"),
        "summary_by_program": report.get("summary_by_program"),
        "top_recoverable": (report.get("top_recoverable") or [])[:10],
        "shortfall_students_sample": [
            {
                "student_id": row.get("student_id"),
                "name": row.get("name"),
                "program": row.get("program"),
                "recommended_credits": row.get("recommended_credits"),
                "graduation_status": row.get("graduation_status"),
            }
            for row in shortfall_rows[:_MAX_LIST_ROWS]
        ],
        "shortfall_students_truncated": len(shortfall_rows) > _MAX_LIST_ROWS,
    }


def _exec_portfolio_triage(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    from core.services.advisors import list_students_by_advisor

    role = _scope_role(scope)
    requested = str(args.get("advisor_id") or "").strip()
    forced: str | None = None
    allowed_departments: list[str] | None = None

    if role == ROLE_ADVISOR:
        own = str(scope.get("advisor_id") or "").strip()
        if not own:
            return {"ok": False, "error": "No advisor identity is linked to this session."}
        advisor_id = own
        forced = own
    elif role == ROLE_GENERAL_ADVISOR:
        if not requested:
            return {
                "ok": False,
                "error": (
                    "advisor_id is required. To search students by name across "
                    "programs, use find_students with name_contains instead."
                ),
            }
        advisor_id = requested
        allowed_departments = _scope_departments(scope)
    else:
        if not requested:
            return {
                "ok": False,
                "error": (
                    "advisor_id is required. To search students by name across "
                    "programs, use find_students with name_contains instead."
                ),
            }
        advisor_id = requested

    focus = str(args.get("focus") or "all").strip().lower()
    if focus not in {"all", "risk", "missing", "zerohours", "attention"}:
        focus = "all"

    report = list_students_by_advisor(
        advisor_id,
        search=str(args.get("search") or "").strip() or None,
        focus=focus,
        program_filter=str(args.get("program") or "").strip() or None,
        forced_advisor_id=forced,
        allowed_departments=allowed_departments,
    )
    if report.get("error"):
        return {"ok": False, "error": str(report["error"])}

    items = report.get("items") or []
    return {
        "ok": True,
        "advisor": report.get("advisor"),
        "focus": focus,
        "count": report.get("count"),
        "summary": report.get("summary"),
        "students_sample": [
            {
                "student_id": row.get("student_id"),
                "name": row.get("name"),
                "program": row.get("program"),
                "gpa": row.get("gpa"),
                "total_earned_credits": row.get("total_earned_credits"),
                "current_term_registered_hours": row.get("current_term_registered_hours"),
            }
            for row in items[:_MAX_LIST_ROWS]
        ],
        "students_truncated": len(items) > _MAX_LIST_ROWS,
    }


def _exec_aggregate_demand(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    from core.services.reporting import build_aggregate_counts
    from core.services.virtual_advisor import _course_names

    programs, error = _resolve_scoped_programs(args, scope)
    if error:
        return {"ok": False, "error": error}
    year, term, error = _ctx_year_term(args, ctx)
    if error:
        return {"ok": False, "error": error}
    section = _clean_section(args.get("section"))

    program_arg: str | list[str] | None
    if not programs:
        program_arg = None
    elif len(programs) == 1:
        program_arg = programs[0]
    else:
        program_arg = list(programs)

    student_count, counter = build_aggregate_counts(
        int(year), int(term), program=program_arg, section=section
    )
    top = counter.most_common(15)
    names = _course_names({code for code, _count in top})
    return {
        "ok": True,
        "academic_year": year,
        "term": term,
        "programs": list(programs) if programs else "all",
        "section": section,
        "student_count": student_count,
        "distinct_courses": len(counter),
        "top_demand": [
            {"course_code": code, "course_name": names.get(code, ""), "students": count}
            for code, count in top
        ],
    }


# ── Registry assembly ────────────────────────────────────────────


def _course_codes_array_schema(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


def build_default_registry() -> AdvisorCapabilityRegistry:
    registry = AdvisorCapabilityRegistry()

    registry.register(
        AdvisorCapability(
            name="find_students",
            description=(
                "Find students in verified university records using filters: name "
                "fragment, earned credits, GPA range, program, gender section "
                "(M/F), advisor, and course status (passed / studying / missing). "
                "Use for any cohort question ('list AI students who passed "
                "AI331') and for finding students by name. The result includes "
                "summary_stats over the matched rows for overview questions."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "min_earned_credits": {"type": "integer"},
                    "max_earned_credits": {"type": "integer"},
                    "min_gpa": {"type": "number"},
                    "max_gpa": {"type": "number"},
                    "program": {"type": "string", "description": "Program code, e.g. AI, CS2"},
                    "name_contains": {
                        "type": "string",
                        "description": "Filter by a fragment of the student's name (Arabic or English)",
                    },
                    "sections": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["M", "F"]},
                        "description": "Gender sections: M = male, F = female",
                    },
                    "advisor_id": {"type": "string"},
                    "passed_courses": _course_codes_array_schema(
                        "Courses the student must have passed"
                    ),
                    "studying_courses": _course_codes_array_schema(
                        "Courses the student must be currently studying"
                    ),
                    "missing_courses": _course_codes_array_schema(
                        "Courses the student must NOT have passed or be studying"
                    ),
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                },
                "additionalProperties": False,
            },
            allowed_roles=_ALL_ROLES,
            executor=_exec_find_students,
        )
    )

    registry.register(
        AdvisorCapability(
            name="get_student_context",
            description=(
                "Full verified academic context for ONE student: profile, GPA, "
                "earned credits, passed and studying courses, remaining programme "
                "requirements, and next-term recommendations. Use whenever the "
                "question is about a specific student's situation."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "student_id": {
                        "type": "integer",
                        "description": "University student id. Omit for the chatting student.",
                    },
                    "academic_year": {"type": "integer"},
                    "term": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            allowed_roles=_ALL_ROLES,
            executor=_exec_get_student_context,
        )
    )

    registry.register(
        AdvisorCapability(
            name="lookup_course",
            description=(
                "Resolve a vague course mention ('the project', 'data mining', "
                "'AI thing') or a course code into exact course codes with names "
                "and credit hours. Always use this before filtering by a course "
                "the user named loosely."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Course name fragment or code"},
                    "program": {"type": "string", "description": "Optional program code filter"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            allowed_roles=_ALL_ROLES,
            executor=_exec_lookup_course,
        )
    )

    registry.register(
        AdvisorCapability(
            name="recommend_courses",
            description=(
                "Compute the official next-term course recommendations for one "
                "student using the verified recommender."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "student_id": {
                        "type": "integer",
                        "description": "University student id. Omit for the chatting student.",
                    },
                    "academic_year": {"type": "integer"},
                    "term": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            allowed_roles=_ALL_ROLES,
            executor=_exec_recommend_courses,
        )
    )

    registry.register(
        AdvisorCapability(
            name="course_prerequisites",
            description=(
                "Official prerequisites for one course (per program), including "
                "hour-based requirements like '90(HOURS)', plus the course's "
                "plan term and credit hours. Use for 'can I/he take X' and "
                "'why is X blocked' questions, combined with the student's "
                "passed courses from get_student_context."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "course_code": {"type": "string"},
                    "program": {
                        "type": "string",
                        "description": "Program code. Omit for the student's own program.",
                    },
                },
                "required": ["course_code"],
                "additionalProperties": False,
            },
            allowed_roles=_ALL_ROLES,
            executor=_exec_course_prerequisites,
        )
    )

    registry.register(
        AdvisorCapability(
            name="course_eligibility",
            description=(
                "Report who can take a course: eligible counts per program, top "
                "missing prerequisites, and a sample of eligible student ids. Use "
                "for 'who can take X' and section-planning questions."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "course_code": {"type": "string"},
                    "program": {"type": "string", "description": "Optional program code"},
                    "section": {"type": "string", "enum": ["M", "F"]},
                },
                "required": ["course_code"],
                "additionalProperties": False,
            },
            allowed_roles=_PROGRAM_ROLES,
            executor=_exec_course_eligibility,
        )
    )

    registry.register(
        AdvisorCapability(
            name="graduation_shortfall",
            description=(
                "Find students whose recommended next-term credits fall below the "
                "minimum (graduation risk / low-load analysis) for one or more "
                "programs, with recoverable course suggestions."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "programs": _course_codes_array_schema('Program codes, e.g. ["IS", "IS2"]'),
                    "section": {"type": "string", "enum": ["M", "F"]},
                    "min_credits": {"type": "integer", "minimum": 1, "maximum": 21},
                    "academic_year": {"type": "integer"},
                    "term": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            allowed_roles=_PROGRAM_ROLES,
            executor=_exec_graduation_shortfall,
        )
    )

    registry.register(
        AdvisorCapability(
            name="portfolio_triage",
            description=(
                "List an advisor's student portfolio with attention signals. "
                "focus filters: 'risk' (GPA below 2.0), 'zerohours' (no current "
                "registration), 'missing' (missing high-priority courses), "
                "'attention' (any flag), 'all'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "advisor_id": {
                        "type": "string",
                        "description": "Advisor id. Omit when the caller IS the advisor.",
                    },
                    "focus": {
                        "type": "string",
                        "enum": ["all", "risk", "missing", "zerohours", "attention"],
                    },
                    "search": {"type": "string", "description": "Name or id fragment"},
                    "program": {"type": "string"},
                },
                "additionalProperties": False,
            },
            allowed_roles=_STAFF_ROLES,
            executor=_exec_portfolio_triage,
        )
    )

    registry.register(
        AdvisorCapability(
            name="aggregate_demand",
            description=(
                "Aggregate next-term course demand: how many students are "
                "recommended each course. Use for 'most needed courses' and "
                "section-count planning questions."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "programs": _course_codes_array_schema("Program codes; omit for all in scope"),
                    "section": {"type": "string", "enum": ["M", "F"]},
                    "academic_year": {"type": "integer"},
                    "term": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            allowed_roles=_PROGRAM_ROLES,
            executor=_exec_aggregate_demand,
        )
    )

    return registry


_default_registry: AdvisorCapabilityRegistry | None = None


def get_default_registry() -> AdvisorCapabilityRegistry:
    """Process-wide default registry (capabilities are stateless)."""
    global _default_registry
    if _default_registry is None:
        _default_registry = build_default_registry()
    return _default_registry

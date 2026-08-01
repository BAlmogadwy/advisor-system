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

    # Sanity guard: the academic calendar is Hijri (e.g. 1448). Live
    # testing caught the model passing Gregorian years (2024); silently
    # fall back to the configured defaults rather than running tools
    # against a phantom year. The executed year/term are echoed in every
    # tool result, so the model sees what was actually used.
    year_arg = _coerce(args.get("academic_year"))
    if year_arg is not None and not (1400 <= year_arg <= 1500):
        logger.warning("Ignoring implausible academic_year=%s from model args", year_arg)
        year_arg = None
    term_arg = _coerce(args.get("term"))
    if term_arg is not None and term_arg not in (1, 2, 3):
        logger.warning("Ignoring implausible term=%s from model args", term_arg)
        term_arg = None

    year = year_arg or _coerce(ctx.get("academic_year"))
    term = term_arg or _coerce(ctx.get("term"))
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


def _resolve_elective_slot(course_code: str, program: str) -> list[dict[str, Any]] | None:
    """Return the real courses that can fill an elective slot, or None if not a slot.

    A placeholder is recognised by its ProgrammeRequirement.type ("... Elective"), not
    by guessing at the code shape — FE1 and CS1 look nothing alike and new families
    would be missed by a pattern.
    """
    from core.models import ElectiveCourse, ElectiveTermMapping, ProgrammeRequirement

    req = ProgrammeRequirement.objects.filter(course_code__iexact=course_code)
    if program:
        req = req.filter(program__iexact=program)
    row = req.values("type", "program").first()
    if not row or "elective" not in str(row.get("type") or "").lower():
        return None

    prog = program or str(row.get("program") or "")
    mapped_ids = ElectiveTermMapping.objects.filter(
        placeholder_code__iexact=course_code, programme__iexact=prog
    ).values_list("elective_id", flat=True)

    options: list[dict[str, Any]] = []
    for e in ElectiveCourse.objects.filter(id__in=list(mapped_ids)).values(
        "course_code", "course_name", "credit_hours", "prerequisites_csv"
    ):
        prereqs = [
            p.strip().upper() for p in str(e["prerequisites_csv"] or "").split(",") if p.strip()
        ]
        options.append(
            {
                "course_code": e["course_code"],
                "course_name": e["course_name"],
                "credit_hours": e["credit_hours"],
                "prerequisites": prereqs,
            }
        )
    return sorted(options, key=lambda o: o["course_code"])[:_MAX_COURSE_MATCHES]


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
    # An elective PLACEHOLDER (FE1, GSE1, CS1 ...) is a slot, not a course. Answering
    # "prerequisites: []" for one reads as "this course has no prerequisites", which is
    # false for every slot whose real courses have them — ElectiveCourse carries a
    # prerequisites_csv per course. Resolve the slot and report the real options.
    elective_options = _resolve_elective_slot(course_code, program)
    if elective_options is not None:
        return {
            "ok": True,
            "course_code": course_code,
            "is_elective_placeholder": True,
            "options": elective_options,
            "note": (
                f"{course_code} is an elective SLOT in the plan, not a course. It has no "
                "prerequisites of its own; each course that can fill it has its own. "
                "Answer with the options and their prerequisites, never with "
                "'this course has no prerequisites'."
            ),
            "tool": "course_prerequisites",
        }

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
    from core.models import ElectiveCourse, ProgrammeRequirement, Student
    from core.services.credit_policy import credit_policy_evidence
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

    profile = (
        Student.objects.filter(student_id=student_id).values("program", "status").first() or {}
    )
    program = str(profile.get("program") or "").strip()
    # Needed by credit_policy_evidence: expected graduates carry a separate,
    # unresolved 16-hour ceiling that must not be papered over with the general one.
    student_status = str(profile.get("status") or "").strip()
    credit_map: dict[str, int] = {}
    credit_qs = ProgrammeRequirement.objects.filter(course_code__in=codes)
    if program:
        for raw_code, hours in credit_qs.filter(program__iexact=program).values_list(
            "course_code", "credit_hours"
        ):
            credit_map.setdefault(normalize_code(raw_code), int(hours or 0))
    for raw_code, hours in credit_qs.values_list("course_code", "credit_hours"):
        credit_map.setdefault(normalize_code(raw_code), int(hours or 0))
    catalogue_missing = [code for code in codes if code not in credit_map]
    if catalogue_missing:
        for raw_code, hours in ElectiveCourse.objects.filter(
            course_code__in=catalogue_missing
        ).values_list("course_code", "credit_hours"):
            credit_map.setdefault(normalize_code(raw_code), int(hours or 0))

    return {
        "ok": True,
        "student_id": student_id,
        "academic_year": year,
        "term": term,
        "recommendation_count": len(codes),
        "recommendations": [
            {
                "course_code": code,
                "course_name": names.get(code, ""),
                "credit_hours": credit_map.get(code),
            }
            for code in codes
        ],
        "credit_policy": credit_policy_evidence(
            recommended_credit_hours=sum(credit_map[code] for code in codes if code in credit_map),
            unknown_for=[code for code in codes if code not in credit_map],
            term=term,
            student_status=student_status,
        ),
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


def _exec_my_progress(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Where the student stands: what is open now, what is blocked and why."""
    from core.services.student_unlock import build_unlock_report

    student_id, error = _resolve_scoped_student_id(args, scope)
    if error:
        return {"ok": False, "error": error}
    year, term, error = _ctx_year_term(args, ctx)
    if error:
        return {"ok": False, "error": error}

    r = build_unlock_report(int(student_id), int(year), int(term))
    if not r:
        return {"ok": False, "error": f"No degree plan found for student {student_id}."}

    def why(c):
        out = []
        for x in c["reasons"]:
            if x["kind"] == "MISSING_COURSE":
                out.append(f"needs {x['code']}")
            elif x["kind"] == "MISSING_HOURS":
                out.append(f"needs {x['required']} credit hours, has {x['effective']}")
            else:
                out.append(x["kind"].lower())
        return out

    return {
        "student_id": int(student_id),
        "program": r["program"],
        "academic_year": year,
        "term": term,
        "counts": r["counts"],
        "most_useful_course_to_pass": r["top_blocker"],
        "open_now": [
            {
                "code": c["code"],
                "name": c["name"],
                "credits": c["credits"],
                "fits_this_term": c["fits_this_term"],
            }
            for c in r["open_courses"][:_MAX_LIST_ROWS]
        ],
        "elective_slots": [c["code"] for c in r["elective_slots"]],
        "blocked": [
            {
                "code": c["code"],
                "name": c["name"],
                "steps_away": c["steps"],
                "opens_n_courses": c["frees_eventually"],
                "nearest_course_you_can_take_now": (c["nearest_open"] or {}).get("code"),
                "why": why(c),
            }
            for c in r["locked_courses"][:_MAX_LIST_ROWS]
        ],
        "note": (
            "A course being studied satisfies a prerequisite but must still be passed. "
            "This does not know which courses actually run this term or seat availability."
        ),
    }


def _exec_why_course_locked(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Explain ONE course: passed, studying, open now, or blocked and exactly why."""
    from core.services.student_unlock import build_unlock_report

    student_id, error = _resolve_scoped_student_id(args, scope)
    if error:
        return {"ok": False, "error": error}
    code = normalize_code(str(args.get("course_code") or ""))
    if not code:
        return {"ok": False, "error": "course_code is required."}
    year, term, error = _ctx_year_term(args, ctx)
    if error:
        return {"ok": False, "error": error}

    r = build_unlock_report(int(student_id), int(year), int(term))
    if not r:
        return {"ok": False, "error": f"No degree plan found for student {student_id}."}

    # The forward direction — "if I pass this, what opens?" — is already computed:
    # build_unlock_report returns a `graph` of prerequisite edges that every caller
    # so far has thrown away. It costs nothing to answer, and it is the question a
    # student actually asks after being told a course is blocked.
    graph = r.get("graph") or {}
    unlocks = sorted(
        {
            edge["course_code"]
            for edge in (graph.get("items") or [])
            if edge.get("prerequisite_course_code") == code
        }
    )
    status_of = graph.get("statusOf") or {}
    name_of = graph.get("nameOf") or {}
    base = {
        "student_id": int(student_id),
        "course_code": code,
        "unlocks_directly": [
            {"code": u, "name": name_of.get(u, ""), "current_status": status_of.get(u, "")}
            for u in unlocks
        ],
        "unlocks_directly_count": len(unlocks),
    }
    for c in r["open_courses"]:
        if c["code"] == code:
            return {
                **base,
                "status": "open_now",
                "name": c["name"],
                "fits_this_term": c["fits_this_term"],
                "explanation": "Every prerequisite is satisfied; it can be registered.",
            }
    for c in r["done"]:
        if c["code"] == code:
            return {**base, "status": "passed", "name": c["name"], "explanation": "Already passed."}
    for c in r["in_progress"]:
        if c["code"] == code:
            return {
                **base,
                "status": "studying",
                "name": c["name"],
                "explanation": "Being studied now; must still be passed.",
            }
    for c in r["locked_courses"]:
        if c["code"] == code:
            return {
                **base,
                "status": "blocked",
                "name": c["name"],
                "steps_away": c["steps"],
                "opens_n_courses": c["frees_eventually"],
                "nearest_course_you_can_take_now": (c["nearest_open"] or {}).get("code"),
                "blocked_by": c["reasons"],
                "explanation": (
                    "Blocked only by a credit-hour requirement, not by any course."
                    if c["hours_only"]
                    else "Blocked by prerequisite courses not yet passed or being studied."
                ),
            }
    return {"ok": False, "error": f"{code} is not in this student's degree plan."}


def _exec_graduation_progress(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """How far from graduating, with the prerequisite floor kept separate from the
    pace assumption."""
    from core.services.student_graduation import build_graduation_report

    student_id, error = _resolve_scoped_student_id(args, scope)
    if error:
        return {"ok": False, "error": error}
    year, term, error = _ctx_year_term(args, ctx)
    if error:
        return {"ok": False, "error": error}

    g = build_graduation_report(int(student_id), int(year), int(term))
    if not g:
        return {"ok": False, "error": f"No degree plan found for student {student_id}."}
    return {
        "student_id": int(student_id),
        "program": g["program"],
        "plan_courses_passed": g["plan_courses_passed"],
        "plan_courses_total": g["plan_courses_total"],
        "percent_complete": g["percent_courses"],
        "courses_remaining": g["remaining_courses"],
        "credits_remaining_in_plan": g["remaining_credits"],
        "credits_earned_registrar": g["earned_credits_registrar"],
        "gpa": g["gpa"],
        "minimum_terms_by_prerequisites": g["chain_floor_terms"],
        "terms_at_assumed_pace": g["pace_terms"],
        "courses_per_term_assumed": g["courses_per_term"],
        "terms_estimate": g["terms_estimate"],
        "credit_hour_gates": g["hour_gates"],
        # Computed by build_graduation_report and previously dropped on the floor.
        # "can this be my last term?" is one of the most-asked questions and the
        # answer was already sitting in the report.
        "final_term_possible": g["final_term_possible"],
        "passed_credits_in_plan": g["passed_credits_in_plan"],
        "registered_credits_now": g["registered_credits_now"],
        "courses_in_progress": g["in_progress"],
        "note": (
            "Registrar credits include courses outside the plan, so they are not a "
            "fraction of the plan total. The prerequisite minimum cannot be beaten by "
            "registering more courses in a term. final_term_possible means the PLAN "
            "could be finished this term; graduation itself is a University Council "
            "decision (TU.GRADUATION.COUNCIL_AWARDS_DEGREE), so it must never be "
            "reported as 'you are graduating'."
        ),
    }


def _exec_my_timetable(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """The student's registered weekly schedule: day, time, course, section, room."""
    from core.services.student_sections import get_student_term_baseline, student_gender

    student_id, error = _resolve_scoped_student_id(args, scope)
    if error:
        return {"ok": False, "error": error}
    year, term, error = _ctx_year_term(args, ctx)
    if error:
        return {"ok": False, "error": error}

    rows = get_student_term_baseline(int(student_id), str(year), str(term))
    if not rows:
        from core.models import StudentTermSection

        published = list(
            StudentTermSection.objects.filter(term_section__scenario__isnull=True)
            .values_list("academic_year", "term")
            .distinct()[:2]
        )
        if len(published) == 1:
            year, term = published[0]
            rows = get_student_term_baseline(int(student_id), str(year), str(term))

    gender = student_gender(int(student_id))
    if gender:
        rows = [
            r
            for r in rows
            if not str(r.get("section") or "").upper().startswith(("M", "F"))
            or str(r.get("section") or "").upper().startswith(gender)
        ]
    meetings = [
        {
            "day": r["day"],
            "start": r["start_time"],
            "end": r["end_time"],
            "course_code": r["course_code"],
            "section": r["section"],
            "room": r["room"],
            "instructor": r["instructor"],
        }
        for r in rows
        if r.get("start_time")
    ]

    # get_student_term_baseline emits ONE ROW PER MEETING, so a 4-credit course
    # meeting three times a week appears three times. Summing the rows' credits
    # therefore multi-counts — measured at 36 credits for a student actually
    # carrying 14. Registrations are de-duplicated on (course, section) first.
    by_section: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        key = (str(r.get("course_code") or ""), str(r.get("section") or ""))
        entry = by_section.setdefault(
            key,
            {
                "course_code": key[0],
                "section": key[1],
                "credits": int(r.get("credits") or 0),
                "meeting_count": 0,
                "scheduled": False,
            },
        )
        entry["meeting_count"] += 1
        if r.get("start_time"):
            entry["scheduled"] = True

    registrations = sorted(by_section.values(), key=lambda x: (x["course_code"], x["section"]))
    # Registered but with no meeting on file — real, and invisible in a meetings list.
    unscheduled = [r for r in registrations if not r["scheduled"]]
    return {
        "student_id": int(student_id),
        "academic_year": year,
        "term": term,
        "meetings": meetings[: _MAX_LIST_ROWS * 2],
        "registrations": registrations,
        "registered_course_count": len(registrations),
        "registered_credit_hours": sum(r["credits"] for r in registrations),
        "courses_without_a_time": sorted(r["course_code"] for r in unscheduled),
        "note": (
            "The timetable on file for the term shown; not a live seat count. "
            "registered_credit_hours counts each course ONCE — the underlying rows are "
            "per meeting, so adding them up over-counts a course that meets several "
            "times a week. courses_without_a_time are genuinely registered; they simply "
            "have no meeting recorded, so they do not appear in meetings."
        ),
    }


def _exec_my_plan_by_term(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """The whole degree plan, level by level, with each course's status.

    ``my_progress`` answers "what can I take"; this answers "show me the plan". The
    difference matters to a student who wants the shape of what is left rather than a
    filtered list of what is open right now.
    """
    from core.report_views import _build_student_plan_payload

    student_id, error = _resolve_scoped_student_id(args, scope)
    if error:
        return {"ok": False, "error": error}

    payload, payload_err = _build_student_plan_payload(int(student_id))
    if payload is None or payload_err is not None:
        return {"ok": False, "error": f"No degree plan found for student {student_id}."}

    terms = payload.get("terms") or []
    only = args.get("term")
    if only not in (None, ""):
        try:
            wanted = int(only)
        except (TypeError, ValueError):
            return {"ok": False, "error": "term must be an integer plan level."}
        terms = [row for row in terms if int(row.get("term") or 0) == wanted]
        if not terms:
            return {
                "ok": True,
                "student_id": int(student_id),
                "program": payload.get("program", ""),
                "plan_level": wanted,
                "terms": [],
                "note": f"The plan has no level {wanted}.",
                "tool": "my_plan_by_term",
            }

    return {
        "ok": True,
        "student_id": int(student_id),
        "program": payload.get("program", ""),
        "summary": payload.get("summary", {}),
        "terms": terms,
        "blocker_hints": payload.get("blocker_hints") or [],
        "note": (
            "Plan LEVELS, not calendar terms - programme_term is where a course sits in "
            "the degree plan, not when it is taught. status is passed / studying / "
            "not_taken, and can_register reflects prerequisites ONLY, never whether a "
            "section is being offered."
        ),
        "tool": "my_plan_by_term",
    }


def _exec_my_advisor(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """The student's own academic adviser, by name rather than by internal id."""
    from core.models import AcademicAdvisor, Student

    student_id, error = _resolve_scoped_student_id(args, scope)
    if error:
        return {"ok": False, "error": error}

    row = Student.objects.filter(student_id=student_id).values("advisor_id", "program").first()
    if not row:
        return {"ok": False, "error": f"Student not found: {student_id}."}

    advisor_id = str(row.get("advisor_id") or "").strip()
    if not advisor_id:
        return {
            "ok": True,
            "student_id": int(student_id),
            "advisor_assigned": False,
            "note": (
                "No adviser is recorded for this student. The guide tells the student to "
                "contact the head of department when no adviser has been assigned "
                "(TU.ADVISING.STUDENT_DUTIES)."
            ),
            "tool": "my_advisor",
        }

    adv = (
        AcademicAdvisor.objects.filter(advisor_id=advisor_id)
        .values("advisor_id", "full_name", "department", "email")
        .first()
    )
    if not adv:
        return {
            "ok": True,
            "student_id": int(student_id),
            "advisor_assigned": True,
            "advisor_id": advisor_id,
            "advisor_name": None,
            "note": (
                "An adviser id is on file but no matching adviser record exists, so the "
                "name cannot be given. Do not present the id to the student as a name."
            ),
            "tool": "my_advisor",
        }

    return {
        "ok": True,
        "student_id": int(student_id),
        "advisor_assigned": True,
        "advisor_id": adv["advisor_id"],
        "advisor_name": adv.get("full_name") or "",
        # Email is withheld deliberately: all 89 advisor rows carry a synthetic
        # address (advisorNN@placeholder.local). Returning it would send a student
        # to an address that does not exist, which is worse than saying nothing.
        "advisor_email": None,
        "contact_note": (
            "No usable adviser email is on file, so none is given. Direct the student "
            "to the adviser through the channels the guide lists "
            "(TU.CONTACT.ADVISER_CHANNELS) rather than inventing contact details."
        ),
        "advisor_department": adv.get("department") or "",
        "tool": "my_advisor",
    }


def _student_sections_context(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Shared setup for the section-shaped capabilities.

    Returns ``(error, context)``. Three guards live here rather than in each caller,
    because getting any of them wrong is a silent wrong answer rather than a crash:

    * **Cohort must resolve.** ``gender_section_filter("")`` is an ALL-PASS filter and
      722 of the 3,807 ids in StudentTermSection have no Student row, so a fallback
      would show the other cohort's sections. Refuse instead.
    * **Sections are TERMLESS.** TermSection has no academic_year and no term column,
      so nothing here may be described as "next term's" sections. The student's own
      baseline does belong to a term, and that is reported separately.
    * **Query by course_key.** ``course_code`` holds the department prefix on real
      sections ('CS') and the full code on generated ones ('CS111'), so filtering on
      it silently returns only the generated rows.
    """
    from core.services.planner_builder import _catalog_for_courses
    from core.services.student_sections import (
        UnknownStudentGender,
        get_student_term_baseline,
        student_gender_strict,
    )

    student_id, error = _resolve_scoped_student_id(args, scope)
    if error:
        return {"ok": False, "error": error}, {}
    year, term, error = _ctx_year_term(args, ctx)
    if error:
        return {"ok": False, "error": error}, {}

    try:
        gender = student_gender_strict(int(student_id))
    except UnknownStudentGender as exc:
        return {
            "ok": False,
            "error": str(exc),
            "reason": "COHORT_UNRESOLVED",
        }, {}

    raw = args.get("course_codes") or ([args["course_code"]] if args.get("course_code") else [])
    if isinstance(raw, str):
        raw = [raw]
    codes = [normalize_code(c) for c in raw if str(c).strip()]
    codes = [c for c in codes if c]
    if not codes:
        return {"ok": False, "error": "course_code (or course_codes) is required."}, {}

    catalog = _catalog_for_courses(str(year), str(term), codes, gender)
    baseline = get_student_term_baseline(int(student_id), str(year), str(term))
    return None, {
        "student_id": int(student_id),
        "year": year,
        "term": term,
        "gender": gender,
        "codes": codes,
        "catalog": catalog,
        "baseline": baseline,
    }


def _exec_my_clash_free_sections(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Which sections of a course fit the student's current timetable, and which do not."""
    from core.services.planner_builder import DAY_MAP, Meeting, _overlap

    error, c = _student_sections_context(args, scope, ctx)
    if error:
        return error

    # (meeting, label) pairs rather than a dict keyed on id(): identity of a freshly
    # built dataclass is not a stable key, and the pairing is what we actually need.
    mine: list[tuple[Any, str]] = [
        (
            Meeting(
                day=DAY_MAP.get(str(r.get("day") or ""), str(r.get("day") or "").upper()[:3]),
                start=str(r.get("start_time") or ""),
                end=str(r.get("end_time") or ""),
            ),
            f"{r.get('course_code')} {r.get('section')}",
        )
        for r in c["baseline"]
        if r.get("start_time")
    ]

    results = []
    for code in c["codes"]:
        sections = c["catalog"].get(code) or []
        if not sections:
            results.append(
                {
                    "course_code": code,
                    "sections_on_file": 0,
                    "clash_free": [],
                    "clashing": [],
                    # NOT "no sections available" — that claims the university offers
                    # none. Only 77 of 246 plan courses have any section on file.
                    "status": "NOT_ON_FILE",
                }
            )
            continue

        free, clashing = [], []
        for s in sections:
            hits = []
            for sm in s["meetings"]:
                for bm, label in mine:
                    if _overlap(sm, bm):
                        hits.append(
                            {
                                "section_meeting": f"{sm.day} {sm.start}-{sm.end}",
                                "conflicts_with": label,
                                "registered_meeting": f"{bm.day} {bm.start}-{bm.end}",
                            }
                        )
            entry = {
                "section": s["section"],
                "meetings": [f"{m.day} {m.start}-{m.end}" for m in s["meetings"]],
            }
            if hits:
                clashing.append({**entry, "conflicts": hits[:4]})
            else:
                free.append(entry)

        results.append(
            {
                "course_code": code,
                "sections_on_file": len(sections),
                "clash_free": free[:_MAX_LIST_ROWS],
                "clashing": clashing[:_MAX_LIST_ROWS],
                "status": "OK" if free else "ALL_CLASH",
            }
        )

    return {
        "ok": True,
        "student_id": c["student_id"],
        "compared_against_term": f"{c['year']}/{c['term']}",
        "courses": results,
        "note": (
            "Compared against the timetable the student is registered in for the term "
            "shown. Sections carry NO term of their own, so never call these 'next "
            "term's' sections. status NOT_ON_FILE means the section catalogue holds "
            "nothing for that course - it does NOT mean the university offers none, and "
            "must not be reported as 'no sections available'. Seat counts are absent "
            "from every section on file, so never say a section has room."
        ),
        "tool": "my_clash_free_sections",
    }


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
            allowed_roles=_STAFF_ROLES,  # cohort search: staff only, never students
            executor=_exec_find_students,
        )
    )

    registry.register(
        AdvisorCapability(
            name="get_student_context",
            description=(
                "Full verified academic context for ONE student: profile, GPA, "
                "earned credits, passed and studying courses, current-term "
                "section registrations (authoritative for what the student is "
                "registered in now — includes retakes and section labels), "
                "remaining programme requirements, and next-term "
                "recommendations. Use whenever the question is about a "
                "specific student's situation."
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
                "student using the verified recommender. The returned list stops at "
                "credit_policy.max_recommended_credit_hours — this system's own "
                "advisory cap. It is a SUGGESTION, never the registration ceiling. "
                "How many hours the student may actually register is a separate "
                "figure, credit_policy.regulatory_max_credit_hours, which is higher "
                "and may be absent for some terms. Use for 'what should I take next "
                "term' questions."
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

    registry.register(
        AdvisorCapability(
            name="my_progress",
            description=(
                "The student's full standing in their degree plan: how many courses are "
                "open to register NOW (all prerequisites satisfied), how many are blocked, "
                "which single course would unlock the most, and for every blocked course "
                "why it is blocked, how many passes away it is, and the nearest course on "
                "that chain they can take today. Use for 'what can I take', 'what is "
                "blocking me', 'what should I do next'. Broader than recommend_courses, "
                "which returns only the credit-capped suggestion for the coming term."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "student_id": {
                        "type": "integer",
                        "description": "Omit for the chatting student.",
                    },
                    "academic_year": {"type": "integer"},
                    "term": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            allowed_roles=_ALL_ROLES,
            executor=_exec_my_progress,
        )
    )

    registry.register(
        AdvisorCapability(
            name="why_course_locked",
            description=(
                "Explain ONE named course for this student: whether it is already passed, "
                "being studied, open to register now, or blocked - and if blocked, exactly "
                "which prerequisite courses are missing or how many credit hours are short, "
                "how many passes away it is, and the nearest course on the chain they can "
                "take now. Use whenever a student asks about a specific course code."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "course_code": {"type": "string", "description": "e.g. CS323"},
                    "student_id": {
                        "type": "integer",
                        "description": "Omit for the chatting student.",
                    },
                    "academic_year": {"type": "integer"},
                    "term": {"type": "integer"},
                },
                "required": ["course_code"],
                "additionalProperties": False,
            },
            allowed_roles=_ALL_ROLES,
            executor=_exec_why_course_locked,
        )
    )

    registry.register(
        AdvisorCapability(
            name="graduation_progress",
            description=(
                "How close this student is to graduating: courses passed of the plan total, "
                "percent complete, courses and credits remaining, registrar credits earned, "
                "GPA, any unmet credit-hour gate, and how many terms remain - split into the "
                "minimum forced by prerequisite chains (which cannot be beaten) and the "
                "estimate at an assumed pace. Use for 'when will I graduate', 'how much is "
                "left', 'am I close to finishing'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "student_id": {
                        "type": "integer",
                        "description": "Omit for the chatting student.",
                    },
                    "academic_year": {"type": "integer"},
                    "term": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            allowed_roles=_ALL_ROLES,
            executor=_exec_graduation_progress,
        )
    )

    registry.register(
        AdvisorCapability(
            name="my_timetable",
            description=(
                "The student's registered weekly class schedule: day, start and end time, "
                "course, section, room and instructor. Use for 'what is my schedule', 'when "
                "is my class', 'what do I have on Monday', 'where is my class'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "student_id": {
                        "type": "integer",
                        "description": "Omit for the chatting student.",
                    },
                    "academic_year": {"type": "integer"},
                    "term": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            allowed_roles=_ALL_ROLES,
            executor=_exec_my_timetable,
        )
    )

    registry.register(
        AdvisorCapability(
            name="my_plan_by_term",
            description=(
                "The student's whole degree plan laid out level by level: every course "
                "marked passed / studying / not taken, and whether prerequisites allow "
                "registering it now. Use for 'show me my plan', 'what is left in level "
                "6', 'how much of the plan have I finished'. Broader than my_progress, "
                "which returns only what is open now. Pass `term` to narrow to one plan "
                "level. can_register reflects prerequisites ONLY - it says nothing about "
                "whether a section is being taught."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "student_id": {
                        "type": "integer",
                        "description": "Omit for the chatting student.",
                    },
                    "term": {
                        "type": "integer",
                        "description": "Optional plan LEVEL (1..10), not a calendar term.",
                    },
                },
                "additionalProperties": False,
            },
            allowed_roles=_ALL_ROLES,
            executor=_exec_my_plan_by_term,
        )
    )

    registry.register(
        AdvisorCapability(
            name="my_advisor",
            description=(
                "Who the student's academic adviser is - name and department, not just "
                "the internal id. Use for 'who is my adviser', 'which department is my "
                "adviser in', 'I do not know who to ask'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "student_id": {
                        "type": "integer",
                        "description": "Omit for the chatting student.",
                    },
                },
                "additionalProperties": False,
            },
            allowed_roles=_ALL_ROLES,
            executor=_exec_my_advisor,
        )
    )

    registry.register(
        AdvisorCapability(
            name="my_clash_free_sections",
            description=(
                "For one or more courses, which sections fit the student's CURRENT "
                "registered timetable and which collide - naming the course, day and "
                "both time ranges of every collision. Use for 'which section of X can I "
                "take', 'does section F11 clash with my schedule', 'all the sections "
                "clash, is that right'. status NOT_ON_FILE means no section is recorded "
                "for that course; say exactly that, never 'no sections available'. There "
                "are no seat counts, so never claim a section has room."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "course_code": {"type": "string", "description": "e.g. CS323"},
                    "course_codes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Several courses at once.",
                    },
                    "student_id": {
                        "type": "integer",
                        "description": "Omit for the chatting student.",
                    },
                    "academic_year": {"type": "integer"},
                    "term": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            allowed_roles=_ALL_ROLES,
            executor=_exec_my_clash_free_sections,
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

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
from core.services.student_helpers import is_elective_slot as _is_elective_slot
from core.services.student_helpers import normalize_code

logger = logging.getLogger(__name__)

_STAFF_ROLES = frozenset({ROLE_SUPER_ADMIN, ROLE_GENERAL_ADVISOR, ROLE_ADVISOR})
_PROGRAM_ROLES = frozenset({ROLE_SUPER_ADMIN, ROLE_GENERAL_ADVISOR})
_ALL_ROLES = _STAFF_ROLES | frozenset({ROLE_STUDENT})

_MAX_LIST_ROWS = 20
_MAX_COURSE_MATCHES = 10

#: Chat asked to discard the student's current sections. It cannot authorise that.
#:
#: A NAMED outcome rather than a sentence, because two different callers have to
#: recognise it without matching on prose: the answering loop, which must hand the
#: student to the planner instead of paraphrasing a refusal, and the tests, which
#: must be able to tell this refusal apart from every other `ok: False`.
REBUILD_REQUIRES_PLANNER_CONFIRMATION = "REBUILD_REQUIRES_PLANNER_CONFIRMATION"
MIXED_TIMETABLE_SOURCES = "MIXED_TIMETABLE_SOURCES"


def _timetable_baseline_kind(rows: list[dict[str, Any]]) -> str:
    """One external vocabulary for every timetable-producing capability."""
    from core.services.student_sections import timetable_snapshot_kind

    return {
        "expected": "EXPECTED_PLAN",
        "mixed": "MIXED_REVIEW_REQUIRED",
        "registered": "REGISTERED",
        "empty": "EMPTY",
    }[timetable_snapshot_kind(rows)]


def _mixed_timetable_error(*, tool: str, academic_year: Any, term: Any) -> dict[str, Any]:
    """Fail closed instead of flattening two provenance classes into 'current'."""
    return {
        "ok": False,
        "tool": tool,
        "reason": MIXED_TIMETABLE_SOURCES,
        "baseline_kind": "MIXED_REVIEW_REQUIRED",
        "academic_year": academic_year,
        "term": term,
        "error": (
            "This term contains both registrar and expected-plan timetable rows. "
            "They cannot be combined into one current timetable; the snapshot needs review."
        ),
    }


# ── Scope helpers ────────────────────────────────────────────────


def _scope_role(scope: dict[str, Any] | None) -> str:
    """The role this call runs under, or nothing.

    Defaulting an absent scope to SUPER_ADMIN made every capability — including
    unfiltered cohort search — one dropped keyword argument away from being fully
    open. An unnamed caller is not the most privileged caller; it is a caller whose
    authority nobody established, and it gets none.
    """
    return str((scope or {}).get("role") or "")


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

    if role == ROLE_SUPER_ADMIN:
        return requested, None

    # Unreachable today, because the registry checks `allowed_roles` before any
    # executor runs and no role set contains the empty string. Closed anyway: an
    # open fall-through here means the restriction rests on the REGISTRY rather
    # than on the resolver, which is the same "safe by accident" shape this module
    # exists to remove.
    return None, "This request carries no authority to read a student record."


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
    if role in {ROLE_ADVISOR, ROLE_SUPER_ADMIN}:
        return requested, None
    return [], "This request carries no authority to aggregate over programmes."


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


def _section_snapshot_matches_requested_term(year: int, term: int, ctx: dict[str, Any]) -> bool:
    """Whether the termless section table is known to represent this request.

    Newer callers can name the section snapshot explicitly.  Older callers only
    carry the server-configured planning term, which is also the term for which
    the one live section snapshot is loaded.  Model-supplied ``args`` never count
    as snapshot provenance.  If neither complete pair is available, timetable
    certification must fail closed.
    """

    def _pair(year_key: str, term_key: str) -> tuple[int, int] | None:
        try:
            snapshot_year = int(ctx[year_key])
            snapshot_term = int(ctx[term_key])
        except (KeyError, TypeError, ValueError):
            return None
        if snapshot_year <= 0 or snapshot_term not in {1, 2, 3}:
            return None
        return snapshot_year, snapshot_term

    explicit_keys = {
        "section_snapshot_academic_year",
        "section_snapshot_term",
    }
    if explicit_keys.intersection(ctx):
        snapshot = _pair("section_snapshot_academic_year", "section_snapshot_term")
    else:
        snapshot = _pair("academic_year", "term")
    return snapshot == (int(year), int(term))


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


#: Re-exported so this module's callers keep their import, but there is now ONE
#: definition, in `student_helpers`, shared with the student screens. It was
#: written twice and the two disagreed on seven mandatory courses (issue #55).
is_elective_slot = _is_elective_slot


def _resolve_elective_slot(
    course_code: str, program: str, *, limit: int | None = _MAX_COURSE_MATCHES
) -> list[dict[str, Any]] | None:
    """Return the real courses that can fill an elective slot, or None if not a slot.

    A placeholder is recognised by its ProgrammeRequirement.type — `Program
    Elective`, exactly — not by guessing at the code shape, and not by the word
    "elective" either. `Free Elective` and `University Elective` are declared
    electives students TAKE: 111 have passed FE1, 139 GSE1. See `is_elective_slot`.
    """
    from core.models import ElectiveCourse, ElectiveTermMapping, ProgrammeRequirement

    req = ProgrammeRequirement.objects.filter(course_code__iexact=course_code)
    if program:
        req = req.filter(program__iexact=program)
    row = req.values("type", "program").first()
    if not row or not is_elective_slot(row.get("type")):
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
    ordered = sorted(options, key=lambda o: o["course_code"])
    # `limit=None` means every option. The cap is a DISPLAY limit — a chat answer
    # listing thirty electives is unreadable — and a caller deciding what a student
    # is ALLOWED to take must not inherit it, or the eleventh option alphabetically
    # becomes a course they are told they may not take.
    return ordered if limit is None else ordered[:limit]


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
    # An elective PLACEHOLDER (AI1, DS1, CS1 ...) is a slot, not a course. Answering
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
    from core.services.student_sections import get_student_term_baseline
    from core.services.timetable_provenance import baseline_sections
    from core.services.timetable_snapshots import Snapshot, forecast_rows
    from core.services.timetable_snapshots import select as select_snapshot
    from core.services.virtual_advisor import _course_names

    student_id, error = _resolve_scoped_student_id(args, scope)
    if error:
        return {"ok": False, "error": error}
    year, term, error = _ctx_year_term(args, ctx)
    if error:
        return {"ok": False, "error": error}

    codes = recommend_next_courses(int(student_id), int(year), int(term))
    baseline = get_student_term_baseline(
        int(student_id), str(year), str(term), snapshot=Snapshot.ANY
    )
    # Split by PROVENANCE CLASS. This used to be a local prefix test — anything not
    # `registration_plan_*` counted as registration — which put a staff planner
    # mapping into `registered_baseline`, so `already_in_current_timetable` and
    # `current_registered_credit_hours` reported a registration the student never
    # made, while the payload's own note tells the model that only
    # `already_in_expected_plan` may not be called current. The fetch above asks for
    # ANY precisely so this split can be made properly, here, once.
    registered_baseline = select_snapshot(baseline, Snapshot.REGISTERED)
    # WORKING rows land in the FORECAST half, never the registered one: they are a
    # department's assertion about the student, not the registrar's.
    expected_baseline = forecast_rows(baseline)
    current_codes = list(
        dict.fromkeys(
            normalize_code(row.get("course_code") or "")
            for row in baseline_sections(registered_baseline)
            if normalize_code(row.get("course_code") or "")
        )
    )
    expected_codes = list(
        dict.fromkeys(
            normalize_code(row.get("course_code") or "")
            for row in baseline_sections(expected_baseline)
            if normalize_code(row.get("course_code") or "")
        )
    )
    existing_codes = set(current_codes) | set(expected_codes)
    new_codes = [code for code in codes if code not in existing_codes]
    already_current = [code for code in codes if code in set(current_codes)]
    already_expected = [code for code in codes if code in set(expected_codes)]
    names = _course_names(set(codes) | existing_codes)

    profile = (
        Student.objects.filter(student_id=student_id).values("program", "status").first() or {}
    )
    program = str(profile.get("program") or "").strip()
    # Needed by credit_policy_evidence: expected graduates carry a separate,
    # unresolved 16-hour ceiling that must not be papered over with the general one.
    student_status = str(profile.get("status") or "").strip()
    credit_map: dict[str, int] = {}
    credit_qs = ProgrammeRequirement.objects.filter(course_code__in=set(codes) | existing_codes)
    if program:
        for raw_code, hours in credit_qs.filter(program__iexact=program).values_list(
            "course_code", "credit_hours"
        ):
            credit_map.setdefault(normalize_code(raw_code), int(hours or 0))
    for raw_code, hours in credit_qs.values_list("course_code", "credit_hours"):
        credit_map.setdefault(normalize_code(raw_code), int(hours or 0))
    catalogue_missing = [code for code in set(codes) | existing_codes if code not in credit_map]
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
        "recommendation_count": len(new_codes),
        "recommendation_state": (
            "NEW_RECOMMENDATIONS_FOUND" if new_codes else "NO_NEW_SYSTEM_RECOMMENDATION"
        ),
        "recommendations": [
            {
                "course_code": code,
                "course_name": names.get(code, ""),
                "credit_hours": credit_map.get(code),
            }
            for code in new_codes
        ],
        "already_in_current_timetable": [
            {
                "course_code": code,
                "course_name": names.get(code, ""),
                "credit_hours": credit_map.get(code),
            }
            for code in already_current
        ],
        "already_in_expected_plan": [
            {
                "course_code": code,
                "course_name": names.get(code, ""),
                "credit_hours": credit_map.get(code),
            }
            for code in already_expected
        ],
        "current_registered_credit_hours": sum(
            credit_map[code] for code in current_codes if code in credit_map
        ),
        "credit_policy": credit_policy_evidence(
            recommended_credit_hours=sum(
                credit_map[code] for code in new_codes if code in credit_map
            ),
            unknown_for=[code for code in new_codes if code not in credit_map],
            term=term,
            student_status=student_status,
        ),
        "note": (
            "recommendations contains only courses not already in the registered timetable "
            "or expected plan. Never call already_in_expected_plan registered/current. "
            "If recommendations is empty, say there is no new "
            "system-recommended course rather than repeating a current course. That empty "
            "list does not prove courses are closed/unavailable or that a credit cap caused "
            "the result, so do not speculate about either."
        ),
    }


def _exec_course_choice_comparison(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Compare two to four named courses from one verified planning baseline."""
    from core.services.course_choice_comparison import compare_course_choices

    student_id, error = _resolve_scoped_student_id(args, scope)
    if error:
        return {"ok": False, "error": error, "tool": "course_choice_comparison"}
    if student_id is None:
        return {
            "ok": False,
            "error": "Student identity is required.",
            "tool": "course_choice_comparison",
        }
    year, term, error = _ctx_year_term(args, ctx)
    if error:
        return {"ok": False, "error": error, "tool": "course_choice_comparison"}
    timetable_evidence_available = _section_snapshot_matches_requested_term(
        int(year), int(term), ctx
    )

    raw_codes = args.get("course_codes")
    if not isinstance(raw_codes, list):
        return {
            "ok": False,
            "error": "course_codes must be a list of two to four course codes.",
            "tool": "course_choice_comparison",
        }
    codes = [normalize_code(code) for code in raw_codes if normalize_code(code)]
    if len(codes) < 2 or len(codes) > 4:
        return {
            "ok": False,
            "error": "Choose two to four course codes to compare.",
            "tool": "course_choice_comparison",
        }
    if len(set(codes)) != len(codes):
        return {
            "ok": False,
            "error": "Each compared course must be different.",
            "tool": "course_choice_comparison",
        }

    objective = str(args.get("objective") or "balanced").strip().lower()
    if objective not in {"balanced", "graduation", "unlock_impact", "timetable_fit"}:
        return {
            "ok": False,
            "error": "Unsupported comparison objective.",
            "tool": "course_choice_comparison",
        }
    try:
        return compare_course_choices(
            int(student_id),
            codes,
            int(year),
            int(term),
            objective=objective,
            timetable_evidence_available=timetable_evidence_available,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "tool": "course_choice_comparison"}


def _exec_feasible_course_replacements(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Certify academically improving swaps against one complete timetable."""
    from core.services.course_replacement_feasibility import (
        find_feasible_course_replacements,
    )

    student_id, error = _resolve_scoped_student_id(args, scope)
    if error:
        return {"ok": False, "error": error, "tool": "feasible_course_replacements"}
    if student_id is None:
        return {
            "ok": False,
            "error": "Student identity is required.",
            "tool": "feasible_course_replacements",
        }
    year, term, error = _ctx_year_term(args, ctx)
    if error:
        return {"ok": False, "error": error, "tool": "feasible_course_replacements"}

    remove_code = normalize_code(args.get("remove_course") or "")
    add_code = normalize_code(args.get("add_course") or "")
    if remove_code and add_code and remove_code == add_code:
        return {
            "ok": False,
            "error": "The removed and added courses must be different.",
            "tool": "feasible_course_replacements",
        }

    if not _section_snapshot_matches_requested_term(int(year), int(term), ctx):
        return {
            "ok": True,
            "tool": "feasible_course_replacements",
            "academic_year": int(year),
            "term": int(term),
            "baseline_kind": "NOT_EVALUATED",
            "status": "NOT_DETERMINABLE",
            "requested_remove_course": remove_code or None,
            "requested_add_course": add_code or None,
            "academic_search": {
                "pairs_evaluated": 0,
                "search_truncated": False,
                "candidate_courses_considered": [],
            },
            "certification_search": {
                "academic_candidates_received": 0,
                "timetable_candidates_checked": 0,
                "certified_result_limit": 0,
                "search_truncated": False,
            },
            "certified_replacements": [],
            "rejected_replacements": [
                {
                    "remove_course": {"course_code": remove_code},
                    "add_course": {"course_code": add_code},
                    "academic": {"status": "NOT_EVALUATED"},
                    "timetable": {
                        "status": "NOT_DETERMINABLE",
                        "reason_code": "SECTION_SNAPSHOT_TERM_MISMATCH",
                        "reason": (
                            "The section catalogue is not verified for the requested term, "
                            "so a replacement timetable cannot be certified."
                        ),
                    },
                }
            ],
            "rejected_replacements_count": 1,
            "limitations": [
                "The section catalogue is a recorded, termless snapshot and is not "
                "verified for the requested term."
            ],
        }

    try:
        return {
            "ok": True,
            "tool": "feasible_course_replacements",
            **find_feasible_course_replacements(
                int(student_id),
                int(year),
                int(term),
                remove_course=remove_code or None,
                add_course=add_code or None,
                max_credits_per_term=18,
            ),
        }
    except (TypeError, ValueError) as exc:
        return {
            "ok": False,
            "error": str(exc),
            "tool": "feasible_course_replacements",
        }
    except Exception:
        logger.exception("Feasible course replacement certification failed")
        return {
            "ok": False,
            "error": (
                "The verified replacement check could not be completed from the recorded data."
            ),
            "tool": "feasible_course_replacements",
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


#: Every `kind` `build_unlock_report` can emit, said in words.
#:
#: The fall-through used to be `x["kind"].lower()`, which handed the model — and so
#: the student — the literal string `unknown_prereq`. That is not a rare edge:
#: UNKNOWN_PREREQ fires for 74 of 320 students on live data, because a plan can name
#: a prerequisite that is not itself a plan row. `ASK_ADVISOR` carries no payload at
#: all and would have read as `ask_advisor`.
#:
#: A closed vocabulary in, a sentence out, and an unknown kind says only what is
#: actually known — the same contract the student screen's template already keeps.
def _explain_reason(reason: dict[str, Any]) -> str:
    kind = str(reason.get("kind") or "")
    if kind == "MISSING_COURSE":
        return f"needs {reason.get('code')}"
    if kind == "MISSING_HOURS":
        return f"needs {reason.get('required')} credit hours, has {reason.get('effective')}"
    if kind == "UNKNOWN_PREREQ":
        # The code is, by definition, not in this student's plan — so there is no
        # name to give and no chain to point at.
        return (
            f"lists {reason.get('code')} as a prerequisite, which is not in this "
            "student's plan; the adviser must confirm it"
        )
    return "the adviser must confirm why this course is blocked"


#: THE sentence for "the prerequisite records are met", written once so the two
#: capabilities that report it cannot drift apart again. The old wording was "Every
#: prerequisite is satisfied; it can be registered." — and `_exec_my_progress` said,
#: three fields below it, that it does "not know which courses actually run this
#: term or seat availability". One payload asserted a registration permission and
#: denied having the evidence for it, and the model was left to pick which half to
#: believe. It picked the permission.
_PREREQS_SATISFIED_EXPLANATION = (
    "All recorded prerequisite conditions are satisfied. This does not confirm that "
    "a section is offered, that registration is permitted, or that a seat is available."
)


def _impact_row(blocker: dict[str, Any] | None) -> dict[str, Any] | None:
    """One open course with BOTH forward counts, under names that say which is which.

    `build_unlock_report` calls them `frees_now` and `frees_eventually`, and a
    reader who has not opened that module cannot tell that the second frees nothing
    — it is the number of courses with this one somewhere in their chain. The
    student screen rendered it as «يفتح لك 6 من مقرراتك المتبقية» when three open.
    """
    if not blocker:
        return None
    return {
        "code": blocker["code"],
        "course_name": blocker["name"],
        "sole_remaining_prerequisite_count": blocker["frees_now"],
        "on_prerequisite_chain_of_count": blocker["frees_eventually"],
    }


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
        return [_explain_reason(x) for x in c["reasons"]]

    return {
        "student_id": int(student_id),
        "program": r["program"],
        "academic_year": year,
        "term": term,
        "counts": r["counts"],
        "most_useful_course_to_pass": _impact_row(r["top_blocker"]),
        # Ranked, not just the winner. «رتّب لي AI331 و CS372 و AI352 حسب تأثير كل
        # واحد» and «أي مقرر يفتح أكبر عدد مباشرة» both need an ORDER over several
        # courses, and the payload carried a single `max()`. Ranking three named
        # courses from one winner is not possible, so the answer was composed.
        # This list is deliberately complete. The capability contract and note both
        # say every academically open concrete course appears here; applying the
        # generic 20-row display cap silently changed that claim for larger plans.
        "unlock_impact_ranking": [_impact_row(b) for b in r["blockers"]],
        # The basis, stated. A ranking whose criterion is unnamed is read as "these
        # are the courses that matter", and this one measures exactly one thing.
        "unlock_impact_ranking_basis": "SOLE_REMAINING_UNLOCK_COUNT_THEN_DOWNSTREAM_COUNT",
        "unlock_impact_ranking_note": (
            "Every course whose prerequisites are satisfied appears here, including "
            "those that unlock nothing - a zero is a real answer, not a reason to omit "
            "the course. Unlock impact is one criterion among several: a course may "
            "still be required for graduation, be in this term's recommendation, or be "
            "needed to reach a reasonable load, and none of those is measured here."
        ),
        "prerequisites_satisfied": [
            {
                "code": c["code"],
                "course_name": c["name"],
                "credits": c["credits"],
                "fits_this_term": c["fits_this_term"],
            }
            for c in r["open_courses"][:_MAX_LIST_ROWS]
        ],
        "elective_slots": [c["code"] for c in r["elective_slots"]],
        "prerequisite_blocked": [
            {
                "code": c["code"],
                "course_name": c["name"],
                "steps_away": c["steps"],
                # Was `opens_n_courses`, which is the count of courses with this one
                # ANYWHERE in their remaining chain. Passing it removes one link from
                # each; it opens none of them by itself, and for a course two steps
                # down it may open none of them ever on its own.
                "on_prerequisite_chain_of_count": c["frees_eventually"],
                "nearest_course_you_can_take_now": (c["nearest_open"] or {}).get("code"),
                "why": why(c),
            }
            for c in r["locked_courses"][:_MAX_LIST_ROWS]
        ],
        "note": (
            "A course being studied satisfies a prerequisite but must still be passed. "
            + _PREREQS_SATISFIED_EXPLANATION
        ),
        # `open_now` and `blocked` are GONE rather than aliased. Every consumer was
        # checked: the privacy projector kept neither, no template or script reads
        # this payload, and the only other reader is the model. Carrying both names
        # would put two lists with one meaning into the same prompt — which is the
        # defect class this commit exists to remove, not a mitigation of it.
        "renamed_fields": {
            "open_now": "prerequisites_satisfied",
            "blocked": "prerequisite_blocked",
        },
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

    # THE FORWARD DIRECTION, IN TWO FIELDS THAT ARE NOT THE SAME NUMBER.
    #
    # This used to emit one list, `unlocks_directly`, built from every graph edge
    # whose `prerequisite_course_code` was this course. That is "X names this course
    # among its prerequisites" — and the name promised "X opens when you pass this".
    # On the controlled evaluation record, AI331 is named by five courses and only three of them are
    # waiting on it alone; the other two also need CS289 and COE332. So the answer to
    # «كم مقرر ينتظر AI331 وحده» was 5, and the true answer is 3.
    #
    # The field is not kept as an alias. Both readings are useful and both are
    # published, but `unlocks_directly` is the name that carried the false one, and
    # an alias would preserve exactly the claim this exists to withdraw.
    deps = (r.get("dependents") or {}).get(code) or {}
    listed_rows = deps.get("listed") or []
    waiting_only = set(deps.get("waiting_only_on_this") or [])
    base = {
        "student_id": int(student_id),
        "course_code": code,
        # Catalogue direction: true of the programme, whatever this student passed.
        "listed_as_prerequisite_for": [
            {
                "code": row["code"],
                "course_name": row["name"],
                "current_status": row["status"],
                "still_also_waiting_on": row["also_waiting_on"],
                "also_short_on_credit_hours": row["also_waiting_on_credit_hours"],
            }
            for row in listed_rows
        ],
        "listed_as_prerequisite_count": len(listed_rows),
        # Student direction: what actually changes the day this course is passed.
        "sole_remaining_prerequisite_for": [
            {"code": row["code"], "course_name": row["name"]}
            for row in listed_rows
            if row["code"] in waiting_only
        ],
        "sole_remaining_prerequisite_count": len(waiting_only),
        # Transitive, and moved into `base` from the blocked branch. AI331 is OPEN
        # for this student, so «وش الفرق بين ما يفتحه مباشرة وما ينفتح عبر السلسلة»
        # was asked of a payload that carried no chain number at all — the count
        # existed only on the branch taken by courses that are themselves blocked.
        "on_prerequisite_chain_of_count": deps.get("on_chain_of_count", 0),
        "forward_relations_note": (
            "listed_as_prerequisite_count counts courses that NAME this one as a "
            "prerequisite. sole_remaining_prerequisite_count counts those for which "
            "it is the last unmet condition — the ones that become "
            "prerequisite-satisfied when it is passed. "
            "on_prerequisite_chain_of_count counts courses with it anywhere in their "
            "remaining chain; passing it removes one link and does not make them "
            "takeable. The three are usually different numbers."
        ),
    }
    for c in r["open_courses"]:
        if c["code"] == code:
            return {
                **base,
                # Was "open_now", explained as "it can be registered" — a
                # registration-permission claim this module says two fields later it
                # cannot make. The status names the only thing that was checked.
                "status": "PREREQUISITES_SATISFIED",
                "prerequisites_satisfied": True,
                "course_name": c["name"],
                "fits_this_term": c["fits_this_term"],
                "explanation": _PREREQS_SATISFIED_EXPLANATION,
            }
    for c in r["done"]:
        if c["code"] == code:
            return {
                **base,
                "status": "passed",
                "prerequisites_satisfied": True,
                "course_name": c["name"],
                "explanation": "Already passed.",
            }
    for c in r["in_progress"]:
        if c["code"] == code:
            return {
                **base,
                "status": "studying",
                "prerequisites_satisfied": True,
                "course_name": c["name"],
                "explanation": "Being studied now; must still be passed.",
            }
    for c in r["locked_courses"]:
        if c["code"] == code:
            return {
                **base,
                "status": "PREREQUISITE_BLOCKED",
                "prerequisites_satisfied": False,
                "course_name": c["name"],
                "steps_away": c["steps"],
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
    """How far from graduating plus a read-only 18-credit term scenario."""
    from core.services.student_graduation import (
        build_graduation_report,
        build_graduation_what_if,
    )

    student_id, error = _resolve_scoped_student_id(args, scope)
    if error:
        return {"ok": False, "error": error}
    year, term, error = _ctx_year_term(args, ctx)
    if error:
        return {"ok": False, "error": error}

    remove_courses = args.get("remove_current_courses") or []
    add_courses = args.get("add_current_courses") or []
    search_replacements = bool(args.get("search_better_replacements", False))
    if not isinstance(remove_courses, list) or not isinstance(add_courses, list):
        return {"ok": False, "error": "Planning-baseline course changes must be lists."}
    if remove_courses or add_courses or search_replacements:
        g = build_graduation_what_if(
            int(student_id),
            int(year),
            int(term),
            remove_current_courses=[str(code) for code in remove_courses],
            add_current_courses=[str(code) for code in add_courses],
            search_better_replacements=search_replacements,
        )
    else:
        g = build_graduation_report(int(student_id), int(year), int(term))
    if not g:
        return {"ok": False, "error": f"No degree plan found for student {student_id}."}
    return {
        "student_id": int(student_id),
        "program": g["program"],
        "planning_baseline_academic_year": g["planning_baseline_academic_year"],
        "planning_baseline_term": g["planning_baseline_term"],
        "scenario_academic_year": int(year),
        "scenario_term": int(term),
        "plan_courses_passed": g["plan_courses_passed"],
        "plan_courses_total": g["plan_courses_total"],
        "percent_complete": g["percent_courses"],
        "courses_remaining": g["remaining_courses"],
        "credits_remaining_in_plan": g["remaining_credits"],
        "credits_earned_registrar": g["earned_credits_registrar"],
        "gpa": g["gpa"],
        "minimum_terms_by_prerequisites": g["chain_floor_terms"],
        "minimum_terms_by_credit_capacity_after_planning_baseline": g[
            "capacity_floor_terms_after_planning_baseline"
        ],
        "minimum_terms_by_credit_capacity_after_current": g["capacity_floor_terms_after_current"],
        "lower_bound_additional_terms": g["lower_bound_additional_terms"],
        "lower_bound_terms_including_planning_baseline": g[
            "lower_bound_terms_including_planning_baseline"
        ],
        "lower_bound_terms_including_current": g["lower_bound_terms_including_current"],
        "max_credits_per_term": g["max_credits_per_term"],
        "estimated_additional_terms": g["estimated_additional_terms"],
        "estimated_terms_including_planning_baseline": g[
            "estimated_terms_including_planning_baseline"
        ],
        "estimated_terms_including_current": g["estimated_terms_including_current"],
        "terms_estimate": g["terms_estimate"],
        "simulation_completed": g["simulation_completed"],
        "simulated_terms_examined": g["simulated_terms_examined"],
        "productive_terms_planned": g["productive_terms_planned"],
        "term_plan": g["term_plan"],
        "unresolved_requirements": g["unresolved_requirements"],
        "scenario_graph": g.get("scenario_graph") or {},
        "planning_baseline_courses_assumed_passed": g["planning_baseline_courses_assumed_passed"],
        "current_courses_assumed_passed": g["current_courses_assumed_passed"],
        "simulation_assumptions": g["simulation_assumptions"],
        "credit_hour_gates": g["hour_gates"],
        # Computed by build_graduation_report and previously dropped on the floor.
        # "can this be my last term?" is one of the most-asked questions and the
        # answer was already sitting in the report.
        "plan_completion_in_planning_baseline_possible": g[
            "plan_completion_in_planning_baseline_possible"
        ],
        "final_term_possible": g["final_term_possible"],
        "passed_credits_in_plan": g["passed_credits_in_plan"],
        "registered_credits_at_planning_baseline": g["registered_credits_at_planning_baseline"],
        "registered_credits_now": g["registered_credits_now"],
        "courses_in_progress": g["in_progress"],
        "what_if": g.get("what_if"),
        "note": (
            "Registrar credits include courses outside the plan, so they are not a "
            "fraction of the plan total. The estimate repeatedly runs the existing "
            "course recommender one main term ahead, assumes the selected planning-"
            "baseline Planner courses pass, then rolls each simulated term forward "
            "in memory only. The planning baseline may be an expected next-term plan, "
            "so it must not be described as the student's actual current term. "
            "It uses an 18-credit maximum for every simulated term and does not "
            "guarantee offerings, seats, or first-attempt passes. final_term_possible "
            "means the PLAN could be finished in the planning-baseline term; graduation "
            "itself is a University Council "
            "decision (TU.GRADUATION.COUNCIL_AWARDS_DEGREE), so it must never be "
            "reported as 'you are graduating'."
        ),
    }


def _exec_my_timetable(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """The student's registered weekly schedule: day, time, course, section, room."""
    from core.services.student_sections import (
        get_student_term_baseline,
        student_gender,
    )
    from core.services.timetable_snapshots import Snapshot

    student_id, error = _resolve_scoped_student_id(args, scope)
    if error:
        return {"ok": False, "error": error}
    year, term, error = _ctx_year_term(args, ctx)
    if error:
        return {"ok": False, "error": error}

    rows = get_student_term_baseline(
        int(student_id), str(year), str(term), snapshot=Snapshot.EFFECTIVE
    )
    if not rows:
        from core.models import StudentTermSection

        published = list(
            StudentTermSection.objects.filter(term_section__scenario__isnull=True)
            .values_list("academic_year", "term")
            .distinct()[:2]
        )
        if len(published) == 1:
            year, term = published[0]
            rows = get_student_term_baseline(
                int(student_id), str(year), str(term), snapshot=Snapshot.EFFECTIVE
            )

    gender = student_gender(int(student_id))
    if gender:
        rows = [
            r
            for r in rows
            if not str(r.get("section") or "").upper().startswith(("M", "F"))
            or str(r.get("section") or "").upper().startswith(gender)
        ]
    schedule_kind = _timetable_baseline_kind(rows)
    if schedule_kind == "MIXED_REVIEW_REQUIRED":
        mixed = _mixed_timetable_error(
            tool="my_timetable",
            academic_year=year,
            term=term,
        )
        mixed["schedule_kind"] = schedule_kind
        mixed["is_expected_plan"] = False
        return mixed
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
    unscheduled = [r for r in registrations if not r["scheduled"]]
    credit_hours = sum(r["credits"] for r in registrations)
    result = {
        "student_id": int(student_id),
        "academic_year": year,
        "term": term,
        "schedule_kind": schedule_kind,
        "is_expected_plan": schedule_kind == "EXPECTED_PLAN",
        "meetings": meetings[: _MAX_LIST_ROWS * 2],
        "registrations": registrations,
        "courses_without_a_time": sorted(r["course_code"] for r in unscheduled),
    }
    if schedule_kind == "EXPECTED_PLAN":
        result.update(
            {
                "expected_course_count": len(registrations),
                "expected_credit_hours": credit_hours,
                "note": (
                    "This is an expected next-term planning snapshot, not actual university "
                    "registration. Never describe these courses or sections as registered or "
                    "current; the student must apply choices in the university portal."
                ),
            }
        )
    else:
        result.update(
            {
                "registered_course_count": len(registrations),
                "registered_credit_hours": credit_hours,
                "note": (
                    "The registered timetable on file for the term shown; not a live seat "
                    "count. registered_credit_hours counts each course once. Courses without "
                    "a time are registered rows whose meeting time is not recorded."
                ),
            }
        )
    return result


def _plan_terms_with_canonical_readiness(terms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add `prerequisites_satisfied` beside `can_register`, without renaming it.

    `can_register` is `report_views._build_student_plan_payload`'s field and it is
    read by name in fifteen places across `page-dashboard.js` and `page-planner.js`,
    plus `report_views` itself, which filters and counts on it. Renaming it there to
    fix a description the MODEL reads would break two screens for no gain to either.

    So the canonical name is added here, at the boundary where a language model is
    the reader, and the legacy name travels beside it. Copied, not mutated: the same
    payload objects are cached and served to those screens, and writing into them
    would leak a field into the JSON the browser gets.

    DERIVED FROM `missing_prereqs`, NOT COPIED FROM `can_register`. The first version
    copied it, and a bit-for-bit copy gives the new name a predicate that is not the
    one it names: `can_register` is `status == "not_taken" and prereqs_ok`, so every
    course the student has already PASSED came back as
    `prerequisites_satisfied: false` — 32 of 32 on the controlled evaluation
    record. A field renamed
    to say what it means has to mean it; otherwise the rename moves the defect
    instead of removing it, and this one told the model that a course the student
    passed still has prerequisites outstanding.

    `missing_prereqs` already carries the hour gate: `report_views` appends
    "146(HOURS)" to it when the gate is unmet, so a capstone short on credit hours is
    not satisfied here either.
    """
    out = []
    for level in terms:
        courses = level.get("courses")
        if not isinstance(courses, list):
            out.append(level)
            continue
        out.append(
            {
                **level,
                "courses": [
                    {**c, "prerequisites_satisfied": not (c.get("missing_prereqs") or [])}
                    if isinstance(c, dict)
                    else c
                    for c in courses
                ],
            }
        )
    return out


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
        "terms": _plan_terms_with_canonical_readiness(terms),
        "blocker_hints": payload.get("blocker_hints") or [],
        "note": (
            "Plan LEVELS, not calendar terms - programme_term is where a course sits in "
            "the degree plan, not when it is taught. status is passed / studying / "
            "failed / not_taken. prerequisites_satisfied is the canonical field and is the ONE "
            "to read: it says the recorded prerequisite conditions are met, whatever "
            "the student has already done, so a PASSED course is satisfied. "
            "can_register is a different, older boolean kept for the screens - it is "
            "false for every passed and studying course, and it is NOT a registration "
            "permission. Never read can_register as prerequisite state. "
            + _PREREQS_SATISFIED_EXPLANATION
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
    from core.services.timetable_snapshots import Snapshot

    student_id, error = _resolve_scoped_student_id(args, scope)
    if error:
        return {"ok": False, "error": error}, {}
    if student_id is None:
        return {"ok": False, "error": "student_id is required."}, {}
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

    from core.models import Student

    program = str(
        Student.objects.filter(student_id=student_id).values_list("program", flat=True).first()
        or ""
    ).strip()
    if not program:
        return {
            "ok": False,
            "error": "Student programme is not recorded.",
            "reason": "PROGRAMME_UNRESOLVED",
        }, {}
    catalog = _catalog_for_courses(str(year), str(term), codes, gender, program)
    baseline = get_student_term_baseline(
        int(student_id), str(year), str(term), snapshot=Snapshot.EFFECTIVE
    )
    baseline_kind = _timetable_baseline_kind(baseline)
    if baseline_kind == "MIXED_REVIEW_REQUIRED":
        return _mixed_timetable_error(
            tool="my_clash_free_sections",
            academic_year=year,
            term=term,
        ), {}
    return None, {
        "student_id": int(student_id),
        "year": year,
        "term": term,
        "gender": gender,
        "codes": codes,
        "catalog": catalog,
        "baseline": baseline,
        "baseline_kind": baseline_kind,
    }


def _exec_my_clash_free_sections(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Which sections fit the student's source-classified timetable baseline."""
    from core.services.planner_builder import DAY_MAP, Meeting, _overlap

    error, c = _student_sections_context(args, scope, ctx)
    if error:
        return error

    # (meeting, label) pairs rather than a dict keyed on id(): identity of a freshly
    # built dataclass is not a stable key, and the pairing is what we actually need.
    mine: list[tuple[Any, str, str]] = [
        (
            Meeting(
                day=DAY_MAP.get(str(r.get("day") or ""), str(r.get("day") or "").upper()[:3]),
                start=str(r.get("start_time") or ""),
                end=str(r.get("end_time") or ""),
            ),
            f"{r.get('course_code')} {r.get('section')}",
            normalize_code(r.get("course_code")),
        )
        for r in c["baseline"]
        if r.get("start_time")
    ]

    results = []
    for code in c["codes"]:
        sections = c["catalog"].get(code) or []
        baseline_sections_for_course = sorted(
            {
                str(row.get("section") or "").strip()
                for row in c["baseline"]
                if normalize_code(row.get("course_code")) == code
                and str(row.get("section") or "").strip()
            }
        )
        if not sections:
            results.append(
                {
                    "course_code": code,
                    "sections_on_file": 0,
                    "currently_registered_sections": (
                        baseline_sections_for_course if c["baseline_kind"] == "REGISTERED" else []
                    ),
                    "expected_plan_sections": (
                        baseline_sections_for_course
                        if c["baseline_kind"] == "EXPECTED_PLAN"
                        else []
                    ),
                    "baseline_sections": baseline_sections_for_course,
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
                # When the student is switching/checking a section of a course
                # already in the timetable, replace that course's existing block
                # before comparing. Otherwise M3 collides with its own three
                # meetings and is reported as unusable precisely because it is
                # already registered.
                for bm, label, baseline_code in mine:
                    if baseline_code == code:
                        continue
                    if _overlap(sm, bm):
                        hits.append(
                            {
                                "section_meeting": f"{sm.day} {sm.start}-{sm.end}",
                                "conflicts_with": label,
                                "baseline_meeting": f"{bm.day} {bm.start}-{bm.end}",
                            }
                        )
            entry = {
                "section": s["section"],
                "meetings": [f"{m.day} {m.start}-{m.end}" for m in s["meetings"]],
                "is_baseline_section": str(s["section"]) in baseline_sections_for_course,
                "is_current_section": (
                    c["baseline_kind"] == "REGISTERED"
                    and str(s["section"]) in baseline_sections_for_course
                ),
                "is_expected_plan_section": (
                    c["baseline_kind"] == "EXPECTED_PLAN"
                    and str(s["section"]) in baseline_sections_for_course
                ),
            }
            if hits:
                clashing.append({**entry, "conflicts": hits[:4]})
            else:
                free.append(entry)

        results.append(
            {
                "course_code": code,
                "sections_on_file": len(sections),
                "currently_registered_sections": (
                    baseline_sections_for_course if c["baseline_kind"] == "REGISTERED" else []
                ),
                "expected_plan_sections": (
                    baseline_sections_for_course if c["baseline_kind"] == "EXPECTED_PLAN" else []
                ),
                "baseline_sections": baseline_sections_for_course,
                "clash_free": free[:_MAX_LIST_ROWS],
                "clashing": clashing[:_MAX_LIST_ROWS],
                "status": "OK" if free else "ALL_CLASH",
            }
        )

    return {
        "ok": True,
        "student_id": c["student_id"],
        "compared_against_term": f"{c['year']}/{c['term']}",
        "baseline_kind": c["baseline_kind"],
        "courses": results,
        "note": (
            "Compared against the stored baseline identified by baseline_kind. EXPECTED_PLAN "
            "is planning data, not registration; MIXED_REVIEW_REQUIRED must not be presented "
            "as one registered timetable. Sections carry NO term of their own, so never call these 'next "
            "term's' sections. status NOT_ON_FILE means the section catalogue holds "
            "nothing for that course - it does NOT mean the university offers none, and "
            "must not be reported as 'no sections available'. A section marked "
            "is_current_section is registrar evidence; is_expected_plan_section is not. When "
            "checking another section of the same course, the current section is replaced "
            "before clash comparison. Seat counts are absent from this result, so never "
            "say a section has room."
        ),
        "tool": "my_clash_free_sections",
    }


#: build_plans reports its own reasons, and one of them is actively misleading to a
#: student: "No sections available" claims the university offers none, when what it
#: means is that our catalogue holds none — true for 169 of 246 plan course codes, and
#: true by definition for every elective placeholder. Each reason is translated to a
#: code plus wording that says only what the data supports.
_UNPLACED_REASONS: dict[str, tuple[str, str]] = {
    "No sections available": (
        "NOT_ON_FILE",
        "No section for this course is recorded in our data. That is not the same as "
        "the university not offering it — check the registration portal.",
    ),
    "No non-conflicting sections available": (
        "ALL_SECTIONS_CLASH",
        "Every section on file collides with something else in this plan.",
    ),
    "Could not fit with chosen constraints/objective": (
        "DID_NOT_FIT",
        "It could not be fitted alongside the rest under the limits given.",
    ),
    "Model infeasible under current hard constraints": (
        "DID_NOT_FIT",
        "No combination satisfied all the limits given.",
    ),
    "Section meeting data is incomplete or invalid": (
        "MEETING_DATA_INCOMPLETE",
        "A recorded section has missing or invalid meeting data, so clashes cannot be certified.",
    ),
}


def _translate_unplaced(raw: str) -> tuple[str, str]:
    text = str(raw or "").strip()
    for prefix, mapped in _UNPLACED_REASONS.items():
        if text.startswith(prefix):
            return mapped
    if text.lower().startswith("blocked by prerequisites"):
        return "PREREQUISITES", text
    if text.lower().startswith("strict mode"):
        return "DID_NOT_FIT", "It could not be fitted under the limits given."
    return "OTHER", text


def _exec_build_my_timetable(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """A clash-free weekly timetable from the sections on file, with an honest tail.

    Wires the SERVICE ``planner_builder.build_plans`` directly, never the HTTP view —
    ``planner_build_view`` is staff-only and throttled, and neither applies to a
    student asking about their own timetable.

    The partial answer is the point. Measured against today's catalogue, a complete
    timetable is possible for roughly 6% of students; most get some courses placed and
    some not. So ``unplaced`` and its reasons are returned as first-class output rather
    than being hidden behind a "no plan found".
    """
    # FIRST, before any import, query or recommendation. The check used to sit
    # after an early return for "nothing to schedule", so a student with an empty
    # plan asking for a rebuild got an ordinary empty result and no route — and
    # now that the model is told to CALL for this, that silent path is the one it
    # would hit. Refusing a rebuild must never depend on how much work the
    # recommender happened to find first.
    # REBUILDING IS NOT AVAILABLE FROM CHAT. It is refused here, in the executor,
    # not merely undocumented in the schema.
    #
    # This used to be `bool(args.get("keep_current_sections", True))`, and the
    # requirement to confirm first lived in the tool's JSON description — a
    # sentence addressed to the model, not a gate. Given the single Arabic word
    # «أكد» with no prior turn establishing what was being confirmed, the model
    # reasoned "this implies they want a full rebuild" and called this capability
    # with `keep_current_sections=false`. Nothing stopped it.
    #
    # The real control already exists on the planner draft path:
    # `planner_drafts.issue_rebuild_token` — hashed, one-use, bound to student +
    # draft + version, fifteen-minute expiry — built precisely because a review of
    # PR #53 found a valid token authorising content the student had never seen.
    # A second confirmation mechanism in chat would not be that control; it would
    # be a way around it. Chat hands the student to the planner instead.
    #
    # `is not True`, not `is False`: a caller that supplies the string "false", 0
    # or None is malformed, and inferring "keep" from a value we could not parse
    # is how the safe default stops being safe. Only an explicit True — or the
    # absence of the argument — means keep.
    requested_keep = args.get("keep_current_sections", True)
    if requested_keep is not True:
        return {
            "ok": False,
            "reason": REBUILD_REQUIRES_PLANNER_CONFIRMATION,
            "error": (
                "Rebuilding without the student's current sections requires "
                "confirmation through the planner draft workflow."
            ),
            "action": "OPEN_STUDENT_PLANNER",
            "tool": "build_my_timetable",
        }
    keep_current_sections = True

    from core.models import ProgrammeRequirement, Student
    from core.services.recommender import recommend_next_courses

    # Through the adapter, not around it. `student_planner.run_solver` calls itself
    # "the ONLY place student-facing code reaches the solver", and this — a
    # student-facing capability — was the counter-example, with the domain-to-solver
    # translation and all three pinned levers duplicated verbatim. Two sole sources
    # of truth is none. Imported at the top of the body rather than beside the call,
    # because DEFAULT_CREDITS is now the one credit fallback for the whole answer and
    # every return path below needs it.
    from core.services.student_planner import DEFAULT_CREDITS, run_solver
    from core.services.student_sections import (
        UnknownStudentGender,
        get_student_term_baseline,
        student_gender_strict,
    )
    from core.services.timetable_provenance import (
        baseline_sections,
        build_timetable_facts,
        verify,
    )
    from core.services.timetable_snapshots import Snapshot

    student_id, error = _resolve_scoped_student_id(args, scope)
    if error:
        return {"ok": False, "error": error}
    year, term, error = _ctx_year_term(args, ctx)
    if error:
        return {"ok": False, "error": error}

    try:
        gender = student_gender_strict(int(student_id))
    except UnknownStudentGender as exc:
        return {"ok": False, "error": str(exc), "reason": "COHORT_UNRESOLVED"}

    program = str(
        Student.objects.filter(student_id=student_id).values_list("program", flat=True).first()
        or ""
    ).strip()
    if not program:
        return {
            "ok": False,
            "error": "Student programme is not recorded.",
            "reason": "PROGRAMME_UNRESOLVED",
            "tool": "build_my_timetable",
        }
    credits = {
        r["course_code"]: int(r["credit_hours"] or 0)
        # `iexact`, matching `get_student_term_baseline` and
        # `student_planner._course_credits`, which read the same table for the same
        # fact. An exact match here gives a programme stored in any other case an
        # EMPTY credit map — harmless while the only consumer was a display total,
        # and not harmless now that `credit_summary` reconciles against the
        # baseline's own credits. Dormant on today's data (0 of 4 live programmes
        # differ in case), fixed because the two lookups have to agree.
        for r in ProgrammeRequirement.objects.filter(program__iexact=program).values(
            "course_code", "credit_hours"
        )
    }

    max_credits = args.get("max_credits")
    try:
        cap = int(max_credits) if max_credits not in (None, "") else 0
    except (TypeError, ValueError):
        return {"ok": False, "error": "max_credits must be an integer."}

    # Courses to place: what the student asked for, AND the official recommendation
    # — kept as two lists the whole way to the answer. Merging them into one field
    # called `requested` is what left TT21 «الجدول أضاف مقررًا أنا ما طلبته، من وين
    # جاء؟» unanswerable: the payload asserted the student had asked for all four
    # courses, so the model concluded the system keeps no record of who chose what.
    wanted = args.get("must_include") or []
    if isinstance(wanted, str):
        wanted = [wanted]
    wanted = [normalize_code(c) for c in wanted if str(c).strip()]
    recommended = [
        normalize_code(c)
        for c in (recommend_next_courses(int(student_id), int(year), int(term)) or [])
    ]
    asked = list(dict.fromkeys(wanted + recommended))

    baseline = get_student_term_baseline(
        int(student_id), str(year), str(term), snapshot=Snapshot.EFFECTIVE
    )
    baseline_kind = _timetable_baseline_kind(baseline)
    if baseline_kind == "MIXED_REVIEW_REQUIRED":
        return _mixed_timetable_error(
            tool="build_my_timetable",
            academic_year=year,
            term=term,
        )
    held_rows = baseline_sections(baseline)
    held = {row["course_code"] for row in held_rows}

    # A course the student is ALREADY registered in this term never goes to the
    # solver. It cannot be scheduled twice, and sending it produces one of two wrong
    # answers, both seen live on TT10: the solver prunes the student's own section
    # because it collides with the student's own baseline, then either picks a
    # DIFFERENT section of the same course — «تم الاحتفاظ بـ CS323-M1» followed by
    # «CS323: شعبة M2» in one answer — or, when nothing else fits, reports
    # ALL_SECTIONS_CLASH, which reads as "you cannot take AI331" about a course the
    # student is sitting in.
    #
    # `recommend_next_courses` is what makes this the common case rather than an
    # edge: it excludes PASSED and STUDYING courses, and a `StudentTermSection`
    # registration is neither, so the term's own registrations come back as
    # recommendations. They belong in `retained_sections`, and that is where they go.
    codes = [c for c in asked if c not in held]

    def _facts(mappings: list[dict[str, Any]], unscheduled: list[dict[str, Any]]) -> dict[str, Any]:
        facts = build_timetable_facts(
            student_id=int(student_id),
            using_timetable_of_term=f"{year}/{term}",
            requested_codes=wanted,
            recommended_codes=recommended,
            baseline=baseline,
            mappings=mappings,
            unscheduled=unscheduled,
            credit_hours=credits,
            default_credits=DEFAULT_CREDITS,
            cap=cap,
            baseline_kind=baseline_kind,
            # `None`, not `[]`. Chat cannot pin a section — `must_include` names
            # courses — so the key is absent rather than asserting nothing was pinned.
            fixed_sections=None,
        )
        # Checked before the payload is built, so a contradiction costs one refused
        # tool call instead of reaching a student as a sentence. `execute` turns the
        # exception into ok=False and logs it.
        verify(
            facts,
            baseline_codes={(r["course_code"], r.get("section", "")) for r in held_rows},
            keep_current=keep_current_sections,
        )
        return facts.as_payload()

    if not asked:
        return {
            "ok": True,
            **_facts([], []),
            "note": (
                "There is nothing additional to schedule: the recommender returned no "
                "courses and none were named. retained_sections are classified by "
                "baseline_kind; EXPECTED_PLAN rows are planning evidence, not registration."
            ),
            "tool": "build_my_timetable",
        }

    result = run_solver(
        year=str(year),
        term=str(term),
        shortlist=[{"course_code": c, "credits": credits.get(c, DEFAULT_CREDITS)} for c in codes],
        baseline=baseline,
        keep_current_sections=keep_current_sections,
        max_credits=cap,
        gender=gender,
        program=program,
    )

    options = result.get("options") or []
    if not options:
        raw_unplaced = result.get("unscheduled") or []
        translated_unplaced: list[dict[str, Any]] = []
        for entry in raw_unplaced:
            reason_code, explanation = _translate_unplaced(entry.get("reason"))
            translated_unplaced.append(
                {
                    "course_code": entry.get("course_code"),
                    "reason_code": reason_code,
                    "reason": explanation,
                }
            )
        if not translated_unplaced:
            translated_unplaced = [
                {
                    "course_code": course_code,
                    "reason_code": None,
                    "reason": "No valid timetable satisfies the current constraints.",
                }
                for course_code in codes
            ]
        reason_codes = ", ".join(
            sorted(
                {
                    str(entry.get("reason_code") or "NO_VALID_TIMETABLE")
                    for entry in translated_unplaced
                }
            )
        )
        return {
            "ok": True,
            **_facts([], translated_unplaced),
            "alternatives_considered": 0,
            "note": (
                "No timetable could be built from the sections on file. Unplaced reason "
                f"codes: {reason_codes}. NOT_ON_FILE means only that this system has no "
                "section record for the course, never that the university does not offer it. "
                "Any section under retained_sections remains part of the stored baseline. "
                "Read baseline_kind: EXPECTED_PLAN means planning evidence and must not be "
                "called registered."
            ),
            "tool": "build_my_timetable",
        }

    best = max(options, key=lambda o: int(o.get("scheduled") or 0))
    unscheduled = []
    for u in best.get("unscheduled") or []:
        code, explanation = _translate_unplaced(u.get("reason"))
        unscheduled.append(
            {"course_code": u.get("course_code"), "reason_code": code, "reason": explanation}
        )

    return {
        "ok": True,
        **_facts(best.get("mappings") or [], unscheduled),
        "alternatives_considered": len(options),
        "note": (
            "A SUGGESTION built from the sections on file, not a registration and not "
            "an offer of a seat - there are no seat counts in the data, so never say a "
            "section has room. Every course and section carries where it came from: "
            "source STUDENT_REQUEST means the student named it, SYSTEM_RECOMMENDATION "
            "means the recommender chose it, CURRENT_REGISTRATION is registrar evidence, "
            "and EXPECTED_PLAN is planning evidence only. baseline_kind decides which one "
            "retained_sections carry. change RETAIN means it was kept untouched, ADD "
            "means it is newly scheduled. Report retained_sections as kept and "
            "new_sections as proposed; never present a retained section as new. A "
            "partial result is normal: a course appears under unplaced_courses with "
            "reason_code NOT_ON_FILE when no section of it is on file (say exactly "
            "that, never 'not available'), with ALL_SECTIONS_CLASH when every section "
            "collides with something already in the week, and with outcome "
            "ALREADY_REGISTERED for registrar evidence or ALREADY_IN_EXPECTED_PLAN for "
            "planning evidence - neither outcome is a scheduling failure. "
            "credit_summary splits the hours already held from the hours this build "
            "adds. Sections carry no term of their own; the term shown is the one the "
            "stored baseline belongs to. The student still registers "
            "through the university portal."
        ),
        "tool": "build_my_timetable",
    }


def _exec_build_timetable_proposal(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Build several clash-checked proposals without changing or saving anything.

    Unlike the legacy ``build_my_timetable`` capability, ``from_scratch`` is safe
    here: it changes only the solver's occupancy rule for this response.  It never
    edits the student's current section mappings and never creates a planner draft.
    """
    from core.models import ProgrammeRequirement, Student
    from core.services.planner_drafts import credit_ceiling
    from core.services.recommender import recommend_next_courses
    from core.services.student_planner import (
        DEFAULT_CREDITS,
        PlannerRequest,
        SectionLabelPin,
        SectionPinResolutionError,
        build_student_options,
        permitted_course_codes,
        resolve_section_label_pins,
        validate_draft_selection,
    )
    from core.services.student_sections import get_student_term_baseline
    from core.services.timetable_provenance import baseline_sections
    from core.services.timetable_snapshots import Snapshot
    from core.services.virtual_advisor import _course_names

    student_id, error = _resolve_scoped_student_id(args, scope)
    if error:
        return {"ok": False, "error": error, "tool": "build_timetable_proposal"}
    year, term, error = _ctx_year_term(args, ctx)
    if error:
        return {"ok": False, "error": error, "tool": "build_timetable_proposal"}

    mode = str(args.get("mode") or "around_current").strip().lower()
    if mode not in {"around_current", "from_scratch"}:
        return {
            "ok": False,
            "error": "mode must be around_current or from_scratch.",
            "tool": "build_timetable_proposal",
        }

    raw_codes = args.get("course_codes") or []
    if isinstance(raw_codes, str):
        raw_codes = [raw_codes]
    if not isinstance(raw_codes, list):
        return {
            "ok": False,
            "error": "course_codes must be a list.",
            "tool": "build_timetable_proposal",
        }

    raw_required = args.get("must_take_courses") or []
    if not isinstance(raw_required, list):
        return {
            "ok": False,
            "error": "must_take_courses must be a list.",
            "must_take_courses": [],
            "pinned_sections": [],
            "constraints_satisfied": False,
            "constraint_failures": [
                {
                    "course_code": "",
                    "section_label": "",
                    "reason": "must_take_courses must be a list of course codes.",
                }
            ],
            "tool": "build_timetable_proposal",
        }
    required: list[str] = []
    for raw_code in raw_required:
        code = normalize_code(raw_code)
        if not code:
            return {
                "ok": False,
                "error": "Every must-take course must have a course code.",
                "must_take_courses": required,
                "pinned_sections": [],
                "constraints_satisfied": False,
                "constraint_failures": [
                    {
                        "course_code": "",
                        "section_label": "",
                        "reason": "Every must-take course must have a course code.",
                    }
                ],
                "tool": "build_timetable_proposal",
            }
        if code not in required:
            required.append(code)

    raw_pins = args.get("pinned_sections") or []
    if not isinstance(raw_pins, list):
        return {
            "ok": False,
            "error": "pinned_sections must be a list.",
            "must_take_courses": required,
            "pinned_sections": [],
            "constraints_satisfied": False,
            "constraint_failures": [
                {
                    "course_code": "",
                    "section_label": "",
                    "reason": (
                        "pinned_sections must be a list of course-code and section-label pairs."
                    ),
                }
            ],
            "tool": "build_timetable_proposal",
        }
    requested_pins: list[SectionLabelPin] = []
    public_pins: list[dict[str, str]] = []
    pin_by_code: dict[str, str] = {}
    for raw_pin in raw_pins:
        if not isinstance(raw_pin, dict) or set(raw_pin) - {"course_code", "section_label"}:
            failure = {
                "course_code": "",
                "section_label": "",
                "reason": ("Each pinned section must contain only course_code and section_label."),
            }
            return {
                "ok": False,
                "error": failure["reason"],
                "must_take_courses": required,
                "pinned_sections": public_pins,
                "constraints_satisfied": False,
                "constraint_failures": [failure],
                "tool": "build_timetable_proposal",
            }
        code = normalize_code(raw_pin.get("course_code") or "")
        label = str(raw_pin.get("section_label") or "").strip().upper()
        if not code or not label:
            failure = {
                "course_code": code,
                "section_label": label,
                "reason": "Each pinned section requires a course code and a section label.",
            }
            return {
                "ok": False,
                "error": failure["reason"],
                "must_take_courses": required,
                "pinned_sections": public_pins,
                "constraints_satisfied": False,
                "constraint_failures": [failure],
                "tool": "build_timetable_proposal",
            }
        previous = pin_by_code.get(code)
        if previous is not None:
            if previous == label:
                continue
            failure = {
                "course_code": code,
                "section_label": label,
                "reason": f"More than one exact section was pinned for {code}; choose one.",
            }
            return {
                "ok": False,
                "error": failure["reason"],
                "must_take_courses": required,
                "pinned_sections": public_pins,
                "constraints_satisfied": False,
                "constraint_failures": [failure],
                "tool": "build_timetable_proposal",
            }
        pin_by_code[code] = label
        requested_pins.append(SectionLabelPin(course_code=code, section_label=label))
        public_pins.append({"course_code": code, "section_label": label})

    # Pins constrain a candidate but do not make it required.  Add each pinned
    # course to the candidate set so the pin is meaningful; only
    # must_take_courses receives the hard must-take flag.
    explicit_codes = [*raw_codes, *required, *pin_by_code]
    try:
        requested, _ = validate_draft_selection(int(student_id), explicit_codes, {})
    except ValueError as exc:
        failure = {
            "course_code": "",
            "section_label": "",
            "reason": str(exc),
        }
        return {
            "ok": False,
            "error": str(exc),
            "must_take_courses": required,
            "pinned_sections": public_pins,
            "constraints_satisfied": False,
            "constraint_failures": [failure],
            "tool": "build_timetable_proposal",
        }

    try:
        resolved_pins = resolve_section_label_pins(
            int(student_id),
            requested,
            tuple(requested_pins),
        )
    except SectionPinResolutionError as exc:
        return {
            "ok": False,
            "error": exc.reason,
            "must_take_courses": required,
            "pinned_sections": public_pins,
            "constraints_satisfied": False,
            "constraint_failures": [exc.as_failure()],
            "tool": "build_timetable_proposal",
        }
    # Echo the canonical labels actually resolved from the current global
    # snapshot.  Internal ids remain below this boundary.
    public_pins = [
        {"course_code": pin.course_code, "section_label": pin.section_label}
        for pin in resolved_pins
    ]

    program = str(
        Student.objects.filter(student_id=student_id).values_list("program", flat=True).first()
        or ""
    ).strip()
    permitted = permitted_course_codes(program)
    recommended = [
        normalize_code(code)
        for code in (recommend_next_courses(int(student_id), int(year), int(term)) or [])
    ]
    recommended = [code for code in recommended if code and code in permitted]

    baseline = get_student_term_baseline(
        int(student_id), str(year), str(term), snapshot=Snapshot.EFFECTIVE
    )
    baseline_kind = _timetable_baseline_kind(baseline)
    if baseline_kind == "MIXED_REVIEW_REQUIRED":
        return _mixed_timetable_error(
            tool="build_timetable_proposal",
            academic_year=year,
            term=term,
        )
    baseline_section_rows = baseline_sections(baseline)
    held_codes = [normalize_code(row.get("course_code") or "") for row in baseline_section_rows]
    resolved_pin_by_code = {pin.course_code: pin for pin in resolved_pins}
    if mode == "around_current":
        held_labels_by_code: dict[str, set[str]] = {}
        for row in baseline_section_rows:
            code = normalize_code(row.get("course_code") or "")
            if not code:
                continue
            held_labels_by_code.setdefault(code, set()).add(
                str(row.get("section") or "").strip().upper()
            )
        for code, pin in resolved_pin_by_code.items():
            held_labels = held_labels_by_code.get(code)
            if held_labels and held_labels != {pin.section_label}:
                failure = {
                    "course_code": code,
                    "section_label": pin.section_label,
                    "reason": (
                        f"{code} is retained in section "
                        f"{', '.join(sorted(label for label in held_labels if label)) or '(blank)'}; "
                        f"section {pin.section_label} cannot be pinned in around-current mode. "
                        "Use a from-scratch proposal to compare another section. Nothing was changed."
                    ),
                }
                return {
                    "ok": False,
                    "error": failure["reason"],
                    "tool": "build_timetable_proposal",
                    "planning_term": f"{year}/{term}",
                    "mode": mode,
                    "baseline_kind": baseline_kind,
                    "must_take_courses": required,
                    "pinned_sections": public_pins,
                    "constraints_satisfied": False,
                    "constraint_failures": [failure],
                }
    if mode == "around_current":
        planned_codes = [
            code
            for code in dict.fromkeys(requested + recommended)
            if code and code not in held_codes
        ]
    else:
        # A fresh arrangement keeps the current COURSES as candidates but is free
        # to choose different sections for them. Nothing in the database changes.
        planned_codes = [
            code for code in dict.fromkeys(held_codes + requested + recommended) if code
        ]

    policy_cap = credit_ceiling(int(term))
    raw_cap = args.get("max_credits")
    try:
        requested_cap = int(raw_cap) if raw_cap not in (None, "") else policy_cap
    except (TypeError, ValueError):
        return {
            "ok": False,
            "error": "max_credits must be an integer.",
            "tool": "build_timetable_proposal",
        }
    if requested_cap <= 0:
        return {
            "ok": False,
            "error": "max_credits must be greater than zero.",
            "tool": "build_timetable_proposal",
        }
    cap = min(requested_cap, policy_cap)

    result = build_student_options(
        PlannerRequest(
            student_id=int(student_id),
            year=int(year),
            term=int(term),
            must_include=tuple(planned_codes),
            required_courses=tuple(required),
            keep_current_sections=mode == "around_current",
            max_credits=cap,
            include_recommendations=False,
            fixed_sections=tuple((pin.course_code, pin.term_section_id) for pin in resolved_pins),
        )
    )

    required_set = set(required)
    raw_result_alternatives = list(result.get("alternatives") or [])

    def _alternative_honours_constraints(alternative: dict[str, Any]) -> bool:
        sections_by_code: dict[str, set[str]] = {}
        for row in alternative.get("courses") or []:
            code = normalize_code(row.get("course_code") or "")
            if not code:
                continue
            sections_by_code.setdefault(code, set()).add(
                str(row.get("section") or "").strip().upper()
            )
        if not required_set.issubset(sections_by_code):
            return False
        for code, label in pin_by_code.items():
            selected_labels = sections_by_code.get(code)
            # Pin-only courses remain optional, but any selected instance must be
            # the exact requested section.
            if selected_labels is not None and selected_labels != {label}:
                return False
        return True

    valid_result_alternatives = [
        alternative
        for alternative in raw_result_alternatives
        if _alternative_honours_constraints(alternative)
    ]

    constraint_failures: list[dict[str, str]] = []

    def _append_constraint_failure(code: str, reason: str) -> None:
        normalized = normalize_code(code)
        failure = {
            "course_code": normalized,
            "section_label": pin_by_code.get(normalized, ""),
            "reason": str(reason or "A required timetable constraint could not be satisfied."),
        }
        identity = (failure["course_code"], failure["section_label"])
        if any(
            (row["course_code"], row["section_label"]) == identity for row in constraint_failures
        ):
            return
        constraint_failures.append(failure)

    if required_set and not valid_result_alternatives:
        unplaced_by_code = {
            normalize_code(row.get("course_code") or ""): str(row.get("reason") or "")
            for row in (result.get("unplaced") or [])
            if normalize_code(row.get("course_code") or "")
        }
        for row in result.get("constraint_failures") or []:
            code = normalize_code(row.get("course_code") or "")
            if code in required_set:
                _append_constraint_failure(code, str(row.get("reason") or ""))
        for code in sorted(required_set):
            _append_constraint_failure(
                code,
                unplaced_by_code.get(code)
                or "No valid timetable satisfies this required course under the current constraints.",
            )

    constraints_satisfied = not constraint_failures
    no_additional_courses = mode == "around_current" and not planned_codes
    if no_additional_courses:
        # The shared planner deliberately treats a retained baseline as one safe
        # fallback alternative.  In chat, however, that baseline already has its
        # own section and there is no target coverage to compare.  Showing it a
        # second time creates a fake 5/5 "proposal" and used to double both the
        # course and credit totals.
        valid_result_alternatives = []

    all_codes = set(held_codes) | set(planned_codes)
    for alternative in valid_result_alternatives:
        all_codes.update(
            normalize_code(row.get("course_code") or "") for row in alternative.get("courses") or []
        )
    names = _course_names({code for code in all_codes if code})
    credits = {
        normalize_code(row["course_code"]): int(row["credit_hours"] or 0)
        for row in ProgrammeRequirement.objects.filter(program__iexact=program).values(
            "course_code", "credit_hours"
        )
    }

    baseline_safe = [
        {
            "course_code": row.get("course_code", ""),
            "course_name": row.get("course_name") or names.get(row.get("course_code", ""), ""),
            "section": row.get("section", ""),
            "credits": credits.get(normalize_code(row.get("course_code") or ""), DEFAULT_CREDITS),
            "meetings": list(row.get("meetings") or []),
        }
        for row in baseline_section_rows
    ]
    baseline_credits = sum(int(row.get("credits") or 0) for row in baseline_safe)

    alternatives = []
    for index, alternative in enumerate(valid_result_alternatives, start=1):
        raw_course_rows = [
            row for row in (alternative.get("courses") or []) if isinstance(row, dict)
        ]
        has_source_metadata = any("source" in row for row in raw_course_rows)
        visible_course_rows = (
            [row for row in raw_course_rows if row.get("source") != "current"]
            if mode == "around_current" and has_source_metadata
            else raw_course_rows
        )
        courses = [
            {
                "course_code": str(row.get("course_code") or ""),
                "course_name": names.get(str(row.get("course_code") or ""), ""),
                "section": str(row.get("section") or ""),
                "credits": int(row.get("credits") or DEFAULT_CREDITS),
            }
            for row in visible_course_rows
        ]
        raw_meeting_rows = [
            row for row in (alternative.get("meetings") or []) if isinstance(row, dict)
        ]
        visible_meeting_rows = (
            [row for row in raw_meeting_rows if row.get("source") != "current"]
            if mode == "around_current" and has_source_metadata
            else raw_meeting_rows
        )
        meetings = [
            {
                "course_code": str(row.get("course_code") or ""),
                "course_name": names.get(str(row.get("course_code") or ""), ""),
                "section": str(row.get("section") or ""),
                "day": str(row.get("day") or ""),
                "start": str(row.get("start") or ""),
                "end": str(row.get("end") or ""),
            }
            for row in visible_meeting_rows
        ]
        raw_credit_hours = int(alternative.get("credit_hours") or 0)
        if mode == "around_current" and has_source_metadata:
            proposed_credits = max(0, raw_credit_hours - baseline_credits)
            total_credit_hours = raw_credit_hours
            held_course_count = len({code for code in held_codes if code})
            scheduled_courses = max(
                0, int(alternative.get("scheduled_courses") or 0) - held_course_count
            )
            target_courses = max(0, int(alternative.get("target_courses") or 0) - held_course_count)
            course_count = int(alternative.get("course_count") or 0)
        else:
            proposed_credits = raw_credit_hours
            total_credit_hours = proposed_credits + (
                baseline_credits if mode == "around_current" else 0
            )
            scheduled_courses = int(
                alternative.get("scheduled_courses")
                if alternative.get("scheduled_courses") is not None
                else len(courses)
            )
            target_courses = int(
                alternative.get("target_courses")
                if alternative.get("target_courses") is not None
                else len(planned_codes)
            )
            course_count = len(courses) + (len(baseline_safe) if mode == "around_current" else 0)
        planner_options = [
            str(name).strip().upper()
            for name in alternative.get("planner_options") or []
            if str(name).strip()
        ]
        alternative_unplaced = [
            {
                "course_code": str(row.get("course_code") or ""),
                "course_name": names.get(str(row.get("course_code") or ""), ""),
                "reason_code": str(row.get("reason_code") or ""),
                "reason": str(row.get("reason") or ""),
            }
            for row in alternative.get("unplaced") or []
        ]
        alternatives.append(
            {
                "option": index,
                "planner_options": planner_options,
                "courses": courses,
                "meetings": meetings,
                "scheduled_courses": scheduled_courses,
                "target_courses": target_courses,
                "unplaced_courses": alternative_unplaced,
                "course_count": course_count,
                "proposed_credit_hours": proposed_credits,
                "total_credit_hours": total_credit_hours,
                "days_on_campus": int(alternative.get("days_on_campus") or 0),
                "days": list(alternative.get("days") or []),
                "earliest_start": alternative.get("earliest_start"),
                "latest_end": alternative.get("latest_end"),
            }
        )

    unplaced = [
        {
            "course_code": str(row.get("course_code") or ""),
            "course_name": names.get(str(row.get("course_code") or ""), ""),
            "reason_code": str(row.get("reason_code") or ""),
            "reason": str(row.get("reason") or ""),
        }
        for row in result.get("unplaced") or []
    ]
    return {
        "ok": True,
        "tool": "build_timetable_proposal",
        "planning_term": f"{year}/{term}",
        "mode": mode,
        "baseline_kind": baseline_kind,
        "student_requested_courses": requested,
        "system_recommended_courses": recommended,
        "must_take_courses": required,
        "pinned_sections": public_pins,
        "constraints_satisfied": constraints_satisfied,
        "constraint_failures": constraint_failures,
        "baseline_sections": baseline_safe,
        "baseline_credit_hours": baseline_credits,
        # Compatibility fields remain truthful.  They are populated only when
        # provenance establishes registrar evidence; expected-plan data has its
        # own fields and must never arrive under a name containing "current".
        "current_sections": baseline_safe if baseline_kind == "REGISTERED" else [],
        "current_credit_hours": baseline_credits if baseline_kind == "REGISTERED" else 0,
        "expected_plan_sections": (baseline_safe if baseline_kind == "EXPECTED_PLAN" else []),
        "expected_plan_credit_hours": (baseline_credits if baseline_kind == "EXPECTED_PLAN" else 0),
        "credit_ceiling": cap,
        "alternatives": alternatives,
        "unplaced_courses": unplaced,
        # With no target courses the solver did not fail to fill the remaining
        # credit ceiling: there was simply nothing to schedule. Keep that state
        # explicit so neither prose nor UI invents an "18 of 19" coverage claim.
        "no_additional_courses": no_additional_courses,
        # The Planner always attempts A1-A3, B1-B3 and C1-C3. Identical section
        # sets are collapsed for chat readability, while their exact Planner
        # names remain on each distinct alternative above.
        "alternatives_generated": (
            0 if no_additional_courses else int(result.get("generated") or len(alternatives))
        ),
        "distinct_alternatives": len(alternatives),
        "registration_action": "STUDENT_MANUAL_PORTAL_ONLY",
        "can_save": False,
        "can_register": False,
        "note": (
            "These are clash-checked proposals from the sections on file, not live seat "
            "availability and not registration. baseline_kind identifies whether the stored "
            "baseline is REGISTERED or EXPECTED_PLAN; expected rows must never be called "
            "registered/current. around_current keeps baseline_sections fixed and alternatives "
            "list only the proposed additions. from_scratch "
            "rebuilds the whole candidate course set. must_take_courses and pinned_sections "
            "are the exact hard constraints used for this build. If constraints_satisfied is "
            "false, explain constraint_failures and do not present a partial timetable as "
            "valid. planner_options are the exact A1-C3 "
            "identities emitted by the Planner; multiple names on one alternative mean those "
            "generator runs produced the same timetable. Name each distinct alternative in "
            "prose with those identities and coverage. The interface renders every actual "
            "section, day and time in a structured card, so do not duplicate all rows in prose. "
            "Use each option's "
            "own unplaced_courses. OMITTED_IN_THIS_VARIANT means another Planner option did "
            "place that course, so tell the student to compare options; it does not mean the "
            "course or its sections are absent, and it does not prove that no complete "
            "clash-free arrangement exists. The finite A1-C3 output is not an exhaustive "
            "search of every possible section combination. Use the natural-language reason in the answer "
            "and do not print internal reason_code labels. State the Planner identities "
            "and scheduled/target coverage for every returned alternative, including one that "
            "placed zero of its target additions. If no_additional_courses is true, the Planner "
            "had no target course to schedule: say the stored baseline is retained with no "
            "proposed additions, using baseline_kind to call it registered or expected. Do "
            "not invent Planner identities or describe baseline credits versus the credit "
            "ceiling as coverage. Do not call an omission a clash unless its returned reason says "
            "that; NOT_ON_FILE means only that no section is recorded here. Never say "
            "timetable access is unavailable."
        ),
    }


def _exec_policy_lookup(
    args: dict[str, Any], scope: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """The university's written rules, with the provenance needed to cite them.

    This is the only route from the adviser to ``policies/``. Everything it returns
    is ``AUTHORITY_APPROVED``; records at an earlier verification stage are invisible
    rather than merely ranked lower.

    Two things in the payload are load-bearing and easy to skim past:

    ``decision_use`` — the record's own statement of whether it may be applied to a
    student. 20 of the 81 records are ``PROHIBITED_FOR_DECISION`` because the inputs
    their conditions need do not exist in the schema (no warning-count feed, no
    absence register). For those, explaining the rule IS the complete answer, and
    ruling on the student's case is the failure mode.

    ``citable`` — the exact citations permitted for this request. A citation naming
    any other policy is rejected downstream by ``validate_citations``, which is what
    stops a model reciting a policy id from memory and having it read as grounded.

    No student data is touched, so no scope resolution is needed: the rules are the
    same for every student. The capability is still student-reachable only through
    the registry's role check, like every other.
    """
    from core.services.policy_store import get_policy_store

    topic = str(args.get("topic") or "").strip() or None
    query = str(args.get("query") or "").strip() or None
    policy_ids = args.get("policy_ids") or None
    if isinstance(policy_ids, str):
        policy_ids = [policy_ids]

    if not (topic or query or policy_ids):
        return {
            "ok": False,
            "error": "Pass query (the student's question in Arabic), topic, or policy_ids.",
        }

    store = get_policy_store()
    try:
        limit = int(args.get("limit") or 8)
    except (TypeError, ValueError):
        return {"ok": False, "error": "limit must be an integer."}

    # Operator annotations name database tables, quote row counts and quote counts
    # of students by status. They exist to tell an engineer why a rule cannot be
    # applied; a student asking about GPA must not be handed the schema.
    result = store.lookup(
        topic=topic,
        query=query,
        policy_ids=policy_ids,
        limit=limit,
        include_operator_notes=_scope_role(scope) in _STAFF_ROLES,
    )
    if not result.get("ok"):
        return result

    # Sort what came back by whether it GOVERNS the question. Retrieval is broad on
    # purpose; without this the model receives a related record and a governing one
    # in the same undifferentiated list, which is how a programme-duration rule came
    # to supply a course-repetition percentage.
    from core.services.policy_applicability import classify

    roles = classify(
        result["policies"],
        question=query or topic or "",
        topics=result.get("matched_topics") or [],
        store=store,
    )
    result["question_concepts"] = roles["question_concepts"]
    result["direct_policy_evidence"] = roles["direct_policy_evidence"]
    result["background_policy_evidence"] = roles["background_policy_evidence"]
    result["conflicting_policy_evidence"] = roles["conflicting_policy_evidence"]
    result["irrelevant_policy_evidence"] = roles["irrelevant_policy_evidence"]

    # Citations are offered ONLY for direct evidence. A background record displayed
    # beside the answer reads as authority for the question whatever the prose says.
    direct_ids = {p["policy_id"] for p in roles["direct_policy_evidence"]}
    result["citable"] = [c for c in result["citable"] if c["policy_id"] in direct_ids]

    if not result["policies"]:
        result["note"] = (
            "No approved policy matches. Do NOT answer the rule from general "
            "knowledge - say the system holds no written rule on this and point the "
            "student to the Deanship of Admission and Registration. Retrying with a "
            "topic from available_topics is worth one attempt."
        )
    elif not roles["direct_policy_evidence"]:
        result["note"] = (
            "NOTHING RETRIEVED GOVERNS THIS QUESTION. Records came back, but none of "
            "them is about what was asked — they are in background_policy_evidence "
            "and irrelevant_policy_evidence. You may say that related material exists "
            "and does not answer the question. You may NOT take a number, a "
            "percentage, a deadline, a definition, a procedure, an appeal route, a "
            "responsible authority, or any statement of what is allowed or required "
            "from them, and you may not cite them. Answer any student-data part of "
            "the question normally, then say the guide does not state the rule and "
            "refer the student to عمادة القبول والتسجيل."
        )
    else:
        result["note"] = (
            "Use direct_policy_evidence for anything the university REQUIRES, "
            "PERMITS, FORBIDS, DEFINES, or tells the student WHERE TO GO. Those "
            "records govern this question; nothing else does. "
            "background_policy_evidence is related material that does NOT answer it "
            "— you may note that it exists, and you may not draw a limit, deadline, "
            "definition, procedure or eligibility from it. "
            "Cite ONLY the entries in `citable`, using them exactly as "
            "given - policy_id, document, edition and page. Never cite a policy that "
            "is not in this list, and never state a page number that is not in its "
            "citation. Where decision_use is PROHIBITED_FOR_DECISION, explain what "
            "the rule says and say plainly that the system cannot check the "
            "student's own case against it; do not rule on their situation. Where a "
            "policy carries `conflicts`, the resolution names which source governs - "
            "follow it and say which document you are quoting, never present the two "
            "as equally valid."
        )
    result["tool"] = "policy_lookup"
    return result


def build_default_registry() -> AdvisorCapabilityRegistry:
    registry = AdvisorCapabilityRegistry()

    registry.register(
        AdvisorCapability(
            name="find_students",
            description=(
                "Find students in verified university records using filters: name "
                "fragment, earned credits, GPA range, program, gender section "
                "(M/F), advisor, and course status (passed / studying / failed / missing). "
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
                "earned credits, passed/studying/failed courses, recorded failed-result "
                "grades or marks when present, current-term "
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
            name="course_choice_comparison",
            description=(
                "Compare two to four exact course choices for this student from one "
                "verified baseline. It keeps prerequisite readiness, recommendation "
                "membership, direct personal unlocks, wider prerequisite-chain impact, "
                "the project's discounted downstream-importance heuristic, recorded "
                "clash-free section fit, and a fair graduation scenario separate. Use "
                "for 'AI331 or DS341?', 'which opens more?', 'which fits my timetable?', "
                "or a ranked comparison. A weighted importance score is a planning "
                "heuristic, never university policy. No section record does not mean the "
                "university offers none, and no result proves live seats, registration "
                "permission, course equivalence, or a portal action. Exact graduation "
                "claims are returned only when the structured scenarios complete."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "student_id": {
                        "type": "integer",
                        "description": "Omit for the chatting student.",
                    },
                    "course_codes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 4,
                        "description": "Two to four distinct exact course codes.",
                    },
                    "academic_year": {"type": "integer"},
                    "term": {"type": "integer"},
                    "objective": {
                        "type": "string",
                        "enum": [
                            "balanced",
                            "graduation",
                            "unlock_impact",
                            "timetable_fit",
                        ],
                        "description": "The student's stated priority; balanced if unstated.",
                    },
                },
                "required": ["course_codes"],
                "additionalProperties": False,
            },
            allowed_roles=_ALL_ROLES,
            executor=_exec_course_choice_comparison,
        )
    )

    registry.register(
        AdvisorCapability(
            name="feasible_course_replacements",
            description=(
                "Find one-for-one replacements that pass both independent gates: the "
                "existing graduation forecast proves an academic improvement, and the "
                "existing Planner places every retained baseline course plus the replacement "
                "in a complete clash-free timetable. Use for 'what can I replace without a "
                "clash?', 'replace DS341 with the best feasible course', or 'will replacing "
                "DS341 with CS285 improve graduation and fit my timetable?'. Optional "
                "remove_course and add_course bind either side of the search. The baseline may "
                "be REGISTERED or EXPECTED_PLAN and must be described accordingly. Results use "
                "only the recorded, termless section snapshot, deliberately ignore capacity, "
                "and never prove live seats, current offering, registration permission, "
                "equivalence, or a portal action. This capability is read-only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "student_id": {
                        "type": "integer",
                        "description": "Omit for the chatting student.",
                    },
                    "remove_course": {
                        "type": "string",
                        "description": "Optional exact baseline course code to replace.",
                    },
                    "add_course": {
                        "type": "string",
                        "description": "Optional exact replacement course code to test.",
                    },
                    "academic_year": {"type": "integer"},
                    "term": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            allowed_roles=_ALL_ROLES,
            executor=_exec_feasible_course_replacements,
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
                "The student's full standing in their degree plan, and the ONLY tool that "
                "ranks courses by unlock impact. Returns: how many courses have every "
                "recorded prerequisite satisfied (prerequisites_satisfied) and how many do "
                "not (prerequisite_blocked); most_useful_course_to_pass; and "
                "unlock_impact_ranking - every course they could pass now, ordered, each "
                "with sole_remaining_prerequisite_count (courses waiting on it alone) and "
                "on_prerequisite_chain_of_count (courses with it anywhere in their chain). "
                "For every blocked course: why, how many passes away, and the nearest "
                "course on that chain they can take today. Use for 'what can I take', "
                "'what is blocking me', 'what should I do next', 'which course is most "
                "important / highest priority', 'which course opens the most', and to rank "
                "or compare several courses by impact. Broader than recommend_courses, "
                "which returns only the credit-capped suggestion for the coming term. "
                "Prerequisite state only: it never establishes that a section is offered, "
                "that registration is permitted, or that a seat is available."
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
                "ONE named course, in BOTH directions. Backward: whether the student has "
                "passed it, is studying it, has satisfied every recorded prerequisite "
                "(PREREQUISITES_SATISFIED) or has not (PREREQUISITE_BLOCKED) - and if "
                "blocked, exactly which prerequisite courses are missing or how many credit "
                "hours are short, how many passes away it is, and the nearest course on the "
                "chain they can take now. FORWARD - use this tool, not course_prerequisites, "
                "for 'what does AI331 unlock', 'how many courses depend on AI331', 'which "
                "courses are waiting on AI331', 'what opens if I pass it': it returns "
                "listed_as_prerequisite_for / _count (courses that NAME it as a "
                "prerequisite), sole_remaining_prerequisite_for / _count (those for which it "
                "is the LAST unmet condition, so they become prerequisite-satisfied when it "
                "is passed) and on_prerequisite_chain_of_count (courses with it anywhere in "
                "their remaining chain). Those three are usually different numbers. "
                "course_prerequisites answers the REVERSE relation - what this course "
                "itself requires - and cannot answer a forward-unlock question. Use whenever "
                "a student asks about a specific course code. Prerequisite state only: it "
                "never establishes that a section is offered, that registration is "
                "permitted, or that a seat is available."
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
                "GPA, any unmet credit-hour gate, and a read-only term-by-term scenario. The "
                "scenario assumes the selected planning-baseline Planner courses pass, repeatedly calls the existing "
                "recommender one main term ahead, and uses at most 18 credits in every term. "
                "It can compare read-only planning-baseline add/remove scenarios or search for a "
                "one-course replacement that has a proven academic improvement. Use for "
                "'when will I graduate', 'what if I do not take DS341', 'what if I replace "
                "DS341 with MATH204', or 'can I replace a current course to improve graduation'."
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
                    "remove_current_courses": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 10,
                        "description": (
                            "Planning-baseline Planner course codes to remove only in this read-only "
                            "graduation scenario."
                        ),
                    },
                    "add_current_courses": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 10,
                        "description": (
                            "Course codes to add only to the simulated planning-baseline term. "
                            "They are assumed passed after that term, never immediately."
                        ),
                    },
                    "search_better_replacements": {
                        "type": "boolean",
                        "description": (
                            "When true, compare bounded one-for-one replacements of planning-baseline "
                            "courses and return only academically proven improvements. Do not "
                            "combine with explicit add/remove lists."
                        ),
                    },
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
                "The student's stored weekly timetable with explicit schedule_kind: "
                "REGISTERED is registrar evidence; EXPECTED_PLAN is a manually seeded "
                "next-term plan and is not registration. Includes day, start/end time, "
                "course, section, room and instructor."
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
                "marked passed / studying / failed / not taken, and whether prerequisites allow "
                "registering it now. Use for 'show me my plan', 'what is left in level "
                "6', 'how much of the plan have I finished'. Broader than my_progress, "
                "which returns only what is open now. Pass `term` to narrow to one plan "
                "level. prerequisites_satisfied (legacy name: can_register) reflects the "
                "recorded prerequisite conditions ONLY - it is not permission to register "
                "and says nothing about whether a section is being taught."
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
                "For one or more courses, which sections fit the student's stored "
                "timetable baseline and which collide - naming the course, day and "
                "both time ranges of every collision. Use for 'which section of X can I "
                "take', 'does section F11 clash with my schedule', 'all the sections "
                "clash, is that right'. status NOT_ON_FILE means no section is recorded "
                "for that course; say exactly that, never 'no sections available'. There "
                "are no seat counts, so call them recorded sections rather than available "
                "sections, and never claim a section has room. Read baseline_kind first: "
                "REGISTERED marks registrar evidence with is_current_section, while "
                "EXPECTED_PLAN marks planning-only evidence with is_expected_plan_section. "
                "MIXED_REVIEW_REQUIRED is refused rather than combined."
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

    registry.register(
        AdvisorCapability(
            name="build_my_timetable",
            description=(
                "Build a clash-free weekly timetable for the student from the sections "
                "on file. Use for 'build me a schedule', 'I must take X, can it fit', "
                "'give me a plan under 12 hours'. Pass must_include for courses the "
                "student insists on and max_credits for a ceiling. A partial result is "
                "the normal outcome and must be reported as such, never as a failure. "
                "It is a SUGGESTION: it does not register anything and cannot promise "
                "a seat. It keeps the stored baseline sections and fits the new courses "
                "around them. Read baseline_kind before naming that baseline: REGISTERED "
                "is registrar evidence; EXPECTED_PLAN is planning-only evidence and must "
                "never be called registered/current. The rows are listed in "
                "retained_sections, so say what was kept from that list and never from memory. "
                # The retention promise used to stand alone, with nothing in the
                # payload behind it: `placed` holds only what the solver chose, and
                # the baseline reaches the solver as an occupancy mask that never
                # enters the result. The model was asked to assert a retention it had
                # no evidence of, and on TT10 it did — «تم الاحتفاظ بـ CS323-M1» beside
                # «CS323: شعبة M2», in one answer.
                "student_requested_courses is what the student named; "
                "system_recommended_courses is what the recommender chose. They are "
                "separate because 'where did this course come from' is a question the "
                "student actually asks, and one merged list cannot answer it. "
                # The model must CALL for a rebuild request, not answer it. This
                # used to read "not available here at all: tell them to open the
                # planner", and the model obeyed — it never called, so the server
                # never saw the request and the model authored the routing prose
                # itself. Live, that became «لا يمكنني» plus advice to delete real
                # registrations. Rebuilding IS available, through the planner's
                # confirmed workflow; what is unavailable is doing it from chat.
                "If the student asks to DISCARD their current sections and rebuild "
                "the week from scratch, call this with keep_current_sections=false. "
                "Chat always keeps the student's current sections; it cannot confirm "
                "their removal itself. "
                "Do not answer that request yourself and do not tell the student it "
                "is impossible — it is not. The server will route them to the "
                "planner, where the rebuild is confirmed. Saying they confirm it to "
                "you is not confirmation."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "student_id": {
                        "type": "integer",
                        "description": "Omit for the chatting student.",
                    },
                    "must_include": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Course codes the student insists on taking.",
                    },
                    "max_credits": {
                        "type": "integer",
                        "description": "Credit ceiling for the plan. Omit for no cap.",
                    },
                    "keep_current_sections": {
                        "type": "boolean",
                        "description": (
                            "Omit, or true, for the normal case. Pass false ONLY when "
                            "the student asks to discard their current registration and "
                            "rebuild from scratch — the server refuses the rebuild here "
                            "and routes them to the planner. It never changes a "
                            "registration."
                        ),
                    },
                    "academic_year": {"type": "integer"},
                    "term": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            allowed_roles=_ALL_ROLES,
            executor=_exec_build_my_timetable,
        )
    )

    registry.register(
        AdvisorCapability(
            name="build_timetable_proposal",
            description=(
                "Build multiple real, clash-checked timetable proposals from the existing "
                "section catalogue. Use whenever the student asks to build/create a "
                "timetable, build around current sections, rebuild from scratch, or show "
                "alternatives without clashes. mode=around_current keeps baseline_sections "
                "fixed and fits proposed additions around them. baseline_kind distinguishes "
                "REGISTERED from planning-only EXPECTED_PLAN data; a mixed source state is "
                "refused for review. mode=from_scratch may choose different sections for the "
                "baseline course set, but still changes nothing. The result contains neutral "
                "baseline_sections, truthful compatibility fields, and alternatives with actual "
                "course/section/day/start/end values. planner_options preserves the exact "
                "A1-A3, B1-B3 and C1-C3 identities from the Planner; several identities on "
                "one alternative mean those runs found the same schedule. Each alternative "
                "has its own scheduled/target counts and unplaced_courses; show the Planner "
                "identity even when zero additions were placed, and preserve each unplaced "
                "reason rather than calling every omission a clash. Use must_take_courses "
                "for courses required in every result. Use "
                "pinned_sections with course_code + section_label to restrict a course to "
                "one exact recorded section; add the course to must_take_courses too when "
                "that exact section is required in every result. Never answer such a request "
                "by saying section times or clash detection are unavailable: call "
                "this tool. This tool never saves, applies, registers, drops, or reserves."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "student_id": {
                        "type": "integer",
                        "description": "Omit for the chatting student.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["around_current", "from_scratch"],
                        "description": (
                            "around_current for keeping the stored baseline sections fixed; "
                            "from_scratch for a fresh section arrangement."
                        ),
                    },
                    "course_codes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional candidate course codes. The Planner may omit a "
                            "candidate when it does not fit; official recommendations are "
                            "considered as additional candidates."
                        ),
                    },
                    "must_take_courses": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Hard required course codes. Every returned alternative must "
                            "contain each course (or retain it from the around-current "
                            "baseline); otherwise no alternative is returned as valid."
                        ),
                    },
                    "pinned_sections": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "course_code": {"type": "string"},
                                "section_label": {"type": "string"},
                            },
                            "required": ["course_code", "section_label"],
                            "additionalProperties": False,
                        },
                        "description": (
                            "Exact section filters named by course code and visible section "
                            "label, for example AI331/M2. A pin does not by itself make the "
                            "course required; also list it in must_take_courses when every "
                            "alternative must contain it. Never send a database section id."
                        ),
                    },
                    "max_credits": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Optional preferred ceiling. The server never exceeds the "
                            "configured policy ceiling."
                        ),
                    },
                    "academic_year": {"type": "integer"},
                    "term": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            allowed_roles=_ALL_ROLES,
            executor=_exec_build_timetable_proposal,
        )
    )

    registry.register(
        AdvisorCapability(
            name="policy_lookup",
            description=(
                "The university's WRITTEN RULES from the approved policy store, with "
                "the page and edition needed to cite them. Call this for any question "
                "about what is allowed, required, how long, how many, or what happens "
                "if - withdrawal, apology, deferral, absence, deprivation, credit "
                "load, GPA, grades, appeals, transfer, honours, dismissal, "
                "re-enrolment, visiting student, conduct. Pass the student's question "
                "verbatim as `query`. Answer rule questions ONLY from what this "
                "returns; if it returns nothing, say the system holds no written rule "
                "rather than answering from memory. Returns each policy's "
                "decision_use, which says whether the rule can be applied to this "
                "student or only explained."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "The student's question, in their own words. Preferred: "
                            "it matches both the topic index and the rule text."
                        ),
                    },
                    "topic": {
                        "type": "string",
                        "description": (
                            "Exact topic key, when known. See available_topics in a "
                            "previous result. Use query instead if unsure."
                        ),
                    },
                    "policy_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Fetch specific policies by id, e.g. after a cross-reference.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum policies to return (default 8, max 20).",
                    },
                },
                "additionalProperties": False,
            },
            allowed_roles=_ALL_ROLES,
            executor=_exec_policy_lookup,
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

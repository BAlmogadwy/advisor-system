import json
import logging
import re
import time
from typing import Any

from django.conf import settings
from django.db.models import Q, QuerySet

from core.models import Course, Prerequisite, ProgrammeRequirement, Student
from core.services.local_llm import (
    ChatResult,
    LocalLLMBadRequest,
    LocalLLMClient,
    LocalLLMUnavailable,
    ToolChatResult,
)
from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_GENERAL_ADVISOR,
    ROLE_STUDENT,
    ROLE_SUPER_ADMIN,
)
from core.services.recommender import recommend_next_courses
from core.services.student_helpers import get_student_passed_and_studying, normalize_code
from core.services.virtual_advisor_capabilities import get_default_registry

logger = logging.getLogger(__name__)

_MAX_CONTEXT_COURSES = 80
_MAX_HISTORY_MESSAGES = 8
_MAX_TOOL_ROWS = 500
_QWEN_EMPTY_THINK_PREFILL = "<think>\n</think>\n\n"


def is_agent_loop_enabled() -> bool:
    return bool(getattr(settings, "VIRTUAL_ADVISOR_AGENT_LOOP_ENABLED", True))


def _max_tool_iterations() -> int:
    return max(1, int(getattr(settings, "VIRTUAL_ADVISOR_MAX_TOOL_ITERATIONS", 5)))


def _max_tool_calls() -> int:
    return max(1, int(getattr(settings, "VIRTUAL_ADVISOR_MAX_TOOL_CALLS", 12)))


def _loop_max_tokens() -> int:
    return max(256, int(getattr(settings, "VIRTUAL_ADVISOR_LOOP_MAX_TOKENS", 3000)))


def _tool_turn_timeout() -> float:
    return max(10.0, float(getattr(settings, "VIRTUAL_ADVISOR_TOOL_TURN_TIMEOUT_SECONDS", 75)))


SYSTEM_PROMPT = """You are a private local university virtual academic advisor.

Rules:
- Answer in the same language as the user's latest question. When the user message carries an answer_language field, write the final answer in that language.
- Use only the verified_context JSON supplied by the university system.
- When verified_context includes tool_results, treat them as authoritative query results.
- If a requested fact is missing from verified_context, say that the system data does not show it.
- Do not invent grades, rules, prerequisites, graduation status, rooms, sections, or approvals.
- Keep advice practical: what is known, why it matters, and the next safest action.
- Never expose chain-of-thought; provide concise evidence from the context instead.
- Treat recommendations as advising support, not official approval.
- For list questions, summarize the count, filters used, and show the most relevant rows instead of repeating every row.
"""

SYSTEM_PROMPT_AGENT = """You are a private local university virtual academic advisor with verified data tools.

Rules:
- Write the final answer in the language named by the answer_language field of the user message. Never switch languages on your own.
- Your ONLY source of facts is the verified_context JSON and the results of the tools you call. Never answer a data question from memory.
- Call tools to gather evidence BEFORE answering. Chain tools when needed (e.g. lookup_course to resolve a vague course name, then find_students with the exact code).
- If a tool returns an error, adjust the arguments or try another tool; explain the limitation only if no tool can answer.
- When evidence is sufficient, STOP calling tools and give the final answer.
- If the question is ambiguous (which student, which course, which term), ask ONE short clarifying question instead of guessing.
- Academic years are Hijri (e.g. 1448), never Gregorian. Tools default to the configured current year/term — omit academic_year/term arguments unless the user explicitly names a different term.
- Do not invent grades, rules, prerequisites, graduation status, rooms, sections, approvals, or student ids. Every specific fact must appear in the evidence.
- Keep advice practical: what is known, why it matters, and the next safest action.
- Never expose chain-of-thought; cite concise evidence instead.
- Treat recommendations as advising support, not official approval.
- For list questions, summarize the count and filters used, then show the most relevant rows.
"""


ADVISOR_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "find_students",
            "description": (
                "Find students from verified university records using safe filters such as "
                "earned credits, GPA, program, advisor, and course status."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "min_earned_credits": {"type": "integer"},
                    "max_earned_credits": {"type": "integer"},
                    "min_gpa": {"type": "number"},
                    "max_gpa": {"type": "number"},
                    "program": {"type": "string"},
                    "section": {"type": "string"},
                    "sections": {"type": "array", "items": {"type": "string"}},
                    "advisor_id": {"type": "string"},
                    "passed_courses": {"type": "array", "items": {"type": "string"}},
                    "studying_courses": {"type": "array", "items": {"type": "string"}},
                    "course_status_any": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "course_code": {"type": "string"},
                                "statuses": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["course_code", "statuses"],
                        },
                    },
                    "missing_courses": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_TOOL_ROWS},
                },
                "additionalProperties": False,
            },
        },
    }
]


def _course_names(codes: set[str]) -> dict[str, str]:
    if not codes:
        return {}
    names: dict[str, str] = {}
    for course in Course.objects.filter(course_code__in=sorted(codes)).values(
        "course_code", "description"
    ):
        code = normalize_code(course.get("course_code"))
        if code:
            names[code] = str(course.get("description") or "").strip()
    for req in ProgrammeRequirement.objects.filter(course_code__in=sorted(codes)).values(
        "course_code", "course_name"
    ):
        code = normalize_code(req.get("course_code"))
        if code and not names.get(code):
            names[code] = str(req.get("course_name") or "").strip()
    return names


def _compact_course_rows(rows: list[dict[str, Any]], names: dict[str, str]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for row in rows[:_MAX_CONTEXT_COURSES]:
        code = normalize_code(row.get("course_code"))
        if not code:
            continue
        compact.append(
            {
                "course_code": code,
                "course_name": names.get(code) or str(row.get("course_name") or ""),
                "type": str(row.get("type") or ""),
                "programme_term": row.get("programme_term"),
                "credit_hours": row.get("credit_hours"),
            }
        )
    return compact


def _coerce_int(
    value: Any, *, minimum: int | None = None, maximum: int | None = None
) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _coerce_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_course_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    raw_items = value if isinstance(value, list) else re.split(r"[,;\s]+", str(value))
    codes: list[str] = []
    for item in raw_items:
        code = normalize_code(item)
        if code and code not in codes:
            codes.append(code)
    return codes[:20]


def _clean_sections(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    raw_items = value if isinstance(value, list) else re.split(r"[,;/\s]+", str(value))
    sections: list[str] = []
    for item in raw_items:
        section = str(item or "").strip().upper()
        if section in {"M", "F"} and section not in sections:
            sections.append(section)
    return sections


def _clean_status_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    raw_items = value if isinstance(value, list) else re.split(r"[,;/\s]+", str(value))
    statuses: list[str] = []
    allowed = {"passed", "studying", "not_taken"}
    for item in raw_items:
        status = str(item or "").strip().lower().replace("-", "_")
        if status in allowed and status not in statuses:
            statuses.append(status)
    return statuses


def _clean_course_status_any(value: Any) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    items = value if isinstance(value, list) else [value]
    cleaned: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for item in items:
        if isinstance(item, dict):
            code = normalize_code(item.get("course_code") or item.get("course"))
            statuses = _clean_status_list(item.get("statuses") or item.get("status"))
        else:
            parts = str(item).split(":", 1)
            code = normalize_code(parts[0])
            statuses = _clean_status_list(parts[1] if len(parts) > 1 else "")
        if not code or not statuses:
            continue
        key = (code, tuple(statuses))
        if key in seen:
            continue
        cleaned.append({"course_code": code, "statuses": statuses})
        seen.add(key)
    return cleaned[:20]


def _apply_student_scope(
    qs: QuerySet[Student], scope: dict[str, Any] | None
) -> tuple[QuerySet[Student], dict[str, Any]]:
    scope = scope or {}
    role = str(scope.get("role") or ROLE_SUPER_ADMIN)
    applied: dict[str, Any] = {"role": role}
    if role == ROLE_STUDENT:
        student_id = _coerce_int(scope.get("student_id"), minimum=1)
        applied["student_id"] = student_id
        if not student_id:
            return qs.none(), applied
        return qs.filter(student_id=student_id), applied
    if role == ROLE_ADVISOR:
        advisor_id = str(scope.get("advisor_id") or "").strip()
        applied["advisor_id"] = advisor_id
        if not advisor_id:
            return qs.none(), applied
        return qs.filter(advisor_id=advisor_id), applied
    if role == ROLE_GENERAL_ADVISOR:
        departments = [
            str(item).strip().upper() for item in scope.get("departments", []) if str(item).strip()
        ]
        applied["departments"] = departments
        if not departments:
            return qs.none(), applied
        return qs.filter(program__in=departments), applied
    applied["scope"] = "all_students"
    return qs, applied


def _students_with_course_status(course_code: str, statuses: list[str]) -> QuerySet[Student]:
    query = Q()
    for status in statuses:
        query |= Q(student_courses__status__iexact=status)
    return Student.objects.filter(
        query,
        student_courses__course__course_code__iexact=course_code,
    )


def _apply_course_status_any(
    qs: QuerySet[Student], course_code: str, statuses: list[str]
) -> QuerySet[Student]:
    query = Q()
    for status in statuses:
        query |= Q(student_courses__status__iexact=status)
    if not query:
        return qs
    return qs.filter(query, student_courses__course__course_code__iexact=course_code)


def find_students_tool(
    args: dict[str, Any], *, scope: dict[str, Any] | None = None
) -> dict[str, Any]:
    min_earned = _coerce_int(args.get("min_earned_credits"), minimum=0)
    max_earned = _coerce_int(args.get("max_earned_credits"), minimum=0)
    min_gpa = _coerce_float(args.get("min_gpa"))
    max_gpa = _coerce_float(args.get("max_gpa"))
    limit = _coerce_int(args.get("limit"), minimum=1, maximum=_MAX_TOOL_ROWS) or 100
    program = str(args.get("program") or "").strip().upper()
    section = str(args.get("section") or "").strip()
    sections = _clean_sections(args.get("sections"))
    if section and not sections:
        sections = _clean_sections([section])
    advisor_id = str(args.get("advisor_id") or "").strip()
    passed_courses = _clean_course_list(args.get("passed_courses"))
    studying_courses = _clean_course_list(args.get("studying_courses"))
    course_status_any = _clean_course_status_any(args.get("course_status_any"))
    missing_courses = _clean_course_list(args.get("missing_courses"))

    filters: dict[str, Any] = {
        "min_earned_credits": min_earned,
        "max_earned_credits": max_earned,
        "min_gpa": min_gpa,
        "max_gpa": max_gpa,
        "program": program,
        "sections": sections,
        "advisor_id": advisor_id,
        "passed_courses": passed_courses,
        "studying_courses": studying_courses,
        "course_status_any": course_status_any,
        "missing_courses": missing_courses,
        "limit": limit,
    }
    filters = {k: v for k, v in filters.items() if v not in (None, "", [])}

    name_contains = str(args.get("name_contains") or "").strip()
    if name_contains:
        filters["name_contains"] = name_contains

    qs = Student.objects.all()
    qs, applied_scope = _apply_student_scope(qs, scope)
    if name_contains:
        qs = qs.filter(name__icontains=name_contains)
    if min_earned is not None:
        qs = qs.filter(total_earned_credits__gte=min_earned)
    if max_earned is not None:
        qs = qs.filter(total_earned_credits__lte=max_earned)
    if min_gpa is not None:
        qs = qs.filter(gpa__gte=min_gpa)
    if max_gpa is not None:
        qs = qs.filter(gpa__lte=max_gpa)
    if program:
        qs = qs.filter(program__iexact=program)
    if sections:
        qs = qs.filter(section__in=sections)
    if advisor_id:
        qs = qs.filter(advisor_id=advisor_id)

    for code in passed_courses:
        qs = qs.filter(
            student_courses__course__course_code__iexact=code,
            student_courses__status__iexact="passed",
        )
    for code in studying_courses:
        qs = qs.filter(
            student_courses__course__course_code__iexact=code,
            student_courses__status__iexact="studying",
        )
    for criterion in course_status_any:
        qs = _apply_course_status_any(
            qs,
            str(criterion["course_code"]),
            [str(status) for status in criterion["statuses"]],
        )
    for code in missing_courses:
        passed_or_current = _students_with_course_status(code, ["passed", "studying"]).values(
            "student_id"
        )
        qs = qs.exclude(student_id__in=passed_or_current)

    qs = qs.distinct().order_by("-total_earned_credits", "student_id")
    total = qs.count()
    rows = list(
        qs.values(
            "student_id",
            "name",
            "program",
            "section",
            "status",
            "gpa",
            "total_earned_credits",
            "current_registered_credits",
            "advisor_id",
        )[:limit]
    )
    status_codes = sorted(
        {
            *passed_courses,
            *studying_courses,
            *(str(item["course_code"]) for item in course_status_any),
        }
    )
    course_statuses: dict[int, dict[str, str]] = {}
    if rows and status_codes:
        student_ids = [int(row["student_id"]) for row in rows if row.get("student_id") is not None]
        from core.models import StudentCourse

        for row in (
            StudentCourse.objects.filter(
                student_id__in=student_ids,
                course__course_code__in=status_codes,
            )
            .select_related("course")
            .values("student_id", "course__course_code", "status")
        ):
            sid = int(row["student_id"])
            code = normalize_code(row.get("course__course_code"))
            if code:
                course_statuses.setdefault(sid, {})[code] = str(row.get("status") or "").strip()

    return {
        "tool": "find_students",
        "ok": True,
        "filters": filters,
        "scope_applied": applied_scope,
        "count": total,
        "returned": len(rows),
        "truncated": total > len(rows),
        "students": [
            {
                "student_id": row.get("student_id"),
                "name": str(row.get("name") or "").strip(),
                "program": str(row.get("program") or "").strip(),
                "section": str(row.get("section") or "").strip(),
                "status": str(row.get("status") or "").strip(),
                "gpa": row.get("gpa"),
                "total_earned_credits": row.get("total_earned_credits"),
                "current_registered_credits": row.get("current_registered_credits"),
                "advisor_id": str(row.get("advisor_id") or "").strip(),
                "course_statuses": course_statuses.get(int(row["student_id"]), {}),
            }
            for row in rows
        ],
    }


_COURSE_RE = re.compile(r"\b[A-Z]{2,5}\s*\d{1,4}\b", re.IGNORECASE)
_COURSE_PREFIX_STOPWORDS = {
    "ABOVE",
    "BELOW",
    "FIRST",
    "GPA",
    "HAD",
    "HAS",
    "HAVE",
    "HOURS",
    "LEAST",
    "LESS",
    "LIMIT",
    "MAX",
    "MIN",
    "MORE",
    "OVER",
    "TOP",
    "UNDER",
}
_PROGRAM_STOPWORDS = {
    "ALL",
    "ANY",
    "AND",
    "ARE",
    "BY",
    "FIND",
    "FOR",
    "FROM",
    "GPA",
    "HAS",
    "HAVE",
    "IN",
    "LIST",
    "MY",
    "OF",
    "OR",
    "OUR",
    "CREDIT",
    "CREDITS",
    "DATA",
    "DB",
    "DID",
    "EARNED",
    "MORE",
    "ABOVE",
    "AT",
    "ALREADY",
    "LEAST",
    "FEMALE",
    "FINISHED",
    "GIRLS",
    "LOCAL",
    "MALE",
    "MEN",
    "NEED",
    "NEEDS",
    "PASSED",
    "STUDENT",
    "STUDENTS",
    "STUDYING",
    "SHOW",
    "TAKING",
    "TO",
    "THE",
    "THIS",
    "THOSE",
    "WITH",
    "WHICH",
    "WHO",
    "WOMEN",
    # quantifier / question words that can precede "students" but are not programs
    "MANY",
    "MOST",
    "SOME",
    "FEW",
    "HOW",
    "THESE",
    "EACH",
    "BOTH",
    "COUNT",
    "TOTAL",
    "NUMBER",
    "NEW",
    "ACTIVE",
    "CURRENT",
    "CURRENTLY",
    "AVERAGE",
}


def _extract_course_codes(question: str) -> list[str]:
    codes: list[str] = []
    for match in _COURSE_RE.findall(question):
        code = normalize_code(match)
        prefix_match = re.match(r"([A-Z]+)", code)
        if prefix_match and prefix_match.group(1) in _COURSE_PREFIX_STOPWORDS:
            continue
        if code and code not in codes:
            codes.append(code)
    return codes


def _extract_min_earned_credits(question: str) -> int | None:
    text = question.lower()
    patterns = [
        r"(?:completed|earned|finished|passed)?\s*(\d{2,3})\s*(?:\+|or more|and above|at least)?\s*(?:credit\s*hours|credits|hours)",
        r"(?:credit\s*hours|credits|hours)\s*(?:>=|>|at least|above|over|more than)\s*(\d{2,3})",
        r"(?:earned|completed|finished|passed)\s+credit(?:\s*hours|s|)?\s*(\d{2,3})\s*(?:\+|or more|and above|at least)?",
        r"(?:at least|minimum|min)\s*(\d{2,3})\s*(?:credit\s*hours|credits|hours)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return _coerce_int(match.group(1), minimum=0)
    return None


def _extract_program(question: str, course_codes: list[str]) -> str:
    text = question.upper()
    for match in re.finditer(r"\b(?:PROGRAM|MAJOR)\s+([A-Z]{2,5})\b", text):
        candidate = match.group(1)
        if candidate not in _PROGRAM_STOPWORDS:
            return candidate
    for match in re.finditer(r"\b([A-Z]{2,5})\s+STUDENTS\b", text):
        candidate = match.group(1)
        if candidate not in _PROGRAM_STOPWORDS:
            return candidate
    for match in re.finditer(r"\b(?:IN|FROM|FOR|UNDER)\s+([A-Z]{2,5})\b", text):
        candidate = match.group(1)
        if candidate not in _PROGRAM_STOPWORDS:
            return candidate
    return ""


def _extract_limit(question: str) -> int | None:
    match = re.search(r"\b(?:top|first|limit)\s+(\d{1,3})\b", question.lower())
    if match:
        return _coerce_int(match.group(1), minimum=1, maximum=_MAX_TOOL_ROWS)
    if re.search(r"\ball\b", question.lower()):
        return _MAX_TOOL_ROWS
    return None


def _extract_gpa_bounds(question: str) -> tuple[float | None, float | None]:
    text = question.lower()
    min_gpa: float | None = None
    max_gpa: float | None = None
    between = re.search(
        r"\bgpa\s*(?:between|from)\s*(\d(?:\.\d+)?)\s*(?:and|to|-)\s*(\d(?:\.\d+)?)",
        text,
    )
    if between:
        first = _coerce_float(between.group(1))
        second = _coerce_float(between.group(2))
        if first is not None and second is not None:
            min_gpa, max_gpa = min(first, second), max(first, second)
            return min_gpa, max_gpa

    min_patterns = [
        r"\bgpa\s*(?:>=|>|at least|above|over|more than|or more)\s*(\d(?:\.\d+)?)",
        r"\bgpa\s*(\d(?:\.\d+)?)\s*(?:\+|or more|and above|or above)",
    ]
    max_patterns = [
        r"\bgpa\s*(?:<=|<|at most|below|under|less than)\s*(\d(?:\.\d+)?)",
        r"\bgpa\s*(\d(?:\.\d+)?)\s*(?:or less|and below|or below)",
    ]
    for pattern in min_patterns:
        match = re.search(pattern, text)
        if match:
            min_gpa = _coerce_float(match.group(1))
            break
    for pattern in max_patterns:
        match = re.search(pattern, text)
        if match:
            max_gpa = _coerce_float(match.group(1))
            break
    return min_gpa, max_gpa


def _extract_sections(question: str) -> list[str]:
    text = question.lower()
    sections: list[str] = []
    if re.search(r"\b(female|females|women|woman|girls|girl)\b", text):
        sections.append("F")
    if re.search(r"\b(male|males|men|man|boys|boy)\b", text):
        sections.append("M")
    if re.search(r"\bm\s*,\s*f\b|\bf\s*,\s*m\b", text):
        return ["M", "F"]
    for match in re.finditer(r"\bsection\s+([mf])\b|\b([mf])\s+students\b", text):
        section = (match.group(1) or match.group(2) or "").upper()
        if section in {"M", "F"} and section not in sections:
            sections.append(section)
    return sections


def _course_prefix(code: str) -> str:
    match = re.match(r"([A-Z]{2,5})", normalize_code(code))
    return match.group(1) if match else ""


def _program_near_course(line: str, code: str, course_codes: list[str]) -> str:
    code_norm = normalize_code(code)
    line_upper = line.upper()
    idx = line_upper.find(code_norm)
    if idx < 0:
        idx = line_upper.find(code_norm[:2] + " " + code_norm[2:])
    before = line_upper[: idx if idx >= 0 else len(line_upper)]
    course_prefixes = {_course_prefix(item) for item in course_codes}
    candidates = re.findall(r"\b[A-Z]{2,5}\b", before)
    for candidate in reversed(candidates):
        if candidate in _PROGRAM_STOPWORDS:
            continue
        if candidate in {"M", "F"}:
            continue
        if candidate in course_prefixes and candidate != _course_prefix(code_norm):
            continue
        return candidate
    return _course_prefix(code_norm)


def _extract_program_course_pairs(question: str, course_codes: list[str]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    lines = [line.strip() for line in re.split(r"[\n\r]+", question) if line.strip()]
    if not lines:
        lines = [question]
    for code in course_codes:
        code_norm = normalize_code(code)
        code_pattern = re.compile(
            rf"\b{re.escape(code_norm[:2])}\s*{re.escape(code_norm[2:])}\b", re.I
        )
        matched_line = next((line for line in lines if code_pattern.search(line)), question)
        program = _program_near_course(matched_line, code_norm, course_codes)
        pair = (program, code_norm)
        if pair not in pairs:
            pairs.append(pair)
    return pairs


def _status_args_for_question(text: str, course_codes: list[str]) -> dict[str, Any]:
    if any(
        word in text
        for word in ("missing", "not passed", "did not pass", "haven't passed", "have not passed")
    ):
        return {"missing_courses": course_codes}
    passed_word = any(
        word in text
        for word in (
            "already did",
            "cleared",
            "completed",
            "done",
            "finished",
            "passed",
            "taken",
            "took",
        )
    ) or bool(re.search(r"\bdid\s+[a-z]{2,5}\s*\d{1,4}\b", text, re.I))
    studying_word = any(
        word in text for word in ("current", "registered now", "studying", "taking", "taking now")
    )
    if passed_word and studying_word:
        return {
            "course_status_any": [
                {"course_code": code, "statuses": ["passed", "studying"]} for code in course_codes
            ]
        }
    if studying_word:
        return {"studying_courses": course_codes}
    if passed_word:
        return {"passed_courses": course_codes}
    return {}


def plan_verified_tools(question: str) -> list[dict[str, Any]]:
    text = question.lower()
    if not any(
        word in text
        for word in (
            "find",
            "get",
            "give",
            "girls",
            "boys",
            "list",
            "need",
            "show",
            "students",
            "which students",
            "who",
        )
    ):
        return []

    course_codes = _extract_course_codes(question)
    min_earned = _extract_min_earned_credits(question)
    limit = _extract_limit(question)
    common_args: dict[str, Any] = {}
    if min_earned is not None:
        common_args["min_earned_credits"] = min_earned
    min_gpa, max_gpa = _extract_gpa_bounds(question)
    if min_gpa is not None:
        common_args["min_gpa"] = min_gpa
    if max_gpa is not None:
        common_args["max_gpa"] = max_gpa
    sections = _extract_sections(question)
    if sections:
        common_args["sections"] = sections
    if limit is not None:
        common_args["limit"] = limit

    if len(course_codes) > 1:
        pairs = _extract_program_course_pairs(question, course_codes)
        distinct_programs = {program for program, _code in pairs if program}
    else:
        pairs = []
        distinct_programs = set()

    if len(course_codes) > 1 and len(distinct_programs) > 1:
        calls: list[dict[str, Any]] = []
        for program, code in pairs:
            args = {**common_args}
            if program:
                args["program"] = program
            args.update(_status_args_for_question(text, [code]))
            if args:
                calls.append({"tool": "find_students", "args": args})
        if calls:
            return calls

    args = {**common_args}
    program = _extract_program(question, course_codes)
    if program:
        args["program"] = program
    if course_codes:
        args.update(_status_args_for_question(text, course_codes))

    if not args and re.search(r"\bstudents\b", text):
        args["limit"] = 100

    if not args:
        return []
    return [{"tool": "find_students", "args": args}]


def execute_advisor_tool(
    tool_name: str, args: dict[str, Any], *, scope: dict[str, Any] | None = None
) -> dict[str, Any]:
    if tool_name == "find_students":
        return find_students_tool(args, scope=scope)
    return {"tool": tool_name, "ok": False, "error": "Unknown advisor tool."}


def run_planned_tools(
    question: str, *, scope: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for call in plan_verified_tools(question):
        tool_name = str(call.get("tool") or "")
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        results.append(execute_advisor_tool(tool_name, args, scope=scope))
    return results


def build_verified_student_context(
    *,
    student_id: int | None,
    academic_year: int | None = None,
    term: int | None = None,
) -> dict[str, Any]:
    if student_id is None:
        return {
            "mode": "general",
            "available_tools": [
                "find students by earned credits, GPA, advisor, program, and course status",
                "student profile lookup",
                "passed/studying course evidence",
                "programme requirement gap summary",
                "next-course recommender when year/term are provided",
            ],
        }

    student = (
        Student.objects.filter(student_id=student_id)
        .values(
            "student_id",
            "name",
            "status",
            "gpa",
            "total_registered_credits",
            "total_earned_credits",
            "current_registered_credits",
            "program",
            "section",
            "advisor_id",
        )
        .first()
    )
    if not student:
        raise ValueError(f"Student not found: {student_id}")

    program = str(student.get("program") or "").strip().upper()
    passed, studying = get_student_passed_and_studying(student_id)
    completed_or_current = passed | studying

    requirement_rows = list(
        ProgrammeRequirement.objects.filter(program=program)
        .order_by("programme_term", "course_code")
        .values("course_code", "course_name", "type", "programme_term", "credit_hours")
    )

    remaining_rows = [
        row
        for row in requirement_rows
        if normalize_code(row.get("course_code")) not in completed_or_current
    ]

    recommendations: list[str] = []
    if academic_year is not None and term is not None:
        recommendations = recommend_next_courses(student_id, academic_year, term)

    all_codes = {
        normalize_code(row.get("course_code"))
        for row in requirement_rows
        if normalize_code(row.get("course_code"))
    }
    all_codes.update(passed)
    all_codes.update(studying)
    all_codes.update(recommendations)
    names = _course_names(all_codes)

    prereq_rows = list(
        Prerequisite.objects.filter(program=program, course_code__in=recommendations).values(
            "course_code", "prerequisite_course_code"
        )
    )
    prereq_map: dict[str, list[str]] = {}
    for row in prereq_rows:
        code = normalize_code(row.get("course_code"))
        prereq_codes = [
            normalize_code(part)
            for part in str(row.get("prerequisite_course_code") or "").split(",")
            if normalize_code(part)
        ]
        if code:
            prereq_map.setdefault(code, []).extend(prereq_codes)

    return {
        "mode": "student",
        "student": {
            "student_id": student.get("student_id"),
            "name": str(student.get("name") or "").strip(),
            "status": str(student.get("status") or "").strip(),
            "program": program,
            "section": str(student.get("section") or "").strip(),
            "gpa": student.get("gpa"),
            "total_registered_credits": student.get("total_registered_credits"),
            "total_earned_credits": student.get("total_earned_credits"),
            "current_registered_credits": student.get("current_registered_credits"),
            "advisor_id": str(student.get("advisor_id") or "").strip(),
        },
        "term_context": {"academic_year": academic_year, "term": term},
        "course_evidence": {
            "passed": sorted(passed)[:_MAX_CONTEXT_COURSES],
            "studying": sorted(studying)[:_MAX_CONTEXT_COURSES],
            "remaining_requirements": _compact_course_rows(remaining_rows, names),
            "remaining_requirement_count": len(remaining_rows),
            # Exact plan totals, so the model never has to assume a
            # "standard" degree size (battery testing caught it guessing
            # 132 hours when these were absent).
            "programme_totals": {
                "total_plan_credit_hours": sum(
                    int(row.get("credit_hours") or 0) for row in requirement_rows
                ),
                "remaining_credit_hours": sum(
                    int(row.get("credit_hours") or 0) for row in remaining_rows
                ),
                "remaining_course_count": len(remaining_rows),
            },
        },
        "recommendations": [
            {
                "course_code": code,
                "course_name": names.get(code, ""),
                "prerequisites": sorted(set(prereq_map.get(code, []))),
            }
            for code in recommendations
        ],
        "limits": {
            "passed_courses_truncated": len(passed) > _MAX_CONTEXT_COURSES,
            "studying_courses_truncated": len(studying) > _MAX_CONTEXT_COURSES,
            "remaining_requirements_truncated": len(remaining_rows) > _MAX_CONTEXT_COURSES,
        },
    }


def _sanitize_history(history: Any) -> list[dict[str, str]]:
    if not isinstance(history, list):
        return []
    clean: list[dict[str, str]] = []
    for item in history[-_MAX_HISTORY_MESSAGES:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip()
        content = str(item.get("content", "")).strip()
        if role not in {"user", "assistant"} or not content:
            continue
        clean.append({"role": role, "content": content[:3000]})
    return clean


def _context_summary(context: dict[str, Any]) -> dict[str, Any]:
    if context.get("mode") != "student":
        return {"mode": "general"}
    student = context.get("student") if isinstance(context.get("student"), dict) else {}
    evidence = (
        context.get("course_evidence") if isinstance(context.get("course_evidence"), dict) else {}
    )
    return {
        "mode": "student",
        "student_id": student.get("student_id"),
        "program": student.get("program"),
        "section": student.get("section"),
        "gpa": student.get("gpa"),
        "total_earned_credits": student.get("total_earned_credits"),
        "passed_count": len(evidence.get("passed") or []),
        "studying_count": len(evidence.get("studying") or []),
        "remaining_requirement_count": evidence.get("remaining_requirement_count"),
        "recommendation_count": len(context.get("recommendations") or []),
    }


def _assistant_prefill_for_model(model: str) -> str | None:
    model_l = model.lower()
    if "qwen3" in model_l or "qwen3.6" in model_l or "qwen3.5" in model_l:
        return _QWEN_EMPTY_THINK_PREFILL
    return None


_STUDENT_ID_RE = re.compile(r"\b\d{6,9}\b")
_ARABIC_SCRIPT_RE = re.compile(r"[؀-ۿ]")


def _answer_language(question: str) -> str:
    """Deterministic answer-language pin (battery testing showed the model
    occasionally answering English questions in Arabic)."""
    return "Arabic" if _ARABIC_SCRIPT_RE.search(question or "") else "English"


def _unverified_student_ids(answer: str, evidence_texts: list[str]) -> list[str]:
    """Student-id grounding check.

    Returns ids mentioned in *answer* that appear in none of the
    evidence texts (context JSON, tool results, or the user's own
    question). High-precision on purpose: only 6-9 digit runs are
    treated as student ids; course codes and credit numbers never match.
    """
    mentioned = set(_STUDENT_ID_RE.findall(answer or ""))
    if not mentioned:
        return []
    evidence = "\n".join(evidence_texts)
    return sorted(sid for sid in mentioned if sid not in evidence)


def _summarise_tool_args(args: dict[str, Any]) -> dict[str, Any]:
    """Telemetry-safe argument summary (caps long lists/strings)."""
    summary: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, list):
            summary[key] = value[:5] + ["…"] if len(value) > 5 else value
        elif isinstance(value, str) and len(value) > 80:
            summary[key] = value[:77] + "…"
        else:
            summary[key] = value
    return summary


def _run_agent_loop(
    *,
    llm: LocalLLMClient,
    resolved_model: str,
    messages: list[dict[str, Any]],
    scope: dict[str, Any] | None,
    ctx: dict[str, Any],
    telemetry: dict[str, Any],
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    """Run the tool-calling loop. Returns (answer, usage, agent_tool_results).

    Raises ``LocalLLMBadRequest`` only when the very first tools request is
    rejected (caller falls back to the single-shot path); later turns degrade
    to a forced no-tools answer instead.
    """
    registry = get_default_registry()
    tool_schemas = registry.tool_schemas_for_scope(scope)
    agent_tool_results: list[dict[str, Any]] = []
    seen_calls: dict[str, dict[str, Any]] = {}
    total_calls = 0
    usage: dict[str, Any] = {}

    for iteration in range(_max_tool_iterations()):
        telemetry["iterations"] = iteration + 1
        try:
            turn: ToolChatResult = llm.chat_with_tools(
                messages,
                tools=tool_schemas,
                model=resolved_model,
                max_tokens=_loop_max_tokens(),
                timeout_seconds=_tool_turn_timeout(),
            )
        except LocalLLMBadRequest:
            if iteration == 0:
                raise  # model/server rejected tools — caller falls back
            logger.warning("Tool turn rejected mid-loop; forcing a final no-tools answer.")
            telemetry["turn_error"] = "bad_request_mid_loop"
            break
        except LocalLLMUnavailable as exc:
            # Timeouts and reasoning-budget exhaustion on a tool turn must
            # not 503 the whole chat. Degrade: answer from the evidence
            # gathered so far via the plain path (whose prefill suppresses
            # hidden reasoning on Qwen thinking models).
            logger.warning("Tool turn failed (%s); forcing a final no-tools answer.", exc)
            telemetry["turn_error"] = str(exc)[:200]
            break
        usage = turn.usage or usage

        if not turn.tool_calls:
            if turn.content:
                return turn.content, usage, agent_tool_results
            break  # neither calls nor content — force a final answer below

        messages.append(turn.assistant_message)
        for call in turn.tool_calls:
            total_calls += 1
            if total_calls > _max_tool_calls():
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(
                            {"ok": False, "error": "Tool budget exhausted. Answer now."},
                            ensure_ascii=False,
                        ),
                    }
                )
                continue
            dedup_key = f"{call.name}:{json.dumps(call.arguments, sort_keys=True)}"
            if dedup_key in seen_calls:
                result = {**seen_calls[dedup_key], "note": "duplicate call; reusing prior result"}
            else:
                started = time.perf_counter()
                result = registry.execute(call.name, call.arguments, scope=scope, ctx=ctx)
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                seen_calls[dedup_key] = result
                agent_tool_results.append(result)
                telemetry["tools_called"].append(
                    {
                        "name": call.name,
                        "ok": bool(result.get("ok")),
                        "ms": elapsed_ms,
                        "args": _summarise_tool_args(call.arguments),
                    }
                )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )

    # Iteration or budget limit reached: force a final answer from the
    # evidence gathered so far, with tools disabled.
    messages.append(
        {
            "role": "user",
            "content": (
                "Answer the question now using only the evidence gathered above. "
                "Do not request more tools. If the evidence is insufficient, say "
                "plainly what could not be retrieved and suggest the next step — "
                "do not guess and do not invent missing details."
            ),
        }
    )
    final: ChatResult = llm.chat(
        messages,
        model=resolved_model,
        max_tokens=_loop_max_tokens(),
        assistant_prefill=_assistant_prefill_for_model(resolved_model),
    )
    telemetry["forced_final"] = True
    return final.content, final.usage or usage, agent_tool_results


def answer_virtual_advisor(
    *,
    question: str,
    student_id: int | None = None,
    academic_year: int | None = None,
    term: int | None = None,
    history: Any = None,
    model: str | None = None,
    scope: dict[str, Any] | None = None,
    client: LocalLLMClient | None = None,
) -> dict[str, Any]:
    # Default the academic term when the caller did not supply one
    # (the WhatsApp gateway never does). Without this, every
    # time-dependent capability errors with "academic_year and term
    # are required" outside the web UI.
    if academic_year is None or term is None:
        from core.settings_views import load_defaults

        defaults = load_defaults()
        academic_year = academic_year if academic_year is not None else defaults["academic_year"]
        term = term if term is not None else defaults["term"]

    context = build_verified_student_context(
        student_id=student_id,
        academic_year=academic_year,
        term=term,
    )

    llm = client or LocalLLMClient()
    resolved_model = llm.resolve_model(model)

    telemetry: dict[str, Any] = {
        "enabled": is_agent_loop_enabled(),
        "loop_used": False,
        "iterations": 0,
        "tools_called": [],
        "fallback_reason": None,
        "forced_final": False,
        "grounding_retry": False,
        "turn_error": None,
    }
    agent_tool_results: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    answer = ""
    usage: dict[str, Any] = {}
    answer_model = resolved_model

    loop_supported = callable(getattr(llm, "chat_with_tools", None))
    if telemetry["enabled"] and not loop_supported:
        telemetry["fallback_reason"] = "client_has_no_tool_support"

    context_json = json.dumps(context, ensure_ascii=False)
    user_message = {
        "role": "user",
        "content": (
            f"verified_context:\n{context_json}\n\n"
            f"answer_language: {_answer_language(question)}\n\n"
            f"latest_question:\n{question.strip()}"
        ),
    }

    if telemetry["enabled"] and loop_supported:
        # Loop mode: NO regex seed. Battery testing showed the seed dumping
        # up to 100 unfiltered student rows into context (~13k prompt
        # tokens), which slowed every turn and tempted the model to answer
        # from a misleading sample instead of calling find_students with
        # the right filters. The model fetches precisely what it needs.
        loop_messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT_AGENT},
            *_sanitize_history(history),
            user_message,
        ]
        try:
            answer, usage, agent_tool_results = _run_agent_loop(
                llm=llm,
                resolved_model=resolved_model,
                messages=loop_messages,
                scope=scope,
                ctx={"academic_year": academic_year, "term": term},
                telemetry=telemetry,
            )
            telemetry["loop_used"] = True
            # The UI evidence panel reads ``tool_results``; in loop mode
            # the agent's tool results are that evidence.
            tool_results = agent_tool_results
        except LocalLLMBadRequest as exc:
            logger.warning("Model rejected tool calling; falling back to single-shot: %s", exc)
            telemetry["fallback_reason"] = "tools_rejected_by_model"

    if not telemetry["loop_used"]:
        # Single-shot fallback: the deterministic regex planner seeds the
        # context exactly as before the agent loop existed.
        tool_results = run_planned_tools(question, scope=scope)
        if tool_results:
            context["tool_results"] = tool_results
        context_json = json.dumps(context, ensure_ascii=False)
        user_message = {
            "role": "user",
            "content": (
                f"verified_context:\n{context_json}\n\n"
                f"answer_language: {_answer_language(question)}\n\n"
                f"latest_question:\n{question.strip()}"
            ),
        }
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(_sanitize_history(history))
        messages.append(user_message)
        result: ChatResult = llm.chat(
            messages,
            model=resolved_model,
            assistant_prefill=_assistant_prefill_for_model(resolved_model),
        )
        answer = result.content
        usage = result.usage
        answer_model = result.model

    # Grounding check: any student id in the answer must exist in the
    # evidence (context, seed tools, agent tools) or the question itself.
    evidence_texts = [
        context_json,
        question,
        *(json.dumps(item, ensure_ascii=False, default=str) for item in agent_tool_results),
    ]
    unverified = _unverified_student_ids(answer, evidence_texts)
    if unverified:
        telemetry["grounding_retry"] = True
        correction = (
            "Your draft answer mentioned student ids that do not appear in the verified "
            f"evidence: {', '.join(unverified)}. Rewrite the answer strictly from the "
            "evidence; never invent identifiers."
        )
        retry_messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            user_message,
            {"role": "assistant", "content": answer},
            {"role": "user", "content": correction},
        ]
        try:
            corrected: ChatResult = llm.chat(
                retry_messages,
                model=resolved_model,
                assistant_prefill=_assistant_prefill_for_model(resolved_model),
            )
            if corrected.content:
                answer = corrected.content
        except Exception:  # pragma: no cover - degrade to the draft answer
            logger.exception("Grounding retry failed; keeping the original answer")

    return {
        "ok": True,
        "answer": answer,
        "model": answer_model,
        "usage": usage,
        "context_summary": _context_summary(context),
        "tool_results": tool_results,
        "verified_context": context,
        "agent": {**telemetry, "tool_results": agent_tool_results},
    }

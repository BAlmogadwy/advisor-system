import json
import logging
import re
import time
from typing import Any

from django.conf import settings
from django.db.models import Q, QuerySet

from core.models import (
    Course,
    ElectiveCourse,
    Prerequisite,
    ProgrammeRequirement,
    Student,
    StudentTermSection,
    TermSection,
)
from core.services.advisor_actions import handoff_for, handoff_for_question
from core.services.advisor_clarification import clarification_for
from core.services.advisor_intent import (
    CompositionKind,
    IntentFamily,
    capabilities_for_route,
    route_intent,
)
from core.services.advisor_principal import AdvisorPrincipal
from core.services.advisor_remote_boundary import (
    DUPLICATE_NOTE,
    CachedToolExecution,
    LocalToolBoundary,
    ToolBoundary,
    boundary_for_scope,
)
from core.services.answer_consistency import check_answer
from core.services.credit_policy import credit_policy_evidence
from core.services.llm_backend import (
    BACKEND_LOCAL,
    LLMConfigError,
    LLMPrivacyError,
    get_llm_client,
)
from core.services.llm_remote_privacy import (
    EMAIL_PLACEHOLDER,
    NAME_PLACEHOLDER,
    PHONE_PLACEHOLDER,
    UNVERIFIED_ID_PLACEHOLDER,
    fold_digits,
    reference_tokens_in,
)
from core.services.local_llm import (
    ChatResult,
    LocalLLMBadRequest,
    LocalLLMClient,
    LocalLLMUnavailable,
    ToolChatResult,
)
from core.services.policy_contract import build_policy_contract_state
from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_GENERAL_ADVISOR,
    ROLE_STUDENT,
    ROLE_SUPER_ADMIN,
)
from core.services.recommender import recommend_next_courses
from core.services.student_helpers import get_student_passed_and_studying, normalize_code
from core.services.student_sections import (
    append_unmapped_studying_courses,
    get_student_term_baseline,
)
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


SYSTEM_PROMPT_TEMPLATE = """You are a university virtual academic advisor operating through verified university data tools.

Rules:
- Answer in the same language as the user's latest question. When the user message carries an answer_language field, write the final answer in that language.
- Use only the verified_context JSON supplied by the university system.
- When verified_context includes tool_results, treat them as authoritative query results.
- If a requested fact is missing from verified_context, say that the system data does not show it.
- current_term_registrations (when present) is the authoritative list of courses the student is registered in this term, with section labels and retake flags; the "studying" list is plan-status only and omits courses the student passed before and is now retaking.
- Terms are independent: never subtract the current term's registered credits from another term's capacity. current_term_registrations belongs to its own academic_year/term; recommendations target the planning term in term_context.
- TWO credit limits, never conflate them. max_recommended_credit_hours is where THIS SYSTEM stops suggesting courses; regulatory_max_credit_hours is what the university lets the student REGISTER, and it is higher. The recommendation list is capped at the first — present it as a suggestion, never as the registration ceiling. If asked how many hours they may register, answer with the regulatory range, not the suggestion cap.
- If regulatory_max_credit_hours is ABSENT from the evidence, no registration limit is known for that term (this happens for the summer term). Say the system does not define one and refer the student to the registrar. Never substitute the recommendation cap, and never assume a standard such as 21.
- If credit_policy carries a qualification block, the student's category has its own separate limit which the source leaves unresolved. Present both figures and say it is unresolved; do not assert the ordinary maximum applies to them.
- ARABIC TERMINOLOGY, non-negotiable: reserve «الحد الأعلى المسموح بتسجيله» for regulatory_max_credit_hours ONLY. Call the recommendation cap «سقف التوصية» — never «الحد الأعلى» or «الحد الأقصى». Both numbers otherwise translate to the same Arabic phrase and the distinction is lost. Worked example: with max_recommended_credit_hours=18 and regulatory_max_credit_hours=19, write «سقف التوصية 18 ساعة، والحد الأعلى المسموح بتسجيله 19 ساعة» — never «الحد الأعلى 18».
- Do not invent grades, rules, prerequisites, graduation status, rooms, sections, or approvals.
- Keep advice practical: what is known, why it matters, and the next safest action.
- Never expose chain-of-thought; provide concise evidence from the context instead.
- Treat recommendations as advising support, not official approval.
- For list questions, summarize the count, filters used, and show the most relevant rows instead of repeating every row.

{POLICY_RULES}
"""

SYSTEM_PROMPT_AGENT_TEMPLATE = """You are a university virtual academic advisor operating through verified university data tools.

Rules:
- Write the final answer in the language named by the answer_language field of the user message. Never switch languages on your own.
- Your ONLY source of facts is the verified_context JSON and the results of the tools you call. Never answer a data question from memory.
- Call tools to gather evidence BEFORE answering. Chain tools when needed (e.g. lookup_course to resolve a vague course name, then find_students with the exact code).
- If a tool returns an error, adjust the arguments or try another tool; explain the limitation only if no tool can answer.
- When evidence is sufficient, STOP calling tools and give the final answer.
- If the question is ambiguous (which student, which course, which term), ask ONE short clarifying question instead of guessing.
- Academic years are Hijri (e.g. 1448), never Gregorian. Tools default to the configured current year/term — omit academic_year/term arguments unless the user explicitly names a different term.
- For what a student is registered in or taking NOW, read course_evidence.current_term_registrations from get_student_context — it is section-level and includes retakes. The plan-status "studying" list omits courses the student passed before and is now retaking; never present it as the registration list.
- Terms are independent: never subtract the current term's registered credits from another term's capacity or limit. current_term_registrations belongs to its own academic_year/term; recommendations target the planning term.
- TWO credit limits, never conflate them. max_recommended_credit_hours is where THIS SYSTEM stops suggesting courses; regulatory_max_credit_hours is what the university lets the student REGISTER, and it is higher. The recommendation list is capped at the first — present it as a suggestion, never as the registration ceiling. If asked how many hours they may register, answer with the regulatory range, not the suggestion cap.
- If regulatory_max_credit_hours is ABSENT from the evidence, no registration limit is known for that term (this happens for the summer term). Say the system does not define one and refer the student to the registrar. Never substitute the recommendation cap, and never assume a standard such as 21.
- If credit_policy carries a qualification block, the student's category has its own separate limit which the source leaves unresolved. Present both figures and say it is unresolved; do not assert the ordinary maximum applies to them.
- ARABIC TERMINOLOGY, non-negotiable: reserve «الحد الأعلى المسموح بتسجيله» for regulatory_max_credit_hours ONLY. Call the recommendation cap «سقف التوصية» — never «الحد الأعلى» or «الحد الأقصى». Both numbers otherwise translate to the same Arabic phrase and the distinction is lost. Worked example: with max_recommended_credit_hours=18 and regulatory_max_credit_hours=19, write «سقف التوصية 18 ساعة، والحد الأعلى المسموح بتسجيله 19 ساعة» — never «الحد الأعلى 18».
- Do not invent grades, rules, prerequisites, graduation status, rooms, sections, approvals, or student ids. Every specific fact must appear in the evidence.
- Keep advice practical: what is known, why it matters, and the next safest action.
- Never expose chain-of-thought; cite concise evidence instead.
- Treat recommendations as advising support, not official approval.
- For list questions, summarize the count and filters used, then show the most relevant rows.

{POLICY_RULES}
"""

#: The policy contract, shared verbatim by BOTH answer paths. It used to live only
#: in the agent prompt, so when the loop was disabled or the model rejected tool
#: calling, the fallback answered regulation questions from parametric memory with
#: nothing to check — a complete grounding bypass, and invisible because the
#: citation check finds nothing to object to when nothing was retrieved.
_POLICY_RULES_HEADER = (
    "UNIVERSITY RULES — what is allowed, required, how long, how many, what happens if:"
)

_NEVER_FROM_MEMORY = (
    "Never state a rule, deadline, limit, penalty or entitlement from your own "
    "knowledge — not even one you are confident about. Your training contains other "
    "universities' regulations, and they are wrong here."
)

_POLICY_RULES_TAIL = """- RETRIEVED IS NOT GOVERNING. Every result is sorted into direct_policy_evidence (records that govern THIS question) and background_policy_evidence (related material that does not answer it). Anything the university REQUIRES, PERMITS, FORBIDS or DEFINES — a number, a percentage, a deadline, who is eligible, what is prohibited, what a status means, how to apply, which office decides, how to appeal — may come ONLY from direct_policy_evidence. From background you may say that related material exists and does not answer the question, and nothing more. Deriving a figure or a procedure from a related record is the failure this separation exists to prevent, and it is invisible downstream because the citation is genuine.
- If direct_policy_evidence is empty, no retrieved record governs the question. Answer any student-data part normally, then say the guide does not state the rule and refer the student to عمادة القبول والتسجيل. Do not substitute the nearest related record, and do not offer a figure "usually" or "generally" — an approximate rule the source does not contain is still an invented rule.
- Cite only policies retrieved for THIS question. Write every citation in EXACTLY this form, including the square brackets: «الدليل الإرشادي للطالب، ص NN [POLICY_ID]» — take NN from that policy's citation.page and POLICY_ID from its citation.policy_id, both verbatim. The bracketed id is how the system checks your citation; an answer that states a rule without one is treated as uncited. Never cite a policy or a page that was not retrieved, and never pair a page with a policy it does not belong to.
- If no policy was retrieved, say the system holds no written rule on it and refer the student to عمادة القبول والتسجيل. Answer any non-policy part of the question from the available evidence as usual, and say which part you could not answer. Silence about the gap is the failure, not the gap.
- Retrieval returns the NEIGHBOURHOOD of a question, not proof that an answer is in it. Read what came back before relying on it: if the policies are about the right subject but none actually states the rule asked about — how many times a course may be repeated, whether lateness counts as absence — say the guide does not state it. Stretching the nearest policy to cover the gap is a fabrication with a real citation attached, which is worse than an obvious one because it survives checking.
- decision_use governs how far you may go, and has exactly four values. PROHIBITED_FOR_DECISION: the rule exists but this system cannot check the student against it — explain the rule, say plainly that their own case cannot be verified here, and name who can. Do not rule on their situation, do not estimate, do not say "you are probably fine", and do not tell them that dismissal, approval, eligibility or safety is or is not indicated for them. PARTIALLY_EVALUABLE: some inputs exist and others do not — say which part you could check and which you could not, and never present the result as a final determination. PERMITTED_WITH_USER_PROVIDED_INPUTS: usable only with figures the student supplies in the conversation; ask for them, and never substitute stored data. EXPLANATORY_ONLY: a statement of fact with no decision in it. A value you do not recognise is to be treated as PROHIBITED_FOR_DECISION.
- If a returned policy carries `conflicts`, two sources disagree and the resolution names which one governs. Follow it, quote the governing document, and say the other source differs. Never average the two, never pick silently, and never repeat the superseded figure as the operative rule.
- Rules and the student's own data are different claims. Keep them separate in the answer: what the regulation says (cited), then what the system knows about this student, then what follows. Never present a rule as a verdict about the student.
"""

#: Agent path. The model no longer fetches its own policies — the server does,
#: before the first request — so this must not name a tool the turn does not
#: advertise. Telling a model to "call policy_lookup FIRST" when that tool is
#: absent from its schema list is the same defect the single-shot prompt had: a
#: promise the path cannot keep, which invites the model to improvise instead.
POLICY_RULES_AGENT = (
    f"{_POLICY_RULES_HEADER}\n"
    "- The approved policies for this question were ALREADY retrieved for you and "
    "are in verified_context.policy_evidence. Do not request policy_lookup; it is "
    "not available on this turn. Use ONLY policy_evidence.policies — those are the "
    f"records that govern this question. {_NEVER_FROM_MEMORY}\n"
    "- If policy_evidence.policies is empty, or the key is absent, then NO approved "
    "policy governs this question and you must not state a rule at all — say the "
    "system holds no written rule on this and refer the student to عمادة القبول "
    "والتسجيل.\n"
    f"{_POLICY_RULES_TAIL}"
)

#: Single-shot path: there are no tools, so retrieval already happened and the result
#: — including "nothing matched" — is in the context. Without this the fallback had no
#: policy contract at all and answered regulation questions from parametric memory.
POLICY_RULES_SEEDED = (
    f"{_POLICY_RULES_HEADER}\n"
    "- You have NO tools on this path. The approved policies for this question were "
    "retrieved for you and are in verified_context.policy_evidence; that is the "
    "complete set available and there is no way to fetch more. "
    f"{_NEVER_FROM_MEMORY}\n"
    "- If policy_evidence.policies is empty, or the key is absent, then NO approved "
    "policy was retrieved and you must not state a rule at all — say the system holds "
    "no written rule on this and refer the student to عمادة القبول والتسجيل.\n"
    f"{_POLICY_RULES_TAIL}"
)

SYSTEM_PROMPT_AGENT = SYSTEM_PROMPT_AGENT_TEMPLATE.format(POLICY_RULES=POLICY_RULES_AGENT)
SYSTEM_PROMPT = SYSTEM_PROMPT_TEMPLATE.format(POLICY_RULES=POLICY_RULES_SEEDED)


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
    # Resolved electives may live only in the elective catalogue or as live
    # section offerings (e.g. a course substituted by the elective resolver
    # that sits in no programme plan).
    missing = {code for code in codes if not names.get(code)}
    if missing:
        for row in ElectiveCourse.objects.filter(course_code__in=sorted(missing)).values(
            "course_code", "course_name"
        ):
            code = normalize_code(row.get("course_code"))
            if code and not names.get(code):
                names[code] = str(row.get("course_name") or "").strip()
    missing = {code for code in codes if not names.get(code)}
    if missing:
        for row in TermSection.objects.filter(course_key__in=sorted(missing)).values(
            "course_key", "course_name"
        ):
            code = normalize_code(row.get("course_key"))
            if code and not names.get(code):
                names[code] = str(row.get("course_name") or "").strip()
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
    # An absent scope is a caller whose authority nobody established, not the most
    # privileged one. Defaulting to SUPER_ADMIN made an unfiltered cohort query one
    # dropped keyword argument away.
    role = str(scope.get("role") or "")
    applied: dict[str, Any] = {"role": role}
    if role == ROLE_STUDENT:
        # NOT clamped to a minimum of 1. The WhatsApp gateway signals "no student
        # here" with -1, and `max(1, -1)` turned that refusal into student number 1
        # — a denial that resolves to a real person's record.
        student_id = _coerce_int(scope.get("student_id"))
        applied["student_id"] = student_id
        if not student_id or student_id < 1:
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
    if role == ROLE_SUPER_ADMIN:
        applied["scope"] = "all_students"
        return qs, applied
    # Anything else — an absent scope, an empty role, a role this function does not
    # know — reaches nobody. Falling through to the unfiltered queryset made "no
    # role" indistinguishable from "the highest role", which is the wrong direction
    # for a default to lean.
    applied["scope"] = "none"
    return qs.none(), applied


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


def _current_term_registrations(student_id: int, passed: set[str]) -> dict[str, Any]:
    """Section-level current registrations from the live timetable scrape.

    StudentTermSection is the authoritative "registered this term" source (the
    Timetable Builder reads the same table via get_student_term_baseline). The
    plan-status ``studying`` set cannot represent retakes: a course passed in an
    earlier term and re-registered now keeps status='passed' there, so it would
    silently vanish from a registration answer. Registrations are read for the
    student's latest (academic_year, term) — the chat's configured term is the
    term being planned FOR and may differ from the term being studied.
    """
    latest = (
        StudentTermSection.objects.filter(student_id=student_id)
        .order_by("-academic_year", "-term")
        .values_list("academic_year", "term")
        .first()
    )
    if latest is not None:
        academic_year, term = latest
        baseline = get_student_term_baseline(student_id, academic_year, term)
    else:
        academic_year = term = None
        baseline = []
    baseline = append_unmapped_studying_courses(student_id, baseline)

    by_section: dict[tuple[str, str], dict[str, Any]] = {}
    for row in baseline:
        code = normalize_code(str(row.get("course_key") or row.get("course_code") or ""))
        if not code:
            continue
        key = (code, str(row.get("section") or "").strip())
        if key in by_section:
            continue
        raw_credits = row.get("credits")
        by_section[key] = {
            "course_code": code,
            "course_name": str(row.get("course_name") or "").strip(),
            "section": key[1],
            "credit_hours": raw_credits if isinstance(raw_credits, int) else 0,
            "retake": code in passed,
        }
    registrations = sorted(by_section.values(), key=lambda r: (r["course_code"], r["section"]))
    # Credits counted once per course even if a course spans several section
    # rows (e.g. lecture + lab), so the total stays comparable to the student
    # record's current_registered_credits.
    credits_by_course: dict[str, int] = {}
    for reg in registrations:
        credits_by_course.setdefault(reg["course_code"], reg["credit_hours"])
    return {
        "academic_year": academic_year,
        "term": term,
        "source": "timetable_sections" if latest is not None else "plan_status_fallback",
        "registered_course_count": len(credits_by_course),
        "registered_credit_hours": sum(credits_by_course.values()),
        "registrations": registrations[:_MAX_CONTEXT_COURSES],
    }


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
    current_registrations = _current_term_registrations(int(student_id), passed)

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

    plan_credit_map: dict[str, int] = {}
    for req_row in requirement_rows:
        req_code = normalize_code(req_row.get("course_code"))
        if req_code:
            plan_credit_map.setdefault(req_code, int(req_row.get("credit_hours") or 0))
    # Resolved electives can land outside this programme's plan — try any
    # programme's plan, then the elective catalogue, before declaring the
    # credit hours unknown.
    rec_missing_credits = [code for code in recommendations if code not in plan_credit_map]
    if rec_missing_credits:
        for raw_code, hours in ProgrammeRequirement.objects.filter(
            course_code__in=rec_missing_credits
        ).values_list("course_code", "credit_hours"):
            plan_credit_map.setdefault(normalize_code(raw_code), int(hours or 0))
    rec_missing_credits = [code for code in recommendations if code not in plan_credit_map]
    if rec_missing_credits:
        for raw_code, hours in ElectiveCourse.objects.filter(
            course_code__in=rec_missing_credits
        ).values_list("course_code", "credit_hours"):
            plan_credit_map.setdefault(normalize_code(raw_code), int(hours or 0))

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
        "term_context": {
            "academic_year": academic_year,
            "term": term,
            "role": "planning_term_for_recommendations",
        },
        "course_evidence": {
            "passed": sorted(passed)[:_MAX_CONTEXT_COURSES],
            "studying": sorted(studying)[:_MAX_CONTEXT_COURSES],
            "current_term_registrations": current_registrations,
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
                "credit_hours": plan_credit_map.get(code),
                "prerequisites": sorted(set(prereq_map.get(code, []))),
            }
            for code in recommendations
        ],
        "recommendation_policy": credit_policy_evidence(
            recommended_credit_hours=sum(
                plan_credit_map[code] for code in recommendations if code in plan_credit_map
            ),
            unknown_for=[code for code in recommendations if code not in plan_credit_map],
            term=term,
            student_status=str(student.get("status") or "").strip(),
        ),
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
        "current_registration_count": (evidence.get("current_term_registrations") or {}).get(
            "registered_course_count"
        ),
        "remaining_requirement_count": evidence.get("remaining_requirement_count"),
        "recommendation_count": len(context.get("recommendations") or []),
    }


def _assistant_prefill_for_client(llm: Any, model: str) -> str | None:
    """The prefill, or None when the provider will not accept one.

    The model name is not enough. `qwen3.7-max` on Model Studio is the same
    family as the local build and takes the same `<think>` suppression, but the
    OpenAI-compatible endpoint does not accept a trailing assistant turn at all
    and the client raises rather than silently discarding it. Deciding from the
    name alone therefore breaks every plain-chat path the moment the backend is
    switched — single-shot, forced final, and all three retries — and each one
    fails as a 500 rather than as a degraded answer.
    """
    if not getattr(llm, "supports_assistant_prefill", True):
        return None
    return _assistant_prefill_for_model(model)


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


def _mentioned_student_ids(answer: str) -> set[str]:
    """Student-id-shaped runs in the answer, in either digit system.

    `_STUDENT_ID_RE` knows only Western digits, and an answer written in Arabic
    can legitimately carry «٤٥٠٢١٥٦». Folding first means the check cannot be
    stepped around by the script the answer happens to be in — which on an
    Arabic-first adviser is not an edge case.
    """
    folded = fold_digits(answer)
    return set(_STUDENT_ID_RE.findall(folded))


def _unverified_student_ids(answer: str, evidence_texts: list[str]) -> list[str]:
    """Student-id grounding check.

    Returns ids mentioned in *answer* that appear in none of the
    evidence texts (context JSON, tool results, or the user's own
    question). High-precision on purpose: only 6-9 digit runs are
    treated as student ids; course codes and credit numbers never match.
    """
    mentioned = _mentioned_student_ids(answer)
    if not mentioned:
        return []
    evidence = "\n".join(evidence_texts)
    return sorted(sid for sid in mentioned if sid not in evidence)


#: What the final answer may not contain, as codes rather than values. A
#: violation is reported by NAME so it can be logged, stored and shown to an
#: operator; the offending identifier itself never travels with it.
VIOLATION_UNVERIFIED_ID = "unverified_student_id"
VIOLATION_IDENTIFIER_ON_REMOTE = "identifier_the_provider_never_saw"
VIOLATION_UNISSUED_REFERENCE = "unissued_student_reference"
VIOLATION_REFERENCE_TO_A_STUDENT = "reference_shown_to_a_student"
VIOLATION_REDACTION_MARKER = "redaction_marker_in_answer"

_REDACTION_MARKERS = (
    EMAIL_PLACEHOLDER,
    PHONE_PLACEHOLDER,
    NAME_PLACEHOLDER,
    UNVERIFIED_ID_PLACEHOLDER,
)


def _withheld_for(route: Any, scope: dict[str, Any]) -> frozenset[str]:
    """Everything this turn must NOT advertise, expressed as the existing withhold set.

    Reuses `withheld_tools` rather than adding a second narrowing mechanism: the loop
    already removes withheld names from the schemas and refuses a call to one, so a
    parallel allow-list would be a second gate with its own bugs.

    `policy_lookup` is withheld on every path, unchanged: retrieval already ran
    server-side, and advertising it would invite a second lookup whose records were
    not in the contract computed before generation.

    ROLE FILTERING IS UNTOUCHED. This subtracts from the registry's role-filtered
    list, so narrowing can only ever remove — a route naming a tool the principal may
    not use does not gain it.
    """
    from core.services.advisor_intent import capabilities_for_route

    permitted = {
        (schema.get("function") or {}).get("name")
        for schema in get_default_registry().tool_schemas_for_scope(scope)
    }
    allowed = capabilities_for_route(route)
    if allowed is None:
        # GENERAL_AGENT: the router was not certain, so the turn keeps the surface it
        # has today. Narrowing an unrecognised question to nothing would be a
        # regression dressed as a safety improvement.
        return frozenset({"policy_lookup"})
    return frozenset({"policy_lookup", *(permitted - set(allowed))})


def _output_contract_violations(
    answer: str,
    *,
    evidence_texts: list[str],
    boundary: ToolBoundary,
    is_student: bool,
    tool_results: list[dict[str, Any]] | None = None,
    action: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> list[str]:
    """What is wrong with this answer, as a list of codes. Empty means shippable.

    THE TRANSPORT SANITISER IS NOT AN OUTPUT AUTHORISER. It decides what may be
    sent to a provider; this decides what may be shown to a person, and the two
    answer different questions. An identifier can be perfectly safe to hold
    locally and still be wrong in an answer, because the question is not "may
    this reader see it" but "did the system that wrote this sentence have any
    basis for it".

    That is why the remote rule is stricter than the local one. Locally the model
    reads the verified context, so an id it repeats is grounded if the evidence
    contains it. Remotely the provider was NEVER given a real identifier — so a
    real identifier in its output cannot have been read, only invented, and it
    does not become grounded by happening to match a student the adviser is
    authorised to see. `_unverified_student_ids` would clear exactly that case,
    because it checks the answer against LOCAL evidence.

    Redaction markers are a violation in their own right. «راسل [EMAIL_REDACTED]»
    is not a safe answer that lost a detail; it is a sentence that reads as
    instruction and cannot be followed, and it tells the reader that something
    was removed.
    """
    text = answer or ""
    violations: list[str] = []

    if any(marker in text for marker in _REDACTION_MARKERS):
        violations.append(VIOLATION_REDACTION_MARKER)

    references = reference_tokens_in(text)
    if references:
        if any(not boundary.reference_is_issued(ref) for ref in references):
            # Includes every reference on a local backend, where none was ever
            # issued: a `STUDENT_REF_…` token there is fabricated by definition.
            violations.append(VIOLATION_UNISSUED_REFERENCE)
        if is_student:
            # A student's session names one person. An opaque handle in their
            # answer is meaningless to them, and it means the model is talking
            # ABOUT a student rather than TO one.
            violations.append(VIOLATION_REFERENCE_TO_A_STUDENT)

    if boundary.is_remote:
        if _mentioned_student_ids(text):
            violations.append(VIOLATION_IDENTIFIER_ON_REMOTE)
    elif _unverified_student_ids(text, evidence_texts):
        violations.append(VIOLATION_UNVERIFIED_ID)

    # THE SAME GATE, not a second one. These ask a different question — does the
    # answer agree with the facts it was given — but they share the retry, the
    # re-validation and the refusal below, because a turn with two gates has two
    # places for a violation to be handled differently.
    violations.extend(check_answer(text, tool_results=tool_results, action=action, context=context))

    return violations


#: The deterministic answer when the output contract cannot be met. Names where
#: to go, for the same reason the citation refusal does: an abstention that
#: leaves the student with nowhere to turn is not much better than a wrong answer.
_GROUNDING_REFUSAL_AR = (
    "لم أتمكن من التحقق من الأرقام والمعرّفات الواردة في هذه الإجابة مقابل السجلات "
    "المعتمدة، ولذلك لن أعرضها حتى لا أنسب إليك بيانات غير مؤكدة. الرجاء مراجعة "
    "مرشدك الأكاديمي أو عمادة القبول والتسجيل للحصول على الإجابة الرسمية."
)
_GROUNDING_REFUSAL_EN = (
    "I could not verify the identifiers in this answer against the approved "
    "records, so I will not show it rather than attribute unconfirmed data to "
    "you. Please check with your academic adviser or the Deanship of Admission "
    "and Registration."
)


#: The deterministic answer when a rule was asked for and none governs it. It
#: does NOT say "the university has no such rule" — the store's silence is a fact
#: about the store, not about the regulations — and it names where to go, because
#: an abstention that leaves the student nowhere is barely better than a guess.
_POLICY_ABSTENTION_AR = (
    "سؤالك يتعلق بنظام أو لائحة، ولم أجد في الأنظمة المعتمدة لدي نصًّا يحكم هذه "
    "الحالة تحديدًا. لن أذكر لك قاعدة غير موثّقة، والأصح أن تراجع مرشدك الأكاديمي "
    "أو عمادة القبول والتسجيل للحصول على الإجابة الرسمية."
)
_POLICY_ABSTENTION_EN = (
    "Your question is about a regulation, and I could not find an approved record "
    "that governs this particular case. I will not state a rule I cannot source. "
    "Please check with your academic adviser or the Deanship of Admission and "
    "Registration for the official answer."
)


def _safe_excerpt(boundary: ToolBoundary, answer: str) -> str:
    """A rejected draft, safe to write to a trace file.

    Through the boundary's OWN sanitiser rather than a private regex, so the
    diagnostic can never leak by the back door what the transport refuses at the
    front — if a new identifier shape becomes protected, this becomes protected with
    it. Capped, because a diagnostic that carries the whole answer is the answer.
    """
    try:
        sanitised = boundary.sanitise_messages([{"role": "assistant", "content": answer}])
        text = str((sanitised or [{}])[0].get("content") or "")
    except Exception:  # noqa: BLE001 - a diagnostic must never break the refusal path
        return "<excerpt withheld: sanitiser failed>"
    return text[:400]


def _output_correction(
    violations: list[str], boundary: ToolBoundary, offending_ids: list[str]
) -> str:
    """What to tell the model, built from the violation CODES.

    The identifiers themselves are quoted ONLY on a local backend, and the
    asymmetry is the point. Locally they never left the institution and naming
    them makes the correction actionable, which means fewer questions ending in a
    refusal. Remotely, quoting them back would send precisely what the boundary
    spent the whole request keeping out — and there the model invented them
    anyway, so it has nothing to learn from seeing them again.
    """
    lines = [
        "Your draft answer breaks the output contract and cannot be shown. "
        "Rewrite it strictly from the verified evidence above."
    ]
    if VIOLATION_REDACTION_MARKER in violations:
        lines.append(
            "- It repeats a redaction placeholder. Never copy a bracketed "
            "[..._REDACTED] token into the answer; omit the detail and say plainly "
            "that it is not available here."
        )
    if VIOLATION_UNISSUED_REFERENCE in violations or VIOLATION_REFERENCE_TO_A_STUDENT in violations:
        lines.append(
            "- It names a student reference that is not valid in this answer. Do not "
            "write STUDENT_REF tokens of your own, and do not address the reader by one."
        )
    if VIOLATION_IDENTIFIER_ON_REMOTE in violations or VIOLATION_UNVERIFIED_ID in violations:
        named = f": {', '.join(offending_ids)}" if offending_ids and not boundary.is_remote else ""
        lines.append(
            f"- It states a student identifier that is not supported by the evidence{named}. "
            "Remove it. Refer to the student by their situation, not by a number, and "
            "never invent or reconstruct an identifier."
        )
    if boundary.is_remote:
        lines.append(
            "Do not attempt to restate the identifier in a different form — spelled "
            "out, in Arabic-Indic digits, or split across words."
        )
    return "\n".join(lines)


#: Policy ids are dotted upper-case runs (``TU.WITHDRAWAL.MAXIMUM``). Distinctive
#: enough that ordinary Arabic or English prose never matches, which is what keeps
#: the fabrication check from firing on innocent text.
_POLICY_ID_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:\.[A-Z0-9_]+){2,}\b")


#: The citation form the prompt mandates: «... ص 24 [TU.WITHDRAWAL.MAXIMUM]». Page
#: digits may be Arabic-Indic, so both ranges are accepted and folded before use.
_CITATION_RE = re.compile(r"ص\s*\.?\s*([0-9٠-٩]+)\s*\[\s*([A-Z][A-Z0-9_.]+)\s*\]")

#: A page reference with no bracketed id beside it. The number a student would
#: actually turn to, cited with nothing to check it against.
#: (?![0-9٠-٩]) forces the whole number to be consumed before the bracket test.
#: Without it the engine backtracks — «ص 24 [ID]» matches as page "2" followed by
#: "4", which is not a bracket, so every correctly-formed citation reads as a bare
#: page and the check fires on exactly the answers that complied.
_BARE_PAGE_RE = re.compile(r"ص\s*\.?\s*([0-9٠-٩]+)(?![0-9٠-٩])(?!\s*\[)")

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def _policy_ids_in_text(text: str) -> set[str]:
    return set(_POLICY_ID_RE.findall(text or ""))


def _claimed_citations(answer: str) -> list[dict[str, Any]]:
    """Citations the answer actually makes, in the mandated bracketed form.

    A bare policy id with no page still counts as a claim — it names a source, so
    it must be checkable — but it is the paired form the prompt requires, because
    the page is the part a student can go and verify.
    """
    claims: list[dict[str, Any]] = []
    paired: set[str] = set()
    for page, policy_id in _CITATION_RE.findall(answer or ""):
        paired.add(policy_id)
        claims.append({"policy_id": policy_id, "page": int(page.translate(_ARABIC_DIGITS))})
    for policy_id in _policy_ids_in_text(answer):
        if policy_id not in paired:
            claims.append({"policy_id": policy_id, "page": None})
    return claims


def _uncheckable_pages(answer: str, allowed: list[dict[str, Any]]) -> list[int]:
    """Page numbers cited with no policy id beside them, that no retrieved policy has.

    This is the hole the first version of this contract left wide open: the prompt
    asked for «... ص 24» and the check looked for dotted ids, so an answer that
    followed the instruction was never examined at all. A page with no id is not
    automatically wrong — but a page belonging to nothing that was retrieved is.
    """
    known: set[int] = set()
    for citation in allowed or []:
        page = citation.get("page")
        for value in page if isinstance(page, list) else [page]:
            if isinstance(value, int):
                known.add(value)
    cited = {int(p.translate(_ARABIC_DIGITS)) for p in _BARE_PAGE_RE.findall(answer or "")}
    return sorted(cited - known)


def _find_credit_block(obj: Any, depth: int = 0) -> dict[str, Any] | None:
    """Locate the credit-load evidence wherever this request happens to carry it.

    On the fallback it sits at ``context["recommendation_policy"]``; on the agent
    path it arrives nested inside ``get_student_context``'s result. Looking in only
    one place is why the first attempt at this silently did nothing on the very path
    that produced the defect.
    """
    if depth > 6:
        return None
    if isinstance(obj, dict):
        if "max_recommended_credit_hours" in obj:
            return obj
        children: Any = obj.values()
    elif isinstance(obj, list | tuple):
        # Tool results arrive as a LIST. Recursing only through dict values walked
        # straight past them, so this found the block on the fallback path and never
        # on the agent path — the one that produced the defect.
        children = obj
    else:
        return None
    for value in children:
        found = _find_credit_block(value, depth + 1)
        if found is not None:
            return found
    return None


def _credit_policy_evidence_citations(context: dict[str, Any]) -> dict[str, Any] | None:
    """Make the credit-load figures citable from the records that state them.

    Returns a ``policy_lookup``-shaped result so the citation contract, the
    validator and the response payload treat it exactly like a retrieved policy.
    Returns None when the block carries no regulatory figure — the advisory cap of
    18 is this system's own and no page of the guide says it, so lending it a
    citation would be the same defect pointed the other way.
    """
    from core.services.credit_policy import backing_citations, verify_against_store
    from core.services.policy_store import get_policy_store

    evidence = _find_credit_block(context)
    if evidence is None:
        return None
    wanted = backing_citations(evidence)
    if not wanted:
        return None

    drift = verify_against_store()
    if drift:
        # The constants and the records disagree. Citing page 23 for a figure page 23
        # does not contain would pass every mechanical check, so withhold instead.
        logger.error("credit_policy constants disagree with the policy store: %s", drift)
        return None

    result = get_policy_store().lookup(policy_ids=wanted)
    if not result.get("policies"):
        return None
    result["tool"] = "policy_lookup"
    result["note"] = (
        "These records state the credit-load figures already present in "
        "recommendation_policy. Cite them the same way as any other policy when you "
        "quote the minimum or maximum. The recommendation cap is NOT among them and "
        "must never be attributed to the guide."
    )
    return result


def _seed_policy_evidence(
    question: str, scope: dict[str, Any] | None = None
) -> tuple[dict[str, Any], str]:
    """Retrieve policies for a path that has no tools of its own.

    Returns the same shape ``policy_lookup`` produces, plus a grounding state for
    telemetry so an ungrounded answer is distinguishable after the fact:

      ``retrieved``    approved policies came back
      ``none_matched`` the store was consulted and held nothing applicable
      ``unavailable``  the store could not be consulted at all

    All three are acceptable answers to "was this grounded?"; what is not acceptable
    is not knowing. On ``unavailable`` the contract still holds — the prompt tells the
    model that an absent or empty ``policies`` list means it may not state a rule —
    so a store outage degrades to abstention rather than to model memory.
    """
    from core.services.virtual_advisor_capabilities import get_default_registry

    try:
        # The CALLER'S scope, not a hardcoded student one. This function was
        # written for the single-shot path, where the student scope was always
        # right; reusing it on the agent path with that constant would silently
        # drop `include_operator_notes` for staff, so an adviser console would
        # lose `runtime_use_reason` — the field that says why a policy may be
        # used — on every turn, with nothing to indicate it had been withheld.
        result = get_default_registry().execute(
            "policy_lookup",
            {"query": question},
            scope=scope or {"role": ROLE_STUDENT},
            ctx={},
        )
    except Exception:  # pragma: no cover - never fail an answer on a store error
        logger.exception("Policy store unavailable while seeding the single-shot path")
        return (
            {
                "tool": "policy_lookup",
                "ok": False,
                "policies": [],
                "citable": [],
                "note": (
                    "The policy store could not be consulted for this question. State "
                    "no rule at all; say the system could not check its regulations "
                    "and refer the student to عمادة القبول والتسجيل."
                ),
            },
            "unavailable",
        )

    if not isinstance(result, dict) or not result.get("ok"):
        return (
            {"tool": "policy_lookup", "ok": False, "policies": [], "citable": []},
            "unavailable",
        )
    # The same three states the agent loop reports. Collapsing "records came back"
    # into "grounded" here would leave the single-shot path silently disagreeing
    # with the loop about the one case that most needs a human.
    if not result.get("policies"):
        return (result, "none_matched")
    if not result.get("direct_policy_evidence"):
        return (result, "none_governing")
    return (result, "retrieved")


#: Buckets the model must never reason FROM, and the ones that make a seeded
#: policy result enormous. `background`, `irrelevant` and `conflicting` are the
#: applicability layer's record of what it considered and set aside; putting them
#: in the prompt invites the model to use exactly the records that were judged not
#: to govern, and one seeded lookup measures ~30 KB with them and ~4 KB without —
#: resent on every iteration of a loop that runs up to five.
_PROMPT_POLICY_KEYS = (
    "tool",
    "ok",
    "error",
    "query",
    "note",
    "as_of",
    "policy_count",
    "matched_topics",
    "grounding_state",
    "direct_policy_evidence",
    "citable",
)


def _policy_evidence_for_prompt(result: dict[str, Any]) -> dict[str, Any]:
    """The seeded evidence, reduced to what an answer may actually be built from.

    A prompt-shaping decision, not a privacy one — the remote projector still runs
    over whatever this returns. The full result stays in `agent_tool_results`,
    where the citation validator and the evidence panel read it, so nothing is
    lost from the record; only the prompt gets smaller and more honest about which
    records govern.
    """
    if not isinstance(result, dict):
        return {}
    reduced = {k: result[k] for k in _PROMPT_POLICY_KEYS if k in result}
    # ONE BUCKET PER RECORD. The first version set `policies` to the governing
    # rows and left `direct_policy_evidence` in place beside it — so the same
    # record reached the model twice, and after `_project_policy_lookup` ran it
    # carried `is_direct_evidence: false` under one key and `true` under the
    # other. A record whose directness depends on which list you read cannot be
    # the basis of a contract that turns on exactly that.
    #
    # `policies` is the name the prompt contract uses, and an empty list is
    # load-bearing — the prompt says an absent or empty `policies` means no rule
    # may be stated — so the governing rows live there and `direct_policy_evidence`
    # is dropped rather than duplicated.
    reduced["policies"] = list(result.get("direct_policy_evidence") or [])
    reduced.pop("direct_policy_evidence", None)
    reduced["background_policy_count"] = len(result.get("background_policy_evidence") or [])
    return reduced


def _retrieved_citations(tool_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Citations the answer is entitled to use: what policy_lookup returned here.

    Deduplicated by policy_id and kept in retrieval order, so the correction message
    lists them the way the model saw them.
    """
    seen: set[str] = set()
    citations: list[dict[str, Any]] = []
    for result in tool_results or []:
        # registry.execute returns the executor's dict directly — these entries are
        # the results themselves, not {"result": ...} envelopes.
        if not isinstance(result, dict) or result.get("tool") != "policy_lookup":
            continue
        for citation in result.get("citable") or []:
            pid = str((citation or {}).get("policy_id") or "")
            if pid and pid not in seen:
                seen.add(pid)
                citations.append(citation)
    return citations


#: What the student sees when the model could not source its own answer. Naming the
#: office is the point: an abstention that leaves the student with nowhere to go is
#: not much better than a wrong answer.
_CITATION_REFUSAL_AR = (
    "لم أتمكن من التحقق من مرجع هذه الإجابة في الأنظمة المعتمدة لدينا، ولذلك لن "
    "أعرضها حتى لا أنقل لك معلومة غير موثّقة. الرجاء مراجعة عمادة القبول والتسجيل "
    "أو مرشدك الأكاديمي للحصول على الإجابة الرسمية."
)
_CITATION_REFUSAL_EN = (
    "I could not verify this answer against the approved regulations held by the "
    "system, so I will not show it rather than give you an unsourced rule. Please "
    "check with the Deanship of Admission and Registration or your academic adviser."
)


def _policy_evidence_block(
    tool_results: list[dict[str, Any]], *, only: frozenset[str] | None = None
) -> str:
    """The retrieved policies as text, for the correction turn.

    `only` narrows it to a named set — the governing records, when the correction
    is about citing one. Showing the full retrieved list there would hand the
    model the background records again and invite it to cite one of those, which
    is the failure the correction exists to fix.
    """
    lines: list[str] = []
    for result in tool_results or []:
        if not isinstance(result, dict) or result.get("tool") != "policy_lookup":
            continue
        rows = result.get("policies") or []
        if only is not None:
            rows = [
                r for r in rows if isinstance(r, dict) and str(r.get("policy_id") or "") in only
            ] or [
                r
                for r in (result.get("direct_policy_evidence") or [])
                if isinstance(r, dict) and str(r.get("policy_id") or "") in only
            ]
        for policy in rows:
            citation = policy.get("citation") or {}
            statement = str(policy.get("statement_ar") or policy.get("title_ar") or "").strip()
            lines.append(
                f"- [{policy.get('policy_id')}] ص {citation.get('page')} — {statement[:400]}"
            )
    return "\n".join(dict.fromkeys(lines))


def _bad_citations(answer: str, citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every citation in the answer that does not check out, with the reason.

    Runs the store's own validator rather than a second implementation, so page,
    document and edition are checked against the record — not merely the policy id
    against a list. Also catches the bare «ص NN» form, which the first version of
    this contract asked the model to produce and then never examined.
    """
    from core.services.policy_store import get_policy_store

    problems: list[dict[str, Any]] = []
    claimed = _claimed_citations(answer)
    if claimed:
        try:
            verdict = get_policy_store().validate_citations(claimed, citations)
        except Exception:  # pragma: no cover - never fail an answer on a store error
            logger.exception("Citation validation failed; treating citations as unchecked")
            verdict = {"rejected": []}
        for item in verdict.get("rejected") or []:
            problems.append(
                {
                    "policy_id": (item.get("citation") or {}).get("policy_id"),
                    "page": (item.get("citation") or {}).get("page"),
                    "reason": item.get("reason"),
                }
            )
    for page in _uncheckable_pages(answer, citations):
        problems.append(
            {"policy_id": None, "page": page, "reason": "PAGE_NOT_IN_ANY_RETRIEVED_POLICY"}
        )
    return problems


def _fabricated_policy_ids(answer: str, citations: list[dict[str, Any]]) -> list[str]:
    """Policy ids the answer cites that were never retrieved this request.

    A real, approved, current policy still counts as fabricated here if the model
    did not fetch it — otherwise an id recalled from training reads as grounded.
    """
    allowed = {c.get("policy_id") for c in citations}
    return sorted(pid for pid in _policy_ids_in_text(answer) if pid not in allowed)


def _known_names_from_context(context: dict[str, Any]) -> tuple[str, ...]:
    """The exact personal names this request already knows about.

    An exact set, never a pattern. A proper-name detector would have to decide
    whether «الرياضيات المتقطعة» or «الدليل الإرشادي للطالب» is a person, and every
    wrong answer either mangles a course title or leaves a name in place. What the
    request genuinely holds is the student's own name — which is also the name most
    likely to be typed into a question — so that is what gets redacted.
    """
    student = (context or {}).get("student")
    name = str((student or {}).get("name") or "").strip() if isinstance(student, dict) else ""
    return (name,) if len(name) >= 3 else ()


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


#: Returned by the loop when a capability produced a route. Not an answer —
#: `answer_virtual_advisor` replaces it with the handoff's own text, in the
#: student's language. A sentinel rather than the text itself so the loop does
#: not need to know about answer language, and so a bug that leaks it is
#: unmistakable rather than plausible.
_ACTION_HANDOFF_SENTINEL = "\x00action-handoff\x00"


def _tool_message(call_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The one place a tool result becomes text bound for the model.

    Every `role: "tool"` message in the loop is built here, so "what did we
    actually send" is a single line to audit rather than four `json.dumps` calls
    that have to be checked individually for which object they were handed.
    """
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(payload, ensure_ascii=False, default=str),
    }


def _scrub_refused_call_arguments(messages: list[dict[str, Any]], call_id: str) -> None:
    """Empty the arguments of a tool call the boundary refused.

    The assistant message carrying the call has already been appended, and it
    stays: the protocol requires every `role: "tool"` reply to answer an
    assistant `tool_call_id`, so deleting it would make the conversation invalid.
    Its ARGUMENTS, though, are not needed by anything — the call did not run.

    Strictly this is not a leak: a forged `student_id` was written by the model,
    so echoing it back tells the provider only what it just told us. It is
    scrubbed anyway for two reasons. A fabricated identifier can collide with a
    real student, and a number that was refused for naming somebody should not be
    reinforced in the context for the next turn. And it keeps the invariant the
    tests assert absolute — "a real id appears nowhere in what was sent" — rather
    than an invariant with an exception that every later test has to remember.
    """
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        calls = message.get("tool_calls") if isinstance(message, dict) else None
        if not isinstance(calls, list):
            continue
        if not any(isinstance(c, dict) and c.get("id") == call_id for c in calls):
            continue
        messages[index] = {
            **message,
            "tool_calls": [
                {**c, "function": {**(c.get("function") or {}), "arguments": "{}"}}
                if isinstance(c, dict) and c.get("id") == call_id
                else c
                for c in calls
            ],
        }
        return


def _inject_credit_evidence(
    local_result: dict[str, Any],
    *,
    seen: set[str],
    messages: list[dict[str, Any]],
    boundary: ToolBoundary,
    local_results: list[dict[str, Any]],
    provider_results: list[dict[str, Any]],
) -> bool:
    """Retrieve and send the policies behind a credit block a tool just returned.

    Returns True if anything was injected. Idempotent per distinct block: `seen`
    keys on the figures themselves, so a tool called twice does not re-send the
    same records, and two tools returning the same block cost one injection.

    Appended as a `role: "user"` message, NOT a second `role: "tool"` one. The
    protocol allows exactly one tool reply per `tool_call_id`, so a second would
    be rejected by a strict provider — and folding it into the first is worse
    anyway: it answers a different question, "where is that number written down",
    and a model shown one merged object cannot tell which part it may cite.
    """
    block = _find_credit_block(local_result)
    if block is None:
        return False
    key = json.dumps(block, sort_keys=True, default=str)[:400]
    if key in seen:
        return False
    seen.add(key)
    evidence = _credit_policy_evidence_citations({"context": local_result})
    if not evidence:
        return False
    local_results.append(evidence)
    try:
        projected = boundary.project_tool_result("policy_lookup", evidence)
    except LLMPrivacyError:
        logger.error("Projection refused credit-policy evidence; nothing was sent.")
        return False
    provider_results.append(projected)
    payload = json.dumps(_policy_evidence_for_prompt(projected), ensure_ascii=False, default=str)
    messages.append(
        {
            "role": "user",
            "content": (
                "policy_evidence for the credit figures just returned — these are the "
                "ONLY records behind them, with their ids and pages. Cite one of these "
                "whenever you state a credit limit, and state no limit they do not "
                "contain:\n" + payload
            ),
        }
    )
    return True


def _run_agent_loop(
    *,
    llm: LocalLLMClient,
    resolved_model: str,
    messages: list[dict[str, Any]],
    scope: dict[str, Any] | None,
    ctx: dict[str, Any],
    telemetry: dict[str, Any],
    boundary: ToolBoundary | None = None,
    withheld_tools: frozenset[str] = frozenset(),
    seeded_local_results: list[dict[str, Any]] | None = None,
    seeded_provider_results: list[dict[str, Any]] | None = None,
    credit_blocks_seeded: set[str] | None = None,
) -> tuple[str, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Run the tool-calling loop.

    Returns (answer, usage, local_tool_results, provider_tool_results). The last
    two are the same objects on a local backend and deliberately different ones on
    a remote backend — see `advisor_remote_boundary`. Callers that build evidence,
    citations or stored turns want the first; anything that will be serialised
    towards a provider wants the second, and the two lists exist so that choice
    has to be made explicitly at each site instead of defaulting to whatever
    variable was nearest.

    Raises ``LocalLLMBadRequest`` only when the very first tools request is
    rejected (caller falls back to the single-shot path); later turns degrade
    to a forced no-tools answer instead.
    """
    boundary = boundary or LocalToolBoundary()
    registry = get_default_registry()
    # Withheld, not deregistered. `policy_lookup` stays in the registry — the CI
    # safety gate executes it, the remote projector is keyed on its name, and five
    # store tests call it — so what changes is only what THIS turn advertises.
    tool_schemas = boundary.tool_schemas(
        [
            schema
            for schema in registry.tool_schemas_for_scope(scope)
            if (schema.get("function") or {}).get("name") not in withheld_tools
        ]
    )
    # A shorter list is not enforcement. Nothing in this loop previously compared
    # a requested tool against the advertised set: the registry checks roles, the
    # local boundary checks nothing, and the remote boundary checks the exposure
    # map — so a model that asked for a withheld tool anyway would simply get it,
    # retrieve a second time, and widen the citation set past the contract that
    # was computed before the loop started.
    advertised = {(schema.get("function") or {}).get("name") for schema in tool_schemas}
    # Seeded rather than empty: the server-side policy prefetch and any
    # credit-policy backing evidence were retrieved BEFORE the first model call,
    # and they are part of this turn's evidence exactly as a tool result is. The
    # lists are copied so the caller's are not mutated from inside the loop.
    agent_tool_results: list[dict[str, Any]] = list(seeded_local_results or [])
    provider_tool_results: list[dict[str, Any]] = list(seeded_provider_results or [])
    seen_calls: dict[str, CachedToolExecution] = {}
    #: Which credit blocks have already had their backing evidence injected, so a
    #: repeated tool does not re-send the same policies every iteration. Shared
    #: with the caller: a block seeded before the loop must not be injected again
    #: the moment a tool returns the same figures.
    credit_blocks_seeded = credit_blocks_seeded if credit_blocks_seeded is not None else set()
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
                return turn.content, usage, agent_tool_results, provider_tool_results
            break  # neither calls nor content — force a final answer below

        messages.append(turn.assistant_message)
        for call in turn.tool_calls:
            total_calls += 1
            if total_calls > _max_tool_calls():
                messages.append(
                    _tool_message(
                        call.id, {"ok": False, "error": "Tool budget exhausted. Answer now."}
                    )
                )
                continue

            if call.name in withheld_tools:
                # DELIBERATELY withheld for this turn, and the model is told why.
                # A bare "not available" would read as a capability gap and invite
                # it to work around one; naming the evidence it already has is the
                # instruction that actually stops the second retrieval.
                telemetry.setdefault("withheld_tool_calls", []).append(call.name)
                _scrub_refused_call_arguments(messages, call.id)
                messages.append(
                    _tool_message(
                        call.id,
                        {
                            "ok": False,
                            "error": (
                                "This tool is not available on this turn. The evidence "
                                "it would return is already in verified_context; use "
                                "that and do not request it again."
                            ),
                        },
                    )
                )
                continue
            if call.name not in advertised:
                # Not withheld — never offered. A capability the boundary or the
                # role check keeps from this session, asked for by name anyway.
                # It takes the BOUNDARY's refusal, not the withheld one, so a
                # denied capability keeps saying the one thing it may safely say
                # and does not get relabelled as "already retrieved".
                logger.warning("Model requested an unadvertised tool: %s", call.name)
                telemetry["boundary_refusals"].append({"name": call.name, "stage": "pre_execution"})
                _scrub_refused_call_arguments(messages, call.id)
                messages.append(_tool_message(call.id, boundary.refusal_result(call.name)))
                continue

            # ── the execution order ──────────────────────────────
            #
            # Written out step by step rather than delegated to one call,
            # because the ORDER is the property being enforced and a reviewer
            # has to be able to see it. Everything above `registry.execute`
            # refuses before any database read; the projection below it refuses
            # before any egress. On a local backend every step here is the
            # identity function and the loop behaves exactly as it always did.
            try:
                boundary.assert_capability_allowed(call.name)
                safe_args = boundary.reject_identity_arguments(call.name, call.arguments)
                resolved_args = boundary.resolve_reference_arguments(call.name, safe_args)
                boundary.authorise_resolved_arguments(call.name, resolved_args)
            except LLMPrivacyError:
                # Nothing ran: no executor, no query, no result to leak. The
                # exception text names the guard and sometimes the student, so
                # it is not logged and not shown to the model — the model gets
                # one refusal that reads the same whichever guard fired.
                logger.warning("Boundary refused %s before execution.", call.name)
                telemetry["boundary_refusals"].append({"name": call.name, "stage": "pre_execution"})
                _scrub_refused_call_arguments(messages, call.id)
                messages.append(_tool_message(call.id, boundary.refusal_result(call.name)))
                continue

            # Keyed on the RESOLVED arguments, so two references to the same
            # student hit the same entry. Never recorded in telemetry: after
            # resolution this string contains a real student id.
            dedup_key = f"{call.name}:{json.dumps(resolved_args, sort_keys=True, default=str)}"
            cached = seen_calls.get(dedup_key)
            if cached is not None:
                # Both halves come from the pair. Re-projecting the cached local
                # result here would read a map that has moved on, and reaching
                # for `cached.local_result` on the provider side is the exact
                # cross-boundary reconstruction the pair exists to prevent.
                local_result = {**cached.local_result, "note": DUPLICATE_NOTE}
                provider_result = {**cached.provider_result, "note": DUPLICATE_NOTE}
            else:
                started = time.perf_counter()
                local_result = registry.execute(call.name, resolved_args, scope=scope, ctx=ctx)
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                try:
                    provider_result = boundary.project_tool_result(call.name, local_result)
                except LLMPrivacyError:
                    # A shape the projector will not accept. Execution has
                    # already happened — that much could not be known in
                    # advance — but egress has not, and the result stays out of
                    # the log line and out of the re-raised exception.
                    logger.error("Projection refused a %s result; nothing was sent.", call.name)
                    telemetry["boundary_refusals"].append(
                        {"name": call.name, "stage": "projection"}
                    )
                    messages.append(_tool_message(call.id, boundary.refusal_result(call.name)))
                    continue
                # A ROUTE, not data. The server has already decided where
                # this request goes; handing that to the model to paraphrase is
                # how a confirmation requirement became «the feature does not
                # exist», with advice to delete real registrations attached.
                # Short-circuit before the provider sees anything.
                handoff = handoff_for(local_result)
                if handoff is not None:
                    telemetry["action_handoff"] = handoff.action
                    agent_tool_results.append(local_result)
                    provider_tool_results.append(provider_result)
                    return (
                        _ACTION_HANDOFF_SENTINEL,
                        usage,
                        agent_tool_results,
                        provider_tool_results,
                    )

                seen_calls[dedup_key] = CachedToolExecution(local_result, provider_result)
                agent_tool_results.append(local_result)
                provider_tool_results.append(provider_result)
                telemetry["tools_called"].append(
                    {
                        "name": call.name,
                        "ok": bool(local_result.get("ok")),
                        "ms": elapsed_ms,
                        # The model's OWN arguments, before resolution. In remote
                        # mode those carry `student_ref`; `resolved_args` carry
                        # the real id, and telemetry is stored and shipped.
                        "args": _summarise_tool_args(call.arguments),
                    }
                )
            messages.append(_tool_message(call.id, provider_result))

            # A tool result can introduce a credit block that the initial context
            # did not have — `get_student_context` is the usual one. Its backing
            # citations are retrieved and injected HERE, before the next model
            # turn, rather than at final validation. Attaching them afterwards
            # tells the validator which policies could have been cited while the
            # model composed the answer without ever seeing them, which is exactly
            # how a correct 19-hour limit arrives with no citation behind it.
            injected = _inject_credit_evidence(
                local_result,
                seen=credit_blocks_seeded,
                messages=messages,
                boundary=boundary,
                local_results=agent_tool_results,
                provider_results=provider_tool_results,
            )
            if injected:
                telemetry.setdefault("credit_evidence_injected", []).append(call.name)

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
        assistant_prefill=_assistant_prefill_for_client(llm, resolved_model),
    )
    telemetry["forced_final"] = True
    return final.content, final.usage or usage, agent_tool_results, provider_tool_results


def answer_virtual_advisor(
    *,
    question: str,
    principal: AdvisorPrincipal,
    academic_year: int | None = None,
    term: int | None = None,
    history: Any = None,
    model: str | None = None,
    client: LocalLLMClient | None = None,
) -> dict[str, Any]:
    """Answer one question, as one identity.

    `principal` replaced a `student_id` parameter and a `scope` dict that each
    carried a student identity. Two channels meant a caller could fill in one and
    forget the other — which is exactly what the conversation view did, so every
    stored turn was answered without the student's own record. They are now one
    object, and the scope is derived from it rather than supplied alongside it, so
    "whose record loads" and "under whose authority" cannot disagree.
    """
    student_id = principal.student_id
    scope = principal.as_scope()

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

    # THE FACTORY, not `LocalLLMClient()`. Until this line `LLM_BACKEND=alibaba`
    # selected a remote privacy boundary while generation still went to the local
    # model — the most misleading state the feature could be in, because every
    # privacy test would pass against a provider that was never contacted.
    llm = client or get_llm_client()
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
        "boundary_refusals": [],
    }
    # Whether anything leaves the institution is decided here, from configuration
    # alone, and once. `LLM_BACKEND=local` yields a boundary whose every step is
    # the identity function, so the shipped default is byte-for-byte the previous
    # behaviour rather than a new path that happens to agree with it.
    #
    # The student's own name is handed over as a redaction target: it is the one
    # personal string that reliably appears in a question ("أنا محمد، كم ساعة…")
    # and the sanitiser works from an exact set rather than a name pattern.
    # Derived from the CLIENT, never from settings independently. Two sources for
    # one fact is how a remote client ends up holding `LocalToolBoundary` and full
    # records: a caller injects a client the settings do not describe — a test, a
    # management command, a future queue worker — and the privacy behaviour
    # silently follows the wrong one. The client is the thing that will actually
    # receive the payload, so it is the thing that decides what may be in it.
    client_backend = str(getattr(llm, "backend", BACKEND_LOCAL) or BACKEND_LOCAL)
    configured_backend = (
        str(getattr(settings, "LLM_BACKEND", BACKEND_LOCAL) or BACKEND_LOCAL).strip().lower()
    )
    if client is None and client_backend != configured_backend:
        # Only for the production construction path. An injected client is
        # allowed to disagree with settings — that is what makes it injectable —
        # but the factory building something the deployment did not ask for is a
        # configuration fault, and answering anyway would answer through the
        # wrong provider.
        raise LLMConfigError(
            f"the configured backend is {configured_backend!r} but the client "
            f"reports {client_backend!r}; refusing to answer through an "
            "unintended provider."
        )

    # ── the route, decided from the question, before any evidence is gathered ──
    #
    # HERE and not later, for two reasons that are not about cost, both measured
    # against the loaded store rather than reasoned about:
    #
    #   «سوِّ لي أكثر من خيار للجدول»  retrieved     8 policies, 2 citable
    #   «احفظ الخيار الثاني…»          none_matched  0 policies
    #   «عدّلت قائمة المقررات…»         retrieved     8 policies, 2 citable
    #
    # Retrieval seeds `tool_results`, which the UI renders as the evidence behind
    # the answer — so two of the three would carry eight policy records beside a
    # fixed referral that cites none of them. And `derive_outcome` reads
    # `policy_grounding`: the preference question matches nothing, `none_matched`
    # becomes POLICY_NOT_FOUND, and POLICY_NOT_FOUND with no citations is ABSTAIN,
    # which stores a route the student can act on as one the adviser declined.
    #
    # STUDENTS ONLY, and that is a correctness gate rather than caution. Every
    # planner-draft endpoint builds its principal with `AdvisorPrincipal.for_student`
    # and refuses anything else, so «افتح المخطط الدراسي» offered to an adviser
    # names a screen that will answer them 403. Routing someone to a door that is
    # locked against them is the same defect as denying a feature that exists,
    # pointed the other way.
    #
    # `PLANNER_REBUILD` is not routed here — see `ROUTED_INTENTS`. It is refused
    # inside `build_my_timetable`, where the arguments are known, and answering it
    # from the question as well would leave one rule with two implementations.
    # Computed BEFORE anything reads it — the rebuild check below, the tool schemas,
    # and the policy contract all key on it. `route_intent` is offline and side-effect
    # free, so this costs a string scan and nothing else.
    route = route_intent(question)
    # Set HERE, before the hand-off short-circuits below. Recorded on every path or
    # the evaluator cannot tell "routed correctly and answered deterministically"
    # from "never routed at all" — and those are the two outcomes it exists to
    # separate.
    telemetry["primary_family"] = str(route.primary_family)
    telemetry["composition"] = str(route.composition)

    # ── the rebuild, whose refusal must not depend on the model choosing to ask ──
    #
    # `PLANNER_REBUILD` is deliberately absent from `ROUTED_INTENTS`: the rule that a
    # destructive rebuild needs confirmation lives in `build_my_timetable`, once, and
    # a second copy here is how the audited one stops running. So the route does not
    # answer the question — it EXECUTES that one implementation.
    #
    # Measured before this existed: with `tool_choice` free, a model that simply did
    # not call the tool got «سأبني لك جدولًا جديدًا يتجاهل تسجيلك الحالي» through with
    # `action: None` — a promise to discard the student's registration, from a system
    # that would not have done it. Narrowing the surface to one tool in 7B made that
    # path both easier to see and the only thing left to close.
    #
    # STUDENTS ONLY, for the same reason the other routes are: every planner-draft
    # endpoint answers 403 to anyone else.
    if principal.role == ROLE_STUDENT and route.primary_family is IntentFamily.PLANNER_REBUILD:
        refusal = get_default_registry().execute(
            "build_my_timetable",
            {"keep_current_sections": False},
            scope=scope,
            ctx={"academic_year": academic_year, "term": term},
        )
        rebuilt = handoff_for(refusal)
        if rebuilt is not None:
            telemetry["action_handoff"] = rebuilt.action
            telemetry["intent_route"] = rebuilt.intent
            telemetry["rebuild_forced_server_side"] = True
            return {
                "ok": True,
                "answer": rebuilt.answer(_answer_language(question)),
                "action": rebuilt.as_payload(),
                "model": "",
                "usage": {},
                "context_summary": _context_summary(context),
                "tool_results": [refusal],
                "verified_context": context,
                "citations": [],
                "cited_policy_ids": [],
                "data_part": {"status": "NOT_ATTEMPTED", "facts": {}},
                "policy_part": {"status": "ANSWERED", "evidence": []},
                "agent": {**telemetry, "tool_results": [refusal]},
            }

    # ── the question that cannot be answered because it names nothing ──
    #
    # Before the provider, for the same reason the hand-offs are: "ask which course
    # they mean" written in a system prompt competes with twelve other instructions
    # and with a tool that looks answerable, and a model that skips it invents the
    # subject. Measured on the contract, three questions are in this shape and all
    # three executed a data tool about a course nobody named.
    #
    # HISTORY FIRST. «هذا المقرر» after a turn about AI331 has a referent, and asking
    # again is worse than guessing because the student already told us.
    asked = clarification_for(question, history=history)
    if asked is not None:
        telemetry["clarification_reason"] = asked.reason
        return {
            "ok": True,
            "answer": asked.answer(_answer_language(question)),
            "action": None,
            "model": "",
            "usage": {},
            "context_summary": _context_summary(context),
            "tool_results": [],
            "verified_context": context,
            "citations": [],
            "cited_policy_ids": [],
            "data_part": {"status": "NOT_ATTEMPTED", "facts": {}},
            "policy_part": {"status": "ANSWERED", "evidence": []},
            "agent": {**telemetry, "tool_results": []},
        }

    if principal.role == ROLE_STUDENT:
        routed = handoff_for_question(question)
        if routed is not None:
            telemetry["action_handoff"] = routed.action
            telemetry["intent_route"] = routed.intent
            return {
                "ok": True,
                "answer": routed.answer(_answer_language(question)),
                "action": routed.as_payload(),
                # No model was contacted, so naming one would attribute a constant
                # in this repository to a provider that never saw the question —
                # and `_persist_answer` stores this string on the turn.
                "model": "",
                "usage": {},
                "context_summary": _context_summary(context),
                "tool_results": [],
                "verified_context": context,
                "citations": [],
                "cited_policy_ids": [],
                "agent": {**telemetry, "tool_results": []},
            }

    boundary = boundary_for_scope(
        scope,
        backend=client_backend,
        known_names=_known_names_from_context(context),
    )

    # The credit range reaches the model through recommendation_policy, not through
    # policy_lookup — a second regulatory channel that was outside the citation
    # contract entirely. The batch caught it: «الحد الأدنى حسب الدليل 12 ساعة» went
    # to a student attributed to the guide with nothing citable behind it. Binding
    # the figures to the records they actually come from makes them citable like any
    # other rule, and makes an unbacked figure impossible rather than merely unlikely.
    tool_results: list[dict[str, Any]] = []
    answer = ""
    usage: dict[str, Any] = {}
    answer_model = resolved_model

    # Recorded, not recomputed by the evaluator. "Which tools did the server offer"
    # and "which did the model call" are different questions, and an evaluation that
    # derives the first from its own copy of the routing table cannot tell an
    # orchestration failure from a model failure — it would agree with itself.
    # ── a MULTI_CAPABILITY route's evidence is the SERVER'S obligation ──
    #
    # Measured on the live canary: TT20 «ليش ما ضفت AI491؟ هل المشكلة في المتطلب
    # السابق أو في وقت الشعبة؟» asks two independent questions, was advertised both
    # tools, and the provider called one — then answered the prerequisite half and
    # left the timetable half unaddressed. `why_course_locked` structurally cannot
    # say whether the course was excluded for a section-fit reason.
    #
    # Retrying and asking the model to please call the other tool is the weaker fix:
    # it costs another paid turn and still depends on the same choice. When the ROUTE
    # declares two capabilities, the server fetches whichever the model did not, and
    # the provider writes the answer over complete evidence.
    #
    # SINGLE and GENERAL_AGENT are untouched — there the model's selection IS the
    # decision, and completing it would be the server answering a question it did not
    # route.
    def _complete_required_evidence(
        results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if route.composition is not CompositionKind.MULTI_CAPABILITY:
            return results
        obtained = {r.get("tool") for r in results if isinstance(r, dict)}
        missing = [t for t in (capabilities_for_route(route) or ()) if t not in obtained]
        for tool in missing:
            logger.info("Completing required evidence for %s: %s", route.composition, tool)
            results = [
                *results,
                get_default_registry().execute(
                    tool, {}, scope=scope, ctx={"academic_year": academic_year, "term": term}
                ),
            ]
            telemetry.setdefault("server_completed_tools", []).append(tool)
        return results

    # Recorded so an evaluation can attribute a failure to a LAYER. Without the
    # family in the trace, "the answer was wrong" cannot be told apart from "the
    # router sent it to the wrong surface", which is what made the first live batch
    # an expensive debugging exercise rather than a measurement.
    withheld = _withheld_for(route, scope)
    telemetry["exposed_tools"] = sorted(
        name
        for name in (
            (schema.get("function") or {}).get("name")
            for schema in get_default_registry().tool_schemas_for_scope(scope)
        )
        if name and name not in withheld
    )

    loop_supported = callable(getattr(llm, "chat_with_tools", None))
    if telemetry["enabled"] and not loop_supported:
        telemetry["fallback_reason"] = "client_has_no_tool_support"

    # ── policy retrieval, server-side, before anything is generated ──
    #
    # Retrieval is no longer the model's decision on either path. It used to be:
    # the loop advertised `policy_lookup` and the prompt asked the model to call
    # it first, so "was this answer grounded" depended on whether a model chose to
    # look — and `not_consulted` was a real, common outcome that read downstream
    # exactly like a grounded one.
    #
    # Run with the CALLER'S scope and the ORIGINAL question, locally, before any
    # projection: the store must see what the student actually wrote, not an
    # aliased or redacted rendering of it.
    policy_evidence, grounding = _seed_policy_evidence(question, scope)
    telemetry["policy_grounding"] = grounding
    # PROJECT FIRST, then shape. The other order runs the prompt trim over a raw
    # local result and hands the projector something that no longer matches the
    # shape it was written against — which is how the duplicate-bucket defect
    # above became invisible. The projector decides what may leave; the trim only
    # decides how much of that is worth the prompt.
    context["policy_evidence"] = _policy_evidence_for_prompt(
        boundary.project_tool_result("policy_lookup", policy_evidence)
    )
    agent_tool_results = [policy_evidence]

    # The credit range reaches the model through `recommendation_policy`, which is
    # in the verified context from the start — so its backing records can be
    # retrieved now rather than at final validation. Doing it afterwards told the
    # validator which policies COULD have been cited while the model composed the
    # answer having never seen them, which is how a correct 19-hour limit arrives
    # attributed to nothing. Blocks that only appear later, when a tool returns
    # them, are injected mid-loop by `_inject_credit_evidence`.
    credit_blocks_seeded: set[str] = set()
    seeded_credit = _credit_policy_evidence_citations({"context": context})
    if seeded_credit:
        credit_blocks_seeded.add(
            json.dumps(_find_credit_block(context), sort_keys=True, default=str)[:400]
        )
        agent_tool_results = [*agent_tool_results, seeded_credit]
        context["credit_policy_evidence"] = _policy_evidence_for_prompt(
            boundary.project_tool_result("policy_lookup", seeded_credit)
        )

    provider_tool_results = [
        boundary.project_tool_result("policy_lookup", r) for r in agent_tool_results
    ]

    # TWO SERIALISATIONS OF THE SAME CONTEXT, and they are not interchangeable.
    # `context_json` is the complete local record: it is the evidence the
    # grounding check reads, and shrinking it there would turn a check into a
    # rubber stamp. `prompt_context_json` is what a model is allowed to see.
    # Locally they are identical strings; remotely the second is the projection.
    context_json = json.dumps(context, ensure_ascii=False)
    prompt_context_json = json.dumps(boundary.project_context(context), ensure_ascii=False)
    user_message = {
        "role": "user",
        "content": (
            f"verified_context:\n{prompt_context_json}\n\n"
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
        loop_messages: list[dict[str, Any]] = boundary.sanitise_messages(
            [
                {"role": "system", "content": SYSTEM_PROMPT_AGENT},
                *_sanitize_history(history),
                user_message,
            ]
        )
        # Sanitised HERE, once, over the whole list rather than over the question
        # alone. History is the part that gets forgotten: a stored turn carries
        # last week's question back into this prompt verbatim, and a boundary that
        # only inspects `question` would wave it through. Messages appended inside
        # the loop need no second pass — they are the model's own words coming
        # back, and tool results that were already projected.
        try:
            answer, usage, agent_tool_results, provider_tool_results = _run_agent_loop(
                llm=llm,
                resolved_model=resolved_model,
                messages=loop_messages,
                scope=scope,
                ctx={"academic_year": academic_year, "term": term},
                telemetry=telemetry,
                boundary=boundary,
                # Already retrieved, server-side, above. Advertising it now would
                # invite a second lookup whose records were never in the contract
                # computed before generation.
                withheld_tools=withheld,
                seeded_local_results=agent_tool_results,
                seeded_provider_results=provider_tool_results,
                credit_blocks_seeded=credit_blocks_seeded,
            )
            telemetry["loop_used"] = True
            agent_tool_results = _complete_required_evidence(agent_tool_results)
            if answer == _ACTION_HANDOFF_SENTINEL:
                # Every downstream check is skipped ON PURPOSE. There is nothing
                # to ground, cite or sanitise: no model wrote this, and the text
                # is a constant in this repository. Running the contracts over it
                # would only create ways for them to reject it.
                handoff = handoff_for(agent_tool_results[-1])
                assert handoff is not None
                return {
                    "ok": True,
                    "answer": handoff.answer(_answer_language(question)),
                    "action": handoff.as_payload(),
                    "model": answer_model,
                    "usage": usage,
                    "context_summary": _context_summary(context),
                    "tool_results": agent_tool_results,
                    "verified_context": context,
                    "citations": [],
                    "cited_policy_ids": [],
                    "agent": {**telemetry, "tool_results": agent_tool_results},
                }
            # `policy_grounding` is NOT recomputed here. It was set by the
            # server-side prefetch, from the retrieval that actually happened, and
            # deriving it a second time from what the model chose to call is the
            # arrangement this change removes. `not_consulted` is no longer a
            # reachable outcome on this path; `PolicyContractState` treats it as a
            # programming failure rather than as an answer.
            #
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

        # The policy store was already consulted, above, for BOTH paths. It used
        # to be re-consulted here because this path had no tools and the loop had
        # the model decide; now retrieval happens once, before either path starts,
        # and the two share one result and one grounding state.

        context_json = json.dumps(context, ensure_ascii=False)
        # Disabling tools does not relax the boundary. This path serialises the
        # WHOLE context into one message — seeded tool results and policy evidence
        # included — so it is the largest single payload the adviser ever sends,
        # and reaching for the unprojected object here because "there are no tools
        # to project" would be the most expensive shortcut in the feature.
        prompt_context_json = json.dumps(boundary.project_context(context), ensure_ascii=False)
        user_message = {
            "role": "user",
            "content": (
                f"verified_context:\n{prompt_context_json}\n\n"
                f"answer_language: {_answer_language(question)}\n\n"
                f"latest_question:\n{question.strip()}"
            ),
        }
        messages = boundary.sanitise_messages(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                *_sanitize_history(history),
                user_message,
            ]
        )
        result: ChatResult = llm.chat(
            messages,
            model=resolved_model,
            assistant_prefill=_assistant_prefill_for_client(llm, resolved_model),
        )
        answer = result.content
        usage = result.usage
        answer_model = result.model

    # Citation check: a policy id in the answer must have been RETRIEVED this
    # request. Prompt instructions are not enforcement — a model that recites a
    # plausible id from training produces an answer that looks sourced and is not.
    # Attached here rather than up front: on the agent path the credit block only
    # exists once get_student_context has returned, so anything earlier finds nothing
    # on exactly the path that produced the uncited «حسب الدليل، الحد الأدنى 12 ساعة».
    # A BACKSTOP now, not the mechanism. The credit records are seeded before
    # generation and injected mid-loop when a tool introduces a new block; this
    # catches a block that reached neither — a shape nobody anticipated — so the
    # citation validator still knows what could have been cited. It runs after
    # generation, so anything it finds was NOT visible to the model, and the
    # answer that used those figures is uncited by construction.
    credit_citations = _credit_policy_evidence_citations(
        {"context": context, "tools": agent_tool_results}
    )
    if credit_citations:
        telemetry["credit_evidence_late"] = True
    if credit_citations:
        agent_tool_results = [*agent_tool_results, credit_citations]
        provider_tool_results = [
            *provider_tool_results,
            boundary.project_tool_result("policy_lookup", credit_citations),
        ]

    # The citation contract is checked against the LOCAL evidence. What the answer
    # was entitled to cite is a fact about what this request retrieved, not about
    # what a provider was shown — and a check run against the projection would
    # weaken with every field the projection drops.
    citations = _retrieved_citations(agent_tool_results)
    bad = _bad_citations(answer, citations)
    if bad:
        telemetry["citation_retry"] = True
        telemetry["bad_citations"] = bad
        # The correction carries the policy TEXT, not just the permitted ids. Asking
        # a model to rewrite a rule-bearing answer while showing it only a list of
        # ids leaves it nothing to rewrite FROM except its own memory — which is
        # the failure this whole feature exists to prevent.
        # The correction quotes policy TEXT back at the model, so it is outbound
        # payload and takes the provider results. The DECISION to retry, and the
        # list of what failed, came from the local check above — the boundary
        # applies to what is sent, not to what is verified.
        evidence = _policy_evidence_block(provider_tool_results)
        correction = (
            "Your draft's citations did not check out: "
            + "; ".join(
                f"{b['policy_id'] or 'page ' + str(b.get('page'))} — {b['reason']}" for b in bad
            )
            + ".\n\nThese are the ONLY policies retrieved in this conversation, with "
            "their exact text, page and id:\n"
            + (evidence or "(none — no policy was retrieved)")
            + "\n\nRewrite the answer using only these, citing as «الدليل الإرشادي "
            "للطالب، ص NN [POLICY_ID]». If none of them states what you claimed, "
            "remove the claim and say the system holds no written rule on that point. "
            "Do not substitute a different policy to keep the claim."
        )
        try:
            # Inside the try on purpose. The draft is the model's own text and may
            # contain an identifier it invented — which is exactly what the
            # grounding check below exists to catch. If the boundary refuses to
            # send it back, that is the correct outcome and the cost is one lost
            # retry: the draft stands, and a draft with bad citations is replaced
            # by the refusal a few lines down rather than shipped.
            retry_messages = boundary.sanitise_messages(
                [
                    {"role": "system", "content": SYSTEM_PROMPT_AGENT},
                    user_message,
                    {"role": "assistant", "content": answer},
                    {"role": "user", "content": correction},
                ]
            )
            corrected_cite: ChatResult = llm.chat(
                retry_messages,
                model=resolved_model,
                assistant_prefill=_assistant_prefill_for_client(llm, resolved_model),
            )
            if corrected_cite.content:
                still_bad = _bad_citations(corrected_cite.content, citations)
                telemetry["bad_citations_after_retry"] = still_bad
                # Keep whichever draft is less wrong. An unconditional overwrite lets
                # a retry that invents a NEW id replace a draft that had fewer.
                if len(still_bad) <= len(bad):
                    answer = corrected_cite.content
                    bad = still_bad
                    telemetry["citation_retry_kept"] = "retry"
                else:
                    # Recorded because it is otherwise invisible: both drafts end in
                    # the same refusal, so an operator asking "did the retry help?"
                    # has nothing to read from the answer itself.
                    telemetry["citation_retry_kept"] = "draft"
        except Exception:  # pragma: no cover - degrade to the draft answer
            logger.exception("Citation retry failed; keeping the original answer")

        if bad:
            # The system knows this answer's sourcing is wrong. Shipping it with
            # ok:true would hand a student an invented rule wearing a real-looking
            # citation — the most dangerous output this system can produce.
            logger.error(
                "Refusing to return an answer with unverifiable citations: %s",
                [b["reason"] for b in bad],
            )
            telemetry["citation_refused"] = True
            answer = (
                _CITATION_REFUSAL_AR
                if _answer_language(question) == "Arabic"
                else _CITATION_REFUSAL_EN
            )

    # ── the regulatory postcondition ─────────────────────────────
    #
    # Runs AFTER the citation retry, because that retry can introduce a fresh
    # policy id, and before the output contract, because a deterministic
    # abstention contains no identifiers and would pass the identifier gate
    # trivially either way.
    contract = build_policy_contract_state(
        question,
        agent_tool_results,
        grounding_state=telemetry["policy_grounding"],
        # The family the router already decided, so the obligation is keyed on the
        # DOMAIN of the question rather than on whether a regulated-sounding word
        # appeared in it.
        # The whole route, not the family: a MULTI_CAPABILITY question's domain is a
        # property of what it is made of. TT20 wins PLANNER_BUILD on precedence and is
        # planner DATA throughout; asking the family alone would have said the same
        # thing for a question that also fired POLICY.
        intent=route,
    )
    telemetry.update(contract.as_telemetry())
    telemetry["secondary_families"] = [str(f) for f in route.secondary_families]
    answer_language = _answer_language(question)
    policy_abstained = False

    if contract.retrieval_missing:
        # Unreachable through any normal path now that retrieval is server-side
        # and unconditional. Kept because "we never looked" must never be able to
        # become "here is the rule": if it ever happens it is a programming
        # failure, and the honest response is to say nothing rather than to let
        # the model fill the gap.
        logger.error("Policy retrieval never ran on a policy-required question.")
        telemetry["policy_contract_failure"] = "retrieval_missing"
        answer = _POLICY_ABSTENTION_AR if answer_language == "Arabic" else _POLICY_ABSTENTION_EN
    elif contract.must_abstain:
        # A rule was asked for and the store holds nothing governing. Today the
        # model answers such questions from memory; measured over the 284-question
        # corpus this is 53 questions, none of which the curated labels call
        # answerable without a policy.
        #
        # The ANSWER PROSE is still replaced wholesale, and that has not changed:
        # removing unsupported rule sentences from free text is a surgery nobody has
        # solved, and a half-edited answer is one whose remaining sentences nobody
        # has checked.
        #
        # What changed is that the verified student data is no longer DESTROYED with
        # it. The facts were never in the prose — they are the structured tool
        # results the server already holds — so `data_part` carries them out beside
        # the abstention, and the interface renders the timetable or the prerequisite
        # list it always could have. Only the regulatory CLAIM is suppressed, which
        # is the half that had no evidence.
        telemetry["policy_contract_failure"] = "no_governing_evidence"
        answer = _POLICY_ABSTENTION_AR if answer_language == "Arabic" else _POLICY_ABSTENTION_EN
        policy_abstained = True
    elif contract.missing_governing_citation(
        _policy_ids_in_text(answer) & contract.citable_policy_ids
    ):
        # Governing evidence EXISTS and the answer cited none of it. Citing a
        # background record does not satisfy this: the id is real, it was
        # retrieved this request, and it passes every check the citation
        # validator makes — which is exactly why the test is against
        # `direct_policy_ids`.
        telemetry["policy_contract_retry"] = True
        direct_block = _policy_evidence_block(
            provider_tool_results, only=contract.direct_policy_ids
        )
        correction = (
            "Your answer states a university rule without citing the record that "
            "governs it. These are the ONLY governing records retrieved for this "
            "question:\n" + (direct_block or "(none)") + "\n\nRewrite the answer citing "
            "at least one of them as «الدليل الإرشادي للطالب، ص NN [POLICY_ID]». Do not "
            "cite any other id. If none of them supports the claim, remove the claim."
        )
        try:
            retry_messages = boundary.sanitise_messages(
                [
                    {"role": "system", "content": SYSTEM_PROMPT_AGENT},
                    user_message,
                    {"role": "assistant", "content": answer},
                    {"role": "user", "content": correction},
                ]
            )
            corrected_policy: ChatResult = llm.chat(
                retry_messages,
                model=resolved_model,
                assistant_prefill=_assistant_prefill_for_client(llm, resolved_model),
            )
            candidate = corrected_policy.content or ""
        except Exception:
            logger.exception("Policy-contract retry failed")
            candidate = ""
        still_missing = not candidate or contract.missing_governing_citation(
            _policy_ids_in_text(candidate) & contract.citable_policy_ids
        )
        if still_missing:
            telemetry["policy_contract_failure"] = "no_governing_citation"
            answer = _CITATION_REFUSAL_AR if answer_language == "Arabic" else _CITATION_REFUSAL_EN
        else:
            answer = candidate
            # The rewrite is a new answer, so the citations it makes are re-checked
            # rather than inherited from the draft that was replaced.
            late_bad = _bad_citations(answer, citations)
            if late_bad:
                telemetry["citation_refused"] = True
                answer = (
                    _CITATION_REFUSAL_AR if answer_language == "Arabic" else _CITATION_REFUSAL_EN
                )

    # ── the output contract ──────────────────────────────────────
    #
    # Detect -> retry -> RE-VALIDATE -> ship only if clean, refuse otherwise.
    #
    # The re-validation is the part that was missing, and its absence was not a
    # gap in coverage but an inversion. The old sequence kept the original draft
    # when the retry raised, and accepted the corrected draft without looking at
    # it — so a request that had PROVEN its answer contained an unverified
    # identifier could return that exact answer, or return a correction that
    # invented a fresh one. The system detected the fault and then shipped it.
    #
    # There is no "degrade to the draft" outcome here. Once the contract has
    # failed, the draft is known-bad; the only safe fallbacks are a clean rewrite
    # or a deterministic refusal.
    evidence_texts = [
        context_json,
        question,
        *(json.dumps(item, ensure_ascii=False, default=str) for item in agent_tool_results),
    ]
    is_student = principal.role == ROLE_STUDENT
    violations = _output_contract_violations(
        answer,
        evidence_texts=evidence_texts,
        boundary=boundary,
        is_student=is_student,
        tool_results=tool_results,
        # `recommendation_policy` lives here, not in the tool results, and the credit
        # check needs it to tell an advisory cap from a contradiction.
        context=context,
    )
    if violations:
        # `grounding_retry` keeps its name: stored turns, the eval battery and the
        # conversation UI already read it.
        telemetry["grounding_retry"] = True
        telemetry["output_violations"] = violations
        # THE REJECTED DRAFT, sanitised, as diagnostic telemetry only. Two paid runs
        # were spent guessing which number tripped the credit check, because the
        # refusal replaced the text that contained it. The student still receives the
        # refusal; this rides on the trace, which is gitignored.
        #
        # It goes through the SAME sanitiser the transport uses, so a draft cannot
        # leak by the diagnostic door what the boundary refuses at the front — and it
        # is capped, because a diagnostic that carries the whole answer is the answer.
        telemetry.setdefault("rejected_drafts", []).append(
            {
                "attempt": len(telemetry.get("rejected_drafts") or []) + 1,
                "violations": violations,
                "safe_excerpt": _safe_excerpt(boundary, answer),
            }
        )
        corrected_answer: str | None = None
        try:
            retry_messages = boundary.sanitise_messages(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    user_message,
                    {"role": "assistant", "content": answer},
                    {
                        "role": "user",
                        "content": _output_correction(
                            violations,
                            boundary,
                            []
                            if boundary.is_remote
                            else _unverified_student_ids(answer, evidence_texts),
                        ),
                    },
                ]
            )
            corrected: ChatResult = llm.chat(
                retry_messages,
                model=resolved_model,
                assistant_prefill=_assistant_prefill_for_client(llm, resolved_model),
            )
            corrected_answer = corrected.content or None
        except Exception:
            # Broad on purpose. The fallback is a refusal, so an unexpected error
            # costs a cautious answer rather than an unsafe one — and narrowing
            # this to LLMError would let a serialisation bug propagate as a 500
            # on a path whose whole job is to fail safely.
            logger.exception("Output-contract retry failed")
            telemetry["grounding_retry_failed"] = True

        remaining = (
            _output_contract_violations(
                corrected_answer,
                evidence_texts=evidence_texts,
                boundary=boundary,
                is_student=is_student,
                # The SAME evidence. Re-validating without it would check the
                # corrected answer against nothing, and the retry would launder
                # every consistency violation away.
                tool_results=tool_results,
                context=context,
            )
            if corrected_answer
            else violations
        )
        if remaining:
            logger.error("Refusing an answer that failed the output contract: %s", remaining)
            telemetry["grounding_refused"] = True
            telemetry["output_violations_after_retry"] = remaining
            answer = (
                _GROUNDING_REFUSAL_AR
                if _answer_language(question) == "Arabic"
                else _GROUNDING_REFUSAL_EN
            )
        else:
            answer = corrected_answer

    return {
        "ok": True,
        "answer": answer,
        #: Present on every response so a caller can test one key rather than
        #: two shapes. `None` means "no route was offered".
        "action": None,
        "model": answer_model,
        "usage": usage,
        "context_summary": _context_summary(context),
        "tool_results": tool_results,
        "verified_context": context,
        # The structured citation contract. `citations` is what the answer was
        # ENTITLED to cite; `cited_policy_ids` is what it actually did. A judge or a
        # UI can check one against the other without re-parsing the prose.
        "citations": citations,
        "cited_policy_ids": sorted(
            _policy_ids_in_text(answer) & {c["policy_id"] for c in citations}
        ),
        # ── the two halves, kept apart ──────────────────────────────────────
        #
        # A turn can owe a rule AND report the student's own record, and those two
        # obligations fail independently. When the store holds nothing governing,
        # the regulatory CLAIM is suppressed — the prose above is the abstention —
        # but the verified facts are not destroyed with it: they were never in the
        # prose to begin with, they are the structured tool results, and the
        # interface can render the timetable or the prerequisite list unchanged.
        #
        # `data_part.facts` is the tool results by NAME, not the raw list, so a
        # consumer reads `facts["my_progress"]` instead of indexing a position.
        "data_part": {
            "status": "ANSWERED" if tool_results else "NOT_ATTEMPTED",
            "facts": {
                str(r.get("tool") or f"result_{i}"): r
                for i, r in enumerate(tool_results)
                if isinstance(r, dict)
            },
        },
        "policy_part": {
            "status": "ABSTAINED" if policy_abstained else "ANSWERED",
            "evidence": citations,
        },
        "agent": {
            **telemetry,
            "tool_results": agent_tool_results,
            # Read off the CLIENT, which counted them at the socket. The runner used
            # to add one per question; a tool-driven answer is two to four outbound
            # calls and a retried one is more, so a per-question count made every
            # cost and budget figure wrong in the same direction.
            "provider_http_calls": int(getattr(llm, "http_calls", 0) or 0),
            "successful_provider_responses": int(getattr(llm, "http_responses", 0) or 0),
            # THREE LISTS, because they mean three different things and conflating
            # them would report the server's work as the model's. TT20's provider
            # called one capability; the route required two, so the server fetched
            # the second. `executed_evidence_tools` is what the answer actually rests
            # on; `model_tools_called` is what the model chose. Reporting one number
            # would either fail a well-served answer or hide a model that stopped
            # choosing — and the gap between them is the metric worth watching.
            "model_tools_called": [
                t.get("name") for t in (telemetry.get("tools_called") or []) if isinstance(t, dict)
            ],
            "executed_evidence_tools": sorted(
                {r.get("tool") for r in agent_tool_results if isinstance(r, dict) and r.get("tool")}
            ),
        },
    }

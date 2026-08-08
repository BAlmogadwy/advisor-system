"""Student Advisor V2: one agent over a deliberately small, read-only surface.

This runtime is intentionally separate from ``virtual_advisor``.  The old runtime
classifies a question into intent families before the model sees it.  V2 does not:
one conversational agent decides which academic evidence it needs, calls the
corresponding read-only capabilities, observes the results, and answers.

Product invariant
-----------------
Nothing reachable from this module registers a course, saves a timetable, applies
a section mapping, or writes to the university portal.  The adviser can prepare a
proposal in chat or on the planner screen.  The student decides whether to reproduce it
manually in the university's main portal.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from django.conf import settings

from core.models import Student
from core.services.advisor_presentations import (
    graduation_presentation_from_tool_results,
    timetable_presentation_from_tool_results,
)
from core.services.advisor_principal import AdvisorPrincipal
from core.services.advisor_remote_boundary import boundary_for_scope
from core.services.llm_backend import LLMError, UsageTotals, get_llm_client
from core.services.policy_contract import requires_policy_contract
from core.services.rbac import ROLE_STUDENT
from core.services.virtual_advisor import (
    _answer_language,
    _assistant_prefill_for_client,
    _bad_citations,
    _credit_policy_evidence_citations,
    _fabricated_policy_ids,
    _policy_evidence_for_prompt,
    _retrieved_citations,
    _sanitize_history,
    _seed_policy_evidence,
    _summarise_tool_args,
    _tool_message,
)
from core.services.virtual_advisor_capabilities import get_default_registry

# Stable ordering keeps the tool list and prompt/test behavior reproducible. These names are
# existing, audited capability implementations. Timetable access is limited to
# read-only inspection and proposal generation; the legacy action tool stays out.
STUDENT_V2_TOOL_NAMES: tuple[str, ...] = (
    "get_student_context",
    "my_progress",
    "my_plan_by_term",
    "my_timetable",
    "my_clash_free_sections",
    "build_timetable_proposal",
    "lookup_course",
    "course_prerequisites",
    "why_course_locked",
    "recommend_courses",
    "graduation_progress",
    "policy_lookup",
    "my_advisor",
)

FORBIDDEN_STUDENT_V2_TOOLS = frozenset(
    {
        "build_my_timetable",
        "planner_build",
        "save_student_sections",
        "register_courses",
    }
)

SYSTEM_PROMPT = """You are one conversational academic adviser for a university student.
You are not a collection of roles and you do not classify the student into a chatbot mode.
Understand the student's goal, gather the minimum verified evidence needed, and give a
clear, practical answer.

Operating rules:
- Answer in the language named in the latest user message.
- Use tools for every claim about this student's record, degree plan, prerequisites,
  recommendations, credit position, graduation outlook, adviser, or university rules.
- You may call several tools and combine their evidence. Ask one short clarification only
  when an essential course, term, or choice is genuinely missing.
- "Best section" is not defined without a course and a preference. If either is missing,
  ask for the course code and whether the student prefers fewer days, a later start, or an
  earlier finish. Never label the fixed current sections as "best" merely because they do
  not clash.
- Distinguish prerequisite readiness from permission to register. A satisfied prerequisite
  does not prove that it is the only registration condition, that a course is offered, has
  a seat, or may be registered now.
- This system has no course grades, marks, or attendance records. You may explain a formula
  and calculate from figures the student types, but never offer to retrieve those records or
  imply that you can read them.
- recommend_courses separates genuinely new recommendations from
  already_in_current_timetable. Never offer a course in the latter list as something the
  student can add. If the new recommendation list is empty, say that this system currently
  has no additional recommended course; do not repeat the student's existing courses and
  do not speculate that courses are closed/unavailable or that a credit cap caused it.
- Section catalogue meetings do not label or separate lecture and laboratory components.
  You may compare whole sections and their times after receiving a course code, but never
  decide whether a lab can be changed independently from its lecture or whether the two are
  linked. State that this needs confirmation from the academic department or adviser.
- For a request about a named section of a named course, call my_clash_free_sections.
  Read currently_registered_sections and is_current_section: if the requested section is
  already in the current timetable, say so directly. Never report that section data is
  missing when the tool returned sections_on_file greater than zero. Call catalogue rows
  "recorded sections", not "available sections": this result has no seat-availability fact.
- Recommendations are proposals shown in this system. This system NEVER registers a real
  course, saves/applies a university timetable, reserves a seat, or changes the main portal.
  If asked to register, save, apply, or confirm courses, explain that you can prepare and
  check a proposal here; the student must enter any chosen courses manually in the
  university's main portal.
- You CAN read the current timetable, inspect clash-free sections, and build real
  clash-checked timetable proposals from the section catalogue. For any request to build,
  create, rebuild, arrange, or show timetable alternatives, call
  build_timetable_proposal. Use mode=around_current when current sections must stay fixed
  and mode=from_scratch when the student asks for a fresh arrangement. Never say that
  timetable times or clash detection are unavailable when this tool can answer.
- When build_timetable_proposal returns alternatives, name each distinct option with its
  exact Planner identity from planner_options (A1-A3, B1-B3, C1-C3), coverage, and important
  differences. Several Planner identities on one alternative mean those generator runs found
  the same timetable; say so rather than presenting duplicates.
  Always print the Planner identity and scheduled_courses/target_courses coverage, even when
  an around-current option placed zero additions.
  Clearly separate current retained sections from proposed additions. Report each option's
  own unplaced_courses and reasons, because partial coverage can differ by option.
  OMITTED_IN_THIS_VARIANT means another generated option placed that course; never describe
  it as not offered, unavailable, or absent from the catalogue. Explain every unplaced
  reason in natural student-facing language and never print internal reason_code labels.
  It also does not prove that a full clash-free arrangement is impossible: another returned
  option may already cover every target. Describe only what each returned variant did, and
  never claim that the finite A1-C3 search exhausts every possible section combination.
  Never summarise an unplaced course as a clash unless its returned reason explicitly says
  it clashes; NOT_ON_FILE means only that no section is recorded in this system's data.
  The chat interface receives a complete structured timetable card from the same verified
  evidence. Keep the prose concise and do not repeat every section/day/time row; the card
  shows all details and is read-only.
  Do not invent an option, section, meeting time, seat count, or live availability.
- When build_timetable_proposal returns no_additional_courses=true, it means there was no
  requested or recommended target course left to add. Say that the current timetable is
  retained and no additional course was proposed. Do not invent Planner identities, call
  current_credit_hours/credit_ceiling scheduled coverage, or describe this as a failed clash
  search. Offer to try a named course if the student wants one checked.
- Never claim that an action was completed in the university portal.
- For a university rule, call policy_lookup and use only direct_policy_evidence. If none
  governs the question, say the available guide does not state the rule. Cite a governing
  policy exactly as «الدليل الإرشادي للطالب، ص NN [POLICY_ID]», using the returned page and
  id. Never invent a rule, page, deadline, limit, approval, or policy id.
- For a pure timetable or student-record request, do not add a policy-gap disclaimer or
  adviser referral merely because the prefetched policy evidence is empty. Answer from the
  verified timetable or student tools and keep the response focused on the student's goal.
- Keep every rule within its exact subject. A procedure for final-grade appeals does not
  become a procedure for attendance, deprivation, or another neighbouring issue. Explain
  adjacent evidence only as adjacent and say when applicability is not stated.
- A definition of an elective category and its required units does not establish whether a
  failed elective must be repeated or may be substituted. Unless a direct policy states that
  consequence, say that the available source does not settle it and refer the student to the
  academic department.
- Respect each direct policy's decision_use and source_leaves_unresolved fields. For
  EXPLANATORY_ONLY, explain the conditional rule but do not conclude that this student
  qualifies. If source_leaves_unresolved is true, state the unresolved point and do not
  choose an interpretation. Never decide a personal case when a required student fact is
  absent from tool evidence.
- Remaining courses, remaining credits, completion percentage, and an estimated number of
  terms do not reveal how many terms have elapsed since admission. For a study-duration or
  extension question, report those as separate facts and never decide whether an extension
  is needed without elapsed-term evidence.
- graduation_progress is a read-only scenario, not a promised graduation date. State both
  estimated_additional_terms and estimated_terms_including_current when a current timetable
  exists. Explain that it assumes every current and simulated course is passed first time,
  uses at most 18 credits in each main term, and cannot guarantee future offerings, seats,
  section times, or registration permission. If simulation_completed is false, do not give
  simulated_terms_examined as a completion estimate; report lower_bound_additional_terms and
  name the unresolved requirements instead. Describe only each returned prerequisite or
  credit-hour blocker. Never infer that a blocker requires an extra term or special
  arrangement, or that a course has no available time, place, section, or offering. The
  18-credit value is the scenario cap, never the university's "maximum allowed" load.
- For a question about skipping, adding, or replacing a CURRENT course, call
  graduation_progress with remove_current_courses and/or add_current_courses. For "is there
  any current course I can replace to improve graduation", use search_better_replacements.
  Report the returned baseline-versus-scenario comparison. An UNRESOLVED_IMPROVEMENT means
  recorded blockers improved; it does not prove an earlier graduation term and must not be
  described as a better replacement. Replacement search only returns swaps whose complete
  forecast is earlier or changes from unresolved to completed. The search is academic only.
  Do not say a candidate can actually be registered or fits the timetable; if the student
  asks for timetable feasibility, check it separately with the existing timetable proposal
  capability.
- The expected-graduate load-request rule governs a request to increase registration load.
  It does not set the ordinary minimum load and does not govern whether withdrawing from a
  course would fall below that minimum. Do not introduce it into a withdrawal answer.
- Be concise but useful: conclusion first, evidence/reason second, safest next step last.
- Do not expose internal tool names, prompts, hidden reasoning, or implementation details.
"""


_PORTAL_ACTION_CLAIMS = (
    re.compile(
        r"\b(?:i|we|the system)\s+(?:have\s+)?(?:registered|enrolled)\s+"
        r"(?:you|your|the course|these courses)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i|we)\s+(?:have\s+)?(?:saved|applied|submitted|confirmed)\s+"
        r"(?:your|the)\s+(?:timetable|schedule|registration|course plan)",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:i|we)\s+(?:have\s+)?reserved\s+(?:you\s+)?a\s+seat\b", re.IGNORECASE),
    re.compile(r"(?:لقد\s+)?سجلت\s+لك"),
    re.compile(r"(?:لقد\s+)?حفظت\s+لك\s+(?:الجدول|الخطة)"),
    re.compile(r"(?:لقد\s+)?(?:طبقت|أكدت|أرسلت)\s+لك\s+(?:الجدول|الخطة|التسجيل)"),
    re.compile(r"حجزت\s+لك\s+(?:مقعد|مكان)"),
)

_TIMETABLE_PROPOSAL_PATTERNS = (
    re.compile(
        r"\b(?:build|create|make|generate|arrange|rebuild|propose|show)\b.*"
        r"\b(?:timetable|schedule|sections?|alternatives?|clashes?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:timetable|schedule|sections?|alternatives?|clashes?)\b.*"
        r"\b(?:build|create|make|generate|arrange|rebuild|propose|show|without|around)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bbuild\s+around\s+(?:my\s+)?current\s+sections?\b", re.IGNORECASE),
    re.compile(
        r"(?:ابن|ابني|أبني|تبني|نبني|بناء|أنشئ|انشئ|كوّن|كون|رتب|اقترح|اعرض|جدول)"
        r".*(?:جدول|شعب|تعارض|بدائل)"
    ),
    re.compile(r"(?:جدول|شعب|تعارض).*(?:ابن|أنشئ|انشئ|كوّن|كون|رتب|اقترح|اعرض)"),
    re.compile(r"(?:لا\s+تغي[ّ]?ر|ثب[ّ]?ت).*?الشعب.*?(?:غي[ّ]?ر|بد[ّ]?ل).*?المقررات"),
)


def _requires_timetable_proposal(question: str) -> bool:
    return any(pattern.search(question or "") for pattern in _TIMETABLE_PROPOSAL_PATTERNS)


_TIMETABLE_CREDIT_CAP_PATTERNS = (
    re.compile(
        r"(?:بحد\s+أقصى|حد(?:اً|ا)?\s+أقصاه|لا\s+(?:يتجاوز|تتجاوز|يزيد\s+عن)|"
        r"لا\s+تبن(?:ي)?\b.*?(?:يتجاوز|فوق))\s*"
        r"(?P<cap>\d{1,2})\s*(?:ساعة|ساعات|وحدة|وحدات)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:at\s+most|maximum(?:\s+of)?|max(?:imum)?|no\s+more\s+than|"
        r"do\s+not\s+exceed|don['’]t\s+exceed)\s*(?P<cap>\d{1,2})\s*"
        r"(?:credits?|hours?)?",
        re.IGNORECASE,
    ),
)
_TIMETABLE_FROM_SCRATCH_PATTERN = re.compile(
    r"(?:\bfrom\s+scratch\b|\bignore\s+(?:all\s+)?(?:my\s+)?current\b|"
    r"من\s+الصفر|تجاهل.*?(?:جدول|شعب).*?الحالي)",
    re.IGNORECASE,
)
_TIMETABLE_AROUND_CURRENT_PATTERN = re.compile(
    r"(?:\baround\s+(?:my\s+)?current\b|\bkeep\s+(?:my\s+)?current\b|"
    r"(?:احتفظ|خل[ّ]?|خلي|ثب[ّ]?ت|لا\s+تغي[ّ]?ر).*?(?:الشعب|الجدول).*?الحالي|"
    r"(?:احتفظ|خل[ّ]?|خلي|ثب[ّ]?ت|لا\s+تغي[ّ]?ر).*?الشعب.*?(?:كما\s+ه[يو]|مثل\s+ما\s+ه[يو])|"
    r"(?:الشعب|الجدول).*?الحالي.*?(?:كما\s+ه[يو]|مثل\s+ما\s+ه[يو]))",
    re.IGNORECASE,
)


def _normalise_timetable_proposal_args(
    question: str, arguments: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Carry explicit mode and credit choices into the existing planner tool."""
    text = str(question or "")
    normalised = dict(arguments or {})
    reasons: list[str] = []

    if _TIMETABLE_FROM_SCRATCH_PATTERN.search(text):
        if normalised.get("mode") != "from_scratch":
            reasons.append("explicit_from_scratch")
        normalised["mode"] = "from_scratch"
    elif _TIMETABLE_AROUND_CURRENT_PATTERN.search(text):
        if normalised.get("mode") != "around_current":
            reasons.append("explicit_around_current")
        normalised["mode"] = "around_current"

    for pattern in _TIMETABLE_CREDIT_CAP_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        cap = int(match.group("cap"))
        if normalised.get("max_credits") != cap:
            reasons.append("explicit_credit_cap")
        normalised["max_credits"] = cap
        break

    return normalised, reasons


_INTERNAL_OUTPUT_MARKERS = re.compile(
    r"(?:source_leaves_unresolved|decision_use|PROHIBITED_FOR_DECISION|"
    r"PARTIALLY_EVALUABLE|PERMITTED_WITH_USER_PROVIDED_INPUTS|EXPLANATORY_ONLY|"
    r"reason_code|NOT_ON_FILE|OMITTED_IN_THIS_VARIANT|"
    r"listed_as_prerequisite_for|sole_remaining_prerequisite(?:_for)?|"
    r"on_prerequisite_chain_of|build_timetable_proposal|recommend_courses|"
    r"graduation_progress|my_clash_free_sections|my_timetable|my_progress|"
    r"my_plan_by_term|get_student_context|lookup_course|course_prerequisites|"
    r"why_course_locked|policy_lookup|my_advisor|max_credits|"
    r"around_current|from_scratch)",
    re.IGNORECASE,
)


def _internal_output_markers(answer: str) -> list[str]:
    return sorted({match.group(0) for match in _INTERNAL_OUTPUT_MARKERS.finditer(answer or "")})


def _humanise_internal_output_markers(answer: str, language: str) -> str:
    """Last-resort cleanup after a bounded rewrite still leaks schema labels."""
    replacements_ar = {
        "source_leaves_unresolved": "المصدر يترك هذه النقطة غير محسومة",
        "decision_use": "حدود استخدام الدليل",
        "prohibited_for_decision": "لا يمكن استخدام القاعدة للحكم على الحالة الفردية",
        "partially_evaluable": "يمكن التحقق من جزء من الحالة فقط",
        "permitted_with_user_provided_inputs": "يتطلب التحقق بيانات يقدمها الطالب",
        "explanatory_only": "قاعدة تفسيرية فقط",
        "reason_code": "سبب النتيجة",
        "not_on_file": "غير مسجل في بيانات النظام",
        "omitted_in_this_variant": "غير مدرج في هذا البديل فقط",
        "listed_as_prerequisite_for": "مقررات تذكره كمتطلب سابق",
        "sole_remaining_prerequisite": "المتطلب السابق الوحيد المتبقي",
        "sole_remaining_prerequisite_for": "مقررات لا يفصلها عن الفتح إلا هذا المتطلب",
        "on_prerequisite_chain_of": "مقررات يقع ضمن سلسلة متطلباتها",
        "build_timetable_proposal": "منشئ مقترح الجدول",
        "recommend_courses": "محرك توصية المقررات",
        "graduation_progress": "محاكاة التقدم نحو التخرج",
        "my_clash_free_sections": "فحص الشعب غير المتعارضة",
        "my_timetable": "جدولك الحالي",
        "my_progress": "تقدمك الأكاديمي",
        "my_plan_by_term": "خطة المقررات حسب الفصل",
        "get_student_context": "بياناتك الأكاديمية",
        "lookup_course": "بيانات المقرر",
        "course_prerequisites": "متطلبات المقرر",
        "why_course_locked": "تحليل سبب عدم إتاحة المقرر",
        "policy_lookup": "مرجع السياسات",
        "my_advisor": "بيانات المرشد",
        "max_credits": "الحد الأقصى للساعات",
        "around_current": "البناء حول الجدول الحالي",
        "from_scratch": "البناء من الصفر",
    }
    replacements_en = {
        "source_leaves_unresolved": "the source leaves this point unresolved",
        "decision_use": "the evidence-use boundary",
        "prohibited_for_decision": "the rule cannot decide the individual case",
        "partially_evaluable": "only part of the case can be checked",
        "permitted_with_user_provided_inputs": "the check requires student-provided inputs",
        "explanatory_only": "an explanatory rule only",
        "reason_code": "the reason",
        "not_on_file": "not recorded in this system's data",
        "omitted_in_this_variant": "omitted only from this alternative",
        "listed_as_prerequisite_for": "courses that list it as a prerequisite",
        "sole_remaining_prerequisite": "the sole remaining prerequisite",
        "sole_remaining_prerequisite_for": "courses for which it is the sole remaining prerequisite",
        "on_prerequisite_chain_of": "courses whose prerequisite chain includes it",
        "build_timetable_proposal": "the timetable proposal builder",
        "recommend_courses": "the course recommendation engine",
        "graduation_progress": "the graduation-progress simulation",
        "my_clash_free_sections": "the clash-free section check",
        "my_timetable": "your current timetable",
        "my_progress": "your academic progress",
        "my_plan_by_term": "your term-by-term plan",
        "get_student_context": "your academic context",
        "lookup_course": "the course record",
        "course_prerequisites": "the course prerequisites",
        "why_course_locked": "the course-lock analysis",
        "policy_lookup": "the policy reference",
        "my_advisor": "your adviser record",
        "max_credits": "the maximum credit limit",
        "around_current": "building around the current timetable",
        "from_scratch": "building from scratch",
    }
    replacements = replacements_ar if language == "Arabic" else replacements_en
    text = str(answer or "")
    unresolved = replacements["source_leaves_unresolved"]
    prohibited = replacements["prohibited_for_decision"]
    text = re.sub(
        r"`?source_leaves_unresolved\s*:\s*true`?",
        unresolved,
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"`?decision_use\s*:\s*PROHIBITED_FOR_DECISION`?",
        prohibited,
        text,
        flags=re.IGNORECASE,
    )
    text = _INTERNAL_OUTPUT_MARKERS.sub(
        lambda match: replacements.get(match.group(0).lower(), "student-facing evidence"),
        text,
    )
    return re.sub(r"`([^`\n]+)`", r"\1", text)


_SECTION_REQUEST_WORD_PATTERN = re.compile(
    r"(?:\bsection\b|شعب(?:ة|ه|تي|تك|ته|تها|هم)?)", re.IGNORECASE
)
_SECTION_CODE_TOKEN_PATTERN = re.compile(r"\b[A-Z]\d{1,3}\b", re.IGNORECASE)


def _requires_section_check(question: str) -> bool:
    """Whether a named-course question requires recorded-section evidence.

    This is the same kind of grounding gate as timetable/graduation checks below,
    not an intent router. It prevents a repeated question from being answered by
    copying an earlier assistant mistake out of conversation history.
    """
    text = str(question or "")
    return bool(
        _COURSE_CODE_TOKEN_PATTERN.search(text)
        and (_SECTION_REQUEST_WORD_PATTERN.search(text) or _SECTION_CODE_TOKEN_PATTERN.search(text))
    )


_GRADUATION_PROGRESS_PATTERN = re.compile(
    r"(?:\bgraduat(?:e|ing|ion)\b|تخر(?:ج|ّج)|التخرج|"
    r"(?:مدة|موعد|وقت).*?إنهاء\s+(?:خطتي|الخطة|متطلبات\s+الخطة))",
    re.IGNORECASE,
)


def _requires_graduation_progress(question: str) -> bool:
    return bool(_GRADUATION_PROGRESS_PATTERN.search(question or ""))


_COURSE_CODE_EXPR = r"[A-Z]{2,6}\s*-?\s*\d{1,4}"
_COURSE_CODE_TOKEN_PATTERN = re.compile(rf"\b{_COURSE_CODE_EXPR}\b", re.IGNORECASE)
_CURRENT_COURSE_CHANGE_PATTERN = re.compile(
    r"(?:\b(?:do\s+not|don['’]t|did\s+not|didn['’]t|not)\s+take\b|"
    r"\b(?:skip|drop|remove|replace|swap|defer)\b|\binstead\s+of\b|"
    r"(?:ما\s*(?:آخذ|اخذ|أخذت|اخذت)|لم\s+(?:آخذ|اخذ)|بدل|استبدل|أستبدل|استبدال|"
    r"أبدل|ابدل|أحذف|احذف|حذف|أؤجل|اؤجل|تأجيل|أترك|اترك))",
    re.IGNORECASE,
)
_OPEN_REPLACEMENT_PATTERN = re.compile(
    r"(?:\b(?:which|what|any)\s+(?:current\s+)?course\b.*\b(?:replace|swap)\b|"
    r"\b(?:replace|swap)\b.*\b(?:which|what|any)\s+(?:current\s+)?course\b|"
    r"(?:أي|اي|فيه|هناك|يوجد).*?(?:مقرر|مادة).*?(?:استبدل|أستبدل|أبدل|ابدل)|"
    r"(?:استبدل|أستبدل|أبدل|ابدل).*?(?:أي|اي|مقرر|مادة))",
    re.IGNORECASE,
)


def _requires_graduation_what_if(question: str) -> bool:
    """Require scenario-bearing evidence for a current-course change question.

    This is an evidence gate, not an intent router: the model still chooses the
    exact tool arguments. The gate only prevents a baseline graduation report
    from being presented as the answer to an explicit skip/swap question.
    """
    text = str(question or "")
    if not _requires_graduation_progress(text):
        return False
    return bool(
        _OPEN_REPLACEMENT_PATTERN.search(text)
        or (_COURSE_CODE_TOKEN_PATTERN.search(text) and _CURRENT_COURSE_CHANGE_PATTERN.search(text))
    )


_ARABIC_INSTEAD_PATTERN = re.compile(
    rf"(?P<add>\b{_COURSE_CODE_EXPR}\b)\s+"
    rf"(?:بدل(?:اً|ا)?(?:\s+من)?|عوض(?:اً|ا)?\s+عن)\s+"
    rf"(?P<remove>\b{_COURSE_CODE_EXPR}\b)",
    re.IGNORECASE,
)
_ENGLISH_INSTEAD_PATTERN = re.compile(
    rf"(?P<add>\b{_COURSE_CODE_EXPR}\b)\s+instead\s+of\s+" rf"(?P<remove>\b{_COURSE_CODE_EXPR}\b)",
    re.IGNORECASE,
)
_ENGLISH_REPLACE_PATTERN = re.compile(
    rf"\b(?:replace|swap)\s+(?P<remove>{_COURSE_CODE_EXPR})\s+"
    rf"(?:with|for)\s+(?P<add>{_COURSE_CODE_EXPR})\b",
    re.IGNORECASE,
)
_ARABIC_REPLACE_PATTERN = re.compile(
    rf"(?:استبدل|أستبدل|أبدل|ابدل)\s+(?P<remove>\b{_COURSE_CODE_EXPR}\b)\s+"
    rf"(?:ب|بـ|مع)\s*(?P<add>\b{_COURSE_CODE_EXPR}\b)",
    re.IGNORECASE,
)
_ARABIC_OMISSION_PATTERN = re.compile(
    rf"(?:ما\s*(?:آخذ|اخذ|أخذت|اخذت)|لم\s+(?:آخذ|اخذ)|"
    rf"أحذف|احذف|حذف|أؤجل|اؤجل|أترك|اترك)\s+"
    rf"(?:مقرر\s+|مادة\s+)?(?P<remove>\b{_COURSE_CODE_EXPR}\b)",
    re.IGNORECASE,
)
_ENGLISH_OMISSION_PATTERN = re.compile(
    rf"\b(?:do\s+not|don['’]t|did\s+not|didn['’]t)\s+take\s+"
    rf"(?P<remove>{_COURSE_CODE_EXPR})\b|"
    rf"\b(?:skip|drop|remove|defer)\s+(?P<remove_action>{_COURSE_CODE_EXPR})\b",
    re.IGNORECASE,
)


def _normalise_course_code(value: Any) -> str:
    return re.sub(r"[\s-]+", "", str(value or "")).upper()


def _normalise_graduation_scenario_args(
    question: str, arguments: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """Correct explicit scenario direction before the read-only tool executes.

    Arabic colloquial negation such as ``ما أخذت DS225`` was interpreted by
    both tested models as an addition. Only high-confidence surface forms are
    normalised here; the graduation engine remains responsible for validating
    current registration, prerequisites, plan membership, and the 18-hour cap.
    """
    text = str(question or "")
    normalised = dict(arguments or {})
    for key in ("remove_current_courses", "add_current_courses"):
        values = normalised.get(key)
        if isinstance(values, list):
            normalised[key] = [
                code for code in (_normalise_course_code(value) for value in values) if code
            ]

    for pattern in (
        _ARABIC_INSTEAD_PATTERN,
        _ENGLISH_INSTEAD_PATTERN,
        _ENGLISH_REPLACE_PATTERN,
        _ARABIC_REPLACE_PATTERN,
    ):
        match = pattern.search(text)
        if match:
            normalised["remove_current_courses"] = [_normalise_course_code(match.group("remove"))]
            normalised["add_current_courses"] = [_normalise_course_code(match.group("add"))]
            normalised.pop("search_better_replacements", None)
            return normalised, "explicit_replacement"

    if _OPEN_REPLACEMENT_PATTERN.search(text) and not _COURSE_CODE_TOKEN_PATTERN.search(text):
        normalised.pop("remove_current_courses", None)
        normalised.pop("add_current_courses", None)
        normalised["search_better_replacements"] = True
        return normalised, "open_replacement_search"

    omission = _ARABIC_OMISSION_PATTERN.search(text) or _ENGLISH_OMISSION_PATTERN.search(text)
    if omission:
        removed = _normalise_course_code(
            omission.groupdict().get("remove") or omission.groupdict().get("remove_action")
        )
        if removed:
            normalised["remove_current_courses"] = [removed]
            additions = normalised.get("add_current_courses")
            if isinstance(additions, list):
                filtered = [code for code in additions if code != removed]
                if filtered:
                    normalised["add_current_courses"] = filtered
                else:
                    normalised.pop("add_current_courses", None)
            normalised.pop("search_better_replacements", None)
            return normalised, "explicit_omission"

    return normalised, ""


_SECTION_DATA_MISSING_CLAIM = re.compile(
    r"(?:لا\s+توجد\s+(?:شعب|بيانات)|"
    r"لا\s+(?:يحتوي|يوجد).*?(?:سجل|شعب)|"
    r"غير\s+مسجل(?:ة|ه)?.*?(?:شعب|النظام)|"
    r"\bno\s+(?:recorded\s+)?sections?\b|"
    r"\bno\s+section\s+data\b|"
    r"\bnot\s+(?:recorded|on\s+file|in\s+the\s+system)\b|"
    r"\bNOT_ON_FILE\b)",
    re.IGNORECASE | re.DOTALL,
)


def _verified_section_results(tool_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for result in tool_results
        if result.get("tool") == "my_clash_free_sections" and result.get("ok")
        for row in result.get("courses") or []
        if isinstance(row, dict)
    ]


def _section_answer_contradicts_evidence(answer: str, tool_results: list[dict[str, Any]]) -> bool:
    has_recorded_sections = any(
        int(row.get("sections_on_file") or 0) > 0 for row in _verified_section_results(tool_results)
    )
    return has_recorded_sections and bool(_SECTION_DATA_MISSING_CLAIM.search(answer or ""))


def _safe_section_answer(language: str, tool_results: list[dict[str, Any]]) -> str:
    """Deterministic last line of defence for verified section evidence."""
    result = next(
        (
            row
            for row in reversed(tool_results)
            if row.get("tool") == "my_clash_free_sections" and row.get("ok")
        ),
        None,
    )
    if not result:
        return ""
    term = str(result.get("compared_against_term") or "").strip()
    lines: list[str] = []
    for course in result.get("courses") or []:
        if not isinstance(course, dict):
            continue
        code = str(course.get("course_code") or "").strip()
        count = int(course.get("sections_on_file") or 0)
        current = [str(value) for value in course.get("currently_registered_sections") or []]
        free = [
            str(row.get("section") or "").strip()
            for row in course.get("clash_free") or []
            if isinstance(row, dict) and str(row.get("section") or "").strip()
        ]
        clashing = [
            str(row.get("section") or "").strip()
            for row in course.get("clashing") or []
            if isinstance(row, dict) and str(row.get("section") or "").strip()
        ]
        if language == "Arabic":
            if not count:
                lines.append(f"لا توجد شعبة للمقرر {code} مسجلة في بيانات النظام.")
                continue
            if current:
                joined = "، ".join(current)
                sentence = f"الشعبة {joined} لمقرر {code} موجودة بالفعل في جدولك الحالي"
                if term:
                    sentence += f" للفصل {term}"
                sentence += "."
                lines.append(sentence)
                if all(section in free for section in current):
                    lines.append("وعند مقارنتها ببقية جدولك لا يظهر لها تعارض.")
            else:
                lines.append(f"يوجد للمقرر {code} عدد {count} من الشعب المسجلة في بيانات النظام.")
            if clashing:
                lines.append("الشعب التي تتعارض مع جدولك الحالي: " + "، ".join(clashing) + ".")
        else:
            if not count:
                lines.append(f"No section for {code} is recorded in this system's data.")
                continue
            if current:
                joined = ", ".join(current)
                sentence = f"Section {joined} of {code} is already in your current timetable"
                if term:
                    sentence += f" for {term}"
                sentence += "."
                lines.append(sentence)
                if all(section in free for section in current):
                    lines.append("It shows no clash when compared with the rest of your timetable.")
            else:
                lines.append(f"The system has {count} recorded sections for {code}.")
            if clashing:
                lines.append(
                    "Sections that clash with your current timetable: " + ", ".join(clashing) + "."
                )
    if not lines:
        return ""
    lines.append(
        "هذا فحص للجدول فقط؛ لم يسجّل النظام أو يغيّر أي شعبة في بوابة الجامعة."
        if language == "Arabic"
        else "This is a timetable check only; no section was registered or changed in the university portal."
    )
    return "\n".join(lines)


def _planner_option_names(tool_results: list[dict[str, Any]]) -> list[str]:
    """Exact A1-C3 identities the final timetable answer must acknowledge."""
    names: list[str] = []
    for result in tool_results:
        if result.get("tool") != "build_timetable_proposal" or not result.get("ok"):
            continue
        for alternative in result.get("alternatives") or []:
            for name in alternative.get("planner_options") or []:
                clean = str(name or "").strip().upper()
                if clean and clean not in names:
                    names.append(clean)
    return names


_EXHAUSTIVE_VARIANT_CLAIM = re.compile(
    r"(?:\bno\s+(?:other\s+)?(?:clash[- ]free\s+)?(?:section\s+)?"
    r"(?:arrangement|combination|option).*?\b(?:fit|accommodat|place)\w*.*?\ball\b|"
    r"\bcould\s+not\s+(?:fit|accommodat|place)\w*.*?\ball\b|"
    r"(?:لا\s+(?:يمكن|يتوفر|يوجد|تسمح).*?(?:ترتيب|خيار|جمع).*?"
    r"(?:يومين|ثلاثة\s+أيام|3\s*أيام)|"
    r"جميع\s+(?:البدائل|الخيارات)\s+الممكنة))",
    re.IGNORECASE | re.DOTALL,
)


def _misstates_variant_omission(answer: str, tool_results: list[dict[str, Any]]) -> bool:
    """Reject an exhaustive impossibility claim from a top-k variant omission.

    A2/A3 and their B/C equivalents deliberately exclude earlier choices to
    create alternatives. Their `OMITTED_IN_THIS_VARIANT` rows therefore cannot
    support a claim that no full section arrangement exists, especially when a
    sibling option already covers every target.
    """
    has_variant_omission = any(
        str(unplaced.get("reason_code") or "") == "OMITTED_IN_THIS_VARIANT"
        for result in tool_results
        if result.get("tool") == "build_timetable_proposal" and result.get("ok")
        for alternative in result.get("alternatives") or []
        for unplaced in alternative.get("unplaced_courses") or []
        if isinstance(unplaced, dict)
    )
    return has_variant_omission and bool(_EXHAUSTIVE_VARIANT_CLAIM.search(answer or ""))


_EMPTY_RECOMMENDATION_SPECULATION = re.compile(
    r"(?:لا\s+توجد\s+مواد\s+(?:مفتوحة|متاحة)|"
    r"(?:استوفيت|بلغت)\s+(?:الحد|سقف)|"
    r"\breached\s+(?:the\s+)?(?:credit\s+)?cap\b|"
    r"\bno\s+(?:new\s+)?courses?\s+(?:are\s+)?(?:open|available)\b)",
    re.IGNORECASE,
)


def _speculates_about_empty_recommendations(
    answer: str, tool_results: list[dict[str, Any]]
) -> bool:
    empty = any(
        result.get("tool") == "recommend_courses"
        and result.get("ok")
        and not (result.get("recommendations") or [])
        for result in tool_results
    )
    return empty and bool(_EMPTY_RECOMMENDATION_SPECULATION.search(answer or ""))


_LOWER_BOUND_MARKERS = re.compile(
    r"(?:\bat\s+least\b|\blower\s+bound\b|\bminimum\b|" r"على\s+الأقل|حد\s+أدنى|الحد\s+الأدنى)",
    re.IGNORECASE,
)


def _graduation_revision_facts(
    answer: str, tool_results: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Facts a graduation answer omitted from verified scenario evidence.

    A model saying only "no estimate was returned" discards the most useful part
    of an incomplete simulation: its defensible lower bound and exact blockers.
    Return a compact correction payload when one bounded revision is required.
    """
    graduation = next(
        (
            row
            for row in reversed(tool_results)
            if row.get("tool") == "graduation_progress" and row.get("ok")
        ),
        None,
    )
    if not graduation:
        return None
    if isinstance(graduation.get("what_if"), dict):
        # What-if answers are rendered from their structured comparison after the
        # agent finishes, so the ordinary forecast fact-reprompt is unnecessary.
        return None

    candidate = answer or ""
    completed = bool(graduation.get("simulation_completed"))
    if completed:
        required_numbers = [graduation.get("estimated_additional_terms")]
        if graduation.get("current_courses_assumed_passed"):
            required_numbers.append(graduation.get("estimated_terms_including_current"))
        missing_numbers = [
            int(number)
            for number in required_numbers
            if number is not None and str(int(number)) not in candidate
        ]
        if not missing_numbers:
            return None
        return {
            "simulation_completed": True,
            "estimated_additional_terms": graduation.get("estimated_additional_terms"),
            "estimated_terms_including_current": graduation.get(
                "estimated_terms_including_current"
            ),
            "missing_numbers": missing_numbers,
        }

    unresolved = [
        row
        for row in graduation.get("unresolved_requirements") or []
        if isinstance(row, dict) and str(row.get("code") or "").strip()
    ]
    required_numbers = [graduation.get("lower_bound_additional_terms")]
    if graduation.get("current_courses_assumed_passed"):
        required_numbers.append(graduation.get("lower_bound_terms_including_current"))
    missing_numbers = [
        int(number)
        for number in required_numbers
        if number is not None and str(int(number)) not in candidate
    ]
    missing_codes = [
        str(row["code"]).strip()
        for row in unresolved
        if str(row["code"]).strip().upper() not in candidate.upper()
    ]
    blocker_detail_tokens: list[str] = []
    for row in unresolved:
        blocker_detail_tokens.extend(
            str(code).strip()
            for code in row.get("missing_course_prerequisites") or []
            if str(code).strip()
        )
        gate = row.get("credit_hour_gate")
        if isinstance(gate, dict) and gate.get("required") is not None:
            blocker_detail_tokens.append(str(int(gate["required"])))
    missing_blocker_details = [
        token for token in blocker_detail_tokens if token.upper() not in candidate.upper()
    ]
    if (
        not missing_numbers
        and not missing_codes
        and not missing_blocker_details
        and _LOWER_BOUND_MARKERS.search(candidate)
    ):
        return None
    return {
        "simulation_completed": False,
        "lower_bound_additional_terms": graduation.get("lower_bound_additional_terms"),
        "lower_bound_terms_including_current": graduation.get(
            "lower_bound_terms_including_current"
        ),
        "unresolved_requirements": unresolved,
        "missing_numbers": missing_numbers,
        "missing_codes": missing_codes,
        "missing_blocker_details": missing_blocker_details,
        "must_state_lower_bound": not bool(_LOWER_BOUND_MARKERS.search(candidate)),
    }


_GRADUATION_UNSUPPORTED_INFERENCE = re.compile(
    r"(?:\b(?:may|might|could)\s+(?:require|need|add).*?\b(?:extra|additional)\s+term\b|"
    r"\bspecial\s+arrangement\b|\bno\s+available\s+(?:time|section|offering)\b|"
    r"\b(?:maximum\s+allowed|permitted\s+maximum)\b|"
    r"(?:قد|مما)\s+(?:يستدعي|يتطلب).*?فصل(?:اً|ًا|ا)?\s+إضاف|"
    r"ترتيب(?:اً|ًا|ا)?\s+خاص|"
    r"الحد\s+الأقصى\s+المسموح|"
    r"(?:لا|لم)\s+(?:يوجد|يظهر).*?(?:موعد|مكان|شعبة).*?(?:متاح|الفصول|المحاك))",
    re.IGNORECASE | re.DOTALL,
)


def _what_if_error_text(error: dict[str, Any], language: str) -> str:
    kind = str(error.get("kind") or "")
    code = str(error.get("course_code") or "").strip()
    if language == "Arabic":
        messages = {
            "NOT_IN_CURRENT_TIMETABLE": f"{code} ليس ضمن مقررات الجدول الحالي المسجلة في النظام.",
            "ALREADY_IN_CURRENT_TIMETABLE": f"{code} موجود بالفعل في الجدول الحالي.",
            "ALREADY_PASSED": f"{code} مسجل كمقرر مجتاز.",
            "COURSE_NOT_ON_FILE": f"لا توجد بيانات مقرر موثوقة للرمز {code}.",
            "COURSE_CREDITS_UNKNOWN": f"ساعات المقرر {code} غير معروفة.",
            "ELECTIVE_PLACEHOLDER_NOT_A_COURSE": f"{code} خانة اختيارية وليست مقررًا محددًا.",
            "SAME_COURSE_REMOVED_AND_ADDED": "لا يمكن حذف المقرر نفسه وإضافته في السيناريو ذاته.",
            "SEARCH_CANNOT_BE_COMBINED_WITH_EXPLICIT_CHANGES": "لا يمكن جمع البحث التلقائي مع تغييرات صريحة في الطلب نفسه.",
            "TOO_MANY_CHANGES": "عدد تغييرات الفصل الحالي يتجاوز الحد المسموح للمحاكاة.",
        }
        if kind == "SCENARIO_EXCEEDS_CREDIT_CAP":
            return (
                f"السيناريو يصل إلى {int(error.get('credits') or 0)} ساعة، ويتجاوز "
                f"حد المحاكاة {int(error.get('maximum') or 0)} ساعة."
            )
        if kind == "ADDED_COURSE_PREREQUISITES_UNMET":
            missing = "، ".join(error.get("missing_prerequisites") or [])
            return f"لا تتحقق المتطلبات المسجلة للمقرر {code}: {missing}."
        if kind == "ADDED_COURSE_CREDIT_GATE_UNMET":
            return (
                f"لا يتحقق شرط ساعات {code}: المطلوب {int(error.get('required') or 0)} "
                f"والمتاح في السيناريو {int(error.get('effective') or 0)}."
            )
        return messages.get(kind, "تعذر التحقق من تغيير الفصل الحالي المطلوب.")

    messages = {
        "NOT_IN_CURRENT_TIMETABLE": f"{code} is not in the recorded current timetable.",
        "ALREADY_IN_CURRENT_TIMETABLE": f"{code} is already in the current timetable.",
        "ALREADY_PASSED": f"{code} is recorded as passed.",
        "COURSE_NOT_ON_FILE": f"No reliable course record was found for {code}.",
        "COURSE_CREDITS_UNKNOWN": f"The credit value for {code} is unknown.",
        "ELECTIVE_PLACEHOLDER_NOT_A_COURSE": f"{code} is an elective slot, not a concrete course.",
        "SAME_COURSE_REMOVED_AND_ADDED": "The same course cannot be removed and added in one scenario.",
        "SEARCH_CANNOT_BE_COMBINED_WITH_EXPLICIT_CHANGES": "Automatic replacement search cannot be combined with explicit changes.",
        "TOO_MANY_CHANGES": "The current-term scenario contains too many changes.",
    }
    if kind == "SCENARIO_EXCEEDS_CREDIT_CAP":
        return (
            f"The scenario totals {int(error.get('credits') or 0)} credits, above the "
            f"{int(error.get('maximum') or 0)}-credit scenario cap."
        )
    if kind == "ADDED_COURSE_PREREQUISITES_UNMET":
        missing = ", ".join(error.get("missing_prerequisites") or [])
        return f"The recorded prerequisites for {code} are not met: {missing}."
    if kind == "ADDED_COURSE_CREDIT_GATE_UNMET":
        return (
            f"The credit gate for {code} is not met: it requires "
            f"{int(error.get('required') or 0)}, while the scenario has "
            f"{int(error.get('effective') or 0)}."
        )
    return messages.get(kind, "The requested current-term change could not be validated.")


def _comparison_effect_text(comparison: dict[str, Any], language: str) -> str:
    effect = str(comparison.get("timing_effect") or "NOT_DETERMINABLE")
    saved = int(comparison.get("terms_saved") or 0)
    delta = comparison.get("term_difference")
    if language == "Arabic":
        if effect == "EARLIER":
            return f"تقدّر المحاكاة الإكمال أبكر بمقدار {saved} فصل."
        if effect == "LATER":
            return f"تقدّر المحاكاة تأخر الإكمال بمقدار {abs(int(delta or 0))} فصل."
        if effect == "SAME":
            return "لا يتغير عدد الفصول المقدّر بين السيناريوهين."
        if effect == "FORECAST_COMPLETED":
            return "التغيير يحل العوائق التي كانت تمنع اكتمال التقدير الآلي."
        if effect == "FORECAST_BECAME_UNRESOLVED":
            return "التغيير يجعل تقدير الإكمال غير محسوم بعد أن كان مكتملًا."
        if effect == "UNRESOLVED_IMPROVEMENT":
            return "التغيير يحسن عوائق مسجلة، لكنه لا يثبت تخرجًا أبكر بعد."
        if effect == "UNRESOLVED_WORSE":
            return "التغيير يضيف عوائق غير محسومة، لذلك هو أسوأ أكاديميًا في المحاكاة."
        return "لا يمكن إثبات أثر دقيق على موعد التخرج من البيانات الحالية."

    if effect == "EARLIER":
        return f"The scenario estimates completion {saved} term(s) earlier."
    if effect == "LATER":
        return f"The scenario estimates completion {abs(int(delta or 0))} term(s) later."
    if effect == "SAME":
        return "The estimated number of terms is unchanged."
    if effect == "FORECAST_COMPLETED":
        return "The change resolves the blockers that prevented a complete forecast."
    if effect == "FORECAST_BECAME_UNRESOLVED":
        return "The change makes the forecast unresolved after it was complete."
    if effect == "UNRESOLVED_IMPROVEMENT":
        return "The change improves recorded blockers, but does not yet prove earlier graduation."
    if effect == "UNRESOLVED_WORSE":
        return (
            "The change introduces unresolved blockers and is academically worse in the scenario."
        )
    return "The available data cannot prove an exact graduation-timing effect."


def _comparison_blocker_text(comparison: dict[str, Any], language: str) -> str:
    resolved = [
        str(row.get("code") or "")
        for row in comparison.get("blockers_resolved") or []
        if isinstance(row, dict) and row.get("code")
    ]
    improved = [
        str(row.get("code") or "")
        for row in comparison.get("blockers_improved") or []
        if isinstance(row, dict) and row.get("code")
    ]
    parts = []
    if language == "Arabic":
        if resolved:
            parts.append("يحل: " + "، ".join(resolved) + ".")
        if improved:
            parts.append("يحسن دون حسم كامل: " + "، ".join(improved) + ".")
    else:
        if resolved:
            parts.append("Resolves: " + ", ".join(resolved) + ".")
        if improved:
            parts.append("Improves without fully resolving: " + ", ".join(improved) + ".")
    return " ".join(parts)


def _safe_graduation_what_if_answer(language: str, what_if: dict[str, Any]) -> str:
    if not what_if.get("valid"):
        errors = [
            _what_if_error_text(error, language)
            for error in what_if.get("validation_errors") or []
            if isinstance(error, dict)
        ]
        if language == "Arabic":
            return (
                "لم تُشغّل محاكاة التغيير لأن الطلب لم يجتز التحقق:\n- "
                + "\n- ".join(errors or ["تعذر التحقق من التغيير المطلوب."])
                + "\nلم يتغير الجدول أو السجل الفعلي."
            )
        return (
            "The change was not simulated because it did not pass validation:\n- "
            + "\n- ".join(errors or ["The requested change could not be validated."])
            + "\nNo real timetable or student record was changed."
        )

    if what_if.get("mode") == "replacement_search":
        replacements = what_if.get("improving_replacements") or []
        partial_count = int(what_if.get("unproven_blocker_progress_pairs") or 0)
        if language == "Arabic":
            if not replacements:
                answer = (
                    "لم يثبت البحث الأكاديمي المحدود وجود استبدال واحد مقابل واحد يحسن "
                    "تقدير التخرج مقارنة بالجدول الحالي. هذا لا يثبت استحالة وجود ترتيب آخر. "
                )
                if partial_count:
                    answer += (
                        f"استبعد البحث {partial_count} استبدالًا حسّن عائقًا جزئيًا فقط، "
                        "لأن مسار التخرج الكامل لم يتحسن. "
                    )
                return answer + "لم يتغير أي مقرر أو جدول فعلي."
            lines = ["الاستبدالات التي أظهرت تحسنًا أكاديميًا في المحاكاة:"]
            for row in replacements:
                removed = str((row.get("remove_course") or {}).get("code") or "")
                added = str((row.get("add_course") or {}).get("code") or "")
                comparison = row.get("comparison") or {}
                lines.append(
                    f"- {removed} ← {added}: "
                    + _comparison_effect_text(comparison, language)
                    + " "
                    + _comparison_blocker_text(comparison, language)
                )
            lines.append(
                "هذه مقارنة أكاديمية فقط؛ يجب فحص الشعب والتعارضات في أداة الجدول، ولا "
                "يثبت ذلك توفر مقعد أو صلاحية التسجيل. لم يتغير جدولك الفعلي."
            )
            return "\n".join(lines)
        if not replacements:
            answer = (
                "The bounded academic search found no one-for-one replacement proven to "
                "improve the graduation forecast over the current timetable. This does not "
                "prove that no other arrangement exists. "
            )
            if partial_count:
                answer += (
                    f"The search rejected {partial_count} replacement(s) that improved only "
                    "an individual blocker because the complete graduation path did not "
                    "improve. "
                )
            return answer + "No real course or timetable changed."
        lines = ["Replacements with a proven academic improvement in the simulation:"]
        for row in replacements:
            removed = str((row.get("remove_course") or {}).get("code") or "")
            added = str((row.get("add_course") or {}).get("code") or "")
            comparison = row.get("comparison") or {}
            lines.append(
                f"- {removed} → {added}: "
                + _comparison_effect_text(comparison, language)
                + " "
                + _comparison_blocker_text(comparison, language)
            )
        lines.append(
            "This is an academic comparison only. Check sections and clashes with the "
            "timetable tool; it does not prove seat availability or registration permission. "
            "No real timetable was changed."
        )
        return "\n".join(lines)

    removed = [
        str(course.get("code") or "") for course in what_if.get("removed_current_courses") or []
    ]
    added = [str(course.get("code") or "") for course in what_if.get("added_current_courses") or []]
    comparison = what_if.get("comparison") or {}
    baseline = what_if.get("baseline") or {}
    scenario = what_if.get("scenario") or {}
    resolved = [str(row.get("code") or "") for row in comparison.get("blockers_resolved") or []]
    improved = [str(row.get("code") or "") for row in comparison.get("blockers_improved") or []]
    introduced = [str(row.get("code") or "") for row in comparison.get("blockers_introduced") or []]
    outside = [str(row.get("code") or "") for row in what_if.get("outside_plan_additions") or []]

    if language == "Arabic":
        change = []
        if removed:
            change.append("حذف " + "، ".join(removed))
        if added:
            change.append("إضافة " + "، ".join(added))
        lines = ["سيناريو الفصل الحالي: " + " و".join(change) + "."]
        lines.append(_comparison_effect_text(comparison, language))
        lines.append(
            f"الحد الأدنى الإضافي: {baseline.get('lower_bound_additional_terms')} في "
            f"الجدول الحالي مقابل {scenario.get('lower_bound_additional_terms')} في السيناريو."
        )
        if resolved:
            lines.append("عوائق حُلّت في المحاكاة: " + "، ".join(resolved) + ".")
        if improved:
            lines.append("عوائق تحسنت ولم تُحسم بالكامل: " + "، ".join(improved) + ".")
        if introduced:
            lines.append("عوائق جديدة: " + "، ".join(introduced) + ".")
        if outside:
            lines.append(
                "المقرر "
                + "، ".join(outside)
                + " خارج متطلبات الخطة؛ قد يؤثر في المتطلبات أو الساعات لكنه لا يكمل مقررًا من الخطة."
            )
        lines.append(
            "هذا افتراض أكاديمي للقراءة فقط. لم يُحذف أو يُضف أو يُسجل أي مقرر فعليًا، "
            "ولا يثبت السيناريو توفر شعبة أو مقعد أو عدم وجود تعارض."
        )
        return "\n".join(lines)

    change = []
    if removed:
        change.append("remove " + ", ".join(removed))
    if added:
        change.append("add " + ", ".join(added))
    lines = ["Current-term scenario: " + " and ".join(change) + "."]
    lines.append(_comparison_effect_text(comparison, language))
    lines.append(
        f"Additional-term lower bound: {baseline.get('lower_bound_additional_terms')} for "
        f"the current timetable versus {scenario.get('lower_bound_additional_terms')} for the scenario."
    )
    if resolved:
        lines.append("Blockers resolved in the simulation: " + ", ".join(resolved) + ".")
    if improved:
        lines.append("Blockers improved but not fully resolved: " + ", ".join(improved) + ".")
    if introduced:
        lines.append("New blockers: " + ", ".join(introduced) + ".")
    if outside:
        lines.append(
            ", ".join(outside)
            + " is outside the degree-plan requirements. It can affect prerequisites or earned "
            "credits, but does not complete a plan course."
        )
    lines.append(
        "This is a read-only academic assumption. No course was removed, added, or registered, "
        "and the scenario does not prove section availability, seats, or a clash-free timetable."
    )
    return "\n".join(lines)


def _safe_graduation_answer(language: str, tool_results: list[dict[str, Any]]) -> str:
    """Deterministic last line of defence for a grounded graduation answer."""
    graduation = next(
        (
            row
            for row in reversed(tool_results)
            if row.get("tool") == "graduation_progress" and row.get("ok")
        ),
        None,
    )
    if not graduation:
        return ""

    what_if = graduation.get("what_if")
    if isinstance(what_if, dict):
        return _safe_graduation_what_if_answer(language, what_if)

    has_current = bool(graduation.get("current_courses_assumed_passed"))
    cap = int(graduation.get("max_credits_per_term") or 18)
    unresolved = [
        row
        for row in graduation.get("unresolved_requirements") or []
        if isinstance(row, dict) and str(row.get("code") or "").strip()
    ]

    if language == "Arabic":
        if graduation.get("simulation_completed"):
            additional = int(graduation.get("estimated_additional_terms") or 0)
            opening = f"تقدّر المحاكاة أنك تحتاج إلى {additional} فصول إضافية"
            if has_current:
                opening += (
                    f"، أو {int(graduation.get('estimated_terms_including_current') or additional)} "
                    "فصول باحتساب الفصل الحالي"
                )
            opening += "."
        else:
            additional = int(graduation.get("lower_bound_additional_terms") or 0)
            opening = f"الحد الأدنى هو {additional} فصول إضافية"
            if has_current:
                opening += (
                    f"، أو {int(graduation.get('lower_bound_terms_including_current') or additional)} "
                    "فصول باحتساب الفصل الحالي"
                )
            opening += "؛ لا يمكن إعطاء فصل إكمال دقيق لأن المحاكاة لم تحسم كل المتطلبات."

        blocker_lines = []
        for row in unresolved:
            code = str(row["code"]).strip()
            reasons = []
            prereqs = [
                str(item).strip()
                for item in row.get("missing_course_prerequisites") or []
                if str(item).strip()
            ]
            if prereqs:
                reasons.append("متطلبات سابقة غير مستوفاة: " + "، ".join(prereqs))
            gate = row.get("credit_hour_gate")
            if isinstance(gate, dict):
                reasons.append(
                    f"شرط الساعات {int(gate.get('required') or 0)}؛ يصل السيناريو إلى "
                    f"{int(gate.get('effective_in_scenario') or 0)} والمتبقي "
                    f"{int(gate.get('remaining') or 0)}"
                )
            blocker_lines.append(f"- {code}: " + ("؛ ".join(reasons) or "متطلب غير محسوم"))
        blockers = "\n" + "\n".join(blocker_lines) if blocker_lines else ""
        return (
            opening + blockers + f"\nهذا سيناريو للقراءة فقط بحد أقصى {cap} ساعة في كل فصل رئيس، "
            "ويفترض اجتياز جميع المقررات من أول محاولة. لا يضمن الطرح المستقبلي "
            "أو المقاعد أو أوقات الشعب أو صلاحية التسجيل، ولا يغيّر سجلك أو يسجل مقررات."
        )

    if graduation.get("simulation_completed"):
        additional = int(graduation.get("estimated_additional_terms") or 0)
        opening = f"The scenario estimates {additional} additional terms"
        if has_current:
            opening += (
                ", or "
                f"{int(graduation.get('estimated_terms_including_current') or additional)} "
                "terms including the current term"
            )
        opening += "."
    else:
        additional = int(graduation.get("lower_bound_additional_terms") or 0)
        opening = f"The lower bound is {additional} additional terms"
        if has_current:
            opening += (
                ", or "
                f"{int(graduation.get('lower_bound_terms_including_current') or additional)} "
                "terms including the current term"
            )
        opening += "; no exact completion term can be given because requirements remain unresolved."

    blocker_lines = []
    for row in unresolved:
        code = str(row["code"]).strip()
        reasons = []
        prereqs = [
            str(item).strip()
            for item in row.get("missing_course_prerequisites") or []
            if str(item).strip()
        ]
        if prereqs:
            reasons.append("unmet prerequisites: " + ", ".join(prereqs))
        gate = row.get("credit_hour_gate")
        if isinstance(gate, dict):
            reasons.append(
                f"{int(gate.get('required') or 0)}-credit gate; the scenario reaches "
                f"{int(gate.get('effective_in_scenario') or 0)}, leaving "
                f"{int(gate.get('remaining') or 0)}"
            )
        blocker_lines.append(f"- {code}: " + ("; ".join(reasons) or "unresolved requirement"))
    blockers = "\n" + "\n".join(blocker_lines) if blocker_lines else ""
    return (
        opening
        + blockers
        + f"\nThis read-only scenario caps each main term at {cap} credits and assumes every "
        "course is passed on the first attempt. It cannot guarantee future offerings, seats, "
        "section times, or registration permission, and it does not change the student record."
    )


def _unresolved_policy_ids(tool_results: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for result in tool_results:
        if result.get("tool") != "policy_lookup" or not result.get("ok"):
            continue
        for policy in result.get("direct_policy_evidence") or []:
            if not isinstance(policy, dict):
                continue
            if not (policy.get("source_is_unclear_on") or policy.get("open_question")):
                continue
            policy_id = str(policy.get("policy_id") or "").strip()
            if policy_id and policy_id not in ids:
                ids.append(policy_id)
    return ids


_UNCERTAINTY_MARKERS = re.compile(
    r"(?:لم\s+(?:يحسم|يوضح|يحدد|يفصل)|غير\s+واضح|لا\s+(?:يحسم|يوضح|يحدد|ينص)|"
    r"does\s+not\s+(?:settle|state|specify|define)|unclear|unresolved)",
    re.IGNORECASE,
)


def is_enabled() -> bool:
    return bool(getattr(settings, "STUDENT_ADVISOR_V2_ENABLED", False))


def _max_iterations() -> int:
    return max(1, int(getattr(settings, "STUDENT_ADVISOR_V2_MAX_TOOL_ITERATIONS", 4)))


def _max_calls() -> int:
    return max(1, int(getattr(settings, "STUDENT_ADVISOR_V2_MAX_TOOL_CALLS", 8)))


def _max_tokens() -> int:
    return max(256, int(getattr(settings, "STUDENT_ADVISOR_V2_MAX_TOKENS", 1800)))


def _tool_timeout() -> float:
    return max(1.0, float(getattr(settings, "STUDENT_ADVISOR_V2_TOOL_TIMEOUT_SECONDS", 75)))


def student_v2_tool_schemas() -> list[dict[str, Any]]:
    """Return only the V2 self-service schemas, with identity removed from arguments.

    A student's identity comes from ``AdvisorPrincipal``.  Advertising a
    ``student_id`` argument invites the model to fill a value it has no authority to
    choose, even though the executor would later refuse it.
    """
    registry = get_default_registry()
    schemas: list[dict[str, Any]] = []
    for name in STUDENT_V2_TOOL_NAMES:
        capability = registry.capabilities[name]
        schema = copy.deepcopy(capability.tool_schema())
        function = schema.get("function") or {}
        parameters = function.get("parameters") or {}
        properties = parameters.get("properties") or {}
        properties.pop("student_id", None)
        required = parameters.get("required")
        if isinstance(required, list):
            parameters["required"] = [item for item in required if item != "student_id"]
        schemas.append(schema)
    return schemas


def execute_student_v2_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    principal: AdvisorPrincipal,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute one allowed self-service capability; refuse everything else."""
    if principal.role != ROLE_STUDENT or principal.student_id is None:
        return {"tool": name, "ok": False, "error": "Student identity is required."}
    if name not in STUDENT_V2_TOOL_NAMES:
        return {"tool": name, "ok": False, "error": "This capability is not available."}
    if not isinstance(arguments, dict):
        return {"tool": name, "ok": False, "error": "Tool arguments must be an object."}
    # Identity is session-owned even if a non-schema-compliant model sends it.
    arguments = {key: value for key, value in arguments.items() if key != "student_id"}
    return get_default_registry().execute(
        name,
        arguments,
        scope=principal.as_scope(),
        ctx=context or {},
    )


def _policy_grounding(question: str, tool_results: list[dict[str, Any]]) -> tuple[bool, str]:
    # Retrieval is unconditional, but a missing rule must only turn the whole
    # response into an abstention when the student actually asked for a rule.
    # Pure timetable and record questions are grounded by their own tools.
    required = requires_policy_contract(question)
    policy = [row for row in tool_results if row.get("tool") == "policy_lookup"]
    if not policy:
        return required, "not_consulted"
    if any(not row.get("ok") for row in policy):
        return required, "unavailable"
    if any(row.get("direct_policy_evidence") for row in policy):
        return required, "retrieved"
    if any(row.get("policies") for row in policy):
        return required, "none_governing"
    return required, "none_matched"


def _citation_refusal(language: str) -> str:
    if language == "Arabic":
        return (
            "لم أتمكن من التحقق من مرجع الإجابة في الأدلة المعتمدة، لذلك لن أعرض "
            "معلومة غير موثقة. راجع مرشدك الأكاديمي أو عمادة القبول والتسجيل."
        )
    return (
        "I could not verify the source for this answer against the approved records, "
        "so I will not present an unsupported rule. Please check with your academic "
        "adviser or the Deanship of Admission and Registration."
    )


def _claims_portal_action(answer: str) -> bool:
    return any(pattern.search(answer or "") for pattern in _PORTAL_ACTION_CLAIMS)


def _portal_boundary_response(language: str) -> str:
    if language == "Arabic":
        return (
            "أستطيع إعداد مقترح دراسي ومراجعته معك هنا، لكن هذا النظام لا يسجل "
            "المقررات ولا يحفظ أو يطبق جدولًا في بوابة الجامعة. إذا أعجبك المقترح، "
            "فعليك إدخال المقررات التي اخترتها بنفسك في البوابة الرئيسية للجامعة."
        )
    return (
        "I can prepare and check a study proposal with you here, but this system cannot "
        "register courses or save/apply a timetable in the university portal. If you "
        "choose the proposal, enter those courses yourself in the university's main portal."
    )


def answer_student_advisor_v2(
    *,
    question: str,
    principal: AdvisorPrincipal,
    academic_year: int | None = None,
    term: int | None = None,
    history: Any = None,
    model: str | None = None,
    llm_client: Any = None,
) -> dict[str, Any]:
    """Run one student turn through a single plan/act/observe agent loop."""
    if principal.role != ROLE_STUDENT or principal.student_id is None:
        raise ValueError("Student Advisor V2 requires an authenticated student principal.")

    clean_question = str(question or "").strip()
    if not clean_question:
        raise ValueError("question is required")

    if academic_year is None or term is None:
        from core.settings_views import load_defaults

        defaults = load_defaults()
        academic_year = (
            academic_year if academic_year is not None else int(defaults["academic_year"])
        )
        term = term if term is not None else int(defaults["term"])
    tool_context = {"academic_year": int(academic_year), "term": int(term)}

    language = _answer_language(clean_question)
    llm = llm_client or get_llm_client()
    resolved_model = llm.resolve_model(model)
    scope = principal.as_scope()
    student_record = Student.objects.filter(student_id=principal.student_id).values("name").first()
    if student_record is None:
        # The conversation endpoint turns this into the existing student-facing
        # 409 and refunds the generation allowance. Calling a model without the
        # roster row would silently turn a personal adviser into a generic chatbot.
        raise ValueError("No student record exists for the authenticated principal.")
    student_name = str(student_record.get("name") or "").strip()
    boundary = boundary_for_scope(
        scope,
        backend=str(getattr(llm, "backend", "local")),
        known_names=(student_name,) if len(student_name) >= 3 else (),
    )

    # Policy retrieval is local and cheap, and must not depend on whether either a
    # classifier or the model recognises the regulation hidden in colloquial Arabic.
    # The same one-agent loop remains in control; this only seeds verified evidence.
    policy_prefetched = True
    policy_result, _ = _seed_policy_evidence(clean_question, scope)
    seeded_policy_results: list[dict[str, Any]] = [policy_result]
    projected_policy = boundary.project_tool_result("policy_lookup", policy_result)
    policy_prompt = "\nverified_policy_evidence: " + json.dumps(
        _policy_evidence_for_prompt(projected_policy),
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )

    schemas = boundary.tool_schemas(student_v2_tool_schemas())
    advertised = {str((schema.get("function") or {}).get("name") or "") for schema in schemas}
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *_sanitize_history(history),
        {
            "role": "user",
            "content": (
                f"answer_language: {language}\n"
                f"configured_planning_term_hijri: {academic_year}/{term}\n"
                "Use this configured term unless the student explicitly asks about another. "
                "Do not ask for a Gregorian year.\n"
                f"student_question: {clean_question}" + policy_prompt
            ),
        },
    ]
    messages = boundary.sanitise_messages(messages)

    usage = UsageTotals()
    local_results: list[dict[str, Any]] = list(seeded_policy_results)
    tools_called: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    total_calls = 0
    answer = ""
    answer_model = resolved_model
    iterations = 0
    tool_turn_error = ""
    fallback_seeded = False
    requires_timetable_proposal = _requires_timetable_proposal(clean_question)
    requires_section_check = _requires_section_check(clean_question)
    requires_graduation_progress = _requires_graduation_progress(clean_question)
    requires_graduation_what_if = _requires_graduation_what_if(clean_question)
    timetable_reprompted = False
    timetable_format_reprompted = False
    timetable_variant_reprompted = False
    section_tool_reprompted = False
    section_evidence_reprompted = False
    section_safe_fallback_used = False
    recommendation_reprompted = False
    graduation_tool_reprompted = False
    graduation_what_if_reprompted = False
    graduation_reprompted = False
    graduation_safe_fallback_used = False
    policy_uncertainty_reprompted = False
    internal_output_reprompted = False
    internal_output_sanitized = False

    for iteration in range(_max_iterations()):
        iterations = iteration + 1
        try:
            turn = llm.chat_with_tools(
                messages,
                tools=schemas,
                model=resolved_model,
                max_tokens=_max_tokens(),
                timeout_seconds=_tool_timeout(),
            )
        except LLMError as exc:
            # A slow/unsupported tool turn must not discard the student's whole
            # question. The no-tools rescue below receives a verified self-snapshot
            # when no evidence was gathered yet; it never answers from memory.
            tool_turn_error = type(exc).__name__
            break
        usage.add(turn.usage)
        answer_model = turn.model or answer_model
        if not turn.tool_calls:
            candidate = str(turn.content or "").strip()
            has_timetable_evidence = any(
                row.get("tool") == "build_timetable_proposal" for row in local_results
            )
            has_section_evidence = bool(_verified_section_results(local_results))
            has_graduation_evidence = any(
                row.get("tool") == "graduation_progress" and row.get("ok") for row in local_results
            )
            has_graduation_what_if_evidence = any(
                row.get("tool") == "graduation_progress"
                and row.get("ok")
                and isinstance(row.get("what_if"), dict)
                for row in local_results
            )
            if (
                candidate
                and requires_section_check
                and not has_section_evidence
                and not section_tool_reprompted
            ):
                messages.append(turn.assistant_message)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "This question names a course and a section, so it requires "
                            "fresh verified section evidence even if conversation history "
                            "contains an earlier answer. Call my_clash_free_sections now "
                            "with the exact COURSE code from student_question (not the "
                            "section label). Do not answer from chat history."
                        ),
                    }
                )
                section_tool_reprompted = True
                continue
            if (
                candidate
                and requires_timetable_proposal
                and not has_timetable_evidence
                and not timetable_reprompted
            ):
                # This is a grounding gate, not an intent router. The same agent
                # remains in control, but it may not answer a timetable-build
                # request from general recommendations or claim the data is
                # unavailable without first consulting the real planner engine.
                messages.append(turn.assistant_message)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "This request requires verified timetable evidence. You do have "
                            "access to section times and clash detection. Call "
                            "build_timetable_proposal now, using around_current or "
                            "from_scratch exactly as the student requested, before answering."
                        ),
                    }
                )
                timetable_reprompted = True
                continue
            if (
                candidate
                and requires_graduation_what_if
                and not has_graduation_what_if_evidence
                and not graduation_what_if_reprompted
            ):
                messages.append(turn.assistant_message)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "The student asked for a current-term graduation what-if, not "
                            "the unchanged baseline. Reread the exact student question and "
                            "call graduation_progress again with scenario arguments. For a "
                            "course they will not take, use remove_current_courses. For X "
                            "instead of Y, add X and remove Y. For an open-ended better "
                            "replacement search, use search_better_replacements=true. Do not "
                            "answer until the returned evidence contains what_if."
                        ),
                    }
                )
                graduation_what_if_reprompted = True
                continue
            if (
                candidate
                and requires_graduation_progress
                and not has_graduation_evidence
                and not graduation_tool_reprompted
            ):
                messages.append(turn.assistant_message)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "This graduation question requires the student's verified "
                            "term-by-term scenario. Call graduation_progress now before "
                            "answering. Do not substitute general progress percentages, "
                            "policy retrieval, or a registrar referral for that evidence."
                        ),
                    }
                )
                graduation_tool_reprompted = True
                continue
            if (
                candidate
                and has_section_evidence
                and _section_answer_contradicts_evidence(candidate, local_results)
                and not section_evidence_reprompted
            ):
                messages.append(turn.assistant_message)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Revise the answer from the verified my_clash_free_sections "
                            "result already in this conversation; do not call another tool. "
                            "Your draft falsely says the section data is missing even though "
                            "sections_on_file is greater than zero. State any "
                            "currently_registered_sections first, then distinguish recorded "
                            "clash-free and clashing sections. Do not infer seat availability "
                            "or claim that a registration/change was performed."
                        ),
                    }
                )
                section_evidence_reprompted = True
                continue
            planner_names = _planner_option_names(local_results)
            missing_planner_names = [
                name for name in planner_names if name not in candidate.upper()
            ]
            if (
                candidate
                and has_timetable_evidence
                and missing_planner_names
                and not timetable_format_reprompted
            ):
                # Calling the right solver is not enough if the answer hides the
                # exact A1-C3 output the student asked to see. Give the same agent
                # one bounded correction turn grounded in the evidence it already
                # has; no second tool call or role/router is introduced.
                messages.append(turn.assistant_message)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Revise the timetable answer from the existing tool evidence. "
                            "Explicitly label every distinct proposal with all of its exact "
                            "Planner identities, including empty around-current proposals. "
                            "Missing Planner identities: "
                            + (", ".join(missing_planner_names) or "none")
                            + ". State scheduled/target coverage, important differences, and "
                            "preserve each returned unplaced reason; do not call it a clash "
                            "unless the reason says so. Keep prose concise because the "
                            "structured timetable card displays every section/day/time row."
                        ),
                    }
                )
                timetable_format_reprompted = True
                continue
            if (
                candidate
                and has_timetable_evidence
                and _misstates_variant_omission(candidate, local_results)
                and not timetable_variant_reprompted
            ):
                messages.append(turn.assistant_message)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Revise the timetable answer from the existing evidence. "
                            "OMITTED_IN_THIS_VARIANT says only that this generated variant "
                            "did not place the course; another returned variant placed it. "
                            "Do not claim that no full clash-free arrangement or no other "
                            "section combination exists, because the finite A1-C3 output "
                            "does not prove exhaustive impossibility. For a requested number "
                            "of campus days, say only that none of the returned A1-C3 options "
                            "met it and that other arrangements may exist. Describe only the "
                            "coverage of the returned options."
                        ),
                    }
                )
                timetable_variant_reprompted = True
                continue
            if (
                candidate
                and _speculates_about_empty_recommendations(candidate, local_results)
                and not recommendation_reprompted
            ):
                messages.append(turn.assistant_message)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Revise the recommendation answer from the existing evidence. "
                            "An empty recommendations list proves only that this system has "
                            "no new recommended course for the configured term. Do not infer "
                            "that courses are closed or unavailable, or that the student "
                            "reached a credit cap. Do not offer courses listed in "
                            "already_in_current_timetable as additions."
                        ),
                    }
                )
                recommendation_reprompted = True
                continue
            leaked_markers = _internal_output_markers(candidate)
            if candidate and leaked_markers and not internal_output_reprompted:
                messages.append(turn.assistant_message)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Rewrite the answer from the existing verified evidence; do not "
                            "call another tool. Remove these internal schema or reason labels: "
                            + ", ".join(leaked_markers)
                            + ". Express their meaning in natural student-facing language. "
                            "Do not expose tool names, enum values, field names, or reason codes."
                        ),
                    }
                )
                internal_output_reprompted = True
                continue
            if candidate and leaked_markers:
                candidate = _humanise_internal_output_markers(candidate, language)
                internal_output_sanitized = True
            graduation_revision = _graduation_revision_facts(candidate, local_results)
            if candidate and graduation_revision and not graduation_reprompted:
                messages.append(turn.assistant_message)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Revise the graduation answer from the existing verified "
                            "evidence; do not call another tool. If the simulation is "
                            "complete, state both the additional-term estimate and the "
                            "estimate including the current term. If it is incomplete, "
                            "do not invent an exact completion term: state the lower bound "
                            "both excluding and including the current term, then name every "
                            "unresolved requirement and its returned prerequisite or "
                            "credit-hour blocker. Keep the assumptions (first-attempt passes, "
                            "18-credit main-term cap, and no guarantee of future offerings, "
                            "seats, or registration permission). Describe only those returned "
                            "blockers: do not infer that one requires an extra term or special "
                            "arrangement, or that a course has no available time, place, "
                            "section, or offering. Call 18 credits the scenario cap, never the "
                            "university's maximum allowed load. Required correction facts: "
                            + json.dumps(
                                graduation_revision,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                default=str,
                            )
                        ),
                    }
                )
                graduation_reprompted = True
                continue
            unresolved_policy_ids = _unresolved_policy_ids(local_results)
            if (
                candidate
                and unresolved_policy_ids
                and not _UNCERTAINTY_MARKERS.search(candidate)
                and not policy_uncertainty_reprompted
            ):
                messages.append(turn.assistant_message)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Revise the answer from the existing evidence. These governing "
                            "policy records explicitly say the source leaves a point unresolved: "
                            + ", ".join(unresolved_policy_ids)
                            + ". State what the source does establish, state clearly what it "
                            "does not settle, and remove any categorical conclusion on that "
                            "unresolved point. Do not call another tool."
                        ),
                    }
                )
                policy_uncertainty_reprompted = True
                continue
            answer = candidate
            if answer:
                break
            continue

        messages.append(turn.assistant_message)
        for call in turn.tool_calls:
            total_calls += 1
            if total_calls > _max_calls():
                messages.append(
                    _tool_message(
                        call.id,
                        {"ok": False, "error": "Tool-call budget reached; answer from evidence."},
                    )
                )
                continue

            model_arguments = dict(call.arguments)
            effective_arguments = model_arguments
            scenario_normalization = ""
            timetable_normalizations: list[str] = []
            if call.name == "graduation_progress":
                effective_arguments, scenario_normalization = _normalise_graduation_scenario_args(
                    clean_question,
                    model_arguments,
                )
            elif call.name == "build_timetable_proposal":
                effective_arguments, timetable_normalizations = _normalise_timetable_proposal_args(
                    clean_question,
                    model_arguments,
                )
            tools_called.append(
                {
                    "name": call.name,
                    "arguments": _summarise_tool_args(effective_arguments),
                    **(
                        {"scenario_normalization": scenario_normalization}
                        if scenario_normalization
                        else {}
                    ),
                    **(
                        {"argument_normalizations": timetable_normalizations}
                        if timetable_normalizations
                        else {}
                    ),
                }
            )
            if call.name not in advertised or call.name not in STUDENT_V2_TOOL_NAMES:
                messages.append(
                    _tool_message(call.id, {"ok": False, "error": "Capability unavailable."})
                )
                continue

            cache_key = json.dumps(
                [call.name, effective_arguments],
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if cache_key in seen:
                messages.append(_tool_message(call.id, seen[cache_key]))
                continue

            try:
                boundary.assert_capability_allowed(call.name)
                arguments = boundary.reject_identity_arguments(call.name, dict(effective_arguments))
                arguments = boundary.resolve_reference_arguments(call.name, arguments)
                boundary.authorise_resolved_arguments(call.name, arguments)
                # Local models do not cross the remote argument filter. Strip the
                # identity here as well so the executor receives only choices the
                # model is actually allowed to make.
                arguments.pop("student_id", None)
                local_result = execute_student_v2_tool(
                    call.name,
                    arguments,
                    principal=principal,
                    context=tool_context,
                )
                provider_result = boundary.project_tool_result(call.name, local_result)
            except Exception:  # fail closed without exposing boundary details
                local_result = {"tool": call.name, "ok": False, "error": "Capability refused."}
                provider_result = boundary.refusal_result(call.name)

            local_results.append(local_result)
            seen[cache_key] = provider_result
            messages.append(_tool_message(call.id, provider_result))

        # Graduation what-if answers are deliberately reconstructed from the
        # structured comparison below; the model's prose is never authoritative for
        # them. Once that comparison exists, another model turn can only add cost or
        # call an unrelated capability (the live replacement-search acceptance case
        # did both before its draft was discarded by the safe-answer boundary).
        # Finish from the verified scenario now. A missing ``what_if`` still follows
        # the reprompt/fail-closed path above on the next iteration.
        verified_what_if = any(
            row.get("tool") == "graduation_progress"
            and row.get("ok")
            and isinstance(row.get("what_if"), dict)
            for row in local_results
        )
        if verified_what_if:
            answer = _safe_graduation_answer(language, local_results)
            if answer:
                graduation_safe_fallback_used = True
                break

    if not answer:
        if not any(row.get("tool") != "policy_lookup" for row in local_results):
            fallback_local = execute_student_v2_tool(
                "get_student_context",
                {},
                principal=principal,
                context=tool_context,
            )
            try:
                fallback_provider = boundary.project_tool_result(
                    "get_student_context", fallback_local
                )
            except Exception:
                fallback_provider = boundary.refusal_result("get_student_context")
            local_results.append(fallback_local)
            tools_called.append(
                {
                    "name": "get_student_context",
                    "arguments": {},
                    "reason": "verified_fallback_after_tool_turn_failure",
                }
            )
            fallback_seeded = True
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Verified read-only student evidence for the fallback answer:\n"
                        + json.dumps(fallback_provider, ensure_ascii=False, default=str)
                    ),
                }
            )
        messages.append(
            {
                "role": "user",
                "content": (
                    "Give the final answer now from the evidence already gathered. Do not call "
                    "more tools. Preserve the no-registration/no-save boundary."
                ),
            }
        )
        forced = llm.chat(
            boundary.sanitise_messages(messages),
            model=resolved_model,
            max_tokens=_max_tokens(),
            assistant_prefill=_assistant_prefill_for_client(llm, resolved_model),
        )
        usage.add(forced.usage)
        answer = forced.content
        answer_model = forced.model or answer_model

    safe_section = _safe_section_answer(language, local_results)
    if (
        requires_section_check
        and safe_section
        and _section_answer_contradicts_evidence(answer, local_results)
    ):
        answer = safe_section
        section_safe_fallback_used = True

    safe_graduation = _safe_graduation_answer(language, local_results)
    incomplete_graduation = any(
        row.get("tool") == "graduation_progress"
        and row.get("ok")
        and not row.get("simulation_completed")
        for row in local_results
    )
    graduation_what_if = any(
        row.get("tool") == "graduation_progress"
        and row.get("ok")
        and isinstance(row.get("what_if"), dict)
        for row in local_results
    )
    missing_required_what_if = requires_graduation_what_if and not graduation_what_if
    if missing_required_what_if:
        answer = (
            "تعذر تشغيل مقارنة التغيير المطلوب على مقررات الفصل الحالي، لذلك لن أعرض "
            "تقدير الجدول الحالي وكأنه يجيب عن السيناريو. لم يتغير أي مقرر أو جدول فعلي."
            if language == "Arabic"
            else (
                "The requested current-term course comparison could not be run, so I will "
                "not present the unchanged baseline as if it answered the scenario. No real "
                "course or timetable was changed."
            )
        )
        graduation_safe_fallback_used = True
    elif safe_graduation and (
        graduation_what_if
        or incomplete_graduation
        or _graduation_revision_facts(answer, local_results)
        or _GRADUATION_UNSUPPORTED_INFERENCE.search(answer or "")
    ):
        answer = safe_graduation
        graduation_safe_fallback_used = True

    if _internal_output_markers(answer):
        answer = _humanise_internal_output_markers(answer, language)
        internal_output_sanitized = True

    # Recommendation/context tools legitimately carry the approved credit-load
    # figures and their backing policy id. Convert that embedded provenance to the
    # same citable shape as policy_lookup before validating the answer; otherwise a
    # grounded credit statement is rejected merely because the agent learned it
    # from a student tool rather than making a second, redundant policy call.
    credit_evidence = _credit_policy_evidence_citations({"tool_results": local_results})
    if credit_evidence is not None:
        local_results.append(credit_evidence)

    citations = _retrieved_citations(local_results)
    citation_bad = _bad_citations(answer, citations)
    fabricated = _fabricated_policy_ids(answer, citations)
    citation_refused = bool(citation_bad or fabricated)
    if citation_refused:
        if safe_graduation and not requires_policy_contract(clean_question):
            answer = safe_graduation
            graduation_safe_fallback_used = True
        else:
            answer = _citation_refusal(language)

    portal_claim_refused = _claims_portal_action(answer)
    if portal_claim_refused:
        answer = _portal_boundary_response(language)

    policy_required, grounding = _policy_grounding(clean_question, local_results)
    cited_policy_ids = [
        str(item.get("policy_id") or "")
        for item in citations
        if str(item.get("policy_id") or "") in answer
    ]

    return {
        "ok": True,
        "answer": answer,
        "model": answer_model,
        "usage": usage.as_dict(),
        "citations": citations,
        "cited_policy_ids": cited_policy_ids,
        "missing_information": [],
        "presentation": (
            graduation_presentation_from_tool_results(local_results)
            or timetable_presentation_from_tool_results(local_results)
        ),
        "agent": {
            "version": "student-v2",
            "loop_used": True,
            "iterations": iterations,
            "tools_called": tools_called,
            "tool_results": local_results,
            "policy_required": policy_required,
            "policy_prefetched": policy_prefetched,
            "policy_grounding": grounding,
            "citation_refused": citation_refused,
            "portal_claim_refused": portal_claim_refused,
            "tool_turn_error": tool_turn_error,
            "fallback_seeded": fallback_seeded,
            "timetable_grounding_required": requires_timetable_proposal,
            "timetable_reprompted": timetable_reprompted,
            "timetable_format_reprompted": timetable_format_reprompted,
            "timetable_variant_reprompted": timetable_variant_reprompted,
            "section_grounding_required": requires_section_check,
            "section_tool_reprompted": section_tool_reprompted,
            "section_evidence_reprompted": section_evidence_reprompted,
            "section_safe_fallback_used": section_safe_fallback_used,
            "recommendation_reprompted": recommendation_reprompted,
            "graduation_grounding_required": requires_graduation_progress,
            "graduation_what_if_required": requires_graduation_what_if,
            "graduation_tool_reprompted": graduation_tool_reprompted,
            "graduation_what_if_reprompted": graduation_what_if_reprompted,
            "graduation_what_if_missing": missing_required_what_if,
            "graduation_reprompted": graduation_reprompted,
            "graduation_safe_fallback_used": graduation_safe_fallback_used,
            "policy_uncertainty_reprompted": policy_uncertainty_reprompted,
            "internal_output_reprompted": internal_output_reprompted,
            "internal_output_sanitized": internal_output_sanitized,
            "read_only": True,
            "portal_action": "student_manual_only",
        },
    }


def answer_student_advisor(**kwargs: Any) -> dict[str, Any]:
    """Feature-flagged seam used by the durable student conversation endpoint."""
    if is_enabled():
        return answer_student_advisor_v2(**kwargs)
    from core.services.virtual_advisor import answer_virtual_advisor

    return answer_virtual_advisor(**kwargs)


__all__ = [
    "FORBIDDEN_STUDENT_V2_TOOLS",
    "STUDENT_V2_TOOL_NAMES",
    "answer_student_advisor",
    "answer_student_advisor_v2",
    "execute_student_v2_tool",
    "is_enabled",
    "student_v2_tool_schemas",
]
